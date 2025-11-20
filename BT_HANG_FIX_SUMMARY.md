# Renogy-BT Bluetooth Hang - Fix Summary

## PROBLEM FOUND
Your renogy-bt process was **stuck for 10+ hours** in Bluetooth discovery mode.

### Root Cause
- `BleakScanner.discover()` in BLEManager.py can hang indefinitely
- The Bluetooth adapter was stuck in "Discovering: yes" state
- No advertisements were being received (total=0 for 38,000+ seconds)
- Renogy battery reads couldn't start because previous read never finished

## IMMEDIATE FIX APPLIED ✅

1. **Killed stuck process** (PID 19906)
2. **Reset Bluetooth adapter** (hciconfig down/up)
3. **Fixed BLEManager.py** - Added hard timeout wrappers
4. **Restarted service** - Now running unified_proxy.py (PID 26431)

## CODE FIX DETAILS

### File: renogybt/BLEManager.py
- **Backup saved**: renogybt/BLEManager.py.pre_fix
- **Added**: DISCOVERY_HARD_TIMEOUT = 30 seconds
- **Wrapped BleakScanner.discover()** with asyncio.wait_for()
- **Result**: Discovery cannot hang more than 30 seconds total

### Why This Fixes It
Before: BleakScanner.discover could hang forever if BlueZ gets stuck
After: asyncio.wait_for forces timeout and fails gracefully

## HOW TO MONITOR

Check if working properly:
```bash
# 1. Check for repeated skips (BAD if you see many)
tail -f /var/log/syslog | grep "Skipping scheduled Renogy read"

# 2. Check BT adapter (should be "Discovering: no")
bluetoothctl show | grep Discovering

# 3. Check advertisements flowing (GOOD if you see many)
tail -f /var/log/syslog | grep "Advertisement"
```

## FUTURE PREVENTION RECOMMENDED

### Add watchdog timer (recommended):
- Monitor how long battery reads take
- Auto-restart if stuck >3 minutes
- Will prevent manual intervention

### Enhanced health check (optional):
- Check if BlueZ stuck in Discovering state
- Auto power-cycle adapter if detected

## FILES CHANGED

- `renogybt/BLEManager.py` - Added hard timeouts
- Backup: `renogybt/BLEManager.py.pre_fix`

## CURRENT STATUS

✅ Service running: unified_proxy.py (PID 26431)
✅ Bluetooth adapter: healthy
✅ ESPHome API: active on port 6053
⏳ Monitor for 24-48 hours to verify stability

## IF IT HANGS AGAIN

Quick recovery commands:
```bash
# Find and kill stuck process
ps aux | grep renogy
sudo kill -9 <PID>

# Reset Bluetooth
sudo hciconfig hci0 down && sudo hciconfig hci0 up

# Restart
cd /home/jorik/renogy-bt
nohup venv/bin/python unified_proxy.py config.ini > /tmp/unified_proxy.log 2>&1 &
```

