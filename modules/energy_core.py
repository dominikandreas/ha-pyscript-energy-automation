from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    # Therefore during type checking we pretend to import them from modules.utils, which it can resolve.
    from modules.const import EV as Const
    from modules.utils import clip
    from modules.victron import Victron

else:
    from const import EV as Const
    from utils import clip
    from victron import Victron


HYSTERESIS_BUFFER = 1200  # Watts buffer for phase switching
SURPLUS_START_MARGIN = 300  # W above the physical minimum before starting
SURPLUS_STOP_MARGIN = 300  # W tolerated below the physical minimum while running


class ChargeAction:
    on = "on"
    off = "off"


class ChargeMode:
    hard_stop = "hard_stop"
    idle = "idle"
    required = "required"
    surplus = "surplus"


@pyscript_compile
def get_ongoing_and_next_drive(ev_schedule, t_now):
    """Return the active drive window and the following departure independently.

    A car can return before the configured drive window ends.  In that case the
    active window still matters to the simulator, but it must not hide the next
    departure from the live charging deadline logic.
    """

    if not ev_schedule:
        return None, None

    ongoing = next(iter([event for event in ev_schedule if event.start <= t_now < event.end]), None)
    next_drive = next(iter([event for event in ev_schedule if event.start > t_now]), None)
    return ongoing, next_drive


@pyscript_compile
def get_forecast_drive_context(ev_schedule, t_now, live_ongoing_drive, vehicle_present):
    """Treat only the current returned-early drive window as physically complete."""

    ongoing, next_drive = get_ongoing_and_next_drive(ev_schedule, t_now)
    if vehicle_present and live_ongoing_drive is not None and ongoing == live_ongoing_drive:
        ongoing = None
    return ongoing, next_drive


@pyscript_compile
def is_vehicle_present_during_active_drive(
    active_drive,
    charger_ready,
    charger_ready_since,
    is_charging,
):
    """Return whether telemetry proves the car returned during this drive.

    ``charger_ready`` can remain on after charging and across a later departure.
    Only a ready transition inside the active drive window (or actual charging)
    is evidence that the car is physically back early.
    """

    if active_drive is None:
        return False
    if is_charging:
        return True
    return bool(
        charger_ready
        and charger_ready_since is not None
        and active_drive.start <= charger_ready_since < active_drive.end
    )


@pyscript_compile
def is_forecast_ev_available(ev_schedule, t_now, forecast_time, ongoing_drive, vehicle_present_now):
    """Return whether the EV can be charged in one forecast bucket.

    Charger readiness is a transient actuator state: it is normally false while
    charging.  Freeze physical presence at the beginning of the forecast and
    only change it according to scheduled drive windows.  If the car is away
    now, a scheduled drive end is the earliest point at which it can be assumed
    to have returned.
    """

    if ongoing_drive is not None:
        return False
    if vehicle_present_now:
        return True
    scheduled_returns = [
        event for event in (ev_schedule or []) if t_now < event.end <= forecast_time
    ]
    return bool(scheduled_returns)


@pyscript_compile
def get_ev_charge_energy_limit(charge_mode, required_soc, smart_charge_limit):
    """Return the EV energy ceiling for one simulated charging action."""

    charge_limit_soc = smart_charge_limit
    if charge_mode == ChargeMode.required:
        charge_limit_soc = min(charge_limit_soc, required_soc)
    return charge_limit_soc * Const.ev_capacity / 100


@pyscript_compile
def get_drive_energy_drain(active_drive, period_hours):
    """Return the EV energy consumed during one forecast interval."""

    if active_drive is None:
        return 0
    drive_duration_hours = (active_drive.end - active_drive.start).total_seconds() / 3600
    if drive_duration_hours <= 0:
        return 0
    total_drive_energy = (active_drive.distance or 200) / 100 * Const.kwh_per_100km
    return total_drive_energy * period_hours / drive_duration_hours


@pyscript_compile
def get_drive_required_soc(required_soc, distance, default_required_soc):
    """Resolve one canonical departure SOC, including the distance margin."""

    if required_soc:
        return float(required_soc)
    if distance:
        drive_energy = min(Const.ev_capacity, float(distance) / 100 * Const.kwh_per_100km)
        return min(100, round(drive_energy / Const.ev_capacity * 100 * 1.2, 2))
    return float(default_required_soc)


