# ruff: noqa: I001


from dataclasses import dataclass, replace
from datetime import datetime, timedelta, date
from math import pi, sin, exp, sqrt
from typing import TYPE_CHECKING, Any


# NODE: many functions have two @time_trigger decorators. this is not redundant, the first one
# without parameter triggers at function reload

if TYPE_CHECKING:
    # The type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    # Therefore during type checking we pretend to import them from modules.utils, which it can resolve.
    from modules.utils import clip, get, get_attr, set_state
    from modules.const import EV as EVConst
    from modules.energy_core import _get_ev_smart_charge_limit, ChargeAction

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
    )  # noqa: F401
    from electricity_price import is_low_price, get_price


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

    set_state(Battery.time_until_charged, round(result, 2), **energy_kwh_attributes)

    required_for_empty = battery_energy
    if power < 0:
        hours = required_for_empty / power
        result = min(hours, 48)
    else:
        result = 48

    set_state(Battery.time_until_discharged, round(result, 2), **energy_kwh_attributes)


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
    night_avg_power = get(House.nightly_average_power, default=0) / 1000
    day_avg_power = get(House.daily_average_power, default=0) / 1000

    next_pv_meet_demand = get(PVProduction.next_meet_demand)
    if not next_pv_meet_demand or next_pv_meet_demand == "unknown":
        return

    next_pv_meet_demand = with_timezone(next_pv_meet_demand)
    t_now = now()
    dt = next_pv_meet_demand - t_now

    total_energy = 0
    # to ensure we never hit an infinity loop for whatever reason
    iters, max_iters = (0, 240)
    while dt < timedelta(hours=0) and iters < max_iters:
        next_pv_meet_demand = next_pv_meet_demand + timedelta(days=1)

        if 7 < (t_now + dt).hour < 19:
            total_energy += day_avg_power
        else:
            total_energy += night_avg_power

        dt = next_pv_meet_demand - t_now
        iters += 1

    # log.info(f"House energy until production meets demand: {total_energy:.2f} kWh")

    set_state(
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
    excess = get(Excess.power, default=0)  # in W
    excess_avg = get(Excess.power_1m_average, default=0)
    excess_avg = round(0.9 * excess_avg + 0.1 * excess, 2)
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
    grid_avg = round(0.8 * grid_avg + 0.2 * grid_now, 2)
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
@time_trigger("period(now, 60sec)")
def auto_victron_set_inverter_mode():
    # Get the current time and electricity price
    now = datetime.now()
    electricity_price = float(get(ElectricityPrices.current_price, default=0))
    min_discharge_price = float(get(Automation.min_discharge_price, default=0))
    ev_is_charging = get(EV.is_charging, False)
    surplus_energy = get(House.energy_surplus, -1337)
    battery_soc = get(Battery.soc, -1337)
    target_soc = get(Automation.battery_target_soc, -1337)
    pv_power = get(PVProduction.total_power, -1337)  # in kW
    daily_avg_power = get(House.daily_average_power, -1337)
    charge_limit = get(Battery.force_charge_up_to, 0)
    max_charge_price = get(Battery.max_charge_price, 0)
    force_charge_switch = get(Battery.force_charge_switch, False)
    current_mode = get(Victron.inverter_mode_input_select)

    for v in (surplus_energy, battery_soc, target_soc, pv_power, daily_avg_power):
        if v == -1337:
            log.error(
                f"Not all required states are available yet: surplus_energy={surplus_energy}, battery_soc={battery_soc}, target_soc={target_soc}, pv_power={pv_power}, daily_avg_power={daily_avg_power}, skipping auto victron inverter mode"
            )
            return

    new_mode, new_charge_limit, new_force_charge_switch_state, reason = get_auto_inverter_mode(
        ev_is_charging,
        surplus_energy,
        pv_power,
        daily_avg_power,
        battery_soc,
        target_soc,
        electricity_price,
        min_discharge_price,
        max_charge_price,
        charge_limit,
        force_charge_switch,
    )

    if new_charge_limit is not None:
        set_state(Battery.charge_limit, new_charge_limit)

    if new_force_charge_switch_state is not None:
        set_state(Battery.force_charge_switch, new_force_charge_switch_state)

    new_mode = Victron.PAYLOAD_TO_MODE.get(new_mode)
    if current_mode != new_mode:
        log.warning(
            f"{current_mode} -> {new_mode}: {reason}. "
            f"ev: {ev_is_charging}, surplus: {surplus_energy}, soc: {battery_soc}, target soc: {target_soc}, "
            f"pv_power: {pv_power}, daily_avg_power {daily_avg_power}"
        )

    set_state(Victron.inverter_mode_input_select, value=new_mode)


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

    set_state(
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


@pyscript_compile
def get_required_energy(
    house_energy_demand: float,
    pv_upcoming: float,
    excess_next_days: float,
    battery_capacity: float,
    battery_energy: float,
    surplus: float,
) -> float:
    return max(
        0,
        house_energy_demand - pv_upcoming,
        min(0, excess_next_days),
        0 if surplus > 0 else min(battery_capacity, battery_energy - surplus),
    )


@time_trigger
@state_trigger(f"{Charger.control_switch} != 'undefined' and {Automation.auto_battery_target_soc} == 'on'")
@time_trigger("cron(*/1 * * * *)")
def auto_battery_target_soc():
    battery_soc = get(Battery.soc, default=50)
    reserve_soc = get_reserve_soc()

    house_demand = get(House.energy_demand, default=10)
    pv_upcoming = get(PVProduction.energy_until_production_meets_demand, default=0)
    excess_next_days = get(Excess.excess_next_three_days, default=0)
    battery_capacity = get(Battery.capacity, default=8)
    battery_cells_balanced = get(Battery.cells_balanced, False)
    battery_energy = get(Battery.energy, default=0)
    surplus = get(House.energy_surplus, 0)

    ev_is_charging = get(EV.is_charging, False)

    req_energy = get_required_energy(
        house_demand,
        pv_upcoming,
        excess_next_days,
        battery_capacity,
        battery_energy,
        surplus,
    )

    set_state(Automation.req_energy, round(req_energy, 2), **energy_kwh_attributes)

    max_soc = 95 if battery_cells_balanced else 100

    minimal_soc = min(max_soc, (max(3, req_energy) / battery_capacity * 100) + reserve_soc)
    set_state(Automation.minimal_soc, round(minimal_soc, 2), unit_of_measurement="%")  # different attributes

    # prevent discharging of the battery if the EV is charging and insufficient surplus
    result_soc = max(battery_soc + 1, minimal_soc) if ev_is_charging and surplus < 1 else minimal_soc

    print(f"auto battery target soc: {result_soc}")
    set_state(
        Automation.battery_target_soc,
        round(result_soc, 2),
        min=0,
        max=100,
        unit_of_measurement="%",  # different attributes
    )


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
    power = sin(normalized_difference_clipped) * power_abs_max
    power = clip(power, -power_abs_max, power_abs_max)
    if ev_is_charging and efficient_discharge:
        planned_leave_soon = False
        if next_planned_drive is not None:
            next_planned_drive = with_timezone(next_planned_drive)
            planned_leave_soon = (next_planned_drive - t_now).total_seconds() / 3600 < 24
        # charge EV slowly to reserve capacity at noon
        if not planned_leave_soon and pv_power > 2000:
            if ev_soc > ev_required_soc:
                power = max(power, pv_power - 4000)
            else:
                power = max(power, pv_power / 3)

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


# @time_trigger
# @time_trigger("cron(*/2 * * * *)")
def calculate_energy_surplus():
    battery_energy = max(0, get(Battery.energy, default=0) - 2)
    battery_demand_now = get(Battery.use_until_pv_meets_demand, default=5)
    excess_today_remaining = get(Excess.excess_today_remaining, default=0)

    excess_tomorrow = get(Excess.energy_next_day, default=0)
    excess_two_days = get(Excess.energy_two_days, default=0)
    excess_three_days = get(Excess.excess_next_three_days, default=0)
    energy_surplus = update_energy_surplus(
        battery_energy, battery_demand_now, excess_today_remaining, excess_tomorrow, excess_two_days, excess_three_days
    )
    set_energy_surplus(energy_surplus)


def set_energy_surplus(surplus, entity, **kwargs):
    set_state(
        entity,
        round(surplus, 2),
        **energy_kwh_attributes,
        icon="mdi:home",
        friendly_name=" ".join(entity.split(".")[-1].split("_")).title(),
        **kwargs,
    )


def update_energy_surplus(
    battery_energy, battery_demand_now, excess_today_remaining, excess_tomorrow, excess_two_days, excess_three_days
):
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
    return result


@pyscript_compile
def parse_full_schedule(
    schedule_data: dict[str, list[dict[str, Any]]], default_required_soc: float
) -> list["EVScheduleEntry"]:
    today = date.today()
    today_weekday = today.weekday()
    day_map = {
        name: i for i, name in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])
    }

    entries = []
    for day_name, events in schedule_data.items():
        event_weekday = day_map.get(day_name, -1)
        if event_weekday == -1:
            continue

        day_offset = (event_weekday - today_weekday + 7) % 7
        event_date = today + timedelta(days=day_offset)

        for event in events:
            soc = event.get("data", {}).get("required")
            distance = event.get("data", {}).get("distance")  # km
            if not soc and distance:
                soc = distance / 100 * EVConst.kwh_per_100km / EVConst.ev_capacity * 100 * 1.2  # add 20% margin
            elif not soc:
                soc = default_required_soc

            entries.append(
                EVScheduleEntry(
                    start=with_timezone(datetime.combine(event_date, event["from"])),
                    end=with_timezone(datetime.combine(event_date, event["to"])),
                    required_soc=float(soc) if soc is not None else None,
                    distance=float(distance) if distance is not None else None,
                )
            )

    return sorted(entries, key=lambda x: x.start)


