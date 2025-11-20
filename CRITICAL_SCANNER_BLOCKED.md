# Renogy-BT Critical Issues Found

## Scanner Completely Blocked ⛔

### Root Cause: Pause Token Accumulation

The scanner is **permanently paused** due to accumulated pause tokens:

```
DEBUG: ScannerSupervisor pause (read:5000); tokens=50
DEBUG: ScannerSupervisor pause (read:5017); tokens=51
...
DEBUG: ScannerSupervisor pause (read:5223); tokens=67

DEBUG: start skipped; running=False tokens=67  ← BLOCKED!
```

**Each Renogy battery read** pauses scanner 8+ times (one per register section), but **resumes are not properly draining tokens**.

### Impact

- ❌ Scanner never runs (tokens > 0)
- ❌ No BLE advertisements (475s+ gaps)
- ❌ Health monitor constantly triggering
- ❌ Renogy reads failing (can't discover device)

### Why Initial Test Seemed to Work

The first ~2 minutes after service start showed ads because:
1. Scanner started with 0 tokens
2. Briefly scanned before first Renogy read
3. Once Renogy read started, tokens accumulated
4. Scanner permanently blocked after that

## Solutions

### Option 1: Disable Renogy Client (TEST PROXY ONLY) ✅ RECOMMENDED

```ini
[home_assistant_proxy]
with_renogy_client = false
```

**Result**: Pure BLE proxy without Renogy interference
- Scanner runs continuously
- All BLE devices forwarded to HA
- No token accumulation

### Option 2: Fix Pause/Resume Logic in Code

The `ble_activity_callback` in BatteryClient is calling pause for each register read but not properly balancing with resumes.

**Code Issue** (BaseClient.py):
```python
# Each section read calls:
pause_ble_callback(True, "read:5000")
pause_ble_callback(True, "read:5017")
# ...8 times total

# But only ONE resume at end
pause_ble_callback(False, "read")
```

**Fix Needed**: Either:
1. Single pause/resume for entire battery read cycle
2. Properly balance each pause with resume
3. Use a different locking mechanism

### Option 3: Disable pause_during_renogy

Already tried, but the issue is the callback itself is causing pauses, not the config option.

## Immediate Action Required

**Deploy pure proxy mode:**

```bash
# Update config
cp config.ini.optimized config.ini

# Edit to set with_renogy_client = false

# Restart
sudo systemctl restart renogy-bt.service

# Verify scanning works
journalctl -u renogy-bt.service -f | grep "BLE advertisement:"
```

This will prove the proxy works perfectly without Renogy client interference.

## Long-Term Fix

The BatteryClient pause/resume logic needs refactoring:

1. **Batch pause/resume**: One pause before all reads, one resume after
2. **Remove per-section callbacks**: Don't pause for each register
3. **Use async locking**: Instead of pause tokens, use proper async locks

## Performance Impact

Current state:
- Scanner: **0% uptime** (permanently paused)
- Advertisements: **0/minute** (none since initial burst)
- Renogy reads: **Failing** (can't discover device while scanner paused)

Expected with proxy-only mode:
- Scanner: **100% uptime** 
- Advertisements: **100+/minute**
- All BLE devices forwarded

---
**Status**: ⛔ **CRITICAL - Scanner Blocked**  
**Cause**: Pause token accumulation from Renogy reads  
**Fix**: Disable Renogy client OR refactor pause logic  
**Priority**: IMMEDIATE
