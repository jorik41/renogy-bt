"""Example entrypoint for the Home Assistant Bluetooth proxy."""

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

    energy_file = str((config_path.parent / "energy_totals.json").resolve())
    config["device"]["energy_file"] = energy_file

    adapter = config["home_assistant_proxy"].get(
        "adapter", fallback=config["device"].get("adapter")
    )
    proxy_source = config["home_assistant_proxy"].get(
        "source", fallback=config["device"].get("alias", "renogybt-proxy")
    )
    data_logger = DataLogger(config)

    def factory():
        return _create_client(config, data_logger)

    api_client = HomeAssistantAPIClient(
        host=config["home_assistant_proxy"].get("host", "homeassistant.local"),
        port=config["home_assistant_proxy"].getint("port", fallback=8123),
        token=_resolve_token(config, config_path),
        ssl=config["home_assistant_proxy"].getboolean("ssl", fallback=False),
        endpoint=config["home_assistant_proxy"].get(
            "endpoint", fallback="/api/bluetooth/remote/adv"
        ),
    )

    proxy = HomeAssistantBluetoothProxy(
        api_client=api_client,
        source=proxy_source,
        adapter=adapter,
        battery_client_factory=factory,
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