def get_ev_schedule():
    ev_schedule = (schedule.get_schedule(entity_id="schedule.tesla_planned_drives") or {}).get(
        "schedule.tesla_planned_drives", {}
    )
    ev_required_soc = get(EV.required_soc, 80)
    return parse_full_schedule(ev_schedule, default_required_soc=ev_required_soc)


@time_trigger
@time_trigger("cron(*/2 * * * *)")
def forecast_surplus():
    task.unique("forecast_surplus", kill_me=True)
    epex_prices = get_attr(ElectricityPrices.epex_forecast_prices, "data", [])
    t_now = now()
    t_end = t_now + timedelta(hours=80)

    battery_capacity = get(Battery.capacity, 1337)
    battery_energy = get(Battery.energy, 1337)

    if 1337 in (battery_capacity, battery_energy):
        log.warning("Battery capacity or energy not available yet, cannot calculate surplus")
        return

    pv_forecast = get_pv_forecast_with_prices(t_start=t_now, t_end=t_end, epex_prices=epex_prices)
    if not pv_forecast or len(pv_forecast) < 2:
        log.warning("Not enough PV forecast data to calculate surplus")
        return
    period_hours = (pv_forecast[1].period_start - pv_forecast[0].period_start).total_seconds() / 3600

    forecast_no_ev = forecast_setpoint(
        forecast=pv_forecast,
        battery_capacity=battery_capacity,
        battery_energy=battery_energy,
        setpoint=-20,
        forecast_dampening=0.75,
        with_ev_charging=False,
        logging=False,
    )

    idx = 0
    total_feedin = 0
    min_battery_energy = min([el.battery_energy for el in forecast_no_ev.detail] or [0])
    surplus = round(max(0, min_battery_energy - 10), 2)

    while (detail := forecast_no_ev.detail[idx]).battery_energy > min_battery_energy and (idx := idx + 1) < len(
        forecast_no_ev.detail
    ):
        total_feedin += detail.feedin / 1000 * period_hours

    if total_feedin > 0:
        surplus += total_feedin

    ev_schedule = get_ev_schedule()
    if ev_schedule:
        forecast_with_ev = forecast_setpoint(
            forecast=pv_forecast,
            battery_capacity=get(Battery.capacity, 0),
            battery_energy=get(Battery.energy, 0),
            setpoint=-20,
            forecast_dampening=0.75,
            with_ev_charging=True,
            ev_schedule=ev_schedule,
            logging=False,
            surplus_energy=surplus,
            ev_energy=get(EV.energy, 0),
        )

    min_battery_energy = min([el.battery_energy for el in forecast_with_ev.detail] or [0])
    surplus_after_ev_charging = round(max(0, min_battery_energy - 10), 2)
    log.warning(
        f"""#################### Forecast Surplus
        Min battery: {min_battery_energy:.2f} kWh
        Final battery energy: {forecast_no_ev.detail[-1].battery_energy:.2f} kWh
        Surplus: {surplus:.2f} kWh
        Surplus after EV charging: {surplus_after_ev_charging:.2f} kWh
        last entry date: {forecast_no_ev.detail[-1].period_start if len(forecast_no_ev.detail) > 0 else "n/a"}
        total feedin: {total_feedin:.2f} kWh
        ####################
        """
    )
    detail_with_ev_vectorized = {
        k: [getattr(el, k) for el in forecast_with_ev.detail] for k in ForecastEntry.__annotations__.keys()
    }
    detail_without_ev_vectorized = {
        k: [getattr(el, k) for el in forecast_no_ev.detail] for k in ForecastEntry.__annotations__.keys()
    }
    set_energy_surplus(surplus, House.energy_surplus, detail=detail_without_ev_vectorized)
    set_energy_surplus(
        surplus_after_ev_charging, House.energy_surplus_after_ev_charging, detail=detail_with_ev_vectorized
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
        energy_use: float
        energy_production: float
        free_capacity: float
        accumulated_energy: float
        feedin: float
        price: float
        battery_power: float
        setpoint_spread: float
        ev_energy: float
        ev_charge_power: float
        excess_target: float
        surplus: float
        power_from_grid: float

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
        max_battery_power_target: float
        detail: list[ForecastEntry]

        def format(self):
            return fix_entry_repr(str(self))

    @dataclass
    class PVForecastWithPrices:
        period_start: datetime
        pv_estimate: float
        price_per_kwh: float = 0

    return EVScheduleEntry, SetpointResult, ForecastEntry, PVForecastWithPrices


EVScheduleEntry, SetpointResult, ForecastEntry, PVForecastWithPrices = define_interfaces()


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

    # exponential decay from 1 to 0 from battery_min_limit + 2 to battery_min_limit
    if battery_energy < battery_min_limit + 2 and pv_power < house_power:
        new_setpoint = setpoint * ((battery_energy - battery_min_limit) / 2) ** 4

    surplus_pv = max(0, pv_power - house_power - max_battery_power_target)

    if surplus_pv > 0 and setpoint < max_setpoint:
        new_setpoint = min(new_setpoint, -surplus_pv)

    return max(-(max_feedin + surplus_pv), min(min_setpoint, new_setpoint))


def forecast_setpoint(
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
    max_setpoint=-20,
    logging=False,
    ev_schedule: list[EVScheduleEntry] | None = None,
    surplus_energy: float | None = None,
):
    t_now = now()

    prices = [max(min_feedin_price, el.price_per_kwh) for el in forecast]
    prices_mean = sum(prices) / len(prices) if len(prices) > 0 else 0
    prices_std = sqrt(sum([(p - prices_mean) ** 2 for p in prices]) / len(prices)) if len(prices) > 0 else 0

    smart_charge_limit = 80
    ev_required_soc = get(EV.required_soc, 80)
    ev_soc = get(EV.soc, 100)
    is_charging_ev = get(EV.is_charging, False)

    smart_limiter_active = get(Automation.auto_charge_limit, False)

    daily_power = get(House.daily_average_power, 0)  # W
    nightly_power = get(House.nightly_average_power, 0)  # W

    charger_ready = get(Charger.ready, False)
    # TODO: is this sufficient to determine if the EV is actually charging?
    #       or should we also check the charger power?
    is_charging_ev = get(Charger.control_switch, False)
    ongoing_drive = None
    next_drive = next(iter([s for s in ev_schedule if s.start > t_now]), None) if ev_schedule else None

    if with_ev_charging:
        assert ev_energy is not None, "ev_energy must be provided if with_ev_charging is True"

    def is_charging_possible(dt, ev_energy, smart_charge_limit):
        if not with_ev_charging:
            return False
        ongoing_drive = next(iter([s for s in ev_schedule if s.start <= dt < s.end]), None)
        return (
            with_ev_charging
            and ((charger_ready or is_charging_ev or dt > next_drive.end) and ongoing_drive is None)
            and ev_energy < EVConst.ev_capacity * smart_charge_limit / 100
        )

    charge_limit = get(Battery.force_charge_up_to, 0)
    max_charge_price = get(Battery.max_charge_price, 0)
    force_charge_switch = get(Battery.force_charge_switch, False)
    min_discharge_price = float(get(Automation.min_discharge_price, default=0))
    surplus = surplus_energy or get(House.energy_surplus, 0)  # TODO, ensure this is passed

    def get_inverter_mode(pv_power, target_soc, current_soc, electricity_price):
        assert surplus is not None
        new_mode, new_charge_power_limit, new_force_charge_switch_state, reason = get_auto_inverter_mode(
            is_charging_ev,
            surplus,
            pv_power,
            daily_power,
            current_soc,
            target_soc,
            electricity_price,
            min_discharge_price,
            max_charge_price,
            charge_limit,
            force_charge_switch,
        )
        return new_mode, new_charge_power_limit, new_force_charge_switch_state, reason

    full_period_minutes = (forecast[1].period_start - forecast[0].period_start).total_seconds() / 60

    accumulated_energy = 0
    max_feedin = 0
    min_forecast_battery = battery_energy
    max_forecast_battery = battery_energy
    t_min_bat, t_max_bat, t_max_feedin = t_now, t_now, t_now
    detail: list[ForecastEntry] = []

    eff_dis = get(Automation.efficient_discharge, False)
    charge_phases, charge_current = 1, 7
    msg = ""
    next_drive_event = None
    for entry, price in zip(forecast, prices):
        start: datetime = entry.period_start
        if ev_schedule:
            ongoing_drive = next(iter([s for s in ev_schedule if s.start <= start < s.end]), None)
            if ongoing_drive is None:
                next_drive_event = next(iter([s for s in ev_schedule if s.start > start]), None)
                if next_drive_event and next_drive_event.required_soc:
                    ev_required_soc = next_drive_event.required_soc
        td = max(t_now - start, timedelta(minutes=0))
        if t_now > start:
            period_minutes = td.total_seconds() / 60
            if td > timedelta(minutes=full_period_minutes):
                continue
        else:
            period_minutes = full_period_minutes
        period_hours = period_minutes / 60

        power_production: float = entry.pv_estimate * forecast_dampening * 1000
        house_power = daily_power if 7 < start.hour < 19 else nightly_power

        if next_drive_event:
            smart_charge_limit = _get_ev_smart_charge_limit(next_drive_event.start, start, active_schedule=False)

        ev_energy_needed = _get_ev_energy_needed(ev_required_soc, ev_soc, smart_charge_limit, smart_limiter_active)

        could_charge_ev = ev_energy_needed > 0 and is_charging_possible(start, ev_energy, smart_charge_limit)

        new_battery_energy = min(max(0, battery_energy + accumulated_energy), battery_capacity)
        new_battery_soc = new_battery_energy / battery_capacity * 100

        battery_target_soc = max(5, new_battery_soc - (surplus / battery_capacity * 100))

        electricity_price = get_price(hour=start.hour, minute=start.minute)
        low_price = is_low_price(electricity_price)

        inverter_mode, charge_power_limit, is_force_charging, inverter_mode_reason = get_inverter_mode(
            power_production, battery_target_soc, new_battery_soc, electricity_price
        )

        excess_target = _get_excess_target(
            battery_target_soc=battery_target_soc,
            battery_soc=new_battery_soc,
            ev_required_soc=ev_required_soc,
            ev_is_charging=is_charging_ev,
            next_planned_drive=next_drive_event.start if next_drive_event else None,
            pv_power=power_production,
            ev_soc=ev_soc,
            t_now=start,
            efficient_discharge=eff_dis,
        )

        if could_charge_ev:
            charge_action, new_charge_phases, charge_current, reason = _get_charge_action(
                next_drive=next_drive_event.start if next_drive_event else None,
                current_soc=ev_soc,
                required_soc=ev_required_soc,
                energy_needed=ev_energy_needed,
                excess_power=power_production - house_power,
                excess_target=excess_target,
                surplus_energy=surplus,
                smart_charge_limit=smart_charge_limit,
                smart_limiter_active=smart_limiter_active,
                configured_phases=charge_phases,
                configured_current=charge_current,
                is_low_price=low_price,  # TODO
                pv_total_power=power_production,
                battery_soc=new_battery_soc,
                is_charging=is_charging_ev,
                t_now=start,
            )
            if new_charge_phases != charge_phases:
                charge_current = 8 if new_charge_phases == 1 else 6

            charge_phases = new_charge_phases

            is_charging_ev = charge_action == ChargeAction.on
            # if is_charging:
            #     log.warning(
            #         f"{start.day} {start.hour}:{start.minute:02d} - EV charge action {charge_action} phases {charge_phases} current {charge_current} due to {reason} "
            #         f"(needed {ev_energy_needed:.1f} kWh, excess {power_production - house_power:.0f}W, target_excess {excess_target:.0f}W, surplus {surplus:.1f}kWh, "
            #         f"smart_limit {smart_charge_limit:.1f}kWh, ev_soc {ev_soc:.1f}%, req_soc {ev_required_soc}%, battery_soc {new_battery_soc:.1f}%)"
            #     )

        else:
            is_charging_ev, charge_phases, charge_current = False, 1, 6

        this_setpoint = map_setpoint(
            setpoint,
            price,
            prices_mean,
            prices_std,
            battery_energy=new_battery_energy,
            battery_min_limit=battery_min_energy,
            pv_power=power_production,
            house_power=house_power,
            setpoint_spread=setpoint_spread,
            max_setpoint=max_setpoint,
            max_battery_power_target=max_battery_power_target,
        )

        ev_charge_power = charge_phases * charge_current * 230  # W
        if is_charging_ev and ev_charge_power > 1:
            ev_energy = min(smart_charge_limit, ev_energy + ev_charge_power * period_hours / 1000)
            free_capacity = smart_charge_limit - ev_energy + battery_capacity - battery_energy
            this_setpoint = -20
            ev_soc = ev_energy / EVConst.ev_capacity * 100
        else:
            free_capacity = battery_capacity - battery_energy
            ev_charge_power = 0

        surplus -= ev_charge_power * period_hours / 1000

        if ongoing_drive and ongoing_drive.distance:
            total_required = ongoing_drive.distance / 100 * EVConst.kwh_per_100km
            ev_energy -= (
                total_required / max((ongoing_drive.end - ongoing_drive.start).total_seconds() / 3600, 1) * period_hours
            )

        # assume ev energy is never completely depleted
        if ev_energy is not None:
            ev_energy = max(5, ev_energy)

        power_draw = house_power - this_setpoint + ev_charge_power
        energy_use = power_draw * period_hours / 1000  # kWh per forecast period
        energy_production = power_production * (period_hours) / 1000  # kWh
        net_energy = energy_production - energy_use

        battery_full = battery_energy + accumulated_energy + net_energy >= battery_capacity

        if battery_full and net_energy > 0:
            remaining_battery_energy = battery_capacity - (battery_energy + accumulated_energy)
            max_battery_power = min(battery_charge_limit, remaining_battery_energy / period_hours * 1000)
        else:
            max_battery_power = min(battery_charge_limit, max_battery_power_target)

        max_intake_energy = max_battery_power / 1000 * period_hours
        added_battery_energy = min(max_intake_energy, net_energy)
        accumulated_energy += added_battery_energy
        new_battery_energy = max(0, min(battery_capacity, battery_energy + accumulated_energy))

        feedin = (net_energy - added_battery_energy) * 1000 / period_hours
        battery_power = min(max_battery_power, net_energy / period_hours * 1000 - feedin)

        battery_empty = battery_energy + accumulated_energy <= 1

        if (battery_empty or inverter_mode in (InverterMode.off, InverterMode.charger_only)) and battery_power < 0:
            power_from_grid = -battery_power
            battery_power = 0
        else:
            power_from_grid = max(0, power_draw - power_production)

        if inverter_mode in (InverterMode.on, InverterMode.charger_only) and is_force_charging:
            battery_power = charge_power_limit
            power_from_grid = charge_power_limit + power_draw - power_production

        if logging:
            msg += (
                f"\n{start.hour}:{start.minute:02d} - batt {battery_power:.0f}W {new_battery_energy:.0f}kWh setpoint {setpoint:.0f} feedin {feedin:.0f} pv_power {power_production:.0f} "
                f"ev_charge_power {ev_charge_power:.0f}W power_draw {power_draw:.0f}W house_power {house_power:.0f}W "
                f"battery_power {battery_power:.0f}W"
            )

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
                period_start=start,
                pv_estimate=power_production,
                battery_energy=new_battery_energy,
                battery_power=battery_power,
                house_power=house_power,
                setpoint=this_setpoint,
                power_draw=power_draw,
                energy_use=energy_use,
                energy_production=energy_production,
                free_capacity=free_capacity,
                accumulated_energy=accumulated_energy,
                feedin=feedin,
                price=price,
                setpoint_spread=setpoint_spread,
                ev_energy=ev_energy,
                ev_charge_power=ev_charge_power,
                excess_target=excess_target,
                surplus=surplus,
                power_from_grid=power_from_grid,
            )
        )
        task.sleep(0.0001)  # yield to other tasks

    if logging:
        log.warning("\n" + msg)

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
        max_battery_power_target=max_battery_power_target,
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
    if len(forecast) == 0:
        log.warning("No forecast data available")
        return []

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
            start_time,
            pv_estimate=forecast_entry["pv_estimate"],
            price_per_kwh=prices.get(get_date_tuple(start_time)),
        )
        if forecast[idx].price_per_kwh is None:
            log.warning(f"No price found for forecast entry {get_date_tuple(start_time)}")

    return forecast


