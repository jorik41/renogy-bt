"""ESPHome mDNS discovery for Home Assistant integration.

This module provides mDNS/zeroconf advertisement to make the Bluetooth proxy
discoverable by Home Assistant's ESPHome integration.

Note: This provides discovery only. Full ESPHome native API support would require
implementing the protobuf-based TCP protocol on port 6053.
"""

from __future__ import annotations

import logging
import socket
from typing import Optional

from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

logger = logging.getLogger(__name__)


class ESPHomeDiscovery:
    """Advertises the device via mDNS so Home Assistant can discover it."""

    def __init__(
        self,
        name: str,
        port: int = 6053,
        version: str = "2024.12.0",
        mac: Optional[str] = None,
    ):
        """Initialize ESPHome discovery.

        Args:
            name: Device name (will be used as hostname)
            port: Port for ESPHome API (default: 6053)
            version: ESPHome version to advertise
            mac: MAC address of the device
        """
        self.name = name.replace(" ", "-").lower()
        self.port = port
        self.version = version
        self.mac = mac or "00:00:00:00:00:00"
        self._aiozc: Optional[AsyncZeroconf] = None
        self._service_info: Optional[AsyncServiceInfo] = None

    def _get_local_ip(self) -> str:
        """Get local IP address using multiple fallback methods.
        
        Returns:
            Local IP address as string
            
        Raises:
            RuntimeError: If unable to determine local IP
        """
        # Method 1: Try connecting to an external IP (works in most cases)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # Use Google DNS as target (doesn't actually send data)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                if local_ip and local_ip != "127.0.0.1":
                    return local_ip
            finally:
                s.close()
        except Exception:
            pass
        
        # Method 2: Try connecting to common local gateway
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("192.168.1.1", 80))
                local_ip = s.getsockname()[0]
                if local_ip and local_ip != "127.0.0.1":
                    return local_ip
            finally:
                s.close()
        except Exception:
            pass
            
        # Method 3: Get hostname and resolve it
        try:
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
            if local_ip and local_ip != "127.0.0.1":
                return local_ip
        except Exception:
            # Ignore errors in hostname resolution; will fall back to next method or default.
            pass
        
        # Method 4: Fallback to 0.0.0.0 (listen on all interfaces)
        # This will work but may cause issues in multi-interface systems
        logger.warning(
            "Unable to determine specific local IP, using 0.0.0.0 "
            "(will listen on all interfaces)"
        )
        return "0.0.0.0"

    async def start(self) -> None:
        """Start advertising the ESPHome device via mDNS."""
        try:
            # Get local IP address - try multiple methods for robustness
            local_ip = self._get_local_ip()
            
            # Convert IP to bytes
            ip_bytes = socket.inet_aton(local_ip)

            # Create service info
            service_type = "_esphomelib._tcp.local."
            service_name = f"{self.name}.{service_type}"

            # ESPHome device properties
            properties = {
                "version": self.version,
                "mac": self.mac,
                "platform": "linux",
                "board": "generic",
                "network": "wifi",
                "api_encryption": "false",  # We don't support encryption yet
            }

            self._service_info = AsyncServiceInfo(
                service_type,
                service_name,
                addresses=[ip_bytes],
                port=self.port,
                properties=properties,
                server=f"{self.name}.local.",
            )

            # Start zeroconf
            self._aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
            await self._aiozc.async_register_service(self._service_info)

            logger.info(
                "ESPHome device '%s' advertised via mDNS on %s:%d",
                self.name,
                local_ip,
                self.port,
            )
        except Exception as e:
            logger.error("Failed to start mDNS advertisement: %s", e, exc_info=True)
            raise

    async def stop(self) -> None:
        """Stop advertising the device."""
        if self._aiozc and self._service_info:
            try:
                await self._aiozc.async_unregister_service(self._service_info)
                await self._aiozc.async_close()
                logger.info("Stopped mDNS advertisement for '%s'", self.name)
            except Exception as e:
                logger.warning("Error stopping mDNS advertisement: %s", e)
            finally:
                self._aiozc = None
                self._service_info = None


__all__ = ["ESPHomeDiscovery"]
