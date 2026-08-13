import ast
import inspect
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.setpoint_control import (
    FeedinCandidate,
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

    def test_five_watt_half_hour_exceedance_is_tiny(self):
        samples = [
            (self.t_now, 3005.0),
            (self.t_now + timedelta(minutes=30), 2900.0),
        ]
        overflow = calculate_pv_overflow_energy(samples, 3000.0, self.t_now)
        self.assertAlmostEqual(overflow, 0.0025)
        self.assertFalse(feedin_constraint_exceeded(FeedinCandidate(-100.0, 3005.0, overflow), 3000.0))

    def test_meaningful_exceedance_is_expressed_as_headroom_energy(self):
        samples = [
            (self.t_now, 3500.0),
            (self.t_now + timedelta(minutes=30), 3000.0),
        ]
        overflow = calculate_pv_overflow_energy(samples, 3000.0, self.t_now)
        self.assertAlmostEqual(overflow, 0.25)
        self.assertTrue(feedin_constraint_exceeded(FeedinCandidate(-100.0, 3500.0, overflow), 3000.0))

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
        self.assertEqual(len(forecast_calls), 3)
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

    def test_unified_forecast_uses_the_published_trajectory_without_price_mapping(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        forecast_node = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_forecast"
        )
        direct_lookup = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "grid_target_schedule"
            and node.func.attr == "get"
            for node in ast.walk(forecast_node)
        )
        self.assertTrue(direct_lookup)

    def test_live_unified_control_adapts_the_same_planned_battery_power(self):
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
