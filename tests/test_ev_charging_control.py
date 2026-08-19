import ast
import unittest
from datetime import datetime, time, timedelta, timezone
from math import pi
from pathlib import Path
from types import SimpleNamespace
from typing import Any


class _EVConst:
    ev_capacity = 60
    kwh_per_100km = 16
    max_current = 16
    min_current = 6
    min_phases = 1
    voltage = 230
    charge_efficiency = 0.9


class _Victron:
    PAYLOAD_TO_MODE = {}


def _clip(value, lower, upper):
    return max(lower, min(upper, value))


def _load_energy_core_symbols():
    source_path = Path(__file__).parents[1] / "modules" / "energy_core.py"
    tree = ast.parse(source_path.read_text())
    required_names = {
        "ChargeAction",
        "ChargeMode",
        "HYSTERESIS_BUFFER",
        "SURPLUS_START_MARGIN",
        "SURPLUS_STOP_MARGIN",
        "calculate_available_ev_power",
        "calculate_charger_current_adjustment",
        "get_drive_energy_drain",
        "get_ev_charge_energy_limit",
        "get_settled_simulated_ev_charge_action",
        "get_simulated_ev_power_inputs",
        "get_drive_required_soc",
        "get_forecast_drive_context",
        "get_ongoing_and_next_drive",
        "is_vehicle_present_during_active_drive",
        "is_forecast_ev_available",
        "_get_charge_action",
    }
    selected = []
    selected_names = set()
    for node in tree.body:
        names = set()
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))

        if names & required_names:
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = []
            selected.append(node)
            selected_names.update(names)

    missing = required_names - selected_names
    if missing:
        raise AssertionError(f"Missing energy-core symbols: {sorted(missing)}")

    namespace = {
        "Const": _EVConst,
        "Victron": _Victron,
        "clip": _clip,
    }
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace


class EVChargingControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = _load_energy_core_symbols()

    def test_incident_surplus_headroom_starts_the_ev_below_three_kw(self):
        t_now = datetime(2026, 8, 17, 18, 10, tzinfo=timezone.utc)
        action, phases, current, _reason, charge_mode = self.core["_get_charge_action"](
            next_drive=t_now + timedelta(hours=12, minutes=20),
            current_soc=34,
            required_soc=80.83,
            energy_needed=28.1,
            excess_power=2718,
            excess_target=174,
            surplus_energy=0.27,
            smart_charge_limit=95,
            smart_limiter_active=True,
            configured_phases=1,
            configured_current=6,
            is_low_price=False,
            pv_total_power=3000,
            battery_soc=30,
            is_charging=False,
            t_now=t_now,
            inverter_mode="off",
            battery_force_charge=False,
            wallbox_power=0,
        )

        self.assertEqual(action, self.core["ChargeAction"].on)
        self.assertEqual(charge_mode, self.core["ChargeMode"].surplus)
        self.assertEqual((phases, current), (1, 11))

    def test_negative_battery_discharge_target_is_not_ev_surplus(self):
        available_power = self.core["calculate_available_ev_power"](
            excess_power=-457,
            excess_target=-2401,
            wallbox_power=0,
            is_charging=False,
        )

        self.assertEqual(available_power, -457)

    def test_protected_home_battery_floor_blocks_optional_ev_charge(self):
        t_now = datetime(2026, 8, 19, 11, 0, tzinfo=timezone.utc)
        action, _phases, _current, reason, mode = self.core["_get_charge_action"](
            next_drive=t_now + timedelta(days=6),
            current_soc=47,
            required_soc=80,
            energy_needed=19.8,
            excess_power=2500,
            excess_target=-1500,
            surplus_energy=5,
            smart_charge_limit=80,
            smart_limiter_active=True,
            configured_phases=1,
            configured_current=6,
            is_low_price=False,
            pv_total_power=3000,
            battery_soc=12.1,
            is_charging=False,
            t_now=t_now,
            inverter_mode="off",
            battery_force_charge=False,
            wallbox_power=0,
            battery_floor_soc=18.125,
        )

        self.assertEqual(action, self.core["ChargeAction"].off)
        self.assertEqual(mode, self.core["ChargeMode"].hard_stop)
        self.assertIn("Protected home battery floor", reason)

    def test_trip_emergency_overrides_home_battery_floor(self):
        t_now = datetime(2026, 8, 19, 11, 0, tzinfo=timezone.utc)
        action, phases, current, _reason, mode = self.core["_get_charge_action"](
            next_drive=t_now + timedelta(hours=1),
            current_soc=47,
            required_soc=80,
            energy_needed=19.8,
            excess_power=-500,
            excess_target=500,
            surplus_energy=0,
            smart_charge_limit=100,
            smart_limiter_active=False,
            configured_phases=1,
            configured_current=6,
            is_low_price=False,
            pv_total_power=0,
            battery_soc=12.1,
            is_charging=False,
            t_now=t_now,
            inverter_mode="off",
            battery_force_charge=False,
            wallbox_power=0,
            battery_floor_soc=18.125,
        )

        self.assertEqual(action, self.core["ChargeAction"].on)
        self.assertEqual(mode, self.core["ChargeMode"].required)
        self.assertEqual((phases, current), (3, 16))

    def test_cheap_required_window_overrides_home_battery_floor(self):
        t_now = datetime(2026, 8, 19, 1, 0, tzinfo=timezone.utc)
        action, _phases, _current, _reason, mode = self.core["_get_charge_action"](
            next_drive=t_now + timedelta(hours=12),
            current_soc=47,
            required_soc=80,
            energy_needed=19.8,
            excess_power=-500,
            excess_target=500,
            surplus_energy=0,
            smart_charge_limit=100,
            smart_limiter_active=False,
            configured_phases=3,
            configured_current=6,
            is_low_price=True,
            pv_total_power=0,
            battery_soc=12.1,
            is_charging=False,
            t_now=t_now,
            inverter_mode="off",
            battery_force_charge=False,
            wallbox_power=0,
            battery_floor_soc=18.125,
        )

        self.assertEqual(action, self.core["ChargeAction"].on)
        self.assertEqual(mode, self.core["ChargeMode"].required)

    def test_active_drive_does_not_hide_the_following_departure(self):
        t_now = datetime(2026, 8, 17, 18, 10, tzinfo=timezone.utc)
        active = SimpleNamespace(
            start=t_now - timedelta(hours=3, minutes=40),
            end=t_now + timedelta(minutes=20),
        )
        following = SimpleNamespace(
            start=datetime(2026, 8, 18, 6, 30, tzinfo=timezone.utc),
            end=datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc),
        )

        ongoing, next_drive = self.core["get_ongoing_and_next_drive"](
            [active, following],
            t_now,
        )

        self.assertIs(ongoing, active)
        self.assertIs(next_drive, following)

    def test_distance_based_trip_requirement_has_one_twenty_percent_margin(self):
        required_soc = self.core["get_drive_required_soc"](
            required_soc=None,
            distance=265.625,
            default_required_soc=65,
        )

        self.assertAlmostEqual(required_soc, 85.0)

    def test_returned_car_is_available_during_only_the_current_drive_window(self):
        t_now = datetime(2026, 8, 17, 18, 10, tzinfo=timezone.utc)
        active = SimpleNamespace(
            start=t_now - timedelta(hours=3, minutes=40),
            end=t_now + timedelta(minutes=20),
        )
        following = SimpleNamespace(
            start=datetime(2026, 8, 18, 6, 30, tzinfo=timezone.utc),
            end=datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc),
        )

        ongoing, next_drive = self.core["get_forecast_drive_context"](
            [active, following],
            t_now,
            active,
            True,
        )

        self.assertIsNone(ongoing)
        self.assertIs(next_drive, following)

    def test_stale_ready_state_from_before_departure_does_not_cancel_drive(self):
        drive = SimpleNamespace(
            start=datetime(2026, 8, 18, 6, 30, tzinfo=timezone.utc),
            end=datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc),
        )
        stale_ready_since = datetime(2026, 8, 18, 2, 57, tzinfo=timezone.utc)

        vehicle_present = self.core["is_vehicle_present_during_active_drive"](
            drive,
            charger_ready=True,
            charger_ready_since=stale_ready_since,
            is_charging=False,
        )
        ongoing, _ = self.core["get_forecast_drive_context"](
            [drive],
            drive.start + timedelta(minutes=30),
            drive,
            vehicle_present,
        )

        self.assertFalse(vehicle_present)
        self.assertIs(ongoing, drive)

    def test_ready_transition_after_departure_marks_returned_car_present(self):
        drive = SimpleNamespace(
            start=datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc),
            end=datetime(2026, 8, 17, 18, 30, tzinfo=timezone.utc),
        )
        returned_at = datetime(2026, 8, 17, 18, 10, tzinfo=timezone.utc)

        vehicle_present = self.core["is_vehicle_present_during_active_drive"](
            drive,
            charger_ready=True,
            charger_ready_since=returned_at,
            is_charging=False,
        )

        self.assertTrue(vehicle_present)

    def test_forecast_keeps_initially_present_ev_available_after_simulated_stop(self):
        t_now = datetime(2026, 8, 19, 11, 0, tzinfo=timezone.utc)
        available = self.core["is_forecast_ev_available"](
            ev_schedule=[],
            t_now=t_now,
            forecast_time=t_now + timedelta(days=1),
            ongoing_drive=None,
            vehicle_present_now=True,
        )

        self.assertTrue(available)

    def test_required_charge_bucket_caps_at_required_soc(self):
        required_limit = self.core["get_ev_charge_energy_limit"](
            self.core["ChargeMode"].required,
            required_soc=85,
            smart_charge_limit=100,
        )
        surplus_limit = self.core["get_ev_charge_energy_limit"](
            self.core["ChargeMode"].surplus,
            required_soc=85,
            smart_charge_limit=100,
        )

        self.assertEqual(required_limit, 51.0)
        self.assertEqual(surplus_limit, 60.0)

    def test_active_drive_consumes_its_full_energy_across_all_buckets(self):
        drive = SimpleNamespace(
            start=datetime(2026, 8, 18, 6, 30, tzinfo=timezone.utc),
            end=datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc),
            distance=265.625,
        )

        bucket_drains = [
            self.core["get_drive_energy_drain"](drive, period_hours=0.5)
            for _ in range(5)
        ]

        self.assertAlmostEqual(bucket_drains[0], 8.5)
        self.assertAlmostEqual(sum(bucket_drains), 42.5)

    def test_falling_pv_surplus_charge_does_not_create_forecast_grid_import(self):
        # Captured from the 2026-08-19 forecast.  The old simulator fed
        # pre-wallbox excess into the live controller contract, ramped to 16 A,
        # and invented 2.69 kWh of grid import between 13:00 and 16:00.
        buckets = [
            (10, 0, 3324.3, 388.783),
            (10, 30, 3753.0, 1251.0),
            (11, 0, 4096.9, 1365.633),
            (11, 30, 4412.9, 1470.967),
            (12, 0, 4508.7, 1502.9),
            (12, 30, 4447.6, 1482.533),
            (13, 0, 4253.3, 1417.767),
            (13, 30, 3947.7, 1315.9),
            (14, 0, 3670.9, 1223.633),
            (14, 30, 3410.8, 1136.933),
            (15, 0, 3273.2, 1091.067),
            (15, 30, 3217.8, 1103.197),
            (16, 0, 2936.6, 1381.039),
        ]
        house_power = 844.0
        phases = 1
        current = 7
        is_charging = False
        cumulative_grid_import_kwh = 0.0
        applied_currents = []

        for hour, minute, pv_power, excess_target in buckets:
            action, phases, current, _reason, mode = self.core[
                "get_settled_simulated_ev_charge_action"
            ](
                next_drive=None,
                current_soc=32,
                required_soc=65,
                energy_needed=20,
                excess_target=excess_target,
                surplus_energy=5.48,
                smart_charge_limit=101,
                smart_limiter_active=False,
                configured_phases=phases,
                configured_current=current,
                is_low_price=False,
                pv_total_power=pv_power,
                house_power=house_power,
                battery_soc=40,
                is_charging=is_charging,
                t_now=datetime(2026, 8, 19, hour, minute, tzinfo=timezone.utc),
                inverter_mode="off",
                battery_force_charge=False,
            )
            is_charging = action == self.core["ChargeAction"].on
            applied_current = current if is_charging else 0
            applied_currents.append(applied_current)
            ev_power = phases * applied_current * self.core["Const"].voltage
            grid_import_w = max(0, house_power + ev_power - pv_power)
            cumulative_grid_import_kwh += grid_import_w * 0.5 / 1000
            if is_charging:
                self.assertEqual(mode, self.core["ChargeMode"].surplus)

        self.assertEqual(applied_currents[6:12], [9, 9, 7, 7, 7, 6])
        self.assertEqual(applied_currents[-1], 0)
        self.assertAlmostEqual(cumulative_grid_import_kwh, 0.0)

    def test_evening_negative_target_does_not_transfer_home_battery_to_ev(self):
        buckets = [
            (17, 0, 2049.4, 844.0, -2443.829),
            (17, 30, 1711.2, 844.0, -2269.544),
            (18, 0, 1246.3, 844.0, -1969.247),
            (18, 30, 730.8, 844.0, -1563.411),
            (19, 0, 274.7, 682.0, -1061.869),
            (19, 30, 104.7, 682.0, -540.827),
            (20, 0, 32.2, 682.0, -64.736),
        ]
        phases = 1
        current = 6
        is_charging = False
        battery_energy = 19.299
        battery_floor = 14.053
        cumulative_grid_import_kwh = 0.0
        applied_currents = []

        for hour, minute, pv_power, house_power, excess_target in buckets:
            action, phases, current, _reason, _mode = self.core[
                "get_settled_simulated_ev_charge_action"
            ](
                next_drive=None,
                current_soc=52,
                required_soc=65,
                energy_needed=8,
                excess_target=excess_target,
                surplus_energy=5.48,
                smart_charge_limit=101,
                smart_limiter_active=False,
                configured_phases=phases,
                configured_current=current,
                is_low_price=False,
                pv_total_power=pv_power,
                house_power=house_power,
                battery_soc=battery_energy / 32 * 100,
                is_charging=is_charging,
                t_now=datetime(2026, 8, 19, hour, minute, tzinfo=timezone.utc),
                inverter_mode="off",
                battery_force_charge=False,
            )
            is_charging = action == self.core["ChargeAction"].on
            applied_current = current if is_charging else 0
            applied_currents.append(applied_current)
            ev_power = phases * applied_current * self.core["Const"].voltage
            natural_deficit_w = max(0, house_power + ev_power - pv_power)
            available_discharge_w = max(0, battery_energy - battery_floor) * 1000 / 0.5
            battery_discharge_w = min(natural_deficit_w, available_discharge_w)
            grid_import_w = natural_deficit_w - battery_discharge_w
            battery_energy -= battery_discharge_w * 0.5 / 1000
            cumulative_grid_import_kwh += grid_import_w * 0.5 / 1000

        self.assertEqual(applied_currents, [0, 0, 0, 0, 0, 0, 0])
        # Only the ordinary house deficit may discharge the battery.
        self.assertAlmostEqual(battery_energy, 18.4252)
        self.assertGreater(battery_energy, battery_floor)
        self.assertAlmostEqual(cumulative_grid_import_kwh, 0.0)

    def test_same_weekday_completed_drive_rolls_to_next_week(self):
        project_root = Path(__file__).parents[1]
        t_now = datetime(2026, 8, 18, 21, 0, tzinfo=timezone.utc)
        schedule_data = {
            "tuesday": [
                {
                    "from": time(14, 30),
                    "to": time(18, 30),
                    "data": {"required": 85, "distance": 265.625},
                }
            ]
        }

        for relative_path in ("ev_charging.py", "energy.py"):
            source_path = project_root / relative_path
            tree = ast.parse(source_path.read_text())
            parser = next(
                node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "parse_full_schedule"
            )
            parser.decorator_list = []
            namespace = {
                "Any": Any,
                "datetime": datetime,
                "timedelta": timedelta,
                "with_timezone": lambda value: value.replace(tzinfo=timezone.utc)
                if value.tzinfo is None
                else value,
                "now": lambda: t_now,
                "get_drive_required_soc": self.core["get_drive_required_soc"],
                "EVScheduleEntry": lambda **kwargs: SimpleNamespace(**kwargs),
            }
            module = ast.Module(body=[parser], type_ignores=[])
            exec(compile(ast.fix_missing_locations(module), str(source_path), "exec"), namespace)
            entries = namespace["parse_full_schedule"](schedule_data, 65, t_now=t_now)

            self.assertEqual(entries[0].start, datetime(2026, 8, 25, 14, 30, tzinfo=timezone.utc))
            self.assertEqual(entries[0].end, datetime(2026, 8, 25, 18, 30, tzinfo=timezone.utc))

    def test_provider_forecast_is_extended_through_scheduled_drive_horizon(self):
        source_path = Path(__file__).parents[1] / "energy.py"
        tree = ast.parse(source_path.read_text())
        extender = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "extend_pv_forecast_to_horizon"
        )
        extender.decorator_list = []
        namespace = {
            "datetime": datetime,
            "PVForecastWithPrices": lambda period_start, pv_estimate, price_per_kwh: SimpleNamespace(
                period_start=period_start,
                pv_estimate=pv_estimate,
                price_per_kwh=price_per_kwh,
            )
        }
        module = ast.Module(body=[extender], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(source_path), "exec"), namespace)

        start = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)
        source = [
            namespace["PVForecastWithPrices"](start, 0.4, 0.10),
            namespace["PVForecastWithPrices"](start + timedelta(minutes=30), 0.1, 0.11),
        ]
        t_end = datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc)
        projected = namespace["extend_pv_forecast_to_horizon"](
            source,
            t_end,
            {(23, 1, 0): 0.08},
        )

        self.assertEqual(projected[-1].period_start, datetime(2026, 8, 24, 0, 30, tzinfo=timezone.utc))
        self.assertEqual(projected[2].price_per_kwh, 0.08)
        self.assertEqual(projected[2].pv_estimate, 0)
        self.assertEqual(projected[-1].pv_estimate, 0.1)

    def test_pv_reserve_targets_are_continuous_across_old_thresholds(self):
        source_path = Path(__file__).parents[1] / "energy.py"
        tree = ast.parse(source_path.read_text())
        selected = []
        for name in ("get_low_pv_reserve_soc", "_get_excess_target"):
            node = next(
                node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
            )
            node.decorator_list = []
            selected.append(node)
        namespace = {
            "clip": _clip,
            "pi": pi,
            "with_timezone": lambda value: value,
        }
        module = ast.Module(body=selected, type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(source_path), "exec"), namespace)

        reserve = namespace["get_low_pv_reserve_soc"]
        self.assertEqual(reserve(1000, 740, False), 15)
        self.assertAlmostEqual(reserve(1000, 875, False), 7.5)
        self.assertEqual(reserve(1000, 1000, False), 0)

        t_now = datetime(2026, 8, 19, 10, 30, tzinfo=timezone.utc)
        targets = [
            namespace["_get_excess_target"](
                battery_target_soc=5,
                battery_soc=13,
                ev_required_soc=80,
                ev_is_charging=True,
                next_planned_drive=t_now + timedelta(days=6),
                pv_power=pv_power,
                ev_soc=47,
                t_now=t_now,
                efficient_discharge=True,
            )
            for pv_power in (1999, 2001)
        ]
        self.assertLess(abs(targets[1] - targets[0]), 10)

    def test_live_and_forecast_use_the_shared_schedule_context(self):
        project_root = Path(__file__).parents[1]
        ev_tree = ast.parse((project_root / "ev_charging.py").read_text())
        energy_tree = ast.parse((project_root / "energy.py").read_text())

        live_node = next(
            node for node in ev_tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "auto_ev_charging"
        )
        forecast_node = next(
            node for node in energy_tree.body if isinstance(node, ast.FunctionDef) and node.name == "_forecast"
        )
        forecast_surplus_node = next(
            node for node in energy_tree.body if isinstance(node, ast.FunctionDef) and node.name == "forecast_surplus"
        )
        forecast_wrapper_node = next(
            node for node in energy_tree.body if isinstance(node, ast.FunctionDef) and node.name == "forecast"
        )

        def helper_calls(node):
            return [
                call
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "get_ongoing_and_next_drive"
            ]

        self.assertGreaterEqual(len(helper_calls(live_node)), 1)
        forecast_context_calls = [
            call
            for call in ast.walk(forecast_node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "get_forecast_drive_context"
        ]
        self.assertGreaterEqual(len(forecast_context_calls), 2)
        for node in (forecast_wrapper_node, forecast_node):
            self.assertIn("charger_ready_since", [argument.arg for argument in node.args.args])
            self.assertIn("ev_plugged_in", [argument.arg for argument in node.args.args])
        forecast_delegate = next(
            call
            for call in ast.walk(forecast_wrapper_node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_forecast"
        )
        self.assertIn("charger_ready_since", [keyword.arg for keyword in forecast_delegate.keywords])
        self.assertIn("ev_plugged_in", [keyword.arg for keyword in forecast_delegate.keywords])
        forecast_charge_call = next(
            call
            for call in ast.walk(forecast_node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "get_settled_simulated_ev_charge_action"
        )
        self.assertIn("house_power", [keyword.arg for keyword in forecast_charge_call.keywords])
        self.assertFalse(
            any(isinstance(node, ast.GeneratorExp) for node in ast.walk(forecast_surplus_node)),
            "forecast_surplus must avoid generator expressions unsupported by Pyscript",
        )

        live_parser = next(
            node for node in ev_tree.body if isinstance(node, ast.FunctionDef) and node.name == "parse_full_schedule"
        )
        forecast_parser = next(
            node for node in energy_tree.body if isinstance(node, ast.FunctionDef) and node.name == "parse_full_schedule"
        )
        for parser in (live_parser, forecast_parser):
            self.assertTrue(
                any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "get_drive_required_soc"
                    for node in ast.walk(parser)
                )
            )
            self.assertFalse(
                any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "now"
                    for node in ast.walk(parser)
                ),
                "compiled schedule parsers must receive a frozen t_now from runtime code",
            )

        live_pv_assignment = next(
            node
            for node in ast.walk(live_node)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "pv_total_power" for target in node.targets)
        )
        self.assertIn("PVProduction.total_power", ast.unparse(live_pv_assignment.value))

        forecast_surplus_source = ast.unparse(forecast_surplus_node)
        self.assertIn("next_departure.end + timedelta(hours=6)", forecast_surplus_source)
        self.assertIn("forecast_ev_charging_enabled", forecast_surplus_source)
        self.assertGreaterEqual(
            forecast_surplus_source.count("with_ev_charging=forecast_ev_charging_enabled"),
            2,
        )
        self.assertIn("EVConst.charge_efficiency", ast.unparse(forecast_node))


if __name__ == "__main__":
    unittest.main()
