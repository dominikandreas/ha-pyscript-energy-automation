import ast
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


class _EVConst:
    ev_capacity = 60
    kwh_per_100km = 16
    max_current = 16
    min_current = 6
    min_phases = 1
    voltage = 230


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
        "get_drive_required_soc",
        "get_forecast_drive_context",
        "get_ongoing_and_next_drive",
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


if __name__ == "__main__":
    unittest.main()
