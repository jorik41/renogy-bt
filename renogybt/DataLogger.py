import json
import logging
import paho.mqtt.client as mqtt
import requests
import time
from configparser import ConfigParser
from datetime import datetime

PVOUTPUT_URL = 'http://pvoutput.org/service/r2/addstatus.jsp'

class CircuitBreaker:
    """Simple circuit breaker to prevent repeated failed requests."""
    def __init__(self, failure_threshold=3, timeout=60):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failure_count = 0
        self.last_failure_time = 0
        self.is_open = False
    
    def record_success(self):
        """Reset circuit breaker on success."""
        self.failure_count = 0
        self.is_open = False
    
    def record_failure(self):
        """Record a failure and open circuit if threshold reached."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.is_open = True
            logging.warning("Circuit breaker opened after %s failures", self.failure_count)
    
    def can_attempt(self):
        """Check if request can be attempted."""
        if not self.is_open:
            return True
        # Check if timeout has passed to try again
        if time.time() - self.last_failure_time > self.timeout:
            logging.info("Circuit breaker half-open, attempting request")
            self.is_open = False
            self.failure_count = 0
            return True
        return False

class DataLogger:
    def __init__(self, config: ConfigParser):
        self.config = config
        # keep track of which devices have had HA discovery config published
        self.ha_config_sent = set()
        # Reuse HTTP session for better performance
        self._http_session = None
        # Reuse MQTT client connection
        self._mqtt_client = None
        self._mqtt_connected = False
        # Circuit breakers for different services
        self._remote_breaker = CircuitBreaker(failure_threshold=5, timeout=120)
        self._pvoutput_breaker = CircuitBreaker(failure_threshold=3, timeout=300)
        self._mqtt_breaker = CircuitBreaker(failure_threshold=5, timeout=60)
        # Rate limiting for MQTT publishes
        self._last_mqtt_publish = {}  # track last publish time per topic
        self._mqtt_min_interval = 1.0  # minimum 1 second between publishes to same topic

    def _get_http_session(self):
        """Get or create a persistent HTTP session."""
        if self._http_session is None:
            self._http_session = requests.Session()
            # Set keep-alive and connection pooling
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=1,
                pool_maxsize=2,
                max_retries=0
            )
            self._http_session.mount('http://', adapter)
            self._http_session.mount('https://', adapter)
        return self._http_session

    def log_remote(self, json_data):
        if not self._remote_breaker.can_attempt():
            logging.debug("Remote logging circuit breaker is open, skipping")
            return
        
        headers = { "Authorization" : f"Bearer {self.config['remote_logging']['auth_header']}" }
        try:
            session = self._get_http_session()
            req = session.post(
                self.config['remote_logging']['url'],
                json=json_data,
                timeout=15,
                headers=headers,
            )
            if req.status_code == 200:
                logging.debug("Log remote 200")
                self._remote_breaker.record_success()
            else:
                logging.error(
                    "Log remote error %s: %s",
                    req.status_code,
                    req.text[:200],
                )
                self._remote_breaker.record_failure()
        except requests.RequestException as exc:
            logging.error(f"Log remote failed: {exc}")
            self._remote_breaker.record_failure()

    def _get_mqtt_client(self):
        """Get or create a persistent MQTT client connection."""
        if self._mqtt_client is None:
            user = self.config['mqtt']['user']
            password = self.config['mqtt']['password']
            
            self._mqtt_client = mqtt.Client(client_id="renogy-bt", clean_session=False)
            if user and password:
                self._mqtt_client.username_pw_set(user, password)
            
            # Set callbacks for connection management
            def on_connect(client, userdata, flags, rc):
                if rc == 0:
                    self._mqtt_connected = True
                    logging.debug("MQTT connected successfully")
                else:
                    self._mqtt_connected = False
                    logging.error("MQTT connection failed with code %s", rc)
            
            def on_disconnect(client, userdata, rc):
                self._mqtt_connected = False
                if rc != 0:
                    logging.warning("MQTT disconnected unexpectedly")
            
            self._mqtt_client.on_connect = on_connect
            self._mqtt_client.on_disconnect = on_disconnect
            
            try:
                self._mqtt_client.connect(
                    self.config['mqtt']['server'],
                    self.config['mqtt'].getint('port'),
                    60  # keepalive
                )
                self._mqtt_client.loop_start()
            except Exception as exc:
                logging.error("Failed to connect MQTT client: %s", exc)
                self._mqtt_client = None
                return None
        
        return self._mqtt_client

    def log_mqtt(self, json_data):
        if not self._mqtt_breaker.can_attempt():
            logging.debug("MQTT circuit breaker is open, skipping")
            return
        
        logging.debug("mqtt logging")
        client = self._get_mqtt_client()
        if client is None:
            logging.error("MQTT client not available")
            self._mqtt_breaker.record_failure()
            return

        alias = self.config['device']['alias']
        device_id = json_data.get('device_id')
        alias_id = f"{alias}_{device_id}" if device_id is not None else alias

        topic_base = self.config['mqtt']['topic']
        topic = f"{topic_base.rstrip('/')}/{alias_id}"

        # Rate limiting: skip if we published to this topic too recently
        now = time.time()
        last_publish = self._last_mqtt_publish.get(topic, 0)
        if now - last_publish < self._mqtt_min_interval:
            logging.debug("Rate limiting: skipping MQTT publish to %s (too soon)", topic)
            return
        
        try:
            # Reconnect only if actually disconnected (check is_connected to avoid race condition)
            if not self._mqtt_connected and not client.is_connected():
                try:
                    client.reconnect()
                    # Give it a moment to reconnect
                    time.sleep(0.5)
                except ValueError:
                    # Already connected - race condition where callback set flag after our check
                    logging.debug("MQTT reconnect skipped - already connected")
            
            result = client.publish(topic, json.dumps(json_data), qos=0)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logging.error("mqtt publish failed with code %s", result.rc)
                self._mqtt_breaker.record_failure()
            else:
                self._last_mqtt_publish[topic] = now
                self._mqtt_breaker.record_success()
        except Exception as exc:
            logging.error("mqtt publish failed: %s", exc)
            self._mqtt_breaker.record_failure()
            # Reset client on error to force reconnect
            self._cleanup_mqtt()
            return

        if self.config['mqtt'].getboolean('homeassistant_discovery', fallback=False):
            self.publish_home_assistant_config(client, json_data, alias_id, topic)

    def publish_home_assistant_config(self, client, json_data, alias_id, state_topic):
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
                result = client.publish(config_topic, json.dumps(payload), qos=0, retain=True)
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    logging.error("Home Assistant discovery publish failed with code %s", result.rc)
                    return
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
        if not self._pvoutput_breaker.can_attempt():
            logging.debug("PVOutput circuit breaker is open, skipping")
            return
        
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
            session = self._get_http_session()
            response = session.post(
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
            self._pvoutput_breaker.record_success()
        except requests.RequestException as exc:
            logging.error("pvoutput logging failed: %s", exc)
            self._pvoutput_breaker.record_failure()

    def _cleanup_mqtt(self):
        """Clean up MQTT client connection."""
        if self._mqtt_client:
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()
            except Exception:
                pass
            self._mqtt_client = None
            self._mqtt_connected = False

    def cleanup(self):
        """Clean up all resources."""
        self._cleanup_mqtt()
        if self._http_session:
            try:
                self._http_session.close()
            except Exception:
                pass
            self._http_session = None
