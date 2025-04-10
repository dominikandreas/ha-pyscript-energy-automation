# ruff: noqa: I001

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from math import pi, sin, exp, sqrt
from typing import TYPE_CHECKING

# NODE: many functions have two @time_trigger decorators. this is not redundant, the first one
# without parameter triggers at function reload

if TYPE_CHECKING:
    # The type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    # Therefore during type checking we pretend to import them from modules.utils, which it can resolve.
    from modules.utils import clip, get, get_attr, set, service
    from modules.const import EV as EVConst

    # These are provided by typescript and do not need to be imported in the actual script
    # They are only needed for type checking (linting), which development easier
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
    from modules.victron import Victron

else:
    from const import EV as EVConst
    from utils import clip, get, get_attr, now, set, with_timezone
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
    from victron import Victron


class Const:
    ev_capacity = 60
    """The capacity of the EV battery in kWh"""
    ev_consumption_per_drive = 55
    """How much percent of the battery is consumed per drive"""
    ev_days_allowed_to_reach_target = 7
    """The average days it should take to reach the target state of charge.


    In order to get the vehicle charged, the schedule allows to define the next
    planned drive. This variable is a fallback to be used when no schedule is defined,
    to control how much energy shall be requested for the vehicle per day.
    """

    ev_phase_switch_delay = 20
    """The delay in seconds between switching the number of phases of the EV charger"""


