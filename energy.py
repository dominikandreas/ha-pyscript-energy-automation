# ruff: noqa: I001


from dataclasses import dataclass, replace
from datetime import datetime, timedelta, date
from math import pi, exp, sqrt, isfinite
from typing import TYPE_CHECKING, Any


# NODE: many functions have two @time_trigger decorators. this is not redundant, the first one
# without parameter triggers at function reload

if TYPE_CHECKING:
    # The type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    # Therefore during type checking we pretend to import them from modules.utils, which it can resolve.
    from modules.utils import clip, get, get_attr, set_state
    from modules.const import EV as EVConst
    from modules.energy_core import (
        _get_ev_smart_charge_limit,
        ChargeAction,
        ChargeMode,
        apply_ev_surplus_start_confirmation,
        calculate_ev_battery_support_power,
        get_drive_energy_drain,
        get_drive_required_soc,
        get_ev_charge_energy_limit,
        get_forecast_drive_context,
        get_ongoing_and_next_drive,
        get_settled_simulated_ev_charge_action,
        is_forecast_ev_available,
        is_vehicle_present_during_active_drive,
        parse_ev_schedule_entries,
    )
    from modules.setpoint_control import (
        FeedinCandidate,
        calculate_auto_max_setpoint,
        apply_hard_feedin_control,
        apply_silent_mode_grid_limits,
        build_price_aware_headroom_schedule,
        build_silent_charge_shaping_schedule,
        calculate_energy_bounded_control,
        calculate_headroom_energy_requirement,
        calculate_pv_overflow_energy,
        choose_stable_candidate,
        feedin_constraint_exceeded,
        limit_target_step,
        resolve_economic_export_floor,
    )
    from modules.export_scheduler import (
        ExportSchedulerSlot,
        ReserveTrajectorySlot,
        SHAPING_PV_CONFIDENCE_FACTOR,
        adapt_grid_target_to_live_power,
        apply_horizon_ev_reservation,
        are_battery_energy_values_valid,
        are_power_measurement_values_valid,
        are_power_measurements_coherent,
        build_hard_cap_storage_hold_schedule,
        build_high_side_headroom_requirements,
        build_reserve_energy_schedule,
        build_unified_export_schedule,
        calculate_absorbable_overflow_power,
        calculate_export_energy_budget,
        calculate_live_headroom_control,
        calculate_measurement_age_seconds,
        calculate_measurement_skew_seconds,
        calculate_net_battery_export_price,
        calculate_time_to_full_minutes,
        compute_room_overflow_requirement,
        first_uncovered_departure_need,
        fit_export_schedule_to_energy_budget,
        get_fresh_export_policy_budget,
        get_interval_price,
        headroom_release_factor,
        is_battery_energy_measurement_usable,
        is_ev_available_for_charging,
        select_finite_grid_target,
        is_ev_plan_fresh,
        is_measurement_fresh,
        parse_aware_datetime,
        read_headroom_released_kwh,
        resolve_directional_power_limit_w,
        limit_live_export_target_to_budget,
        select_no_battery_export_target,
        update_headroom_release_ledger,
        update_live_headroom_state,
        update_monotonic_export_budget_ledger,
    )

    # These are provided pyscript and defined for type inference only. They do not need to
    # (or rather must not) be imported in the actual script. They are only needed for type
    # checking (linting), which makes development easier
    from modules.utils import (
        log,
        now,
        pyscript_compile,
        time_trigger,
        state_trigger,
        with_timezone,
    )

    from modules.states import (
        Automation,
        Battery,
        EV,
        Excess,
        House,
        PVProduction,
        Grid,
        ElectricityPrices,
        Charger,
        PVForecast,
    )
    from modules.victron import Victron, get_auto_inverter_mode, InverterMode
    from modules.energy_core import _get_ev_energy_needed, _get_charge_action
    from electricity_price import is_low_price, get_price

else:
    from const import EV as EVConst
    from utils import clip, get, get_attr, now, set_state, with_timezone
    from states import (
        Automation,
        Battery,
        EV,
        Excess,
        House,
        PVProduction,
        Grid,
        ElectricityPrices,
        Charger,
        PVForecast,
    )
    from victron import Victron, get_auto_inverter_mode, InverterMode
    from energy_core import (
        _get_ev_smart_charge_limit,
        _get_ev_energy_needed,
        _get_charge_action,
        ChargeAction,
        ChargeMode,
        apply_ev_surplus_start_confirmation,
        calculate_ev_battery_support_power,
        get_drive_energy_drain,
        get_drive_required_soc,
        get_ev_charge_energy_limit,
        get_forecast_drive_context,
        get_ongoing_and_next_drive,
        get_settled_simulated_ev_charge_action,
        is_forecast_ev_available,
        is_vehicle_present_during_active_drive,
        parse_ev_schedule_entries,
    )  # noqa: F401
    from setpoint_control import (
        FeedinCandidate,
        calculate_auto_max_setpoint,
        apply_hard_feedin_control,
        apply_silent_mode_grid_limits,
        build_price_aware_headroom_schedule,
        build_silent_charge_shaping_schedule,
        calculate_energy_bounded_control,
        calculate_headroom_energy_requirement,
        calculate_pv_overflow_energy,
        choose_stable_candidate,
        feedin_constraint_exceeded,
        limit_target_step,
        resolve_economic_export_floor,
    )
    from export_scheduler import (
        ExportSchedulerSlot,
        ReserveTrajectorySlot,
        SHAPING_PV_CONFIDENCE_FACTOR,
        adapt_grid_target_to_live_power,
        apply_horizon_ev_reservation,
        are_battery_energy_values_valid,
        are_power_measurement_values_valid,
        are_power_measurements_coherent,
        build_hard_cap_storage_hold_schedule,
        build_high_side_headroom_requirements,
        build_reserve_energy_schedule,
        build_unified_export_schedule,
        calculate_absorbable_overflow_power,
        calculate_export_energy_budget,
        calculate_live_headroom_control,
        calculate_measurement_age_seconds,
        calculate_measurement_skew_seconds,
        calculate_net_battery_export_price,
        calculate_time_to_full_minutes,
        compute_room_overflow_requirement,
        first_uncovered_departure_need,
        fit_export_schedule_to_energy_budget,
        get_fresh_export_policy_budget,
        get_interval_price,
        headroom_release_factor,
        is_battery_energy_measurement_usable,
        is_ev_available_for_charging,
        select_finite_grid_target,
        is_ev_plan_fresh,
        is_measurement_fresh,
        parse_aware_datetime,
        read_headroom_released_kwh,
        resolve_directional_power_limit_w,
        limit_live_export_target_to_budget,
        select_no_battery_export_target,
        update_headroom_release_ledger,
        update_live_headroom_state,
        update_monotonic_export_budget_ledger,
    )
    from electricity_price import is_low_price, get_price


# EV capacity and charging constants live in modules/const.EV (imported as
# EVConst). A local duplicate used to exist here; it was removed so there is
# a single source of truth.
# Recorder calibration through 2026-08-26: the 90th-percentile 00:00-07:00
# house load was about 12% above the configured nightly baseline, while the
# 20th-percentile actual/Solcast power ratio was about 0.76.  Round outward so
# optional export keeps a risk reserve that is released as the morning nears.
# Pre-release shaping uses the stricter SHAPING_PV_CONFIDENCE_FACTOR from
# modules.export_scheduler: forced discharge through the PV ramp diverts
# rising PV to the grid too, so the hole outgrows the scheduled energy.
EXPORT_HOUSE_LOAD_MARGIN_FRACTION = 0.15
EXPORT_PV_CONFIDENCE_FACTOR = 0.75
EXPORT_BASE_UNCERTAINTY_MARGIN_KWH = 0.1
# Silent mode (input_boolean.silent_mode) keeps the inverter quiet by capping
# battery/grid exchange at SILENT_MODE_MAX_BATTERY_POWER_W: scheduled export
# and live battery-to-grid discharge never exceed it, while house and EV
# coverage stay unlimited.  Charging is capped only once shareable surplus
# exceeds the cap plus SILENT_MODE_CHARGE_SURPLUS_MARGIN_W; inside the margin
# band the battery takes everything instead of exporting a trickle.
SILENT_MODE_MAX_BATTERY_POWER_W = 3500.0
SILENT_MODE_CHARGE_SURPLUS_MARGIN_W = 500.0
# LFP cells drift unless the pack reaches full regularly.  When the last
# 100% charge is this many days back, the scheduler withholds economic
# export and soft-target pre-release until the battery fills again.
BALANCE_CHASE_AFTER_DAYS = 7.0
BALANCE_FULL_SOC_THRESHOLD = 99.5
HIGH_SIDE_PV_ERROR_DECAY_HOURS = 2.0
HIGH_SIDE_PV_MAX_ERROR_W = 4000.0
HIGH_SIDE_PV_MAX_MEASUREMENT_AGE_SECONDS = 120.0

# The forecast scheduler operates in half-hour buckets and cannot react safely
# to a cloud-edge ramp once the battery reaches full charge.  The fast loop
# therefore keeps a small, dynamically-sized energy buffer near full SOC.  It
# derives natural export from measured grid and battery power, holds existing
# free capacity through short PV dips, and creates additional capacity only
# while export remains below the scheduler's planning cap.
LIVE_HEADROOM_RISK_HORIZON_MINUTES = 10.0
LIVE_HEADROOM_HOLD_MINUTES = 15.0
LIVE_HEADROOM_BASE_KWH = 0.05
LIVE_HEADROOM_MAX_KWH = 1.0
LIVE_HEADROOM_ACTIVATION_BUFFER_KWH = 0.05
LIVE_HEADROOM_ACTIVATION_LEAD_TIME_MINUTES = 60.0
LIVE_HEADROOM_RELEASE_HYSTERESIS_W = 500.0
LIVE_HEADROOM_TRACKING_MARGIN_W = 200.0
LIVE_HEADROOM_MAX_TARGET_STEP_W = 1000.0
LIVE_HEADROOM_PUBLISH_INTERVAL_SECONDS = 15.0
# Grid power publishes several times per second while the battery sensor
# publishes roughly every 10 s, so a tighter skew gate flaps between coherent
# and skewed on healthy data.  Match both gates to the real cadence; the
# hold hysteresis below absorbs the remaining single-sample jitter.
LIVE_HEADROOM_MAX_MEASUREMENT_AGE_SECONDS = 20.0
LIVE_HEADROOM_MAX_MEASUREMENT_SKEW_SECONDS = 15.0
LIVE_PV_MAX_MEASUREMENT_AGE_SECONDS = 30.0
LIVE_HOUSE_LOAD_MAX_MEASUREMENT_AGE_SECONDS = 10.0
# sensor.battery_energy updates every 60-300 s (slower on quiet evenings), so a
# tighter gate flaps between fresh and stale on healthy data and pins the
# applied setpoint at the failsafe.  Match the gate to the real cadence.
LIVE_BATTERY_ENERGY_MAX_MEASUREMENT_AGE_SECONDS = 300.0
LIVE_POWER_MAX_ABSOLUTE_W = 20000.0
# The export policy snapshot is produced on a 120-second cadence, so a
# 120-second horizon has zero slack: normal publish jitter alone trips it
# and slams the setpoint to PV-only for a cycle (observed). 300 seconds
# covers two missed producer cycles; aging is safe because the live budget
# deducts battery draw since the snapshot before authorizing export.
LIVE_EXPORT_BUDGET_HORIZON_SECONDS = 300.0
# Budgets below this floor the setpoint at the PV-only target instead of
# authorizing export.  Single-digit Wh dither (planner snapshots eaten by
# live discharge within a cycle or two) must not flap the setpoint between
# the floor and the desired export: 0.05 kWh is far above that noise yet
# negligible next to any real export budget.
LIVE_EXPORT_BUDGET_RELEASE_KWH = 0.05
# Blind fallback must never force import: hold grid at zero and let the
# battery cover the difference. A blind import spikes grid cost to cover
# load the battery could have ridden through, while the battery controller
# already guards against truly unsafe discharge. (Kept historical name so
# existing source references and tests keep resolving; value must stay 0.)
LIVE_TELEMETRY_FAILSAFE_IMPORT_TARGET_W = 0.0
EXPORT_POLICY_MAX_AGE_SECONDS = 180.0

# When the battery is already high and PV is materially above the preferred
# feed-in level, price ranking must not postpone the first export bucket.  The
# scheduler otherwise preserves energy for later cheap-price windows and lets
# today's battery fill before the forecast catches up with a cloud-edge ramp.
PROACTIVE_EXPORT_TRIGGER_SOC_PERCENT = 90.0
PROACTIVE_EXPORT_PV_MARGIN_W = 1000.0
PROACTIVE_EXPORT_MAX_STEP_W = 3000.0

# AC/DC conversion assumptions used consistently by forecast energy accounting
# and economic export ranking.  Keeping these conservative is intentional: an
# optimistic conversion factor can authorize export that the battery cannot
# later replace.
HOME_BATTERY_CHARGE_EFFICIENCY = 0.90
HOME_BATTERY_DISCHARGE_EFFICIENCY = 0.90
EXPORT_BROKER_REVENUE_MARGIN_FRACTION = 0.03

# Do not tie up today's genuinely spendable energy for a distant weekly drive.
# Within the trustworthy 80-hour planning horizon, however, the EV obligation
# is energy-balanced through its actual departure rather than compared with a
# single battery-trough timestamp.
EV_EXPORT_RESERVATION_HORIZON_HOURS = 80.0


state.persist(  # noqa: F821 - provided by the Pyscript runtime
    "pyscript.export_policy_budget_ledger",
    default_value="",
    default_attributes={
        "friendly_name": "Export policy remaining-budget ledger",
    },
)


state.persist(  # noqa: F821 - provided by the Pyscript runtime
    "pyscript.roof_pv_emergency_pending",
    default_value="off",
    default_attributes={
        "friendly_name": "Roof PV emergency recovery pending",
    },
)


state.persist(  # noqa: F821 - provided by the Pyscript runtime
    "pyscript.battery_last_full",
    default_value="",
    default_attributes={
        "friendly_name": "Battery last full charge",
    },
)


@service  # noqa: F821 - provided by the Pyscript runtime
def set_roof_pv_emergency_pending(pending=True):
    """Persist whether this controller must reconcile curtailed roof PV."""

    pending_state = "off" if pending is False else "on"
    state.set(  # noqa: F821 - provided by the Pyscript runtime
        "pyscript.roof_pv_emergency_pending",
        pending_state,
        updated_at=now().isoformat(),
    )


@pyscript_compile
def get_forecast_battery_power_limits(
    configured_charge_limit_w: float,
    configured_discharge_limit_w: float,
) -> tuple[float, float]:
    """Keep forecast charge and discharge constraints direction-specific."""

    return (
        max(0.0, float(configured_charge_limit_w)),
        max(0.0, float(configured_discharge_limit_w)),
    )


def limit_forecast_discharge_to_hardware_w(
    planned_discharge_limit_w: float,
    hardware_discharge_limit_w: float,
) -> float:
    """Cap the planned battery discharge at the hardware discharge limit.

    The inverter (and the price automation through it) can forbid discharge
    entirely.  A forecast that ignores the ban discharges a battery that
    cannot discharge, then counts the stranded headroom as spendable surplus
    while the house draws from the grid.
    """
    try:
        planned = float(planned_discharge_limit_w)
        hardware = float(hardware_discharge_limit_w)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not isfinite(planned) or not isfinite(hardware):
        return 0.0
    return max(0.0, min(planned, hardware))


def get_planned_discharge_limit_w(default: float = -1):
    """Read discharge capability for planning, ignoring the telemetry hold.

    The stale-telemetry hold parks the shared discharge-limit input at 0
    for a few seconds to freeze live actuation.  A 24-hour forecast that
    samples that transient zero plans a battery that cannot discharge at
    all: frozen energy, grid import all night, slashed export schedule
    (observed 2026-09-24).  While the hold latch holds the pre-hold value,
    plan with that instead; sustained bans (price automation, inverter)
    set no latch and still reach the simulation unchanged.
    """
    held_limit = get("pyscript.telemetry_hold_discharge_limit", "")
    if held_limit not in ("", None, "unknown", "unavailable"):
        try:
            return float(held_limit)
        except (TypeError, ValueError):
            pass
    return get(Battery.discharge_limit, default)


@pyscript_compile
def calculate_battery_energy_delta_kwh(
    battery_ac_power_w: float,
    duration_hours: float,
    charge_efficiency: float = 0.90,
    discharge_efficiency: float = 0.90,
) -> float:
    """Convert AC-side battery power into stored-energy change."""

    duration_hours = max(0.0, duration_hours)
    charge_efficiency = max(1e-9, min(1.0, charge_efficiency))
    discharge_efficiency = max(1e-9, min(1.0, discharge_efficiency))
    ac_energy_kwh = float(battery_ac_power_w) * duration_hours / 1000
    if ac_energy_kwh >= 0:
        return ac_energy_kwh * charge_efficiency
    return ac_energy_kwh / discharge_efficiency


power_w_attributes = {
    "unit_of_measurement": "W",
    "device_class": "power",
    "state_class": "measurement",
}

grid_setpoint_basis_attributes = {
    **power_w_attributes,
    "min": -5500,
    "max": 5500,
}

power_kw_attributes = {
    "unit_of_measurement": "kW",
    "device_class": "power",
    "state_class": "measurement",
}

energy_kwh_attributes = {
    "unit_of_measurement": "kWh",
    "device_class": "energy",
    "state_class": "total",
}


@time_trigger
@time_trigger("cron(*/5 * * * *)")
def upcoming_demand():
    ev_current_soc = get(EV.battery_soc, default=50)
    ev_required_soc = get(EV.required_soc, default=50)

    t_now = now()
    next_event = get_attr(EV.planned_drives, "next_event")
    planned_distance = get(EV.planned_distance, 100)
    ongoing_drive = False
    planned_leave_soon = False
    if next_event is not None:
        next_event = with_timezone(next_event)
        td = next_event - t_now

        if td < timedelta(hours=24):
            ongoing_drive = get(EV.planned_drives, False)
            planned_leave_soon = True
    else:
        ongoing_drive = False
        td = None

    a = (t_now.month - 6) / 6
    usual_consumption_rate = a * 0.20 + (1 - a) * 0.16  # kWh/km

    required_charge = max(0, (ev_required_soc - ev_current_soc) / 100) * EVConst.ev_capacity

    if ongoing_drive or planned_leave_soon:
        expected_consumption = usual_consumption_rate * planned_distance
    else:
        expected_consumption = 0

    energy_to_wash = 2
    days_between_washes = 7

    t_since_washing = t_now - datetime.fromisoformat(get(House.last_washing)).astimezone()
    days_since_washing_machine_ran = t_since_washing.days + t_since_washing.seconds / 3600 / 24

    p_washing = max(0, min(1, days_since_washing_machine_ran / days_between_washes))
    washing_energy = p_washing * energy_to_wash

    # log.warning(f"ongoing drive {ongoing_drive} planned_leaving_soon {planned_leave_soon} required charge {required_charge:.0f} expected consumption {expected_consumption} wash {washing_energy}")
    ev_energy = required_charge + expected_consumption

    set_state(
        House.upcoming_demand,
        round(ev_energy + washing_energy, 2),
        **energy_kwh_attributes,
        icon="mdi:home-lightning",  # added icon
        friendly_name="Upcoming Demand",  # more descriptive friendly name
    )


@time_trigger
@time_trigger("cron(*/2 * * * *)")
def house_energy_until_production_meets_demand():
    night_avg_power = get(House.nightly_average_power, default=0)
    day_avg_power = get(House.daily_average_power, default=0)

    next_pv_meet_demand = get(PVProduction.next_meet_demand)
    if not next_pv_meet_demand or next_pv_meet_demand == "unknown":
        return

    next_pv_meet_demand = with_timezone(next_pv_meet_demand)
    t_now = now()

    total_energy = 0
    cursor = t_now
    # Integrate partial hours exactly; rounding both ends to whole hours can
    # otherwise reserve almost one extra hour of house demand.
    iters, max_iters = (0, 240)
    while cursor < next_pv_meet_demand and iters < max_iters:
        next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        segment_end = min(next_hour, next_pv_meet_demand)
        duration_hours = (segment_end - cursor).total_seconds() / 3600

        if 7 < cursor.hour < 19:
            total_energy += day_avg_power / 1000 * duration_hours
        else:
            total_energy += night_avg_power / 1000 * duration_hours

        cursor = segment_end
        iters += 1

    log.warning(
        f"House energy until production meets demand: daily avg {day_avg_power}W, nightly avg {night_avg_power}W, "
        f"next meet demand at {next_pv_meet_demand}, "
        f"total required=>{total_energy:.2f} kWh"
    )

    set_state(
        House.energy_demand,
        f"{total_energy:.2f}",
        **energy_kwh_attributes,
        icon="mdi:home",
        friendly_name="House Energy Until Production Meets Demand",
    )


@pyscript_compile
def _update_ewma_average(previous_average, current_value, alpha):
    """Shared exponentially-weighted moving-average step for 1m power sensors."""

    return round((1 - alpha) * previous_average + alpha * current_value, 2)


@time_trigger
@time_trigger("period(now, 10sec)")
def excess_power_1m_average():
    excess = get(Excess.power, default=0)  # in W
    excess_avg = get(Excess.power_1m_average, default=0)
    excess_avg = _update_ewma_average(excess_avg, excess, 0.1)
    set_state(
        Excess.power_1m_average,
        f"{excess_avg:.2f}",
        **power_w_attributes,
        friendly_name="Excess Power 1m Avg",
    )


@time_trigger
@time_trigger("period(now, 5sec)")
def grid_1m_average():
    grid_now = get(Grid.power_ac, default=0)  # in kW
    grid_avg = get(Grid.power_1m_average, default=grid_now)
    grid_avg = _update_ewma_average(grid_avg, grid_now, 0.2)
    set_state(
        Grid.power_1m_average,
        grid_avg,
        **power_kw_attributes,
        friendly_name="Grid 1m Average",
    )


@time_trigger
@state_trigger(Automation.min_discharge_price)
@state_trigger(ElectricityPrices.current_price)
@state_trigger(EV.is_charging)
@time_trigger("period(now, 300sec)")
def auto_victron_set_inverter_mode():
    # Get the current time and electricity price
    if not get(Victron.auto_set_inverter_mode, False):
        log.warning("Auto Victron inverter mode is disabled, skipping")
        return

    electricity_price = float(get(ElectricityPrices.current_price, default=0))
    min_discharge_price = float(get(Automation.min_discharge_price, default=0))
    low_price = get(ElectricityPrices.low_price, False)
    ev_is_charging = get(EV.is_charging, False)
    surplus_energy = get(House.energy_surplus, -1337)
    forecast_battery_headroom_energy = get(House.battery_headroom_until_trough, -1337)
    battery_soc = get(Battery.soc, -1337)
    target_soc = get(Automation.battery_target_soc, -1337)
    pv_power = get(PVProduction.total_power, -1337)  # in kW
    daily_avg_power = get(House.daily_average_power, -1337)
    charge_limit_percent = get(Battery.force_charge_up_to, 0)
    max_charge_price = get(Battery.max_charge_price, 0)
    force_charge_switch = get(Battery.force_charge_switch, False)
    current_mode = get(Victron.inverter_mode_input_select)
    battery_capacity = get(Battery.capacity, -1337)
    battery_energy = get(Battery.energy, -1337)
    reserve_soc = get_reserve_soc()
    minimal_soc = get(Automation.minimal_soc, reserve_soc)
    t_now = now()

    unified_scheduler_active = get_attr(Grid.power_setpoint_target, "unified_scheduler_active", False)
    if battery_capacity != -1337 and battery_energy != -1337:
        battery_export_floor = get_battery_export_floor(battery_capacity, reserve_soc, minimal_soc)
        if unified_scheduler_active:
            battery_export_floor = max(
                battery_export_floor,
                get_attr(
                    Grid.power_setpoint_target,
                    "planned_battery_floor_kwh",
                    battery_export_floor,
                ),
            )
        battery_headroom_energy = max(0, battery_energy - battery_export_floor)
    else:
        battery_headroom_energy = forecast_battery_headroom_energy

    if pv_power == -1337:
        log.warning("Total PV power state not available, using forecast instead")
        pv_power = get(PVForecast.power_now, -1337)

    for v in (surplus_energy, battery_headroom_energy, battery_soc, target_soc, pv_power, daily_avg_power):
        if v == -1337:
            log.error(
                f"Not all required states are available yet: surplus_energy: {surplus_energy}, "
                f"battery_headroom_energy: {battery_headroom_energy}, battery_soc: {battery_soc}, "
                f"target_soc: {target_soc}, pv_power: {pv_power}, daily_avg_power: {daily_avg_power}, skipping auto victron inverter mode"
            )
            return

    new_mode, new_charge_power_limit, new_force_charge_switch_state, reason = get_auto_inverter_mode(
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
        t_now=t_now,
        enforce_battery_floor=unified_scheduler_active,
        is_low_price=low_price,
    )

    # log.warning(
    #     f"Auto Victron inverter mode: {new_mode} {reason} pv power {pv_power}W daily avg {daily_avg_power}W battery soc {battery_soc}% target soc {target_soc}% surplus energy {surplus_energy}kWh target soc {target_soc}%"
    # )

    if new_charge_power_limit is not None:
        set_state(Battery.charge_limit, new_charge_power_limit)

    if new_force_charge_switch_state is not None:
        set_state(Battery.force_charge_switch, new_force_charge_switch_state)

    new_mode = Victron.PAYLOAD_TO_MODE.get(new_mode)
    if current_mode != new_mode:
        log.warning(
            f"{current_mode} -> {new_mode}: {reason}. "
            f"ev: {ev_is_charging}, surplus: {surplus_energy}, headroom: {battery_headroom_energy}, "
            f"soc: {battery_soc}, target soc: {target_soc}, "
            f"pv_power: {pv_power}, daily_avg_power {daily_avg_power}"
        )

        last_changed = get_attr(Victron.inverter_mode_input_select, "last_changed")
        if new_mode == InverterMode.on and (now() - last_changed).total_seconds() / 60 < 30:
            log.warning("Skipping Victron inverter mode change since last change was less than 30 minutes ago")
            return

    set_state(Victron.inverter_mode_input_select, new_mode)


@time_trigger
@time_trigger("cron(*/3 * * * *)")
def battery_use_until_pv_meets_demand():
    next_pv_meet_demand = get(PVProduction.next_meet_demand, None)
    if next_pv_meet_demand is None:
        return
    next_pv_meet_demand = with_timezone(next_pv_meet_demand)

    energy_until_pv_meets_demand = get(PVProduction.energy_until_production_meets_demand, 0)
    discharge_price = get(Automation.min_discharge_price, 100)
    daily_avg_power = get(House.daily_average_power, 0)
    night_avg_power = get(House.nightly_average_power, 0)
    t_now = now()

    price_attr = get_attr(ElectricityPrices.current_price)

    total_battery_use = 0

    # log.warning(f"got next_pv_meet_demand: {next_pv_meet_demand}, price_attr: {price_attr}")

    if next_pv_meet_demand and price_attr and "today" in price_attr and "tomorrow" in price_attr:
        log.info(
            f"iterating over prices to check if any below {discharge_price} for accumulating {daily_avg_power}W/{night_avg_power}W"
        )
        today = price_attr.get("today")
        tomorrow = price_attr.get("tomorrow")

        all_prices = (today or []) + (tomorrow or [])

        for entry in all_prices:
            start = datetime.fromisoformat(entry["startsAt"]).astimezone()
            house_power = (daily_avg_power if 7 < start.hour < 19 else night_avg_power) / 1000

            stop = min(start + timedelta(minutes=30), next_pv_meet_demand)
            price = entry["total"]
            if t_now < stop and start < next_pv_meet_demand:
                if price > discharge_price:
                    if t_now < stop and start < next_pv_meet_demand:
                        minutes = (min(stop, next_pv_meet_demand) - max(t_now, start)).seconds / 60
                        total_battery_use += house_power * minutes / 60
                else:
                    log.info(f"skipping {start.hour:2d}:{start.minute:02d} since {price} < {discharge_price}")

        result = max(0, total_battery_use - energy_until_pv_meets_demand)
    else:
        if next_pv_meet_demand:
            avg_house_power = (daily_avg_power + night_avg_power) / 2 / 1000
            result = ((next_pv_meet_demand - t_now).total_seconds() / 3600) * avg_house_power
        else:
            result = 0

    log.info(f"battery use until pv meets demand: {result}")

    set_state(
        Battery.use_until_pv_meets_demand,
        round(result, 3),
        **energy_kwh_attributes,
        friendly_name="Battery Use Until PV Meets Demand",
    )


def get_reserve_soc(t_now: datetime | None = None):
    # No software reserve: the battery may deplete until actually empty and
    # the BMS owns deep-discharge protection.  This replaces the former 5-30%
    # seasonal curve, which stranded usable energy at high prices.  The
    # argument stays so forecast callers keep passing their slot time.
    return 0.0


@pyscript_compile
def get_battery_export_floor(
    battery_capacity: float,
    reserve_soc: float,
    minimal_soc: float,
    uncertainty_margin_kwh: float = 0,
) -> float:
    """Energy that must remain available for house demand and battery reserve."""
    protected_soc = max(reserve_soc, minimal_soc)
    return min(battery_capacity, protected_soc / 100 * battery_capacity + uncertainty_margin_kwh)


@pyscript_compile
def get_pv_only_setpoint(
    pv_power: float,
    house_power: float,
    max_battery_charge_power: float,
    neutral_setpoint: float = -20,
) -> float:
    """Most negative setpoint that does not require battery-to-grid export."""
    unavoidable_pv_export = max(0, pv_power - house_power - max_battery_charge_power)
    return min(neutral_setpoint, -unavoidable_pv_export)


@pyscript_compile
def get_low_pv_reserve_soc(house_power: float, pv_power: float, battery_cells_balanced: bool) -> float:
    if battery_cells_balanced:
        return 0
    if house_power <= 0:
        return 0
    # Preserve the full 15% reserve below 75% PV coverage, then release it
    # continuously as PV approaches the house load.  The former hard 75%
    # switch made the battery target and EV current jump under passing clouds.
    pv_coverage = max(0, pv_power) / house_power
    return 15 * clip((1 - pv_coverage) / 0.25, 0, 1)