@pyscript_compile
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
            smart_charge_limit = 95
        elif td_hours < 40:
            smart_charge_limit = 90
        elif td_hours < 60:
            smart_charge_limit = 85
        else:
            smart_charge_limit = 80
    return smart_charge_limit


@pyscript_compile
def _get_ev_energy_needed(required_soc, current_soc, smart_charge_limit, smart_limiter_active):
    """Calculate the energy needed to charge the EV to the required state of charge"""

    if smart_limiter_active:
        required_soc = min(smart_charge_limit, required_soc)

    return max(0, (required_soc - current_soc) / 100 * Const.ev_capacity)


@pyscript_compile
def calculate_charger_current_adjustment(
    current_excess: float, target_excess: float, configured_phases: int, configured_current: float
) -> int:
    """Calculate appropriate current adjustment based on excess power delta"""
    diff_w = current_excess - target_excess
    if abs(diff_w) < 300:
        return 0

    phases = max(Const.min_phases, configured_phases)
    watts_per_amp = Const.voltage * phases
    upper_limit = min(2, Const.max_current - configured_current)
    lower_limit = max(-2, Const.min_current - configured_current)

    raw_adj = diff_w / watts_per_amp
    return int(clip(round(raw_adj), lower_limit, upper_limit))


@pyscript_compile
def calculate_available_ev_power(
    excess_power: float, excess_target: float, wallbox_power: float, is_charging: bool
) -> float:
    """Return EV power available at the configured excess target.

    ``excess_power`` includes the wallbox load while charging. Adding the
    measured wallbox power back makes this value independent of whether the
    charger is currently on or off.
    """

    active_wallbox_power = max(0, wallbox_power) if is_charging else 0
    # A negative excess target deliberately requests home-battery discharge.
    # That energy is not PV surplus and must never be offered to an optional EV
    # charge.  Positive targets remain reserved for charging the home battery.
    reserved_excess_power = max(0, excess_target)
    return excess_power - reserved_excess_power + active_wallbox_power


@pyscript_compile
def get_simulated_ev_power_inputs(
    pv_power: float,
    house_power: float,
    configured_phases: int,
    configured_current: float,
    is_charging: bool,
):
    """Adapt forecast power flow to the live EV controller contract.

    Live ``excess_power`` already includes the active wallbox load.  Forecast
    PV and house load do not, so subtract the simulated wallbox here and pass
    that same load separately for availability normalization.
    """

    wallbox_power = (
        configured_phases * configured_current * Const.voltage
        if is_charging
        else 0
    )
    return pv_power - house_power - wallbox_power, wallbox_power


