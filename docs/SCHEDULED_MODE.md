# Renogy Scheduled Mode

## Overview

The Renogy client now supports two operation modes to control how battery/controller data is read and sent to MQTT:

1. **Scheduled Mode** (Recommended) - Non-blocking, priority to proxy scanning
2. **Continuous Mode** (Legacy) - Continuous polling, blocks proxy scanning

## Configuration

Add these options to the `[home_assistant_proxy]` section in `config.ini`:

```ini
[home_assistant_proxy]
enabled = true
use_native_api = true
with_renogy_client = true  # Enable Renogy client

# Renogy client operation mode
renogy_poll_mode = scheduled    # Use 'scheduled' or 'continuous'
renogy_read_interval = 60       # Interval in seconds (scheduled mode only)
```

## Scheduled Mode (Recommended)

**When to use:** When running the ESPHome Bluetooth proxy alongside Renogy reads.

**How it works:**
- Performs a single read of the Renogy device at configured intervals
- Yields control back to proxy scanner between reads
- Does not block BLE advertisements for Home Assistant
- Less time-critical than proxy scanning

**Benefits:**
- ESPHome proxy gets priority for BLE scanning
- Regular MQTT updates still maintained
- Better for resource-constrained devices (Pi Zero 2 W)
- Prevents Wi-Fi contention issues

**Configuration example:**
```ini
renogy_poll_mode = scheduled
renogy_read_interval = 60  # Read every 60 seconds
```

**Behavior:**
1. Proxy starts and begins scanning for BLE advertisements
2. Every 60 seconds (or configured interval), Renogy read is triggered
3. Client connects to Renogy device, reads data, sends to MQTT
4. Client disconnects and yields back to proxy scanning
5. Repeat at next interval

## Continuous Mode (Legacy)

**When to use:** When not using ESPHome proxy, or when you need real-time data updates.

**How it works:**
- Continuously polls Renogy device based on `[data] poll_interval`
- Blocks BLE airtime during reads
- Original behavior from earlier versions

**Configuration example:**
```ini
renogy_poll_mode = continuous
# Uses [data] poll_interval for timing
```

In the `[data]` section:
```ini
[data]
enable_polling = true
poll_interval = 10  # Read every 10 seconds
```

## Migration Guide

### From Old Behavior to Scheduled Mode

**Old config.ini:**
```ini
[home_assistant_proxy]
enabled = true
use_native_api = true
with_renogy_client = true

[data]
enable_polling = true
poll_interval = 10
```

**New config.ini (recommended):**
```ini
[home_assistant_proxy]
enabled = true
use_native_api = true
with_renogy_client = true
renogy_poll_mode = scheduled      # NEW
renogy_read_interval = 60         # NEW - adjust as needed

[data]
enable_polling = true              # Still needed for scheduled mode
poll_interval = 10                 # Not used in scheduled mode
```

### Choosing the Right Interval

**For scheduled mode:**
- **30-60 seconds**: Good balance for most use cases
- **120-300 seconds**: Slower updates, minimal impact on proxy
- **15-30 seconds**: Faster updates, slight impact on proxy

**Factors to consider:**
- Battery data changes slowly (voltage, current, SOC)
- ESPHome proxy needs consistent BLE airtime
- Wi-Fi quality on your device
- How often you need MQTT updates

## Logging

The proxy logs will show which mode is active:

**Scheduled mode:**
```
INFO:root:Starting Renogy battery client setup (mode: scheduled)
INFO:root:Renogy scheduled reader enabled (interval=60.0s)
INFO:root:Renogy client in scheduled mode - waiting for first scheduled interval
INFO:root:ESPHome proxy running on adapter hci0 (port 6053, mac AA:BB:CC:DD:EE:FF, 
          Renogy client: enabled (mode: scheduled, interval: 60.0s), ...)
```

**Continuous mode:**
```
INFO:root:Starting Renogy battery client setup (mode: continuous)
INFO:root:Starting Renogy client in continuous mode
INFO:root:ESPHome proxy running on adapter hci0 (port 6053, mac AA:BB:CC:DD:EE:FF, 
          Renogy client: enabled (mode: continuous, interval: 60.0s), ...)
```

## Troubleshooting

### No MQTT updates in scheduled mode

1. Check logs for "Triggering scheduled Renogy read"
2. Verify `with_renogy_client = true`
3. Verify `enable_polling = true` in `[data]` section
4. Check `renogy_read_interval` is not too long

### Reads taking too long

1. Increase `renogy_read_interval` to give more time between reads
2. Check BLE signal strength to Renogy device
3. Enable `pause_during_renogy = true` to pause proxy during reads

### Want faster updates

1. Decrease `renogy_read_interval` (minimum ~15 seconds recommended)
2. Or switch to `continuous` mode if proxy scanning is not critical

## Technical Details

### Scheduled Mode Implementation

1. Client is created with `scheduled_mode=True` flag
2. After successful data read, client automatically stops
3. Scheduler task wakes up every `renogy_read_interval` seconds
4. Checks if previous read is still running (skips if yes)
5. Triggers new read by calling `start_battery_client()`
6. Airtime scheduler ensures proxy gets priority

### Continuous Mode Implementation

1. Client is created with `scheduled_mode=False` flag
2. Client continues polling based on `[data] poll_interval`
3. On disconnect or error, client automatically restarts
4. Uses original restart logic with exponential backoff

## See Also

- [config.ini](../config.ini) - Full configuration example
- [README.md](../README.md) - Main project documentation
- [renogy_bt_proxy.py](../renogy_bt_proxy.py) - Proxy implementation
