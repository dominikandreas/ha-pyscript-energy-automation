import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from forecast_harness import load_energy, incident_inputs

class NightChargingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.energy=load_energy()
        from victron import get_auto_inverter_mode
        cls.decide=staticmethod(get_auto_inverter_mode)

    def inputs(self):
        return dict(ev_is_charging=False,surplus_energy=0,battery_headroom_energy=0,
            pv_power=0,daily_avg_power=899,battery_soc=15,target_soc=8,
            electricity_price=.1775,min_discharge_price=.212,max_charge_price=.2,
            charge_limit_percent=45,force_charge_switch=False,
            t_now=datetime(2026,10,28,1),enforce_battery_floor=True,is_low_price=True)

    def test_charge_target_is_not_blocked_by_smaller_house_reserve(self):
        mode,power,force,reason=self.decide(**self.inputs())
        self.assertEqual((mode,power,force),('3',3000,True))
        self.assertIn('45.0%',reason)

    def test_stop_at_target_or_expensive_price_and_zero_disables(self):
        for changes in [dict(battery_soc=45),dict(electricity_price=.2875),dict(charge_limit_percent=0)]:
            with self.subTest(changes=changes):
                r=self.decide(**(self.inputs()|dict(force_charge_switch=True)|changes))
                self.assertFalse(r[2])
                self.assertEqual(r[1],-1)
        self.assertTrue(self.decide(**(self.inputs()|dict(battery_soc=44.8)))[2])
        self.assertTrue(self.decide(**(self.inputs()|dict(battery_soc=44.8,force_charge_switch=True)))[2])

    def forecast(self,pv=0):
        start=datetime(2026,10,28,0,tzinfo=ZoneInfo('Europe/Berlin'))
        return [self.energy.PVForecastWithPrices(start+timedelta(minutes=30*i),
            pv if 10 <= (start+timedelta(minutes=30*i)).hour < 15 else 0,.1) for i in range(96)]

    def plan(self,pv=0,now=None,capacity=32):
        f=self.forecast(pv)
        return self.energy.calculate_night_charge_plan(f,now or f[0].period_start,
            capacity,1.6,900,540,.2)

    def test_dark_day_covers_expensive_hours_through_next_midnight(self):
        p=self.plan()
        # 11 daytime hours, 8 other expensive hours, protected reserve.
        self.assertAlmostEqual(p['required_kwh'],1.6+(11*.9+8*.54)/.9,places=3)
        self.assertEqual(p['charge_end'],'2026-10-28T05:00:00+01:00')
        self.assertEqual(p['coverage_end'],'2026-10-29T00:00:00+01:00')

    def test_target_stable_across_night_and_credits_only_timely_solar(self):
        first=self.plan(pv=10)
        later=self.plan(pv=10,now=self.forecast()[0].period_start+timedelta(hours=3,minutes=17))
        self.assertEqual(first,later)
        self.assertLess(first['target_soc'],self.plan()['target_soc'])
        # Even unlimited noon PV cannot fund the morning before it arrives.
        self.assertGreater(first['required_kwh'],1.6+3*.54/.9)

    def test_capacity_and_incomplete_forecast_are_explicit(self):
        p=self.plan(capacity=5)
        self.assertEqual(p['target_soc'],100)
        self.assertGreater(p['capacity_shortfall_kwh'],0)
        f=self.forecast()[:8]
        p=self.energy.calculate_night_charge_plan(f,f[0].period_start,32,1.6,900,540,.2)
        self.assertEqual(p['status'],'no_complete_charge_window')
        self.assertEqual(p['target_soc'],0)

    def test_headroom_diagnostic_detects_pv_conflict_without_lowering_reserve(self):
        dark = self.plan(pv=0)
        f = self.forecast()
        for entry in f:
            if 9 <= entry.period_start.hour < 17:
                entry.pv_estimate = 10
        sunny = self.energy.calculate_night_charge_plan(
            f, f[0].period_start, 32, 1.6, 900, 540, .2)
        self.assertFalse(dark['headroom_conflict'])
        self.assertTrue(sunny['headroom_conflict'])
        self.assertGreater(sunny['high_pv_peak_kwh'], 32 * .98)
        self.assertGreater(sunny['required_kwh'], sunny['headroom_ceiling_kwh'])

    def test_lower_solar_forecast_increases_reserve(self):
        f = self.forecast(pv=2)
        baseline = self.energy.calculate_night_charge_plan(
            f, f[0].period_start, 32, 1.6, 900, 540, .2)
        for entry in f:
            entry.pv_estimate10 = entry.pv_estimate * .25
        conservative = self.energy.calculate_night_charge_plan(
            f, f[0].period_start, 32, 1.6, 900, 540, .2)
        self.assertGreater(conservative['target_soc'], baseline['target_soc'])

    def test_headroom_tradeoff_is_bounded_and_preserves_morning_reserve(self):
        f = self.forecast()
        for entry in f:
            if 9 <= entry.period_start.hour < 16:
                entry.pv_estimate, entry.pv_estimate10 = 5, 1
        p = self.energy.calculate_night_charge_plan(f, f[0].period_start, 32, 1.6, 900, 540, .2)
        self.assertTrue(p['headroom_conflict'])
        self.assertLess(p['selected_kwh'], p['required_kwh'])
        self.assertGreaterEqual(p['selected_kwh'], p['morning_reserve_kwh'])
        self.assertLessEqual(p['required_kwh'] - p['selected_kwh'], 1.601)
        self.assertLess(p['selected_high_pv_peak_kwh'], p['high_pv_peak_kwh'])

    def test_automatic_charge_authorization_expires_and_matches_target(self):
        now = datetime(2026, 11, 10, 4, tzinfo=ZoneInfo('Europe/Berlin'))
        end = now + timedelta(hours=1)
        valid = self.energy.is_night_charge_authorized
        self.assertTrue(valid(100, 100, end, now))
        self.assertFalse(valid(100, 100, end, end))
        self.assertFalse(valid(100, 30, end, now))
        self.assertFalse(valid(100, None, end, now))

    def test_invalid_forecast_rejected(self):
        f=self.forecast(); del f[4]
        with self.assertRaises(ValueError):
            self.energy.calculate_night_charge_plan(f,f[0].period_start,32,1.6,900,540,.2)

    def test_simulator_charges_to_exact_target_and_never_above_it(self):
        args=incident_inputs(self.energy)
        start=datetime(2026,10,28,0,tzinfo=ZoneInfo('Europe/Berlin'))
        f=[self.energy.PVForecastWithPrices(start+timedelta(minutes=30*i),0,.1775) for i in range(10)]
        args.update(forecast=f,t_now=start,battery_energy=3,battery_min_energy=0,
            battery_floor_schedule={e.period_start.isoformat():8 for e in f},
            with_ev_charging=False,ev_schedule=[],charge_limit=40,max_charge_price=.2,
            force_charge_switch=False,surplus_energy=0)
        result=self.energy._forecast(**args)
        self.assertGreater(result.detail[0].battery_power,0)
        self.assertLessEqual(max(e.battery_energy for e in result.detail),12.8+.001)
        self.assertAlmostEqual(max(e.battery_energy for e in result.detail),12.8,places=2)

    def test_idle_energy_guard_does_not_skip_night_target_publication(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        E=self.energy
        f=self.forecast()
        values={E.Battery.capacity:32,E.Battery.energy:22,E.Battery.cells_balanced:True,
                E.Automation.auto_force_charge_target:True,E.Battery.max_charge_price:.2,
                E.House.daily_average_power:900,E.House.nightly_average_power:540,
                E.Battery.charge_limit:-1,E.EV.energy:20}
        publications=[];calls=[]; planned_caps=[]
        actual_plan = E.calculate_night_charge_plan
        def capture_plan(*args, **kwargs):
            planned_caps.append(kwargs['pv_charge_limit_w'])
            return actual_plan(*args, **kwargs)
        overrides=dict(now=lambda:f[0].period_start,
            task=SimpleNamespace(unique=lambda *a,**kw:None),
            log=SimpleNamespace(warning=lambda *a,**kw:None),
            get=lambda entity,default=None:values.get(entity,default),
            get_attr=lambda entity,name=None,default=None:default,
            get_reserve_soc=lambda:0,get_ev_schedule=lambda *a,**kw:[],
            get_pv_forecast_with_prices=lambda **kw:f,
            calculate_night_charge_plan=capture_plan,
            is_battery_energy_measurement_usable=lambda *a,**kw:False,
            state=SimpleNamespace(set=lambda *a,**kw:None),
            service=SimpleNamespace(call=lambda *a,**kw:calls.append((a,kw))),
            set_state=lambda *a,**kw:publications.append((a,kw)))
        with patch.dict(E.__dict__,overrides):E.forecast_surplus()
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0],('input_number','set_value'))
        self.assertEqual(calls[0][1]['value'],54.4)
        self.assertEqual(publications[0][0][0],'sensor.battery_night_charge_plan')
        values[E.Battery.charge_limit] = 3000
        values[E.Battery.force_charge_switch] = True
        with patch.dict(E.__dict__,overrides):E.forecast_surplus()
        self.assertEqual(planned_caps[-1], 6600)

    def test_weekly_balance_latches_until_completed_and_only_in_cheap_window(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        E = self.energy
        f = self.forecast()
        shift = datetime(2026, 11, 9, tzinfo=ZoneInfo('Europe/Berlin')) - f[0].period_start
        for entry in f:
            entry.period_start += shift
        t_now = f[0].period_start + timedelta(hours=1)
        values = {E.Battery.capacity:32, E.Battery.energy:4, E.Battery.cells_balanced:False,
                  E.Automation.auto_force_charge_target:True, E.Battery.max_charge_price:.2,
                  E.ElectricityPrices.current_price:.1775, E.House.daily_average_power:900,
                  E.House.nightly_average_power:540, E.Battery.charge_limit:-1,
                  'pyscript.battery_last_full':(t_now-timedelta(days=8)).isoformat()}
        attrs = {}; calls = []; publications = []
        def persist(entity, value, **kw):
            values[entity] = value
            attrs[entity] = kw
        def get_attr(entity, name=None, default=None):
            return attrs.get(entity, {}).get(name, default)
        overrides = dict(now=lambda:t_now, task=SimpleNamespace(unique=lambda *a,**kw:None),
            log=SimpleNamespace(warning=lambda *a,**kw:None), state=SimpleNamespace(set=persist),
            get=lambda entity,default=None:values.get(entity,default), get_attr=get_attr,
            get_reserve_soc=lambda:0, get_ev_schedule=lambda *a,**kw:[],
            get_pv_forecast_with_prices=lambda **kw:f,
            is_battery_energy_measurement_usable=lambda *a,**kw:False,
            service=SimpleNamespace(call=lambda *a,**kw:calls.append((a,kw))),
            set_state=lambda *a,**kw:publications.append((a,kw)))
        with patch.dict(E.__dict__, overrides):
            E.forecast_surplus()
            self.assertEqual(calls[-1][1]['value'], 100)
            self.assertTrue(publications[-1][1]['balance_active'])
            self.assertEqual(attrs['pyscript.battery_balance_session']['deadline'],
                             '2026-11-09T05:00:00+01:00')
            E.forecast_surplus()
            self.assertTrue(publications[-1][1]['balance_active'])
            previous_auth = values['pyscript.night_charge_authorization']
            previous_deadline = attrs['pyscript.night_charge_authorization']['valid_until']
            previous_calls = len(calls)
            E.get_pv_forecast_with_prices = lambda **kw:f[:8]
            E.forecast_surplus()
            self.assertEqual(len(calls), previous_calls)
            self.assertEqual(values['pyscript.night_charge_authorization'], previous_auth)
            self.assertEqual(attrs['pyscript.night_charge_authorization']['valid_until'], previous_deadline)
            E.get_pv_forecast_with_prices = lambda **kw:f
            values['pyscript.battery_balance_completed_at'] = (t_now + timedelta(minutes=30)).isoformat()
            E.forecast_surplus()
            self.assertFalse(publications[-1][1]['balance_active'])
            self.assertLess(calls[-1][1]['value'], 100)


    def test_morning_discharge_uses_capacity_not_held_actuator_zero(self):
        E=self.energy
        from unittest.mock import patch
        with patch.dict(E.__dict__,get=lambda entity,default=None: {
                "input_number.battery_discharge_power_cap":-1,
                E.Battery.discharge_limit:0,
                "pyscript.telemetry_hold_discharge_limit":"0"}.get(entity,default)):
            cap=E.get_planned_discharge_limit_w()
        self.assertEqual(cap,-1)
        args=incident_inputs(E)
        start=datetime(2026,10,29,5,tzinfo=ZoneInfo('Europe/Berlin'))
        f=[E.PVForecastWithPrices(start+timedelta(minutes=30*i),0,.2875) for i in range(6)]
        args.update(forecast=f,t_now=start,battery_energy=22.14,battery_min_energy=1.6,
                    battery_floor_schedule={},with_ev_charging=False,ev_schedule=[],
                    charge_limit=10.7,max_charge_price=.2,force_charge_switch=False,
                    surplus_energy=0,max_battery_power_target=4000)
        result=E._forecast(**args)
        self.assertTrue(all(e.battery_power < 0 for e in result.detail))
        self.assertTrue(all(e.power_from_grid < 1 for e in result.detail))
        self.assertLess(result.detail[-1].battery_energy,21)
        self.assertEqual(E.resolve_battery_discharge_policy(-1,True),0)
        self.assertEqual(E.resolve_battery_discharge_policy(0,False),0)
        self.assertEqual(E.resolve_battery_discharge_policy(2500,False),2500)
