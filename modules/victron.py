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
    is_low_price=False,
):
    min_charge_power = 6 * 230  # 6A amps minimum

    reason = "Default mode"
    new_mode = InverterMode.on
    new_charge_limit = None  # in W
    new_force_charge_switch_state = None

    headroom_threshold = 0.1  # kWh anti-flap epsilon; the battery may drain to ~empty, the BMS guards the cells

    # LFP state of charge is only ever an estimate, here a pessimistic one.
    # At high prices discharge must never be software-blocked on that guess:
    # importing at the day rate while usable energy sits in the battery is
    # always wrong, and only the BMS low-voltage cutoff can tell true empty.
    # At low prices, holding back an apparently empty battery is correct.
    allow_discharge_block = bool(is_low_price)

    # When ev is charging, disable inverter if no surplus energy or insufficient pv power
    if ev_is_charging:
        # The unified forecast already supplies a tariff-aware physical battery
        # floor.  While there is headroom above it, that floor is authoritative;
        # the broader optional-load surplus budget must not strand battery energy
        # during a required EV charge.  Exception: at low prices with no
        # post-obligation surplus, the grid must fund the required charge and
        # the battery must hold its floor for the house; importing cheap power
        # while draining storage into the EV only shifts load into the
        # expensive morning (2026-10-21 incident: 4.9 kWh drained at 0.1775
        # with zero EV-adjusted surplus).
        if (
            enforce_battery_floor
            and battery_headroom_energy > headroom_threshold
            and (surplus_energy > 0 or not allow_discharge_block)
        ):
            reason = (
                f"EV is charging with {battery_headroom_energy:.2f} kWh "
                "above the planned battery floor"
            )
            new_mode = InverterMode.on
        elif surplus_energy > 0 or pv_power > (min_charge_power + daily_avg_power) and battery_soc > target_soc:
            reason = f"EV is charging with surplus energy of {surplus_energy} or pv_power > (min_charge_power + daily_avg_power)  {pv_power} > ({min_charge_power} + {daily_avg_power})"
            new_mode = InverterMode.on
        elif allow_discharge_block:
            reason = f"EV is charging {'without' if pv_power < 10 else 'with'} PV power"
            new_mode = InverterMode.off if pv_power < 10 else InverterMode.charger_only
        else:
            reason = "EV is charging at high price: BMS decides when the battery is empty"
            new_mode = InverterMode.on
    else:
        # For normal self-consumption, use physical battery headroom instead of the
        # reserve-aware surplus budget. A zero optional-load surplus does not mean the
        # battery cannot still cover house load without hitting the hard floor.
        if is_low_price and battery_headroom_energy <= headroom_threshold:
            if pv_power == 0:
                reason = f"no PV, battery headroom {battery_headroom_energy:.2f} kWh <= {headroom_threshold:.2f} kWh and price is low"
                new_mode = InverterMode.off
            else:
                reason = f"battery {battery_soc}% < target {target_soc}% and price is low"
                new_mode = InverterMode.charger_only

    if allow_discharge_block and enforce_battery_floor and battery_headroom_energy <= headroom_threshold:
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

    # The grid-charge target is independent of the self-consumption reserve.
    # During the overnight cheap-price window this is the energy required at
    # its end to cover the following expensive region.  Let the battery serve
    # cheap-period load while it is above that reserve, then turn the inverter
    # off at the reserve so it cannot be drained below the forecast need and
    # charged back again. Daytime low-price periods retain normal inverter
    # behavior so PV can serve loads and charge the battery. The start
    # tolerance only covers the 0.1% SOC reporting resolution.
    is_night = t_now.hour < 7 or t_now.hour >= 19
    night_battery_hold_required = (
        0 < charge_limit_percent <= 100
        and battery_soc <= charge_limit_percent
    )
    if (electricity_price < max_charge_price
            and 0 < charge_limit_percent <= 100
            and battery_soc < charge_limit_percent - (0 if force_charge_switch else 0.1)):
        reason = "Setting Victron inverter mode to 'On' due to low price"
        new_mode = InverterMode.on
        # turn on switch.victron_victron_force_charge
        reason = f"Grid charging: SOC {battery_soc:.1f}% < charge target {charge_limit_percent:.1f}%, price {electricity_price:.4f} < {max_charge_price:.4f}"
        new_force_charge_switch_state = True
        new_charge_limit = 3000
    else:
        if force_charge_switch:
            reason = "Disabling force charge switch and resetting charge limit"
            new_charge_limit = -1
            new_force_charge_switch_state = False
        if is_low_price and is_night and night_battery_hold_required and not ev_is_charging:
            reason = (
                f"Low-price battery hold: SOC {battery_soc:.1f}% has reached "
                f"the {charge_limit_percent:.1f}% reserve for the following "
                "high-price period"
            )
            new_mode = InverterMode.off
            new_charge_limit = -1
        elif (
            allow_discharge_block
            and pv_power < 0.5 * daily_avg_power
            and battery_headroom_energy <= headroom_threshold
            and battery_soc < min(25, target_soc)
        ):
            reason = f"insufficient pv power for charging, battery headroom {battery_headroom_energy:.2f} kWh <= {headroom_threshold:.2f} kWh, battery soc below target of {target_soc}%"
            new_mode = InverterMode.off
            new_charge_limit = -1

    return new_mode, new_charge_limit, new_force_charge_switch_state, reason
