from enum import Enum

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .utils import log, service, state

from utils import set


class Victron:
    """Victron specific constants and states"""

    device_id = "c0619ab445e5"
    inverter_mode_sensor = "sensor.victron_inverter_mode_set"
    inverter_mode_input_select = "input_select.victron_inverter_mode"
    inverter_mode_mqtt_write_topic = f"victron/W/{device_id}/vebus/275/Mode"

    mode_input_select = "input_select.victron_inverter_mode"
    """Input select for inverter mode"""
    mode_sensor = "sensor.victron_inverter_mode_set"
    """Sensor for inverter mode"""
    mode = "input_select.victron_inverter_mode"  # duplicate of mode_input_select?

    inverter_efficiency = "sensor.victron_inverter_efficiency"
    inverter_power = "sensor.victron_inverter_power"

    class InverterMode:
        charger_only = '1'
        inverter_only = '2'
        on = '3'
        off = '4'

    MODE_TO_PAYLOAD = {'Charger only': '1', 'Inverter only': '2', 'On': '3', 'Off': '4'}
    # {'1': 'Charger only', '2': 'Inverter only', '3': 'On', '4': 'Off'}
    PAYLOAD_TO_MODE = {v: k for k, v in MODE_TO_PAYLOAD.items()}

    def get_inverter_mode():
        """Get the mode for the given payload, e.g. "2" -> "Inverter only"."""
        return state.get(Victron.inverter_mode_sensor)

    def set_inverter_mode(mode_or_payload):
        if mode_or_payload in Victron.MODE_TO_PAYLOAD:
            new_mode = mode_or_payload
        else:
            new_mode = Victron.PAYLOAD_TO_MODE.get(mode_or_payload)

        if not new_mode:
            raise ValueError(f"Invalid mode: {mode_or_payload}")
        log.warning(f"Inverter mode changed to {new_mode}")
        set(Victron.inverter_mode_input_select, new_mode)
