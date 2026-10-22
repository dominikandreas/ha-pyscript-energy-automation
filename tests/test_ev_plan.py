import ast
import unittest
from datetime import timedelta
from types import SimpleNamespace
from forecast_harness import load_energy, incident_inputs, ROOT

class EVPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.energy = load_energy()
        import ev_plan
        cls.plan = ev_plan
        cls.inputs = incident_inputs(cls.energy)

    def test_weak_weekend_preserves_house_and_completes_wednesday_trip(self):
        plan = self.plan.build_ev_charge_plan(self.energy._forecast, self.inputs)
        result = self.energy._forecast(**dict(self.inputs, ev_charge_plan=plan["slots"]))
        before_monday = [e for e in result.detail if e.period_start.date().isoformat() < "2026-10-19"]
        self.assertEqual(sum(e.ev_charge_power for e in before_monday), 0)
        self.assertAlmostEqual(plan["planned_ev_wall_kwh"], 3.0, places=3)
        # 2026-10-21: required night EV charging now comes from the grid
        # instead of the house battery (inverter no longer forced on at low
        # prices without post-obligation surplus), so the plan imports ~0.06
        # kWh more while the EV outcome below is unchanged.
        # 2026-10-22: the simulator no longer hard-pins the battery at the
        # reserve schedule (live control discharges through it at high
        # prices), so the plan self-consumes ~0.86 kWh more instead of
        # importing it.
        self.assertAlmostEqual(plan["planned_grid_kwh"], 7.2495, places=3)
        departure = self.inputs["ev_schedule"][0].start
        final_ev = [e.ev_energy for e in result.detail if e.period_start < departure][-1]
        self.assertAlmostEqual(final_ev / 60 * 100, 69.5, places=3)
        self.assertLess(plan["replay_count"], 2 * len(self.inputs["forecast"]) + 2)

    def test_optional_solar_spill_is_still_used_without_extra_import(self):
        inputs = dict(self.inputs)
        start = inputs["t_now"].replace(hour=10, minute=0, second=0, microsecond=0)
        inputs.update(t_now=start, battery_energy=31, ev_energy=42, ev_soc=70,
                      ev_required_soc=70, ev_schedule=[], ev_charge_plan={}, battery_floor_schedule={})
        inputs["forecast"] = [self.energy.PVForecastWithPrices(start+timedelta(minutes=30*i), 10 if i<12 else 0, .1) for i in range(48)]
        baseline = self.energy._forecast(**inputs)
        plan = self.plan.build_ev_charge_plan(self.energy._forecast, inputs)
        result = self.energy._forecast(**dict(inputs, ev_charge_plan=plan["slots"]))
        before = self.plan.ev_replay_metrics(baseline,start,.5)
        after = self.plan.ev_replay_metrics(result,start,.5)
        self.assertGreater(after[2], 1)
        self.assertLessEqual(after[0], before[0]+.005)
        self.assertGreaterEqual(after[3], before[3]-.005)

    def test_empty_plan_still_meets_imminent_departure_without_pv(self):
        inputs = dict(self.inputs)
        now=inputs["t_now"]
        inputs.update(ev_energy=30,ev_soc=50,ev_required_soc=70,battery_energy=1,ev_charge_plan={},battery_floor_schedule={})
        inputs["ev_schedule"]=[self.energy.EVScheduleEntry(now+timedelta(hours=2),now+timedelta(hours=4),0,70)]
        inputs["forecast"]=[self.energy.PVForecastWithPrices(now+timedelta(minutes=30*i),0,.1) for i in range(10)]
        result=self.energy._forecast(**inputs)
        self.assertGreater(sum(e.ev_charge_power for e in result.detail[:4]),0)
        self.assertGreaterEqual(result.detail[3].ev_energy,42-.001)

    def test_plan_freshness_schedule_and_battery_drop_fail_closed(self):
        now=self.inputs["t_now"]
        attrs=dict(version=1,calculated_at=now.isoformat(),schedule_signature="trip",source_ev_soc=65,source_battery_kwh=17.63,slots={"test":{}})
        valid=self.plan.valid_ev_plan_slots
        self.assertTrue(valid(attrs,now,"trip",65,17.63))
        for when, signature, soc, battery in [(now+timedelta(seconds=301),"trip",65,17.63),(now,"changed",65,17.63),(now,"trip",60,17.63),(now,"trip",65,15),(now,"trip",65,float("nan")),(now-timedelta(seconds=1),"trip",65,17.63)]:
            self.assertEqual(valid(attrs,when,signature,soc,battery),{})

    def test_slot_expiry_and_malformed_slots(self):
        now=self.inputs["t_now"]
        slots={now.isoformat():dict(end=(now+timedelta(minutes=30)).isoformat(),power_w=2300,target_soc=70)}
        self.assertEqual(self.plan.ev_plan_slot(slots,now),(2300,70))
        self.assertEqual(self.plan.ev_plan_slot(slots,now+timedelta(minutes=30)),(0,0))
        self.assertEqual(self.plan.ev_plan_slot({"bad":None},now),(0,0))

    def test_live_plan_never_borrows_battery_or_rounds_above_pv(self):
        import energy_core
        args=dict(next_drive=self.inputs["ev_schedule"][0].start,current_soc=65,required_soc=69.5,energy_needed=2.7,excess_power=500,excess_target=-2500,surplus_energy=20,smart_charge_limit=80,smart_limiter_active=True,configured_phases=1,configured_current=6,is_low_price=False,pv_total_power=2000,battery_soc=80,is_charging=False,t_now=self.inputs["t_now"],battery_support_power=2500,planned_power=3000,planned_target_soc=69.5)
        result=energy_core._get_charge_action(**args)
        self.assertEqual(result[0],"off")
        self.assertEqual(result[4],energy_core.ChargeMode.hard_stop)
        for power in [1700,3000,4000,4140,4500,7000]:
            result=energy_core._get_charge_action(**dict(args,excess_power=power,planned_power=power))
            self.assertEqual(result[0],"on")
            self.assertLessEqual(result[1]*result[2]*230,power)
        self.assertEqual(energy_core._get_charge_action(**dict(args,current_soc=70,energy_needed=0,excess_power=4000))[0],"off")
        self.assertEqual(energy_core._get_charge_action(**dict(args,planned_power=0,excess_power=4000))[0],"off")
        self.assertEqual(energy_core._get_charge_action(**dict(args,planned_power=0,next_drive=args["t_now"]+timedelta(hours=10),is_low_price=True))[4],energy_core.ChargeMode.required)

    def test_phase_cooldown_stops_when_existing_phases_cannot_fit_power(self):
        tree=ast.parse((ROOT/"ev_charging.py").read_text())
        func=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="set_phases_and_current")
        func.decorator_list=[]
        now=self.inputs["t_now"]
        from states import Charger
        import const
        writes=[]
        namespace=dict(task=SimpleNamespace(unique=lambda *a, **kw:None),now=lambda:now,last_ev_charging_phase_change=now,Charger=Charger,Const=const.EV,timedelta=timedelta,log=SimpleNamespace(warning=lambda *a:None),set_current=lambda *a:writes.append(("current",a)),turn_off_charger=lambda *a,**kw:writes.append(("off",a)),get=lambda e,d=None:{Charger.control_switch:True,Charger.phases:3,Charger.current_setting:10}.get(e,d))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[func],type_ignores=[])),"ev_charging.py","exec"),namespace)
        self.assertFalse(namespace["set_phases_and_current"](1,6,"test"))
        self.assertEqual(writes[0][0],"off")

