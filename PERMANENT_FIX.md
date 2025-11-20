# RENOGY-BT PERMANENT FIX - ROOT CAUSE ANALYSIS
## Date: 2025-11-18
## System: 192.168.1.28 (Raspberry Pi)

---

## ROOT CAUSE - DUPLICATE SYSTEMD SERVICES

### The Problem:
**TWO systemd services were enabled and fighting for the same port 6053:**

1. **`renogy-bt.service`** (older service)
   - Enabled at boot
   - Located: `/etc/systemd/system/renogy-bt.service`
   - Had aggressive cleanup: `ExecStopPost=+/bin/sh -c 'pkill -9 -f "renogy_bt_proxy.py" || true'`
   - **This killed ALL renogy processes, including the other service!**

2. **`renogy-bt-proxy.service`** (newer service)
   - Also enabled at boot
   - Located: `/etc/systemd/system/renogy-bt-proxy.service`
   - Had no cleanup mechanism

### Why a Reboot Didn't Fix It:

At boot time:
1. Both services started simultaneously
2. One grabbed port 6053 successfully
3. The other failed with "Address already in use"
4. The failed service kept restarting (RestartSec=5)
5. When the first service stopped, its ExecStopPost killed ALL processes
6. This created a cycle of conflicts and crashes
7. Eventually both services gave up or one left an orphan process

### The Orphan Process:
- Process PID 680 from user session (not systemd) held port 6053
- It was started before the last reboot and survived due to user session persistence
- Neither service could start because of this orphan

---

## PERMANENT FIX APPLIED

### 1. Disabled Conflicting Service ✓
```bash
sudo systemctl stop renogy-bt.service
sudo systemctl disable renogy-bt.service
```

### 2. Created Safe Cleanup Script ✓
**File:** `/home/jorik/renogy-bt/scripts/cleanup_port.sh`
```bash
#!/bin/bash
# Safe cleanup: only kill processes on port 6053 if they exist
PORT=6053
PIDS=$(ss -tlnp 2>/dev/null | grep ":$PORT " | grep -oP "pid=\K[0-9]+" | sort -u)
if [ -n "$PIDS" ]; then
    for pid in $PIDS; do
        if ps -p $pid -o cmd= 2>/dev/null | grep -q "renogy_bt_proxy.py"; then
            echo "Cleaning up orphan process $pid on port $PORT"
            kill -9 $pid 2>/dev/null || true
        fi
    done
    sleep 1
fi
exit 0
```

### 3. Updated renogy-bt-proxy.service ✓
**New configuration:**
- Added `Conflicts=renogy-bt.service renogy-bt-old.service` to prevent conflicts
- Added `ExecStartPre=+/home/jorik/renogy-bt/scripts/cleanup_port.sh` for cleanup
- Set `User=jorik` and `Group=jorik` for proper permissions
- Added resource limits (MemoryMax=256M)
- Improved restart policy (RestartSec=10, StartLimitBurst=5)

---

## VERIFICATION

### Service Status:
```
✓ renogy-bt-proxy.service: enabled and running
✗ renogy-bt.service: disabled (permanently)
✗ renogy-bt-unified.service: disabled
```

### Functionality Test:
```
✓ BLE Proxy working (forwarding advertisements)
✓ Renogy battery data reading every 30 seconds
✓ ESPHome API listening on port 6053
✓ Service survives restart
✓ No orphan processes after stop/start
```

### Sample Battery Data:
```
Voltage: 49.4V
Current: 0.41A
Power: 20.25W
Battery Level: 30.94%
Temperature: 73.4°F
Model: RBT50LFP48S
```

---

## PREVENTION MEASURES

### 1. Service Conflicts Prevented
- `Conflicts=` directive ensures only one service runs
- Cleanup script only kills orphans on the specific port
- Service properly runs as user `jorik` (not root)

### 2. Boot Behavior
- Only `renogy-bt-proxy.service` starts at boot
- Cleanup script runs before start (ExecStartPre)
- Orphan processes are cleaned automatically

### 3. Restart Resilience
- Service restarts on failure (Restart=on-failure)
- 10-second delay between restarts (RestartSec=10)
- Rate limiting prevents restart loops (StartLimitBurst=5)

---

## MAINTENANCE COMMANDS

### Check Service Status:
```bash
systemctl status renogy-bt-proxy.service
```

### View Live Logs:
```bash
journalctl -u renogy-bt-proxy.service -f
```

### Restart Service:
```bash
sudo systemctl restart renogy-bt-proxy.service
```

### Check for Orphans Manually:
```bash
ps aux | grep renogy_bt_proxy.py
sudo netstat -tulpn | grep 6053
```

### Force Cleanup (if needed):
```bash
sudo /home/jorik/renogy-bt/scripts/cleanup_port.sh
sudo systemctl restart renogy-bt-proxy.service
```

---

## FILES MODIFIED

1. `/etc/systemd/system/renogy-bt-proxy.service` - Updated with cleanup and conflicts
2. `/home/jorik/renogy-bt/scripts/cleanup_port.sh` - New safe cleanup script
3. Service state: `renogy-bt.service` disabled permanently

---

## CONFIGURATION

- **Config:** `/home/jorik/renogy-bt/config.ini`
- **Python venv:** `/home/jorik/renogy-bt/venv`
- **API Port:** 6053
- **Renogy Device:** BT-TH-FD86824D (6C:B2:FD:86:82:4D)
- **Read Interval:** 30 seconds
- **Proxy Mode:** Scheduled with continuous BLE scanning

---

## SUCCESS CRITERIA MET

✅ Service starts on boot without conflicts
✅ No orphan processes after stop/start cycles
✅ Port 6053 properly released on service stop
✅ BLE proxy forwards all Bluetooth advertisements
✅ Renogy battery data collected every 30 seconds
✅ Service survives restarts and reboots
✅ Resource limits prevent runaway processes
✅ Proper logging to systemd journal

---

## CONCLUSION

The issue was caused by duplicate systemd services competing for the same resources.
The permanent fix includes:
1. Disabled the conflicting service
2. Added intelligent cleanup before service start
3. Implemented proper conflict prevention
4. Added resource limits and restart policies

The system is now production-ready and will survive reboots reliably.
