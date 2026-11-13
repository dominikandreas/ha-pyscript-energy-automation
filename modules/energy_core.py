from datetime import datetime, timedelta
from math import ceil, isfinite
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    # Therefore during type checking we pretend to import them from modules.utils, which it can resolve.
    from modules.const import EV as Const
    from modules.utils import clip, with_timezone
    from modules.victron import Victron

else:
    from const import EV as Const
    from utils import clip, with_timezone
    from victron import Victron


HYSTERESIS_BUFFER = 1200  # Watts buffer for phase switching
SURPLUS_START_MARGIN = 300  # W above the physical minimum before starting
SURPLUS_STOP_MARGIN = 300  # W tolerated below the physical minimum while running
EV_SURPLUS_START_CONFIRMATION_MINUTES = 3.0
EV_BATTERY_SUPPORT_RESERVE_KWH = 3.0
EV_BATTERY_SUPPORT_BUDGET_HOURS = 1.0
LIVE_PV_HEADROOM_RUNNING_MARGIN_W = 150.0
EV_SURPLUS_IMPORT_STOP_W = 300.0
EV_SURPLUS_IMPORT_EMERGENCY_STOP_W = 5000.0
EV_SURPLUS_IMPORT_STARTUP_GRACE_SECONDS = 30.0
EV_LOW_PV_TRIGGER_SOC = 40.0
EV_LOW_PV_TARGET_SOC = 80.0
EV_LOW_PV_DRIVE_TARGET_SOC = 100.0
EV_LOW_PV_BALANCE_THRESHOLD_KWH = 3.0
EV_LOW_PV_ACTIVE_SESSION_MIN_SHORTFALL_KWH = 0.1
EV_LOW_PV_BALANCE_MAX_AGE_MINUTES = 20.0
EV_LOW_PRICE_NIGHT_START_HOUR = 19
EV_LOW_PRICE_NIGHT_END_HOUR = 7


class ChargeAction:
    on = "on"
    off = "off"


class ChargeMode:
    hard_stop = "hard_stop"
    idle = "idle"
    required = "required"
    low_pv_grid = "low_pv_grid"
    surplus = "surplus"
    arrival_precharge = "arrival_precharge"


@pyscript_compile
def get_ev_surplus_import_safety_action(
    grid_import_w,
    seconds_since_adjustment,
    configured_current,
    configured_phases,
    stop_threshold_w=EV_SURPLUS_IMPORT_STOP_W,
    emergency_threshold_w=EV_SURPLUS_IMPORT_EMERGENCY_STOP_W,
    startup_grace_seconds=EV_SURPLUS_IMPORT_STARTUP_GRACE_SECONDS,
    voltage=230.0,
    min_current=6,
):
    """Return the least disruptive response to surplus-charge grid import.

    The wallbox and home-battery controller need several seconds to exchange
    load after the charge switch closes or its current changes. Moderate import
    during that interval is not evidence of an ongoing deficit. Once settled,
    shed enough current to approach the import threshold before stopping the
    transaction. Only an emergency import or persistent import at minimum
    current requires an immediate stop.

    Returns ``(action, current)`` where action is one of ``none``, ``hold``,
    ``reduce`` or ``stop``. ``current`` is populated only for ``reduce``.
    """

    try:
        grid_import_w = float(grid_import_w)
        configured_current = float(configured_current)
        configured_phases = int(configured_phases)
        stop_threshold_w = float(stop_threshold_w)
        emergency_threshold_w = float(emergency_threshold_w)
        startup_grace_seconds = float(startup_grace_seconds)
        voltage = float(voltage)
        min_current = int(min_current)
        if seconds_since_adjustment is not None:
            seconds_since_adjustment = float(seconds_since_adjustment)
    except (TypeError, ValueError, OverflowError):
        return "stop", None

    if not isfinite(grid_import_w) or grid_import_w <= stop_threshold_w:
        return "none", None
    if grid_import_w >= emergency_threshold_w:
        return "stop", None
    if (
        seconds_since_adjustment is None
        or not isfinite(seconds_since_adjustment)
        or seconds_since_adjustment < startup_grace_seconds
    ):
        return "hold", None
    if configured_phases not in (1, 3) or voltage <= 0 or configured_current <= min_current:
        return "stop", None

    amps_to_shed = max(
        1,
        ceil((grid_import_w - stop_threshold_w) / (voltage * configured_phases)),
    )
    return "reduce", max(min_current, int(configured_current) - amps_to_shed)


