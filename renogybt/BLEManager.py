import asyncio
import inspect
import logging
from bleak import BLEDevice, BleakClient, BleakScanner
from bleak.exc import BleakError

CREATE_TASK = getattr(asyncio, "create_task", asyncio.ensure_future)

DISCOVERY_TIMEOUT = 5  # max wait time to complete the bluetooth scanning (seconds)
DISCOVER_RETRIES = 3   # number of times to retry discovery on failure
DISCOVER_DELAY = 5     # wait time between retries (seconds)
CONNECT_RETRIES = 3    # number of times to retry connecting
MAX_BACKOFF_DELAY = 30  # maximum exponential backoff delay (seconds)

class BLEManager:
    def __init__(
        self,
        mac_address,
        alias,
        on_data,
        on_connect_fail,
        write_service_uuid,
        notify_char_uuid,
        write_char_uuid,
        adapter=None,
        *,
        discovery_timeout=DISCOVERY_TIMEOUT,
        discover_retries=DISCOVER_RETRIES,
        discover_delay=DISCOVER_DELAY,
    ):
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
        self._discovery_timeout = discovery_timeout
        self._discover_retries = max(1, discover_retries)
        self._discover_delay = max(0, discover_delay)
        self._reconnect_count = 0

    async def discover(self):
        mac_address = self.mac_address.upper()
        for attempt in range(1, self._discover_retries + 1):
            try:
                logging.info("Starting discovery (attempt %s)...", attempt)
                self.discovered_devices = await BleakScanner.discover(
                    timeout=self._discovery_timeout,
                    adapter=self.adapter,
                )
                logging.info("Devices found: %s", len(self.discovered_devices))
                for dev in self.discovered_devices:
                    logging.debug(
                        "Discovered device: %s (%s)",
                        getattr(dev, "address", "unknown"),
                        getattr(dev, "name", ""),
                    )
                if self.discovered_devices:
                    break
                logging.warning(
                    "No BLE devices discovered (attempt %s/%s); retrying after %ss",
                    attempt,
                    self._discover_retries,
                    self._discover_delay,
                )
            except BleakError as exc:
                logging.error("Discovery failed: %s", exc)
            # Exponential backoff for retries
            if attempt < self._discover_retries:
                delay = min(self._discover_delay * (2 ** (attempt - 1)), MAX_BACKOFF_DELAY)
                await asyncio.sleep(delay)
            else:
                self.connect_fail_callback(Exception("Discovery exhausted without finding target"))
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
                # Reset reconnect counter on successful connection
                self._reconnect_count = 0
                return
            except Exception as exc:
                logging.error("Error connecting to device: %s", exc)
                # Exponential backoff for connection retries
                if attempt < CONNECT_RETRIES:
                    self._reconnect_count += 1
                    delay = min(self._discover_delay * (2 ** (self._reconnect_count - 1)), MAX_BACKOFF_DELAY)
                    await asyncio.sleep(delay)
                else:
                    self.connect_fail_callback(exc)

    def notification_callback(self, _, data: bytearray):
        logging.info("notification_callback")
        try:
            result = self.data_callback(data)
            if inspect.isawaitable(result):
                CREATE_TASK(result)
        except Exception as exc:
            logging.error("Notification callback failed: %s", exc)

    async def characteristic_write_value(self, data):
        if not self.client or not self.client.is_connected:
            raise BleakError("Client is not connected")
        if self.write_char_handle is None:
            raise BleakError("Write characteristic handle is not available")
        try:
            logging.debug(f'writing to {self.write_char_uuid} {data}')
            await self.client.write_gatt_char(self.write_char_handle, bytearray(data), response=False)
            logging.debug('characteristic_write_value succeeded')
            await asyncio.sleep(0.5)
        except Exception as e:
            logging.error('characteristic_write_value failed %s', e)
            raise

    async def disconnect(self):
        if self.client and self.client.is_connected:
            logging.info(f"Exit: Disconnecting device: {self.device.name} {self.device.address}")
            await self.client.disconnect()
