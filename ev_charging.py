# ruff: noqa: I001

from datetime import timedelta
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    # The type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    # Therefore during type checking we pretend to import them from modules.utils, which it can resolve.
    from modules.utils import get, get_attr
    from modules.const import EV as Const
    from modules.energy_core import (
        ChargeMode,
        HYSTERESIS_BUFFER,
        get_drive_required_soc,
        get_ongoing_and_next_drive,
    )

    # These are provided by typescript and do not need to be imported in the actual script
    # They are only needed for type checking (linting), which development easier
    from modules.utils import (
        log,
        now,
        time_trigger,
        with_timezone,
        state_active,
        state_trigger,
        service,
        task,
        set_state,
        pyscript_compile,
    )
    from modules.energy_core import _get_ev_smart_charge_limit, _get_ev_energy_needed, _get_charge_action  # noqa: F401

    from modules.states import Automation, Charger, ElectricityPrices, EV, Excess, Battery, House, PVProduction
    from modules.victron import Victron

else:
    from const import EV as Const
    from utils import get, set_state, get_attr, now, with_timezone
    from states import Automation, Charger, ElectricityPrices, EV, Excess, Battery, House, PVProduction
    from energy_core import (
        ChargeMode,
        HYSTERESIS_BUFFER,
        _get_charge_action,
        _get_ev_energy_needed,
        _get_ev_smart_charge_limit,
        get_drive_required_soc,
        get_ongoing_and_next_drive,
    )  # noqa: F401
    from victron import Victron


@state_trigger(f"{EV.planned_drives}")
@time_trigger
@time_trigger("period(now, 30sec)")
def smart_charge_limit():
    """The smart charge limit is the maximum state of charge the EV should be charged to
    to ensure the battery is not fully charged when the car is not used for a longer
    period of time.

    The limit is calculated based on the time until the next drive.
    """
    schedule = get_attr(EV.planned_drives, "next_event")
    active_schedule = get(EV.planned_drives, False)

    smart_charge_limit = _get_ev_smart_charge_limit(schedule, now(), active_schedule=active_schedule)

    set_state(EV.smart_charge_limit, smart_charge_limit)


last_ev_charging_phase_change = now() - timedelta(minutes=15)
last_charger_reset = now() - timedelta(minutes=15)

PV_SURPLUS_START_CONFIRMATION = timedelta(minutes=3)
PV_SURPLUS_STOP_CONFIRMATION = timedelta(minutes=5)
PV_SURPLUS_MIN_RUN_TIME = timedelta(minutes=10)
PV_SURPLUS_RESTART_LOCKOUT = timedelta(minutes=5)

pv_surplus_active = get(Charger.turned_on_by_automation, False) and get(Charger.control_switch, False)
pv_surplus_started_at = now() if pv_surplus_active else None
pv_surplus_start_candidate_since = None
pv_surplus_stop_candidate_since = None
last_pv_surplus_stop = now() - PV_SURPLUS_RESTART_LOCKOUT


def set_pv_surplus_marker(active: bool):
    """Persist whether the automation currently owns a PV-surplus charge."""

    marker_active = get(Charger.turned_on_by_automation, False)
    if marker_active != active:
        service.call(
            "input_boolean",
            "turn_on" if active else "turn_off",
            entity_id=Charger.turned_on_by_automation,
        )


@time_trigger
@time_trigger("period(now, 15sec)")
def ev_energy():
    """Calculate the energy needed to charge the EV to the required state of charge"""
    current_soc = get(EV.soc, 0)
    ev_energy = (current_soc) / 100 * Const.ev_capacity
    set_state(EV.energy, ev_energy)


@state_trigger(f"{Charger.force_charge} == 'on' and {Charger.ready} == 'on' and {EV.plugged_in}")
@time_trigger  # run when script is reloaded
@time_trigger("period(now, 600sec)")
def force_charge():
    if not get(Charger.force_charge, False):
        log.warning("Force charge disabled")
        return
    if not (state := get(EV.plugged_in, False)):
        log.warning("No vehicle connected")
        return
    log.warning(f"state is {state}")
    if (val := get(Charger.control_switch, default=None, mapper=bool)) is False:
        changed = True
        if get(Charger.phases, 3) != 3 or get(Charger.current_setting, -1) != 16:
            changed = set_phases_and_current(3, 16, "Force charge enabled, setting max power")
        if changed:
            turn_on_charger("Force charge enabled")
        else:
            log.warning("Force charge enabled, but phase/current change was blocked; not turning on EV charger")
    else:
        log.warning(f"Force charge enabled, EV charger already on: {val}")


