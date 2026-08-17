from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .utils import log, set_state, state

from utils import set_state


class InverterMode:
    charger_only = "1"
    inverter_only = "2"
    on = "3"
    off = "4"


class Victron:
    """Victron specific constants and states"""

    device_id = "c0619ab445e5"
    inverter_mode_sensor = "sensor.victron_inverter_mode_set"
    """Sensor for inverter mode"""
    inverter_mode_input_select = "input_select.victron_inverter_mode"
    """Input select for inverter mode"""
    auto_set_inverter_mode = "input_boolean.auto_victron_inverter_mode"
    """Input boolean to enable/disable automatic inverter mode"""
    inverter_mode_mqtt_write_topic = f"victron/W/{device_id}/vebus/275/Mode"
    setpoint_topic = f"victron/W/{device_id}/settings/0/Settings/CGwacs/AcPowerSetPoint"

    inverter_efficiency = "sensor.victron_inverter_efficiency"
    inverter_power = "sensor.victron_inverter_power"

    MODE_TO_PAYLOAD = {"Charger only": "1", "Inverter only": "2", "On": "3", "Off": "4"}
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
        set_state(Victron.inverter_mode_input_select, new_mode)


@pyscript_compile
def get_auto_inverter_mode(
    ev_is_charging,
    surplus_energy,
    battery_headroom_energy,
    pv_power,
    daily_avg_power,
    battery_soc,
    target_soc,
    electricity_price,
    min_discharge_price,
    max_charge_price,
    charge_limit_percent,
    force_charge_switch,
    t_now,
    enforce_battery_floor=False,
):
    min_charge_power = 6 * 230  # 6A amps minimum

    reason = "Default mode"
    new_mode = InverterMode.on
    new_charge_limit = None  # in W
    new_force_charge_switch_state = None

    headroom_threshold = 0.5  # kWh hysteresis to avoid toggling at the battery floor

    # When ev is charging, disable inverter if no surplus energy or insufficient pv power
    if ev_is_charging:
        # EV charging should use the stricter, reserve-aware surplus budget. Physical
        # battery headroom is still reserved for house backup/normal operation here.
        if surplus_energy > 0 or pv_power > (min_charge_power + daily_avg_power) and battery_soc > target_soc:
            reason = f"EV is charging with surplus energy of {surplus_energy} or pv_power > (min_charge_power + daily_avg_power)  {pv_power} > ({min_charge_power} + {daily_avg_power})"
            new_mode = InverterMode.on
        else:
            reason = f"EV is charging {'without' if pv_power < 10 else 'with'} PV power"
            new_mode = InverterMode.off if pv_power < 10 else InverterMode.charger_only
    else:
        # For normal self-consumption, use physical battery headroom instead of the
        # reserve-aware surplus budget. A zero optional-load surplus does not mean the
        # battery cannot still cover house load without hitting the hard floor.
        if electricity_price < min_discharge_price and battery_headroom_energy <= headroom_threshold:
            if pv_power == 0:
                reason = f"no PV, battery headroom {battery_headroom_energy:.2f} kWh <= {headroom_threshold:.2f} kWh and price is low"
                new_mode = InverterMode.off
            else:
                reason = f"battery {battery_soc}% < target {target_soc}% and price is low"
                new_mode = InverterMode.charger_only

    if enforce_battery_floor and battery_headroom_energy <= headroom_threshold:
        if pv_power < 0.5 * daily_avg_power:
            reason = (
                f"planned battery floor reached with {battery_headroom_energy:.2f} kWh headroom "
                "and insufficient PV"
            )
            new_mode = InverterMode.off
        else:
            reason = (
                f"planned battery floor reached with {battery_headroom_energy:.2f} kWh headroom "
                "while PV is available"
            )
            new_mode = InverterMode.charger_only

    if electricity_price < max_charge_price and battery_soc < target_soc and battery_soc < charge_limit_percent:
        reason = "Setting Victron inverter mode to 'On' due to low price"
        new_mode = InverterMode.on
        # turn on switch.victron_victron_force_charge
        reason = f"Enabling force charge switch and setting charge limit, {battery_soc} < {target_soc}, < {charge_limit_percent}"
        new_force_charge_switch_state = True
        new_charge_limit = 3000
    else:
        if force_charge_switch:
            reason = "Disabling force charge switch and resetting charge limit"
            new_charge_limit = -1
            new_force_charge_switch_state = False
        elif (
            pv_power < 0.5 * daily_avg_power
            and battery_headroom_energy <= headroom_threshold
            and battery_soc < min(25, target_soc)
        ):
            reason = f"insufficient pv power for charging, battery headroom {battery_headroom_energy:.2f} kWh <= {headroom_threshold:.2f} kWh, battery soc below target of {target_soc}%"
            new_mode = InverterMode.off
            new_charge_limit = -1

    return new_mode, new_charge_limit, new_force_charge_switch_state, reason
