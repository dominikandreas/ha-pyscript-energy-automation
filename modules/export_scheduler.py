"""Pure helpers for the unified battery export scheduler.

The scheduler plans optional battery export power, never an absolute future
grid trajectory.  Forecast and live control apply that same relative request
on top of their current neutral behavior, so changed battery state, PV, house
load, and EV charging cannot leave stale grid targets behind.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite


# Stricter-than-forecast PV discount for pre-release shaping.  Forced
# discharge through the PV ramp also diverts rising PV to the grid instead
# of the battery, so the hole grows roughly 2x the scheduled energy and the
# forecast-accuracy discount alone (0.75) over-releases: the battery then
# misses 100% at end of day.  Reserve/surplus budgets keep their own factor.
SHAPING_PV_CONFIDENCE_FACTOR = 0.6

# Share of each headroom requirement that may move to night-before bands.
# The first pass locks the remainder same-day, where the forecast is
# freshest; the second pass tops up to the full cumulative target from
# any band before the deadline, so the proactive share chases the best
# price (often the evening peak) while same-day keeps a large
# forecast-error margin.  0.0 restores strict same-day confinement with
# night-before overflow only for the unfillable remainder.
NIGHT_BEFORE_HEADROOM_SHARE = 0.5



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
    protect_deficit: bool = True


@dataclass(frozen=True)
class ExportSchedulePlan:
    """Serializable result of one scheduler run."""

    grid_target_schedule: dict[str, float]
    battery_power_delta_schedule: dict[str, float]
    headroom_hold_schedule: dict[str, float]
    allocated_energy_kwh: float
    headroom_energy_kwh: float
    economic_energy_kwh: float
    unallocated_headroom_kwh: float
    hard_headroom_required_kwh: float
    hard_headroom_shortfall_kwh: float
    headroom_power_schedule: dict[str, float] | None = None


@dataclass(frozen=True)
class ExportEnergyBudget:
    """Physical and EV-aware authorization for optional battery export."""

    safe_export_energy_kwh: float
    ev_reservation_energy_kwh: float


@dataclass(frozen=True)
class FreshExportPolicyBudget:
    """Validated EV-aware policy allowance tied to one forecast snapshot."""

    base_spendable_energy_kwh: float
    ev_adjusted_spendable_energy_kwh: float
    battery_draw_since_calculation_kwh: float
    source_age_seconds: float | None
    valid: bool
    reason: str


@dataclass(frozen=True)
class MonotonicExportBudgetLedger:
    """Persistent remaining authorization for one policy snapshot."""

    remaining_kwh: float
    state_value: str
    valid: bool
    reason: str


@dataclass(frozen=True)
class HeadroomReleaseLedger:
    """Cumulative headroom energy already released for one spill event."""

    released_kwh: float
    state_value: str
    valid: bool
    reason: str


@dataclass(frozen=True)
class HighSideHeadroomPlan:
    """Headroom deadlines after correcting for observed PV underprediction."""

    requirements: list[tuple[datetime, float]]
    observed_pv_error_w: float
    peak_high_side_natural_export_w: float


@dataclass(frozen=True)
class LiveHeadroomControl:
    """One fast-loop decision for preserving emergency battery headroom."""

    requested_headroom_kwh: float
    headroom_deficit_kwh: float
    natural_grid_power_w: float
    natural_export_w: float
    desired_grid_target_w: float
    available_export_margin_w: float
    can_create_headroom: bool


@dataclass(frozen=True)
class LiveHeadroomState:
    """Latched state for the live export safety supervisor."""

    active: bool
    target_headroom_kwh: float
    hold_until: datetime | None


def build_high_side_headroom_requirements(
    slots: list[ExportSchedulerSlot],
    period_ends: dict[str, datetime],
    current_time: datetime,
    live_pv_power_w: float,
    forecast_pv_power_w: float,
    max_grid_export_w: float,
    error_decay_hours: float = 2.0,
    maximum_error_w: float = 4000.0,
    max_battery_charge_w: float | None = None,
    maximum_live_pv_power_w: float = 20000.0,
    battery_free_capacity_kwh: float | None = None,
) -> HighSideHeadroomPlan:
    """Project near-term headroom from the positive live PV forecast residual.

    A positive residual is applied to the natural (battery-neutral) export and
    decays linearly.  Energy above the planning export target becomes a
    cumulative headroom deadline, allowing the scheduler to react before the
    battery reaches full charge without treating one residual as a day-long
    forecast multiplier.  Known free battery room absorbs the residual first:
    surplus that fits in the battery needs no pre-release, so cumulative
    requirements are netted against it.  Unknown room leaves them untouched.
    """

    try:
        live_pv_power_w = float(live_pv_power_w)
        forecast_pv_power_w = float(forecast_pv_power_w)
        maximum_live_pv_power_w = max(0.0, float(maximum_live_pv_power_w))
        maximum_error_w = max(0.0, float(maximum_error_w))
        error_decay_hours = float(error_decay_hours)
        max_grid_export_w = float(max_grid_export_w)
    except (TypeError, ValueError, OverflowError):
        return HighSideHeadroomPlan([], 0.0, 0.0)
    if (
        not isfinite(live_pv_power_w)
        or not isfinite(forecast_pv_power_w)
        or not isfinite(maximum_live_pv_power_w)
        or not isfinite(maximum_error_w)
        or not isfinite(error_decay_hours)
        or not isfinite(max_grid_export_w)
        or live_pv_power_w < 0.0
        or live_pv_power_w > maximum_live_pv_power_w
        or forecast_pv_power_w < 0.0
        or forecast_pv_power_w > maximum_live_pv_power_w
    ):
        return HighSideHeadroomPlan([], 0.0, 0.0)

    observed_pv_error_w = min(
        maximum_error_w,
        max(0.0, live_pv_power_w - forecast_pv_power_w),
    )
    error_decay_hours = max(1e-9, error_decay_hours)
    max_grid_export_w = max(0.0, max_grid_export_w)
    if max_battery_charge_w is None:
        battery_charge_limit_w = float("inf")
    else:
        try:
            battery_charge_limit_w = float(max_battery_charge_w)
        except (TypeError, ValueError, OverflowError):
            return HighSideHeadroomPlan([], 0.0, 0.0)
        if not isfinite(battery_charge_limit_w) or battery_charge_limit_w < 0.0:
            return HighSideHeadroomPlan([], 0.0, 0.0)
    cumulative_headroom_kwh = 0.0
    peak_high_side_natural_export_w = 0.0
    requirements = []

    for slot in slots:
        hours_ahead = max(
            0.0,
            (slot.period_start - current_time).total_seconds() / 3600,
        )
        decay = max(0.0, 1.0 - hours_ahead / error_decay_hours)
        natural_grid_exchange_w = (
            float(slot.baseline_grid_export_w)
            + float(slot.baseline_battery_power_w)
            - float(slot.intentional_grid_import_w)
        )
        high_side_natural_export_w = max(
            0.0,
            natural_grid_exchange_w + observed_pv_error_w * decay,
        )
        peak_high_side_natural_export_w = max(
            peak_high_side_natural_export_w,
            high_side_natural_export_w,
        )
        overflow_w = calculate_absorbable_overflow_power(
            overflow_power_w=max(
                0.0,
                high_side_natural_export_w - max_grid_export_w,
            ),
            planned_battery_charge_power_w=slot.baseline_battery_power_w,
            max_battery_charge_power_w=battery_charge_limit_w,
        )
        if overflow_w <= 0 or slot.duration_hours <= 0:
            continue
        cumulative_headroom_kwh += (
            overflow_w * max(0.0, float(slot.duration_hours)) / 1000
        )
        deadline = period_ends.get(slot.period_start.isoformat())
        if deadline is not None:
            requirements.append((deadline, cumulative_headroom_kwh))

    try:
        free_capacity_kwh = (
            max(0.0, float(battery_free_capacity_kwh))
            if battery_free_capacity_kwh is not None
            else None
        )
    except (TypeError, ValueError, OverflowError):
        free_capacity_kwh = None
    if free_capacity_kwh is not None:
        netted_requirements = []
        for deadline, cumulative_kwh in requirements:
            try:
                remaining_kwh = max(0.0, float(cumulative_kwh)) - free_capacity_kwh
            except (TypeError, ValueError, OverflowError):
                netted_requirements.append((deadline, cumulative_kwh))
                continue
            if remaining_kwh > 0.0:
                netted_requirements.append((deadline, remaining_kwh))
        requirements = netted_requirements

    return HighSideHeadroomPlan(
        requirements=requirements,
        observed_pv_error_w=observed_pv_error_w,
        peak_high_side_natural_export_w=peak_high_side_natural_export_w,
    )


def parse_aware_datetime(value: datetime | str | None) -> datetime | None:
    """Parse only timezone-aware datetimes, returning ``None`` on bad input."""

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, datetime):
        return None
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return value


def calculate_measurement_age_seconds(
    current_time: datetime,
    measured_at: datetime | None,
) -> float | None:
    """Return signed sample age, or ``None`` for incomparable timestamps."""

    if not isinstance(current_time, datetime) or not isinstance(measured_at, datetime):
        return None
    try:
        return (current_time - measured_at).total_seconds()
    except (TypeError, ValueError):
        return None


def are_power_measurement_values_valid(
    grid_power_w: float,
    battery_power_w: float,
    max_absolute_power_w: float = 20000.0,
) -> bool:
    """Reject non-finite and physically implausible live power samples."""

    try:
        grid_power_w = float(grid_power_w)
        battery_power_w = float(battery_power_w)
        max_absolute_power_w = max(0.0, float(max_absolute_power_w))
    except (TypeError, ValueError, OverflowError):
        return False
    return bool(
        isfinite(grid_power_w)
        and isfinite(battery_power_w)
        and isfinite(max_absolute_power_w)
        and abs(grid_power_w) <= max_absolute_power_w
        and abs(battery_power_w) <= max_absolute_power_w
    )


def are_battery_energy_values_valid(
    battery_capacity_kwh: float,
    battery_energy_kwh: float,
) -> bool:
    """Require finite battery energy within a positive physical capacity."""

    try:
        battery_capacity_kwh = float(battery_capacity_kwh)
        battery_energy_kwh = float(battery_energy_kwh)
    except (TypeError, ValueError, OverflowError):
        return False
    return bool(
        isfinite(battery_capacity_kwh)
        and isfinite(battery_energy_kwh)
        and battery_capacity_kwh > 0.0
        and 0.0 <= battery_energy_kwh <= battery_capacity_kwh
    )


def resolve_directional_power_limit_w(
    configured_limit_w: float,
    unavailable_fallback_w: float,
    physical_maximum_w: float,
) -> float:
    """Resolve one directional limit without turning invalid data into permission."""

    try:
        configured_limit_w = float(configured_limit_w)
        unavailable_fallback_w = float(unavailable_fallback_w)
        physical_maximum_w = float(physical_maximum_w)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if (
        not isfinite(configured_limit_w)
        or not isfinite(unavailable_fallback_w)
        or not isfinite(physical_maximum_w)
        or unavailable_fallback_w < 0.0
        or physical_maximum_w < 0.0
    ):
        return 0.0
    if configured_limit_w == -1.0:
        return min(unavailable_fallback_w, physical_maximum_w)
    if configured_limit_w < 0.0:
        return 0.0
    return min(configured_limit_w, physical_maximum_w)


def is_measurement_fresh(
    current_time: datetime,
    measured_at: datetime | None,
    max_age_seconds: float,
    allowed_future_seconds: float = 0.0,
) -> bool:
    """Accept only comparable timestamps inside the configured age window."""

    age_seconds = calculate_measurement_age_seconds(current_time, measured_at)
    if age_seconds is None:
        return False
    return bool(
        -max(0.0, float(allowed_future_seconds))
        <= age_seconds
        <= max(0.0, float(max_age_seconds))
    )


STATIC_BATTERY_POWER_TOLERANCE_W = 50.0
"""Battery power below which SoC/energy may legitimately sit still."""

STATIC_BATTERY_ENERGY_CONSISTENCY_TOLERANCE_KWH = 0.5
"""Max deviation between stored energy and SoC x capacity for a still reading."""

MAX_UNOBSERVED_BATTERY_ENERGY_CHANGE_KWH = 0.1
"""Maximum bounded drift accepted between rounded energy sensor updates."""


def is_battery_energy_measurement_usable(
    current_time: datetime,
    energy_updated_at: datetime | None,
    battery_energy_kwh: float,
    battery_capacity_kwh: float,
    battery_soc_percent: float,
    battery_power_w: float,
    battery_power_updated_at: datetime | None,
    max_age_seconds: float = 300.0,
    static_power_tolerance_w: float = STATIC_BATTERY_POWER_TOLERANCE_W,
    energy_consistency_tolerance_kwh: float = STATIC_BATTERY_ENERGY_CONSISTENCY_TOLERANCE_KWH,
    battery_soc_updated_at: datetime | None = None,
    max_unobserved_energy_change_kwh: float = MAX_UNOBSERVED_BATTERY_ENERGY_CHANGE_KWH,
) -> bool:
    """Freshness with a static-battery exception.

    Home Assistant only advances ``last_updated`` when the value changes, so
    an idle battery's SoC/energy reading legitimately goes "stale" while the
    battery sits still. Trust such a reading when the (fresh) battery power
    proves the battery is idle and the energy agrees with SoC x capacity;
    otherwise fall back to strict freshness.
    """

    if is_measurement_fresh(current_time, energy_updated_at, max_age_seconds):
        return True
    power_is_fresh = is_measurement_fresh(
        current_time, battery_power_updated_at, max_age_seconds
    )
    soc_is_fresh = is_measurement_fresh(
        current_time, battery_soc_updated_at, max_age_seconds
    )
    if not power_is_fresh and not soc_is_fresh:
        return False
    try:
        energy_kwh = float(battery_energy_kwh)
        capacity_kwh = float(battery_capacity_kwh)
        soc_percent = float(battery_soc_percent)
        power_w = float(battery_power_w)
        power_tolerance_w = max(0.0, float(static_power_tolerance_w))
        consistency_tolerance_kwh = max(
            0.0, float(energy_consistency_tolerance_kwh)
        )
    except (TypeError, ValueError, OverflowError):
        return False
    if not (
        isfinite(energy_kwh)
        and isfinite(capacity_kwh)
        and isfinite(soc_percent)
        and isfinite(power_w)
    ):
        return False
    if capacity_kwh <= 0.0:
        return False
    if not 0.0 <= soc_percent <= 100.0:
        return False
    if not 0.0 <= energy_kwh <= capacity_kwh:
        return False
    expected_energy_kwh = soc_percent / 100.0 * capacity_kwh
    if abs(energy_kwh - expected_energy_kwh) > consistency_tolerance_kwh:
        return False
    # A fresh SoC provides an independent, capacity-consistent estimate even
    # while power is nonzero. Without it, retain the older idle-power proof.
    if soc_is_fresh or (power_is_fresh and abs(power_w) <= power_tolerance_w):
        return True
    energy_age_seconds = calculate_measurement_age_seconds(current_time, energy_updated_at)
    if not power_is_fresh or energy_age_seconds is None or energy_age_seconds < 0:
        return False
    # Rounded energy and SoC sensors can remain unchanged for longer than the
    # nominal freshness window at low power. Bound the possible drift using
    # the fresh power measurement; high power therefore expires much sooner.
    unobserved_change_kwh = abs(power_w) * energy_age_seconds / 3_600_000
    return bool(
        unobserved_change_kwh <= max(0.0, float(max_unobserved_energy_change_kwh))
    )


def get_fresh_export_policy_budget(
    base_spendable_energy_kwh: float,
    ev_adjusted_spendable_energy_kwh: float,
    source_battery_energy_kwh: float,
    current_battery_energy_kwh: float,
    calculated_at: datetime | str | None,
    current_time: datetime,
    max_age_seconds: float = 180.0,
    discharge_efficiency: float = 0.90,
) -> FreshExportPolicyBudget:
    """Validate a policy snapshot before it authorizes battery export."""

    try:
        base_spendable_energy_kwh = float(base_spendable_energy_kwh)
        ev_adjusted_spendable_energy_kwh = float(ev_adjusted_spendable_energy_kwh)
        source_battery_energy_kwh = float(source_battery_energy_kwh)
        current_battery_energy_kwh = float(current_battery_energy_kwh)
        discharge_efficiency = float(discharge_efficiency)
    except (TypeError, ValueError, OverflowError):
        values_are_valid = False
    else:
        values_are_valid = bool(
            isfinite(base_spendable_energy_kwh)
            and isfinite(ev_adjusted_spendable_energy_kwh)
            and isfinite(source_battery_energy_kwh)
            and isfinite(current_battery_energy_kwh)
            and isfinite(discharge_efficiency)
            and base_spendable_energy_kwh >= 0.0
            and 0.0
            <= ev_adjusted_spendable_energy_kwh
            <= base_spendable_energy_kwh
            and source_battery_energy_kwh >= 0.0
            and current_battery_energy_kwh >= 0.0
            and 0.0 < discharge_efficiency <= 1.0
        )

    calculated_at = parse_aware_datetime(calculated_at)
    source_age_seconds = calculate_measurement_age_seconds(
        current_time,
        calculated_at,
    )
    if not values_are_valid or not is_measurement_fresh(
        current_time,
        calculated_at,
        max_age_seconds=max_age_seconds,
    ):
        return FreshExportPolicyBudget(
            base_spendable_energy_kwh=0.0,
            ev_adjusted_spendable_energy_kwh=0.0,
            battery_draw_since_calculation_kwh=0.0,
            source_age_seconds=source_age_seconds,
            valid=False,
            reason="invalid_or_stale_source",
        )

    discharge_efficiency = max(0.0, min(1.0, discharge_efficiency))
    battery_draw_since_calculation_kwh = (
        max(0.0, source_battery_energy_kwh - current_battery_energy_kwh)
        * discharge_efficiency
    )
    base_spendable_energy_kwh = max(
        0.0,
        base_spendable_energy_kwh - battery_draw_since_calculation_kwh,
    )
    ev_adjusted_spendable_energy_kwh = max(
        0.0,
        min(
            base_spendable_energy_kwh,
            ev_adjusted_spendable_energy_kwh
            - battery_draw_since_calculation_kwh,
        ),
    )
    return FreshExportPolicyBudget(
        base_spendable_energy_kwh=base_spendable_energy_kwh,
        ev_adjusted_spendable_energy_kwh=ev_adjusted_spendable_energy_kwh,
        battery_draw_since_calculation_kwh=battery_draw_since_calculation_kwh,
        source_age_seconds=source_age_seconds,
        valid=True,
        reason="none",
    )


def update_monotonic_export_budget_ledger(
    snapshot_id: str,
    candidate_remaining_kwh: float,
    previous_state: str,
    current_time: datetime | None = None,
    battery_discharge_power_w: float = 0.0,
    measurements_available: bool | None = None,
    accounting_enabled: bool = True,
    max_accounting_gap_seconds: float = 15.0,
    max_snapshot_future_skew_seconds: float = 2.0,
) -> MonotonicExportBudgetLedger:
    """Persist a non-restoring policy budget and cumulative battery discharge.

    When accounting resumes after a gap (e.g. the scheduler held export while
    power telemetry stayed healthy) the live candidate already deducts the
    source-to-current battery draw, so the clock is rebased instead of
    latching the snapshot invalid until it rotates. Gaps without healthy
    telemetry, or a clock that runs backwards, still invalidate.
    """

    if not isinstance(snapshot_id, str) or not snapshot_id or "|" in snapshot_id:
        return MonotonicExportBudgetLedger(0.0, "", False, "invalid_snapshot_id")
    snapshot_at = parse_aware_datetime(snapshot_id)
    if snapshot_at is None:
        return MonotonicExportBudgetLedger(0.0, "", False, "invalid_snapshot_id")
    try:
        candidate_remaining_kwh = float(candidate_remaining_kwh)
        battery_discharge_power_w = float(battery_discharge_power_w)
        max_accounting_gap_seconds = float(max_accounting_gap_seconds)
        max_snapshot_future_skew_seconds = float(max_snapshot_future_skew_seconds)
    except (TypeError, ValueError, OverflowError):
        return MonotonicExportBudgetLedger(0.0, "", False, "invalid_candidate")
    if (
        not isfinite(candidate_remaining_kwh)
        or candidate_remaining_kwh < 0.0
        or not isfinite(battery_discharge_power_w)
        or battery_discharge_power_w < 0.0
        or not isfinite(max_accounting_gap_seconds)
        or max_accounting_gap_seconds <= 0.0
        or not isfinite(max_snapshot_future_skew_seconds)
        or max_snapshot_future_skew_seconds < 0.0
    ):
        return MonotonicExportBudgetLedger(0.0, "", False, "invalid_candidate")

    accounted_at = parse_aware_datetime(current_time)
    if current_time is not None and accounted_at is None:
        return MonotonicExportBudgetLedger(0.0, "", False, "invalid_accounting_time")
    if (
        accounted_at is not None
        and (snapshot_at - accounted_at).total_seconds()
        > max_snapshot_future_skew_seconds
    ):
        return MonotonicExportBudgetLedger(0.0, "", False, "future_snapshot_id")
    if current_time is None and battery_discharge_power_w > 0.0 and accounting_enabled:
        return MonotonicExportBudgetLedger(0.0, "", False, "missing_accounting_time")

    previous_state = str(previous_state or "")
    initial_kwh = candidate_remaining_kwh
    consumed_kwh = 0.0
    floor_kwh = candidate_remaining_kwh
    previous_accounted_at = None
    previous_accounted_at_text = ""
    same_snapshot = False
    if previous_state:
        parts = previous_state.split("|")
        if len(parts) != 6:
            return MonotonicExportBudgetLedger(
                0.0,
                previous_state,
                False,
                "invalid_persisted_state",
            )
        (
            previous_snapshot_id,
            previous_initial_text,
            previous_consumed_text,
            previous_floor_text,
            previous_accounted_at_text,
            previous_status,
        ) = parts
        try:
            previous_initial_kwh = float(previous_initial_text)
            previous_consumed_kwh = float(previous_consumed_text)
            previous_floor_kwh = float(previous_floor_text)
        except (TypeError, ValueError, OverflowError):
            previous_initial_kwh = -1.0
            previous_consumed_kwh = -1.0
            previous_floor_kwh = -1.0
        previous_values_valid = (
            isfinite(previous_initial_kwh)
            and previous_initial_kwh >= 0.0
            and isfinite(previous_consumed_kwh)
            and previous_consumed_kwh >= 0.0
            and isfinite(previous_floor_kwh)
            and previous_floor_kwh >= 0.0
        )
        if previous_accounted_at_text:
            previous_accounted_at = parse_aware_datetime(previous_accounted_at_text)
        previous_snapshot_at = parse_aware_datetime(previous_snapshot_id)
        if (
            not previous_snapshot_id
            or previous_snapshot_at is None
            or not previous_values_valid
            or (previous_accounted_at_text and previous_accounted_at is None)
            or previous_status not in ("valid", "invalid")
        ):
            return MonotonicExportBudgetLedger(
                0.0,
                previous_state,
                False,
                "invalid_persisted_state",
            )
        if (
            accounted_at is not None
            and (previous_snapshot_at - accounted_at).total_seconds()
            > max_snapshot_future_skew_seconds
        ):
            return MonotonicExportBudgetLedger(
                0.0,
                previous_state,
                False,
                "future_persisted_snapshot_id",
            )
        if snapshot_at < previous_snapshot_at:
            return MonotonicExportBudgetLedger(
                0.0,
                previous_state,
                False,
                "out_of_order_snapshot_id",
            )
        same_snapshot = snapshot_at == previous_snapshot_at
        if same_snapshot:
            initial_kwh = previous_initial_kwh
            consumed_kwh = previous_consumed_kwh
            floor_kwh = min(previous_floor_kwh, candidate_remaining_kwh)

            if previous_status == "invalid":
                return MonotonicExportBudgetLedger(
                    0.0,
                    previous_state,
                    False,
                    "persisted_snapshot_invalid",
                )

    if measurements_available is False:
        state_value = (
            f"{snapshot_id}|{initial_kwh:.6f}|{consumed_kwh:.6f}|"
            f"{floor_kwh:.6f}|{previous_accounted_at_text}|invalid"
        )
        return MonotonicExportBudgetLedger(
            0.0,
            state_value,
            False,
            "power_measurements_unavailable",
        )

    ledger_reason = "none"
    if (
        accounting_enabled
        and same_snapshot
        and accounted_at is not None
        and previous_accounted_at is not None
    ):
        try:
            elapsed_seconds = (accounted_at - previous_accounted_at).total_seconds()
        except (TypeError, ValueError, OverflowError):
            elapsed_seconds = -1.0
        if elapsed_seconds < 0.0 or (
            elapsed_seconds > max_accounting_gap_seconds
            and not measurements_available
        ):
            state_value = (
                f"{snapshot_id}|{initial_kwh:.6f}|{consumed_kwh:.6f}|"
                f"{floor_kwh:.6f}|{previous_accounted_at_text}|invalid"
            )
            return MonotonicExportBudgetLedger(
                0.0,
                state_value,
                False,
                "invalid_accounting_gap",
            )
        if elapsed_seconds > max_accounting_gap_seconds:
            # Healthy telemetry across the gap: the live candidate already
            # deducts the source-to-current draw, so resume the clock and
            # cap the floor instead of latching invalid until rotation.
            floor_kwh = min(floor_kwh, candidate_remaining_kwh)
            ledger_reason = "accounting_gap_rebased"
        else:
            consumed_kwh += battery_discharge_power_w * elapsed_seconds / 3_600_000.0

    remaining_kwh = min(
        floor_kwh,
        max(0.0, initial_kwh - consumed_kwh),
    )
    accounted_at_text = (
        accounted_at.isoformat()
        if accounting_enabled and accounted_at is not None
        else previous_accounted_at_text
    )
    state_value = (
        f"{snapshot_id}|{initial_kwh:.6f}|{consumed_kwh:.6f}|"
        f"{floor_kwh:.6f}|{accounted_at_text}|valid"
    )
    return MonotonicExportBudgetLedger(
        remaining_kwh=remaining_kwh,
        state_value=state_value,
        valid=True,
        reason=ledger_reason,
    )


def read_headroom_released_kwh(event_key: str, previous_state: str) -> float:
    """Return ledgered headroom energy for ``event_key`` without advancing it.

    Unknown events, empty states, and malformed states read as zero so a
    fresh or corrupted ledger never blocks legitimate pre-release.
    """

    if not isinstance(event_key, str) or not event_key:
        return 0.0
    parts = str(previous_state or "").split("|")
    if len(parts) != 3:
        return 0.0
    stored_key, released_text, accounted_text = parts
    if stored_key != event_key:
        return 0.0
    try:
        released_kwh = float(released_text)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not isfinite(released_kwh) or released_kwh < 0.0:
        return 0.0
    if parse_aware_datetime(accounted_text) is None:
        return 0.0
    return released_kwh


def headroom_release_factor(required_kwh: float, released_kwh: float) -> float:
    """Scale headroom requirements by the share not yet released.

    With no requirement there is nothing to scale; with no valid released
    total the full requirement stands.  The factor reaches zero once the
    ledger covers the requirement so repeated runs converge instead of
    re-allocating the same spill energy every few seconds.
    """

    try:
        required_kwh = float(required_kwh)
        released_kwh = float(released_kwh)
    except (TypeError, ValueError, OverflowError):
        return 1.0
    if (
        not isfinite(required_kwh)
        or not isfinite(released_kwh)
        or required_kwh <= 0.0
        or released_kwh < 0.0
    ):
        return 1.0
    return max(0.0, min(1.0, (required_kwh - released_kwh) / required_kwh))


def update_headroom_release_ledger(
    event_key: str,
    headroom_power_w: float,
    current_time: datetime | str | None,
    previous_state: str,
    max_accounting_gap_seconds: float = 600.0,
) -> HeadroomReleaseLedger:
    """Accumulate commanded headroom power into per-spill-event energy.

    The planner re-allocates every few seconds from a requirement that does
    not shrink as the battery drains (a strong-PV day fills by late morning
    from any evening level), so without execution memory the same spill
    energy is released over and over.  The ledger integrates the commanded
    headroom power over wall time per event key (one spill deadline) so the
    planner can net already-released energy.  A new event key resets the
    total; gaps beyond ``max_accounting_gap_seconds`` (restarts, downtime)
    are capped instead of extrapolated; invalid input fails safe to zero
    released (legacy behavior) rather than blocking pre-release.
    """

    if not isinstance(event_key, str) or not event_key or "|" in event_key:
        return HeadroomReleaseLedger(0.0, "", False, "invalid_event_key")
    try:
        power_w = max(0.0, float(headroom_power_w))
        max_gap = float(max_accounting_gap_seconds)
    except (TypeError, ValueError, OverflowError):
        return HeadroomReleaseLedger(0.0, "", False, "invalid_candidate")
    if not isfinite(power_w) or not isfinite(max_gap) or max_gap <= 0.0:
        return HeadroomReleaseLedger(0.0, "", False, "invalid_candidate")
    accounted_at = parse_aware_datetime(current_time)
    if accounted_at is None:
        return HeadroomReleaseLedger(0.0, "", False, "invalid_accounting_time")

    previous_state = str(previous_state or "")
    if not previous_state:
        return HeadroomReleaseLedger(
            0.0,
            f"{event_key}|0.000000|{accounted_at.isoformat()}",
            True,
            "ledger_started",
        )
    parts = previous_state.split("|")
    if len(parts) != 3:
        return HeadroomReleaseLedger(0.0, previous_state, False, "invalid_persisted_state")
    stored_key, released_text, accounted_text = parts
    if stored_key != event_key:
        return HeadroomReleaseLedger(
            0.0,
            f"{event_key}|0.000000|{accounted_at.isoformat()}",
            True,
            "event_rotated",
        )
    try:
        released_kwh = float(released_text)
    except (TypeError, ValueError, OverflowError):
        return HeadroomReleaseLedger(0.0, previous_state, False, "invalid_persisted_state")
    previous_accounted_at = parse_aware_datetime(accounted_text)
    if (
        not isfinite(released_kwh)
        or released_kwh < 0.0
        or previous_accounted_at is None
    ):
        return HeadroomReleaseLedger(0.0, previous_state, False, "invalid_persisted_state")
    elapsed_seconds = (accounted_at - previous_accounted_at).total_seconds()
    if elapsed_seconds < 0.0:
        return HeadroomReleaseLedger(0.0, previous_state, False, "clock_moved_backwards")
    released_kwh += power_w * min(elapsed_seconds, max_gap) / 3_600_000.0
    return HeadroomReleaseLedger(
        released_kwh,
        f"{event_key}|{released_kwh:.6f}|{accounted_at.isoformat()}",
        True,
        "accounted",
    )


def calculate_measurement_skew_seconds(
    grid_updated_at: datetime | None,
    battery_updated_at: datetime | None,
) -> float | None:
    """Return sample skew, or ``None`` when timestamps cannot be compared."""

    if not isinstance(grid_updated_at, datetime) or not isinstance(
        battery_updated_at,
        datetime,
    ):
        return None
    try:
        return abs((grid_updated_at - battery_updated_at).total_seconds())
    except (TypeError, ValueError):
        return None


def are_power_measurements_coherent(
    current_time: datetime,
    grid_updated_at: datetime | None,
    battery_updated_at: datetime | None,
    max_age_seconds: float = 10.0,
    max_skew_seconds: float = 5.0,
) -> bool:
    """Require fresh grid/battery samples from approximately the same instant."""

    if not isinstance(grid_updated_at, datetime) or not isinstance(
        battery_updated_at,
        datetime,
    ):
        return False
    skew_seconds = calculate_measurement_skew_seconds(
        grid_updated_at,
        battery_updated_at,
    )
    if skew_seconds is None:
        return False
    max_skew_seconds = max(0.0, float(max_skew_seconds))
    return bool(
        is_measurement_fresh(
            current_time,
            grid_updated_at,
            max_age_seconds=max_age_seconds,
        )
        and is_measurement_fresh(
            current_time,
            battery_updated_at,
            max_age_seconds=max_age_seconds,
        )
        and skew_seconds <= max_skew_seconds
    )


def calculate_absorbable_overflow_power(
    overflow_power_w: float,
    planned_battery_charge_power_w: float,
    max_battery_charge_power_w: float,
) -> float:
    """Return overflow that extra battery charging can physically absorb."""

    remaining_charge_power_w = max(
        0.0,
        float(max_battery_charge_power_w)
        - max(0.0, float(planned_battery_charge_power_w)),
    )
    return min(max(0.0, float(overflow_power_w)), remaining_charge_power_w)


def calculate_battery_neutral_grid_target_w(
    measured_grid_power_w: float,
    measured_battery_power_w: float,
) -> float:
    """Return the grid target that requests zero battery power."""

    return float(measured_grid_power_w) - float(measured_battery_power_w)


def select_no_battery_export_target(
    pv_only_grid_target_w: float,
    neutral_grid_target_w: float,
    pv_measurement_fresh: bool,
) -> float:
    """Use PV-derived export only while its source measurement is trustworthy."""

    if not pv_measurement_fresh:
        return float(neutral_grid_target_w)
    return float(pv_only_grid_target_w)


def limit_live_export_target_to_budget(
    desired_grid_target_w: float,
    pv_only_grid_target_w: float,
    safe_export_energy_kwh: float,
    budget_horizon_seconds: float = 120.0,
    max_battery_discharge_w: float | None = None,
) -> float:
    """Cap battery-to-grid power to the EV-adjusted budget until replanning."""

    desired_grid_target_w = float(desired_grid_target_w)
    pv_only_grid_target_w = float(pv_only_grid_target_w)
    safe_export_energy_kwh = max(0.0, float(safe_export_energy_kwh))
    budget_horizon_seconds = max(1.0, float(budget_horizon_seconds))
    max_optional_export_w = (
        safe_export_energy_kwh * 1000.0 * 3600.0 / budget_horizon_seconds
    )
    if max_battery_discharge_w is not None:
        try:
            max_battery_discharge_w = float(max_battery_discharge_w)
        except (TypeError, ValueError, OverflowError):
            max_battery_discharge_w = 0.0
        if not isfinite(max_battery_discharge_w):
            max_battery_discharge_w = 0.0
        max_optional_export_w = min(
            max_optional_export_w,
            max(0.0, max_battery_discharge_w),
        )
    budget_limited_target_w = pv_only_grid_target_w - max_optional_export_w
    return max(desired_grid_target_w, budget_limited_target_w)


def calculate_time_to_full_minutes(
    battery_free_capacity_kwh: float,
    battery_charge_power_w: float,
    charge_efficiency: float = 0.90,
) -> float | None:
    """Estimate saturation time from the live stored-energy charge rate."""

    stored_charge_power_w = max(0.0, float(battery_charge_power_w)) * max(
        0.0,
        min(1.0, float(charge_efficiency)),
    )
    if stored_charge_power_w <= 0:
        return None
    return (
        max(0.0, float(battery_free_capacity_kwh))
        * 1000
        / stored_charge_power_w
        * 60
    )


def update_live_headroom_state(
    current_time: datetime,
    previous_active: bool,
    previous_target_headroom_kwh: float,
    previous_hold_until: datetime | None,
    requested_headroom_kwh: float,
    battery_free_capacity_kwh: float,
    natural_export_w: float,
    preferred_pv_export_w: float,
    hold_minutes: float = 15.0,
    activation_buffer_kwh: float = 0.05,
    release_hysteresis_w: float = 500.0,
    time_to_full_minutes: float | None = None,
    activation_lead_time_minutes: float = 60.0,
) -> LiveHeadroomState:
    """Latch headroom control through short cloud gaps.

    A new hold starts only near the requested free-capacity boundary.  Once
    active, higher risk can raise the target but a transient PV dip cannot
    lower it.  Release needs only expiry of the hold timer: fifteen
    continuous minutes without risk.  Requiring natural export below a
    hysteretic threshold as well traps the supervisor forever in the band
    between the thresholds (observed 2026-09-25: 2841 W natural export
    held a stale 0.628 kWh target all evening, pinning the battery at
    98.3% and discharging to "create" room for no current risk), while
    risk that actually returns re-engages on the next cycle anyway.
    ``release_hysteresis_w`` is retained in the signature for
    compatibility and no longer gates the release.
    """

    requested_headroom_kwh = max(0.0, float(requested_headroom_kwh))
    battery_free_capacity_kwh = max(0.0, float(battery_free_capacity_kwh))
    natural_export_w = max(0.0, float(natural_export_w))
    preferred_pv_export_w = max(0.0, float(preferred_pv_export_w))
    activation_buffer_kwh = max(0.0, float(activation_buffer_kwh))
    release_hysteresis_w = max(0.0, float(release_hysteresis_w))
    hold_duration = max(0.0, float(hold_minutes))
    activation_lead_time_minutes = max(0.0, float(activation_lead_time_minutes))
    saturation_imminent = bool(
        time_to_full_minutes is not None
        and 0.0 <= float(time_to_full_minutes) <= activation_lead_time_minutes
    )

    active = bool(previous_active)
    target_headroom_kwh = max(0.0, float(previous_target_headroom_kwh))
    hold_until = previous_hold_until
    risk_present = requested_headroom_kwh > 0

    if not active and risk_present:
        active = (
            battery_free_capacity_kwh
            <= requested_headroom_kwh + activation_buffer_kwh
            or saturation_imminent
        )
        if active:
            target_headroom_kwh = requested_headroom_kwh
            hold_until = current_time + timedelta(minutes=hold_duration)
    elif active and risk_present:
        target_headroom_kwh = max(
            target_headroom_kwh,
            requested_headroom_kwh,
        )
        hold_until = current_time + timedelta(minutes=hold_duration)
    elif active:
        hold_expired = hold_until is None or current_time >= hold_until
        if hold_expired:
            active = False
            target_headroom_kwh = 0.0
            hold_until = None

    return LiveHeadroomState(
        active=active,
        target_headroom_kwh=target_headroom_kwh,
        hold_until=hold_until,
    )


def calculate_live_headroom_control(
    measured_grid_power_w: float,
    measured_battery_power_w: float,
    battery_free_capacity_kwh: float,
    held_headroom_target_kwh: float,
    preferred_pv_export_w: float,
    planned_grid_export_cap_w: float,
    neutral_grid_target_w: float,
    max_battery_charge_w: float = 6600.0,
    max_battery_discharge_w: float = 8000.0,
    risk_horizon_minutes: float = 10.0,
    base_headroom_kwh: float = 0.05,
    maximum_headroom_kwh: float = 1.0,
    tracking_margin_w: float = 200.0,
    discharge_efficiency: float = 0.90,
) -> LiveHeadroomControl:
    """Return a bounded live headroom hold/create target.

    Grid and battery power use the installation convention: grid import and
    battery charging are positive.  Removing measured battery power from the
    measured grid flow recovers the natural grid flow that would remain if the
    battery stopped charging or discharging.  This is substantially more
    reliable during rapid PV ramps than the derived house-load sensor.

    The requested headroom is an energy quantity derived from natural export
    above the preferred PV target over a short risk horizon.  The returned grid
    target preserves the effective headroom target while allowing free
    capacity above it to charge, paced over the risk horizon so a sudden PV
    spike still finds room; only when no excess room remains does the hold
    request zero battery power.  Missing capacity is created only from
    export margin below the planning cap.  It never asks battery discharge
    to push grid export through that cap.
    """

    battery_free_capacity_kwh = max(0.0, float(battery_free_capacity_kwh))
    held_headroom_target_kwh = max(0.0, float(held_headroom_target_kwh))
    preferred_pv_export_w = max(0.0, float(preferred_pv_export_w))
    planned_grid_export_cap_w = max(0.0, float(planned_grid_export_cap_w))
    max_battery_charge_w = max(0.0, float(max_battery_charge_w))
    max_battery_discharge_w = max(0.0, float(max_battery_discharge_w))
    risk_horizon_hours = max(0.0, float(risk_horizon_minutes)) / 60
    base_headroom_kwh = max(0.0, float(base_headroom_kwh))
    maximum_headroom_kwh = max(base_headroom_kwh, float(maximum_headroom_kwh))
    tracking_margin_w = max(0.0, float(tracking_margin_w))
    discharge_efficiency = max(1e-9, min(1.0, float(discharge_efficiency)))

    natural_grid_power_w = float(measured_grid_power_w) - float(measured_battery_power_w)
    natural_export_w = max(0.0, -natural_grid_power_w)
    remaining_charge_acceptance_w = max(
        0.0,
        max_battery_charge_w - max(0.0, float(measured_battery_power_w)),
    )
    risk_power_w = min(
        max(0.0, natural_export_w - preferred_pv_export_w),
        remaining_charge_acceptance_w,
    )
    requested_headroom_kwh = 0.0
    if risk_power_w > 0 and risk_horizon_hours > 0:
        requested_headroom_kwh = min(
            maximum_headroom_kwh,
            base_headroom_kwh + risk_power_w * risk_horizon_hours / 1000,
        )

    effective_headroom_target_kwh = max(
        requested_headroom_kwh,
        held_headroom_target_kwh,
    )
    headroom_deficit_kwh = max(
        0.0,
        effective_headroom_target_kwh - battery_free_capacity_kwh,
    )

    safe_export_cap_w = max(0.0, planned_grid_export_cap_w - tracking_margin_w)
    available_export_margin_w = max(0.0, safe_export_cap_w - natural_export_w)
    desired_discharge_w = 0.0
    if headroom_deficit_kwh > 0 and risk_horizon_hours > 0:
        # Stored DC energy falls faster than delivered AC energy because of
        # conversion loss.  Only the AC power needed to create the requested
        # DC capacity is scheduled here.
        desired_discharge_w = min(
            available_export_margin_w,
            max_battery_discharge_w,
            headroom_deficit_kwh
            * discharge_efficiency
            * 1000
            / risk_horizon_hours,
        )

    # Charge the headroom excess instead of freezing the battery: with
    # free room above the target the top-up continues, paced over the risk
    # horizon so charging converges to the target without overshooting it.
    # A deficit leaves no excess, so creation and charging stay exclusive.
    headroom_excess_kwh = max(
        0.0,
        battery_free_capacity_kwh - effective_headroom_target_kwh,
    )
    if risk_horizon_hours > 0:
        paced_charge_w = headroom_excess_kwh * 1000 / risk_horizon_hours
    else:
        paced_charge_w = 0.0
    allowed_charge_w = min(
        natural_export_w,
        remaining_charge_acceptance_w,
        paced_charge_w,
    )
    desired_export_w = min(
        safe_export_cap_w,
        natural_export_w - allowed_charge_w + desired_discharge_w,
    )
    desired_grid_target_w = max(
        -safe_export_cap_w,
        min(float(neutral_grid_target_w), -desired_export_w),
    )

    return LiveHeadroomControl(
        requested_headroom_kwh=requested_headroom_kwh,
        headroom_deficit_kwh=headroom_deficit_kwh,
        natural_grid_power_w=natural_grid_power_w,
        natural_export_w=natural_export_w,
        desired_grid_target_w=desired_grid_target_w,
        available_export_margin_w=available_export_margin_w,
        can_create_headroom=(
            headroom_deficit_kwh > 1e-9 and desired_discharge_w > 1e-9
        ),
    )


def calculate_net_battery_export_price(
    gross_price_per_kwh: float,
    inverter_efficiency: float = 0.90,
    broker_revenue_margin_fraction: float = 0.03,
) -> float:
    """Return net revenue per stored battery kWh exported to the grid.

    The market price is a ranking signal, not permission to export energy.  The
    separate policy budget remains authoritative.  Applying conversion loss and
    the broker's revenue share here makes ``min_discharge_price`` comparable to
    the value of energy still stored in the battery.
    """

    inverter_efficiency = max(0.0, min(1.0, inverter_efficiency))
    broker_revenue_margin_fraction = max(
        0.0,
        min(1.0, broker_revenue_margin_fraction),
    )
    return (
        float(gross_price_per_kwh)
        * inverter_efficiency
        * (1.0 - broker_revenue_margin_fraction)
    )


def get_interval_price(
    prices: list[dict],
    period_start: datetime,
    fallback: float | None = None,
) -> float | None:
    """Return the price whose published interval contains ``period_start``.

    Home Assistant's EPEX integration publishes hourly intervals while this
    controller simulates half-hour buckets.  Timestamp containment avoids
    coupling the two resolutions and remains correct across DST transitions.
    Malformed provider entries are ignored so one bad bucket does not stop the
    surplus forecast or leave a stale export authorization active.
    """

    target_timestamp = period_start.timestamp()
    for entry in prices:
        try:
            start = datetime.fromisoformat(entry["start_time"])
            end = datetime.fromisoformat(entry["end_time"])
            price = float(entry["price_per_kwh"])
        except (KeyError, TypeError, ValueError):
            continue
        if start.timestamp() <= target_timestamp < end.timestamp():
            return price
    return fallback


def build_reserve_energy_schedule(
    slots: list[ReserveTrajectorySlot],
    battery_capacity_kwh: float,
    uncertainty_margin_kwh: float = 1.0,
    house_load_margin_fraction: float = 0.0,
    pv_confidence_factor: float = 1.0,
    discharge_efficiency: float = 1.0,
) -> dict[str, float]:
    """Return the protected energy for every future interval.

    Protected deficit intervals reserve enough energy to reach the next
    interval where conservative PV meets conservative house demand.  Cheap
    import intervals may be left unprotected while still carrying later,
    expensive demand backwards.  Inflating house load and discounting PV makes
    the reserve shrink naturally as uncertain overnight intervals become
    observed history.  Once conservative PV covers demand, only the
    installation reserve and base uncertainty margin remain.  Walking
    backwards makes the result deterministic and linear in the forecast length.
    """

    battery_capacity_kwh = max(0.0, battery_capacity_kwh)
    uncertainty_margin_kwh = max(0.0, uncertainty_margin_kwh)
    house_load_margin_fraction = max(0.0, house_load_margin_fraction)
    pv_confidence_factor = max(0.0, min(1.0, pv_confidence_factor))
    discharge_efficiency = max(1e-9, min(1.0, discharge_efficiency))
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
        elif slot.protect_deficit:
            deficit_until_solar_kwh += (
                (protected_house_power_w - protected_pv_power_w)
                * duration_hours
                / 1000
                / discharge_efficiency
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


def get_policy_bounded_export_budget(
    physically_available_energy_kwh: float,
    spendable_energy_after_ev_kwh: float,
) -> float:
    """Cap optional export by both physical headroom and the policy budget.

    The policy budget is the energy left after all protected loads, including a
    pending EV obligation.  Neither input may authorize energy the other one
    has already reserved.
    """

    return max(
        0.0,
        min(
            physically_available_energy_kwh,
            spendable_energy_after_ev_kwh,
        ),
    )


def calculate_export_energy_budget(
    physically_available_energy_kwh: float,
    spendable_energy_before_ev_kwh: float,
    spendable_energy_after_ev_kwh: float,
) -> ExportEnergyBudget:
    """Return the quantitative EV reservation and remaining export budget.

    ``spendable_energy_after_ev_kwh`` already includes the forecast EV drive
    and charging obligation.  Treating an active drive as a second boolean
    hold would reserve the whole battery and discard that quantitative result.
    """

    spendable_before_ev_kwh = max(0.0, float(spendable_energy_before_ev_kwh))
    spendable_after_ev_kwh = max(0.0, float(spendable_energy_after_ev_kwh))
    return ExportEnergyBudget(
        safe_export_energy_kwh=get_policy_bounded_export_budget(
            physically_available_energy_kwh=physically_available_energy_kwh,
            spendable_energy_after_ev_kwh=spendable_after_ev_kwh,
        ),
        ev_reservation_energy_kwh=max(
            0.0,
            spendable_before_ev_kwh - spendable_after_ev_kwh,
        ),
    )


def first_uncovered_departure_need(
    detail_entries,
    schedule_events,
    current_time,
    horizon_hours,
    default_required_soc,
    ev_capacity_kwh,
) -> tuple[float, datetime | None]:
    """Return the nearest forecast-uncovered EV departure need in the horizon.

    Each future departure is measured against the simulated EV energy just
    before it starts, so energy the forecast already plans to charge is not
    reserved twice.  Only the first uncovered departure counts: later needs
    are re-evaluated on later cycles with fresher forecasts, while the
    nearest binding need must be held now.  Ongoing and past departures and
    anything beyond the horizon return no reservation.
    """

    try:
        horizon_hours = float(horizon_hours)
        default_required_soc = float(default_required_soc)
        ev_capacity_kwh = float(ev_capacity_kwh)
    except (TypeError, ValueError, OverflowError):
        return 0.0, None
    if (
        not isfinite(horizon_hours)
        or not isfinite(default_required_soc)
        or not isfinite(ev_capacity_kwh)
        or horizon_hours <= 0
        or ev_capacity_kwh <= 0
    ):
        return 0.0, None
    try:
        upcoming = sorted(
            [
                event
                for event in (schedule_events or [])
                if event is not None
                and event.start is not None
                and event.start > current_time
                and event.start <= current_time + timedelta(hours=horizon_hours)
            ],
            key=lambda event: event.start,
        )
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0.0, None
    for event in upcoming:
        try:
            required_soc = event.required_soc
            required_soc = float(required_soc if required_soc else default_required_soc)
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        if not isfinite(required_soc):
            continue
        required_energy_kwh = required_soc / 100 * ev_capacity_kwh
        available_energy_kwh = None
        for entry in detail_entries or []:
            try:
                entry_start = entry.period_start
                entry_energy = float(entry.ev_energy)
            except (AttributeError, TypeError, ValueError, OverflowError):
                continue
            if entry_start is not None and entry_start < event.start and isfinite(entry_energy):
                available_energy_kwh = entry_energy
        if available_energy_kwh is None:
            continue
        shortfall_kwh = max(0.0, required_energy_kwh - available_energy_kwh)
        if shortfall_kwh > 0:
            return shortfall_kwh, event.start
    return 0.0, None


def apply_horizon_ev_reservation(
    spendable_energy_kwh: float,
    horizon_shortfall_kwh: float,
    charge_efficiency: float,
) -> float:
    """Reserve forecast-uncovered EV departures from the export budget.

    Energy the simulation cannot prove spendable must not be sold before the
    departures that need it.  The shortfall is EV-battery energy; the reserve
    covers it through charger losses.  Invalid inputs reserve everything.
    """

    try:
        spendable_kwh = float(spendable_energy_kwh)
        shortfall_kwh = float(horizon_shortfall_kwh)
        efficiency = float(charge_efficiency)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not isfinite(spendable_kwh) or not isfinite(shortfall_kwh):
        return 0.0
    if not isfinite(efficiency) or efficiency <= 0 or efficiency > 1:
        return 0.0
    return max(0.0, max(0.0, spendable_kwh) - max(0.0, shortfall_kwh) / efficiency)


def select_finite_grid_target(
    candidate_grid_target_w: float,
    fallback_grid_target_w: float,
) -> float | None:
    """Return a finite actuator target, or a validated finite fallback."""

    try:
        fallback_target = float(fallback_grid_target_w)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isfinite(fallback_target):
        return None

    try:
        candidate_target = float(candidate_grid_target_w)
    except (TypeError, ValueError, OverflowError):
        return fallback_target
    if not isfinite(candidate_target):
        return fallback_target
    return candidate_target


def apply_ev_discharge_guard(
    desired_grid_target_w: float,
    discharge_guard_target_w: float,
    guard_active: bool,
) -> float | None:
    """Clamp an EV-period target so later budgets cannot bypass its guard."""

    try:
        desired_target = float(desired_grid_target_w)
        guard_target = float(discharge_guard_target_w)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isfinite(desired_target) or not isfinite(guard_target):
        return None
    if guard_active:
        return max(desired_target, guard_target)
    return desired_target


def select_ev_discharge_guard_target(
    no_battery_discharge_target_w: float,
    neutral_grid_target_w: float,
    ev_is_charging: bool,
    electricity_price: float,
    min_discharge_price: float,
    battery_energy_kwh: float,
    protected_battery_floor_kwh: float,
    telemetry_valid: bool,
) -> float | None:
    """Allow economical battery self-consumption without authorizing export.

    Optional-export policy must not turn an active EV into forced grid import.
    When the battery is above its protected floor and the import price meets
    the configured discharge threshold, the guarded target may move as far as
    grid-neutral. Natural PV export remains allowed. A transient telemetry gap
    while the EV charges rides through at neutral instead of swinging the
    guard to a live import hold and back every sensor cycle; genuinely
    protective states (battery at/below floor, uneconomical price, no EV
    charging) still fail closed to the no-battery-discharge target, as do
    invalid inputs.
    """

    try:
        no_battery_target = float(no_battery_discharge_target_w)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isfinite(no_battery_target):
        return None

    try:
        neutral_target = float(neutral_grid_target_w)
        import_price = float(electricity_price)
        discharge_price = float(min_discharge_price)
        battery_energy = float(battery_energy_kwh)
        protected_floor = float(protected_battery_floor_kwh)
    except (TypeError, ValueError, OverflowError):
        return no_battery_target

    numeric_values = (
        no_battery_target,
        neutral_target,
        import_price,
        discharge_price,
        battery_energy,
        protected_floor,
    )
    for value in numeric_values:
        if not isfinite(value):
            return no_battery_target
    if (
        not ev_is_charging
        or import_price < discharge_price
        or battery_energy <= protected_floor
    ):
        return no_battery_target

    # NOTE: telemetry_valid deliberately absent above. While the EV charges,
    # the caller clamps the final setpoint to at most neutral, so a stale
    # sample can only briefly hold neutral (the battery covers the
    # difference) instead of pinning a kilowatt-scale import hold until the
    # next fresh sample arrives. Without EV charging, the first branch already
    # failed closed to no_battery_target.
    return min(no_battery_target, neutral_target)


def is_ev_arrival_precharge_active(
    able_to_charge_on_arrival: bool,
    charger_ready: bool,
    ev_plugged_in: bool,
    ev_is_charging: bool,
    ev_energy_kwh: float,
    target_energy_kwh: float,
) -> bool:
    """Return whether a physically present EV still needs arrival charging."""

    vehicle_present = bool(charger_ready or ev_plugged_in or ev_is_charging)
    return bool(
        able_to_charge_on_arrival
        and vehicle_present
        and float(ev_energy_kwh) < float(target_energy_kwh)
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
    max_age_seconds: float = 300.0,
    energy_tolerance_kwh: float = 0.1,
) -> bool:
    """Return whether a published export plan matches the live EV state.

    Optional export is safe only when the scheduler saw the same EV inputs as
    the live controller.  This closes the short transition window after a
    charging-state change without blocking export merely because a connected
    EV still has planned energy demand.

    The default age window covers the 120-second plan cadence plus one missed
    publish tick (e.g. a cycle aborted on a transient stale battery reading),
    so a short planner stall with agreeing EV inputs does not pin the
    setpoint at the neutral target.
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
    except (TypeError, ValueError, OverflowError):
        return False
    if (
        not isfinite(planned_energy_needed_kwh)
        or not isfinite(live_energy_needed_kwh)
        or planned_energy_needed_kwh < 0.0
        or live_energy_needed_kwh < 0.0
    ):
        return False
    if abs(planned_energy_needed_kwh - live_energy_needed_kwh) > max(
        0.0,
        energy_tolerance_kwh,
    ):
        return False

    plan_calculated_at = parse_aware_datetime(plan_calculated_at)
    current_time = parse_aware_datetime(current_time)
    if plan_calculated_at is None or current_time is None:
        return False

    try:
        plan_age_seconds = (current_time - plan_calculated_at).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return False
    return 0.0 <= plan_age_seconds <= max(0.0, max_age_seconds)


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
    not_before: datetime | None = None,
) -> float:
    """Return scheduled export energy in the viable interval before a deadline."""

    total_kwh = 0.0
    for key, allocated_w in allocated_w_by_key.items():
        period_start = period_start_by_key.get(key)
        if (
            period_start is None
            or period_start >= deadline
            or (not_before is not None and period_start < not_before)
        ):
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
    headroom_reset_times: list[datetime] | None = None,
) -> float:
    """Allocate price-ranked energy while honoring every cumulative deadline.

    Returns the largest unmet cumulative requirement at any deadline. An
    allocation made after an early solar peak must not count as headroom that
    was available before that peak. Likewise, an allocation before a mandatory
    battery-recharge interval cannot satisfy a later requirement because that
    recharge may consume the created capacity.
    """

    max_shortfall_kwh = 0.0
    cumulative_target_kwh = 0.0
    headroom_reset_times = list(headroom_reset_times or [])
    for deadline, required_kwh in sorted(requirements):
        not_before = None
        for reset_time in headroom_reset_times:
            if reset_time <= deadline and (
                not_before is None or reset_time > not_before
            ):
                not_before = reset_time
        cumulative_target_kwh = max(cumulative_target_kwh, max(0.0, required_kwh))
        allocated_before_kwh = _allocated_energy_before_deadline(
            allocated_w_by_key,
            duration_by_key,
            period_start_by_key,
            deadline,
            not_before=not_before,
        )
        missing_kwh = max(0.0, cumulative_target_kwh - allocated_before_kwh)
        remaining_budget_kwh = max(
            0.0,
            energy_budget_kwh - _allocated_energy_total(allocated_w_by_key, duration_by_key),
        )
        requested_kwh = min(missing_kwh, remaining_budget_kwh)
        eligible_bands = []
        for band in ranked_bands:
            if band[1] < deadline and (
                not_before is None or band[1] >= not_before
            ):
                eligible_bands.append(band)
        _allocate_bands(eligible_bands, requested_kwh, allocated_w_by_key)
        allocated_before_kwh = _allocated_energy_before_deadline(
            allocated_w_by_key,
            duration_by_key,
            period_start_by_key,
            deadline,
            not_before=not_before,
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
    min_export_price_per_kwh: float = 0.0,
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
    the protected battery energy budget. Slots below the export price floor
    are skipped: forcing feed-in at junk prices while the battery could hold
    (or physics spills anyway) only manufactures dip-draining.
    """

    max_grid_export_w = max(0.0, max_grid_export_w)
    try:
        floor_price = float(min_export_price_per_kwh)
    except (TypeError, ValueError, OverflowError):
        floor_price = 0.0
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
                or slot.price_per_kwh < floor_price
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


def build_headroom_storage_hold_schedule(
    slots: list[ExportSchedulerSlot],
    headroom_power_delta_schedule: dict[str, float],
    headroom_requirements: list[tuple[datetime, float]],
    max_grid_export_w: float,
    neutral_grid_target_w: float,
    max_battery_charge_w: float | None = None,
    min_export_price_per_kwh: float = 0.0,
    current_battery_energy_kwh: float | None = None,
    battery_capacity_kwh: float | None = None,
    battery_charge_efficiency: float = 1.0,
    battery_discharge_efficiency: float = 1.0,
) -> dict[str, float]:
    """Preserve price-ranked headroom until each cumulative deadline.

    Once a selected slot creates headroom, later forecast PV must not refill it
    before the requirement that justified the discharge.  Each requirement
    sets an energy ceiling (capacity minus required room, capped at the
    room actually created); the hold simulates the trajectory forward and
    routes only the charging that would breach the ceiling, so a battery
    with room to spare keeps absorbing toward the 100% top-up.  Each
    requirement's hold is confined to its deadline's own day: earlier days
    only simulate the trajectory, since the intervening discharge (evening
    export, overnight load) usually creates the room on its own and
    freezing today's top-up for tomorrow's peak would sacrifice the 100%
    balancing charge for nothing.  Wherever neither the slot nor any
    later slot before the deadline can breach the hard cap, the ceiling
    lifts and soft shaping yields to the top-up; a hard breach at or
    after the slot keeps it binding.  Surplus beyond charge power is
    always routed.  The export price floor skips base holds at junk
    prices, but never ceiling preservation at non-negative prices;
    negative prices still skip.  Without battery state the ceilings are
    unbounded and the hold degrades exactly to the beyond-charge-power
    behavior.
    """

    max_grid_export_w = max(0.0, float(max_grid_export_w))
    try:
        floor_price = float(min_export_price_per_kwh)
    except (TypeError, ValueError, OverflowError):
        floor_price = 0.0
    if max_battery_charge_w is None:
        absorbable_charge_w = 0.0
    else:
        try:
            absorbable_charge_w = max(0.0, float(max_battery_charge_w))
        except (TypeError, ValueError, OverflowError):
            absorbable_charge_w = 0.0
    try:
        charge_efficiency = min(1.0, max(0.0, float(battery_charge_efficiency)))
    except (TypeError, ValueError, OverflowError):
        charge_efficiency = 1.0
    try:
        discharge_efficiency = min(1.0, max(0.0, float(battery_discharge_efficiency)))
    except (TypeError, ValueError, OverflowError):
        discharge_efficiency = 1.0
    if discharge_efficiency <= 0.0:
        discharge_efficiency = 1.0
    try:
        start_energy_kwh = float(current_battery_energy_kwh)
    except (TypeError, ValueError, OverflowError):
        start_energy_kwh = None
    try:
        capacity_kwh = float(battery_capacity_kwh)
    except (TypeError, ValueError, OverflowError):
        capacity_kwh = None
    if capacity_kwh is not None and capacity_kwh <= 0.0:
        capacity_kwh = None
    has_battery_state = start_energy_kwh is not None and capacity_kwh is not None
    ordered_slots = sorted(slots, key=lambda item: item.period_start)
    schedule = {}
    for deadline, required_kwh in sorted(headroom_requirements):
        if required_kwh <= 0:
            continue
        try:
            deadline_day_start = deadline.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        except (AttributeError, TypeError, ValueError):
            deadline_day_start = None
        first_allocation_start = None
        created_kwh = 0.0
        for slot in ordered_slots:
            key = slot.period_start.isoformat()
            if slot.period_start >= deadline:
                continue
            allocated_w = headroom_power_delta_schedule.get(key, 0.0)
            if allocated_w <= 0:
                continue
            created_kwh += (
                allocated_w * max(0.0, slot.duration_hours) / 1000
            )
            if (
                first_allocation_start is None
                or slot.period_start < first_allocation_start
            ):
                first_allocation_start = slot.period_start
        if first_allocation_start is None:
            continue
        # Only created room is protected: without an allocation there is
        # nothing to preserve, and capping short of the requirement keeps
        # the ceiling honest when allocation fell short.
        effective_required_kwh = min(max(0.0, required_kwh), max(0.0, created_kwh))
        if has_battery_state:
            ceiling_kwh = capacity_kwh - effective_required_kwh
            energy_kwh = min(capacity_kwh, max(0.0, start_energy_kwh))
        else:
            ceiling_kwh = float("inf")
            energy_kwh = 0.0
        # A hard breach at or after this slot keeps every ceiling binding;
        # without one the top-up wins over soft shaping, since holding room
        # for an already-passed (or soft-only) peak must never cost the
        # 100% balancing charge.
        slot_count = len(ordered_slots)
        hard_breach_window = []
        for _slot in ordered_slots:
            hard_breach_window.append(False)
        later_breach = False
        for reverse_index in range(slot_count - 1, -1, -1):
            reverse_slot = ordered_slots[reverse_index]
            if reverse_slot.period_start >= deadline:
                hard_breach_window[reverse_index] = False
                continue
            reverse_baseline = (
                reverse_slot.intentional_grid_import_w
                - reverse_slot.baseline_grid_export_w
            )
            reverse_natural = reverse_baseline - reverse_slot.baseline_battery_power_w
            if max(0.0, -reverse_natural) > max_grid_export_w:
                later_breach = True
            hard_breach_window[reverse_index] = later_breach

        for slot_index in range(slot_count):
            slot = ordered_slots[slot_index]
            duration_hours = max(0.0, slot.duration_hours)
            key = slot.period_start.isoformat()
            allocated_w = max(
                0.0, headroom_power_delta_schedule.get(key, 0.0)
            )
            baseline_discharge_w = max(
                0.0, -float(slot.baseline_battery_power_w)
            )
            before_deadline_day = (
                deadline_day_start is not None
                and slot.period_start < deadline_day_start
            )
            if slot.period_start <= first_allocation_start or before_deadline_day:
                energy_kwh += (
                    max(0.0, float(slot.baseline_battery_power_w))
                    * duration_hours
                    / 1000
                    * charge_efficiency
                )
                energy_kwh -= (
                    allocated_w * duration_hours / 1000 / discharge_efficiency
                )
                energy_kwh -= (
                    baseline_discharge_w * duration_hours / 1000 / discharge_efficiency
                )
                if has_battery_state:
                    energy_kwh = min(capacity_kwh, max(0.0, energy_kwh))
                continue
            if slot.period_start >= deadline:
                continue
            if has_battery_state and not hard_breach_window[slot_index]:
                effective_ceiling_kwh = float("inf")
            else:
                effective_ceiling_kwh = ceiling_kwh
            signed_baseline_grid_w = (
                slot.intentional_grid_import_w - slot.baseline_grid_export_w
            )
            natural_grid_w = signed_baseline_grid_w - slot.baseline_battery_power_w
            surplus_power_w = max(0.0, -natural_grid_w)
            # Baseline holds a slight export trickle; only surplus above it
            # is chargeable, so capped charging leaves baseline grid flow
            # otherwise untouched.
            surplus_chargeable_w = max(
                0.0,
                surplus_power_w + min(0.0, float(neutral_grid_target_w)),
            )
            if duration_hours <= 0:
                room_headroom_w = float("inf")
            elif has_battery_state:
                room_headroom_w = (
                    max(0.0, effective_ceiling_kwh - energy_kwh)
                    / duration_hours
                    * 1000
                )
            else:
                room_headroom_w = float("inf")
            # Charging is allowed up to the charge limit, the remaining
            # room under the ceiling, and the actual surplus; the rest
            # routes to the grid.  Without battery state the ceiling is
            # unbounded and this degrades exactly to beyond-charge-power
            # routing.
            charge_allowed_w = min(
                absorbable_charge_w, room_headroom_w, surplus_chargeable_w
            )
            ceiling_binds = room_headroom_w < min(
                absorbable_charge_w, surplus_chargeable_w
            ) - 1e-9
            if (
                slot.duration_hours <= 0
                or slot.ev_charge_power_w > 1
                or slot.intentional_grid_import_w > 1
            ):
                energy_kwh += (
                    max(0.0, float(slot.baseline_battery_power_w))
                    * duration_hours
                    / 1000
                    * charge_efficiency
                )
            elif ceiling_binds and slot.price_per_kwh < 0:
                energy_kwh += (
                    max(0.0, float(slot.baseline_battery_power_w))
                    * duration_hours
                    / 1000
                    * charge_efficiency
                )
            elif not ceiling_binds and slot.price_per_kwh < floor_price:
                energy_kwh += (
                    max(0.0, float(slot.baseline_battery_power_w))
                    * duration_hours
                    / 1000
                    * charge_efficiency
                )
            else:
                energy_kwh += (
                    charge_allowed_w * duration_hours / 1000 * charge_efficiency
                )
                export_goal_w = min(
                    float(neutral_grid_target_w),
                    natural_grid_w + charge_allowed_w,
                )
                desired_grid_target_w = max(
                    -max_grid_export_w,
                    export_goal_w,
                )
                hold_power_w = max(
                    0.0,
                    float(neutral_grid_target_w) - desired_grid_target_w,
                )
                if hold_power_w > 0:
                    schedule[key] = max(schedule.get(key, 0.0), hold_power_w)
            energy_kwh -= allocated_w * duration_hours / 1000 / discharge_efficiency
            energy_kwh -= (
                baseline_discharge_w * duration_hours / 1000 / discharge_efficiency
            )
            if has_battery_state:
                energy_kwh = min(capacity_kwh, max(0.0, energy_kwh))

    return schedule


def _day_energy_trajectory(
    day_slots: list[ExportSchedulerSlot],
    current_kwh: float,
    capacity_kwh: float,
    charge_efficiency: float = 1.0,
    discharge_efficiency: float = 1.0,
    allocated_w_by_key: dict[str, float] | None = None,
    pv_confidence_factor: float = 1.0,
) -> tuple[bool, float]:
    """Simulate one day's battery energy from its baseline powers.

    Returns whether the trajectory touches full and the closing energy.
    Charging applies the charge efficiency, discharging the discharge
    efficiency (baseline powers are AC-side, matching the forecast).
    Optional export deltas drain on top of the baseline when given.
    ``pv_confidence_factor`` discounts charging legs: baseline charging is
    PV-driven, so an undiscounted trajectory trusts the forecast exactly
    and any shortfall lands below full (observed September 2026: ~20%
    shortfall ended the day at ~91% instead of 100%).
    """

    charge_efficiency = min(1.0, max(0.0, charge_efficiency))
    discharge_efficiency = min(1.0, max(0.0, discharge_efficiency))
    pv_confidence_factor = min(1.0, max(0.0, pv_confidence_factor))
    energy_kwh = min(capacity_kwh, max(0.0, current_kwh))
    touches_full = energy_kwh >= capacity_kwh - 1e-9
    for slot in day_slots:
        duration_hours = max(0.0, slot.duration_hours)
        net_kw = float(slot.baseline_battery_power_w)
        if allocated_w_by_key is not None:
            net_kw -= allocated_w_by_key.get(slot.period_start.isoformat(), 0.0)
        if net_kw >= 0.0:
            energy_kwh += net_kw * duration_hours / 1000.0 * charge_efficiency * pv_confidence_factor
        elif discharge_efficiency > 0.0:
            energy_kwh += net_kw * duration_hours / 1000.0 / discharge_efficiency
        energy_kwh = min(capacity_kwh, max(0.0, energy_kwh))
        if energy_kwh >= capacity_kwh - 1e-9:
            touches_full = True
    return touches_full, energy_kwh


def _day_surplus_cutoff_index(
    day_slots: list[ExportSchedulerSlot],
    baseline_spill_threshold_w: float = 1.0,
) -> int:
    """Return the last index with PV surplus, or -1 when there is none."""

    cutoff_index = -1
    for index, slot in enumerate(day_slots):
        if (
            slot.baseline_grid_export_w > baseline_spill_threshold_w
            or slot.baseline_battery_power_w > 1.0
        ):
            cutoff_index = index
    return cutoff_index


def _apply_end_of_day_top_up_clamp(
    slots: list[ExportSchedulerSlot],
    allocated_w_by_key: dict[str, float],
    headroom_w_by_key: dict[str, float],
    current_battery_energy_kwh: float,
    battery_capacity_kwh: float,
    baseline_spill_threshold_w: float = 1.0,
    charge_efficiency: float = 1.0,
    discharge_efficiency: float = 1.0,
    pv_confidence_factor: float = 1.0,
) -> None:
    """Trim late economic export so each sunny day can still end at 100%.

    Simulates the baseline battery trajectory per day up to its last
    PV-surplus interval, with and without the allocated optional export.
    When the baseline alone reaches full, economic (arbitrage) deltas are
    reduced latest-first until the day ends full again. Headroom deltas
    protect physical overflow and are never trimmed. Days the sun cannot
    fill are left untouched. The fill gate itself uses best-estimate
    physics; only the deficit sizing discounts charging legs by
    ``pv_confidence_factor`` so the top-up survives forecast shortfall.
    """

    try:
        current_kwh = float(current_battery_energy_kwh)
        capacity_kwh = float(battery_capacity_kwh)
    except (TypeError, ValueError, OverflowError):
        return
    if not isfinite(current_kwh) or not isfinite(capacity_kwh):
        return
    if capacity_kwh <= 0.0 or current_kwh < 0.0 or current_kwh > capacity_kwh:
        return
    slots_by_date: dict = {}
    for slot in slots:
        slots_by_date.setdefault(slot.period_start.date(), []).append(slot)
    for day_slots in slots_by_date.values():
        day_slots.sort(key=lambda item: item.period_start)
        cutoff_index = _day_surplus_cutoff_index(
            day_slots, baseline_spill_threshold_w
        )
        if cutoff_index < 0:
            continue
        window = day_slots[: cutoff_index + 1]
        # The fill gate uses best-estimate physics: it decides whether the
        # sun can fill at all.  Only the deficit below is discounted.  A
        # discounted gate would misclassify fill days as hopeless and drop
        # their (physical, hard-cap) headroom requirements entirely.
        touches_full, _ = _day_energy_trajectory(
            window,
            current_kwh,
            capacity_kwh,
            charge_efficiency,
            discharge_efficiency,
        )
        if not touches_full:
            continue
        _, planned_kwh = _day_energy_trajectory(
            window,
            current_kwh,
            capacity_kwh,
            charge_efficiency,
            discharge_efficiency,
            allocated_w_by_key,
            pv_confidence_factor,
        )
        try:
            store_efficiency = min(1.0, max(0.0, float(charge_efficiency)))
        except (TypeError, ValueError, OverflowError):
            store_efficiency = 1.0
        deficit_kwh = capacity_kwh - planned_kwh
        for slot in reversed(window):
            if deficit_kwh <= 1e-9:
                break
            key = slot.period_start.isoformat()
            duration_hours = max(0.0, slot.duration_hours)
            if duration_hours <= 0.0 or store_efficiency <= 0.0:
                continue
            economic_w = max(
                0.0,
                allocated_w_by_key.get(key, 0.0) - headroom_w_by_key.get(key, 0.0),
            )
            if economic_w <= 0.0:
                continue
            trim_w = min(
                economic_w,
                deficit_kwh * 1000.0 / duration_hours / store_efficiency,
            )
            allocated_w_by_key[key] = allocated_w_by_key.get(key, 0.0) - trim_w
            deficit_kwh -= trim_w * duration_hours / 1000.0 * store_efficiency


def requirement_day_start_times(
    requirements: list[tuple[datetime, float]],
) -> list[datetime]:
    """Midnight at the start of each requirement deadline's date.

    Used as first-pass allocator reset times, these confine the
    same-day share of each requirement to its own day — where the
    forecast is freshest — while leaving same-day requirements (midnight
    already past) untouched.  The second pass tops up to the full
    cumulative target from any band before the deadline, so the
    night-before share chases the best price and stranded same-day
    remainder still finds evening room, keeping noon-full avoidable.
    """

    day_starts = []
    for requirement in requirements:
        try:
            deadline, _required_kwh = requirement
            midnight = deadline.replace(hour=0, minute=0, second=0, microsecond=0)
        except (AttributeError, TypeError, ValueError):
            continue
        if midnight not in day_starts:
            day_starts.append(midnight)
    return day_starts


def compute_room_overflow_requirement(
    entries: list[tuple[datetime, float, float, float]],
    battery_capacity_kwh: float,
    spill_target_w: float,
    pv_discount: float,
    current_date,
) -> tuple[datetime, float] | None:
    """Size today's pre-release so peak spill stays at the feed-in target.

    Each entry is ``(period_start, battery_energy_kwh, pv_feedin_w,
    duration_hours)`` from the neutral baseline trajectory.  Once the
    baseline pins at full, later PV surplus must spill, so releasing that
    volume early ends the day just as full: no energy is rebought, the same
    sun refills what was sold.  Future PV is discounted before measuring
    the spill, which keeps the release at or below the real spill and is
    what prevents over-release.  Fills on other days need no release today
    since tonight's discharge reopens room on its own.
    """

    try:
        capacity_kwh = float(battery_capacity_kwh)
        target_w = max(0.0, float(spill_target_w))
        discount = min(1.0, max(0.0, float(pv_discount)))
    except (TypeError, ValueError, OverflowError):
        return None
    if capacity_kwh <= 0.0 or discount <= 0.0:
        return None
    fill_index = None
    for index, entry in enumerate(entries):
        try:
            period_start, battery_energy_kwh, _pv_feedin_w, _duration_hours = entry
            pinned = float(battery_energy_kwh) >= capacity_kwh - 1e-3
            same_day = period_start.date() == current_date
        except (TypeError, ValueError, OverflowError, AttributeError):
            continue
        if not same_day:
            continue
        if pinned and fill_index is None:
            fill_index = index
    if fill_index is None or fill_index == 0:
        return None
    try:
        fill_start, _fill_energy_kwh, _fill_feedin_w, fill_duration_hours = entries[
            fill_index - 1
        ]
        deadline = fill_start + timedelta(hours=max(0.0, float(fill_duration_hours)))
    except (TypeError, ValueError, OverflowError, AttributeError, IndexError):
        return None
    spill_kwh = 0.0
    for entry in entries[fill_index:]:
        try:
            period_start, _battery_energy_kwh, pv_feedin_w, duration_hours = entry
            if period_start.date() != current_date:
                continue
            spill_kwh += (
                max(0.0, float(pv_feedin_w) * discount - target_w)
                * max(0.0, float(duration_hours))
                / 1000.0
            )
        except (TypeError, ValueError, OverflowError, AttributeError):
            continue
    if spill_kwh <= 0.0:
        return None
    return deadline, spill_kwh


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
    neutral_grid_target_w: float = -100.0,
    max_battery_charge_w: float | None = None,
    current_battery_energy_kwh: float | None = None,
    battery_capacity_kwh: float | None = None,
    battery_charge_efficiency: float = 1.0,
    battery_discharge_efficiency: float = 1.0,
    top_up_pv_confidence_factor: float = 1.0,
    allow_economic_export: bool = True,
) -> ExportSchedulePlan:
    """Build one price-ranked schedule used by forecast and live control.

    During quiet hours the configured tolerance is split symmetrically around
    the actual price: the efficient band receives a preference and the louder
    boost band a penalty.  The first hour after the quiet window keeps the
    efficient/boost split without the quiet-hour bonus.  This one-hour grace
    avoids a hard power discontinuity at midnight while a materially better
    later price can still win. Hard combined-grid-cap requirements are
    physical constraints and therefore outrank export value; they are
    allocated first across all bands. Preferred PV-feed-in requirements
    consume only the remaining safe battery budget, price-ranked into any
    slot before their deadline (pre-release shaping obeys the configured
    feed-in target; the discharge floor gates profit-seeking arbitrage
    and forced holds, not shaping). Within each deadline price ranking still
    maximizes export value.

    EV-charging and intentional grid-import intervals stay at their neutral
    baseline target.  Combined forecast export is capped at
    ``max_grid_export_w``; unavoidable PV spill remains a physical exception.
    The storage hold routes only surplus beyond battery charge power, so
    chargeable energy is stored before anything is forced to the grid.
    The hold simulates the final schedule including economic export, so its
    room ceiling only binds where the executed plan would actually breach it.
    Economic export requires baseline spill, except on days the baseline
    fills the battery anyway: then the top-up is safe by construction, so
    any slot above the discharge price floor is eligible, including the
    high-priced morning and evening peaks (the price floor and the energy
    budget guard those; quiet-hour scoring still favors efficient power at
    night).  On days the sun cannot fill, only true spill intervals are
    eligible, so arbitrage can never drain a top-up that will not recover.
    When the current battery energy and capacity are known, late
    economic export is additionally trimmed back until the simulated day
    ends full again.  Headroom deadlines are physical overflow protection
    and stay exempt on days the baseline fills; on days it cannot, their
    requirements are dropped because pre-releasing would only drain the
    top-up (true excess beyond charge power still flows via the hold
    schedules).  ``top_up_pv_confidence_factor`` discounts refill charging
    when sizing the clamp trim so the top-up survives forecast shortfall;
    the fill classification itself stays best-estimate so fill days keep
    their headroom requirements.  ``allow_economic_export`` disables the
    arbitrage leg entirely (cell-balancing chase mode); headroom and holds
    are unaffected.
    """

    explicit_headroom_requirements = headroom_requirements is not None
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
    try:
        trickle_export_w = max(0.0, -float(neutral_grid_target_w))
    except (TypeError, ValueError, OverflowError):
        trickle_export_w = 0.0
    # The neutral replay holds its own (usually slightly exporting) target,
    # so only feed-in beyond that commanded trickle counts as spill the
    # battery cannot absorb.
    spill_threshold_w = trickle_export_w + 1.0
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
    headroom_reset_times = []
    # Whether each day's baseline fills the battery.  Needed below so
    # arbitrage can use high-price slots (morning/evening peaks) on days
    # the top-up is safe, instead of being confined to spill intervals.
    try:
        touch_current_kwh = float(current_battery_energy_kwh)
        touch_capacity_kwh = float(battery_capacity_kwh)
        touch_known = (
            current_battery_energy_kwh is not None
            and battery_capacity_kwh is not None
            and isfinite(touch_current_kwh)
            and isfinite(touch_capacity_kwh)
            and touch_capacity_kwh > 0.0
            and 0.0 <= touch_current_kwh <= touch_capacity_kwh
        )
    except (TypeError, ValueError, OverflowError):
        touch_known = False
    touch_by_date: dict = {}
    if touch_known:
        for slot in slots:
            touch_by_date.setdefault(slot.period_start.date(), []).append(slot)
        for day_slots in touch_by_date.values():
            day_slots.sort(key=lambda item: item.period_start)
            cutoff_index = _day_surplus_cutoff_index(
                day_slots, spill_threshold_w
            )
            if cutoff_index < 0:
                touch_by_date[day_slots[0].period_start.date()] = False
                continue
            # Best-estimate fill check: eligibility and requirement
            # keep/drop follow physical possibility, not the haircut.  The
            # haircut applies only when sizing the trim below.
            touches_full, _ = _day_energy_trajectory(
                day_slots[: cutoff_index + 1],
                touch_current_kwh,
                touch_capacity_kwh,
                battery_charge_efficiency,
                battery_discharge_efficiency,
            )
            touch_by_date[day_slots[0].period_start.date()] = touches_full

    for slot in slots:
        key = slot.period_start.isoformat()
        duration_by_key[key] = max(0.0, slot.duration_hours)
        period_start_by_key[key] = slot.period_start
        grid_target_schedule[key] = max(-max_grid_export_w, min(0.0, slot.baseline_setpoint_w))
        battery_power_delta_schedule[key] = 0.0

        if slot.baseline_battery_power_w > 1 and (
            slot.ev_charge_power_w > 1 or slot.intentional_grid_import_w > 1
        ):
            headroom_reset_times.append(
                slot.period_start
                + timedelta(hours=max(0.0, slot.duration_hours))
            )

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
            # Spill in the neutral baseline means the battery cannot absorb
            # more right now, so exporting diverts nothing from the top-up.
            # On days the baseline fills anyway, the top-up is safe by
            # construction, so high-price slots (morning/evening peaks) may
            # also sell: the price floor and the energy budget, not the
            # spill, are the guards there.  On days the sun cannot fill,
            # only true spill is eligible.
            try:
                day_fills = bool(touch_by_date.get(slot.period_start.date(), False))
            except (AttributeError, TypeError, ValueError):
                day_fills = False
            if slot.price_per_kwh >= min_discharge_price and (
                slot.baseline_grid_export_w > spill_threshold_w or day_fills
            ):
                economic_bands.append(band)

    # First pass locks the same-day share of each requirement, where the
    # forecast is freshest.  The second pass below tops up to the full
    # cumulative target from any band before the deadline, so the
    # night-before share chases the best price while same-day keeps the
    # error margin (and stranded same-day remainder still finds room in
    # the evening, keeping noon-full avoidable).
    confinement_reset_times = requirement_day_start_times(
        list(hard_headroom_requirements) + list(headroom_requirements)
    )
    physical_reset_count = len(headroom_reset_times)
    for reset_time in confinement_reset_times:
        headroom_reset_times.append(reset_time)
    headroom_bands.sort()
    economic_bands.sort()
    allocated_w_by_key = {}

    # Days the baseline cannot fill keep every kWh: headroom pre-release
    # there would drain the top-up for no unavoidable spill (true excess
    # beyond charge power still flows via the hold schedules below).
    # touch_by_date was computed above before the band loop.
    if touch_known:
        # NOTE: plain loops, no nested closures here: this module also runs
        # under pyscript's AST evaluator, which does not resolve closures
        # over enclosing locals.
        kept_headroom_requirements = []
        for requirement in headroom_requirements:
            try:
                deadline_date = requirement[0].date()
            except (AttributeError, TypeError, ValueError):
                kept_headroom_requirements.append(requirement)
                continue
            if (
                deadline_date in touch_by_date
                and not touch_by_date[deadline_date]
            ):
                continue
            kept_headroom_requirements.append(requirement)
        headroom_requirements = kept_headroom_requirements
        kept_hard_headroom_requirements = []
        for requirement in hard_headroom_requirements:
            try:
                deadline_date = requirement[0].date()
            except (AttributeError, TypeError, ValueError):
                kept_hard_headroom_requirements.append(requirement)
                continue
            if (
                deadline_date in touch_by_date
                and not touch_by_date[deadline_date]
            ):
                continue
            kept_hard_headroom_requirements.append(requirement)
        hard_headroom_requirements = kept_hard_headroom_requirements

    if not explicit_headroom_requirements and headroom_deadline is not None:
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
    # First pass locks the same-day share; the second pass tops up to
    # the full cumulative target from any band before the deadline, so up
    # to NIGHT_BEFORE_HEADROOM_SHARE chases the best price (often the
    # evening peak) while same-day keeps the error margin.  Physical
    # recharge resets still apply in both passes; only the same-day
    # confinement is lifted in the second.
    same_day_share = max(0.0, min(1.0, 1.0 - NIGHT_BEFORE_HEADROOM_SHARE))
    first_pass_hard = []
    for deadline, target_kwh in hard_headroom_requirements:
        first_pass_hard.append((deadline, target_kwh * same_day_share))
    first_pass_preferred = []
    for deadline, target_kwh in headroom_requirements:
        first_pass_preferred.append((deadline, target_kwh * same_day_share))
    physical_reset_times = headroom_reset_times[:physical_reset_count]
    hard_headroom_shortfall_kwh = _allocate_cumulative_requirements(
        first_pass_hard,
        hard_headroom_bands,
        allocated_w_by_key,
        duration_by_key,
        period_start_by_key,
        available_kwh,
        headroom_reset_times=headroom_reset_times,
    )
    hard_headroom_shortfall_kwh = _allocate_cumulative_requirements(
        hard_headroom_requirements,
        hard_headroom_bands,
        allocated_w_by_key,
        duration_by_key,
        period_start_by_key,
        available_kwh,
        headroom_reset_times=physical_reset_times,
    )
    preferred_headroom_shortfall_kwh = _allocate_cumulative_requirements(
        first_pass_preferred,
        headroom_bands,
        allocated_w_by_key,
        duration_by_key,
        period_start_by_key,
        available_kwh,
        headroom_reset_times=headroom_reset_times,
    )
    preferred_headroom_shortfall_kwh = _allocate_cumulative_requirements(
        headroom_requirements,
        headroom_bands,
        allocated_w_by_key,
        duration_by_key,
        period_start_by_key,
        available_kwh,
        headroom_reset_times=physical_reset_times,
    )
    allocated_headroom_kwh = _allocated_energy_total(
        allocated_w_by_key,
        duration_by_key,
    )
    remaining_budget_kwh = max(0.0, available_kwh - allocated_headroom_kwh)
    headroom_allocated_w_by_key = dict(allocated_w_by_key)
    if allow_economic_export:
        _allocate_bands(
            economic_bands,
            remaining_budget_kwh,
            allocated_w_by_key,
        )
    if (
        current_battery_energy_kwh is not None
        and battery_capacity_kwh is not None
    ):
        _apply_end_of_day_top_up_clamp(
            slots,
            allocated_w_by_key,
            headroom_allocated_w_by_key,
            current_battery_energy_kwh,
            battery_capacity_kwh,
            spill_threshold_w,
            battery_charge_efficiency,
            battery_discharge_efficiency,
            top_up_pv_confidence_factor,
        )
    # The hold simulates the trajectory it protects, so it runs last on the
    # final schedule: headroom-only deltas would blind it to planned economic
    # discharge and pin charging against phantom energy (observed 2026-09-26:
    # 5 kW midday export with twice the required room already free while the
    # battery sat at 50% instead of filling for the EV arrival).
    headroom_hold_schedule = build_headroom_storage_hold_schedule(
        slots,
        dict(allocated_w_by_key),
        list(hard_headroom_requirements) + list(headroom_requirements),
        max_grid_export_w=max_grid_export_w,
        neutral_grid_target_w=neutral_grid_target_w,
        max_battery_charge_w=max_battery_charge_w,
        min_export_price_per_kwh=min_discharge_price,
        current_battery_energy_kwh=current_battery_energy_kwh,
        battery_capacity_kwh=battery_capacity_kwh,
        battery_charge_efficiency=battery_charge_efficiency,
        battery_discharge_efficiency=battery_discharge_efficiency,
    )
    allocated_economic_kwh = max(
        0.0,
        _allocated_energy_total(allocated_w_by_key, duration_by_key)
        - allocated_headroom_kwh,
    )

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
        headroom_hold_schedule=headroom_hold_schedule,
        allocated_energy_kwh=allocated_headroom_kwh + allocated_economic_kwh,
        headroom_energy_kwh=allocated_headroom_kwh,
        economic_energy_kwh=allocated_economic_kwh,
        unallocated_headroom_kwh=max(0.0, preferred_headroom_shortfall_kwh),
        hard_headroom_required_kwh=hard_headroom_required_kwh,
        hard_headroom_shortfall_kwh=hard_headroom_shortfall_kwh,
        headroom_power_schedule={
            key: min(headroom_w, allocated_w_by_key.get(key, 0.0))
            for key, headroom_w in headroom_allocated_w_by_key.items()
        },
    )


def adapt_grid_target_to_live_power(
    planned_battery_power_w: float,
    planned_optional_export_power_w: float,
    house_load_w: float,
    pv_power_w: float,
    max_grid_export_w: float,
    neutral_grid_target_w: float,
    ev_is_charging: bool = False,
    hold_optional_export: bool = False,
) -> float | None:
    """Adapt the unified plan to live load and PV measurements.

    Without an optional export request, the neutral grid target lets ESS absorb
    live PV above forecast instead of exporting the forecast error.  While an
    optional export or hard-cap hold is active, rebuilding the target from live
    natural flow preserves the explicitly planned battery trajectory.  The
    export cap and neutral clamp prevent excess export or avoidable import.
    """

    try:
        planned_battery_power_w = float(planned_battery_power_w)
        planned_optional_export_power_w = float(planned_optional_export_power_w)
        house_load_w = float(house_load_w)
        pv_power_w = float(pv_power_w)
        max_grid_export_w = float(max_grid_export_w)
        neutral_grid_target_w = float(neutral_grid_target_w)
    except (TypeError, ValueError, OverflowError):
        return None
    numeric_values = (
        planned_battery_power_w,
        planned_optional_export_power_w,
        house_load_w,
        pv_power_w,
        max_grid_export_w,
        neutral_grid_target_w,
    )
    for value in numeric_values:
        if not isfinite(value):
            return None

    if ev_is_charging or hold_optional_export:
        return neutral_grid_target_w

    if planned_optional_export_power_w <= 0:
        return neutral_grid_target_w

    natural_grid_power_w = house_load_w - pv_power_w
    desired_grid_target_w = natural_grid_power_w + planned_battery_power_w
    return max(
        -max(0.0, max_grid_export_w),
        min(neutral_grid_target_w, desired_grid_target_w),
    )