def turn_on_charger(reason: str = ""):
    charger_enabled = get(Charger.control_switch, False)
    if not charger_enabled:
        log.warning(f"Turning on ev charger {reason}")
        service.call("switch", "turn_on", entity_id=Charger.control_switch)
        task.sleep(5)
        new_state = get(Charger.control_switch, False)

        return new_state


def reset_charger_if_stuck(reason: str = ""):
    global last_charger_reset

    if last_charger_reset > now() - timedelta(minutes=5):
        log.warning(f"Charger still appears stuck, but reset cooldown is active. Reason: {reason}")
        return False

    wallbox_power = get(Charger.power, 0)
    connector_status = str(get(Charger.status_connector, "")).lower()
    switch_on = get(Charger.control_switch, False)
    physically_charging = wallbox_power > 300 or connector_status == "charging"

    if switch_on or physically_charging:
        log.warning(
            f"Resetting EV charger after ignored off command. switch={switch_on}, "
            f"wallbox_power={wallbox_power:.0f}W, connector={connector_status}. Reason: {reason}"
        )
        service.call("button", "press", entity_id=Charger.reset_button)
        last_charger_reset = now()
        return True

    return False


def turn_off_charger(reason: str = "", check_phase_change_cooldown=True, force=False):
    is_charging = get(Charger.control_switch, False)
    configured_phases = get(Charger.phases, 3)

    if get(Charger.force_charge, False) and not force:
        log.warning(f"Not turning off charging, force charge is on. Reason for request {reason}")
    elif is_charging:
        if (
            not force
            and check_phase_change_cooldown
            and configured_phases == 3
            and last_ev_charging_phase_change > now() - timedelta(minutes=15)
        ):
            log.warning(f"Phase change too frequent - cooldown active. Reason: {reason or 'no reason provided'}")
            return

        log.warning(f"Turning off EV charger. Reason for request: {reason}")
        service.call("switch", "turn_off", entity_id=Charger.control_switch)
        task.sleep(5)
        new_state = get(Charger.control_switch, False)
        if new_state:
            reset_charger_if_stuck(reason)
        else:
            task.sleep(10)
            reset_charger_if_stuck(reason)

        return new_state

    return get(Charger.control_switch, False)


def set_current(current, reason: str | None = None):
    configured_current = get(Charger.current_setting, -1)
    if configured_current != current:
        if not Const.min_current <= current <= Const.max_current:
            log.warning(
                f"Current out of bounds {current} - skipping current change. Reason: {reason or 'no reason provided'}"
            )
            return
        service.call("number", "set_value", entity_id=Charger.current_setting, value=current)
        log.warning(
            f"Setting current from {configured_current:.0f}A to {current}A. Reason: {reason or 'no reason provided'}"
        )
    else:
        log.warning(f"Current of {current} already set - skipping current change")


def set_phases_and_current(phases, current, reason: str | None = None):
    task.unique("control_ev_charging", kill_me=False)

    global last_ev_charging_phase_change

    charger_enabled = get(Charger.control_switch, False)
    configured_phases = get(Charger.phases, 3)
    configured_current = get(Charger.current_setting, -1)

    if not Const.min_current <= current <= Const.max_current:
        log.warning(
            f"Current out of bounds {current} - skipping change. Reason: {reason or 'no reason provided'}"
        )
        return False

    phase_change_needed = configured_phases != phases
    if not phase_change_needed:
        set_current(current, reason)
        return True

    if last_ev_charging_phase_change > now() - timedelta(minutes=15):
        log.warning(
            f"Phase change too frequent - cooldown active. Keeping existing {configured_phases}P-{configured_current}A. "
            f"Requested {phases}P-{current}A. Reason: {reason or 'no reason provided'}"
        )
        return False

    desc = f"ON->ON: {configured_phases}P-{configured_current}A -> {phases}P-{current}A"
    log.warning(f"{desc}. Reason: {reason or 'no reason provided'}")

    if charger_enabled:
        turn_off_charger(f"Phase change from {configured_phases} -> {phases}", check_phase_change_cooldown=False)

    service.call("vestel_ecv04", "set_phases_and_current", current=Const.max_current, num_phases=phases)
    last_ev_charging_phase_change = now()  # Update phase change timestamp
    log.warning(f"Phase change initiated - waiting {Const.ev_phase_switch_delay} seconds")
    task.sleep(Const.ev_phase_switch_delay)

    set_current(current, reason)
    log.warning(f"Phase change completed. Phase is now set to: {get(Charger.phases) or 'unknown'}")

    if charger_enabled:
        turn_on_charger(reason)

    return True


