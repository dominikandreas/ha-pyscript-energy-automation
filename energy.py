# ruff: noqa: I001

from datetime import datetime, timedelta
from math import pi, sin
from typing import TYPE_CHECKING

# NODE: many functions have two @time_trigger decorators. this is not redundant, the first one
# without parameter triggers at function reload

if TYPE_CHECKING:
    # The type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    # Therefore during type checking we pretend to import them from modules.utils, which it can resolve.
    from modules.utils import clip, get, get_attr, set, service

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

    from modules.states import Automation, Battery, EV, Excess, House, PVProduction, Grid, ElectricityPrices, Charger
    from modules.victron import Victron

else:
    from utils import clip, get, get_attr, now, set, with_timezone
    from states import Automation, Battery, EV, Excess, House, PVProduction, Grid, ElectricityPrices, Charger
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
    ongoing_drive = False

    if next_event is not None:
        next_event = with_timezone(next_event)
        td = next_event - t_now

        if td < timedelta(hours=24 * 4):
            ongoing_drive = get(EV.planned_drives, False)

    else:
        ongoing_drive = False
        td = None

    a = (t_now.month - 6) / 6
    usual_consumption_rate = a * 0.6 + (1 - a) * 0.85
    
    
    required_charge = max(0, (ev_required_soc - ev_current_soc) / 100) * Const.ev_capacity

    if ongoing_drive:
        expected_consumption = usual_consumption_rate * ev_required_soc
    else:
        expected_consumption = 0

    energy_to_wash = 2
    days_between_washes = 7

    t_since_washing = t_now - datetime.fromisoformat(get(House.last_washing)).astimezone()
    days_since_washing_machine_ran = t_since_washing.days + t_since_washing.seconds / 3600 / 24

    p_washing = max(0, min(1, days_since_washing_machine_ran / days_between_washes))
    washing_energy = p_washing * energy_to_wash

    log.warning(f"ongoing drive {ongoing_drive} required charge {required_charge:.0f} expected consumption {expected_consumption} wash {washing_energy}")
    ev_energy = required_charge + expected_consumption
 
 
    set(
        House.upcoming_demand,
        round(ev_energy + washing_energy, 2),
        **energy_kwh_attributes,
        icon="mdi:home-lightning", # added icon
        friendly_name="Upcoming Demand", # more descriptive friendly name
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
        log.warning(f"Enabling force charge switch and setting charge limit, {battery_soc} < {target_soc}, < {charge_limit}")
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
@state_trigger(f"{Charger.control_switch} != 'undefined'")
@time_trigger("cron(*/1 * * * *)")
def auto_battery_target_soc():
    battery_soc = get(Battery.soc, default=50)
    reserve_soc = get_reserve_soc()

    house_energy_demand = get(House.energy_demand, default=10)
    pv_upcoming = get(PVProduction.energy_until_production_meets_demand, default=0)
    excess_next_days = get(Excess.excess_next_three_days, default=0)
    battery_capacity = get(Battery.capacity, default=8)
    battery_cells_balanced = get(Battery.cells_balanced, False)
    surplus = get(House.energy_surplus, 0)

    ev_is_charging = get(EV.is_charging, False)

    req_energy = max(
        0,
        house_energy_demand - pv_upcoming,
        min(0, excess_next_days),
    )

    set(Automation.req_energy, round(req_energy, 2), **energy_kwh_attributes)

    max_soc = 95 if battery_cells_balanced else 100

    minimal_soc = min(max_soc, (max(0, req_energy) / battery_capacity * 100) + reserve_soc)
    set(Automation.minimal_soc, round(minimal_soc, 2), unit_of_measurement="%") # different attributes

    # prevent discharging of the battery if the EV is charging and insufficient surplus
    result_soc = max(battery_soc + 1, minimal_soc) if ev_is_charging and surplus < 1 else minimal_soc

    if not get(Automation.auto_battery_target_soc, False):
        return False

    print(f"auto battery target soc: {result_soc}")
    set(
        Automation.battery_target_soc,
        round(result_soc, 2),
        unit_of_measurement="%", # different attributes
    )


@time_trigger("cron(*/1 * * * *)")
async def auto_excess_target():
    if not get(Automation.auto_excess_target, False):
        return

    battery_target_soc = get(Automation.battery_target_soc, default=0)
    battery_soc = get(Battery.soc, default=0)

    power_abs_max = 5000
    soc_difference = (battery_target_soc - battery_soc) / 100
    normalized_difference = soc_difference * 2 * pi
    normalized_difference_clipped = clip(normalized_difference, -pi / 2, pi / 2)
    power = sin(normalized_difference_clipped) * power_abs_max
    power = clip(power, -power_abs_max, power_abs_max)

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
    excess_today_remaining = get(Excess.excess_today_remaining, default=0)
    excess_tomorrow = get(Excess.energy_next_day, default=0)
    excess_two_days = get(Excess.energy_two_days, default=0)
    excess_three_days = get(Excess.excess_next_three_days, default=0)

    t_now = now()
    a = (abs(t_now.month - 6) / 6)**3
    surplus_energy_target = a * 10 + (1-a) * 5
    demand_per_day = 4 # extra kWh load that might occur

    remaining_today = battery_energy + excess_today_remaining
    remaining_tomorrow = excess_tomorrow - demand_per_day
    remaining_day_after_tomorrow = excess_two_days - 2 * demand_per_day
    remaining_next_three_days = excess_three_days - 3 * demand_per_day

    result = min(
        max(0, battery_energy - surplus_energy_target),
        max(0, remaining_today - surplus_energy_target),
        max(0, remaining_tomorrow - surplus_energy_target),
        max(0, remaining_day_after_tomorrow - surplus_energy_target),
        max(0, remaining_next_three_days - surplus_energy_target),
    )

    log.warning(
        f"Energy surplus: {result:.1f} kWh"
        f"\n\t a {a:.2f} surplus energy target {surplus_energy_target:.1f}"
        f"\n\t battery_energy {battery_energy:.1f} - surplus_energy_target = {battery_energy - surplus_energy_target:.1f} "
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
