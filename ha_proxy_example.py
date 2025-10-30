"""Example entrypoint for the Home Assistant Bluetooth proxy.

This script can run in two modes:

1. **Standalone BT Proxy Mode** (default, with_renogy_client=false):
   - Acts as a pure ESP32 Bluetooth proxy
   - Forwards BLE advertisements from nearby devices to Home Assistant
   - No Renogy battery data collection
   - Lightweight and focuses solely on proxy functionality

2. **Combined Mode** (with_renogy_client=true):
   - Runs both the BT proxy AND Renogy battery client
   - Collects Renogy battery data while forwarding other BLE advertisements
   - Useful when you want both functionalities on the same adapter

For pure Renogy battery data collection without BT proxy, use example.py instead.
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import logging
import sys
from pathlib import Path
from typing import List, Optional

try:
    from renogybt import (
        BatteryClient,
        DataLogger,
        DCChargerClient,
        HomeAssistantAPIClient,
        HomeAssistantBluetoothProxy,
        InverterClient,
        RoverClient,
        RoverHistoryClient,
        Utils,
    )
except ModuleNotFoundError as exc:
    missing = exc.name
    if missing in {"bleak", "requests", "aiohttp", "paho"}:
        sys.stderr.write(
            f"Missing Python dependency '{missing}'. Install the requirements first:\n"
            "  python3 -m pip install -r requirements.txt\n"
            "Alternatively, run the proxy with the bundled virtualenv:\n"
            "  ./venv/bin/python ha_proxy_example.py config.ini\n"
        )
        raise SystemExit(1)
    raise


logging.basicConfig(level=logging.INFO)


def _create_client(config: configparser.ConfigParser, data_logger: DataLogger):
    alias = config["device"]["alias"]

    battery_map = {}

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
        logging.info("%s => %s", client.ble_manager.device.name, filtered_data)

        if config["device"]["type"] == "RNG_BATT" and len(client.device_ids) > 1:
            if dev_id is not None:
                battery_map[dev_id] = data
            if len(battery_map) == len(client.device_ids):
                combined = Utils.combine_battery_readings(battery_map)
                filtered_combined = Utils.filter_fields(combined, fields)
                logging.info("combined => %s", filtered_combined)
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
        logging.error("Proxy battery client error: %s", error)

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


def _resolve_token(config: configparser.ConfigParser, config_path: Path) -> Optional[str]:
    token = config["home_assistant_proxy"].get("access_token", fallback="").strip()
    token_file = config["home_assistant_proxy"].get("access_token_file", fallback="").strip()

    if token_file:
        file_path = Path(token_file).expanduser()
        if not file_path.is_absolute():
            file_path = (config_path.parent / file_path).resolve()
        if not file_path.exists():
            logging.error("Home Assistant token file not found: %s", file_path)
            raise SystemExit(1)
        token = file_path.read_text(encoding="utf-8").strip()

    return token or None


async def run_proxy(config_path: Path) -> None:
    config = configparser.ConfigParser(inline_comment_prefixes=("#",))
    config.read(config_path)

    if not config.getboolean("home_assistant_proxy", "enabled", fallback=False):
        raise RuntimeError("home_assistant_proxy.enabled must be true")

    # Check if we should run with Renogy client (default: False for standalone mode)
    with_renogy_client = config.getboolean(
        "home_assistant_proxy", "with_renogy_client", fallback=False
    )

    # Get adapter - first try proxy section, then device section, default to hci0
    adapter = config.get("home_assistant_proxy", "adapter", fallback=None)
    if adapter is None and config.has_section("device"):
        adapter = config.get("device", "adapter", fallback=None)
    if adapter is None:
        adapter = "hci0"
    
    # Get source identifier for the proxy
    proxy_source = config.get("home_assistant_proxy", "source", fallback=None)
    if proxy_source is None and config.has_section("device"):
        proxy_source = config.get("device", "alias", fallback=None)
    if proxy_source is None:
        proxy_source = "renogybt-proxy"

    # Only create data logger and client factory if running with Renogy client
    battery_client_factory = None
    if with_renogy_client:
        if not config.has_section("device"):
            raise RuntimeError(
                "with_renogy_client=true requires [device] section in config"
            )
        energy_file = str((config_path.parent / "energy_totals.json").resolve())
        config["device"]["energy_file"] = energy_file
        data_logger = DataLogger(config)

        def factory():
            return _create_client(config, data_logger)

        battery_client_factory = factory
        logging.info("Running in combined mode (BT proxy + Renogy client)")
    else:
        logging.info("Running in standalone BT proxy mode")

    endpoint = config["home_assistant_proxy"].get(
        "endpoint", fallback="/api/bluetooth/adv"
    )
    fallback_raw = config["home_assistant_proxy"].get(
        "fallback_endpoints", fallback=""
    )
    if fallback_raw:
        fallback_endpoints = [
            item.strip() for item in fallback_raw.split(",") if item.strip()
        ]
    else:
        fallback_endpoints = [
            ep
            for ep in (
                "/api/bluetooth/remote/adv",
                "/ble/advertisements",
            )
            if ep != endpoint
        ]

    api_client = HomeAssistantAPIClient(
        host=config["home_assistant_proxy"].get("host", "homeassistant.local"),
        port=config["home_assistant_proxy"].getint("port", fallback=8123),
        token=_resolve_token(config, config_path),
        ssl=config["home_assistant_proxy"].getboolean("ssl", fallback=False),
        endpoint=endpoint,
        fallback_endpoints=fallback_endpoints,
    )

    proxy = HomeAssistantBluetoothProxy(
        api_client=api_client,
        source=proxy_source,
        adapter=adapter,
        battery_client_factory=battery_client_factory,
    )

    try:
        await proxy.start()
    except asyncio.CancelledError:  # pragma: no cover - shutdown path
        proxy.request_stop()
        raise


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="RenogyBT Home Assistant proxy")
    parser.add_argument(
        "config",
        nargs="?",
        default="config.ini",
        help="Path to the configuration file",
    )

    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    if not config_path.exists():
        logging.error("Config file not found: %s", config_path)
        raise SystemExit(1)

    try:
        asyncio.run(run_proxy(config_path))
    except KeyboardInterrupt:
        logging.info("Proxy interrupted by user")
    except RuntimeError as e:
        logging.error("Configuration error: %s", e)
        raise SystemExit(1)
    except Exception as e:
        logging.error("Unexpected error: %s", e, exc_info=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
