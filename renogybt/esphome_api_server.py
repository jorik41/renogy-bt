"""ESPHome Native API server for Bluetooth proxy functionality.

This module implements the ESPHome native API protocol to expose the Bluetooth
proxy functionality to Home Assistant's ESPHome integration.

Based on research and code patterns from:
- OHF-Voice/linux-voice-assistant
- peterkeen/aioesphomeserver
- ESPHome protocol documentation
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable, Optional

from aioesphomeapi._frame_helper.packets import make_plain_text_packets
from aioesphomeapi.api_pb2 import (  # type: ignore[attr-defined]
    BluetoothLEAdvertisementResponse,
    DeviceInfoRequest,
    DeviceInfoResponse,
    DisconnectRequest,
    DisconnectResponse,
    HelloRequest,
    HelloResponse,
    ListEntitiesDoneResponse,
    ListEntitiesRequest,
    PingRequest,
    PingResponse,
    SubscribeBluetoothLEAdvertisementsRequest,
)
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO
from google.protobuf import message

PROTO_TO_MESSAGE_TYPE = {v: k for k, v in MESSAGE_TYPE_TO_PROTO.items()}

logger = logging.getLogger(__name__)


class ESPHomeAPIProtocol(asyncio.Protocol):
    """ESPHome native API protocol handler."""

    def __init__(
        self,
        name: str,
        mac_address: str,
        version: str = "2024.12.0",
        on_advertisement_callback: Optional[Callable] = None,
    ):
        """Initialize the ESPHome API protocol.

        Args:
            name: Device name
            mac_address: Device MAC address
            version: ESPHome version
            on_advertisement_callback: Callback to register for BLE advertisements
        """
        self.name = name
        self.mac_address = mac_address
        self.version = version
        self._on_advertisement_callback = on_advertisement_callback
        self._subscribed_to_ble = False

        self._buffer: Optional[bytes] = None
        self._buffer_len: int = 0
        self._pos: int = 0
        self._transport = None
        self._writelines = None

    def connection_made(self, transport) -> None:
        """Handle new connection."""
        self._transport = transport
        self._writelines = transport.writelines
        peer = transport.get_extra_info("peername")
        logger.info("New ESPHome API connection from %s", peer)

    def connection_lost(self, exc) -> None:
        """Handle connection lost."""
        logger.info("ESPHome API connection closed")
        self._transport = None
        self._writelines = None
        self._subscribed_to_ble = False

    def data_received(self, data: bytes) -> None:
        """Process received data."""
        if self._buffer is None:
            self._buffer = data
            self._buffer_len = len(data)
        else:
            self._buffer += data
            self._buffer_len += len(data)

        while self._buffer_len >= 3:
            self._pos = 0

            # Read preamble (should be 0x00)
            if (preamble := self._read_varuint()) != 0x00:
                logger.error("Incorrect preamble: %s", preamble)
                return

            if (length := self._read_varuint()) == -1:
                logger.error("Incorrect length")
                return

            if (msg_type := self._read_varuint()) == -1:
                logger.error("Incorrect message type")
                return

            if length == 0:
                # Empty message
                self._remove_from_buffer()
                self._process_packet(msg_type, b"")
                continue

            if (packet_data := self._read(length)) is None:
                return

            self._remove_from_buffer()
            self._process_packet(msg_type, packet_data)

    def _process_packet(self, msg_type: int, packet_data: bytes) -> None:
        """Process a single packet."""
        try:
            msg_class = MESSAGE_TYPE_TO_PROTO[msg_type]
            msg_inst = msg_class.FromString(packet_data)
            self._handle_message(msg_inst)
        except Exception as e:
            logger.error("Error processing packet: %s", e, exc_info=True)

    def _handle_message(self, msg: message.Message) -> None:
        """Handle incoming message and send responses."""
        responses: list[message.Message] = []

        if isinstance(msg, HelloRequest):
            responses.append(
                HelloResponse(
                    api_version_major=1,
                    api_version_minor=10,
                    name=self.name,
                    server_info=f"renogybt-proxy/{self.version}",
                )
            )
        elif isinstance(msg, DisconnectRequest):
            responses.append(DisconnectResponse())
            if self._transport:
                self._transport.close()
        elif isinstance(msg, PingRequest):
            responses.append(PingResponse())
        elif isinstance(msg, DeviceInfoRequest):
            responses.append(
                DeviceInfoResponse(
                    uses_password=False,
                    name=self.name,
                    mac_address=self.mac_address,
                    esphome_version=self.version,
                    compilation_time="",
                    model="Linux Bluetooth Proxy",
                    manufacturer="RenogyBT",
                    has_deep_sleep=False,
                    project_name="renogybt",
                    project_version=self.version,
                    webserver_port=0,
                    bluetooth_proxy_feature_flags=1,  # Passive scanning support
                )
            )
        elif isinstance(msg, ListEntitiesRequest):
            # No entities to list for a pure Bluetooth proxy
            responses.append(ListEntitiesDoneResponse())
        elif isinstance(msg, SubscribeBluetoothLEAdvertisementsRequest):
            logger.info("Client subscribed to Bluetooth LE advertisements")
            self._subscribed_to_ble = True
            # Register callback to forward advertisements
            if self._on_advertisement_callback:
                self._on_advertisement_callback(self._send_ble_advertisement)

        if responses:
            self._send_messages(responses)

    def _send_ble_advertisement(self, advertisement_data: dict) -> None:
        """Send BLE advertisement to connected client.

        Args:
            advertisement_data: Dictionary with BLE advertisement data
                Expected keys: address, rssi, address_type, name,
                manufacturer_data, service_data, service_uuids
        """
        if not self._subscribed_to_ble or not self._writelines:
            return

        try:
            # Convert manufacturer_data dict to list of tuples
            manufacturer_data = []
            for company_id, data_bytes in advertisement_data.get(
                "manufacturer_data", {}
            ).items():
                if isinstance(data_bytes, bytes):
                    manufacturer_data.append((int(company_id), data_bytes))
                else:
                    manufacturer_data.append((int(company_id), bytes.fromhex(data_bytes)))

            # Convert service_data dict to list of tuples
            service_data = []
            for uuid_str, data_bytes in advertisement_data.get("service_data", {}).items():
                if isinstance(data_bytes, bytes):
                    service_data.append((uuid_str, data_bytes))
                else:
                    service_data.append((uuid_str, bytes.fromhex(data_bytes)))

            # Create advertisement response
            adv_response = BluetoothLEAdvertisementResponse(
                address=int(advertisement_data["address"].replace(":", ""), 16),
                rssi=advertisement_data["rssi"],
                address_type=1 if advertisement_data.get("address_type") == "random" else 0,
                name=advertisement_data.get("name", ""),
                service_uuids=advertisement_data.get("service_uuids", []),
                manufacturer_data=manufacturer_data,
                service_data=service_data,
            )

            self._send_messages([adv_response])
        except Exception as e:
            logger.error("Error sending BLE advertisement: %s", e, exc_info=True)

    def _send_messages(self, msgs: list[message.Message]) -> None:
        """Send messages to the client."""
        if not self._writelines:
            return

        try:
            packets = [
                (PROTO_TO_MESSAGE_TYPE[msg.__class__], msg.SerializeToString())
                for msg in msgs
            ]
            packet_bytes = make_plain_text_packets(packets)
            self._writelines(packet_bytes)
        except Exception as e:
            logger.error("Error sending messages: %s", e, exc_info=True)

    def _read(self, length: int) -> bytes | None:
        """Read exactly length bytes from buffer."""
        new_pos = self._pos + length
        if self._buffer_len < new_pos:
            return None
        original_pos = self._pos
        self._pos = new_pos
        if TYPE_CHECKING:
            assert self._buffer is not None
        return self._buffer[original_pos:new_pos]

    def _read_varuint(self) -> int:
        """Read a varuint from the buffer."""
        if not self._buffer:
            return -1

        result = 0
        bitpos = 0
        while self._buffer_len > self._pos:
            val = self._buffer[self._pos]
            self._pos += 1
            result |= (val & 0x7F) << bitpos
            if (val & 0x80) == 0:
                return result
            bitpos += 7
        return -1

    def _remove_from_buffer(self) -> None:
        """Remove processed data from buffer."""
        end_of_frame_pos = self._pos
        self._buffer_len -= end_of_frame_pos
        if self._buffer_len == 0:
            self._buffer = None
            return
        if TYPE_CHECKING:
            assert self._buffer is not None
        self._buffer = self._buffer[end_of_frame_pos:]


class ESPHomeAPIServer:
    """ESPHome native API server."""

    def __init__(
        self,
        name: str,
        mac_address: str,
        port: int = 6053,
        version: str = "2024.12.0",
    ):
        """Initialize the API server.

        Args:
            name: Device name
            mac_address: Device MAC address  
            port: Port to listen on (default: 6053)
            version: ESPHome version to advertise
        """
        self.name = name
        self.mac_address = mac_address
        self.port = port
        self.version = version
        self._server: Optional[asyncio.Server] = None
        self._advertisement_callback: Optional[Callable] = None

    def set_advertisement_callback(self, callback: Callable) -> None:
        """Set callback to be called when client subscribes to BLE advertisements.

        The callback will be called with a function that can be used to send
        advertisements to the client.
        """
        self._advertisement_callback = callback

    async def start(self) -> None:
        """Start the API server."""
        def protocol_factory():
            return ESPHomeAPIProtocol(
                self.name,
                self.mac_address,
                self.version,
                self._advertisement_callback,
            )

        self._server = await asyncio.get_running_loop().create_server(
            protocol_factory,
            host="0.0.0.0",
            port=self.port,
        )

        logger.info("ESPHome native API server started on port %d", self.port)

    async def stop(self) -> None:
        """Stop the API server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("ESPHome native API server stopped")


__all__ = ["ESPHomeAPIServer"]
