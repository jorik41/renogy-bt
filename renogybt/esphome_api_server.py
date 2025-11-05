"""ESPHome Native API server for Bluetooth proxy functionality."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Dict, List, Optional, Type

from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]
    AuthenticationRequest,
    AuthenticationResponse,
    BluetoothConnectionsFreeResponse,
    BluetoothLEAdvertisementResponse,
    BluetoothLERawAdvertisement,
    BluetoothLERawAdvertisementsResponse,
    BluetoothScannerMode,
    BluetoothScannerSetModeRequest,
    BluetoothScannerState,
    BluetoothScannerStateResponse,
    DeviceInfoRequest,
    DeviceInfoResponse,
    DisconnectRequest,
    DisconnectResponse,
    HelloRequest,
    HelloResponse,
    ListEntitiesDoneResponse,
    ListEntitiesRequest,
    ListEntitiesSensorResponse,
    NoiseEncryptionSetKeyRequest,
    NoiseEncryptionSetKeyResponse,
    PingRequest,
    PingResponse,
    SensorStateClass,
    SensorStateResponse,
    SubscribeBluetoothConnectionsFreeRequest,
    SubscribeBluetoothLEAdvertisementsRequest,
    SubscribeStatesRequest,
    UnsubscribeBluetoothLEAdvertisementsRequest,
)
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO
from google.protobuf.message import Message

# Bluetooth proxy feature flags (based on ESPHome bluetooth_proxy component)
BLUETOOTH_PROXY_FEATURE_PASSIVE_SCAN = 1 << 0
BLUETOOTH_PROXY_FEATURE_ACTIVE_CONNECTIONS = 1 << 1
BLUETOOTH_PROXY_FEATURE_REMOTE_CACHING = 1 << 2
BLUETOOTH_PROXY_FEATURE_PAIRING = 1 << 3
BLUETOOTH_PROXY_FEATURE_CACHE_CLEARING = 1 << 4
BLUETOOTH_PROXY_FEATURE_RAW_ADVERTISEMENTS = 1 << 5
BLUETOOTH_PROXY_FEATURE_STATE_AND_MODE = 1 << 6

PROJECT_NAME = "renogybt.bluetooth_proxy"

# We support passive scan, raw advertisements, and state/mode reporting
# but not active connections, caching, pairing, or cache clearing
BLUETOOTH_PROXY_FEATURES = (
    BLUETOOTH_PROXY_FEATURE_PASSIVE_SCAN
    | BLUETOOTH_PROXY_FEATURE_RAW_ADVERTISEMENTS
    | BLUETOOTH_PROXY_FEATURE_STATE_AND_MODE
)

# Connection limits - we don't support active connections
BLUETOOTH_PROXY_MAX_CONNECTIONS = 0

PROTO_TO_MESSAGE_TYPE = {v: k for k, v in MESSAGE_TYPE_TO_PROTO.items()}

logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.addHandler(logging.NullHandler())
logger.setLevel(logging.DEBUG)

def _encode_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)

def _make_packet(msg_type: int, payload: bytes) -> bytes:
    """Create a single ESPHome API packet.
    
    In the modern ESPHome protocol (aioesphomeapi 42.x+), the length field
    represents ONLY the payload size, not including the msg_type varint.
    """
    msg_type_bytes = _encode_varint(msg_type)
    length = len(payload)  # Only payload size, not including msg_type
    packet = bytearray([0x00])  # preamble
    packet.extend(_encode_varint(length))
    packet.extend(msg_type_bytes)
    packet.extend(payload)
    return bytes(packet)

class ESPHomeAPIProtocol(asyncio.Protocol):
    """ESPHome native API protocol handler."""

    def __init__(
        self,
        name: str,
        mac_address: str,
        version: str = "2024.12.0",
        on_subscribe_callback: Optional[Callable[[Callable[[dict], None]], None]] = None,
        sensor_entities: Optional[Dict[str, Dict]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ) -> None:
        self.name = name
        self.mac_address = mac_address
        self.version = version
        self._on_subscribe_callback = on_subscribe_callback
        self._sensor_entities = sensor_entities or {}
        self._on_disconnect = on_disconnect
        self._subscribed_to_ble = False
        self._subscribed_to_connections_free = False
        self._subscribed_to_states = False
        self._scanner_mode = BluetoothScannerMode.BLUETOOTH_SCANNER_MODE_PASSIVE
        self._scanner_state = BluetoothScannerState.BLUETOOTH_SCANNER_STATE_IDLE
        self._buffer: Optional[bytes] = None
        self._buffer_len = 0
        self._pos = 0
        self._transport: Optional[asyncio.Transport] = None
        self._writelines: Optional[Callable[[List[bytes]], None]] = None
        self._close_after_send = False

    # asyncio.Protocol API -------------------------------------------------

    def connection_made(self, transport: asyncio.Transport) -> None:
        self._transport = transport
        self._writelines = transport.writelines  # type: ignore[attr-defined]
        peer = transport.get_extra_info("peername")
        logger.info("ESPHome API connection from %s", peer)

    def connection_lost(self, exc: Optional[BaseException]) -> None:
        logger.info("ESPHome API connection closed")
        self._transport = None
        self._writelines = None
        self._subscribed_to_ble = False
        self._subscribed_to_connections_free = False
        self._subscribed_to_states = False
        if self._on_disconnect:
            self._on_disconnect()

    def data_received(self, data: bytes) -> None:
        logger.debug("ESPHome API raw bytes received len=%d: %s", len(data), data[:20].hex())
        if self._buffer is None:
            self._buffer = data
            self._buffer_len = len(data)
        else:
            self._buffer += data
            self._buffer_len += len(data)

        while self._buffer_len >= 3:
            self._pos = 0

            preamble = self._read_varuint()
            if preamble != 0x00:
                logger.error("Invalid ESPHome preamble %s; closing connection", preamble)
                self._reset_buffer()
                self._close_transport()
                return

            length = self._read_varuint()
            if length == -1:
                logger.error("Failed to read length; closing connection")
                self._reset_buffer()
                self._close_transport()
                return

            msg_type = self._read_varuint()
            if msg_type == -1:
                logger.error("Failed to read message type; closing connection")
                self._reset_buffer()
                self._close_transport()
                return

            # In the modern ESPHome protocol (aioesphomeapi 42.x+), the length field
            # represents ONLY the payload size, not including the msg_type varint.
            # This is different from older versions where length = msg_type_size + payload_size.
            if length == 0:
                self._remove_from_buffer()
                self._process_packet(msg_type, b"")
                continue
            
            payload_len = length
            packet = self._read(payload_len)
            if packet is None:
                return  # Wait for the rest of the packet

            self._remove_from_buffer()
            self._process_packet(msg_type, packet)

    # Message handling -----------------------------------------------------

    def _process_packet(self, msg_type: int, payload: bytes) -> None:
        try:
            msg_class: Type[Message] = MESSAGE_TYPE_TO_PROTO[msg_type]
            message = msg_class.FromString(payload)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Error decoding ESPHome message %s: %s", msg_type, exc, exc_info=True)
            return

        logger.debug("Received ESPHome message: %s", msg_class.__name__)
        self._handle_message(message)

    def _handle_message(self, message: Message) -> None:
        responses: List[Message] = []

        if isinstance(message, HelloRequest):
            logger.info(
                "ESPHome Hello from %s (api %s.%s, encryption=%s)",
                message.client_info,
                message.api_version_major,
                message.api_version_minor,
                getattr(message, "supports_encryption", False),
            )
            responses.append(
                HelloResponse(
                    api_version_major=1,
                    api_version_minor=12,
                    name=self.name,
                    server_info=f"renogybt-proxy/{self.version}",
                )
            )
        elif isinstance(message, AuthenticationRequest):
            responses.append(AuthenticationResponse(invalid_password=False))
            logger.info("ESPHome client authenticated (no password)")
        elif isinstance(message, DisconnectRequest):
            responses.append(DisconnectResponse())
            self._close_after_send = True
        elif isinstance(message, PingRequest):
            responses.append(PingResponse())
        elif isinstance(message, DeviceInfoRequest):
            responses.append(
                DeviceInfoResponse(
                    uses_password=False,
                    name=self.name,
                    mac_address=self.mac_address,
                    esphome_version=self.version,
                    compilation_time="",
                    model="ESPHome Bluetooth Proxy",
                    manufacturer="ESPHome",
                    has_deep_sleep=False,
                    project_name=PROJECT_NAME,
                    project_version=self.version,
                    webserver_port=0,
                    bluetooth_proxy_feature_flags=BLUETOOTH_PROXY_FEATURES,
                    bluetooth_mac_address=self.mac_address,
                    api_encryption_supported=False,
                )
            )
        elif isinstance(message, ListEntitiesRequest):
            # Send sensor entity definitions
            for key, entity_info in self._sensor_entities.items():
                sensor_response = ListEntitiesSensorResponse(
                    object_id=entity_info.get('object_id', key),
                    key=entity_info['key'],
                    name=entity_info.get('name', key),
                    icon=entity_info.get('icon', ''),
                    unit_of_measurement=entity_info.get('unit_of_measurement', ''),
                    accuracy_decimals=entity_info.get('accuracy_decimals', 2),
                    force_update=entity_info.get('force_update', False),
                    device_class=entity_info.get('device_class', ''),
                    state_class=entity_info.get('state_class', SensorStateClass.SENSOR_STATE_CLASS_MEASUREMENT),
                    disabled_by_default=entity_info.get('disabled_by_default', False),
                )
                responses.append(sensor_response)
            responses.append(ListEntitiesDoneResponse())
        elif isinstance(message, SubscribeBluetoothLEAdvertisementsRequest):
            logger.info("ESPHome client subscribed to BLE advertisements")
            self._subscribed_to_ble = True
            self._scanner_state = BluetoothScannerState.BLUETOOTH_SCANNER_STATE_RUNNING
            # Send scanner state response
            responses.append(
                BluetoothScannerStateResponse(
                    state=self._scanner_state,
                    mode=self._scanner_mode,
                    configured_mode=self._scanner_mode,
                )
            )
            if self._on_subscribe_callback:
                self._on_subscribe_callback(self._send_ble_advertisement)
        elif isinstance(message, UnsubscribeBluetoothLEAdvertisementsRequest):
            logger.info("ESPHome client unsubscribed from BLE advertisements")
            self._subscribed_to_ble = False
            self._scanner_state = BluetoothScannerState.BLUETOOTH_SCANNER_STATE_IDLE
            # Send scanner state response
            responses.append(
                BluetoothScannerStateResponse(
                    state=self._scanner_state,
                    mode=self._scanner_mode,
                    configured_mode=self._scanner_mode,
                )
            )
        elif isinstance(message, SubscribeBluetoothConnectionsFreeRequest):
            logger.info("ESPHome client subscribed to connections free updates")
            self._subscribed_to_connections_free = True
            # Send initial connections free response
            # We don't support active connections, so report 0 free/limit
            responses.append(
                BluetoothConnectionsFreeResponse(
                    free=BLUETOOTH_PROXY_MAX_CONNECTIONS,
                    limit=BLUETOOTH_PROXY_MAX_CONNECTIONS,
                )
            )
            # Send scanner state to advertise Bluetooth capability to Home Assistant
            # Set scanner to RUNNING if it's currently IDLE (initial state).
            # If already RUNNING (from a BLE subscription), keep it RUNNING.
            # This ensures HA knows the proxy has an active scanner during connection handshake.
            if self._scanner_state == BluetoothScannerState.BLUETOOTH_SCANNER_STATE_IDLE:
                self._scanner_state = BluetoothScannerState.BLUETOOTH_SCANNER_STATE_RUNNING
            # Always send the current state so HA knows scanner is available
            responses.append(
                BluetoothScannerStateResponse(
                    state=self._scanner_state,
                    mode=self._scanner_mode,
                    configured_mode=self._scanner_mode,
                )
            )
        elif isinstance(message, BluetoothScannerSetModeRequest):
            # Handle scanner mode changes (active/passive)
            logger.info("ESPHome client requested scanner mode change to %s", message.mode)
            self._scanner_mode = message.mode
            # Send scanner state response with new mode
            responses.append(
                BluetoothScannerStateResponse(
                    state=self._scanner_state,
                    mode=self._scanner_mode,
                    configured_mode=BluetoothScannerMode.BLUETOOTH_SCANNER_MODE_PASSIVE,
                )
            )
        elif isinstance(message, SubscribeStatesRequest):
            logger.info("ESPHome client subscribed to sensor states")
            self._subscribed_to_states = True
        elif isinstance(message, NoiseEncryptionSetKeyRequest):
            logger.info("ESPHome client attempted to set Noise key; rejecting")
            responses.append(NoiseEncryptionSetKeyResponse(success=False))

        if responses:
            self._send_messages(responses)
        if self._close_after_send:
            self._close_after_send = False
            self._close_transport()

    # Helpers --------------------------------------------------------------

    def _send_ble_advertisement(self, advertisement: dict) -> None:
        # Only send if there is an active connection transport and a client is subscribed.
        if not self._subscribed_to_ble or not self._transport:
            return

        try:
            address = int(advertisement["address"].replace(":", ""), 16)
            rssi = int(advertisement.get("rssi", 0))
            address_type = 1 if advertisement.get("address_type") == "random" else 0
            manufacturer_data = advertisement.get("manufacturer_data", {}) or {}
            service_data = advertisement.get("service_data", {}) or {}
            service_uuids = advertisement.get("service_uuids", []) or []
            name_field = advertisement.get("name", "") or ""
            if isinstance(name_field, (bytes, bytearray)):
                name_bytes = bytes(name_field)
                name_str = name_bytes.decode("utf-8", errors="ignore")
            else:
                name_str = str(name_field)
                name_bytes = name_str.encode("utf-8", errors="ignore")

            normalized_manufacturer: Dict[int, bytes] = {}
            for key, value in manufacturer_data.items():
                try:
                    if isinstance(key, (bytes, bytearray)):
                        company_int = int.from_bytes(key, "little")
                    else:
                        company_int = int(key)
                except (TypeError, ValueError):
                    logger.debug("Skipping manufacturer key with unexpected type: %r", key)
                    continue
                if isinstance(value, str):
                    normalized_manufacturer[company_int] = bytes.fromhex(value)
                elif isinstance(value, (bytes, bytearray)):
                    normalized_manufacturer[company_int] = bytes(value)
                else:
                    normalized_manufacturer[company_int] = bytes(value or b"")
            manufacturer_data = normalized_manufacturer

            normalized_service: Dict[str, bytes] = {}
            for key, value in service_data.items():
                if isinstance(value, str):
                    normalized_service[key] = bytes.fromhex(value)
                elif isinstance(value, (bytes, bytearray)):
                    normalized_service[key] = bytes(value)
                else:
                    normalized_service[key] = bytes(value or b"")
            service_data = normalized_service

            raw_segments: List[bytes] = []

            def add_segment(ad_type: int, payload: bytes) -> None:
                if not payload:
                    return
                length = len(payload) + 1
                if length > 255:
                    logger.debug(
                        "Skipping AD type %s due to payload length %s", ad_type, length
                    )
                    return
                raw_segments.append(bytes((length, ad_type)) + payload)

            flags = advertisement.get("flags")
            if isinstance(flags, int):
                add_segment(0x01, bytes([flags & 0xFF]))
            else:
                add_segment(0x01, b"\x06")

            if name_bytes:
                add_segment(0x09, name_bytes)

            for company_id, data_bytes in manufacturer_data.items():
                try:
                    company_int = int(company_id)
                except (TypeError, ValueError):
                    logger.debug(
                        "Skipping manufacturer data with unexpected key %r", company_id
                    )
                    continue
                payload = bytes(
                    (company_int & 0xFF, (company_int >> 8) & 0xFF)
                ) + data_bytes
                add_segment(0xFF, payload)

            for uuid_str, data_bytes in service_data.items():
                normalized_uuid = uuid_str.replace("-", "")
                if len(normalized_uuid) == 4:
                    add_segment(
                        0x16, bytes.fromhex(normalized_uuid)[::-1] + data_bytes
                    )
                elif len(normalized_uuid) == 8:
                    add_segment(
                        0x20, bytes.fromhex(normalized_uuid)[::-1] + data_bytes
                    )
                elif len(normalized_uuid) == 32:
                    add_segment(
                        0x21, bytes.fromhex(normalized_uuid)[::-1] + data_bytes
                    )
                else:
                    logger.debug(
                        "Skipping service data for unsupported UUID %s", uuid_str
                    )

            uuid_16_bytes = []
            uuid_32_bytes = []
            uuid_128_bytes = []
            for uuid_str in service_uuids:
                normalized_uuid = uuid_str.replace("-", "")
                if len(normalized_uuid) == 4:
                    uuid_16_bytes.append(bytes.fromhex(normalized_uuid)[::-1])
                elif len(normalized_uuid) == 8:
                    uuid_32_bytes.append(bytes.fromhex(normalized_uuid)[::-1])
                elif len(normalized_uuid) == 32:
                    uuid_128_bytes.append(bytes.fromhex(normalized_uuid)[::-1])
                else:
                    logger.debug(
                        "Skipping service UUID with unsupported format: %s", uuid_str
                    )
            if uuid_16_bytes:
                add_segment(0x03, b"".join(uuid_16_bytes))
            if uuid_32_bytes:
                add_segment(0x05, b"".join(uuid_32_bytes))
            if uuid_128_bytes:
                add_segment(0x07, b"".join(uuid_128_bytes))

            tx_power = advertisement.get("tx_power")
            if isinstance(tx_power, int):
                add_segment(0x0A, bytes([tx_power & 0xFF]))

            raw_adv = BluetoothLERawAdvertisement(
                address=address,
                rssi=rssi,
                address_type=address_type,
                data=b"".join(raw_segments),
            )

            legacy_adv = BluetoothLEAdvertisementResponse(
                address=address,
                rssi=rssi,
                address_type=address_type,
                name=name_bytes,
                service_uuids=list(service_uuids),
            )
            for uuid_str, data_bytes in service_data.items():
                entry = legacy_adv.service_data.add()
                entry.uuid = uuid_str
                entry.data = data_bytes
            for company_id, data_bytes in manufacturer_data.items():
                try:
                    company_int = int(company_id)
                except (TypeError, ValueError):
                    logger.debug(
                        "Skipping manufacturer data entry with unexpected key %r",
                        company_id,
                    )
                    continue
                entry = legacy_adv.manufacturer_data.add()
                entry.uuid = str(company_int)
                entry.data = data_bytes
            raw_response = BluetoothLERawAdvertisementsResponse(
                advertisements=[raw_adv]
            )

            self._send_messages([legacy_adv, raw_response])
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Failed to serialise BLE advertisement: %s", exc, exc_info=True)

    def send_sensor_states(self, sensor_data: Dict[str, float]) -> None:
        """Send sensor state updates to subscribed clients."""
        if not self._subscribed_to_states or not self._transport:
            return

        try:
            responses = []
            for data_key, value in sensor_data.items():
                # Find the corresponding sensor entity
                for entity_key, entity_info in self._sensor_entities.items():
                    if entity_info.get('data_key', entity_key) == data_key:
                        sensor_state = SensorStateResponse(
                            key=entity_info['key'],
                            state=float(value),
                            missing_state=False,
                        )
                        responses.append(sensor_state)
                        break
            
            if responses:
                self._send_messages(responses)
                logger.debug("Sent %d sensor state updates", len(responses))
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Failed to send sensor states: %s", exc, exc_info=True)

    def _send_messages(self, messages: List[Message]) -> None:
        # Guard on transport instead of writelines since we use write() below.
        if not self._transport:
            return

        try:
            packets = [
                (PROTO_TO_MESSAGE_TYPE[msg.__class__], msg.SerializeToString())
                for msg in messages
            ]
            for msg in messages:
                logger.debug("Sending ESPHome message: %s", msg.__class__.__name__)
            for msg_type, payload in packets:
                packet = _make_packet(msg_type, payload)
                logger.debug("ESPHome packet bytes (len=%d): %s", len(payload), payload.hex())
                self._transport.write(packet)  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Failed to send ESPHome messages: %s", exc, exc_info=True)

    def _read(self, length: int) -> Optional[bytes]:
        new_pos = self._pos + length
        if self._buffer_len < new_pos:
            return None
        assert self._buffer is not None
        data = self._buffer[self._pos:new_pos]
        self._pos = new_pos
        return data

    def _read_varuint(self) -> int:
        if not self._buffer:
            return -1
        result = 0
        shift = 0
        while self._buffer_len > self._pos:
            byte = self._buffer[self._pos]
            self._pos += 1
            result |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                return result
            shift += 7
        return -1

    def _remove_from_buffer(self) -> None:
        end_pos = self._pos
        self._buffer_len -= end_pos
        if self._buffer_len == 0:
            self._buffer = None
            return
        assert self._buffer is not None
        self._buffer = self._buffer[end_pos:]

    def _reset_buffer(self) -> None:
        self._buffer = None
        self._buffer_len = 0
        self._pos = 0

    def _close_transport(self) -> None:
        if self._transport:
            self._transport.close()

class ESPHomeAPIServer:
    """Small wrapper that exposes ESPHomeAPIProtocol on a TCP port."""

    def __init__(
        self,
        name: str,
        mac_address: str,
        port: int = 6053,
        version: str = "2024.12.0",
    ) -> None:
        self.name = name
        self.mac_address = mac_address
        self.port = port
        self.version = version
        self._server: Optional[asyncio.base_events.Server] = None
        self._advertisement_callback: Optional[Callable[[Callable[[dict], None]], None]] = None
        self._sensor_entities: Dict[str, Dict] = {}
        self._active_protocols: List[ESPHomeAPIProtocol] = []

    def set_advertisement_callback(self, callback: Callable[[Callable[[dict], None]], None]) -> None:
        self._advertisement_callback = callback

    def set_sensor_entities(self, entities: Dict[str, Dict]) -> None:
        """Define sensor entities to expose via the ESPHome API."""
        self._sensor_entities = entities
        logger.info("Configured %d sensor entities", len(entities))

    def send_sensor_states(self, sensor_data: Dict[str, float]) -> None:
        """Send sensor state updates to all connected clients."""
        for protocol in self._active_protocols:
            protocol.send_sensor_states(sensor_data)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        def factory() -> ESPHomeAPIProtocol:
            protocol = ESPHomeAPIProtocol(
                self.name,
                self.mac_address,
                self.version,
                self._advertisement_callback,
                self._sensor_entities,
                on_disconnect=lambda: self._remove_protocol(protocol),
            )
            self._active_protocols.append(protocol)
            return protocol

        self._server = await loop.create_server(factory, host="0.0.0.0", port=self.port)
        logger.info("ESPHome native API server listening on %d", self.port)

    def _remove_protocol(self, protocol: ESPHomeAPIProtocol) -> None:
        """Remove a disconnected protocol from the active list."""
        if protocol in self._active_protocols:
            self._active_protocols.remove(protocol)

    async def stop(self) -> None:
        if not self._server:
            return
        self._server.close()
        await self._server.wait_closed()
        logger.info("ESPHome native API server stopped")


__all__ = [
    "ESPHomeAPIServer",
    "ESPHomeAPIProtocol",
    "BLUETOOTH_PROXY_FEATURE_PASSIVE_SCAN",
    "BLUETOOTH_PROXY_FEATURE_RAW_ADVERTISEMENTS",
    "BLUETOOTH_PROXY_FEATURE_STATE_AND_MODE",
    "BLUETOOTH_PROXY_FEATURES",
]
