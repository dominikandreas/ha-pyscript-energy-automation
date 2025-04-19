# ruff: noqa: I001

from datetime import timedelta
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    # The type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    # Therefore during type checking we pretend to import them from modules.utils, which it can resolve.
    from modules.utils import clip, get, get_attr
    from modules.const import EV as Const

    # These are provided by typescript and do not need to be imported in the actual script
    # They are only needed for type checking (linting), which development easier
    from modules.utils import log, now, time_trigger, with_timezone, state_active, state_trigger, service, task, set_state

    from modules.states import Automation, Charger, ElectricityPrices, EV, Excess, Battery, House, PVProduction
    from modules.victron import Victron

else:
    from const import EV as Const
    from utils import clip, get, set_state, get_attr, now, with_timezone
    from states import Automation, Charger, ElectricityPrices, EV, Excess, Battery, House, PVProduction
    from victron import Victron


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
    is_on = get(EV.planned_drives, False)

    t_now = now()

    if not schedule:
        smart_charge_limit = 85
    else:
        td = schedule - t_now
        td_hours = td.total_seconds() // 3600

        if (
            td_hours < 6 or is_on
        ):  # `or is_on` ensures that the car can continue to be charged when it was scheduled to leave but hasn't done so yet
            smart_charge_limit = 100
        elif td_hours < 20:
            smart_charge_limit = 98
        elif td_hours < 40:
            smart_charge_limit = 95
        elif td_hours < 60:
            smart_charge_limit = 93
        else:
            smart_charge_limit = 85

    set_state(EV.smart_charge_limit, smart_charge_limit)


def get_ev_requested_energy_today():
    t_now = now()

    ev_soc = get(EV.soc, default=50)
    ev_short_term_demand = get(EV.short_term_demand, default=5)
    next_drive = get_attr(EV.planned_drives, "next_event")
    required_soc = get(EV.required_soc, default=50)

    required_energy_total = max(0, (required_soc - ev_soc) / 100 * Const.ev_capacity)

    if next_drive is not None:
        next_drive = with_timezone(next_drive)

        leaving_soon = (next_drive - t_now) < timedelta(hours=8)

        if leaving_soon:
            return required_energy_total

        return max(0, ev_short_term_demand)

    return required_energy_total * 1 / Const.ev_days_allowed_to_reach_target


def calculate_charger_current_adjustment(
    current_excess: float, target_excess: float, configured_phases: int, configured_current: float
) -> int:
    """Calculate appropriate current adjustment based on excess power delta"""
    diff = current_excess - target_excess
    power_change_per_current_step = 230 * max(6, configured_phases) / 1000  # kW
    upper_limit = min(4, 16 - configured_current)
    lower_limit = max(-4, 6 - configured_current)

    # Calculate raw adjustment and apply limits
    raw_adj = diff / power_change_per_current_step
    adj = clip(round(raw_adj), lower_limit, upper_limit)

    log.warning(
        f"Calculating charger current adjustment: {diff:.2f} kW delta, {configured_current}A current, {configured_phases} phases, "
        f"{power_change_per_current_step:.3f} kW/step, limits[{lower_limit}, {upper_limit}]. Raw: {raw_adj:.1f} → Adj: {adj}"
    )
    return adj


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
    charger_enabled = get(Charger.control_switch, False)
    if not charger_enabled:
        log.warning(f"Turning on ev charger {reason}")
        service.call("switch", "turn_on", entity_id=Charger.control_switch)
        task.sleep(5)
        new_state = get(Charger.control_switch, False)
        return new_state


def turn_off_charger(reason: str = ""):
    is_charging = get(Charger.control_switch, False)

    if get(Charger.force_charge, False):
        log.warning(f"Not turning off charging, force charge in on. Reason for request {reason}")
    elif is_charging:
        log.warning(f"Turning off ev charger. Reason for request: {reason}")
        service.call("switch", "turn_off", entity_id=Charger.control_switch)
        task.sleep(5)
        new_state = get(Charger.control_switch, False)
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
            turn_off_charger(f"Phase change from {configured_phases} -> {phases}")

        service.call("vestel_ecv04", "set_phases_and_current", current=Const.max_current, num_phases=phases)
        last_ev_charging_phase_change = now()  # Update phase change timestamp
        log.warning(f"Phase change initiated - waiting {Const.ev_phase_switch_delay} seconds")
        task.sleep(Const.ev_phase_switch_delay)
        log.warning(f"Phase change completed. Phase is now set to: {get(Charger.phases) or 'unknown'}")

        if charger_enabled:
            turn_on_charger()
    else:
        log.warning(f"Phases of {phases} already set - skipping phase change")