@time_trigger
@time_trigger("period(now, 30sec)")
def ev_energy_needed():
    """Calculate the energy needed to charge the EV to the required state of charge"""
    required_soc = get(EV.required_soc, 80)
    current_soc = get(EV.soc, 0)
    smart_charge_limit = get(EV.smart_charge_limit, required_soc)
    smart_limiter_active = get(Automation.auto_charge_limit, False)

    energy_needed = _get_ev_energy_needed(required_soc, current_soc, smart_charge_limit, smart_limiter_active)
    set_state(EV.energy_needed, energy_needed)


@pyscript_compile
def define_interfaces():
    from dataclasses import dataclass
    from datetime import datetime

    @dataclass
    class EVScheduleEntry:
        start: datetime
        end: datetime
        distance: float | None = None
        required_soc: float | None = None

    return EVScheduleEntry


EVScheduleEntry = define_interfaces()


@pyscript_compile
def parse_full_schedule(
    schedule_data: dict[str, list[dict[str, Any]]], default_required_soc: float
) -> list["EVScheduleEntry"]:
    from datetime import date, datetime

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
            soc = get_drive_required_soc(soc, distance, default_required_soc)

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
    ev_schedule = (schedule.get_schedule(entity_id=EV.planned_drives) or {}).get(
        EV.planned_drives, {}
    )
    ev_required_soc = get(EV.required_soc, 80)
    return parse_full_schedule(ev_schedule, default_required_soc=ev_required_soc)