@pyscript_compile
def get_required_energy(
    house_energy_demand: float,
    pv_upcoming: float,
    excess_next_days: float,
) -> float:
    return max(
        0,
        house_energy_demand - pv_upcoming,
        min(0, excess_next_days),
    )


@time_trigger
@state_trigger(f"{Charger.control_switch} != 'undefined' and {Automation.auto_battery_target_soc} == 'on'")
@time_trigger("cron(*/1 * * * *)")
def auto_battery_target_soc():
    if not get(Automation.auto_battery_target_soc, False):
        return
    battery_soc = get(Battery.soc, default=50)
    base_reserve_soc = get_reserve_soc()

    house_demand = get(House.energy_demand, default=10)
    pv_upcoming = get(PVProduction.energy_until_production_meets_demand, default=0)
    excess_next_days = get(Excess.excess_next_three_days, default=0)
    house_power = get(House.daily_average_power, default=0)
    battery_capacity = get(Battery.capacity, default=8)
    battery_cells_balanced = get(Battery.cells_balanced, False)
    battery_energy = get(Battery.energy, default=0)
    surplus = get(House.energy_surplus, 0)
    surplus_detail = get_attr(House.energy_forecast, "detail")
    pv_power = get(PVProduction.total_power, default=0)

    if pv_power == 0:
        pv_power = get(PVForecast.power_now, default=0)

    reserve_soc_floor = max(
        base_reserve_soc,
        get_low_pv_reserve_soc(
            house_power=house_power, pv_power=pv_power, battery_cells_balanced=battery_cells_balanced
        ),
    )

    if not surplus_detail:
        return

    ev_is_charging = get(EV.is_charging, False)
    protect_battery_from_ev = ev_is_charging and surplus <= 0

    req_energy = get_required_energy(
        house_demand,
        pv_upcoming,
        excess_next_days,
    )

    msg = (
        f"\nAuto battery target soc calculation: \n"
        f"\n\thouse_demand: {house_demand:.2f}kWh, pv_upcoming: {pv_upcoming:.2f}kWh, reserve_soc: {reserve_soc_floor:.2f}%, "
        f"\n\texcess_next_days: {excess_next_days:.2f}kWh, battery_capacity: {battery_capacity:.2f}kWh, battery_energy: {battery_energy:.2f}kWh, surplus: {surplus:.2f}kWh => req_energy: {req_energy:.2f}kWh"
    )

    set_state(Automation.req_energy, round(req_energy, 2), **energy_kwh_attributes)

    max_soc = 80 if battery_cells_balanced else 100

    # req_energy is expressed in kWh and already contains the forecast deficit.
    # Convert it to SOC exactly once; adding a kWh delta directly to a percentage
    # made the protected floor rise when forecast PV exceeded house demand.
    minimal_soc = min(max_soc, (max(0, req_energy) / battery_capacity * 100) + reserve_soc_floor)
    set_state(Automation.minimal_soc, round(minimal_soc, 2), unit_of_measurement="%")  # different attributes

    # Prevent EV charging from pulling protected battery energy. If the EV is
    # charging from a positive surplus budget, do not raise the battery target:
    # that target jump fights the charger and creates on/off oscillation.
    result_soc = max(battery_soc + 1, minimal_soc) if protect_battery_from_ev else minimal_soc

    msg += f"\n\tminimal soc {minimal_soc:.1f} auto battery target soc: {result_soc:.1f}, protect_battery_from_ev: {protect_battery_from_ev}"
    set_state(
        Automation.battery_target_soc,
        round(result_soc, 2),
        min=0,
        max=100,
        unit_of_measurement="%",  # different attributes
    )

    log.warning(msg)


@pyscript_compile
def _get_excess_target(
    battery_target_soc,
    battery_soc,
    ev_required_soc,
    ev_is_charging,
    next_planned_drive,
    pv_power,
    ev_soc,
    t_now,
    efficient_discharge=True,
):
    power_abs_max = (
        2500 if efficient_discharge else 6000
    )  # use less than half of max inverter power for best efficiency
    soc_difference = (battery_target_soc - battery_soc) / 100

    normalized_difference = soc_difference * 2 * pi
    normalized_difference_clipped = clip(normalized_difference, -pi / 2, pi / 2)

    from math import sin

    power = sin(normalized_difference_clipped) * power_abs_max
    power = clip(power, -power_abs_max, power_abs_max)

    if ev_is_charging and efficient_discharge:
        planned_leave_soon = False
        if next_planned_drive is not None:
            next_planned_drive = with_timezone(next_planned_drive)
            planned_leave_soon = (next_planned_drive - t_now).total_seconds() / 3600 < 24
        # Reserve PV for the home battery with a continuous ramp.  The former
        # 2 kW threshold flipped the target by roughly 2 kW in one control
        # interval and made the wallbox oscillate between 6 and 14 A.
        if not planned_leave_soon:
            if ev_soc > ev_required_soc:
                pv_charge_reserve = max(0, pv_power - 4000)
            else:
                pv_charge_reserve = min(pv_power / 3, max(0, pv_power - 1500))
            # With no PV to reserve (notably at night) the override must not
            # touch the target: forcing it to 0 while charging starves the EV
            # battery support the discharge target just authorized, and the
            # wallbox cycles on/off all night (2026-10-11 incident).
            if pv_charge_reserve > 0:
                power = max(power, pv_charge_reserve)

    return power


@time_trigger("cron(*/1 * * * *)")
@time_trigger
async def auto_excess_target():
    if not get(Automation.auto_excess_target, False):
        return

    t_now = now()

    battery_target_soc = get(Automation.battery_target_soc, default=0)
    battery_soc = get(Battery.soc, default=0)
    ev_required_soc = get(EV.required_soc, 80)
    ev_is_charging = get(EV.is_charging, False)
    next_event = get_attr(EV.planned_drives, "next_event")
    pv_power = get(PVProduction.total_power, 0)
    ev_soc = get(EV.soc, 100)
    eff_dis = get(Automation.efficient_discharge, False)
    power = _get_excess_target(
        battery_target_soc,
        battery_soc,
        ev_required_soc,
        ev_is_charging,
        next_event,
        pv_power,
        ev_soc,
        t_now,
        efficient_discharge=eff_dis,
    )
    # log.warning(f"""
    #     Got Auto excess target: {power:.0f} W with params
    #         battery_target_soc: {battery_target_soc}
    #         battery_soc: {battery_soc}
    #         ev_required_soc: {ev_required_soc}
    #         ev_is_charging: {ev_is_charging}
    #         next_event: {next_event}
    #         pv_power: {pv_power}
    #         ev_soc: {ev_soc}
    #         t_now: {t_now}
    #         efficient_discharge: {eff_dis}
    # """)
    set_state(
        Excess.target,
        round(power, 2),
        min=-8000,
        max=8000,
        **power_w_attributes,
    )


@time_trigger
@time_trigger("cron(*/1 * * * *)")
def battery_energy():
    battery_soc = get(Battery.soc, default=-1)
    battery_capacity = get(Battery.capacity, default=0)
    if battery_capacity == 0 or battery_soc == -1:
        return

    set_state(
        Battery.energy,
        round(battery_soc / 100 * battery_capacity, 2),
        **energy_kwh_attributes,
        icon="mdi:car-battery",
        friendly_name="Battery Energy",
    )

    # Stamp every full charge so the export scheduler knows when cells last
    # balanced.  Missing history reads as overdue, which only matters until
    # the first observed full charge after deploy.
    if battery_soc >= BALANCE_FULL_SOC_THRESHOLD:
        set_state(
            "pyscript.battery_last_full",
            now().isoformat(),
            friendly_name="Battery last full charge",
        )


def set_energy_surplus(entity, surplus, **kwargs):
    set_state(
        entity,
        round(surplus, 2),
        **energy_kwh_attributes,
        icon="mdi:home",
        **kwargs,
    )


@pyscript_compile
def get_spendable_solar_cycle_energy(
    detail: list["ForecastEntry"],
    battery_export_floor: float,
    t_now: datetime,
    period_hours: float,
    battery_discharge_efficiency: float = 1.0,
    battery_discharge_allowed: bool = True,
) -> tuple[float, float, float, datetime | None]:
    """Return a conservative energy budget for the current solar cycle.

    The caller must provide a baseline forecast in which optional loads and
    optional battery-to-grid export are disabled.  Energy is spendable when it
    either remains above the protected battery floor at the following overnight
    trough, or would otherwise be exported from PV before that trough.

    When battery discharge is banned (``battery_discharge_allowed`` is False),
    headroom above the floor is stranded: it can neither cover loads nor be
    exported, so only PV spill counts as spendable.  Reporting it anyway
    publishes a surplus while the house draws from the grid.

    The result is a schedulable energy budget, not an assertion that all of the
    energy can be drawn immediately; live power controls still need to align a
    variable load with the available PV/battery power.
    """
    future = [entry for entry in detail if entry.period_start >= t_now]
    if not future:
        return 0, 0, 0, None

    today = t_now.date()
    solar_today = [
        entry
        for entry in future
        if entry.period_start.date() == today and entry.pv_estimate >= entry.house_power
    ]

    # With no useful solar window left today, retain the old conservative
    # meaning: only expose battery headroom at the next local trough.
    if not solar_today:
        trough_energy = future[0].battery_energy
        previous_energy = trough_energy
        trough_time = future[0].period_start
        for entry in future[1:]:
            if entry.battery_energy <= trough_energy:
                trough_energy = entry.battery_energy
                trough_time = entry.period_start
            elif entry.battery_energy > previous_energy + 0.05:
                break
            previous_energy = entry.battery_energy
        battery_headroom = max(0, trough_energy - battery_export_floor)
        spendable_headroom = (
            battery_headroom * battery_discharge_efficiency
            if battery_discharge_allowed
            else 0.0
        )
        return (
            spendable_headroom,
            battery_headroom,
            0,
            trough_time,
        )

    solar_end_time = solar_today[-1].period_start

    # End the budget at the next solar cycle.  This forces today's spendable
    # energy to cover the evening/night demand before tomorrow's PV is credited.
    next_solar_start = next(
        (
            entry.period_start
            for entry in future
            if entry.period_start.date() > today and entry.pv_estimate >= entry.house_power
        ),
        None,
    )
    cycle = [
        entry
        for entry in future
        if next_solar_start is None or entry.period_start < next_solar_start
    ]
    after_solar = [entry for entry in cycle if entry.period_start >= solar_end_time]
    trough_entry = min(after_solar or cycle, key=lambda entry: entry.battery_energy)

    battery_headroom = max(0, trough_entry.battery_energy - battery_export_floor)
    pv_spill = sum(max(0, entry.pv_feedin - 20) for entry in cycle) * period_hours / 1000
    spendable_headroom = (
        battery_headroom * battery_discharge_efficiency
        if battery_discharge_allowed
        else 0.0
    )
    spendable = spendable_headroom + pv_spill
    return spendable, battery_headroom, pv_spill, trough_entry.period_start


@pyscript_compile
def calculate_horizon_energy_balance(
    detail: list["ForecastEntry"],
    t_now: datetime,
    horizon_hours: float,
    battery_energy_now: float,
    battery_export_floor: float,
    period_hours: float,
    battery_discharge_efficiency: float = 1.0,
) -> tuple[float, float, float, float, float]:
    """Return the signed net energy position over a rolling horizon.

    Stock (usable battery above the protected floor) plus forecast PV
    inflows minus forecast house and EV wall-power outflows.  Negative
    means grid import will be needed inside the window.  Returns
    (balance, usable_battery, pv, house, ev_wall) in kWh.

    Informational/planning signal only: a positive balance assumes
    energy can shift freely inside the window (Wednesday PV cannot
    cover Monday night).  Never use it as a spendable control budget;
    the solar-cycle surplus remains the control signal.
    """
    try:
        horizon_hours = float(horizon_hours)
        battery_energy_now = float(battery_energy_now)
        battery_export_floor = float(battery_export_floor)
        period_hours = float(period_hours)
        battery_discharge_efficiency = float(battery_discharge_efficiency)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("horizon inputs must be finite numbers")
    for value in (
        horizon_hours,
        battery_energy_now,
        battery_export_floor,
        period_hours,
        battery_discharge_efficiency,
    ):
        if value != value or abs(value) == float("inf"):
            raise ValueError("horizon inputs must be finite numbers")
    if horizon_hours <= 0 or period_hours <= 0:
        raise ValueError("horizon_hours and period_hours must be positive")
    if battery_discharge_efficiency <= 0 or battery_discharge_efficiency > 1:
        raise ValueError("battery_discharge_efficiency must be in (0, 1]")

    window_end = t_now + timedelta(hours=horizon_hours)
    usable_battery = (
        max(0.0, battery_energy_now - battery_export_floor)
        * battery_discharge_efficiency
    )
    pv_kwh = 0.0
    house_kwh = 0.0
    ev_wall_kwh = 0.0
    for entry in detail:
        if not t_now <= entry.period_start < window_end:
            continue
        pv_kwh += max(0.0, entry.pv_estimate) * period_hours / 1000
        house_kwh += max(0.0, entry.house_power) * period_hours / 1000
        ev_wall_kwh += max(0.0, entry.ev_charge_power) * period_hours / 1000
    return (
        usable_battery + pv_kwh - house_kwh - ev_wall_kwh,
        usable_battery,
        pv_kwh,
        house_kwh,
        ev_wall_kwh,
    )


@pyscript_compile
def calculate_ev_floor_reserve(
    detail: list["ForecastEntry"],
    t_now: datetime,
    horizon_hours: float,
    ev_min_energy_kwh: float,
    ev_energy_fallback: float | None,
    ev_charge_efficiency: float,
) -> float:
    """Return the wall kWh needed to keep the EV at/above ev_min_energy_kwh.

    Compares the standing minimum against the EV energy at the end of
    the window, so scheduled in-window charging (already counted in the
    horizon balance) shrinks the reserve instead of double-counting it.
    Falls back to ev_energy_fallback when the trajectory carries no
    usable end state (e.g. forecasts simulated without EV charging).
    """
    try:
        horizon_hours = float(horizon_hours)
        ev_min_energy_kwh = float(ev_min_energy_kwh)
        ev_charge_efficiency = float(ev_charge_efficiency)
        if ev_energy_fallback is not None:
            ev_energy_fallback = float(ev_energy_fallback)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("reserve inputs must be finite numbers")
    for value in (horizon_hours, ev_min_energy_kwh, ev_charge_efficiency):
        if value != value or abs(value) == float("inf"):
            raise ValueError("reserve inputs must be finite numbers")
    if ev_energy_fallback is not None and (
        ev_energy_fallback != ev_energy_fallback
        or abs(ev_energy_fallback) == float("inf")
    ):
        raise ValueError("reserve inputs must be finite numbers")
    if horizon_hours <= 0:
        raise ValueError("horizon_hours must be positive")
    if ev_charge_efficiency <= 0 or ev_charge_efficiency > 1:
        raise ValueError("ev_charge_efficiency must be in (0, 1]")

    window_end = t_now + timedelta(hours=horizon_hours)
    latest: tuple[datetime, float] | None = None
    for entry in detail:
        if not t_now <= entry.period_start < window_end:
            continue
        try:
            value = float(getattr(entry, "ev_energy", None))
        except (TypeError, ValueError):
            continue
        if value != value or abs(value) == float("inf"):
            continue
        if latest is None or entry.period_start >= latest[0]:
            latest = (entry.period_start, value)
    if latest is None:
        if ev_energy_fallback is None:
            return 0.0
        end_energy = ev_energy_fallback
    else:
        end_energy = latest[1]
    return max(0.0, ev_min_energy_kwh - end_energy) / ev_charge_efficiency


@pyscript_compile
def get_spendable_solar_cycle_curve(
    detail: list["ForecastEntry"],
    battery_export_floor: float,
    period_hours: float,
    battery_discharge_efficiency: float = 1.0,
    battery_discharge_allowed: bool = True,
) -> list[float]:
    """Return the non-negative spendable budget at every forecast bucket.

    Each point is evaluated against its own solar-cycle horizon.  This keeps
    the published curve aligned with ``get_spendable_solar_cycle_energy``
    instead of exposing the simulator's longer-running internal accumulator.
    """
    curve = []
    for entry in detail:
        spendable, _, _, _ = get_spendable_solar_cycle_energy(
            detail,
            battery_export_floor,
            entry.period_start,
            period_hours,
            battery_discharge_efficiency,
            battery_discharge_allowed,
        )
        curve.append(spendable)
    return curve


@pyscript_compile
def calculate_ev_adjusted_spendable_energy(
    base_spendable_energy_kwh: float,
    ev_forecast_spendable_energy_kwh: float,
    remaining_ev_energy_kwh: float,
    charge_efficiency: float,
    obligation_due_within_horizon: bool,
    local_ev_wall_energy_before_departure_kwh: float = 0.0,
) -> float:
    """Return local energy left after reserving an imminent EV obligation.

    ``ev_forecast_spendable_energy_kwh`` alone is unsafe: the EV simulation may
    buy most of the charge from the grid and leave battery headroom behind.  A
    grid-funded charge must not turn that remaining headroom into surplus that
    can be sold before the charge has happened.
    """

    try:
        base_spendable_energy_kwh = float(base_spendable_energy_kwh)
        ev_forecast_spendable_energy_kwh = float(
            ev_forecast_spendable_energy_kwh
        )
        remaining_ev_energy_kwh = float(remaining_ev_energy_kwh)
        charge_efficiency = float(charge_efficiency)
        local_ev_wall_energy_before_departure_kwh = float(
            local_ev_wall_energy_before_departure_kwh
        )
    except (TypeError, ValueError, OverflowError):
        raise ValueError("energy inputs must be finite and non-negative")

    for value in (
        base_spendable_energy_kwh,
        ev_forecast_spendable_energy_kwh,
        remaining_ev_energy_kwh,
        charge_efficiency,
        local_ev_wall_energy_before_departure_kwh,
    ):
        if value < 0.0 or value != value or abs(value) == float("inf"):
            raise ValueError("energy inputs must be finite and non-negative")

    if charge_efficiency <= 0 or charge_efficiency > 1:
        raise ValueError("charge_efficiency must be in (0, 1]")

    conservative_forecast_budget_kwh = min(
        base_spendable_energy_kwh,
        ev_forecast_spendable_energy_kwh,
    )
    if not obligation_due_within_horizon:
        return conservative_forecast_budget_kwh

    remaining_ev_wall_energy_kwh = max(0.0, remaining_ev_energy_kwh) / charge_efficiency
    uncovered_ev_wall_energy_kwh = max(
        0.0,
        remaining_ev_wall_energy_kwh
        - max(0.0, local_ev_wall_energy_before_departure_kwh),
    )
    obligation_budget_kwh = max(
        0.0,
        base_spendable_energy_kwh - uncovered_ev_wall_energy_kwh,
    )
    return min(conservative_forecast_budget_kwh, obligation_budget_kwh)


@pyscript_compile
def calculate_local_ev_wall_energy_before_departure(
    base_detail: list["ForecastEntry"],
    ev_detail: list["ForecastEntry"],
    t_start: datetime,
    departure: datetime,
    fallback_period_hours: float,
) -> float:
    """Return forecast EV wall energy supplied without additional grid import.

    Compare the EV replay with the same optional-export-free baseline.  EV load
    that increases grid import is not local energy and must not make battery
    headroom exportable.  EV load supplied by PV, battery, or avoided baseline
    feed-in is local and can legitimately reduce the protected obligation.
    """

    if departure <= t_start:
        return 0.0

    ev_entry_by_start = {entry.period_start: entry for entry in ev_detail}
    local_wall_energy_kwh = 0.0
    for index, base_entry in enumerate(base_detail):
        period_start = base_entry.period_start
        if period_start >= departure:
            break
        ev_entry = ev_entry_by_start.get(period_start)
        if ev_entry is None:
            continue

        if index + 1 < len(base_detail):
            period_end = base_detail[index + 1].period_start
        else:
            period_end = period_start + timedelta(hours=fallback_period_hours)
        effective_start = max(t_start, period_start)
        effective_end = min(departure, period_end)
        duration_hours = max(
            0.0,
            (effective_end - effective_start).total_seconds() / 3600,
        )
        if duration_hours <= 0:
            continue

        incremental_grid_import_w = max(
            0.0,
            ev_entry.power_from_grid - base_entry.power_from_grid,
        )
        local_ev_wall_power_w = max(
            0.0,
            ev_entry.ev_charge_power - incremental_grid_import_w,
        )
        local_wall_energy_kwh += (
            local_ev_wall_power_w * duration_hours / 1000
        )

    return local_wall_energy_kwh


@pyscript_compile
def get_ev_adjusted_spendable_solar_cycle_curve(
    base_detail: list["ForecastEntry"],
    ev_detail: list["ForecastEntry"],
    battery_export_floor: float,
    period_hours: float,
    initial_ev_energy_kwh: float,
    initial_ev_energy_needed_kwh: float,
    next_departure: datetime | None,
    charge_efficiency: float,
    reservation_horizon_hours: float,
    battery_discharge_efficiency: float,
    battery_discharge_allowed: bool = True,
) -> list[float]:
    """Return a curve that reserves the unfinished EV charge until departure."""

    ev_entry_by_start = {entry.period_start: entry for entry in ev_detail}
    curve = []
    for index, base_entry in enumerate(base_detail):
        ev_entry = ev_entry_by_start.get(base_entry.period_start)
        if ev_entry is None:
            curve.append(0.0)
            continue

        base_spendable, _, _, _ = get_spendable_solar_cycle_energy(
            base_detail,
            battery_export_floor,
            base_entry.period_start,
            period_hours,
            battery_discharge_efficiency,
            battery_discharge_allowed,
        )
        ev_spendable, _, _, _ = get_spendable_solar_cycle_energy(
            ev_detail,
            battery_export_floor,
            ev_entry.period_start,
            period_hours,
            battery_discharge_efficiency,
            battery_discharge_allowed,
        )
        projected_ev_charge_kwh = max(
            0.0,
            ev_entry.ev_energy - initial_ev_energy_kwh,
        )
        remaining_ev_energy_kwh = max(
            0.0,
            initial_ev_energy_needed_kwh - projected_ev_charge_kwh,
        )
        if index + 1 < len(base_detail):
            point_time = base_detail[index + 1].period_start
        else:
            point_time = base_entry.period_start + timedelta(hours=period_hours)
        obligation_due_within_horizon = bool(
            next_departure is not None
            and base_entry.period_start < next_departure
            and next_departure
            <= base_entry.period_start
            + timedelta(hours=reservation_horizon_hours)
        )
        local_ev_wall_energy_before_departure_kwh = 0.0
        if obligation_due_within_horizon:
            local_ev_wall_energy_before_departure_kwh = (
                calculate_local_ev_wall_energy_before_departure(
                    base_detail,
                    ev_detail,
                    point_time,
                    next_departure,
                    period_hours,
                )
            )
        curve.append(
            calculate_ev_adjusted_spendable_energy(
                base_spendable_energy_kwh=base_spendable,
                ev_forecast_spendable_energy_kwh=ev_spendable,
                remaining_ev_energy_kwh=remaining_ev_energy_kwh,
                charge_efficiency=charge_efficiency,
                obligation_due_within_horizon=obligation_due_within_horizon,
                local_ev_wall_energy_before_departure_kwh=(
                    local_ev_wall_energy_before_departure_kwh
                ),
            )
        )
    return curve


@pyscript_compile
def parse_full_schedule(
    schedule_data: dict[str, list[dict[str, Any]]],
    default_required_soc: float,
    t_now=None,
    preserve_recent_arrival: bool = False,
) -> list["EVScheduleEntry"]:
    # Shared implementation lives in modules.energy_core so the live EV
    # charging app and the forecast stay in lockstep.  ``now`` is a Pyscript
    # runtime helper and cannot safely be resolved from inside a
    # ``@pyscript_compile`` function.  Callers must freeze the time before
    # entering this deterministic parser.
    return parse_ev_schedule_entries(
        schedule_data,
        default_required_soc,
        t_now,
        EVScheduleEntry,
        preserve_recent_arrival,
    )


def get_ev_schedule(t_now=None, preserve_recent_arrival: bool = False):
    if t_now is None:
        t_now = now()
    ev_schedule = (schedule.get_schedule(entity_id="schedule.tesla_planned_drives") or {}).get(
        "schedule.tesla_planned_drives", {}
    )
    ev_required_soc = get(EV.required_soc, 80)
    return parse_full_schedule(
        ev_schedule,
        default_required_soc=ev_required_soc,
        t_now=t_now,
        preserve_recent_arrival=preserve_recent_arrival,
    )


