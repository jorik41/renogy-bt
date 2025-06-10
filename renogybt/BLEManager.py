import asyncio
import logging
import sys
from bleak import BleakClient, BleakScanner, BLEDevice
from bleak.exc import BleakError

DISCOVERY_TIMEOUT = 5  # max wait time to complete the bluetooth scanning (seconds)
DISCOVER_RETRIES = 3   # number of times to retry discovery on failure
DISCOVER_DELAY = 5     # wait time between retries (seconds)
CONNECT_RETRIES = 3    # number of times to retry connecting

class BLEManager:
    def __init__(self, mac_address, alias, on_data, on_connect_fail, write_service_uuid, notify_char_uuid, write_char_uuid, adapter=None):
        self.mac_address = mac_address
        self.device_alias = alias
        self.data_callback = on_data
        self.connect_fail_callback = on_connect_fail
        self.write_service_uuid = write_service_uuid
        self.notify_char_uuid = notify_char_uuid
        self.write_char_uuid = write_char_uuid
        self.write_char_handle = None
        self.device: BLEDevice = None
        self.client: BleakClient = None
        self.discovered_devices = []
        self.adapter = adapter

    async def discover(self):
        mac_address = self.mac_address.upper()
        for attempt in range(1, DISCOVER_RETRIES + 1):
            try:
                logging.info("Starting discovery (attempt %s)...", attempt)
                self.discovered_devices = await BleakScanner.discover(
                    timeout=DISCOVERY_TIMEOUT,
                    adapter=self.adapter,
                )
                logging.info("Devices found: %s", len(self.discovered_devices))
                break
            except BleakError as exc:
                logging.error("Discovery failed: %s", exc)
                if attempt < DISCOVER_RETRIES:
                    await asyncio.sleep(DISCOVER_DELAY)
                else:
                    self.connect_fail_callback(sys.exc_info())
                    return

        for dev in self.discovered_devices:
            if dev.address is not None and (
                dev.address.upper() == mac_address
                or (dev.name and dev.name.strip() == self.device_alias)
            ):
                logging.info(f"Found matching device {dev.name} => {dev.address}")
                self.device = dev

    async def connect(self):
        if not self.device:
            return logging.error("No device connected!")

        for attempt in range(1, CONNECT_RETRIES + 1):
            self.client = BleakClient(self.device)
            try:
                logging.info("Connecting to device (attempt %s)...", attempt)
                await self.client.connect()
                logging.info(f"Client connection: {self.client.is_connected}")
                if not self.client.is_connected:
                    raise BleakError("Unable to connect")

                for service in self.client.services:
                    for characteristic in service.characteristics:
                        if characteristic.uuid == self.notify_char_uuid:
                            await self.client.start_notify(
                                characteristic, self.notification_callback
                            )
                            logging.info(
                                f"subscribed to notification {characteristic.uuid}"
                            )
                        if (
                            characteristic.uuid == self.write_char_uuid
                            and service.uuid == self.write_service_uuid
                        ):
                            self.write_char_handle = characteristic.handle
                            logging.info(
                                f"found write characteristic {characteristic.uuid}, service {service.uuid}"
                            )
                return
            except Exception as exc:
                logging.error("Error connecting to device: %s", exc)
                if attempt < CONNECT_RETRIES:
                    await asyncio.sleep(DISCOVER_DELAY)
                else:
                    self.connect_fail_callback(sys.exc_info())

    async def notification_callback(self, _, data: bytearray):
        logging.info("notification_callback")
        await self.data_callback(data)

    async def characteristic_write_value(self, data):
        try:
            logging.info(f'writing to {self.write_char_uuid} {data}')
            await self.client.write_gatt_char(self.write_char_handle, bytearray(data), response=False)
            logging.info('characteristic_write_value succeeded')
            await asyncio.sleep(0.5)
        except Exception as e:
            logging.info(f'characteristic_write_value failed {e}')

    async def disconnect(self):
        if self.client and self.client.is_connected:
            logging.info(f"Exit: Disconnecting device: {self.device.name} {self.device.address}")
            await self.client.disconnect()
