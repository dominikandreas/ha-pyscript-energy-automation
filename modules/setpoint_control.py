"""Pure helpers for stable forecast-driven grid setpoint control."""

from dataclasses import dataclass
from datetime import datetime


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

    control_key = (lambda candidate: candidate.control_value)
    baseline = (max if prefer_larger_control else min)(candidates, key=control_key)
    if not feedin_constraint_exceeded(baseline, limit_w):
        return baseline

    best = min(
        candidates,
        key=lambda candidate: (
            candidate.overflow_energy_kwh,
            candidate.predicted_peak_w,
            -candidate.control_value if prefer_larger_control else candidate.control_value,
        ),
    )
    peak_effect_w = baseline.predicted_peak_w - best.predicted_peak_w
    energy_effect_kwh = baseline.overflow_energy_kwh - best.overflow_energy_kwh
    if peak_effect_w < MIN_CONTROL_PEAK_EFFECT_W and energy_effect_kwh < MIN_CONTROL_ENERGY_EFFECT_KWH:
        return baseline

    acceptable = [candidate for candidate in candidates if not feedin_constraint_exceeded(candidate, limit_w)]
    if acceptable:
        return (max if prefer_larger_control else min)(acceptable, key=control_key)
    return best


def limit_target_step(desired_w: float, previous_w: float, max_step_w: float = MAX_TARGET_STEP_W) -> float:
    """Apply a symmetric per-cycle slew limit to a grid setpoint target."""

    if max_step_w <= 0:
        raise ValueError("max_step_w must be positive")
    return max(previous_w - max_step_w, min(previous_w + max_step_w, desired_w))