class ForecastFloorTests(unittest.TestCase):
    # 2026-10-22 incident: the backward-accumulated reserve schedule (24 kWh
    # against dark-day deficits) hard-pinned the simulated battery at
    # 9.26 kWh for 24 h while the house imported at peak rates.  Live control
    # discharges through the schedule at high prices and only honors it via
    # the inverter mode at low prices; the simulator must do the same.
    @classmethod
    def setUpClass(cls):
        cls.energy = load_energy()

    def _run(self, start, schedule_floor):
        periods = [start + timedelta(minutes=30 * i) for i in range(4)]
        forecast = [self.energy.PVForecastWithPrices(p, 0.0, 0.2875) for p in periods]
        return self.energy._forecast(
            forecast=forecast,
            setpoint=-20,
            battery_capacity=32,
            battery_energy=9.26,
            battery_min_energy=0.0,
            battery_floor_schedule={p.isoformat(): schedule_floor for p in periods},
            with_ev_charging=False,
            ev_schedule=[],
            ev_required_soc=70.0,
            ev_soc=65.0,
            surplus_energy=4.49,
            battery_export_budget_kwh=0.0,
            optional_export_power_schedule={},
            max_setpoint=-20,
            daily_power=920.0,
            nightly_power=546.0,
            is_charging_ev=False,
            smart_limiter_active=False,
            charge_limit=90.0,
            max_charge_price=0.2,
            force_charge_switch=False,
            min_discharge_price=0.0,
            battery_cells_balanced=True,
            eff_dis=False,
            t_now=start,
        )

    def test_sim_discharges_through_schedule_floor_at_high_price(self):
        from datetime import datetime
        result = self._run(datetime(2026, 10, 22, 18, 0), 24.0)
        final_battery = result.detail[-1].battery_energy
        grid_kwh = sum(e.power_from_grid for e in result.detail) * 0.5 / 1000
        self.assertLess(final_battery, 8.5)
        self.assertLess(grid_kwh, 0.1)

    def test_sim_holds_schedule_floor_at_low_price(self):
        from datetime import datetime
        result = self._run(datetime(2026, 10, 22, 1, 0), 24.0)
        final_battery = result.detail[-1].battery_energy
        grid_kwh = sum(e.power_from_grid for e in result.detail) * 0.5 / 1000
        self.assertAlmostEqual(final_battery, 9.26, places=2)
        self.assertGreater(grid_kwh, 0.5)

    def test_sim_ev_gating_uses_protected_floor_not_schedule(self):
        tree = ast.parse((ROOT / "energy.py").read_text())
        forecast_node = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_forecast"
        )
        calls = [
            c for c in ast.walk(forecast_node)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name)
            and c.func.id == "get_settled_simulated_ev_charge_action"
        ]
        self.assertEqual(len(calls), 1)
        floor_kw = next(
            k for k in calls[0].keywords if k.arg == "battery_floor_soc"
        )
        self.assertIn("period_battery_hard_floor", ast.unparse(floor_kw.value))
