# ruff: noqa: I001


from dataclasses import dataclass, replace
from datetime import datetime, timedelta, date
from math import pi, exp, sqrt
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
        hours = required_for_empty / abs(power)
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
    night_avg_power = get(House.nightly_average_power, default=0)
    day_avg_power = get(House.daily_average_power, default=0)

    next_pv_meet_demand = get(PVProduction.next_meet_demand)
    if not next_pv_meet_demand or next_pv_meet_demand == "unknown":
        return

    next_pv_meet_demand = with_timezone(next_pv_meet_demand)
    t_now = now()
    dt = next_pv_meet_demand - t_now

    total_energy = 0
    # to ensure we never hit an infinity loop for whatever reason
    iters, max_iters = (0, 240)
    while dt > timedelta(hours=0) and iters < max_iters:
        if 7 < (t_now + dt).hour < 19:
            total_energy += day_avg_power / 1000
        else:
            total_energy += night_avg_power / 1000

        dt = dt - timedelta(hours=1)
        iters += 1

    log.warning(
        f"House energy until production meets demand: daily avg {day_avg_power}W, nightly avg {night_avg_power}W, "
        f"next meet demand at {next_pv_meet_demand}, "
        f"total required=>{total_energy:.2f} kWh"
    )

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


# @time_trigger
# def set_input_boolean_auto_set_inverter_mode():
#     current_mode = get(Victron.auto_set_inverter_mode, "off")
#     set_state(Victron.auto_set_inverter_mode, current_mode, friendly_name="Auto Victron Inverter Mode")


@time_trigger
@state_trigger(Automation.min_discharge_price)
@state_trigger(ElectricityPrices.current_price)
@state_trigger(EV.is_charging)
@time_trigger("period(now, 300sec)")
def auto_victron_set_inverter_mode():
    # Get the current time and electricity price
    if not get(Victron.auto_set_inverter_mode, False):
        log.warning("Auto Victron inverter mode is disabled, skipping")
        return

    electricity_price = float(get(ElectricityPrices.current_price, default=0))
    min_discharge_price = float(get(Automation.min_discharge_price, default=0))
    ev_is_charging = get(EV.is_charging, False)
    surplus_energy = get(House.energy_surplus, -1337)
    forecast_battery_headroom_energy = get(House.battery_headroom_until_trough, -1337)
    battery_soc = get(Battery.soc, -1337)
    target_soc = get(Automation.battery_target_soc, -1337)
    pv_power = get(PVProduction.total_power, -1337)  # in kW
    daily_avg_power = get(House.daily_average_power, -1337)
    charge_limit_percent = get(Battery.force_charge_up_to, 0)
    max_charge_price = get(Battery.max_charge_price, 0)
    force_charge_switch = get(Battery.force_charge_switch, False)
    current_mode = get(Victron.inverter_mode_input_select)
    battery_capacity = get(Battery.capacity, -1337)
    battery_energy = get(Battery.energy, -1337)
    reserve_soc = get_reserve_soc()
    minimal_soc = get(Automation.minimal_soc, reserve_soc)
    t_now = now()

    if battery_capacity != -1337 and battery_energy != -1337:
        battery_export_floor = get_battery_export_floor(battery_capacity, reserve_soc, minimal_soc)
        battery_headroom_energy = max(0, battery_energy - battery_export_floor)
    else:
        battery_headroom_energy = forecast_battery_headroom_energy

    if pv_power == -1337:
        log.warning("Total PV power state not available, using forecast instead")
        pv_power = get(PVForecast.power_now, -1337)

    for v in (surplus_energy, battery_headroom_energy, battery_soc, target_soc, pv_power, daily_avg_power):
        if v == -1337:
            log.error(
                f"Not all required states are available yet: surplus_energy: {surplus_energy}, "
                f"battery_headroom_energy: {battery_headroom_energy}, battery_soc: {battery_soc}, "
                f"target_soc: {target_soc}, pv_power: {pv_power}, daily_avg_power: {daily_avg_power}, skipping auto victron inverter mode"
            )
            return

    new_mode, new_charge_power_limit, new_force_charge_switch_state, reason = get_auto_inverter_mode(
        ev_is_charging,
        surplus_energy,
        battery_headroom_energy,
        pv_power,
        daily_avg_power,
        battery_soc,
        target_soc,
        electricity_price,
        min_discharge_price,
        max_charge_price,
        charge_limit_percent,
        force_charge_switch,
        t_now=t_now,
    )

    # log.warning(
    #     f"Auto Victron inverter mode: {new_mode} {reason} pv power {pv_power}W daily avg {daily_avg_power}W battery soc {battery_soc}% target soc {target_soc}% surplus energy {surplus_energy}kWh target soc {target_soc}%"
    # )

    if new_charge_power_limit is not None:
        set_state(Battery.charge_limit, new_charge_power_limit)

    if new_force_charge_switch_state is not None:
        set_state(Battery.force_charge_switch, new_force_charge_switch_state)

    new_mode = Victron.PAYLOAD_TO_MODE.get(new_mode)
    if current_mode != new_mode:
        log.warning(
            f"{current_mode} -> {new_mode}: {reason}. "
            f"ev: {ev_is_charging}, surplus: {surplus_energy}, headroom: {battery_headroom_energy}, "
            f"soc: {battery_soc}, target soc: {target_soc}, "
            f"pv_power: {pv_power}, daily_avg_power {daily_avg_power}"
        )

        last_changed = get_attr(Victron.inverter_mode_input_select, "last_changed")
        if new_mode == InverterMode.on and (now() - last_changed).total_seconds() / 60 < 30:
            log.warning("Skipping Victron inverter mode change since last change was less than 30 minutes ago")
            return

    set_state(Victron.inverter_mode_input_select, new_mode)


@time_trigger
@time_trigger("cron(*/3 * * * *)")
def battery_use_until_pv_meets_demand():
    next_pv_meet_demand = get(PVProduction.next_meet_demand, None)
    if next_pv_meet_demand is None:
        return
    next_pv_meet_demand = with_timezone(next_pv_meet_demand)

    energy_until_pv_meets_demand = get(PVProduction.energy_until_production_meets_demand, 0)
    discharge_price = get(Automation.min_discharge_price, 100)
    daily_avg_power = get(House.daily_average_power, 0)
    night_avg_power = get(House.nightly_average_power, 0)
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
    summer_deviation = ((6 - (t_now.month - 1 + t_now.day / 30.0)) / 6) ** 4  # ranging from 0 to 1

    return max(min_reserve, round(summer_deviation * 30, 0))  # ranging from 5 to 30 (max 30% reserve during winter)


@pyscript_compile
def get_battery_export_floor(
    battery_capacity: float,
    reserve_soc: float,
    minimal_soc: float,
    uncertainty_margin_kwh: float = 1,
) -> float:
    """Energy that must remain available for house demand and battery reserve."""
    protected_soc = max(reserve_soc, minimal_soc)
    return min(battery_capacity, protected_soc / 100 * battery_capacity + uncertainty_margin_kwh)


@pyscript_compile
def get_pv_only_setpoint(
    pv_power: float,
    house_power: float,
    max_battery_charge_power: float,
    neutral_setpoint: float = -20,
) -> float:
    """Most negative setpoint that does not require battery-to-grid export."""
    unavoidable_pv_export = max(0, pv_power - house_power - max_battery_charge_power)
    return min(neutral_setpoint, -unavoidable_pv_export)


