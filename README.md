# Renogy BT
![256924763-940c205e-738d-4a68-982f-1695c80bfed5](https://github.com/cyrils/renogy-bt/assets/5549113/bcdef6ec-efc9-44fd-af70-67165cf6862e)

Cross-platform Python library to read Renogy¹ Solar Charge Controllers and Smart Batteries using  [BT-1](https://www.renogy.com/bt-1-bluetooth-module-new-version/) or [BT-2](https://www.renogy.com/bt-2-bluetooth-module/) type (RS232 or RS485)  bluetooth modules. Tested with **Rover** / **Wanderer** series charge controllers, but it might also work with other  "SRNE like" devices like Rich Solar, PowMr etc. See the list of [compatible devices](#compatibility). It can also upload data to local **MQTT** broker, **PVOutput** cloud or your own custom server.

## Dependencies
You will need [Python](https://www.python.org/downloads/) 3.6 or above in your system. In some platforms you may have to create python virtual environment. Then install dependencies by running the command:
```sh
python3 -m pip install -r requirements.txt
```
This library should work on any modern Linux/Windows/Mac platforms that supports [Bleak](https://github.com/hbldh/bleak). 

## Example
Each device needs a separate [config.ini](https://github.com/cyrils/renogy-bt1/blob/main/config.ini) file. Update the config with the correct values for `mac_addr`, `alias` and `type`.  If your system has multiple bluetooth interfaces you can specify which one to use via the optional `adapter` setting (for example `hci0`). Once configured, install the dependencies and run the example:

Install the dependencies once per environment:
```sh
python3 -m pip install -r requirements.txt
```

If you prefer to use the bundled virtual environment, activate it via `source venv/bin/activate` or run the scripts with `./venv/bin/python`.

```sh
python3 ./example.py config.ini
```

The library now retries bluetooth discovery and connection automatically if the adapter is not ready right away. This is helpful on systems that take a moment to power on their bluetooth interface after a reboot.

### Discovering your Renogy Bluetooth address

Use the helper script to list nearby Bluetooth devices. Renogy devices usually start with `BT-TH`, `RNGRBP`, or `BTRIC`.

```sh
python3 scan_devices.py --adapter hci0
```

Devices whose names match a known prefix are flagged with a `*`. Copy the address and name into `config.ini`. When working with a Renogy Smart Battery hub, specify all battery `device_id` values (for example `48,49,50,51`) in the `[device]` section.

### Home Assistant ESPHome Bluetooth Proxy

Use the ESPHome native API to make the device appear as a native ESPHome Bluetooth proxy in Home Assistant. This provides automatic discovery and seamless integration while also reading your Renogy battery data and publishing to MQTT.

**Features:**
- Automatic discovery via mDNS/zeroconf
- Native ESPHome integration (port 6053)
- Zero configuration in Home Assistant
- Full Bluetooth proxy functionality
- **Integrated Renogy battery monitoring**
- **MQTT publishing with Home Assistant discovery**

**Quick Start:**

1. Edit `config.ini` with your device settings:
```ini
[device]
adapter = hci0
mac_addr = 6C:B2:FD:86:82:4D  # Your Renogy device MAC
alias = BT-TH-FD86824D
type = RNG_BATT  # or RNG_CTRL for charge controller

[mqtt]
enabled = true
server = 192.168.1.89
port = 1883
topic = solar/state
homeassistant_discovery = true

[home_assistant_proxy]
enabled = true
device_name = renogy-bt-proxy
adapter = hci0
```

2. Run the integrated ESPHome proxy:
```sh
python3 ./esphome_proxy_example.py config.ini
```

3. In Home Assistant:
   - Go to Settings → Devices & Services
   - Click "+ Add Integration"
   - Search for "ESPHome"
   - Your device appears automatically as a Bluetooth proxy!
   - Battery sensors will appear via MQTT discovery

📖 **[Full ESPHome Integration Guide](docs/ESPHOME_INTEGRATION.md)**

**How to get mac address?**

The library will automatically list possible compatible devices discovered nearby, just run `example.py`. You can alternatively use apps like [BLE Scanner](https://play.google.com/store/apps/details?id=com.macdom.ble.blescanner).

**Output**

```
INFO:root:Init RoverClient: BT-TH-B00FXXXX => 80:6F:B0:0F:XX:XX
INFO:root:Adapter status - Powered: True
INFO:root:Starting discovery...
INFO:root:Devices found: 5
INFO:root:Found matching device BT-TH-B00FXXXX => [80:6F:B0:0F:XX:XX]
INFO:root:[80:6f:b0:0f:XX:XX] Discovered, alias = BT-TH-B00FXXXX
INFO:root:[80:6F:B0:0F:XX:XX] Connected
INFO:root:[80:6F:B0:0F:XX:XX] Resolved services
INFO:root:found write characteristic 0000ffd1-0000-1000-8000-00805f9b34fb
INFO:root:subscribed to notification 0000fff1-0000-1000-8000-00805f9b34fb
INFO:root:resolved services
INFO:root:reading params
DEBUG:root:create_read_request 256 => [255, 3, 1, 0, 0, 34, 209, 241]
INFO:root:characteristic_write_value_succeeded
INFO:root:characteristic_enable_notifications_succeeded
INFO:root:on_data_received: response for read operation
DEBUG:root:BT-TH-B00FXXXX => {'function': 'READ', 'model': 'RNG-CTRL-WND10', 'battery_percentage': 87, 'battery_voltage': 12.9, 'battery_current': 2.58, 'battery_temperature': 25, 'controller_temperature': 33, 'load_status': 'off', 'load_voltage': 0.0,'load_current': 0.0, 'load_power': 0, 'pv_voltage': 17.1, 'pv_current': 2.04, 'pv_power': 35, 'max_charging_power_today': 143, 'max_discharging_power_today': 0, 'charging_amp_hours_today': 34, 'discharging_amp_hours_today': 34, 'power_generation_today': 432, 'power_consumption_today': 0, 'power_generation_total': 426038, 'charging_status': 'mppt', 'battery_type': 'lithium', 'device_id': 97}
INFO:root:Exit: Disconnecting device: BT-TH-B00FXXXX [80:6F:B0:0F:XX:XX]
```
```
# Rover historical data (7 days summary)
DEBUG:root:BT-TH-30A3XXXX => {'function': 'READ', 'daily_power_generation': [1754, 1907, 1899, 1804, 1841, 1630, 1344],'daily_charge_ah': [135, 147, 147, 139, 142, 125, 102], 'daily_max_power': [234, 344, 360, 335, 331, 307, 290]}
```
```
# Battery output
DEBUG:root:BT-TH-161EXXXX => {'function': 'READ', 'model': 'RBT100LFP12S-G', 'cell_count': 4, 'cell_voltage_0': 3.6, 'cell_voltage_1': 3.6, 'cell_voltage_2': 3.6, 'cell_voltage_3': 3.6, 'sensor_count': 4, 'temperature_0': 21.0, 'temperature_1': 21.0, 'temperature_2': 21.0, 'temperature_3': 21.0, 'current': 1.4, 'voltage': 14.5, 'remaining_charge': 99.941, 'capacity': 100.0, 'device_id': 48} 
```
```
# Inverter output
DEBUG:root:BTRIC13400XXXX => {'function': 'READ', 'input_voltage': 124.9, 'input_current': 2.2, 'output_voltage': 124.9, 'output_current': 1.19, 'output_frequency': 59.97, 'battery_voltage': 14.4, 'temperature': 30.0, 'input_frequency': 59.97, 'device_id': 32, 'model': 'RIV1230RCH-SPS', 'battery_percentage': 100, 'charging_current': 0.7, 'solar_voltage': 0.0, 'solar_current': 0.0, 'solar_power': 0, 'charging_status': 'deactivated', 'charging_power': 10, 'load_curent': 1.2, 'load_active_power': 108, 'load_apparent_power': 150, 'line_charging_current': 0.0, 'load_percentage': 5, '__device': 'BTRIC13400XXXX', '__client': 'InverterClient'}
```

```
# DC Charger output
INFO:root:BT-TH-XXXXXXXX => {'function': 'READ', 'model': 'RBC50D1S-G1', 'device_id': 96, 'battery_percentage': 100, 'battery_voltage': 13.2, 'combined_charge_current': 0.0, 'controller_temperature': 18, 'battery_temperature': 25, 'alternator_voltage': 12.9, 'alternator_current': 0.0, 'alternator_power': 0, 'pv_voltage': 0.0, 'pv_current': 0.0, 'pv_power': 0, 'battery_min_voltage_today': 13.2, 'battery_max_voltage_today': 13.3, 'battery_max_current_today': 17.02, 'max_charging_power_today': 238, 'charging_amp_hours_today': 25, 'power_generation_today': 336, 'total_working_days': 703, 'count_battery_overdischarged': 0, 'count_battery_fully_charged': 1435, 'battery_ah_total_accumulated': 5607, 'power_generation_total': 76580, 'charging_status': 'current limiting', 'error': 'battery_over_discharge', 'battery_type': None, '__device': 'BT-TH-XXXXXXXX', '__client': 'DCChargerClient'}
```

**Have multiple devices in Hub mode?**

If you have multiple devices connected to a single BT-2 module (daisy chained or using [Communication Hub](https://www.renogy.com/communication-hub/)), you need to find out the individual device Id (aka address) of each of these devices. Below are some of the usual suspects:

|  | Stand-alone | Daisy-chained | Hub mode |
| :-------- | :-------- | :-------- | :-------- |
|  Controller | 255, 17 | 16, 17 | 96, 97 |
|  Battery | 255 | 33, 34, 35 | 48, 49, 50 |
|  Inverter | 255, 32 | 32 | 32 |

 If you receive no response or garbled data with above ids, connect a single device to the Hub at a time and use the default broadcast address of 255 in `config.ini` to find out the actual `device_id` from output log. Then use this device Id to connect in Hub mode. When using a Hub with multiple devices, `device_id` in `config.ini` can contain a comma separated list (e.g. `48,49,50`).

## Compatibility
| Device | Type | Adapter | Supported |
| -------- | :-------- | :--------: | :--------: |
| Renogy Rover/Wanderer/Adventurer | Controller |  BT-1 | ✅ |
| Renogy Rover Elite RCC40RVRE | Controller | BT-2 |  ✅ |
| Renogy DC-DC Charger DCC50S | Controller | BT-2 |  ✅ |
| SRNE ML24/ML48 Series | Controller | BT-1 | ✅ |
| RICH SOLAR 20/40/60 | Controller | BT-1 | ✅ |
| Renogy RBT100LFP12S / RBT50LFP48S | Battery | BT-2 | ✅ |
| Renogy RBT100LFP12-BT / RBT200LFP12-BT (Built-in BLE) | Battery | - | ✅ |
| Renogy RBT12100LFP-BT / RBT12200LFP-BT (Pro Series) | Battery | - | ✅ |
| Renogy RIV4835CSH1S | Inverter | BT-2 | ✅ |
| Renogy Rego RIV1230RCH (Built-in BLE) | Inverter | - | ✅ |
| Renogy Smart Shunt | Shunt | - | ❌ |

## Data logging

Supports logging data to local MQTT brokers like [Mosquitto](https://mosquitto.org/) or [Home Assistant](https://www.home-assistant.io/) dashboards. You can also log it to third party cloud services like [PVOutput](https://pvoutput.org/). See [config.ini](https://github.com/cyrils/renogy-bt1/blob/main/config.ini) for more details. Note that free PVOutput accounts have a cap of one request per minute.

When multiple `device_id`s are configured (e.g. `48,49` when using a BT‑2 hub), data from each device is published to a unique MQTT topic in the form `<base_topic>/<alias>_<device_id>`. For a single device the `<device_id>` suffix is still added, ensuring every device has its own topic. Up to eight battery devices can be listed and will be combined using `Utils.combine_battery_readings`.
The combined payload also exposes `combined_energy_in_kwh` and `combined_energy_out_kwh` which sum the energy in and out across all batteries.
Energy totals for each device are written to `energy_totals.json`. Each update
stores the accumulated mAh with a timestamp so the `energy_in_kwh` and
`energy_out_kwh` values reflect the real energy transferred between polls.
These totals are published as energy sensors via MQTT and can be selected
directly in the Home Assistant energy dashboard.

If you enable `homeassistant_discovery` under the `[mqtt]` section in `config.ini`, sensors will be automatically created in Home Assistant using MQTT discovery. Alternatively you can configure them manually as shown below:

Example config to add to your home assistant `configuration.yaml` (using the topic generated for device `BT-TH-XXXX_48` as an example):
```yaml
mqtt:
  sensor:
    - name: "Solar Power"
      state_topic: "solar/state/BT-TH-XXXX_48"
      device_class: "power"
      unit_of_measurement: "W"
      value_template: "{{ value_json.pv_power }}"
    - name: "Battery SOC"
      state_topic: "solar/state/BT-TH-XXXX_48"
      device_class: "battery"
      unit_of_measurement: "%"
      value_template: "{{ value_json.battery_percentage }}"
# check output log for more fields
```

**Custom logging**

Should you choose to upload to your own server, the json data is posted as body of the HTTP POST call. The optional `auth_header` is sent as http header `Authorization: Bearer <auth-header>`

Example php code at the server:
```php
$headers = getallheaders();
if ($headers['Authorization'] != "Bearer 123456789") {
    header( 'HTTP/1.0 403 Forbidden', true, 403 );
    die('403 Forbidden');
}
$json_data = json_decode(file_get_contents('php://input'), true);
```

**How to get continues output?**

 The best way to get continues data is to schedule a cronjob by running `crontab -e` and insert the following command:
```sh
*/5 * * * * python3 /path/to/renogy-bt/example.py config.ini #runs every 5 mins
```
If you want to monitor real-time data, turn on polling in `config.ini` for continues streaming (default interval is 10 secs). You may also register it as a [service](https://github.com/cyrils/renogy-bt/issues/77) for added reliability.

### Disclaimer

¹This is not an official library endorsed by the device manufacturer. Renogy and all other trademarks in this repo are the property of their respective owners and their use herein does not imply any sponsorship or endorsement.

## References

 - [Olen/solar-monitor](https://github.com/Olen/solar-monitor)
 - [corbinbs/solarshed](https://github.com/corbinbs/solarshed)
 - [Renogy modbus documentation](https://github.com/cyrils/renogy-bt/discussions/94)
 - [mavenius/renogy-bt-esphome](//github.com/mavenius/renogy-bt-esphome) - ESPHome port of this project
