"""Example entrypoint for the Home Assistant Bluetooth proxy."""

from __future__ import annotations

import argparse
import asyncio
import configparser
import logging
from pathlib import Path
from typing import List, Optional

from renogybt import (
    BatteryClient,
    DataLogger,
    DCChargerClient,
    HomeAssistantBluetoothProxy,
    InverterClient,
    RoverClient,
    RoverHistoryClient,
    Utils,
)


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
    proxy_name = config["home_assistant_proxy"].get(
        "name", fallback=config["device"].get("alias", "renogybt-proxy")
    )
    friendly_name = config["home_assistant_proxy"].get(
        "friendly_name", fallback=proxy_name
    )
    data_logger = DataLogger(config)

    def factory():
        return _create_client(config, data_logger)

    blocked_addrs = {
        addr.strip()
        for addr in config["home_assistant_proxy"]
        .get("blocked_addresses", fallback="")
        .split(",")
        if addr.strip()
    }
    battery_retry_seconds = config["home_assistant_proxy"].getint(
        "battery_retry_seconds", fallback=30
    )

    proxy = HomeAssistantBluetoothProxy(
        name=proxy_name,
        friendly_name=friendly_name,
        bind_host=config["home_assistant_proxy"].get("bind_host", fallback="0.0.0.0"),
        port=config["home_assistant_proxy"].getint("port", fallback=6053),
        adapter=adapter,
        battery_client_factory=factory,
        blocked_addresses=blocked_addrs,
        project_name=config["home_assistant_proxy"].get("project_name"),
        project_version=config["home_assistant_proxy"].get("project_version"),
        manufacturer=config["home_assistant_proxy"].get("manufacturer"),
        model=config["home_assistant_proxy"].get("model"),
        suggested_area=config["home_assistant_proxy"].get("suggested_area"),
        max_connections=config["home_assistant_proxy"].getint(
            "max_connections", fallback=3
        ),
        battery_retry_seconds=battery_retry_seconds,
    )

    try:
        await proxy.start()
    except asyncio.CancelledError:  # pragma: no cover - shutdown path
        proxy.request_stop()
        raise
    finally:
        # Cleanup resources
        data_logger.cleanup()
        Utils.flush_energy_totals(energy_file)


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
        raise SystemExit(f"Config file not found: {config_path}")

    try:
        asyncio.run(run_proxy(config_path))
    except KeyboardInterrupt:
        logging.info("Proxy interrupted by user")


if __name__ == "__main__":
    main()