@pyscript_compile
def get_low_pv_reserve_soc(house_power: float, pv_power: float, battery_cells_balanced: bool) -> float:
    if battery_cells_balanced:
        return 0
    if house_power <= 0:
        return 0
    return 15 if pv_power < house_power * 0.75 else 0


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
    if not get(Automation.auto_battery_target_soc, False):
        return
    battery_soc = get(Battery.soc, default=50)
    base_reserve_soc = get_reserve_soc()

    house_demand = get(House.energy_demand, default=10)
    pv_upcoming = get(PVProduction.energy_until_production_meets_demand, default=0)
    excess_next_days = get(Excess.excess_next_three_days, default=0)
    house_power = get(House.daily_average_power, default=0)
    battery_capacity = get(Battery.capacity, default=8)
    battery_cells_balanced = get(Battery.cells_balanced, False)
    battery_energy = get(Battery.energy, default=0)
    surplus = get(House.energy_surplus, 0)
    surplus_detail = get_attr(House.energy_forecast, "detail")
    pv_power = get(PVProduction.total_power, default=0)

    if pv_power == 0:
        pv_power = get(PVForecast.power_now, default=0)

    reserve_soc_floor = max(
        base_reserve_soc,
        get_low_pv_reserve_soc(
            house_power=house_power, pv_power=pv_power, battery_cells_balanced=battery_cells_balanced
        ),
    )

    if not surplus_detail:
        return

    ev_is_charging = get(EV.is_charging, False)
    protect_battery_from_ev = ev_is_charging and surplus <= 0

    req_energy = get_required_energy(
        house_demand,
        pv_upcoming,
        excess_next_days,
        battery_capacity,
        battery_energy,
        surplus,
    )

    msg = (
        f"\nAuto battery target soc calculation: \n"
        f"\n\thouse_demand: {house_demand:.2f}kWh, pv_upcoming: {pv_upcoming:.2f}kWh, reserve_soc: {reserve_soc_floor:.2f}%, "
        f"\n\texcess_next_days: {excess_next_days:.2f}kWh, battery_capacity: {battery_capacity:.2f}kWh, battery_energy: {battery_energy:.2f}kWh, surplus: {surplus:.2f}kWh => req_energy: {req_energy:.2f}kWh"
    )

    set_state(Automation.req_energy, round(req_energy, 2), **energy_kwh_attributes)

    max_soc = 80 if battery_cells_balanced else 100

    reserve_soc = max(reserve_soc_floor, base_reserve_soc - (house_demand - pv_upcoming))

    minimal_soc = min(max_soc, (max(0, req_energy) / battery_capacity * 100) + reserve_soc)
    set_state(Automation.minimal_soc, round(minimal_soc, 2), unit_of_measurement="%")  # different attributes

    # Prevent EV charging from pulling protected battery energy. If the EV is
    # charging from a positive surplus budget, do not raise the battery target:
    # that target jump fights the charger and creates on/off oscillation.
    result_soc = max(battery_soc + 1, minimal_soc) if protect_battery_from_ev else minimal_soc

    msg += f"\n\tminimal soc {minimal_soc:.1f} auto battery target soc: {result_soc:.1f}, protect_battery_from_ev: {protect_battery_from_ev}"
    set_state(
        Automation.battery_target_soc,
        round(result_soc, 2),
        min=0,
        max=100,
        unit_of_measurement="%",  # different attributes
    )

    log.warning(msg)


@pyscript_compile
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

    from math import sin

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
        min=-8000,
        max=8000,
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


