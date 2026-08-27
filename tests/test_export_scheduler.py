import unittest
from datetime import datetime, timedelta, timezone

from modules.export_scheduler import (
    ExportSchedulerSlot,
    ReserveTrajectorySlot,
    adapt_grid_target_to_live_power,
    build_hard_cap_storage_hold_schedule,
    build_reserve_energy_schedule,
    build_unified_export_schedule,
    calculate_net_battery_export_price,
    fit_export_schedule_to_energy_budget,
    get_policy_bounded_export_budget,
    get_interval_price,
    get_optional_export_grid_target,
    is_ev_available_for_charging,
    is_ev_plan_fresh,
)


class UnifiedExportSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc)

    def test_net_export_price_accounts_for_inverter_and_broker(self):
        self.assertAlmostEqual(
            calculate_net_battery_export_price(0.30, 0.90, 0.03),
            0.2619,
        )
        self.assertAlmostEqual(
            calculate_net_battery_export_price(0.17, 0.90, 0.03),
            0.14841,
        )

    def test_raw_market_price_lookup_covers_subintervals_and_dst_offsets(self):
        prices = [
            {
                "start_time": "2026-10-25T02:00:00+02:00",
                "end_time": "2026-10-25T02:00:00+01:00",
                "price_per_kwh": 0.30,
            },
            {
                "start_time": "2026-10-25T02:00:00+01:00",
                "end_time": "2026-10-25T03:00:00+01:00",
                "price_per_kwh": 0.17,
            },
        ]
        first_fold = datetime.fromisoformat("2026-10-25T02:30:00+02:00")
        second_fold = datetime.fromisoformat("2026-10-25T02:30:00+01:00")

        self.assertEqual(get_interval_price(prices, first_fold), 0.30)
        self.assertEqual(get_interval_price(prices, second_fold), 0.17)
        self.assertIsNone(
            get_interval_price(
                prices,
                datetime.fromisoformat("2026-10-25T04:00:00+01:00"),
            )
        )

    def test_high_price_cannot_create_export_without_policy_surplus(self):
        safe_budget = get_policy_bounded_export_budget(
            physically_available_energy_kwh=20.0,
            spendable_energy_after_ev_kwh=0.0,
        )
        high_price = self.slot(0, 1.00)
        plan = self.plan([high_price], energy=safe_budget)

        self.assertEqual(safe_budget, 0.0)
        self.assertEqual(plan.allocated_energy_kwh, 0.0)
        self.assertEqual(
            plan.grid_target_schedule[high_price.period_start.isoformat()],
            high_price.baseline_setpoint_w,
        )

    def slot(
        self,
        offset: int,
        price: float,
        *,
        hour: int | None = None,
        battery_power: float = 0.0,
        grid_export: float = 100.0,
        ev_power: float = 0.0,
        grid_import: float = 0.0,
    ) -> ExportSchedulerSlot:
        period_start = self.start + timedelta(minutes=15 * offset)
        if hour is not None:
            period_start = period_start.replace(hour=hour)
        return ExportSchedulerSlot(
            period_start=period_start,
            duration_hours=0.25,
            price_per_kwh=price,
            baseline_setpoint_w=-100.0,
            baseline_battery_power_w=battery_power,
            baseline_grid_export_w=grid_export,
            ev_charge_power_w=ev_power,
            intentional_grid_import_w=grid_import,
        )

    def plan(
        self,
        slots,
        energy=2.0,
        headroom=0.0,
        deadline=None,
        tolerance=0.05,
        headroom_reference_price=0.0,
        headroom_requirements=None,
        hard_headroom_requirements=None,
    ):
        return build_unified_export_schedule(
            slots=slots,
            available_export_energy_kwh=energy,
            required_headroom_energy_kwh=headroom,
            headroom_deadline=deadline,
            min_discharge_price=0.10,
            max_grid_export_w=5500.0,
            max_battery_discharge_w=8000.0,
            efficient_discharge_w=3500.0,
            quiet_boost_penalty_fraction=tolerance,
            headroom_reference_price=headroom_reference_price,
            headroom_min_price_spread=0.01,
            headroom_requirements=headroom_requirements,
            hard_headroom_requirements=hard_headroom_requirements,
        )

    def test_evening_incident_replay_uses_the_highest_price_buckets(self):
        slots = [
            self.slot(0, 0.32663),
            self.slot(1, 0.33885),
            self.slot(2, 0.27002),
            self.slot(3, 0.21348),
        ]
        plan = self.plan(slots, energy=2.0)

        self.assertLess(plan.grid_target_schedule[slots[1].period_start.isoformat()], -3500.0)
        self.assertEqual(plan.grid_target_schedule[slots[0].period_start.isoformat()], -3600.0)
        self.assertEqual(plan.grid_target_schedule[slots[2].period_start.isoformat()], -100.0)
        self.assertEqual(plan.grid_target_schedule[slots[3].period_start.isoformat()], -100.0)

    def test_quiet_tolerance_prefers_two_efficient_bands_over_one_noisy_boost(self):
        lower = self.slot(0, 0.32663)
        higher = self.slot(1, 0.33885)
        plan = self.plan([lower, higher], energy=1.75, tolerance=0.05)

        self.assertEqual(plan.grid_target_schedule[lower.period_start.isoformat()], -3600.0)
        self.assertEqual(plan.grid_target_schedule[higher.period_start.isoformat()], -3600.0)

    def test_quiet_efficiency_window_includes_the_full_23_hour(self):
        lower = self.slot(0, 0.32663, hour=23)
        higher = self.slot(1, 0.33885, hour=23)
        plan = self.plan([lower, higher], energy=1.75, tolerance=0.05)

        self.assertEqual(plan.grid_target_schedule[lower.period_start.isoformat()], -3600.0)
        self.assertEqual(plan.grid_target_schedule[higher.period_start.isoformat()], -3600.0)

    def test_quiet_efficiency_window_includes_the_midnight_hour(self):
        midnight = self.slot(0, 0.17430, hour=0)
        half_past = self.slot(1, 0.17430, hour=0)
        plan = self.plan([midnight, half_past], energy=1.75, tolerance=0.15)

        self.assertEqual(plan.grid_target_schedule[midnight.period_start.isoformat()], -3600.0)
        self.assertEqual(plan.grid_target_schedule[half_past.period_start.isoformat()], -3600.0)

    def test_quiet_efficiency_preference_crosses_the_midnight_boundary(self):
        quiet = self.slot(0, 0.17398, hour=23)
        midnight = ExportSchedulerSlot(
            period_start=quiet.period_start + timedelta(hours=1),
            duration_hours=0.25,
            price_per_kwh=0.17430,
            baseline_setpoint_w=-100.0,
            baseline_battery_power_w=0.0,
            baseline_grid_export_w=100.0,
        )
        plan = self.plan([quiet, midnight], energy=0.875, tolerance=0.15)

        self.assertEqual(plan.grid_target_schedule[quiet.period_start.isoformat()], -3600.0)
        self.assertEqual(plan.grid_target_schedule[midnight.period_start.isoformat()], -100.0)

    def test_materially_better_midnight_price_still_wins(self):
        quiet = self.slot(0, 0.17, hour=23)
        midnight = ExportSchedulerSlot(
            period_start=quiet.period_start + timedelta(hours=1),
            duration_hours=0.25,
            price_per_kwh=0.21,
            baseline_setpoint_w=-100.0,
            baseline_battery_power_w=0.0,
            baseline_grid_export_w=100.0,
        )
        plan = self.plan([quiet, midnight], energy=0.875, tolerance=0.15)

        self.assertEqual(plan.grid_target_schedule[quiet.period_start.isoformat()], -100.0)
        self.assertEqual(plan.grid_target_schedule[midnight.period_start.isoformat()], -3600.0)

    def test_slew_limited_current_export_displaces_future_energy(self):
        current = self.slot(0, 0.20)
        future = self.slot(1, 0.21)
        schedule = {
            current.period_start.isoformat(): 2000.0,
            future.period_start.isoformat(): 4000.0,
        }

        fitted = fit_export_schedule_to_energy_budget(
            schedule,
            [current, future],
            energy_budget_kwh=1.0,
            locked_keys={current.period_start.isoformat()},
        )

        self.assertEqual(fitted[current.period_start.isoformat()], 2000.0)
        self.assertEqual(fitted[future.period_start.isoformat()], 2000.0)
        self.assertAlmostEqual(
            sum(
                fitted[slot.period_start.isoformat()] * slot.duration_hours / 1000
                for slot in (current, future)
            ),
            1.0,
        )

    def test_forecast_uncertainty_reserve_inflates_load_and_discounts_pv(self):
        night = ReserveTrajectorySlot(
            period_start=self.start,
            duration_hours=1.0,
            house_power_w=600.0,
            pv_power_w=0.0,
            reserve_soc=5.0,
        )
        marginal_solar = ReserveTrajectorySlot(
            period_start=self.start + timedelta(hours=1),
            duration_hours=1.0,
            house_power_w=600.0,
            pv_power_w=800.0,
            reserve_soc=5.0,
        )

        baseline = build_reserve_energy_schedule(
            [night, marginal_solar],
            battery_capacity_kwh=60.0,
            uncertainty_margin_kwh=1.0,
        )
        robust = build_reserve_energy_schedule(
            [night, marginal_solar],
            battery_capacity_kwh=60.0,
            uncertainty_margin_kwh=1.0,
            house_load_margin_fraction=0.15,
            pv_confidence_factor=0.75,
        )

        self.assertAlmostEqual(baseline[marginal_solar.period_start.isoformat()], 4.0)
        self.assertAlmostEqual(baseline[night.period_start.isoformat()], 4.6)
        self.assertAlmostEqual(robust[marginal_solar.period_start.isoformat()], 4.09)
        self.assertAlmostEqual(robust[night.period_start.isoformat()], 4.78)
        self.assertGreater(
            robust[night.period_start.isoformat()],
            robust[marginal_solar.period_start.isoformat()],
        )

    def test_reserve_converts_ac_deficit_to_required_stored_energy(self):
        slot = ReserveTrajectorySlot(
            period_start=self.start,
            duration_hours=1.0,
            house_power_w=1000.0,
            pv_power_w=0.0,
            reserve_soc=0.0,
        )
        schedule = build_reserve_energy_schedule(
            [slot],
            battery_capacity_kwh=60.0,
            uncertainty_margin_kwh=0.0,
            discharge_efficiency=0.90,
        )
        self.assertAlmostEqual(
            schedule[slot.period_start.isoformat()],
            1.0 / 0.90,
        )

    def test_zero_quiet_tolerance_uses_maximum_power_at_the_highest_price(self):
        lower = self.slot(0, 0.32663)
        higher = self.slot(1, 0.33885)
        plan = self.plan([lower, higher], energy=1.35, tolerance=0.0)

        self.assertEqual(plan.grid_target_schedule[higher.period_start.isoformat()], -5500.0)
        self.assertEqual(plan.grid_target_schedule[lower.period_start.isoformat()], -100.0)

    def test_daytime_has_no_quiet_power_penalty(self):
        high = self.slot(0, 0.30, hour=12)
        low = self.slot(1, 0.29, hour=12)
        plan = self.plan([high, low], energy=1.35)

        self.assertEqual(plan.grid_target_schedule[high.period_start.isoformat()], -5500.0)
        self.assertEqual(plan.grid_target_schedule[low.period_start.isoformat()], -100.0)

    def test_ev_charging_slot_is_not_used_for_battery_export(self):
        ev_slot = self.slot(0, 0.40, ev_power=3680.0)
        other_slot = self.slot(1, 0.20)
        plan = self.plan([ev_slot, other_slot], energy=0.875)

        self.assertEqual(plan.grid_target_schedule[ev_slot.period_start.isoformat()], -100.0)
        self.assertEqual(plan.grid_target_schedule[other_slot.period_start.isoformat()], -3600.0)

    def test_intentional_grid_charge_slot_is_not_used_for_export(self):
        import_slot = self.slot(0, 0.40, grid_import=4000.0)
        other_slot = self.slot(1, 0.20)
        plan = self.plan([import_slot, other_slot], energy=0.875)

        self.assertEqual(plan.grid_target_schedule[import_slot.period_start.isoformat()], -100.0)
        self.assertEqual(plan.grid_target_schedule[other_slot.period_start.isoformat()], -3600.0)

    def test_headroom_is_allocated_before_raw_peak_even_below_price_threshold(self):
        before_peak = self.slot(0, 0.05)
        after_peak = self.slot(1, 0.50)
        deadline = after_peak.period_start
        plan = self.plan(
            [before_peak, after_peak],
            energy=1.0,
            headroom=0.875,
            deadline=deadline,
        )

        self.assertEqual(plan.grid_target_schedule[before_peak.period_start.isoformat()], -3600.0)
        self.assertAlmostEqual(plan.headroom_energy_kwh, 0.875)

    def test_headroom_shortage_is_reported_without_violating_energy_budget(self):
        slot = self.slot(0, 0.05)
        plan = self.plan(
            [slot],
            energy=0.25,
            headroom=1.0,
            deadline=slot.period_start + timedelta(minutes=15),
        )

        self.assertAlmostEqual(plan.allocated_energy_kwh, 0.25)
        self.assertAlmostEqual(plan.headroom_energy_kwh, 0.25)
        self.assertAlmostEqual(plan.unallocated_headroom_kwh, 0.75)

    def test_physical_headroom_ignores_an_unprofitable_price_spread(self):
        flat_price = self.slot(0, 0.09)
        plan = self.plan(
            [flat_price],
            energy=0.875,
            headroom=0.875,
            deadline=flat_price.period_start + timedelta(minutes=15),
            headroom_reference_price=0.09,
        )

        self.assertAlmostEqual(plan.headroom_energy_kwh, 0.875)
        self.assertAlmostEqual(plan.unallocated_headroom_kwh, 0.0)
        self.assertEqual(plan.battery_power_delta_schedule[flat_price.period_start.isoformat()], 3500.0)

    def test_soft_headroom_uses_a_profitable_price_spread(self):
        valuable_export = self.slot(0, 0.20)
        plan = self.plan(
            [valuable_export],
            energy=0.875,
            headroom=0.875,
            deadline=valuable_export.period_start + timedelta(minutes=15),
            headroom_reference_price=0.05,
        )

        self.assertAlmostEqual(plan.headroom_energy_kwh, 0.875)
        self.assertEqual(
            plan.battery_power_delta_schedule[valuable_export.period_start.isoformat()],
            3500.0,
        )

    def test_cumulative_headroom_is_exported_before_the_first_solar_deadline(self):
        first_solar_window = self.slot(0, -0.01)
        later_valuable_slot = self.slot(1, 0.30)
        plan = self.plan(
            [first_solar_window, later_valuable_slot],
            energy=0.875,
            headroom=0.875,
            deadline=later_valuable_slot.period_start + timedelta(minutes=15),
            headroom_requirements=[
                (first_solar_window.period_start + timedelta(minutes=15), 0.875),
            ],
        )

        self.assertEqual(
            plan.battery_power_delta_schedule[first_solar_window.period_start.isoformat()],
            3500.0,
        )
        self.assertEqual(
            plan.battery_power_delta_schedule[later_valuable_slot.period_start.isoformat()],
            0.0,
        )

    def test_negative_price_headroom_fills_contiguous_power_bands(self):
        negative_price_slot = self.slot(0, -0.01)
        plan = self.plan(
            [negative_price_slot],
            energy=1.35,
            headroom=1.35,
            deadline=negative_price_slot.period_start + timedelta(minutes=15),
        )

        self.assertEqual(
            plan.battery_power_delta_schedule[negative_price_slot.period_start.isoformat()],
            5400.0,
        )
        self.assertAlmostEqual(plan.allocated_energy_kwh, 1.35)

    def test_hard_grid_cap_headroom_wins_when_energy_budget_is_short(self):
        early_preferred_slot = self.slot(0, 0.05)
        later_hard_slot = self.slot(1, 0.30)
        plan = self.plan(
            [early_preferred_slot, later_hard_slot],
            energy=0.875,
            headroom=0.875,
            deadline=later_hard_slot.period_start + timedelta(minutes=15),
            headroom_requirements=[
                (early_preferred_slot.period_start + timedelta(minutes=15), 0.875),
            ],
            hard_headroom_requirements=[
                (later_hard_slot.period_start + timedelta(minutes=15), 0.875),
            ],
        )

        self.assertEqual(
            plan.battery_power_delta_schedule[early_preferred_slot.period_start.isoformat()],
            0.0,
        )
        self.assertEqual(
            plan.battery_power_delta_schedule[later_hard_slot.period_start.isoformat()],
            3500.0,
        )
        self.assertAlmostEqual(plan.hard_headroom_shortfall_kwh, 0.0)
        self.assertAlmostEqual(plan.unallocated_headroom_kwh, 0.875)

    def test_hard_grid_cap_headroom_is_created_just_before_the_deadline(self):
        earlier_high_price = self.slot(0, 0.40)
        later_low_price = self.slot(1, -0.01)
        deadline = later_low_price.period_start + timedelta(minutes=15)
        plan = self.plan(
            [earlier_high_price, later_low_price],
            energy=0.875,
            hard_headroom_requirements=[(deadline, 0.875)],
        )

        self.assertEqual(
            plan.battery_power_delta_schedule[earlier_high_price.period_start.isoformat()],
            0.0,
        )
        self.assertEqual(
            plan.battery_power_delta_schedule[later_low_price.period_start.isoformat()],
            3500.0,
        )

    def test_hard_cap_hold_preserves_headroom_through_sub_cap_pv_slots(self):
        before_peak = self.slot(0, 0.20, battery_power=2000.0, grid_export=100.0)
        first_violation = self.slot(1, 0.20, grid_export=5200.0)
        sub_cap_peak = self.slot(2, 0.20, battery_power=3000.0, grid_export=100.0)
        last_violation = self.slot(3, 0.20, grid_export=5600.0)
        after_peak = self.slot(4, 0.20, battery_power=1000.0, grid_export=100.0)

        hold = build_hard_cap_storage_hold_schedule(
            [before_peak, first_violation, sub_cap_peak, last_violation, after_peak],
            max_grid_export_w=5000.0,
            neutral_grid_target_w=-100.0,
        )

        self.assertNotIn(before_peak.period_start.isoformat(), hold)
        self.assertEqual(hold[first_violation.period_start.isoformat()], 4900.0)
        self.assertEqual(hold[sub_cap_peak.period_start.isoformat()], 3000.0)
        self.assertEqual(hold[last_violation.period_start.isoformat()], 4900.0)
        self.assertNotIn(after_peak.period_start.isoformat(), hold)

    def test_existing_pv_export_reduces_room_below_the_grid_cap(self):
        slot = self.slot(0, 0.30, grid_export=5000.0)
        plan = self.plan([slot], energy=1.0)

        self.assertEqual(plan.grid_target_schedule[slot.period_start.isoformat()], -600.0)
        self.assertLessEqual(-plan.grid_target_schedule[slot.period_start.isoformat()], 5500.0)

    def test_existing_battery_discharge_counts_toward_efficient_power(self):
        slot = self.slot(0, 0.30, battery_power=-2000.0)
        plan = self.plan([slot], energy=0.375)

        self.assertEqual(plan.battery_power_delta_schedule[slot.period_start.isoformat()], 1500.0)

    def test_zero_export_budget_keeps_live_target_neutral(self):
        target = get_optional_export_grid_target(
            optional_export_power_w=0.0,
            max_grid_export_w=5500.0,
            neutral_grid_target_w=-100.0,
        )
        self.assertEqual(target, -100.0)

    def test_live_target_absorbs_unforecast_pv_without_optional_export(self):
        target = adapt_grid_target_to_live_power(
            planned_battery_power_w=4151.0,
            planned_optional_export_power_w=0.0,
            house_load_w=840.0,
            pv_power_w=8949.0,
            max_grid_export_w=5000.0,
            neutral_grid_target_w=-100.0,
        )
        self.assertEqual(target, -100.0)

    def test_live_target_preserves_planned_battery_power_for_optional_export(self):
        target = adapt_grid_target_to_live_power(
            planned_battery_power_w=4151.0,
            planned_optional_export_power_w=1000.0,
            house_load_w=840.0,
            pv_power_w=8949.0,
            max_grid_export_w=5000.0,
            neutral_grid_target_w=-100.0,
        )
        self.assertEqual(target, -3958.0)

    def test_live_target_preserves_planned_discharge_without_import(self):
        target = adapt_grid_target_to_live_power(
            planned_battery_power_w=-3500.0,
            planned_optional_export_power_w=3500.0,
            house_load_w=2000.0,
            pv_power_w=0.0,
            max_grid_export_w=5000.0,
            neutral_grid_target_w=-100.0,
        )
        self.assertEqual(target, -1500.0)

    def test_stale_plan_holds_live_power_adaptation_neutral(self):
        target = adapt_grid_target_to_live_power(
            planned_battery_power_w=0.0,
            planned_optional_export_power_w=1000.0,
            house_load_w=500.0,
            pv_power_w=7500.0,
            max_grid_export_w=5000.0,
            neutral_grid_target_w=-100.0,
            hold_optional_export=True,
        )
        self.assertEqual(target, -100.0)

    def test_optional_export_target_uses_the_planning_margin(self):
        self.assertEqual(
            get_optional_export_grid_target(
                optional_export_power_w=6000.0,
                max_grid_export_w=5350.0,
                neutral_grid_target_w=-100.0,
            ),
            -5350.0,
        )

    def test_optional_export_target_is_neutral_while_ev_charges(self):
        self.assertEqual(
            get_optional_export_grid_target(
                optional_export_power_w=3500.0,
                max_grid_export_w=5350.0,
                neutral_grid_target_w=-100.0,
                ev_is_charging=True,
            ),
            -100.0,
        )

    def test_optional_export_never_exceeds_physical_or_policy_energy(self):
        cases = [
            (2.314, 0.0, 0.0),
            (2.314, 0.99, 0.99),
            (2.314, 4.0, 2.314),
            (-1.0, 4.0, 0.0),
            (4.0, -1.0, 0.0),
        ]
        for physical_kwh, policy_kwh, expected_kwh in cases:
            with self.subTest(physical_kwh=physical_kwh, policy_kwh=policy_kwh):
                result = get_policy_bounded_export_budget(
                    physically_available_energy_kwh=physical_kwh,
                    spendable_energy_after_ev_kwh=policy_kwh,
                )
                self.assertEqual(result, expected_kwh)
                self.assertGreaterEqual(result, 0.0)
                self.assertLessEqual(result, max(0.0, physical_kwh))
                self.assertLessEqual(result, max(0.0, policy_kwh))

    def test_stale_charging_transition_holds_optional_export_neutral(self):
        plan_time = datetime(2026, 8, 20, 10, 46, 20, tzinfo=timezone.utc)
        plan_is_fresh = is_ev_plan_fresh(
            planned_ev_available=True,
            planned_ev_is_charging=True,
            planned_ev_energy_needed_kwh=11.4,
            plan_calculated_at=plan_time,
            live_ev_available=True,
            live_ev_is_charging=False,
            live_ev_energy_needed_kwh=11.4,
            current_time=plan_time + timedelta(seconds=11),
        )

        self.assertFalse(plan_is_fresh)
        self.assertEqual(
            get_optional_export_grid_target(
                optional_export_power_w=5250.0,
                max_grid_export_w=5350.0,
                neutral_grid_target_w=-100.0,
                ev_is_charging=False,
                hold_optional_export=not plan_is_fresh,
            ),
            -100.0,
        )

    def test_refreshed_plan_releases_the_stale_plan_hold(self):
        plan_time = datetime(2026, 8, 20, 10, 46, 31, tzinfo=timezone.utc)
        plan_is_fresh = is_ev_plan_fresh(
            planned_ev_available=True,
            planned_ev_is_charging=False,
            planned_ev_energy_needed_kwh=1.0,
            plan_calculated_at=plan_time.isoformat(),
            live_ev_available=True,
            live_ev_is_charging=False,
            live_ev_energy_needed_kwh=1.0,
            current_time=plan_time + timedelta(seconds=2),
        )

        self.assertTrue(plan_is_fresh)
        self.assertEqual(
            get_optional_export_grid_target(
                optional_export_power_w=5250.0,
                max_grid_export_w=5350.0,
                neutral_grid_target_w=-100.0,
                ev_is_charging=False,
                hold_optional_export=not plan_is_fresh,
            ),
            -5350.0,
        )

    def test_aged_plan_holds_optional_export(self):
        plan_time = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(
            is_ev_plan_fresh(
                planned_ev_available=True,
                planned_ev_is_charging=False,
                planned_ev_energy_needed_kwh=1.0,
                plan_calculated_at=plan_time,
                live_ev_available=True,
                live_ev_is_charging=False,
                live_ev_energy_needed_kwh=1.0,
                current_time=plan_time + timedelta(seconds=181),
            )
        )

    def test_changed_ev_energy_demand_stales_plan(self):
        plan_time = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(
            is_ev_plan_fresh(
                planned_ev_available=True,
                planned_ev_is_charging=False,
                planned_ev_energy_needed_kwh=1.0,
                plan_calculated_at=plan_time,
                live_ev_available=True,
                live_ev_is_charging=False,
                live_ev_energy_needed_kwh=1.2,
                current_time=plan_time + timedelta(seconds=2),
            )
        )

    def test_active_charge_control_counts_as_ev_available(self):
        self.assertTrue(
            is_ev_available_for_charging(
                charger_ready=False,
                charger_control_enabled=True,
                ev_is_charging=True,
            )
        )

    def test_ready_charger_counts_as_ev_available_after_charge_stops(self):
        self.assertTrue(
            is_ev_available_for_charging(
                charger_ready=True,
                charger_control_enabled=False,
                ev_is_charging=False,
            )
        )

    def test_reserve_trajectory_tracks_deficit_until_solar_meets_load(self):
        slots = [
            ReserveTrajectorySlot(self.start, 1.0, 1000.0, 0.0, 5.0),
            ReserveTrajectorySlot(self.start + timedelta(hours=1), 1.0, 1000.0, 0.0, 5.0),
            ReserveTrajectorySlot(self.start + timedelta(hours=2), 1.0, 1000.0, 2000.0, 5.0),
        ]

        schedule = build_reserve_energy_schedule(
            slots,
            battery_capacity_kwh=20.0,
            uncertainty_margin_kwh=1.0,
        )

        self.assertEqual(schedule[slots[0].period_start.isoformat()], 4.0)
        self.assertEqual(schedule[slots[1].period_start.isoformat()], 3.0)
        self.assertEqual(schedule[slots[2].period_start.isoformat()], 2.0)

    def test_reserve_trajectory_honors_a_higher_slot_reserve(self):
        slot = ReserveTrajectorySlot(self.start, 0.5, 500.0, 2000.0, 15.0)
        schedule = build_reserve_energy_schedule(
            [slot],
            battery_capacity_kwh=20.0,
            uncertainty_margin_kwh=1.0,
        )

        self.assertEqual(schedule[slot.period_start.isoformat()], 4.0)

    def test_reserve_trajectory_skips_cheap_deficit_but_carries_future_expensive_need(self):
        cheap = ReserveTrajectorySlot(
            self.start,
            1.0,
            1000.0,
            0.0,
            5.0,
            protect_deficit=False,
        )
        expensive = ReserveTrajectorySlot(
            self.start + timedelta(hours=1),
            1.0,
            1000.0,
            0.0,
            5.0,
            protect_deficit=True,
        )
        solar = ReserveTrajectorySlot(
            self.start + timedelta(hours=2),
            1.0,
            1000.0,
            2000.0,
            5.0,
        )

        schedule = build_reserve_energy_schedule(
            [cheap, expensive, solar],
            battery_capacity_kwh=60.0,
            uncertainty_margin_kwh=1.0,
        )

        self.assertEqual(schedule[solar.period_start.isoformat()], 4.0)
        self.assertEqual(schedule[expensive.period_start.isoformat()], 5.0)
        self.assertEqual(schedule[cheap.period_start.isoformat()], 5.0)


if __name__ == "__main__":
    unittest.main()
