"""Pure helpers for the unified battery export scheduler.

The scheduler plans optional battery export power, never an absolute future
grid trajectory.  Forecast and live control apply that same relative request
on top of their current neutral behavior, so changed battery state, PV, house
load, and EV charging cannot leave stale grid targets behind.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ExportSchedulerSlot:
    """One forecast interval used by the export scheduler."""

    period_start: datetime
    duration_hours: float
    price_per_kwh: float
    baseline_setpoint_w: float
    baseline_battery_power_w: float
    baseline_grid_export_w: float
    ev_charge_power_w: float = 0.0
    intentional_grid_import_w: float = 0.0


@dataclass(frozen=True)
class ReserveTrajectorySlot:
    """One interval used to derive the protected battery-energy trajectory."""

    period_start: datetime
    duration_hours: float
    house_power_w: float
    pv_power_w: float
    reserve_soc: float


@dataclass(frozen=True)
class ExportSchedulePlan:
    """Serializable result of one scheduler run."""

    grid_target_schedule: dict[str, float]
    battery_power_delta_schedule: dict[str, float]
    allocated_energy_kwh: float
    headroom_energy_kwh: float
    economic_energy_kwh: float
    unallocated_headroom_kwh: float


def build_reserve_energy_schedule(
    slots: list[ReserveTrajectorySlot],
    battery_capacity_kwh: float,
    uncertainty_margin_kwh: float = 1.0,
) -> dict[str, float]:
    """Return the protected energy for every future interval.

    Each deficit interval reserves enough energy to reach the next interval
    where forecast PV meets house demand.  Once PV covers demand, only the
    installation reserve and uncertainty margin remain.  Walking backwards
    makes the result deterministic and linear in the forecast length.
    """

    battery_capacity_kwh = max(0.0, battery_capacity_kwh)
    uncertainty_margin_kwh = max(0.0, uncertainty_margin_kwh)
    deficit_until_solar_kwh = 0.0
    schedule = {}

    for slot in reversed(slots):
        duration_hours = max(0.0, slot.duration_hours)
        if slot.pv_power_w >= slot.house_power_w:
            deficit_until_solar_kwh = 0.0
        else:
            deficit_until_solar_kwh += (
                max(0.0, slot.house_power_w - slot.pv_power_w)
                * duration_hours
                / 1000
            )

        reserve_energy_kwh = (
            max(0.0, slot.reserve_soc) / 100 * battery_capacity_kwh
        )
        schedule[slot.period_start.isoformat()] = min(
            battery_capacity_kwh,
            reserve_energy_kwh
            + deficit_until_solar_kwh
            + uncertainty_margin_kwh,
        )

    return schedule


def get_optional_export_grid_target(
    optional_export_power_w: float,
    max_grid_export_w: float,
    neutral_grid_target_w: float,
    ev_is_charging: bool = False,
    hold_optional_export: bool = False,
) -> float:
    """Apply one optional export request to the shared neutral grid target."""

    if ev_is_charging or hold_optional_export:
        return neutral_grid_target_w

    optional_export_power_w = max(0.0, optional_export_power_w)
    max_grid_export_w = max(0.0, max_grid_export_w)
    requested_target_w = neutral_grid_target_w - optional_export_power_w
    return max(
        -max_grid_export_w,
        min(neutral_grid_target_w, requested_target_w),
    )


def is_ev_available_for_charging(
    charger_ready: bool,
    charger_control_enabled: bool,
    ev_is_charging: bool,
) -> bool:
    """Return whether the home wallbox can presently serve the EV.

    ``charger_ready`` is false while a charging transaction is active, so it
    must never be used on its own as the forecast-availability signal.
    """

    return bool(charger_ready or charger_control_enabled or ev_is_charging)


def is_ev_plan_fresh(
    planned_ev_available: bool | None,
    planned_ev_is_charging: bool | None,
    planned_ev_energy_needed_kwh: float | None,
    plan_calculated_at: str | datetime | None,
    live_ev_available: bool,
    live_ev_is_charging: bool,
    live_ev_energy_needed_kwh: float,
    current_time: datetime,
    max_age_seconds: float = 180.0,
    energy_tolerance_kwh: float = 0.1,
) -> bool:
    """Return whether a published export plan matches the live EV state.

    Optional export is safe only when the scheduler saw the same EV inputs as
    the live controller.  This closes the short transition window after a
    charging-state change without blocking export merely because a connected
    EV still has planned energy demand.
    """

    if planned_ev_available is None or planned_ev_is_charging is None:
        return False
    if bool(planned_ev_available) != bool(live_ev_available):
        return False
    if bool(planned_ev_is_charging) != bool(live_ev_is_charging):
        return False

    try:
        planned_energy_needed_kwh = float(planned_ev_energy_needed_kwh)
        live_energy_needed_kwh = float(live_ev_energy_needed_kwh)
    except (TypeError, ValueError):
        return False
    if abs(planned_energy_needed_kwh - live_energy_needed_kwh) > max(
        0.0,
        energy_tolerance_kwh,
    ):
        return False

    if isinstance(plan_calculated_at, str):
        try:
            plan_calculated_at = datetime.fromisoformat(plan_calculated_at)
        except ValueError:
            return False
    if not isinstance(plan_calculated_at, datetime):
        return False
    if plan_calculated_at.tzinfo is None or current_time.tzinfo is None:
        return False

    plan_age_seconds = (current_time - plan_calculated_at).total_seconds()
    return -5.0 <= plan_age_seconds <= max(0.0, max_age_seconds)


def _is_quiet_hour(period_start: datetime, quiet_start_hour: int, quiet_end_hour: int) -> bool:
    hour = period_start.hour
    if quiet_start_hour == quiet_end_hour:
        return False
    if quiet_start_hour < quiet_end_hour:
        return quiet_start_hour <= hour < quiet_end_hour
    return hour >= quiet_start_hour or hour < quiet_end_hour


def _allocate_bands(
    ranked_bands,
    remaining_kwh: float,
    allocated_w_by_key: dict[str, float],
) -> float:
    """Allocate energy over ranked power bands and return the remainder."""

    for _negative_score, _period_start, key, duration_hours, band_start_w, band_end_w in ranked_bands:
        if remaining_kwh <= 1e-9:
            break

        already_allocated_w = allocated_w_by_key.get(key, 0.0)
        usable_start_w = max(band_start_w, already_allocated_w)
        available_power_w = max(0.0, band_end_w - usable_start_w)
        available_energy_kwh = available_power_w * duration_hours / 1000
        allocated_energy_kwh = min(remaining_kwh, available_energy_kwh)
        if allocated_energy_kwh <= 0:
            continue

        allocated_w_by_key[key] = usable_start_w + allocated_energy_kwh * 1000 / duration_hours
        remaining_kwh -= allocated_energy_kwh

    return max(0.0, remaining_kwh)


def fit_export_schedule_to_energy_budget(
    schedule: dict[str, float],
    slots: list[ExportSchedulerSlot],
    energy_budget_kwh: float,
    locked_keys: set[str] | None = None,
) -> dict[str, float]:
    """Return a non-negative schedule that fits the available energy.

    A slew-limited current request can be larger than the freshly optimized
    request while the controller ramps down.  Keep that current request when
    possible and proportionally reduce future allocations so the replay does
    not spend the same energy twice.  If the locked request alone exceeds the
    budget, safety wins and the locked request is reduced as well.
    """

    result = {key: max(0.0, float(value)) for key, value in schedule.items()}
    duration_by_key = {
        slot.period_start.isoformat(): max(0.0, slot.duration_hours)
        for slot in slots
    }
    locked_keys = set(locked_keys or ())
    energy_budget_kwh = max(0.0, energy_budget_kwh)

    scheduled_keys = set(duration_by_key)
    adjustable_keys = scheduled_keys - locked_keys
    locked_energy_kwh = 0.0
    adjustable_energy_kwh = 0.0
    for key in scheduled_keys:
        scheduled_energy_kwh = (
            result.get(key, 0.0) * duration_by_key.get(key, 0.0) / 1000
        )
        if key in locked_keys:
            locked_energy_kwh += scheduled_energy_kwh
        else:
            adjustable_energy_kwh += scheduled_energy_kwh
    total_energy_kwh = locked_energy_kwh + adjustable_energy_kwh
    if total_energy_kwh <= energy_budget_kwh + 1e-9:
        return result

    if locked_energy_kwh >= energy_budget_kwh - 1e-9:
        locked_scale = (
            energy_budget_kwh / locked_energy_kwh
            if locked_energy_kwh > 0
            else 0.0
        )
        for key in scheduled_keys:
            result[key] = result.get(key, 0.0) * locked_scale if key in locked_keys else 0.0
        return result

    remaining_budget_kwh = energy_budget_kwh - locked_energy_kwh
    adjustable_scale = (
        min(1.0, remaining_budget_kwh / adjustable_energy_kwh)
        if adjustable_energy_kwh > 0
        else 0.0
    )
    for key in adjustable_keys:
        result[key] = result.get(key, 0.0) * adjustable_scale
    return result


def build_unified_export_schedule(
    slots: list[ExportSchedulerSlot],
    available_export_energy_kwh: float,
    required_headroom_energy_kwh: float,
    headroom_deadline: datetime | None,
    min_discharge_price: float,
    max_grid_export_w: float,
    max_battery_discharge_w: float,
    efficient_discharge_w: float,
    quiet_boost_penalty_fraction: float,
    quiet_start_hour: int = 17,
    quiet_end_hour: int = 24,
    headroom_reference_price: float = 0.0,
    headroom_min_price_spread: float = 0.0,
) -> ExportSchedulePlan:
    """Build one price-ranked schedule used by forecast and live control.

    During quiet hours the configured tolerance is split symmetrically around
    the actual price: the efficient band receives a preference and the louder
    boost band a penalty.  The first hour after the quiet window keeps the
    efficient/boost split without the quiet-hour bonus.  This one-hour grace
    avoids a hard power discontinuity at midnight while a materially better
    later price can still win.  Preferred PV headroom remains a soft economic
    objective based on the actual/penalized export value.

    EV-charging and intentional grid-import intervals stay at their neutral
    baseline target.  Combined forecast export is capped at
    ``max_grid_export_w``; unavoidable PV spill remains a physical exception.
    """

    available_kwh = max(0.0, available_export_energy_kwh)
    requested_headroom_kwh = max(0.0, required_headroom_energy_kwh)
    required_headroom_kwh = min(requested_headroom_kwh, available_kwh)
    max_grid_export_w = max(0.0, max_grid_export_w)
    max_battery_discharge_w = max(0.0, max_battery_discharge_w)
    efficient_discharge_w = max(0.0, min(max_battery_discharge_w, efficient_discharge_w))
    quiet_boost_penalty_fraction = max(0.0, min(1.0, quiet_boost_penalty_fraction))
    quiet_score_scale = max(
        1e-9,
        (1.0 - quiet_boost_penalty_fraction) ** 0.5,
    )
    headroom_reference_price = max(0.0, headroom_reference_price)
    headroom_min_price_spread = max(0.0, headroom_min_price_spread)
    profitable_headroom_price = headroom_reference_price + headroom_min_price_spread
    soft_headroom_enabled = headroom_reference_price > 0 or headroom_min_price_spread > 0

    grid_target_schedule = {}
    battery_power_delta_schedule = {}
    headroom_bands = []
    economic_bands = []

    for slot in slots:
        key = slot.period_start.isoformat()
        grid_target_schedule[key] = max(-max_grid_export_w, min(0.0, slot.baseline_setpoint_w))
        battery_power_delta_schedule[key] = 0.0

        if (
            slot.duration_hours <= 0
            or slot.ev_charge_power_w > 1
            or slot.intentional_grid_import_w > 1
        ):
            continue

        grid_headroom_w = max(0.0, max_grid_export_w - max(0.0, slot.baseline_grid_export_w))
        battery_headroom_w = max(0.0, max_battery_discharge_w + slot.baseline_battery_power_w)
        max_delta_w = min(grid_headroom_w, battery_headroom_w)
        if max_delta_w <= 0:
            continue

        quiet = _is_quiet_hour(slot.period_start, quiet_start_hour, quiet_end_hour)
        efficiency_grace = (
            not quiet
            and quiet_start_hour != quiet_end_hour
            and slot.period_start.hour == quiet_end_hour % 24
        )
        efficient_delta_end_w = min(
            max_delta_w,
            max(0.0, efficient_discharge_w + slot.baseline_battery_power_w),
        )

        bands = []
        if not quiet and not efficiency_grace:
            bands.append((slot.price_per_kwh, slot.price_per_kwh, 0.0, max_delta_w))
        else:
            if efficient_delta_end_w > 0:
                bands.append(
                    (
                        (
                            slot.price_per_kwh / quiet_score_scale
                            if quiet
                            else slot.price_per_kwh
                        ),
                        slot.price_per_kwh,
                        0.0,
                        efficient_delta_end_w,
                    )
                )
            if max_delta_w > efficient_delta_end_w:
                bands.append(
                    (
                        slot.price_per_kwh * quiet_score_scale,
                        slot.price_per_kwh * (1.0 - quiet_boost_penalty_fraction),
                        efficient_delta_end_w,
                        max_delta_w,
                    )
                )

        for score, headroom_value, band_start_w, band_end_w in bands:
            band = (
                -score,
                slot.period_start,
                key,
                slot.duration_hours,
                band_start_w,
                band_end_w,
            )
            if (
                headroom_deadline is not None
                and slot.period_start < headroom_deadline
                and (
                    not soft_headroom_enabled
                    or headroom_value >= profitable_headroom_price
                )
            ):
                headroom_bands.append(band)
            if slot.price_per_kwh >= min_discharge_price:
                economic_bands.append(band)

    headroom_bands.sort()
    economic_bands.sort()
    allocated_w_by_key = {}

    remaining_headroom_kwh = _allocate_bands(
        headroom_bands,
        required_headroom_kwh,
        allocated_w_by_key,
    )
    allocated_headroom_kwh = required_headroom_kwh - remaining_headroom_kwh
    remaining_budget_kwh = max(0.0, available_kwh - allocated_headroom_kwh)
    remaining_economic_kwh = _allocate_bands(
        economic_bands,
        remaining_budget_kwh,
        allocated_w_by_key,
    )
    allocated_economic_kwh = remaining_budget_kwh - remaining_economic_kwh

    for slot in slots:
        key = slot.period_start.isoformat()
        delta_w = allocated_w_by_key.get(key, 0.0)
        battery_power_delta_schedule[key] = delta_w
        grid_target_schedule[key] = max(
            -max_grid_export_w,
            min(0.0, slot.baseline_setpoint_w - delta_w),
        )

    return ExportSchedulePlan(
        grid_target_schedule=grid_target_schedule,
        battery_power_delta_schedule=battery_power_delta_schedule,
        allocated_energy_kwh=allocated_headroom_kwh + allocated_economic_kwh,
        headroom_energy_kwh=allocated_headroom_kwh,
        economic_energy_kwh=allocated_economic_kwh,
        unallocated_headroom_kwh=max(
            0.0,
            requested_headroom_kwh - allocated_headroom_kwh,
        ),
    )