def set_energy_surplus(entity, surplus, **kwargs):
    set_state(
        entity,
        round(surplus, 2),
        **energy_kwh_attributes,
        icon="mdi:home",
        **kwargs,
    )


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

    setpoint = get(Grid.power_setpoint_basis, -20)
    battery_capacity = get(Battery.capacity, 1337)
    battery_energy = get(Battery.energy, 1337)
    current_surplus = get(House.energy_surplus, 0)
    ev_energy = get(EV.energy, 0)
    reserve_soc = get_reserve_soc()
    minimal_soc = get(Automation.minimal_soc, reserve_soc)
    battery_export_floor = get_battery_export_floor(battery_capacity, reserve_soc, minimal_soc)

    if 1337 in (battery_capacity, battery_energy):
        log.warning("Battery capacity or energy not available yet, cannot calculate surplus")
        return

    pv_forecast = get_pv_forecast_with_prices(t_start=t_now, t_end=t_end, epex_prices=epex_prices)
    if not pv_forecast or len(pv_forecast) < 2:
        log.warning("Not enough PV forecast data to calculate surplus")
        return
    period_hours = (pv_forecast[1].period_start - pv_forecast[0].period_start).total_seconds() / 3600

    forecast_no_ev = forecast(
        forecast=pv_forecast,
        battery_capacity=battery_capacity,
        battery_energy=battery_energy,
        setpoint=setpoint,
        forecast_dampening=1,
        with_ev_charging=False,
        battery_min_energy=battery_export_floor,
        # logging=True,
        surplus_energy=current_surplus,
    )

    min_battery_energy = min([el.battery_energy for el in forecast_no_ev.detail] or [0])
    reserve_energy = reserve_soc / 100 * battery_capacity

    next_local_min_battery_energy = battery_energy
    t_next_local_min = t_now
    if forecast_no_ev.detail:
        next_local_min_battery_energy = forecast_no_ev.detail[0].battery_energy
        t_next_local_min = forecast_no_ev.detail[0].period_start
        previous_battery_energy = next_local_min_battery_energy
        for el in forecast_no_ev.detail[1:]:
            if el.battery_energy <= next_local_min_battery_energy:
                next_local_min_battery_energy = el.battery_energy
                t_next_local_min = el.period_start
            elif el.battery_energy > previous_battery_energy + 0.05:
                break
            previous_battery_energy = el.battery_energy

    # Use the same protected floor for optional loads and battery-export
    # headroom so a clamped forecast cannot manufacture exportable energy.
    surplus = max(0, next_local_min_battery_energy - battery_export_floor)
    battery_headroom_surplus = max(0, next_local_min_battery_energy - battery_export_floor)

    scheduled_grid_setpoint_energy_until_trough = sum([
        el.setpoint
        for el in forecast_no_ev.detail
        if t_next_local_min is not None and el.period_start <= t_next_local_min
    ]) * period_hours / 1000

    surplus = max(0, battery_headroom_surplus - scheduled_grid_setpoint_energy_until_trough)
    battery_headroom_until_trough = battery_headroom_surplus

    forecast_today = [el for el in forecast_no_ev.detail if el.period_start.day == t_now.day]
    feedin_today = sum([max(0, el.feedin - 20) for el in forecast_today]) * period_hours / 1000
    pv_feedin_today = sum([max(0, el.pv_feedin - 20) for el in forecast_today]) * period_hours / 1000


    detail_without_ev_vectorized = {
        k: [getattr(el, k) for el in forecast_no_ev.detail] for k in ForecastEntry.__annotations__.keys()
    }

    set_energy_surplus(House.energy_surplus, surplus)
    set_energy_surplus(
        House.battery_headroom_until_trough,
        battery_headroom_until_trough,
        friendly_name="Battery Headroom Until Trough",
    )
    set_state(
        House.energy_forecast,
        round(forecast_no_ev.detail[-1].battery_energy, 2),
        **energy_kwh_attributes,
        detail=detail_without_ev_vectorized,
    )

    ev_schedule = get_ev_schedule()
    if ev_schedule:
        forecast_with_ev = forecast(
            forecast=pv_forecast,
            battery_capacity=battery_capacity,
            battery_energy=battery_energy,
            setpoint=setpoint,
            forecast_dampening=1,
            with_ev_charging=True,
            ev_schedule=ev_schedule,
            battery_min_energy=battery_export_floor,
            # logging=True,
            surplus_energy=surplus,
            ev_energy=ev_energy,
        )

        min_battery_energy = min([el.battery_energy for el in forecast_with_ev.detail] or [0])
        surplus_after_ev_charging = min([el.surplus for el in forecast_with_ev.detail] or [0])
        log.warning(
            f"""\n#################### Forecast Surplus  #####################\n
            Reserve battery energy: {reserve_energy:.2f} kWh ({reserve_soc:.2f}% reserve SOC)
            Next local min battery: {next_local_min_battery_energy:.2f} kWh
            Battery headroom until trough: {battery_headroom_until_trough:.2f} kWh
            Min battery: {min_battery_energy:.2f} kWh
            Final battery energy: {forecast_no_ev.detail[-1].battery_energy:.2f} kWh
            Surplus: {surplus:.2f} kWh
            Surplus after EV charging: {surplus_after_ev_charging:.2f} kWh
            last entry date: {forecast_no_ev.detail[-1].period_start if len(forecast_no_ev.detail) > 0 else "n/a"}
            total feedin: {feedin_today:.2f} kWh
            total pv feedin: {pv_feedin_today:.2f} kWh
            \n#################### Forecast Surplus End #####################
            """
        )
        detail_with_ev_vectorized = {
            k: [getattr(el, k) for el in forecast_with_ev.detail] for k in ForecastEntry.__annotations__.keys()
        }

        set_state(
            House.energy_forecast_with_ev,
            round(forecast_with_ev.detail[-1].battery_energy, 2),
            **energy_kwh_attributes,
            detail=detail_with_ev_vectorized,
        )
        set_energy_surplus(House.energy_surplus_after_ev_charging, surplus_after_ev_charging)

    else:
        set_state(
            House.energy_forecast_with_ev,
            round(forecast_no_ev.detail[-1].battery_energy, 2),
            **energy_kwh_attributes,
            detail=detail_without_ev_vectorized,
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
        free_capacity: float
        accumulated_energy: float
        feedin: float
        pv_feedin: float
        epex_price: float
        electricity_price: float
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
    class ForecastResult:
        setpoint: int
        min_bat: float
        t_min_bat: datetime
        max_bat: float
        t_max_bat: datetime
        max_feedin: float
        t_max_feedin: datetime
        max_pv_feedin: float
        t_max_pv_feedin: datetime
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

    return EVScheduleEntry, ForecastResult, ForecastEntry, PVForecastWithPrices


EVScheduleEntry, ForecastResult, ForecastEntry, PVForecastWithPrices = define_interfaces()


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
    surplus_energy: float | None = None,
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

    surplus_pv = max(0, pv_power - house_power - max_battery_power_target)
    pv_only_setpoint = get_pv_only_setpoint(
        pv_power,
        house_power,
        max_battery_power_target,
        neutral_setpoint=min_setpoint,
    )

    if surplus_pv > 0 and setpoint < max_setpoint:
        new_setpoint = min(new_setpoint, -surplus_pv)

    # Scale only the battery-to-grid portion of the target. At the protected
    # floor, retain a neutral target or unavoidable PV export, but never ask the
    # battery to export. The factor is monotonic and cannot rise below the floor.
    battery_headroom_factor = clip((battery_energy - battery_min_limit) / 2, 0, 1) ** 4
    if surplus_energy is not None:
        battery_headroom_factor = min(battery_headroom_factor, clip(surplus_energy / 2, 0, 1) ** 4)

    if new_setpoint < pv_only_setpoint:
        new_setpoint = pv_only_setpoint + (new_setpoint - pv_only_setpoint) * battery_headroom_factor

    return max(-(max_feedin + surplus_pv), min(min_setpoint, new_setpoint))


def forecast(
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
    daily_power: float | None = None,
    nightly_power: float | None = None,
    ev_required_soc: float | None = None,
    ev_soc: float | None = None,
    is_charging_ev: bool | None = None,
    smart_limiter_active: bool | None = None,
    charge_limit: float | None = None,
    max_charge_price: float | None = None,
    force_charge_switch: bool | None = None,
    min_discharge_price: float | None = None,
    charger_ready: bool | None = None,
    eff_dis: bool | None = None,
    battery_cells_balanced: bool | None = None,
    t_now: datetime | None = None,
):
    t_now = t_now or now()

    daily_power = daily_power or get(House.daily_average_power, 0)  # W
    nightly_power = nightly_power or get(House.nightly_average_power, 0)  # W

    ev_required_soc = ev_required_soc or get(EV.required_soc, 80)
    ev_soc = ev_soc or get(EV.soc, 100)
    is_charging_ev = is_charging_ev or get(EV.is_charging, False)

    smart_limiter_active = smart_limiter_active or get(Automation.auto_charge_limit, False)
    charge_limit = charge_limit or get(Battery.force_charge_up_to, 0)
    max_charge_price = max_charge_price or get(Battery.max_charge_price, 0)
    force_charge_switch = force_charge_switch or get(Battery.force_charge_switch, False)
    min_discharge_price = min_discharge_price or float(get(Automation.min_discharge_price, default=0))
    charger_ready = charger_ready or get(Charger.ready, False)
    eff_dis = eff_dis or get(Automation.efficient_discharge, False)
    battery_cells_balanced = (
        battery_cells_balanced if battery_cells_balanced is not None else get(Battery.cells_balanced, False)
    )
    return _forecast(
        forecast=forecast,
        setpoint=setpoint,
        battery_capacity=battery_capacity,
        min_feedin_price=min_feedin_price,
        forecast_dampening=forecast_dampening,
        battery_energy=battery_energy,
        setpoint_spread=setpoint_spread,
        battery_min_energy=battery_min_energy,
        battery_charge_limit=battery_charge_limit,
        with_ev_charging=with_ev_charging,
        ev_energy=ev_energy,
        max_battery_power_target=max_battery_power_target,
        max_pv_feedin_target=max_pv_feedin_target,
        max_setpoint=max_setpoint,
        logging=logging,
        ev_schedule=ev_schedule,
        surplus_energy=surplus_energy,
        daily_power=daily_power,
        nightly_power=nightly_power,
        ev_required_soc=ev_required_soc,
        ev_soc=ev_soc,
        is_charging_ev=is_charging_ev,
        smart_limiter_active=smart_limiter_active,
        charge_limit=charge_limit,
        max_charge_price=max_charge_price,
        force_charge_switch=force_charge_switch,
        min_discharge_price=min_discharge_price,
        charger_ready=charger_ready,
        eff_dis=eff_dis,
        battery_cells_balanced=battery_cells_balanced,
        t_now=t_now,
    )


@pyscript_compile
def _forecast(
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
    daily_power: float | None = None,
    nightly_power: float | None = None,
    ev_required_soc: float | None = None,
    ev_soc: float | None = None,
    is_charging_ev: bool | None = None,
    smart_limiter_active: bool | None = None,
    charge_limit: float | None = None,
    max_charge_price: float | None = None,
    force_charge_switch: bool | None = None,
    min_discharge_price: float | None = None,
    charger_ready: bool | None = None,
    eff_dis: bool | None = None,
    battery_cells_balanced: bool | None = None,
    t_now: datetime | None = None,
):
    if not len(forecast) > 1:
        return None

    prices = [el.price_per_kwh for el in forecast]
    prices_mean = sum(prices) / len(prices) if len(prices) > 0 else 0
    prices_std = sqrt(sum([(p - prices_mean) ** 2 for p in prices]) / len(prices)) if len(prices) > 0 else 0

    if surplus_energy is None:
        # Calculate an initial surplus estimate based on forecasted PV production and house demand
        period_minutes = (forecast[1].period_start - forecast[0].period_start).total_seconds() / 60
        period_hours = period_minutes / 60
        forecast_days = {}
        for entry in forecast:
            day_key = entry.period_start.date()
            if day_key not in forecast_days:
                forecast_days[day_key] = 0
            forecast_days[day_key] += entry.pv_estimate * forecast_dampening * period_hours / 1000  # kWh

        required_energy = 5  # start with an overhead of 5 kWh
        house_energy = (daily_power * 12 + nightly_power * 12) / 1000  # kWh
        for pv_energy in forecast_days.values():
            required_energy = house_energy - pv_energy

        surplus_energy = max(0, battery_energy - required_energy)

    surplus = surplus_energy
    orig_setpoint = setpoint

    smart_charge_limit = 80

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
            and ((charger_ready or is_charging_ev or not next_drive or dt > next_drive.end) and ongoing_drive is None)
            and ev_energy < EVConst.ev_capacity * (smart_charge_limit or 101) / 100
        )

    def get_inverter_mode(
        pv_power, target_soc, current_soc, electricity_price, surplus, battery_headroom_energy, _t_now
    ):
        assert surplus is not None
        new_mode, new_charge_power_limit, new_force_charge_switch_state, reason = get_auto_inverter_mode(
            is_charging_ev,
            surplus,
            battery_headroom_energy,
            pv_power,
            daily_power,
            current_soc,
            target_soc,
            electricity_price,
            min_discharge_price,
            max_charge_price,
            charge_limit,
            force_charge_switch,
            t_now=_t_now,
        )
        return new_mode, new_charge_power_limit, new_force_charge_switch_state, reason

    full_period_minutes = (forecast[1].period_start - forecast[0].period_start).total_seconds() / 60

    accumulated_energy = 0
    max_feedin = max_pv_feedin = 0
    min_forecast_battery = battery_energy
    max_forecast_battery = battery_energy
    t_min_bat, t_max_bat, t_max_feedin, t_max_pv_feedin = t_now, t_now, t_now, t_now
    detail: list[ForecastEntry] = []

    charge_phases, charge_current = 1, 7
    msg = ""
    next_drive_event = None

    for entry, epex_price in zip(forecast, prices):
        start: datetime = entry.period_start
        if ev_schedule:
            ongoing_drive = next(iter([s for s in ev_schedule if s.start <= start < s.end]), None)
            if ongoing_drive is None:
                next_drive_event = next(iter([s for s in ev_schedule if s.start > start]), None)
                if next_drive_event and next_drive_event.required_soc:
                    ev_required_soc = next_drive_event.required_soc
        elapsed = max(t_now - start, timedelta(minutes=0))
        if t_now > start:
            period_minutes = max(0, full_period_minutes - elapsed.total_seconds() / 60)
            if period_minutes == 0:
                continue
        else:
            period_minutes = full_period_minutes
        period_hours = period_minutes / 60

        power_production: float = entry.pv_estimate * forecast_dampening * 1000
        house_power = daily_power if 7 < start.hour < 19 else nightly_power
        period_battery_min_energy = battery_min_energy
        low_pv_reserve_soc = get_low_pv_reserve_soc(
            house_power=house_power,
            pv_power=power_production,
            battery_cells_balanced=bool(battery_cells_balanced),
        )
        if low_pv_reserve_soc > 0:
            period_battery_min_energy = max(period_battery_min_energy, battery_capacity * low_pv_reserve_soc / 100 + 1)

        smart_charge_limit = 101
        if next_drive_event:
            smart_charge_limit = _get_ev_smart_charge_limit(next_drive_event.start, start, active_schedule=False)

        ev_energy_needed = _get_ev_energy_needed(ev_required_soc, ev_soc, smart_charge_limit, smart_limiter_active)

        could_charge_ev = with_ev_charging and (
            ev_energy_needed > 0 or ev_energy < EVConst.ev_capacity * smart_charge_limit / 100
        )
        could_charge_ev = could_charge_ev and is_charging_possible(start, ev_energy, smart_charge_limit)

        # --- Battery State Estimation (Pre-calculation) ---
        # We calculate a 'tentative' SOC to help the inverter make decisions
        tentative_batt_energy = min(max(0, battery_energy + accumulated_energy), battery_capacity)
        tentative_batt_soc = tentative_batt_energy / battery_capacity * 100

        battery_target_soc = max(5, tentative_batt_soc - (surplus / battery_capacity * 100))
        electricity_price = get_price(hour=start.hour, minute=start.minute)
        low_price = is_low_price(electricity_price)

        excess_target = _get_excess_target(
            battery_target_soc=battery_target_soc,
            battery_soc=tentative_batt_soc,
            ev_required_soc=ev_required_soc,
            ev_is_charging=is_charging_ev,
            next_planned_drive=next_drive_event.start if next_drive_event else None,
            pv_power=power_production,
            ev_soc=ev_soc,
            t_now=start,
            efficient_discharge=eff_dis,
        )

        battery_headroom_energy = max(0, tentative_batt_energy - period_battery_min_energy)
        inverter_mode, charge_power_limit, is_force_charging, inverter_mode_reason = get_inverter_mode(
            power_production,
            battery_target_soc,
            tentative_batt_soc,
            electricity_price,
            surplus,
            battery_headroom_energy,
            _t_now=start,
        )

        # --- EV Charging Action ---
        ev_charge_power = 0.0

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
                is_low_price=low_price,
                pv_total_power=power_production,
                battery_soc=tentative_batt_soc,
                is_charging=is_charging_ev,
                t_now=start,
                inverter_mode=inverter_mode,
                battery_force_charge=is_force_charging,
            )

            if new_charge_phases != charge_phases:
                charge_current = 8 if new_charge_phases == 1 else 6

            charge_phases = new_charge_phases
            is_charging_ev = charge_action == ChargeAction.on

            if is_charging_ev:
                ev_charge_power = charge_phases * charge_current * 230  # W
                # prevent unnecessary discharging of battety
                battery_target_soc = tentative_batt_soc + 1

        else:
            is_charging_ev, charge_phases, charge_current = False, 1, 6

        this_setpoint = map_setpoint(
            setpoint,
            epex_price,
            prices_mean,
            prices_std,
            battery_energy=tentative_batt_energy,
            battery_min_limit=period_battery_min_energy,
            pv_power=power_production,
            house_power=house_power,
            setpoint_spread=setpoint_spread,
            max_setpoint=max_setpoint,
            max_battery_power_target=max_battery_power_target,
            surplus_energy=surplus,
        )

        # --- Apply EV Charging Physics ---
        if is_charging_ev and ev_charge_power > 1:
            added_ev_energy = ev_charge_power * period_hours / 1000
            # Cap at smart limit
            if ev_energy + added_ev_energy > smart_charge_limit * EVConst.ev_capacity / 100:
                added_ev_energy = max(0, smart_charge_limit * EVConst.ev_capacity / 100 - ev_energy)
                # Recalculate power to match the energy cap
                ev_charge_power = (added_ev_energy * 1000) / period_hours if period_hours > 0 else 0

            ev_energy += added_ev_energy
            free_capacity = (
                smart_charge_limit * EVConst.ev_capacity / 100 - ev_energy + battery_capacity - battery_energy
            )

            this_setpoint = -20

        else:
            free_capacity = battery_capacity - battery_energy
            ev_charge_power = 0

        # --- Driving Physics ---
        if ongoing_drive and ev_energy is not None:
            total_required_kwh = (ongoing_drive.distance or 200) / 100 * EVConst.kwh_per_100km
            drive_duration_s = (ongoing_drive.end - ongoing_drive.start).total_seconds()
            # ev_energy -= total_required_kwh / max(drive_duration_s / 3600, 1) * period_hours
            if drive_duration_s > 0:
                energy_drained = total_required_kwh * (period_hours * 3600 / drive_duration_s)
                ev_energy -= energy_drained

                if ev_energy < 0:
                    ev_energy = 0

        # assume ev energy is never completely depleted
        if ev_energy is not None:
            ev_energy = max(5, ev_energy)
            ev_soc = (ev_energy / EVConst.ev_capacity) * 100

        # --- Battery & Grid Physics ---

        # 1. Calculate Base Load Balance (House + EV - PV)
        # Positive = Draw needed, Negative = Excess PV
        power_balance_w = house_power - this_setpoint + ev_charge_power - power_production

        # 2. Determine Battery Action based on Inverter Mode & Physics

        battery_power = 0.0  # Positive = Charging, Negative = Discharging
        feedin = 0.0
        pv_feedin = 0.0
        power_from_grid = 0.0

        # Current actual energy before this step
        current_bat_kwh = max(0, min(battery_capacity, battery_energy + accumulated_energy))

        # --- BRANCH A: Force Charging ---
        if inverter_mode in (InverterMode.on, InverterMode.charger_only) and is_force_charging:
            # We enforce charging at the limit
            target_charge_power = charge_power_limit

            # Physical limit check (Capacity)
            max_intake_wh = (battery_capacity - current_bat_kwh) * 1000
            possible_charge_power = min(target_charge_power, max_intake_wh / period_hours if period_hours > 0 else 0)

            battery_power = possible_charge_power

            # Grid must supply: House + EV + Battery - PV
            power_from_grid = max(0, power_balance_w + battery_power)

            # No feedin during force charge usually
            feedin = 0
            pv_feedin = 0

        # --- BRANCH B: Normal Operation (Surplus/Deficit) ---
        else:
            # Calculate the "Natural" balance without battery
            # Positive = We need power (Deficit)
            # Negative = We have excess power (Surplus)
            natural_deficit_watts = (house_power + ev_charge_power) - power_production

            # Calculate what the battery *should* do to hit the Setpoint
            # Equation: (Load - PV) - Battery_Discharge = Setpoint
            # Therefore: Battery_Discharge = (Load - PV) - Setpoint
            target_battery_discharge_watts = natural_deficit_watts - this_setpoint

            # --- SUB-BRANCH 1: Battery wants to DISCHARGE ---
            if target_battery_discharge_watts > 0:
                # Discharging for house self-consumption is allowed as long as
                # physical headroom exists. Additional battery-to-grid export is
                # only allowed from the reserve-aware surplus budget.
                self_consumption_discharge_watts = max(0, natural_deficit_watts)
                surplus_export_discharge_watts = (
                    max(0, surplus) * 1000 / period_hours if period_hours > 0 else 0
                )
                max_allowed_discharge_watts = self_consumption_discharge_watts + surplus_export_discharge_watts

                # Check Mode/SOC constraints
                if (
                    inverter_mode in (InverterMode.off, InverterMode.charger_only)
                    or current_bat_kwh <= period_battery_min_energy
                ):
                    actual_discharge_watts = 0
                else:
                    # Physical Constraints: Limit and Available Energy
                    max_discharge_rate = min(battery_charge_limit, max_battery_power_target)
                    available_energy_kwh = current_bat_kwh - period_battery_min_energy
                    max_discharge_by_energy = (available_energy_kwh * 1000) / period_hours if period_hours > 0 else 0

                    actual_discharge_watts = min(
                        target_battery_discharge_watts,
                        max_discharge_rate,
                        max_discharge_by_energy,
                        max_allowed_discharge_watts,
                    )

                battery_power = -actual_discharge_watts  # Negative = Discharging (Energy leaving battery)

            # --- SUB-BRANCH 2: Battery wants to CHARGE ---
            else:
                # target_battery_discharge_watts is negative, so we want to charge
                target_charge_watts = -target_battery_discharge_watts

                # Physical Constraints: Limit and Free Capacity
                max_charge_rate = min(battery_charge_limit, max_battery_power_target)
                free_capacity_kwh = battery_capacity - current_bat_kwh
                max_charge_by_capacity = (free_capacity_kwh * 1000) / period_hours if period_hours > 0 else 0

                actual_charge_watts = min(target_charge_watts, max_charge_rate, max_charge_by_capacity)

                battery_power = actual_charge_watts  # Positive = Charging (Energy entering battery)

            # --- FINAL GRID CALCULATION ---
            # Grid = (Load - PV) + Battery_Power (where Charging is positive load, Discharging is negative load)
            # OR simpler: Grid = Natural_Deficit + Battery_Power

            grid_exchange_watts = natural_deficit_watts + battery_power
            pv_feedin = 0
            if grid_exchange_watts > 0:
                power_from_grid = grid_exchange_watts
                feedin = 0
            else:
                power_from_grid = 0
                feedin = -grid_exchange_watts
                pv_feedin = min(feedin, max(0, -natural_deficit_watts))

        # --- Update Accumulator ---
        added_battery_energy = (battery_power * period_hours) / 1000
        accumulated_energy += added_battery_energy

        # Calculate final battery state for this step
        new_battery_energy = max(0, min(battery_capacity, battery_energy + accumulated_energy))

        # Track only energy that actually remains available after meeting load and imports.
        surplus_delta = (battery_power - power_from_grid) * period_hours / 1000
        surplus += surplus_delta

        # Update setpoint for next loop context
        setpoint = this_setpoint
        if logging:
            msg += (
                f"\n{start.hour}:{start.minute:02d} - batt {battery_power:.0f}W {new_battery_energy:.0f}kWh setpoint {setpoint:.0f} feedin {feedin:.0f} pv_power {power_production:.0f} "
                f"ev_charge_power {ev_charge_power:.0f}W power_draw {power_balance_w:.0f}W house_power {house_power:.0f}W "
                f"battery_power {battery_power:.0f}W"
            )
        # --- Statistics Tracking ---
        if feedin > max_feedin:
            max_feedin = feedin
            t_max_feedin = start

        if pv_feedin > max_pv_feedin and pv_feedin > max_pv_feedin_target:
            max_pv_feedin = pv_feedin
            t_max_pv_feedin = start

        if new_battery_energy < min_forecast_battery:
            min_forecast_battery = new_battery_energy
            t_min_bat = start

        if new_battery_energy > max_forecast_battery:
            max_forecast_battery = new_battery_energy
            t_max_bat = start + timedelta(minutes=period_minutes)

        if max_feedin > max_pv_feedin_target and (max_pv_feedin_target > 0 or t_max_feedin.day == start.day):
            max_pv_feedin_target = max_feedin
            t_max_feedin = start

        surplus = min(surplus, new_battery_energy - 0.5)  # can't have more surplus than total battery energy

        if logging:
            msg += f"\n{surplus:.1f} surplus += (battery_power - power_from_grid) * period_hours / 1000: ({battery_power} - {power_from_grid}) * {period_hours} / 1000 = {surplus_delta}"

        detail.append(
            ForecastEntry(
                period_start=start,
                pv_estimate=power_production,
                battery_energy=new_battery_energy,
                battery_power=battery_power,
                house_power=house_power,
                setpoint=this_setpoint,
                power_draw=power_balance_w,
                free_capacity=free_capacity,
                accumulated_energy=accumulated_energy,
                feedin=feedin,
                pv_feedin=pv_feedin,
                epex_price=epex_price,
                electricity_price=electricity_price,
                setpoint_spread=setpoint_spread,
                ev_energy=ev_energy,
                ev_charge_power=ev_charge_power,
                excess_target=excess_target,
                surplus=surplus,
                power_from_grid=power_from_grid,
            )
        )

    # for idx, entry in enumerate(detail):
    #     future = detail[idx : int(idx + 16 / period_hours)]
    #     min_bat, t_min_bat = min([(e.battery_energy, e.period_start) for e in future], key=lambda x: x[0])
    #     headroom = max(0, min_bat - battery_min_energy)
    #     feedin_power = sum([e.pv_feedin for e in future if e.period_start < t_min_bat or min_bat > battery_min_energy])
    #     feedin_energy = feedin_power * period_hours / 1000
    #     surplus = min(headroom + feedin_energy, entry.battery_energy - battery_min_energy)
    #     detail[idx] = replace(entry, surplus=surplus)

    if logging:
        print("\n" + msg)

    return ForecastResult(
        setpoint=orig_setpoint,
        min_bat=min_forecast_battery,
        t_min_bat=t_min_bat,
        max_bat=max_forecast_battery,
        t_max_bat=t_max_bat,
        max_feedin=max_feedin,
        t_max_feedin=t_max_feedin,
        max_pv_feedin=max_pv_feedin,
        t_max_pv_feedin=t_max_pv_feedin,
        setpoint_spread=setpoint_spread,
        prices_mean=prices_mean,
        prices_std=prices_std,
        detail=detail,
        max_battery_power_target=max_battery_power_target,
    )


