"""ESPHome-compatible Home Assistant Bluetooth proxy server.

This module implements enough of the ESPHome native API to let Home Assistant
discover this application as a Bluetooth proxy while we continue to keep the
Renogy BLE session alive.  The implementation focuses on the features Home
Assistant relies on for Bluetooth proxies:

* Handshake, device info and ping handling.
* Advertisement forwarding with the legacy (non-batched) payload.
* Remote GATT connections (connect/disconnect/read/write/notify/descriptors).
* Scanner state tracking and connection limits.

The server is intentionally conservative in scope – it does not attempt to
implement every ESPHome API endpoint, only those required for Bluetooth proxy
operation.  Unsupported requests are logged for visibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Optional, Set

from bleak import AdvertisementData, BLEDevice, BleakClient, BleakScanner
from bleak.exc import BleakError
from google.protobuf.message import Message
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from aioesphomeapi.api_pb2 import (
    AuthenticationRequest,
    AuthenticationResponse,
    BluetoothConnectionsFreeResponse,
    BluetoothDeviceClearCacheResponse,
    BluetoothDeviceConnectionResponse,
    BluetoothDevicePairingResponse,
    BluetoothDeviceRequest,
    BluetoothDeviceUnpairingResponse,
    BluetoothGATTDescriptor,
    BluetoothGATTErrorResponse,
    BluetoothGATTGetServicesDoneResponse,
    BluetoothGATTGetServicesRequest,
    BluetoothGATTGetServicesResponse,
    BluetoothGATTNotifyDataResponse,
    BluetoothGATTNotifyRequest,
    BluetoothGATTNotifyResponse,
    BluetoothGATTReadDescriptorRequest,
    BluetoothGATTReadRequest,
    BluetoothGATTReadResponse,
    BluetoothGATTService,
    BluetoothGATTCharacteristic,
    BluetoothGATTWriteDescriptorRequest,
    BluetoothGATTWriteRequest,
    BluetoothGATTWriteResponse,
    BluetoothLEAdvertisementResponse,
    BluetoothScannerMode,
    BluetoothScannerSetModeRequest,
    BluetoothScannerState,
    BluetoothScannerStateResponse,
    BluetoothServiceData,
    DeviceInfoRequest,
    DeviceInfoResponse,
    DisconnectRequest,
    HelloRequest,
    HelloResponse,
    ListEntitiesDoneResponse,
    ListEntitiesRequest,
    PingRequest,
    PingResponse,
    SubscribeBluetoothConnectionsFreeRequest,
    SubscribeBluetoothLEAdvertisementsRequest,
    UnsubscribeBluetoothLEAdvertisementsRequest,
)
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO, to_human_readable_address
from aioesphomeapi.model import BluetoothProxyFeature, BluetoothProxySubscriptionFlag

CREATE_TASK = getattr(asyncio, "create_task", asyncio.ensure_future)

LOGGER = logging.getLogger(__name__)

MDNS_SERVICE_TYPE = "_esphomelib._tcp.local."
DEFAULT_API_VERSION_MAJOR = 1
DEFAULT_API_VERSION_MINOR = 13

PROTO_CLASS_TO_ID = {cls: msg_type for msg_type, cls in MESSAGE_TYPE_TO_PROTO.items()}


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _encode_varint(value: int) -> bytes:
    """Encode an integer to protobuf-style varint bytes."""
    out = bytearray()
    remaining = value
    while True:
        to_write = remaining & 0x7F
        remaining >>= 7
        if remaining:
            out.append(to_write | 0x80)
        else:
            out.append(to_write)
            break
    return bytes(out)


async def _read_varint(reader: asyncio.StreamReader) -> int:
    """Decode a protobuf varint from the stream."""
    shift = 0
    result = 0
    while True:
        chunk = await reader.readexactly(1)
        byte = chunk[0]
        result |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            return result
        shift += 7
        if shift > 64:
            raise ValueError("varint too long")


async def _read_message(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Read a framed ESPHome API message."""
    preamble = await _read_varint(reader)
    if preamble != 0x00:
        raise ValueError(f"Invalid preamble 0x{preamble:02x}")
    length = await _read_varint(reader)
    msg_type = await _read_varint(reader)
    payload = await reader.readexactly(length) if length else b""
    return msg_type, payload


async def _write_message(
    writer: asyncio.StreamWriter,
    message: Message,
    *,
    lock: asyncio.Lock,
) -> None:
    """Serialise a protobuf message and send it over the stream."""
    msg_type = PROTO_CLASS_TO_ID[type(message)]
    payload = message.SerializeToString()
    frame = (
        _encode_varint(0x00)
        + _encode_varint(len(payload))
        + _encode_varint(msg_type)
        + payload
    )
    async with lock:
        writer.write(frame)
        await writer.drain()


