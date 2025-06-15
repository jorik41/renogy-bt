import logging
import configparser
import os
import sys
from renogybt import DCChargerClient, InverterClient, RoverClient, RoverHistoryClient, BatteryClient, DataLogger, Utils

logging.basicConfig(level=logging.INFO)

config_file = sys.argv[1] if len(sys.argv) > 1 else 'config.ini'
config_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), config_file)
config = configparser.ConfigParser(inline_comment_prefixes=('#'))
config.read(config_path)
data_logger: DataLogger = DataLogger(config)
energy_file = os.path.join(os.path.dirname(config_path), 'energy_totals.json')

# store battery data when reading multiple batteries
battery_map = {}

# the callback func when you receive data
def on_data_received(client, data):
    Utils.add_calculated_values(data)
    alias = config['device']['alias']
    dev_id = data.get('device_id')
    alias_id = f"{alias}_{dev_id}" if dev_id is not None else alias
    Utils.update_energy_totals(
        data,
        interval_sec=config['data'].getint('poll_interval'),
        file_path=energy_file,
        alias=alias_id,
    )
    filtered_data = Utils.filter_fields(data, config['data']['fields'])
    logging.info(f"{client.ble_manager.device.name} => {filtered_data}")

    # collect data for combined MQTT message when multiple batteries are read
    if config['device']['type'] == 'RNG_BATT' and len(client.device_ids) > 1:
        dev_id = data.get('device_id')
        if dev_id is not None:
            battery_map[dev_id] = data
        if len(battery_map) == len(client.device_ids):
            combined = Utils.combine_battery_readings(battery_map)
            filtered_combined = Utils.filter_fields(combined, config['data']['fields'])
            logging.info(f"combined => {filtered_combined}")
            if config['mqtt'].getboolean('enabled'):
                data_logger.log_mqtt(json_data=filtered_combined)
            battery_map.clear()
    if config['remote_logging'].getboolean('enabled'):
        data_logger.log_remote(json_data=filtered_data)
    if config['mqtt'].getboolean('enabled'):
        data_logger.log_mqtt(json_data=filtered_data)
    if config['pvoutput'].getboolean('enabled') and config['device']['type'] == 'RNG_CTRL':
        data_logger.log_pvoutput(json_data=filtered_data)
    if not config['data'].getboolean('enable_polling'):
        client.stop()

# error callback
def on_error(client, error):
    logging.error(f"on_error: {error}")

# start client
if config['device']['type'] == 'RNG_CTRL':
    RoverClient(config, on_data_received, on_error).start()
elif config['device']['type'] == 'RNG_CTRL_HIST':
    RoverHistoryClient(config, on_data_received, on_error).start()
elif config['device']['type'] == 'RNG_BATT':
    BatteryClient(config, on_data_received, on_error).start()
elif config['device']['type'] == 'RNG_INVT':
    InverterClient(config, on_data_received, on_error).start()
elif config['device']['type'] == 'RNG_DCC':
    DCChargerClient(config, on_data_received, on_error).start()
else:
    logging.error("unknown device type")