power_w_attributes = {
    "unit_of_measurement": "W",
    "device_class": "power",
    "state_class": "measurement",
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


def update_battery_charge_discharge_times(battery_capacity, battery_energy, power):
    required_for_full = battery_capacity - battery_energy

    if power > 0:
        hours = required_for_full / power
        result = min(hours, 48)
    else:
        result = 48

    set(Battery.time_until_charged, round(result, 2), **energy_kwh_attributes)

    required_for_empty = battery_energy
    if power < 0:
        hours = required_for_empty / power
        result = min(hours, 48)
    else:
        result = 48

    set(Battery.time_until_discharged, round(result, 2), **energy_kwh_attributes)


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

    required_charge = max(0, (ev_required_soc - ev_current_soc) / 100) * Const.ev_capacity

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

    set(
        House.upcoming_demand,
        round(ev_energy + washing_energy, 2),
        **energy_kwh_attributes,
        icon="mdi:home-lightning",  # added icon
        friendly_name="Upcoming Demand",  # more descriptive friendly name
    )


@time_trigger
@time_trigger("cron(*/2 * * * *)")
def house_energy_until_production_meets_demand():
    night_avg_power = get(House.nightly_average_power, default=0) / 1000
    day_avg_power = get(House.daily_average_power, default=0) / 1000

    next_pv_meet_demand = get(PVProduction.next_meet_demand)
    if not next_pv_meet_demand or next_pv_meet_demand == "unknown":
        return

    t_now = now()
    dt = with_timezone(next_pv_meet_demand) - t_now

    total_energy = 0
    while True:
        if dt < timedelta(hours=0):
            break
        if 7 < (t_now + dt).hour < 19:
            total_energy += day_avg_power
        else:
            total_energy += night_avg_power
        dt -= timedelta(hours=1)

    log.info(f"House energy until production meets demand: {total_energy:.2f} kWh")

    set(
        House.energy_demand,
        f"{total_energy:.2f}",
        **energy_kwh_attributes,
        icon="mdi:home",
        friendly_name="House Energy Until Production Meets Demand",
    )


@pyscript_compile
def bilinear_interpolate(y, y1, y2, x1, x2):
    if y1 == y2:
        return x1
    y_range = abs(y1 - y2)
    a = abs((y - y1) / (y_range if y_range != 0 else 1))
    a_clipped = min(1, max(0, a))
    return (1 - a_clipped) * x1 + a_clipped * x2


@time_trigger
@time_trigger("period(now, 10sec)")
def excess_power_1m_average():
    excess = get(Excess.power, default=0)
    excess_avg = get(Excess.power_1m_average, default=0)
    excess_avg = round(0.9 * excess_avg + 0.1 * excess, 2)
    set(
        Excess.power_1m_average,
        f"{excess_avg:.2f}",
        **power_kw_attributes,
        friendly_name="Excess Power 1m Avg",
    )


@time_trigger
@time_trigger("period(now, 5sec)")
def grid_1m_average():
    grid_now = get(Grid.power_ac, default=0)  # in kW
    grid_avg = get(Grid.power_1m_average, default=grid_now)
    grid_avg = round(0.8 * grid_avg + 0.2 * grid_now, 2)
    set(
        Grid.power_1m_average,
        grid_avg,
        **power_kw_attributes,
        friendly_name="Grid 1m Average",
    )


@time_trigger
@state_trigger(Automation.min_discharge_price)
@state_trigger(ElectricityPrices.current_price)
@state_trigger(EV.is_charging)
@time_trigger("period(now, 60sec)")
def auto_victron_set_inverter_mode():
    # Get the current time and electricity price
    now = datetime.now()
    electricity_price = float(get(ElectricityPrices.current_price, default=0))
    min_discharge_price = float(get(Automation.min_discharge_price, default=0))
    ev_is_charging = get(EV.is_charging, False)
    surplus_energy = get(House.energy_surplus, 0)
    battery_soc = get(Battery.soc, 0)
    target_soc = get(Automation.battery_target_soc, 0)
    pv_power = get(PVProduction.total_power, 0)  # in kW
    daily_avg_power = get(House.nightly_average_power, 0)
    min_charge_power = 6 * 230  # 6A amps minimum

    # Determine if the current time is between 18:00 and 06:00
    is_night_time = now.hour >= 18 or now.hour < 6

    new_mode = Victron.InverterMode.off

    reason = "Default mode"
    new_mode = Victron.InverterMode.on

    if ev_is_charging:
        if surplus_energy > 0 or pv_power > (min_charge_power + daily_avg_power) and battery_soc > target_soc:
            reason = f"EV is charging with surplus energy of {surplus_energy} or pv_power > (min_charge_power + daily_avg_power)  {pv_power} > ({min_charge_power} + {daily_avg_power})"
            new_mode = Victron.InverterMode.on
        else:
            reason = f"EV is charging at {'night' if is_night_time else 'day'}"
            new_mode = Victron.InverterMode.off if is_night_time else Victron.InverterMode.charger_only
    else:
        if electricity_price < min_discharge_price and battery_soc < max(5, target_soc, -5):
            if is_night_time:
                reason = f"night time, battery {battery_soc}% < target {target_soc}% and price is low"
                new_mode = Victron.InverterMode.off
            else:
                reason = f"battery {battery_soc}% < target {target_soc}% and price is low"
                new_mode = Victron.InverterMode.charger_only

    charge_limit = get(Battery.force_charge_up_to, 0)
    if electricity_price < get(Battery.max_charge_price, 0) and battery_soc < target_soc and battery_soc < charge_limit:
        reason = "Setting Victron inverter mode to 'On' due to low price"
        new_mode = Victron.InverterMode.on
        # turn on switch.victron_victron_force_charge
        log.warning(
            f"Enabling force charge switch and setting charge limit, {battery_soc} < {target_soc}, < {charge_limit}"
        )
        service.call("switch", "turn_on", entity_id=Battery.force_charge_switch)
        set(Battery.charge_limit, 1550)
    elif get(Battery.force_charge_switch, False):
        log.warning("Disabling force charge switch and resetting charge limit")
        set(Battery.charge_limit, -1)
        service.call("switch", "turn_off", entity_id=Battery.force_charge_switch)

    new_mode = Victron.PAYLOAD_TO_MODE.get(new_mode)
    current_mode = get(Victron.inverter_mode_input_select)
    if current_mode != new_mode:
        log.warning(
            f"{current_mode} -> {new_mode}: {reason}. "
            f"ev: {ev_is_charging}, surplus: {surplus_energy}, soc: {battery_soc}, target soc: {target_soc}, "
            f"pv_power: {pv_power}, min_charge_power {min_charge_power} daily_avg_power {daily_avg_power}"
        )

    set(Victron.inverter_mode_input_select, value=new_mode)


@time_trigger
@time_trigger("cron(*/3 * * * *)")
def battery_use_until_pv_meets_demand():
    next_pv_meet_demand = get(PVProduction.next_meet_demand, None)
    if next_pv_meet_demand is None:
        return
    next_pv_meet_demand = with_timezone(next_pv_meet_demand)

    energy_until_pv_meets_demand = get(PVProduction.energy_until_production_meets_demand, 0)
    discharge_price = get(Automation.min_discharge_price, 100)
    daily_avg_power = get(House.nightly_average_power, 0)
    night_avg_power = get(House.daily_average_power, 0)
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

    set(
        Battery.use_until_pv_meets_demand,
        round(result, 3),
        **energy_kwh_attributes,
        friendly_name="Battery Use Until PV Meets Demand",
    )


def get_reserve_soc():
    t_now = now()
    min_reserve = 5
    summer_deviation = ((6 - (t_now.month - 1 + t_now.day / 30.0)) / 6) ** 2  # ranging from 0 to 1
    return min(min_reserve, round(summer_deviation * 30, 0))  # ranging from 5 to 30 (max 30% reserve during winter)


def get_ev_requested_energy_today():
    t_now = now()

    ev_soc = get(EV.soc, default=50)
    ev_short_term_demand = get(EV.short_term_demand, default=5)
    next_drive = get_attr(EV.planned_drives, "next_event")
    required_soc = get(EV.required_soc, default=50)

    required_energy_total = max(0, (required_soc - ev_soc) / 100 * Const.ev_capacity)

    if next_drive is not None:
        next_drive = with_timezone(next_drive)

        leaving_soon = (next_drive - t_now) < timedelta(hours=8)

        if leaving_soon:
            return required_energy_total

        return max(0, ev_short_term_demand)

    return required_energy_total * 1 / Const.ev_days_allowed_to_reach_target


@time_trigger
@state_trigger(f"{Charger.control_switch} != 'undefined' and {Automation.auto_battery_target_soc} == 'on'")
@time_trigger("cron(*/1 * * * *)")
def auto_battery_target_soc():
    battery_soc = get(Battery.soc, default=50)
    reserve_soc = get_reserve_soc()

    house_energy_demand = get(House.energy_demand, default=10)
    pv_upcoming = get(PVProduction.energy_until_production_meets_demand, default=0)
    excess_next_days = get(Excess.excess_next_three_days, default=0)
    battery_capacity = get(Battery.capacity, default=8)
    battery_cells_balanced = get(Battery.cells_balanced, False)
    battery_energy = get(Battery.energy, default=0)
    surplus = get(House.energy_surplus, 0)

    ev_is_charging = get(EV.is_charging, False)

    req_energy = max(
        0,
        house_energy_demand - pv_upcoming,
        min(0, excess_next_days),
        0 if surplus > 0 else min(battery_capacity, battery_energy - surplus),
    )

    set(Automation.req_energy, round(req_energy, 2), **energy_kwh_attributes)

    max_soc = 95 if battery_cells_balanced else 100

    minimal_soc = min(max_soc, (max(0, req_energy) / battery_capacity * 100) + reserve_soc)
    set(Automation.minimal_soc, round(minimal_soc, 2), unit_of_measurement="%")  # different attributes

    # prevent discharging of the battery if the EV is charging and insufficient surplus
    result_soc = max(battery_soc + 1, minimal_soc) if ev_is_charging and surplus < 1 else minimal_soc

    print(f"auto battery target soc: {result_soc}")
    set(
        Automation.battery_target_soc,
        round(result_soc, 2),
        unit_of_measurement="%",  # different attributes
    )


@time_trigger("cron(*/1 * * * *)")
@time_trigger
async def auto_excess_target():
    if not get(Automation.auto_excess_target, False):
        return

    battery_target_soc = get(Automation.battery_target_soc, default=0)
    battery_soc = get(Battery.soc, default=0)
    ev_required_soc = get(EV.required_soc, 80)

    power_abs_max = 2500  # half of max inverter power for best efficiency
    soc_difference = (battery_target_soc - battery_soc) / 100
    normalized_difference = soc_difference * 2 * pi
    normalized_difference_clipped = clip(normalized_difference, -pi / 2, pi / 2)
    power = sin(normalized_difference_clipped) * power_abs_max
    power = clip(power, -power_abs_max, power_abs_max)

    ev_is_charging = get(EV.is_charging, False)

    if ev_is_charging:
        t_now = now()

        next_event = get_attr(EV.planned_drives, "next_event")
        planned_leave_soon = False
        if next_event is not None:
            next_event = with_timezone(next_event)
            planned_leave_soon = (next_event - t_now).total_seconds() / 3600 < 8
            log.warning(f"Next event is {next_event}, planned_leave_soon: {planned_leave_soon}")
        if not planned_leave_soon:
            pv_power = get(PVProduction.total_power, 0)
            ev_soc = get(EV.soc, 100)
            log.warning(
                f"checking update of excess target with {t_now.hour} <= 14 and {pv_power} > 6000 and ev_soc {ev_soc} > 75 and bat {battery_soc} < 80"
            )
            # charge EV slowly before noon to reserve capacity at noon
            if 6 < t_now.hour < 15 and pv_power > 2000:
                if ev_soc > ev_required_soc:
                    log.warning(
                        f"Updating excess target from {power} to {pv_power - 4000}W because EV is charging, PV is high"
                    )
                    power = max(power, pv_power - 4000)
                else:
                    log.warning(f"Updating excess target from {power} to {pv_power - 2000}W because EV is charging")
                    power = max(power, pv_power - 2000)
    set(
        Excess.target,
        round(power / 1000, 2),
        **power_kw_attributes,
    )


@time_trigger
@time_trigger("cron(*/1 * * * *)")
def battery_energy():
    battery_soc = get(Battery.soc, default=-1)
    battery_capacity = get(Battery.capacity, default=0)
    if battery_capacity == 0 or battery_soc == -1:
        return

    set(
        Battery.energy,
        round(battery_soc / 100 * battery_capacity, 2),
        **energy_kwh_attributes,
        icon="mdi:car-battery",
        friendly_name="Battery Energy",
    )


@time_trigger
@time_trigger("cron(*/2 * * * *)")
def calculate_energy_surplus():
    battery_energy = max(0, get(Battery.energy, default=0) - 2)
    battery_demand_now = get(Battery.use_until_pv_meets_demand, default=5)
    excess_today_remaining = get(Excess.excess_today_remaining, default=0)
    excess_tomorrow = get(Excess.energy_next_day, default=0)
    excess_two_days = get(Excess.energy_two_days, default=0)
    excess_three_days = get(Excess.excess_next_three_days, default=0)

    t_now = now()
    a = (abs(t_now.month - 6) / 6) ** 3
    surplus_energy_target = a * 5 + (1 - a) * 0
    demand_per_day = 4  # extra kWh load that might occur

    remaining_today = battery_energy + excess_today_remaining
    remaining_tomorrow = battery_energy + excess_tomorrow - demand_per_day
    remaining_day_after_tomorrow = battery_energy + excess_two_days - 2 * demand_per_day
    remaining_next_three_days = battery_energy + excess_three_days - 3 * demand_per_day

    result = min(
        (
            (battery_energy - battery_demand_now)
            if battery_energy < battery_demand_now
            else remaining_today - surplus_energy_target
        ),
        remaining_tomorrow - surplus_energy_target,
        remaining_day_after_tomorrow - surplus_energy_target,
        remaining_next_three_days - surplus_energy_target,
    )

    log.warning(
        f"\nEnergy surplus: {result:.1f} kWh"
        f"\n\t a {a:.2f} surplus energy target {surplus_energy_target:.1f}"
        f"\n\t battery_energy = {battery_energy:.1f}, battery_demand = {battery_demand_now:.1f}, diff = {(battery_energy - battery_demand_now):.1f}"
        f"\n\t (battery_energy - battery_demand_now) - surplus_energy_target = {((battery_energy - battery_demand_now) - surplus_energy_target):.1f}"
        f"\n\t remaining_today {remaining_today:.1f} - surplus_energy_target = {remaining_today - surplus_energy_target:.1f}"
        f"\n\t remaining_tomorrow {remaining_tomorrow:.1f} - surplus_energy_target = {remaining_tomorrow - surplus_energy_target:.1f}"
        f"\n\t remaining_day_after_tomorrow {remaining_day_after_tomorrow:.1f} - surplus_energy_target = {remaining_day_after_tomorrow - surplus_energy_target:.1f}"
        f"\n\t remaining_next_three_days {remaining_next_three_days:.1f} - surplus_energy_target = {remaining_next_three_days - surplus_energy_target:.1f}"
    )

    set(
        House.energy_surplus,
        round(result, 2),
        **energy_kwh_attributes,
        icon="mdi:home",
        friendly_name="Energy Surplus Today",
    )


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
    class ForecastEntry:
        period_start: str
        pv_estimate: float
        battery_energy: float
        house_power: float
        setpoint: int
        power_draw: float
        energy_use: float
        energy_production: float
        free_capacity: float
        accumulated_energy: float
        feedin: float
        price: float
        battery_power: float
        setpoint_spread: float

        def format(self):
            return fix_entry_repr(str(self))[len(type(self).__name__) + 1 : -1]

    @dataclass
    class SetpointResult:
        setpoint: int
        min_bat: float
        t_min_bat: datetime
        max_bat: float
        t_max_bat: datetime
        max_feedin: float
        t_max_feedin: datetime
        setpoint_spread: float
        prices_mean: float
        prices_std: float
        detail: list[ForecastEntry]

        def format(self):
            return fix_entry_repr(str(self))

    @dataclass
    class PVForecastWithPrices:
        period_start: datetime
        pv_estimate: float
        price_per_kwh: float = 0

    return SetpointResult, ForecastEntry, PVForecastWithPrices


SetpointResult, ForecastEntry, PVForecastWithPrices = define_interfaces()

# @pyscript_compile
# def map_setpoint(setpoint, price, prices_mean, prices_std, max_feedin=4000):
#     if price > prices_mean + prices_std:
#         return max(-max_feedin, setpoint * 4)
#     elif price > prices_mean + 0.5 * prices_std:
#         return max(-max_feedin, setpoint * 2)
#     elif price < prices_mean - 0.5 * prices_std:
#         return max(-max_feedin, setpoint * 1 / 2)
#     elif price < prices_mean - prices_std:
#         return max(-max_feedin, setpoint * 1 / 4)
#     elif price == 0:
#         return 0
#     else:
#         return max(-max_feedin, setpoint)


# @pyscript_compile
# def map_setpoint(setpoint, price, prices_mean, prices_std, max_feedin=4000):
#     prob = gaussian_prob(price, prices_mean, prices_std)

#     if 1 > prob > 0 and price > prices_mean and setpoint < -20:
#         new_setpoint = (1 - prob) ** 2 * setpoint
#     else:
#         new_setpoint = -20
#     return round(min(-20, max(-max_feedin, new_setpoint)))


@pyscript_compile
def gaussian(x, mean, std):
    return exp(-0.5 * ((x - mean) / std) ** 2) / (std * sqrt(2 * pi))


@pyscript_compile
def map_setpoint(setpoint, price, prices_mean, prices_std, max_feedin=4000, setpoint_spread=1, min_setpoint=-20):
    prices_std = max(0.1, prices_std)
    if price > prices_mean + prices_std:
        price = prices_mean + prices_std

    gaus_prob = gaussian(price, prices_mean + prices_std, setpoint_spread * 2 * prices_std)

    setpoint = gaus_prob * setpoint / 0.4
    return max(-max_feedin, min(min_setpoint, setpoint))


def forecast_setpoint(
    forecast: list[PVForecastWithPrices],
    setpoint: float,
    battery_capacity: int,
    min_feedin_price: int,
    feed_in_limit=5500,
    forecast_dampening=0.8,
    battery_energy: float = 2.0,
    setpoint_spread=1,
):
    t_now = now()

    prices = [max(min_feedin_price, el.price_per_kwh) for el in forecast]
    prices_mean = sum(prices) / len(prices) if len(prices) > 0 else 0
    prices_std = sqrt(sum([(p - prices_mean) ** 2 for p in prices]) / len(prices)) if len(prices) > 0 else 0

    ev_capacity = EVConst.ev_capacity * min(
        get(EV.smart_charge_limit, 0.95), 0.95
    )  # assume 95% charging limit just to be safe
    ev_energy = get(EV.energy, 60)

    daily_power = get(House.daily_average_power, 0)  # W
    nightly_power = get(House.nightly_average_power, 0)  # W

    next_departure = get_attr(EV.planned_drives, "next_event", default=None, mapper=with_timezone)

    def get_free_capacity(dt):
        ev_charging_possible = (get(Charger.ready, False) or get(Charger.control_switch, False)) and (
            next_departure is None or dt < next_departure
        )

        if ev_charging_possible:
            free_capacity = ev_capacity - ev_energy + battery_capacity - battery_energy
        else:
            free_capacity = battery_capacity - battery_energy
        return free_capacity

    period_hours = (forecast[1].period_start - forecast[0].period_start).total_seconds() / 60 / 60
    period_minutes = period_hours * 60

    accumulated_energy = 0
    max_feedin = 0
    min_forecast_battery = battery_energy
    max_forecast_battery = battery_energy
    max_pv_feedin_target = 1000
    t_min_bat, t_max_bat, t_max_feedin = t_now, t_now, t_now
    detail: list[ForecastEntry] = []
    for entry, price in zip(forecast, prices):
        start: datetime = entry.period_start
        power_production: float = entry.pv_estimate * forecast_dampening * 1000

        this_setpoint = map_setpoint(setpoint, price, prices_mean, prices_std, feed_in_limit, setpoint_spread)

        house_power = daily_power if 7 < start.hour < 19 else nightly_power
        power_draw = house_power - this_setpoint
        energy_use = power_draw * period_hours / 1000  # kWh per forecast period

        energy_production = power_production * (period_minutes / 60) / 1000  # kWh
        free_capacity = get_free_capacity(start)

        if start < t_now:
            td = t_now - start
            if td < timedelta(minutes=period_minutes):
                td_minutes = td.total_seconds() / 60
                factor = 1 - td_minutes / period_minutes
                power_production *= factor
                power_draw *= factor
                energy_use *= factor
                energy_production *= factor
            else:
                continue

        accumulated_energy += energy_production - energy_use
        new_battery_energy = min(battery_capacity, battery_energy + accumulated_energy)

        feedin = 0
        if new_battery_energy == battery_capacity:
            feedin = max(0, power_production - power_draw)

        if feedin > max_feedin:
            max_feedin = power_production - power_draw
            t_max_feedin = start

        if new_battery_energy < min_forecast_battery:
            min_forecast_battery = new_battery_energy
            t_min_bat = start

        if new_battery_energy > max_forecast_battery:
            max_forecast_battery = new_battery_energy
            t_max_bat = start + timedelta(minutes=period_minutes)

        if max_feedin > max_pv_feedin_target and (max_pv_feedin_target > 0 or t_max_feedin.day == start.day):
            max_pv_feedin_target = max_feedin
            t_max_feedin = start

        detail.append(
            ForecastEntry(
                start,
                power_production,
                new_battery_energy,
                daily_power,
                this_setpoint,
                power_draw,
                energy_use,
                energy_production,
                free_capacity,
                accumulated_energy,
                feedin,
                price,
                power_draw,
                setpoint_spread=setpoint_spread,
            )
        )

    # log.warning("\n" + "\n".join([entry.format() for entry in detail[:10][::-1]]))

    return SetpointResult(
        setpoint,
        min_forecast_battery,
        t_min_bat,
        max_forecast_battery,
        t_max_bat,
        max_feedin,
        t_max_feedin,
        setpoint_spread,
        prices_mean=prices_mean,
        prices_std=prices_std,
        detail=detail,
    )


@pyscript_compile
def merge_setpoint_results(a: SetpointResult, b: SetpointResult, t_split: datetime):
    # Merge two setpoint results
    a_detail = [entry for entry in a.detail if entry.period_start <= t_split]
    b_detail = [entry for entry in b.detail if entry.period_start > t_split]

    merged_setpoint = replace(
        a,
        min_bat=min(a.min_bat, b.min_bat),
        t_min_bat=a.t_min_bat if a.t_min_bat < b.t_min_bat else b.t_min_bat,
        max_bat=max(a.max_bat, b.max_bat),
        t_max_bat=a.t_max_bat if a.t_max_bat > b.t_max_bat else b.t_max_bat,
        max_feedin=max(a.max_feedin, b.max_feedin),
        t_max_feedin=a.t_max_feedin if a.t_max_feedin > b.t_max_feedin else b.t_max_feedin,
        setpoint_spread=a.setpoint_spread,
        detail=a_detail + b_detail,
    )
    return merged_setpoint


def get_forecast_with_prices(t_start: datetime, t_end: datetime, epex_prices: list[dict]):
    forecast = [
        el
        for el in [
            *get_attr(PVForecast.forecast_today, "detailedForecast", default=[]),
            *get_attr(PVForecast.forecast_tomorrow, "detailedForecast", default=[]),
        ]
        if el["period_start"] > (t_start - timedelta(minutes=31)) and el["period_start"] < t_end
    ]

    def get_date_tuple(date_time: str | datetime):
        if isinstance(date_time, datetime):
            dt = date_time.astimezone()
        else:
            dt = datetime.fromisoformat(date_time).astimezone()
        return dt.day, dt.hour, dt.minute

    prices = {
        (get_date_tuple(entry["start_time"]), get_date_tuple(entry["end_time"])): entry["price_per_kwh"]
        for entry in epex_prices
    }
    period_hours = (forecast[1]["period_start"] - forecast[0]["period_start"]).total_seconds() / 60 / 60
    prices = {}
    for entry in epex_prices:
        start_time = datetime.fromisoformat(entry["start_time"]).astimezone()
        prices[get_date_tuple(start_time)] = entry["price_per_kwh"]
        prices[get_date_tuple(start_time + timedelta(hours=period_hours))] = entry["price_per_kwh"]

    for idx, forecast_entry in enumerate(list(forecast)):
        # insert price
        start_time = forecast_entry["period_start"]

        forecast[idx] = PVForecastWithPrices(
            start_time, pv_estimate=forecast_entry["pv_estimate"], price_per_kwh=prices.get(get_date_tuple(start_time))
        )
        if forecast[idx].price_per_kwh is None:
            log.warning(f"No price found for forecast entry {get_date_tuple(start_time)}")

    return forecast


@time_trigger("period(now, 30sec)")
@state_trigger(f"{Automation.auto_setpoint} == 'on' or {Grid.max_feedin_target} or {Grid.max_pv_feedin_target}")
def auto_setpoint_target_update():
    # if not get(Automation.auto_setpoint, False):
    #     return
    t_now = now()
    setpoint = 0

    max_feedin_limit = get(Grid.max_feedin_target, 4000)
    max_pv_feedin = get(Grid.max_pv_feedin_target, 4000)

    high_battery_power_threshold = 4000

    forecast_dampening = 0.9  # dampen the forecast to account for inaccuracies
    battery_min_energy = 2
    battery_energy = get(Battery.energy, 0)
    min_feedin_price = 0.08

    battery_capacity = get(Battery.capacity, 0)

    # log.warning(forecast_prices)
    def forecast_setpoint_local(
        forecast,
        setpoint,
        setpoint_spread=0.1,
        current_battery_energy=0,
        t_start: datetime | None = None,
        t_end: datetime | None = None,
    ):
        if t_start is not None:
            forecast = [entry for entry in forecast if t_start < entry.period_start]
        elif t_end is not None:
            forecast = [entry for entry in forecast if entry.period_start < t_end]
        return forecast_setpoint(
            forecast,
            setpoint=setpoint,
            battery_capacity=battery_capacity,
            min_feedin_price=min_feedin_price,
            feed_in_limit=max_feedin_limit,
            forecast_dampening=forecast_dampening,
            battery_energy=current_battery_energy,
            setpoint_spread=setpoint_spread,
        )

    # if (res := forecast_setpoint_local(forecast, -20)).max_feedin < 100 or surplus <= 0:
    #     log.warning(
    #         f"No significant feedin expected, setting setpoint target to -20: {replace(res, detail=None)}"
    #         + "\n"
    #         + "\n ".join([entry.format() for entry in res.detail[:10][::-1]])
    #     )
    #     return set(Grid.power_setpoint_target, -20, detail=res.detail)

    accuracy = 10  # Desired accuracy in watts

    # log_msg = ""

    # Binary search for optimal setpoint
    current_battery_energy = battery_energy

    def setpoint_binary_search(forecast, min_setpoint=-max_feedin_limit, max_setpoint=-20, setpoint_spread=0.1):
        search_results = []
        for itr in range(20):
            mid_setpoint = (min_setpoint + max_setpoint) // 2

            r = forecast_setpoint_local(
                forecast,
                mid_setpoint,
                setpoint_spread,
                current_battery_energy,
            )
            search_results.append(r)
            if r.min_bat < battery_min_energy:
                # If battery is too low, we need a more positive setpoint
                min_setpoint = mid_setpoint
            elif r.max_feedin > max_pv_feedin / 2:
                # If feed-in limit is exceeded, we need a more negative setpoint
                max_setpoint = mid_setpoint
            else:
                # If feed-in limit is not exceeded, we can use this or less negative
                min_setpoint = mid_setpoint

            if (max_setpoint - min_setpoint) < accuracy:
                break
        return search_results

    def price_setpoint_spread_search(
        forecast, setpoint, battery_energy, setpoint_spread=0.2, t_start=None, t_end=None, max_iters=10
    ):
        """Search for the optimal setpoint spread depending on feedin price."""

        for itr in range(max_iters):
            # the update factor quadratically decreases from 1.5 to 1.05
            update_factor = 1 + (1.5 - 1) * (1 - (itr / max_iters) ** 0.5)

            r = forecast_setpoint_local(forecast, setpoint, setpoint_spread, battery_energy, t_start, t_end)

            high_battery_power = next(
                iter([el.battery_power for el in r.detail if abs(el.battery_power) > high_battery_power_threshold]),
                False,
            )

            if r.min_bat < battery_min_energy:
                setpoint_spread *= 1 / update_factor
                setpoint *= 1 / update_factor

            if r.max_feedin > 2000:
                setpoint_spread *= update_factor
                setpoint *= update_factor

            else:
                if high_battery_power:
                    setpoint_spread *= update_factor
                    setpoint *= update_factor
                else:
                    break

        return replace(r, setpoint=setpoint, setpoint_spread=setpoint_spread)

    epex_prices = get_attr(ElectricityPrices.epex_forecast_prices, "data", [])

    forecast = get_forecast_with_prices(t_start=t_now, t_end=t_now + timedelta(hours=24), epex_prices=epex_prices)

    search_results = setpoint_binary_search(forecast, -max_feedin_limit, -20, setpoint_spread=0.2)
    setpoint_result = search_results[-1]
    t_start = t_now
    t_end = t_now + timedelta(hours=24)
    start_battery_energy = battery_energy
    for _ in range(4):
        t_max_feedin = t_min_bat = None
        setpoint_forecast = [e for e in setpoint_result.detail if t_start <= e.period_start <= t_end]
        t_max_feedin = next(iter([e.period_start for e in setpoint_forecast if e.feedin > max_feedin_limit]), None)

        if t_max_feedin is not None:
            log.warning(f"t_max_feedin: {t_max_feedin}")
            t_end = t_max_feedin + timedelta(hours=1)
            t_min_bat = next(
                iter(
                    [
                        e.period_start
                        for e in setpoint_forecast
                        if e.battery_energy < (battery_min_energy + 1) and e.period_start < t_end
                    ]
                ),
                None,
            )
            if t_min_bat is not None:
                t_start = t_min_bat  # + timedelta(hours=1)
            else:
                t_start = setpoint_result.t_max_feedin - timedelta(hours=4)
        else:
            t_end = t_now + timedelta(hours=24)

        if (t_end - t_start) < timedelta(hours=1):
            log.warning(f" too short time: {t_start} to {t_end}")
            break

        log.warning(f"checking setpoint for {t_start} to {t_end}")

        start_battery_energy = next(
            iter([e.battery_energy for e in setpoint_result.detail if e.period_start >= t_start]), start_battery_energy
        )

        new_setpoint_result = price_setpoint_spread_search(
            forecast,
            setpoint_result.setpoint,
            start_battery_energy,
            setpoint_spread=setpoint_result.setpoint_spread,
            t_start=t_start,
            t_end=t_end,
        )

        if t_start == t_now:
            setpoint_result = new_setpoint_result
        else:
            setpoint_result = merge_setpoint_results(setpoint_result, new_setpoint_result, t_start)

        t_start = t_end + timedelta(minutes=1)
        t_end = t_now + timedelta(hours=24)

    price = max(min_feedin_price, get(ElectricityPrices.epex_forecast_prices, min_feedin_price))

    setpoint = map_setpoint(
        setpoint_result.setpoint,
        price,
        setpoint_result.prices_mean,
        setpoint_result.prices_std,
        max_feedin_limit,
        setpoint_result.setpoint_spread,
    )

    log.warning(
        f"Mapped setpoint: {setpoint_result.setpoint} to {setpoint} with spread {setpoint_result.setpoint_spread} "
        f"price now {price:.2f} mean {setpoint_result.prices_mean:.2f} price std {setpoint_result.prices_std:.2f} "
        f"min_bat {setpoint_result.min_bat:.1f} at {setpoint_result.t_min_bat.strftime('%H:%M')} "
    )

    # log_msg += (
    #     f"\n\t -> calculated setpoint at {setpoint} with min batt {r.min_bat:.1f} at {r.t_max_bat.strftime('%H:%M')} "
    #     f"max batt {r.max_bat:.1f} at {r.t_max_bat.strftime('%H:%M')} max feedin {r.max_feedin:.0f} at {r.t_max_feedin.strftime('%H:%M')}"
    # )

    # current_setpoint = get(Grid.power_setpoint, 0)

    # Print setpoint results in tablular format (without forecast details)
    lines = []

    def ft(t):
        return t.strftime("%H:%M")

    def fi(k):
        return f"{k:.0f}"

    lines = [
        f"{str(r.setpoint):>{7}}{r.setpoint_spread:9.1f}{r.min_bat:9.1f}{'':6s}{ft(r.t_min_bat):10s}{r.max_bat:5.1f}{'':6s}{ft(r.t_max_bat):12s}{fi(r.max_feedin):12s}{ft(r.t_max_feedin):10s}"
        for r in [*search_results, setpoint_result]
    ]
    log.warning(
        f"Setpoint results:\n{'setpoint':<11s}{'spread':<10s}{'min_bat':<10s}{'t_min_bat':<11s}{'max_bat':<11s}{'t_max_bat':<11s}{'max_feedin':<11s}{'t_max_feedin':<10s}\n"
        + "\n".join(lines)
    )

    # detail = []
    # for setpoint_result in setpoint_results:
    #     for entry in setpoint_result.detail:
    #         if len(detail) == 0 or entry.period_start > detail[-1].period_start:
    #             detail.append(entry)

    set(
        Grid.power_setpoint_target,
        setpoint,
        **power_w_attributes,
        detail=setpoint_result.detail,
    )
