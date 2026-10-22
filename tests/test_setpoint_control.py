import ast
import inspect
import re
import unittest
from datetime import datetime, timedelta, timezone
from math import isfinite, nan
from pathlib import Path
from types import SimpleNamespace

from modules import export_scheduler
from modules.setpoint_control import (
    FeedinCandidate,
    calculate_auto_max_setpoint,
    apply_hard_feedin_control,
    apply_silent_mode_grid_limits,
    build_price_aware_headroom_schedule,
    build_silent_charge_shaping_schedule,
    calculate_energy_bounded_control,
    calculate_headroom_energy_requirement,
    calculate_pv_overflow_energy,
    choose_stable_candidate,
    feedin_constraint_exceeded,
    limit_target_step,
    resolve_economic_export_floor,
)


def _expanded_forecast_keywords(func_node):
    """Collect ``forecast(...)`` keywords, resolving the shared base dict.

    ``forecast_surplus`` passes its common inputs via
    ``**base_forecast_kwargs``; tests that pin per-pass wiring must see the
    effective keywords, so ``**`` entries are expanded from the
    ``base_forecast_kwargs = dict(...)`` assignment in the same scope.
    """

    base_keywords = {}
    for node in ast.walk(func_node):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "base_forecast_kwargs" for target in node.targets)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "dict"
        ):
            base_keywords.update(
                {keyword.arg: keyword.value for keyword in node.value.keywords if keyword.arg is not None}
            )
    calls = [
        node
        for node in ast.walk(func_node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "forecast"
    ]
    expanded = []
    for call in calls:
        keywords = dict(base_keywords)
        keywords.update({keyword.arg: keyword.value for keyword in call.keywords if keyword.arg is not None})
        expanded.append(keywords)
    return expanded


class SetpointControlTests(unittest.TestCase):
    def setUp(self):
        self.t_now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

    def test_active_drive_uses_quantitative_ev_export_budget(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_setpoint_target"
        )
        target_source = ast.unparse(target)

        self.assertIn("calculate_export_energy_budget", target_source)
        self.assertNotIn(
            "if ev_arrival_reservation_active:\n    safe_export_energy_kwh = 0.0",
            target_source,
        )

    def test_fast_loop_has_no_import_forcing_discharge_guard(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        target_source = ast.unparse(target)

        # No import-forcing guard machinery: the fast loop must never pin a
        # positive grid import on stale telemetry or while the EV charges.
        for removed in (
            "select_ev_discharge_guard_target",
            "apply_ev_discharge_guard",
            "battery_discharge_guard_target_w",
            "telemetry_safe_grid_target_w",
            "ev_discharge_guard_active",
            "is_ev_arrival_precharge_active",
            "calculate_battery_neutral_grid_target_w",
        ):
            self.assertNotIn(removed, target_source)
        # Battery protection stays on the PV-only floor (never an import).
        self.assertIn(
            "pv_only_grid_target_w=pv_only_setpoint",
            target_source,
        )
        self.assertIn(
            "if setpoint < pv_only_setpoint",
            target_source,
        )
        # Stale telemetry holds the last setpoint and disables discharge.
        self.assertIn("if not measurements_available", target_source)
        self.assertIn("prev_telemetry_hold_active", target_source)
        self.assertIn("set_state(Battery.discharge_limit, 0)", target_source)
        self.assertIn("select_finite_grid_target", target_source)
        self.assertIn("LIVE_TELEMETRY_FAILSAFE_IMPORT_TARGET_W", target_source)

    def test_auto_apply_setpoint_does_not_force_import_for_malformed_plan(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        target.decorator_list = []

        now_value = datetime(2026, 9, 12, 7, 24, tzinfo=timezone.utc)
        symbols = {
            name: SimpleNamespace(**{field: f"{name}.{field}" for field in fields})
            for name, fields in {
                "Automation": ("auto_setpoint", "min_discharge_price", "minimal_soc", "silent_mode"),
                "Battery": (
                "capacity",
                "charge_limit",
                "discharge_limit",
                "energy",
                "power",
                "soc",
            ),
                "Charger": ("control_switch", "ready"),
                "EV": ("able_to_charge_on_arrival", "energy", "energy_needed", "is_charging", "plugged_in", "smart_charge_limit"),
                "ElectricityPrices": ("current_price",),
                "Grid": ("max_feedin_target", "max_pv_feedin_target", "max_setpoint", "power_ac", "power_setpoint", "power_setpoint_target"),
                "House": ("daily_average_power", "energy_surplus", "loads"),
                "PVProduction": ("total_power",),
            }.items()
        }
        symbols["EVConst"] = SimpleNamespace(
            ev_capacity=60.0,
            min_current=6.0,
            voltage=230.0,
        )
        values = {
            symbols["Automation"].auto_setpoint: True,
            symbols["Automation"].min_discharge_price: 0.212,
            symbols["Automation"].minimal_soc: 5.0,
            symbols["Battery"].capacity: 32.0,
            symbols["Battery"].charge_limit: -1.0,
            symbols["Battery"].discharge_limit: -1.0,
            symbols["Battery"].energy: 15.81,
            symbols["Battery"].soc: 49.4,
            symbols["Battery"].power: 0.782031982421875,
            symbols["Charger"].control_switch: True,
            symbols["Charger"].ready: False,
            symbols["EV"].able_to_charge_on_arrival: True,
            symbols["EV"].energy: 44.4,
            symbols["EV"].energy_needed: 0.0,
            symbols["EV"].is_charging: True,
            symbols["EV"].plugged_in: True,
            symbols["EV"].smart_charge_limit: 80.0,
            symbols["ElectricityPrices"].current_price: 0.2875,
            symbols["Grid"].max_feedin_target: 5500.0,
            symbols["Grid"].max_pv_feedin_target: 5500.0,
            symbols["Grid"].max_setpoint: -100.0,
            symbols["Grid"].power_ac: 8.3082,
            symbols["Grid"].power_setpoint: 7485.0,
            symbols["Grid"].power_setpoint_target: -100.0,
            symbols["House"].daily_average_power: 500.0,
            symbols["House"].energy_surplus: 23.32,
            symbols["House"].loads: 500.0,
            symbols["PVProduction"].total_power: 2568.4,
            "pyscript.export_policy_budget_ledger": "",
        }
        target_attributes = {
            "unified_scheduler_active": True,
            "planned_optional_export_power_w": "unavailable",
            "planned_battery_power_w": "unavailable",
            "planned_max_grid_export_w": 5500.0,
            "planned_battery_floor_kwh": 2.6,
            "ev_available_for_charging": True,
            "ev_is_charging": True,
            "ev_energy_needed_kwh": 0.0,
            "plan_calculated_at": now_value.isoformat(),
            "export_policy_calculated_at": now_value.isoformat(),
            "export_policy_valid": True,
            "safe_export_energy_kwh": 11.412,
            "safe_export_source_battery_kwh": 15.81,
            "safe_export_calculated_at": now_value.isoformat(),
        }

        def get(entity, default=None):
            return values.get(entity, default)

        def get_attr(entity, attribute, default=None):
            if entity == symbols["Grid"].power_setpoint_target:
                return target_attributes.get(attribute, default)
            if attribute in ("last_updated", "last_changed"):
                return now_value
            return default

        actuator_calls = []
        logged_warnings = []
        inactive_headroom = SimpleNamespace(
            active=False,
            target_headroom_kwh=0.0,
            hold_until=None,
            requested_headroom_kwh=0.0,
            natural_export_w=0.0,
            headroom_deficit_kwh=0.0,
            can_create_headroom=False,
            desired_grid_target_w=-100.0,
            available_export_margin_w=0.0,
        )
        valid_budget = SimpleNamespace(
            valid=True,
            reason="valid",
            ev_adjusted_spendable_energy_kwh=11.412,
            source_age_seconds=0.0,
            battery_draw_since_calculation_kwh=0.0,
        )
        valid_ledger = SimpleNamespace(
            valid=True,
            reason="valid",
            remaining_kwh=11.412,
            state_value="ledger",
        )
        namespace = {
            **symbols,
            "get": get,
            "get_attr": get_attr,
            "set_state": lambda entity, value, **attributes: actuator_calls.append((entity, value, attributes)),
            "state": SimpleNamespace(set=lambda *args, **kwargs: None),
            "log": SimpleNamespace(warning=lambda *args, **kwargs: logged_warnings.append(args[0] if args else "")),
            "now": lambda: now_value,
            "prev_applied_grid_target_w": None,
            "prev_house_loads": 500.0,
            "prev_optional_export_ev_plan_hold": False,
            "live_headroom_active": False,
            "live_headroom_target_kwh": 0.0,
            "live_headroom_hold_until": None,
            "prev_live_headroom_mode": "normal",
            "last_live_headroom_publish": now_value,
            "prev_telemetry_hold_active": False,
            "prev_discharge_limit_before_hold": None,
            "prev_telemetry_good_streak": 0,
            "prev_telemetry_bad_streak": 0,
            "prev_telemetry_hold_boot_checked": False,
            "LIVE_EXPORT_BUDGET_RELEASE_KWH": 0.05,
            "HOME_BATTERY_CHARGE_EFFICIENCY": 0.95,
            "HOME_BATTERY_DISCHARGE_EFFICIENCY": 0.95,
            "LIVE_BATTERY_ENERGY_MAX_MEASUREMENT_AGE_SECONDS": 90.0,
            "LIVE_EXPORT_BUDGET_HORIZON_SECONDS": 120.0,
            "LIVE_HEADROOM_ACTIVATION_BUFFER_KWH": 0.02,
            "LIVE_HEADROOM_ACTIVATION_LEAD_TIME_MINUTES": 60.0,
            "LIVE_HEADROOM_BASE_KWH": 0.15,
            "LIVE_HEADROOM_HOLD_MINUTES": 15.0,
            "LIVE_HEADROOM_MAX_KWH": 1.5,
            "LIVE_HEADROOM_MAX_MEASUREMENT_AGE_SECONDS": 10.0,
            "LIVE_HEADROOM_MAX_MEASUREMENT_SKEW_SECONDS": 5.0,
            "LIVE_HEADROOM_MAX_TARGET_STEP_W": 1500.0,
            "LIVE_HEADROOM_PUBLISH_INTERVAL_SECONDS": 30.0,
            "LIVE_HEADROOM_RELEASE_HYSTERESIS_W": 250.0,
            "LIVE_HEADROOM_RISK_HORIZON_MINUTES": 30.0,
            "LIVE_HEADROOM_TRACKING_MARGIN_W": 250.0,
            "LIVE_HOUSE_LOAD_MAX_MEASUREMENT_AGE_SECONDS": 10.0,
            "LIVE_POWER_MAX_ABSOLUTE_W": 20000.0,
            "LIVE_PV_MAX_MEASUREMENT_AGE_SECONDS": 30.0,
            "LIVE_TELEMETRY_FAILSAFE_IMPORT_TARGET_W": 0.0,
            "adapt_grid_target_to_live_power": export_scheduler.adapt_grid_target_to_live_power,
            "select_finite_grid_target": export_scheduler.select_finite_grid_target,
            "are_battery_energy_values_valid": lambda *args, **kwargs: True,
            "are_power_measurement_values_valid": lambda *args, **kwargs: True,
            "are_power_measurements_coherent": lambda *args, **kwargs: True,
            "calculate_live_headroom_control": lambda *args, **kwargs: inactive_headroom,
            "calculate_measurement_age_seconds": lambda *args, **kwargs: 0.0,
            "calculate_measurement_skew_seconds": lambda *args, **kwargs: 0.0,
            "calculate_time_to_full_minutes": lambda *args, **kwargs: 120.0,
            "get_battery_export_floor": lambda *args, **kwargs: 2.6,
            "get_fresh_export_policy_budget": lambda *args, **kwargs: valid_budget,
            "get_pv_only_setpoint": lambda *args, **kwargs: 0.0,
            "get_reserve_soc": lambda: 5.0,
            "is_battery_energy_measurement_usable": (
                export_scheduler.is_battery_energy_measurement_usable
            ),
            "is_ev_available_for_charging": lambda *args, **kwargs: True,
            "is_ev_plan_fresh": lambda *args, **kwargs: True,
            "is_measurement_fresh": lambda *args, **kwargs: True,
            "limit_live_export_target_to_budget": export_scheduler.limit_live_export_target_to_budget,
            "limit_target_step": limit_target_step,
            "parse_aware_datetime": export_scheduler.parse_aware_datetime,
            "resolve_directional_power_limit_w": lambda raw, fallback, maximum: maximum,
            "select_no_battery_export_target": lambda pv_only_grid_target_w, neutral_grid_target_w, pv_measurement_fresh: pv_only_grid_target_w,
            "update_live_headroom_state": lambda *args, **kwargs: inactive_headroom,
            "update_monotonic_export_budget_ledger": lambda *args, **kwargs: valid_ledger,
            "validate_pv_power_measurement": lambda pv_power_w, updated_at, current_time, max_age_seconds: (True, True),
        }
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[target], type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )

        namespace["auto_apply_setpoint"]()

        actuator_call = next(
            call
            for call in reversed(actuator_calls)
            if call[0] == symbols["Grid"].power_setpoint
        )
        # Malformed plan + active EV charging follows the configured max
        # setpoint with no import-forcing guard on top.
        self.assertEqual(actuator_call[1], -100)
        # The transition must be logged with the rejection context so the
        # applied target is diagnosable from the log alone.
        applied_logs = [message for message in logged_warnings if "Applied grid setpoint" in message]
        self.assertEqual(len(applied_logs), 1)
        self.assertIn("Applied grid setpoint -100 W (was unknown)", applied_logs[0])
        self.assertIn("rejection=", applied_logs[0])

    def test_stale_power_measurements_hold_setpoint_and_recover(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        target.decorator_list = []

        now_value = datetime(2026, 9, 12, 7, 24, tzinfo=timezone.utc)
        symbols = {
            name: SimpleNamespace(**{field: f"{name}.{field}" for field in fields})
            for name, fields in {
                "Automation": ("auto_setpoint", "min_discharge_price", "minimal_soc", "silent_mode"),
                "Battery": (
                "capacity",
                "charge_limit",
                "discharge_limit",
                "energy",
                "power",
                "soc",
            ),
                "Charger": ("control_switch", "ready"),
                "EV": ("able_to_charge_on_arrival", "energy", "energy_needed", "is_charging", "plugged_in", "smart_charge_limit"),
                "ElectricityPrices": ("current_price",),
                "Grid": ("max_feedin_target", "max_pv_feedin_target", "max_setpoint", "power_ac", "power_setpoint", "power_setpoint_target"),
                "House": ("daily_average_power", "energy_surplus", "loads"),
                "PVProduction": ("total_power",),
            }.items()
        }
        symbols["EVConst"] = SimpleNamespace(
            ev_capacity=60.0,
            min_current=6.0,
            voltage=230.0,
        )
        values = {
            symbols["Automation"].auto_setpoint: True,
            symbols["Automation"].min_discharge_price: 0.212,
            symbols["Automation"].minimal_soc: 5.0,
            symbols["Battery"].capacity: 32.0,
            symbols["Battery"].charge_limit: -1.0,
            symbols["Battery"].discharge_limit: -1.0,
            symbols["Battery"].energy: 15.81,
            symbols["Battery"].soc: 49.4,
            symbols["Battery"].power: 0.782031982421875,
            symbols["Charger"].control_switch: True,
            symbols["Charger"].ready: False,
            symbols["EV"].able_to_charge_on_arrival: True,
            symbols["EV"].energy: 44.4,
            symbols["EV"].energy_needed: 0.0,
            symbols["EV"].is_charging: False,
            symbols["EV"].plugged_in: True,
            symbols["EV"].smart_charge_limit: 80.0,
            symbols["ElectricityPrices"].current_price: 0.2875,
            symbols["Grid"].max_feedin_target: 5500.0,
            symbols["Grid"].max_pv_feedin_target: 5500.0,
            symbols["Grid"].max_setpoint: -100.0,
            symbols["Grid"].power_ac: 8.3082,
            symbols["Grid"].power_setpoint: -100.0,
            symbols["Grid"].power_setpoint_target: -100.0,
            symbols["House"].daily_average_power: 500.0,
            symbols["House"].energy_surplus: 23.32,
            symbols["House"].loads: 500.0,
            symbols["PVProduction"].total_power: 2568.4,
            "pyscript.export_policy_budget_ledger": "",
        }
        target_attributes = {
            "unified_scheduler_active": True,
            "planned_optional_export_power_w": 0.0,
            "planned_battery_power_w": 0.0,
            "planned_max_grid_export_w": 5500.0,
            "planned_battery_floor_kwh": 2.6,
            "ev_available_for_charging": True,
            "ev_is_charging": False,
            "ev_energy_needed_kwh": 0.0,
            "plan_calculated_at": now_value.isoformat(),
            "export_policy_calculated_at": now_value.isoformat(),
            "export_policy_valid": True,
            "safe_export_energy_kwh": 11.412,
            "safe_export_source_battery_kwh": 15.81,
            "safe_export_calculated_at": now_value.isoformat(),
        }
        power_timestamps = {
            "stale": now_value - timedelta(seconds=400),
            "fresh": now_value,
        }
        power_freshness = {"stale": True}
        persisted_states = {}

        def get(entity, default=None):
            if entity in persisted_states:
                return persisted_states[entity]
            return values.get(entity, default)

        def record_state(entity, value, **attributes):
            actuator_calls.append((entity, value, attributes))
            # Mirror Home Assistant: a written entity reads back the new value.
            values[entity] = value

        def persist_state(entity, value=None, **attributes):
            persisted_states[entity] = value

        def get_attr(entity, attribute, default=None):
            if entity == symbols["Grid"].power_setpoint_target:
                return target_attributes.get(attribute, default)
            if attribute in ("last_updated", "last_changed"):
                if entity in (symbols["Grid"].power_ac, symbols["Battery"].power):
                    return power_timestamps["fresh" if not power_freshness["stale"] else "stale"]
                return now_value
            return default

        actuator_calls = []
        logged_warnings = []
        inactive_headroom = SimpleNamespace(
            active=False,
            target_headroom_kwh=0.0,
            hold_until=None,
            requested_headroom_kwh=0.0,
            natural_export_w=0.0,
            headroom_deficit_kwh=0.0,
            can_create_headroom=False,
            desired_grid_target_w=-100.0,
            available_export_margin_w=0.0,
        )
        valid_budget = SimpleNamespace(
            valid=True,
            reason="valid",
            ev_adjusted_spendable_energy_kwh=11.412,
            source_age_seconds=0.0,
            battery_draw_since_calculation_kwh=0.0,
        )
        valid_ledger = SimpleNamespace(
            valid=True,
            reason="valid",
            remaining_kwh=11.412,
            state_value="ledger",
        )
        namespace = {
            **symbols,
            "get": get,
            "get_attr": get_attr,
            "set_state": record_state,
            "state": SimpleNamespace(set=persist_state),
            "log": SimpleNamespace(warning=lambda *args, **kwargs: logged_warnings.append(args[0] if args else "")),
            "now": lambda: now_value,
            "prev_applied_grid_target_w": -100,
            "prev_house_loads": 500.0,
            "prev_optional_export_ev_plan_hold": False,
            "live_headroom_active": False,
            "live_headroom_target_kwh": 0.0,
            "live_headroom_hold_until": None,
            "prev_live_headroom_mode": "normal",
            "last_live_headroom_publish": now_value,
            "prev_telemetry_hold_active": False,
            "prev_discharge_limit_before_hold": None,
            "prev_telemetry_good_streak": 0,
            "prev_telemetry_bad_streak": 0,
            "prev_telemetry_hold_boot_checked": False,
            "LIVE_EXPORT_BUDGET_RELEASE_KWH": 0.05,
            "HOME_BATTERY_CHARGE_EFFICIENCY": 0.95,
            "HOME_BATTERY_DISCHARGE_EFFICIENCY": 0.95,
            "LIVE_BATTERY_ENERGY_MAX_MEASUREMENT_AGE_SECONDS": 90.0,
            "LIVE_EXPORT_BUDGET_HORIZON_SECONDS": 120.0,
            "LIVE_HEADROOM_ACTIVATION_BUFFER_KWH": 0.02,
            "LIVE_HEADROOM_ACTIVATION_LEAD_TIME_MINUTES": 60.0,
            "LIVE_HEADROOM_BASE_KWH": 0.15,
            "LIVE_HEADROOM_HOLD_MINUTES": 15.0,
            "LIVE_HEADROOM_MAX_KWH": 1.5,
            "LIVE_HEADROOM_MAX_MEASUREMENT_AGE_SECONDS": 10.0,
            "LIVE_HEADROOM_MAX_MEASUREMENT_SKEW_SECONDS": 5.0,
            "LIVE_HEADROOM_MAX_TARGET_STEP_W": 1500.0,
            "LIVE_HEADROOM_PUBLISH_INTERVAL_SECONDS": 30.0,
            "LIVE_HEADROOM_RELEASE_HYSTERESIS_W": 250.0,
            "LIVE_HEADROOM_RISK_HORIZON_MINUTES": 30.0,
            "LIVE_HEADROOM_TRACKING_MARGIN_W": 250.0,
            "LIVE_HOUSE_LOAD_MAX_MEASUREMENT_AGE_SECONDS": 10.0,
            "LIVE_POWER_MAX_ABSOLUTE_W": 20000.0,
            "LIVE_PV_MAX_MEASUREMENT_AGE_SECONDS": 30.0,
            "LIVE_TELEMETRY_FAILSAFE_IMPORT_TARGET_W": 0.0,
            "adapt_grid_target_to_live_power": export_scheduler.adapt_grid_target_to_live_power,
            "select_finite_grid_target": export_scheduler.select_finite_grid_target,
            "are_battery_energy_values_valid": lambda *args, **kwargs: True,
            "are_power_measurement_values_valid": lambda *args, **kwargs: True,
            "are_power_measurements_coherent": export_scheduler.are_power_measurements_coherent,
            "calculate_live_headroom_control": lambda *args, **kwargs: inactive_headroom,
            "calculate_measurement_age_seconds": export_scheduler.calculate_measurement_age_seconds,
            "calculate_measurement_skew_seconds": lambda *args, **kwargs: 0.0,
            "calculate_time_to_full_minutes": lambda *args, **kwargs: 120.0,
            "get_battery_export_floor": lambda *args, **kwargs: 2.6,
            "get_fresh_export_policy_budget": lambda *args, **kwargs: valid_budget,
            "get_pv_only_setpoint": lambda *args, **kwargs: 0.0,
            "get_reserve_soc": lambda: 5.0,
            "is_battery_energy_measurement_usable": (
                export_scheduler.is_battery_energy_measurement_usable
            ),
            "is_ev_available_for_charging": lambda *args, **kwargs: True,
            "is_ev_plan_fresh": lambda *args, **kwargs: True,
            "is_measurement_fresh": lambda *args, **kwargs: True,
            "limit_live_export_target_to_budget": export_scheduler.limit_live_export_target_to_budget,
            "limit_target_step": limit_target_step,
            "parse_aware_datetime": export_scheduler.parse_aware_datetime,
            "resolve_directional_power_limit_w": lambda raw, fallback, maximum: maximum,
            "select_no_battery_export_target": lambda pv_only_grid_target_w, neutral_grid_target_w, pv_measurement_fresh: pv_only_grid_target_w,
            "update_live_headroom_state": lambda *args, **kwargs: inactive_headroom,
            "update_monotonic_export_budget_ledger": lambda *args, **kwargs: valid_ledger,
            "validate_pv_power_measurement": lambda pv_power_w, updated_at, current_time, max_age_seconds: (True, True),
        }
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[target], type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )

        def writes_to(entity):
            return [call for call in actuator_calls if call[0] == entity]

        # A single skewed sample is jitter: no hold, writes continue.
        namespace["auto_apply_setpoint"]()
        self.assertTrue(writes_to(symbols["Grid"].power_setpoint))
        self.assertEqual(writes_to(symbols["Battery"].discharge_limit), [])
        self.assertFalse(namespace["prev_telemetry_hold_active"])

        # Second consecutive bad cycle: the hold engages, the setpoint is
        # left untouched and discharge is disabled.
        actuator_calls.clear()
        logged_warnings.clear()
        namespace["auto_apply_setpoint"]()
        self.assertEqual(writes_to(symbols["Grid"].power_setpoint), [])
        discharge_writes = writes_to(symbols["Battery"].discharge_limit)
        self.assertEqual([call[1] for call in discharge_writes], [0])
        hold_logs = [message for message in logged_warnings if "holding last" in message]
        self.assertEqual(len(hold_logs), 1)
        self.assertIn("holding last grid setpoint (-100 W)", hold_logs[0])
        self.assertTrue(namespace["prev_telemetry_hold_active"])
        self.assertEqual(
            persisted_states.get("pyscript.telemetry_hold_discharge_limit"),
            "-1.0",
        )

        # Simulate a reload during the hold: the volatile latch is lost, but
        # the persisted pre-hold limit lets the next cycle resume the hold
        # instead of stranding a disabled discharge with no owner.
        namespace["prev_telemetry_hold_active"] = False
        namespace["prev_discharge_limit_before_hold"] = None
        namespace["prev_telemetry_good_streak"] = 0
        namespace["prev_telemetry_bad_streak"] = 0
        namespace["prev_telemetry_hold_boot_checked"] = False
        actuator_calls.clear()
        logged_warnings.clear()
        namespace["auto_apply_setpoint"]()
        self.assertEqual(writes_to(symbols["Grid"].power_setpoint), [])
        self.assertEqual(writes_to(symbols["Battery"].discharge_limit), [])
        self.assertTrue(namespace["prev_telemetry_hold_active"])
        self.assertEqual(namespace["prev_discharge_limit_before_hold"], -1.0)
        resume_logs = [message for message in logged_warnings if "after restart" in message]
        self.assertEqual(len(resume_logs), 1)

        # A single fresh sample does not release the hold yet.
        actuator_calls.clear()
        logged_warnings.clear()
        power_freshness["stale"] = False
        namespace["auto_apply_setpoint"]()
        self.assertEqual(writes_to(symbols["Grid"].power_setpoint), [])
        self.assertEqual(writes_to(symbols["Battery"].discharge_limit), [])
        self.assertTrue(namespace["prev_telemetry_hold_active"])

        # Second consecutive fresh sample: discharge limit restored, writes resume.
        actuator_calls.clear()
        logged_warnings.clear()
        namespace["auto_apply_setpoint"]()
        restored_writes = writes_to(symbols["Battery"].discharge_limit)
        self.assertEqual([call[1] for call in restored_writes], [-1.0])
        resumed_writes = writes_to(symbols["Grid"].power_setpoint)
        self.assertTrue(resumed_writes)
        recovery_logs = [message for message in logged_warnings if "recovered" in message]
        self.assertEqual(len(recovery_logs), 1)
        self.assertFalse(namespace["prev_telemetry_hold_active"])
        self.assertEqual(
            persisted_states.get("pyscript.telemetry_hold_discharge_limit"),
            "",
        )

    def test_stale_telemetry_holds_setpoint_and_disables_discharge(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        target_source = ast.unparse(target)

        # The NaN backstop stays at 0 and never grows into a pinned import.
        self.assertIn(
            "LIVE_TELEMETRY_FAILSAFE_IMPORT_TARGET_W = 0.0",
            source,
        )
        self.assertNotIn("10000", source)
        # EV charging still follows the configured max setpoint.
        self.assertIn("if ev_is_charging:", target_source)
        self.assertNotIn(
            "if ev_is_charging and not setpoint_input_invalid",
            target_source,
        )
        # Stale telemetry holds instead of falling back: no blind target is
        # pinned, and discharge is disabled until fresh data arrives.
        self.assertNotIn("telemetry_safe_grid_target_w", target_source)
        self.assertNotIn("battery_discharge_guard_target_w", target_source)
        self.assertIn("if not measurements_available:", target_source)
        self.assertIn("prev_telemetry_hold_active", target_source)
        self.assertIn("prev_telemetry_bad_streak", target_source)
        self.assertIn("prev_telemetry_good_streak", target_source)
        self.assertIn("set_state(Battery.discharge_limit, 0)", target_source)
        self.assertIn("pyscript.telemetry_hold_discharge_limit", target_source)

    def test_planned_discharge_limit_ignores_telemetry_hold_zero(self):
        # 2026-09-24: the planner sampled the discharge-limit input while a
        # seconds-long telemetry hold had parked it at 0, publishing a
        # day-long forecast with a frozen battery and grid import all night.
        # While the hold latch is set, planning uses the pre-hold value.
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "get_planned_discharge_limit_w"
        )
        target.decorator_list = []
        values = {}
        namespace = {
            "get": lambda entity, default=None: values.get(entity, default),
            "Battery": SimpleNamespace(discharge_limit="Battery.discharge_limit"),
        }
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[target], type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )
        helper = namespace["get_planned_discharge_limit_w"]

        values["pyscript.telemetry_hold_discharge_limit"] = "-1.0"
        values["Battery.discharge_limit"] = 0.0
        self.assertEqual(helper(-1), -1.0)

        # No latch: a sustained zero (price automation, inverter ban) still
        # reaches the simulation unchanged.
        values["pyscript.telemetry_hold_discharge_limit"] = ""
        values["Battery.discharge_limit"] = 0.0
        self.assertEqual(helper(-1), 0.0)

        # Normal operation passes the live input through.
        values["Battery.discharge_limit"] = -1.0
        self.assertEqual(helper(-1), -1.0)

        # An unparseable latch falls back to the live input, never to a
        # fabricated capability.
        values["pyscript.telemetry_hold_discharge_limit"] = "stuck"
        values["Battery.discharge_limit"] = 0.0
        self.assertEqual(helper(-1), 0.0)

    def test_telemetry_hold_latch_brackets_the_zero_write(self):
        # The planner trusts the latch to explain a hold-owned 0, so the
        # latch must bracket the input write on both transitions: persist
        # before parking at 0 on engage, restore before clearing on release.
        # Either order leaves no window with an unexplained 0.
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        branches = {}
        for node in ast.walk(target):
            if not isinstance(node, ast.If):
                continue
            test_source = ast.unparse(node.test)
            if "prev_telemetry_bad_streak >= 2" in test_source:
                branches["engage"] = ast.unparse(node)
            elif "prev_telemetry_good_streak >= 2" in test_source:
                branches["release"] = ast.unparse(node)
        self.assertIn("engage", branches)
        self.assertIn("release", branches)
        engage = branches["engage"]
        self.assertLess(
            engage.index("telemetry_hold_discharge_limit"),
            engage.index("set_state(Battery.discharge_limit, 0)"),
        )
        release = branches["release"]
        self.assertLess(
            release.index("set_state(Battery.discharge_limit, restored_limit)"),
            release.index("telemetry_hold_discharge_limit"),
        )

    def test_fast_loop_has_no_arrival_precharge_guard(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        target_source = ast.unparse(target)

        # EV priority is owned by the unified export budget; the fast loop
        # carries no precharge guard that could pin a positive setpoint.
        self.assertNotIn("is_ev_arrival_precharge_active", target_source)
        self.assertNotIn("ev_arrival_precharge_active", target_source)
        self.assertNotIn(
            "ev_plugged_in=get(EV.plugged_in, False)", target_source
        )

    def test_live_headroom_uses_time_to_full_for_early_activation(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        target_source = ast.unparse(target)

        self.assertIn("calculate_time_to_full_minutes", target_source)
        self.assertIn("time_to_full_minutes=time_to_full_minutes", target_source)

    def test_price_ranked_proactive_headroom_is_not_blocked_by_active_drive(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_setpoint_target"
        )
        target_source = ast.unparse(target)

        self.assertNotIn("and (not ev_arrival_reservation_active)", target_source)
        self.assertIn("schedule_plan.headroom_energy_kwh > 0.01", target_source)
        self.assertIn(
            "proactive_export_power_w = max(0.0, desired_current_optional_export_w)",
            target_source,
        )

    def test_pv_only_guard_uses_charge_limit_not_discharge_limit(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        calls = [
            node
            for node in ast.walk(target)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_pv_only_setpoint"
        ]

        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.unparse(calls[0].args[2]), "max_battery_charge_power_w")
        self.assertIn("get(Battery.charge_limit, -1)", ast.unparse(target))

    def test_zero_charge_limit_is_respected_by_live_pv_only_guard(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        target_source = ast.unparse(target)

        self.assertIn("resolve_directional_power_limit_w", target_source)
        self.assertIn("get(Battery.charge_limit, -1)", target_source)
        self.assertIn("6600.0, 6600.0", " ".join(target_source.split()))

    def test_live_headroom_rejects_stale_or_skewed_measurements(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        target_source = ast.unparse(target)

        self.assertIn("are_power_measurements_coherent", target_source)
        self.assertIn("calculate_measurement_skew_seconds", target_source)
        self.assertIn("max_skew_seconds=LIVE_HEADROOM_MAX_MEASUREMENT_SKEW_SECONDS", target_source)

    def test_live_control_fails_safe_on_stale_pv_without_budget_veto(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        target_source = ast.unparse(target)

        self.assertIn("are_power_measurement_values_valid", target_source)
        self.assertIn("pv_power_measurement_fresh =", target_source)
        self.assertIn("is_measurement_fresh", target_source)
        self.assertIn("select_no_battery_export_target", target_source)
        # Stale PV falls back to zero in the value path instead of vetoing
        # the export budget: the sensor sleeps for minutes on a flat zero
        # at night, and a veto turns that nap into a full-range oscillation.
        self.assertIn(
            "pv_power_for_control_w = live_pv_power_w "
            "if pv_power_measurement_fresh else 0.0",
            target_source,
        )
        self.assertNotIn("not pv_power_measurement_fresh", target_source)
        self.assertIn("pv_measurement_age_seconds=", target_source)
        self.assertIn("pv_measurement_fresh=", target_source)

    def test_live_headroom_rejects_nonfinite_power_values(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        calls = [
            node
            for node in ast.walk(target)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "are_power_measurement_values_valid"
        ]
        target_source = ast.unparse(target)

        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("LIVE_POWER_MAX_ABSOLUTE_W = 20000.0", source)
        self.assertIn("power_measurement_values_valid =", target_source)
        self.assertIn("'invalid_power'", target_source)
        self.assertIn("max_battery_discharge_power_w =", target_source)
        self.assertGreaterEqual(
            target_source.count("max_battery_charge_w=max_battery_charge_power_w"),
            2,
        )
        self.assertGreaterEqual(
            target_source.count(
                "max_battery_discharge_w=max_battery_discharge_power_w"
            ),
            3,
        )

    def test_live_export_requires_budget_and_coherent_measurements(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        target_source = ast.unparse(target)

        self.assertIn("limit_live_export_target_to_budget", target_source)
        self.assertIn("'safe_export_energy_kwh'", target_source)
        self.assertIn("optional_export_ev_plan_hold", target_source)
        self.assertIn("not measurements_available", target_source)
        self.assertIn("'export_policy_calculated_at'", target_source)
        self.assertIn("export_policy_fresh =", target_source)
        self.assertIn("is_measurement_fresh", target_source)
        self.assertIn(
            "max_age_seconds=LIVE_EXPORT_BUDGET_HORIZON_SECONDS",
            target_source,
        )
        self.assertIn("not export_policy_fresh", target_source)
        self.assertIn("get_fresh_export_policy_budget", target_source)
        self.assertIn("parse_aware_datetime", target_source)
        self.assertNotIn(
            "safe_export_calculated_at = with_timezone",
            target_source,
        )
        self.assertNotIn(
            "export_policy_calculated_at = with_timezone",
            target_source,
        )
        self.assertIn("'safe_export_source_battery_kwh'", target_source)
        self.assertIn("'safe_export_calculated_at'", target_source)
        self.assertIn("export_budget_energy_kwh", target_source)
        # Sub-threshold budget dither must not re-authorize export: the
        # PV-only floor releases only above a meaningful budget.
        self.assertIn("LIVE_EXPORT_BUDGET_RELEASE_KWH", target_source)

    def test_scheduler_requires_fresh_policy_snapshot_and_battery_anchor(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        planner = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_setpoint_target"
        )
        forecast_surplus = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "forecast_surplus"
        )
        planner_source = ast.unparse(planner)

        self.assertIn("get_fresh_export_policy_budget", planner_source)
        self.assertIn("parse_aware_datetime", planner_source)
        self.assertNotIn(
            "published_surplus_calculated_at = with_timezone",
            planner_source,
        )
        self.assertIn("'battery_energy_at_calculation'", planner_source)
        self.assertIn("export_policy_valid=", planner_source)
        self.assertIn("safe_export_source_battery_kwh=", planner_source)
        self.assertIn("safe_export_calculated_at=", planner_source)
        policy_publications = [
            node
            for node in ast.walk(forecast_surplus)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "set_energy_surplus"
            and any(
                isinstance(child, ast.Attribute)
                and child.attr == "energy_surplus_after_ev_charging"
                for child in ast.walk(node)
            )
        ]
        self.assertEqual(len(policy_publications), 2)
        for publication in policy_publications:
            keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in publication.keywords}
            self.assertEqual(keywords.get("calculated_at"), "t_now")
            self.assertEqual(
                keywords.get("battery_energy_at_calculation"),
                "battery_energy",
            )

    def test_scheduler_publishes_quantitative_ev_reservation_and_block_reason(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_setpoint_target"
        )
        target_source = ast.unparse(target)

        self.assertIn("ev_arrival_reserved_kwh=", target_source)
        self.assertIn("ev_arrival_reservation_active=", target_source)
        self.assertIn("export_block_reason=", target_source)

    def test_live_headroom_publishes_saturation_and_measurement_diagnostics(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        publish_call = next(
            node
            for node in ast.walk(target)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "set_state"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "sensor.grid_export_safety"
        )
        published_attributes = {keyword.arg for keyword in publish_call.keywords}

        self.assertIn("time_to_full_minutes", published_attributes)
        self.assertIn("measurement_skew_seconds", published_attributes)
        self.assertIn("grid_measurement_age_seconds", published_attributes)
        self.assertIn("battery_measurement_age_seconds", published_attributes)
        self.assertIn("measurement_rejection_reason", published_attributes)

    def test_scheduler_adds_observed_high_side_pv_error_to_price_ranked_headroom(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_setpoint_target"
        )
        target_source = ast.unparse(target)

        self.assertIn("build_high_side_headroom_requirements", target_source)
        self.assertIn(
            "preferred_headroom_requirements.extend(high_side_headroom.requirements)",
            target_source,
        )
        self.assertIn("observed_pv_forecast_error_w=", target_source)
        self.assertIn("high_side_headroom_energy_kwh=", target_source)
        self.assertIn(
            "max_battery_charge_w=configured_battery_charge_limit_w",
            target_source,
        )
        self.assertIn("validate_pv_power_measurement", target_source)
        self.assertIn("pv_power_value_valid", target_source)
        validator = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "validate_pv_power_measurement"
        )
        validator_source = ast.unparse(validator)
        self.assertIn("are_power_measurement_values_valid", validator_source)
        self.assertIn("is_measurement_fresh", validator_source)
        self.assertIn(
            "maximum_live_pv_power_w=LIVE_POWER_MAX_ABSOLUTE_W",
            target_source,
        )
        self.assertIn(
            "live_pv_power_w=live_pv_power_for_planning",
            target_source,
        )
        self.assertIn(
            "pv_power_measurement_fresh=pv_power_measurement_fresh",
            target_source,
        )
        self.assertGreaterEqual(
            target_source.count("calculate_absorbable_overflow_power"),
            2,
        )
        compact_target_source = " ".join(target_source.split())
        self.assertIn(
            "headroom_constraint_active=headroom_energy_kwh > 0 or hard_headroom_energy_kwh > 0",
            compact_target_source,
        )

    def test_scheduler_replans_when_live_pv_forecast_changes(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_setpoint_target"
        )
        decorators = "\n".join(ast.unparse(node) for node in target.decorator_list)

        self.assertIn("PVForecast.power_now", decorators)
        self.assertIn("PVProduction.total_power", decorators)

    def test_directional_battery_limits_preserve_asymmetric_configuration(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        helper = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "get_forecast_battery_power_limits"
        )
        helper.decorator_list = []
        namespace = {}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )

        charge_limit_w, discharge_limit_w = namespace[
            "get_forecast_battery_power_limits"
        ](1800.0, 7200.0)

        self.assertEqual(charge_limit_w, 1800.0)
        self.assertEqual(discharge_limit_w, 7200.0)

    def test_forecast_discharge_cap_follows_hardware_limit(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        helper = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "limit_forecast_discharge_to_hardware_w"
        )
        helper.decorator_list = []
        namespace = {"isfinite": isfinite}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )
        cap = namespace["limit_forecast_discharge_to_hardware_w"]

        # Unlimited hardware keeps the planned cap; a ban or a tighter
        # hardware limit wins; garbage fails closed to zero.
        self.assertEqual(cap(4000.0, 8000.0), 4000.0)
        self.assertEqual(cap(4000.0, 0.0), 0.0)
        self.assertEqual(cap(4000.0, 1500.0), 1500.0)
        self.assertEqual(cap(4000.0, -1.0), 0.0)
        self.assertEqual(cap("bad", 4000.0), 0.0)
        self.assertEqual(cap(4000.0, nan), 0.0)

        # The simulation actually uses the cap: forecast() clamps the plan
        # cap to the hardware limit.  The clamp must stay in uncompiled
        # code: _forecast() is compiled and must not gain new
        # cross-function calls (one such call crashed every planner run
        # with an unawaited-coroutine TypeError).
        functions = {
            node.name: ast.unparse(node)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"forecast", "_forecast"}
        }
        self.assertIn("battery_discharge_limit_w", functions["forecast"])
        self.assertIn("limit_forecast_discharge_to_hardware_w", functions["forecast"])
        self.assertNotIn("limit_forecast_discharge_to_hardware_w", functions["_forecast"])

    def test_unified_scheduler_preserves_configured_zero_discharge_limit(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_setpoint_target"
        )
        target_source = ast.unparse(target)

        self.assertIn("resolve_directional_power_limit_w", target_source)
        self.assertIn(
            "configured_discharge_limit_w, 8000.0, 8000.0",
            " ".join(target_source.split()),
        )
        self.assertNotIn(
            "configured_discharge_limit_w if configured_discharge_limit_w >= 0 else 8000.0",
            target_source,
        )

    def test_planner_and_fast_loop_reject_invalid_battery_energy(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        planner = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_setpoint_target"
        )
        fast_loop = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        planner_source = ast.unparse(planner)
        fast_loop_source = ast.unparse(fast_loop)

        self.assertIn("are_battery_energy_values_valid", planner_source)
        self.assertIn("battery_energy_values_valid", fast_loop_source)
        self.assertIn("not battery_energy_values_valid", fast_loop_source)

    def test_planner_and_fast_loop_reject_stale_battery_energy(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        functions = {
            node.name: ast.unparse(node)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name
            in {"forecast_surplus", "auto_setpoint_target", "auto_apply_setpoint"}
        }

        for function_name in ("forecast_surplus", "auto_setpoint_target"):
            function_source = functions[function_name]
            self.assertIn(
                "get_attr(Battery.energy, 'last_updated')",
                function_source,
            )
            self.assertIn("battery_energy_measurement_fresh", function_source)
            self.assertIn(
                "LIVE_BATTERY_ENERGY_MAX_MEASUREMENT_AGE_SECONDS",
                function_source,
            )

        fast_loop_source = functions["auto_apply_setpoint"]
        self.assertIn("battery_energy_measurement_age_seconds", fast_loop_source)
        self.assertIn("not battery_energy_measurement_fresh", fast_loop_source)
        self.assertIn(
            "LIVE_BATTERY_ENERGY_MAX_MEASUREMENT_AGE_SECONDS",
            fast_loop_source,
        )

    def test_battery_freshness_gate_matches_sensor_cadence(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        match = re.search(
            r"^LIVE_BATTERY_ENERGY_MAX_MEASUREMENT_AGE_SECONDS\s*=\s*([0-9.]+)",
            source,
            re.MULTILINE,
        )
        self.assertIsNotNone(match)
        gate_seconds = float(match.group(1))
        now_value = datetime(2026, 9, 13, 20, 35, tzinfo=timezone.utc)
        # sensor.battery_energy updates every 60-300 s (slower on quiet
        # evenings): a 4-minute-old sample is healthy cadence and must not
        # pin the applied setpoint at the failsafe...
        self.assertTrue(
            export_scheduler.is_measurement_fresh(
                now_value,
                now_value - timedelta(seconds=240),
                gate_seconds,
            )
        )
        # ...while a clearly dead sensor still fails closed.
        self.assertFalse(
            export_scheduler.is_measurement_fresh(
                now_value,
                now_value - timedelta(seconds=400),
                gate_seconds,
            )
        )

    def test_export_budget_horizon_covers_missed_policy_cycles(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        match = re.search(
            r"^LIVE_EXPORT_BUDGET_HORIZON_SECONDS\s*=\s*([0-9.]+)",
            source,
            re.MULTILINE,
        )
        self.assertIsNotNone(match)
        # The policy snapshot is produced every 120 s; the horizon must
        # cover at least two missed cycles or normal publish jitter alone
        # trips it and slams the setpoint to PV-only for a cycle.
        self.assertGreaterEqual(float(match.group(1)), 240.0)

    def test_fast_loop_validates_house_load_and_holds_on_incoherent_measurements(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        fast_loop = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        fast_loop_source = ast.unparse(fast_loop)

        self.assertIn("get_attr(House.loads, 'last_updated')", fast_loop_source)
        self.assertIn("house_load_measurement_fresh", fast_loop_source)
        self.assertIn("are_power_measurement_values_valid", fast_loop_source)
        # Loads/PV freshness still gates value validity and the rejection
        # reason, but neither may veto the export budget: loads are an
        # unsynchronized residual that swings negative on fast grid ramps
        # and PV sleeps on a flat zero at night, and vetoing on either
        # oscillates full export against a PV-only slam every few seconds.
        for node in ast.walk(fast_loop):
            if not isinstance(node, ast.If):
                continue
            zeroes_budget = any(
                isinstance(child, ast.Assign)
                and any(
                    isinstance(target, ast.Name)
                    and target.id == "export_budget_energy_kwh"
                    for target in child.targets
                )
                for child in ast.walk(node)
            )
            if zeroes_budget:
                test_source = ast.unparse(node.test)
                self.assertNotIn("house_load_measurement_fresh", test_source)
                self.assertNotIn("pv_power_measurement_fresh", test_source)
        # Incoherent power measurements hold the last setpoint instead of
        # pinning a battery-neutral import target.
        self.assertNotIn(
            "calculate_battery_neutral_grid_target_w", fast_loop_source
        )
        self.assertNotIn("telemetry_safe_grid_target_w", fast_loop_source)
        self.assertIn("if not measurements_available:", fast_loop_source)
        self.assertIn("prev_telemetry_hold_active", fast_loop_source)

    def test_proactive_mode_does_not_override_price_ranked_current_bucket(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        planner = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_setpoint_target"
        )
        proactive_blocks = [
            node
            for node in ast.walk(planner)
            if isinstance(node, ast.If)
            and "proactive_export_active" in ast.unparse(node.test)
        ]

        self.assertTrue(proactive_blocks)
        for block in proactive_blocks:
            block_source = ast.unparse(block)
            self.assertNotIn(
                "optional_export_power_schedule[current_period_key] =",
                block_source,
            )
            self.assertNotIn("schedule_plan.headroom_energy_kwh *", block_source)

        planner_source = ast.unparse(planner)
        self.assertIn(
            "proactive_export_power_w = max(0.0, desired_current_optional_export_w)",
            planner_source,
        )

    def test_planner_applies_price_ranked_headroom_hold_schedule(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        planner = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_setpoint_target"
        )
        planner_source = ast.unparse(planner)
        scheduler_calls = [
            node
            for node in ast.walk(planner)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_unified_export_schedule"
        ]

        self.assertEqual(len(scheduler_calls), 1)
        scheduler_keywords = {
            keyword.arg: ast.unparse(keyword.value)
            for keyword in scheduler_calls[0].keywords
        }
        self.assertEqual(scheduler_keywords.get("neutral_grid_target_w"), "max_setpoint")
        self.assertIn(
            "headroom_hold_schedule = dict(schedule_plan.headroom_hold_schedule)",
            planner_source,
        )
        self.assertIn("for key, hold_power_w in headroom_hold_schedule.items()", planner_source)
        self.assertIn("headroom_hold_schedule=", planner_source)

    def test_export_policy_budget_ledger_is_persistent_and_used_by_both_loops(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        functions = {
            node.name: ast.unparse(node)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"auto_setpoint_target", "auto_apply_setpoint"}
        }

        self.assertIn(
            'state.persist(\'pyscript.export_policy_budget_ledger\'',
            ast.unparse(tree),
        )
        for function_name in ("auto_setpoint_target", "auto_apply_setpoint"):
            function_source = functions[function_name]
            self.assertIn("update_monotonic_export_budget_ledger", function_source)
            self.assertIn("export_budget_ledger.remaining_kwh", function_source)
            self.assertIn(
                "state.set('pyscript.export_policy_budget_ledger', export_budget_ledger.state_value)",
                function_source,
            )

        planner = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "auto_setpoint_target"
        )
        planner_ledger_call = next(
            node
            for node in ast.walk(planner)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "update_monotonic_export_budget_ledger"
        )
        planner_ledger_keywords = {
            keyword.arg: ast.unparse(keyword.value)
            for keyword in planner_ledger_call.keywords
            if keyword.arg is not None
        }
        self.assertEqual(planner_ledger_keywords.get("current_time"), "t_now")
        self.assertEqual(
            planner_ledger_keywords.get("accounting_enabled"),
            "False",
        )

        fast_loop = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "auto_apply_setpoint"
        )
        fast_ledger_call = next(
            node
            for node in ast.walk(fast_loop)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "update_monotonic_export_budget_ledger"
        )
        fast_ledger_keywords = {
            keyword.arg: ast.unparse(keyword.value)
            for keyword in fast_ledger_call.keywords
            if keyword.arg is not None
        }
        self.assertEqual(
            fast_ledger_keywords.get("current_time"),
            "current_control_time",
        )
        self.assertEqual(
            fast_ledger_keywords.get("battery_discharge_power_w"),
            "max(0.0, -measured_battery_power_w) if measurements_available else 0.0",
        )
        self.assertEqual(
            fast_ledger_keywords.get("measurements_available"),
            "measurements_available",
        )
        self.assertIn(
            "if export_budget_ledger.state_value:",
            functions["auto_apply_setpoint"],
        )

    def test_forecast_uses_directional_limits_in_charge_and_discharge_paths(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        forecast_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_forecast"
        )
        forecast_source = ast.unparse(forecast_node)

        self.assertIn("get_forecast_battery_power_limits", forecast_source)
        self.assertIn("max_discharge_rate = forecast_discharge_limit_w", forecast_source)
        self.assertIn("max_charge_rate = forecast_charge_limit_w", forecast_source)
        self.assertIn(
            "target_charge_power = min(charge_power_limit, forecast_charge_limit_w)",
            forecast_source,
        )

    def test_all_policy_forecasts_receive_the_configured_charge_limit(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        planner = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "auto_setpoint_target"
        )
        forecast_surplus = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "forecast_surplus"
        )
        planner_source = ast.unparse(planner)
        policy_forecast_calls = _expanded_forecast_keywords(forecast_surplus)
        local_forecast = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "forecast_setpoint_local"
        )

        self.assertIn("resolve_directional_power_limit_w", planner_source)
        self.assertIn(
            "battery_charge_limit=configured_battery_charge_limit_w",
            planner_source,
        )
        self.assertIn(
            "battery_charge_limit=ctx.battery_charge_limit",
            ast.unparse(local_forecast),
        )
        self.assertGreaterEqual(len(policy_forecast_calls), 4)
        for keyword_values in policy_forecast_calls:
            self.assertEqual(
                ast.unparse(keyword_values["battery_charge_limit"]),
                "configured_battery_charge_limit_w",
            )

    def test_battery_energy_accounting_applies_both_conversion_directions(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        helper = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "calculate_battery_energy_delta_kwh"
        )
        helper.decorator_list = []
        namespace = {}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )
        calculate = namespace["calculate_battery_energy_delta_kwh"]
        self.assertAlmostEqual(calculate(4000.0, 0.5, 0.90, 0.90), 1.8)
        self.assertAlmostEqual(calculate(-3600.0, 0.5, 0.90, 0.90), -2.0)

    def test_five_watt_half_hour_exceedance_is_tiny(self):
        samples = [
            (self.t_now, 3005.0),
            (self.t_now + timedelta(minutes=30), 2900.0),
        ]
        overflow = calculate_pv_overflow_energy(samples, 3000.0, self.t_now)
        self.assertAlmostEqual(overflow, 0.0025)
        self.assertFalse(feedin_constraint_exceeded(FeedinCandidate(-100.0, 3005.0, overflow), 3000.0))

    def test_legacy_surplus_fallback_is_independent_of_day_pv_distribution(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        helper = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "calculate_legacy_surplus_fallback"
        )
        helper.decorator_list = []
        namespace = {"PVForecastWithPrices": SimpleNamespace}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )
        start = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)

        def build(pv_values):
            return [
                SimpleNamespace(
                    period_start=start + timedelta(hours=6 * index),
                    pv_estimate=pv,
                )
                for index, pv in enumerate(pv_values)
            ]

        calculate = namespace["calculate_legacy_surplus_fallback"]
        first = calculate(build([0.0, 2.0, 0.0, 2.0]), 20.0, 1000.0, 500.0, 0.8)
        second = calculate(build([2.0, 0.0, 2.0, 0.0]), 20.0, 1000.0, 500.0, 0.8)
        self.assertAlmostEqual(first, second)

    def test_discounted_pv_feedin_peak_prefers_near_term_peaks(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        wanted = {"get_discounted_pv_feedin_peak", "_feedin_peak_discount", "_FEEDIN_PEAK_DISCOUNT_POINTS"}
        nodes = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.Assign))
            and (
                getattr(node, "name", "") in wanted
                or any(getattr(target, "id", "") in wanted for target in getattr(node, "targets", []))
            )
        ]
        self.assertEqual(len(nodes), 3)
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "energy.py", "exec"), namespace)
        t_now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

        def entry(hours_ahead, pv_feedin):
            return SimpleNamespace(
                period_start=t_now + timedelta(hours=hours_ahead),
                pv_feedin=pv_feedin,
            )

        result = SimpleNamespace(
            detail=[entry(-1, 5000), entry(1, 1000), entry(7.5, 2000), entry(0, 9000), entry(25, 9000)]
        )
        peak, peak_time, raw_peak = namespace["get_discounted_pv_feedin_peak"](result, t_now)
        self.assertAlmostEqual(peak, 2000 * 0.875)
        self.assertEqual(peak_time, t_now + timedelta(hours=7.5))
        self.assertEqual(raw_peak, 2000)
        self.assertEqual(namespace["get_discounted_pv_feedin_peak"](None, t_now), (0, t_now, 0))

    def _load_or_get_helper(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        helper = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_or_get"
        )
        calls = []

        def fake_get(entity_id, default=None):
            calls.append((entity_id, default))
            return f"state:{entity_id}"

        namespace = {"get": fake_get}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )
        return namespace["_or_get"], calls

    def test_forecast_wrapper_preserves_explicit_zero_inputs(self):
        or_get, calls = self._load_or_get_helper()
        # Explicit falsy-but-set inputs must pass through without touching state.
        self.assertEqual(or_get("entity.a", 0, 99), 0)
        self.assertEqual(or_get("entity.b", 0.0, 99), 0.0)
        self.assertEqual(or_get("entity.c", False, True), False)
        self.assertEqual(calls, [])
        # Missing inputs fall back to the HA state default.
        self.assertEqual(or_get("entity.a", None, 99), "state:entity.a")
        self.assertEqual(calls, [("entity.a", 99)])

        # The wrapper must route its state fallbacks through the helper so a
        # future ``or``-based rewrite cannot reintroduce zero-clobbering.
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        wrapper = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "forecast"
        )
        assignments = {
            target.id: ast.unparse(node.value)
            for node in wrapper.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for name in (
            "daily_power",
            "nightly_power",
            "ev_required_soc",
            "ev_soc",
            "charge_limit",
            "max_charge_price",
        ):
            self.assertIn("_or_get", assignments[name])
        self.assertIn("is not None", assignments["min_discharge_price"])

    def test_partial_pv_forecast_and_missing_prices_are_safe(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        helper = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "get_pv_forecast_with_prices"
        )
        helper.decorator_list = []

        class PVForecastStub:
            forecast_today = "today"
            forecast_tomorrow = "tomorrow"
            forecast_day_3 = "day_3"
            forecast_day_4 = "day_4"
            forecast_day_5 = "day_5"

        class LogStub:
            def warning(self, _message):
                return None

        start = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
        source_entries = []

        def get_attr(entity, _name, default=None):
            return source_entries if entity == "today" else default

        namespace = {
            "datetime": datetime,
            "timedelta": timedelta,
            "PVForecast": PVForecastStub,
            "PVForecastWithPrices": lambda period_start, pv_estimate, price_per_kwh: SimpleNamespace(
                period_start=period_start,
                pv_estimate=pv_estimate,
                price_per_kwh=price_per_kwh,
            ),
            "get_attr": get_attr,
            "get_price": lambda _hour, _minute: 0.2875,
            "extend_pv_forecast_to_horizon": lambda forecast, _end, _prices: forecast,
            "log": LogStub(),
        }
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )
        get_forecast = namespace["get_pv_forecast_with_prices"]
        source_entries.append({"period_start": start, "pv_estimate": 1.0})
        self.assertEqual(
            get_forecast(start - timedelta(minutes=1), start + timedelta(hours=1), []),
            [],
        )

        source_entries.append(
            {"period_start": start + timedelta(minutes=30), "pv_estimate": 1.0}
        )
        result = get_forecast(
            start - timedelta(minutes=1),
            start + timedelta(hours=1),
            [],
        )
        self.assertEqual([entry.price_per_kwh for entry in result], [0.2875, 0.2875])

    def test_meaningful_exceedance_is_expressed_as_headroom_energy(self):
        samples = [
            (self.t_now, 3500.0),
            (self.t_now + timedelta(minutes=30), 3000.0),
        ]
        overflow = calculate_pv_overflow_energy(samples, 3000.0, self.t_now)
        self.assertAlmostEqual(overflow, 0.25)
        self.assertTrue(feedin_constraint_exceeded(FeedinCandidate(-100.0, 3500.0, overflow), 3000.0))

    def test_auto_max_setpoint_keeps_legacy_bias_without_surplus(self):
        self.assertEqual(
            calculate_auto_max_setpoint(0.0, horizon_hours=24.0),
            -20.0,
        )

    def test_auto_max_setpoint_budgets_partial_surplus_over_horizon(self):
        self.assertEqual(
            calculate_auto_max_setpoint(1.0, horizon_hours=20.0),
            -50.0,
        )

    def test_auto_max_setpoint_caps_abundant_surplus_at_measured_margin(self):
        self.assertEqual(
            calculate_auto_max_setpoint(13.9, horizon_hours=23.5),
            -100.0,
        )

    def test_auto_max_setpoint_rejects_invalid_horizon(self):
        with self.assertRaises(ValueError):
            calculate_auto_max_setpoint(1.0, horizon_hours=0.0)

    def test_auto_max_setpoint_ignores_negative_surplus(self):
        self.assertEqual(
            calculate_auto_max_setpoint(-2.0, horizon_hours=12.0),
            -20.0,
        )

    def test_observed_insensitive_search_keeps_neutral_baseline(self):
        candidates = [
            FeedinCandidate(-100.0, 3006.0, 0.0030),
            FeedinCandidate(-2800.0, 3006.0, 0.0030),
            FeedinCandidate(-4150.0, 3006.0, 0.0030),
            FeedinCandidate(-5332.0, 3005.0, 0.0025),
        ]
        selected = choose_stable_candidate(candidates, 3000.0, prefer_larger_control=True)
        self.assertEqual(selected.control_value, -100.0)

    def test_meaningful_but_uncontrollable_violation_does_not_saturate(self):
        candidates = [
            FeedinCandidate(-100.0, 3500.0, 0.25),
            FeedinCandidate(-2800.0, 3495.0, 0.248),
            FeedinCandidate(-5332.0, 3490.0, 0.245),
        ]
        selected = choose_stable_candidate(candidates, 3000.0, prefer_larger_control=True)
        self.assertEqual(selected.control_value, -100.0)

    def test_selects_least_aggressive_candidate_that_solves_violation(self):
        candidates = [
            FeedinCandidate(-100.0, 3500.0, 0.25),
            FeedinCandidate(-1000.0, 3200.0, 0.10),
            FeedinCandidate(-2000.0, 3020.0, 0.01),
            FeedinCandidate(-3000.0, 2900.0, 0.0),
        ]
        selected = choose_stable_candidate(candidates, 3000.0, prefer_larger_control=True)
        self.assertEqual(selected.control_value, -2000.0)

    def test_spread_selection_prefers_lowest_acceptable_value(self):
        candidates = [
            FeedinCandidate(0.01, 3500.0, 0.25),
            FeedinCandidate(5.0, 3020.0, 0.01),
            FeedinCandidate(25.0, 2900.0, 0.0),
        ]
        selected = choose_stable_candidate(candidates, 3000.0, prefer_larger_control=False)
        self.assertEqual(selected.control_value, 5.0)

    def test_target_slew_limit_caps_both_directions(self):
        self.assertEqual(limit_target_step(-5260.0, -31.0), -531.0)
        self.assertEqual(limit_target_step(-31.0, -5260.0), -4760.0)
        self.assertEqual(limit_target_step(-200.0, -31.0), -200.0)

    def test_energy_bounded_control_uses_available_energy_until_peak(self):
        control = calculate_energy_bounded_control(
            overflow_energy_kwh=16.961,
            available_energy_kwh=14.16,
            hours_to_peak=5.67,
            min_control_w=-5500.0,
            max_control_w=-100.0,
        )
        self.assertAlmostEqual(control, -2497.35, places=2)

    def test_energy_bounded_control_handles_long_overnight_horizon(self):
        control = calculate_energy_bounded_control(
            overflow_energy_kwh=15.933,
            available_energy_kwh=12.0,
            hours_to_peak=14.3,
            min_control_w=-5500.0,
            max_control_w=-100.0,
        )
        self.assertAlmostEqual(control, -839.16, places=2)

    def test_energy_bounded_control_keeps_neutral_without_usable_energy(self):
        self.assertEqual(
            calculate_energy_bounded_control(0.019, 12.0, 6.0, -5500.0, -100.0),
            -100.0,
        )
        self.assertEqual(
            calculate_energy_bounded_control(12.0, 0.0, 6.0, -5500.0, -100.0),
            -100.0,
        )

    def test_energy_bounded_control_clamps_to_export_limit(self):
        self.assertEqual(
            calculate_energy_bounded_control(20.0, 20.0, 1.0, -5500.0, -100.0),
            -5500.0,
        )

    def test_headroom_energy_requirement_respects_tolerance_and_available_energy(self):
        self.assertAlmostEqual(calculate_headroom_energy_requirement(4.25, 10.0), 4.20)
        self.assertEqual(calculate_headroom_energy_requirement(4.25, 2.0), 2.0)
        self.assertEqual(calculate_headroom_energy_requirement(0.01, 2.0), 0.0)

    def test_price_aware_schedule_fills_highest_price_buckets_first(self):
        samples = [
            (self.t_now, 0.10, -500.0),
            (self.t_now + timedelta(minutes=30), 0.30, -500.0),
            (self.t_now + timedelta(minutes=60), 0.20, -500.0),
        ]
        schedule = build_price_aware_headroom_schedule(
            samples=samples,
            required_energy_kwh=2.0,
            deadline=self.t_now + timedelta(minutes=90),
            t_now=self.t_now,
            max_export_w=2000.0,
        )
        self.assertNotIn(self.t_now.isoformat(), schedule)
        self.assertEqual(schedule[(self.t_now + timedelta(minutes=30)).isoformat()], -2000.0)
        self.assertEqual(schedule[(self.t_now + timedelta(minutes=60)).isoformat()], -1500.0)

    def test_price_aware_schedule_clips_current_bucket_to_remaining_time(self):
        samples = [
            (self.t_now, 0.30, 0.0),
            (self.t_now + timedelta(minutes=30), 0.20, 0.0),
        ]
        schedule = build_price_aware_headroom_schedule(
            samples=samples,
            required_energy_kwh=1.0,
            deadline=self.t_now + timedelta(hours=1),
            t_now=self.t_now + timedelta(minutes=15),
            max_export_w=2000.0,
        )
        self.assertEqual(schedule[self.t_now.isoformat()], -2000.0)
        self.assertEqual(schedule[(self.t_now + timedelta(minutes=30)).isoformat()], -1000.0)

    def test_price_aware_schedule_needs_no_override_when_baseline_is_sufficient(self):
        samples = [
            (self.t_now, 0.10, -1000.0),
            (self.t_now + timedelta(minutes=30), 0.20, -1000.0),
        ]
        schedule = build_price_aware_headroom_schedule(
            samples=samples,
            required_energy_kwh=1.0,
            deadline=self.t_now + timedelta(hours=1),
            t_now=self.t_now,
            max_export_w=2000.0,
        )
        self.assertEqual(schedule, {})

    def test_forecast_headroom_is_not_capped_to_pv_limit_during_solar_window(self):
        self.assertEqual(
            apply_hard_feedin_control(
                -20.0,
                -5500.0,
                constraint_active=True,
            ),
            -5500.0,
        )

    def test_hard_feedin_control_forces_headroom_before_pv_limit_is_active(self):
        self.assertEqual(
            apply_hard_feedin_control(
                -20.0,
                -2500.0,
                constraint_active=True,
            ),
            -2500.0,
        )

    def test_headroom_control_keeps_more_aggressive_price_export(self):
        self.assertEqual(
            apply_hard_feedin_control(
                -3000.0,
                -2500.0,
                constraint_active=True,
            ),
            -3000.0,
        )

    def test_inactive_hard_feedin_control_preserves_price_mapping(self):
        self.assertEqual(
            apply_hard_feedin_control(
                -20.0,
                -5500.0,
                constraint_active=False,
            ),
            -20.0,
        )

    def test_active_headroom_control_without_current_schedule_preserves_price_mapping(self):
        self.assertEqual(
            apply_hard_feedin_control(
                -321.0,
                None,
                constraint_active=True,
            ),
            -321.0,
        )

    def test_candidate_selector_avoids_pyscript_closure_constructs(self):
        tree = ast.parse(inspect.getsource(choose_stable_candidate))
        closure_nodes = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        self.assertFalse(any(isinstance(node, closure_nodes) for node in ast.walk(tree)))

    def test_forecast_does_not_overwrite_configured_feedin_limit_with_observed_metric(self):
        # Regression for 2026-08-12 07:25: live export was 5.5 kW while the
        # forecast showed zero because _forecast reused max_feedin for both the
        # configured clamp and the running observed maximum.
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        forecast_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_forecast"
        )
        argument_names = [argument.arg for argument in forecast_node.args.args]
        self.assertIn("configured_max_feedin", argument_names)

        configured_limit_is_forwarded = any(
            isinstance(node, ast.keyword)
            and node.arg == "max_feedin"
            and isinstance(node.value, ast.Name)
            and node.value.id == "configured_max_feedin"
            for node in ast.walk(forecast_node)
        )
        self.assertTrue(configured_limit_is_forwarded)

    def test_dashboard_forecasts_use_the_published_battery_power_limit(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        forecast_surplus_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "forecast_surplus"
        )
        forecast_calls = _expanded_forecast_keywords(forecast_surplus_node)
        self.assertEqual(len(forecast_calls), 5)
        for keywords in forecast_calls:
            published_limit = keywords.get("max_battery_power_target")
            self.assertIsInstance(published_limit, ast.Name)
            self.assertEqual(published_limit.id, "max_battery_power_target")

            published_headroom = keywords.get("headroom_schedule")
            self.assertIsInstance(published_headroom, ast.Name)
            self.assertEqual(published_headroom.id, "headroom_schedule")

            published_schedule = keywords.get("grid_target_schedule")
            self.assertIsNotNone(published_schedule)

            published_neutral_target = keywords.get("max_setpoint")
            self.assertIsInstance(published_neutral_target, ast.Name)
            self.assertEqual(published_neutral_target.id, "forecast_max_setpoint")

        decorators = forecast_surplus_node.decorator_list
        target_triggered = any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "state_trigger"
            for decorator in decorators
        )
        self.assertTrue(target_triggered)

    def test_unified_forecast_uses_optional_export_and_reserve_schedules(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        forecast_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_forecast"
        )
        argument_names = [argument.arg for argument in forecast_node.args.args]
        self.assertIn("optional_export_power_schedule", argument_names)
        self.assertIn("battery_floor_schedule", argument_names)

        optional_export_lookup = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "optional_export_power_schedule"
            and node.func.attr == "get"
            for node in ast.walk(forecast_node)
        )
        reserve_lookup = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "battery_floor_schedule"
            and node.func.attr == "get"
            for node in ast.walk(forecast_node)
        )
        self.assertTrue(optional_export_lookup)
        self.assertTrue(reserve_lookup)

    def test_unified_orchestrator_replays_relative_export_with_the_reserve_trajectory(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "auto_setpoint_target"
        )
        replay_calls = []
        for node in ast.walk(target_node):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "forecast_setpoint_local"
            ):
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            optional_schedule = keywords.get("optional_export_power_schedule")
            floor_schedule = keywords.get("battery_floor_schedule")
            if (
                isinstance(optional_schedule, ast.Name)
                and optional_schedule.id == "optional_export_power_schedule"
                and isinstance(floor_schedule, ast.Name)
                and floor_schedule.id == "battery_floor_schedule"
            ):
                replay_calls.append(keywords)

        self.assertEqual(len(replay_calls), 1)
        absolute_schedule = replay_calls[0].get("grid_target_schedule")
        self.assertIsInstance(absolute_schedule, ast.Constant)
        self.assertIsNone(absolute_schedule.value)

        reserve_builds = [
            node
            for node in ast.walk(target_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_reserve_energy_schedule"
        ]
        self.assertEqual(len(reserve_builds), 1)

    def test_live_unified_control_preserves_replayed_battery_power(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        apply_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "auto_apply_setpoint"
        )
        calls = [
            node
            for node in ast.walk(apply_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "adapt_grid_target_to_live_power"
        ]
        self.assertEqual(len(calls), 1)

        planned_optional_reads = [
            node
            for node in ast.walk(apply_node)
            if isinstance(node, ast.Constant) and node.value == "planned_optional_export_power_w"
        ]
        self.assertEqual(len(planned_optional_reads), 1)
        planned_battery_reads = [
            node
            for node in ast.walk(apply_node)
            if isinstance(node, ast.Constant) and node.value == "planned_battery_power_w"
        ]
        self.assertEqual(len(planned_battery_reads), 1)

        call_keywords = {keyword.arg: keyword.value for keyword in calls[0].keywords}
        self.assertIsInstance(
            call_keywords.get("planned_optional_export_power_w"),
            ast.Name,
        )
        self.assertEqual(
            call_keywords["planned_optional_export_power_w"].id,
            "planned_optional_export_power_w",
        )
        self.assertIsInstance(call_keywords.get("hold_optional_export"), ast.Name)
        self.assertEqual(
            call_keywords["hold_optional_export"].id,
            "optional_export_ev_plan_hold",
        )
        self.assertNotIn(
            "ev_requires_charge",
            {node.id for node in ast.walk(apply_node) if isinstance(node, ast.Name)},
        )

        freshness_calls = [
            node
            for node in ast.walk(apply_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "is_ev_plan_fresh"
        ]
        self.assertEqual(len(freshness_calls), 1)

    def test_setpoint_planner_tracks_ev_availability_transitions(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "auto_setpoint_target"
        )

        trigger_entities = {
            ast.unparse(decorator.args[0])
            for decorator in target_node.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "state_trigger"
            and decorator.args
            and not isinstance(decorator.args[0], ast.JoinedStr)
        }
        self.assertTrue(
            {"EV.is_charging", "EV.energy_needed", "Charger.ready", "Charger.control_switch"}
            <= trigger_entities
        )

        availability_calls = [
            node
            for node in ast.walk(target_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "is_ev_available_for_charging"
        ]
        self.assertEqual(len(availability_calls), 1)
        availability_source = ast.unparse(availability_calls[0])
        self.assertIn("Charger.ready", availability_source)
        self.assertIn("Charger.control_switch", availability_source)
        self.assertIn("ev_is_charging", availability_source)

        charging_snapshot_assignments = [
            node
            for node in ast.walk(target_node)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "ev_is_charging"
                for target in node.targets
            )
            and "EV.is_charging" in ast.unparse(node.value)
        ]
        self.assertEqual(len(charging_snapshot_assignments), 1)

        published_signature_attributes = {
            keyword.arg
            for node in ast.walk(target_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "set_state"
            for keyword in node.keywords
        }
        self.assertTrue(
            {
                "ev_available_for_charging",
                "ev_is_charging",
                "ev_energy_needed_kwh",
                "plan_calculated_at",
            }
            <= published_signature_attributes
        )

    def test_unified_live_mode_enforces_the_planned_battery_floor(self):
        source = Path(__file__).parents[1].joinpath("modules/victron.py").read_text()
        tree = ast.parse(source)
        selected = [
            node
            for node in tree.body
            if (isinstance(node, ast.ClassDef) and node.name == "InverterMode")
            or (isinstance(node, ast.FunctionDef) and node.name == "get_auto_inverter_mode")
        ]
        for node in selected:
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = []
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])), "victron.py", "exec"), namespace)

        common = dict(
            ev_is_charging=False,
            surplus_energy=0.0,
            battery_headroom_energy=0.0,
            pv_power=0.0,
            daily_avg_power=900.0,
            battery_soc=30.0,
            target_soc=40.0,
            electricity_price=0.15,
            min_discharge_price=0.10,
            max_charge_price=0.0,
            charge_limit_percent=0.0,
            force_charge_switch=False,
            t_now=self.t_now,
            is_low_price=True,
        )
        enforced_mode, _, _, _ = namespace["get_auto_inverter_mode"](
            **common,
            enforce_battery_floor=True,
        )
        normal_mode, _, _, _ = namespace["get_auto_inverter_mode"](
            **common,
            enforce_battery_floor=False,
        )

        self.assertEqual(enforced_mode, namespace["InverterMode"].off)
        # Without enforcement the legacy low-price empty leg still stops
        # discharge: at low prices holding back an empty battery is correct.
        self.assertEqual(normal_mode, namespace["InverterMode"].off)

        ev_common = {
            **common,
            "ev_is_charging": True,
            "surplus_energy": -5.0,
            "battery_headroom_energy": 3.0,
            "battery_soc": 35.0,
            "target_soc": 36.0,
        }
        enforced_ev_mode, _, _, _ = namespace["get_auto_inverter_mode"](
            **ev_common,
            enforce_battery_floor=True,
        )
        normal_ev_mode, _, _, _ = namespace["get_auto_inverter_mode"](
            **ev_common,
            enforce_battery_floor=False,
        )

        # 2026-10-21 incident: even with floor enforcement, headroom above
        # the planned floor must not fund EV charging at low prices once the
        # post-obligation surplus is gone (surplus -5 here); the grid funds
        # the required charge and the battery holds its floor for the house.
        # (Genuine-surplus and high-price cases still stay on; see
        # test_required_night_ev_charge_comes_from_grid.)
        self.assertEqual(enforced_ev_mode, namespace["InverterMode"].off)
        self.assertEqual(normal_ev_mode, namespace["InverterMode"].off)

        energy_tree = ast.parse(Path(__file__).parents[1].joinpath("energy.py").read_text())
        live_node = next(
            node for node in energy_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "auto_victron_set_inverter_mode"
        )
        live_call = next(
            node for node in ast.walk(live_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_auto_inverter_mode"
        )
        keywords = {keyword.arg: keyword.value for keyword in live_call.keywords}
        self.assertIn("enforce_battery_floor", keywords)
        self.assertTrue(
            any(
                isinstance(node, ast.Constant) and node.value == "planned_battery_floor_kwh"
                for node in ast.walk(live_node)
            )
        )

    def test_battery_may_deplete_until_actually_empty(self):
        # 2026-10-02 incident: charger-only at 9.3% SOC (2.94 kWh) while
        # importing at the day rate.  The stacked software reserve (5%
        # seasonal + 1 kWh margin + 0.5 kWh hysteresis + 1 kWh planned pad)
        # stranded ~3 kWh permanently; the BMS owns deep-discharge
        # protection, so the software floor must be ~zero year-round.
        energy_source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        energy_tree = ast.parse(energy_source)
        selected = [
            node
            for node in energy_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in ("get_reserve_soc", "get_battery_export_floor")
        ]
        self.assertEqual(len(selected), 2)
        for node in selected:
            node.decorator_list = []
        namespace = {"datetime": datetime}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )
        for month, day in ((10, 2), (1, 15), (6, 15)):
            reserve = namespace["get_reserve_soc"](
                datetime(2026, month, day, 10, 0, tzinfo=timezone.utc)
            )
            self.assertEqual(reserve, 0)
        self.assertEqual(namespace["get_battery_export_floor"](32.0, 0, 0), 0)

        margin = next(
            node.value.value
            for node in ast.walk(energy_tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "EXPORT_BASE_UNCERTAINTY_MARGIN_KWH"
                for target in node.targets
            )
        )
        self.assertLessEqual(margin, 0.1)

        victron_source = (
            Path(__file__).parents[1].joinpath("modules/victron.py").read_text()
        )
        victron_tree = ast.parse(victron_source)
        victron_selected = [
            node
            for node in victron_tree.body
            if (isinstance(node, ast.ClassDef) and node.name == "InverterMode")
            or (
                isinstance(node, ast.FunctionDef)
                and node.name == "get_auto_inverter_mode"
            )
        ]
        for node in victron_selected:
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = []
        victron_namespace = {}
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=victron_selected, type_ignores=[])
                ),
                "victron.py",
                "exec",
            ),
            victron_namespace,
        )
        decide = victron_namespace["get_auto_inverter_mode"]
        modes = victron_namespace["InverterMode"]
        incident = dict(
            ev_is_charging=True,
            surplus_energy=9.07,
            battery_headroom_energy=0.41,
            pv_power=4879.6,
            daily_avg_power=722.0,
            battery_soc=9.3,
            target_soc=5.0,
            electricity_price=0.2875,
            min_discharge_price=0.0,
            max_charge_price=0.0,
            charge_limit_percent=75.0,
            force_charge_switch=False,
            t_now=datetime(2026, 10, 2, 8, 38, tzinfo=timezone.utc),
            enforce_battery_floor=True,
            is_low_price=False,
        )
        incident_mode, _, _, _ = decide(**incident)
        self.assertEqual(incident_mode, modes.on)
        # At high prices even an apparently empty battery stays on: LFP SOC
        # is a pessimistic estimate, and only the BMS cutoff tells true
        # empty.  Importing at the day rate on that guess is always wrong.
        empty_high_mode, _, _, empty_high_reason = decide(
            **{
                **incident,
                "battery_headroom_energy": 0.05,
                "surplus_energy": -1.0,
                "battery_soc": 4.0,
            }
        )
        self.assertEqual(empty_high_mode, modes.on)
        self.assertIn("BMS", empty_high_reason)
        # At low prices the empty battery keeps its protection: recharge-only
        # with PV, off without.
        empty_low_pv_mode, _, _, _ = decide(
            **{
                **incident,
                "battery_headroom_energy": 0.05,
                "electricity_price": 0.1775,
                "is_low_price": True,
            }
        )
        self.assertEqual(empty_low_pv_mode, modes.charger_only)
        empty_low_dark_mode, _, _, _ = decide(
            **{
                **incident,
                "battery_headroom_energy": 0.05,
                "pv_power": 0.0,
                "electricity_price": 0.1775,
                "is_low_price": True,
            }
        )
        self.assertEqual(empty_low_dark_mode, modes.off)

    def test_required_night_ev_charge_comes_from_grid(self):
        # 2026-10-21 incident: required trip charge 00:01-00:33 at the 0.1775
        # night rate drained the house battery 21.7 -> 16.8 kWh even though
        # the EV-adjusted surplus was 0.00.  The inverter must block battery
        # discharge then so the grid funds the charge; only genuine surplus
        # (or high prices) may keep it on.
        victron_source = (
            Path(__file__).parents[1].joinpath("modules/victron.py").read_text()
        )
        victron_tree = ast.parse(victron_source)
        selected = [
            node
            for node in victron_tree.body
            if (isinstance(node, ast.ClassDef) and node.name == "InverterMode")
            or (
                isinstance(node, ast.FunctionDef)
                and node.name == "get_auto_inverter_mode"
            )
        ]
        for node in selected:
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = []
        namespace = {}
        exec(
            compile(
                ast.fix_missing_locations(
                    ast.Module(body=selected, type_ignores=[])
                ),
                "victron.py",
                "exec",
            ),
            namespace,
        )
        decide = namespace["get_auto_inverter_mode"]
        modes = namespace["InverterMode"]
        night = dict(
            ev_is_charging=True,
            surplus_energy=0.0,
            battery_headroom_energy=15.4,
            pv_power=0.0,
            daily_avg_power=722.0,
            battery_soc=67.0,
            target_soc=5.0,
            electricity_price=0.1775,
            min_discharge_price=0.0,
            max_charge_price=0.0,
            charge_limit_percent=75.0,
            force_charge_switch=False,
            t_now=datetime(2026, 10, 21, 0, 1, tzinfo=timezone.utc),
            enforce_battery_floor=True,
            is_low_price=True,
        )
        night_mode, _, _, night_reason = decide(**night)
        self.assertEqual(night_mode, modes.off)
        self.assertIn("without PV power", night_reason)
        # Genuine surplus still authorizes battery support at low prices.
        surplus_mode, _, _, _ = decide(**{**night, "surplus_energy": 2.0})
        self.assertEqual(surplus_mode, modes.on)
        # At high prices the battery still funds the charge instead of
        # importing at the day rate.
        high_mode, _, _, _ = decide(
            **{**night, "is_low_price": False, "electricity_price": 0.2875}
        )
        self.assertEqual(high_mode, modes.on)

        # The caller must feed the post-obligation budget while the EV draws
        # current; the plain surplus ignores the running session.
        energy_source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        energy_tree = ast.parse(energy_source)
        auto_node = next(
            node
            for node in energy_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "auto_victron_set_inverter_mode"
        )
        budgeted = [
            node
            for node in ast.walk(auto_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "min"
            and any(
                isinstance(child, ast.Attribute)
                and child.attr == "energy_surplus_after_ev_charging"
                for child in ast.walk(node)
            )
        ]
        self.assertEqual(len(budgeted), 1)

    def test_surplus_floor_reserves_prediction_error_headroom(self):
        # Optional-load budgets must not spend the battery to a literally
        # empty trough: the forecast misses on both house demand and PV.
        # The margin only narrows the surplus floor; the operational and
        # forecast floors keep the deplete-until-empty behavior above.
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        fraction = next(
            node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "SURPLUS_HEADROOM_RESERVE_FRACTION"
                for target in node.targets
            )
        )
        self.assertGreater(fraction, 0)
        self.assertLessEqual(fraction, 0.1)
        forecast_surplus_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "forecast_surplus"
        )
        floor_assignments = [
            node.value
            for node in ast.walk(forecast_surplus_node)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "surplus_battery_floor"
                for target in node.targets
            )
            and isinstance(node.value, ast.Call)
        ]
        self.assertEqual(len(floor_assignments), 1)
        margin = next(
            keyword.value
            for keyword in floor_assignments[0].keywords
            if keyword.arg == "uncertainty_margin_kwh"
        )
        self.assertEqual(
            ast.unparse(margin),
            "SURPLUS_HEADROOM_RESERVE_FRACTION * battery_capacity",
        )
        selected = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "get_battery_export_floor"
        ]
        selected[0].decorator_list = []
        namespace = {"datetime": datetime}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )
        floor = namespace["get_battery_export_floor"]
        self.assertEqual(floor(32.0, 0, 0), 0)
        self.assertAlmostEqual(floor(32.0, 0, 0, uncertainty_margin_kwh=0.05 * 32.0), 1.6)

    def test_dashboard_forecasts_consume_optional_export_and_reserve_schedules(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        forecast_surplus_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "forecast_surplus"
        )
        forecast_calls = _expanded_forecast_keywords(forecast_surplus_node)
        self.assertEqual(len(forecast_calls), 5)
        for keywords in forecast_calls:
            self.assertIn("optional_export_power_schedule", keywords)
            self.assertIn("battery_floor_schedule", keywords)

    def test_reserve_trajectory_slots_scale_pv_and_bound_periods(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        helper = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build_reserve_trajectory_slots"
        )
        helper.decorator_list = []
        namespace = {
            "datetime": datetime,
            "timedelta": timedelta,
            "get_reserve_soc": lambda _when: 5.0,
            "get_low_pv_reserve_soc": lambda house_power, pv_power, battery_cells_balanced: 7.0,
            "is_low_price": lambda _price: False,
            "get_price": lambda hour, minute: 0.30,
            "ReserveTrajectorySlot": lambda **kwargs: SimpleNamespace(**kwargs),
        }
        exec(compile(ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])), "energy.py", "exec"), namespace)
        build = namespace["build_reserve_trajectory_slots"]
        t_now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

        def entry(hours_ahead, pv_estimate):
            return SimpleNamespace(
                period_start=t_now + timedelta(hours=hours_ahead),
                pv_estimate=pv_estimate,
            )

        entries = [entry(0, 1.0), entry(0.5, 2.0), entry(1.0, 3.0)]
        slots = build(entries, t_now, 800.0, 400.0, 1.0, True)
        self.assertEqual(len(slots), 3)
        # Period end comes from the next entry; last entry reuses the prior interval.
        self.assertAlmostEqual(slots[0].duration_hours, 0.5)
        self.assertAlmostEqual(slots[2].duration_hours, 0.5)
        self.assertEqual(slots[1].pv_power_w, 2000.0)
        self.assertEqual(slots[1].house_power_w, 800.0)
        self.assertEqual(slots[1].reserve_soc, 7.0)
        self.assertTrue(slots[1].protect_deficit)
        # Dampened pass scales PV identically for every slot.
        dampened = build(entries, t_now, 800.0, 400.0, 0.8, True)
        self.assertEqual(dampened[1].pv_power_w, 1600.0)
        self.assertEqual(dampened[1].duration_hours, slots[1].duration_hours)

    def test_ev_surplus_curve_uses_the_solar_cycle_budget_not_raw_accumulator(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        selected = []
        for name in ("get_spendable_solar_cycle_energy", "get_spendable_solar_cycle_curve"):
            node = next(
                node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
            )
            node.decorator_list = []
            selected.append(node)

        namespace = {"datetime": datetime}
        module = ast.Module(body=selected, type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "energy.py", "exec"), namespace)

        start = datetime(2026, 8, 16, 20, 0, tzinfo=timezone.utc)
        detail = [
            SimpleNamespace(
                period_start=start,
                pv_estimate=0.0,
                house_power=700.0,
                battery_energy=8.0,
                pv_feedin=0.0,
                surplus=-4.0,
            ),
            SimpleNamespace(
                period_start=start + timedelta(minutes=30),
                pv_estimate=0.0,
                house_power=700.0,
                battery_energy=6.51,
                pv_feedin=0.0,
                surplus=-10.19,
            ),
            SimpleNamespace(
                period_start=start + timedelta(hours=1),
                pv_estimate=0.0,
                house_power=700.0,
                battery_energy=7.0,
                pv_feedin=0.0,
                surplus=-9.0,
            ),
        ]

        curve = namespace["get_spendable_solar_cycle_curve"](
            detail,
            battery_export_floor=5.8,
            period_hours=0.5,
        )

        self.assertAlmostEqual(curve[0], 0.71)
        self.assertTrue(all(value >= 0 for value in curve))

    def test_banned_discharge_excludes_stranded_headroom_from_surplus(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        selected = []
        for name in ("get_spendable_solar_cycle_energy", "get_spendable_solar_cycle_curve"):
            node = next(
                node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
            )
            node.decorator_list = []
            selected.append(node)

        namespace = {"datetime": datetime}
        module = ast.Module(body=selected, type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "energy.py", "exec"), namespace)

        # Night with no PV left: the battery sits above its floor while the
        # house draws from the grid.
        start = datetime(2026, 8, 16, 20, 0, tzinfo=timezone.utc)
        detail = [
            SimpleNamespace(
                period_start=start + timedelta(minutes=30 * index),
                pv_estimate=0.0,
                house_power=700.0,
                battery_energy=energy,
                pv_feedin=0.0,
            )
            for index, energy in enumerate((8.0, 7.9, 7.85))
        ]
        spendable = namespace["get_spendable_solar_cycle_energy"]

        # Allowed (and the legacy default): headroom above the floor counts.
        surplus, headroom, spill, _ = spendable(
            detail,
            battery_export_floor=5.8,
            t_now=start,
            period_hours=0.5,
            battery_discharge_efficiency=0.95,
        )
        self.assertAlmostEqual(headroom, 2.05)
        self.assertAlmostEqual(surplus, 2.05 * 0.95)
        self.assertEqual(spill, 0)

        # Banned: the same headroom is stranded, so only PV spill (zero
        # overnight) counts as spendable.  The physical headroom is still
        # reported; it is the spendable budget that must not promise it.
        banned_surplus, banned_headroom, banned_spill, _ = spendable(
            detail,
            battery_export_floor=5.8,
            t_now=start,
            period_hours=0.5,
            battery_discharge_efficiency=0.95,
            battery_discharge_allowed=False,
        )
        self.assertEqual(banned_surplus, 0.0)
        self.assertEqual(banned_headroom, headroom)
        self.assertEqual(banned_spill, 0)

        banned_curve = namespace["get_spendable_solar_cycle_curve"](
            detail,
            battery_export_floor=5.8,
            period_hours=0.5,
            battery_discharge_efficiency=0.95,
            battery_discharge_allowed=False,
        )
        self.assertTrue(all(value == 0.0 for value in banned_curve))

        # forecast_surplus() feeds the live ban state into every spendable
        # computation instead of assuming the battery can always discharge.
        planner = next(
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "forecast_surplus"
        )
        planner_source = ast.unparse(planner)
        self.assertIn("hardware_discharge_limit_w", planner_source)
        self.assertIn("battery_discharge_allowed", planner_source)

    def test_roof_pv_recovery_marker_is_persistent_and_service_managed(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        persistent_entity = "pyscript.roof_pv_emergency_pending"

        persist_calls = [
            node
            for node in tree.body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "state"
            and node.value.func.attr == "persist"
        ]
        self.assertTrue(persist_calls)
        marker_persist_call = next(
            call
            for call in persist_calls
            if call.value.args[0].value == persistent_entity
        )
        defaults = {
            keyword.arg: keyword.value
            for keyword in marker_persist_call.value.keywords
        }
        self.assertEqual(defaults["default_value"].value, "off")

        service_function = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name == "set_roof_pv_emergency_pending"
            ),
            None,
        )
        self.assertIsNotNone(service_function)
        self.assertTrue(
            any(
                isinstance(decorator, ast.Name) and decorator.id == "service"
                for decorator in service_function.decorator_list
            )
        )
        state_set_calls = [
            node
            for node in ast.walk(service_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "state"
            and node.func.attr == "set"
        ]
        self.assertEqual(len(state_set_calls), 1)
        self.assertEqual(state_set_calls[0].args[0].value, persistent_entity)

    def test_incident_ev_obligation_eliminates_false_positive_surplus(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        helper = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "calculate_ev_adjusted_spendable_energy"
        )
        helper.decorator_list = []
        namespace = {}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )
        calculate = namespace["calculate_ev_adjusted_spendable_energy"]

        # Captured 2026-08-26 23:14: the EV needed 30.6 kWh by 08:00,
        # while only 1.94 kWh remained locally spendable.  The EV replay bought
        # energy from the grid and incorrectly reported 0.99 kWh as surplus.
        self.assertEqual(
            calculate(
                base_spendable_energy_kwh=1.94,
                ev_forecast_spendable_energy_kwh=0.99,
                remaining_ev_energy_kwh=30.6,
                charge_efficiency=0.9,
                obligation_due_within_horizon=True,
            ),
            0.0,
        )

    def test_ev_adjusted_surplus_rejects_invalid_energy_inputs(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        helper = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "calculate_ev_adjusted_spendable_energy"
        )
        helper.decorator_list = []
        namespace = {}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )
        calculate = namespace["calculate_ev_adjusted_spendable_energy"]
        base = dict(
            base_spendable_energy_kwh=10.0,
            ev_forecast_spendable_energy_kwh=8.0,
            remaining_ev_energy_kwh=4.0,
            charge_efficiency=0.9,
            obligation_due_within_horizon=True,
        )
        invalid_cases = (
            {"base_spendable_energy_kwh": float("nan")},
            {"ev_forecast_spendable_energy_kwh": float("inf")},
            {"remaining_ev_energy_kwh": float("nan")},
            {"base_spendable_energy_kwh": -0.1},
            {"ev_forecast_spendable_energy_kwh": -0.1},
            {"remaining_ev_energy_kwh": -0.1},
            {"charge_efficiency": float("nan")},
            {"charge_efficiency": float("inf")},
            {"local_ev_wall_energy_before_departure_kwh": float("nan")},
            {"local_ev_wall_energy_before_departure_kwh": -0.1},
        )
        for override in invalid_cases:
            with self.subTest(override=override), self.assertRaises(ValueError):
                calculate(**(base | override))

    def test_ev_departure_budget_counts_only_non_grid_wall_energy(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        selected = []
        for name in (
            "calculate_ev_adjusted_spendable_energy",
            "calculate_local_ev_wall_energy_before_departure",
        ):
            node = next(
                node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == name
            )
            node.decorator_list = []
            selected.append(node)
        namespace = {"datetime": datetime, "timedelta": timedelta}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )

        start = datetime(2026, 8, 27, 5, 0, tzinfo=timezone.utc)
        base_detail = []
        grid_funded_detail = []
        locally_funded_detail = []
        for offset in range(4):
            period_start = start + timedelta(minutes=30 * offset)
            base_detail.append(
                SimpleNamespace(period_start=period_start, power_from_grid=0.0)
            )
            grid_funded_detail.append(
                SimpleNamespace(
                    period_start=period_start,
                    power_from_grid=7000.0,
                    ev_charge_power=7000.0,
                )
            )
            locally_funded_detail.append(
                SimpleNamespace(
                    period_start=period_start,
                    power_from_grid=2000.0,
                    ev_charge_power=7000.0,
                )
            )

        local_energy = namespace[
            "calculate_local_ev_wall_energy_before_departure"
        ]
        departure = start + timedelta(hours=2)
        self.assertEqual(
            local_energy(
                base_detail,
                grid_funded_detail,
                start,
                departure,
                0.5,
            ),
            0.0,
        )
        self.assertAlmostEqual(
            local_energy(
                base_detail,
                locally_funded_detail,
                start,
                departure,
                0.5,
            ),
            10.0,
        )

        calculate = namespace["calculate_ev_adjusted_spendable_energy"]
        # 9 kWh into the EV is 10 kWh at the wall. Grid-funded charging must
        # leave no export budget even when the departure follows the trough.
        self.assertEqual(calculate(10.0, 10.0, 9.0, 0.9, True, 0.0), 0.0)
        self.assertEqual(calculate(10.0, 10.0, 9.0, 0.9, True, 10.0), 10.0)

    def test_ev_export_reservation_uses_departure_horizon_not_trough(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        for function_name in ("forecast_surplus", "auto_setpoint_target"):
            function = next(
                node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == function_name
            )
            assignment = next(
                node for node in ast.walk(function)
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name)
                    and target.id == "ev_obligation_due_within_horizon"
                    for target in node.targets
                )
            )
            expression = ast.unparse(assignment.value)
            self.assertIn("EV_EXPORT_RESERVATION_HORIZON_HOURS", expression)
            self.assertNotIn("spendable_horizon", expression)

    def test_ev_adjusted_surplus_invariants_hold_over_energy_grid(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        helper = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "calculate_ev_adjusted_spendable_energy"
        )
        helper.decorator_list = []
        namespace = {}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )
        calculate = namespace["calculate_ev_adjusted_spendable_energy"]

        for base_kwh in (0.0, 1.94, 10.0, 40.0):
            for ev_forecast_kwh in (0.0, 0.99, 6.0, 50.0):
                previous = None
                for ev_need_kwh in (0.0, 1.0, 10.0, 30.6, 60.0):
                    result = calculate(
                        base_spendable_energy_kwh=base_kwh,
                        ev_forecast_spendable_energy_kwh=ev_forecast_kwh,
                        remaining_ev_energy_kwh=ev_need_kwh,
                        charge_efficiency=0.9,
                        obligation_due_within_horizon=True,
                    )
                    with self.subTest(
                        base_kwh=base_kwh,
                        ev_forecast_kwh=ev_forecast_kwh,
                        ev_need_kwh=ev_need_kwh,
                    ):
                        self.assertGreaterEqual(result, 0.0)
                        self.assertLessEqual(result, base_kwh)
                        self.assertLessEqual(result, ev_forecast_kwh)
                        if previous is not None:
                            self.assertLessEqual(result, previous)
                    previous = result

        # Enough local energy for both the EV wall loss and 6 kWh export.
        self.assertAlmostEqual(
            calculate(40.0, 40.0, 30.6, 0.9, True),
            6.0,
        )
        # A departure beyond this solar-cycle horizon reserves nothing yet.
        self.assertAlmostEqual(
            calculate(1.94, 0.99, 30.6, 0.9, False),
            0.99,
        )
        with self.assertRaises(ValueError):
            calculate(10.0, 10.0, 1.0, 0.0, True)

    def test_ev_surplus_curve_reserves_the_captured_deadline_obligation(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        selected = []
        for name in (
            "get_spendable_solar_cycle_energy",
            "calculate_ev_adjusted_spendable_energy",
            "calculate_local_ev_wall_energy_before_departure",
            "get_ev_adjusted_spendable_solar_cycle_curve",
        ):
            node = next(
                node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == name
            )
            node.decorator_list = []
            selected.append(node)
        namespace = {"datetime": datetime, "timedelta": timedelta}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )

        start = datetime(2026, 8, 26, 23, 0, tzinfo=timezone.utc)
        base_detail = [
            SimpleNamespace(period_start=start, pv_estimate=0.0, house_power=589.0, battery_energy=12.61, pv_feedin=0.0),
            SimpleNamespace(period_start=start + timedelta(minutes=30), pv_estimate=0.0, house_power=589.0, battery_energy=7.74, pv_feedin=0.0),
            SimpleNamespace(period_start=start + timedelta(hours=1), pv_estimate=0.0, house_power=589.0, battery_energy=8.0, pv_feedin=0.0),
        ]
        ev_detail = [
            SimpleNamespace(period_start=start, pv_estimate=0.0, house_power=589.0, battery_energy=12.61, pv_feedin=0.0, ev_energy=26.4),
            SimpleNamespace(period_start=start + timedelta(minutes=30), pv_estimate=0.0, house_power=589.0, battery_energy=6.79, pv_feedin=0.0, ev_energy=26.4),
            SimpleNamespace(period_start=start + timedelta(hours=1), pv_estimate=0.0, house_power=589.0, battery_energy=7.0, pv_feedin=0.0, ev_energy=31.368),
        ]
        curve = namespace["get_ev_adjusted_spendable_solar_cycle_curve"](
            base_detail=base_detail,
            ev_detail=ev_detail,
            battery_export_floor=5.8,
            period_hours=0.5,
            initial_ev_energy_kwh=26.4,
            initial_ev_energy_needed_kwh=30.6,
            next_departure=start + timedelta(minutes=30),
            charge_efficiency=0.9,
            reservation_horizon_hours=80.0,
            battery_discharge_efficiency=0.9,
        )

        self.assertEqual(curve[0], 0.0)
        self.assertTrue(all(value >= 0.0 for value in curve))

    def _load_horizon_balance(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        helper = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "calculate_horizon_energy_balance"
        )
        helper.decorator_list = []
        namespace = {"datetime": datetime, "timedelta": timedelta}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )
        return namespace["calculate_horizon_energy_balance"]

    def test_horizon_balance_reports_signed_net_position(self):
        calculate = self._load_horizon_balance()
        start = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)

        def entry(offset_hours, pv_w, house_w, ev_w=0.0):
            return SimpleNamespace(
                period_start=start + timedelta(hours=offset_hours),
                pv_estimate=pv_w,
                house_power=house_w,
                ev_charge_power=ev_w,
            )

        sunny = [entry(h, 4000.0, 500.0) for h in (0.0, 0.5, 1.0, 1.5)]
        balance, usable, pv, house, ev = calculate(
            sunny, start, 72.0, 10.0, 4.0, 0.5, 1.0
        )
        for actual, expected in zip((usable, pv, house, ev), (6.0, 8.0, 1.0, 0.0)):
            self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(balance, 13.0)
        self.assertAlmostEqual(balance, usable + pv - house - ev)

        cloudy_with_ev = [entry(h, 500.0, 800.0, 3000.0) for h in (0.0, 0.5, 1.0, 1.5)]
        balance, usable, pv, house, ev = calculate(
            cloudy_with_ev, start, 72.0, 5.0, 4.0, 0.5, 1.0
        )
        for actual, expected in zip((usable, pv, house, ev), (1.0, 1.0, 1.6, 6.0)):
            self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(balance, -5.6)

    def test_horizon_balance_ignores_entries_outside_window(self):
        calculate = self._load_horizon_balance()
        start = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
        detail = [
            SimpleNamespace(period_start=start - timedelta(hours=1), pv_estimate=99999.0, house_power=0.0, ev_charge_power=0.0),
            SimpleNamespace(period_start=start, pv_estimate=1000.0, house_power=0.0, ev_charge_power=0.0),
            SimpleNamespace(period_start=start + timedelta(hours=73), pv_estimate=99999.0, house_power=0.0, ev_charge_power=0.0),
        ]
        balance, _, pv, _, _ = calculate(detail, start, 72.0, 4.0, 4.0, 0.5, 1.0)
        self.assertAlmostEqual(pv, 0.5)
        self.assertAlmostEqual(balance, 0.5)

    def test_horizon_balance_empty_detail_returns_battery_only(self):
        calculate = self._load_horizon_balance()
        start = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(
            calculate([], start, 72.0, 10.0, 4.0, 0.5, 0.9),
            (5.4, 5.4, 0.0, 0.0, 0.0),
        )

    def test_horizon_balance_rejects_invalid_inputs(self):
        calculate = self._load_horizon_balance()
        start = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
        base = dict(
            detail=[],
            t_now=start,
            horizon_hours=72.0,
            battery_energy_now=10.0,
            battery_export_floor=4.0,
            period_hours=0.5,
            battery_discharge_efficiency=0.9,
        )
        invalid_cases = (
            {"horizon_hours": 0.0},
            {"horizon_hours": -1.0},
            {"period_hours": 0.0},
            {"battery_discharge_efficiency": 0.0},
            {"battery_discharge_efficiency": 1.5},
            {"battery_energy_now": float("nan")},
            {"battery_export_floor": float("inf")},
        )
        for override in invalid_cases:
            with self.subTest(override=override), self.assertRaises(ValueError):
                calculate(**(base | override))

    def _load_shortfall_helpers(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        selected = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in ("get_solar_cycle_shortfall", "calculate_force_charge_target_soc")
        ]
        self.assertEqual(len(selected), 2)
        for node in selected:
            node.decorator_list = []
        namespace = {"datetime": datetime, "timedelta": timedelta}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )
        return namespace["get_solar_cycle_shortfall"], namespace["calculate_force_charge_target_soc"]

    def test_shortfall_sums_cycle_grid_import_plus_floor_top_up(self):
        shortfall, _ = self._load_shortfall_helpers()
        start = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc)

        def entry(offset_hours, battery, grid_w):
            return SimpleNamespace(
                period_start=start + timedelta(hours=offset_hours),
                pv_estimate=0.0,
                house_power=400.0,
                battery_energy=battery,
                power_from_grid=grid_w,
            )

        night = [entry(h, 10.0 - h, 0.0) for h in (0.0, 0.5, 1.0, 1.5)]
        value, horizon = shortfall(night, 1.6, start, 0.5, 0.9)
        self.assertAlmostEqual(value, 0.0)
        self.assertEqual(horizon, start + timedelta(hours=1.5))

        drained = [
            entry(0.0, 2.0, 0.0),
            entry(0.5, 0.5, 500.0),
            entry(1.0, 0.5, 500.0),
        ]
        value, _ = shortfall(drained, 1.6, start, 0.5, 0.9)
        # 0.5 kWh imported + (1.6 - 0.5) kWh spent below the floor, delivered.
        self.assertAlmostEqual(value, 0.5 + 1.1 * 0.9)

        self.assertEqual(shortfall([], 1.6, start, 0.5, 0.9), (0.0, None))

    def test_shortfall_stops_at_next_solar_start(self):
        shortfall, _ = self._load_shortfall_helpers()
        start = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)

        def entry(when, pv, house, battery, grid_w):
            return SimpleNamespace(
                period_start=when,
                pv_estimate=pv,
                house_power=house,
                battery_energy=battery,
                power_from_grid=grid_w,
            )

        detail = [
            entry(start, 3000.0, 500.0, 20.0, 0.0),
            entry(start + timedelta(hours=10), 0.0, 500.0, 5.0, 1000.0),
            entry(start + timedelta(hours=22), 3000.0, 500.0, 5.0, 0.0),
            entry(start + timedelta(hours=26), 0.0, 500.0, 1.0, 2000.0),
        ]
        value, _ = shortfall(detail, 1.6, start, 0.5, 0.9)
        # Only the in-cycle 1000 W period counts; post-sunrise import excluded.
        self.assertAlmostEqual(value, 0.5)

    def test_shortfall_rejects_invalid_inputs(self):
        shortfall, _ = self._load_shortfall_helpers()
        start = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
        base = dict(
            detail=[],
            battery_export_floor=1.6,
            t_now=start,
            period_hours=0.5,
            battery_discharge_efficiency=0.9,
        )
        for kwargs in (
            {"period_hours": 0.0},
            {"period_hours": -0.5},
            {"battery_discharge_efficiency": 0.0},
            {"battery_discharge_efficiency": 1.5},
            {"battery_export_floor": float("nan")},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                shortfall(**(base | kwargs))

    def test_force_charge_target_covers_shortfall_from_current_level(self):
        _, target = self._load_shortfall_helpers()
        self.assertEqual(target(16.0, 32.0, 0.04, 0.9, 0.05), 0.0)
        self.assertEqual(target(16.0, 32.0, 0.0, 0.9, 0.05), 0.0)
        # 3.6 kWh delivered needs 4.0 kWh stored on top of 16 kWh in 32 kWh.
        self.assertAlmostEqual(target(16.0, 32.0, 3.6, 0.9, 0.05), 62.5)
        self.assertEqual(target(30.0, 32.0, 10.0, 0.9, 0.05), 100.0)
        for args in (
            (16.0, 0.0, 3.6, 0.9, 0.05),
            (16.0, 32.0, 3.6, 0.0, 0.05),
            (16.0, 32.0, 3.6, 1.5, 0.05),
            (float("nan"), 32.0, 3.6, 0.9, 0.05),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                target(*args)

    def test_surplus_run_publishes_shortfall_and_force_charge_target(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        node = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "forecast_surplus"
        )
        shortfall_calls = [
            call for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "get_solar_cycle_shortfall"
        ]
        self.assertEqual(len(shortfall_calls), 1)
        # House-only baseline: required EV charging must never be routed
        # through the house battery.
        self.assertEqual(ast.unparse(shortfall_calls[0].args[0]), "spendable_detail_80h")
        target_sets = [
            call for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "set_state"
            and any(
                isinstance(child, ast.Attribute) and child.attr == "force_charge_up_to"
                for child in ast.walk(call)
            )
        ]
        self.assertEqual(len(target_sets), 1)
        published = [
            call for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "set_energy_surplus"
            and any(keyword.arg == "expected_shortfall_kwh" for keyword in call.keywords)
        ]
        self.assertEqual(len(published), 1)

    def test_force_charge_target_write_is_behind_its_flag(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        node = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "forecast_surplus"
        )
        gated = [
            if_node for if_node in ast.walk(node)
            if isinstance(if_node, ast.If)
            and "auto_force_charge_target" in ast.unparse(if_node.test)
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "set_state"
                and any(
                    isinstance(child, ast.Attribute) and child.attr == "force_charge_up_to"
                    for child in ast.walk(call)
                )
                for stmt in if_node.body
                for call in ast.walk(stmt)
            )
        ]
        self.assertEqual(len(gated), 1)
        # The flag defaults to off: a missing helper must not enable writes.
        self.assertIn("False", ast.unparse(gated[0].test))

    def test_balance_3_day_published_from_conservative_detail(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        forecast_surplus_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "forecast_surplus"
        )
        publish_node = next(
            node for node in ast.walk(forecast_surplus_node)
            if isinstance(node, ast.FunctionDef) and node.name == "publish_n_day_balance"
        )
        published = [
            node for node in ast.walk(publish_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "set_energy_surplus"
            and node.args
            and isinstance(node.args[0], ast.Call)
            and isinstance(node.args[0].func, ast.Name)
            and node.args[0].func.id == "getattr"
        ]
        self.assertEqual(len(published), 1)
        call_sources = {
            ast.unparse(node.args[0])
            for node in ast.walk(forecast_surplus_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "publish_n_day_balance"
        }
        self.assertEqual(
            call_sources,
            {"spendable_forecast_with_ev.detail", "spendable_forecast.detail"},
        )

    def _load_floor_reserve(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        helper = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "calculate_ev_floor_reserve"
        )
        helper.decorator_list = []
        namespace = {"datetime": datetime, "timedelta": timedelta}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                "energy.py",
                "exec",
            ),
            namespace,
        )
        return namespace["calculate_ev_floor_reserve"]

    def test_ev_floor_reserve_tops_up_post_drive_dip(self):
        calculate = self._load_floor_reserve()
        start = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)

        def entry(offset_hours, ev_energy):
            return SimpleNamespace(
                period_start=start + timedelta(hours=offset_hours),
                ev_energy=ev_energy,
            )

        detail = [
            entry(0.0, 40.0),
            entry(1.0, 41.7),
            entry(24.0, 8.0),
            entry(48.0, 8.0),
        ]
        self.assertAlmostEqual(
            calculate(detail, start, 72.0, 42.0, None, 0.9), (42.0 - 8.0) / 0.9
        )

    def test_ev_floor_reserve_zero_when_window_end_above_floor(self):
        calculate = self._load_floor_reserve()
        start = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
        detail = [
            SimpleNamespace(period_start=start + timedelta(hours=h), ev_energy=ev)
            for h, ev in ((0.0, 30.0), (1.0, 45.0), (24.0, 45.0))
        ]
        self.assertEqual(calculate(detail, start, 72.0, 42.0, None, 0.9), 0.0)

    def test_ev_floor_reserve_uses_fallback_without_trajectory(self):
        calculate = self._load_floor_reserve()
        start = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
        detail = [
            SimpleNamespace(period_start=start + timedelta(hours=h), ev_energy=None)
            for h in (0.0, 1.0)
        ]
        self.assertAlmostEqual(
            calculate(detail, start, 72.0, 42.0, 30.0, 0.9), (42.0 - 30.0) / 0.9
        )
        self.assertEqual(calculate(detail, start, 72.0, 42.0, None, 0.9), 0.0)
        self.assertEqual(calculate([], start, 72.0, 42.0, None, 0.9), 0.0)

    def test_ev_floor_reserve_ignores_dip_after_window(self):
        calculate = self._load_floor_reserve()
        start = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
        detail = [
            SimpleNamespace(period_start=start, ev_energy=45.0),
            SimpleNamespace(
                period_start=start + timedelta(hours=73), ev_energy=5.0
            ),
        ]
        self.assertEqual(calculate(detail, start, 72.0, 42.0, None, 0.9), 0.0)

    def test_ev_floor_reserve_rejects_invalid_inputs(self):
        calculate = self._load_floor_reserve()
        start = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
        base = dict(
            detail=[],
            t_now=start,
            horizon_hours=72.0,
            ev_min_energy_kwh=42.0,
            ev_energy_fallback=None,
            ev_charge_efficiency=0.9,
        )
        invalid_cases = (
            {"horizon_hours": 0.0},
            {"ev_charge_efficiency": 0.0},
            {"ev_charge_efficiency": 1.5},
            {"ev_min_energy_kwh": float("nan")},
            {"ev_energy_fallback": float("inf")},
        )
        for override in invalid_cases:
            with self.subTest(override=override), self.assertRaises(ValueError):
                calculate(**(base | override))

    def test_balance_5_day_publishes_floor_reserve(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        forecast_surplus_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "forecast_surplus"
        )
        publish_node = next(
            node for node in ast.walk(forecast_surplus_node)
            if isinstance(node, ast.FunctionDef) and node.name == "publish_n_day_balance"
        )
        reserve_calls = [
            node for node in ast.walk(publish_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "calculate_ev_floor_reserve"
        ]
        self.assertEqual(len(reserve_calls), 1)
        horizon_calls = [
            node for node in ast.walk(publish_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id
            in {"calculate_horizon_energy_balance", "calculate_ev_floor_reserve"}
        ]
        self.assertEqual(len(horizon_calls), 2)
        for node in horizon_calls:
            self.assertEqual(ast.unparse(node.args[2]), "hours")
        published = [
            node for node in ast.walk(publish_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "set_energy_surplus"
            and any(
                keyword.arg == "ev_floor_reserve_kwh" for keyword in node.keywords
            )
        ]
        self.assertEqual(len(published), 1)

    def test_ev_surplus_publication_uses_conservative_same_horizon_curve(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        forecast_surplus_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "forecast_surplus"
        )

        conservative_ev_forecasts = []
        for node in ast.walk(forecast_surplus_node):
            if not (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "spendable_forecast_with_ev"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Call)
            ):
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.value.keywords}
            conservative_ev_forecasts.append(keywords)

        self.assertEqual(len(conservative_ev_forecasts), 1)
        keywords = conservative_ev_forecasts[0]
        self.assertEqual(keywords["forecast_dampening"].value, 0.8)
        self.assertEqual(keywords["with_ev_charging"].id, "forecast_ev_charging_enabled")
        self.assertEqual(keywords["surplus_energy"].value, 0)
        self.assertEqual(keywords["battery_min_energy"].id, "surplus_battery_floor")

        published_ev_forecasts = []
        for node in ast.walk(forecast_surplus_node):
            if not (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "forecast_with_ev"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Call)
            ):
                continue
            published_ev_forecasts.append(
                {keyword.arg: keyword.value for keyword in node.value.keywords}
            )

        self.assertEqual(len(published_ev_forecasts), 1)
        published_keywords = published_ev_forecasts[0]
        self.assertEqual(published_keywords["forecast_dampening"].value, 0.8)
        self.assertEqual(
            published_keywords["ev_battery_support_energy_kwh"].value,
            0,
        )

        raw_minimum_publications = [
            node
            for node in ast.walk(forecast_surplus_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "min"
            and any(
                isinstance(child, ast.Attribute) and child.attr == "surplus"
                for child in ast.walk(node)
            )
        ]
        self.assertEqual(raw_minimum_publications, [])

        published_curve_assignments = [
            node
            for node in ast.walk(forecast_surplus_node)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "detail_with_ev_vectorized"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "surplus"
                for target in node.targets
            )
        ]
        self.assertEqual(len(published_curve_assignments), 1)
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "get_ev_adjusted_spendable_solar_cycle_curve"
                for node in ast.walk(forecast_surplus_node)
            )
        )

        base_curve_assignments = [
            node
            for node in ast.walk(forecast_surplus_node)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "detail_without_ev_vectorized"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "surplus"
                for target in node.targets
            )
        ]
        self.assertEqual(len(base_curve_assignments), 1)

        self.assertIsInstance(keywords["optional_export_power_schedule"], ast.Dict)
        self.assertEqual(keywords["optional_export_power_schedule"].keys, [])
        self.assertEqual(keywords["battery_export_budget_kwh"].value, 0)

        surplus_assignment = next(
            node for node in ast.walk(forecast_surplus_node)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "surplus_after_ev_charging"
                for target in node.targets
            )
        )
        self.assertIsInstance(surplus_assignment.value, ast.Call)
        self.assertEqual(
            surplus_assignment.value.func.id,
            "calculate_ev_adjusted_spendable_energy",
        )
        published_surplus_calls = [
            node for node in ast.walk(forecast_surplus_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "set_energy_surplus"
            and any(
                isinstance(child, ast.Attribute)
                and child.attr == "energy_surplus_after_ev_charging"
                for child in ast.walk(node)
            )
        ]
        self.assertEqual(len(published_surplus_calls), 2)
        incident_publication = next(
            call for call in published_surplus_calls
            if any(
                keyword.arg == "ev_obligation_due_within_horizon"
                and isinstance(keyword.value, ast.Name)
                for keyword in call.keywords
            )
        )
        publication_keywords = {keyword.arg for keyword in incident_publication.keywords}
        self.assertTrue(
            {
                "base_spendable_energy",
                "ev_forecast_spendable_energy",
                "ev_energy_needed",
                "ev_wall_energy_required",
                "calculated_at",
            }
            <= publication_keywords
        )

    def test_legacy_controller_remains_a_separate_flagged_branch(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "auto_setpoint_target"
        )
        feature_flag_reads = [
            node
            for node in ast.walk(target_node)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "Automation"
            and node.attr == "unified_export_scheduler"
        ]
        self.assertTrue(feature_flag_reads)
        self.assertTrue(any(isinstance(node, ast.Return) for node in ast.walk(target_node)))

    def test_unified_scheduler_uses_ev_aware_surplus_and_trajectory_floor(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target_node = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "auto_setpoint_target"
        )

        raw_published_policy_assignment = next(
            node
            for node in ast.walk(target_node)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "raw_published_policy_surplus_energy_kwh"
                for target in node.targets
            )
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "House"
                and node.attr == "energy_surplus_after_ev_charging"
                for node in ast.walk(raw_published_policy_assignment.value)
            )
        )

        published_policy_assignment = next(
            node
            for node in ast.walk(target_node)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "published_policy_surplus_energy_kwh"
                for target in node.targets
            )
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "export_policy_budget"
                and node.attr == "ev_adjusted_spendable_energy_kwh"
                for node in ast.walk(published_policy_assignment.value)
            )
        )

        policy_assignment = next(
            node for node in ast.walk(target_node)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "policy_surplus_energy_kwh"
                for target in node.targets
            )
        )
        self.assertIsInstance(policy_assignment.value, ast.Call)
        self.assertEqual(
            policy_assignment.value.func.id,
            "calculate_ev_adjusted_spendable_energy",
        )
        policy_keywords = {
            keyword.arg: keyword.value for keyword in policy_assignment.value.keywords
        }
        self.assertEqual(policy_keywords["remaining_ev_energy_kwh"].id, "ev_energy_needed")
        self.assertEqual(
            policy_keywords["obligation_due_within_horizon"].id,
            "ev_obligation_due_within_horizon",
        )

        budget_calls = [
            node for node in ast.walk(target_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "calculate_export_energy_budget"
        ]
        self.assertEqual(len(budget_calls), 1)
        budget_keywords = {
            keyword.arg: keyword.value for keyword in budget_calls[0].keywords
        }
        self.assertEqual(
            budget_keywords["spendable_energy_after_ev_kwh"].id,
            "policy_surplus_energy_kwh",
        )

        floor_assignment = next(
            node for node in target_node.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "battery_min_energy"
                for target in node.targets
            )
        )
        self.assertIsInstance(floor_assignment.value, ast.IfExp)
        self.assertTrue(
            any(
                isinstance(node, ast.Name) and node.id == "unified_scheduler_enabled"
                for node in ast.walk(floor_assignment.value.test)
            )
        )

    def test_unified_neutral_target_does_not_feed_back_the_previous_basis(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "auto_setpoint_target"
        )
        configured_assignment = next(
            node
            for node in target_node.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "max_setpoint"
                for target in node.targets
            )
        )
        self.assertIsInstance(configured_assignment.value, ast.IfExp)

    def test_forecast_applies_headroom_control_after_price_mapping(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        forecast_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_forecast"
        )
        found = any(
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "this_setpoint" for target in node.targets)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "min"
            and any(isinstance(argument, ast.Name) and argument.id == "scheduled_headroom_w" for argument in node.value.args)
            for node in ast.walk(forecast_node)
        )
        self.assertTrue(found)

    def test_unified_raw_peak_uses_natural_surplus_not_capped_feedin(self):
        # The neutral replay caps pv_feedin at the neutral setpoint while the
        # simulated battery absorbs the rest, so the raw peak scanner must use
        # natural surplus (pv_estimate - house_power).  Regression test for the
        # "raw PV peak 100 W at 09:00" misreport on 2026-09-19, where the true
        # midday surplus was ~1055 W.
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "auto_setpoint_target"
        )
        peak_comparisons = [
            node
            for node in ast.walk(target_node)
            if isinstance(node, ast.Compare)
            and any(isinstance(comparator, ast.Name) and comparator.id == "raw_peak_w" for comparator in node.comparators)
        ]
        self.assertEqual(len(peak_comparisons), 1)
        self.assertIsInstance(peak_comparisons[0].left, ast.Name)
        peak_source = next(
            node
            for node in ast.walk(target_node)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == peak_comparisons[0].left.id for target in node.targets)
        )
        compared_attrs = {
            node.attr for node in ast.walk(peak_source.value) if isinstance(node, ast.Attribute)
        }
        self.assertIn("pv_estimate", compared_attrs)
        self.assertIn("house_power", compared_attrs)
        self.assertNotIn("pv_feedin", compared_attrs)

    def test_balance_chase_withholds_export_until_cells_rebalance(self):
        # Ten days without 100% (2026-09-12..21) while exporting 23+ kWh on
        # strong days: once the last full charge is a week back, the
        # scheduler must withhold economic export and soft-target
        # pre-release until the battery fills again.
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        target_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "auto_setpoint_target"
        )
        schedule_calls = [
            node
            for node in ast.walk(target_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_unified_export_schedule"
        ]
        self.assertEqual(len(schedule_calls), 1)
        keywords = {keyword.arg: keyword.value for keyword in schedule_calls[0].keywords}
        self.assertIn("top_up_pv_confidence_factor", keywords)
        self.assertIn("allow_economic_export", keywords)
        allow_value = keywords["allow_economic_export"]
        conjuncts = (
            allow_value.values
            if isinstance(allow_value, ast.BoolOp)
            else [allow_value]
        )
        chase_conjuncts = [
            value
            for value in conjuncts
            if isinstance(value, ast.UnaryOp)
            and isinstance(value.op, ast.Not)
            and isinstance(value.operand, ast.Name)
            and value.operand.id == "balance_chase_active"
        ]
        self.assertEqual(len(chase_conjuncts), 1)

        chase_assignment = next(
            node
            for node in ast.walk(target_node)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "balance_chase_active" for target in node.targets)
        )
        chase_names = {node.id for node in ast.walk(chase_assignment.value) if isinstance(node, ast.Name)}
        self.assertIn("days_since_full_charge", chase_names)
        self.assertIn("BALANCE_CHASE_AFTER_DAYS", chase_names)

        energy_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "battery_energy"
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Constant) and node.value == "pyscript.battery_last_full"
                for node in ast.walk(energy_node)
            )
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "persist"
                and any(
                    isinstance(arg, ast.Constant) and arg.value == "pyscript.battery_last_full"
                    for arg in node.args
                )
                for node in ast.walk(tree)
            )
        )

    def test_scheduler_freezes_live_forecast_inputs_across_replays(self):
        # Neutral, candidate, and unified replays run seconds apart with task
        # yields between them.  A mid-run entity flip (observed 2026-09-19:
        # the telemetry hold set the discharge limit to 0 between replays)
        # must not make the import guard compare two different worlds.
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)

        target_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "auto_setpoint_target"
        )
        frozen_assignment = next(
            node
            for node in ast.walk(target_node)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "frozen_forecast_inputs" for target in node.targets)
        )
        self.assertIsInstance(frozen_assignment.value, ast.Dict)
        frozen_keys = {
            key.value for key in frozen_assignment.value.keys if isinstance(key, ast.Constant)
        }
        for expected in (
            "battery_discharge_limit_w",
            "daily_power",
            "nightly_power",
            "ev_soc",
            "is_charging_ev",
            "ev_required_soc",
            "smart_limiter_active",
            "charge_limit",
            "max_charge_price",
            "force_charge_switch",
            "min_discharge_price",
            "charger_ready",
            "charger_ready_since",
            "ev_plugged_in",
            "eff_dis",
            "battery_cells_balanced",
        ):
            self.assertIn(expected, frozen_keys)

        ctx_calls = [
            node
            for node in ast.walk(target_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SetpointForecastContext"
        ]
        self.assertEqual(len(ctx_calls), 1)
        ctx_keywords = {keyword.arg: keyword.value for keyword in ctx_calls[0].keywords}
        self.assertIn("frozen_forecast_inputs", ctx_keywords)

        local_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "forecast_setpoint_local"
        )
        forecast_calls = [
            node
            for node in ast.walk(local_node)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "forecast"
        ]
        self.assertEqual(len(forecast_calls), 1)
        self.assertTrue(
            any(
                keyword.arg is None
                and isinstance(keyword.value, ast.Attribute)
                and keyword.value.attr == "frozen_forecast_inputs"
                for keyword in forecast_calls[0].keywords
            )
        )


    def test_silent_charge_shaping_binds_above_margin_band(self):
        schedule = build_silent_charge_shaping_schedule(
            [
                ("peak", 6647.0, 647.0, 0.0),
                ("band", 4347.0, 647.0, 0.0),
                ("low", 2647.0, 647.0, 0.0),
                ("ev", 6647.0, 647.0, 2500.0),
            ],
            silent_power_w=3500.0,
            surplus_margin_w=500.0,
        )

        # 6000 W shareable surplus exports the 2500 W above silent power.
        self.assertEqual(schedule, {"peak": 2500.0})

    def test_silent_charge_shaping_ignores_bad_input(self):
        self.assertEqual(
            build_silent_charge_shaping_schedule(
                [("bad", float("nan"), 0.0, 0.0), ("also-bad",)],
                silent_power_w=3500.0,
                surplus_margin_w=500.0,
            ),
            {},
        )
        self.assertEqual(
            build_silent_charge_shaping_schedule(
                [("peak", 6647.0, 647.0, 0.0)],
                silent_power_w=float("nan"),
            ),
            {},
        )

    def test_silent_live_limits_cap_charge_above_margin(self):
        # 6000 W shareable: charge capped at 3500 W, 2500 W exported.
        self.assertEqual(
            apply_silent_mode_grid_limits(-100.0, 6600.0, 600.0, 0.0, 5000.0),
            -2500.0,
        )
        # Inside the margin band the battery takes everything.
        self.assertEqual(
            apply_silent_mode_grid_limits(-100.0, 4300.0, 600.0, 0.0, 5000.0),
            -100.0,
        )
        # EV charging consumes surplus first: 2500 W shareable, no shaping.
        self.assertEqual(
            apply_silent_mode_grid_limits(-100.0, 6600.0, 600.0, 3500.0, 5000.0),
            -100.0,
        )

    def test_silent_live_limits_cap_grid_discharge_not_house(self):
        # Night export capped at 3500 W battery-to-grid.
        self.assertEqual(
            apply_silent_mode_grid_limits(-5000.0, 0.0, 500.0, 0.0, 5000.0),
            -3500.0,
        )
        # PV passthrough is not battery discharge: 5000 W export on 2000 W
        # surplus only draws 3000 W from the battery, untouched.
        self.assertEqual(
            apply_silent_mode_grid_limits(-5000.0, 2600.0, 600.0, 0.0, 5000.0),
            -5000.0,
        )
        # Same export on 1000 W surplus draws 4000 W: capped to 4500 W.
        self.assertEqual(
            apply_silent_mode_grid_limits(-5000.0, 1600.0, 600.0, 0.0, 5000.0),
            -4500.0,
        )
        # House coverage from the battery stays unlimited (import untouched).
        self.assertEqual(
            apply_silent_mode_grid_limits(2000.0, 0.0, 5000.0, 0.0, 5000.0),
            2000.0,
        )

    def test_silent_live_limits_prefer_export_cap_over_silence(self):
        # 9000 W shareable would export 5500 W; the 5000 W cap wins and the
        # battery charges 4000 W instead of 3500 W.
        self.assertEqual(
            apply_silent_mode_grid_limits(-100.0, 9600.0, 600.0, 0.0, 5000.0),
            -5000.0,
        )

    def test_silent_live_limits_ignore_bad_input(self):
        self.assertEqual(
            apply_silent_mode_grid_limits(-100.0, float("nan"), 600.0, 0.0, 5000.0),
            -100.0,
        )
        self.assertEqual(
            apply_silent_mode_grid_limits("junk", 6600.0, 600.0, 0.0, 5000.0),
            "junk",
        )

    def test_silent_mode_is_wired_into_planner_and_live_loop(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        planner = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_setpoint_target"
        )
        planner_source = ast.unparse(planner)
        self.assertIn("Automation.silent_mode", planner_source)
        self.assertIn("scheduler_max_discharge_w", planner_source)
        self.assertIn("build_silent_charge_shaping_schedule", planner_source)
        live = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_apply_setpoint"
        )
        live_source = ast.unparse(live)
        self.assertIn("Automation.silent_mode", live_source)
        self.assertIn("apply_silent_mode_grid_limits", live_source)
        states_source = (
            Path(__file__).parents[1].joinpath("modules/states.py").read_text()
        )
        self.assertIn('silent_mode = "input_boolean.silent_mode"', states_source)


