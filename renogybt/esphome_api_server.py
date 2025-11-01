"""ESPHome Native API server for Bluetooth proxy functionality."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, List, Optional, Type

from aioesphomeapi._frame_helper.packets import make_plain_text_packets
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
    NoiseEncryptionSetKeyRequest,
    NoiseEncryptionSetKeyResponse,
    PingRequest,
    PingResponse,
    SubscribeBluetoothConnectionsFreeRequest,
    SubscribeBluetoothLEAdvertisementsRequest,
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


class ESPHomeAPIProtocol(asyncio.Protocol):
    """ESPHome native API protocol handler."""

    def __init__(
        self,
        name: str,
        mac_address: str,
        version: str = "2024.12.0",
        on_subscribe_callback: Optional[Callable[[Callable[[dict], None]], None]] = None,
    ) -> None:
        self.name = name
        self.mac_address = mac_address
        self.version = version
        self._on_subscribe_callback = on_subscribe_callback
        self._subscribed_to_ble = False
        self._subscribed_to_connections_free = False
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

    def data_received(self, data: bytes) -> None:
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

            if length == 0:
                self._remove_from_buffer()
                self._process_packet(msg_type, b"")
                continue

            packet = self._read(length)
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
            responses.append(
                HelloResponse(
                    api_version_major=1,
                    api_version_minor=13,
                    name=self.name,
                    server_info=f"renogybt-proxy/{self.version}",
                )
            )
        elif isinstance(message, AuthenticationRequest):
            responses.append(AuthenticationResponse())
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
                    project_name="renogybt",
                    project_version=self.version,
                    webserver_port=0,
                    bluetooth_proxy_feature_flags=BLUETOOTH_PROXY_FEATURES,
                    bluetooth_mac_address=self.mac_address,
                    api_encryption_supported=False,
                )
            )
        elif isinstance(message, ListEntitiesRequest):
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
        if not self._subscribed_to_ble or not self._writelines:
            return

        try:
            # Convert the advertisement to raw format
            address = int(advertisement["address"].replace(":", ""), 16)
            rssi = int(advertisement.get("rssi", 0))
            address_type = 1 if advertisement.get("address_type") == "random" else 0
            
            # Build raw BLE advertisement data
            # This should include the complete advertisement packet data
            # For now, we'll create a minimal valid packet
            raw_data = bytearray()
            
            # Add name if present (AD Type 0x09 = Complete Local Name)
            name_value = advertisement.get("name", b"")
            if isinstance(name_value, str):
                name_value = name_value.encode()
            if name_value:
                raw_data.append(len(name_value) + 1)  # Length
                raw_data.append(0x09)  # AD Type: Complete Local Name
                raw_data.extend(name_value)
            
            # Add manufacturer data if present (AD Type 0xFF)
            manufacturer_data = advertisement.get("manufacturer_data", {})
            for company_id, data in manufacturer_data.items():
                company_id_int = int(company_id)
                if isinstance(data, str):
                    data = bytes.fromhex(data)
                raw_data.append(len(data) + 3)  # Length (data + 1 type + 2 company ID)
                raw_data.append(0xFF)  # AD Type: Manufacturer Specific Data
                raw_data.append(company_id_int & 0xFF)  # Company ID (little-endian)
                raw_data.append((company_id_int >> 8) & 0xFF)
                raw_data.extend(data)
            
            # Add service data if present (AD Type 0x16 for 16-bit UUIDs)
            service_data = advertisement.get("service_data", {})
            for uuid_str, data in service_data.items():
                if isinstance(data, str):
                    data = bytes.fromhex(data)
                # For 16-bit UUIDs (4-character hex strings)
                if len(uuid_str) == 4:
                    uuid_bytes = bytes.fromhex(uuid_str)[::-1]  # Reverse for little-endian
                    raw_data.append(len(data) + 3)  # Length
                    raw_data.append(0x16)  # AD Type: Service Data - 16-bit UUID
                    raw_data.extend(uuid_bytes)
                    raw_data.extend(data)
                else:
                    # Log warning for unsupported UUID formats
                    logger.debug("Skipping service data for unsupported UUID format (128-bit): %s", uuid_str)
            
            # Add service UUIDs if present (AD Type 0x03 for complete 16-bit UUIDs)
            service_uuids = advertisement.get("service_uuids", [])
            if service_uuids:
                # Group 16-bit UUIDs (represented as 4-character hex strings)
                uuid_16_list = [u for u in service_uuids if len(u) == 4]
                if uuid_16_list:
                    uuid_bytes = bytearray()
                    for uuid_str in uuid_16_list:
                        # Convert UUID to 16-bit little-endian
                        uuid_bytes.extend(bytes.fromhex(uuid_str)[::-1])
                    if uuid_bytes:
                        raw_data.append(len(uuid_bytes) + 1)
                        raw_data.append(0x03)  # Complete list of 16-bit UUIDs
                        raw_data.extend(uuid_bytes)
                
                # Log warning for unsupported UUID formats
                unsupported_uuids = [u for u in service_uuids if len(u) != 4]
                if unsupported_uuids:
                    logger.debug("Skipping unsupported UUID formats (128-bit): %s", unsupported_uuids)
            
            # Create raw advertisement message
            raw_adv = BluetoothLERawAdvertisement(
                address=address,
                rssi=rssi,
                address_type=address_type,
                data=bytes(raw_data),
            )
            
            # Send as a batch (even though it's just one advertisement)
            bluetooth_response = BluetoothLERawAdvertisementsResponse(
                advertisements=[raw_adv]
            )
            self._send_messages([bluetooth_response])
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Failed to serialise BLE advertisement: %s", exc, exc_info=True)

    def _send_messages(self, messages: List[Message]) -> None:
        if not self._writelines:
            return

        try:
            packets = [
                (PROTO_TO_MESSAGE_TYPE[msg.__class__], msg.SerializeToString())
                for msg in messages
            ]
            for msg in messages:
                logger.debug("Sending ESPHome message: %s", msg.__class__.__name__)
            payloads = make_plain_text_packets(packets)
            for payload in payloads:
                logger.debug("ESPHome packet bytes (len=%d): %s", len(payload), payload.hex())
                self._transport.write(payload)  # type: ignore[union-attr]
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

    def set_advertisement_callback(self, callback: Callable[[Callable[[dict], None]], None]) -> None:
        self._advertisement_callback = callback

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        def factory() -> ESPHomeAPIProtocol:
            return ESPHomeAPIProtocol(
                self.name,
                self.mac_address,
                self.version,
                self._advertisement_callback,
            )

        self._server = await loop.create_server(factory, host="0.0.0.0", port=self.port)
        logger.info("ESPHome native API server listening on %d", self.port)

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