def _mac_to_int(mac: str) -> int:
    """Convert AA:BB:CC:DD:EE:FF to 64-bit integer form."""
    return int(mac.replace(":", "").replace("-", ""), 16)


def _normalise_mac(mac: str) -> str:
    return mac.upper()


def _split_uuid(uuid_str: str) -> tuple[int, int]:
    """Split a UUID string into high/low 64-bit integers."""
    value = int(uuid_str.replace("-", ""), 16)
    high = (value >> 64) & 0xFFFFFFFFFFFFFFFF
    low = value & 0xFFFFFFFFFFFFFFFF
    return high, low


def _read_adapter_mac(adapter: Optional[str]) -> Optional[str]:
    """Attempt to read the Bluetooth adapter MAC address."""
    if not adapter:
        adapter = "hci0"
    sysfs_path = Path(f"/sys/class/bluetooth/{adapter}/address")
    try:
        return sysfs_path.read_text().strip().upper()
    except FileNotFoundError:
        LOGGER.warning("Unable to read adapter address from %s", sysfs_path)
    return None


def _guess_primary_ip() -> str:
    """Best effort attempt to determine the host IP for mDNS."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _derive_device_info_response(
    *,
    name: str,
    friendly_name: Optional[str],
    adapter_mac: Optional[str],
    project_name: Optional[str],
    project_version: Optional[str],
    manufacturer: Optional[str],
    model: Optional[str],
    suggested_area: Optional[str],
    feature_flags: BluetoothProxyFeature,
) -> DeviceInfoResponse:
    """Generate a DeviceInfoResponse message for the proxy."""
    resp = DeviceInfoResponse()
    resp.uses_password = False
    resp.name = name
    resp.esphome_version = "renogy-bt-proxy 1.0"
    if adapter_mac:
        resp.mac_address = adapter_mac
        resp.bluetooth_mac_address = adapter_mac
    if friendly_name:
        resp.friendly_name = friendly_name
    if project_name:
        resp.project_name = project_name
    if project_version:
        resp.project_version = project_version
    if manufacturer:
        resp.manufacturer = manufacturer
    if model:
        resp.model = model
    if suggested_area:
        resp.suggested_area = suggested_area
    resp.bluetooth_proxy_feature_flags = int(feature_flags)
    resp.api_encryption_supported = False
    return resp


# ---------------------------------------------------------------------------
# Peripheral connection management
# ---------------------------------------------------------------------------

@dataclass
class _PeripheralNotification:
    callbacks: Dict["ProxyClient", callable] = field(default_factory=dict)
    callback_registered: bool = False


class PeripheralConnection:
    """Manages a single BLE peripheral controlled by Home Assistant."""

    def __init__(self, address: str, *, adapter: Optional[str]):
        self.address = address
        self._adapter = adapter
        self._client: Optional[BleakClient] = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._mtu = 0
        self._notifications: Dict[int, _PeripheralNotification] = {}

    @property
    def mtu(self) -> int:
        return self._mtu

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        async with self._lock:
            if self._connected:
                return
            self._client = BleakClient(self.address, adapter=self._adapter)
            try:
                await self._client.connect()
                self._connected = True
                self._mtu = getattr(self._client, "mtu_size", 0) or 0
                await self._client.get_services()
            except Exception:
                await self._cleanup_client()
                raise

    async def disconnect(self) -> None:
        async with self._lock:
            await self._cleanup_client()

    async def ensure_connected(self) -> BleakClient:
        async with self._lock:
            if not self._connected or not self._client:
                raise BleakError(f"Peripheral {self.address} not connected")
            return self._client

    async def get_services(self) -> Iterable[BleakClient]:
        client = await self.ensure_connected()
        return client.services

    async def read_characteristic(self, handle: int) -> bytes:
        client = await self.ensure_connected()
        return await client.read_gatt_char(handle)

    async def write_characteristic(self, handle: int, data: bytes, *, response: bool) -> None:
        client = await self.ensure_connected()
        await client.write_gatt_char(handle, data, response=response)

    async def read_descriptor(self, handle: int) -> bytes:
        client = await self.ensure_connected()
        return await client.read_gatt_descriptor(handle)

    async def write_descriptor(self, handle: int, data: bytes) -> None:
        client = await self.ensure_connected()
        await client.write_gatt_descriptor(handle, data)

    async def enable_notifications(
        self,
        handle: int,
        client_ref: "ProxyClient",
        callback: callable,
    ) -> None:
        client = await self.ensure_connected()
        notif = self._notifications.setdefault(handle, _PeripheralNotification())
        notif.callbacks[client_ref] = callback
        if notif.callback_registered:
            return

        loop = asyncio.get_running_loop()

        def _dispatch(_: int, data: bytearray) -> None:
            # Ensure callbacks run in the asyncio loop
            def _run_callbacks() -> None:
                for cb in list(notif.callbacks.values()):
                    try:
                        cb(bytes(data))
                    except Exception as exc:  # pragma: no cover - logging only
                        LOGGER.debug("Notification callback error for %s handle %s: %s", self.address, handle, exc)

            loop.call_soon(_run_callbacks)

        await client.start_notify(handle, _dispatch)
        notif.callback_registered = True

    async def disable_notifications(self, handle: int, client_ref: "ProxyClient") -> None:
        if handle not in self._notifications:
            return
        notif = self._notifications[handle]
        notif.callbacks.pop(client_ref, None)
        if notif.callbacks:
            return
        async with self._lock:
            if not self._client:
                return
            await self._client.stop_notify(handle)
            del self._notifications[handle]

    async def _cleanup_client(self) -> None:
        if not self._client:
            return
        with contextlib.suppress(Exception):
            await self._client.disconnect()
        self._client = None
        self._connected = False
        self._mtu = 0
        self._notifications.clear()


# ---------------------------------------------------------------------------
# Proxy client connection
# ---------------------------------------------------------------------------

class ProxyClient:
    """Represents a single Home Assistant API connection."""

    def __init__(self, server: "ESPHomeProxyServer", reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.server = server
        self.reader = reader
        self.writer = writer
        self._write_lock = asyncio.Lock()
        self._advertisement_flags = BluetoothProxySubscriptionFlag(0)
        self._subscribed_connections_free = False
        self._closing = False
        self._log_prefix = f"{writer.get_extra_info('peername')}"

    async def run(self) -> None:
        LOGGER.info("Client connected: %s", self._log_prefix)
        try:
            while True:
                msg_type, payload = await _read_message(self.reader)
                message_cls = MESSAGE_TYPE_TO_PROTO.get(msg_type)
                if not message_cls:
                    LOGGER.warning("Unsupported message type %s", msg_type)
                    continue
                message = message_cls()
                if payload:
                    message.ParseFromString(payload)
                await self._handle_message(message)
        except asyncio.IncompleteReadError:
            LOGGER.info("Client disconnected: %s", self._log_prefix)
        except Exception as exc:
            LOGGER.exception("Client error: %s", exc)
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        await self.server.remove_client(self)
        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()

    async def send(self, message: Message) -> None:
        if self._closing:
            return
        try:
            await _write_message(self.writer, message, lock=self._write_lock)
        except Exception as exc:
            LOGGER.debug("Error sending to client %s: %s", self._log_prefix, exc)
            await self.close()

    async def _handle_message(self, message: Message) -> None:
        if isinstance(message, HelloRequest):
            await self._handle_hello(message)
        elif isinstance(message, AuthenticationRequest):
            await self._handle_auth(message)
        elif isinstance(message, DeviceInfoRequest):
            await self.send(self.server.device_info_response)
        elif isinstance(message, ListEntitiesRequest):
            await self.send(ListEntitiesDoneResponse())
        elif isinstance(message, SubscribeBluetoothLEAdvertisementsRequest):
            await self._handle_subscribe_advertisements(message)
        elif isinstance(message, UnsubscribeBluetoothLEAdvertisementsRequest):
            await self._handle_unsubscribe_advertisements()
        elif isinstance(message, BluetoothScannerSetModeRequest):
            await self.server.set_scanner_mode(BluetoothScannerMode(message.mode), self)
        elif isinstance(message, SubscribeBluetoothConnectionsFreeRequest):
            await self._handle_subscribe_connections_free()
        elif isinstance(message, BluetoothDeviceRequest):
            await self.server.handle_device_request(self, message)
        elif isinstance(message, BluetoothGATTGetServicesRequest):
            await self.server.handle_get_services(self, message)
        elif isinstance(message, BluetoothGATTReadRequest):
            await self.server.handle_gatt_read(self, message)
        elif isinstance(message, BluetoothGATTWriteRequest):
            await self.server.handle_gatt_write(self, message)
        elif isinstance(message, BluetoothGATTReadDescriptorRequest):
            await self.server.handle_gatt_read_descriptor(self, message)
        elif isinstance(message, BluetoothGATTWriteDescriptorRequest):
            await self.server.handle_gatt_write_descriptor(self, message)
        elif isinstance(message, BluetoothGATTNotifyRequest):
            await self.server.handle_gatt_notify(self, message)
        elif isinstance(message, PingRequest):
            await self.send(PingResponse())
        elif isinstance(message, DisconnectRequest):
            await self.close()
        else:
            LOGGER.debug("Ignoring unsupported message from client: %s", type(message).__name__)

    async def _handle_hello(self, message: HelloRequest) -> None:
        LOGGER.info("Hello from %s (API %s.%s)", message.client_info, message.api_version_major, message.api_version_minor)
        resp = HelloResponse()
        resp.api_version_major = DEFAULT_API_VERSION_MAJOR
        resp.api_version_minor = DEFAULT_API_VERSION_MINOR
        resp.server_info = "renogy-bt-proxy"
        resp.name = self.server.name
        await self.send(resp)

    async def _handle_auth(self, message: AuthenticationRequest) -> None:
        if message.password:
            LOGGER.warning("Authentication password provided but proxy runs without auth")
        await self.send(AuthenticationResponse(invalid_password=False))

    async def _handle_subscribe_advertisements(self, message: SubscribeBluetoothLEAdvertisementsRequest) -> None:
        self._advertisement_flags = BluetoothProxySubscriptionFlag(message.flags)
        await self.server.add_advertisement_subscriber(self)

    async def _handle_unsubscribe_advertisements(self) -> None:
        self._advertisement_flags = BluetoothProxySubscriptionFlag(0)
        await self.server.remove_advertisement_subscriber(self)

    async def _handle_subscribe_connections_free(self) -> None:
        self._subscribed_connections_free = True
        await self.send(self.server.connections_free_response())

    async def send_advertisement(self, message: BluetoothLEAdvertisementResponse) -> None:
        await self.send(message)

    async def send_connections_free(self, message: BluetoothConnectionsFreeResponse) -> None:
        if self._subscribed_connections_free:
            await self.send(message)


# ---------------------------------------------------------------------------
# ESPHome proxy server
# ---------------------------------------------------------------------------

class ESPHomeProxyServer:
    """Coordinates BLE scanning, Home Assistant API handling and Renogy client."""

    def __init__(
        self,
        *,
        name: str,
        friendly_name: Optional[str],
        bind_host: str,
        port: int,
        adapter: Optional[str],
        battery_client_factory,
        blocked_addresses: Set[str],
        project_name: Optional[str],
        project_version: Optional[str],
        manufacturer: Optional[str],
        model: Optional[str],
        suggested_area: Optional[str],
        max_connections: int,
        battery_retry_seconds: int,
    ):
        self.name = name
        self._friendly_name = friendly_name
        self._bind_host = bind_host
        self._port = port
        self._adapter = adapter
        self._battery_client_factory = battery_client_factory
        self._blocked_addresses = {_normalise_mac(addr) for addr in blocked_addresses}
        self._project_name = project_name
        self._project_version = project_version
        self._manufacturer = manufacturer
        self._model = model
        self._suggested_area = suggested_area
        self._max_connections = max_connections
        self._battery_retry_seconds = max(5, battery_retry_seconds)

        self._clients: Set[ProxyClient] = set()
        self._advertisement_subscribers: Set[ProxyClient] = set()
        self._peripherals: Dict[str, PeripheralConnection] = {}
        self._connections_free_lock = asyncio.Lock()
        self._connections_free_message = BluetoothConnectionsFreeResponse()
        self._battery_task: Optional[asyncio.Task] = None
        self._battery_stop_event: Optional[asyncio.Event] = None
        self._active_battery_client = None

        self.device_info_response = _derive_device_info_response(
            name=name,
            friendly_name=friendly_name,
            adapter_mac=_read_adapter_mac(adapter),
            project_name=project_name,
            project_version=project_version,
            manufacturer=manufacturer,
            model=model,
            suggested_area=suggested_area,
            feature_flags=BluetoothProxyFeature.PASSIVE_SCAN
            | BluetoothProxyFeature.ACTIVE_CONNECTIONS
            | BluetoothProxyFeature.FEATURE_STATE_AND_MODE,
        )

        self._server: Optional[asyncio.AbstractServer] = None
        self._scanner: Optional[BleakScanner] = None
        self._zeroconf: Optional[AsyncZeroconf] = None
        self._zeroconf_info: Optional[ServiceInfo] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._scanner_mode = BluetoothScannerMode.BLUETOOTH_SCANNER_MODE_PASSIVE
        self._scanner_state = BluetoothScannerState.BLUETOOTH_SCANNER_STATE_IDLE

    # --------------------- lifecycle management -------------------------

    async def run(self) -> None:
        if self._stop_event:
            return
        self._stop_event = asyncio.Event()

        await self._start_dependencies()

        try:
            await self._stop_event.wait()
        finally:
            await self._stop_dependencies()
            self._stop_event = None

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()

    def request_stop(self) -> None:
        if self._stop_event and not self._stop_event.is_set():
            self._stop_event.set()

    async def _start_dependencies(self) -> None:
        await self._start_battery_client()
        await self._start_scanner()
        await self._start_server()
        await self._start_zeroconf()

    async def _stop_dependencies(self) -> None:
        await self._stop_server()
        await self._stop_scanner()
        await self._stop_all_peripherals()
        await self._stop_battery_client()
        await self._stop_zeroconf()

    # --------------------- client management ----------------------------

    async def add_client(self, client: ProxyClient) -> None:
        self._clients.add(client)
        await client.send(self._scanner_state_payload())

    async def remove_client(self, client: ProxyClient) -> None:
        self._clients.discard(client)
        await self.remove_advertisement_subscriber(client)

    # --------------------- advertisement handling -----------------------

    async def add_advertisement_subscriber(self, client: ProxyClient) -> None:
        self._advertisement_subscribers.add(client)
        await client.send(self._scanner_state_payload())

    async def remove_advertisement_subscriber(self, client: ProxyClient) -> None:
        self._advertisement_subscribers.discard(client)

    def _on_advertisement(self, device: BLEDevice, advertisement: AdvertisementData) -> None:
        if not self._advertisement_subscribers:
            return
        message = self._build_advertisement_message(device, advertisement)
        if message is None:
            return
        for client in list(self._advertisement_subscribers):
            CREATE_TASK(client.send_advertisement(message))

    def _build_advertisement_message(
        self,
        device: BLEDevice,
        advertisement: AdvertisementData,
    ) -> Optional[BluetoothLEAdvertisementResponse]:
        if not device.address:
            return None
        message = BluetoothLEAdvertisementResponse()
        message.address = _mac_to_int(device.address)
        message.name = (advertisement.local_name or device.name or "").encode("utf-8", "ignore")
        message.rssi = advertisement.rssi
        address_type = device.metadata.get("address_type") if device.metadata else None
        if isinstance(address_type, str):
            message.address_type = 1 if address_type.lower() == "random" else 0
        if advertisement.service_uuids:
            message.service_uuids.extend(advertisement.service_uuids)
        for svc_uuid, data in (advertisement.service_data or {}).items():
            svc = message.service_data.add()
            svc.uuid = svc_uuid
            svc.data = data
        for manufacturer_id, data in (advertisement.manufacturer_data or {}).items():
            man = message.manufacturer_data.add()
            man.uuid = f"{manufacturer_id:04x}"
            man.data = data
        return message

    # --------------------- scanner management ---------------------------

    async def set_scanner_mode(self, mode: BluetoothScannerMode, client: ProxyClient) -> None:
        if mode == self._scanner_mode:
            await client.send(self._scanner_state_payload())
            return
        LOGGER.info("Switching scanner mode to %s", mode.name)
        self._scanner_mode = mode
        # Bleak does not expose passive/active toggle – this is best effort.
        await client.send(self._scanner_state_payload())

    def _scanner_state_payload(self) -> BluetoothScannerStateResponse:
        resp = BluetoothScannerStateResponse()
        resp.state = self._scanner_state
        resp.mode = self._scanner_mode
        resp.configured_mode = self._scanner_mode
        return resp

    async def _start_scanner(self) -> None:
        scanner_kwargs = {
            "scanning_mode": "passive",
            "bluez": {"filter_duplicates": True},
        }
        if self._adapter:
            scanner_kwargs["adapter"] = self._adapter

        try:
            self._scanner = BleakScanner(
                detection_callback=self._on_advertisement,
                **scanner_kwargs,
            )
        except BleakError as exc:
            LOGGER.warning(
                "Passive scanning not available (%s); falling back to active mode",
                exc,
            )
            scanner_kwargs["scanning_mode"] = "active"
            self._scanner = BleakScanner(
                detection_callback=self._on_advertisement,
                **scanner_kwargs,
            )
        try:
            await self._scanner.start()
            self._scanner_state = BluetoothScannerState.BLUETOOTH_SCANNER_STATE_RUNNING
        except Exception as exc:
            LOGGER.error("Failed to start BLE scanner: %s", exc)
            self._scanner_state = BluetoothScannerState.BLUETOOTH_SCANNER_STATE_FAILED

    async def _stop_scanner(self) -> None:
        if self._scanner:
            with contextlib.suppress(Exception):
                await self._scanner.stop()
            self._scanner = None
        self._scanner_state = BluetoothScannerState.BLUETOOTH_SCANNER_STATE_STOPPED

    # --------------------- device connection handling -------------------

    async def handle_device_request(self, client: ProxyClient, request: BluetoothDeviceRequest) -> None:
        address = to_human_readable_address(request.address)
        normalized = _normalise_mac(address)
        if normalized in self._blocked_addresses:
            LOGGER.info("Blocking connection attempt to %s", address)
            await client.send(
                BluetoothDeviceConnectionResponse(
                    address=request.address,
                    connected=False,
                    mtu=0,
                    error=BleakError.__hash__(BleakError("blocked")),  # type: ignore[arg-type]
                )
            )
            return

        conn = self._peripherals.get(normalized)
        if request.request_type in (
            BluetoothDeviceRequest.BLUETOOTH_DEVICE_REQUEST_TYPE_CONNECT_V3_WITH_CACHE,
            BluetoothDeviceRequest.BLUETOOTH_DEVICE_REQUEST_TYPE_CONNECT_V3_WITHOUT_CACHE,
            BluetoothDeviceRequest.BLUETOOTH_DEVICE_REQUEST_TYPE_CONNECT,
        ):
            if not conn:
                conn = PeripheralConnection(address, adapter=self._adapter)
                self._peripherals[normalized] = conn
            try:
                await conn.connect()
                await client.send(
                    BluetoothDeviceConnectionResponse(
                        address=request.address,
                        connected=True,
                        mtu=conn.mtu,
                        error=0,
                    )
                )
                await self._broadcast_connections_free()
            except Exception as exc:
                LOGGER.error("Failed to connect to %s: %s", address, exc)
                await client.send(
                    BluetoothDeviceConnectionResponse(
                        address=request.address,
                        connected=False,
                        mtu=0,
                        error=1,
                    )
                )
                self._peripherals.pop(normalized, None)
        elif request.request_type == BluetoothDeviceRequest.BLUETOOTH_DEVICE_REQUEST_TYPE_DISCONNECT:
            if conn:
                with contextlib.suppress(Exception):
                    await conn.disconnect()
                self._peripherals.pop(normalized, None)
            await client.send(
                BluetoothDeviceConnectionResponse(
                    address=request.address,
                    connected=False,
                    mtu=0,
                    error=0,
                )
            )
            await self._broadcast_connections_free()
        elif request.request_type == BluetoothDeviceRequest.BLUETOOTH_DEVICE_REQUEST_TYPE_PAIR:
            await client.send(
                BluetoothDevicePairingResponse(
                    address=request.address,
                    paired=False,
                    error=1,
                )
            )
        elif request.request_type == BluetoothDeviceRequest.BLUETOOTH_DEVICE_REQUEST_TYPE_UNPAIR:
            await client.send(
                BluetoothDeviceUnpairingResponse(
                    address=request.address,
                    success=False,
                    error=1,
                )
            )
        elif request.request_type == BluetoothDeviceRequest.BLUETOOTH_DEVICE_REQUEST_TYPE_CLEAR_CACHE:
            await client.send(
                BluetoothDeviceClearCacheResponse(
                    address=request.address,
                    success=False,
                    error=1,
                )
            )
        else:
            LOGGER.warning("Unsupported BluetoothDeviceRequest type: %s", request.request_type)

    async def handle_get_services(self, client: ProxyClient, request: BluetoothGATTGetServicesRequest) -> None:
        connection = self._connection_for(request.address, client)
        if not connection:
            return
        try:
            services = await connection.get_services()
        except Exception as exc:
            LOGGER.error("Service discovery failed for %s: %s", request.address, exc)
            await client.send(
                BluetoothGATTGetServicesDoneResponse(address=request.address)
            )
            return

        response = BluetoothGATTGetServicesResponse()
        response.address = request.address
        for service in services:
            svc_msg = response.services.add()
            svc_msg.handle = getattr(service, "handle", 0)
            try:
                high, low = _split_uuid(service.uuid)
                svc_msg.uuid.extend([high, low])
            except Exception:
                svc_msg.short_uuid = 0
            for characteristic in service.characteristics:
                char_msg = svc_msg.characteristics.add()
                char_msg.handle = characteristic.handle
                try:
                    high, low = _split_uuid(characteristic.uuid)
                    char_msg.uuid.extend([high, low])
                except Exception:
                    char_msg.short_uuid = 0
                char_msg.properties = _translate_properties(characteristic.properties)
                for descriptor in characteristic.descriptors:
                    desc_msg = char_msg.descriptors.add()
                    desc_msg.handle = descriptor.handle
                    try:
                        high, low = _split_uuid(descriptor.uuid)
                        desc_msg.uuid.extend([high, low])
                    except Exception:
                        desc_msg.short_uuid = 0

        await client.send(response)
        await client.send(BluetoothGATTGetServicesDoneResponse(address=request.address))

    async def handle_gatt_read(self, client: ProxyClient, request: BluetoothGATTReadRequest) -> None:
        connection = self._connection_for(request.address, client)
        if not connection:
            return
        try:
            data = await connection.read_characteristic(request.handle)
            await client.send(
                BluetoothGATTReadResponse(
                    address=request.address,
                    handle=request.handle,
                    data=data,
                )
            )
        except Exception as exc:
            LOGGER.error("Read failed for %s handle %s: %s", request.address, request.handle, exc)
            await client.send(
                BluetoothGATTErrorResponse(address=request.address, handle=request.handle, error=1)
            )

    async def handle_gatt_write(self, client: ProxyClient, request: BluetoothGATTWriteRequest) -> None:
        connection = self._connection_for(request.address, client)
        if not connection:
            return
        try:
            await connection.write_characteristic(request.handle, request.data, response=request.response)
            await client.send(
                BluetoothGATTWriteResponse(address=request.address, handle=request.handle)
            )
        except Exception as exc:
            LOGGER.error("Write failed for %s handle %s: %s", request.address, request.handle, exc)
            await client.send(
                BluetoothGATTErrorResponse(address=request.address, handle=request.handle, error=1)
            )

    async def handle_gatt_read_descriptor(self, client: ProxyClient, request: BluetoothGATTReadDescriptorRequest) -> None:
        connection = self._connection_for(request.address, client)
        if not connection:
            return
        try:
            data = await connection.read_descriptor(request.handle)
            await client.send(
                BluetoothGATTReadResponse(
                    address=request.address,
                    handle=request.handle,
                    data=data,
                )
            )
        except Exception as exc:
            LOGGER.error("Descriptor read failed for %s handle %s: %s", request.address, request.handle, exc)
            await client.send(
                BluetoothGATTErrorResponse(address=request.address, handle=request.handle, error=1)
            )

    async def handle_gatt_write_descriptor(self, client: ProxyClient, request: BluetoothGATTWriteDescriptorRequest) -> None:
        connection = self._connection_for(request.address, client)
        if not connection:
            return
        try:
            await connection.write_descriptor(request.handle, request.data)
            await client.send(
                BluetoothGATTWriteResponse(address=request.address, handle=request.handle)
            )
        except Exception as exc:
            LOGGER.error("Descriptor write failed for %s handle %s: %s", request.address, request.handle, exc)
            await client.send(
                BluetoothGATTErrorResponse(address=request.address, handle=request.handle, error=1)
            )

    async def handle_gatt_notify(self, client: ProxyClient, request: BluetoothGATTNotifyRequest) -> None:
        connection = self._connection_for(request.address, client)
        if not connection:
            return
        try:
            if request.enable:
                await connection.enable_notifications(
                    request.handle,
                    client,
                    lambda data: CREATE_TASK(
                        client.send(
                            BluetoothGATTNotifyDataResponse(
                                address=request.address,
                                handle=request.handle,
                                data=data,
                            )
                        )
                    ),
                )
            else:
                await connection.disable_notifications(request.handle, client)
            await client.send(
                BluetoothGATTNotifyResponse(address=request.address, handle=request.handle)
            )
        except Exception as exc:
            LOGGER.error("Notify toggle failed for %s handle %s: %s", request.address, request.handle, exc)
            await client.send(
                BluetoothGATTErrorResponse(address=request.address, handle=request.handle, error=1)
            )

    def _connection_for(self, address_int: int, client: ProxyClient) -> Optional[PeripheralConnection]:
        address = _normalise_mac(to_human_readable_address(address_int))
        if address not in self._peripherals:
            LOGGER.warning("Client requested operation on unknown peripheral %s", address)
            CREATE_TASK(
                client.send(
                    BluetoothDeviceConnectionResponse(
                        address=address_int,
                        connected=False,
                        mtu=0,
                        error=1,
                    )
                )
            )
            return None
        return self._peripherals[address]

    async def _stop_all_peripherals(self) -> None:
        for conn in list(self._peripherals.values()):
            with contextlib.suppress(Exception):
                await conn.disconnect()
        self._peripherals.clear()

    # --------------------- connections free --------------------------------

    async def _broadcast_connections_free(self) -> None:
        message = self.connections_free_response()
        for client in list(self._clients):
            CREATE_TASK(client.send_connections_free(message))

    def connections_free_response(self) -> BluetoothConnectionsFreeResponse:
        in_use = len(self._peripherals)
        free = max(0, self._max_connections - in_use)
        message = BluetoothConnectionsFreeResponse()
        message.free = free
        message.limit = self._max_connections
        for addr in self._peripherals.keys():
            message.allocated.append(_mac_to_int(addr))
        return message

    # --------------------- server socket -----------------------------------

    async def _start_server(self) -> None:
        self._server = await asyncio.start_server(self._accept_client, self._bind_host, self._port)
        socket_names = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        LOGGER.info("ESPHome proxy listening on %s", socket_names)

    async def _stop_server(self) -> None:
        if not self._server:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _accept_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = ProxyClient(self, reader, writer)
        await self.add_client(client)
        await client.run()

    # --------------------- renogy battery client ---------------------------

    async def _start_battery_client(self) -> None:
        if not self._battery_client_factory or self._battery_task:
            return

        self._battery_stop_event = asyncio.Event()
        self._battery_task = CREATE_TASK(self._run_battery_supervisor())
        LOGGER.info("Battery client supervisor started")

    async def _run_battery_supervisor(self) -> None:
        assert self._battery_client_factory
        retry_delay = self._battery_retry_seconds
        loop = asyncio.get_running_loop()

        while True:
            if self._battery_stop_event and self._battery_stop_event.is_set():
                break

            try:
                client = self._battery_client_factory()
            except Exception as exc:  # pragma: no cover - factory errors
                LOGGER.error("Failed to create battery client: %s", exc)
                await asyncio.sleep(retry_delay)
                continue

            self._active_battery_client = client
            LOGGER.info("Battery client thread starting")

            try:
                await loop.run_in_executor(None, client.start)
            except Exception as exc:  # pragma: no cover - hardware runtime
                LOGGER.error("Battery client exited unexpectedly: %s", exc)
            finally:
                with contextlib.suppress(Exception):
                    client.stop()
                self._active_battery_client = None

            if self._battery_stop_event and self._battery_stop_event.is_set():
                break

            LOGGER.info(
                "Battery client stopped; retrying in %s seconds", retry_delay
            )
            await asyncio.sleep(retry_delay)

        LOGGER.info("Battery client supervisor stopped")

    async def _stop_battery_client(self) -> None:
        if not self._battery_task:
            return

        if self._battery_stop_event and not self._battery_stop_event.is_set():
            self._battery_stop_event.set()

        if self._active_battery_client:
            with contextlib.suppress(Exception):  # pragma: no cover - best effort
                self._active_battery_client.stop()

        task = self._battery_task
        self._battery_task = None
        self._active_battery_client = None

        try:
            await task
        except Exception as exc:  # pragma: no cover - supervisor errors
            LOGGER.debug("Battery supervisor finished with error: %s", exc)

        self._battery_stop_event = None

    # --------------------- zeroconf advertisement --------------------------

    async def _start_zeroconf(self) -> None:
        try:
            self._zeroconf = AsyncZeroconf()
            ip_bytes = socket.inet_aton(_guess_primary_ip())
            properties = {
                b"version": (self._project_version or "renogy-bt-proxy").encode(),
            }
            if self._friendly_name:
                properties[b"friendly_name"] = self._friendly_name.encode()
            if self._project_name:
                properties[b"project_name"] = self._project_name.encode()
            service_name = f"{self.name}.{MDNS_SERVICE_TYPE}"
            self._zeroconf_info = ServiceInfo(
                MDNS_SERVICE_TYPE,
                service_name,
                addresses=[ip_bytes],
                port=self._port,
                properties=properties,
            )
            await self._zeroconf.async_register_service(self._zeroconf_info)
            LOGGER.info("mDNS service %s registered", service_name)
        except Exception as exc:
            LOGGER.warning("Failed to register mDNS service: %s", exc)
            self._zeroconf = None
            self._zeroconf_info = None

    async def _stop_zeroconf(self) -> None:
        if not self._zeroconf:
            return
        if self._zeroconf_info:
            with contextlib.suppress(Exception):
                await self._zeroconf.async_unregister_service(self._zeroconf_info)
        with contextlib.suppress(Exception):
            await self._zeroconf.async_close()
        self._zeroconf = None
        self._zeroconf_info = None


# ---------------------------------------------------------------------------
# Helper for translating characteristic properties
# ---------------------------------------------------------------------------

PROPERTY_FLAGS = {
    "broadcast": 0x0001,
    "read": 0x0002,
    "write-without-response": 0x0004,
    "write": 0x0008,
    "notify": 0x0010,
    "indicate": 0x0020,
    "authenticated-signed-writes": 0x0040,
    "extended-properties": 0x0080,
    "reliable-write": 0x0100,
    "writable-auxiliaries": 0x0200,
}


def _translate_properties(properties: Iterable[str]) -> int:
    mask = 0
    for prop in properties:
        mask |= PROPERTY_FLAGS.get(prop, 0)
    return mask


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

class HomeAssistantBluetoothProxy:
    """Convenience wrapper that mirrors the previous API."""

    def __init__(
        self,
        *,
        name: str,
        friendly_name: Optional[str],
        bind_host: str,
        port: int,
        adapter: Optional[str],
        battery_client_factory,
        blocked_addresses: Set[str],
        project_name: Optional[str],
        project_version: Optional[str],
        manufacturer: Optional[str],
        model: Optional[str],
        suggested_area: Optional[str],
        max_connections: int = 3,
        battery_retry_seconds: int = 30,
    ):
        self._server = ESPHomeProxyServer(
            name=name,
            friendly_name=friendly_name,
            bind_host=bind_host,
            port=port,
            adapter=adapter,
            battery_client_factory=battery_client_factory,
            blocked_addresses=blocked_addresses,
            project_name=project_name,
            project_version=project_version,
            manufacturer=manufacturer,
            model=model,
            suggested_area=suggested_area,
            max_connections=max_connections,
            battery_retry_seconds=battery_retry_seconds,
        )

    async def start(self) -> None:
        await self._server.run()

    async def stop(self) -> None:
        await self._server.stop()

    def request_stop(self) -> None:
        self._server.request_stop()


__all__ = [
    "HomeAssistantBluetoothProxy",
]