@time_trigger
@time_trigger("cron(*/2 * * * *)")
@state_trigger(Grid.power_setpoint_target)
def forecast_surplus():
    task.unique("forecast_surplus", kill_me=True)
    epex_prices = get_attr(ElectricityPrices.epex_forecast_prices, "data", [])
    t_now = now()
    ev_schedule = get_ev_schedule(t_now, preserve_recent_arrival=True)
    _, next_departure = get_ongoing_and_next_drive(ev_schedule, t_now)
    # The simulation spans 5 days for the energy balance; the solar-cycle
    # budget keeps its 80-hour scope via the *_80h slices below.
    t_end = t_now + timedelta(hours=120)
    if next_departure:
        t_end = max(t_end, next_departure.end + timedelta(hours=6))

    setpoint = get(Grid.power_setpoint_basis, -20)
    battery_capacity = get(Battery.capacity, 1337)
    battery_energy = get(Battery.energy, 1337)
    battery_energy_updated_at = get_attr(Battery.energy, "last_updated")
    # Battery.power is in W; an idle battery's energy reading legitimately
    # sits still (HA only refreshes last_updated on change).
    battery_energy_measurement_fresh = is_battery_energy_measurement_usable(
        t_now,
        battery_energy_updated_at,
        battery_energy,
        battery_capacity,
        get(Battery.soc, -1),
        get(Battery.power, 1337),
        get_attr(Battery.power, "last_updated"),
        max_age_seconds=LIVE_BATTERY_ENERGY_MAX_MEASUREMENT_AGE_SECONDS,
    )
    current_surplus = get(House.energy_surplus, 0)
    previous_ev_adjusted_surplus = get(House.energy_surplus_after_ev_charging, 0)
    ev_energy = get(EV.energy, 0)
    if (
        1337 in (battery_capacity, battery_energy)
        or not are_battery_energy_values_valid(battery_capacity, battery_energy)
        or not battery_energy_measurement_fresh
    ):
        log.warning("Battery capacity or energy invalid or stale; cannot calculate surplus")
        return
    if not are_battery_energy_values_valid(EVConst.ev_capacity, ev_energy):
        log.warning("EV energy invalid; cannot calculate EV-adjusted surplus")
        return
    configured_battery_charge_limit_w = resolve_directional_power_limit_w(
        get(Battery.charge_limit, -1),
        6600.0,
        6600.0,
    )
    hardware_discharge_limit_w = resolve_directional_power_limit_w(
        get_planned_discharge_limit_w(-1),
        8000.0,
        8000.0,
    )
    battery_discharge_allowed = hardware_discharge_limit_w > 0.0
    reserve_soc = get_reserve_soc()
    minimal_soc = get(Automation.minimal_soc, reserve_soc)
    operational_battery_floor = get_battery_export_floor(battery_capacity, reserve_soc, minimal_soc)
    max_battery_power_target = resolve_directional_power_limit_w(
        get_attr(Grid.power_setpoint_target, "max_battery_power_target", 4000),
        4000.0,
        8000.0,
    )
    unified_scheduler_active = get_attr(Grid.power_setpoint_target, "unified_scheduler_active", False)
    forecast_max_setpoint = get(Grid.max_setpoint, -20) if unified_scheduler_active else -20
    optional_export_power_schedule = (
        get_attr(Grid.power_setpoint_target, "optional_export_power_schedule", {})
        if unified_scheduler_active
        else None
    )
    unified_export_budget_kwh = (
        get_attr(Grid.power_setpoint_target, "scheduled_export_energy_kwh", 0)
        if unified_scheduler_active
        else None
    )
    headroom_constraint_active = get_attr(Grid.power_setpoint_target, "headroom_constraint_active", False)
    headroom_schedule = (
        get_attr(Grid.power_setpoint_target, "headroom_schedule", {})
        if headroom_constraint_active and not unified_scheduler_active
        else None
    )

    # Forecasted house demand must not also be embedded in the forecast floor.
    # The forecast consumes that energy explicitly, so using minimal_soc here
    # reserves the same demand twice and understates spendable energy.
    forecast_battery_floor = get_battery_export_floor(
        battery_capacity,
        reserve_soc,
        reserve_soc,
    )

    # Keep the unbalanced-cell low-PV reserve for optional loads. The spendable
    # horizon ends at an overnight/early-morning trough, where that reserve is
    # active even though it is deliberately excluded from minimal_soc above.
    battery_cells_balanced = get(Battery.cells_balanced, False)
    surplus_reserve_soc = max(reserve_soc, 0 if battery_cells_balanced else 15)
    surplus_battery_floor = get_battery_export_floor(
        battery_capacity,
        surplus_reserve_soc,
        surplus_reserve_soc,
    )


    pv_forecast = get_pv_forecast_with_prices(t_start=t_now, t_end=t_end, epex_prices=epex_prices)
    if not pv_forecast or len(pv_forecast) < 2:
        log.warning("Not enough PV forecast data to calculate surplus")
        return
    period_hours = (pv_forecast[1].period_start - pv_forecast[0].period_start).total_seconds() / 3600

    battery_floor_schedule = None
    if unified_scheduler_active:
        daily_house_power = get(House.daily_average_power, 0)
        nightly_house_power = get(House.nightly_average_power, 0)
        reserve_slots = build_reserve_trajectory_slots(
            pv_forecast,
            t_now,
            day_house_power_w=daily_house_power,
            night_house_power_w=nightly_house_power,
            pv_power_scale=1.0,
            battery_cells_balanced=battery_cells_balanced,
        )
        battery_floor_schedule = build_reserve_energy_schedule(
            reserve_slots,
            battery_capacity_kwh=battery_capacity,
            uncertainty_margin_kwh=EXPORT_BASE_UNCERTAINTY_MARGIN_KWH,
            house_load_margin_fraction=EXPORT_HOUSE_LOAD_MARGIN_FRACTION,
            pv_confidence_factor=EXPORT_PV_CONFIDENCE_FACTOR,
            discharge_efficiency=HOME_BATTERY_DISCHARGE_EFFICIENCY,
        )

    # Shared inputs for the four simulation passes below.  Each pass overrides
    # only the dampening, EV participation, and budget knobs that define it.
    base_forecast_kwargs = dict(
        forecast=pv_forecast,
        battery_capacity=battery_capacity,
        battery_energy=battery_energy,
        setpoint=setpoint,
        max_battery_power_target=max_battery_power_target,
        battery_charge_limit=configured_battery_charge_limit_w,
        headroom_schedule=headroom_schedule,
        grid_target_schedule=None,
        battery_floor_schedule=battery_floor_schedule,
        max_setpoint=forecast_max_setpoint,
    )

    forecast_no_ev = forecast(
        **base_forecast_kwargs,
        forecast_dampening=1,
        with_ev_charging=False,
        battery_min_energy=forecast_battery_floor,
        optional_export_power_schedule=optional_export_power_schedule,
        battery_export_budget_kwh=unified_export_budget_kwh,
        surplus_energy=current_surplus,
    )

    # Calculate the variable-load budget from a separate conservative baseline.
    # Starting this forecast with zero surplus prevents a feedback loop where a
    # previously reported budget authorizes battery export which is then counted
    # again as newly available energy.  Forecast PV is reduced by 20% and only
    # PV that still spills after charging the battery is added to the budget.
    spendable_forecast = forecast(
        **base_forecast_kwargs,
        forecast_dampening=0.8,
        with_ev_charging=False,
        battery_min_energy=forecast_battery_floor,
        optional_export_power_schedule={},
        battery_export_budget_kwh=0,
        surplus_energy=0,
    )

    min_battery_energy = min([el.battery_energy for el in forecast_no_ev.detail] or [0])
    reserve_energy = reserve_soc / 100 * battery_capacity

    next_local_min_battery_energy = battery_energy
    t_next_local_min = t_now
    if forecast_no_ev.detail:
        next_local_min_battery_energy = forecast_no_ev.detail[0].battery_energy
        t_next_local_min = forecast_no_ev.detail[0].period_start
        previous_battery_energy = next_local_min_battery_energy
        for el in forecast_no_ev.detail[1:]:
            if el.battery_energy <= next_local_min_battery_energy:
                next_local_min_battery_energy = el.battery_energy
                t_next_local_min = el.period_start
            elif el.battery_energy > previous_battery_energy + 0.05:
                break
            previous_battery_energy = el.battery_energy

    cycle_scope_end = t_now + timedelta(hours=80)
    spendable_detail_80h = [
        entry for entry in spendable_forecast.detail if entry.period_start < cycle_scope_end
    ]
    surplus, battery_headroom_until_trough, conservative_pv_spill, spendable_horizon = (
        get_spendable_solar_cycle_energy(
            spendable_detail_80h,
            surplus_battery_floor,
            t_now,
            period_hours,
            HOME_BATTERY_DISCHARGE_EFFICIENCY,
            battery_discharge_allowed,
        )
    )
    spendable_surplus_curve = get_spendable_solar_cycle_curve(
        spendable_detail_80h,
        surplus_battery_floor,
        period_hours,
        HOME_BATTERY_DISCHARGE_EFFICIENCY,
        battery_discharge_allowed,
    )

    forecast_today = [el for el in forecast_no_ev.detail if el.period_start.day == t_now.day]
    feedin_today = sum([max(0, el.feedin - 20) for el in forecast_today]) * period_hours / 1000
    pv_feedin_today = sum([max(0, el.pv_feedin - 20) for el in forecast_today]) * period_hours / 1000

    detail_without_ev_vectorized = {
        k: [getattr(el, k) for el in forecast_no_ev.detail] for k in ForecastEntry.__annotations__.keys()
    }
    detail_without_ev_vectorized["surplus"] = spendable_surplus_curve

    set_energy_surplus(
        House.energy_surplus,
        surplus,
        battery_headroom=battery_headroom_until_trough,
        conservative_pv_spill=round(conservative_pv_spill, 2),
        protected_battery_floor=round(surplus_battery_floor, 2),
        forecast_battery_floor=round(forecast_battery_floor, 2),
        operational_battery_floor=round(operational_battery_floor, 2),
        forecast_pv_factor=0.8,
        spendable_horizon=spendable_horizon,
        calculated_at=t_now,
        battery_energy_at_calculation=battery_energy,
    )
    set_energy_surplus(
        House.battery_headroom_until_trough,
        battery_headroom_until_trough,
        friendly_name="Battery Headroom Until Trough",
    )
    set_state(
        House.energy_forecast,
        round(forecast_no_ev.detail[-1].battery_energy, 2),
        **energy_kwh_attributes,
        detail=detail_without_ev_vectorized,
    )

    def publish_5_day_balance(detail):
        (
            balance_5_day,
            balance_usable_battery_kwh,
            balance_pv_120h_kwh,
            balance_house_120h_kwh,
            balance_ev_wall_120h_kwh,
        ) = calculate_horizon_energy_balance(
            detail,
            t_now,
            120.0,
            battery_energy,
            surplus_battery_floor,
            period_hours,
            HOME_BATTERY_DISCHARGE_EFFICIENCY,
        )
        ev_floor_reserve_kwh = calculate_ev_floor_reserve(
            detail,
            t_now,
            120.0,
            get(EV.required_soc, 80) / 100 * EVConst.ev_capacity,
            ev_energy,
            EVConst.charge_efficiency,
        )
        balance_5_day -= ev_floor_reserve_kwh
        set_energy_surplus(
            House.energy_balance_5_day,
            balance_5_day,
            usable_battery_kwh=round(balance_usable_battery_kwh, 3),
            pv_120h_kwh=round(balance_pv_120h_kwh, 3),
            house_120h_kwh=round(balance_house_120h_kwh, 3),
            ev_wall_120h_kwh=round(balance_ev_wall_120h_kwh, 3),
            ev_floor_reserve_kwh=round(ev_floor_reserve_kwh, 3),
            horizon_hours=120.0,
            horizon_end=(t_now + timedelta(hours=120)).isoformat(),
            calculated_at=t_now,
            battery_energy_at_calculation=battery_energy,
        )

    if ev_schedule:
        forecast_ev_charging_enabled = bool(
            get(Automation.auto_ev_charging, False)
            or get(Charger.force_charge, False)
            or get(EV.is_charging, False)
        )
        drive_ongoing_for_precharge, _ = get_ongoing_and_next_drive(ev_schedule, t_now)
        recent_arrival_for_precharge = None
        if drive_ongoing_for_precharge is None:
            completed_drives = [
                event
                for event in ev_schedule
                if event.end <= t_now
                and t_now - event.end <= timedelta(hours=EV_EXPORT_RESERVATION_HORIZON_HOURS)
            ]
            if completed_drives:
                recent_arrival_for_precharge = max(completed_drives, key=lambda event: event.end)
        arrival_precharge_enabled = bool(
            forecast_ev_charging_enabled
            and get(EV.able_to_charge_on_arrival, False)
            and (
                drive_ongoing_for_precharge is not None
                or recent_arrival_for_precharge is not None
            )
        )
        precharge_arrival_time = (
            drive_ongoing_for_precharge.end
            if drive_ongoing_for_precharge is not None
            else recent_arrival_for_precharge.end
            if recent_arrival_for_precharge is not None
            else None
        )
        arrival_precharge_budget_kwh = max(
            0.0,
            battery_energy - surplus_battery_floor - 1.0,
        )
        # Re-run the conservative, optional-export-free baseline with EV loads
        # included.  Start its policy accumulator at zero; a previously
        # published surplus must not authorize optional consumption here.
        spendable_forecast_with_ev = forecast(
            **base_forecast_kwargs,
            forecast_dampening=0.8,
            with_ev_charging=forecast_ev_charging_enabled,
            ev_schedule=ev_schedule,
            battery_min_energy=surplus_battery_floor,
            optional_export_power_schedule={},
            battery_export_budget_kwh=0,
            surplus_energy=0,
            ev_energy=ev_energy,
            allow_battery_precharge=arrival_precharge_enabled,
            precharge_arrival_time=precharge_arrival_time,
            ev_battery_precharge_budget_kwh=arrival_precharge_budget_kwh,
        )
        # Publish the same conservative PV confidence used by the spendable
        # budget.  The dashboard must not promise an EV start that live
        # measured headroom is unlikely to sustain.
        forecast_with_ev = forecast(
            **base_forecast_kwargs,
            forecast_dampening=0.8,
            with_ev_charging=forecast_ev_charging_enabled,
            ev_schedule=ev_schedule,
            battery_min_energy=surplus_battery_floor,
            optional_export_power_schedule=optional_export_power_schedule,
            battery_export_budget_kwh=unified_export_budget_kwh,
            surplus_energy=surplus,
            ev_energy=ev_energy,
            # Use the prior conservative publication to avoid a circular
            # forecast.  The controller applies a further 3 kWh safety reserve,
            # so a slightly old value fails gradually instead of overcommitting.
            ev_battery_support_energy_kwh=previous_ev_adjusted_surplus,
            allow_battery_precharge=arrival_precharge_enabled,
            precharge_arrival_time=precharge_arrival_time,
            ev_battery_precharge_budget_kwh=arrival_precharge_budget_kwh,
        )

        min_battery_energy = min([el.battery_energy for el in forecast_with_ev.detail] or [0])
        active_drive, _ = get_ongoing_and_next_drive(ev_schedule, t_now)
        active_drive_forecast_summary = ""
        if active_drive:
            charger_ready = get(Charger.ready, False)
            charger_ready_since = get_attr(Charger.ready, "last_changed")
            is_charging_ev = get(EV.is_charging, False)
            returned_early = is_vehicle_present_during_active_drive(
                active_drive,
                charger_ready,
                charger_ready_since,
                is_charging_ev,
            )
            active_drive_entries = [
                entry
                for entry in forecast_with_ev.detail
                if active_drive.start <= entry.period_start < active_drive.end
            ]
            if active_drive_entries:
                active_drive_end_energy = active_drive_entries[-1].ev_energy
                active_drive_forecast_summary = (
                    f"Active EV drive {active_drive.start} -> {active_drive.end}: "
                    f"forecast {ev_energy:.2f} -> {active_drive_end_energy:.2f} kWh "
                    f"(use {max(0, ev_energy - active_drive_end_energy):.2f} kWh), "
                    f"returned early: {returned_early}"
                )
        _, next_departure = get_ongoing_and_next_drive(ev_schedule, t_now)
        departure_forecast_summary = "No future EV departure in the forecast horizon"
        if next_departure:
            departure_entries = [
                entry for entry in forecast_with_ev.detail if entry.period_start < next_departure.start
            ]
            required_departure_soc = next_departure.required_soc or get(EV.required_soc, 80)
            required_departure_energy = required_departure_soc / 100 * EVConst.ev_capacity
            if departure_entries:
                departure_ev_energy = departure_entries[-1].ev_energy
                departure_ev_soc = departure_ev_energy / EVConst.ev_capacity * 100
                departure_shortfall = max(0, required_departure_energy - departure_ev_energy)
                planned_charge_energy = sum(
                    [
                        entry.ev_charge_power * period_hours / 1000 * EVConst.charge_efficiency
                        for entry in departure_entries
                    ]
                )
                first_charge_entry = next(
                    iter([entry for entry in departure_entries if entry.ev_charge_power > 100]),
                    None,
                )
                first_charge_time = None
                if first_charge_entry:
                    first_charge_time = first_charge_entry.period_start + timedelta(
                        minutes=first_charge_entry.ev_charge_start_delay_minutes
                    )
                departure_forecast_summary = (
                    f"EV departure {next_departure.start}: required {required_departure_soc:.2f}% "
                    f"({required_departure_energy:.2f} kWh), forecast {departure_ev_soc:.2f}% "
                    f"({departure_ev_energy:.2f} kWh), shortfall {departure_shortfall:.2f} kWh, "
                    f"planned charge {planned_charge_energy:.2f} kWh, "
                    f"first charge {first_charge_time if first_charge_time else 'none'}"
                )
        horizon_ev_shortfall_kwh, horizon_ev_shortfall_departure = (
            first_uncovered_departure_need(
                forecast_with_ev.detail,
                ev_schedule,
                t_now,
                EV_EXPORT_RESERVATION_HORIZON_HOURS,
                get(EV.required_soc, 80),
                EVConst.ev_capacity,
            )
        )
        horizon_ev_shortfall_summary = (
            "no uncovered EV departure in the reservation horizon"
        )
        if horizon_ev_shortfall_departure is not None:
            horizon_ev_shortfall_summary = (
                f"EV horizon reservation {horizon_ev_shortfall_kwh:.2f} kWh "
                f"for departure {horizon_ev_shortfall_departure}"
            )
        spendable_ev_detail_80h = [
            entry
            for entry in spendable_forecast_with_ev.detail
            if entry.period_start < cycle_scope_end
        ]
        ev_forecast_spendable_energy, _, _, _ = get_spendable_solar_cycle_energy(
            spendable_ev_detail_80h,
            surplus_battery_floor,
            t_now,
            period_hours,
            HOME_BATTERY_DISCHARGE_EFFICIENCY,
            battery_discharge_allowed,
        )
        ev_energy_needed = max(0.0, float(get(EV.energy_needed, 0)))
        ev_obligation_due_within_horizon = bool(
            forecast_ev_charging_enabled
            and next_departure is not None
            and t_now < next_departure.start
            and next_departure.start
            <= t_now + timedelta(hours=EV_EXPORT_RESERVATION_HORIZON_HOURS)
        )
        local_ev_wall_energy_before_departure_kwh = 0.0
        if ev_obligation_due_within_horizon:
            local_ev_wall_energy_before_departure_kwh = (
                calculate_local_ev_wall_energy_before_departure(
                    spendable_forecast.detail,
                    spendable_forecast_with_ev.detail,
                    t_now,
                    next_departure.start,
                    period_hours,
                )
            )
        ev_wall_energy_required = (
            ev_energy_needed / EVConst.charge_efficiency
            if ev_obligation_due_within_horizon
            else 0.0
        )
        surplus_after_ev_charging = calculate_ev_adjusted_spendable_energy(
            base_spendable_energy_kwh=surplus,
            ev_forecast_spendable_energy_kwh=ev_forecast_spendable_energy,
            remaining_ev_energy_kwh=ev_energy_needed,
            charge_efficiency=EVConst.charge_efficiency,
            obligation_due_within_horizon=ev_obligation_due_within_horizon,
            local_ev_wall_energy_before_departure_kwh=(
                local_ev_wall_energy_before_departure_kwh
            ),
        )
        spendable_surplus_curve_with_ev = get_ev_adjusted_spendable_solar_cycle_curve(
            spendable_detail_80h,
            spendable_ev_detail_80h,
            surplus_battery_floor,
            period_hours,
            ev_energy,
            ev_energy_needed,
            next_departure.start if forecast_ev_charging_enabled and next_departure else None,
            EVConst.charge_efficiency,
            EV_EXPORT_RESERVATION_HORIZON_HOURS,
            HOME_BATTERY_DISCHARGE_EFFICIENCY,
            battery_discharge_allowed,
        )
        forecast_grid_import_kwh = 0.0
        forecast_grid_import_windows = []
        for detail_entry in forecast_with_ev.detail:
            import_power_w = max(0.0, detail_entry.power_from_grid)
            if import_power_w > 100.0:
                forecast_grid_import_kwh += import_power_w * period_hours / 1000
                if len(forecast_grid_import_windows) < 6:
                    forecast_grid_import_windows.append(
                        f"{detail_entry.period_start} {import_power_w:.0f}W "
                        f"(batt {detail_entry.battery_energy:.1f}kWh)"
                    )
        forecast_grid_import_summary = (
            "; ".join(forecast_grid_import_windows)
            if forecast_grid_import_windows
            else "none"
        )
        log.warning(
            f"""\n#################### Forecast Surplus  #####################\n
            Forecast grid import total: {forecast_grid_import_kwh:.2f} kWh; largest windows: {forecast_grid_import_summary}
            Reserve battery energy: {reserve_energy:.2f} kWh ({reserve_soc:.2f}% reserve SOC)
            Forecast battery floor: {forecast_battery_floor:.2f} kWh
            Protected surplus floor: {surplus_battery_floor:.2f} kWh
            Operational battery floor: {operational_battery_floor:.2f} kWh
            Next local min battery: {next_local_min_battery_energy:.2f} kWh
            Battery headroom until trough: {battery_headroom_until_trough:.2f} kWh
            Conservative PV spill until trough: {conservative_pv_spill:.2f} kWh
            Spendable horizon: {spendable_horizon}
            Min battery: {min_battery_energy:.2f} kWh
            Final battery energy: {forecast_no_ev.detail[-1].battery_energy:.2f} kWh
            Surplus: {surplus:.2f} kWh
            Surplus after EV charging: {surplus_after_ev_charging:.2f} kWh
            EV energy obligation: {ev_energy_needed:.2f} kWh ({ev_wall_energy_required:.2f} kWh at wall)
            Forecast local EV wall energy before departure: {local_ev_wall_energy_before_departure_kwh:.2f} kWh
            EV obligation due within reservation horizon: {ev_obligation_due_within_horizon}
            EV charging controller enabled: {forecast_ev_charging_enabled}
            {active_drive_forecast_summary}
            {departure_forecast_summary}
            {horizon_ev_shortfall_summary}
            last entry date: {forecast_no_ev.detail[-1].period_start if len(forecast_no_ev.detail) > 0 else "n/a"}
            total feedin: {feedin_today:.2f} kWh
            total pv feedin: {pv_feedin_today:.2f} kWh
            \n#################### Forecast Surplus End #####################
            """
        )
        detail_with_ev_vectorized = {
            k: [getattr(el, k) for el in forecast_with_ev.detail] for k in ForecastEntry.__annotations__.keys()
        }
        # ``ForecastEntry.surplus`` remains an internal simulator accumulator
        # used for policy decisions.  Publish the same-horizon spendable curve
        # instead; the raw accumulator includes ordinary house demand over the
        # full 80-hour simulation and is not an energy-surplus contract.
        detail_with_ev_vectorized["surplus"] = spendable_surplus_curve_with_ev

        set_state(
            House.energy_forecast_with_ev,
            round(forecast_with_ev.detail[-1].battery_energy, 2),
            **energy_kwh_attributes,
            ev_charging_enabled=forecast_ev_charging_enabled,
            forecast_pv_factor=0.8,
            ev_battery_support_budget=round(previous_ev_adjusted_surplus, 3),
            detail=detail_with_ev_vectorized,
        )
        set_energy_surplus(
            House.energy_surplus_after_ev_charging,
            surplus_after_ev_charging,
            base_spendable_energy=round(surplus, 3),
            ev_forecast_spendable_energy=round(ev_forecast_spendable_energy, 3),
            ev_energy_needed=round(ev_energy_needed, 3),
            horizon_ev_shortfall_kwh=round(horizon_ev_shortfall_kwh, 3),
            horizon_ev_shortfall_departure=(
                horizon_ev_shortfall_departure.isoformat()
                if horizon_ev_shortfall_departure is not None
                else None
            ),
            ev_wall_energy_required=round(ev_wall_energy_required, 3),
            local_ev_wall_energy_before_departure=round(
                local_ev_wall_energy_before_departure_kwh,
                3,
            ),
            ev_obligation_due_within_horizon=ev_obligation_due_within_horizon,
            next_departure=next_departure.start if next_departure else None,
            spendable_horizon=spendable_horizon,
            calculated_at=t_now,
            battery_energy_at_calculation=battery_energy,
        )
        publish_5_day_balance(spendable_forecast_with_ev.detail)

    else:
        set_state(
            House.energy_forecast_with_ev,
            round(forecast_no_ev.detail[-1].battery_energy, 2),
            **energy_kwh_attributes,
            detail=detail_without_ev_vectorized,
        )
        set_energy_surplus(
            House.energy_surplus_after_ev_charging,
            surplus,
            base_spendable_energy=round(surplus, 3),
            ev_forecast_spendable_energy=round(surplus, 3),
            ev_energy_needed=0.0,
            ev_wall_energy_required=0.0,
            local_ev_wall_energy_before_departure=0.0,
            ev_obligation_due_within_horizon=False,
            next_departure=None,
            spendable_horizon=spendable_horizon,
            calculated_at=t_now,
            battery_energy_at_calculation=battery_energy,
        )
        publish_5_day_balance(spendable_forecast.detail)


@pyscript_compile
def define_interfaces():
    def fix_entry_repr(entry_repr):
        entry_repr = (
            entry_repr.replace(", tzinfo=zoneinfo.ZoneInfo(key='Europe/Berlin')", "")
            .replace("datetime.datetime", "")
            .replace("define_interfaces.<locals>.", "")
            .replace(", 0), ", ",  0), ")
        )
        import re

        entry_repr = re.sub(r"(\d+).[\d]+", r"\1", entry_repr)
        return entry_repr

    @dataclass
    class EVScheduleEntry:
        start: datetime
        end: datetime
        distance: float | None = None
        required_soc: float | None = None

    @dataclass
    class ForecastEntry:
        period_start: str
        pv_estimate: float
        battery_energy: float
        house_power: float
        setpoint: int
        power_draw: float
        free_capacity: float
        accumulated_energy: float
        feedin: float
        pv_feedin: float
        epex_price: float
        electricity_price: float
        battery_power: float
        setpoint_spread: float
        ev_energy: float
        ev_charge_power: float
        ev_charge_start_delay_minutes: float
        ev_battery_support_power: float
        excess_target: float
        surplus: float
        power_from_grid: float

        def format(self):
            return fix_entry_repr(str(self))[len(type(self).__name__) + 1 : -1]

    @dataclass
    class ForecastResult:
        setpoint: int
        min_bat: float
        t_min_bat: datetime
        max_bat: float
        t_max_bat: datetime
        max_feedin: float
        t_max_feedin: datetime
        max_pv_feedin: float
        t_max_pv_feedin: datetime
        setpoint_spread: float
        prices_mean: float
        prices_std: float
        max_battery_power_target: float
        detail: list[ForecastEntry]

        def format(self):
            return fix_entry_repr(str(self))

    @dataclass
    class PVForecastWithPrices:
        period_start: datetime
        pv_estimate: float
        price_per_kwh: float = 0

    return EVScheduleEntry, ForecastResult, ForecastEntry, PVForecastWithPrices


EVScheduleEntry, ForecastResult, ForecastEntry, PVForecastWithPrices = define_interfaces()


@pyscript_compile
def gaussian(x, mean, std):
    return exp(-0.5 * ((x - mean) / std) ** 2) / (std * sqrt(2 * pi))


@pyscript_compile
def map_setpoint(
    setpoint,
    price,
    prices_mean,
    prices_std,
    battery_energy,
    battery_min_limit,
    pv_power,
    house_power,
    max_feedin=4000,
    setpoint_spread=1,
    min_setpoint=-20,
    max_setpoint=-20,
    max_battery_power_target=4000,
    surplus_energy: float | None = None,
):
    price = price * 100
    prices_mean = prices_mean * 100
    prices_std = prices_std * 100

    prices_std = max(5, prices_std)

    if price > prices_mean + prices_std:
        price = prices_mean + prices_std

    mean = prices_mean + prices_std
    std = max(1e-5, setpoint_spread) ** 0.5 * prices_std

    max_prob = gaussian(0, 0, std)

    # print(f"p: {max_prob:.2f} price: {price:.2f} mean: {mean:.2f} std: {std:.2f} spread {setpoint_spread:.2f}")
    # print(f"({std} * {sqrt(2 * pi)})")

    gaus_prob = gaussian(price, mean, std) / max_prob

    new_setpoint = gaus_prob * setpoint

    surplus_pv = max(0, pv_power - house_power - max_battery_power_target)
    pv_only_setpoint = get_pv_only_setpoint(
        pv_power,
        house_power,
        max_battery_power_target,
        neutral_setpoint=min_setpoint,
    )

    if surplus_pv > 0 and setpoint < max_setpoint:
        new_setpoint = min(new_setpoint, -surplus_pv)

    # Scale only the battery-to-grid portion of the target. At the protected
    # floor, retain a neutral target or unavoidable PV export, but never ask the
    # battery to export. The factor is monotonic and cannot rise below the floor.
    battery_headroom_factor = clip((battery_energy - battery_min_limit) / 2, 0, 1) ** 4
    if surplus_energy is not None:
        battery_headroom_factor = min(battery_headroom_factor, clip(surplus_energy / 2, 0, 1) ** 4)

    if new_setpoint < pv_only_setpoint:
        new_setpoint = pv_only_setpoint + (new_setpoint - pv_only_setpoint) * battery_headroom_factor

    return max(-(max_feedin + surplus_pv), min(min_setpoint, new_setpoint))


@pyscript_compile
def calculate_legacy_surplus_fallback(
    forecast: list[PVForecastWithPrices],
    battery_energy: float,
    daily_power: float,
    nightly_power: float,
    forecast_dampening: float,
) -> float:
    """Return an order-independent fallback budget for legacy callers."""

    if len(forecast) < 2:
        return 0.0
    period_hours = (
        forecast[1].period_start - forecast[0].period_start
    ).total_seconds() / 3600
    net_forecast_demand_kwh = 0.0
    for entry in forecast:
        house_power = (
            daily_power if 7 < entry.period_start.hour < 19 else nightly_power
        )
        pv_power = entry.pv_estimate * forecast_dampening * 1000
        net_forecast_demand_kwh += (
            (house_power - pv_power) * period_hours / 1000
        )

    required_energy_kwh = 5 + max(0.0, net_forecast_demand_kwh)
    return max(0.0, battery_energy - required_energy_kwh)


def _or_get(entity_id, local_value, default):
    """Return an explicit caller value, falling back to the HA state default."""

    return local_value if local_value is not None else get(entity_id, default)


def forecast(
    forecast: list[PVForecastWithPrices],
    setpoint: float,
    battery_capacity: int,
    min_feedin_price: int = 0,
    forecast_dampening=0.8,
    battery_energy: float = 2.0,
    setpoint_spread=1,
    battery_min_energy: float = 2,
    battery_charge_limit: float = 6600,
    with_ev_charging=True,
    ev_energy: float | None = None,
    max_battery_power_target: float = 4000,
    max_pv_feedin_target: float = 1000,
    max_feedin: float | None = None,
    headroom_schedule: dict[str, float] | None = None,
    grid_target_schedule: dict[str, float] | None = None,
    optional_export_power_schedule: dict[str, float] | None = None,
    battery_floor_schedule: dict[str, float] | None = None,
    max_setpoint=-20,
    logging=False,
    ev_schedule: list[EVScheduleEntry] | None = None,
    surplus_energy: float | None = None,
    ev_battery_support_energy_kwh: float = 0,
    ev_battery_precharge_budget_kwh: float = 0,
    allow_battery_precharge: bool = False,
    precharge_arrival_time: datetime | None = None,
    battery_export_budget_kwh: float | None = None,
    daily_power: float | None = None,
    nightly_power: float | None = None,
    ev_required_soc: float | None = None,
    ev_soc: float | None = None,
    is_charging_ev: bool | None = None,
    smart_limiter_active: bool | None = None,
    charge_limit: float | None = None,
    max_charge_price: float | None = None,
    force_charge_switch: bool | None = None,
    min_discharge_price: float | None = None,
    charger_ready: bool | None = None,
    charger_ready_since: datetime | None = None,
    ev_plugged_in: bool | None = None,
    eff_dis: bool | None = None,
    battery_cells_balanced: bool | None = None,
    t_now: datetime | None = None,
    battery_discharge_limit_w: float | None = None,
):
    t_now = t_now if t_now is not None else now()

    daily_power = _or_get(House.daily_average_power, daily_power, 0)  # W
    nightly_power = _or_get(House.nightly_average_power, nightly_power, 0)  # W
    # A sustained discharge ban (price automation, inverter) must reach
    # the simulation: without it the forecast discharges a battery that
    # cannot discharge.  The seconds-long stale-telemetry hold must not: it
    # parks the shared limit input at 0, and a day-long forecast built on
    # that transient zero freezes the battery and imports from the grid.
    # None keeps the legacy plan-only cap, and direct callers without a
    # caller value fall back to the hold-immune read.  The clamp happens
    # here, in uncompiled code, so _forecast() itself stays untouched: it
    # must not gain new cross-function calls.
    if battery_discharge_limit_w is None:
        battery_discharge_limit_w = get_planned_discharge_limit_w(-1)
    max_battery_power_target = limit_forecast_discharge_to_hardware_w(
        max_battery_power_target,
        resolve_directional_power_limit_w(
            battery_discharge_limit_w,
            8000.0,
            8000.0,
        ),
    )

    ev_required_soc = _or_get(EV.required_soc, ev_required_soc, 80)
    ev_soc = _or_get(EV.soc, ev_soc, 100)
    is_charging_ev = _or_get(EV.is_charging, is_charging_ev, False)

    smart_limiter_active = _or_get(Automation.auto_charge_limit, smart_limiter_active, False)
    charge_limit = _or_get(Battery.force_charge_up_to, charge_limit, 0)
    max_charge_price = _or_get(Battery.max_charge_price, max_charge_price, 0)
    force_charge_switch = _or_get(Battery.force_charge_switch, force_charge_switch, False)
    min_discharge_price = (
        min_discharge_price
        if min_discharge_price is not None
        else float(get(Automation.min_discharge_price, default=0))
    )
    charger_ready = _or_get(Charger.ready, charger_ready, False)
    charger_ready_since = charger_ready_since or get_attr(Charger.ready, "last_changed")
    ev_plugged_in = _or_get(EV.plugged_in, ev_plugged_in, False)
    eff_dis = _or_get(Automation.efficient_discharge, eff_dis, False)
    battery_cells_balanced = _or_get(Battery.cells_balanced, battery_cells_balanced, False)
    max_feedin = _or_get(Grid.max_feedin_target, max_feedin, 4000)
    return _forecast(
        forecast=forecast,
        setpoint=setpoint,
        battery_capacity=battery_capacity,
        min_feedin_price=min_feedin_price,
        forecast_dampening=forecast_dampening,
        battery_energy=battery_energy,
        setpoint_spread=setpoint_spread,
        battery_min_energy=battery_min_energy,
        battery_charge_limit=battery_charge_limit,
        with_ev_charging=with_ev_charging,
        ev_energy=ev_energy,
        max_battery_power_target=max_battery_power_target,
        max_pv_feedin_target=max_pv_feedin_target,
        configured_max_feedin=max_feedin,
        headroom_schedule=headroom_schedule,
        grid_target_schedule=grid_target_schedule,
        optional_export_power_schedule=optional_export_power_schedule,
        battery_floor_schedule=battery_floor_schedule,
        max_setpoint=max_setpoint,
        logging=logging,
        ev_schedule=ev_schedule,
        surplus_energy=surplus_energy,
        ev_battery_support_energy_kwh=ev_battery_support_energy_kwh,
        ev_battery_precharge_budget_kwh=ev_battery_precharge_budget_kwh,
        allow_battery_precharge=allow_battery_precharge,
        precharge_arrival_time=precharge_arrival_time,
        battery_export_budget_kwh=battery_export_budget_kwh,
        daily_power=daily_power,
        nightly_power=nightly_power,
        ev_required_soc=ev_required_soc,
        ev_soc=ev_soc,
        is_charging_ev=is_charging_ev,
        smart_limiter_active=smart_limiter_active,
        charge_limit=charge_limit,
        max_charge_price=max_charge_price,
        force_charge_switch=force_charge_switch,
        min_discharge_price=min_discharge_price,
        charger_ready=charger_ready,
        charger_ready_since=charger_ready_since,
        ev_plugged_in=ev_plugged_in,
        eff_dis=eff_dis,
        battery_cells_balanced=battery_cells_balanced,
        t_now=t_now,
    )


