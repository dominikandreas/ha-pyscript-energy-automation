"""Pure helpers for stable forecast-driven grid setpoint control."""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite


PV_FEEDIN_PEAK_TOLERANCE_W = 50.0
PV_OVERFLOW_ENERGY_TOLERANCE_KWH = 0.05
MIN_CONTROL_PEAK_EFFECT_W = 50.0
MIN_CONTROL_ENERGY_EFFECT_KWH = 0.02
MAX_TARGET_STEP_W = 500.0


@dataclass(frozen=True)
class FeedinCandidate:
    """Forecast response for one setpoint or spread candidate."""

    control_value: float
    predicted_peak_w: float
    overflow_energy_kwh: float


def calculate_pv_overflow_energy(
    samples: list[tuple[datetime, float]],
    limit_w: float,
    t_now: datetime,
) -> float:
    """Integrate forecast PV feed-in above ``limit_w`` into kWh.

    Forecast entries represent the power from their timestamp until the next
    entry. The first and last periods are clipped to the available future
    horizon. A peak only a few watts above the target therefore produces a
    correspondingly tiny headroom requirement instead of a full-scale command.
    """

    points = sorted(samples, key=lambda sample: sample[0])
    if len(points) < 2:
        return 0.0

    positive_periods = [
        right[0] - left[0]
        for left, right in zip(points, points[1:])
        if right[0] > left[0]
    ]
    if not positive_periods:
        return 0.0
    fallback_period = min(positive_periods)

    overflow_kwh = 0.0
    for index, (period_start, feedin_w) in enumerate(points):
        period_end = points[index + 1][0] if index + 1 < len(points) else period_start + fallback_period
        effective_start = max(period_start, t_now)
        if period_end <= effective_start:
            continue

        duration_hours = (period_end - effective_start).total_seconds() / 3600
        overflow_kwh += max(0.0, feedin_w - limit_w) * duration_hours / 1000

    return overflow_kwh


def feedin_constraint_exceeded(candidate: FeedinCandidate, limit_w: float) -> bool:
    """Return whether a forecast violation is large enough to control."""

    return (
        candidate.predicted_peak_w > limit_w + PV_FEEDIN_PEAK_TOLERANCE_W
        and candidate.overflow_energy_kwh > PV_OVERFLOW_ENERGY_TOLERANCE_KWH
    )


def calculate_energy_bounded_control(
    overflow_energy_kwh: float,
    available_energy_kwh: float,
    hours_to_peak: float,
    min_control_w: float,
    max_control_w: float,
) -> float:
    """Convert required battery headroom into a bounded average export target.

    This is a fallback for forecast models that report the same PV overflow for
    every tested control. It exports no more energy than is both useful for the
    predicted overflow and safely available above the battery floor.
    """

    if min_control_w > max_control_w:
        raise ValueError("min_control_w must not exceed max_control_w")
    if hours_to_peak <= 0:
        return max_control_w

    usable_energy_kwh = calculate_headroom_energy_requirement(
        overflow_energy_kwh,
        available_energy_kwh,
    )
    if usable_energy_kwh <= 0:
        return max_control_w

    desired_control_w = -(usable_energy_kwh * 1000 / hours_to_peak)
    return max(min_control_w, min(max_control_w, desired_control_w))


def calculate_headroom_energy_requirement(
    overflow_energy_kwh: float,
    available_energy_kwh: float,
) -> float:
    """Return useful, safely exportable energy for forecast headroom."""

    useful_overflow_kwh = max(0.0, overflow_energy_kwh - PV_OVERFLOW_ENERGY_TOLERANCE_KWH)
    return min(useful_overflow_kwh, max(0.0, available_energy_kwh))


def build_price_aware_headroom_schedule(
    samples: list[tuple[datetime, float, float]],
    required_energy_kwh: float,
    deadline: datetime,
    t_now: datetime,
    max_export_w: float,
) -> dict[str, float]:
    """Allocate required export energy to the highest-priced intervals.

    Each sample contains ``(period_start, price_per_kwh, baseline_control_w)``.
    Baseline price-mapped export already planned before ``deadline`` counts
    toward the energy requirement. The returned mapping only contains buckets
    that need a stronger target, keyed by ISO period start for safe publication
    as a Home Assistant state attribute.
    """

    if required_energy_kwh <= 0 or max_export_w <= 0 or deadline <= t_now:
        return {}

    points = sorted(samples)
    if not points:
        return {}

    baseline_energy_kwh = 0.0
    ranked_slots = []
    for index, (period_start, price_per_kwh, baseline_control_w) in enumerate(points):
        period_end = points[index + 1][0] if index + 1 < len(points) else deadline
        period_end = min(period_end, deadline)
        effective_start = max(period_start, t_now)
        if period_end <= effective_start:
            continue

        duration_hours = (period_end - effective_start).total_seconds() / 3600
        baseline_export_w = min(max_export_w, max(0.0, -baseline_control_w))
        baseline_energy_kwh += baseline_export_w * duration_hours / 1000
        # Natural tuple ordering gives highest price first and, for a tie,
        # the earlier bucket. Avoid a lambda: Pyscript closure compilation is
        # unreliable for imported helpers.
        ranked_slots.append(
            (-price_per_kwh, period_start, duration_hours, baseline_export_w)
        )

    remaining_kwh = max(0.0, required_energy_kwh - baseline_energy_kwh)
    ranked_slots.sort()
    schedule = {}
    for _negative_price, period_start, duration_hours, baseline_export_w in ranked_slots:
        if remaining_kwh <= 1e-9:
            break
        additional_capacity_kwh = max(0.0, max_export_w - baseline_export_w) * duration_hours / 1000
        additional_energy_kwh = min(remaining_kwh, additional_capacity_kwh)
        if additional_energy_kwh <= 0:
            continue
        scheduled_export_w = baseline_export_w + additional_energy_kwh * 1000 / duration_hours
        schedule[period_start.isoformat()] = -scheduled_export_w
        remaining_kwh -= additional_energy_kwh

    return schedule


