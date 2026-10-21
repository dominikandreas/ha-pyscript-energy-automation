"""Full-horizon admission planning for discretionary EV charging.

The caller supplies a frozen, side-effect-free replay of the real house/EV
simulator. No second battery model, calendar, tariff, or SOC policy lives here.
Required/deadline charging remains the controller's fallback. Earlier charging
must improve that baseline; charging beyond the trip requirement must be paid
for by otherwise-unused energy, including at the end of the forecast.
"""
from datetime import datetime, timedelta
from math import isfinite

from energy_core import _get_ev_smart_charge_limit, get_ongoing_and_next_drive


@pyscript_compile
def ev_schedule_signature(schedule, t_now):
    ongoing, departure = get_ongoing_and_next_drive(schedule, t_now)
    return "|".join(
        f"{e.start.isoformat()}/{e.end.isoformat()}/{e.required_soc}/{e.distance}"
        for e in (ongoing, departure) if e is not None
    )


@pyscript_compile
def valid_ev_plan_slots(attributes, t_now, schedule_signature, ev_soc, battery_energy):
    """Fail closed for elective charging, without suppressing urgent charging."""
    try:
        if attributes.get("version") != 1:
            return {}
        calculated = datetime.fromisoformat(attributes["calculated_at"])
        age = (t_now - calculated).total_seconds()
        if not 0 <= age <= 300:
            return {}
        if attributes.get("schedule_signature") != schedule_signature:
            return {}
        source_soc = float(attributes["source_ev_soc"])
        source_battery = float(attributes["source_battery_kwh"])
        if not all(isfinite(v) for v in (source_soc, source_battery, ev_soc, battery_energy)):
            return {}
        # A drive, changed SOC estimate, or unexpected battery draw invalidates
        # the counterfactual. Normal charging/SOC quantization remains valid.
        if ev_soc < source_soc - 2 or battery_energy < source_battery - 1:
            return {}
        slots = attributes.get("slots", {})
        return slots if isinstance(slots, dict) else {}
    except (TypeError, ValueError, KeyError, AttributeError):
        return {}


@pyscript_compile
def ev_plan_slot(slots, t_now):
    """Find the interval containing now; never carry a request past its end."""
    for start, slot in (slots or {}).items():
        try:
            begin = datetime.fromisoformat(start)
            end = datetime.fromisoformat(slot["end"])
            power = float(slot["power_w"])
            target = float(slot["target_soc"])
            if begin <= t_now < end and isfinite(power) and isfinite(target):
                return max(0.0, min(11040.0, power)), max(0.0, min(100.0, target))
        except (TypeError, ValueError, KeyError):
            continue
    return 0.0, 0.0


@pyscript_compile
def ev_replay_metrics(result, t_now, period_hours):
    grid = cost = wall = 0.0
    for entry in result.detail:
        hours = max(0.0, period_hours - max(0.0, (t_now - entry.period_start).total_seconds() / 3600))
        imported = max(0.0, entry.power_from_grid) * hours / 1000
        grid += imported
        cost += imported * entry.electricity_price
        wall += entry.ev_charge_power * hours / 1000
    return grid, cost, wall, result.detail[-1].battery_energy


@pyscript_compile
def build_ev_charge_plan(replay, frozen_inputs):
    """Admit PV slots by replaying their complete downstream consequences.

    First move required charging to PV only when total import/cost improve.
    Then admit extra charging against that fixed trip-serving baseline. Both
    passes use a descending PV-headroom order and at most two replays per PV
    slot. This is bounded greedy admission, not a claim of global optimality.
    """
    t_now = frozen_inputs["t_now"]
    forecast = frozen_inputs["forecast"]
    period_hours = (forecast[1].period_start - forecast[0].period_start).total_seconds() / 3600
    inputs = dict(frozen_inputs)
    inputs["ev_charge_plan"] = {}
    inputs["ev_battery_support_energy_kwh"] = 0
    inputs["allow_battery_precharge"] = False
    inputs["ev_battery_precharge_budget_kwh"] = 0
    # Never fund discretionary charging with a planned optional export or with
    # the old rolling solar-cycle surplus accumulator.
    inputs["optional_export_power_schedule"] = {}
    inputs["battery_export_budget_kwh"] = 0
    inputs["surplus_energy"] = 0
    baseline = replay(**inputs)
    accepted = baseline
    metrics = ev_replay_metrics(accepted, t_now, period_hours)
    initial_metrics = metrics
    plan = {}
    candidates = []
    for entry in baseline.detail:
        start = entry.period_start
        end = start + timedelta(hours=period_hours)
        if end <= t_now + timedelta(minutes=3):
            continue
        local_power = max(0.0, entry.pv_estimate - entry.house_power)
        if local_power < 1680:
            continue
        _, departure = get_ongoing_and_next_drive(inputs.get("ev_schedule"), start)
        required = departure.required_soc if departure and departure.required_soc is not None else inputs["ev_required_soc"]
        smart = _get_ev_smart_charge_limit(departure.start, start) if departure else 80.0
        target = min(required, smart) if inputs["smart_limiter_active"] else required
        # Highest local production first; prefer later equally productive slots
        # to avoid storing unnecessary energy in the EV far before departure.
        candidates.append((local_power, start, end, target, max(target, smart)))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    replay_count = 1
    for optional in (False, True):
        protected = metrics
        for power, start, end, required, ceiling in candidates:
            proposed = dict(plan)
            proposed[start.isoformat()] = {
                "end": end.isoformat(),
                "power_w": min(11040.0, power),
                "target_soc": ceiling if optional else required,
            }
            candidate = replay(**dict(inputs, ev_charge_plan=proposed))
            replay_count += 1
            measure = ev_replay_metrics(candidate, t_now, period_hours)
            if optional:
                accept = (
                    measure[0] <= protected[0] + 0.005
                    and measure[1] <= protected[1] + 0.002
                    and measure[3] >= protected[3] - 0.005
                    and measure[2] > metrics[2] + 0.02
                )
            else:
                accept = (
                    measure[0] <= metrics[0] + 0.005
                    and measure[1] <= metrics[1] + 0.002
                    and (measure[0] < metrics[0] - 0.02 or measure[1] < metrics[1] - 0.005)
                )
            if accept:
                plan, accepted, metrics = proposed, candidate, measure
        if not optional:
            required_metrics = metrics
    return {
        "slots": plan,
        "baseline_grid_kwh": round(initial_metrics[0], 4),
        "required_plan_grid_kwh": round(required_metrics[0], 4),
        "planned_grid_kwh": round(metrics[0], 4),
        "planned_grid_cost": round(metrics[1], 4),
        "planned_ev_wall_kwh": round(metrics[2], 4),
        "terminal_battery_kwh": round(metrics[3], 4),
        "replay_count": replay_count,
    }