@pyscript_compile
def _forecast(
    forecast: list[PVForecastWithPrices],
    setpoint: float,
    battery_capacity: int,
    min_feedin_price: int = 0,
    forecast_dampening=0.8,
    battery_energy: float = 2.0,
    setpoint_spread=1,
    battery_min_energy: float = 2,
    battery_charge_limit: float = 6600,
    with_ev_charging=True,
    ev_energy: float | None = None,
    max_battery_power_target: float = 4000,
    max_pv_feedin_target: float = 1000,
    configured_max_feedin: float = 4000,
    headroom_schedule: dict[str, float] | None = None,
    grid_target_schedule: dict[str, float] | None = None,
    optional_export_power_schedule: dict[str, float] | None = None,
    battery_floor_schedule: dict[str, float] | None = None,
    max_setpoint=-20,
    logging=False,
    ev_schedule: list[EVScheduleEntry] | None = None,
    surplus_energy: float | None = None,
    ev_battery_support_energy_kwh: float = 0,
    ev_battery_precharge_budget_kwh: float = 0,
    allow_battery_precharge: bool = False,
    precharge_arrival_time: datetime | None = None,
    battery_export_budget_kwh: float | None = None,
    daily_power: float | None = None,
    nightly_power: float | None = None,
    ev_required_soc: float | None = None,
    ev_soc: float | None = None,
    is_charging_ev: bool | None = None,
    smart_limiter_active: bool | None = None,
    charge_limit: float | None = None,
    max_charge_price: float | None = None,
    force_charge_switch: bool | None = None,
    min_discharge_price: float | None = None,
    charger_ready: bool | None = None,
    charger_ready_since: datetime | None = None,
    ev_plugged_in: bool | None = None,
    eff_dis: bool | None = None,
    battery_cells_balanced: bool | None = None,
    t_now: datetime | None = None,
):
    if not len(forecast) > 1:
        return None

    forecast_charge_limit_w, forecast_discharge_limit_w = (
        get_forecast_battery_power_limits(
            battery_charge_limit,
            max_battery_power_target,
        )
    )

    prices = [el.price_per_kwh for el in forecast]
    prices_mean = sum(prices) / len(prices) if len(prices) > 0 else 0
    prices_std = sqrt(sum([(p - prices_mean) ** 2 for p in prices]) / len(prices)) if len(prices) > 0 else 0

    if surplus_energy is None:
        surplus_energy = calculate_legacy_surplus_fallback(
            forecast,
            battery_energy,
            daily_power,
            nightly_power,
            forecast_dampening,
        )

    surplus = surplus_energy
    ev_battery_support_budget_remaining_kwh = max(
        0,
        ev_battery_support_energy_kwh,
        ev_battery_precharge_budget_kwh if allow_battery_precharge else 0,
    )
    battery_export_budget_remaining_kwh = battery_export_budget_kwh
    orig_setpoint = setpoint

    smart_charge_limit = 80

    live_ongoing_drive, next_drive = get_ongoing_and_next_drive(ev_schedule, t_now)
    returned_during_current_drive = is_vehicle_present_during_active_drive(
        live_ongoing_drive,
        charger_ready,
        charger_ready_since,
        is_charging_ev,
    )
    vehicle_present_now = bool(ev_plugged_in or charger_ready or is_charging_ev)

    if with_ev_charging:
        assert ev_energy is not None, "ev_energy must be provided if with_ev_charging is True"

    def is_charging_possible(dt, ev_energy, smart_charge_limit):
        if not with_ev_charging:
            return False
        ongoing_drive, _ = get_forecast_drive_context(
            ev_schedule,
            dt,
            live_ongoing_drive,
            returned_during_current_drive,
        )
        return is_forecast_ev_available(
            ev_schedule,
            t_now,
            dt,
            ongoing_drive,
            vehicle_present_now,
        ) and ev_energy < EVConst.ev_capacity * (smart_charge_limit or 101) / 100

    def resolve_period_inverter_mode(
        pv_power, target_soc, current_soc, electricity_price, surplus, battery_headroom_energy, _t_now
    ):
        assert surplus is not None
        new_mode, new_charge_power_limit, new_force_charge_switch_state, reason = get_auto_inverter_mode(
            is_charging_ev,
            surplus,
            battery_headroom_energy,
            pv_power,
            daily_power,
            current_soc,
            target_soc,
            electricity_price,
            min_discharge_price,
            max_charge_price,
            charge_limit,
            force_charge_switch,
            t_now=_t_now,
            enforce_battery_floor=battery_floor_schedule is not None,
            is_low_price=is_low_price(electricity_price),
        )
        return new_mode, new_charge_power_limit, new_force_charge_switch_state, reason

    full_period_minutes = (forecast[1].period_start - forecast[0].period_start).total_seconds() / 60

    accumulated_energy = 0
    max_feedin = max_pv_feedin = 0
    min_forecast_battery = battery_energy
    max_forecast_battery = battery_energy
    t_min_bat, t_max_bat, t_max_feedin, t_max_pv_feedin = t_now, t_now, t_now, t_now
    detail: list[ForecastEntry] = []

    charge_phases, charge_current = 1, 7
    msg = ""
    next_drive_event = None

    for entry, epex_price in zip(forecast, prices):
        start: datetime = entry.period_start
        ongoing_drive = None
        if ev_schedule:
            ongoing_drive, next_drive_event = get_forecast_drive_context(
                ev_schedule,
                start,
                live_ongoing_drive,
                returned_during_current_drive,
            )
            if next_drive_event and next_drive_event.required_soc:
                ev_required_soc = next_drive_event.required_soc
        elapsed = max(t_now - start, timedelta(minutes=0))
        if t_now > start:
            period_minutes = max(0, full_period_minutes - elapsed.total_seconds() / 60)
            if period_minutes == 0:
                continue
        else:
            period_minutes = full_period_minutes
        period_hours = period_minutes / 60

        power_production: float = entry.pv_estimate * forecast_dampening * 1000
        house_power = daily_power if 7 < start.hour < 19 else nightly_power
        period_battery_min_energy = battery_min_energy
        if battery_floor_schedule is not None:
            period_battery_min_energy = max(
                period_battery_min_energy,
                battery_floor_schedule.get(start.isoformat(), period_battery_min_energy),
            )
        low_pv_reserve_soc = get_low_pv_reserve_soc(
            house_power=house_power,
            pv_power=power_production,
            battery_cells_balanced=bool(battery_cells_balanced),
        )
        if low_pv_reserve_soc > 0:
            period_battery_min_energy = max(period_battery_min_energy, battery_capacity * low_pv_reserve_soc / 100 + 1)

        smart_charge_limit = 101
        if next_drive_event:
            smart_charge_limit = _get_ev_smart_charge_limit(next_drive_event.start, start, active_schedule=False)

        ev_energy_needed = _get_ev_energy_needed(ev_required_soc, ev_soc, smart_charge_limit, smart_limiter_active)

        ev_battery_support_power = 0.0
        arrival_precharge_active = bool(
            allow_battery_precharge
            and precharge_arrival_time is not None
            and start >= precharge_arrival_time
            and next_drive_event is not None
            and start < next_drive_event.start
        )
        precharge_energy_needed = 0.0
        if arrival_precharge_active:
            precharge_target_soc = max(ev_required_soc, smart_charge_limit)
            precharge_energy_needed = max(
                0.0,
                (precharge_target_soc - ev_soc) / 100 * EVConst.ev_capacity,
            )
            precharge_power_budget = min(
                3000.0,
                ev_battery_support_budget_remaining_kwh * 1000 / max(period_hours, 1 / 60),
            )
            ev_battery_support_power = max(
                ev_battery_support_power,
                precharge_power_budget,
            )

        could_charge_ev = with_ev_charging and (
            ev_energy_needed > 0 or ev_energy < EVConst.ev_capacity * smart_charge_limit / 100
        )
        could_charge_ev = could_charge_ev and is_charging_possible(start, ev_energy, smart_charge_limit)

        # --- Battery State Estimation (Pre-calculation) ---
        # We calculate a 'tentative' SOC to help the inverter make decisions
        tentative_batt_energy = min(max(0, battery_energy + accumulated_energy), battery_capacity)
        tentative_batt_soc = tentative_batt_energy / battery_capacity * 100

        battery_target_soc = max(5, tentative_batt_soc - (surplus / battery_capacity * 100))
        electricity_price = get_price(hour=start.hour, minute=start.minute)
        low_price = is_low_price(electricity_price)

        excess_target = _get_excess_target(
            battery_target_soc=battery_target_soc,
            battery_soc=tentative_batt_soc,
            ev_required_soc=ev_required_soc,
            ev_is_charging=is_charging_ev,
            next_planned_drive=next_drive_event.start if next_drive_event else None,
            pv_power=power_production,
            ev_soc=ev_soc,
            t_now=start,
            efficient_discharge=eff_dis,
        )

        battery_headroom_energy = max(0, tentative_batt_energy - period_battery_min_energy)
        inverter_mode, charge_power_limit, is_force_charging, inverter_mode_reason = resolve_period_inverter_mode(
            power_production,
            battery_target_soc,
            tentative_batt_soc,
            electricity_price,
            surplus,
            battery_headroom_energy,
            _t_now=start,
        )

        # --- EV Charging Action ---
        ev_charge_power = 0.0
        ev_charge_start_delay_minutes = 0.0
        charge_mode = ChargeMode.idle
        was_charging_ev = is_charging_ev

        if could_charge_ev:
            ev_battery_support_power = calculate_ev_battery_support_power(
                excess_target=excess_target,
                ev_adjusted_surplus_energy=ev_battery_support_budget_remaining_kwh,
            )
            charge_action, new_charge_phases, charge_current, reason, charge_mode = (
                get_settled_simulated_ev_charge_action(
                    next_drive=next_drive_event.start if next_drive_event else None,
                    current_soc=ev_soc,
                    required_soc=ev_required_soc,
                    energy_needed=ev_energy_needed,
                    excess_target=excess_target,
                    surplus_energy=surplus,
                    smart_charge_limit=smart_charge_limit,
                    smart_limiter_active=smart_limiter_active,
                    configured_phases=charge_phases,
                    configured_current=charge_current,
                    is_low_price=low_price,
                    pv_total_power=power_production,
                    house_power=house_power,
                    battery_soc=tentative_batt_soc,
                    is_charging=is_charging_ev,
                    t_now=start,
                    inverter_mode=inverter_mode,
                    battery_force_charge=is_force_charging,
                    battery_floor_soc=period_battery_min_energy / battery_capacity * 100,
                    battery_support_power=ev_battery_support_power,
                    allow_battery_precharge=arrival_precharge_active,
                    precharge_energy_needed=precharge_energy_needed,
                )
            )

            charge_phases = new_charge_phases
            is_charging_ev = charge_action == ChargeAction.on

            if is_charging_ev:
                ev_charge_power = charge_phases * charge_current * 230  # W
                ev_charge_power, ev_charge_start_delay_minutes = (
                    apply_ev_surplus_start_confirmation(
                        ev_charge_power,
                        period_minutes,
                        was_charging_ev,
                        charge_mode,
                    )
                )
                # prevent unnecessary discharging of battety
                battery_target_soc = tentative_batt_soc + 1

        else:
            is_charging_ev, charge_phases, charge_current = False, 1, 6

        optional_export_power = 0.0
        if optional_export_power_schedule is not None:
            optional_export_power = optional_export_power_schedule.get(start.isoformat(), 0.0)
            this_setpoint = max(
                -configured_max_feedin,
                min(max_setpoint, max_setpoint - max(0.0, optional_export_power)),
            )
        elif grid_target_schedule is not None:
            # Unified mode publishes the exact trajectory which this simulator
            # and live control both consume.  A missing key is deliberately
            # neutral; it must never fall back to the legacy price mapper.
            this_setpoint = grid_target_schedule.get(start.isoformat(), max_setpoint)
            this_setpoint = max(-configured_max_feedin, min(max_setpoint, this_setpoint))
        else:
            this_setpoint = map_setpoint(
                setpoint,
                epex_price,
                prices_mean,
                prices_std,
                battery_energy=tentative_batt_energy,
                battery_min_limit=period_battery_min_energy,
                pv_power=power_production,
                house_power=house_power,
                max_feedin=configured_max_feedin,
                setpoint_spread=setpoint_spread,
                max_setpoint=max_setpoint,
                max_battery_power_target=max_battery_power_target,
                surplus_energy=surplus,
            )
            if headroom_schedule is not None:
                scheduled_headroom_w = headroom_schedule.get(start.isoformat())
                if scheduled_headroom_w is not None:
                    this_setpoint = min(this_setpoint, scheduled_headroom_w)

        # --- Apply EV Charging Physics ---
        if is_charging_ev and ev_charge_power > 1:
            added_ev_energy = ev_charge_power * period_hours / 1000 * EVConst.charge_efficiency
            ev_charge_energy_limit = get_ev_charge_energy_limit(
                charge_mode,
                ev_required_soc,
                smart_charge_limit,
            )
            if ev_energy + added_ev_energy > ev_charge_energy_limit:
                added_ev_energy = max(0, ev_charge_energy_limit - ev_energy)
                # Recalculate power to match the energy cap
                ev_charge_power = (
                    (added_ev_energy * 1000) / (period_hours * EVConst.charge_efficiency)
                    if period_hours > 0
                    else 0
                )

            ev_energy += added_ev_energy
            local_ev_headroom_power = max(
                0,
                power_production - house_power - max(0, excess_target),
            )
            actual_battery_support_power = min(
                ev_battery_support_power,
                max(0, ev_charge_power - local_ev_headroom_power),
            )
            ev_battery_support_budget_remaining_kwh = max(
                0,
                ev_battery_support_budget_remaining_kwh
                - actual_battery_support_power * period_hours / 1000,
            )
            ev_battery_support_power = actual_battery_support_power
            free_capacity = (
                ev_charge_energy_limit - ev_energy + battery_capacity - battery_energy
            )

            if optional_export_power_schedule is not None or grid_target_schedule is not None:
                this_setpoint = max_setpoint
                optional_export_power = 0.0
            else:
                this_setpoint = -20

        else:
            free_capacity = battery_capacity - battery_energy
            ev_charge_power = 0

        # --- Driving Physics ---
        if ongoing_drive and ev_energy is not None:
            energy_drained = get_drive_energy_drain(ongoing_drive, period_hours)
            if energy_drained > 0:
                ev_energy -= energy_drained

                if ev_energy < 0:
                    ev_energy = 0

        # assume ev energy is never completely depleted
        if ev_energy is not None:
            ev_energy = max(5, ev_energy)
            ev_soc = (ev_energy / EVConst.ev_capacity) * 100

        # --- Battery & Grid Physics ---

        # 1. Calculate Base Load Balance (House + EV - PV)
        # Positive = Draw needed, Negative = Excess PV
        power_balance_w = house_power - this_setpoint + ev_charge_power - power_production

        # 2. Determine Battery Action based on Inverter Mode & Physics

        battery_power = 0.0  # Positive = Charging, Negative = Discharging
        feedin = 0.0
        pv_feedin = 0.0
        power_from_grid = 0.0

        # Current actual energy before this step
        current_bat_kwh = max(0, min(battery_capacity, battery_energy + accumulated_energy))

        # --- BRANCH A: Force Charging ---
        if inverter_mode in (InverterMode.on, InverterMode.charger_only) and is_force_charging:
            # We enforce charging at the limit
            target_charge_power = min(
                charge_power_limit,
                forecast_charge_limit_w,
            )

            # Physical limit check (Capacity)
            max_intake_wh = (battery_capacity - current_bat_kwh) * 1000
            possible_charge_power = min(
                target_charge_power,
                max_intake_wh
                / period_hours
                / HOME_BATTERY_CHARGE_EFFICIENCY
                if period_hours > 0
                else 0,
            )

            battery_power = possible_charge_power

            # Grid must supply: House + EV + Battery - PV
            power_from_grid = max(0, power_balance_w + battery_power)

            # No feedin during force charge usually
            feedin = 0
            pv_feedin = 0

        # --- BRANCH B: Normal Operation (Surplus/Deficit) ---
        else:
            # Calculate the "Natural" balance without battery
            # Positive = We need power (Deficit)
            # Negative = We have excess power (Surplus)
            natural_deficit_watts = (house_power + ev_charge_power) - power_production

            # Calculate what the battery *should* do to hit the Setpoint
            # Equation: (Load - PV) - Battery_Discharge = Setpoint
            # Therefore: Battery_Discharge = (Load - PV) - Setpoint
            target_battery_discharge_watts = natural_deficit_watts - this_setpoint

            # --- SUB-BRANCH 1: Battery wants to DISCHARGE ---
            if target_battery_discharge_watts > 0:
                # Discharging for house self-consumption is allowed as long as
                # physical headroom exists. Additional battery-to-grid export is
                # only allowed from the reserve-aware surplus budget.
                self_consumption_discharge_watts = max(0, natural_deficit_watts)
                if battery_export_budget_remaining_kwh is None:
                    surplus_export_discharge_watts = (
                        max(0, surplus) * 1000 / period_hours if period_hours > 0 else 0
                    )
                else:
                    surplus_export_discharge_watts = (
                        max(0, battery_export_budget_remaining_kwh) * 1000 / period_hours
                        if period_hours > 0
                        else 0
                    )
                max_allowed_discharge_watts = self_consumption_discharge_watts + surplus_export_discharge_watts

                # Check Mode/SOC constraints
                if (
                    inverter_mode in (InverterMode.off, InverterMode.charger_only)
                    or current_bat_kwh <= period_battery_min_energy
                ):
                    actual_discharge_watts = 0
                else:
                    # Physical Constraints: Limit and Available Energy
                    max_discharge_rate = forecast_discharge_limit_w
                    available_energy_kwh = current_bat_kwh - period_battery_min_energy
                    max_discharge_by_energy = (
                        available_energy_kwh
                        * HOME_BATTERY_DISCHARGE_EFFICIENCY
                        * 1000
                        / period_hours
                        if period_hours > 0
                        else 0
                    )

                    actual_discharge_watts = min(
                        target_battery_discharge_watts,
                        max_discharge_rate,
                        max_discharge_by_energy,
                        max_allowed_discharge_watts,
                    )

                if battery_export_budget_remaining_kwh is not None:
                    battery_to_grid_watts = max(
                        0.0,
                        actual_discharge_watts - self_consumption_discharge_watts,
                    )
                    battery_export_budget_remaining_kwh = max(
                        0.0,
                        battery_export_budget_remaining_kwh
                        - battery_to_grid_watts * period_hours / 1000,
                    )

                battery_power = -actual_discharge_watts  # Negative = Discharging (Energy leaving battery)

            # --- SUB-BRANCH 2: Battery wants to CHARGE ---
            else:
                # target_battery_discharge_watts is negative, so we want to charge
                target_charge_watts = -target_battery_discharge_watts

                # Physical Constraints: Limit and Free Capacity
                max_charge_rate = forecast_charge_limit_w
                free_capacity_kwh = battery_capacity - current_bat_kwh
                max_charge_by_capacity = (
                    free_capacity_kwh
                    * 1000
                    / period_hours
                    / HOME_BATTERY_CHARGE_EFFICIENCY
                    if period_hours > 0
                    else 0
                )

                actual_charge_watts = min(target_charge_watts, max_charge_rate, max_charge_by_capacity)

                battery_power = actual_charge_watts  # Positive = Charging (Energy entering battery)

            # --- FINAL GRID CALCULATION ---
            # Grid = (Load - PV) + Battery_Power (where Charging is positive load, Discharging is negative load)
            # OR simpler: Grid = Natural_Deficit + Battery_Power

            grid_exchange_watts = natural_deficit_watts + battery_power
            pv_feedin = 0
            if grid_exchange_watts > 0:
                power_from_grid = grid_exchange_watts
                feedin = 0
            else:
                power_from_grid = 0
                feedin = -grid_exchange_watts
                pv_feedin = min(feedin, max(0, -natural_deficit_watts))

        # --- Update Accumulator ---
        added_battery_energy = calculate_battery_energy_delta_kwh(
            battery_power,
            period_hours,
            charge_efficiency=HOME_BATTERY_CHARGE_EFFICIENCY,
            discharge_efficiency=HOME_BATTERY_DISCHARGE_EFFICIENCY,
        )
        accumulated_energy += added_battery_energy

        # Calculate final battery state for this step
        new_battery_energy = max(0, min(battery_capacity, battery_energy + accumulated_energy))

        # Track only energy that actually remains available after meeting load and imports.
        # Surplus is an AC-deliverable policy budget.  Charging gains only the
        # energy that can later survive both conversion directions; discharge
        # and grid import are already AC-side quantities.
        surplus_battery_power = battery_power
        if battery_power > 0:
            surplus_battery_power = (
                battery_power
                * HOME_BATTERY_CHARGE_EFFICIENCY
                * HOME_BATTERY_DISCHARGE_EFFICIENCY
            )
        surplus_delta = (
            (surplus_battery_power - power_from_grid)
            * period_hours
            / 1000
        )
        surplus += surplus_delta

        # Update setpoint for next loop context
        setpoint = this_setpoint
        if logging:
            msg += (
                f"\n{start.hour}:{start.minute:02d} - batt {battery_power:.0f}W {new_battery_energy:.0f}kWh setpoint {setpoint:.0f} feedin {feedin:.0f} pv_power {power_production:.0f} "
                f"ev_charge_power {ev_charge_power:.0f}W power_draw {power_balance_w:.0f}W house_power {house_power:.0f}W "
                f"battery_power {battery_power:.0f}W"
            )
        # --- Statistics Tracking ---
        if feedin > max_feedin:
            max_feedin = feedin
            t_max_feedin = start

        if pv_feedin > max_pv_feedin and pv_feedin > max_pv_feedin_target:
            max_pv_feedin = pv_feedin
            t_max_pv_feedin = start

        if new_battery_energy < min_forecast_battery:
            min_forecast_battery = new_battery_energy
            t_min_bat = start

        if new_battery_energy > max_forecast_battery:
            max_forecast_battery = new_battery_energy
            t_max_bat = start + timedelta(minutes=period_minutes)

        if max_feedin > max_pv_feedin_target and (max_pv_feedin_target > 0 or t_max_feedin.day == start.day):
            max_pv_feedin_target = max_feedin
            t_max_feedin = start

        surplus = min(surplus, new_battery_energy - 0.5)  # can't have more surplus than total battery energy

        if logging:
            msg += f"\n{surplus:.1f} surplus += (battery_power - power_from_grid) * period_hours / 1000: ({battery_power} - {power_from_grid}) * {period_hours} / 1000 = {surplus_delta}"

        detail.append(
            ForecastEntry(
                period_start=start,
                pv_estimate=power_production,
                battery_energy=new_battery_energy,
                battery_power=battery_power,
                house_power=house_power,
                setpoint=this_setpoint,
                power_draw=power_balance_w,
                free_capacity=free_capacity,
                accumulated_energy=accumulated_energy,
                feedin=feedin,
                pv_feedin=pv_feedin,
                epex_price=epex_price,
                electricity_price=electricity_price,
                setpoint_spread=setpoint_spread,
                ev_energy=ev_energy,
                ev_charge_power=ev_charge_power,
                ev_charge_start_delay_minutes=ev_charge_start_delay_minutes,
                ev_battery_support_power=ev_battery_support_power,
                excess_target=excess_target,
                surplus=surplus,
                power_from_grid=power_from_grid,
            )
        )

    # for idx, entry in enumerate(detail):
    #     future = detail[idx : int(idx + 16 / period_hours)]
    #     min_bat, t_min_bat = min([(e.battery_energy, e.period_start) for e in future], key=lambda x: x[0])
    #     headroom = max(0, min_bat - battery_min_energy)
    #     feedin_power = sum([e.pv_feedin for e in future if e.period_start < t_min_bat or min_bat > battery_min_energy])
    #     feedin_energy = feedin_power * period_hours / 1000
    #     surplus = min(headroom + feedin_energy, entry.battery_energy - battery_min_energy)
    #     detail[idx] = replace(entry, surplus=surplus)

    if logging:
        print("\n" + msg)

    return ForecastResult(
        setpoint=orig_setpoint,
        min_bat=min_forecast_battery,
        t_min_bat=t_min_bat,
        max_bat=max_forecast_battery,
        t_max_bat=t_max_bat,
        max_feedin=max_feedin,
        t_max_feedin=t_max_feedin,
        max_pv_feedin=max_pv_feedin,
        t_max_pv_feedin=t_max_pv_feedin,
        setpoint_spread=setpoint_spread,
        prices_mean=prices_mean,
        prices_std=prices_std,
        detail=detail,
        max_battery_power_target=max_battery_power_target,
    )


@pyscript_compile
def merge_forecast_results(a: ForecastResult, b: ForecastResult, t_split: datetime):
    # Merge two setpoint results
    a_detail = [entry for entry in a.detail if entry.period_start <= t_split]
    b_detail = [entry for entry in b.detail if entry.period_start > t_split]

    merged_setpoint = replace(
        a,
        min_bat=min(a.min_bat, b.min_bat),
        t_min_bat=min(a.t_min_bat, b.t_min_bat),
        max_bat=max(a.max_bat, b.max_bat),
        t_max_bat=max(a.t_max_bat, b.t_max_bat),
        max_feedin=max(a.max_feedin, b.max_feedin),
        t_max_feedin=max(a.t_max_feedin, b.t_max_feedin),
        detail=a_detail + b_detail,
    )
    return merged_setpoint


@pyscript_compile
def extend_pv_forecast_to_horizon(
    forecast: list[PVForecastWithPrices],
    t_end: datetime,
    price_by_period: dict[tuple[int, int, int], float],
) -> list[PVForecastWithPrices]:
    """Extend a provider-limited forecast so scheduled drives stay visible.

    Solcast currently publishes five days.  For a later scheduled drive, reuse
    the latest available value for each wall-clock bucket.  Actual future EPEX
    prices win when present; otherwise the matching source bucket's price is
    retained.  This projected tail is display/planning context only: the live
    surplus budget still ends at the next solar-cycle trough.
    """
    if len(forecast) < 2:
        return forecast

    period = forecast[1].period_start - forecast[0].period_start
    latest_by_clock = {}
    for entry in forecast:
        latest_by_clock[(entry.period_start.hour, entry.period_start.minute)] = entry

    extended = list(forecast)
    cursor = forecast[-1].period_start + period
    while cursor < t_end:
        template = latest_by_clock.get((cursor.hour, cursor.minute))
        projected_pv = template.pv_estimate if template is not None else 0
        fallback_price = template.price_per_kwh if template is not None else 0
        projected_price = price_by_period.get(
            (cursor.day, cursor.hour, cursor.minute),
            fallback_price,
        )
        extended.append(
            PVForecastWithPrices(
                cursor,
                pv_estimate=projected_pv,
                price_per_kwh=projected_price,
            )
        )
        cursor += period

    return extended


def get_pv_forecast_with_prices(t_start: datetime, t_end: datetime, epex_prices: list[dict]):
    forecast = [
        el
        for el in [
            *get_attr(PVForecast.forecast_today, "detailedForecast", default=[]),
            *get_attr(PVForecast.forecast_tomorrow, "detailedForecast", default=[]),
            *get_attr(PVForecast.forecast_day_3, "detailedForecast", default=[]),
            *get_attr(PVForecast.forecast_day_4, "detailedForecast", default=[]),
            *get_attr(PVForecast.forecast_day_5, "detailedForecast", default=[]),
        ]
        if el["period_start"] > (t_start - timedelta(minutes=31)) and el["period_start"] < t_end
    ]
    if len(forecast) < 2:
        log.warning(
            f"Not enough forecast data available ({len(forecast)} entries)"
        )
        return []

    def get_date_tuple(date_time: str | datetime):
        if isinstance(date_time, datetime):
            dt = date_time.astimezone()
        else:
            dt = datetime.fromisoformat(date_time).astimezone()
        return dt.day, dt.hour, dt.minute

    period_hours = (forecast[1]["period_start"] - forecast[0]["period_start"]).total_seconds() / 60 / 60
    prices = {}
    for entry in epex_prices:
        start_time = datetime.fromisoformat(entry["start_time"]).astimezone()
        prices[get_date_tuple(start_time)] = entry["price_per_kwh"]
        prices[get_date_tuple(start_time + timedelta(hours=period_hours))] = entry["price_per_kwh"]

    source_forecast_end = forecast[-1]["period_start"]
    priced_forecast = []
    for forecast_entry in forecast:
        # insert price
        start_time = forecast_entry["period_start"]

        period_price = prices.get(get_date_tuple(start_time))
        if period_price is None:
            # Price data and PV data update independently.  A missing bucket must
            # not kill surplus publication and leave an old energy authorization
            # behind; fall back to the conservative import-tariff model.
            period_price = get_price(start_time.hour, start_time.minute)
            log.warning(
                f"No EPEX price found for forecast entry {get_date_tuple(start_time)}; "
                f"using fallback price {period_price:.4f}"
            )

        priced_forecast.append(
            PVForecastWithPrices(
                start_time,
                pv_estimate=forecast_entry["pv_estimate"],
                price_per_kwh=period_price,
            )
        )
    forecast = priced_forecast

    forecast = extend_pv_forecast_to_horizon(forecast, t_end, prices)
    if forecast[-1].period_start > source_forecast_end:
        log.warning(
            f"PV forecast source ends at {source_forecast_end}; projected the final daily profile "
            f"through {forecast[-1].period_start} to cover the EV schedule"
        )

    return forecast


