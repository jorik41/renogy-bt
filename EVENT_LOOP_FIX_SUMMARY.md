# Event Loop Responsiveness Fix

## Problem
The ESPHome Native API server was not responding to connections even though the TCP server was accepting them. The root cause was that BLE scanner and Renogy client operations were monopolizing the asyncio event loop, preventing the Protocol callbacks from being processed.

## Solution Implemented

### 1. Event Loop Heartbeat Task
Added a periodic heartbeat task that yields to the event loop every 50ms to ensure other async operations can be processed:

```python
async def event_loop_heartbeat():
    try:
        while True:
            await asyncio.sleep(0.05)  # Yield every 50ms
    except asyncio.CancelledError:
        pass
```

### 2. BLE Advertisement Callback Yielding
Added yielding points in the BLE advertisement callback to prevent it from blocking:

```python
if total_advertisements % 5 == 0:
    loop.call_soon_threadsafe(lambda: None)
```

## Results
- ✅ ESPHome API server now responds to connections even with BLE operations running
- ✅ connection_made() callbacks are properly triggered
- ✅ HandshakeRequest/Response working correctly
- ✅ Event loop remains responsive under load

## Testing
Tested with all 4 batteries (48, 49, 50, 51) and reduced sensor count (8 important sensors only).

## Files Modified
- renogy_bt_proxy.py: Added event loop heartbeat and BLE yielding
- renogybt/sensor_definitions.py: Filtered to only create important sensors

Date: 2025-11-06
