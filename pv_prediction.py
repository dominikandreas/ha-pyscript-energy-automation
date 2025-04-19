from datetime import datetime, timedelta
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
        log.info("unable to compute time to reach target")

        time_to_reach_target = current_time
        if current_time.hour > 10:
            time_to_reach_target += timedelta(days=1, hours=10 - current_time.hour)
        time_to_reach_target = time_to_reach_target.replace(hour=10, minute=0, second=0)
        energy_until_target = (time_to_reach_target - current_time).seconds / 3600 * house_demand

    log.info(f"Time to reach target: {time_to_reach_target}, Energy until target: {energy_until_target}")
    set_state(PVProduction.next_meet_demand, time_to_reach_target)
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
    if not forecast:
        log.warning("could not get any forecast:" + str(get_attr(PVForecast.forecast_today, "detailedForecast")))
    return forecast or []


def interpolate_value(p, p1, p2, v1, v2):
    if p1 == p2:
        return v1
    return v1 + ((p - p1) / ((p2 - p1) + 1e-10)) * (v2 - v1)


def find_time_and_energy_to_reach_target(target_production, forecast_days):
    t_now = now()
    total_energy = 0.0
    time_to_reach_target = None
    past_reached_time = None
    log_msg = "\n"

    for forecast_data in forecast_days:
        if not forecast_data:
            continue

        for current_period, next_period in zip(forecast_data, forecast_data[1:]):
            current_period_start = current_period["period_start"]
            next_period_start = next_period["period_start"]
            
            current_period_estimate = float(current_period["pv_estimate"])
            next_period_estimate = float(next_period["pv_estimate"])
            period_hours = (next_period_start - current_period_start).seconds / 3600

            log_msg += f"{current_period_start} target {target_production} is {current_period['pv_estimate']} kWh\n"

            # Localize current_time to the same timezone as current_period_start

            if next_period_start < t_now:
                if current_period_estimate >= target_production:
                    past_reached_time = current_period_start

                continue

            if current_period_start <= t_now < next_period_start:
                interpolated_pv = interpolate_value(
                    t_now.timestamp(),
                    current_period_start.timestamp(),
                    next_period_start.timestamp(),
                    current_period_estimate,
                    next_period_estimate,
                )
                total_energy += interpolated_pv * period_hours
                log_msg += f"iterpolated total energy to now: {total_energy} from td {next_period_start - current_period_start}: pv {interpolated_pv} kWh  total {total_energy}\n"

            elif (
                next_period_estimate >= target_production
                and time_to_reach_target is None
                and (not past_reached_time or current_period_start.day != past_reached_time.day)
            ):
                log_msg += f"found next period to reach target: {next_period_start} - {next_period['pv_estimate']} kWh total {total_energy}\n"
                interpolated_time = interpolate_value(
                    target_production,
                    current_period_estimate,
                    next_period_estimate,
                    current_period_start.timestamp(),
                    next_period_start.timestamp(),
                )
                time_to_reach_target = datetime.fromtimestamp(interpolated_time)

                interpolated_pv = interpolate_value(
                    interpolated_time,
                    current_period_start.timestamp(),
                    next_period_start.timestamp(),
                    current_period_estimate,
                    next_period_estimate,
                )
                total_energy += interpolated_pv * period_hours
                log_msg += f"trtt {time_to_reach_target}: current {current_period_start} - next {next_period_start}: pv {interpolated_pv} kWh  total {total_energy}\n"
                break
            else:
                total_energy += period_hours * next_period_estimate
                log_msg += f"start:{current_period_start} day now {t_now.day}- reached day {past_reached_time.day if past_reached_time else 'None'} next period > production {next_period['pv_estimate'] >= target_production} time to reach is None {time_to_reach_target is None}  total {total_energy}\n"

            if time_to_reach_target is not None:
                if (
                    next_period_start.hour + next_period_start.minute / 60
                    <= time_to_reach_target.hour + time_to_reach_target.minute / 60
                ):
                    total_energy += next_period_estimate * period_hours
                break

    log.info("find time to reach targert log:\n" + log_msg)

    return time_to_reach_target, total_energy