@time_trigger
@time_trigger("period(now, 120sec)")
@state_trigger(f"{Automation.auto_max_setpoint} or {House.energy_surplus_after_ev_charging}")
def update_auto_max_setpoint():
    """Set the neutral export bias from conservatively spendable energy."""

    if not get(Automation.auto_max_setpoint, False):
        return

    t_now = now()
    spendable_surplus_kwh = get(
        House.energy_surplus_after_ev_charging,
        get(House.energy_surplus, 0),
    )
    spendable_horizon = get_attr(House.energy_surplus, "spendable_horizon", None)
    horizon_hours = 24.0
    if spendable_horizon is not None:
        try:
            horizon_hours = (with_timezone(spendable_horizon) - t_now).total_seconds() / 3600
        except (TypeError, ValueError):
            log.warning(f"Invalid spendable surplus horizon {spendable_horizon}; using 24 hours")
            horizon_hours = 24.0
    horizon_hours = max(1.0, min(36.0, horizon_hours))

    # One day of live history at the neutral -100 W command showed a 5-second
    # p99 tracking error of 71 W. A 100 W ceiling covers that plus margin;
    # larger biases bought negligible import reduction while continuously
    # exporting more energy outside the price-aware scheduler.
    target_setpoint = calculate_auto_max_setpoint(
        spendable_surplus_kwh=spendable_surplus_kwh,
        horizon_hours=horizon_hours,
        minimum_bias_w=20.0,
        full_bias_w=100.0,
    )
    current_setpoint = get(Grid.max_setpoint, -20)
    if abs(target_setpoint - current_setpoint) >= 5:
        set_state(Grid.max_setpoint, target_setpoint)
    log.warning(
        f"Auto max setpoint target {target_setpoint:.0f} W (previous {current_setpoint:.0f} W) from "
        f"{spendable_surplus_kwh:.2f} kWh spendable surplus after EV over "
        f"{horizon_hours:.1f} hours"
    )


# Time-discount table for ranking PV feed-in peaks: near-term peaks count in
# full, peaks 20+ hours out count for nothing.
_FEEDIN_PEAK_DISCOUNT_POINTS = ((0.0, 1.0), (6.0, 1.0), (9.0, 0.75), (12.0, 0.5), (16.0, 0.25), (20.0, 0.0))


def _feedin_peak_discount(hours_ahead):
    if hours_ahead <= _FEEDIN_PEAK_DISCOUNT_POINTS[0][0]:
        return _FEEDIN_PEAK_DISCOUNT_POINTS[0][1]

    for (left_hours, left_discount), (right_hours, right_discount) in zip(
        _FEEDIN_PEAK_DISCOUNT_POINTS, _FEEDIN_PEAK_DISCOUNT_POINTS[1:]
    ):
        if hours_ahead <= right_hours:
            span = right_hours - left_hours
            if span <= 0:
                return right_discount
            ratio = (hours_ahead - left_hours) / span
            return left_discount + ratio * (right_discount - left_discount)

    return 0.0


def get_discounted_pv_feedin_peak(forecast_result, t_now):
    if not forecast_result or not forecast_result.detail:
        return 0, t_now, 0

    best_weighted_peak = 0.0
    best_peak_time = t_now
    best_raw_peak = 0.0

    for entry in forecast_result.detail:
        if entry.period_start <= t_now or entry.pv_feedin <= 0:
            continue

        hours_ahead = max(0, (entry.period_start - t_now).total_seconds() / 3600)
        discount = _feedin_peak_discount(hours_ahead)

        weighted_peak = entry.pv_feedin * discount
        if weighted_peak > best_weighted_peak:
            best_weighted_peak = weighted_peak
            best_peak_time = entry.period_start
            best_raw_peak = entry.pv_feedin

    return best_weighted_peak, best_peak_time, best_raw_peak


def format_setpoint_results(search_results, title, t_now):
    # Print setpoint results in tablular format (without forecast details)
    if search_results is None:
        return None

    def ft(t):
        return t.strftime("%d %H:%M")

    def fi(k):
        return f"{k:.0f}"

    lines = []
    for r in search_results:
        discounted_peak, discounted_time, raw_peak = get_discounted_pv_feedin_peak(r, t_now)
        lines.append(
            f"{r.setpoint:2.0f} {' ' * 4}  {r.setpoint_spread:8.6f}{r.min_bat:9.1f}{'':6s}{ft(r.t_min_bat):10s}{r.max_bat:5.1f}{'':6s}{ft(r.t_max_bat):12s}{fi(raw_peak):12s}{fi(discounted_peak):18s}{ft(discounted_time):10s}"
        )
    return (
        f"Setpoint results {title}:\n{'setpoint':<11s}{'spread':<13s}{'min_bat':<10s}{'t_min_bat':<11s}{'max_bat':<11s}{'t_max_bat':<11s}{'max_pv_feedin':<12s}{'discounted_pv':<18s}{'t_discounted':<10s}\n"
        + "\n".join(lines)
    )


def validate_pv_power_measurement(pv_power_w, updated_at, current_time, max_age_seconds):
    """Shared PV validation: range-check the reading, then check its age."""

    value_valid = are_power_measurement_values_valid(
        pv_power_w,
        0.0,
        max_absolute_power_w=LIVE_POWER_MAX_ABSOLUTE_W,
    )
    measurement_fresh = value_valid and is_measurement_fresh(
        current_time,
        updated_at,
        max_age_seconds=max_age_seconds,
    )
    return value_valid, measurement_fresh


def build_reserve_trajectory_slots(
    pv_forecast,
    t_now,
    day_house_power_w,
    night_house_power_w,
    pv_power_scale,
    battery_cells_balanced,
):
    """Shared reserve-trajectory slot construction for both forecast passes.

    ``forecast_surplus`` uses an unscaled PV estimate while
    ``auto_setpoint_target`` applies its forecast dampening; both share the
    period-boundary, house-power, reserve-SOC, and deficit-protection logic.
    """

    reserve_slots = []
    for index, forecast_entry in enumerate(pv_forecast):
        if index + 1 < len(pv_forecast):
            period_end = pv_forecast[index + 1].period_start
        elif index > 0:
            period_end = forecast_entry.period_start + (
                forecast_entry.period_start - pv_forecast[index - 1].period_start
            )
        else:
            period_end = forecast_entry.period_start + timedelta(minutes=30)
        effective_start = max(t_now, forecast_entry.period_start)
        duration_hours = max(0.0, (period_end - effective_start).total_seconds() / 3600)
        forecast_house_power = (
            day_house_power_w
            if 7 < forecast_entry.period_start.hour < 19
            else night_house_power_w
        )
        forecast_pv_power = forecast_entry.pv_estimate * pv_power_scale * 1000
        slot_reserve_soc = max(
            get_reserve_soc(forecast_entry.period_start),
            get_low_pv_reserve_soc(
                house_power=forecast_house_power,
                pv_power=forecast_pv_power,
                battery_cells_balanced=bool(battery_cells_balanced),
            ),
        )
        reserve_slots.append(
            ReserveTrajectorySlot(
                period_start=forecast_entry.period_start,
                duration_hours=duration_hours,
                house_power_w=forecast_house_power,
                pv_power_w=forecast_pv_power,
                reserve_soc=slot_reserve_soc,
                protect_deficit=not is_low_price(
                    get_price(
                        hour=forecast_entry.period_start.hour,
                        minute=forecast_entry.period_start.minute,
                    )
                ),
            )
        )
    return reserve_slots


class SetpointForecastContext:
    """Loop-invariant optimizer inputs for the hoisted setpoint search helpers.

    ``auto_setpoint_target`` builds this once per run.  The hoisted
    ``forecast_setpoint_local`` / ``setpoint_binary_search`` /
    ``get_feedin_candidate`` helpers take it explicitly instead of closing
    over the optimizer locals, so the search logic is testable in isolation.

    ``frozen_forecast_inputs`` carries the live HA inputs that ``forecast()``
    would otherwise re-read on every replay (discharge limit, EV state,
    charge policy, ...).  The neutral baseline and the unified replay run
    seconds apart with task yields between them; without frozen inputs a
    mid-run entity flip (e.g. the telemetry hold setting the discharge
    limit to 0) makes the safety comparison compare two different worlds
    and discard a valid schedule.
    """

    def __init__(
        self,
        battery_capacity,
        min_feedin_price,
        forecast_dampening,
        battery_min_energy,
        max_pv_feedin,
        max_setpoint,
        battery_charge_limit,
        max_feedin_limit,
        ev_schedule,
        t_now,
        frozen_forecast_inputs=None,
    ):
        self.battery_capacity = battery_capacity
        self.min_feedin_price = min_feedin_price
        self.forecast_dampening = forecast_dampening
        self.battery_min_energy = battery_min_energy
        self.max_pv_feedin = max_pv_feedin
        self.max_setpoint = max_setpoint
        self.battery_charge_limit = battery_charge_limit
        self.max_feedin_limit = max_feedin_limit
        self.ev_schedule = ev_schedule
        self.t_now = t_now
        self.frozen_forecast_inputs = frozen_forecast_inputs or {}


def forecast_setpoint_local(
    ctx,
    pv_forecast,
    setpoint,
    with_ev_charging,
    ev_energy,
    configured_max_feedin,
    setpoint_spread=0.1,
    current_battery_energy=0,
    t_start: datetime | None = None,
    t_end: datetime | None = None,
    max_battery_power_target: float = 4000,
    logging=False,
    grid_target_schedule=None,
    optional_export_power_schedule=None,
    battery_floor_schedule=None,
    surplus_energy=None,
    battery_export_budget_kwh=None,
):
    if t_start is not None:
        pv_forecast = [entry for entry in pv_forecast if t_start < entry.period_start]
    if t_end is not None:
        pv_forecast = [entry for entry in pv_forecast if entry.period_start < t_end]
    result = forecast(
        pv_forecast,
        setpoint=setpoint,
        battery_capacity=ctx.battery_capacity,
        min_feedin_price=ctx.min_feedin_price,
        forecast_dampening=ctx.forecast_dampening,
        battery_energy=current_battery_energy,
        setpoint_spread=setpoint_spread,
        battery_min_energy=ctx.battery_min_energy,
        with_ev_charging=with_ev_charging and ev_energy is not None,
        ev_energy=ev_energy,
        max_battery_power_target=max_battery_power_target,
        battery_charge_limit=ctx.battery_charge_limit,
        max_pv_feedin_target=ctx.max_pv_feedin,
        max_feedin=configured_max_feedin,
        grid_target_schedule=grid_target_schedule,
        optional_export_power_schedule=optional_export_power_schedule,
        battery_floor_schedule=battery_floor_schedule,
        max_setpoint=ctx.max_setpoint,
        logging=logging,
        ev_schedule=ctx.ev_schedule,
        surplus_energy=surplus_energy,
        battery_export_budget_kwh=battery_export_budget_kwh,
        t_now=ctx.t_now,
        **ctx.frozen_forecast_inputs,
    )
    task.sleep(0.01)  # sleep to allow other tasks to run
    return result


def get_feedin_candidate(forecast_result, control_value, ctx, samples=None):
    discounted_peak, _peak_time, _raw_peak = get_discounted_pv_feedin_peak(forecast_result, ctx.t_now)
    if samples is None:
        samples = [(entry.period_start, entry.pv_feedin) for entry in forecast_result.detail]
    overflow_energy = calculate_pv_overflow_energy(samples, ctx.max_pv_feedin, ctx.t_now)
    return FeedinCandidate(
        control_value=control_value,
        predicted_peak_w=discounted_peak,
        overflow_energy_kwh=overflow_energy,
    )


def setpoint_binary_search(
    ctx,
    pv_forecast,
    min_setpoint,
    max_setpoint,
    with_ev_charging,
    battery_energy,
    ev_energy,
    logging,
    max_iters=5,
    max_battery_power_target: float = 8000,
    log_setpoint=False,
    current_setpoint=None,
    current_spread=None,
    min_spread=1e-5,
    max_spread=50,
    update_setpoint=True,
    update_spread=False,
):
    msg = ""
    if not len(pv_forecast) > 1:
        return None
    search_results = []
    assert update_setpoint or update_spread
    control_min_setpoint = min_setpoint
    control_max_setpoint = max_setpoint

    log.warning(
        f"Starting binary search for setpoint with parameters: ev_energy={ev_energy} min_setpoint={min_setpoint}, max_setpoint={max_setpoint}, min_spread={min_spread}, max_spread={max_spread}, update_setpoint={update_setpoint}, update_spread={update_spread}, max_iters={max_iters}, log_setpoint={log_setpoint}"
    )
    if not update_spread:
        assert current_spread is not None
    if not update_setpoint:
        assert current_setpoint is not None

    candidate_metrics = []

    def evaluate_candidate(candidate_setpoint, candidate_spread):
        result = forecast_setpoint_local(
            ctx,
            pv_forecast,
            candidate_setpoint,
            with_ev_charging=with_ev_charging,
            ev_energy=ev_energy,
            configured_max_feedin=ctx.max_feedin_limit,
            setpoint_spread=candidate_spread,
            current_battery_energy=battery_energy,
            max_battery_power_target=max_battery_power_target,
        )
        control_value = candidate_setpoint if update_setpoint else candidate_spread
        candidate = get_feedin_candidate(result, control_value, ctx)
        search_results.append(result)
        candidate_metrics.append(candidate)
        return result, candidate

    # Always evaluate the least aggressive boundary. The previous search
    # never sampled it, so an insensitive forecast could only terminate at
    # the opposite, full-export boundary.
    baseline_setpoint = max_setpoint if update_setpoint else current_setpoint
    baseline_spread = current_spread if update_setpoint else min_spread
    evaluate_candidate(baseline_setpoint, baseline_spread)

    for itr in range(max_iters):
        if update_setpoint:
            current_setpoint = (min_setpoint + max_setpoint) // 2
        if update_spread:
            current_spread = (min_spread + max_spread) / 2

        r, feedin_candidate = evaluate_candidate(current_setpoint, current_spread)
        update_reason = ""
        discounted_pv_feedin, discounted_peak_time, raw_pv_feedin = get_discounted_pv_feedin_peak(r, ctx.t_now)
        constraint_exceeded = feedin_constraint_exceeded(feedin_candidate, ctx.max_pv_feedin)

        if (
            constraint_exceeded
            and discounted_peak_time.day != ctx.t_now.day
            and (
                r.min_bat < ctx.battery_min_energy
                or min(
                    [d.surplus for d in r.detail if d.period_start.day == ctx.t_now.day and d.period_start > ctx.t_now]
                    or [float("inf")]
                )
                < 1
            )
        ):
            update_reason = "pv peak tomorrow, battery too low"
            # If we have a feedin peak tomorrow but battery is too low, we need to be more conservative (lower setpoint, higher spread)
            if update_setpoint:
                min_setpoint = current_setpoint
            if update_spread:
                max_spread = current_spread

        elif constraint_exceeded:
            update_reason = "max feedin too large"
            # If feed-in limit is exceeded, we need a more negative setpoint, higher spread
            if update_setpoint:
                max_setpoint = current_setpoint
            if update_spread:
                min_spread = current_spread

        else:
            update_reason = "default"
            if update_setpoint:
                min_setpoint = current_setpoint
            if update_spread:
                max_spread = current_spread

        if logging:
            msg += (
                f"Iteration {itr + 1}: reason  {update_reason} setpoint {r.setpoint:0f} spread {r.setpoint_spread:.1f} "
                f"min_setpoint={min_setpoint:.0f} max_setpoint={max_setpoint:.0f} pv feedin {raw_pv_feedin:.0f} "
                f"discounted {discounted_pv_feedin:.0f} overflow {feedin_candidate.overflow_energy_kwh:.3f} kWh "
                f"at {discounted_peak_time.strftime('%d %H:%M')} min_bat {r.min_bat:.1f} max_bat {r.max_bat:.1f} t_max_bat {r.t_max_bat}\n"
            )

        if (update_spread and abs(max_spread - min_spread) < 1e-3) or (
            update_setpoint and abs(max_setpoint - min_setpoint) < 50
        ):
            break

    selected_candidate = choose_stable_candidate(
        candidate_metrics,
        ctx.max_pv_feedin,
        prefer_larger_control=update_setpoint,
    )
    selected_index = candidate_metrics.index(selected_candidate)
    selected_result = search_results[selected_index]

    # Some forecast paths do not model the setpoint's effect on PV
    # overflow. If every candidate is effectively identical, the stable
    # selector correctly refuses to saturate at maximum export. For a real
    # remaining violation, derive the least export needed to create useful
    # battery headroom before the predicted peak.
    baseline_candidate = candidate_metrics[0]
    if (
        update_setpoint
        and selected_index == 0
        and feedin_constraint_exceeded(baseline_candidate, ctx.max_pv_feedin)
    ):
        _discounted_peak, peak_time, _raw_peak = get_discounted_pv_feedin_peak(selected_result, ctx.t_now)
        hours_to_peak = max(0.0, (peak_time - ctx.t_now).total_seconds() / 3600)
        available_energy_kwh = max(0.0, battery_energy - ctx.battery_min_energy)
        fallback_setpoint = calculate_energy_bounded_control(
            overflow_energy_kwh=baseline_candidate.overflow_energy_kwh,
            available_energy_kwh=available_energy_kwh,
            hours_to_peak=hours_to_peak,
            min_control_w=control_min_setpoint,
            max_control_w=control_max_setpoint,
        )
        if fallback_setpoint < baseline_candidate.control_value:
            selected_result, selected_candidate = evaluate_candidate(
                fallback_setpoint,
                current_spread,
            )
            log.warning(
                f"Using energy-bounded fallback setpoint {fallback_setpoint:.0f} W for "
                f"{available_energy_kwh:.2f} kWh available, "
                f"{baseline_candidate.overflow_energy_kwh:.2f} kWh overflow, "
                f"{hours_to_peak:.2f} h to peak"
            )
    if selected_result is not search_results[-1]:
        log.warning(
            f"Selected stable control {selected_candidate.control_value:.2f}; "
            f"peak {selected_candidate.predicted_peak_w:.0f} W, "
            f"overflow {selected_candidate.overflow_energy_kwh:.3f} kWh"
        )
        search_results.append(selected_result)
    if logging:
        log.warning(f"Binary search completed in {itr + 1} iterations: \n" + msg)
    return search_results