@pyscript_compile
def calculate_live_pv_headroom_power(
    grid_power_w,
    battery_power_kw,
    wallbox_power_w,
    is_charging,
    running_margin_w=LIVE_PV_HEADROOM_RUNNING_MARGIN_W,
):
    """Return measured PV power that can be redirected to the EV safely.

    A zero grid reading is ambiguous while the home battery is charging: the
    battery may be absorbing substantial PV before it reaches the meter. Its
    positive charge power is therefore recoverable PV headroom, just like
    export. Any simultaneous import is removed, so the value can never use a
    grid or forced battery charge. During an active EV charge the wallbox load
    is added back to retain the proven level, with a small import margin.
    """

    try:
        grid_power_w = float(grid_power_w)
        battery_power_kw = float(battery_power_kw)
        wallbox_power_w = float(wallbox_power_w)
        running_margin_w = float(running_margin_w)
    except (TypeError, ValueError):
        return 0.0

    grid_import_w = max(0.0, grid_power_w)
    grid_export_w = max(0.0, -grid_power_w)
    battery_charge_w = max(0.0, battery_power_kw * 1000.0)
    recovered_wallbox_w = max(0.0, wallbox_power_w) if is_charging else 0.0
    margin_w = max(0.0, running_margin_w) if is_charging else 0.0
    return max(
        0.0,
        grid_export_w + battery_charge_w + recovered_wallbox_w - grid_import_w - margin_w,
    )


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
def get_low_pv_ev_charge_target(
    current_soc,
    five_day_balance,
    next_drive,
    t_now,
    existing_target=0,
    balance_calculated_at=None,
    existing_deadline=None,
    pv_opportunity_kwh=None,
    pv_opportunity_calculated_at=None,
):
    """Return the latched overnight EV target for a weak five-day forecast.

    Preserve an admitted session independently of transient forecast validity.
    Grid authorization is evaluated separately, so stale data can pause a
    session without losing it. ``existing_deadline`` binds a 100% target to the
    departure that created it, allowing the session to survive midnight.
    """

    try:
        current_soc = float(current_soc)
        five_day_balance = float(five_day_balance)
        existing_target = float(existing_target or 0)
        if isinstance(next_drive, str):
            next_drive = datetime.fromisoformat(next_drive)
        if isinstance(balance_calculated_at, str):
            balance_calculated_at = datetime.fromisoformat(balance_calculated_at)
        if isinstance(existing_deadline, str) and existing_deadline:
            existing_deadline = datetime.fromisoformat(existing_deadline)
        if isinstance(pv_opportunity_calculated_at, str) and pv_opportunity_calculated_at:
            pv_opportunity_calculated_at = datetime.fromisoformat(pv_opportunity_calculated_at)
    except (TypeError, ValueError, OverflowError):
        return 0.0

    if not all(isfinite(value) for value in (current_soc, existing_target)):
        return 0.0
    if not isinstance(t_now, datetime):
        return 0.0

    if existing_target >= EV_LOW_PV_TARGET_SOC:
        if current_soc >= existing_target - 0.5:
            return 0.0
        following_day_drive = bool(
            isinstance(next_drive, datetime)
            and next_drive.date() == (t_now + timedelta(days=1)).date()
        )
        if existing_target >= EV_LOW_PV_DRIVE_TARGET_SOC:
            # Keep the balancing target while the same departure is pending.
            if existing_deadline is not None and next_drive == existing_deadline:
                return EV_LOW_PV_DRIVE_TARGET_SOC
            return EV_LOW_PV_DRIVE_TARGET_SOC if following_day_drive else EV_LOW_PV_TARGET_SOC
        return EV_LOW_PV_DRIVE_TARGET_SOC if following_day_drive else EV_LOW_PV_TARGET_SOC

    if not isfinite(five_day_balance):
        return 0.0
    if current_soc >= EV_LOW_PV_TRIGGER_SOC:
        return 0.0

    following_day_drive = bool(
        isinstance(next_drive, datetime)
        and next_drive.date() == (t_now + timedelta(days=1)).date()
    )
    candidate = EV_LOW_PV_DRIVE_TARGET_SOC if following_day_drive else EV_LOW_PV_TARGET_SOC
    return candidate if is_low_pv_ev_grid_charge_authorized(
        candidate,
        current_soc,
        five_day_balance,
        t_now,
        balance_calculated_at,
        pv_opportunity_kwh,
        pv_opportunity_calculated_at,
    ) else 0.0