@pyscript_compile
def merge_forecast_results(a: ForecastResult, b: ForecastResult, t_split: datetime):
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
    logging = True
    forecast_hours = 24

    max_feedin_limit = get(Grid.max_feedin_target, 4000)
    max_pv_feedin = get(Grid.max_pv_feedin_target, 4000)

    skip_automation_message = ""
    if automation_disabled := get(Automation.auto_setpoint, False) is False:
        skip_automation_message = "Auto setpoint is disabled"

        current_setpoint = get(Grid.power_setpoint, -20)
    else:
        current_setpoint = get(Grid.power_setpoint_target, -20)

    max_setpoint = min(current_setpoint + 500, get(Grid.max_setpoint, -20))
    min_setpoint = min(max_setpoint, -max_feedin_limit)
    reserve_soc = get_reserve_soc()

    forecast_dampening = 1.0  # dampen the forecast to account for inaccuracies
    battery_energy = get(Battery.energy, 0)
    min_feedin_price = 0

    house_avg_power: float = get(House.daily_average_power, 0)  # W

    battery_capacity = get(Battery.capacity, 1337)

    if battery_capacity == 1337:
        log.error("Battery capacity not available yet, cannot calculate setpoint")
        return

    minimal_soc = get(Automation.minimal_soc, reserve_soc)
    battery_min_energy = get_battery_export_floor(battery_capacity, reserve_soc, minimal_soc)

    if logging:
        log.warning(f"battery capacity: {battery_capacity} min energy: {battery_min_energy}")

    ev_schedule = (schedule.get_schedule(entity_id="schedule.tesla_planned_drives") or {}).get(
        "schedule.tesla_planned_drives", {}
    )
    # example:
    # data = {'monday': [], 'tuesday': [], 'wednesday': [{'from': datetime.time(7, 30), 'to': datetime.time(17, 0)}], 'thursday': [], 'friday': [], 'saturday': [], 'sunday': [{'from': datetime.time(9, 30), 'to': datetime.time(10, 0), 'data': {'required_charge': 50}}]}
    ev_required_soc = get(EV.required_soc, 80)
    ev_schedule = parse_full_schedule(ev_schedule, default_required_soc=ev_required_soc)
    drive_ongoing = next(iter([s for s in ev_schedule if s.start <= t_now < s.end]), None)
    ev_energy = get(EV.energy, EVConst.ev_capacity)

    wallbox_connected = get(Charger.ready, False)
    assume_able_to_charge_on_arrival = get(EV.able_to_charge_on_arrival, False)

    with_ev_charging = wallbox_connected or drive_ongoing and assume_able_to_charge_on_arrival

    def forecast_setpoint_local(
        pv_forecast,
        setpoint,
        setpoint_spread=0.1,
        current_battery_energy=0,
        t_start: datetime | None = None,
        t_end: datetime | None = None,
        with_ev_charging=with_ev_charging,
        ev_energy: float | None = ev_energy,
        max_battery_power_target: float = 4000,
        logging=False,
        ev_schedule=ev_schedule,
    ):
        if t_start is not None:
            pv_forecast = [entry for entry in pv_forecast if t_start < entry.period_start]
        elif t_end is not None:
            pv_forecast = [entry for entry in pv_forecast if entry.period_start < t_end]
        result = forecast(
            pv_forecast,
            setpoint=setpoint,
            battery_capacity=battery_capacity,
            min_feedin_price=min_feedin_price,
            forecast_dampening=forecast_dampening,
            battery_energy=current_battery_energy,
            setpoint_spread=setpoint_spread,
            battery_min_energy=battery_min_energy,
            with_ev_charging=with_ev_charging and ev_energy is not None,
            ev_energy=ev_energy,
            max_battery_power_target=max_battery_power_target,
            max_pv_feedin_target=max_pv_feedin,
            max_setpoint=max_setpoint,
            logging=logging,
            ev_schedule=ev_schedule,
        )
        task.sleep(0.01)  # sleep to allow other tasks to run
        return result

    def get_discounted_pv_feedin_peak(forecast_result):
        if not forecast_result or not forecast_result.detail:
            return 0, t_now, 0

        discount_points = ((0.0, 1.0), (6.0, 1.0), (9.0, 0.75), (12.0, 0.5), (16.0, 0.25), (20.0, 0.0))

        def get_discount(hours_ahead):
            if hours_ahead <= discount_points[0][0]:
                return discount_points[0][1]

            for (left_hours, left_discount), (right_hours, right_discount) in zip(discount_points, discount_points[1:]):
                if hours_ahead <= right_hours:
                    span = right_hours - left_hours
                    if span <= 0:
                        return right_discount
                    ratio = (hours_ahead - left_hours) / span
                    return left_discount + ratio * (right_discount - left_discount)

            return 0.0

        best_weighted_peak = 0.0
        best_peak_time = t_now
        best_raw_peak = 0.0

        for entry in forecast_result.detail:
            if entry.period_start <= t_now or entry.pv_feedin <= 0:
                continue

            hours_ahead = max(0, (entry.period_start - t_now).total_seconds() / 3600)
            discount = get_discount(hours_ahead)

            weighted_peak = entry.pv_feedin * discount
            if weighted_peak > best_weighted_peak:
                best_weighted_peak = weighted_peak
                best_peak_time = entry.period_start
                best_raw_peak = entry.pv_feedin

        return best_weighted_peak, best_peak_time, best_raw_peak

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

    initial_forecast = forecast_setpoint_local(
        pv_forecast=epex_pv_forecast,
        setpoint=current_setpoint if automation_disabled else max_setpoint,
        setpoint_spread=0.01,
        current_battery_energy=current_battery_energy,
        with_ev_charging=with_ev_charging,
        ev_energy=ev_energy,
        max_battery_power_target=8000,  # TODO: make this configurable
    )

    if not automation_disabled and initial_forecast.max_feedin < 200:
        skip_automation_message = "No significant feedin expected"
    battery_too_low = (
        battery_energy <= battery_min_energy + 0.5
        or initial_forecast.min_bat < battery_min_energy + 0.5
    )
    if battery_too_low:
        skip_automation_message = "Battery too low in forecast, skipping automation to avoid depletion"

    if skip_automation_message:
        fallback_setpoint = initial_forecast.setpoint
        if battery_too_low:
            fallback_setpoint = get_pv_only_setpoint(
                pv_power_total,
                get(House.loads, house_avg_power),
                8000,
            )
        log.warning(f"{skip_automation_message}, setting setpoint to {fallback_setpoint}")

        set_state(
            Grid.power_setpoint_target,
            fallback_setpoint,
            **power_w_attributes,
            detail=initial_forecast.detail,
        )
        set_state(
            Grid.power_setpoint_basis,
            fallback_setpoint,
            **power_w_attributes,
        )
        return

    def setpoint_binary_search(
        pv_forecast,
        min_setpoint=min_setpoint,
        max_setpoint=max_setpoint,
        with_ev_charging=with_ev_charging,
        battery_energy=current_battery_energy,
        ev_energy=ev_energy,
        max_iters=5,
        max_battery_power_target: float = 8000,
        log_setpoint=False,
        current_setpoint=None,
        current_spread=None,
        min_spread=1e-5,
        max_spread=50,
        update_setpoint=True,
        update_spread=False,
    ):
        msg = ""
        if not len(pv_forecast) > 1:
            return None
        search_results = []
        assert update_setpoint or update_spread

        log.warning(
            f"Starting binary search for setpoint with parameters: ev_energy={ev_energy} min_setpoint={min_setpoint}, max_setpoint={max_setpoint}, min_spread={min_spread}, max_spread={max_spread}, update_setpoint={update_setpoint}, update_spread={update_spread}, max_iters={max_iters}, log_setpoint={log_setpoint}"
        )
        if not update_spread:
            assert current_spread is not None
        if not update_setpoint:
            assert current_setpoint is not None

        for itr in range(max_iters):
            if update_setpoint:
                current_setpoint = (min_setpoint + max_setpoint) // 2
            if update_spread:
                current_spread = (min_spread + max_spread) / 2

            r = forecast_setpoint_local(
                pv_forecast,
                current_setpoint,
                current_spread,
                battery_energy,
                with_ev_charging=with_ev_charging,
                ev_energy=ev_energy,
                max_battery_power_target=max_battery_power_target,
            )
            update_reason = ""
            discounted_pv_feedin, discounted_peak_time, raw_pv_feedin = get_discounted_pv_feedin_peak(r)

            if (
                discounted_pv_feedin > max_pv_feedin
                and discounted_peak_time.day != t_now.day
                and (
                    r.min_bat < battery_min_energy
                    or min(
                        [d.surplus for d in r.detail if d.period_start.day == t_now.day and d.period_start > t_now]
                        or [float("inf")]
                    )
                    < 1
                )
            ):
                update_reason = "pv peak tomorrow, battery too low"
                # If we have a feedin peak tomorrow but battery is too low, we need to be more conservative (lower setpoint, higher spread)
                if update_setpoint:
                    min_setpoint = current_setpoint
                if update_spread:
                    max_spread = current_spread

            elif discounted_pv_feedin > max_pv_feedin:
                update_reason = "max feedin too large"
                # If feed-in limit is exceeded, we need a more negative setpoint, higher spread
                if update_setpoint:
                    max_setpoint = current_setpoint
                if update_spread:
                    min_spread = current_spread

            else:
                update_reason = "default"
                if update_setpoint:
                    min_setpoint = current_setpoint
                if update_spread:
                    max_spread = current_spread

            search_results.append(r)

            if logging:
                msg += (
                    f"Iteration {itr + 1}: reason  {update_reason} setpoint {r.setpoint:0f} spread {r.setpoint_spread:.1f} "
                    f"min_setpoint={min_setpoint:.0f} max_setpoint={max_setpoint:.0f} pv feedin {raw_pv_feedin:.0f} "
                    f"discounted {discounted_pv_feedin:.0f} at {discounted_peak_time.strftime('%d %H:%M')} min_bat {r.min_bat:.1f} max_bat {r.max_bat:.1f} t_max_bat {r.t_max_bat}\n"
                )

            if (update_spread and abs(max_spread - min_spread) < 1e-3) or (
                update_setpoint and abs(max_setpoint - min_setpoint) < 50
            ):
                break
        if logging:
            log.warning(f"Binary search completed in {itr + 1} iterations: \n" + msg)
        return search_results

    def format_setpoint_results(search_results, title):
        # Print setpoint results in tablular format (without forecast details)
        if search_results is None:
            return None
        lines = []

        def ft(t):
            return t.strftime("%d %H:%M")

        def fi(k):
            return f"{k:.0f}"

        lines = []
        for r in search_results:
            discounted_peak, discounted_time, raw_peak = get_discounted_pv_feedin_peak(r)
            lines.append(
                f"{r.setpoint:2.0f} {' ' * 4}  {r.setpoint_spread:8.6f}{r.min_bat:9.1f}{'':6s}{ft(r.t_min_bat):10s}{r.max_bat:5.1f}{'':6s}{ft(r.t_max_bat):12s}{fi(raw_peak):12s}{fi(discounted_peak):18s}{ft(discounted_time):10s}"
            )
        return (
            f"Setpoint results {title}:\n{'setpoint':<11s}{'spread':<13s}{'min_bat':<10s}{'t_min_bat':<11s}{'max_bat':<11s}{'t_max_bat':<11s}{'max_pv_feedin':<12s}{'discounted_pv':<18s}{'t_discounted':<10s}\n"
            + "\n".join(lines)
        )

    search_results = setpoint_binary_search(
        epex_pv_forecast,
        min_setpoint=min_setpoint,
        max_setpoint=max_setpoint,
        current_spread=0.01,
        ev_energy=ev_energy,
    )

    if logging:
        log.warning(format_setpoint_results(search_results, "initial search"))

    assert search_results[-1].setpoint_spread is not None

    search_results += setpoint_binary_search(
        epex_pv_forecast,
        current_setpoint=search_results[-1].setpoint,
        min_setpoint=search_results[-1].setpoint - 200,
        max_setpoint=search_results[-1].setpoint + 200,
        min_spread=1e-5,
        update_spread=True,
        update_setpoint=False,
        # log_setpoint=True,
        ev_energy=ev_energy,
    )

    initial_result = search_results[-1]

    assert initial_result.setpoint_spread is not None
    forecast_setpoint_local(
        epex_pv_forecast,
        setpoint=initial_result.setpoint,
        setpoint_spread=initial_result.setpoint_spread,
        current_battery_energy=battery_energy,
        with_ev_charging=with_ev_charging,
        ev_energy=ev_energy,
        max_battery_power_target=8000,
        logging=False,
    )

    max_pv_feedin_today = max(
        [d.pv_feedin for d in initial_result.detail if d.period_start.day == t_now.day and d.period_start > t_now],
        default=0,
    )
    discounted_pv_feedin, discounted_peak_time, raw_pv_feedin = get_discounted_pv_feedin_peak(initial_result)
    log.warning(
        f" \n\ninitial_result.max_pv_feedin {raw_pv_feedin:.0f} W, discounted {discounted_pv_feedin:.0f} W at {discounted_peak_time.strftime('%d %H:%M')} > max_pv_feedin {max_pv_feedin:.0f} W: {discounted_pv_feedin > max_pv_feedin}\n"
        f" max pv feedin today {max_pv_feedin_today:.0f} W > max_pv_feedin {max_pv_feedin:.0f} W: {max_pv_feedin_today > max_pv_feedin}\n\n"
    )
    if max_pv_feedin_today > max_pv_feedin:
        t_start = max(t_now, t_now.replace(hour=8))

        t_end = next(
            iter(
                [
                    e.period_start
                    for e in epex_pv_forecast
                    if e.period_start > t_start
                    and e.period_start.hour > 16
                    and (e.pv_estimate * 1000 - house_avg_power) < (max_feedin_limit / 2)
                ]
            ),
            t_start + timedelta(hours=8),
        )
        new_price_forecast = [el for el in epex_pv_forecast if t_start < el.period_start <= t_end]

        # log tstart and tend
        msg = f" \n\nSearching for feedin setpoint at {t_start.strftime('%m-%d between %H:%M')} and {t_end.strftime('%H:%M')}\n"
        msg += ", ".join([f"{e.period_start.strftime('%H:%M')}: {e.pv_estimate:.1f}kW" for e in new_price_forecast])

        start_detail = next(iter([e for e in initial_result.detail if e.period_start >= t_start]), None)
        if len(new_price_forecast) < 2:
            search_results = [initial_result]
            price_forecast = epex_pv_forecast
        else:
            price_forecast = new_price_forecast
            search_results = setpoint_binary_search(
                price_forecast,
                with_ev_charging=with_ev_charging,
                battery_energy=start_detail.battery_energy,
                ev_energy=start_detail.ev_energy,
                current_setpoint=initial_result.setpoint,
                update_spread=True,
                update_setpoint=False,
                log_setpoint=True,
            )

        log.warning(msg + "\n" + format_setpoint_results(search_results, "update spread"))
        new_result = search_results[-1]
        assert new_result is not None

        if logging:
            log.warning(
                f" \n\nnew_result.discounted_pv_feedin {get_discounted_pv_feedin_peak(new_result)[0]:0f} > max_pv_feedin {max_pv_feedin:0f}: {get_discounted_pv_feedin_peak(new_result)[0] > max_pv_feedin}\n\n!!"
            )
        msg = ""
        # while new_result.max_pv_feedin > max_pv_feedin and new_result.max_battery_power_target > 100:
        #     new_result = forecast_setpoint_local(
        #         price_forecast,
        #         setpoint=new_result.setpoint,
        #         setpoint_spread=new_result.setpoint_spread,
        #         current_battery_energy=start_detail.battery_energy,
        #         t_start=t_start,
        #         ev_energy=start_detail.ev_energy,
        #         with_ev_charging=with_ev_charging,
        #         max_battery_power_target=round(new_result.max_battery_power_target * 0.7),
        #     )

        #     msg += (
        #         f"\nupdated max battery power target: {new_result.max_battery_power_target:.1f} W, "
        #         f"setpoint: {new_result.setpoint:.1f} W, max_pv_feedin: {new_result.max_pv_feedin:.1f} W"
        #     )

        # if logging:
        #     log.warning(msg)

        # if the end time is before the time limit, need to forecast again for the remaining time
        rest_forecast = [el for el in epex_pv_forecast if el.period_start > t_end]

        if rest_forecast:
            log.warning("starting rest forecast")
            rest_result = setpoint_binary_search(
                rest_forecast,
                min_setpoint=-max_feedin_limit,
                max_setpoint=max_setpoint,
                with_ev_charging=True,
                battery_energy=new_result.detail[-1].battery_energy,
                ev_energy=new_result.detail[-1].ev_energy,
                current_spread=new_result.setpoint_spread,
                log_setpoint=True,
            )[-1]

            final_result = merge_forecast_results(
                new_result,
                rest_result,
                t_split=t_end,
            )
            log.warning(f"merged \n\t{replace(rest_result, detail=None)} into \n\t{replace(new_result, detail=None)}")
            search_results.append(final_result)

        # if t_start > t_now:
        #     final_result = merge_forecast_results(
        #         initial_result,
        #         new_result,
        #         t_split=t_start,
        #     )
        #     final_result.max_battery_power_target = new_result.max_battery_power_target

    log.warning(format_setpoint_results(search_results, "final result"))

    setpoint_result = search_results[-1]

    price = max(min_feedin_price, get(ElectricityPrices.epex_forecast_prices, min_feedin_price))

    pv_power_total = get(PVProduction.total_power, 0)
    house_power = get(House.loads, 0)
    surplus_energy = get(House.energy_surplus, 0)

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
        surplus_energy=surplus_energy,
    )

    if logging:
        log.warning(
            f"\nMapped setpoint: {setpoint_result.setpoint:.0f} to {setpoint} with spread {setpoint_result.setpoint_spread:.2f} and max batt power {setpoint_result.max_battery_power_target} W\n"
            f"price now {price:.2f} mean {setpoint_result.prices_mean:.2f} price std {setpoint_result.prices_std:.2f} "
            f"surplus {surplus_energy:.2f} kWh min_bat {setpoint_result.min_bat:.1f} at {setpoint_result.t_min_bat.strftime('%H:%M')} "
        )

    set_state(
        Grid.power_setpoint_basis,
        setpoint_result.setpoint,
        **power_w_attributes,
    )

    detail_with_ev_vectorized = {
        k: [getattr(el, k) for el in setpoint_result.detail] for k in ForecastEntry.__annotations__.keys()
    }

    set_state(
        Grid.power_setpoint_target,
        setpoint,
        **power_w_attributes,
        max_battery_power_target=setpoint_result.max_battery_power_target,
        detail=detail_with_ev_vectorized,
    )