@time_trigger("period(now, 120sec)")
@state_trigger(
    f"{Grid.max_feedin_target} or {Grid.max_pv_feedin_target} or {Automation.auto_setpoint} "
    f"or {Grid.max_setpoint} or {Automation.auto_max_setpoint} "
    f"or {Automation.unified_export_scheduler} or {Automation.efficient_export_power} "
    f"or {Automation.quiet_export_price_tolerance}"
)
@state_trigger(EV.is_charging)
@state_trigger(EV.energy_needed)
@state_trigger(Charger.ready)
@state_trigger(Charger.control_switch)
@state_trigger(PVForecast.power_now)
@state_trigger(PVProduction.total_power)
def auto_setpoint_target():
    task.unique("auto setpoint target", kill_me=True)
    t_now = now()
    setpoint = 0
    logging = True
    forecast_hours = 24

    max_feedin_limit = get(Grid.max_feedin_target, 4000)
    max_pv_feedin = get(Grid.max_pv_feedin_target, 4000)

    skip_automation_message = ""
    unified_scheduler_requested = bool(get(Automation.unified_export_scheduler, False))
    if automation_disabled := get(Automation.auto_setpoint, False) is False:
        skip_automation_message = "Auto setpoint is disabled"

        current_setpoint = get(Grid.power_setpoint, -20)
    else:
        # Keep this optimizer in its own (unmapped) control domain. Feeding the
        # price-mapped target back into the search bound created a second,
        # unintended feedback loop after every large target change.
        current_setpoint = get(Grid.power_setpoint_basis, -20)

    unified_scheduler_enabled = unified_scheduler_requested and not automation_disabled

    configured_max_setpoint = get(Grid.max_setpoint, -20)
    max_setpoint = (
        configured_max_setpoint
        if unified_scheduler_requested and not automation_disabled
        else min(current_setpoint + 500, configured_max_setpoint)
    )
    min_setpoint = min(max_setpoint, -max_feedin_limit)
    reserve_soc = get_reserve_soc()

    forecast_dampening = 1.0  # dampen the forecast to account for inaccuracies
    battery_energy = get(Battery.energy, 0)
    battery_energy_updated_at = get_attr(Battery.energy, "last_updated")
    # Battery.power is in W; an idle battery's energy reading legitimately
    # sits still (HA only refreshes last_updated on change).
    battery_energy_measurement_fresh = is_battery_energy_measurement_usable(
        t_now,
        battery_energy_updated_at,
        battery_energy,
        get(Battery.capacity, 1337),
        get(Battery.soc, -1),
        get(Battery.power, 1337),
        get_attr(Battery.power, "last_updated"),
        max_age_seconds=LIVE_BATTERY_ENERGY_MAX_MEASUREMENT_AGE_SECONDS,
    )
    min_feedin_price = 0

    house_avg_power: float = get(House.daily_average_power, 0)  # W

    battery_capacity = get(Battery.capacity, 1337)

    if (
        battery_capacity == 1337
        or not are_battery_energy_values_valid(battery_capacity, battery_energy)
        or not battery_energy_measurement_fresh
    ):
        log.error(
            "Battery capacity or energy is unavailable, invalid, or stale; cannot calculate setpoint"
        )
        return
    configured_battery_charge_limit_w = resolve_directional_power_limit_w(
        get(Battery.charge_limit, -1),
        6600.0,
        6600.0,
    )

    minimal_soc = get(Automation.minimal_soc, reserve_soc)
    operational_battery_min_energy = get_battery_export_floor(
        battery_capacity,
        reserve_soc,
        minimal_soc,
    )
    battery_min_energy = (
        get_battery_export_floor(battery_capacity, reserve_soc, reserve_soc)
        if unified_scheduler_enabled
        else operational_battery_min_energy
    )

    if logging:
        log.warning(
            f"battery capacity: {battery_capacity} min energy: {battery_min_energy} "
            f"operational min energy: {operational_battery_min_energy}"
        )

    ev_schedule = (schedule.get_schedule(entity_id="schedule.tesla_planned_drives") or {}).get(
        "schedule.tesla_planned_drives", {}
    )
    # example:
    # data = {'monday': [], 'tuesday': [], 'wednesday': [{'from': datetime.time(7, 30), 'to': datetime.time(17, 0)}], 'thursday': [], 'friday': [], 'saturday': [], 'sunday': [{'from': datetime.time(9, 30), 'to': datetime.time(10, 0), 'data': {'required_charge': 50}}]}
    ev_required_soc = get(EV.required_soc, 80)
    ev_schedule = parse_full_schedule(
        ev_schedule,
        default_required_soc=ev_required_soc,
        t_now=t_now,
    )
    drive_ongoing, next_departure = get_ongoing_and_next_drive(ev_schedule, t_now)
    ev_energy = get(EV.energy, EVConst.ev_capacity)

    ev_is_charging = get(EV.is_charging, False)
    ev_energy_needed = get(EV.energy_needed, 0)
    wallbox_connected = is_ev_available_for_charging(
        charger_ready=get(Charger.ready, False),
        charger_control_enabled=get(Charger.control_switch, False),
        ev_is_charging=ev_is_charging,
    )
    assume_able_to_charge_on_arrival = get(EV.able_to_charge_on_arrival, False)

    # An active drive ending soon is an EV-arrival reservation, even when the
    # wallbox is not connected yet.  Do not spend battery inventory on
    # optional export for a future PV peak while the car is still away: the
    # arrival is the next physical opportunity to satisfy the EV obligation.
    ev_arrival_reservation_active = bool(
        drive_ongoing
        and drive_ongoing.end > t_now
        and drive_ongoing.end <= t_now + timedelta(hours=EV_EXPORT_RESERVATION_HORIZON_HOURS)
    )

    with_ev_charging = wallbox_connected or (
        drive_ongoing and assume_able_to_charge_on_arrival
    )

    # Freeze every live input that forecast() would otherwise re-read per
    # replay.  The optimizer runs neutral, candidate, and unified replays
    # seconds apart; a mid-run entity flip must not make the safety
    # comparison compare two different worlds.  Defaults match forecast().
    frozen_forecast_inputs = {
        "daily_power": house_avg_power,
        "nightly_power": get(House.nightly_average_power, 0),
        "battery_discharge_limit_w": get_planned_discharge_limit_w(-1),
        "ev_required_soc": ev_required_soc,
        "ev_soc": get(EV.soc, 100),
        "is_charging_ev": ev_is_charging,
        "smart_limiter_active": get(Automation.auto_charge_limit, False),
        "charge_limit": get(Battery.force_charge_up_to, 0),
        "max_charge_price": get(Battery.max_charge_price, 0),
        "force_charge_switch": get(Battery.force_charge_switch, False),
        "min_discharge_price": float(get(Automation.min_discharge_price, default=0)),
        "charger_ready": get(Charger.ready, False),
        "charger_ready_since": get_attr(Charger.ready, "last_changed"),
        "ev_plugged_in": get(EV.plugged_in, False),
        "eff_dis": get(Automation.efficient_discharge, False),
        "battery_cells_balanced": get(Battery.cells_balanced, False),
    }

    forecast_ctx = SetpointForecastContext(
        battery_capacity=battery_capacity,
        min_feedin_price=min_feedin_price,
        forecast_dampening=forecast_dampening,
        battery_min_energy=battery_min_energy,
        max_pv_feedin=max_pv_feedin,
        max_setpoint=max_setpoint,
        battery_charge_limit=configured_battery_charge_limit_w,
        max_feedin_limit=max_feedin_limit,
        ev_schedule=ev_schedule,
        t_now=t_now,
        frozen_forecast_inputs=frozen_forecast_inputs,
    )

    # Binary search for optimal setpoint
    current_battery_energy = battery_energy
    epex_prices = get_attr(ElectricityPrices.epex_forecast_prices, "data", [])
    epex_export_prices = get_attr(
        ElectricityPrices.epex_export_forecast_prices,
        "data",
        [],
    )

    if not epex_prices:
        log.warning("Unable to forecast setpoint, no EPEX prices available")
        return

    pv_power_total = get(PVProduction.total_power, 0)
    pv_power_updated_at = get_attr(PVProduction.total_power, "last_updated")
    pv_power_value_valid, pv_power_measurement_fresh = validate_pv_power_measurement(
        pv_power_total,
        pv_power_updated_at,
        t_now,
        HIGH_SIDE_PV_MAX_MEASUREMENT_AGE_SECONDS,
    )

    epex_pv_forecast = get_pv_forecast_with_prices(
        t_start=t_now, t_end=t_now + timedelta(hours=forecast_hours), epex_prices=epex_prices
    )

    if unified_scheduler_enabled:
        hard_max_grid_export_w = min(5500.0, max_feedin_limit)
        # This is the sustained planning cap, not a zero-duration trip point.
        # Short Victron tracking overshoots are acceptable.  The scheduler must
        # avoid multi-minute / settlement-interval violations where battery
        # headroom can do so; an external HA automation disables roof PV only as
        # the independent last resort when storage cannot contain the peak.
        export_cap_margin_w = 500.0
        planned_max_grid_export_w = max(
            0.0,
            hard_max_grid_export_w - export_cap_margin_w,
        )
        configured_discharge_limit_w = get_planned_discharge_limit_w(-1)
        max_battery_discharge_w = resolve_directional_power_limit_w(
            configured_discharge_limit_w,
            8000.0,
            8000.0,
        )
        # Silent mode caps scheduled battery-to-grid export (the deltas are
        # purely grid-bound, so house and EV coverage in the replay stay
        # unlimited); the live safety supervisor remains uncapped for true
        # cap emergencies.
        silent_mode_active = get(Automation.silent_mode, False)
        scheduler_max_discharge_w = (
            min(max_battery_discharge_w, SILENT_MODE_MAX_BATTERY_POWER_W)
            if silent_mode_active
            else max_battery_discharge_w
        )
        efficient_discharge_w = get(Automation.efficient_export_power, 3500)
        quiet_price_tolerance = get(Automation.quiet_export_price_tolerance, 5) / 100
        min_discharge_price = float(get(Automation.min_discharge_price, default=0))
        raw_published_policy_surplus_energy_kwh = get(
            House.energy_surplus_after_ev_charging,
            0,
        )
        published_surplus_calculated_at = parse_aware_datetime(
            get_attr(
                House.energy_surplus_after_ev_charging,
                "calculated_at",
            )
        )
        published_surplus_battery_energy_kwh = get_attr(
            House.energy_surplus_after_ev_charging,
            "battery_energy_at_calculation",
        )
        raw_base_spendable_energy_kwh = get(House.energy_surplus, 0)
        export_policy_budget = get_fresh_export_policy_budget(
            base_spendable_energy_kwh=raw_base_spendable_energy_kwh,
            ev_adjusted_spendable_energy_kwh=raw_published_policy_surplus_energy_kwh,
            source_battery_energy_kwh=published_surplus_battery_energy_kwh,
            current_battery_energy_kwh=battery_energy,
            calculated_at=published_surplus_calculated_at,
            current_time=t_now,
            max_age_seconds=EXPORT_POLICY_MAX_AGE_SECONDS,
            discharge_efficiency=HOME_BATTERY_DISCHARGE_EFFICIENCY,
        )
        base_spendable_energy_kwh = (
            export_policy_budget.base_spendable_energy_kwh
        )
        published_policy_surplus_energy_kwh = (
            export_policy_budget.ev_adjusted_spendable_energy_kwh
        )
        published_surplus_age_seconds = export_policy_budget.source_age_seconds
        export_policy_valid = export_policy_budget.valid
        export_policy_reason = export_policy_budget.reason
        if not export_policy_valid:
            log.warning(
                "Holding optional battery export: EV-adjusted surplus policy "
                f"snapshot is {export_policy_reason}"
            )
        published_local_ev_wall_energy_kwh = max(
            0.0,
            float(
                get_attr(
                    House.energy_surplus_after_ev_charging,
                    "local_ev_wall_energy_before_departure",
                    0,
                )
                or 0
            ),
        )
        ev_obligation_due_within_horizon = bool(
            with_ev_charging
            and next_departure is not None
            and t_now < next_departure.start
            and next_departure.start
            <= t_now + timedelta(hours=EV_EXPORT_RESERVATION_HORIZON_HOURS)
        )
        # Re-apply the same invariant with live EV demand.  This closes the
        # short race where EV.energy_needed changes before the surplus sensor's
        # next forecast publication.
        policy_surplus_energy_kwh = calculate_ev_adjusted_spendable_energy(
            base_spendable_energy_kwh=base_spendable_energy_kwh,
            ev_forecast_spendable_energy_kwh=published_policy_surplus_energy_kwh,
            remaining_ev_energy_kwh=ev_energy_needed,
            charge_efficiency=EVConst.charge_efficiency,
            obligation_due_within_horizon=ev_obligation_due_within_horizon,
            local_ev_wall_energy_before_departure_kwh=(
                published_local_ev_wall_energy_kwh
            ),
        )
        if policy_surplus_energy_kwh + 0.01 < published_policy_surplus_energy_kwh:
            log.warning(
                f"Live EV obligation reduced the published export budget from "
                f"{published_policy_surplus_energy_kwh:.2f} to {policy_surplus_energy_kwh:.2f} kWh"
            )
        # Never export energy the forecast cannot prove spendable: reserve
        # the nearest uncovered departure in the reservation horizon, through
        # charger losses. The reservation self-releases as live charging
        # shrinks the shortfall on later cycles.
        horizon_ev_shortfall_kwh = 0.0
        try:
            horizon_ev_shortfall_kwh = max(
                0.0,
                float(
                    get_attr(
                        House.energy_surplus_after_ev_charging,
                        "horizon_ev_shortfall_kwh",
                        0,
                    )
                    or 0
                ),
            )
        except (TypeError, ValueError, OverflowError):
            horizon_ev_shortfall_kwh = 0.0
        if horizon_ev_shortfall_kwh > 0:
            reserved_policy_surplus_kwh = apply_horizon_ev_reservation(
                policy_surplus_energy_kwh,
                horizon_ev_shortfall_kwh,
                EVConst.charge_efficiency,
            )
            if reserved_policy_surplus_kwh + 0.01 < policy_surplus_energy_kwh:
                log.warning(
                    f"Horizon EV reservation reduced the export budget from "
                    f"{policy_surplus_energy_kwh:.2f} to {reserved_policy_surplus_kwh:.2f} kWh "
                    f"for {horizon_ev_shortfall_kwh:.2f} kWh uncovered departure need"
                )
            policy_surplus_energy_kwh = reserved_policy_surplus_kwh

        battery_cells_balanced = get(Battery.cells_balanced, False)
        nightly_house_power = get(House.nightly_average_power, 0)
        reserve_slots = build_reserve_trajectory_slots(
            epex_pv_forecast,
            t_now,
            day_house_power_w=house_avg_power,
            night_house_power_w=nightly_house_power,
            pv_power_scale=forecast_dampening,
            battery_cells_balanced=battery_cells_balanced,
        )

        battery_floor_schedule = build_reserve_energy_schedule(
            reserve_slots,
            battery_capacity_kwh=battery_capacity,
            uncertainty_margin_kwh=EXPORT_BASE_UNCERTAINTY_MARGIN_KWH,
            house_load_margin_fraction=EXPORT_HOUSE_LOAD_MARGIN_FRACTION,
            pv_confidence_factor=EXPORT_PV_CONFIDENCE_FACTOR,
            discharge_efficiency=HOME_BATTERY_DISCHARGE_EFFICIENCY,
        )

        # The neutral replay supplies capacity and spill estimates only. The
        # actual plan below contains relative optional-export power, so replay
        # always recomputes charging and self-consumption from its evolving
        # battery state instead of preserving this trajectory's grid targets.
        neutral_forecast = forecast_setpoint_local(
            forecast_ctx,
            pv_forecast=epex_pv_forecast,
            setpoint=max_setpoint,
            setpoint_spread=0.01,
            current_battery_energy=current_battery_energy,
            with_ev_charging=with_ev_charging,
            ev_energy=ev_energy,
            max_battery_power_target=max_battery_discharge_w,
            configured_max_feedin=planned_max_grid_export_w,
            grid_target_schedule=None,
            optional_export_power_schedule={},
            battery_floor_schedule=battery_floor_schedule,
            surplus_energy=policy_surplus_energy_kwh,
            battery_export_budget_kwh=0,
        )
        if neutral_forecast is None or not neutral_forecast.detail:
            log.error("Unified export scheduler has no neutral forecast; keeping the previous target")
            return

        scheduler_safety_margin_kwh = 0.5
        current_planned_floor_kwh = battery_floor_schedule.get(
            neutral_forecast.detail[0].period_start.isoformat(),
            battery_min_energy,
        )
        safe_stored_energy_kwh = max(
            0.0,
            battery_energy - current_planned_floor_kwh - scheduler_safety_margin_kwh,
        )
        for detail_entry in neutral_forecast.detail:
            period_floor = battery_floor_schedule.get(
                detail_entry.period_start.isoformat(),
                battery_min_energy,
            )
            safe_stored_energy_kwh = min(
                safe_stored_energy_kwh,
                max(
                    0.0,
                    detail_entry.battery_energy - period_floor - scheduler_safety_margin_kwh,
                ),
            )

        # ``safe_stored_energy_kwh`` is DC battery inventory.  The scheduler
        # allocates AC grid export, so conversion loss must reduce its budget.
        physical_safe_export_energy_kwh = (
            safe_stored_energy_kwh
            * HOME_BATTERY_DISCHARGE_EFFICIENCY
        )
        export_energy_budget = calculate_export_energy_budget(
            physically_available_energy_kwh=physical_safe_export_energy_kwh,
            spendable_energy_before_ev_kwh=base_spendable_energy_kwh,
            spendable_energy_after_ev_kwh=policy_surplus_energy_kwh,
        )
        safe_export_energy_kwh = export_energy_budget.safe_export_energy_kwh
        export_policy_snapshot_id = (
            published_surplus_calculated_at.isoformat()
            if published_surplus_calculated_at is not None
            else ""
        )
        export_budget_ledger = update_monotonic_export_budget_ledger(
            snapshot_id=export_policy_snapshot_id,
            candidate_remaining_kwh=safe_export_energy_kwh,
            previous_state=get("pyscript.export_policy_budget_ledger", ""),
            current_time=t_now,
            accounting_enabled=False,
        )
        if export_budget_ledger.valid and export_policy_valid:
            safe_export_energy_kwh = min(
                safe_export_energy_kwh,
                export_budget_ledger.remaining_kwh,
            )
            state.set(  # noqa: F821 - provided by the Pyscript runtime
                "pyscript.export_policy_budget_ledger",
                export_budget_ledger.state_value,
            )
        else:
            safe_export_energy_kwh = 0.0
        ev_arrival_reserved_kwh = export_energy_budget.ev_reservation_energy_kwh
        if ev_arrival_reservation_active:
            log.warning(
                f"EV arrival reservation active until {drive_ongoing.end}; "
                f"{ev_arrival_reserved_kwh:.2f} kWh retained quantitatively, "
                f"{safe_export_energy_kwh:.2f} kWh remains safe for optional export"
            )
        ev_export_hold_active = bool(
            ev_obligation_due_within_horizon
            and ev_energy_needed > 0.1
            and policy_surplus_energy_kwh <= 0.1
        )
        export_block_reason = "none"
        if safe_export_energy_kwh <= 0.01:
            if not export_policy_valid:
                export_block_reason = "stale_policy"
            elif ev_export_hold_active:
                export_block_reason = "ev_obligation"
            elif physical_safe_export_energy_kwh <= 0.01:
                export_block_reason = "battery_floor"
            else:
                export_block_reason = "policy_reserve"
        if safe_export_energy_kwh + 0.01 < physical_safe_export_energy_kwh:
            log.warning(
                f"Policy budget capped optional battery export from "
                f"{physical_safe_export_energy_kwh:.2f} to {safe_export_energy_kwh:.2f} kWh; "
                f"pending EV charge {ev_energy_needed:.2f} kWh"
            )

        scheduler_slots = []
        scheduler_period_ends = {}
        missing_export_price_periods = 0
        for index, detail_entry in enumerate(neutral_forecast.detail):
            if index + 1 < len(neutral_forecast.detail):
                period_end = neutral_forecast.detail[index + 1].period_start
            elif index > 0:
                period_end = detail_entry.period_start + (
                    detail_entry.period_start - neutral_forecast.detail[index - 1].period_start
                )
            else:
                period_end = detail_entry.period_start + timedelta(minutes=15)
            effective_start = max(t_now, detail_entry.period_start)
            duration_hours = max(0.0, (period_end - effective_start).total_seconds() / 3600)
            gross_export_price = get_interval_price(
                epex_export_prices,
                detail_entry.period_start,
            )
            if gross_export_price is None:
                # The live surplus budget still prevents speculative export.
                # Falling back to the EPEX total-price bucket keeps ranking
                # available during a transient market-price attribute update;
                # the installation currently configures zero import surcharge.
                gross_export_price = detail_entry.epex_price
                missing_export_price_periods += 1
            scheduler_slots.append(
                ExportSchedulerSlot(
                    period_start=detail_entry.period_start,
                    duration_hours=duration_hours,
                    price_per_kwh=calculate_net_battery_export_price(
                        gross_export_price,
                        inverter_efficiency=HOME_BATTERY_DISCHARGE_EFFICIENCY,
                        broker_revenue_margin_fraction=(
                            EXPORT_BROKER_REVENUE_MARGIN_FRACTION
                        ),
                    ),
                    baseline_setpoint_w=min(
                        0.0,
                        detail_entry.power_from_grid - detail_entry.feedin,
                    ),
                    baseline_battery_power_w=detail_entry.battery_power,
                    baseline_grid_export_w=detail_entry.feedin,
                    ev_charge_power_w=detail_entry.ev_charge_power,
                    intentional_grid_import_w=detail_entry.power_from_grid,
                )
            )
            scheduler_period_ends[detail_entry.period_start.isoformat()] = period_end
        if missing_export_price_periods:
            log.warning(
                f"No raw EPEX export price for {missing_export_price_periods} forecast "
                "periods; using the matching total-price bucket for timing only"
            )

        pv_feedin_samples = []
        preferred_headroom_requirements = []
        hard_headroom_requirements = []
        cumulative_preferred_overflow_kwh = 0.0
        cumulative_hard_overflow_kwh = 0.0
        raw_peak_w = 0.0
        raw_peak_time = None
        headroom_reference_value = 0.0
        headroom_reference_energy_kwh = 0.0
        for detail_entry, scheduler_slot in zip(neutral_forecast.detail, scheduler_slots):
            pv_feedin_samples.append((detail_entry.period_start, detail_entry.pv_feedin))
            # The replayed pv_feedin is capped by the neutral setpoint (the
            # simulated battery absorbs the rest), so it cannot see the real
            # PV peak.  Size the peak from natural surplus instead; the same
            # capped samples stay correct for overflow math below, which asks
            # what still spills after absorption.
            natural_surplus_w = max(
                0.0,
                detail_entry.pv_estimate - detail_entry.house_power,
            )
            if natural_surplus_w > raw_peak_w:
                raw_peak_w = natural_surplus_w
                raw_peak_time = detail_entry.period_start
            preferred_overflow_w = calculate_absorbable_overflow_power(
                overflow_power_w=max(0.0, detail_entry.pv_feedin - max_pv_feedin),
                planned_battery_charge_power_w=detail_entry.battery_power,
                max_battery_charge_power_w=configured_battery_charge_limit_w,
            )
            hard_overflow_w = calculate_absorbable_overflow_power(
                overflow_power_w=max(
                    0.0,
                    detail_entry.feedin - planned_max_grid_export_w,
                ),
                planned_battery_charge_power_w=detail_entry.battery_power,
                max_battery_charge_power_w=configured_battery_charge_limit_w,
            )
            # Discount future PV before sizing pre-release: the release
            # must stay at or below the real spill.
            preferred_overflow_kwh = (
                preferred_overflow_w
                * scheduler_slot.duration_hours
                / 1000
                * SHAPING_PV_CONFIDENCE_FACTOR
            )
            hard_overflow_kwh = (
                hard_overflow_w * scheduler_slot.duration_hours / 1000
            )
            cumulative_preferred_overflow_kwh += preferred_overflow_kwh
            cumulative_hard_overflow_kwh += hard_overflow_kwh
            period_end = scheduler_period_ends[detail_entry.period_start.isoformat()]
            preferred_requirement_kwh = calculate_headroom_energy_requirement(
                cumulative_preferred_overflow_kwh,
                cumulative_preferred_overflow_kwh,
            )
            hard_requirement_kwh = calculate_headroom_energy_requirement(
                cumulative_hard_overflow_kwh,
                cumulative_hard_overflow_kwh,
            )
            if preferred_overflow_kwh > 0:
                preferred_headroom_requirements.append(
                    (period_end, preferred_requirement_kwh)
                )
            if hard_overflow_kwh > 0:
                hard_headroom_requirements.append((period_end, hard_requirement_kwh))
            headroom_reference_energy_kwh += preferred_overflow_kwh
            headroom_reference_value += (
                preferred_overflow_kwh * scheduler_slot.price_per_kwh
            )

        current_neutral_detail = neutral_forecast.detail[0]
        for detail_entry in neutral_forecast.detail:
            if (
                detail_entry.period_start <= t_now
                and detail_entry.period_start >= current_neutral_detail.period_start
            ):
                current_neutral_detail = detail_entry
        live_pv_power_for_planning = (
            max(0.0, pv_power_total)
            if pv_power_measurement_fresh
            else max(0.0, current_neutral_detail.pv_estimate)
        )
        try:
            free_battery_capacity_kwh = max(
                0.0, float(battery_capacity) - float(current_battery_energy)
            )
        except (TypeError, ValueError, OverflowError):
            free_battery_capacity_kwh = None
        high_side_headroom = build_high_side_headroom_requirements(
            slots=scheduler_slots,
            period_ends=scheduler_period_ends,
            current_time=t_now,
            live_pv_power_w=live_pv_power_for_planning,
            forecast_pv_power_w=current_neutral_detail.pv_estimate,
            max_grid_export_w=planned_max_grid_export_w,
            error_decay_hours=HIGH_SIDE_PV_ERROR_DECAY_HOURS,
            maximum_error_w=HIGH_SIDE_PV_MAX_ERROR_W,
            max_battery_charge_w=configured_battery_charge_limit_w,
            maximum_live_pv_power_w=LIVE_POWER_MAX_ABSOLUTE_W,
            battery_free_capacity_kwh=free_battery_capacity_kwh,
        )
        # Soft requirements price-rank into early windows by design: when
        # the battery is going to fill, releasing the spill above the
        # feed-in target early ends the day just as full.  Hard grid-cap
        # requirements and live-measured high-side surplus stay on the
        # fast path.
        # Room-overflow fuel: discounted future PV above the feed-in
        # target after the fill.  Releasing it early needs no rebuy: the
        # same sun refills what was sold.  The stricter shaping discount
        # (SHAPING_PV_CONFIDENCE_FACTOR) is what prevents over-release.
        room_entries = []
        for detail_entry, scheduler_slot in zip(
            neutral_forecast.detail, scheduler_slots
        ):
            room_entries.append(
                (
                    detail_entry.period_start,
                    detail_entry.battery_energy,
                    detail_entry.pv_feedin,
                    scheduler_slot.duration_hours,
                )
            )
        room_requirement = compute_room_overflow_requirement(
            room_entries,
            battery_capacity,
            max_pv_feedin,
            SHAPING_PV_CONFIDENCE_FACTOR,
            t_now.date(),
        )
        if room_requirement is not None:
            preferred_headroom_requirements.append(room_requirement)
        preferred_headroom_requirements.extend(high_side_headroom.requirements)
        high_side_headroom_energy_kwh = max(
            [requirement for _deadline, requirement in high_side_headroom.requirements]
            or [0.0]
        )
        overflow_energy_kwh = calculate_pv_overflow_energy(
            pv_feedin_samples,
            max_pv_feedin,
            t_now,
        )
        headroom_energy_kwh = calculate_headroom_energy_requirement(
            overflow_energy_kwh,
            overflow_energy_kwh,
        )
        headroom_reference_price = (
            headroom_reference_value / headroom_reference_energy_kwh
            if headroom_reference_energy_kwh > 0
            else 0.0
        )
        headroom_min_price_spread = 0.01
        hard_headroom_energy_kwh = max(
            [requirement for _deadline, requirement in hard_headroom_requirements]
            or [0.0]
        )
        next_headroom_deadline = (
            preferred_headroom_requirements[0][0]
            if preferred_headroom_requirements
            else raw_peak_time
        )

        # Cell-balancing chase: withhold economic export and soft-target
        # pre-release once the last full charge is a week back, so the next
        # fill-capable day ends at 100%.  Hard-cap headroom and true-spill
        # holds stay active, and fill-incapable days are unaffected anyway
        # (no economic eligibility, preferred requirements dropped there).
        last_full_charge_at = parse_aware_datetime(
            get("pyscript.battery_last_full", "")
        )
        if last_full_charge_at is None:
            days_since_full_charge = None
        else:
            days_since_full_charge = (
                t_now - last_full_charge_at
            ).total_seconds() / 86400
        balance_chase_active = (
            days_since_full_charge is None
            or days_since_full_charge >= BALANCE_CHASE_AFTER_DAYS
        )

        # Profitability gate: the market price ranks spill-driven pre-release
        # slots, but realized feed-in compensation is capped at a fixed tariff
        # until the dynamic export contract switch.  Selling stored energy
        # only pays when it beats the cheapest future rebuy, so economic
        # export stays blocked while the cap cannot beat night-low import.
        feedin_compensation_cap = get(
            ElectricityPrices.feedin_compensation_cap, 0.08
        )
        future_import_prices = [
            get_price(slot.period_start.hour, slot.period_start.minute)
            for slot in scheduler_slots
        ]
        economic_export_floor = resolve_economic_export_floor(
            min_discharge_price,
            feedin_compensation_cap,
            future_import_prices,
        )
        if economic_export_floor is None:
            log.warning(
                f"Economic battery export blocked at feed-in cap "
                f"{feedin_compensation_cap}: realized revenue cannot beat the "
                "cheapest future import; only spill pre-release is scheduled"
            )

        # Headroom release ledger: the spill requirement does not shrink as
        # the battery drains on strong-PV days (it fills by late morning
        # from any evening level), so repeated runs re-allocate the same
        # energy.  Net already-released energy per spill event so the
        # pre-release converges (2026-10-02: 4.5 kWh executed vs 1.7
        # required).  Hard cap requirements stay unscaled: compliance first.
        headroom_release_event_key = (
            raw_peak_time.date().isoformat()
            if raw_peak_time is not None
            else t_now.date().isoformat()
        )
        headroom_released_kwh = read_headroom_released_kwh(
            headroom_release_event_key,
            get("pyscript.headroom_release_ledger", ""),
        )
        preferred_headroom_total_kwh = max(
            [headroom_energy_kwh]
            + [
                requirement
                for _deadline, requirement in preferred_headroom_requirements
            ]
        )
        headroom_release_scaling_active = hard_headroom_energy_kwh <= 0
        headroom_factor = (
            headroom_release_factor(
                preferred_headroom_total_kwh, headroom_released_kwh
            )
            if headroom_release_scaling_active
            else 1.0
        )
        scaled_headroom_energy_kwh = headroom_energy_kwh * headroom_factor
        scaled_preferred_headroom_requirements = [
            (deadline, target_kwh * headroom_factor)
            for deadline, target_kwh in preferred_headroom_requirements
        ]

        schedule_plan = build_unified_export_schedule(
            slots=scheduler_slots,
            available_export_energy_kwh=safe_export_energy_kwh,
            required_headroom_energy_kwh=scaled_headroom_energy_kwh,
            headroom_deadline=raw_peak_time,
            min_discharge_price=(
                min_discharge_price
                if economic_export_floor is None
                else economic_export_floor
            ),
            max_grid_export_w=planned_max_grid_export_w,
            max_battery_discharge_w=scheduler_max_discharge_w,
            efficient_discharge_w=efficient_discharge_w,
            quiet_boost_penalty_fraction=quiet_price_tolerance,
            headroom_reference_price=headroom_reference_price,
            headroom_min_price_spread=headroom_min_price_spread,
            headroom_requirements=(
                []
                if balance_chase_active
                else scaled_preferred_headroom_requirements
            ),
            hard_headroom_requirements=hard_headroom_requirements,
            neutral_grid_target_w=max_setpoint,
            # Storage absorbs first: the hold routes only surplus beyond
            # charge power, never chargeable energy.
            max_battery_charge_w=configured_battery_charge_limit_w,
            # End-of-day top-up: trim late arbitrage until the simulated
            # day ends at 100% again.  Refill charging is discounted with
            # the same PV haircut as the reserve trajectory so the top-up
            # survives forecast shortfall instead of trusting it exactly.
            current_battery_energy_kwh=current_battery_energy,
            battery_capacity_kwh=battery_capacity,
            battery_charge_efficiency=HOME_BATTERY_CHARGE_EFFICIENCY,
            battery_discharge_efficiency=HOME_BATTERY_DISCHARGE_EFFICIENCY,
            top_up_pv_confidence_factor=EXPORT_PV_CONFIDENCE_FACTOR,
            allow_economic_export=(
                not balance_chase_active and economic_export_floor is not None
            ),
        )
        optional_export_power_schedule = dict(schedule_plan.battery_power_delta_schedule)

        current_period_start = None
        for detail_entry in neutral_forecast.detail:
            if detail_entry.period_start <= t_now and (
                current_period_start is None or detail_entry.period_start > current_period_start
            ):
                current_period_start = detail_entry.period_start
        if current_period_start is None:
            current_period_start = neutral_forecast.detail[0].period_start
        current_period_key = current_period_start.isoformat()

        desired_current_optional_export_w = optional_export_power_schedule.get(
            current_period_key,
            0.0,
        )
        battery_soc_percent = (
            100.0 * current_battery_energy / battery_capacity
            if battery_capacity > 0
            else 0.0
        )
        live_pv_power_w = live_pv_power_for_planning
        forecast_or_live_pv_peak_w = max(raw_peak_w, live_pv_power_w)
        proactive_export_power_w = max(0.0, desired_current_optional_export_w)
        proactive_export_active = (
            battery_soc_percent >= PROACTIVE_EXPORT_TRIGGER_SOC_PERCENT
            and forecast_or_live_pv_peak_w
            >= max_pv_feedin + PROACTIVE_EXPORT_PV_MARGIN_W
            and schedule_plan.headroom_energy_kwh > 0.01
            and proactive_export_power_w > 0.0
        )
        if proactive_export_active:
            log.warning(
                f"Price-ranked proactive PV-ramp reserve active: SOC "
                f"{battery_soc_percent:.1f}%, PV peak "
                f"{forecast_or_live_pv_peak_w:.0f} W, current scheduled export "
                f"{proactive_export_power_w:.0f} W"
            )

        # Make switching modes and bucket transitions gentle.  Ramp only the
        # current optional request; future buckets remain the scheduler output.
        desired_current_target = max(
            -planned_max_grid_export_w,
            min(
                max_setpoint,
                max_setpoint - max(0.0, desired_current_optional_export_w),
            ),
        )
        previous_target = get(Grid.power_setpoint_target, max_setpoint)
        applied_current_target = limit_target_step(
            desired_current_target,
            previous_target,
            max_step_w=(
                PROACTIVE_EXPORT_MAX_STEP_W
                if proactive_export_active
                else 1500
            ),
        )
        optional_export_power_schedule[current_period_key] = max(
            0.0,
            max_setpoint - applied_current_target,
        )
        optional_export_power_schedule = fit_export_schedule_to_energy_budget(
            optional_export_power_schedule,
            scheduler_slots,
            schedule_plan.allocated_energy_kwh,
            locked_keys={current_period_key},
        )

        headroom_hold_schedule = dict(schedule_plan.headroom_hold_schedule)
        for key, hold_power_w in headroom_hold_schedule.items():
            optional_export_power_schedule[key] = max(
                optional_export_power_schedule.get(key, 0.0),
                hold_power_w,
            )

        # Once hard headroom has been created, keep it available through the
        # critical solar window. These controls route PV to the grid without
        # authorizing extra battery discharge, so they are applied after the
        # battery-energy budget fit.
        hard_cap_hold_schedule = build_hard_cap_storage_hold_schedule(
            scheduler_slots,
            max_grid_export_w=planned_max_grid_export_w,
            neutral_grid_target_w=max_setpoint,
            min_export_price_per_kwh=min_discharge_price,
        )
        for key, hold_power_w in hard_cap_hold_schedule.items():
            optional_export_power_schedule[key] = max(
                optional_export_power_schedule.get(key, 0.0),
                hold_power_w,
            )

        # Silent-mode charge shaping: export neutral surplus above the silent
        # power (past the margin band) so the replay charges at most
        # 3.5 kW, matching what the live loop enforces.  Merged with max()
        # like the holds, before the replay and the energy accounting.
        silent_shaping_energy_kwh = 0.0
        if silent_mode_active:
            silent_shaping_schedule = build_silent_charge_shaping_schedule(
                [
                    (
                        detail_entry.period_start.isoformat(),
                        detail_entry.pv_estimate,
                        detail_entry.house_power,
                        detail_entry.ev_charge_power,
                    )
                    for detail_entry in neutral_forecast.detail
                ],
                silent_power_w=SILENT_MODE_MAX_BATTERY_POWER_W,
                surplus_margin_w=SILENT_MODE_CHARGE_SURPLUS_MARGIN_W,
            )
            slot_hours_by_key = {
                scheduler_slot.period_start.isoformat(): max(
                    0.0, scheduler_slot.duration_hours
                )
                for scheduler_slot in scheduler_slots
            }
            for key, shaping_power_w in silent_shaping_schedule.items():
                previous_power_w = optional_export_power_schedule.get(key, 0.0)
                optional_export_power_schedule[key] = max(
                    previous_power_w,
                    shaping_power_w,
                )
                if shaping_power_w > previous_power_w:
                    silent_shaping_energy_kwh += (
                        shaping_power_w * slot_hours_by_key.get(key, 0.0) / 1000
                    )

        scheduled_control_energy_kwh = 0.0
        for scheduler_slot in scheduler_slots:
            scheduled_control_energy_kwh += (
                optional_export_power_schedule.get(
                    scheduler_slot.period_start.isoformat(),
                    0.0,
                )
                * scheduler_slot.duration_hours
                / 1000
            )
        scheduled_export_energy_kwh = schedule_plan.allocated_energy_kwh

        unified_forecast = forecast_setpoint_local(
            forecast_ctx,
            pv_forecast=epex_pv_forecast,
            setpoint=max_setpoint,
            setpoint_spread=0.01,
            current_battery_energy=current_battery_energy,
            with_ev_charging=with_ev_charging,
            ev_energy=ev_energy,
            max_battery_power_target=max_battery_discharge_w,
            configured_max_feedin=planned_max_grid_export_w,
            grid_target_schedule=None,
            optional_export_power_schedule=optional_export_power_schedule,
            battery_floor_schedule=battery_floor_schedule,
            surplus_energy=policy_surplus_energy_kwh,
            battery_export_budget_kwh=scheduled_export_energy_kwh,
        )
        if unified_forecast is None or not unified_forecast.detail:
            log.error("Unified export scheduler replay failed; keeping the previous target")
            return

        baseline_by_start = {}
        for detail_entry in neutral_forecast.detail:
            baseline_by_start[detail_entry.period_start.isoformat()] = detail_entry
        unsafe_import_increase_w = 0.0
        for detail_entry in unified_forecast.detail:
            baseline_entry = baseline_by_start.get(detail_entry.period_start.isoformat())
            if baseline_entry is None or detail_entry.ev_charge_power > 1:
                continue
            unsafe_import_increase_w = max(
                unsafe_import_increase_w,
                detail_entry.power_from_grid - baseline_entry.power_from_grid,
            )
        if unsafe_import_increase_w > 50:
            log.error(
                f"Unified schedule would add {unsafe_import_increase_w:.0f} W grid import; "
                "falling back to the neutral trajectory"
            )
            optional_export_power_schedule = {}
            headroom_hold_schedule = {}
            hard_cap_hold_schedule = {}
            unified_forecast = neutral_forecast
            scheduled_export_energy_kwh = 0.0
            scheduled_control_energy_kwh = 0.0
            allocated_headroom_energy_kwh = 0.0
            unallocated_headroom_energy_kwh = headroom_energy_kwh
            hard_headroom_shortfall_kwh = hard_headroom_energy_kwh
        else:
            allocated_headroom_energy_kwh = schedule_plan.headroom_energy_kwh
            unallocated_headroom_energy_kwh = schedule_plan.unallocated_headroom_kwh
            hard_headroom_shortfall_kwh = schedule_plan.hard_headroom_shortfall_kwh

        # Attribute this run's commanded headroom power to the spill event so
        # the next run nets it (see above).  The unsafe-import fallback
        # clears the schedule, so nothing is released then; hard-cap runs
        # rebase the clock without accumulating (unscaled, see above).
        scheduled_headroom_power_by_key = (
            schedule_plan.headroom_power_schedule or {}
        )
        headroom_ledger_update = update_headroom_release_ledger(
            headroom_release_event_key,
            (
                0.0
                if unsafe_import_increase_w > 50
                or not headroom_release_scaling_active
                else scheduled_headroom_power_by_key.get(current_period_key, 0.0)
            ),
            t_now,
            get("pyscript.headroom_release_ledger", ""),
        )
        if headroom_ledger_update.valid:
            state.set(
                "pyscript.headroom_release_ledger",
                headroom_ledger_update.state_value,
            )
            headroom_released_kwh = headroom_ledger_update.released_kwh
        else:
            log.warning(
                "Headroom release ledger invalid "
                f"({headroom_ledger_update.reason}); pre-release unscaled this run"
            )

        current_detail = unified_forecast.detail[0]
        for detail_entry in unified_forecast.detail:
            if detail_entry.period_start <= t_now and detail_entry.period_start >= current_detail.period_start:
                current_detail = detail_entry

        current_target = current_detail.setpoint
        current_optional_export_w = optional_export_power_schedule.get(current_period_key, 0.0)
        current_planned_floor_kwh = battery_floor_schedule.get(
            current_detail.period_start.isoformat(),
            battery_min_energy,
        )
        published_optional_export_schedule = {
            key: round(value, 1)
            for key, value in optional_export_power_schedule.items()
            if value > 0.1
        }
        max_forecast_feedin_w = max([entry.feedin for entry in unified_forecast.detail] or [0])
        if max_forecast_feedin_w > hard_max_grid_export_w + 50:
            log.warning(
                f"Unified schedule forecasts unavoidable feed-in {max_forecast_feedin_w:.0f} W "
                f"above hard combined target {hard_max_grid_export_w:.0f} W"
            )
        if balance_chase_active:
            if days_since_full_charge is None:
                chase_detail = "no full-charge history"
            else:
                chase_detail = f"{days_since_full_charge:.1f} days since full"
            chase_suffix = (
                f", BALANCE CHASE ({chase_detail}, "
                "economic + preferred export withheld)"
            )
        else:
            chase_suffix = ""
        silent_suffix = (
            f", SILENT ({silent_shaping_energy_kwh:.2f} kWh shaped, "
            f"export capped at {SILENT_MODE_MAX_BATTERY_POWER_W:.0f} W)"
            if silent_mode_active
            else ""
        )
        blocked_suffix = (
            f", ECONOMIC BLOCKED (feed-in cap {feedin_compensation_cap})"
            if economic_export_floor is None
            else ""
        )
        log.warning(
            f"Unified optional export schedule: {scheduled_export_energy_kwh:.2f} kWh requested "
            f"({allocated_headroom_energy_kwh:.2f}/{headroom_energy_kwh:.2f} kWh preferred PV headroom, "
            f"{headroom_released_kwh:.2f} kWh already released, "
            f"hard cap {hard_headroom_energy_kwh:.2f} kWh with "
            f"{hard_headroom_shortfall_kwh:.2f} kWh deadline shortfall, "
            f"reference {headroom_reference_price:.3f}/kWh), "
            f"safe budget {safe_export_energy_kwh:.2f} kWh, raw PV peak {raw_peak_w:.0f} W at "
            f"{raw_peak_time}, current target {current_target:.0f} W, "
            f"optional export {current_optional_export_w:.0f} W, "
            f"planned battery {current_detail.battery_power:.0f} W, "
            f"floor {current_planned_floor_kwh:.2f} kWh"
            f"{chase_suffix}"
            f"{silent_suffix}"
            f"{blocked_suffix}"
        )

        detail_vectorized = {
            key: [getattr(entry, key) for entry in unified_forecast.detail]
            for key in ForecastEntry.__annotations__.keys()
        }
        set_state(Grid.power_setpoint_basis, current_target, **grid_setpoint_basis_attributes)
        set_state(
            Grid.power_setpoint_target,
            current_target,
            **power_w_attributes,
            desired_setpoint=round(desired_current_target, 1),
            unified_scheduler_active=True,
            # Clear large absolute schedules left by earlier unified versions.
            grid_target_schedule={},
            battery_power_delta_schedule={},
            battery_floor_schedule={},
            optional_export_power_schedule=published_optional_export_schedule,
            desired_optional_export_power_w=round(desired_current_optional_export_w, 1),
            planned_optional_export_power_w=round(current_optional_export_w, 1),
            planned_battery_power_w=round(current_detail.battery_power, 1),
            planned_battery_floor_kwh=round(current_planned_floor_kwh, 3),
            planned_house_power_w=round(current_detail.house_power, 1),
            planned_pv_power_w=round(current_detail.pv_estimate, 1),
            planned_ev_charge_power_w=round(current_detail.ev_charge_power, 1),
            ev_available_for_charging=bool(wallbox_connected),
            ev_is_charging=bool(ev_is_charging),
            ev_energy_needed_kwh=round(ev_energy_needed, 2),
            ev_export_hold_active=ev_export_hold_active,
            economic_export_blocked=economic_export_floor is None,
            economic_export_floor=(
                round(economic_export_floor, 4)
                if economic_export_floor is not None
                else None
            ),
            ev_arrival_reservation_active=ev_arrival_reservation_active,
            ev_arrival_reserved_kwh=round(ev_arrival_reserved_kwh, 3),
            export_block_reason=export_block_reason,
            balance_chase_active=balance_chase_active,
            days_since_full_charge=(
                round(days_since_full_charge, 2)
                if days_since_full_charge is not None
                else None
            ),
            top_up_pv_confidence_factor=EXPORT_PV_CONFIDENCE_FACTOR,
            export_policy_valid=export_policy_valid,
            export_policy_reason=export_policy_reason,
            export_policy_calculated_at=(
                published_surplus_calculated_at.isoformat()
                if published_surplus_calculated_at is not None
                else None
            ),
            export_policy_source_battery_kwh=(
                round(float(published_surplus_battery_energy_kwh), 3)
                if export_policy_valid
                else None
            ),
            export_policy_battery_draw_kwh=round(
                export_policy_budget.battery_draw_since_calculation_kwh,
                3,
            ),
            safe_export_source_battery_kwh=round(battery_energy, 3),
            safe_export_calculated_at=t_now.isoformat(),
            plan_calculated_at=t_now.isoformat(),
            max_battery_power_target=max_battery_discharge_w,
            max_grid_export_w=hard_max_grid_export_w,
            planned_max_grid_export_w=planned_max_grid_export_w,
            export_cap_margin_w=export_cap_margin_w,
            export_house_load_margin_fraction=EXPORT_HOUSE_LOAD_MARGIN_FRACTION,
            export_pv_confidence_factor=EXPORT_PV_CONFIDENCE_FACTOR,
            export_base_uncertainty_margin_kwh=EXPORT_BASE_UNCERTAINTY_MARGIN_KWH,
            home_battery_charge_efficiency=HOME_BATTERY_CHARGE_EFFICIENCY,
            home_battery_discharge_efficiency=(
                HOME_BATTERY_DISCHARGE_EFFICIENCY
            ),
            export_broker_revenue_margin_fraction=(
                EXPORT_BROKER_REVENUE_MARGIN_FRACTION
            ),
            export_price_source=(
                ElectricityPrices.epex_export_forecast_prices
            ),
            export_price_fallback_periods=missing_export_price_periods,
            safe_export_energy_kwh=round(safe_export_energy_kwh, 3),
            safe_stored_energy_kwh=round(safe_stored_energy_kwh, 3),
            physical_safe_export_energy_kwh=round(
                physical_safe_export_energy_kwh,
                3,
            ),
            published_policy_surplus_energy_kwh=round(
                published_policy_surplus_energy_kwh,
                3,
            ),
            published_surplus_age_seconds=(
                round(published_surplus_age_seconds, 1)
                if published_surplus_age_seconds is not None
                else None
            ),
            published_local_ev_wall_energy_kwh=round(
                published_local_ev_wall_energy_kwh,
                3,
            ),
            policy_surplus_energy_kwh=round(policy_surplus_energy_kwh, 3),
            ev_obligation_due_within_horizon=ev_obligation_due_within_horizon,
            scheduled_export_energy_kwh=round(scheduled_export_energy_kwh, 3),
            scheduled_control_energy_kwh=round(scheduled_control_energy_kwh, 3),
            hard_cap_hold_schedule={
                key: round(value, 1)
                for key, value in hard_cap_hold_schedule.items()
                if value > 0.1
            },
            headroom_hold_schedule={
                key: round(value, 1)
                for key, value in headroom_hold_schedule.items()
                if value > 0.1
            },
            headroom_constraint_active=(
                headroom_energy_kwh > 0 or hard_headroom_energy_kwh > 0
            ),
            proactive_export_active=proactive_export_active,
            proactive_export_power_w=round(proactive_export_power_w, 1),
            silent_mode_active=silent_mode_active,
            silent_max_battery_power_w=round(SILENT_MODE_MAX_BATTERY_POWER_W, 1),
            silent_shaping_energy_kwh=round(silent_shaping_energy_kwh, 3),
            headroom_control_w=current_target,
            headroom_energy_kwh=round(headroom_energy_kwh, 3),
            headroom_allocated_kwh=round(allocated_headroom_energy_kwh, 3),
            headroom_released_kwh=round(headroom_released_kwh, 3),
            headroom_reference_price=round(headroom_reference_price, 5),
            headroom_min_price_spread=round(headroom_min_price_spread, 5),
            headroom_is_soft=False,
            headroom_unallocated_kwh=round(unallocated_headroom_energy_kwh, 3),
            hard_headroom_energy_kwh=round(hard_headroom_energy_kwh, 3),
            hard_headroom_shortfall_kwh=round(hard_headroom_shortfall_kwh, 3),
            headroom_deadline=(
                next_headroom_deadline.isoformat()
                if next_headroom_deadline is not None
                else None
            ),
            headroom_schedule={},
            raw_pv_feedin_peak_w=round(raw_peak_w, 1),
            observed_pv_forecast_error_w=round(
                high_side_headroom.observed_pv_error_w,
                1,
            ),
            pv_power_measurement_fresh=pv_power_measurement_fresh,
            high_side_headroom_energy_kwh=round(
                high_side_headroom_energy_kwh,
                3,
            ),
            high_side_natural_export_peak_w=round(
                high_side_headroom.peak_high_side_natural_export_w,
                1,
            ),
            quiet_efficient_power_w=round(efficient_discharge_w, 1),
            quiet_price_tolerance_percent=round(quiet_price_tolerance * 100, 1),
            scheduled_export_net_prices={
                slot.period_start.isoformat(): round(slot.price_per_kwh, 4)
                for slot in scheduler_slots
                if optional_export_power_schedule.get(
                    slot.period_start.isoformat(), 0.0
                )
                > 0.0
            },
            detail=detail_vectorized,
        )
        return

    initial_forecast = forecast_setpoint_local(
        forecast_ctx,
        pv_forecast=epex_pv_forecast,
        setpoint=current_setpoint if automation_disabled else max_setpoint,
        with_ev_charging=with_ev_charging,
        ev_energy=ev_energy,
        configured_max_feedin=max_feedin_limit,
        setpoint_spread=0.01,
        current_battery_energy=current_battery_energy,
        max_battery_power_target=8000,  # TODO: make this configurable
    )

    if not automation_disabled and initial_forecast.max_feedin < 200:
        skip_automation_message = "No significant feedin expected"
    battery_too_low = (
        battery_energy <= battery_min_energy + 0.5
        or initial_forecast.min_bat < battery_min_energy + 0.5
    )
    if battery_too_low:
        skip_automation_message = "Battery too low in forecast, skipping automation to avoid depletion"

    if skip_automation_message:
        fallback_setpoint = initial_forecast.setpoint
        if battery_too_low:
            house_power = get(House.loads, house_avg_power)
            fallback_setpoint = get_pv_only_setpoint(
                pv_power_total,
                house_power,
                8000,
            )
        log.warning(f"{skip_automation_message}, setting setpoint to {fallback_setpoint}")

        set_state(
            Grid.power_setpoint_target,
            fallback_setpoint,
            **power_w_attributes,
            desired_setpoint=fallback_setpoint,
            unified_scheduler_active=False,
            grid_target_schedule={},
            headroom_constraint_active=False,
            headroom_control_w=fallback_setpoint,
            headroom_energy_kwh=0.0,
            headroom_deadline=None,
            headroom_schedule={},
            detail=initial_forecast.detail,
        )
        set_state(
            Grid.power_setpoint_basis,
            fallback_setpoint,
            **grid_setpoint_basis_attributes,
        )
        return

    search_results = setpoint_binary_search(
        forecast_ctx,
        epex_pv_forecast,
        min_setpoint=min_setpoint,
        max_setpoint=max_setpoint,
        with_ev_charging=with_ev_charging,
        battery_energy=current_battery_energy,
        ev_energy=ev_energy,
        logging=logging,
        current_spread=0.01,
    )

    if logging:
        log.warning(format_setpoint_results(search_results, "initial search", t_now))

    assert search_results[-1].setpoint_spread is not None

    search_results += setpoint_binary_search(
        forecast_ctx,
        epex_pv_forecast,
        min_setpoint=search_results[-1].setpoint - 200,
        max_setpoint=search_results[-1].setpoint + 200,
        with_ev_charging=with_ev_charging,
        battery_energy=current_battery_energy,
        ev_energy=ev_energy,
        logging=logging,
        current_setpoint=search_results[-1].setpoint,
        min_spread=1e-5,
        update_spread=True,
        update_setpoint=False,
        # log_setpoint=True,
    )

    initial_result = search_results[-1]

    assert initial_result.setpoint_spread is not None
    forecast_setpoint_local(
        forecast_ctx,
        epex_pv_forecast,
        setpoint=initial_result.setpoint,
        with_ev_charging=with_ev_charging,
        ev_energy=ev_energy,
        configured_max_feedin=max_feedin_limit,
        setpoint_spread=initial_result.setpoint_spread,
        current_battery_energy=battery_energy,
        max_battery_power_target=8000,
        logging=False,
    )

    max_pv_feedin_today = max(
        [d.pv_feedin for d in initial_result.detail if d.period_start.day == t_now.day and d.period_start > t_now],
        default=0,
    )
    today_samples = [
        (entry.period_start, entry.pv_feedin)
        for entry in initial_result.detail
        if entry.period_start.day == t_now.day
    ]
    today_candidate = FeedinCandidate(
        control_value=initial_result.setpoint,
        predicted_peak_w=max_pv_feedin_today,
        overflow_energy_kwh=calculate_pv_overflow_energy(today_samples, max_pv_feedin, t_now),
    )
    today_constraint_exceeded = feedin_constraint_exceeded(today_candidate, max_pv_feedin)
    discounted_pv_feedin, discounted_peak_time, raw_pv_feedin = get_discounted_pv_feedin_peak(initial_result, t_now)
    log.warning(
        f" \n\ninitial_result.max_pv_feedin {raw_pv_feedin:.0f} W, discounted {discounted_pv_feedin:.0f} W at {discounted_peak_time.strftime('%d %H:%M')}\n"
        f" max pv feedin today {max_pv_feedin_today:.0f} W, overflow {today_candidate.overflow_energy_kwh:.3f} kWh, "
        f"constraint active: {today_constraint_exceeded}\n\n"
    )
    if today_constraint_exceeded:
        t_start = max(t_now, t_now.replace(hour=8))

        t_end = next(
            iter(
                [
                    e.period_start
                    for e in epex_pv_forecast
                    if e.period_start > t_start
                    and e.period_start.hour > 16
                    and (e.pv_estimate * 1000 - house_avg_power) < (max_feedin_limit / 2)
                ]
            ),
            t_start + timedelta(hours=8),
        )
        new_price_forecast = [el for el in epex_pv_forecast if t_start < el.period_start <= t_end]

        # log tstart and tend
        msg = f" \n\nSearching for feedin setpoint at {t_start.strftime('%m-%d between %H:%M')} and {t_end.strftime('%H:%M')}\n"
        msg += ", ".join([f"{e.period_start.strftime('%H:%M')}: {e.pv_estimate:.1f}kW" for e in new_price_forecast])

        start_detail = next(iter([e for e in initial_result.detail if e.period_start >= t_start]), None)
        if len(new_price_forecast) < 2:
            search_results = [initial_result]
            price_forecast = epex_pv_forecast
        else:
            price_forecast = new_price_forecast
            search_results = setpoint_binary_search(
                forecast_ctx,
                price_forecast,
                min_setpoint=min_setpoint,
                max_setpoint=max_setpoint,
                with_ev_charging=with_ev_charging,
                battery_energy=start_detail.battery_energy,
                ev_energy=start_detail.ev_energy,
                logging=logging,
                current_setpoint=initial_result.setpoint,
                update_spread=True,
                update_setpoint=False,
                log_setpoint=True,
            )

        log.warning(msg + "\n" + format_setpoint_results(search_results, "update spread", t_now))
        new_result = search_results[-1]
        assert new_result is not None

        if logging:
            log.warning(
                f" \n\nnew_result.discounted_pv_feedin {get_discounted_pv_feedin_peak(new_result, t_now)[0]:0f} > max_pv_feedin {max_pv_feedin:0f}: {get_discounted_pv_feedin_peak(new_result, t_now)[0] > max_pv_feedin}\n\n!!"
            )
        msg = ""

        # if the end time is before the time limit, need to forecast again for the remaining time
        rest_forecast = [el for el in epex_pv_forecast if el.period_start > t_end]

        if rest_forecast:
            log.warning("starting rest forecast")
            rest_result = setpoint_binary_search(
                forecast_ctx,
                rest_forecast,
                min_setpoint=-max_feedin_limit,
                max_setpoint=max_setpoint,
                with_ev_charging=True,
                battery_energy=new_result.detail[-1].battery_energy,
                ev_energy=new_result.detail[-1].ev_energy,
                logging=logging,
                current_spread=new_result.setpoint_spread,
                log_setpoint=True,
            )[-1]

            final_result = merge_forecast_results(
                new_result,
                rest_result,
                t_split=t_end,
            )
            log.warning(f"merged \n\t{replace(rest_result, detail=None)} into \n\t{replace(new_result, detail=None)}")
            search_results.append(final_result)

        # if t_start > t_now:
        #     final_result = merge_forecast_results(
        #         initial_result,
        #         new_result,
        #         t_split=t_start,
        #     )
        #     final_result.max_battery_power_target = new_result.max_battery_power_target

    log.warning(format_setpoint_results(search_results, "final result", t_now))

    setpoint_result = search_results[-1]

    price = max(min_feedin_price, get(ElectricityPrices.epex_forecast_prices, min_feedin_price))

    pv_power_total = get(PVProduction.total_power, 0)
    house_power = get(House.loads, 0)
    surplus_energy = get(House.energy_surplus, 0)

    price_mapped_setpoint = map_setpoint(
        setpoint_result.setpoint,
        price,
        setpoint_result.prices_mean,
        setpoint_result.prices_std,
        battery_energy=battery_energy,
        battery_min_limit=battery_min_energy,
        pv_power=pv_power_total,
        house_power=house_power,
        max_feedin=max_feedin_limit,
        setpoint_spread=setpoint_result.setpoint_spread,
        max_battery_power_target=setpoint_result.max_battery_power_target,
        surplus_energy=surplus_energy,
    )
    final_feedin_candidate = get_feedin_candidate(
        setpoint_result,
        setpoint_result.setpoint,
        forecast_ctx,
    )
    hard_feedin_constraint_active = feedin_constraint_exceeded(
        final_feedin_candidate,
        max_pv_feedin,
    )
    _discounted_peak, headroom_deadline, _raw_peak = get_discounted_pv_feedin_peak(setpoint_result, t_now)
    available_headroom_energy_kwh = max(0.0, battery_energy - battery_min_energy)
    headroom_energy_kwh = (
        calculate_headroom_energy_requirement(
            final_feedin_candidate.overflow_energy_kwh,
            available_headroom_energy_kwh,
        )
        if hard_feedin_constraint_active
        else 0.0
    )
    # The optimized result can start at the next half-hour boundary after a
    # merge. Recover the actual current EPEX bucket from the original forecast
    # so the live target and dashboard use the same published schedule key.
    current_price_period_start = None
    for entry in epex_pv_forecast:
        if entry.period_start <= t_now and (
            current_price_period_start is None or entry.period_start > current_price_period_start
        ):
            current_price_period_start = entry.period_start
    if current_price_period_start is None:
        current_price_period_start = t_now
    headroom_samples = [(current_price_period_start, price, price_mapped_setpoint)]
    for entry in setpoint_result.detail:
        if t_now < entry.period_start <= headroom_deadline:
            headroom_samples.append((entry.period_start, entry.epex_price, entry.setpoint))
    headroom_schedule = build_price_aware_headroom_schedule(
        samples=headroom_samples,
        required_energy_kwh=headroom_energy_kwh,
        deadline=headroom_deadline,
        t_now=t_now,
        max_export_w=min(max_feedin_limit, setpoint_result.max_battery_power_target),
    )
    scheduled_headroom_w = headroom_schedule.get(current_price_period_start.isoformat())
    current_pv_surplus = max(0.0, pv_power_total - house_power)
    desired_setpoint = apply_hard_feedin_control(
        price_mapped_control_w=price_mapped_setpoint,
        headroom_control_w=scheduled_headroom_w,
        constraint_active=hard_feedin_constraint_active,
    )
    if hard_feedin_constraint_active:
        current_schedule_message = (
            f"{scheduled_headroom_w:.0f} W"
            if scheduled_headroom_w is not None
            else "normal price mapping"
        )
        log.warning(
            f"Price-aware headroom schedule: {headroom_energy_kwh:.2f} kWh by "
            f"{headroom_deadline.strftime('%H:%M')}, {len(headroom_schedule)} boosted price buckets; "
            f"current bucket {current_schedule_message}"
        )
    if hard_feedin_constraint_active and desired_setpoint != price_mapped_setpoint:
        log.warning(
            f"Scheduled forecast headroom control: enforcing {desired_setpoint:.0f} W "
            f"instead of price-mapped {price_mapped_setpoint:.0f} W; "
            f"current PV surplus {current_pv_surplus:.0f} W, "
            f"peak {final_feedin_candidate.predicted_peak_w:.0f} W, "
            f"overflow {final_feedin_candidate.overflow_energy_kwh:.2f} kWh"
        )
    previous_setpoint_target = get(Grid.power_setpoint_target, desired_setpoint)
    setpoint = limit_target_step(desired_setpoint, previous_setpoint_target)

    if logging:
        log.warning(
            f"\nMapped setpoint: {setpoint_result.setpoint:.0f} to desired {desired_setpoint:.0f}, "
            f"slew-limited to {setpoint:.0f} from {previous_setpoint_target:.0f}, "
            f"with spread {setpoint_result.setpoint_spread:.2f} and max batt power {setpoint_result.max_battery_power_target} W\n"
            f"price now {price:.2f} mean {setpoint_result.prices_mean:.2f} price std {setpoint_result.prices_std:.2f} "
            f"surplus {surplus_energy:.2f} kWh min_bat {setpoint_result.min_bat:.1f} at {setpoint_result.t_min_bat.strftime('%H:%M')} "
        )

    set_state(
        Grid.power_setpoint_basis,
        setpoint_result.setpoint,
        **grid_setpoint_basis_attributes,
    )

    detail_with_ev_vectorized = {
        k: [getattr(el, k) for el in setpoint_result.detail] for k in ForecastEntry.__annotations__.keys()
    }

    set_state(
        Grid.power_setpoint_target,
        setpoint,
        **power_w_attributes,
        desired_setpoint=round(desired_setpoint, 1),
        unified_scheduler_active=False,
        grid_target_schedule={},
        max_battery_power_target=setpoint_result.max_battery_power_target,
        headroom_constraint_active=hard_feedin_constraint_active,
        headroom_control_w=round(scheduled_headroom_w, 1) if scheduled_headroom_w is not None else None,
        headroom_energy_kwh=round(headroom_energy_kwh, 3),
        headroom_deadline=headroom_deadline.isoformat() if hard_feedin_constraint_active else None,
        headroom_schedule=headroom_schedule,
        detail=detail_with_ev_vectorized,
    )


