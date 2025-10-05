from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    # Therefore during type checking we pretend to import them from modules.utils, which it can resolve.
    from modules.const import EV as Const
    from modules.states import Battery
    from modules.utils import clip, get
    from modules.victron import Victron

else:
    from const import EV as Const
    from states import Battery
    from utils import clip, get

    from victron import Victron


HYSTERESIS_BUFFER = 500  # Watts buffer for phase switching


class ChargeAction:
    on = "on"
    off = "off"


def _get_ev_smart_charge_limit(schedule, t_now, active_schedule=False):
    if not schedule:
        smart_charge_limit = 85
    else:
        td = schedule - t_now
        td_hours = td.total_seconds() // 3600

        # `or active_schedule` ensures that the car can continue to be charged when it was scheduled to leave but hasn't done so yet
        if td_hours < 6 or active_schedule:
            smart_charge_limit = 100
        elif td_hours < 20:
            smart_charge_limit = 98
        elif td_hours < 40:
            smart_charge_limit = 95
        elif td_hours < 60:
            smart_charge_limit = 90
        else:
            smart_charge_limit = 85
    return smart_charge_limit


def _get_ev_energy_needed(required_soc, current_soc, smart_charge_limit, smart_limiter_active):
    """Calculate the energy needed to charge the EV to the required state of charge"""

    if smart_limiter_active:
        required_soc = min(smart_charge_limit, required_soc)

    return max(0, (required_soc - current_soc) / 100 * Const.ev_capacity)


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

    # log.warning(
    #     f"Calculating charger current adjustment: {diff:.2f} kW delta, {configured_current}A current, {configured_phases} phases, "
    #     f"{power_change_per_current_step:.3f} kW/step, limits[{lower_limit}, {upper_limit}]. Raw: {raw_adj:.1f} → Adj: {adj}"
    # )
    return adj


