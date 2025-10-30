"""Example ESPHome Bluetooth Proxy using native API.

This example shows how to use the ESPHome native API server to expose
the Bluetooth proxy to Home Assistant's ESPHome integration.

The device will:
1. Advertise itself via mDNS so Home Assistant can discover it
2. Listen on port 6053 for ESPHome native API connections
3. Forward BLE advertisements to connected clients

Usage:
    python esphome_proxy_example.py

After running, you can add the device in Home Assistant:
1. Go to Settings -> Devices & Services
2. Click "+ Add Integration"
3. Search for "ESPHome"
4. The device should appear automatically via mDNS discovery
5. Click on it to add it

The device will then act as a Bluetooth proxy, extending Home Assistant's
Bluetooth range.
"""

import asyncio
import logging
from bleak import BLEDevice, AdvertisementData, BleakScanner

from renogybt.esphome_api_server import ESPHomeAPIServer
from renogybt.esphome_discovery import ESPHomeDiscovery

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


async def main():
    # Configuration
    device_name = "renogy-bt-proxy"
    mac_address = "AA:BB:CC:DD:EE:FF"  # Replace with your device MAC
    adapter = "hci0"  # Bluetooth adapter to use

    # Create ESPHome API server
    api_server = ESPHomeAPIServer(
        name=device_name,
        mac_address=mac_address,
        port=6053,
        version="2024.12.0",
    )

    # Create mDNS discovery service
    discovery = ESPHomeDiscovery(
        name=device_name,
        port=6053,
        mac=mac_address,
    )

    # Callback to send advertisements
    send_advertisement_callback = None

    def register_advertisement_sender(callback):
        """Called when a client subscribes to BLE advertisements."""
        nonlocal send_advertisement_callback
        send_advertisement_callback = callback
        logger.info("Client subscribed to BLE advertisements")

    api_server.set_advertisement_callback(register_advertisement_sender)

    # BLE advertisement handler
    def on_ble_advertisement(device: BLEDevice, advertisement_data: AdvertisementData):
        """Handle BLE advertisement from scanner."""
        if send_advertisement_callback is None:
            return

        # Convert advertisement to the format expected by the API
        adv_dict = {
            "address": device.address,
            "rssi": advertisement_data.rssi,
            "address_type": "random" if device.address_type == "random" else "public",
            "name": advertisement_data.local_name or "",
            "manufacturer_data": {
                str(k): v.hex()
                for k, v in advertisement_data.manufacturer_data.items()
            },
            "service_data": {
                k: v.hex()
                for k, v in advertisement_data.service_data.items()
            },
            "service_uuids": list(advertisement_data.service_uuids),
        }

        send_advertisement_callback(adv_dict)

    # Create BLE scanner
    scanner = BleakScanner(
        detection_callback=on_ble_advertisement,
        adapter=adapter,
    )

    try:
        # Start all services
        logger.info("Starting ESPHome Bluetooth Proxy...")
        await api_server.start()
        await discovery.start()
        await scanner.start()

        logger.info("=" * 60)
        logger.info("ESPHome Bluetooth Proxy is running!")
        logger.info("Device name: %s", device_name)
        logger.info("Add this device in Home Assistant:")
        logger.info("  1. Go to Settings -> Devices & Services")
        logger.info("  2. Click '+ Add Integration'")
        logger.info("  3. Search for 'ESPHome'")
        logger.info("  4. The device should appear automatically")
        logger.info("=" * 60)

        # Run forever
        await asyncio.Event().wait()

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await scanner.stop()
        await discovery.stop()
        await api_server.stop()
        logger.info("Stopped")


if __name__ == "__main__":
    asyncio.run(main())
