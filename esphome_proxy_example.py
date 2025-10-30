"""Example ESPHome Bluetooth Proxy using native API with Renogy battery integration.

This example shows how to use the ESPHome native API server to expose
the Bluetooth proxy to Home Assistant's ESPHome integration, while also
reading Renogy battery data and publishing to MQTT.

The device will:
1. Advertise itself via mDNS so Home Assistant can discover it
2. Listen on port 6053 for ESPHome native API connections
3. Forward BLE advertisements to connected clients
4. Read Renogy battery data from configured device
5. Publish battery data to MQTT (if enabled)

Usage:
    python esphome_proxy_example.py [config.ini]

After running, you can add the device in Home Assistant:
1. Go to Settings -> Devices & Services
2. Click "+ Add Integration"
3. Search for "ESPHome"
4. The device should appear automatically via mDNS discovery
5. Click on it to add it

The device will then act as a Bluetooth proxy, extending Home Assistant's
Bluetooth range, while also monitoring your Renogy batteries.
"""

import asyncio
import configparser
import logging
import sys
from pathlib import Path
from bleak import BLEDevice, AdvertisementData, BleakScanner

from renogybt.esphome_api_server import ESPHomeAPIServer
from renogybt.esphome_discovery import ESPHomeDiscovery
from renogybt import (
    BatteryClient,
    RoverClient,
    DCChargerClient,
    InverterClient,
    DataLogger,
    Utils,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

# Supported Renogy device types
SUPPORTED_DEVICE_TYPES = ['RNG_BATT', 'RNG_CTRL', 'RNG_CTRL_HIST', 'RNG_INVT', 'RNG_DCC']


async def main():
    # Load configuration
    config_file = sys.argv[1] if len(sys.argv) > 1 else 'config.ini'
    base_dir = Path(__file__).resolve().parent
    config_path = (base_dir / config_file).resolve()
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)
    
    config = configparser.ConfigParser(inline_comment_prefixes=('#'))
    config.read(str(config_path))
    
    # Validate required config sections exist
    required_sections = ['device', 'data', 'home_assistant_proxy']
    missing_sections = [s for s in required_sections if not config.has_section(s)]
    if missing_sections:
        logger.error("Missing required config sections: %s", ', '.join(missing_sections))
        logger.error("Please check your config.ini file")
        sys.exit(1)
    
    # Add optional sections if they don't exist
    optional_sections = ['mqtt', 'remote_logging', 'pvoutput']
    for section in optional_sections:
        if not config.has_section(section):
            config.add_section(section)
            config.set(section, 'enabled', 'false')
    
    # Check if proxy is enabled
    if not config['home_assistant_proxy'].getboolean('enabled', fallback=False):
        logger.error("Home Assistant proxy is not enabled in config.ini")
        logger.error("Set 'enabled = true' in [home_assistant_proxy] section")
        sys.exit(1)
    
    # Get configuration
    device_name = config['home_assistant_proxy'].get('device_name', 'renogy-bt-proxy')
    adapter = config['home_assistant_proxy'].get('adapter', config['device'].get('adapter', 'hci0'))
    mac_address = config['device'].get('mac_addr', 'AA:BB:CC:DD:EE:FF')
    native_api_port = config['home_assistant_proxy'].getint('native_api_port', 6053)
    
    # Initialize data logger and battery map
    data_logger = DataLogger(config)
    energy_file = str((config_path.parent / 'energy_totals.json').resolve())
    battery_map = {}
    
    # Create battery client based on device type
    device_type = config['device'].get('type')
    if not device_type:
        logger.error("Device type not specified in config.ini [device] section")
        logger.error("Set 'type' to one of: %s", ', '.join(SUPPORTED_DEVICE_TYPES))
        sys.exit(1)
    
    if device_type not in SUPPORTED_DEVICE_TYPES:
        logger.warning("Unknown device type '%s'. Supported types: %s", 
                      device_type, ', '.join(SUPPORTED_DEVICE_TYPES))
    
    device_alias = config['device'].get('alias', 'renogy-device')
    renogy_client = None
    
    def on_battery_data_received(client, data):
        """Callback when battery data is received."""
        Utils.add_calculated_values(data)
        alias = device_alias
        dev_id = data.get('device_id')
        alias_id = f"{alias}_{dev_id}" if dev_id is not None else alias
        Utils.update_energy_totals(
            data,
            interval_sec=config['data'].getint('poll_interval', fallback=0),
            file_path=energy_file,
            alias=alias_id,
        )
        fields = config['data'].get('fields', fallback='')
        filtered_data = Utils.filter_fields(data, fields)
        logger.info(f"{client.ble_manager.device.name} => {filtered_data}")
        
        # Handle combined battery readings for multiple batteries
        if device_type == 'RNG_BATT' and len(client.device_ids) > 1:
            dev_id = data.get('device_id')
            if dev_id is not None:
                battery_map[dev_id] = data
            if len(battery_map) == len(client.device_ids):
                combined = Utils.combine_battery_readings(battery_map)
                filtered_combined = Utils.filter_fields(combined, fields)
                logger.info(f"combined => {filtered_combined}")
                if config['mqtt'].getboolean('enabled'):
                    data_logger.log_mqtt(json_data=filtered_combined)
                battery_map.clear()
        
        # Log to remote and MQTT
        if config['remote_logging'].getboolean('enabled'):
            data_logger.log_remote(json_data=filtered_data)
        if config['mqtt'].getboolean('enabled'):
            data_logger.log_mqtt(json_data=filtered_data)
        if config['pvoutput'].getboolean('enabled') and device_type == 'RNG_CTRL':
            data_logger.log_pvoutput(json_data=filtered_data)
    
    def on_error(client, error):
        logger.error(f"Battery client error: {error}")
    
    # Create ESPHome API server
    api_server = ESPHomeAPIServer(
        name=device_name,
        mac_address=mac_address,
        port=native_api_port,
        version="2024.12.0",
    )

    # Create mDNS discovery service
    discovery = ESPHomeDiscovery(
        name=device_name,
        port=native_api_port,
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
        logger.info("Starting ESPHome Bluetooth Proxy with Renogy battery integration...")
        await api_server.start()
        await discovery.start()
        await scanner.start()
        
        # Start Renogy battery client
        if device_type == 'RNG_CTRL':
            renogy_client = RoverClient(config, on_battery_data_received, on_error)
        elif device_type == 'RNG_BATT':
            renogy_client = BatteryClient(config, on_battery_data_received, on_error)
        elif device_type == 'RNG_INVT':
            renogy_client = InverterClient(config, on_battery_data_received, on_error)
        elif device_type == 'RNG_DCC':
            renogy_client = DCChargerClient(config, on_battery_data_received, on_error)
        else:
            logger.warning(f"Unknown device type: {device_type}. Only BT proxy will be active.")
        
        if renogy_client:
            renogy_client.start()
            logger.info(f"Started Renogy {device_type} client")

        logger.info("=" * 60)
        logger.info("ESPHome Bluetooth Proxy with Renogy integration is running!")
        logger.info("Device name: %s", device_name)
        logger.info("Device type: %s", device_type)
        logger.info("MQTT enabled: %s", config['mqtt'].getboolean('enabled'))
        logger.info("")
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
        if renogy_client:
            renogy_client.stop()
        await scanner.stop()
        await discovery.stop()
        await api_server.stop()
        logger.info("Stopped")


if __name__ == "__main__":
    asyncio.run(main())
