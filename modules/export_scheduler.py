"""Pure helpers for the unified battery export scheduler.

The scheduler plans a delta from a neutral forecast.  This is important: the
neutral forecast already contains PV charging, house self-consumption, planned
EV charging, inverter modes, and battery floors.  The scheduler only decides
where safely spendable battery energy is most valuable.
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
class ExportSchedulePlan:
    """Serializable result of one scheduler run."""

    grid_target_schedule: dict[str, float]
    battery_power_delta_schedule: dict[str, float]
    allocated_energy_kwh: float
    headroom_energy_kwh: float
    economic_energy_kwh: float
    unallocated_headroom_kwh: float


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
    quiet_end_hour: int = 23,
) -> ExportSchedulePlan:
    """Build one price-ranked schedule used by forecast and live control.

    During quiet hours the discharge band above ``efficient_discharge_w`` is
    ranked at a configurable price discount.  At other times the whole useful
    inverter range receives the actual price.  Mandatory PV headroom is
    allocated first, before its raw (undiscounted) forecast peak.

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

    grid_target_schedule = {}
    battery_power_delta_schedule = {}
    mandatory_bands = []
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
        efficient_delta_end_w = min(
            max_delta_w,
            max(0.0, efficient_discharge_w + slot.baseline_battery_power_w),
        )

        bands = []
        if not quiet:
            bands.append((slot.price_per_kwh, 0.0, max_delta_w))
        else:
            if efficient_delta_end_w > 0:
                bands.append((slot.price_per_kwh, 0.0, efficient_delta_end_w))
            if max_delta_w > efficient_delta_end_w:
                bands.append(
                    (
                        slot.price_per_kwh * (1.0 - quiet_boost_penalty_fraction),
                        efficient_delta_end_w,
                        max_delta_w,
                    )
                )

        for score, band_start_w, band_end_w in bands:
            band = (
                -score,
                slot.period_start,
                key,
                slot.duration_hours,
                band_start_w,
                band_end_w,
            )
            if headroom_deadline is not None and slot.period_start < headroom_deadline:
                mandatory_bands.append(band)
            if slot.price_per_kwh >= min_discharge_price:
                economic_bands.append(band)

    mandatory_bands.sort()
    economic_bands.sort()
    allocated_w_by_key = {}

    remaining_headroom_kwh = _allocate_bands(
        mandatory_bands,
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


def adapt_grid_target_to_live_power(
    planned_battery_power_w: float,
    house_load_w: float,
    pv_power_w: float,
    max_grid_export_w: float,
    neutral_grid_target_w: float,
    ev_is_charging: bool = False,
) -> float:
    """Preserve planned battery power using live load/PV without grid draw.

    Battery power follows the forecast convention: positive charges, negative
    discharges.  Clamping at the neutral export target means a PV shortfall can
    reduce planned charging/export but cannot request avoidable grid import.
    """

    if ev_is_charging:
        return neutral_grid_target_w

    natural_grid_power_w = house_load_w - pv_power_w
    desired_grid_target_w = natural_grid_power_w + planned_battery_power_w
    return max(-max_grid_export_w, min(neutral_grid_target_w, desired_grid_target_w))
