# ESPHome Sensor Integration - Testing Guide

## Overview

This guide helps you test the new ESPHome sensor integration feature that sends Renogy device data directly to Home Assistant via the ESPHome API.

## Prerequisites

1. Working Renogy BT proxy setup (see main README.md)
2. Home Assistant with ESPHome integration
3. A Renogy device configured in `config.ini`

## Quick Start

### 1. Enable ESPHome Sensors

Edit your `config.ini` to enable ESPHome sensors:

```ini
[home_assistant_proxy]
enabled = true
use_native_api = true
with_renogy_client = true
esphome_sensors = true  # Enable sensor integration (default)

[mqtt]
enabled = false  # Optional: disable MQTT if you don't need it
```

### 2. Start the Proxy

```bash
python3 ./renogy_bt_proxy.py config.ini
```

### 3. Check the Logs

You should see log messages indicating sensor entities are being created:

```
INFO:root:Initialized 14 sensor entities for ESPHome API
DEBUG:root:Sent sensor states to ESPHome API
```

### 4. Add Device to Home Assistant

1. Go to **Settings → Devices & Services**
2. Look for ESPHome discovery notification or manually add:
   - Click **Add Integration → ESPHome**
   - Enter your proxy's IP address
   - Port: `6053`
   - Leave password blank
3. Click **Submit**

### 5. Verify Sensors

1. Go to **Settings → Devices & Services → ESPHome**
2. Click on your proxy device (e.g., "renogy.proxy")
3. You should see sensor entities like:
   - Battery Voltage
   - Battery Current
   - Battery Percentage
   - Temperature sensors
   - PV Voltage/Current/Power (for controllers)
   - Cell voltages (for batteries)

## Expected Behavior

### On First Data Read

When the Renogy client successfully reads data for the first time:
- Sensor entities are automatically created based on available data
- Each numeric field becomes a sensor with appropriate units and device class
- Entities are registered with Home Assistant

### On Subsequent Reads

- Sensor state updates are sent to Home Assistant
- Values update in real-time based on `renogy_read_interval`
- No additional entity creation (unless new data fields appear)

## Troubleshooting

### No Sensors Appearing

**Check logs for:**
```
INFO:root:Initialized X sensor entities for ESPHome API
```

If missing, verify:
- `esphome_sensors = true` in config
- `with_renogy_client = true` in config
- Renogy client is successfully connecting and reading data

### Sensors Not Updating

**Check logs for:**
```
DEBUG:root:Sent sensor states to ESPHome API
```

If missing:
- Verify Home Assistant is subscribed to states (check for "subscribed to sensor states")
- Check that Renogy reads are happening (look for data read log messages)

### Connection Issues

If ESPHome integration can't connect:
- Verify port `6053` is accessible
- Check firewall settings
- Try manual IP address instead of auto-discovery
- Verify `device_name = renogy.proxy` (must contain a dot)

## Testing Different Device Types

### Battery (RNG_BATT)

Expected sensors:
- `voltage` - Battery voltage (V)
- `current` - Battery current (A)
- `remaining_charge` - Remaining charge (Ah)
- `capacity` - Battery capacity (Ah)
- `cell_voltage_X` - Individual cell voltages (V)
- `temperature_X` - Temperature sensors (°C or °F)

### Controller (RNG_CTRL)

Expected sensors:
- `battery_percentage` - Battery SOC (%)
- `battery_voltage` - Battery voltage (V)
- `battery_current` - Battery current (A)
- `pv_voltage` - Solar panel voltage (V)
- `pv_current` - Solar panel current (A)
- `pv_power` - Solar power (W)
- `load_voltage` - Load voltage (V)
- `load_current` - Load current (A)
- `load_power` - Load power (W)
- `*_temperature` - Temperature sensors (°C or °F)

### Inverter (RNG_INVT)

Expected sensors:
- `input_voltage` - Input voltage (V)
- `input_current` - Input current (A)
- `output_voltage` - Output voltage (V)
- `output_current` - Output current (A)
- `battery_voltage` - Battery voltage (V)
- `temperature` - Temperature (°C or °F)

## Comparing with MQTT

To verify ESPHome sensors match MQTT data:

1. Enable both temporarily:
```ini
[mqtt]
enabled = true

[home_assistant_proxy]
esphome_sensors = true
```

2. Compare sensor values in Home Assistant:
   - ESPHome sensors: Check device entities
   - MQTT sensors: Check MQTT topics under `solar/state/`

3. Values should match (within normal read variation)

## Performance Notes

- **Scheduled Mode** (recommended): Sensor updates every `renogy_read_interval` seconds
- **Continuous Mode**: Sensor updates every `poll_interval` seconds
- No performance impact on Bluetooth proxy functionality
- Minimal network overhead (only numeric sensor states sent)

## Debug Mode

For detailed logging, modify the proxy script:

```python
logging.basicConfig(level=logging.DEBUG)
```

This shows:
- Sensor entity creation details
- State update transmissions
- Protocol-level ESPHome API messages

## Migration from MQTT

If migrating from MQTT-only setup:

### Step 1: Enable ESPHome Sensors
```ini
[home_assistant_proxy]
esphome_sensors = true
```

### Step 2: Keep MQTT Running Initially
Test ESPHome sensors while MQTT is still active.

### Step 3: Update Automations/Scripts
Replace MQTT sensor references with ESPHome sensor IDs.

### Step 4: Disable MQTT (Optional)
```ini
[mqtt]
enabled = false
```

### Step 5: Remove MQTT Config
Clean up `configuration.yaml` MQTT sensor definitions if using manual config.

## Common Issues

### Duplicate Sensors

If you see duplicate sensors after enabling ESPHome:
- Old MQTT sensors may still exist
- Remove old MQTT sensor definitions from Home Assistant
- Or use unique naming in `alias` config field

### Wrong Units

If temperature shows wrong unit:
- Check `temperature_unit` in `[data]` section
- Should be `C` or `F`
- Sensors use this setting

### Missing Energy Sensors

Energy tracking (`energy_in_kwh`, `energy_out_kwh`) requires:
- Multiple data reads to accumulate
- Check `energy_totals.json` file is being created
- May take a few poll cycles to appear

## Reporting Issues

When reporting issues, include:
1. Relevant config.ini sections
2. Proxy logs (with DEBUG level)
3. Home Assistant ESPHome integration logs
4. Device type (battery, controller, inverter)
5. Expected vs actual behavior

## Success Indicators

✅ Proxy starts without errors
✅ "Initialized X sensor entities" in logs
✅ "Sent sensor states to ESPHome API" in logs
✅ Home Assistant discovers/connects to device
✅ Sensor entities appear in device page
✅ Sensor values update regularly
✅ Units and device classes correct

## Next Steps

Once sensors are working:
- Add sensors to Home Assistant dashboards
- Create automations based on sensor values
- Set up alerts for battery/charging thresholds
- Use in Energy dashboard (for energy sensors)
- Optionally disable MQTT if no longer needed

## See Also

- [Main README](README.md) - Full documentation
- [SCHEDULED_MODE.md](docs/SCHEDULED_MODE.md) - Scheduling options
- [config.ini](config.ini) - Configuration reference
