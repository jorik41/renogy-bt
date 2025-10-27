import json
import logging
import paho.mqtt.publish as publish
import requests
from configparser import ConfigParser
from datetime import datetime

PVOUTPUT_URL = 'http://pvoutput.org/service/r2/addstatus.jsp'

class DataLogger:
    def __init__(self, config: ConfigParser):
        self.config = config
        # keep track of which devices have had HA discovery config published
        self.ha_config_sent = set()

    def log_remote(self, json_data):
        headers = { "Authorization" : f"Bearer {self.config['remote_logging']['auth_header']}" }
        try:
            req = requests.post(
                self.config['remote_logging']['url'],
                json=json_data,
                timeout=15,
                headers=headers,
            )
            if req.status_code == 200:
                logging.info("Log remote 200")
            else:
                logging.error(
                    "Log remote error %s: %s",
                    req.status_code,
                    req.text[:200],
                )
        except requests.RequestException as exc:
            logging.error(f"Log remote failed: {exc}")

    def log_mqtt(self, json_data):
        logging.info("mqtt logging")
        user = self.config['mqtt']['user']
        password = self.config['mqtt']['password']
        auth = None if not user or not password else {"username": user, "password": password}

        alias = self.config['device']['alias']
        device_id = json_data.get('device_id')
        alias_id = f"{alias}_{device_id}" if device_id is not None else alias

        topic_base = self.config['mqtt']['topic']
        topic = f"{topic_base.rstrip('/')}/{alias_id}"

        try:
            publish.single(
                topic,
                payload=json.dumps(json_data),
                hostname=self.config['mqtt']['server'],
                port=self.config['mqtt'].getint('port'),
                auth=auth,
                client_id="renogy-bt",
            )
        except Exception as exc:  # paho raises generic Exception
            logging.error("mqtt publish failed: %s", exc)
            return

        if self.config['mqtt'].getboolean('homeassistant_discovery', fallback=False):
            self.publish_home_assistant_config(auth, json_data, alias_id, topic)

    def publish_home_assistant_config(self, auth, json_data, alias_id, state_topic):
        if alias_id in self.ha_config_sent:
            return

        topic_prefix = f"homeassistant/sensor/{alias_id}"
        device = {
            "identifiers": [alias_id],
            "name": alias_id,
            "manufacturer": "Renogy",
            "model": self.config['device']['type']
        }

        temperature_unit = self.config['data']['temperature_unit'].strip()

        for key in json_data.keys():
            config_topic = f"{topic_prefix}/{key}/config"
            payload = {
                "name": f"{alias_id} {key}",
                "state_topic": state_topic,
                "value_template": f"{{{{ value_json.{key} }}}}",
                "unique_id": f"renogy_bt_{alias_id}_{key}",
                "device": device
            }

            unit, device_class = self._guess_unit_class(key, temperature_unit)
            if unit:
                payload["unit_of_measurement"] = unit
            if device_class:
                payload["device_class"] = device_class
            if isinstance(json_data.get(key), (int, float)):
                if "energy" in key.lower():
                    payload["state_class"] = "total_increasing"
                else:
                    payload["state_class"] = "measurement"

            try:
                publish.single(
                    config_topic,
                    payload=json.dumps(payload),
                    hostname=self.config['mqtt']['server'],
                    port=self.config['mqtt'].getint('port'),
                    auth=auth,
                    client_id="renogy-bt",
                    retain=True,
                )
            except Exception as exc:
                logging.error("Home Assistant discovery publish failed: %s", exc)
                return

        self.ha_config_sent.add(alias_id)

    def _guess_unit_class(self, key, temp_unit):
        lkey = key.lower()
        if 'temperature' in lkey:
            unit = '°F' if temp_unit == 'F' else '°C'
            return unit, 'temperature'
        if lkey.endswith('voltage'):
            return 'V', 'voltage'
        if lkey.endswith('current'):
            return 'A', 'current'
        if lkey.endswith('power'):
            return 'W', 'power'
        if lkey.endswith('percentage') or 'soc' in lkey:
            return '%', 'battery'
        if lkey.endswith('level') and 'battery' in lkey:
            return '%', 'battery'
        if 'amp_hour' in lkey or lkey.endswith('_ah'):
            return 'Ah', None
        if 'energy' in lkey:
            unit = 'kWh' if 'kwh' in lkey else 'Wh'
            return unit, 'energy'
        if lkey.endswith('frequency'):
            return 'Hz', 'frequency'
        return None, None

    def log_pvoutput(self, json_data):
        required = (
            'power_generation_today',
            'pv_power',
            'power_consumption_today',
            'load_power',
            'controller_temperature',
            'battery_voltage',
        )
        if not all(field in json_data for field in required):
            missing = [field for field in required if field not in json_data]
            logging.error("pvoutput logging skipped; missing fields: %s", ", ".join(missing))
            return

        date_time = datetime.now().strftime("d=%Y%m%d&t=%H:%M")
        data = (
            f"{date_time}&v1={json_data['power_generation_today']}"
            f"&v2={json_data['pv_power']}"
            f"&v3={json_data['power_consumption_today']}"
            f"&v4={json_data['load_power']}"
            f"&v5={json_data['controller_temperature']}"
            f"&v6={json_data['battery_voltage']}"
        )
        try:
            response = requests.post(
                PVOUTPUT_URL,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Pvoutput-Apikey": self.config['pvoutput']['api_key'],
                    "X-Pvoutput-SystemId":  self.config['pvoutput']['system_id']
                },
                timeout=15,
            )
            response.raise_for_status()
            logging.info("pvoutput %s", response.status_code)
        except requests.RequestException as exc:
            logging.error("pvoutput logging failed: %s", exc)
