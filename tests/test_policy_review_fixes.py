import ast
import unittest
from datetime import datetime, timedelta
from math import isfinite
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from forecast_harness import ROOT, load_energy


def extracted(filename, function, namespace):
    tree = ast.parse((ROOT / filename).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
    node.decorator_list = []
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), filename, "exec"),
        namespace,
    )
    return namespace[function]


class PolicyReviewFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.energy = load_energy()
        import energy_core
        import export_scheduler

        cls.core = energy_core
        cls.export = export_scheduler

    def test_required_charge_repairs_low_offer_once_per_transaction(self):
        now = datetime(2026, 11, 13, 0, 2, tzinfo=ZoneInfo('Europe/Berlin'))
        charger = SimpleNamespace(force_charge='force', control_switch='switch',
            status_connector='status', transaction_id='tx', current_setting='setting',
            current_offered='offered', session_current_limit='session')
        automation = SimpleNamespace(auto_ev_charging='auto')
        modes = SimpleNamespace(required='required', low_pv_grid='low_pv_grid', surplus='surplus')
        values = {'auto':True, 'force':False, 'switch':True, 'status':'Charging',
                  'tx':'123', 'setting':16, 'offered':6}
        calls = []
        ns = dict(Charger=charger, Automation=automation, ChargeMode=modes,
            Const=SimpleNamespace(min_current=6,max_current=16), now=lambda:now,
            get=lambda entity,default=None,mapper=None: values.get(entity,default),
            get_attr=lambda *args:now-timedelta(seconds=90),
            service=SimpleNamespace(call=lambda *a,**kw:calls.append((a,kw))),
            log=SimpleNamespace(warning=lambda *a:None),
            last_ev_mode='low_pv_grid', last_offer_reconcile_transaction=None)
        reconcile = extracted('ev_charging.py', 'reconcile_required_charge_offer', ns)
        reconcile()
        self.assertEqual(calls, [(('number','set_value'), {'entity_id':'session','value':16})])
        reconcile()
        self.assertEqual(len(calls), 1)
        values['tx'] = '124'
        ns['last_ev_mode'] = 'surplus'
        reconcile()
        self.assertEqual(len(calls), 1)
        ns['last_ev_mode'] = 'required'
        values['offered'] = 16
        reconcile()
        self.assertEqual(len(calls), 1)

    def test_low_pv_session_survives_midnight_and_stale_data_pauses_authorization(self):
        tz = ZoneInfo("Europe/Berlin")
        before = datetime(2026, 11, 4, 23, 59, tzinfo=tz)
        after = before + timedelta(minutes=1)
        departure = datetime(2026, 11, 5, 7, 0, tzinfo=tz)
        target = self.core.get_low_pv_ev_charge_target

        admitted = target(30, -10, departure, before, 0, before)
        retained = target(30, -10, departure, after, admitted, after, departure)
        self.assertEqual((admitted, retained), (100, 100))

        stale_target = target(50, -10, None, after, 80, before - timedelta(minutes=30))
        self.assertEqual(stale_target, 80)
        self.assertFalse(
            self.core.is_low_pv_ev_grid_charge_authorized(
                stale_target, 50, -10, after, before - timedelta(minutes=30)
            )
        )

    def test_fresh_pv_opportunity_blocks_unnecessary_grid_fill(self):
        now = datetime(2026, 11, 4, 22, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        target = self.core.get_low_pv_ev_charge_target
        self.assertEqual(target(18, -40, None, now, 0, now, None, 40, now), 0)
        self.assertEqual(target(18, -40, None, now, 0, now, None, 10, now), 80)

    def test_required_full_charge_has_departure_taper_margin(self):
        now = datetime(2026, 11, 13, 9, 0, tzinfo=ZoneInfo('Europe/Berlin'))
        common = dict(next_drive=now + timedelta(hours=3, minutes=30),
            current_soc=59, required_soc=100, energy_needed=24.6,
            excess_power=0, excess_target=0, surplus_energy=0,
            smart_charge_limit=100, smart_limiter_active=True,
            configured_phases=3, configured_current=16, is_low_price=False,
            pv_total_power=0, battery_soc=30, is_charging=False, t_now=now,
            planned_power=0, planned_target_soc=0, live_pv_headroom_power=0)
        urgent = self.core._get_charge_action(**dict(common, next_drive=now+timedelta(hours=3,minutes=25)))
        self.assertEqual((urgent[0], urgent[1], urgent[2], urgent[4]),
                         ('on', 3, 16, self.core.ChargeMode.required))
        optional = self.core._get_charge_action(**dict(common, required_soc=50, energy_needed=0,
            low_pv_charge_target_soc=100, low_pv_grid_charge_allowed=True))
        self.assertEqual(optional[0], 'off')

    def test_trip_fallback_scales_current_to_remaining_time(self):
        now = datetime(2026, 11, 13, 10, 47, tzinfo=ZoneInfo('Europe/Berlin'))
        args = dict(next_drive=now + timedelta(hours=1, minutes=43),
            current_soc=69, required_soc=80.83, energy_needed=7.1,
            excess_power=0, excess_target=0, surplus_energy=0,
            smart_charge_limit=100, smart_limiter_active=True,
            configured_phases=3, configured_current=16, is_low_price=False,
            pv_total_power=0, battery_soc=10, is_charging=True, t_now=now,
            planned_power=0, planned_target_soc=0, live_pv_headroom_power=0)
        action = self.core._get_charge_action(**args)
        self.assertEqual((action[0], action[1], action[2], action[4]),
                         ('on', 3, 8, self.core.ChargeMode.required))

    def test_running_surplus_never_requests_below_minimum_current(self):
        now = datetime(2026, 11, 13, 10, 50, tzinfo=ZoneInfo('Europe/Berlin'))
        args = dict(next_drive=now + timedelta(hours=3), current_soc=70,
            required_soc=66.67, energy_needed=0, excess_power=520, excess_target=0,
            surplus_energy=1, smart_charge_limit=100, smart_limiter_active=True,
            configured_phases=1, configured_current=16, is_low_price=False,
            pv_total_power=5000, battery_soc=10, is_charging=True, t_now=now,
            wallbox_power=1000, planned_power=0, planned_target_soc=0,
            live_pv_headroom_power=1520)
        action = self.core._get_charge_action(**args)
        self.assertEqual((action[0], action[1], action[2], action[4]),
                         ('on', 1, 6, self.core.ChargeMode.surplus))

    def test_admitted_low_pv_session_finishes_below_admission_threshold(self):
        """Regression for the 2026-11-05 charge stopping at 76% of an 80% target."""
        now = datetime(2026, 11, 5, 3, 35, tzinfo=ZoneInfo("Europe/Berlin"))
        authorize = self.core.is_low_pv_ev_grid_charge_authorized

        # The remaining 2.67 kWh wall energy is below the 3 kWh threshold used
        # to admit a new session, but an already admitted session must finish.
        self.assertFalse(authorize(80, 76, -40, now, now, 0, now))
        self.assertTrue(authorize(80, 76, -40, now, now, 0, now, active_session=True))

        # A fresh forecast that can cover the remainder still pauses grid use.
        self.assertFalse(authorize(80, 76, -40, now, now, 3, now, active_session=True))

    def test_pv_surplus_start_keeps_confirmation_margin_in_reserve(self):
        now = datetime(2026, 11, 5, 15, 8, tzinfo=ZoneInfo("Europe/Berlin"))
        common = dict(
            next_drive=now + timedelta(days=2), current_soc=74, required_soc=38,
            energy_needed=0, excess_power=1930, excess_target=-2500,
            surplus_energy=6, smart_charge_limit=85, smart_limiter_active=True,
            configured_phases=1, configured_current=8, is_low_price=False,
            pv_total_power=2800, battery_soc=82, is_charging=False, t_now=now,
            planned_power=0, planned_target_soc=0, low_pv_charge_target_soc=80,
            live_pv_headroom_power=1930,
        )
        action = self.core._get_charge_action(**common)
        self.assertEqual(
            (action[0], action[1], action[2], action[4]),
            ("on", 1, 7, self.core.ChargeMode.surplus),
        )

        # Once charging is established, feedback may use the full proven
        # headroom and adjust upward normally.
        running = self.core._get_charge_action(**dict(common, is_charging=True))
        self.assertEqual((running[1], running[2]), (1, 7))

        # November 6 incident: 3.137 kW headroom at 11 A was rounded up to
        # 13 A, consumed the safety margin and stopped the whole session.
        ramp = self.core._get_charge_action(**dict(
            common, is_charging=True, configured_current=11,
            excess_power=3137, live_pv_headroom_power=3137,
        ))
        self.assertEqual((ramp[1], ramp[2]), (1, 12))

    def test_surplus_import_guard_steps_down_before_stopping(self):
        action = self.core.get_ev_surplus_import_safety_action

        # The recorded 15:01 incident peaked below 1 kW while the battery
        # controller yielded, then recovered to export within five seconds.
        self.assertEqual(action(903, 8, 13, 1), ("hold", None))
        self.assertEqual(action(250, 120, 13, 1), ("none", None))
        self.assertEqual(action(350, 31, 13, 1), ("reduce", 12))

        # November 6 incident: reduce the 13 A load to its floor and give the
        # battery controller time to yield instead of ending the transaction.
        self.assertEqual(action(2483, 95, 13, 1), ("reduce", 6))
        self.assertEqual(action(900, 10, 6, 1), ("hold", None))
        self.assertEqual(action(900, 31, 6, 1), ("stop", None))

        # Truly large imports retain an immediate fail-safe stop.
        self.assertEqual(action(5100, 8, 13, 1), ("stop", None))
        self.assertEqual(action(350, None, 13, 1), ("hold", None))

    def test_no_schedule_replay_finds_pv_and_optional_grid_charge_stops_at_target(self):
        import ev_plan

        from forecast_harness import incident_inputs

        now = datetime(2026, 11, 4, 22, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        inputs = incident_inputs(self.energy)
        inputs.update(
            t_now=now, battery_energy=6, ev_energy=10.8, ev_soc=18,
            ev_required_soc=40, ev_schedule=[], ev_charge_plan={},
            battery_floor_schedule={}, daily_power=1000, nightly_power=1000,
            surplus_energy=0, ev_battery_support_energy_kwh=0, charge_limit=0,
            ev_low_pv_charge_target_soc=0, ev_low_pv_grid_charge_allowed=False,
        )
        inputs["forecast"] = [
            self.energy.PVForecastWithPrices(
                now + timedelta(minutes=30*i),
                12 if (
                    (now + timedelta(minutes=30*i)).date() == (now + timedelta(days=1)).date()
                    and 9 <= (now + timedelta(minutes=30*i)).hour < 17
                ) else 0,
                0.1,
            )
            for i in range(96)
        ]
        plan = ev_plan.build_ev_charge_plan(self.energy._forecast, inputs)
        self.assertGreater(plan["planned_pv_ev_wall_kwh"], 15)

        dawn = (now + timedelta(days=1)).replace(hour=8)
        paused = self.energy._forecast(**dict(
            inputs, ev_charge_plan=plan["slots"], ev_low_pv_charge_target_soc=80,
            ev_low_pv_grid_charge_allowed=False,
        ))
        charged = self.energy._forecast(**dict(
            inputs, ev_charge_plan=plan["slots"], ev_low_pv_charge_target_soc=80,
            ev_low_pv_grid_charge_allowed=True,
        ))
        paused_night = sum(
            e.ev_charge_power * .5 / 1000 for e in paused.detail if e.period_start < dawn
        )
        charged_night = sum(
            e.ev_charge_power * .5 / 1000 for e in charged.detail if e.period_start < dawn
        )
        self.assertEqual(paused_night, 0)
        self.assertAlmostEqual(charged_night, (80-18)*.6/.9, places=2)

    def test_optional_target_never_becomes_high_price_trip_emergency(self):
        now = datetime(2026, 11, 4, 18, 0)
        result = self.core._get_charge_action(
            next_drive=now + timedelta(hours=1), current_soc=50, required_soc=40,
            energy_needed=0, excess_power=0, excess_target=0, surplus_energy=-10,
            smart_charge_limit=80, smart_limiter_active=True, configured_phases=1,
            configured_current=6, is_low_price=False, pv_total_power=0,
            battery_soc=50, t_now=now, planned_power=0, planned_target_soc=0,
            low_pv_charge_target_soc=80, low_pv_grid_charge_allowed=True,
        )
        self.assertEqual((result[0], result[4]), ("off", self.core.ChargeMode.hard_stop))

    def test_low_pv_grid_mode_has_its_own_energy_ceiling(self):
        self.assertEqual(
            self.core.get_ev_charge_energy_limit(self.core.ChargeMode.low_pv_grid, 40, 80),
            48,
        )

    def test_battery_energy_can_be_confirmed_by_fresh_consistent_soc_while_moving(self):
        now = datetime(2026, 11, 4, 23, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        args = dict(
            current_time=now, energy_updated_at=now - timedelta(minutes=6),
            battery_energy_kwh=6.05, battery_capacity_kwh=32,
            battery_soc_percent=18.9, battery_power_w=-330,
            battery_power_updated_at=now, max_age_seconds=300,
        )
        self.assertTrue(
            self.export.is_battery_energy_measurement_usable(
                **args, battery_soc_updated_at=now - timedelta(seconds=20)
            )
        )
        self.assertTrue(
            self.export.is_battery_energy_measurement_usable(
                **args, battery_soc_updated_at=now - timedelta(minutes=6)
            )
        )
        self.assertFalse(
            self.export.is_battery_energy_measurement_usable(
                **dict(args, battery_power_w=-1500),
                battery_soc_updated_at=now - timedelta(minutes=6),
            )
        )

    def test_pv_integration_stops_at_first_crossing_and_missing_data_is_zero(self):
        now = datetime(2026, 11, 4, 8, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        namespace = {"now": lambda: now, "datetime": datetime}
        namespace["interpolate_value"] = extracted("pv_prediction.py", "interpolate_value", {})
        integrate = extracted("pv_prediction.py", "find_time_and_energy_to_reach_target", namespace)
        first = [
            {"period_start": now + timedelta(hours=i), "pv_estimate": value}
            for i, value in enumerate((0, 0, 1))
        ]
        later = [
            {"period_start": now + timedelta(days=1, hours=i), "pv_estimate": 2}
            for i in range(2)
        ]
        one = integrate(0.5, [first])
        two = integrate(0.5, [first, later])
        self.assertEqual(one, two)
        self.assertEqual(integrate(0.5, []), (None, 0.0))

    def test_idle_inverter_efficiency_and_explicit_zero_average_are_safe(self):
        efficiency = extracted(
            "victron.py", "set_victron_efficiency",
            {"get": lambda *args, **kwargs: 0, "isfinite": isfinite},
        )
        self.assertIsNone(efficiency())

        writes = []
        average = extracted(
            "victron.py", "update_moving_average_power",
            {"get": lambda *args, **kwargs: 99, "moving_averages": {},
             "set_state": lambda *args, **kwargs: writes.append(args)},
        )
        average("sensor.test", factor=1, new_val=0)
        self.assertEqual(writes[-1][1], 0)

    def test_ev_stop_symbol_is_imported_and_no_schedule_policy_is_forecast(self):
        ev_source = (ROOT / "ev_charging.py").read_text()
        tree = ast.parse(ev_source)
        imported = {
            alias.asname or alias.name
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        self.assertIn("ChargeAction", imported)
        energy_source = (ROOT / "energy.py").read_text()
        self.assertIn("if forecast_ev_charging_enabled or ev_schedule:", energy_source)
        self.assertIn("ev_low_pv_charge_target_soc=0", energy_source)


if __name__ == "__main__":
    unittest.main()
