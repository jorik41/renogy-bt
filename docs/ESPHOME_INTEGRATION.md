# ESPHome Integration for Home Assistant

This guide explains how to use the ESPHome native API to integrate the Bluetooth proxy with Home Assistant.

## Overview

The renogy-bt project can now act as an ESPHome device, making it discoverable and usable through Home Assistant's ESPHome integration. This provides a native, seamless way to extend Home Assistant's Bluetooth range using any Linux device with Bluetooth capability.

## Features

- **Automatic Discovery**: Device advertises itself via mDNS/zeroconf
- **Native API**: Uses ESPHome's native protocol (TCP port 6053)
- **Bluetooth Proxy**: Forwards BLE advertisements to Home Assistant
- **Zero Configuration**: No API tokens or manual setup required
- **Compatible**: Works with Home Assistant's ESPHome integration

## Quick Start

### 1. Configuration

Edit `config.ini` and enable native API mode:

```ini
[home_assistant_proxy]
enabled = true
use_native_api = true
native_api_port = 6053
device_name = renogy-bt-proxy
adapter = hci0
```

### 2. Run the Proxy

Using the example script:

```bash
python3 esphome_proxy_example.py
```

### 3. Add to Home Assistant

1. Open Home Assistant
2. Go to **Settings** → **Devices & Services**
3. Click **"+ ADD INTEGRATION"**
4. Search for **"ESPHome"**
5. Your device should appear automatically
6. Click on it to add it

That's it! The device now acts as a Bluetooth proxy.

## Troubleshooting

### Device Not Appearing

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

## References

- [ESPHome Bluetooth Proxy Docs](https://esphome.io/components/bluetooth_proxy.html)
- [Home Assistant ESPHome Integration](https://www.home-assistant.io/integrations/esphome/)