def _get_charge_action(
    next_drive,
    current_soc,
    required_soc,
    energy_needed,
    excess_power,
    excess_target,
    surplus_energy,
    smart_charge_limit,
    smart_limiter_active,
    configured_phases,
    configured_current,
    is_low_price,
    pv_total_power,
    battery_soc,
    hysteresis=HYSTERESIS_BUFFER,
    is_charging=False,
    t_now=None,
):
    """Calculate the action to take for EV charging based on various conditions.

    -------------------------- Charging Strategy Logic -----------------------------------
     - when there's time to charge is running out to reach target SOC, charge with max current
     - when price is low and less than 24h left, charge with max current
     - when there's excess power and no time constraints, charge with excess power
     - when charging already active, control the charge amps to meet target excess
     - when price is high and time is not constrained, turn off the charger
     -------------------------------------------------------------------------------------
    """

    hours_available_to_charge = ((next_drive - t_now).total_seconds() / 3600) if next_drive else 999
    high_price = not is_low_price

    # Calculate minimum time needed to charge the vehicle, we subtract 1 to account for charging inefficiencies
    min_hours_needed = energy_needed / (3 * Const.voltage * (Const.max_current - 1) / 1000)  # in hours

    # Adjust target excess and surplus energy to account for inefficiencies, leave room for other devices
    surplus_energy = surplus_energy - 3

    # Charging hysteresis to prevent rapid changes in charging state
    if not is_charging and smart_charge_limit < 100:
        # 1 % hysteresis for charge limit
        smart_charge_limit = smart_charge_limit - 1
        # 2 kWh hysteresis for surplus energy
        surplus_energy = surplus_energy - 2
        # 1000 W for target excess
        excess_target = excess_target + 1000

    if smart_charge_limit == 100:
        smart_limiter_active = False
    # if no more charging needed, turn off the charger
    if energy_needed <= 0 and surplus_energy <= 1:
        reason = f"required SoC was reached, stopping charge with surplus of {surplus_energy:.0f}kWh"
        return (ChargeAction.off, 1, 6, reason)
    elif smart_limiter_active and current_soc > smart_charge_limit:
        return (ChargeAction.off, 1, 6, f"Smart charge limit of {smart_charge_limit} reached.")
    # Emergency charge, independently of price or excess power
    elif (hours_available_to_charge < 2 or hours_available_to_charge < min_hours_needed) and current_soc < (
        required_soc - 1
    ):
        reason = f"Emergency charge - SOC: hours available {hours_available_to_charge} time needed {min_hours_needed}  {current_soc}% < Target: {required_soc}%"
        phases, current = (3, Const.max_current)
        return (ChargeAction.on, phases, current, reason)
    # PV surplus charging, when sufficient excess is available and no time constraints
    elif (
        excess_power > excess_target
        and (surplus_energy > 0)
        and (  # prevent charging by discharging from battery when we can excess charge the next day
            battery_soc > 90 or pv_total_power > 1500 or hours_available_to_charge < 14 
        )
    ):
        available_power = (excess_power - excess_target) # in W
        # Hysteresis logic with proper unit conversion (W->kW)
        min_3phase_power = 3 * Const.voltage * Const.min_current  # in W
        if configured_phases == 3:
            phase_switch_threshold = min_3phase_power - hysteresis
        else:
            phase_switch_threshold = min_3phase_power + hysteresis

        current_power = configured_phases * configured_current * Const.voltage  # W
        phases = 3 if (current_power + available_power) >= phase_switch_threshold else 1
        # Calculate current with bounds checking and explicit type conversion

        adj = calculate_charger_current_adjustment(excess_power, excess_target, configured_phases, configured_current)
        phases, current, reason = (
            phases,
            configured_current + adj,
            f"Excess power detected: {excess_power:.0f} W, Target: {excess_target:.0f} W, current power {current_power:.0f} W, "
            f"Target Power for EV: {current_power + available_power:.0f} W",
        )
        return (ChargeAction.on, phases, current, reason)

    # When charging already active, need to control the charge amps to meet target excess
    elif is_charging and excess_power < excess_target:
        deficit = excess_target - excess_power
        # Consider phase reduction if we're at minimum current for 3-phase
        if configured_current == Const.min_current:
            if configured_phases > Const.min_phases:
                # Calculate max current for 1 phase and set it
                adj = calculate_charger_current_adjustment(
                    excess_power, excess_target, configured_phases=1, configured_current=Const.max_current
                )
                new_current = clip(Const.max_current + adj, Const.min_current, Const.max_current)
                return (
                    ChargeAction.on,
                    1,
                    new_current,
                    f"Reducing phases to meet deficit of {deficit:.2f} W",
                )
            else:
                return (
                    "off",
                    1,
                    6,
                    f"ON->OFF: Excess {excess_power:.1f} W below target of {excess_target:.1f} W, current {configured_current}A"
                    f" already at minimum, unable to reduce further",
                )
        else:
            adj = calculate_charger_current_adjustment(
                excess_power, excess_target, configured_phases, configured_current
            )
            return (
                "on",
                configured_phases,
                max(Const.min_current, configured_current + adj),
                f"Excess power detected: {excess_power:.2f} W, Target: {excess_target:.2f} W "
                f"Reducing current to meet deficit of {deficit:.2f} W",
            )

    # Charge when price is low and not much time left (likely not possible to charge via excess)
    elif is_low_price and hours_available_to_charge < 14:  # Use cheap electricity
        battery_discharging = get(Victron.mode_sensor, default="off").lower() == "on" and not get(
            Battery.force_charge_switch, False
        )
        if battery_discharging:  # in case we're discharging from battery, we should limit the current accordingly
            adj = calculate_charger_current_adjustment(
                excess_power, excess_target, configured_phases=3, configured_current=configured_current
            )  # Calculate current adjustment
            phases, current, reason = (3, configured_current + adj, "Charging at low price, with battery discharging")
        else:
            phases, current, reason = (3, Const.max_current, "Charging at low price, no battery discharging")
        return (ChargeAction.on, phases, current, reason)

    elif high_price and surplus_energy <= 0 and excess_power < excess_target:
        # Only charge if time-constrained with explicit unit conversion
        charge_rate_kw = (3 * Const.voltage * Const.max_current) / 1000  # Convert W to kW
        min_required_time = energy_needed / charge_rate_kw  # Hours
        if not (hours_available_to_charge < min_required_time - 0.5):
            return (ChargeAction.off, 1, 6, "high electricity price and still time available")
        else:
            return (ChargeAction.off, 1, 6, "high electricity price and still time available")

    return (ChargeAction.off, 1, 6, "None of the conditions for auto charging matched")