prev_house_loads = None
prev_optional_export_ev_plan_hold = None
live_headroom_active = False
live_headroom_target_kwh = 0.0
live_headroom_hold_until = None
prev_live_headroom_mode = None
last_live_headroom_publish = None
prev_applied_grid_target_w = None
prev_telemetry_hold_active = False
prev_discharge_limit_before_hold = None
prev_telemetry_good_streak = 0
prev_telemetry_bad_streak = 0
prev_telemetry_hold_boot_checked = False


@time_trigger
@time_trigger("period(now, 5sec)")
def auto_apply_setpoint():
    logging = False
    msg = ""
    if not get(Automation.auto_setpoint, False):
        msg = "Auto setpoint disabled"
        return

    global prev_house_loads
    global prev_optional_export_ev_plan_hold
    global live_headroom_active
    global live_headroom_target_kwh
    global live_headroom_hold_until
    global prev_live_headroom_mode
    global last_live_headroom_publish
    global prev_applied_grid_target_w
    global prev_telemetry_hold_active
    global prev_discharge_limit_before_hold
    global prev_telemetry_good_streak
    global prev_telemetry_bad_streak
    global prev_telemetry_hold_boot_checked
    current_control_time = now()

    max_setpoint = select_finite_grid_target(
        candidate_grid_target_w=get(Grid.max_setpoint, -20),
        fallback_grid_target_w=-20.0,
    )
    if max_setpoint is None:
        return
    msg += f" max setpoint {max_setpoint}"
    raw_house_power_long_term_average = get(House.daily_average_power, 0)  # W
    house_power_long_term_average_valid = are_power_measurement_values_valid(
        raw_house_power_long_term_average,
        0.0,
        max_absolute_power_w=LIVE_POWER_MAX_ABSOLUTE_W,
    ) and float(raw_house_power_long_term_average) >= 0.0
    house_power_long_term_average = (
        float(raw_house_power_long_term_average)
        if house_power_long_term_average_valid
        else 500.0
    )
    raw_house_loads = get(House.loads, None)
    house_load_updated_at = get_attr(House.loads, "last_updated")
    house_load_measurement_age_seconds = calculate_measurement_age_seconds(
        current_control_time,
        house_load_updated_at,
    )
    house_load_value_valid = (
        raw_house_loads is not None
        and are_power_measurement_values_valid(
            raw_house_loads,
            0.0,
            max_absolute_power_w=LIVE_POWER_MAX_ABSOLUTE_W,
        )
        and float(raw_house_loads) >= 0.0
    )
    house_load_measurement_fresh = house_load_value_valid and is_measurement_fresh(
        current_control_time,
        house_load_updated_at,
        max_age_seconds=LIVE_HOUSE_LOAD_MAX_MEASUREMENT_AGE_SECONDS,
    )
    previous_house_load_valid = (
        prev_house_loads is not None
        and are_power_measurement_values_valid(
            prev_house_loads,
            0.0,
            max_absolute_power_w=LIVE_POWER_MAX_ABSOLUTE_W,
        )
        and float(prev_house_loads) >= 0.0
    )
    if not previous_house_load_valid:
        prev_house_loads = house_power_long_term_average
    if house_load_measurement_fresh:
        bounded_house_loads = max(
            float(raw_house_loads),
            house_power_long_term_average * 0.5,
        )
        house_loads = prev_house_loads * 0.5 + bounded_house_loads * 0.5
        prev_house_loads = house_loads
    else:
        house_loads = prev_house_loads
    setpoint_target = select_finite_grid_target(
        candidate_grid_target_w=get(Grid.power_setpoint_target, 0),
        fallback_grid_target_w=max_setpoint,
    )
    max_setpoint_target = select_finite_grid_target(
        candidate_grid_target_w=get(Grid.max_feedin_target, 0),
        fallback_grid_target_w=0.0,
    )
    if setpoint_target is None or max_setpoint_target is None:
        return
    unified_scheduler_active = get_attr(Grid.power_setpoint_target, "unified_scheduler_active", False)
    live_pv_power_w = get(PVProduction.total_power, 0)
    pv_power_updated_at = get_attr(PVProduction.total_power, "last_updated")
    pv_measurement_age_seconds = calculate_measurement_age_seconds(
        current_control_time,
        pv_power_updated_at,
    )
    pv_power_value_valid, pv_power_measurement_fresh = validate_pv_power_measurement(
        live_pv_power_w,
        pv_power_updated_at,
        current_control_time,
        LIVE_PV_MAX_MEASUREMENT_AGE_SECONDS,
    )
    pv_power_for_control_w = (
        live_pv_power_w if pv_power_measurement_fresh else 0.0
    )
    ev_is_charging = get(EV.is_charging, False)
    ev_available_for_charging = is_ev_available_for_charging(
        charger_ready=get(Charger.ready, False),
        charger_control_enabled=get(Charger.control_switch, False),
        ev_is_charging=ev_is_charging,
    )
    ev_energy_needed = get(EV.energy_needed, 0)
    planned_ev_available = get_attr(
        Grid.power_setpoint_target,
        "ev_available_for_charging",
        None,
    )
    planned_ev_is_charging = get_attr(
        Grid.power_setpoint_target,
        "ev_is_charging",
        None,
    )
    planned_ev_energy_needed = get_attr(
        Grid.power_setpoint_target,
        "ev_energy_needed_kwh",
        None,
    )
    plan_calculated_at = get_attr(
        Grid.power_setpoint_target,
        "plan_calculated_at",
        None,
    )
    export_policy_calculated_at = parse_aware_datetime(
        get_attr(
            Grid.power_setpoint_target,
            "export_policy_calculated_at",
            None,
        )
    )
    export_policy_fresh = bool(
        get_attr(Grid.power_setpoint_target, "export_policy_valid", False)
    ) and is_measurement_fresh(
        current_control_time,
        export_policy_calculated_at,
        max_age_seconds=LIVE_EXPORT_BUDGET_HORIZON_SECONDS,
    )
    optional_export_ev_plan_hold = unified_scheduler_active and not is_ev_plan_fresh(
        planned_ev_available=planned_ev_available,
        planned_ev_is_charging=planned_ev_is_charging,
        planned_ev_energy_needed_kwh=planned_ev_energy_needed,
        plan_calculated_at=plan_calculated_at,
        live_ev_available=ev_available_for_charging,
        live_ev_is_charging=ev_is_charging,
        live_ev_energy_needed_kwh=ev_energy_needed,
        current_time=now(),
    )

    if optional_export_ev_plan_hold != prev_optional_export_ev_plan_hold:
        if optional_export_ev_plan_hold:
            log.warning(
                "Holding optional battery export neutral for stale EV plan: "
                f"planned available={planned_ev_available}, charging={planned_ev_is_charging}, "
                f"energy needed={planned_ev_energy_needed}, calculated={plan_calculated_at}; "
                f"live available={ev_available_for_charging}, charging={ev_is_charging}, "
                f"energy needed={ev_energy_needed:.2f} kWh"
            )
        elif prev_optional_export_ev_plan_hold:
            log.warning("Releasing optional battery export stale-EV-plan hold")
        prev_optional_export_ev_plan_hold = optional_export_ev_plan_hold

    if unified_scheduler_active:
        planned_optional_export_power_w = get_attr(
            Grid.power_setpoint_target,
            "planned_optional_export_power_w",
            0,
        )
        planned_battery_power_w = get_attr(
            Grid.power_setpoint_target,
            "planned_battery_power_w",
            0,
        )
        planned_max_grid_export_w = select_finite_grid_target(
            candidate_grid_target_w=get_attr(
                Grid.power_setpoint_target,
                "planned_max_grid_export_w",
                min(5500, max_setpoint_target),
            ),
            fallback_grid_target_w=0.0,
        )
        if planned_max_grid_export_w is None:
            return
        setpoint = adapt_grid_target_to_live_power(
            planned_battery_power_w=planned_battery_power_w,
            planned_optional_export_power_w=planned_optional_export_power_w,
            house_load_w=house_loads,
            pv_power_w=pv_power_for_control_w,
            max_grid_export_w=min(planned_max_grid_export_w, max_setpoint_target),
            neutral_grid_target_w=max_setpoint,
            ev_is_charging=ev_is_charging,
            hold_optional_export=optional_export_ev_plan_hold,
        )
        setpoint = select_finite_grid_target(
            candidate_grid_target_w=setpoint,
            fallback_grid_target_w=LIVE_TELEMETRY_FAILSAFE_IMPORT_TARGET_W,
        )
        if setpoint is None:
            return
        msg += (
            f" unified live target {setpoint:.0f} W from planned battery "
            f"{planned_battery_power_w} W, optional export "
            f"{planned_optional_export_power_w} W, live loads "
            f"{house_loads:.0f} W and PV {live_pv_power_w:.0f} W"
        )
    elif setpoint_target < (max_setpoint - 30):
        current_diff_from_avg = house_power_long_term_average - house_loads
        setpoint = round(max(-max_setpoint_target, min(max_setpoint, setpoint_target - current_diff_from_avg)))
        msg += f" \nupdating setpoint {setpoint_target:.0f} with house_avg {house_power_long_term_average} loads {house_loads} and diff {current_diff_from_avg:.0f} to {setpoint:.0f}"
    else:
        setpoint = min(max_setpoint, setpoint_target)
        msg += f" updated setpoint to {setpoint}"

    if ev_is_charging:
        msg = f"setpoint adjusted to {max_setpoint} since EV is charging"
        setpoint = max_setpoint

    # Silent mode quiets battery/grid exchange ahead of the safety
    # supervisor, so true cap emergencies can still push past it.  House
    # and EV coverage stay unlimited; only grid-bound flows are capped.
    if get(Automation.silent_mode, False):
        try:
            live_wallbox_power_w = max(0.0, get(Charger.power, 0))
        except (TypeError, ValueError, OverflowError):
            live_wallbox_power_w = 0.0
        silent_cap_w = (
            min(planned_max_grid_export_w, max_setpoint_target)
            if unified_scheduler_active
            else max_setpoint_target
        )
        silent_target_w = apply_silent_mode_grid_limits(
            setpoint,
            pv_power_for_control_w,
            house_loads,
            live_wallbox_power_w,
            silent_cap_w,
            silent_power_w=SILENT_MODE_MAX_BATTERY_POWER_W,
            surplus_margin_w=SILENT_MODE_CHARGE_SURPLUS_MARGIN_W,
        )
        if silent_target_w != setpoint:
            msg += (
                f" silent-clamped setpoint from {setpoint:.0f} to "
                f"{silent_target_w:.0f} (PV {pv_power_for_control_w:.0f} W, "
                f"loads {house_loads:.0f} W, EV {live_wallbox_power_w:.0f} W)"
            )
            setpoint = silent_target_w

    setpoint = select_finite_grid_target(
        candidate_grid_target_w=setpoint,
        fallback_grid_target_w=LIVE_TELEMETRY_FAILSAFE_IMPORT_TARGET_W,
    )
    if setpoint is None:
        return

    surplus_energy = get(House.energy_surplus, 0)
    battery_capacity = get(Battery.capacity, 0)
    battery_energy = get(Battery.energy, 0)
    battery_energy_updated_at = get_attr(Battery.energy, "last_updated")
    battery_energy_measurement_age_seconds = calculate_measurement_age_seconds(
        current_control_time,
        battery_energy_updated_at,
    )
    # Battery.power is in W; an idle battery's energy reading legitimately
    # sits still (HA only refreshes last_updated on change).
    battery_energy_measurement_fresh = is_battery_energy_measurement_usable(
        current_control_time,
        battery_energy_updated_at,
        battery_energy,
        battery_capacity,
        get(Battery.soc, -1),
        get(Battery.power, 1337),
        get_attr(Battery.power, "last_updated"),
        max_age_seconds=LIVE_BATTERY_ENERGY_MAX_MEASUREMENT_AGE_SECONDS,
    )
    battery_energy_values_valid = are_battery_energy_values_valid(
        battery_capacity,
        battery_energy,
    )
    reserve_soc = get_reserve_soc()
    minimal_soc = get(Automation.minimal_soc, reserve_soc)
    if battery_energy_values_valid:
        battery_export_floor = get_battery_export_floor(
            battery_capacity,
            reserve_soc,
            minimal_soc,
        )
        if unified_scheduler_active:
            planned_battery_floor_kwh = get_attr(
                Grid.power_setpoint_target,
                "planned_battery_floor_kwh",
                battery_export_floor,
            )
            if are_battery_energy_values_valid(
                battery_capacity,
                planned_battery_floor_kwh,
            ):
                battery_export_floor = planned_battery_floor_kwh
            else:
                battery_energy_values_valid = False
    else:
        battery_export_floor = 0.0
    pv_power = pv_power_for_control_w
    max_battery_charge_power_w = resolve_directional_power_limit_w(
        get(Battery.charge_limit, -1),
        6600.0,
        6600.0,
    )
    max_battery_discharge_power_w = resolve_directional_power_limit_w(
        get(Battery.discharge_limit, -1),
        8000.0,
        8000.0,
    )
    raw_pv_only_setpoint = get_pv_only_setpoint(
        pv_power,
        house_loads,
        max_battery_charge_power_w,
    )
    pv_only_setpoint = select_no_battery_export_target(
        pv_only_grid_target_w=raw_pv_only_setpoint,
        neutral_grid_target_w=max_setpoint,
        pv_measurement_fresh=pv_power_measurement_fresh,
    )

    live_headroom_mode = "disabled"
    live_headroom_decision = None
    time_to_full_minutes = None
    measurement_skew_seconds = None
    grid_measurement_age_seconds = None
    battery_measurement_age_seconds = None
    measurement_rejection_reason = (
        "invalid_battery_energy"
        if unified_scheduler_active and not battery_energy_values_valid
        else (
            "stale_battery_energy"
            if unified_scheduler_active and not battery_energy_measurement_fresh
            else (
                "battery_capacity_unavailable"
                if unified_scheduler_active
                else "not_applicable"
            )
        )
    )
    measurements_available = not unified_scheduler_active
    battery_free_capacity_kwh = (
        max(0.0, battery_capacity - battery_energy)
        if battery_energy_values_valid and battery_energy_measurement_fresh
        else 0.0
    )
    if (
        unified_scheduler_active
        and battery_energy_values_valid
        and battery_energy_measurement_fresh
    ):
        current_time = now()
        unavailable_power_kw = 1337.0
        measured_grid_power_kw = get(Grid.power_ac, unavailable_power_kw)
        measured_battery_power_kw = get(Battery.power, unavailable_power_kw)
        grid_power_updated_at = get_attr(Grid.power_ac, "last_updated")
        battery_power_updated_at = get_attr(Battery.power, "last_updated")
        grid_measurement_age_seconds = calculate_measurement_age_seconds(
            current_time,
            grid_power_updated_at,
        )
        battery_measurement_age_seconds = calculate_measurement_age_seconds(
            current_time,
            battery_power_updated_at,
        )
        measurement_skew_seconds = calculate_measurement_skew_seconds(
            grid_power_updated_at,
            battery_power_updated_at,
        )
        power_measurements_present = (
            measured_grid_power_kw != unavailable_power_kw
            and measured_battery_power_kw != unavailable_power_kw
        )
        power_measurement_values_valid = (
            power_measurements_present
            and are_power_measurement_values_valid(
                measured_grid_power_kw,
                measured_battery_power_kw,
                max_absolute_power_w=LIVE_POWER_MAX_ABSOLUTE_W / 1000.0,
            )
        )
        measurements_available = (
            power_measurements_present and power_measurement_values_valid
        )
        if not power_measurements_present:
            measurement_rejection_reason = "missing_power"
        elif not power_measurement_values_valid:
            measurement_rejection_reason = "invalid_power"
        else:
            measurements_available = are_power_measurements_coherent(
                current_time=current_time,
                grid_updated_at=grid_power_updated_at,
                battery_updated_at=battery_power_updated_at,
                max_age_seconds=LIVE_HEADROOM_MAX_MEASUREMENT_AGE_SECONDS,
                max_skew_seconds=LIVE_HEADROOM_MAX_MEASUREMENT_SKEW_SECONDS,
            )
            if measurements_available:
                measurement_rejection_reason = (
                    "none"
                    if house_load_measurement_fresh
                    else "stale_or_invalid_house_load"
                )
            elif (
                grid_measurement_age_seconds is None
                or battery_measurement_age_seconds is None
                or measurement_skew_seconds is None
            ):
                measurement_rejection_reason = "invalid_timestamp"
            elif (
                grid_measurement_age_seconds < -2.0
                or battery_measurement_age_seconds < -2.0
                or grid_measurement_age_seconds
                > LIVE_HEADROOM_MAX_MEASUREMENT_AGE_SECONDS
                or battery_measurement_age_seconds
                > LIVE_HEADROOM_MAX_MEASUREMENT_AGE_SECONDS
            ):
                measurement_rejection_reason = "stale_or_future"
            elif (
                measurement_skew_seconds
                > LIVE_HEADROOM_MAX_MEASUREMENT_SKEW_SECONDS
            ):
                measurement_rejection_reason = "timestamp_skew"
            else:
                measurement_rejection_reason = "incoherent"

        # Debounce the health signal before anything actuates on it.  A single
        # skewed sample is normal sensor jitter (see the gate comment above),
        # so the hold engages after two consecutive bad cycles and releases
        # after two consecutive good ones.  Everything downstream, including
        # the headroom mode and the tail hold, uses the debounced signal.
        # A missing persisted entity makes get() log an error line, so check
        # at most once per load; a reload resets the flag and re-checks.
        if not prev_telemetry_hold_active and not prev_telemetry_hold_boot_checked:
            prev_telemetry_hold_boot_checked = True
            persisted_hold_limit = get(
                "pyscript.telemetry_hold_discharge_limit", ""
            )
            if persisted_hold_limit not in ("", None, "unknown", "unavailable"):
                try:
                    prev_discharge_limit_before_hold = float(persisted_hold_limit)
                except (TypeError, ValueError):
                    prev_discharge_limit_before_hold = None
                    state.set(  # noqa: F821 - provided by the Pyscript runtime
                        "pyscript.telemetry_hold_discharge_limit",
                        "",
                    )
                else:
                    prev_telemetry_hold_active = True
                    log.warning(
                        "Resuming telemetry hold after restart; battery "
                        "discharge stays disabled until fresh measurements arrive"
                    )
        if measurements_available:
            prev_telemetry_good_streak += 1
            prev_telemetry_bad_streak = 0
        else:
            prev_telemetry_bad_streak += 1
            prev_telemetry_good_streak = 0
        if not prev_telemetry_hold_active and prev_telemetry_bad_streak >= 2:
            held_setpoint = (
                f"{prev_applied_grid_target_w} W"
                if prev_applied_grid_target_w is not None
                else "unknown"
            )
            prev_discharge_limit_before_hold = get(Battery.discharge_limit, -1)
            # Persist the pre-hold limit outside volatile globals: a reload
            # between entry and recovery must not strand a 0 limit with no
            # owner left to restore it.
            state.set(  # noqa: F821 - provided by the Pyscript runtime
                "pyscript.telemetry_hold_discharge_limit",
                str(prev_discharge_limit_before_hold),
            )
            if prev_discharge_limit_before_hold != 0:
                set_state(Battery.discharge_limit, 0)
            log.warning(
                f"Power telemetry {measurement_rejection_reason}: holding last "
                f"grid setpoint ({held_setpoint}), battery discharge disabled"
            )
            prev_telemetry_hold_active = True
        elif prev_telemetry_hold_active and prev_telemetry_good_streak >= 2:
            restored_limit = prev_discharge_limit_before_hold
            prev_telemetry_hold_active = False
            prev_discharge_limit_before_hold = None
            # Restore the input before clearing the latch (engage does the
            # reverse: latch first, then park at 0).  Either order leaves no
            # window where the planner sees a hold-owned 0 with no latch to
            # explain it, so get_planned_discharge_limit_w stays race-free.
            current_limit = get(Battery.discharge_limit, -1)
            if current_limit == 0 and restored_limit not in (None, 0):
                set_state(Battery.discharge_limit, restored_limit)
                log.warning(
                    "Power telemetry recovered, "
                    f"battery discharge limit restored to {restored_limit}"
                )
            elif current_limit == 0:
                log.warning(
                    "Power telemetry recovered, discharge limit already "
                    "disabled before hold, nothing to restore"
                )
            else:
                log.warning(
                    "Power telemetry recovered, discharge limit changed "
                    "externally during hold, leaving as-is"
                )
            state.set(  # noqa: F821 - provided by the Pyscript runtime
                "pyscript.telemetry_hold_discharge_limit",
                "",
            )
        measurements_available = not prev_telemetry_hold_active

        if not measurements_available:
            live_headroom_mode = "measurements_unavailable"
        else:
            measured_grid_power_w = float(measured_grid_power_kw) * 1000.0
            measured_battery_power_w = float(measured_battery_power_kw) * 1000.0
            time_to_full_minutes = calculate_time_to_full_minutes(
                battery_free_capacity_kwh=battery_free_capacity_kwh,
                battery_charge_power_w=measured_battery_power_w,
                charge_efficiency=HOME_BATTERY_CHARGE_EFFICIENCY,
            )
        preferred_pv_export_w = get(
            Grid.max_pv_feedin_target,
            planned_max_grid_export_w,
        )

        # First calculate the instantaneous request without an existing hold,
        # then update the latch and apply its retained target.  Separating the
        # two prevents the held value from masquerading as fresh PV risk.
        if measurements_available:
            live_headroom_probe = calculate_live_headroom_control(
                measured_grid_power_w=measured_grid_power_w,
                measured_battery_power_w=measured_battery_power_w,
                battery_free_capacity_kwh=battery_free_capacity_kwh,
                held_headroom_target_kwh=0.0,
                preferred_pv_export_w=preferred_pv_export_w,
                planned_grid_export_cap_w=planned_max_grid_export_w,
                neutral_grid_target_w=max_setpoint,
                max_battery_charge_w=max_battery_charge_power_w,
                max_battery_discharge_w=max_battery_discharge_power_w,
                risk_horizon_minutes=LIVE_HEADROOM_RISK_HORIZON_MINUTES,
                base_headroom_kwh=LIVE_HEADROOM_BASE_KWH,
                maximum_headroom_kwh=LIVE_HEADROOM_MAX_KWH,
                tracking_margin_w=LIVE_HEADROOM_TRACKING_MARGIN_W,
                discharge_efficiency=HOME_BATTERY_DISCHARGE_EFFICIENCY,
            )
            live_headroom_state = update_live_headroom_state(
                current_time=current_time,
                previous_active=live_headroom_active,
                previous_target_headroom_kwh=live_headroom_target_kwh,
                previous_hold_until=live_headroom_hold_until,
                requested_headroom_kwh=live_headroom_probe.requested_headroom_kwh,
                battery_free_capacity_kwh=battery_free_capacity_kwh,
                natural_export_w=live_headroom_probe.natural_export_w,
                preferred_pv_export_w=preferred_pv_export_w,
                hold_minutes=LIVE_HEADROOM_HOLD_MINUTES,
                activation_buffer_kwh=LIVE_HEADROOM_ACTIVATION_BUFFER_KWH,
                release_hysteresis_w=LIVE_HEADROOM_RELEASE_HYSTERESIS_W,
                time_to_full_minutes=time_to_full_minutes,
                activation_lead_time_minutes=LIVE_HEADROOM_ACTIVATION_LEAD_TIME_MINUTES,
            )
            live_headroom_active = live_headroom_state.active
            live_headroom_target_kwh = live_headroom_state.target_headroom_kwh
            live_headroom_hold_until = live_headroom_state.hold_until

            live_headroom_decision = calculate_live_headroom_control(
                measured_grid_power_w=measured_grid_power_w,
                measured_battery_power_w=measured_battery_power_w,
                battery_free_capacity_kwh=battery_free_capacity_kwh,
                held_headroom_target_kwh=live_headroom_target_kwh,
                preferred_pv_export_w=preferred_pv_export_w,
                planned_grid_export_cap_w=planned_max_grid_export_w,
                neutral_grid_target_w=max_setpoint,
                max_battery_charge_w=max_battery_charge_power_w,
                max_battery_discharge_w=max_battery_discharge_power_w,
                risk_horizon_minutes=LIVE_HEADROOM_RISK_HORIZON_MINUTES,
                base_headroom_kwh=LIVE_HEADROOM_BASE_KWH,
                maximum_headroom_kwh=LIVE_HEADROOM_MAX_KWH,
                tracking_margin_w=LIVE_HEADROOM_TRACKING_MARGIN_W,
                discharge_efficiency=HOME_BATTERY_DISCHARGE_EFFICIENCY,
            )

            if live_headroom_active:
                if live_headroom_decision.headroom_deficit_kwh <= 0.01:
                    live_headroom_mode = "hold_headroom"
                elif live_headroom_decision.can_create_headroom:
                    live_headroom_mode = "create_headroom"
                else:
                    live_headroom_mode = "hold_only_at_cap"
                current_applied_setpoint_w = get(
                    Grid.power_setpoint,
                    setpoint,
                )
                limited_safety_target_w = limit_target_step(
                    live_headroom_decision.desired_grid_target_w,
                    current_applied_setpoint_w,
                    max_step_w=LIVE_HEADROOM_MAX_TARGET_STEP_W,
                )
                setpoint = min(
                    setpoint,
                    limited_safety_target_w,
                )
                msg += (
                    f" live safety {live_headroom_mode}: target "
                    f"{live_headroom_target_kwh:.3f} kWh, free "
                    f"{battery_free_capacity_kwh:.3f} kWh, natural export "
                    f"{live_headroom_decision.natural_export_w:.0f} W, grid target "
                    f"{live_headroom_decision.desired_grid_target_w:.0f} W"
                )
            else:
                live_headroom_mode = "normal"
    else:
        live_headroom_active = False
        live_headroom_target_kwh = 0.0
        live_headroom_hold_until = None

    setpoint = select_finite_grid_target(
        candidate_grid_target_w=setpoint,
        fallback_grid_target_w=LIVE_TELEMETRY_FAILSAFE_IMPORT_TARGET_W,
    )
    if setpoint is None:
        return

    export_budget_energy_kwh = surplus_energy
    live_export_budget = None
    export_budget_ledger = None
    if unified_scheduler_active:
        published_safe_export_energy_kwh = get_attr(
            Grid.power_setpoint_target,
            "safe_export_energy_kwh",
            0.0,
        )
        safe_export_source_battery_kwh = get_attr(
            Grid.power_setpoint_target,
            "safe_export_source_battery_kwh",
            None,
        )
        safe_export_calculated_at = parse_aware_datetime(
            get_attr(
                Grid.power_setpoint_target,
                "safe_export_calculated_at",
                None,
            )
        )
        live_export_budget = get_fresh_export_policy_budget(
            base_spendable_energy_kwh=published_safe_export_energy_kwh,
            ev_adjusted_spendable_energy_kwh=published_safe_export_energy_kwh,
            source_battery_energy_kwh=safe_export_source_battery_kwh,
            current_battery_energy_kwh=battery_energy,
            calculated_at=safe_export_calculated_at,
            current_time=current_control_time,
            max_age_seconds=LIVE_EXPORT_BUDGET_HORIZON_SECONDS,
            discharge_efficiency=HOME_BATTERY_DISCHARGE_EFFICIENCY,
        )
        export_budget_energy_kwh = (
            live_export_budget.ev_adjusted_spendable_energy_kwh
        )
        export_policy_snapshot_id = (
            export_policy_calculated_at.isoformat()
            if export_policy_calculated_at is not None
            else ""
        )
        export_budget_ledger = update_monotonic_export_budget_ledger(
            snapshot_id=export_policy_snapshot_id,
            candidate_remaining_kwh=export_budget_energy_kwh,
            previous_state=get("pyscript.export_policy_budget_ledger", ""),
            current_time=current_control_time,
            battery_discharge_power_w=(
                max(0.0, -measured_battery_power_w)
                if measurements_available
                else 0.0
            ),
            measurements_available=measurements_available,
        )
        if export_budget_ledger.state_value:
            state.set(  # noqa: F821 - provided by the Pyscript runtime
                "pyscript.export_policy_budget_ledger",
                export_budget_ledger.state_value,
            )
        if (
            export_budget_ledger.valid
            and export_policy_fresh
            and live_export_budget.valid
        ):
            export_budget_energy_kwh = min(
                export_budget_energy_kwh,
                export_budget_ledger.remaining_kwh,
            )
        else:
            export_budget_energy_kwh = 0.0
        # Stale house-loads or PV readings must not veto the export budget.
        # Loads are an unsynchronized residual that swings negative on fast
        # grid ramps; PV sleeps for minutes at night on a flat zero. Vetoing
        # on either turns benign sensor naps into a self-sustaining limit
        # cycle (full export -> stale/invalid sample -> slam to PV-only ->
        # repeat). Both inputs already fail safe in the value path above
        # (loads ride on the last good value, PV falls back to zero), so the
        # veto adds no safety, only oscillation. Export safety rests on
        # grid/battery coherence plus the live battery floor below, both
        # still enforced.
        if (
            optional_export_ev_plan_hold
            or not measurements_available
            or not battery_energy_values_valid
            or not battery_energy_measurement_fresh
            or not export_policy_fresh
            or not live_export_budget.valid
            or not export_budget_ledger.valid
        ):
            export_budget_energy_kwh = 0.0
        budget_limited_setpoint = limit_live_export_target_to_budget(
            desired_grid_target_w=setpoint,
            pv_only_grid_target_w=pv_only_setpoint,
            safe_export_energy_kwh=export_budget_energy_kwh,
            budget_horizon_seconds=LIVE_EXPORT_BUDGET_HORIZON_SECONDS,
            max_battery_discharge_w=max_battery_discharge_power_w,
        )
        if budget_limited_setpoint > setpoint:
            msg += (
                f" budget-capped setpoint from {setpoint:.0f} to "
                f"{budget_limited_setpoint:.0f}; safe export budget "
                f"{export_budget_energy_kwh:.3f} kWh, measurements "
                f"coherent={measurements_available}"
            )
            setpoint = budget_limited_setpoint

    if (
        export_budget_energy_kwh < LIVE_EXPORT_BUDGET_RELEASE_KWH
        or not battery_energy_values_valid
        or not battery_energy_measurement_fresh
        or battery_energy <= battery_export_floor
    ):
        if setpoint < pv_only_setpoint:
            msg += (
                f" capped setpoint from {setpoint:.0f} to "
                f"{pv_only_setpoint:.0f}; "
                f"export budget {export_budget_energy_kwh:.2f} kWh, "
                f"battery {battery_energy:.2f} kWh, "
                f"protected floor {battery_export_floor:.2f} kWh"
            )
            setpoint = pv_only_setpoint

    publish_live_headroom = live_headroom_mode != prev_live_headroom_mode
    if last_live_headroom_publish is None:
        publish_live_headroom = True
    elif (now() - last_live_headroom_publish).total_seconds() >= LIVE_HEADROOM_PUBLISH_INTERVAL_SECONDS:
        publish_live_headroom = True
    if publish_live_headroom:
        natural_export_w = 0.0
        requested_headroom_kwh = 0.0
        headroom_deficit_kwh = 0.0
        available_export_margin_w = 0.0
        safety_grid_target_w = None
        can_create_headroom = False
        if live_headroom_decision is not None:
            natural_export_w = live_headroom_decision.natural_export_w
            requested_headroom_kwh = live_headroom_decision.requested_headroom_kwh
            headroom_deficit_kwh = live_headroom_decision.headroom_deficit_kwh
            available_export_margin_w = live_headroom_decision.available_export_margin_w
            safety_grid_target_w = live_headroom_decision.desired_grid_target_w
            can_create_headroom = live_headroom_decision.can_create_headroom
        set_state(
            "sensor.grid_export_safety",
            live_headroom_mode,
            friendly_name="Grid Export Safety",
            active=live_headroom_active,
            target_headroom_kwh=round(live_headroom_target_kwh, 3),
            requested_headroom_kwh=round(requested_headroom_kwh, 3),
            battery_free_capacity_kwh=round(battery_free_capacity_kwh, 3),
            headroom_deficit_kwh=round(headroom_deficit_kwh, 3),
            natural_export_w=round(natural_export_w),
            available_export_margin_w=round(available_export_margin_w),
            time_to_full_minutes=(
                round(time_to_full_minutes, 1)
                if time_to_full_minutes is not None
                else None
            ),
            measurement_skew_seconds=(
                round(measurement_skew_seconds, 2)
                if measurement_skew_seconds is not None
                else None
            ),
            grid_measurement_age_seconds=(
                round(grid_measurement_age_seconds, 2)
                if grid_measurement_age_seconds is not None
                else None
            ),
            battery_measurement_age_seconds=(
                round(battery_measurement_age_seconds, 2)
                if battery_measurement_age_seconds is not None
                else None
            ),
            battery_energy_measurement_age_seconds=(
                round(battery_energy_measurement_age_seconds, 2)
                if battery_energy_measurement_age_seconds is not None
                else None
            ),
            battery_energy_measurement_fresh=battery_energy_measurement_fresh,
            house_load_measurement_age_seconds=(
                round(house_load_measurement_age_seconds, 2)
                if house_load_measurement_age_seconds is not None
                else None
            ),
            house_load_measurement_fresh=house_load_measurement_fresh,
            pv_measurement_age_seconds=(
                round(pv_measurement_age_seconds, 2)
                if pv_measurement_age_seconds is not None
                else None
            ),
            pv_measurement_fresh=pv_power_measurement_fresh,
            max_battery_charge_power_w=round(max_battery_charge_power_w),
            max_battery_discharge_power_w=round(max_battery_discharge_power_w),
            remaining_export_budget_kwh=round(export_budget_energy_kwh, 3),
            live_export_budget_valid=(
                live_export_budget.valid
                if live_export_budget is not None
                else False
            ),
            live_export_budget_reason=(
                live_export_budget.reason
                if live_export_budget is not None
                else "not_applicable"
            ),
            live_export_budget_age_seconds=(
                round(live_export_budget.source_age_seconds, 2)
                if live_export_budget is not None
                and live_export_budget.source_age_seconds is not None
                else None
            ),
            live_export_budget_battery_draw_kwh=(
                round(live_export_budget.battery_draw_since_calculation_kwh, 3)
                if live_export_budget is not None
                else 0.0
            ),
            export_budget_ledger_valid=(
                export_budget_ledger.valid
                if export_budget_ledger is not None
                else False
            ),
            export_budget_ledger_reason=(
                export_budget_ledger.reason
                if export_budget_ledger is not None
                else "not_applicable"
            ),
            export_budget_ledger_remaining_kwh=(
                round(export_budget_ledger.remaining_kwh, 3)
                if export_budget_ledger is not None
                else 0.0
            ),
            measurement_rejection_reason=measurement_rejection_reason,
            safety_grid_target_w=(
                round(safety_grid_target_w)
                if safety_grid_target_w is not None
                else None
            ),
            applied_grid_target_w=round(setpoint),
            can_create_headroom=can_create_headroom,
            hold_until=(
                live_headroom_hold_until.isoformat()
                if live_headroom_hold_until is not None
                else None
            ),
        )
        if live_headroom_mode != prev_live_headroom_mode:
            log.warning(
                f"Grid export safety changed from {prev_live_headroom_mode} "
                f"to {live_headroom_mode}: active={live_headroom_active}, "
                f"target={live_headroom_target_kwh:.3f} kWh, "
                f"free={battery_free_capacity_kwh:.3f} kWh, "
                f"natural export={natural_export_w:.0f} W, "
                f"applied target={setpoint:.0f} W"
            )
        prev_live_headroom_mode = live_headroom_mode
        last_live_headroom_publish = now()

    # Debounced stale-telemetry hold (see above): leave the last applied
    # setpoint untouched instead of pinning a blind fallback.  Discharge
    # handling already ran with the debounce transition.
    if not measurements_available:
        return

    if logging:
        log.warning(msg)

    setpoint = select_finite_grid_target(
        candidate_grid_target_w=setpoint,
        fallback_grid_target_w=LIVE_TELEMETRY_FAILSAFE_IMPORT_TARGET_W,
    )
    if setpoint is None:
        return
    applied_setpoint_w = round(setpoint)
    if applied_setpoint_w != prev_applied_grid_target_w:
        previous_setpoint = (
            f"{prev_applied_grid_target_w} W"
            if prev_applied_grid_target_w is not None
            else "unknown"
        )
        log.warning(
            f"Applied grid setpoint {applied_setpoint_w} W (was {previous_setpoint}); "
            f"rejection={measurement_rejection_reason}"
            + (f"; {msg}" if msg else "")
        )
        prev_applied_grid_target_w = applied_setpoint_w
    set_state(Grid.power_setpoint, applied_setpoint_w)
