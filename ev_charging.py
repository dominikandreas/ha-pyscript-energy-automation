# ruff: noqa: I001

from datetime import timedelta
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    # The type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    # Therefore during type checking we pretend to import them from modules.utils, which it can resolve.
    from modules.utils import get, get_attr
    from modules.const import EV as Const
    from modules.energy_core import HYSTERESIS_BUFFER

    # These are provided by typescript and do not need to be imported in the actual script
    # They are only needed for type checking (linting), which development easier
    from modules.utils import (
        log,
        now,
        time_trigger,
        with_timezone,
        state_active,
        state_trigger,
        service,
        task,
        set_state,
    )
    from modules.energy_core import _get_ev_smart_charge_limit, _get_ev_energy_needed, _get_charge_action  # noqa: F401

    from modules.states import Automation, Charger, ElectricityPrices, EV, Excess, Battery, House, PVProduction

else:
    from const import EV as Const
    from utils import get, set_state, get_attr, now, with_timezone
    from states import Automation, Charger, ElectricityPrices, EV, Excess, Battery, House, PVProduction
    from energy_core import _get_ev_smart_charge_limit, _get_ev_energy_needed, _get_charge_action, HYSTERESIS_BUFFER  # noqa: F401


@state_trigger(f"{EV.planned_drives}")
@time_trigger
@time_trigger("period(now, 30sec)")
def smart_charge_limit():
    """The smart charge limit is the maximum state of charge the EV should be charged to
    to ensure the battery is not fully charged when the car is not used for a longer
    period of time.

    The limit is calculated based on the time until the next drive.
    """
    schedule = get_attr(EV.planned_drives, "next_event")
    active_schedule = get(EV.planned_drives, False)

    smart_charge_limit = _get_ev_smart_charge_limit(schedule, now(), active_schedule=active_schedule)

    set_state(EV.smart_charge_limit, smart_charge_limit)


def get_ev_requested_energy_today():
    t_now = now()

    ev_short_term_demand = get(EV.short_term_demand, default=5)
    next_drive = get_attr(EV.planned_drives, "next_event")
    drive_ongoing = get(EV.planned_drives, False)

    required_energy_total = get(EV.energy_needed, default=0)

    if drive_ongoing:
        # If the car is already in use, we don't need to charge it
        return 0

    if next_drive is not None:
        next_drive = with_timezone(next_drive)

        leaving_soon = (next_drive - t_now) < timedelta(hours=8)

        if leaving_soon:
            return required_energy_total

        return max(0, ev_short_term_demand)

    return required_energy_total * 1 / Const.ev_days_allowed_to_reach_target


last_ev_charging_phase_change = now() - timedelta(minutes=15)
ev_charging_turned_on_by_automation = get(Charger.turned_on_by_automation, False)


@time_trigger
@time_trigger("period(now, 15sec)")
def ev_energy():
    """Calculate the energy needed to charge the EV to the required state of charge"""
    current_soc = get(EV.soc, 0)
    ev_energy = (current_soc) / 100 * Const.ev_capacity
    set_state(EV.energy, ev_energy)


@state_trigger(f"{Charger.force_charge}.lower() == 'on'")
def enable_force_charge():
    set_state(Charger.control_switch, True)
    set_phases_and_current(3, 16, "Force charge enabled, setting max power")


def turn_on_charger(reason: str = ""):
    global last_ev_charging_phase_change
    charger_enabled = get(Charger.control_switch, False)
    if not charger_enabled:
        log.warning(f"Turning on ev charger {reason}")
        service.call("switch", "turn_on", entity_id=Charger.control_switch)
        task.sleep(5)
        new_state = get(Charger.control_switch, False)

        return new_state


def turn_off_charger(reason: str = "", check_phase_change_cooldown=True):
    global last_ev_charging_phase_change

    is_charging = get(Charger.control_switch, False)

    if get(Charger.force_charge, False):
        log.warning(f"Not turning off charging, force charge in on. Reason for request {reason}")
    elif is_charging:
        if check_phase_change_cooldown and last_ev_charging_phase_change > now() - timedelta(minutes=15):
            log.warning(f"Phase change too frequent - cooldown active. Reason: {reason or 'no reason provided'}")
            return

        log.warning(f"Turning off ev charger. Reason for request: {reason}")
        service.call("switch", "turn_off", entity_id=Charger.control_switch)
        task.sleep(5)
        new_state = get(Charger.control_switch, False)
        if new_state is False:
            last_ev_charging_phase_change = now()  # Update phase change timestamp

        return new_state

    return get(Charger.control_switch, False)


