"""Async zeroconf helper to advertise the ESPHome proxy to Home Assistant."""

from __future__ import annotations

import logging
import socket
from typing import Optional

from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

logger = logging.getLogger(__name__)


class ESPHomeDiscovery:
    """Advertise the proxy over mDNS so Home Assistant discovers it automatically."""

    def __init__(
        self,
        name: str,
        port: int = 6053,
        version: str = "2024.12.0",
        mac: Optional[str] = None,
        ip: Optional[str] = None,
    ) -> None:
        self.name = name.replace(" ", "-").lower()
        self.port = port
        self.version = version
        self.mac = (mac or "00:00:00:00:00:00").lower()
        self._ip_override = ip
        self._aiozc: Optional[AsyncZeroconf] = None
        self._service_info: Optional[AsyncServiceInfo] = None

    async def start(self) -> None:
        ip_addr = self._ip_override or self._detect_ip()
        address = socket.inet_aton(ip_addr)

        properties = {
            "version": self.version,
            "mac": self.mac,
            "platform": "linux",
            "board": "generic",
            "network": "ethernet",
            "api_version": "1.13",
            "use_password": "false",
            "bluetooth_proxy": "true",
            "bluetooth_proxy_version": "5",
            "bluetooth_proxy_feature_flags": "97",
            "project_name": "renogybt",
            "project_version": self.version,
        }

        service_type = "_esphomelib._tcp.local."
        service_name = f"{self.name}.{service_type}"

        self._aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        self._service_info = AsyncServiceInfo(
            type_=service_type,
            name=service_name,
            addresses=[address],
            port=self.port,
            properties=properties,
        )

        await self._aiozc.async_register_service(self._service_info)
        logger.info("Advertised ESPHome proxy via mDNS as %s (%s:%d)", service_name, ip_addr, self.port)

    async def stop(self) -> None:
        if not self._aiozc or not self._service_info:
            return
        try:
            await self._aiozc.async_unregister_service(self._service_info)
        finally:
            await self._aiozc.async_close()
        self._aiozc = None
        self._service_info = None
        logger.info("Stopped ESPHome proxy mDNS advertisement")

    def _detect_ip(self) -> str:
        candidates = [
            ("8.8.8.8", 80),
            ("192.168.1.1", 80),
        ]
        for host, port in candidates:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.connect((host, port))
                ip = sock.getsockname()[0]
                sock.close()
                if ip and not ip.startswith("127."):
                    return ip
            except OSError:
                continue
        try:
            hostname = socket.gethostname()
            ip = socket.gethostbyname(hostname)
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            pass
        raise RuntimeError("Unable to determine local IP address for ESPHome discovery")


__all__ = ["ESPHomeDiscovery"]