@time_trigger("period(now, 120sec)")
@state_trigger(f"{Grid.max_feedin_target} or {Grid.max_pv_feedin_target} or {Automation.auto_setpoint}")
def auto_setpoint_target():
    task.unique("auto setpoint target", kill_me=True)
    t_now = now()
    setpoint = 0
    logging = False
    forecast_hours = 24

    max_feedin_limit = get(Grid.max_feedin_target, 4000)
    max_pv_feedin = get(Grid.max_pv_feedin_target, 4000)
    max_setpoint = get(Grid.max_setpoint, -20)

    forecast_dampening = 0.9  # dampen the forecast to account for inaccuracies
    battery_energy = get(Battery.energy, 0)
    min_feedin_price = 0

    house_avg_power = get(House.daily_average_power, 0)  # W

    battery_capacity = get(Battery.capacity, 1337)

    if battery_capacity == 1337:
        log.error("Battery capacity not available yet, cannot calculate setpoint")
        return

    battery_min_energy = 0.1 * battery_capacity  # 10% of battery capacity
    if logging:
        log.warning(f"battery capacity: {battery_capacity} min energy: {battery_min_energy}")

    ev_schedule = (schedule.get_schedule(entity_id="schedule.tesla_planned_drives") or {}).get(
        "schedule.tesla_planned_drives", {}
    )
    # example:
    # data = {'monday': [], 'tuesday': [], 'wednesday': [{'from': datetime.time(7, 30), 'to': datetime.time(17, 0)}], 'thursday': [], 'friday': [], 'saturday': [], 'sunday': [{'from': datetime.time(9, 30), 'to': datetime.time(10, 0), 'data': {'required_charge': 50}}]}
    ev_required_soc = get(EV.required_soc, 80)
    ev_schedule = parse_full_schedule(ev_schedule, default_required_soc=ev_required_soc)
    ev_energy = get(EV.energy, EVConst.ev_capacity)
    current_setpoint = get(Grid.power_setpoint_target, max_setpoint)

    def forecast_setpoint_local(
        forecast,
        setpoint,
        setpoint_spread=0.1,
        current_battery_energy=0,
        t_start: datetime | None = None,
        t_end: datetime | None = None,
        with_ev_charging=True,
        ev_energy: float | None = None,
        max_battery_power_target: float = 4000,
        logging=logging,
        ev_schedule=ev_schedule,
    ):
        if t_start is not None:
            forecast = [entry for entry in forecast if t_start < entry.period_start]
        elif t_end is not None:
            forecast = [entry for entry in forecast if entry.period_start < t_end]
        result = forecast_setpoint(
            forecast,
            setpoint=setpoint,
            battery_capacity=battery_capacity,
            min_feedin_price=min_feedin_price,
            forecast_dampening=forecast_dampening,
            battery_energy=current_battery_energy,
            setpoint_spread=setpoint_spread,
            battery_min_energy=battery_min_energy,
            with_ev_charging=with_ev_charging,
            ev_energy=ev_energy,
            max_battery_power_target=max_battery_power_target,
            max_pv_feedin_target=max_pv_feedin,
            max_setpoint=max_setpoint,
            logging=logging,
            ev_schedule=ev_schedule,
        )
        task.sleep(0.01)  # sleep to allow other tasks to run
        return result

    # Binary search for optimal setpoint
    current_battery_energy = battery_energy
    epex_prices = get_attr(ElectricityPrices.epex_forecast_prices, "data", [])

    if not epex_prices:
        log.warning("Unable to forecast setpoint, no EPEX prices available")
        return

    pv_power_total = get(PVProduction.total_power, 0)

    epex_pv_forecast = get_pv_forecast_with_prices(
        t_start=t_now, t_end=t_now + timedelta(hours=forecast_hours), epex_prices=epex_prices
    )

    skip_automation_message = ""
    if automation_disabled := get(Automation.auto_setpoint, False) is False:
        skip_automation_message = "Auto setpoint is disabled"

    initial_forecast = forecast_setpoint_local(
        forecast=epex_pv_forecast,
        setpoint=current_setpoint if automation_disabled else max_setpoint,
        setpoint_spread=0.05,
        current_battery_energy=current_battery_energy,
        with_ev_charging=True,
        ev_energy=ev_energy,
        max_battery_power_target=8000,  # TODO: make this configurable
    )

    if not automation_disabled and initial_forecast.max_feedin == 0:
        skip_automation_message = "No significant feedin expected"

    if skip_automation_message:
        log.warning(f"{skip_automation_message}")

        set_state(
            Grid.power_setpoint_target,
            initial_forecast.setpoint,
            **power_w_attributes,
            detail=initial_forecast.detail,
        )
        return

    def setpoint_binary_search(
        forecast,
        min_setpoint=-max_feedin_limit,
        max_setpoint=max_setpoint,
        with_ev_charging=True,
        battery_energy=current_battery_energy,
        ev_energy=None,
        max_iters=10,
        max_battery_power_target: float = 8000,
        log_setpoint=False,
        current_setpoint=None,
        current_spread=None,
        min_spread=1e-2,
        max_spread=5,
        update_setpoint=True,
        update_spread=False,
    ):
        search_results = []
        assert update_setpoint or update_spread
        if not update_spread:
            assert current_spread is not None
        if not update_setpoint:
            assert current_setpoint is not None

        for itr in range(max_iters):
            if update_setpoint:
                current_setpoint = (min_setpoint + max_setpoint) // 2
            if update_spread:
                current_spread = (min_spread + max_spread) / 2

            if log_setpoint and logging:
                log.warning(f"mid setpoint: {current_setpoint} mid spread {current_spread}")

            r = forecast_setpoint_local(
                forecast,
                current_setpoint,
                current_spread,
                battery_energy,
                with_ev_charging=with_ev_charging,
                ev_energy=ev_energy,
                max_battery_power_target=max_battery_power_target,
            )
            search_results.append(r)

            if (update_spread and abs(max_spread - min_spread) < 1e-3) or (
                update_setpoint and abs(max_setpoint - min_setpoint) < 50
            ):
                break

            if r.min_bat < battery_min_energy + 0.1:
                # If battery is too low, we need a more positive setpoint, lower spread
                if update_setpoint:
                    min_setpoint = current_setpoint
                if update_spread:
                    # setpoint_spread /= spread_update_factor
                    max_spread = current_spread
                # log.warning(
                #     f"{itr}: min_bat {r.min_bat:.1f} < {battery_min_energy:.1f} - setting min_setpoint to {mid_setpoint:.1f}"
                # )
            elif r.max_feedin > max_pv_feedin:
                # If feed-in limit is exceeded, we need a more negative setpoint, higher spread
                if update_setpoint:
                    max_setpoint = current_setpoint
                if update_spread:
                    # setpoint_spread *= spread_update_factor
                    min_spread = current_spread
                # log.warning(
                #     f"{itr}: max_feedin {r.max_feedin:.1f} > {max_pv_feedin / 2:.1f} - setting max_setpoint to {mid_setpoint:.1f}"
                # )
            elif update_spread and r.max_bat < battery_capacity:
                max_spread = current_spread

        return search_results

    def format_setpoint_results(search_results, title):
        # Print setpoint results in tablular format (without forecast details)
        lines = []

        def ft(t):
            return t.strftime("%d %H:%M")

        def fi(k):
            return f"{k:.0f}"

        lines = [
            f"{r.setpoint:2.0f} {' ' * 4}  {r.setpoint_spread:8.6f}{r.min_bat:9.1f}{'':6s}{ft(r.t_min_bat):10s}{r.max_bat:5.1f}{'':6s}{ft(r.t_max_bat):12s}{fi(r.max_feedin):12s}{ft(r.t_max_feedin):10s}"
            for r in search_results
        ]
        return (
            f"Setpoint results {title}:\n{'setpoint':<11s}{'spread':<13s}{'min_bat':<10s}{'t_min_bat':<11s}{'max_bat':<11s}{'t_max_bat':<11s}{'max_feedin':<11s}{'t_max_feedin':<10s}\n"
            + "\n".join(lines)
        )

    search_results = setpoint_binary_search(
        epex_pv_forecast,
        min_setpoint=-max_feedin_limit,
        max_setpoint=max_setpoint,
        current_spread=0.1,
        # setpoint_spread=0.25,
        # spread_update_factor=1.5,
    )

    if logging:
        log.warning(format_setpoint_results(search_results, "initial search"))

    assert search_results[-1].setpoint_spread is not None

    search_results += setpoint_binary_search(
        epex_pv_forecast,
        current_setpoint=search_results[-1].setpoint,
        update_spread=True,
        update_setpoint=False,
        log_setpoint=True,
    )

    initial_result = search_results[-1]

    assert initial_result.setpoint_spread is not None
    forecast_setpoint_local(
        epex_pv_forecast,
        setpoint=initial_result.setpoint,
        setpoint_spread=initial_result.setpoint_spread,
        current_battery_energy=battery_energy,
        with_ev_charging=True,
        ev_energy=ev_energy,
        max_battery_power_target=8000,
        logging=False,
    )

    max_feedin_today = max([d.feedin for d in initial_result.detail if d.period_start.day == t_now.day])
    log.warning(
        f" \n\ninitial_result.max_feedin {initial_result.max_feedin:.0f} > max_pv_feedin {max_pv_feedin}: {initial_result.max_feedin > max_pv_feedin}\n"
        f" max feedin today > max pv feedin: {max_feedin_today} > {max_pv_feedin}: {max_feedin_today > max_pv_feedin}\n\n"
    )
    if max_feedin_today > max_pv_feedin:
        t_start = max(t_now, t_now.replace(hour=8))

        t_end = next(
            iter(
                [
                    e.period_start
                    for e in epex_pv_forecast
                    if e.period_start > t_start
                    and e.period_start.hour > 14
                    and (e.pv_estimate * 1000) < (max_feedin_limit / 2 + house_avg_power)
                ]
            ),
            t_start + timedelta(hours=8),
        )
        # log tstart and tend
        log.warning(
            f" \n\nSearching for feedin setpoint at {t_start.strftime('%m-%d between %H:%M')} and {t_end.strftime('%H:%M')}\n"
            + ", ".join(
                [
                    f"{e.period_start.strftime('%H:%M')}: {e.pv_estimate:.1f}k"
                    for e in epex_pv_forecast
                    if t_start < e.period_start <= t_end
                ]
            )
            + "\n\n!!!!!\n"
        )

        start_detail = next(iter([e for e in initial_result.detail if e.period_start >= t_start]), None)
        price_forecast = [el for el in epex_pv_forecast if t_start < el.period_start <= t_end]
        search_results = setpoint_binary_search(
            price_forecast,
            min_spread=1e-5,
            max_spread=10,
            with_ev_charging=True,
            battery_energy=start_detail.battery_energy,
            ev_energy=start_detail.ev_energy,
            current_setpoint=initial_result.setpoint,
            update_spread=True,
            update_setpoint=False,
        )

        log.warning(format_setpoint_results(search_results, "update spread"))
        new_result = search_results[-1]

        if logging:
            log.warning(
                f" \n\nnew_result.max_feedin {new_result.max_feedin:0f} > max_pv_feedin {max_pv_feedin:0f}: {new_result.max_feedin > max_pv_feedin}\n\n!!"
            )
        msg = ""
        while new_result.max_feedin > max_pv_feedin and new_result.max_battery_power_target > 100:
            new_result = forecast_setpoint_local(
                price_forecast,
                setpoint=new_result.setpoint,
                setpoint_spread=new_result.setpoint_spread,
                current_battery_energy=start_detail.battery_energy,
                t_start=t_start,
                ev_energy=start_detail.ev_energy,
                with_ev_charging=True,
                max_battery_power_target=round(new_result.max_battery_power_target * 0.7),
            )

            msg += (
                f"\nupdated max battery power target: {new_result.max_battery_power_target:.1f} W, "
                f"setpoint: {new_result.setpoint:.1f} W, max_feedin: {new_result.max_feedin:.1f} W"
            )

        if logging:
            log.warning(msg)

        # if the end time is before the time limit, need to forecast again for the remaining time
        rest_forecast = [el for el in epex_pv_forecast if el.period_start > t_end]

        if rest_forecast:
            rest_result = setpoint_binary_search(
                rest_forecast,
                min_setpoint=-max_feedin_limit,
                max_setpoint=max_setpoint,
                # setpoint_spread=0.1,
                # spread_update_factor=1.2,
                with_ev_charging=True,
                battery_energy=new_result.detail[-1].battery_energy,
                ev_energy=new_result.detail[-1].ev_energy,
                current_spread=initial_result.setpoint_spread,
                # log_setpoint=True
            )[-1]

            final_result = merge_setpoint_results(
                new_result,
                rest_result,
                t_split=t_end,
            )

        if t_start > t_now:
            final_result = merge_setpoint_results(
                initial_result,
                new_result,
                t_split=t_start,
            )
            final_result.max_battery_power_target = new_result.max_battery_power_target

        search_results.append(final_result)

    log.warning(format_setpoint_results(search_results, "final result"))

    setpoint_result = search_results[-1]

    price = max(min_feedin_price, get(ElectricityPrices.epex_forecast_prices, min_feedin_price))

    pv_power_total = get(PVProduction.total_power, 0)
    house_power = get(House.loads, 0)

    setpoint = map_setpoint(
        setpoint_result.setpoint,
        price,
        setpoint_result.prices_mean,
        setpoint_result.prices_std,
        battery_energy=battery_energy,
        battery_min_limit=battery_min_energy,
        pv_power=pv_power_total,
        house_power=house_power,
        setpoint_spread=setpoint_result.setpoint_spread,
        max_battery_power_target=setpoint_result.max_battery_power_target,
    )

    if logging:
        log.warning(
            f"\nMapped setpoint: {setpoint_result.setpoint:.0f} to {setpoint} with spread {setpoint_result.setpoint_spread:.2f} and max batt power {setpoint_result.max_battery_power_target} W\n"
            f"price now {price:.2f} mean {setpoint_result.prices_mean:.2f} price std {setpoint_result.prices_std:.2f} "
            f"min_bat {setpoint_result.min_bat:.1f} at {setpoint_result.t_min_bat.strftime('%H:%M')} "
        )

    set_state(
        Grid.power_setpoint_target,
        setpoint,
        **power_w_attributes,
        detail=setpoint_result.detail,
    )
