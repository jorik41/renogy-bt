"""BlueZ D-Bus Resilience Layer - Safeguards against D-Bus hangs"""

import asyncio
import logging
from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus

logger = logging.getLogger(__name__)


class BlueZAdapterMonitor:
    """Monitor and recover from BlueZ adapter issues."""
    
    def __init__(self, adapter: str = "hci0"):
        self.adapter = adapter
        self.adapter_path = f"/org/bluez/{adapter}"
        self._last_reset_time = 0
        self._reset_count = 0
        self._max_resets_per_hour = 10
        
    async def check_adapter_state(self) -> dict:
        """Check current BlueZ adapter state with timeout protection."""
        bus = None
        try:
            bus = MessageBus(bus_type=BusType.SYSTEM)
            await asyncio.wait_for(bus.connect(), timeout=5.0)
            
            introspection = await asyncio.wait_for(
                bus.introspect("org.bluez", self.adapter_path), timeout=5.0
            )
            proxy = bus.get_proxy_object("org.bluez", self.adapter_path, introspection)
            props = proxy.get_interface("org.freedesktop.DBus.Properties")
            
            powered = await asyncio.wait_for(
                props.call_get("org.bluez.Adapter1", "Powered"), timeout=3.0
            )
            discovering = await asyncio.wait_for(
                props.call_get("org.bluez.Adapter1", "Discovering"), timeout=3.0
            )
            
            return {
                'powered': powered.value,
                'discovering': discovering.value,
                'healthy': True,
            }
            
        except asyncio.TimeoutError:
            logger.error("Timeout checking adapter - D-Bus may be hung")
            return {'healthy': False, 'error': 'timeout'}
        except Exception as e:
            logger.error("Error checking adapter: %s", e)
            return {'healthy': False, 'error': str(e)}
        finally:
            if bus:
                bus.disconnect()
    
    async def force_stop_discovery(self) -> bool:
        """Force stop discovery if adapter is stuck."""
        bus = None
        try:
            bus = MessageBus(bus_type=BusType.SYSTEM)
            await asyncio.wait_for(bus.connect(), timeout=5.0)
            
            introspection = await asyncio.wait_for(
                bus.introspect("org.bluez", self.adapter_path), timeout=5.0
            )
            proxy = bus.get_proxy_object("org.bluez", self.adapter_path, introspection)
            adapter_iface = proxy.get_interface("org.bluez.Adapter1")
            
            logger.warning("Force stopping discovery on %s", self.adapter)
            await asyncio.wait_for(adapter_iface.call_stop_discovery(), timeout=5.0)
            await asyncio.sleep(0.5)
            return True
            
        except Exception as e:
            logger.warning("Could not stop discovery: %s", e)
            return False
        finally:
            if bus:
                bus.disconnect()
    
    async def power_cycle_adapter(self, delay: float = 1.0) -> bool:
        """Power cycle adapter to recover from stuck state with rate limiting."""
        import time
        
        current_time = time.time()
        if current_time - self._last_reset_time < 3600:
            if self._reset_count >= self._max_resets_per_hour:
                logger.error("Too many resets (%d/hr) - refusing", self._reset_count)
                return False
        else:
            self._reset_count = 0
        
        bus = None
        try:
            bus = MessageBus(bus_type=BusType.SYSTEM)
            await asyncio.wait_for(bus.connect(), timeout=5.0)
            
            introspection = await asyncio.wait_for(
                bus.introspect("org.bluez", self.adapter_path), timeout=5.0
            )
            proxy = bus.get_proxy_object("org.bluez", self.adapter_path, introspection)
            props = proxy.get_interface("org.freedesktop.DBus.Properties")
            
            logger.warning("Power cycling adapter %s", self.adapter)
            
            try:
                await self.force_stop_discovery()
            except:
                pass
            
            # Power off
            await asyncio.wait_for(
                props.call_set("org.bluez.Adapter1", "Powered", Variant("b", False)),
                timeout=5.0
            )
            await asyncio.sleep(delay)
            
            # Power on
            await asyncio.wait_for(
                props.call_set("org.bluez.Adapter1", "Powered", Variant("b", True)),
                timeout=5.0
            )
            await asyncio.sleep(delay)
            
            self._last_reset_time = current_time
            self._reset_count += 1
            
            logger.info("Adapter power cycle OK (reset %d/%d/hr)",
                       self._reset_count, self._max_resets_per_hour)
            return True
            
        except Exception as e:
            logger.error("Power cycle failed: %s", e)
            return False
        finally:
            if bus:
                bus.disconnect()