@pyscript_compile
def is_low_pv_ev_grid_charge_authorized(
    target_soc,
    current_soc,
    five_day_balance,
    t_now,
    balance_calculated_at,
    pv_opportunity_kwh=None,
    pv_opportunity_calculated_at=None,
    active_session=False,
):
    """Validate current data before optional grid charging is allowed.

    The five-day balance remains the broad weak-PV gate. When the forecast
    planner has published a fresh EV-specific PV opportunity, require a real
    shortfall to the target as well. The opportunity is wall energy already
    admitted by the complete house/battery replay. The 3 kWh threshold is an
    admission filter: once a session has been admitted, finish the target as
    long as forecast PV still leaves a material shortfall. Reapplying the
    admission threshold to the shrinking remainder would always stop a charge
    several percentage points below its target.
    """

    try:
        target_soc = float(target_soc)
        current_soc = float(current_soc)
        five_day_balance = float(five_day_balance)
        if isinstance(balance_calculated_at, str):
            balance_calculated_at = datetime.fromisoformat(balance_calculated_at)
        if isinstance(pv_opportunity_calculated_at, str) and pv_opportunity_calculated_at:
            pv_opportunity_calculated_at = datetime.fromisoformat(pv_opportunity_calculated_at)
    except (TypeError, ValueError, OverflowError):
        return False
    if not all(isfinite(v) for v in (target_soc, current_soc, five_day_balance)):
        return False
    if not isinstance(t_now, datetime) or not isinstance(balance_calculated_at, datetime):
        return False

    def age_minutes(measured_at):
        if not isinstance(measured_at, datetime):
            return None
        if t_now.tzinfo is not None and measured_at.tzinfo is None:
            measured_at = measured_at.replace(tzinfo=t_now.tzinfo)
        try:
            return (t_now - measured_at).total_seconds() / 60
        except (TypeError, ValueError):
            return None

    balance_age = age_minutes(balance_calculated_at)
    if balance_age is None or not 0 <= balance_age <= EV_LOW_PV_BALANCE_MAX_AGE_MINUTES:
        return False
    if five_day_balance > EV_LOW_PV_BALANCE_THRESHOLD_KWH or target_soc <= current_soc:
        return False

    # No planner result yet: retain the conservative five-day fallback. Once a
    # planner result exists, stale opportunity data fails closed for new grid
    # energy and for an active session until the next forecast refresh.
    if pv_opportunity_kwh is None:
        return True
    try:
        pv_opportunity_kwh = float(pv_opportunity_kwh)
    except (TypeError, ValueError, OverflowError):
        return False
    opportunity_age = age_minutes(pv_opportunity_calculated_at)
    if (
        not isfinite(pv_opportunity_kwh)
        or opportunity_age is None
        or not 0 <= opportunity_age <= EV_LOW_PV_BALANCE_MAX_AGE_MINUTES
    ):
        return False
    wall_energy_to_target = max(
        0.0,
        (target_soc - current_soc) / 100 * Const.ev_capacity / Const.charge_efficiency,
    )
    minimum_shortfall = (
        EV_LOW_PV_ACTIVE_SESSION_MIN_SHORTFALL_KWH
        if active_session
        else EV_LOW_PV_BALANCE_THRESHOLD_KWH
    )
    return wall_energy_to_target - max(0.0, pv_opportunity_kwh) > minimum_shortfall


