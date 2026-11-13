from datetime import datetime
from typing import TYPE_CHECKING
# ruff: noqa: I001


if not TYPE_CHECKING:
    # pyscript providese these imports from the subfolder modules directly
    from utils import now, set_state, get, get_attr
    from states import House, PVForecast, PVProduction
else:
    # The type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    # Therefore during type checking we pretend to import them from modules.utils, which it can resolve.
    from modules.utils import get, set_state, get_attr

    # These are provided by pscript and do not need to be imported in the actual script
    # They are only needed for type checking (linting), which development easier
    from modules.utils import log, now, time_trigger
    from modules.states import House, PVForecast, PVProduction


@time_trigger  # run on reload
@time_trigger("cron(*/1 * * * *)")
def calculate_target_time_and_energy():
    current_time = now()
    house_demand = get(House.daily_average_power, 500) / 1000  # convert to kW
    forecast_days = get_forecast_data()

    time_to_reach_target, energy_until_target = find_time_and_energy_to_reach_target(house_demand, forecast_days)
    log.info(f"Time to reach target: {time_to_reach_target}, Energy until target: {energy_until_target}")

    if not time_to_reach_target:
        # Missing or incomplete PV data must not invent energy that downstream
        # battery policy can spend. Keep the time unavailable and publish zero.
        log.warning("unable to compute when PV production will meet demand")
        energy_until_target = 0.0

    log.info(f"Time to reach target: {time_to_reach_target}, Energy until target: {energy_until_target}")
    set_state(PVProduction.next_meet_demand, time_to_reach_target or "unknown")
    set_state(
        PVProduction.energy_until_production_meets_demand,
        energy_until_target,
        unit_of_measurement="kWh",
        device_class="energy",
    )


def get_forecast_data():
    forecast = [
        get_attr(state, "detailedForecast", default=[])
        for state in [
            PVForecast.forecast_today,
            PVForecast.forecast_tomorrow,
            PVForecast.forecast_day_3,
            PVForecast.forecast_day_4,
        ]
    ]
    if not any(forecast):
        log.warning("could not get any forecast:" + str(get_attr(PVForecast.forecast_today, "detailedForecast")))
    return forecast or []


def interpolate_value(p, p1, p2, v1, v2):
    if p1 == p2:
        return v1
    return v1 + ((p - p1) / ((p2 - p1) + 1e-10)) * (v2 - v1)


def find_time_and_energy_to_reach_target(target_production, forecast_days):
    """Integrate forecast PV until its power first meets house demand."""

    t_now = now()
    periods = {}
    for forecast_data in forecast_days or []:
        for period in forecast_data or []:
            try:
                periods[period["period_start"]] = max(0.0, float(period["pv_estimate"]))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
    ordered = sorted(periods.items(), key=lambda item: item[0])
    if len(ordered) < 2:
        return None, 0.0

    total_energy = 0.0
    for (period_start, period_power), (next_start, next_power) in zip(ordered, ordered[1:]):
        if next_start <= t_now or next_start <= period_start:
            continue
        start = max(period_start, t_now)
        if start >= next_start:
            continue
        start_power = interpolate_value(
            start.timestamp(),
            period_start.timestamp(),
            next_start.timestamp(),
            period_power,
            next_power,
        )
        if start_power >= target_production:
            return start, total_energy
        if next_power >= target_production and next_power > start_power:
            crossing_ts = interpolate_value(
                target_production,
                start_power,
                next_power,
                start.timestamp(),
                next_start.timestamp(),
            )
            crossing = datetime.fromtimestamp(crossing_ts, tz=start.tzinfo)
            hours = (crossing - start).total_seconds() / 3600
            total_energy += (start_power + target_production) / 2 * hours
            return crossing, total_energy
        hours = (next_start - start).total_seconds() / 3600
        total_energy += (start_power + next_power) / 2 * hours

    return None, 0.0
