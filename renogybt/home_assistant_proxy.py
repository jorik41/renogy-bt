"""Home Assistant Bluetooth proxy utilities.

This module provides a framework that emulates the behaviour of the
`bluetooth_proxy` component typically found in ESPHome devices. It allows
this project to act as an ESP32 Bluetooth proxy, forwarding BLE advertisements
to Home Assistant.

The proxy can operate in two modes:

1. **Standalone Mode**: Pure ESP32 BT proxy functionality
   - Scans for nearby BLE devices and forwards advertisements to Home Assistant
   - No Renogy-specific functionality
   - Lightweight and efficient for general BT proxy use

2. **Combined Mode**: BT proxy + Renogy client
   - Simultaneously acts as a BT proxy and polls Renogy batteries
   - Allows sharing a single Bluetooth adapter for both functions
   - Useful when running on hardware like a Raspberry Pi Zero

The implementation is designed to be flexible and work on Linux SBCs where
the bleak backend is available, providing a Python alternative to ESP32-based
proxies.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Sequence

import aiohttp
from bleak import BLEDevice, AdvertisementData, BleakScanner

# CREATE_TASK provides compatibility for different Python/asyncio versions
CREATE_TASK = getattr(asyncio, "create_task", asyncio.ensure_future)

# Pattern to identify Bluetooth adapter devices (e.g., "hci0 (C8:8A:D8:41:0B:4F)")
# These should not be forwarded to Home Assistant as they represent the local adapter
ADAPTER_NAME_PATTERN = re.compile(r'^hci\d+\s+\([0-9A-Fa-f:]+\)$')


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
    """Minimal HTTP client used to forward advertisements to Home Assistant.
    Following ESPHome Bluetooth proxy protocol, this client does not use
    authentication tokens. ESPHome proxies operate on trusted networks without
    bearer token authentication.
    """

    def __init__(
        self,
        host: str,
        port: int,
        ssl: bool = False,
        endpoint: str = "/api/bluetooth/adv",
        fallback_endpoints: Optional[Sequence[str]] = None,
        session_factory: Optional[
            Callable[[], Awaitable[aiohttp.ClientSession]]
        ] = None,
    ) -> None:
        self._host = host.rstrip("/")
        self._port = port
        self._ssl = ssl
        endpoints: List[str] = []
        if endpoint:
            endpoints.append(endpoint)
        if fallback_endpoints:
            for candidate in fallback_endpoints:
                if candidate and candidate not in endpoints:
                    endpoints.append(candidate)
        if not endpoints:
            endpoints.append("/api/bluetooth/adv")
        self._endpoints = endpoints
        self._endpoint_failures: Dict[str, int] = {}
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
        """Send a BLE advertisement payload to Home Assistant.
        
        Following ESPHome protocol, no authentication token is used.
        The proxy should be on a trusted network.
        """

        if not self._session:
            raise RuntimeError("Client session has not been initialised")

        payload = packet.as_payload()
        for index, endpoint in enumerate(self._endpoints):
            url = f"{self.base_url}{endpoint}"
            try:
                async with self._session.post(
                    url,
                    json=payload,
                ) as resp:
                    if resp.status < 400:
                        if endpoint in self._endpoint_failures:
                            self._endpoint_failures.pop(endpoint, None)
                        return
                    body = await resp.text()
                    should_try_fallback = (
                        resp.status in {404, 405, 410}
                        and index < len(self._endpoints) - 1
                    )
                    if should_try_fallback:
                        attempts = self._endpoint_failures.get(endpoint, 0)
                        if attempts == 0:
                            logging.info(
                                "Endpoint %s responded with %s, trying fallback %s",
                                endpoint,
                                resp.status,
                                self._endpoints[index + 1],
                            )
                        self._endpoint_failures[endpoint] = attempts + 1
                        continue
                    logging.warning(
                        "Failed to forward advertisement via %s: status=%s body=%s",
                        endpoint,
                        resp.status,
                        body,
                    )
            except aiohttp.ClientError as exc:
                if index < len(self._endpoints) - 1:
                    logging.info(
                        "Error sending advertisement via %s (%s), trying fallback %s",
                        endpoint,
                        exc,
                        self._endpoints[index + 1],
                    )
                    continue
                logging.error("Error sending advertisement to Home Assistant: %s", exc)


class HomeAssistantBluetoothProxy:
    """Coordinates BLE scanning with Home Assistant forwarding.

    This proxy acts as an ESP32 Bluetooth proxy, forwarding BLE advertisements
    to Home Assistant. It can optionally keep a Renogy BLE client alive in the
    background, but is designed to work as a standalone BT proxy as well.
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

        # Only start battery client if factory is provided
        if self._battery_client_factory:
            await self._start_battery_client()

        self._scanner = BleakScanner(
            detection_callback=self._on_advertisement,
            adapter=self._adapter,
        )

        try:
            async with self._api_client:
                await self._scanner.start()
                mode = "with Renogy client" if self._battery_client_factory else "standalone"
                logging.info(
                    "Home Assistant bluetooth proxy started (%s) on adapter %s",
                    mode,
                    self._adapter,
                )
                try:
                    await self._stop_event.wait()
                finally:
                    await self._scanner.stop()
        finally:
            if self._battery_client_factory:
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
        
        # Skip forwarding advertisements from local Bluetooth adapters
        # Adapters typically have names like "hci0 (MAC_ADDRESS)"
        if device.name and ADAPTER_NAME_PATTERN.match(device.name):
            logging.debug(
                "Skipping adapter device: %s (%s)", device.name, device.address
            )
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