@pyscript_compile
def is_low_price_night(t_now):
    """Return whether a timestamp is inside the overnight charging window."""

    return bool(
        isinstance(t_now, datetime)
        and (t_now.hour >= EV_LOW_PRICE_NIGHT_START_HOUR or t_now.hour < EV_LOW_PRICE_NIGHT_END_HOUR)
    )


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
        # The reserve is an absolute 10 percentage points of EV SOC.  A 20%
        # multiplier on trip energy left almost no usable reserve on short
        # drives (30 km previously produced a 10.2% target).
        return min(100, round(drive_energy / Const.ev_capacity * 100 + 10, 2))
    return float(default_required_soc)


@pyscript_compile
def parse_ev_schedule_entries(
    schedule_data,
    default_required_soc,
    t_now,
    entry_cls,
    preserve_recent_arrival=False,
):
    """Shared EV schedule parser behind both app-level ``parse_full_schedule`` forks.

    ``now`` is a Pyscript runtime helper and cannot safely be resolved from
    inside a ``@pyscript_compile`` function.  Callers must freeze the time
    before entering this deterministic parser.
    """

    if t_now is None:
        raise ValueError("parse_ev_schedule_entries requires t_now")
    t_now = with_timezone(t_now)
    today = t_now.date()
    today_weekday = today.weekday()
    day_map = {
        name: i for i, name in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])
    }

    entries = []
    for day_name, events in schedule_data.items():
        event_weekday = day_map.get(day_name, -1)
        if event_weekday == -1:
            continue

        day_offset = (event_weekday - today_weekday + 7) % 7
        event_date = today + timedelta(days=day_offset)

        for event in events:
            soc = event.get("data", {}).get("required")
            distance = event.get("data", {}).get("distance")  # km
            soc = get_drive_required_soc(soc, distance, default_required_soc)

            event_start = with_timezone(datetime.combine(event_date, event["from"]))
            event_end = with_timezone(datetime.combine(event_date, event["to"]))
            if event_end <= event_start:
                event_end += timedelta(days=1)
            if event_end <= t_now and not (
                preserve_recent_arrival and t_now - event_end <= timedelta(hours=80)
            ):
                event_start += timedelta(days=7)
                event_end += timedelta(days=7)

            entries.append(
                entry_cls(
                    start=event_start,
                    end=event_end,
                    required_soc=float(soc) if soc is not None else None,
                    distance=float(distance) if distance is not None else None,
                )
            )

    return sorted(entries, key=lambda x: x.start)


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
    excess_power: float,
    excess_target: float,
    wallbox_power: float,
    is_charging: bool,
    battery_support_power: float = 0,
) -> float:
    """Return EV power available at the configured excess target.

    ``excess_power`` includes the wallbox load while charging. Adding the
    measured wallbox power back makes this value independent of whether the
    charger is currently on or off.
    """

    active_wallbox_power = max(0, wallbox_power) if is_charging else 0
    # Positive targets remain reserved for charging the home battery. Negative
    # targets are offered only through the separately budgeted support power;
    # this prevents a setpoint alone from turning stored energy into EV load.
    reserved_excess_power = max(0, excess_target)
    return (
        excess_power
        - reserved_excess_power
        + active_wallbox_power
        + max(0, battery_support_power)
    )