prev_house_loads = None


@time_trigger
@time_trigger("period(now, 5sec)")
def auto_apply_setpoint():
    logging = False
    msg = ""
    if not get(Automation.auto_setpoint, False):
        msg = "Auto setpoint disabled"
        return

    global prev_house_loads
    if prev_house_loads is None:
        prev_house_loads = get(House.loads, 500)

    max_setpoint = get(Grid.max_setpoint, -20)
    msg += f" max setpont {max_setpoint}"
    house_power_long_term_average = get(House.daily_average_power, 0)  # W
    house_loads = get(House.loads, prev_house_loads)
    # when true PV power changes rapidly, house_loads can be negative due to the house loads are often calculated
    house_loads = max(house_loads, house_power_long_term_average * 0.5)

    house_loads = prev_house_loads * 0.5 + 0.5 * house_loads  # prevent oscillations, outliers
    prev_house_loads = house_loads
    setpoint_target = get(Grid.power_setpoint_target, 0)
    max_setpoint_target = get(Grid.max_feedin_target, 0)

    if setpoint_target < (max_setpoint - 30):
        current_diff_from_avg = house_power_long_term_average - house_loads
        setpoint = round(max(-max_setpoint_target, min(max_setpoint, setpoint_target - current_diff_from_avg)))
        msg += f" \nupdating setpoint {setpoint_target:.0f} with house_avg {house_power_long_term_average} loads {house_loads} and diff {current_diff_from_avg:.0f} to {setpoint:.0f}"
    else:
        setpoint = min(max_setpoint, setpoint_target)
        msg += f" updated setpoint to {setpoint}"

    ev_is_charging = get(EV.is_charging, False)
    if ev_is_charging:
        msg = f"setpoint adjusted to {max_setpoint} since EV is charging"
        setpoint = max_setpoint

    surplus_energy = get(House.energy_surplus, 0)
    battery_capacity = get(Battery.capacity, 0)
    battery_energy = get(Battery.energy, 0)
    reserve_soc = get_reserve_soc()
    minimal_soc = get(Automation.minimal_soc, reserve_soc)
    battery_export_floor = get_battery_export_floor(battery_capacity, reserve_soc, minimal_soc)
    pv_power = get(PVProduction.total_power, 0)
    max_battery_power_target = get_attr(Grid.power_setpoint_target, "max_battery_power_target", 4000)
    pv_only_setpoint = get_pv_only_setpoint(
        pv_power,
        house_loads,
        max_battery_power_target,
    )

    ev_energy_needed = get(EV.energy_needed, 0)
    min_ev_charge_power = EVConst.voltage * EVConst.min_current
    ev_has_pv_headroom = pv_power > house_loads + min_ev_charge_power
    protect_opportunistic_ev_from_battery = ev_is_charging and ev_energy_needed <= 0 and ev_has_pv_headroom

    if protect_opportunistic_ev_from_battery and setpoint < pv_only_setpoint:
        msg += (
            f" capped EV setpoint from {setpoint:.0f} to {pv_only_setpoint:.0f}; "
            f"PV {pv_power:.0f} W, loads {house_loads:.0f} W, energy_needed {ev_energy_needed:.2f} kWh"
        )
        setpoint = pv_only_setpoint

    if surplus_energy <= 0 or battery_energy <= battery_export_floor:
        if setpoint < pv_only_setpoint:
            msg += (
                f" capped setpoint from {setpoint:.0f} to {pv_only_setpoint:.0f}; "
                f"surplus {surplus_energy:.2f} kWh, battery {battery_energy:.2f} kWh, "
                f"protected floor {battery_export_floor:.2f} kWh"
            )
            setpoint = pv_only_setpoint

    if logging:
        log.warning(msg)

    set_state(Grid.power_setpoint, round(setpoint))