def apply_hard_feedin_control(
    price_mapped_control_w: float,
    headroom_control_w: float | None,
    constraint_active: bool,
) -> float:
    """Apply the scheduled headroom target for the current price bucket.

    A missing current-bucket target deliberately leaves the normal price mapper
    in control. Scheduled buckets may export battery energy before sunrise and
    PV energy later; the protected battery floor remains enforced downstream.
    """

    if not constraint_active or headroom_control_w is None:
        return price_mapped_control_w
    return min(price_mapped_control_w, headroom_control_w)


def choose_stable_candidate(
    candidates: list[FeedinCandidate],
    limit_w: float,
    prefer_larger_control: bool,
) -> FeedinCandidate:
    """Choose the least aggressive useful forecast-control candidate.

    The old binary search always selected its final boundary. If all tested
    controls produced effectively the same forecast peak, a tiny violation
    therefore commanded maximum export. This selector explicitly keeps the
    least aggressive candidate when the forecast is already acceptable or the
    modeled actuator response is below the noise floor.
    """

    if not candidates:
        raise ValueError("at least one feed-in candidate is required")

    # Keep this deliberately imperative. Pyscript compiles lambdas and
    # comprehensions into separate functions and cannot reliably close over
    # arguments from this function (eg ``prefer_larger_control``).
    baseline = candidates[0]
    for candidate in candidates[1:]:
        if prefer_larger_control and candidate.control_value > baseline.control_value:
            baseline = candidate
        elif not prefer_larger_control and candidate.control_value < baseline.control_value:
            baseline = candidate
    if not feedin_constraint_exceeded(baseline, limit_w):
        return baseline

    best = candidates[0]
    for candidate in candidates[1:]:
        candidate_is_better = candidate.overflow_energy_kwh < best.overflow_energy_kwh
        if candidate.overflow_energy_kwh == best.overflow_energy_kwh:
            candidate_is_better = candidate.predicted_peak_w < best.predicted_peak_w
            if candidate.predicted_peak_w == best.predicted_peak_w:
                candidate_is_better = (
                    candidate.control_value > best.control_value
                    if prefer_larger_control
                    else candidate.control_value < best.control_value
                )
        if candidate_is_better:
            best = candidate

    peak_effect_w = baseline.predicted_peak_w - best.predicted_peak_w
    energy_effect_kwh = baseline.overflow_energy_kwh - best.overflow_energy_kwh
    if peak_effect_w < MIN_CONTROL_PEAK_EFFECT_W and energy_effect_kwh < MIN_CONTROL_ENERGY_EFFECT_KWH:
        return baseline

    selected_acceptable = None
    for candidate in candidates:
        if feedin_constraint_exceeded(candidate, limit_w):
            continue
        if selected_acceptable is None:
            selected_acceptable = candidate
        elif prefer_larger_control and candidate.control_value > selected_acceptable.control_value:
            selected_acceptable = candidate
        elif not prefer_larger_control and candidate.control_value < selected_acceptable.control_value:
            selected_acceptable = candidate

    if selected_acceptable is not None:
        return selected_acceptable
    return best


def calculate_auto_max_setpoint(
    spendable_surplus_kwh: float,
    horizon_hours: float,
    minimum_bias_w: float = 20.0,
    full_bias_w: float = 100.0,
    rounding_w: float = 10.0,
) -> float:
    """Budget a small export bias over the remaining solar-cycle horizon.

    The bias protects against Victron tracking error without turning the
    neutral setpoint into an unpriced battery-export schedule. The caller
    supplies the conservative surplus after planned EV charging, so the
    maximum possible bias energy is covered by energy that is already
    spendable. Intentional export remains the export scheduler's job.
    """

    if horizon_hours <= 0:
        raise ValueError("horizon_hours must be positive")
    if minimum_bias_w < 0 or full_bias_w < minimum_bias_w:
        raise ValueError("invalid bias limits")
    if rounding_w <= 0:
        raise ValueError("rounding_w must be positive")

    energy_bounded_bias_w = max(0.0, spendable_surplus_kwh) * 1000 / horizon_hours
    bias_w = max(minimum_bias_w, min(full_bias_w, energy_bounded_bias_w))
    rounded_bias_w = round(bias_w / rounding_w) * rounding_w
    return -rounded_bias_w


