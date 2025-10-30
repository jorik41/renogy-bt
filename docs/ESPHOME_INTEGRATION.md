# ESPHome Integration for Home Assistant

This guide explains how to use the ESPHome native API to integrate the Bluetooth proxy with Home Assistant while also monitoring your Renogy battery.

## Overview

The renogy-bt project can now act as an ESPHome device, making it discoverable and usable through Home Assistant's ESPHome integration. This provides a native, seamless way to extend Home Assistant's Bluetooth range using any Linux device with Bluetooth capability, **while simultaneously reading and publishing your Renogy battery data to MQTT**.

## Features

- **Automatic Discovery**: Device advertises itself via mDNS/zeroconf
- **Native API**: Uses ESPHome's native protocol (TCP port 6053)
- **Bluetooth Proxy**: Forwards BLE advertisements to Home Assistant
- **Renogy Integration**: Reads battery/controller data from your Renogy device
- **MQTT Publishing**: Publishes battery data to MQTT with Home Assistant discovery
- **Zero Configuration**: No API tokens or manual setup required for BT proxy
- **Compatible**: Works with Home Assistant's ESPHome integration

## Quick Start

### 1. Configuration

Edit `config.ini` with your complete device settings:

```ini
[device]
adapter = hci0
mac_addr = 6C:B2:FD:86:82:4D  # Your Renogy device Bluetooth MAC
alias = BT-TH-FD86824D
type = RNG_BATT  # RNG_CTRL for charge controller, RNG_BATT for battery
device_id = 48,49,50,51  # For battery hubs with multiple batteries

[data]
enable_polling = true
poll_interval = 10  # Read battery data every 10 seconds
temperature_unit = F

[mqtt]
enabled = true
server = 192.168.1.89
port = 1883
topic = solar/state
homeassistant_discovery = true

[home_assistant_proxy]
enabled = true
native_api_port = 6053
device_name = renogy-bt-proxy
adapter = hci0
```

### 2. Run the Integrated Proxy

Using the example script:

```bash
python3 esphome_proxy_example.py config.ini
```

The script will:
- Start the ESPHome Bluetooth proxy (for HA discovery)
- Start the BLE scanner (forward advertisements to HA)
- Connect to your Renogy device
- Read battery data periodically
- Publish data to MQTT with Home Assistant discovery

### 3. Add to Home Assistant

**For the Bluetooth Proxy:**

1. Open Home Assistant
2. Go to **Settings** → **Devices & Services**
3. Click **"+ ADD INTEGRATION"**
4. Search for **"ESPHome"**
5. Your device should appear automatically
6. Click on it to add it

That's it! The device now acts as a Bluetooth proxy for HA.

**For Battery Sensors:**

If MQTT and Home Assistant discovery are enabled in your config, battery sensors will automatically appear in Home Assistant after the first data reading.

## What You Get

### ESPHome Bluetooth Proxy
- Extends Home Assistant's Bluetooth range
- Shows up as a native ESPHome device
- Allows HA to discover Bluetooth devices through this proxy

### Battery Monitoring
- All battery/controller sensors published to MQTT
- Automatic Home Assistant discovery
- Real-time voltage, current, temperature, SOC, etc.
- Energy tracking (if configured)

## Troubleshooting

### Device Not Appearing in ESPHome Integration

1. Check mDNS/Avahi is running:
   ```bash
   systemctl status avahi-daemon
   avahi-browse _esphomelib._tcp -t
   ```

2. Verify port 6053 is open:
   ```bash
   netstat -tuln | grep 6053
   ```

3. Check firewall settings

### No BLE Advertisements

1. Verify Bluetooth adapter is working:
   ```bash
   hciconfig
   ```

2. Check permissions (may need sudo or capabilities)

### Battery Data Not Publishing

1. Check MQTT connection:
   - Verify server IP and port in config.ini
   - Check MQTT broker logs

2. Verify Renogy device MAC address:
   ```bash
   python3 scan_devices.py --adapter hci0
   ```

3. Check logs for connection errors

### No Battery Sensors in Home Assistant

1. Verify `homeassistant_discovery = true` in `[mqtt]` section
2. Check that MQTT integration is configured in Home Assistant
3. Wait for first data reading (poll_interval seconds)
4. Check MQTT explorer for `homeassistant/sensor/...` topics

## References

- [ESPHome Bluetooth Proxy Docs](https://esphome.io/components/bluetooth_proxy.html)
- [Home Assistant ESPHome Integration](https://www.home-assistant.io/integrations/esphome/)
- [Home Assistant MQTT Discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