# set(Charger.force_charge, "off")
# _force_charge = get(Charger.force_charge)
# _charger_ready = get(Charger.ready)
# _auto_ev_charging = get(Automation.auto_ev_charging)
# _control_switch = get(Charger.control_switch)
# log.warning(
#     f"Condition: force_charge {_force_charge}=='off' {_force_charge == 'off'} "
#     f" and auto_ev_charging {_auto_ev_charging}==on: {_auto_ev_charging == 'on'} "
#     f"and (Charger.ready == 'on' {_charger_ready == 'on'} or Charger.control_switch == 'on' {_control_switch == 'on'})"
# )
# condition = _force_charge == "off" and _auto_ev_charging == "on" and (_charger_ready == "on" or _control_switch == "on")
# log.warning(f"Condition: {condition}")


@time_trigger("period(now, 15sec)")
@state_active(
    f"{Charger.force_charge} == 'off' and {Automation.auto_ev_charging} == 'on' and ({Charger.ready} == 'on' or {Charger.control_switch} == 'on')"
)
async def auto_ev_charging():
    """Combined EV charging control with excess power, price and temperature awareness"""
    global last_ev_charging_phase_change

    # ensure only one instance of this task is running
    task.unique("control_ev_charging", kill_me=True)

    # Configuration parameters from Const class
    MIN_CURRENT = 6  # Amps (absolute minimum supported by charger)
    MAX_CURRENT = 16  # Amps (maximum supported by circuit)
    VOLTAGE = 230  # Volts (regional standard)

    HYSTERESIS_BUFFER = 500  # Watts buffer for phase switching

    # the current state of charge of the EV
    current_soc = get(EV.soc, 0)
    # required state of charge defined by the owner
    required_soc = get(EV.required_soc, 80)
    # the current excess power available, this is defined as power going into the battery or into the grid (or the opposite, depending on the sign)
    excess_power = get(Excess.power, 0)  # in kW
    battery_soc = get(Battery.soc, 0)

    pv_total_power = get(PVProduction.total_power, 0)  # in W

    # target excess is the amount of power requested by the home battery to be able to cover the house loads in the near future
    # it is dynamically updated by a separate automation
    target_excess = get(Excess.target, 0)  # in kW
    # surplus energy is the amount of energy that is likely available after accounting for house loads in the near future
    surplus_energy = get(House.energy_surplus, 0)  # in kWh

    # this is the maximum charge current that the vehicle should be charge with right now
    configured_current = get(Charger.current_setting, 6)
    configured_phases = get(Charger.phases, 3)

    debug_info = f"Current SOC: {current_soc}%, Required SOC: {required_soc}%, Surplus {surplus_energy:.2f} Excess: {excess_power:.2f} kW, Target: {target_excess:.2f} kW "

    # Calculate energy requirements
    smart_charge_limit = get(EV.smart_charge_limit, required_soc)
    smart_limiter_active = get(Automation.auto_charge_limit, False)
    if smart_limiter_active:
        required_soc = min(smart_charge_limit, required_soc)

    energy_needed = max(0, (required_soc - current_soc) / 100 * Const.ev_capacity)
    if energy_needed <= 0 and surplus_energy < 1:
        turn_off_charger(reason="No energy needed - turning off charger")
        return

    # next drive is the point in time where the user needs to have the car charged to the required soc
    ongoing = get(EV.planned_drives, False)
    next_drive = get_attr(EV.planned_drives, "next_event")

    if ongoing:
        next_drive = None  # if ongoing, next_drive is actually next_return, so we ignore it

    elif next_drive:
        next_drive = with_timezone(next_drive)
        hours_available_to_charge = (next_drive - now()).total_seconds() / 3600
    else:
        hours_available_to_charge = 999

    # Calculate minimum time needed to charge the vehicle, we subtract 1 to account for charging inefficiencies
    min_hours_needed = energy_needed / (3 * VOLTAGE * (MAX_CURRENT - 1) / 1000)  # in hours

    # these are binary sensors defined separately that indicate whether the price is relatively low or high
    low_price = get(ElectricityPrices.low_price, False)
    high_price = get(ElectricityPrices.high_price, False)

    debug_info += (
        f"Energy needed: {energy_needed:.2f} kWh, Time needed: {min_hours_needed:.2f}h, "
        f"left: {hours_available_to_charge:.2f}h, low price: {low_price}, high price: {high_price}, "
        f"excess_power > target_excess: {excess_power > target_excess}"
    )

    is_charging = get(Charger.control_switch, False)

    #  -------------------------- Charging Strategy Logic -----------------------------------
    #  - when there's time to charge is running out to reach target SOC, charge with max current
    #  - when price is low and less than 24h left, charge with max current
    #  - when there's excess power and no time constraints, charge with excess power
    #  - when charging already active, control the charge amps to meet target excess
    #  - when price is high and time is not constrained, turn off the charger
    #  -------------------------------------------------------------------------------------

    # if no more charging needed, turn off the charger
    if current_soc > required_soc and surplus_energy <= 1:
        turn_off_charger(f"required SoC was reached, stopping charge with surplus of {surplus_energy:.0f}kWh")
    elif smart_limiter_active and current_soc >= smart_charge_limit:
        turn_off_charger(f"Smart charge limit of {smart_charge_limit} reached.")
    # Emergency charge, independently of price or excess power
    elif (
        hours_available_to_charge < 2 or hours_available_to_charge < min_hours_needed
    ) and current_soc < (required_soc - 1):
        set_phases_and_current(3, MAX_CURRENT, f"Emergency charge - SOC: {current_soc}% < Target: {required_soc}%")
        turn_on_charger(f"Emergency charge - SOC: {current_soc}% < Target: {required_soc}%")
    # Charge when price is low and not much time left (likely not possible to charge via excess)
    elif low_price and hours_available_to_charge < 24:  # Use cheap electricity
        battery_discharging = get(Victron.mode_sensor, default="off").lower() == "on" and not get(
            Battery.force_charge_switch, False
        )
        if battery_discharging:  # in case we're discharging from battery, we should limit the current accordingly
            log.warning("Charging at low price, with battery discharging")
            adj = calculate_charger_current_adjustment(
                excess_power, target_excess, configured_phases=3, configured_current=configured_current
            )  # Calculate current adjustment
            set_phases_and_current(3, configured_current + adj, "Charging at low price, with battery discharging")
        else:
            log.warning("Charging at low price, no battery discharging")
            set_phases_and_current(3, MAX_CURRENT, "Charging at low price, no battery discharging")
        turn_on_charger(reason="Low price charging")
    # PV surplus charging, when sufficient excess is available and no time constraints
    elif (
        excess_power > target_excess
        and (surplus_energy > 5 or excess_power > 2 and battery_soc > 90)
        and (  # prevent charging by discharging from battery when we can excess charge the next day
            pv_total_power > 1000 or hours_available_to_charge < 24 and current_soc <= required_soc
        )
    ):
        available_power = (excess_power - target_excess) * 1000  # convert kW to W
        # Hysteresis logic with proper unit conversion (W->kW)
        min_3phase_power = 3 * VOLTAGE * MIN_CURRENT  # in W
        if configured_phases == 3:
            phase_switch_threshold = min_3phase_power - HYSTERESIS_BUFFER
        else:
            phase_switch_threshold = min_3phase_power + HYSTERESIS_BUFFER

        current_power = configured_phases * configured_current * VOLTAGE  # W
        phases = 3 if (current_power + available_power) >= phase_switch_threshold else 1
        # Calculate current with bounds checking and explicit type conversion

        adj = calculate_charger_current_adjustment(excess_power, target_excess, configured_phases, configured_current)
        set_phases_and_current(
            phases, configured_current + adj, f"Target Power for EV: {current_power + available_power:.2f} kW"
        )
        turn_on_charger(reason=f"Excess power detected: {excess_power:.2f} kW, Target: {target_excess:.2f} kW")

    # When charging already active, need to control the charge amps to meet target excess
    elif is_charging and excess_power < target_excess:
        deficit = target_excess - excess_power
        # Consider phase reduction if we're at minimum current for 3-phase
        log.warning(
            f"Excess {excess_power} target {target_excess} deficit: {deficit:.2f} kW, configured_current: {configured_current}A, configured_phases: {configured_phases}"
        )
        if configured_current == MIN_CURRENT:
            if configured_phases == 3:
                # Calculate max current for 1 phase and set it
                adj = calculate_charger_current_adjustment(
                    excess_power, target_excess, configured_phases=1, configured_current=MAX_CURRENT
                )
                log.warning(f"Reducing phases to meet deficit: {deficit:.2f} {configured_current}+{adj} kW")
                set_phases_and_current(
                    1, configured_current + adj, f"Reducing phases to meet deficit of {deficit:.2f} kW"
                )
            else:
                turn_off_charger(
                    f"Excess {excess_power:.1f}kW below target of {target_excess:.1f}kW, current {configured_current}A already at minimum, unable to reduce further"
                )
        else:
            log.warning(f"Adjusting current to meet deficit: {deficit:.2f} kW")
            adj = calculate_charger_current_adjustment(
                excess_power, target_excess, configured_phases, configured_current
            )
            set_current(
                max(MIN_CURRENT, configured_current + adj), f"Reducing current to meet deficit of {deficit:.2f} kW"
            )

    elif high_price and surplus_energy <= 0 and excess_power < (target_excess - 1):
        # Only charge if time-constrained with explicit unit conversion
        charge_rate_kw = (3 * VOLTAGE * MAX_CURRENT) / 1000  # Convert W to kW
        min_required_time = energy_needed / charge_rate_kw  # Hours
        if not (hours_available_to_charge < min_required_time - 0.5):
            turn_off_charger("high electricity price and still time available")

    else:
        turn_off_charger("None of the conditions for auto charging matched")

    log.warning(debug_info)