@time_trigger
@time_trigger("period(now, 60sec)")
@state_active(
    f"{Charger.force_charge} == 'off' and {Automation.auto_ev_charging} == 'on' and ({Charger.ready} == 'on' or {Charger.control_switch} == 'on')"
)
async def auto_ev_charging():
    """Combined EV charging control with excess power, price and temperature awareness"""
    # log.warning(f"Auto ev charging active. Charger state: {get(Charger.ready, False) or get(Charger.control_switch, False)}")

    # ensure only one instance of this task is running (phase switching can take a while)
    task.unique("control_ev_charging", kill_me=True)

    # Configuration parameters from Const class
    Const.voltage = 230  # Volts (regional standard)

    # the current state of charge of the EV
    current_soc = get(EV.soc, -1)
    if current_soc < 0:
        log.warning("EV SOC is not set, cannot proceed with charging control.")
        return

    ev_schedule = get_ev_schedule()
    t_now = now()

    # required state of charge defined by the owner
    required_soc = get(EV.required_soc, 80)

    # the current excess power available, this is defined as power going into the battery or into the grid (or the opposite, depending on the sign)
    excess_power = get(Excess.power_1m_average, 1337)  # in W
    if excess_power == 1337:
        log.warning("Excess power is not set, cannot proceed with charging control.")
        return

    battery_soc = get(Battery.soc, 0)

    pv_total_power = get(PVProduction.power_now_estimated, 0)  # in W

    # target excess is the amount of power requested by the home battery to be able to cover the house loads in the near future
    # it is dynamically updated by a separate automation
    excess_target = get(Excess.target, 0)  # in W
    # surplus energy is the amount of energy that is likely available after accounting for house loads in the near future
    surplus_energy = get(House.energy_surplus, 0)  # in kWh

    # this is the maximum charge current that the vehicle should be charge with right now
    configured_current = get(Charger.current_setting, default=None, mapper=float)
    configured_phases = get(Charger.phases, default=None, mapper=int)
    if (
        configured_current is None
        or not Const.min_current <= configured_current <= Const.max_current
        or configured_phases not in (1, 3)
    ):
        log.warning(
            f"Invalid charger configuration state: phases={configured_phases}, current={configured_current}; "
            "holding the current charger state"
        )
        return
    wallbox_power = get(Charger.power, 0)

    smart_limiter_active = get(Automation.auto_charge_limit, False)
    ev_charge_limit = get(EV.smart_charge_limit, 80)

    energy_needed = get(EV.energy_needed, 0)  # in kWh
    battery_force_charge = get(Battery.force_charge_switch, False)

    inverter_mode = get(Victron.inverter_mode_sensor, default="off").lower()

    next_drive = None
    msg = ""
    # next drive is the point in time where the user needs to have the car charged to the required soc
    ongoing = get(EV.planned_drives, False)
    if ev_schedule is not None:
        ongoing, next_drive_event = get_ongoing_and_next_drive(ev_schedule, t_now)
        if next_drive_event:
            next_drive = next_drive_event.start
            if next_drive_event.required_soc:
                required_soc = next_drive_event.required_soc
                msg += (f"required soc defined via next drive event {required_soc}")
            elif next_drive_event.distance:
                energy_needed = min(Const.ev_capacity, next_drive_event.distance / 100 * Const.kwh_per_100km)
                required_soc = min(100, (energy_needed) / Const.ev_capacity * 100 + 10)
                msg += (f"setting required soc to {required_soc}")
            effective_required_soc = required_soc
            if smart_limiter_active:
                effective_required_soc = min(required_soc, ev_charge_limit)
            if effective_required_soc != required_soc:
                msg += f", limited by smart charge limit to {effective_required_soc}"
            required_soc = effective_required_soc
            energy_needed = max(0, required_soc - current_soc) / 100 * Const.ev_capacity

    else:
        next_drive = get_attr(EV.planned_drives, "next_event")

    is_charging = get(Charger.control_switch, False)

    if next_drive:
        next_drive = with_timezone(next_drive)

    # Calculate minimum time needed to charge the vehicle, we subtract 1 to account for charging inefficiencies
    min_hours_needed = energy_needed / (3 * Const.voltage * (Const.max_current - 1) / 1000)  # in hours

    # these are binary sensors defined separately that indicate whether the price is relatively low or high
    low_price = get(ElectricityPrices.low_price, False)
    high_price = get(ElectricityPrices.high_price, False)
    t_now = now()

    log.warning(
        f"Current SOC: {current_soc}%, Required SOC: {required_soc}%, Surplus {surplus_energy:.2f}, energy needed {energy_needed:.2f} "
        f"Excess: {excess_power:.0f} W, Target: {excess_target:.0f} W "
        f"Energy needed: {energy_needed:.2f} kWh, Time needed: {min_hours_needed:.2f}h, "
        f"low price: {low_price}, high price: {high_price}, next drive: {next_drive}, "
        f"EV charge limit: {ev_charge_limit:.0f}%"
    )

    #  -------------------------- Charging Strategy Logic -----------------------------------
    #  - when there's time to charge is running out to reach target SOC, charge with max current
    #  - when price is low and less than 24h left, charge with max current
    #  - when there's excess power and no time constraints, charge with excess power
    #  - when charging already active, control the charge amps to meet target excess
    #  - when price is high and time is not constrained, turn off the charger
    #  -------------------------------------------------------------------------------------
    action, phases, current, reason, charge_mode = _get_charge_action(
        next_drive=next_drive,
        current_soc=current_soc,
        required_soc=required_soc,
        energy_needed=energy_needed,
        excess_power=excess_power,
        excess_target=excess_target,
        surplus_energy=surplus_energy,
        smart_charge_limit=ev_charge_limit,
        smart_limiter_active=smart_limiter_active,
        configured_phases=configured_phases,
        configured_current=configured_current,
        is_low_price=low_price,
        pv_total_power=pv_total_power,
        battery_soc=battery_soc,
        hysteresis=HYSTERESIS_BUFFER,
        is_charging=is_charging,
        t_now=t_now,
        inverter_mode=inverter_mode,
        battery_force_charge=battery_force_charge,
        wallbox_power=wallbox_power,
    )

    hours_available_to_charge = ((next_drive - t_now).total_seconds() / 3600) if next_drive else 999

    log.warning(f"Got charge action: {action} mode {charge_mode} phases {phases} current {current}: {reason}")

    global pv_surplus_active
    global pv_surplus_started_at
    global pv_surplus_start_candidate_since
    global pv_surplus_stop_candidate_since
    global last_pv_surplus_stop

    is_pv_surplus_action = action == "on" and charge_mode == ChargeMode.surplus
    hard_stop = charge_mode == ChargeMode.hard_stop

    if is_pv_surplus_action:
        pv_surplus_stop_candidate_since = None

        if not is_charging:
            lockout_remaining = PV_SURPLUS_RESTART_LOCKOUT - (t_now - last_pv_surplus_stop)
            if lockout_remaining.total_seconds() > 0:
                pv_surplus_start_candidate_since = None
                log.warning(
                    f"Holding PV surplus charge off for another {lockout_remaining.total_seconds():.0f}s "
                    "due to restart lockout"
                )
                return

            if pv_surplus_start_candidate_since is None:
                pv_surplus_start_candidate_since = t_now
                log.warning("PV surplus start candidate detected; waiting for 3 minutes of stable headroom")
                return

            stable_for = t_now - pv_surplus_start_candidate_since
            if stable_for < PV_SURPLUS_START_CONFIRMATION:
                log.warning(
                    f"PV surplus start candidate stable for {stable_for.total_seconds():.0f}s; "
                    "waiting for 180s"
                )
                return

        changed = set_phases_and_current(phases, current, reason)
        if not changed:
            return

        if not is_charging and not turn_on_charger(reason):
            log.warning("PV surplus start command was not confirmed")
            return

        if not pv_surplus_active:
            pv_surplus_active = True
            pv_surplus_started_at = t_now
            set_pv_surplus_marker(True)
        pv_surplus_start_candidate_since = None

    elif action == "on":
        # Mandatory/deadline/cheap charging bypasses all PV timing guards.
        pv_surplus_start_candidate_since = None
        pv_surplus_stop_candidate_since = None
        if pv_surplus_active:
            pv_surplus_active = False
            pv_surplus_started_at = None
            set_pv_surplus_marker(False)

        changed = set_phases_and_current(phases, current, reason)
        if changed:
            turn_on_charger(reason)

    elif action == "off":
        pv_surplus_start_candidate_since = None

        if hard_stop:
            pv_surplus_stop_candidate_since = None
        elif is_charging and (pv_surplus_active or reason.startswith("ON->OFF")):
            if not pv_surplus_active:
                # Safe recovery after a script reload while a surplus charge is active.
                pv_surplus_active = True
                pv_surplus_started_at = t_now
                set_pv_surplus_marker(True)

            if pv_surplus_stop_candidate_since is None:
                pv_surplus_stop_candidate_since = t_now
                log.warning("PV surplus deficit detected; waiting for 5 minutes before stopping")
                return

            deficit_for = t_now - pv_surplus_stop_candidate_since
            run_time = t_now - pv_surplus_started_at
            if deficit_for < PV_SURPLUS_STOP_CONFIRMATION or run_time < PV_SURPLUS_MIN_RUN_TIME:
                log.warning(
                    f"Holding PV surplus charge on: deficit for {deficit_for.total_seconds():.0f}s/300s, "
                    f"run time {run_time.total_seconds():.0f}s/600s"
                )
                return

        turn_off_charger(reason, check_phase_change_cooldown=False, force=hard_stop)
        if get(Charger.control_switch, False):
            log.warning("EV charger stop command was not confirmed; keeping PV surplus state active")
            return

        if pv_surplus_active:
            pv_surplus_active = False
            pv_surplus_started_at = None
            last_pv_surplus_stop = t_now
            set_pv_surplus_marker(False)
        pv_surplus_stop_candidate_since = None
    else:
        log.warning(f"Skipping unknown action: {action}")
