import asyncio
import configparser
import logging
import traceback
from typing import Callable, Iterable, List, Optional

from .BLEManager import (
    BLEManager,
    DISCOVER_DELAY,
    DISCOVER_RETRIES,
    DISCOVERY_TIMEOUT,
)
from .Utils import bytes_to_int, crc16_modbus, int_to_bytes

# Base class that works with all Renogy family devices
# Should be extended by each client with its own parsers and section definitions
# Section example: {'register': 5000, 'words': 8, 'parser': self.parser_func}

ALIAS_PREFIXES = ['BT-TH', 'RNGRBP', 'BTRIC']
WRITE_SERVICE_UUID = "0000ffd0-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID  = "0000ffd1-0000-1000-8000-00805f9b34fb"
READ_TIMEOUT = 15 # (seconds)
READ_SUCCESS = 3
READ_ERROR = 131
CREATE_TASK = getattr(asyncio, "create_task", asyncio.ensure_future)

class BaseClient:
    def __init__(self, config):
        self.config: configparser.ConfigParser = config
        self.ble_manager: Optional[BLEManager] = None
        self.device = None
        self.poll_timer = None
        self.read_timeout = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.future: Optional[asyncio.Future] = None

        device_id_str = (self.config['device'].get('device_id') or "").strip()
        if not device_id_str:
            raise ValueError("Config option 'device_id' must contain at least one id")
        try:
            self.device_ids: List[int] = [int(x.strip()) for x in device_id_str.split(',') if x.strip()]
        except ValueError as exc:
            raise ValueError(f"Invalid device_id list '{device_id_str}'") from exc
        if not self.device_ids:
            raise ValueError("No valid device ids were found in configuration")

        self.device_index = 0
        self.device_id = self.device_ids[self.device_index]
        self.sections = []
        self.section_index = 0
        self.reset_device_data()
        self.discovery_timeout = self.config['device'].getint(
            'discovery_timeout', fallback=DISCOVERY_TIMEOUT
        )
        self.discovery_retries = self.config['device'].getint(
            'discovery_retries', fallback=DISCOVER_RETRIES
        )
        self.discovery_delay = self.config['device'].getint(
            'discovery_delay', fallback=DISCOVER_DELAY
        )
        logging.info(
            "Init %s: %s => %s",
            self.__class__.__name__,
            self.config['device']['alias'],
            self.config['device']['mac_addr'],
        )

    def start(self):
        try:
            if self.loop and self.loop.is_running():
                raise RuntimeError("Event loop already running")
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.future = self.loop.create_future()
            self.loop.create_task(self.connect())
            self.loop.run_until_complete(self.future)
        except Exception as e:
            self.__on_error(e)
        except KeyboardInterrupt:
            self.__on_error("KeyboardInterrupt")
        finally:
            if self.loop:
                try:
                    all_tasks_fn: Callable[[Optional[asyncio.AbstractEventLoop]], Iterable[asyncio.Task]]
                    all_tasks_fn = getattr(asyncio, "all_tasks", None)  # type: ignore[assignment]
                    if all_tasks_fn is None:
                        all_tasks_fn = asyncio.Task.all_tasks  # type: ignore[attr-defined]

                    pending = [
                        task for task in all_tasks_fn(self.loop)
                        if task is not self.future and not task.done()
                    ]
                    # Only cancel and wait for a limited number of tasks to avoid hanging
                    if pending:
                        for task in pending:
                            task.cancel()
                        # Use wait_for with timeout to avoid indefinite waiting
                        try:
                            self.loop.run_until_complete(
                                asyncio.wait_for(
                                    asyncio.gather(*pending, return_exceptions=True),
                                    timeout=5.0
                                )
                            )
                        except asyncio.TimeoutError:
                            logging.warning("Timeout waiting for tasks to complete during cleanup")
                except Exception:
                    pass
                finally:
                    self.loop.close()
                    asyncio.set_event_loop(None)
                    self.loop = None
                    self.future = None
                    # Cleanup data logger if present
                    if hasattr(self, 'data_logger'):
                        try:
                            self.data_logger.cleanup()
                        except Exception:
                            pass

    async def connect(self):
        self.ble_manager = BLEManager(
            mac_address=self.config['device']['mac_addr'],
            alias=self.config['device']['alias'],
            on_data=self.on_data_received,
            on_connect_fail=self.__on_connect_fail,
            notify_char_uuid=NOTIFY_CHAR_UUID,
            write_char_uuid=WRITE_CHAR_UUID,
            write_service_uuid=WRITE_SERVICE_UUID,
            adapter=self.config['device'].get('adapter'),
            discovery_timeout=self.discovery_timeout,
            discover_retries=self.discovery_retries,
            discover_delay=self.discovery_delay,
        )
        await self.ble_manager.discover()

        if not self.ble_manager.device:
            logging.error(f"Device not found: {self.config['device']['alias']} => {self.config['device']['mac_addr']}, please check the details provided.")
            for dev in self.ble_manager.discovered_devices:
                if dev.name != None and dev.name.startswith(tuple(ALIAS_PREFIXES)):
                    logging.info(f"Possible device found! ====> {dev.name} > [{dev.address}]")
            self.stop()
        else:
            await self.ble_manager.connect()
            if self.ble_manager.client and self.ble_manager.client.is_connected: await self.read_section()

    async def disconnect(self):
        if self.ble_manager:
            await self.ble_manager.disconnect()
            self.ble_manager = None
        if self.future and not self.future.done():
            self.future.set_result('DONE')

    async def on_data_received(self, response):
        if self.read_timeout and not self.read_timeout.cancelled(): self.read_timeout.cancel()
        operation = bytes_to_int(response, 1, 1)

        if operation == READ_SUCCESS or operation == READ_ERROR:
            if (operation == READ_SUCCESS and
                self.section_index < len(self.sections) and
                self.sections[self.section_index]['parser'] != None and
                self.sections[self.section_index]['words'] * 2 + 5 == len(response)):
                # call the parser and update data
                logging.debug(f"on_data_received: read operation success")
                self.__safe_parser(self.sections[self.section_index]['parser'], response)
            else:
                logging.debug(f"on_data_received: read operation failed: {response.hex()}")

            if self.section_index >= len(self.sections) - 1: # last section, read complete
                self.section_index = 0
                self.on_read_operation_complete()
                if self.device_index >= len(self.device_ids) - 1:
                    self.device_index = 0
                    self.device_id = self.device_ids[self.device_index]
                    await self.check_polling()
                else:
                    self.device_index += 1
                    self.device_id = self.device_ids[self.device_index]
                    await asyncio.sleep(0.5)
                    await self.read_section()
            else:
                self.section_index += 1
                await asyncio.sleep(0.5)
                await self.read_section()
        else:
            logging.warning("on_data_received: unknown operation={}".format(operation))

    def on_read_operation_complete(self):
        logging.debug("on_read_operation_complete")
        self.data['__device'] = self.config['device']['alias']
        self.data['__client'] = self.__class__.__name__
        self.__safe_callback(self.on_data_callback, self.data)
        self.reset_device_data()

    def on_read_timeout(self):
        logging.error("on_read_timeout => Timed out! Please check your device_id!")
        self.stop()

    async def check_polling(self):
        if self.config['data'].getboolean('enable_polling'): 
            await asyncio.sleep(self.config['data'].getint('poll_interval'))
            await self.read_section()

    async def read_section(self):
        index = self.section_index
        if self.device_id == None or len(self.sections) == 0:
            return logging.error("BaseClient cannot be used directly")

        # Check if still connected before reading
        if not self.ble_manager or not self.ble_manager.client or not self.ble_manager.client.is_connected:
            logging.warning("BLE connection lost, attempting to reconnect")
            try:
                await self.connect()
            except Exception as exc:
                logging.error("Failed to reconnect: %s", exc)
                self.stop()
                return

        self.read_timeout = self.loop.call_later(READ_TIMEOUT, self.on_read_timeout)
        request = self.create_generic_read_request(self.device_id, 3, self.sections[index]['register'], self.sections[index]['words']) 
        try:
            await self.ble_manager.characteristic_write_value(request)
        except Exception as exc:
            logging.error("Failed to write characteristic: %s", exc)
            if self.read_timeout and not self.read_timeout.cancelled():
                self.read_timeout.cancel()
            # Try to recover by stopping and letting the system retry
            self.stop()

    def create_generic_read_request(self, device_id, function, regAddr, readWrd):                             
        data = None                                
        if regAddr != None and readWrd != None:
            data = []
            data.append(device_id)
            data.append(function)
            data.append(int_to_bytes(regAddr, 0))
            data.append(int_to_bytes(regAddr, 1))
            data.append(int_to_bytes(readWrd, 0))
            data.append(int_to_bytes(readWrd, 1))

            crc = crc16_modbus(bytes(data))
            data.append(crc[0])
            data.append(crc[1])
            logging.debug("{} {} => {}".format("create_request_payload", regAddr, data))
        return data

    def __on_error(self, error = None):
        logging.error(f"Exception occurred: {error}")
        self.__safe_callback(self.on_error_callback, error)
        self.stop()

    def __on_connect_fail(self, error):
        logging.error(f"Connection failed: {error}")
        self.__safe_callback(self.on_error_callback, error)
        self.stop()

    def reset_device_data(self):
        self.data = {}

    def stop(self):
        if self.read_timeout and not self.read_timeout.cancelled(): 
            self.read_timeout.cancel()
        if not self.loop or self.loop.is_closed():
            return
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(lambda: CREATE_TASK(self.disconnect()))
        else:
            self.loop.run_until_complete(self.disconnect())

    def __safe_callback(self, calback, param):
        if calback is not None:
            try:
                calback(self, param)
            except Exception as e:
                logging.error(f"__safe_callback => exception in callback! {e}")
                traceback.print_exc()

    def __safe_parser(self, parser, param):
        if parser is not None:
            try:
                parser(param)
            except Exception as e:
                logging.error(f"exception in parser! {e}")
                traceback.print_exc()
