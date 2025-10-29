"""Simple helper script to discover nearby Renogy BLE devices."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Iterable

try:
    from bleak import BleakScanner
except ModuleNotFoundError:
    sys.stderr.write(
        "Missing Python dependency 'bleak'. Install the requirements first:\n"
        "  python3 -m pip install -r requirements.txt\n"
        "Alternatively, run the scanner with the bundled virtualenv:\n"
        "  ./venv/bin/python scan_devices.py\n"
    )
    raise SystemExit(1)

DEFAULT_PREFIXES: tuple[str, ...] = ("BT-TH", "RNGRBP", "BTRIC")


async def discover_devices(
    *,
    adapter: str | None,
    timeout: float,
    prefixes: Iterable[str],
) -> None:
    logging.info("Scanning for Bluetooth devices (timeout=%ss)...", timeout)
    devices = await BleakScanner.discover(timeout=timeout, adapter=adapter)
    if not devices:
        logging.warning("No Bluetooth devices were discovered.")
        return

    normalized_prefixes = tuple(prefix.upper() for prefix in prefixes)

    print("Discovered devices:")
    for device in devices:
        name = device.name or "<unknown>"
        address = device.address or "<no address>"
        match = name.upper().startswith(normalized_prefixes)
        flag = "*" if match else " "
        print(f"{flag} {name:<32} {address}")

    print()
    print("Legend: '*' marks devices whose name matches common Renogy prefixes.")
    if adapter:
        print(f"Adapter used: {adapter}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Discover nearby Renogy Bluetooth devices."
    )
    parser.add_argument(
        "--adapter",
        help="Bluetooth adapter to use (e.g. hci0). Defaults to system default.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        help="Scan duration in seconds (default: 8).",
    )
    parser.add_argument(
        "--prefix",
        action="append",
        dest="prefixes",
        help=(
            "Restrict matches to device name prefixes. "
            "Can be used multiple times; defaults to BT-TH, RNGRBP, BTRIC."
        ),
    )

    args = parser.parse_args(argv)
    prefixes = tuple(args.prefixes) if args.prefixes else DEFAULT_PREFIXES

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(message)s")

    try:
        asyncio.run(
            discover_devices(
                adapter=args.adapter,
                timeout=args.timeout,
                prefixes=prefixes,
            )
        )
    except KeyboardInterrupt:
        logging.info("Scan interrupted by user.")


if __name__ == "__main__":
    main()
