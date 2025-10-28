# Home Assistant Bluetooth Proxy

This project can expose a full ESPHome-compatible Bluetooth proxy while it
continues to poll the Renogy battery/controller locally. Home Assistant connects
over the ESPHome native API (default port `6053`) and sees the device as a
standard ESPHome Bluetooth Proxy node.

## Configuration

Enable the proxy in `config.ini`:

```ini
[home_assistant_proxy]
enabled = true
bind_host = 0.0.0.0        # where the ESPHome API listens
port = 6053                # Home Assistant default
name = renogy-bt-proxy     # ESPHome node name
friendly_name = Renogy Bluetooth Proxy
adapter = hci0             # Linux adapter name
blocked_addresses = 6C:B2:FD:86:82:4D
max_connections = 2
battery_retry_seconds = 30
```

### Key options

| Option | Description |
| --- | --- |
| `blocked_addresses` | Prevent Home Assistant from taking over peripherals that this project must own (e.g. Renogy battery MAC). |
| `max_connections` | How many simultaneous remote GATT sessions Home Assistant may open through the proxy. |
| `battery_retry_seconds` | Background retry interval for the Renogy polling client. The proxy keeps running even when the battery cannot be discovered; the Renogy client will retry on this cadence. |

The `[device]` section should continue to point to your Renogy hardware so the
local poller can be launched in the background.

## Running the proxy

```bash
source venv/bin/activate
python ha_proxy_example.py config.ini
```

You should see log output similar to:

```
INFO:renogybt.home_assistant_proxy:ESPHome proxy listening on ('0.0.0.0', 6053)
INFO:renogybt.home_assistant_proxy:mDNS service renogy-bt-proxy._esphomelib._tcp.local. registered
```

At that point Home Assistant can add the device using the ESPHome integration.
If the Renogy BLE client cannot find the battery, it will log the failure and
retry after `battery_retry_seconds` without stopping the proxy.

## Troubleshooting

- Use `bluetoothctl scan on` (or a phone scanner) to confirm the Renogy device
  is advertising at the MAC address configured in `config.ini`.
- Tail the proxy logs (`tail -f ha_proxy.log`) to watch the retry loop and Home
  Assistant connections.
- If another process already listens on `port`, adjust `port` in
  `[home_assistant_proxy]` and restart the proxy.
