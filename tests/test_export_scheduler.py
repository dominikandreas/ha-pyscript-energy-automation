import unittest
from datetime import datetime, timedelta, timezone

from modules.export_scheduler import (
    ExportSchedulerSlot,
    ReserveTrajectorySlot,
    build_reserve_energy_schedule,
    build_unified_export_schedule,
    fit_export_schedule_to_energy_budget,
    get_optional_export_grid_target,
    is_ev_available_for_charging,
)


class UnifiedExportSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc)

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

    def test_soft_headroom_skips_an_unprofitable_battery_cycle(self):
        flat_price = self.slot(0, 0.09)
        plan = self.plan(
            [flat_price],
            energy=0.875,
            headroom=0.875,
            deadline=flat_price.period_start + timedelta(minutes=15),
            headroom_reference_price=0.09,
        )

        self.assertAlmostEqual(plan.headroom_energy_kwh, 0.0)
        self.assertAlmostEqual(plan.unallocated_headroom_kwh, 0.875)
        self.assertEqual(plan.battery_power_delta_schedule[flat_price.period_start.isoformat()], 0.0)

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

    def test_optional_export_target_is_neutral_while_connected_ev_needs_charge(self):
        self.assertEqual(
            get_optional_export_grid_target(
                optional_export_power_w=5250.0,
                max_grid_export_w=5350.0,
                neutral_grid_target_w=-100.0,
                ev_is_charging=False,
                ev_requires_charge=True,
            ),
            -100.0,
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


if __name__ == "__main__":
    unittest.main()