def limit_target_step(desired_w: float, previous_w: float, max_step_w: float = MAX_TARGET_STEP_W) -> float:
    """Apply a symmetric per-cycle slew limit to a grid setpoint target."""

    if max_step_w <= 0:
        raise ValueError("max_step_w must be positive")
    return max(previous_w - max_step_w, min(previous_w + max_step_w, desired_w))


def build_silent_charge_shaping_schedule(
    surplus_slots: list[tuple[str, float, float, float]],
    silent_power_w: float = 3500.0,
    surplus_margin_w: float = 500.0,
) -> dict[str, float]:
    """Export PV surplus above the silent charge power, with a margin band.

    Each slot is ``(period_key, pv_power_w, house_power_w,
    ev_charge_power_w)``.  Battery charging is capped at ``silent_power_w``
    only once shareable surplus (PV minus house and EV) exceeds the cap plus
    ``surplus_margin_w``; inside the margin band the battery takes everything
    so a few hundred watts are not pointlessly exported.  Returns per-slot
    grid-export power for slots that need shaping.
    """

    try:
        silent_power_w = float(silent_power_w)
        surplus_margin_w = float(surplus_margin_w)
    except (TypeError, ValueError, OverflowError):
        return {}
    if not isfinite(silent_power_w) or not isfinite(surplus_margin_w):
        return {}
    silent_power_w = max(0.0, silent_power_w)
    surplus_margin_w = max(0.0, surplus_margin_w)
    schedule = {}
    for entry in surplus_slots:
        try:
            period_key, pv_power_w, house_power_w, ev_charge_power_w = entry
            shareable_w = (
                float(pv_power_w) - float(house_power_w) - float(ev_charge_power_w)
            )
        except (TypeError, ValueError, OverflowError):
            continue
        if not isfinite(shareable_w):
            continue
        if shareable_w > silent_power_w + surplus_margin_w:
            schedule[str(period_key)] = shareable_w - silent_power_w
    return schedule


def apply_silent_mode_grid_limits(
    desired_grid_target_w: float,
    pv_power_w: float,
    house_load_w: float,
    ev_charge_power_w: float,
    max_grid_export_w: float,
    silent_power_w: float = 3500.0,
    surplus_margin_w: float = 500.0,
) -> float:
    """Clamp a live grid target so battery/grid flows stay quiet.

    Sign convention: a negative target exports.  Charging is capped at
    ``silent_power_w`` once shareable surplus (PV minus house and EV) exceeds
    the cap plus margin, exporting the excess instead.  Battery-to-grid
    discharge is capped at ``silent_power_w`` above PV passthrough, while
    house and EV coverage stay unlimited.  The grid export cap always wins
    over silence.  Invalid inputs leave the desired target untouched.
    """

    try:
        target_w = float(desired_grid_target_w)
        shareable_w = (
            float(pv_power_w) - float(house_load_w) - float(ev_charge_power_w)
        )
        cap_w = float(max_grid_export_w)
        silent_w = float(silent_power_w)
        margin_w = float(surplus_margin_w)
    except (TypeError, ValueError, OverflowError):
        return desired_grid_target_w
    if not (
        isfinite(target_w)
        and isfinite(shareable_w)
        and isfinite(cap_w)
        and isfinite(silent_w)
        and isfinite(margin_w)
    ):
        return desired_grid_target_w
    cap_w = max(0.0, cap_w)
    silent_w = max(0.0, silent_w)
    margin_w = max(0.0, margin_w)
    if shareable_w > silent_w + margin_w:
        target_w = min(target_w, -(shareable_w - silent_w))
    target_w = max(target_w, -(silent_w + max(0.0, shareable_w)))
    return max(target_w, -cap_w)


def resolve_economic_export_floor(
    configured_floor: float,
    feedin_compensation_cap: float,
    future_import_prices: list,
) -> float | None:
    """Gate speculative battery export on realized feed-in profitability.

    The market price ranks spill-driven pre-release slots, but the realized
    feed-in compensation is capped (a fixed tariff until the dynamic export
    contract switch).  Selling stored energy only pays when the realized
    revenue beats the cheapest future rebuy, so economic export is blocked
    (``None``) unless the cap exceeds the minimum future import price; when
    allowed, the floor is the higher of the configured floor and that cheapest
    rebuy.  Invalid inputs fail safe to blocked.  Headroom (unavoidable-spill)
    export is unaffected: it sells at any price rather than curtailing.
    """

    try:
        floor = float(configured_floor)
        cap = float(feedin_compensation_cap)
        prices = [float(price) for price in future_import_prices]
    except (TypeError, ValueError, OverflowError):
        return None
    if not prices or not isfinite(floor) or not isfinite(cap):
        return None
    cheapest_rebuy = None
    for price in prices:
        if not isfinite(price):
            return None
        if cheapest_rebuy is None or price < cheapest_rebuy:
            cheapest_rebuy = price
    if cap <= cheapest_rebuy:
        return None
    return max(floor, cheapest_rebuy)
