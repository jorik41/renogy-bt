"""Combined Renogy client + ESPHome Bluetooth proxy service."""

from __future__ import annotations

import argparse
import asyncio
import configparser
import logging
import re
import signal
import sys
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from bleak import AdvertisementData, BLEDevice, BleakScanner

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
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Skip forwarding advertisements that originate from the local adapter
ADAPTER_NAME_PATTERN = re.compile(r"^hci\d+\s+\([0-9A-Fa-f:]+\)$")


def _format_mac(raw: int) -> str:
    """Format a MAC address from the integer returned by uuid.getnode()."""
    return ":".join(f"{(raw >> shift) & 0xFF:02x}" for shift in range(40, -8, -8))


def _determine_proxy_mac(config: configparser.ConfigParser) -> str:
    """Derive the MAC address to advertise for the ESPHome device."""
    for key in ("mac", "native_api_mac"):
        if config.has_option("home_assistant_proxy", key):
            candidate = config["home_assistant_proxy"].get(key, "").strip()
            if candidate:
                return candidate.lower()
    node = uuid.getnode()
    if (node >> 40) % 2:
        logger.warning(
            "uuid.getnode() returned a locally administered MAC. "
            "Consider setting [home_assistant_proxy].mac explicitly."
        )
    return _format_mac(node)


def _create_client(config: configparser.ConfigParser, data_logger: DataLogger):
    """Instantiate the appropriate Renogy client."""

    alias = config["device"]["alias"]
    battery_map: Dict[int, Dict[str, object]] = {}

    def on_data_received(client, data):
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

        if config["device"]["type"] == "RNG_BATT" and len(client.device_ids) > 1:
            if dev_id is not None:
                battery_map[dev_id] = data
            if len(battery_map) == len(client.device_ids):
                combined = Utils.combine_battery_readings(battery_map)
                filtered_combined = Utils.filter_fields(combined, fields)
                logger.info("combined => %s", filtered_combined)
                if config["mqtt"].getboolean("enabled"):
                    data_logger.log_mqtt(json_data=filtered_combined)
                battery_map.clear()

        if config["remote_logging"].getboolean("enabled"):
            data_logger.log_remote(json_data=filtered_data)
        if config["mqtt"].getboolean("enabled"):
            data_logger.log_mqtt(json_data=filtered_data)
        if (
            config["pvoutput"].getboolean("enabled")
            and config["device"]["type"] == "RNG_CTRL"
        ):
            data_logger.log_pvoutput(json_data=filtered_data)
        if not config["data"].getboolean("enable_polling"):
            client.stop()

    def on_error(client, error):
        logger.error("Proxy battery client error: %s", error)

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

    energy_file = str((config_path.parent / "energy_totals.json").resolve())
    config["device"]["energy_file"] = energy_file
    data_logger = DataLogger(config)

    with_renogy_client = config.getboolean(proxy_section, "with_renogy_client", fallback=True)
    battery_client = None
    battery_future: Optional[asyncio.Future] = None

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
        nonlocal send_advertisement_callback
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
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to send synthetic advertisement: %s", exc)

    api_server.set_advertisement_callback(register_advertisement_sender)

    def on_ble_advertisement(device: BLEDevice, advertisement: AdvertisementData) -> None:
        if not send_advertisement_callback:
            return
        if device.name and ADAPTER_NAME_PATTERN.match(device.name):
            return
        logger.info(
            "BLE advertisement: %s (%s) rssi=%s",
            device.address,
            device.name or "",
            advertisement.rssi,
        )
        payload = _ble_packet_to_dict(device, advertisement)
        send_advertisement_callback(payload)

    scanner = BleakScanner(detection_callback=on_ble_advertisement, adapter=adapter)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    async def start_battery_client() -> None:
        nonlocal battery_client, battery_future
        if not with_renogy_client:
            return
        battery_client = _create_client(config, data_logger)

        def run_client() -> None:
            try:
                battery_client.start()
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Battery client exited unexpectedly: %s", exc)

        battery_future = loop.run_in_executor(None, run_client)
        logger.info("Renogy client started in background executor")

    async def stop_battery_client() -> None:
        nonlocal battery_client, battery_future
        if not battery_client:
            return
        try:
            battery_client.stop()
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("Error stopping battery client: %s", exc)
        if battery_future:
            await asyncio.wrap_future(battery_future)
        battery_client = None
        battery_future = None
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

    await start_battery_client()

    try:
        await api_server.start()
        await discovery.start()
        await scanner.start()
        logger.info(
            "ESPHome proxy running on adapter %s (port %d, mac %s, Renogy client: %s)",
            adapter,
            native_port,
            proxy_mac,
            "enabled" if with_renogy_client else "disabled",
        )
        await stop_event.wait()
    except asyncio.CancelledError:
        raise
    finally:
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
