"""Sensor entity definitions for Renogy devices via ESPHome API."""

from __future__ import annotations

from typing import Dict

from aioesphomeapi.api_pb2 import SensorStateClass


def _guess_sensor_attributes(key: str, temp_unit: str = 'C') -> Dict[str, object]:
    """Guess sensor attributes (unit, device_class, etc.) based on the key name."""
    lkey = key.lower()
    attrs = {
        'unit_of_measurement': '',
        'device_class': '',
        'state_class': SensorStateClass.STATE_CLASS_MEASUREMENT,
        'accuracy_decimals': 2,
        'icon': '',
    }
    
    # Temperature sensors
    if 'temperature' in lkey:
        attrs['unit_of_measurement'] = '°F' if temp_unit == 'F' else '°C'
        attrs['device_class'] = 'temperature'
        attrs['accuracy_decimals'] = 1
        attrs['icon'] = 'mdi:thermometer'
    
    # Voltage sensors
    elif lkey.endswith('voltage') or '_voltage' in lkey:
        attrs['unit_of_measurement'] = 'V'
        attrs['device_class'] = 'voltage'
        attrs['accuracy_decimals'] = 1
        attrs['icon'] = 'mdi:flash'
    
    # Current sensors
    elif lkey.endswith('current') or '_current' in lkey:
        attrs['unit_of_measurement'] = 'A'
        attrs['device_class'] = 'current'
        attrs['accuracy_decimals'] = 2
        attrs['icon'] = 'mdi:current-dc'
    
    # Power sensors
    elif lkey.endswith('power') or '_power' in lkey:
        attrs['unit_of_measurement'] = 'W'
        attrs['device_class'] = 'power'
        attrs['accuracy_decimals'] = 0
        attrs['icon'] = 'mdi:lightning-bolt'
    
    # Battery percentage / SOC
    elif lkey.endswith('percentage') or 'soc' in lkey or (lkey.endswith('level') and 'battery' in lkey):
        attrs['unit_of_measurement'] = '%'
        attrs['device_class'] = 'battery'
        attrs['accuracy_decimals'] = 0
        attrs['icon'] = 'mdi:battery'
    
    # Amp hours
    elif 'amp_hour' in lkey or lkey.endswith('_ah'):
        attrs['unit_of_measurement'] = 'Ah'
        attrs['accuracy_decimals'] = 1
        attrs['icon'] = 'mdi:battery-charging'
    
    # Energy sensors
    elif 'energy' in lkey:
        if 'kwh' in lkey:
            attrs['unit_of_measurement'] = 'kWh'
        else:
            attrs['unit_of_measurement'] = 'Wh'
        attrs['device_class'] = 'energy'
        attrs['state_class'] = SensorStateClass.STATE_CLASS_TOTAL_INCREASING
        attrs['accuracy_decimals'] = 2
        attrs['icon'] = 'mdi:lightning-bolt-circle'
    
    # Frequency
    elif lkey.endswith('frequency'):
        attrs['unit_of_measurement'] = 'Hz'
        attrs['device_class'] = 'frequency'
        attrs['accuracy_decimals'] = 2
        attrs['icon'] = 'mdi:sine-wave'
    
    # Capacity (for batteries)
    elif 'capacity' in lkey or lkey.endswith('charge') or lkey.endswith('_charge'):
        attrs['unit_of_measurement'] = 'Ah'
        attrs['accuracy_decimals'] = 2
        attrs['icon'] = 'mdi:battery-high'
    
    return attrs


def create_sensor_entities_from_data(
    data: Dict[str, object],
    alias: str,
    temp_unit: str = 'C',
    base_key: int = 1000,
) -> Dict[str, Dict]:
    """Create sensor entity definitions from a Renogy data dictionary.
    
    Args:
        data: Dictionary of Renogy device data (e.g., from battery, controller)
        alias: Device alias for entity naming
        temp_unit: Temperature unit ('C' or 'F')
        base_key: Starting key number for sensor entities
        
    Returns:
        Dictionary mapping data keys to entity definitions
    """
    entities = {}
    key_counter = base_key
    
    for data_key, value in data.items():
        # Skip non-numeric fields and internal fields
        if not isinstance(value, (int, float)):
            continue
        if data_key.startswith('_'):
            continue
        if data_key in ['function', 'device_id']:
            continue
        
        # Get sensor attributes
        attrs = _guess_sensor_attributes(data_key, temp_unit)
        
        # Create entity definition
        entity_key = f"{alias}_{data_key}"
        entities[entity_key] = {
            'key': key_counter,
            'data_key': data_key,  # Original key in the data dictionary
            'object_id': entity_key,
            'name': f"{alias} {data_key.replace('_', ' ').title()}",
            'icon': attrs['icon'],
            'unit_of_measurement': attrs['unit_of_measurement'],
            'accuracy_decimals': attrs['accuracy_decimals'],
            'device_class': attrs['device_class'],
            'state_class': attrs['state_class'],
            'force_update': False,
            'disabled_by_default': False,
        }
        key_counter += 1
    
    return entities


def update_sensor_entities(
    existing_entities: Dict[str, Dict],
    new_data: Dict[str, object],
    alias: str,
    temp_unit: str = 'C',
) -> Dict[str, Dict]:
    """Update or add sensor entities based on new data.
    
    This allows dynamic sensor creation when new data fields appear.
    
    Args:
        existing_entities: Current entity definitions
        new_data: New Renogy device data
        alias: Device alias for entity naming
        temp_unit: Temperature unit
        
    Returns:
        Updated entity dictionary
    """
    # Find the highest existing key
    max_key = max((e['key'] for e in existing_entities.values()), default=999)
    
    # Create entities for any new data keys
    new_entities = create_sensor_entities_from_data(
        new_data,
        alias,
        temp_unit,
        base_key=max_key + 1,
    )
    
    # Merge with existing, keeping existing entity keys
    for entity_key, entity_def in new_entities.items():
        if entity_key not in existing_entities:
            existing_entities[entity_key] = entity_def
    
    return existing_entities


__all__ = [
    'create_sensor_entities_from_data',
    'update_sensor_entities',
]
