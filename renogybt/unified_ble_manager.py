"""
Unified BLE Manager - Single scanner for both proxy and Renogy reads

This module provides a unified BLE management system that:
- Runs a continuous BLE scanner for advertisement forwarding (proxy mode)
- Performs GATT reads on Renogy battery WITHOUT stopping the scanner
- Uses a single event loop, no threads or subprocess spawning
- Efficient resource usage: one scanner, one process
"""

import asyncio
import logging
import time
from typing import Optional, Callable, Dict, Any
from bleak import BleakClient, BleakScanner, AdvertisementData, BLEDevice
from bleak.exc import BleakError

logger = logging.getLogger(__name__)


class UnifiedBLEManager:
    """
    Unified BLE Manager that handles both passive scanning (proxy) and 
    active GATT connections (Renogy) efficiently in a single process.
    """
    
    def __init__(
        self,
        renogy_mac: Optional[str] = None,
        renogy_enabled: bool = True,
        renogy_read_interval: float = 60.0,
        advertisement_callback: Optional[Callable[[BLEDevice, AdvertisementData], None]] = None,
        renogy_data_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.renogy_mac = renogy_mac.upper() if renogy_mac else None
        self.renogy_enabled = renogy_enabled and renogy_mac is not None
        self.renogy_read_interval = renogy_read_interval
        self.advertisement_callback = advertisement_callback
        self.renogy_data_callback = renogy_data_callback
        
        # State
        self.scanner: Optional[BleakScanner] = None
        self.renogy_device: Optional[BLEDevice] = None
        self.last_renogy_read: float = 0
        self.running: bool = False
        self.renogy_task: Optional[asyncio.Task] = None
        
        # Statistics
        self.total_advertisements = 0
        self.renogy_read_count = 0
        self.renogy_error_count = 0
        
    async def start(self) -> None:
        """Start the unified BLE manager"""
        if self.running:
            logger.warning("UnifiedBLEManager already running")
            return
            
        self.running = True
        logger.info("Starting UnifiedBLEManager (Renogy: %s)", "enabled" if self.renogy_enabled else "disabled")
        
        # Create scanner with advertisement callback
        self.scanner = BleakScanner(detection_callback=self._on_advertisement)
        
        # Start continuous scanning
        await self.scanner.start()
        logger.info("BLE scanner started (continuous mode)")
        
        # Start Renogy reader task if enabled
        if self.renogy_enabled:
            self.renogy_task = asyncio.create_task(self._renogy_reader_loop())
            logger.info("Renogy reader loop started (interval: %.1fs)", self.renogy_read_interval)
    
    async def stop(self) -> None:
        """Stop the unified BLE manager"""
        if not self.running:
            return
            
        logger.info("Stopping UnifiedBLEManager...")
        self.running = False
        
        # Stop Renogy reader task
        if self.renogy_task:
            self.renogy_task.cancel()
            try:
                await self.renogy_task
            except asyncio.CancelledError:
                pass
            self.renogy_task = None
        
        # Stop scanner
        if self.scanner:
            try:
                await self.scanner.stop()
                logger.info("BLE scanner stopped")
            except Exception as e:
                logger.warning("Error stopping scanner: %s", e)
            self.scanner = None
        
        logger.info("UnifiedBLEManager stopped (ads: %d, renogy reads: %d, errors: %d)",
                   self.total_advertisements, self.renogy_read_count, self.renogy_error_count)
    
    def _on_advertisement(self, device: BLEDevice, advertisement_data: AdvertisementData) -> None:
        """
        Handle BLE advertisement.
        This callback is invoked by the scanner for EVERY advertisement.
        """
        self.total_advertisements += 1
        
        # Track Renogy device when seen
        if self.renogy_enabled and device.address.upper() == self.renogy_mac:
            self.renogy_device = device
        
        # Forward advertisement to proxy callback
        if self.advertisement_callback:
            try:
                self.advertisement_callback(device, advertisement_data)
            except Exception as e:
                logger.error("Error in advertisement callback: %s", e)
    
    async def _renogy_reader_loop(self) -> None:
        """
        Background task that reads Renogy battery data periodically.
        Scanner continues running during these reads!
        """
        logger.info("Renogy reader loop starting...")
        
        # Wait a bit for initial advertisements
        await asyncio.sleep(10)
        
        while self.running:
            try:
                # Check if we should read
                elapsed = time.time() - self.last_renogy_read
                if elapsed >= self.renogy_read_interval and self.renogy_device:
                    logger.info("Triggering Renogy read (device: %s)", self.renogy_device.address)
                    await self._read_renogy_data()
                elif not self.renogy_device:
                    logger.debug("Renogy device not yet discovered")
                else:
                    logger.debug("Waiting for next read interval (%.1fs remaining)", 
                               self.renogy_read_interval - elapsed)
                
                # Sleep until next check
                await asyncio.sleep(min(10.0, self.renogy_read_interval))
                
            except asyncio.CancelledError:
                logger.info("Renogy reader loop cancelled")
                break
            except Exception as e:
                logger.error("Error in Renogy reader loop: %s", e)
                await asyncio.sleep(30)  # Back off on error
    
    async def _read_renogy_data(self) -> None:
        """
        Read data from Renogy battery via GATT.
        Supports multiple device IDs (48, 49, 50, 51) for multi-battery systems.
        Temporarily pause scanner during GATT connection (BlueZ limitation).
        """
        if not self.renogy_device:
            logger.warning("Cannot read Renogy: device not discovered")
            return
        
        start_time = time.time()
        logger.info("Reading Renogy battery data (pausing scanner temporarily)...")
        
        # BlueZ limitation: Must stop scanner before GATT connection
        scanner_was_running = self.scanner and self.running
        if scanner_was_running:
            try:
                await self.scanner.stop()
                logger.debug("Scanner paused for GATT read")
            except Exception as e:
                logger.warning("Error stopping scanner: %s", e)
        
        try:
            # Connect to device
            async with BleakClient(self.renogy_device.address, timeout=20.0) as client:
                logger.info("Connected to Renogy battery")
                
                # Device IDs to read (48, 49, 50, 51 for 4 batteries)
                device_ids = [48, 49, 50, 51]
                
                # Read register sections SEQUENTIALLY (concurrent reads interfere)
                sections = [
                    (5000, 17, "cell_voltage"),
                    (5017, 17, "cell_temperature"),
                    (5042, 6, "battery_info"),
                    (5122, 8, "device_info"),
                    (5223, 1, "device_address"),
                ]
                
                all_batteries_data = {}
                
                # Read each battery sequentially
                for device_id in device_ids:
                    logger.info("Reading battery device ID %d...", device_id)
                    battery_data = {}
                    
                    for register, words, name in sections:
                        result = await self._read_register_section(
                            client, register, words, name, device_id
                        )
                        if isinstance(result, Exception):
                            logger.warning("Device %d register %d (%s) failed: %s", 
                                         device_id, register, name, result)
                        elif result:
                            battery_data.update(result)
                        # Small delay between reads
                        await asyncio.sleep(0.2)
                    
                    # Store with device_id prefix
                    if battery_data:
                        all_batteries_data[device_id] = battery_data
                        logger.debug("Device %d: collected %d data fields", device_id, len(battery_data))
                    
                    # Small delay between batteries
                    await asyncio.sleep(0.3)
                
                # Send data to callback
                if all_batteries_data and self.renogy_data_callback:
                    try:
                        self.renogy_data_callback(all_batteries_data)
                    except Exception as e:
                        logger.error("Error in Renogy data callback: %s", e)
                
                self.renogy_read_count += 1
                self.last_renogy_read = time.time()
                
                elapsed = time.time() - start_time
                logger.info("Renogy read complete in %.2fs (%d batteries)", 
                          elapsed, len(all_batteries_data))
                
        except asyncio.TimeoutError:
            logger.error("Renogy read timed out")
            self.renogy_error_count += 1
        except BleakError as e:
            logger.error("Renogy read failed: %s", e)
            self.renogy_error_count += 1
            # Clear device to trigger rediscovery
            self.renogy_device = None
        except Exception as e:
            logger.error("Unexpected error reading Renogy: %s", e)
            self.renogy_error_count += 1
        finally:
            # Always restart scanner
            if scanner_was_running and self.running:
                try:
                    await asyncio.sleep(0.2)  # Brief settle time
                    await self.scanner.start()
                    logger.info("Scanner resumed after Renogy read")
                except Exception as e:
                    logger.error("Error restarting scanner: %s", e)
    
    async def _read_register_section(
        self, 
        client: BleakClient, 
        register: int, 
        words: int, 
        name: str,
        device_id: int = 0xFF
    ) -> Optional[Dict[str, Any]]:
        """
        Read a single register section from Renogy battery.
        
        This uses the standard Modbus-over-BLE protocol used by Renogy devices.
        device_id: Modbus device ID (48, 49, 50, 51 for multi-battery, 0xFF for broadcast)
        """
        try:
            # Construct Modbus read request
            function = 0x03  # Read holding registers
            
            request = bytearray([
                device_id,
                function,
                (register >> 8) & 0xFF,  # Register high byte
                register & 0xFF,          # Register low byte
                (words >> 8) & 0xFF,      # Word count high byte
                words & 0xFF,             # Word count low byte
            ])
            
            # Add CRC
            crc = self._calculate_crc(request)
            request.extend(crc)
            
            # Write UUID (standard for Renogy BT-2 module)
            write_uuid = "0000ffd1-0000-1000-8000-00805f9b34fb"
            notify_uuid = "0000fff1-0000-1000-8000-00805f9b34fb"
            
            logger.debug("Reading register %d (%s): request=%s", register, name, request.hex())
            
            # Setup notification handler
            response_future = asyncio.Future()
            response_data = []
            
            def notification_handler(sender, data):
                logger.debug("Notification received for %s: %d bytes: %s", name, len(data), data.hex())
                response_data.append(bytes(data))
                if not response_future.done():
                    response_future.set_result(bytes(data))
            
            # Start notifications
            await client.start_notify(notify_uuid, notification_handler)
            logger.debug("Notifications started for %s", name)
            
            # Send request
            await client.write_gatt_char(write_uuid, request, response=False)
            logger.debug("Request sent for %s", name)
            
            # Wait for response (with timeout)
            try:
                response = await asyncio.wait_for(response_future, timeout=8.0)
            except asyncio.TimeoutError:
                logger.warning("Register %d (%s) read timeout (no notification received)", register, name)
                await client.stop_notify(notify_uuid)
                return None
            
            await client.stop_notify(notify_uuid)
            logger.debug("Got response for %s: %s", name, response.hex())
            
            # Parse response
            if len(response) >= 5 and response[1] == 0x03:
                logger.info("Register %d (%s) read successful: %d bytes", 
                           register, name, len(response))
                # Return raw data for now - parsing handled elsewhere
                return {f"{name}_raw": response.hex()}
            else:
                logger.warning("Register %d (%s) invalid response: %s", register, name, response.hex())
                return None
                
        except Exception as e:
            logger.warning("Register %d (%s) read error: %s", register, name, e)
            return None
    
    @staticmethod
    def _calculate_crc(data: bytearray) -> bytes:
        """Calculate Modbus CRC-16"""
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x0001:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        return bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get current statistics"""
        return {
            "running": self.running,
            "total_advertisements": self.total_advertisements,
            "renogy_enabled": self.renogy_enabled,
            "renogy_read_count": self.renogy_read_count,
            "renogy_error_count": self.renogy_error_count,
            "renogy_device_found": self.renogy_device is not None,
            "last_renogy_read": self.last_renogy_read,
        }