class TestResolveEconomicExportFloor(unittest.TestCase):
    def test_fixed_feedin_below_cheapest_rebuy_blocks_economic_export(self):
        # 0.08 feed-in can never beat the 0.1775 night-low rebuy: tonight's
        # 19:00 sale of stored energy must not be scheduled.
        future_import = [0.2875] * 20 + [0.1775] * 20
        self.assertIsNone(
            resolve_economic_export_floor(0.0, 0.08, future_import)
        )

    def test_dynamic_feedin_above_rebuy_sets_profitable_floor(self):
        # After the contract switch the cap no longer binds, so EPEX-ranked
        # economic export returns with the cheapest rebuy as its floor.
        floor = resolve_economic_export_floor(0.0, 1.0, [0.2875, 0.1775])
        self.assertAlmostEqual(floor, 0.1775)

    def test_configured_floor_still_wins_when_higher(self):
        floor = resolve_economic_export_floor(0.25, 1.0, [0.2875, 0.1775])
        self.assertAlmostEqual(floor, 0.25)

    def test_invalid_inputs_fail_safe_to_blocked(self):
        self.assertIsNone(resolve_economic_export_floor(0.0, 0.08, []))
        self.assertIsNone(resolve_economic_export_floor(0.0, nan, [0.1775]))
        self.assertIsNone(
            resolve_economic_export_floor(0.0, 1.0, [0.1775, float("inf")])
        )

    def test_profitability_gate_is_wired_into_planner(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        planner = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_setpoint_target"
        )
        planner_source = ast.unparse(planner)
        self.assertIn("resolve_economic_export_floor", planner_source)
        self.assertIn("feedin_compensation_cap", planner_source)
        states_source = (
            Path(__file__).parents[1].joinpath("modules/states.py").read_text()
        )
        self.assertIn(
            'feedin_compensation_cap = "input_number.feedin_compensation_cap"',
            states_source,
        )


if __name__ == "__main__":
    unittest.main()
