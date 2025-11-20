# Renogy-BT Code Fixes Applied

## Issues Fixed

### 1. BLE Scanner Not Auto-Starting ✅
**Problem**: Scanner supervisor was scheduled but never explicitly started scanning.

**Root Cause**: 
- `scanner_supervisor.start()` creates task but doesn't trigger `_ensure_running()`
- Scanner waits in paused state with no initial trigger

**Fix**:
```python
# Line ~1071: After scheduling scanner supervisor
await asyncio.sleep(0.5)  # Allow initialization
logger.info("Triggering initial scanner start")
await scanner_supervisor._ensure_running("explicit-initial-start")
```

### 2. Proxy Cycle Never Triggered ✅
**Problem**: `poll_after_proxy_cycle=true` waits for cycle event that never fires initially.

**Root Cause**:
- `_mark_proxy_cycle()` only called after resume_window completes
- Initial service start never triggers first cycle
- Scheduled Renogy reads timeout waiting

**Fix**:
```python
# Line ~1078: After scanner start, trigger initial cycle
if poll_after_proxy_cycle and with_renogy_client:
    logger.info("Triggering initial proxy cycle marker")
    _mark_proxy_cycle()
```

### 3. Proxy Cycle Timeout Deadlock ✅
**Problem**: Timeout waiting for proxy cycle leaves service stuck.

**Root Cause**:
- On timeout, no recovery mechanism to unblock
- Subsequent reads continue waiting indefinitely

**Fix**:
```python
# Line ~744: On timeout, manually trigger cycle
except asyncio.TimeoutError:
    logger.warning("Timed out waiting for BLE proxy cycle; continuing")
    _mark_proxy_cycle()  # Unblock future waits
```

### 4. Battery Client Process Monitoring ✅
**Problem**: Battery client runs in executor with own event loop, hard to track.

**Root Cause**:
- `BaseClient.start()` creates `new_event_loop()` in thread
- Multiple instances could conflict
- No visibility into thread PID

**Fix**:
```python
# Line ~948: Add PID tracking and monitoring
def run_client() -> None:
    import os
    current_pid = os.getpid()
    logger.debug(f"Battery client starting in PID {current_pid}")
    
    try:
        battery_client.start()
    finally:
        if os.getpid() != current_pid:
            logger.error(f"CRITICAL: PID changed from {current_pid}!")
            sys.exit(1)
```

### 5. Battery Client Stop Timeout ✅
**Problem**: Stopping battery client could hang indefinitely.

**Root Cause**:
- `await asyncio.wrap_future(battery_future)` has no timeout
- If thread is stuck, service shutdown hangs

**Fix**:
```python
# Line ~1008: Add timeout to stop operation
try:
    await asyncio.wait_for(asyncio.wrap_future(battery_future), timeout=5.0)
except asyncio.TimeoutError:
    logger.warning("Battery client stop timed out after 5s")
    battery_future.cancel()
```

## Expected Improvements

### Before ❌
- Scanner scheduled but never scanning
- 0 BLE advertisements forwarded
- "Timed out waiting for BLE proxy cycle" every minute
- Scheduled Renogy reads never execute
- Process confusion (appears as 2 PIDs)

### After ✅
- Scanner explicitly started after initialization
- BLE advertisements begin flowing immediately
- Initial proxy cycle triggers scheduled reads
- Timeout recovery prevents deadlocks
- Better PID tracking and monitoring
- Clean shutdown with timeouts

## Testing Checklist

1. **Service starts cleanly**
   ```bash
   sudo systemctl restart renogy-bt.service
   # Should see "Triggering initial scanner start"
   # Should see "Triggering initial proxy cycle marker"
   ```

2. **BLE advertisements flow**
   ```bash
   journalctl -u renogy-bt.service -f | grep "BLE advertisement:"
   # Should see multiple unique devices within 30s
   ```

3. **Scheduled Renogy reads execute**
   ```bash
   journalctl -u renogy-bt.service -f | grep "Triggering scheduled Renogy read"
   # Should trigger at configured interval (60s)
   ```

4. **No process duplication**
   ```bash
   ps aux | grep "[p]ython.*renogy"
   # Should only show ONE process
   ```

5. **Clean shutdown**
   ```bash
   sudo systemctl stop renogy-bt.service
   # Should complete within 20s
   ```

## Files Modified

- `renogy_bt_proxy.py` - 5 code fixes applied

## Deployment

```bash
# On development machine
cd /home/jorik41/renogy-bt
git add renogy_bt_proxy.py
git commit -m "Fix BLE scanner auto-start and proxy cycle issues"

# Deploy to Pi
scp renogy_bt_proxy.py jorik@192.168.1.28:/home/jorik/renogy-bt/
ssh jorik@192.168.1.28 'sudo systemctl restart renogy-bt.service'

# Monitor
ssh jorik@192.168.1.28 'journalctl -u renogy-bt.service -f'
```

## Rollback

```bash
ssh jorik@192.168.1.28
cp /home/jorik/renogy-bt/renogy_bt_proxy.py.bak /home/jorik/renogy-bt/renogy_bt_proxy.py
sudo systemctl restart renogy-bt.service
```

---
**Status**: Code fixes complete, ready for deployment  
**Date**: 2025-11-06 15:30 CET  
**Changes**: 5 critical fixes in renogy_bt_proxy.py