@pyscript_compile
def calculate_ev_battery_support_power(
    excess_target: float,
    ev_adjusted_surplus_energy: float,
) -> float:
    """Return bounded home-battery power that may support optional EV charging.

    ``ev_adjusted_surplus_energy`` is the conservative, AC-deliverable budget
    after the scheduled EV obligation and protected home-battery floor.  Keep a
    further error reserve and never authorize more power than the negative
    excess target requests or the remaining budget could sustain for one hour.
    """

    requested_discharge_power = max(0, -excess_target)
    support_budget_kwh = max(
        0,
        ev_adjusted_surplus_energy - EV_BATTERY_SUPPORT_RESERVE_KWH,
    )
    energy_bounded_power = support_budget_kwh * 1000 / EV_BATTERY_SUPPORT_BUDGET_HOURS
    return min(requested_discharge_power, energy_bounded_power)


@pyscript_compile
def apply_ev_surplus_start_confirmation(
    ev_charge_power: float,
    period_minutes: float,
    was_charging: bool,
    charge_mode: str,
):
    """Apply the live surplus-start confirmation delay to forecast power."""

    if ev_charge_power <= 0 or was_charging or charge_mode != ChargeMode.surplus:
        return ev_charge_power, 0.0
    delay_minutes = min(
        max(0, period_minutes),
        EV_SURPLUS_START_CONFIRMATION_MINUTES,
    )
    if period_minutes <= 0:
        return 0.0, delay_minutes
    active_fraction = (period_minutes - delay_minutes) / period_minutes
    return ev_charge_power * active_fraction, delay_minutes


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
def is_arrival_precharge_allowed(
    able_to_charge_on_arrival,
    next_drive,
    ongoing,
    current_soc,
    charge_limit,
    hours_available_to_charge,
    battery_soc,
    is_low_price=False,
):
    """Gate battery-funded arrival precharge on price, time pressure or overflow risk.

    Recovered rule ("prevent charging ev from battery unless leaving soon"):
    when the next drive leaves enough time to catch tomorrow's solar surplus,
    waiting is preferred over draining the home battery tonight.  Battery
    funded precharge is reserved for departures under 24h out, or a nearly
    full home battery (>90%, mirroring the surplus-start guard) that would
    otherwise spill PV the EV cannot absorb later.

    Precharge is strictly a low-price top-up: at the day rate it self-sustains
    on wallbox feedback plus the support budget and imports for hours while
    the high-price stop below it stays shadowed.  True urgency belongs to the
    emergency branch, real overflow to the surplus branch.
    """

    if not is_low_price:
        return False
    if not able_to_charge_on_arrival:
        return False
    if next_drive is None or ongoing:
        return False
    if current_soc >= charge_limit:
        return False
    if hours_available_to_charge < 24:
        return True
    return battery_soc > 90


@pyscript_compile
def get_ev_charge_feedback_state(
    controller_active,
    action,
    is_charging,
    start_candidate_since,
    stop_candidate_since,
    last_stop,
    started_at,
    t_now,
    start_confirmation_minutes=3,
    stop_confirmation_minutes=5,
    min_run_minutes=10,
    restart_lockout_minutes=5,
    force_active=False,
):
    """Map the EV surplus-timing state machine to dashboard feedback.

    Returns ``(state, seconds_remaining)`` where ``state`` is one of
    ``off``, ``confirming_start``, ``restart_lockout``, ``starting``,
    ``charging`` or ``confirming_stop``.  ``seconds_remaining`` counts down
    the active confirmation/lockout window and is ``None`` otherwise.
    Mirrors the guards in the live controller so the dashboard shows why
    the wallbox has not switched yet instead of looking dead.
    """

    if force_active and is_charging:
        return ("charging", None)
    if not controller_active:
        return ("off", None)
    if is_charging and stop_candidate_since is not None:
        deficit_for = (t_now - stop_candidate_since).total_seconds()
        run_for = (t_now - started_at).total_seconds() if started_at is not None else 0
        remaining = max(
            stop_confirmation_minutes * 60 - deficit_for,
            min_run_minutes * 60 - run_for,
            0,
        )
        return ("confirming_stop", remaining)
    if is_charging:
        return ("charging", None)
    if start_candidate_since is not None:
        stable_for = (t_now - start_candidate_since).total_seconds()
        remaining = max(0, start_confirmation_minutes * 60 - stable_for)
        return ("confirming_start", remaining)
    if action == "on":
        lockout_remaining = restart_lockout_minutes * 60 - (t_now - last_stop).total_seconds()
        if lockout_remaining > 0:
            return ("restart_lockout", lockout_remaining)
        return ("starting", None)
    return ("off", None)


