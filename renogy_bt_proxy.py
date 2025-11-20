"""Combined Renogy client + ESPHome Bluetooth proxy service."""

from __future__ import annotations

import argparse
import asyncio
import configparser
import contextlib
import logging
import re
import signal
import sys
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus

from bleak import AdvertisementData, BLEDevice, BleakScanner
from bleak.exc import BleakDBusError, BleakError

from renogybt import (
    BatteryClient,
    DataLogger,
    DCChargerClient,
    ESPHomeAPIServer,
    ESPHomeDiscovery,
    InverterClient,
    RoverClient,
    RoverHistoryClient,
    Utils,
    create_sensor_entities_from_data,
    update_sensor_entities,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Skip forwarding advertisements that originate from the local adapter
ADAPTER_NAME_PATTERN = re.compile(r"^hci\d+\s+\([0-9A-Fa-f:]+\)$")


def _is_in_progress_error(exc: Exception) -> bool:
    """Return True if the exception indicates an in-progress BlueZ operation."""
    if isinstance(exc, BleakDBusError):
        if exc.dbus_error == "org.bluez.Error.InProgress":
            return True
        if exc.dbus_error == "org.bluez.Error.Failed" and "No discovery started" in str(
            exc
        ):
            return True
    message = str(exc)
    return "InProgress" in message or "in progress" in message


def _is_not_ready_error(exc: Exception) -> bool:
    """Return True if the exception indicates the adapter is not ready yet."""
    if isinstance(exc, BleakDBusError):
        if exc.dbus_error == "org.bluez.Error.NotReady":
            return True
    message = str(exc)
    return "NotReady" in message or "Not Ready" in message


async def _power_cycle_adapter(adapter: str, delay: float = 1.0) -> None:
    """Toggle the BlueZ adapter power to recover from stuck discovery."""
    path = f"/org/bluez/{adapter}"
    bus = MessageBus(bus_type=BusType.SYSTEM)
    await bus.connect()
    try:
        introspection = await bus.introspect("org.bluez", path)
        proxy = bus.get_proxy_object("org.bluez", path, introspection)
        props = proxy.get_interface("org.freedesktop.DBus.Properties")
        logger.warning("Power cycling BLE adapter %s to recover discovery", adapter)
        with contextlib.suppress(Exception):
            await props.call_set("org.bluez.Adapter1", "Discovering", Variant("b", False))
        await props.call_set("org.bluez.Adapter1", "Powered", Variant("b", False))
        await asyncio.sleep(delay)
        await props.call_set("org.bluez.Adapter1", "Powered", Variant("b", True))
        await asyncio.sleep(delay)
    finally:
        bus.disconnect()


class ScannerSupervisor:
    """Manage BLE scanner runtime, pausing during Renogy operations and applying duty cycle."""

    def __init__(
        self,
        scanner: BleakScanner,
        *,
        loop: asyncio.AbstractEventLoop,
        active_time: float = 0.0,
        idle_time: float = 0.0,
    ) -> None:
        self._scanner = scanner
        self._loop = loop
        self._active_time = max(0.0, active_time)
        self._idle_time = max(0.0, idle_time)
        self._lock = asyncio.Lock()
        self._running = False
        self._pause_tokens = 0
        self._duty_task: Optional[asyncio.Task] = None
        self._shutdown = False
        self._retry_handle: Optional[asyncio.Handle] = None

    @property
    def duty_cycle_enabled(self) -> bool:
        return self._active_time > 0 and self._idle_time > 0

    async def start(self) -> None:
        await self._ensure_running("initial start")
        if self.duty_cycle_enabled and not self._duty_task:
            self._duty_task = asyncio.create_task(self._run_duty_cycle())

    async def pause(self, reason: str) -> None:
        async with self._lock:
            self._pause_tokens += 1
            logger.debug(
                "ScannerSupervisor pause (%s); tokens=%d", reason, self._pause_tokens
            )
            if self._pause_tokens == 1:
                await self._set_running_locked(False, f"pause:{reason}")

    async def resume(self, reason: str) -> None:
        async with self._lock:
            if self._pause_tokens == 0:
                logger.debug(
                    "ScannerSupervisor resume (%s) skipped; already resumed", reason
                )
                return
            self._pause_tokens -= 1
            logger.debug(
                "ScannerSupervisor resume (%s); tokens=%d",
                reason,
                self._pause_tokens,
            )
            if self._pause_tokens == 0:
                await self._set_running_locked(True, f"resume:{reason}")

    def pause_from_thread(self, reason: str) -> None:
        future = asyncio.run_coroutine_threadsafe(self.pause(reason), self._loop)
        with contextlib.suppress(Exception):
            future.result(timeout=5)

    def resume_from_thread(self, reason: str) -> None:
        future = asyncio.run_coroutine_threadsafe(self.resume(reason), self._loop)
        with contextlib.suppress(Exception):
            future.result(timeout=5)

    def kick_from_thread(self, reason: str) -> None:
        future = asyncio.run_coroutine_threadsafe(self._kick(reason), self._loop)
        with contextlib.suppress(Exception):
            future.result(timeout=5)

    async def shutdown(self) -> None:
        self._shutdown = True
        self._cancel_start_retry()
        if self._duty_task:
            self._duty_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._duty_task
            self._duty_task = None
        async with self._lock:
            await self._set_running_locked(False, "shutdown")

    async def _ensure_running(self, reason: str) -> None:
        async with self._lock:
            await self._set_running_locked(True, reason)

    async def _set_running(self, target: bool, reason: str) -> None:
        async with self._lock:
            await self._set_running_locked(target, reason)

    async def _set_running_locked(self, target: bool, reason: str) -> None:
        if target:
            if self._running or self._shutdown or self._pause_tokens > 0:
                logger.debug(
                    "ScannerSupervisor start skipped (%s); running=%s shutdown=%s tokens=%d",
                    reason,
                    self._running,
                    self._shutdown,
                    self._pause_tokens,
                )
                return
            try:
                await self._scanner.start()
            except BleakError as exc:
                if _is_in_progress_error(exc):
                    logger.debug(
                        "BLE scanner already running when starting (%s): %s",
                        reason,
                        exc,
                    )
                    self._running = True
                    return
                if _is_not_ready_error(exc):
                    logger.warning(
                        "BLE scanner not ready when starting (%s): %s",
                        reason,
                        exc,
                    )
                    self._schedule_start_retry(f"retry:{reason}")
                    return
                logger.error("Failed to start BLE scanner (%s): %s", reason, exc)
                raise
            self._running = True
            self._cancel_start_retry()
            logger.debug("BLE scanner started (%s)", reason)
        else:
            if not self._running:
                logger.debug("ScannerSupervisor stop skipped (%s); already stopped", reason)
                return
            try:
                await self._scanner.stop()
            except BleakError as exc:
                if _is_in_progress_error(exc):
                    logger.debug(
                        "BLE scanner stop already in progress (%s): %s",
                        reason,
                        exc,
                    )
                else:
                    logger.error("Failed to stop BLE scanner (%s): %s", reason, exc)
                    raise
            self._running = False
            self._cancel_start_retry()
            logger.debug("BLE scanner stopped (%s)", reason)

    async def _kick(self, reason: str) -> None:
        async with self._lock:
            # Force a stop/start cycle to ensure BlueZ keeps streaming advertisements.
            logger.debug("ScannerSupervisor kick requested (%s)", reason)
            await self._set_running_locked(False, f"kick-stop:{reason}")
            await self._set_running_locked(True, f"kick-start:{reason}")

    async def _run_duty_cycle(self) -> None:
        try:
            while not self._shutdown:
                await asyncio.sleep(self._active_time)
                if self._shutdown:
                    break
                await self._set_running(False, "duty-cycle pause")
                await asyncio.sleep(self._idle_time)
                if self._shutdown:
                    break
                await self._ensure_running("duty-cycle resume")
        except asyncio.CancelledError:
            pass

    def _schedule_start_retry(self, reason: str, delay: float = 2.0) -> None:
        if self._shutdown:
            return

        def _retry() -> None:
            self._retry_handle = None
            if self._shutdown:
                return
            self._loop.create_task(self._ensure_running(reason))

        logger.warning(
            "Scheduling BLE scanner start retry (%s) in %.1fs",
            reason,
            delay,
        )
        self._cancel_start_retry()
        self._retry_handle = self._loop.call_later(delay, _retry)

    def _cancel_start_retry(self) -> None:
        if self._retry_handle:
            self._retry_handle.cancel()
            self._retry_handle = None


class AirtimeScheduler:
    """Coordinate BLE scanner airtime between Renogy operations and proxy scanning."""

    def __init__(
        self,
        supervisor: Optional[ScannerSupervisor],
        *,
        loop: asyncio.AbstractEventLoop,
        resume_window: float,
        settle_time: float,
        cycle_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        self._supervisor = supervisor
        self._loop = loop
        self._resume_window = max(0.0, resume_window)
        self._settle_time = max(0.0, settle_time)
        self._resume_handle: Optional[asyncio.Handle] = None
        self._window_handle: Optional[asyncio.Handle] = None
        self._pending_reason: Optional[str] = None
        self._cycle_callback = cycle_callback

    def pause(self, reason: str) -> None:
        if not self._supervisor:
            return
        logger.debug("AirtimeScheduler pause: %s", reason)

        def _apply_pause() -> None:
            # Do not cancel pending resume so outstanding tokens can drain.
            self._cancel_handles(cancel_resume=False)
            logger.debug("AirtimeScheduler pause applied: %s", reason)
            self._supervisor.pause_from_thread(reason)

        self._loop.call_soon_threadsafe(_apply_pause)

    def resume_window(self, reason: str) -> None:
        if not self._supervisor:
            return
        logger.debug(
            "AirtimeScheduler resume_window start: %s (settle=%.3fs window=%.3fs)",
            reason,
            self._settle_time,
            self._resume_window,
        )
        logger.debug("AirtimeScheduler dispatching resume scheduling: %s", reason)

        def _schedule_resume() -> None:
            self._cancel_handles(cancel_resume=False)
            self._pending_reason = reason
            if self._resume_handle:
                logger.debug(
                    "AirtimeScheduler resume already pending (reason=%s); keeping existing timer",
                    self._pending_reason,
                )
                return
            logger.debug("AirtimeScheduler scheduling resume: %s", reason)

            def _do_resume() -> None:
                pending_reason = self._pending_reason or reason
                self._pending_reason = None
                logger.debug("AirtimeScheduler resume executing: %s", pending_reason)
                self._resume_handle = None
                self._supervisor.resume_from_thread(pending_reason)
                if self._cycle_callback:
                    try:
                        self._cycle_callback()
                    except Exception:
                        logger.exception("AirtimeScheduler cycle callback failed")
                if self._resume_window > 0:
                    logger.debug(
                        "AirtimeScheduler scheduling window pause in %.3fs for %s",
                        self._resume_window,
                        pending_reason,
                    )

                    def _pause_window() -> None:
                        logger.debug(
                            "AirtimeScheduler window pause firing for %s",
                            pending_reason,
                        )
                        self._window_handle = None
                        self._supervisor.pause_from_thread("airtime-window")

                    self._window_handle = self._loop.call_later(
                        self._resume_window,
                        _pause_window,
                    )

            if self._settle_time <= 0:
                _do_resume()
            else:
                self._resume_handle = self._loop.call_later(
                    self._settle_time,
                    _do_resume,
                )

        self._loop.call_soon_threadsafe(_schedule_resume)

    def cancel(self) -> None:
        self._cancel_handles()

    def _cancel_handles(
        self, *, cancel_resume: bool = True, cancel_window: bool = True
    ) -> None:
        if cancel_resume and self._resume_handle:
            logger.debug("AirtimeScheduler cancelling pending resume")
            self._resume_handle.cancel()
            self._resume_handle = None
            self._pending_reason = None
        if cancel_window and self._window_handle:
            logger.debug("AirtimeScheduler cancelling pending window pause")
            self._window_handle.cancel()
            self._window_handle = None


def _format_mac(raw: int) -> str:
    """Format a MAC address from the integer returned by uuid.getnode()."""
    return ":".join(f"{(raw >> shift) & 0xFF:02X}" for shift in range(40, -8, -8))


def _determine_proxy_mac(config: configparser.ConfigParser) -> str:
    """Derive the MAC address to advertise for the ESPHome device."""
    for key in ("mac", "native_api_mac"):
        if config.has_option("home_assistant_proxy", key):
            candidate = config["home_assistant_proxy"].get(key, "").strip()
            if candidate:
                return candidate.upper()
    node = uuid.getnode()
    if (node >> 40) % 2:
        logger.warning(
            "uuid.getnode() returned a locally administered MAC. "
            "Consider setting [home_assistant_proxy].mac explicitly."
        )
    return _format_mac(node)


def _create_client(
    config: configparser.ConfigParser,
    data_logger: DataLogger,
    api_server: Optional[ESPHomeAPIServer] = None,
    scheduled_mode: bool = False,
    failure_counter: Optional[List[int]] = None,  # Pass failure counter as mutable list
):
    """Instantiate the appropriate Renogy client."""

    alias = config["device"]["alias"]
    battery_map: Dict[int, Dict[str, object]] = {}
    sensor_entities_initialized_ids = set()

    def on_data_received(client, data):
        nonlocal sensor_entities_initialized_ids
        
        # Reset failure counter on successful data read
        if failure_counter and failure_counter[0] > 0:
            logger.info("Renogy connection successful - resetting failure counter (was %d)", failure_counter[0])
            failure_counter[0] = 0
        
        Utils.add_calculated_values(data)
        dev_id = data.get("device_id")
        alias_id = f"{alias}_{dev_id}" if dev_id is not None else alias

        Utils.update_energy_totals(
            data,
            interval_sec=config["data"].getint("poll_interval", fallback=0),
            file_path=config["device"]["energy_file"],
            alias=alias_id,
        )

        fields = config["data"].get("fields", fallback="")
        filtered_data = Utils.filter_fields(data, fields)
        logger.info("%s => %s", client.ble_manager.device.name, filtered_data)

        # Initialize sensor entities on first data read
        if api_server is not None and alias_id not in sensor_entities_initialized_ids:
            try:
                temp_unit = config["data"].get("temperature_unit", fallback="C")
                device_num = dev_id if isinstance(dev_id, int) else 48
                base_key = 1000 + (device_num - 48) * 1000
                entities = create_sensor_entities_from_data(filtered_data, alias_id, temp_unit, base_key)
                # Merge entities for multiple batteries, don't replace
                api_server.set_sensor_entities(entities, replace=False)
                sensor_entities_initialized_ids.add(alias_id)
                logger.info("Initialized %d sensor entities for ESPHome API", len(entities))
            except Exception as exc:
                logger.error("Failed to initialize sensor entities: %s", exc)

        # Send sensor states to ESPHome API if enabled
        if api_server is not None:
            try:
                api_server.send_sensor_states(filtered_data)
                logger.debug("Sent sensor states to ESPHome API")
            except Exception as exc:
                logger.error("Failed to send sensor states: %s", exc)

        if config["device"]["type"] == "RNG_BATT" and len(client.device_ids) > 1:
            if dev_id is not None:
                battery_map[dev_id] = data
            if len(battery_map) == len(client.device_ids):
                combined = Utils.combine_battery_readings(battery_map)
                filtered_combined = Utils.filter_fields(combined, fields)
                logger.info("combined => %s", filtered_combined)
                # Initialize sensor entities for combined data if not already done
                combined_alias = f"{alias}_combined"
                if api_server is not None and combined_alias not in sensor_entities_initialized_ids:
                    try:
                        temp_unit = config["data"].get("temperature_unit", fallback="C")
                        entities = create_sensor_entities_from_data(filtered_combined, combined_alias, temp_unit, base_key=5000)
                        # Merge combined entities with individual battery entities
                        api_server.set_sensor_entities(entities, replace=False)
                        sensor_entities_initialized_ids.add(combined_alias)
                        logger.info("Initialized %d sensor entities for combined data (total: %d)", len(entities), len(api_server._sensor_entities))
                        
                        # Force HA to reconnect now that ALL entities are configured
                        logger.warning("All battery entities configured - disconnecting clients to force full re-discovery")
                        for protocol in list(api_server._active_protocols):
                            try:
                                protocol._transport.close()
                            except Exception:
                                pass
                    except Exception as exc:
                        logger.error("Failed to initialize combined sensor entities: %s", exc)
                # Send combined data to ESPHome API
                if api_server is not None:
                    try:
                        api_server.send_sensor_states(filtered_combined)
                    except Exception as exc:
                        logger.error("Failed to send combined sensor states: %s", exc)
                battery_map.clear()

        if config["remote_logging"].getboolean("enabled"):
            data_logger.log_remote(json_data=filtered_data)
        if (
            config["pvoutput"].getboolean("enabled")
            and config["device"]["type"] == "RNG_CTRL"
        ):
            data_logger.log_pvoutput(json_data=filtered_data)
        
        # In scheduled mode, stop after reading all batteries (not just one)
        # For multi-battery setups, wait until all batteries are read
        should_stop = False
        if scheduled_mode or not config["data"].getboolean("enable_polling"):
            # If multi-battery setup, only stop after all batteries are read
            if config["device"]["type"] == "RNG_BATT" and len(client.device_ids) > 1:
                # battery_map was already populated above at line 471
                if len(battery_map) >= len(client.device_ids):
                    should_stop = True
                    logger.info("All %d batteries read, stopping client", len(client.device_ids))
            else:
                # Single battery or non-battery device, stop immediately
                should_stop = True
        
        if should_stop:
            client.stop()

    def on_error(client, error):
        logger.error("Proxy battery client error: %s", error)
        
        # Check if this is a discovery/connection failure and update counter if provided
        if failure_counter:
            error_str = str(error).lower()
            if "discovery" in error_str or "connection" in error_str or "connect" in error_str:
                failure_counter[0] += 1
                logger.warning(
                    "Renogy connection failure count: %d",
                    failure_counter[0]
                )

    device_type = config["device"].get("type", "").upper()

    if device_type == "RNG_CTRL":
        return RoverClient(config, on_data_received, on_error)
    if device_type == "RNG_CTRL_HIST":
        return RoverHistoryClient(config, on_data_received, on_error)
    if device_type == "RNG_BATT":
        return BatteryClient(config, on_data_received, on_error)
    if device_type == "RNG_INVT":
        return InverterClient(config, on_data_received, on_error)
    if device_type == "RNG_DCC":
        return DCChargerClient(config, on_data_received, on_error)

    raise ValueError(f"Unsupported device type '{device_type}'")


def _extract_adv_flags(advertisement: AdvertisementData) -> Optional[int]:
    """Attempt to derive the Flags AD value from platform-specific metadata."""
    platform_data = getattr(advertisement, "platform_data", ())
    if not platform_data or len(platform_data) < 2:
        return None
    props = platform_data[1]
    if not isinstance(props, dict):
        return None
    adv_data = props.get("AdvertisingData") or {}
    if isinstance(adv_data, dict):
        flags = adv_data.get(0x01)
        if isinstance(flags, (bytes, bytearray)) and flags:
            return flags[0]
    return None


def _ble_packet_to_dict(device: BLEDevice, advertisement: AdvertisementData) -> Dict[str, object]:
    """Translate bleak advertisement structures to ESPHome payload format."""
    manufacturer_data = {
        str(k): bytes(v) for k, v in (advertisement.manufacturer_data or {}).items()
    }
    service_data = {
        k: bytes(v) for k, v in (advertisement.service_data or {}).items()
    }
    return {
        "address": device.address,
        "rssi": advertisement.rssi,
        "address_type": "random" if getattr(device, "address_type", "public") == "random" else "public",
        "name": advertisement.local_name or "",
        "manufacturer_data": manufacturer_data,
        "service_data": service_data,
        "service_uuids": list(advertisement.service_uuids or []),
        "tx_power": advertisement.tx_power,
        "flags": _extract_adv_flags(advertisement),
    }


async def run_proxy(config_path: Path) -> None:
    """Main proxy coroutine with event loop responsiveness improvements."""
    
    async def event_loop_heartbeat():
        """Periodic task to keep event loop responsive."""
        try:
            while True:
                await asyncio.sleep(0.05)  # Yield every 50ms
        except asyncio.CancelledError:
            pass
    
    config = configparser.ConfigParser(inline_comment_prefixes=("#",))
    config.read(config_path)

    proxy_section = "home_assistant_proxy"
    if not config.getboolean(proxy_section, "enabled", fallback=False):
        raise RuntimeError(f"{proxy_section}.enabled must be true")
    if not config.getboolean(proxy_section, "use_native_api", fallback=True):
        raise RuntimeError("Native API mode is required in this release")

    adapter = config.get(proxy_section, "adapter", fallback=None)
    if adapter is None and config.has_section("device"):
        adapter = config.get("device", "adapter", fallback="hci0")
    adapter = adapter or "hci0"

    device_name = config.get(proxy_section, "device_name", fallback="renogy-bt-proxy")
    native_port = config.getint(proxy_section, "native_api_port", fallback=6053)
    proxy_mac = _determine_proxy_mac(config)
    
    # Check if ESPHome API sensors are enabled
    esphome_sensors_enabled = config.getboolean(proxy_section, "esphome_sensors", fallback=True)

    energy_file = str((config_path.parent / "energy_totals.json").resolve())
    config["device"]["energy_file"] = energy_file
    data_logger = DataLogger(config)

    with_renogy_client = config.getboolean(proxy_section, "with_renogy_client", fallback=True)
    renogy_poll_mode = config.get(proxy_section, "renogy_poll_mode", fallback="continuous").lower()
    renogy_read_interval = max(0.0, config.getfloat(proxy_section, "renogy_read_interval", fallback=60.0))
    battery_client = None
    battery_future: Optional[asyncio.Future] = None
    battery_stopping = False
    battery_restart_lock = asyncio.Lock()
    last_battery_restart: float = 0.0
    consecutive_timeouts = 0
    consecutive_failures_list = [0]  # Use list for mutable reference
    bt_reset_threshold = 3  # Reset BT after this many consecutive failures
    last_bt_reset: float = 0.0
    renogy_scheduler_task: Optional[asyncio.Task] = None

    async def _restart_battery_client(
        reason: str,
        exc: Optional[BaseException],
        client_error: Optional[str],
    ) -> None:
        nonlocal battery_client, battery_future, battery_stopping, last_battery_restart, consecutive_timeouts
        if not with_renogy_client:
            return
        async with battery_restart_lock:
            if stop_event.is_set() or battery_stopping:
                return
            timeout_error = client_error == "read_timeout"
            if timeout_error:
                consecutive_timeouts += 1
                logger.warning(
                    "Renogy client timed out waiting for data (count=%d)", consecutive_timeouts
                )
            else:
                consecutive_timeouts = 0
                if client_error:
                    logger.warning("Renogy client stopped (%s); restarting", client_error)
                elif exc:
                    logger.warning("Renogy client stopped (%s): %s", reason, exc)
                else:
                    logger.warning("Renogy client stopped (%s); restarting", reason)
            loop_obj = asyncio.get_running_loop()
            now = loop_obj.time()
            if last_battery_restart and now - last_battery_restart < 20:
                delay = 20 - (now - last_battery_restart)
                logger.debug(
                    "Delaying Renogy client restart by %.1fs to respect cooldown", delay
                )
                await asyncio.sleep(delay)
            should_power_cycle = (
                not timeout_error or consecutive_timeouts >= 3 or exc is not None
            )
            if should_power_cycle:
                logger.warning("Power cycling BLE adapter %s to recover discovery", adapter)
                try:
                    await _power_cycle_adapter(adapter)
                except Exception as power_exc:
                    logger.error("Adapter power cycle failed: %s", power_exc)
                await asyncio.sleep(5)
            else:
                logger.info("Restarting Renogy client without power cycle")
                await asyncio.sleep(2)
            if stop_event.is_set() or battery_stopping:
                return
            last_battery_restart = asyncio.get_running_loop().time()
            await start_battery_client()

    api_server = ESPHomeAPIServer(
        name=device_name,
        mac_address=proxy_mac,
        port=native_port,
        version="2024.12.0",
    )
    discovery = ESPHomeDiscovery(
        name=device_name,
        port=native_port,
        mac=proxy_mac,
        ip=config.get(proxy_section, "mdns_ip", fallback=None),
    )

    send_advertisement_callback: Optional[Callable[[Dict[str, object]], None]] = None

    def register_advertisement_sender(callback: Callable[[Dict[str, object]], None]) -> None:
        nonlocal send_advertisement_callback, last_adv_timestamp, total_advertisements
        send_advertisement_callback = callback
        logger.info("ESPHome client subscribed to BLE advertisements")
        # Emit a synthetic advertisement so Home Assistant immediately sees the proxy
        test_payload = {
            "address": proxy_mac,
            "rssi": -40,
            "address_type": "public",
            "name": "renogy-bt-proxy",
            "manufacturer_data": {},
            "service_data": {},
            "service_uuids": [],
            "tx_power": None,
            "flags": 0x06,
        }
        try:
            callback(test_payload)
            total_advertisements += 1
            last_adv_timestamp = loop.time()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to send synthetic advertisement: %s", exc)

    api_server.set_advertisement_callback(register_advertisement_sender)

    def on_ble_advertisement(device: BLEDevice, advertisement: AdvertisementData) -> None:
        nonlocal total_advertisements, last_adv_timestamp
        logger.debug(f"on_ble_advertisement called: device={device.address}, callback={'SET' if send_advertisement_callback else 'None'}")
        if not send_advertisement_callback:
            return
        if device.name and ADAPTER_NAME_PATTERN.match(device.name):
            return
        total_advertisements += 1
        # Yield to event loop every 5 advertisements to prevent blocking
        if total_advertisements % 5 == 0:
            loop.call_soon_threadsafe(lambda: None)
        last_adv_timestamp = loop.time()
        logger.info(
            "BLE advertisement: %s (%s) rssi=%s",
            device.address,
            device.name or "",
            advertisement.rssi,
        )
        payload = _ble_packet_to_dict(device, advertisement)
        send_advertisement_callback(payload)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    poll_cycle_event = asyncio.Event()
    last_proxy_cycle_time = loop.time()
    last_renogy_read_time = loop.time() - max(0.0, renogy_read_interval)

    def _mark_proxy_cycle() -> None:
        nonlocal last_proxy_cycle_time
        if not poll_after_proxy_cycle:
            return
        last_proxy_cycle_time = loop.time()
        poll_cycle_event.set()

    async def _await_poll_window() -> None:
        nonlocal last_renogy_read_time
        if poll_after_proxy_cycle and airtime_scheduler is not None:
            event_triggered = False
            if poll_cycle_event.is_set():
                poll_cycle_event.clear()
                event_triggered = True
            else:
                try:
                    await asyncio.wait_for(
                        poll_cycle_event.wait(),
                        timeout=poll_cycle_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timed out waiting for BLE proxy cycle before Renogy poll; continuing"
                    )
                    # FIX: On timeout, manually trigger a proxy cycle marker so we don't get stuck
                    _mark_proxy_cycle()
                else:
                    poll_cycle_event.clear()
                    event_triggered = True

            if event_triggered and poll_cycle_dwell > 0:
                elapsed = loop.time() - last_proxy_cycle_time
                if elapsed < poll_cycle_dwell:
                    await asyncio.sleep(poll_cycle_dwell - elapsed)
            elif poll_cycle_dwell > 0:
                await asyncio.sleep(poll_cycle_dwell)
        # Always enforce minimum interval between reads
        min_interval = max(0.0, renogy_read_interval)
        if not poll_after_proxy_cycle and min_interval <= 0:
            # Prevent busy-looping when interval is unset in continuous scheduling
            min_interval = 60.0
        if min_interval > 0:
            elapsed_since_read = loop.time() - last_renogy_read_time
            if elapsed_since_read < min_interval:
                await asyncio.sleep(min_interval - elapsed_since_read)

    scan_mode = config.get(proxy_section, "scan_mode", fallback="").strip().lower()

    def _get_scan_float(option: str, fallback: float) -> float:
        try:
            return config.getfloat(proxy_section, option, fallback=fallback)
        except ValueError:
            logger.warning(
                "Invalid value for %s.%s; falling back to %.2f",
                proxy_section,
                option,
                fallback,
            )
            return fallback

    scan_active = _get_scan_float("scan_active_seconds", 0.0)
    scan_idle = _get_scan_float("scan_idle_seconds", 0.0)
    airtime_settle = _get_scan_float("airtime_settle_seconds", 0.4)
    airtime_window = _get_scan_float("airtime_window_seconds", 3.0)
    health_interval = _get_scan_float("health_check_interval", 30.0)
    health_threshold = _get_scan_float("health_check_threshold", 45.0)
    health_reset_adapter = config.getboolean(
        proxy_section,
        "health_reset_adapter",
        fallback=True,
    )
    health_reset_limit = config.getint(
        proxy_section,
        "health_reset_limit",
        fallback=3,
    )

    pause_during_renogy = config.getboolean(
        proxy_section,
        "pause_during_renogy",
        fallback=False,
    )

    poll_after_proxy_cycle = config.getboolean(
        "data",
        "poll_after_proxy_cycle",
        fallback=False,
    )

    def _get_data_float(option: str, fallback: float) -> float:
        try:
            return config.getfloat("data", option, fallback=fallback)
        except ValueError:
            logger.warning(
                "Invalid value for data.%s; falling back to %.2f",
                option,
                fallback,
            )
            return fallback

    poll_cycle_dwell = max(0.0, _get_data_float("poll_cycle_dwell_seconds", 1.0))
    poll_cycle_timeout = max(5.0, _get_data_float("poll_cycle_timeout_seconds", 30.0))
    renogy_read_timeout = max(20.0, _get_data_float("renogy_read_timeout_seconds", 45.0))

    scanner_kwargs = {
        "detection_callback": on_ble_advertisement,
        "adapter": adapter,
    }
    if scan_mode in {"active", "passive"}:
        scanner_kwargs["scanning_mode"] = scan_mode
    # Allow duplicate advertisements so Home Assistant sees regular beacon updates.
    bluez_filters = scanner_kwargs.get("bluez", {}) or {}
    filters = dict(bluez_filters.get("filters") or {})
    filters.setdefault("DuplicateData", True)
    bluez_filters["filters"] = filters
    scanner_kwargs["bluez"] = bluez_filters
    try:
        scanner = BleakScanner(**scanner_kwargs)
    except TypeError:
        # Older versions of bleak may not accept scanning_mode
        scanner_kwargs.pop("scanning_mode", None)
        scanner_kwargs.pop("bluez", None)
        scanner = BleakScanner(**scanner_kwargs)

    use_supervisor = with_renogy_client or pause_during_renogy or (scan_active > 0 and scan_idle > 0)
    scanner_supervisor: Optional[ScannerSupervisor] = None
    airtime_scheduler: Optional[AirtimeScheduler] = None
    scanner_task: Optional[asyncio.Task] = None
    last_adv_timestamp = loop.time()
    total_advertisements = 0
    health_task: Optional[asyncio.Task] = None
    if use_supervisor:
        scanner_supervisor = ScannerSupervisor(
            scanner,
            loop=loop,
            active_time=scan_active,
            idle_time=scan_idle,
        )
        airtime_scheduler = AirtimeScheduler(
            scanner_supervisor,
            loop=loop,
            resume_window=airtime_window,
            settle_time=airtime_settle,
            cycle_callback=_mark_proxy_cycle if poll_after_proxy_cycle else None,
        )

    async def monitor_scanner_health() -> None:
        if health_interval <= 0 or health_threshold <= 0:
            return
        warnings = 0
        resets = 0
        try:
            while not stop_event.is_set():
                await asyncio.sleep(max(5.0, health_interval))
                gap = loop.time() - last_adv_timestamp
                if gap <= health_threshold:
                    warnings = 0
                    continue
                warnings += 1
                logger.warning(
                    "No BLE advertisements observed for %.1fs (total=%d)",
                    gap,
                    total_advertisements,
                )
                if scanner_supervisor:
                    scanner_supervisor.kick_from_thread("health-gap")
                if airtime_scheduler:
                    airtime_scheduler.resume_window("health-gap")
                if health_reset_adapter and warnings >= 2:
                    if resets < max(1, health_reset_limit):
                        resets += 1
                        logger.warning(
                            "Health monitor triggering adapter power cycle (%d/%d)",
                            resets,
                            health_reset_limit,
                        )
                        try:
                            await _power_cycle_adapter(adapter)
                        except Exception as exc:
                            logger.error("Health power cycle failed: %s", exc)
                    warnings = 0
        except asyncio.CancelledError:
            logger.debug("Scanner health monitor cancelled")
            return

    async def start_battery_client() -> None:
        nonlocal battery_client, battery_future, battery_stopping, last_renogy_read_time, last_bt_reset
        if not with_renogy_client:
            logger.info("Renogy client disabled by configuration")
            return
        if battery_future and not battery_future.done():
            logger.debug("Renogy client already running; skipping start")
            return
        logger.info("Starting Renogy battery client setup (mode: %s)", renogy_poll_mode)
        battery_stopping = False
        
        # For scheduled mode, create client with scheduled_mode=True to stop after one read
        scheduled = renogy_poll_mode == "scheduled"
        # Only pass api_server if ESPHome sensors are enabled
        api_server_arg = api_server if esphome_sensors_enabled else None
        battery_client = _create_client(config, data_logger, api_server_arg, scheduled_mode=scheduled, failure_counter=consecutive_failures_list)
        setattr(battery_client, "last_error", None)
        
        # Check if we need to reset BT adapter due to consecutive failures
        if consecutive_failures_list[0] >= bt_reset_threshold:
            current_time = loop.time()
            if current_time - last_bt_reset > 60.0:  # Rate limit to once per minute
                logger.warning(
                    "Consecutive failure threshold (%d) reached - resetting BT adapter %s",
                    bt_reset_threshold,
                    adapter
                )
                last_bt_reset = current_time
                consecutive_failures_list[0] = 0  # Reset counter
                try:
                    await _power_cycle_adapter(adapter, delay=2.0)
                    await asyncio.sleep(3.0)  # Wait for adapter to stabilize
                    logger.info("BT adapter reset complete")
                except Exception as exc:
                    logger.error("BT adapter reset failed: %s", exc)

        if airtime_scheduler:
            def handle_ble_activity(request_pause: bool, stage: str) -> None:
                if request_pause:
                    airtime_scheduler.pause(stage)
                else:
                    airtime_scheduler.resume_window(stage)

            battery_client.set_ble_activity_callback(handle_ble_activity)
        elif scanner_supervisor:
            def handle_ble_activity_legacy(request_pause: bool, stage: str) -> None:
                if request_pause:
                    scanner_supervisor.pause_from_thread(stage)
                else:
                    scanner_supervisor.resume_from_thread(stage)

            battery_client.set_ble_activity_callback(handle_ble_activity_legacy)

        def run_client() -> None:
            """Run the battery client in executor thread.
            
            FIX: Ensure we're running in the same process and not spawning subprocesses.
            The BatteryClient.start() should run synchronously in this thread.
            """
            import os
            import sys
            current_pid = os.getpid()
            logger.debug(f"Battery client starting in PID {current_pid}")
            
            try:
                battery_client.start()
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Battery client exited unexpectedly: %s", exc)
            finally:
                # Verify we're still in the same process
                if os.getpid() != current_pid:
                    logger.error(f"CRITICAL: Battery client changed PID from {current_pid} to {os.getpid()}!")
                    sys.exit(1)

        battery_future = loop.run_in_executor(None, run_client)
        last_renogy_read_time = loop.time()

        def _battery_done_callback(
            fut: asyncio.Future, client_ref=battery_client
        ) -> None:
            if stop_event.is_set() or battery_stopping:
                return
            exc: Optional[BaseException]
            try:
                fut.result()
            except Exception as err:  # pragma: no cover - best effort logging
                exc = err
            else:
                exc = None
            if stop_event.is_set() or battery_stopping:
                return
            # In scheduled mode, don't auto-restart - the scheduler will trigger next read
            if renogy_poll_mode != "scheduled":
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        _restart_battery_client(
                            "executor exit",
                            exc,
                            getattr(client_ref, "last_error", None),
                        )
                    )
                )
            else:
                # In scheduled mode, just log completion
                logger.debug("Renogy scheduled read completed")

        battery_future.add_done_callback(_battery_done_callback)
        logger.info("Renogy client started in background executor")
        if scanner_supervisor and not pause_during_renogy:
            scanner_supervisor.kick_from_thread("renogy-start")
    
    async def stop_battery_client() -> None:
        nonlocal battery_client, battery_future, battery_stopping
        if not battery_client:
            return
        battery_stopping = True
        try:
            battery_client.stop()
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("Error stopping battery client: %s", exc)
        if battery_future:
            try:
                await asyncio.wait_for(asyncio.wrap_future(battery_future), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Battery client stop timed out after 5s")
                # FIX: Cancel the future to prevent it from running indefinitely
                battery_future.cancel()
        battery_client = None
        battery_future = None
        battery_stopping = False
        logger.info("Renogy client stopped")

    def _handle_signal(signum, frame) -> None:  # pragma: no cover - signal handling
        if not stop_event.is_set():
            logger.info("Received signal %s, shutting down proxy", signum)
            stop_event.set()

    add_signal_handler = getattr(loop, "add_signal_handler", None)
    if add_signal_handler:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                add_signal_handler(sig, lambda s=sig: _handle_signal(s, None))
            except (NotImplementedError, AttributeError):
                pass

    async def scheduled_renogy_reader() -> None:
        """Periodically trigger Renogy reads in scheduled mode."""
        if renogy_poll_mode != "scheduled":
            return
        nonlocal last_renogy_read_time

        logger.info(
            "Renogy scheduled reader enabled (interval=%.1fs, poll_after_proxy_cycle=%s)",
            renogy_read_interval,
            poll_after_proxy_cycle and airtime_scheduler is not None,
        )

        try:
            while not stop_event.is_set():
                await _await_poll_window()
                if stop_event.is_set():
                    break

                # Check if previous read is still running
                if battery_future and not battery_future.done():
                    logger.debug("Skipping scheduled Renogy read - previous read still in progress")
                    await asyncio.sleep(1.0)
                    continue

                logger.info("Triggering scheduled Renogy read")
                await start_battery_client()
                last_renogy_read_time = loop.time()
        except asyncio.CancelledError:
            logger.debug("Renogy scheduler cancelled")
            return

    if health_interval > 0 and health_threshold > 0:
        health_task = asyncio.create_task(monitor_scanner_health())
        logger.info(
            "Scanner health monitor enabled (interval=%.1fs threshold=%.1fs)",
            health_interval,
            health_threshold,
        )
    else:
        logger.info("Scanner health monitor disabled")

    logger.info("with_renogy_client=%s", with_renogy_client)
    
    # Start scheduled reader if in scheduled mode
    if with_renogy_client and renogy_poll_mode == "scheduled":
        renogy_scheduler_task = asyncio.create_task(scheduled_renogy_reader())

    # Start heartbeat task to ensure event loop stays responsive
    heartbeat_task = asyncio.create_task(event_loop_heartbeat())
    
    await api_server.start()
    await discovery.start()
    if scanner_supervisor:
        logger.info("Starting scanner supervisor")
        scanner_task = asyncio.create_task(scanner_supervisor.start())
        logger.info("Scanner supervisor task scheduled")
        # FIX: Explicitly start scanning after a short delay to allow initialization
        # The scanner.start() in supervisor may not trigger if pause_tokens > 0
        await asyncio.sleep(0.5)
        logger.info("Triggering initial scanner start")
        await scanner_supervisor._ensure_running("explicit-initial-start")
    else:
        if poll_after_proxy_cycle:
            logger.warning(
                "poll_after_proxy_cycle requested but scanner supervisor is disabled; using time-based scheduling"
            )
        await scanner.start()
        logger.info("Scanner started without supervisor")

    # FIX: Trigger initial proxy cycle to unblock scheduled Renogy reads
    if poll_after_proxy_cycle and with_renogy_client:
        logger.info("Triggering initial proxy cycle marker")
        _mark_proxy_cycle()

    # In continuous mode, start client immediately; in scheduled mode, the scheduler will trigger it
    if renogy_poll_mode == "continuous":
        logger.info("Starting Renogy client in continuous mode")
        await start_battery_client()
    else:
        logger.info("Renogy client in scheduled mode - waiting for first scheduled interval")

    try:
        logger.info(
            (
                "ESPHome proxy running on adapter %s (port %d, mac %s, "
                "Renogy client: %s (mode: %s, interval: %.1fs, proxy_gated: %s), scan mode: %s, duty cycle: %.1fs/%.1fs, "
                "autopause: %s)"
            ),
            adapter,
            native_port,
            proxy_mac,
            "enabled" if with_renogy_client else "disabled",
            renogy_poll_mode,
            renogy_read_interval,
            "on" if (poll_after_proxy_cycle and airtime_scheduler is not None) else "off",
            scanner_kwargs.get("scanning_mode", "default"),
            scan_active,
            scan_idle,
            "on" if pause_during_renogy else "off",
        )
        await stop_event.wait()
    except asyncio.CancelledError:
        raise
    finally:
        if airtime_scheduler:
            airtime_scheduler.cancel()
        if renogy_scheduler_task:
            renogy_scheduler_task.cancel()
            with contextlib.suppress(Exception):
                await renogy_scheduler_task
        if health_task:
            health_task.cancel()
            with contextlib.suppress(Exception):
                await health_task
        if scanner_task:
            scanner_task.cancel()
            with contextlib.suppress(Exception):
                await scanner_task
        if scanner_supervisor:
            await scanner_supervisor.shutdown()
        else:
            with contextlib.suppress(Exception):
                await scanner.stop()
        await discovery.stop()
        await api_server.stop()
        await stop_battery_client()


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Renogy BT ESPHome proxy service")
    parser.add_argument(
        "config",
        nargs="?",
        default="config.ini",
        help="Path to configuration file",
    )
    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        raise SystemExit(1)

    try:
        asyncio.run(run_proxy(config_path))
    except KeyboardInterrupt:
        logger.info("Proxy interrupted by user")
    except RuntimeError as exc:
        logger.error("Configuration error: %s", exc)
        raise SystemExit(1)
    except Exception as exc:
        logger.error("Unexpected error: %s", exc, exc_info=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