@pyscript_compile
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
    inverter_mode: str = "off",
    battery_force_charge=False,
    wallbox_power: float = 0,
    battery_floor_soc: float = 0,
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

    if inverter_mode in ("1", "2", "3", "4"):
        inverter_mode = Victron.PAYLOAD_TO_MODE.get(inverter_mode)

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
    if smart_charge_limit == 100:
        smart_limiter_active = False

    required_charge_limit = required_soc
    if smart_limiter_active:
        required_charge_limit = min(required_soc, smart_charge_limit)

    # Required SoC is a hard trip target. Smart limit is the PV-opportunistic
    # ceiling. Do not block surplus charging just because the trip target is met.
    surplus_charge_limit = max(required_charge_limit, smart_charge_limit)
    available_ev_power = calculate_available_ev_power(
        excess_power=excess_power,
        excess_target=excess_target,
        wallbox_power=wallbox_power,
        is_charging=is_charging,
    )
    minimum_charge_power = Const.min_phases * Const.voltage * Const.min_current
    surplus_power_threshold = (
        minimum_charge_power - SURPLUS_STOP_MARGIN
        if is_charging
        else minimum_charge_power + SURPLUS_START_MARGIN
    )
    control_excess_target = max(0, excess_target)
    # Positive ``excess_target`` reserves power needed by the home battery.
    # Negative targets request battery discharge and are intentionally ignored
    # for optional EV charging.
    # Once the remaining physical headroom can sustain the charger's minimum,
    # a separate global-energy/3 kW gate must not force that PV into the grid.
    surplus_available = available_ev_power >= surplus_power_threshold

    if current_soc >= surplus_charge_limit:
        return (
            ChargeAction.off,
            1,
            6,
            f"EV charge limit reached: {current_soc:.0f}% >= {surplus_charge_limit:.0f}%",
            ChargeMode.hard_stop,
        )

    # if no more charging needed, turn off the charger
    active_surplus_charge = is_charging and surplus_energy > 0 and current_soc < surplus_charge_limit
    if energy_needed <= 0 and not (surplus_available and current_soc < surplus_charge_limit) and not active_surplus_charge:
        reason = f"Required SoC reached and no PV surplus charging room, energy_needed={energy_needed:.2f}kWh"
        return (ChargeAction.off, 1, 6, reason, ChargeMode.idle)

    elif smart_limiter_active and current_soc >= smart_charge_limit:
        return (
            ChargeAction.off,
            1,
            6,
            f"Smart charge limit of {smart_charge_limit} reached.",
            ChargeMode.hard_stop,
        )
    # Emergency charge, independently of price or excess power
    elif (hours_available_to_charge < 2 or hours_available_to_charge < min_hours_needed) and current_soc < (
        required_soc - 1
    ):
        reason = f"Emergency charge - SOC: hours available {hours_available_to_charge} time needed {min_hours_needed}  {current_soc}% < Target: {required_soc}%"
        phases, current = (3, Const.max_current)
        return (ChargeAction.on, phases, current, reason, ChargeMode.required)
    elif (
        battery_floor_soc > 0
        and battery_soc < battery_floor_soc
        and not (energy_needed > 0 and is_low_price and hours_available_to_charge < 14)
    ):
        return (
            ChargeAction.off,
            1,
            6,
            f"Protected home battery floor: {battery_soc:.1f}% < {battery_floor_soc:.1f}%",
            ChargeMode.hard_stop,
        )
    # PV surplus charging, when sufficient excess is available and no time constraints
    elif (
        surplus_available
        and (  # prevent charging by discharging from battery when we can excess charge the next day
            battery_soc > 90 or pv_total_power > 1500 or hours_available_to_charge < 14 or current_soc < 40
        )
    ):
        # Hysteresis logic with proper unit conversion (W->kW)
        min_3phase_power = 3 * Const.voltage * Const.min_current  # in W
        if configured_phases == 3:
            phase_switch_threshold = min_3phase_power - hysteresis
        else:
            phase_switch_threshold = min_3phase_power + hysteresis

        current_power = configured_phases * configured_current * Const.voltage  # W
        phases = 3 if available_ev_power >= phase_switch_threshold else 1
        # Calculate current with bounds checking and explicit type conversion

        if is_charging:
            adj = calculate_charger_current_adjustment(
                excess_power, control_excess_target, configured_phases, configured_current
            )
            target_current = configured_current + adj if phases <= configured_phases else 7
        else:
            # Start at a setpoint the measured headroom can actually support.
            target_current = int(available_ev_power // (Const.voltage * phases))

        phases, current, reason = (
            phases,
            clip(target_current, Const.min_current, Const.max_current),
            f"Excess power detected: {excess_power:.0f} W, PV reserve target: {control_excess_target:.0f} W, current power {current_power:.0f} W, "
            f"Available Power for EV: {available_ev_power:.0f} W",
        )
        return (ChargeAction.on, phases, current, reason, ChargeMode.surplus)

    # Charge when price is low and not much time left (likely not possible to charge via excess)
    elif energy_needed > 0 and is_low_price and hours_available_to_charge < 14:  # Use cheap electricity
        battery_discharging = inverter_mode == "on" and not battery_force_charge
        if battery_discharging:  # in case we're discharging from battery, we should limit the current accordingly
            adj = calculate_charger_current_adjustment(
                excess_power, excess_target, configured_phases=3, configured_current=configured_current
            )  # Calculate current adjustment
            current = clip(configured_current + adj, Const.min_current, Const.max_current)
            phases, current, reason = (3, current, "Charging at low price, with battery discharging")
        else:
            phases, current, reason = (3, Const.max_current, "Charging at low price, no battery discharging")
        return (ChargeAction.on, phases, current, reason, ChargeMode.required)

    # When charging already active, need to control the charge amps to meet target excess
    elif is_charging and excess_power < control_excess_target:
        deficit = control_excess_target - excess_power
        # Consider phase reduction if we're at minimum current for 3-phase
        if configured_current == Const.min_current:
            if configured_phases > Const.min_phases:
                # Calculate max current for 1 phase and set it
                adj = calculate_charger_current_adjustment(
                    excess_power, control_excess_target, configured_phases=1, configured_current=Const.max_current
                )
                new_current = clip(Const.max_current + adj, Const.min_current, Const.max_current)
                return (
                    ChargeAction.on,
                    1,
                    new_current,
                    f"Reducing phases to meet deficit of {deficit:.2f} W",
                    ChargeMode.surplus,
                )
            else:
                return (
                    "off",
                    1,
                    6,
                    f"ON->OFF: Excess {excess_power:.1f} W below target of {control_excess_target:.1f} W, current {configured_current}A"
                    f" already at minimum, unable to reduce further",
                    ChargeMode.idle,
                )
        else:
            adj = calculate_charger_current_adjustment(
                excess_power, control_excess_target, configured_phases, configured_current
            )
            return (
                "on",
                configured_phases,
                max(Const.min_current, configured_current + adj),
                f"Excess power detected: {excess_power:.2f} W, Target: {control_excess_target:.2f} W "
                f"Reducing current to meet deficit of {deficit:.2f} W",
                ChargeMode.surplus,
            )

    elif high_price and surplus_energy <= 0 and excess_power < excess_target:
        return (ChargeAction.off, 1, 6, "high electricity price and still time available", ChargeMode.idle)

    return (ChargeAction.off, 1, 6, "None of the conditions for auto charging matched", ChargeMode.idle)


@pyscript_compile
def get_settled_simulated_ev_charge_action(
    next_drive,
    current_soc,
    required_soc,
    energy_needed,
    excess_target,
    surplus_energy,
    smart_charge_limit,
    smart_limiter_active,
    configured_phases,
    configured_current,
    is_low_price,
    pv_total_power,
    house_power,
    battery_soc,
    is_charging,
    t_now,
    inverter_mode,
    battery_force_charge,
    battery_floor_soc=0,
    max_control_steps=10,
):
    """Settle minute-rate EV control inside one forecast interval.

    Live control runs once per minute, while forecast intervals are normally
    30 minutes.  Replaying several controller decisions prevents a 2 A live
    slew limit from being stretched across whole forecast buckets.
    """

    action = ChargeAction.on if is_charging else ChargeAction.off
    phases = configured_phases
    current = configured_current
    reason = ""
    charge_mode = ChargeMode.idle

    for _ in range(max_control_steps):
        previous_state = (is_charging, phases, current)
        excess_power, wallbox_power = get_simulated_ev_power_inputs(
            pv_total_power,
            house_power,
            phases,
            current,
            is_charging,
        )
        action, new_phases, new_current, reason, charge_mode = _get_charge_action(
            next_drive=next_drive,
            current_soc=current_soc,
            required_soc=required_soc,
            energy_needed=energy_needed,
            excess_power=excess_power,
            excess_target=excess_target,
            surplus_energy=surplus_energy,
            smart_charge_limit=smart_charge_limit,
            smart_limiter_active=smart_limiter_active,
            configured_phases=phases,
            configured_current=current,
            is_low_price=is_low_price,
            pv_total_power=pv_total_power,
            battery_soc=battery_soc,
            is_charging=is_charging,
            t_now=t_now,
            inverter_mode=inverter_mode,
            battery_force_charge=battery_force_charge,
            wallbox_power=wallbox_power,
            battery_floor_soc=battery_floor_soc,
        )
        next_is_charging = action == ChargeAction.on
        if new_phases != phases:
            new_current = 8 if new_phases == 1 else 6
        next_state = (next_is_charging, new_phases, new_current)
        is_charging, phases, current = next_state
        if not is_charging or next_state == previous_state:
            break

    return action, phases, current, reason, charge_mode
