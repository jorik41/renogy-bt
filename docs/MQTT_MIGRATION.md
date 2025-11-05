# Quick Migration Guide: MQTT to ESPHome Sensors

## Overview

This guide helps you migrate from MQTT-based data logging to ESPHome API sensors.

## Why Migrate?

- ✅ No separate MQTT broker needed
- ✅ More efficient direct protocol
- ✅ Native Home Assistant integration
- ✅ Automatic sensor discovery
- ✅ Cleaner architecture

## Before You Start

**Backup your current setup:**
1. Save a copy of your `config.ini`
2. Export Home Assistant dashboards using Renogy sensors
3. Note which automations use Renogy MQTT sensors

## Migration Steps

### Step 1: Enable ESPHome Sensors

Edit `config.ini`:

```ini
[home_assistant_proxy]
enabled = true
use_native_api = true
with_renogy_client = true
esphome_sensors = true  # Add this line (or ensure it's true)

# Keep MQTT enabled for now
[mqtt]
enabled = true  # Keep enabled during migration
```

### Step 2: Restart the Proxy

```bash
# Stop the current proxy
pkill -f renogy_bt_proxy.py

# Or if using systemd:
sudo systemctl restart renogy-bt-proxy

# Start it again
python3 ./renogy_bt_proxy.py config.ini
```

### Step 3: Verify ESPHome Device in Home Assistant

1. Go to **Settings → Devices & Services → ESPHome**
2. Look for your device (e.g., "renogy.proxy")
3. Click on it to see sensor entities
4. Verify sensor values match your MQTT sensors

Example sensors you should see:
- Battery Voltage
- Battery Current
- Battery Percentage
- PV Voltage (if controller)
- PV Power (if controller)
- Temperature sensors

### Step 4: Update Your Dashboards

Replace MQTT sensor references with ESPHome sensor IDs:

**Before (MQTT):**
```yaml
type: sensor
entity: sensor.solar_power  # MQTT sensor
```

**After (ESPHome):**
```yaml
type: sensor
entity: sensor.bt_th_xxxx_48_pv_power  # ESPHome sensor
```

Tip: ESPHome sensor IDs follow pattern: `sensor.<alias>_<device_id>_<field_name>`

### Step 5: Update Automations

Replace MQTT sensor references in your automations:

**Before (MQTT trigger):**
```yaml
trigger:
  - platform: state
    entity_id: sensor.battery_soc  # MQTT sensor
```

**After (ESPHome trigger):**
```yaml
trigger:
  - platform: state
    entity_id: sensor.bt_th_xxxx_48_battery_percentage  # ESPHome sensor
```

### Step 6: Test Everything

1. **Dashboard**: Verify all cards show correct values
2. **Automations**: Trigger your automations to ensure they work
3. **History**: Check sensor history is being recorded
4. **Energy Dashboard**: Verify energy sensors if used

### Step 7: Disable MQTT (Optional)

Once everything works with ESPHome sensors:

Edit `config.ini`:
```ini
[mqtt]
enabled = false  # Disable MQTT
```

Restart the proxy:
```bash
python3 ./renogy_bt_proxy.py config.ini
```

### Step 8: Clean Up (Optional)

If you're no longer using MQTT anywhere:

1. **Remove MQTT sensors from configuration.yaml** (if manual config)
2. **Uninstall MQTT broker** (if only used for Renogy)
3. **Remove MQTT integration** from Home Assistant (if not needed)

## Sensor Name Mapping

Common MQTT → ESPHome sensor mappings:

| MQTT Sensor | ESPHome Sensor Example |
|-------------|------------------------|
| `sensor.solar_power` | `sensor.bt_th_b00f_97_pv_power` |
| `sensor.battery_soc` | `sensor.bt_th_b00f_97_battery_percentage` |
| `sensor.battery_voltage` | `sensor.bt_th_161e_48_voltage` |
| `sensor.battery_current` | `sensor.bt_th_161e_48_current` |
| `sensor.cell_voltage_1` | `sensor.bt_th_161e_48_cell_voltage_0` |

Note: Actual names depend on your `alias` in config.ini

## Finding ESPHome Sensor IDs

1. Go to **Developer Tools → States**
2. Filter by your device alias (e.g., "BT-TH")
3. Look for entities starting with `sensor.`
4. Click to see full entity_id and attributes

## Rollback Plan

If you need to revert:

1. Edit `config.ini`:
   ```ini
   [mqtt]
   enabled = true  # Re-enable MQTT
   
   [home_assistant_proxy]
   esphome_sensors = false  # Disable ESPHome sensors
   ```

2. Restart proxy
3. Revert dashboard and automation changes
4. ESPHome sensors will stop updating but remain visible (can be disabled in HA)

## Common Issues During Migration

### Issue: ESPHome sensors not appearing

**Solution:**
- Verify `esphome_sensors = true` in config
- Check proxy logs for "Initialized X sensor entities"
- Ensure Home Assistant ESPHome integration is working

### Issue: Sensor values don't match MQTT

**Solution:**
- Both should show same values (within normal variation)
- If different, check which one matches actual device display
- May be timing difference in read cycles

### Issue: Old MQTT sensors interfering

**Solution:**
- Use unique names by changing `alias` in config.ini
- Or disable old MQTT sensors in Home Assistant UI

### Issue: Dashboard breaks after switching

**Solution:**
- Keep both enabled during migration
- Update dashboards one at a time
- Use entity ID instead of friendly name

## Best Practices

1. **Migrate Gradually**: Keep MQTT enabled while testing ESPHome
2. **Test Thoroughly**: Verify all dashboards and automations
3. **Update Documentation**: Note new sensor IDs in your setup docs
4. **Keep Backups**: Save working config before changes

## After Migration

Benefits you should notice:
- ✅ Simpler architecture (one less service)
- ✅ Faster sensor updates (direct protocol)
- ✅ Better integration (native ESPHome device)
- ✅ Automatic discovery (no manual sensor config)

## Need Help?

- Check logs: Look for errors in proxy logs
- Test with both: Keep MQTT enabled to compare
- Documentation: See docs/ESPHOME_SENSORS.md for detailed testing
- Report issues: Include logs and config when asking for help

## Success Checklist

Before disabling MQTT, verify:
- [ ] ESPHome device appears in Home Assistant
- [ ] All expected sensors are present
- [ ] Sensor values are correct and updating
- [ ] Dashboards show correct data
- [ ] Automations trigger correctly
- [ ] Energy dashboard works (if applicable)
- [ ] No errors in proxy logs
- [ ] No errors in Home Assistant logs

Once all checked, you can safely disable MQTT!

## Example Complete Migration

**Original config.ini:**
```ini
[device]
alias = BT-TH-B00F
type = RNG_CTRL

[mqtt]
enabled = true
server = 192.168.1.89
topic = solar/state

[home_assistant_proxy]
enabled = true
use_native_api = true
```

**New config.ini:**
```ini
[device]
alias = BT-TH-B00F
type = RNG_CTRL

[mqtt]
enabled = false  # Disabled after successful migration

[home_assistant_proxy]
enabled = true
use_native_api = true
esphome_sensors = true  # Enabled
```

**Result:**
- No MQTT broker needed
- Sensors appear as: `sensor.bt_th_b00f_97_*`
- Native ESPHome integration
- Cleaner setup

---

**Migration time:** ~30-60 minutes depending on complexity
**Difficulty:** Medium (requires updating dashboards/automations)
**Rollback:** Easy (re-enable MQTT in config)