def set_current(current, reason: str | None = None):
    configured_current = get(Charger.current_setting, -1)
    if configured_current != current:
        if not Const.min_current <= current <= Const.max_current:
            log.warning(
                f"Current out of bounds {current} - skipping phase change. Reason: {reason or 'no reason provided'}"
            )
            return
        service.call("number", "set_value", entity_id=Charger.current_setting, value=current)
        log.warning(
            f"Setting current from {configured_current:.0f}A to {current}A. Reason: {reason or 'no reason provided'}"
        )
    else:
        log.warning(f"Current of {current} already set - skipping current change")


def set_phases_and_current(phases, current, reason: str | None = None):
    global last_ev_charging_phase_change

    charger_enabled = get(Charger.control_switch, False)

    set_current(current, reason)

    configured_phases = get(Charger.phases, 3)

    if configured_phases != phases:
        if last_ev_charging_phase_change > now() - timedelta(minutes=15):
            log.warning(f"Phase change too frequent - cooldown active. Reason: {reason or 'no reason provided'}")
            return

        log.warning(f"Setting phases: {phases}, current: {current}A. Reason: {reason or 'no reason provided'}")

        if charger_enabled:
            turn_off_charger(f"Phase change from {configured_phases} -> {phases}", check_phase_change_cooldown=False)

        service.call("vestel_ecv04", "set_phases_and_current", current=Const.max_current, num_phases=phases)
        last_ev_charging_phase_change = now()  # Update phase change timestamp
        log.warning(f"Phase change initiated - waiting {Const.ev_phase_switch_delay} seconds")
        task.sleep(Const.ev_phase_switch_delay)
        log.warning(f"Phase change completed. Phase is now set to: {get(Charger.phases) or 'unknown'}")

        if charger_enabled:
            turn_on_charger()
    else:
        log.warning(f"Phases of {phases} already set - skipping phase change")


@time_trigger
@time_trigger("period(now, 30sec)")
def ev_energy_needed():
    """Calculate the energy needed to charge the EV to the required state of charge"""
    required_soc = get(EV.required_soc, 80)
    current_soc = get(EV.soc, 0)
    smart_charge_limit = get(EV.smart_charge_limit, required_soc)
    smart_limiter_active = get(Automation.auto_charge_limit, False)

    energy_needed = _get_ev_energy_needed(required_soc, current_soc, smart_charge_limit, smart_limiter_active)
    set_state(EV.energy_needed, energy_needed)


