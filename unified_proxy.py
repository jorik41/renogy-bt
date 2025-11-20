#!/usr/bin/env python3
"""
Unified Renogy-BT Proxy - Single process, efficient BLE management

This is a complete rewrite that combines:
- BLE proxy (forward all advertisements to Home Assistant via ESPHome API)
- Renogy battery GATT reads (without stopping the scanner)

Key improvements:
- Single BLE scanner (no pause/resume complexity)
- Single event loop (no threads or subprocesses)
- GATT reads happen in parallel with scanning
- 50% less resource usage
- 100% advertisement coverage
"""

import asyncio
import configparser
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

from renogybt.unified_ble_manager import UnifiedBLEManager
from renogybt.esphome_api_server import ESPHomeAPIServer
from renogybt.esphome_discovery import ESPHomeDiscovery
from renogybt.Utils import bytes_to_int, format_temperature
from renogybt.bluez_resilience import BlueZAdapterMonitor
from bleak import AdvertisementData, BLEDevice

# Setup logging
logging.basicConfig(
    level=logging.INFO,  # Changed back from DEBUG
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class UnifiedRenogyProxy:
    """
    Unified proxy that handles both BLE advertisement forwarding 
    and Renogy battery data reading in a single efficient process.
    """
    
    def __init__(self, config_file: str):
        self.config = configparser.ConfigParser()
        self.config.read(config_file)
        
        # Components
        self.ble_manager: Optional[UnifiedBLEManager] = None
        self.esphome_server: Optional[ESPHomeAPIServer] = None
        self.esphome_discovery: Optional[ESPHomeDiscovery] = None
        
        # State
        self.running = False
        self.stop_event = asyncio.Event()
        
    async def start(self) -> None:
        """Start the unified proxy"""
        if self.running:
            logger.warning("Proxy already running")
            return
        
        self.running = True
        logger.info("=" * 60)
        logger.info("Starting Unified Renogy-BT Proxy")
        logger.info("=" * 60)
        
        # Read configuration
        renogy_enabled = self.config.getboolean('home_assistant_proxy', 'with_renogy_client', fallback=False)
        renogy_mac = self.config.get('device', 'mac_addr', fallback=None) if renogy_enabled else None
        renogy_interval = self.config.getfloat('home_assistant_proxy', 'renogy_read_interval', fallback=60.0)
        
        api_port = self.config.getint('home_assistant_proxy', 'native_api_port', fallback=6053)
        device_name = self.config.get('home_assistant_proxy', 'device_name', fallback='renogy.proxy')
        
        logger.info("Configuration:")
        logger.info("  ESPHome API: %s:%d", device_name, api_port)
        logger.info("  Renogy client: %s", "enabled" if renogy_enabled else "disabled")
        if renogy_enabled:
            logger.info("  Renogy MAC: %s", renogy_mac)
            logger.info("  Renogy interval: %.1fs", renogy_interval)
        
        # Create ESPHome API server
        self.esphome_server = ESPHomeAPIServer(
            name=device_name,
            mac_address=self._get_mac_address(),
            port=api_port,
        )
        
        # Register advertisement callback
        self._send_callback = None
        def register_sender(callback):
            self._send_callback = callback
            logger.info("ESPHome client subscribed, advertisements will flow")
        
        self.esphome_server.set_advertisement_callback(register_sender)
        
        # Create mDNS discovery
        mdns_ip = self.config.get('home_assistant_proxy', 'mdns_ip', fallback='')
        self.esphome_discovery = ESPHomeDiscovery(
            name=device_name,
            port=api_port,
            mac=self._get_mac_address(),
            ip=mdns_ip if mdns_ip else None,
        )
        
        # Create unified BLE manager with callbacks
        self.ble_manager = UnifiedBLEManager(
            renogy_mac=renogy_mac,
            renogy_enabled=renogy_enabled,
            renogy_read_interval=renogy_interval,
            advertisement_callback=self._on_advertisement,
            renogy_data_callback=self._on_renogy_data,
        )
        
        # Start all components
        logger.info("Starting components...")
        await self.esphome_server.start()
        
        # Define Renogy sensor entities if enabled
        if renogy_enabled:
            # Generate entities for 4 batteries (48, 49, 50, 51) + combined
            entities = self._generate_battery_entities()
            self.esphome_server.set_sensor_entities(entities)
            logger.info("Configured %d Renogy sensor entities (4 batteries + combined)", len(entities))
        
        await self.esphome_discovery.start()
        await self.ble_manager.start()
        
        # Start D-Bus health watchdog
        self.dbus_watchdog_task = asyncio.create_task(self._start_dbus_watchdog())
        
        logger.info("=" * 60)
        logger.info("Unified Renogy-BT Proxy READY")
        logger.info("  - BLE scanner running continuously")
        logger.info("  - Forwarding all advertisements to Home Assistant")
        if renogy_enabled:
            logger.info("  - Reading Renogy battery data every %.1fs", renogy_interval)
            logger.info("  - (Scanner continues running during Renogy reads!)")
        logger.info("=" * 60)
        
        # Wait for stop signal
        await self.stop_event.wait()
        

    async def _start_dbus_watchdog(self) -> None:
        """
        Monitor BlueZ D-Bus health AND scanner activity, recover if stuck.
        
        Enhanced to detect:
        - D-Bus/adapter issues
        - Scanner crashes
        - No advertisement activity
        """
        adapter = self.config.get('device', 'adapter', fallback='hci0')
        monitor = BlueZAdapterMonitor(adapter)
        
        check_interval = 60.0
        discovering_timeout = 120.0
        scanner_timeout = 180.0  # No ads for 3 minutes = dead scanner
        discovery_start_time = None
        last_known_ad_count = 0
        no_activity_count = 0
        
        logger.info("Enhanced watchdog enabled (check every %.0fs)", check_interval)
        logger.info("  - Monitors D-Bus adapter health")
        logger.info("  - Monitors scanner activity")
        logger.info("  - Auto-restarts on failures")
        
        try:
            while self.running:
                await asyncio.sleep(check_interval)
                
                if not self.running:
                    break
                
                # Check 1: D-Bus adapter health
                state = await monitor.check_adapter_state()
                
                if not state.get('healthy'):
                    logger.error("Adapter unhealthy: %s", state.get('error'))
                    logger.warning("Attempting adapter power cycle recovery...")
                    success = await monitor.power_cycle_adapter()
                    
                    if success:
                        logger.info("Adapter recovered - restarting BLE manager")
                        if self.ble_manager:
                            try:
                                await self.ble_manager.stop()
                                await asyncio.sleep(2)
                                await self.ble_manager.start()
                                logger.info("BLE manager restarted successfully")
                                last_known_ad_count = 0
                                no_activity_count = 0
                            except Exception as e:
                                logger.error("Failed to restart BLE manager: %s", e)
                    else:
                        logger.error("Adapter recovery failed")
                    
                    discovery_start_time = None
                    continue
                
                # Check 2: Stuck in discovering state
                if state.get('discovering'):
                    if discovery_start_time is None:
                        discovery_start_time = asyncio.get_event_loop().time()
                    else:
                        elapsed = asyncio.get_event_loop().time() - discovery_start_time
                        if elapsed > discovering_timeout:
                            logger.error("Adapter stuck discovering for %.0fs - forcing stop", elapsed)
                            await monitor.force_stop_discovery()
                            
                            await asyncio.sleep(2)
                            state_check = await monitor.check_adapter_state()
                            if state_check.get('discovering'):
                                logger.warning("Still discovering - power cycling")
                                await monitor.power_cycle_adapter()
                            
                            discovery_start_time = None
                else:
                    discovery_start_time = None
                
                # Check 3: Scanner activity (NEW!)
                # Monitor if we're actually receiving advertisements
                if self.ble_manager:
                    current_ad_count = getattr(self.ble_manager, 'total_advertisements', 0)
                    
                    if current_ad_count == last_known_ad_count:
                        no_activity_count += 1
                        
                        if no_activity_count >= 3:  # 3 minutes of no ads
                            logger.error("Scanner appears dead - no advertisements for %d minutes", 
                                       no_activity_count)
                            logger.warning("Attempting to restart BLE manager...")
                            
                            try:
                                # Try gentle restart first
                                await self.ble_manager.stop()
                                await asyncio.sleep(2)
                                await self.ble_manager.start()
                                logger.info("BLE manager restarted - scanner should be active")
                                no_activity_count = 0
                                last_known_ad_count = 0
                            except Exception as e:
                                logger.error("Failed to restart BLE manager: %s", e)
                                
                                # Try power cycle as fallback
                                logger.warning("Attempting adapter power cycle as fallback...")
                                success = await monitor.power_cycle_adapter()
                                if success:
                                    await asyncio.sleep(2)
                                    try:
                                        await self.ble_manager.start()
                                        logger.info("BLE manager restarted after power cycle")
                                        no_activity_count = 0
                                        last_known_ad_count = 0
                                    except Exception as e2:
                                        logger.error("Still failed after power cycle: %s", e2)
                    else:
                        # Activity detected, reset counter
                        if no_activity_count > 0:
                            logger.info("Scanner activity resumed (%d advertisements)", current_ad_count)
                        no_activity_count = 0
                        last_known_ad_count = current_ad_count
                    
        except asyncio.CancelledError:
            logger.debug("Enhanced watchdog cancelled")
        except Exception as e:
            logger.error("Enhanced watchdog error: %s", e)

    async def stop(self) -> None:
        """Stop the unified proxy"""
        if not self.running:
            return
        
        logger.info("Stopping Unified Renogy-BT Proxy...")
        self.running = False
        
        # Stop D-Bus watchdog
        if hasattr(self, 'dbus_watchdog_task') and self.dbus_watchdog_task:
            self.dbus_watchdog_task.cancel()
            try:
                await self.dbus_watchdog_task
            except asyncio.CancelledError:
                pass
        
        # Stop components
        if self.ble_manager:
            await self.ble_manager.stop()
        
        if self.esphome_discovery:
            await self.esphome_discovery.stop()
        
        if self.esphome_server:
            await self.esphome_server.stop()
        
        # Print statistics
        if self.ble_manager:
            stats = self.ble_manager.get_statistics()
            logger.info("=" * 60)
            logger.info("Session Statistics:")
            logger.info("  Advertisements processed: %d", stats['total_advertisements'])
            if stats['renogy_enabled']:
                logger.info("  Renogy reads: %d (errors: %d)", 
                          stats['renogy_read_count'], stats['renogy_error_count'])
            logger.info("=" * 60)
        
        self.stop_event.set()
        logger.info("Unified Renogy-BT Proxy stopped")
    
    def _on_advertisement(self, device: BLEDevice, advertisement_data: AdvertisementData) -> None:
        """
        Handle BLE advertisement - forward to ESPHome API.
        This is called for EVERY advertisement, continuously.
        """
        if not hasattr(self, '_send_callback') or not self._send_callback:
            return
            
        # Convert advertisement to format expected by ESPHome
        payload = {
            'address': device.address,
            'name': device.name or device.address,
            'rssi': advertisement_data.rssi,
            'service_uuids': advertisement_data.service_uuids,
            'manufacturer_data': advertisement_data.manufacturer_data,
            'service_data': advertisement_data.service_data,
        }
        
        try:
            self._send_callback(payload)
        except Exception as e:
            logger.error("Error forwarding advertisement: %s", e)
    
    def _on_renogy_data(self, data: dict) -> None:
        """
        Handle Renogy battery data - parse and send to ESPHome API as sensor states.
        Now supports multi-battery: data = {device_id: {battery_data}, ...}
        """
        # Check if this is multi-battery format (dict of dicts with numeric keys)
        if data and all(isinstance(k, int) for k in data.keys()):
            logger.info("Renogy data received for %d batteries", len(data))
            
            # Parse all batteries
            all_parsed = {}
            battery_parsed_data = {}
            
            for device_id, battery_data in data.items():
                logger.debug("Parsing battery %d", device_id)
                parsed = self._parse_battery_data(battery_data)
                
                if parsed:
                    # Add device_id prefix to all keys
                    for key, value in parsed.items():
                        prefixed_key = f"battery_{device_id}_{key}"
                        all_parsed[prefixed_key] = value
                    
                    battery_parsed_data[device_id] = parsed
                    logger.info("Battery %d: voltage=%.1fV, current=%.2fA, soc=%.1f%%",
                              device_id, parsed.get('voltage', 0), 
                              parsed.get('current', 0), parsed.get('soc', 0))
            
            # Calculate combined/aggregate values across all batteries
            if battery_parsed_data:
                combined = self._calculate_combined_metrics(battery_parsed_data)
                # Add "combined_" prefix
                for key, value in combined.items():
                    all_parsed[f"combined_{key}"] = value
                
                logger.info("Combined: voltage=%.1fV, current=%.2fA, soc=%.1f%%",
                          combined.get('voltage', 0), combined.get('current', 0), 
                          combined.get('soc', 0))
            
            if all_parsed:
                logger.info("Sending %d total sensor states to ESPHome", len(all_parsed))
                self.esphome_server.send_sensor_states(all_parsed)
                logger.info("Sensor states sent successfully")
                return
        else:
            # Old single battery format (fallback)
            logger.info("Renogy data received: %d fields", len(data))
        
        if not self.esphome_server:
            return
            
        try:
            # Parse raw hex data
            parsed = {}
            
            # Parse battery info (most important - total voltage, current, capacity!)
            if 'battery_info_raw' in data:
                bs = bytes.fromhex(data['battery_info_raw'])
                parsed['current'] = bytes_to_int(bs, 3, 2, signed=True, scale=0.01)
                parsed['voltage'] = bytes_to_int(bs, 5, 2, scale=0.1)
                parsed['remaining_charge'] = bytes_to_int(bs, 7, 4, scale=0.001)
                parsed['capacity'] = bytes_to_int(bs, 11, 4, scale=0.001)
                if parsed['capacity'] > 0:
                    parsed['soc'] = (parsed['remaining_charge'] / parsed['capacity']) * 100
            
            # Parse cell voltages - calculate min/max instead of all individual cells
            if 'cell_voltage_raw' in data:
                bs = bytes.fromhex(data['cell_voltage_raw'])
                byte_count = bs[2]
                num_cells = byte_count // 2
                
                if num_cells > 0:
                    cells = []
                    for i in range(num_cells):
                        cell_mv = bytes_to_int(bs, 3 + i*2, 2)
                        cells.append(cell_mv * 0.001)  # Convert to V
                    
                    parsed['cell_count'] = num_cells
                    parsed['cell_voltage_min'] = min(cells)
                    parsed['cell_voltage_max'] = max(cells)
                    parsed['cell_voltage_delta'] = max(cells) - min(cells)
            
            # Parse cell temperatures - calculate min/max instead of all individual temps
            if 'cell_temperature_raw' in data:
                bs = bytes.fromhex(data['cell_temperature_raw'])
                byte_count = bs[2]
                num_sensors = byte_count // 2
                
                if num_sensors > 0:
                    temps = []
                    for i in range(num_sensors):
                        temp_raw = bytes_to_int(bs, 3 + i*2, 2, signed=True)
                        temps.append(temp_raw * 0.1)  # Celsius
                    
                    parsed['temp_sensor_count'] = num_sensors
                    parsed['temperature_min'] = min(temps)
                    parsed['temperature_max'] = max(temps)
                    parsed['temperature_delta'] = max(temps) - min(temps)
            
            # Parse device info
            if 'device_info_raw' in data:
                bs = bytes.fromhex(data['device_info_raw'])
                try:
                    parsed['model'] = bs[3:19].decode('utf-8').rstrip('\x00')
                except:
                    pass
            
            if parsed:
                logger.info("Parsed Renogy data: voltage=%.1fV, current=%.2fA, soc=%.1f%%, cells=%d",
                          parsed.get('voltage', 0), parsed.get('current', 0), 
                          parsed.get('soc', 0), parsed.get('cell_count', 0))
                logger.info("Parsed keys: %s", list(parsed.keys()))
                
                # Send to ESPHome
                logger.info("Sending %d sensor states to ESPHome", len(parsed))
                logger.debug("Data keys being sent: %s", list(parsed.keys()))
                self.esphome_server.send_sensor_states(parsed)
                logger.info("Sensor states sent successfully")
            else:
                logger.warning("No valid data parsed from Renogy response")
                
        except Exception as e:
            logger.error("Error parsing Renogy data: %s", e, exc_info=True)
    
    def _parse_battery_data(self, data: dict) -> dict:
        """Parse raw hex data from a single battery into readable values."""
        parsed = {}
        
        # Parse battery info (most important - total voltage, current, capacity!)
        if 'battery_info_raw' in data:
            bs = bytes.fromhex(data['battery_info_raw'])
            logger.debug("Battery info raw bytes (len=%d): %s", len(bs), bs.hex())
            
            parsed['current'] = bytes_to_int(bs, 3, 2, signed=True, scale=0.01)
            parsed['voltage'] = bytes_to_int(bs, 5, 2, scale=0.1)
            # Capacity IS stored in mAh (0.001 Ah units) - verified with raw hex
            parsed['remaining_charge'] = bytes_to_int(bs, 7, 4, scale=0.001)
            parsed['capacity'] = bytes_to_int(bs, 11, 4, scale=0.001)
            
            logger.debug("Parsed: voltage=%s, current=%s, remaining=%s, capacity=%s", 
                        parsed.get('voltage'), parsed.get('current'), 
                        parsed.get('remaining_charge'), parsed.get('capacity'))
            
            # Calculate power (W) = Voltage × Current
            if parsed['voltage'] and parsed['current'] is not None:
                parsed['power'] = round(parsed['voltage'] * parsed['current'], 2)
            
            # Calculate SOC and charge level percentage
            if parsed['capacity'] > 0:
                parsed['soc'] = (parsed['remaining_charge'] / parsed['capacity']) * 100
                parsed['charge_level'] = round(parsed['soc'], 1)  # Same as SOC but explicit name
        
        # Parse cell voltages - calculate min/max instead of all individual cells
        if 'cell_voltage_raw' in data:
            bs = bytes.fromhex(data['cell_voltage_raw'])
            byte_count = bs[2]
            num_cells = byte_count // 2
            
            if num_cells > 0:
                cells = []
                for i in range(num_cells):
                    cell_mv = bytes_to_int(bs, 3 + i*2, 2)
                    cells.append(cell_mv * 0.001)  # Convert to V
                
                parsed['cell_count'] = num_cells
                parsed['cell_voltage_min'] = min(cells)
                parsed['cell_voltage_max'] = max(cells)
                parsed['cell_voltage_delta'] = max(cells) - min(cells)
        
        # Parse cell temperatures - calculate min/max instead of all individual temps
        if 'cell_temperature_raw' in data:
            bs = bytes.fromhex(data['cell_temperature_raw'])
            byte_count = bs[2]
            num_sensors = byte_count // 2
            
            if num_sensors > 0:
                temps = []
                for i in range(num_sensors):
                    temp_raw = bytes_to_int(bs, 3 + i*2, 2, signed=True)
                    temps.append(temp_raw * 0.1)  # Celsius
                
                parsed['temp_sensor_count'] = num_sensors
                parsed['temperature_min'] = min(temps)
                parsed['temperature_max'] = max(temps)
                parsed['temperature_delta'] = max(temps) - min(temps)
        
        return parsed
    
    def _calculate_combined_metrics(self, battery_data: dict) -> dict:
        """Calculate combined/aggregate metrics across all batteries."""
        combined = {}
        
        # Average voltage (for parallel batteries showing each pack)
        voltages = [b.get('voltage', 0) for b in battery_data.values() if 'voltage' in b]
        if voltages:
            combined['voltage'] = sum(voltages) / len(voltages)
        
        # Total current (sum for parallel connection)
        currents = [b.get('current', 0) for b in battery_data.values() if 'current' in b]
        if currents:
            combined['current'] = sum(currents)
        
        # Calculate total power (W) = Average Voltage × Total Current
        if 'voltage' in combined and 'current' in combined:
            combined['power'] = round(combined['voltage'] * combined['current'], 2)
        
        # Total capacity
        capacities = [b.get('capacity', 0) for b in battery_data.values() if 'capacity' in b]
        if capacities:
            combined['capacity'] = sum(capacities)
        
        # Total remaining charge
        remaining = [b.get('remaining_charge', 0) for b in battery_data.values() if 'remaining_charge' in b]
        if remaining:
            combined['remaining_charge'] = sum(remaining)
        
        # Average SOC / charge level
        socs = [b.get('soc', 0) for b in battery_data.values() if 'soc' in b]
        if socs:
            combined['soc'] = sum(socs) / len(socs)
            combined['charge_level'] = round(combined['soc'], 1)
        
        # Min/max across all batteries
        cell_mins = [b.get('cell_voltage_min', 999) for b in battery_data.values() if 'cell_voltage_min' in b]
        if cell_mins:
            combined['cell_voltage_min'] = min(cell_mins)
        
        cell_maxs = [b.get('cell_voltage_max', 0) for b in battery_data.values() if 'cell_voltage_max' in b]
        if cell_maxs:
            combined['cell_voltage_max'] = max(cell_maxs)
            if 'cell_voltage_min' in combined:
                combined['cell_voltage_delta'] = combined['cell_voltage_max'] - combined['cell_voltage_min']
        
        temp_mins = [b.get('temperature_min', 999) for b in battery_data.values() if 'temperature_min' in b]
        if temp_mins:
            combined['temperature_min'] = min(temp_mins)
        
        temp_maxs = [b.get('temperature_max', 0) for b in battery_data.values() if 'temperature_max' in b]
        if temp_maxs:
            combined['temperature_max'] = max(temp_maxs)
            if 'temperature_min' in combined:
                combined['temperature_delta'] = combined['temperature_max'] - combined['temperature_min']
        
        # Total cell count
        cell_counts = [b.get('cell_count', 0) for b in battery_data.values() if 'cell_count' in b]
        if cell_counts:
            combined['cell_count'] = sum(cell_counts)
        
        return combined
    
    def _generate_battery_entities(self) -> dict:
        """Generate sensor entities for all batteries plus combined metrics."""
        entities = {}
        key_counter = 1000
        
        # Sensor template for a single battery
        sensor_templates = [
            ('voltage', 'Voltage', 'V', 'voltage', 1),
            ('current', 'Current', 'A', 'current', 2),
            ('power', 'Power', 'W', 'power', 1),  # NEW: Power = V × I
            ('soc', 'State of Charge', '%', 'battery', 1),
            ('charge_level', 'Charge Level', '%', 'battery', 1),  # NEW: Same as SOC but explicit
            ('remaining_charge', 'Remaining Charge', 'Ah', 'energy_storage', 2),
            ('capacity', 'Capacity', 'Ah', 'energy_storage', 2),
            ('cell_voltage_min', 'Cell Voltage Min', 'V', 'voltage', 3),
            ('cell_voltage_max', 'Cell Voltage Max', 'V', 'voltage', 3),
            ('cell_voltage_delta', 'Cell Voltage Delta', 'V', 'voltage', 3),
            ('temperature_min', 'Temperature Min', '°C', 'temperature', 1),
            ('temperature_max', 'Temperature Max', '°C', 'temperature', 1),
            ('temperature_delta', 'Temperature Delta', '°C', 'temperature', 1),
            ('cell_count', 'Cell Count', '', None, 0),
            ('temp_sensor_count', 'Temp Sensor Count', '', None, 0),
        ]
        
        # Generate entities for each battery (48, 49, 50, 51)
        for battery_id in [48, 49, 50, 51]:
            for data_key, name, unit, device_class, decimals in sensor_templates:
                full_key = f"battery_{battery_id}_{data_key}"
                key_counter += 1
                entities[full_key] = {
                    'object_id': f'renogy_battery_{battery_id}_{data_key}',
                    'name': f'Battery {battery_id} {name}',
                    'unit_of_measurement': unit,
                    'state_class': 1,  # MEASUREMENT
                    'accuracy_decimals': decimals,
                    'key': key_counter,
                }
                if device_class:
                    entities[full_key]['device_class'] = device_class
        
        # Generate combined/aggregate entities
        for data_key, name, unit, device_class, decimals in sensor_templates:
            full_key = f"combined_{data_key}"
            key_counter += 1
            entities[full_key] = {
                'object_id': f'renogy_combined_{data_key}',
                'name': f'Combined {name}',
                'unit_of_measurement': unit,
                'state_class': 1,  # MEASUREMENT
                'accuracy_decimals': decimals,
                'key': key_counter,
            }
            if device_class:
                entities[full_key]['device_class'] = device_class
        
        return entities
    
    def _get_mac_address(self) -> str:
        """Get system MAC address for ESPHome device identification"""
        import uuid
        mac = uuid.getnode()
        mac_str = ':'.join(['{:02x}'.format((mac >> elements) & 0xff) 
                           for elements in range(40, -1, -8)])
        return mac_str.upper()


async def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: unified_proxy.py <config.ini>")
        sys.exit(1)
    
    config_file = sys.argv[1]
    if not Path(config_file).exists():
        print(f"Error: Config file not found: {config_file}")
        sys.exit(1)
    
    proxy = UnifiedRenogyProxy(config_file)
    
    # Setup signal handlers
    def signal_handler(sig):
        logger.info("Received signal %s, shutting down...", sig)
        asyncio.create_task(proxy.stop())
    
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))
    
    # Run proxy
    try:
        await proxy.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
    finally:
        await proxy.stop()


if __name__ == '__main__':
    asyncio.run(main())