@pyscript_compile
def required_departure_charge_setting(energy_needed_kwh, hours_available, required_soc):
    """Choose the lowest standard EVSE setting likely to meet a hard trip target."""
    usable_hours = max(0.05, hours_available - (0.5 if required_soc >= 99 else 0.25))
    if required_soc >= 99:
        usable_hours /= 1.25  # high-SOC taper and uncertainty
    wall_w = max(0.0, energy_needed_kwh) * 1000 / (0.9 * usable_hours)
    phases = 1 if wall_w <= Const.voltage * Const.max_current else 3
    amps = max(Const.min_current, min(Const.max_current, ceil(wall_w / (Const.voltage * phases))))
    return phases, amps


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
    battery_support_power: float = 0,
    allow_battery_precharge=False,
    precharge_energy_needed=0,
    planned_power=None,
    planned_target_soc=0,
    live_pv_headroom_power=0,
    low_pv_charge_target_soc=0,
    low_pv_grid_charge_allowed=True,
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
    # Charging to full can taper materially near 100%. A bare constant-power
    # estimate starts the departure fallback too late for cell balancing and
    # normal charger or vehicle delays. This applies only to a hard 100% trip
    # requirement, not the optional low-PV 100% ceiling.
    deadline_hours_needed = (
        min_hours_needed * 1.25 + 0.5 if required_soc >= 99 and energy_needed > 0 else min_hours_needed
    )

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
    required_charge_limit = max(required_charge_limit, low_pv_charge_target_soc)

    # Required SoC is a hard trip target. Smart limit is the PV-opportunistic
    # ceiling. Do not block surplus charging just because the trip target is met.
    surplus_charge_limit = max(required_charge_limit, smart_charge_limit, low_pv_charge_target_soc)
    available_ev_power = calculate_available_ev_power(
        excess_power=excess_power,
        excess_target=excess_target,
        wallbox_power=wallbox_power,
        is_charging=is_charging,
        battery_support_power=battery_support_power,
    )
    minimum_charge_power = Const.min_phases * Const.voltage * Const.min_current
    surplus_power_threshold = (
        minimum_charge_power - SURPLUS_STOP_MARGIN
        if is_charging
        else minimum_charge_power + SURPLUS_START_MARGIN
    )
    control_excess_target = max(0, excess_target) - max(0, battery_support_power)
    # Positive ``excess_target`` reserves power needed by the home battery.
    # A negative target is honored only up to ``battery_support_power``, which
    # has already been limited by the EV-adjusted kWh budget.
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

    if (
        low_pv_charge_target_soc > current_soc
        and low_pv_grid_charge_allowed
        and is_low_price
        and is_low_price_night(t_now)
    ):
        return (
            ChargeAction.on,
            3,
            Const.max_current,
            f"Low-PV overnight charge to {low_pv_charge_target_soc:.0f}%",
            ChargeMode.low_pv_grid,
        )

    if planned_power is not None:
        # A plan is an authorization, never a substitute for measured power.
        # Optional charging cannot add home-battery discharge to PV headroom.
        # Missing/expired/empty plans pass zero and retain only trip fallback.
        # Measured PV headroom is an exception: it is energy currently flowing
        # to the grid or into a charging home battery, so it must remain usable
        # even when a horizon plan deliberately reserved a later PV slot. The
        # live value is capped by the same local-power measurement and may
        # never borrow from the battery or create an import.
        urgent = (
            energy_needed > 0
            and current_soc < required_soc - 1
            and (hours_available_to_charge < 2 or hours_available_to_charge < deadline_hours_needed)
        )
        if urgent:
            phases, current = required_departure_charge_setting(energy_needed, hours_available_to_charge, required_soc)
            return (ChargeAction.on, phases, current,
                    "Departure deadline: charging to required SOC", ChargeMode.required)
        local_power = max(0.0, excess_power + (max(0.0, wallbox_power) if is_charging else 0.0))
        admitted_power = min(max(0.0, planned_power), local_power)
        target = min(surplus_charge_limit, planned_target_soc)
        start_threshold = minimum_charge_power + (0 if is_charging else SURPLUS_START_MARGIN)
        measured_pv_headroom = min(max(0.0, live_pv_headroom_power or 0.0), local_power)
        if current_soc < surplus_charge_limit and measured_pv_headroom >= start_threshold:
            # Keep the start-confirmation margin in reserve during the first
            # command. Previously it was required by the eligibility check and
            # then immediately consumed by current quantization, which caused
            # a small import transient and repeated safety-stop cycles.
            # Preserve control reserve during running adjustments too. An
            # upward amp step must not consume all measured headroom.
            command_power = measured_pv_headroom - SURPLUS_START_MARGIN
            phases = 3 if command_power >= 3 * Const.voltage * Const.min_current else 1
            current = max(Const.min_current, min(Const.max_current, int(command_power // (Const.voltage * phases))))
            return (
                ChargeAction.on,
                phases,
                current,
                f"Measured PV headroom charging to {surplus_charge_limit:.1f}%: {measured_pv_headroom:.0f} W available",
                ChargeMode.surplus,
            )
        if current_soc < target and admitted_power >= start_threshold:
            # Quantize down, including across phase transitions. Never use the
            # old phase hysteresis to round up beyond physically available PV.
            command_power = admitted_power - SURPLUS_START_MARGIN
            phases = 3 if command_power >= 3 * Const.voltage * Const.min_current else 1
            current = max(Const.min_current, min(Const.max_current, int(command_power // (Const.voltage * phases))))
            return (ChargeAction.on, phases, current,
                    f"Forecast-approved PV charge to {target:.1f}%: {admitted_power:.0f} W available",
                    ChargeMode.surplus)
        if energy_needed > 0 and is_low_price and hours_available_to_charge < 14:
            return (ChargeAction.on, 3, Const.max_current,
                    "Required trip charge in low-price window", ChargeMode.required)
        # A withdrawn authorization or inadequate measured PV is not a cloud
        # deficit that may be financed from grid during a minimum-run timer.
        return (ChargeAction.off, 1, Const.min_current,
                "Waiting for forecast-approved PV or required departure charge",
                ChargeMode.hard_stop)

    if (
        allow_battery_precharge
        and precharge_energy_needed > 0
        and battery_support_power >= minimum_charge_power
        and available_ev_power >= minimum_charge_power
    ):
        min_3phase_power = 3 * Const.voltage * Const.min_current
        phases = 3 if available_ev_power >= min_3phase_power - hysteresis else 1
        target_current = int(available_ev_power // (Const.voltage * phases))
        return (
            ChargeAction.on,
            phases,
            clip(target_current, Const.min_current, Const.max_current),
            f"Arrival precharge using bounded battery headroom: {precharge_energy_needed:.2f} kWh remaining",
            ChargeMode.arrival_precharge,
        )

    # if no more charging needed, turn off the charger
    active_surplus_charge = (
        is_charging
        and surplus_available
        and surplus_energy > 0
        and current_soc < surplus_charge_limit
    )
    if energy_needed <= 0 and not (surplus_available and current_soc < surplus_charge_limit) and not active_surplus_charge:
        reason = f"Required SoC reached and no PV surplus charging room, energy_needed={energy_needed:.2f}kWh"
        # Reaching the trip target while importing is a terminal condition, not
        # a transient PV deficit.  The live wrapper must bypass surplus
        # minimum-run and stop-confirmation timers here.
        return (ChargeAction.off, 1, 6, reason, ChargeMode.hard_stop)

    elif smart_limiter_active and current_soc >= smart_charge_limit:
        return (
            ChargeAction.off,
            1,
            6,
            f"Smart charge limit of {smart_charge_limit} reached.",
            ChargeMode.hard_stop,
        )
    # Emergency charge, independently of price or excess power
    elif (hours_available_to_charge < 2 or hours_available_to_charge < deadline_hours_needed) and current_soc < (
        required_soc - 1
    ):
        reason = f"Emergency charge - SOC: hours available {hours_available_to_charge} time needed {min_hours_needed}  {current_soc}% < Target: {required_soc}%"
        phases, current = required_departure_charge_setting(energy_needed, hours_available_to_charge, required_soc)
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
            battery_soc > 90 or pv_total_power > 1500 or current_soc < 40
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
            if battery_support_power > 0:
                # The filtered control error may lag a fast charger ramp.  Cap
                # the command immediately to power justified by current local
                # headroom plus the explicitly authorized battery support.
                safe_current = int(available_ev_power // (Const.voltage * phases))
                target_current = min(target_current, safe_current)
        else:
            # Start at a setpoint the measured headroom can actually support.
            target_current = int(available_ev_power // (Const.voltage * phases))

        phases, current, reason = (
            phases,
            clip(target_current, Const.min_current, Const.max_current),
            f"Local surplus power detected: {excess_power:.0f} W, control target: {control_excess_target:.0f} W, "
            f"battery support: {battery_support_power:.0f} W, current power {current_power:.0f} W, "
            f"available power for EV: {available_ev_power:.0f} W",
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
        # A deficit covered only by home-battery discharge, with no time
        # pressure and no overflow risk, must stop (via the normal
        # stop-confirmation path) instead of riding the battery down: the
        # surplus-start guard above already refuses such starts, and the
        # stop-confirmation timer cannot accumulate while "on" is returned.
        battery_bridged_deficit = (
            battery_support_power > 0
            and pv_total_power < 1500
            and battery_soc <= 90
            and hours_available_to_charge >= 24
        )
        if battery_bridged_deficit:
            return (
                "off",
                1,
                Const.min_current,
                f"ON->OFF: deficit of {deficit:.0f} W covered only by home battery "
                f"(PV {pv_total_power:.0f} W, battery {battery_soc:.0f}%, "
                f"{hours_available_to_charge:.1f}h to drive); waiting for sun",
                ChargeMode.surplus,
            )
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
    battery_support_power=0,
    allow_battery_precharge=False,
    precharge_energy_needed=0,
    max_control_steps=10,
    planned_power=None,
    planned_target_soc=0,
    low_pv_charge_target_soc=0,
    low_pv_grid_charge_allowed=True,
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
            battery_support_power=battery_support_power,
            allow_battery_precharge=allow_battery_precharge,
            precharge_energy_needed=precharge_energy_needed,
            planned_power=planned_power,
            planned_target_soc=planned_target_soc,
            low_pv_charge_target_soc=low_pv_charge_target_soc,
            low_pv_grid_charge_allowed=low_pv_grid_charge_allowed,
        )
        next_is_charging = action == ChargeAction.on
        if new_phases != phases and planned_power is None:
            new_current = 8 if new_phases == 1 else 6
        next_state = (next_is_charging, new_phases, new_current)
        is_charging, phases, current = next_state
        if not is_charging or next_state == previous_state:
            break

    return action, phases, current, reason, charge_mode
