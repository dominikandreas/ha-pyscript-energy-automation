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
    hard_headroom_required_kwh: float
    hard_headroom_shortfall_kwh: float


def build_reserve_energy_schedule(
    slots: list[ReserveTrajectorySlot],
    battery_capacity_kwh: float,
    uncertainty_margin_kwh: float = 1.0,
    house_load_margin_fraction: float = 0.0,
    pv_confidence_factor: float = 1.0,
) -> dict[str, float]:
    """Return the protected energy for every future interval.

    Each deficit interval reserves enough energy to reach the next interval
    where conservative PV meets conservative house demand.  Inflating house
    load and discounting PV makes the reserve shrink naturally as uncertain
    overnight intervals become observed history.  Once conservative PV covers
    demand, only the installation reserve and base uncertainty margin remain.
    Walking backwards makes the result deterministic and linear in the
    forecast length.
    """

    battery_capacity_kwh = max(0.0, battery_capacity_kwh)
    uncertainty_margin_kwh = max(0.0, uncertainty_margin_kwh)
    house_load_margin_fraction = max(0.0, house_load_margin_fraction)
    pv_confidence_factor = max(0.0, min(1.0, pv_confidence_factor))
    deficit_until_solar_kwh = 0.0
    schedule = {}

    for slot in reversed(slots):
        duration_hours = max(0.0, slot.duration_hours)
        protected_house_power_w = max(0.0, slot.house_power_w) * (
            1.0 + house_load_margin_fraction
        )
        protected_pv_power_w = max(0.0, slot.pv_power_w) * pv_confidence_factor
        if protected_pv_power_w >= protected_house_power_w:
            deficit_until_solar_kwh = 0.0
        else:
            deficit_until_solar_kwh += (
                (protected_house_power_w - protected_pv_power_w)
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

    while remaining_kwh > 1e-9:
        allocated_this_pass = False
        for _negative_score, _period_start, key, duration_hours, band_start_w, band_end_w in ranked_bands:
            if remaining_kwh <= 1e-9:
                break

            already_allocated_w = allocated_w_by_key.get(key, 0.0)
            # Power bands are cumulative. A boost band cannot be allocated
            # before its lower efficient band merely because a negative price
            # reverses their score order; doing so would publish unbudgeted
            # power. A later pass can use it after the lower band is filled.
            if already_allocated_w + 1e-9 < band_start_w:
                continue
            usable_start_w = max(band_start_w, already_allocated_w)
            available_power_w = max(0.0, band_end_w - usable_start_w)
            available_energy_kwh = available_power_w * duration_hours / 1000
            allocated_energy_kwh = min(remaining_kwh, available_energy_kwh)
            if allocated_energy_kwh <= 0:
                continue

            allocated_w_by_key[key] = usable_start_w + allocated_energy_kwh * 1000 / duration_hours
            remaining_kwh -= allocated_energy_kwh
            allocated_this_pass = True

        if not allocated_this_pass:
            break

    return max(0.0, remaining_kwh)


def _allocated_energy_before_deadline(
    allocated_w_by_key: dict[str, float],
    duration_by_key: dict[str, float],
    period_start_by_key: dict[str, datetime],
    deadline: datetime,
) -> float:
    """Return scheduled export energy in slots beginning before ``deadline``."""

    total_kwh = 0.0
    for key, allocated_w in allocated_w_by_key.items():
        period_start = period_start_by_key.get(key)
        if period_start is None or period_start >= deadline:
            continue
        total_kwh += max(0.0, allocated_w) * duration_by_key.get(key, 0.0) / 1000
    return total_kwh


def _allocated_energy_total(
    allocated_w_by_key: dict[str, float],
    duration_by_key: dict[str, float],
) -> float:
    total_kwh = 0.0
    for key, allocated_w in allocated_w_by_key.items():
        total_kwh += max(0.0, allocated_w) * duration_by_key.get(key, 0.0) / 1000
    return total_kwh


def _allocate_cumulative_requirements(
    requirements: list[tuple[datetime, float]],
    ranked_bands,
    allocated_w_by_key: dict[str, float],
    duration_by_key: dict[str, float],
    period_start_by_key: dict[str, datetime],
    energy_budget_kwh: float,
) -> float:
    """Allocate price-ranked energy while honoring every cumulative deadline.

    Returns the largest unmet cumulative requirement at any deadline. An
    allocation made after an early solar peak must not count as headroom that
    was available before that peak.
    """

    max_shortfall_kwh = 0.0
    cumulative_target_kwh = 0.0
    for deadline, required_kwh in sorted(requirements):
        cumulative_target_kwh = max(cumulative_target_kwh, max(0.0, required_kwh))
        allocated_before_kwh = _allocated_energy_before_deadline(
            allocated_w_by_key,
            duration_by_key,
            period_start_by_key,
            deadline,
        )
        missing_kwh = max(0.0, cumulative_target_kwh - allocated_before_kwh)
        remaining_budget_kwh = max(
            0.0,
            energy_budget_kwh - _allocated_energy_total(allocated_w_by_key, duration_by_key),
        )
        requested_kwh = min(missing_kwh, remaining_budget_kwh)
        eligible_bands = []
        for band in ranked_bands:
            if band[1] < deadline:
                eligible_bands.append(band)
        _allocate_bands(eligible_bands, requested_kwh, allocated_w_by_key)
        allocated_before_kwh = _allocated_energy_before_deadline(
            allocated_w_by_key,
            duration_by_key,
            period_start_by_key,
            deadline,
        )
        max_shortfall_kwh = max(
            max_shortfall_kwh,
            cumulative_target_kwh - allocated_before_kwh,
        )

    return max(0.0, max_shortfall_kwh)


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


def build_hard_cap_storage_hold_schedule(
    slots: list[ExportSchedulerSlot],
    max_grid_export_w: float,
    neutral_grid_target_w: float,
) -> dict[str, float]:
    """Keep previously-created battery headroom through a critical PV peak.

    Hard headroom is normally created just before the first forecast cap
    violation. Without an explicit hold, the following sub-cap PV intervals
    simply recharge the battery and consume that headroom before the largest
    peak arrives. Between the first and last violation of each day, request
    the live natural grid flow (clamped at the cap). This holds battery power
    at zero while flow is below the cap and charges only the excess above it.

    The returned values are optional-export control power, not battery-export
    energy. Routing surplus PV directly to the grid therefore must not consume
    the protected battery energy budget.
    """

    max_grid_export_w = max(0.0, max_grid_export_w)
    critical_ranges_by_date = {}
    for index, slot in enumerate(slots):
        if slot.baseline_grid_export_w <= max_grid_export_w:
            continue
        period_date = slot.period_start.date()
        critical_range = critical_ranges_by_date.get(period_date)
        if critical_range is None:
            critical_ranges_by_date[period_date] = [index, index]
        else:
            critical_range[1] = index

    schedule = {}
    for first_index, last_index in critical_ranges_by_date.values():
        for slot in slots[first_index : last_index + 1]:
            if (
                slot.duration_hours <= 0
                or slot.ev_charge_power_w > 1
                or slot.intentional_grid_import_w > 1
            ):
                continue
            signed_baseline_grid_w = (
                slot.intentional_grid_import_w - slot.baseline_grid_export_w
            )
            natural_grid_w = signed_baseline_grid_w - slot.baseline_battery_power_w
            desired_grid_target_w = max(
                -max_grid_export_w,
                min(neutral_grid_target_w, natural_grid_w),
            )
            schedule[slot.period_start.isoformat()] = max(
                0.0,
                neutral_grid_target_w - desired_grid_target_w,
            )

    return schedule


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
    headroom_requirements: list[tuple[datetime, float]] | None = None,
    hard_headroom_requirements: list[tuple[datetime, float]] | None = None,
) -> ExportSchedulePlan:
    """Build one price-ranked schedule used by forecast and live control.

    During quiet hours the configured tolerance is split symmetrically around
    the actual price: the efficient band receives a preference and the louder
    boost band a penalty.  The first hour after the quiet window keeps the
    efficient/boost split without the quiet-hour bonus.  This one-hour grace
    avoids a hard power discontinuity at midnight while a materially better
    later price can still win. Headroom deadlines are physical constraints and
    therefore outrank export value. Hard combined-grid-cap requirements are
    allocated first; preferred PV-feed-in requirements consume only the
    remaining safe battery budget. Within each deadline price ranking still
    maximizes export value.

    EV-charging and intentional grid-import intervals stay at their neutral
    baseline target.  Combined forecast export is capped at
    ``max_grid_export_w``; unavoidable PV spill remains a physical exception.
    """

    available_kwh = max(0.0, available_export_energy_kwh)
    headroom_requirements = list(headroom_requirements or [])
    hard_headroom_requirements = list(hard_headroom_requirements or [])
    hard_headroom_required_kwh = 0.0
    for _deadline, requirement in hard_headroom_requirements:
        hard_headroom_required_kwh = max(
            hard_headroom_required_kwh,
            max(0.0, requirement),
        )
    requested_headroom_kwh = max(
        0.0,
        required_headroom_energy_kwh,
        hard_headroom_required_kwh,
    )
    max_grid_export_w = max(0.0, max_grid_export_w)
    max_battery_discharge_w = max(0.0, max_battery_discharge_w)
    efficient_discharge_w = max(0.0, min(max_battery_discharge_w, efficient_discharge_w))
    quiet_boost_penalty_fraction = max(0.0, min(1.0, quiet_boost_penalty_fraction))
    quiet_score_scale = max(
        1e-9,
        (1.0 - quiet_boost_penalty_fraction) ** 0.5,
    )
    # Retained in the public signature for compatibility and diagnostics. A
    # price spread must never disable headroom required by a physical limit.

    grid_target_schedule = {}
    battery_power_delta_schedule = {}
    headroom_bands = []
    economic_bands = []
    duration_by_key = {}
    period_start_by_key = {}

    for slot in slots:
        key = slot.period_start.isoformat()
        duration_by_key[key] = max(0.0, slot.duration_hours)
        period_start_by_key[key] = slot.period_start
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

        for score, _headroom_value, band_start_w, band_end_w in bands:
            band = (
                -score,
                slot.period_start,
                key,
                slot.duration_hours,
                band_start_w,
                band_end_w,
            )
            headroom_bands.append(band)
            if slot.price_per_kwh >= min_discharge_price:
                economic_bands.append(band)

    headroom_bands.sort()
    economic_bands.sort()
    allocated_w_by_key = {}

    if not headroom_requirements and headroom_deadline is not None:
        headroom_requirements = [(headroom_deadline, requested_headroom_kwh)]

    # Hard grid-cap headroom must be created as late as possible. If it is
    # discharged in an earlier high-price bucket, later PV can refill the
    # battery before the critical peak and the nominal headroom never reaches
    # the physical event it was intended to protect. Lower power bands remain
    # ahead of their boost band for a shared slot.
    hard_headroom_bands = sorted(
        headroom_bands,
        key=lambda band: (band[1], -band[4]),
        reverse=True,
    )
    hard_headroom_shortfall_kwh = _allocate_cumulative_requirements(
        hard_headroom_requirements,
        hard_headroom_bands,
        allocated_w_by_key,
        duration_by_key,
        period_start_by_key,
        available_kwh,
    )
    preferred_headroom_shortfall_kwh = _allocate_cumulative_requirements(
        headroom_requirements,
        headroom_bands,
        allocated_w_by_key,
        duration_by_key,
        period_start_by_key,
        available_kwh,
    )
    allocated_headroom_kwh = _allocated_energy_total(
        allocated_w_by_key,
        duration_by_key,
    )
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
        unallocated_headroom_kwh=max(0.0, preferred_headroom_shortfall_kwh),
        hard_headroom_required_kwh=hard_headroom_required_kwh,
        hard_headroom_shortfall_kwh=hard_headroom_shortfall_kwh,
    )


def adapt_grid_target_to_live_power(
    planned_battery_power_w: float,
    house_load_w: float,
    pv_power_w: float,
    max_grid_export_w: float,
    neutral_grid_target_w: float,
    ev_is_charging: bool = False,
    hold_optional_export: bool = False,
) -> float:
    """Preserve planned battery power using live load and PV measurements.

    A fixed neutral-minus-delta grid target loses its battery effect once
    natural PV export is already more negative than that target. Rebuilding
    the target from live natural flow keeps the replayed battery trajectory
    effective while the export cap and neutral clamp prevent excess export or
    avoidable import.
    """

    if ev_is_charging or hold_optional_export:
        return neutral_grid_target_w

    natural_grid_power_w = house_load_w - pv_power_w
    desired_grid_target_w = natural_grid_power_w + planned_battery_power_w
    return max(
        -max(0.0, max_grid_export_w),
        min(neutral_grid_target_w, desired_grid_target_w),
    )
