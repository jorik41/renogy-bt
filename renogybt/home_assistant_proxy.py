"""Home Assistant Bluetooth proxy utilities.

This module adds a small framework that mirrors the behaviour of the
`bluetooth_proxy` component that is normally found in ESPHome devices.
The aim is to allow this project to operate as if it were running on an
ESP32 based proxy while continuing to poll the Renogy battery over the
existing BLE connection.  The implementation focuses on providing the
building blocks required to run on a Linux SBC such as a Raspberry Pi
Zero where the bleak backend is already available.

The Home Assistant side of the proxy is intentionally kept flexible –
it exposes a simple HTTP client that can be adapted to the needs of the
target Home Assistant deployment.  The default endpoint follows the API
shape of Home Assistant's Bluetooth remote receivers which accept the
raw advertisement payloads.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

import aiohttp
from bleak import BLEDevice, AdvertisementData, BleakScanner


CREATE_TASK = getattr(asyncio, "create_task", asyncio.ensure_future)


@dataclass
class AdvertisementPacket:
    """Representation of a BLE advertisement.

    The data model is heavily inspired by ESPHome's proxy implementation
    and mirrors the structure that Home Assistant expects when receiving
    forwarded advertisements from a remote proxy device.
    """

    address: str
    rssi: int
    address_type: str
    local_name: Optional[str]
    manufacturer_data: Dict[int, bytes]
    service_data: Dict[str, bytes]
    service_uuids: List[str]
    timestamp: float
    source: str

    @classmethod
    def from_bleak(
        cls,
        device: BLEDevice,
        advertisement: AdvertisementData,
        source: str,
    ) -> "AdvertisementPacket":
        """Create an :class:`AdvertisementPacket` from bleak structures."""

        address_type = device.metadata.get("address_type") if device.metadata else None
        manufacturer_data = advertisement.manufacturer_data or {}
        service_data = advertisement.service_data or {}
        service_uuids = advertisement.service_uuids or []

        return cls(
            address=device.address,
            rssi=advertisement.rssi,
            address_type=address_type or "public",
            local_name=advertisement.local_name,
            manufacturer_data=dict(manufacturer_data),
            service_data=dict(service_data),
            service_uuids=list(service_uuids),
            timestamp=time.time(),
            source=source,
        )

    def as_payload(self) -> Dict[str, object]:
        """Serialise the advertisement to a JSON friendly dictionary."""

        return {
            "address": self.address,
            "address_type": self.address_type,
            "rssi": self.rssi,
            "local_name": self.local_name,
            "manufacturer_data": {
                str(k): v.hex() for k, v in self.manufacturer_data.items()
            },
            "service_data": {k: v.hex() for k, v in self.service_data.items()},
            "service_uuids": self.service_uuids,
            "timestamp": self.timestamp,
            "source": self.source,
        }


class HomeAssistantAPIClient:
    """Minimal HTTP client used to forward advertisements to Home Assistant."""

    def __init__(
        self,
        host: str,
        port: int,
        token: Optional[str],
        ssl: bool = False,
        endpoint: str = "/api/bluetooth/adv",
        session_factory: Optional[
            Callable[[], Awaitable[aiohttp.ClientSession]]
        ] = None,
    ) -> None:
        self._host = host.rstrip("/")
        self._port = port
        self._token = token
        self._ssl = ssl
        self._endpoint = endpoint
        self._session_factory = session_factory
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "HomeAssistantAPIClient":
        if self._session is None:
            if self._session_factory:
                self._session = await self._session_factory()
            else:
                self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if self._session:
            await self._session.close()
        self._session = None

    @property
    def base_url(self) -> str:
        if "://" in self._host:
            return self._host
        scheme = "https" if self._ssl else "http"
        return f"{scheme}://{self._host}:{self._port}"

    async def send_advertisement(self, packet: AdvertisementPacket) -> None:
        """Send a BLE advertisement payload to Home Assistant."""

        if not self._session:
            raise RuntimeError("Client session has not been initialised")

        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        url = f"{self.base_url}{self._endpoint}"
        try:
            async with self._session.post(url, json=packet.as_payload(), headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logging.warning(
                        "Failed to forward advertisement: status=%s body=%s",
                        resp.status,
                        body,
                    )
        except aiohttp.ClientError as exc:
            logging.error("Error sending advertisement to Home Assistant: %s", exc)


class HomeAssistantBluetoothProxy:
    """Coordinates BLE scanning with Home Assistant forwarding.

    The proxy keeps the Renogy BLE client alive by running it in a
    background executor.  This mirrors the behaviour of ESPHome devices
    which simultaneously act as a Bluetooth peripheral and a proxy for
    Home Assistant.
    """

    def __init__(
        self,
        api_client: HomeAssistantAPIClient,
        source: str,
        adapter: Optional[str] = None,
        battery_client_factory: Optional[Callable[[], object]] = None,
    ) -> None:
        self._api_client = api_client
        self._source = source
        self._adapter = adapter
        self._battery_client_factory = battery_client_factory
        self._battery_client = None
        self._battery_future: Optional[asyncio.Future] = None
        self._scanner: Optional[BleakScanner] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event = asyncio.Event()

        await self._start_battery_client()

        self._scanner = BleakScanner(adapter=self._adapter)
        self._scanner.register_detection_callback(self._on_advertisement)

        try:
            async with self._api_client:
                await self._scanner.start()
                logging.info(
                    "Home Assistant bluetooth proxy started on adapter %s",
                    self._adapter,
                )
                try:
                    await self._stop_event.wait()
                finally:
                    await self._scanner.stop()
        finally:
            await self._stop_battery_client()
            self._running = False
            self._stop_event = None

    async def stop(self) -> None:
        if not self._running:
            return
        if self._stop_event and not self._stop_event.is_set():
            self._stop_event.set()

    def request_stop(self) -> None:
        """Request the proxy to stop running."""

        if self._stop_event and not self._stop_event.is_set():
            self._stop_event.set()

    async def _start_battery_client(self) -> None:
        if not self._battery_client_factory:
            return

        loop = asyncio.get_running_loop()
        self._battery_client = self._battery_client_factory()

        def run_client() -> None:
            try:
                self._battery_client.start()
            except Exception as exc:  # pragma: no cover - defensive, hardware runtime
                logging.error("Battery client exited unexpectedly: %s", exc)

        self._battery_future = loop.run_in_executor(None, run_client)
        logging.info("Battery client started in background executor")

    async def _stop_battery_client(self) -> None:
        if not self._battery_client:
            return
        try:
            self._battery_client.stop()
        except Exception as exc:  # pragma: no cover - best effort stop
            logging.warning("Error stopping battery client: %s", exc)
        if self._battery_future:
            await asyncio.wrap_future(self._battery_future)
        self._battery_client = None
        self._battery_future = None
        logging.info("Battery client stopped")

    def _on_advertisement(self, device: BLEDevice, advertisement: AdvertisementData) -> None:
        if not self._running:
            return
        packet = AdvertisementPacket.from_bleak(device, advertisement, self._source)
        CREATE_TASK(self._forward_packet(packet))

    async def _forward_packet(self, packet: AdvertisementPacket) -> None:
        await self._api_client.send_advertisement(packet)


__all__ = [
    "AdvertisementPacket",
    "HomeAssistantAPIClient",
    "HomeAssistantBluetoothProxy",
]