@time_trigger("period(now, 15sec)")
@state_active(
    f"{Charger.force_charge} == 'off' and {Automation.auto_ev_charging} == 'on' and ({Charger.ready} == 'on' or {Charger.control_switch} == 'on')"
)
async def auto_ev_charging():
    """Combined EV charging control with excess power, price and temperature awareness"""
    global last_ev_charging_phase_change

    # ensure only one instance of this task is running (phase switching can take a while)
    task.unique("control_ev_charging", kill_me=True)

    # Configuration parameters from Const class
    Const.voltage = 230  # Volts (regional standard)

    # the current state of charge of the EV
    current_soc = get(EV.soc, -1)
    if current_soc < 0:
        log.warning("EV SOC is not set, cannot proceed with charging control.")
        return
    # required state of charge defined by the owner
    required_soc = get(EV.required_soc, 80)
    # the current excess power available, this is defined as power going into the battery or into the grid (or the opposite, depending on the sign)
    excess_power = get(Excess.power, 1337)  # in kW
    if excess_power == 1337:
        log.warning("Excess power is not set, cannot proceed with charging control.")
        return

    battery_soc = get(Battery.soc, 0)

    pv_power_estimated = get(PVProduction.power_now_estimated, 0)  # in W

    # target excess is the amount of power requested by the home battery to be able to cover the house loads in the near future
    # it is dynamically updated by a separate automation
    target_excess = get(Excess.target, 0) / 1000  # in kW
    # surplus energy is the amount of energy that is likely available after accounting for house loads in the near future
    surplus_energy = get(House.energy_surplus, 0)  # in kWh

    # this is the maximum charge current that the vehicle should be charge with right now
    configured_current = get(Charger.current_setting, 16)
    configured_phases = get(Charger.phases, 3)

    smart_limiter_active = get(Automation.auto_charge_limit, False)
    ev_charge_limit = get(EV.smart_charge_limit, 80)

    energy_needed = get(EV.energy_needed, 0)  # in kWh

    # next drive is the point in time where the user needs to have the car charged to the required soc
    ongoing = get(EV.planned_drives, False)
    next_drive = get_attr(EV.planned_drives, "next_event")
    is_charging = get(Charger.control_switch, False)

    if ongoing:
        next_drive = None  # if ongoing, next_drive is actually next_return, so we ignore it

    elif next_drive:
        next_drive = with_timezone(next_drive)

    # Calculate minimum time needed to charge the vehicle, we subtract 1 to account for charging inefficiencies
    min_hours_needed = energy_needed / (3 * Const.voltage * (Const.max_current - 1) / 1000)  # in hours

    # these are binary sensors defined separately that indicate whether the price is relatively low or high
    low_price = get(ElectricityPrices.low_price, False)
    high_price = get(ElectricityPrices.high_price, False)
    t_now = now()

    log.warning(
        f"Current SOC: {current_soc}%, Required SOC: {required_soc}%, Surplus {surplus_energy:.2f}, "
        f"Excess: {excess_power:.2f} kW, Target: {target_excess:.2f} kW "
        f"Energy needed: {energy_needed:.2f} kWh, Time needed: {min_hours_needed:.2f}h, "
        f"low price: {low_price}, high price: {high_price}, next drive: {next_drive}, "
        f"EV charge limit: {ev_charge_limit:.0f}%"
    )

    #  -------------------------- Charging Strategy Logic -----------------------------------
    #  - when there's time to charge is running out to reach target SOC, charge with max current
    #  - when price is low and less than 24h left, charge with max current
    #  - when there's excess power and no time constraints, charge with excess power
    #  - when charging already active, control the charge amps to meet target excess
    #  - when price is high and time is not constrained, turn off the charger
    #  -------------------------------------------------------------------------------------
    action, phases, current, reason = _get_charge_action(
        next_drive=next_drive,
        current_soc=current_soc,
        required_soc=required_soc,
        energy_needed=energy_needed,
        excess_power=excess_power,
        target_excess=target_excess,
        surplus_energy=surplus_energy,
        smart_charge_limit=ev_charge_limit,
        smart_limiter_active=smart_limiter_active,
        configured_phases=configured_phases,
        configured_current=configured_current,
        is_low_price=low_price,
        pv_total_power=pv_power_estimated,
        battery_soc=battery_soc,
        hysteresis=HYSTERESIS_BUFFER,
        is_charging=is_charging,
        t_now=t_now,
    )

    hours_available_to_charge = ((next_drive - t_now).total_seconds() / 3600) if next_drive else 999

    log.warning(f"""excess_power > target_excess: {excess_power:.2f} > {target_excess:.2f}: {excess_power > target_excess}
        and surplus_energy {surplus_energy:.2f} > 5 or excess_power {excess_power:.2f} > 2 and battery_soc {battery_soc} > 90: {(surplus_energy > 5 or excess_power > 2 and battery_soc > 90)}
        and ( 
            {pv_power_estimated} > 1000 and 10 <= t_now.hour {t_now.hour} < 17: {pv_power_estimated > 1000 and 10 <= t_now.hour < 17}
            or hours_available_to_charge {hours_available_to_charge:.2f} < 24 and current_soc {current_soc} <= required_soc {required_soc}:
        )""")
    log.warning(f"Got charge action: {action} phases {phases} current {current}: {reason}")

    if action == "on":
        set_phases_and_current(phases, current, reason)
        turn_on_charger(reason)
    elif action == "off":
        turn_off_charger(reason)
    else:
        log.warning(f"Skipping unknown action: {action}")
