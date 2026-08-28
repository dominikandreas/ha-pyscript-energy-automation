import ast
import inspect
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from modules.setpoint_control import (
    FeedinCandidate,
    calculate_auto_max_setpoint,
    apply_hard_feedin_control,
    build_price_aware_headroom_schedule,
    calculate_energy_bounded_control,
    calculate_headroom_energy_requirement,
    calculate_pv_overflow_energy,
    choose_stable_candidate,
    feedin_constraint_exceeded,
    limit_target_step,
)


class SetpointControlTests(unittest.TestCase):
    def setUp(self):
        self.t_now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

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

    def test_forecast_wrapper_preserves_explicit_zero_inputs(self):
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
            "min_discharge_price",
        ):
            self.assertIn("is not None", assignments[name])

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
        forecast_calls = [
            node
            for node in ast.walk(forecast_surplus_node)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "forecast"
        ]
        self.assertEqual(len(forecast_calls), 4)
        for call in forecast_calls:
            published_limit = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "max_battery_power_target"),
                None,
            )
            self.assertIsInstance(published_limit, ast.Name)
            self.assertEqual(published_limit.id, "max_battery_power_target")

            published_headroom = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "headroom_schedule"),
                None,
            )
            self.assertIsInstance(published_headroom, ast.Name)
            self.assertEqual(published_headroom.id, "headroom_schedule")

            published_schedule = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "grid_target_schedule"),
                None,
            )
            self.assertIsNotNone(published_schedule)

            published_neutral_target = next(
                (keyword.value for keyword in call.keywords if keyword.arg == "max_setpoint"),
                None,
            )
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
            electricity_price=0.30,
            min_discharge_price=0.10,
            max_charge_price=0.0,
            charge_limit_percent=0.0,
            force_charge_switch=False,
            t_now=self.t_now,
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
        self.assertEqual(normal_mode, namespace["InverterMode"].on)

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

        self.assertEqual(enforced_ev_mode, namespace["InverterMode"].on)
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

    def test_dashboard_forecasts_consume_optional_export_and_reserve_schedules(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        forecast_surplus_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "forecast_surplus"
        )
        forecast_calls = [
            node
            for node in ast.walk(forecast_surplus_node)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "forecast"
        ]
        self.assertEqual(len(forecast_calls), 4)
        for call in forecast_calls:
            keyword_names = {keyword.arg for keyword in call.keywords}
            self.assertIn("optional_export_power_schedule", keyword_names)
            self.assertIn("battery_floor_schedule", keyword_names)

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
            published_keywords["ev_battery_support_energy_kwh"].id,
            "previous_ev_adjusted_surplus",
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

        published_policy_assignment = next(
            node for node in ast.walk(target_node)
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
                and node.value.id == "House"
                and node.attr == "energy_surplus_after_ev_charging"
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
            and node.func.id == "get_policy_bounded_export_budget"
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


if __name__ == "__main__":
    unittest.main()
