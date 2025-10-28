from typing import TYPE_CHECKING

from .RoverClient import RoverClient
from .DataLogger import DataLogger
from .BatteryClient import BatteryClient
from .RoverHistoryClient import RoverHistoryClient
from .InverterClient import InverterClient
from .DCChargerClient import DCChargerClient
from .Utils import *

if TYPE_CHECKING:  # pragma: no cover - import only used for type checkers
    from .home_assistant_proxy import HomeAssistantBluetoothProxy

__all__ = [
    "RoverClient",
    "DataLogger",
    "BatteryClient",
    "RoverHistoryClient",
    "InverterClient",
    "DCChargerClient",
    "HomeAssistantBluetoothProxy",
]


def __getattr__(name: str):
    if name == "HomeAssistantBluetoothProxy":
        from .home_assistant_proxy import HomeAssistantBluetoothProxy

        return HomeAssistantBluetoothProxy
    raise AttributeError(f"module {__name__} has no attribute {name}")
