import ast
import inspect
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from modules import export_scheduler
from modules.export_scheduler import (
    ExportSchedulerSlot,
    ReserveTrajectorySlot,
    adapt_grid_target_to_live_power,
    build_hard_cap_storage_hold_schedule,
    build_reserve_energy_schedule,
    build_unified_export_schedule,
    calculate_live_headroom_control,
    calculate_net_battery_export_price,
    fit_export_schedule_to_energy_budget,
    get_policy_bounded_export_budget,
    get_interval_price,
    get_optional_export_grid_target,
    headroom_release_factor,
    is_ev_available_for_charging,
    is_ev_plan_fresh,
    read_headroom_released_kwh,
    update_headroom_release_ledger,
    update_live_headroom_state,
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

    def test_live_headroom_uses_grid_and_battery_instead_of_derived_load(self):
        decision = calculate_live_headroom_control(
            measured_grid_power_w=-100.0,
            measured_battery_power_w=3500.0,
            battery_free_capacity_kwh=0.13,
            held_headroom_target_kwh=0.0,
            preferred_pv_export_w=3000.0,
            planned_grid_export_cap_w=5000.0,
            neutral_grid_target_w=-100.0,
        )

        self.assertEqual(decision.natural_grid_power_w, -3600.0)
        self.assertAlmostEqual(decision.requested_headroom_kwh, 0.15)
        self.assertAlmostEqual(decision.headroom_deficit_kwh, 0.02)
        self.assertAlmostEqual(decision.desired_grid_target_w, -3708.0)
        self.assertTrue(decision.can_create_headroom)

    def test_live_headroom_respects_directional_battery_limits(self):
        no_charge_acceptance = calculate_live_headroom_control(
            measured_grid_power_w=0.0,
            measured_battery_power_w=6600.0,
            battery_free_capacity_kwh=0.0,
            held_headroom_target_kwh=0.0,
            preferred_pv_export_w=3000.0,
            planned_grid_export_cap_w=5000.0,
            neutral_grid_target_w=-100.0,
            max_battery_charge_w=6600.0,
            max_battery_discharge_w=8000.0,
        )
        zero_discharge = calculate_live_headroom_control(
            measured_grid_power_w=-100.0,
            measured_battery_power_w=3500.0,
            battery_free_capacity_kwh=0.0,
            held_headroom_target_kwh=0.15,
            preferred_pv_export_w=3000.0,
            planned_grid_export_cap_w=5000.0,
            neutral_grid_target_w=-100.0,
            max_battery_charge_w=6600.0,
            max_battery_discharge_w=0.0,
        )

        self.assertEqual(no_charge_acceptance.requested_headroom_kwh, 0.0)
        self.assertFalse(no_charge_acceptance.can_create_headroom)
        self.assertEqual(zero_discharge.desired_grid_target_w, -3600.0)
        self.assertFalse(zero_discharge.can_create_headroom)

    def test_live_headroom_holds_existing_capacity_without_discharging(self):
        decision = calculate_live_headroom_control(
            measured_grid_power_w=-100.0,
            measured_battery_power_w=3500.0,
            battery_free_capacity_kwh=0.30,
            held_headroom_target_kwh=0.15,
            preferred_pv_export_w=3000.0,
            planned_grid_export_cap_w=5000.0,
            neutral_grid_target_w=-100.0,
        )

        self.assertEqual(decision.headroom_deficit_kwh, 0.0)
        # The hold preserves the 0.15 kWh target without discharging, but
        # the 0.15 kWh excess above it keeps charging (paced to 900 W over
        # the risk horizon) instead of being pinned at zero: with twice the
        # required room free, exporting the full natural 3600 W would
        # strand the top-up (observed 2026-09-23: battery stuck at 91%).
        self.assertEqual(decision.desired_grid_target_w, -2700.0)
        self.assertFalse(decision.can_create_headroom)

    def test_live_headroom_hold_charges_paced_headroom_excess(self):
        # 2026-09-23: the hold pinned the battery at zero (full natural
        # export to grid) while free capacity was 3.6x the target, so the
        # battery sat at 91% exporting 4.4 kW instead of topping up.  The
        # hold must only preserve the target room and pace charging of the
        # excess over the risk horizon, converging to full instead of
        # freezing below it.
        decision = calculate_live_headroom_control(
            measured_grid_power_w=-4514.0,
            measured_battery_power_w=0.0,
            battery_free_capacity_kwh=2.78,
            held_headroom_target_kwh=0.771,
            preferred_pv_export_w=3000.0,
            planned_grid_export_cap_w=5000.0,
            neutral_grid_target_w=-100.0,
        )

        self.assertEqual(decision.headroom_deficit_kwh, 0.0)
        self.assertFalse(decision.can_create_headroom)
        # Excess 2.009 kWh over the 10-minute horizon paces to ~12 kW, so
        # the whole 4514 W natural export is allowed into the battery and
        # the grid target yields to neutral instead of exporting everything.
        self.assertEqual(decision.desired_grid_target_w, -100.0)

    def test_live_headroom_hold_converges_to_target_without_overshoot(self):
        # As free capacity approaches the held target, allowed charging
        # shrinks to zero: the target room itself is never charged into.
        approaching = calculate_live_headroom_control(
            measured_grid_power_w=-100.0,
            measured_battery_power_w=3500.0,
            battery_free_capacity_kwh=0.30,
            held_headroom_target_kwh=0.15,
            preferred_pv_export_w=3000.0,
            planned_grid_export_cap_w=5000.0,
            neutral_grid_target_w=-100.0,
        )
        at_target = calculate_live_headroom_control(
            measured_grid_power_w=-100.0,
            measured_battery_power_w=3500.0,
            battery_free_capacity_kwh=0.15,
            held_headroom_target_kwh=0.15,
            preferred_pv_export_w=3000.0,
            planned_grid_export_cap_w=5000.0,
            neutral_grid_target_w=-100.0,
        )

        # Excess 0.15 kWh over 10 minutes allows 900 W of the 3600 W
        # natural export into the battery; the rest still goes to grid.
        self.assertEqual(approaching.desired_grid_target_w, -2700.0)
        # No excess left: the hold pins the battery at zero exactly.
        self.assertEqual(at_target.desired_grid_target_w, -3600.0)

    def test_live_headroom_hold_charging_respects_charge_acceptance(self):
        decision = calculate_live_headroom_control(
            measured_grid_power_w=1400.0,
            measured_battery_power_w=6000.0,
            battery_free_capacity_kwh=2.78,
            held_headroom_target_kwh=0.771,
            preferred_pv_export_w=3000.0,
            planned_grid_export_cap_w=5000.0,
            neutral_grid_target_w=-100.0,
            max_battery_charge_w=6600.0,
        )

        # Natural export is 4600 W but only 600 W of charge acceptance
        # remains, so export drops by exactly that 600 W.
        self.assertEqual(decision.desired_grid_target_w, -4000.0)

    def test_live_headroom_never_creates_capacity_through_export_cap(self):
        decision = calculate_live_headroom_control(
            measured_grid_power_w=-5000.0,
            measured_battery_power_w=0.0,
            battery_free_capacity_kwh=0.0,
            held_headroom_target_kwh=0.50,
            preferred_pv_export_w=3000.0,
            planned_grid_export_cap_w=5000.0,
            neutral_grid_target_w=-100.0,
        )

        self.assertEqual(decision.available_export_margin_w, 0.0)
        self.assertEqual(decision.desired_grid_target_w, -4800.0)
        self.assertFalse(decision.can_create_headroom)

    def test_live_headroom_is_inactive_below_preferred_export(self):
        decision = calculate_live_headroom_control(
            measured_grid_power_w=-100.0,
            measured_battery_power_w=2000.0,
            battery_free_capacity_kwh=0.0,
            held_headroom_target_kwh=0.0,
            preferred_pv_export_w=3000.0,
            planned_grid_export_cap_w=5000.0,
            neutral_grid_target_w=-100.0,
        )

        self.assertEqual(decision.requested_headroom_kwh, 0.0)
        self.assertEqual(decision.headroom_deficit_kwh, 0.0)
        self.assertEqual(decision.desired_grid_target_w, -2100.0)

    def test_recent_but_time_skewed_power_measurements_are_rejected(self):
        self.assertTrue(hasattr(export_scheduler, "are_power_measurements_coherent"))
        coherent = export_scheduler.are_power_measurements_coherent(
            current_time=self.start,
            grid_updated_at=self.start - timedelta(seconds=1),
            battery_updated_at=self.start - timedelta(seconds=9),
            max_age_seconds=10.0,
            max_skew_seconds=5.0,
        )

        self.assertFalse(coherent)

    def test_policy_budget_rejects_invalid_or_stale_snapshots(self):
        base = dict(
            base_spendable_energy_kwh=23.9,
            ev_adjusted_spendable_energy_kwh=21.97,
            source_battery_energy_kwh=25.0,
            current_battery_energy_kwh=25.0,
            current_time=self.start,
            max_age_seconds=180.0,
        )
        invalid_cases = (
            {"calculated_at": None},
            {"calculated_at": self.start - timedelta(seconds=181)},
            {"calculated_at": self.start + timedelta(seconds=1)},
            {"calculated_at": self.start.replace(tzinfo=None)},
            {"calculated_at": self.start.replace(tzinfo=None).isoformat()},
            {"calculated_at": "not-a-datetime"},
            {"calculated_at": self.start, "base_spendable_energy_kwh": float("nan")},
            {"calculated_at": self.start, "source_battery_energy_kwh": float("inf")},
            {"calculated_at": self.start, "base_spendable_energy_kwh": -0.1},
            {"calculated_at": self.start, "ev_adjusted_spendable_energy_kwh": -0.1},
            {
                "calculated_at": self.start,
                "ev_adjusted_spendable_energy_kwh": 24.0,
            },
            {"calculated_at": self.start, "source_battery_energy_kwh": -0.1},
            {"calculated_at": self.start, "current_battery_energy_kwh": -0.1},
            {"calculated_at": self.start, "discharge_efficiency": 0.0},
            {"calculated_at": self.start, "discharge_efficiency": 1.1},
        )

        for override in invalid_cases:
            with self.subTest(override=override):
                snapshot = export_scheduler.get_fresh_export_policy_budget(
                    **(base | override)
                )
                self.assertFalse(snapshot.valid)
                self.assertEqual(snapshot.base_spendable_energy_kwh, 0.0)
                self.assertEqual(snapshot.ev_adjusted_spendable_energy_kwh, 0.0)

        aware_string_snapshot = export_scheduler.get_fresh_export_policy_budget(
            **(base | {"calculated_at": self.start.isoformat()})
        )
        self.assertTrue(aware_string_snapshot.valid)

    def test_policy_budget_uses_no_pyscript_unsupported_generator_expression(self):
        helper_tree = ast.parse(
            inspect.getsource(export_scheduler.get_fresh_export_policy_budget)
        )
        self.assertFalse(
            any(isinstance(node, ast.GeneratorExp) for node in ast.walk(helper_tree))
        )

    def test_policy_budget_deducts_battery_draw_since_snapshot(self):
        snapshot = export_scheduler.get_fresh_export_policy_budget(
            base_spendable_energy_kwh=23.9,
            ev_adjusted_spendable_energy_kwh=21.97,
            source_battery_energy_kwh=25.0,
            current_battery_energy_kwh=20.0,
            calculated_at=self.start - timedelta(seconds=60),
            current_time=self.start,
            max_age_seconds=180.0,
            discharge_efficiency=0.90,
        )

        self.assertTrue(snapshot.valid)
        self.assertAlmostEqual(snapshot.battery_draw_since_calculation_kwh, 4.5)
        self.assertAlmostEqual(snapshot.base_spendable_energy_kwh, 19.4)
        self.assertAlmostEqual(snapshot.ev_adjusted_spendable_energy_kwh, 17.47)

    def test_policy_budget_consumption_survives_replans_with_original_anchor(self):
        base = dict(
            base_spendable_energy_kwh=10.0,
            ev_adjusted_spendable_energy_kwh=8.0,
            source_battery_energy_kwh=25.0,
            calculated_at=self.start - timedelta(seconds=60),
            max_age_seconds=180.0,
            discharge_efficiency=0.90,
        )
        first_cycle = export_scheduler.get_fresh_export_policy_budget(
            **(base | {"current_battery_energy_kwh": 24.0, "current_time": self.start})
        )
        second_cycle = export_scheduler.get_fresh_export_policy_budget(
            **(
                base
                | {
                    "current_battery_energy_kwh": 23.0,
                    "current_time": self.start + timedelta(seconds=5),
                }
            )
        )

        self.assertTrue(first_cycle.valid)
        self.assertTrue(second_cycle.valid)
        self.assertAlmostEqual(first_cycle.battery_draw_since_calculation_kwh, 0.9)
        self.assertAlmostEqual(second_cycle.battery_draw_since_calculation_kwh, 1.8)
        self.assertAlmostEqual(first_cycle.ev_adjusted_spendable_energy_kwh, 7.1)
        self.assertAlmostEqual(second_cycle.ev_adjusted_spendable_energy_kwh, 6.2)

    def test_policy_budget_ledger_never_restores_consumed_energy_after_recharge(self):
        snapshot_id = "2026-09-11T12:00:00+00:00"
        first = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id=snapshot_id,
            candidate_remaining_kwh=5.0,
            previous_state="",
        )
        after_discharge = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id=snapshot_id,
            candidate_remaining_kwh=3.2,
            previous_state=first.state_value,
        )
        after_recharge = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id=snapshot_id,
            candidate_remaining_kwh=5.0,
            previous_state=after_discharge.state_value,
        )
        new_snapshot = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id="2026-09-11T12:02:00+00:00",
            candidate_remaining_kwh=5.0,
            previous_state=after_recharge.state_value,
        )

        self.assertTrue(first.valid)
        self.assertEqual(first.remaining_kwh, 5.0)
        self.assertEqual(after_discharge.remaining_kwh, 3.2)
        self.assertEqual(after_recharge.remaining_kwh, 3.2)
        self.assertEqual(new_snapshot.remaining_kwh, 5.0)

    def test_policy_budget_ledger_accumulates_discharge_even_if_battery_recharges(self):
        snapshot_id = "2026-09-11T12:00:00+00:00"
        start = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        first = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id=snapshot_id,
            candidate_remaining_kwh=5.0,
            previous_state="",
            current_time=start,
            battery_discharge_power_w=0.0,
        )
        after_discharge = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id=snapshot_id,
            candidate_remaining_kwh=5.0,
            previous_state=first.state_value,
            current_time=start + timedelta(seconds=5),
            battery_discharge_power_w=3600.0,
        )
        after_recharge = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id=snapshot_id,
            candidate_remaining_kwh=5.0,
            previous_state=after_discharge.state_value,
            current_time=start + timedelta(seconds=10),
            battery_discharge_power_w=0.0,
        )

        self.assertAlmostEqual(after_discharge.remaining_kwh, 4.995)
        self.assertAlmostEqual(after_recharge.remaining_kwh, 4.995)

    def test_policy_budget_ledger_invalidates_snapshot_across_power_telemetry_outage(self):
        snapshot_id = "2026-09-11T12:00:00+00:00"
        start = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        first = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id=snapshot_id,
            candidate_remaining_kwh=5.0,
            previous_state="",
            current_time=start,
            battery_discharge_power_w=0.0,
            measurements_available=True,
        )
        outage = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id=snapshot_id,
            candidate_remaining_kwh=5.0,
            previous_state=first.state_value,
            current_time=start + timedelta(seconds=5),
            battery_discharge_power_w=0.0,
            measurements_available=False,
        )
        recovery = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id=snapshot_id,
            candidate_remaining_kwh=5.0,
            previous_state=outage.state_value,
            current_time=start + timedelta(seconds=10),
            battery_discharge_power_w=0.0,
            measurements_available=True,
        )
        new_snapshot = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id="2026-09-11T12:00:06+00:00",
            candidate_remaining_kwh=5.0,
            previous_state=outage.state_value,
            current_time=start + timedelta(seconds=10),
            battery_discharge_power_w=0.0,
            measurements_available=True,
        )

        self.assertTrue(first.valid)
        self.assertFalse(outage.valid)
        self.assertEqual(outage.reason, "power_measurements_unavailable")
        self.assertFalse(recovery.valid)
        self.assertEqual(recovery.reason, "persisted_snapshot_invalid")
        self.assertTrue(new_snapshot.valid)
        self.assertEqual(new_snapshot.remaining_kwh, 5.0)

    def test_policy_budget_ledger_rebases_accounting_gap_with_healthy_telemetry(self):
        snapshot_id = "2026-09-11T12:00:00+00:00"
        start = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        first = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id=snapshot_id,
            candidate_remaining_kwh=5.0,
            previous_state="",
            current_time=start,
            battery_discharge_power_w=0.0,
            measurements_available=True,
        )
        # Scheduler hold skipped accounting for a minute while telemetry
        # stayed healthy; the live candidate already deducts the draw.
        rebased = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id=snapshot_id,
            candidate_remaining_kwh=4.7,
            previous_state=first.state_value,
            current_time=start + timedelta(seconds=65),
            battery_discharge_power_w=0.0,
            measurements_available=True,
        )
        resumed = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id=snapshot_id,
            candidate_remaining_kwh=4.7,
            previous_state=rebased.state_value,
            current_time=start + timedelta(seconds=70),
            battery_discharge_power_w=0.0,
            measurements_available=True,
        )
        # Same gap without healthy telemetry still invalidates.
        blind = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id=snapshot_id,
            candidate_remaining_kwh=4.7,
            previous_state=first.state_value,
            current_time=start + timedelta(seconds=65),
            battery_discharge_power_w=0.0,
            measurements_available=None,
        )

        self.assertTrue(rebased.valid)
        self.assertEqual(rebased.reason, "accounting_gap_rebased")
        self.assertAlmostEqual(rebased.remaining_kwh, 4.7)
        self.assertTrue(resumed.valid)
        self.assertAlmostEqual(resumed.remaining_kwh, 4.7)
        # The floor stays monotonic: a later higher candidate cannot restore.
        restored = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id=snapshot_id,
            candidate_remaining_kwh=5.0,
            previous_state=resumed.state_value,
            current_time=start + timedelta(seconds=75),
            battery_discharge_power_w=0.0,
            measurements_available=True,
        )
        self.assertAlmostEqual(restored.remaining_kwh, 4.7)
        self.assertFalse(blind.valid)
        self.assertEqual(blind.reason, "invalid_accounting_gap")

    def test_policy_budget_ledger_rejects_malformed_or_future_snapshot_ids(self):
        current_time = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        malformed = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id="not-a-timestamp",
            candidate_remaining_kwh=5.0,
            previous_state="",
            current_time=current_time,
        )
        future = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id="2026-09-11T12:00:10+00:00",
            candidate_remaining_kwh=5.0,
            previous_state="",
            current_time=current_time,
        )

        self.assertFalse(malformed.valid)
        self.assertEqual(malformed.reason, "invalid_snapshot_id")
        self.assertFalse(future.valid)
        self.assertEqual(future.reason, "future_snapshot_id")

    def test_policy_budget_ledger_only_resets_for_newer_snapshot(self):
        current_time = datetime(2026, 9, 11, 12, 0, 20, tzinfo=timezone.utc)
        persisted = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id="2026-09-11T12:00:10+00:00",
            candidate_remaining_kwh=3.0,
            previous_state="",
            current_time=current_time,
        )
        older = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id="2026-09-11T12:00:05+00:00",
            candidate_remaining_kwh=5.0,
            previous_state=persisted.state_value,
            current_time=current_time,
        )
        newer = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id="2026-09-11T12:00:15+00:00",
            candidate_remaining_kwh=5.0,
            previous_state=persisted.state_value,
            current_time=current_time,
        )

        self.assertFalse(older.valid)
        self.assertEqual(older.reason, "out_of_order_snapshot_id")
        self.assertTrue(newer.valid)
        self.assertEqual(newer.remaining_kwh, 5.0)

    def test_policy_budget_ledger_rejects_future_persisted_snapshot(self):
        current_time = datetime(2026, 9, 11, 12, 0, 20, tzinfo=timezone.utc)
        persisted_future = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id="2026-09-11T12:01:00+00:00",
            candidate_remaining_kwh=1.0,
            previous_state="",
        )
        result = export_scheduler.update_monotonic_export_budget_ledger(
            snapshot_id="2026-09-11T12:00:05+00:00",
            candidate_remaining_kwh=5.0,
            previous_state=persisted_future.state_value,
            current_time=current_time,
        )

        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "future_persisted_snapshot_id")
        self.assertEqual(result.state_value, persisted_future.state_value)

    def test_coherent_power_defines_battery_neutral_grid_target(self):
        self.assertEqual(
            export_scheduler.calculate_battery_neutral_grid_target_w(
                measured_grid_power_w=-3000.0,
                measured_battery_power_w=-2000.0,
            ),
            -1000.0,
        )
        self.assertEqual(
            export_scheduler.calculate_battery_neutral_grid_target_w(
                measured_grid_power_w=500.0,
                measured_battery_power_w=2000.0,
            ),
            -1500.0,
        )

    def test_stale_pv_uses_neutral_no_battery_export_target(self):
        self.assertEqual(
            export_scheduler.select_no_battery_export_target(
                pv_only_grid_target_w=-5000.0,
                neutral_grid_target_w=-20.0,
                pv_measurement_fresh=False,
            ),
            -20.0,
        )
        self.assertEqual(
            export_scheduler.select_no_battery_export_target(
                pv_only_grid_target_w=-5000.0,
                neutral_grid_target_w=-20.0,
                pv_measurement_fresh=True,
            ),
            -5000.0,
        )

    def test_power_measurements_reject_nonfinite_or_implausible_values(self):
        values_are_valid = export_scheduler.are_power_measurement_values_valid

        self.assertTrue(values_are_valid(-3500.0, 6200.0))
        self.assertFalse(values_are_valid(float("nan"), 1000.0))
        self.assertFalse(values_are_valid(-1000.0, float("inf")))
        self.assertFalse(values_are_valid(-150000.0, 1000.0))

    def test_measurement_age_is_none_when_timestamps_are_not_comparable(self):
        calculate_age = export_scheduler.calculate_measurement_age_seconds

        self.assertEqual(
            calculate_age(
                self.start,
                self.start - timedelta(seconds=7),
            ),
            7.0,
        )
        self.assertIsNone(
            calculate_age(
                self.start,
                self.start.replace(tzinfo=None),
            )
        )

    def test_battery_energy_and_directional_limits_reject_invalid_values(self):
        self.assertTrue(
            export_scheduler.are_battery_energy_values_valid(23.0, 12.0)
        )
        for capacity_kwh, energy_kwh in (
            (0.0, 0.0),
            (23.0, -0.1),
            (23.0, 23.1),
            (float("nan"), 12.0),
            (23.0, float("inf")),
        ):
            self.assertFalse(
                export_scheduler.are_battery_energy_values_valid(
                    capacity_kwh,
                    energy_kwh,
                )
            )

        self.assertEqual(
            export_scheduler.resolve_directional_power_limit_w(0.0, 8000.0, 8000.0),
            0.0,
        )
        self.assertEqual(
            export_scheduler.resolve_directional_power_limit_w(-1.0, 8000.0, 8000.0),
            8000.0,
        )
        self.assertEqual(
            export_scheduler.resolve_directional_power_limit_w(9000.0, 8000.0, 8000.0),
            8000.0,
        )
        for invalid_limit in (float("nan"), float("inf"), -2.0, "bad"):
            self.assertEqual(
                export_scheduler.resolve_directional_power_limit_w(
                    invalid_limit,
                    8000.0,
                    8000.0,
                ),
                0.0,
            )

    def test_still_battery_reading_is_usable_when_idle_and_consistent(self):
        usable = export_scheduler.is_battery_energy_measurement_usable

        def check(**overrides):
            params = {
                "current_time": self.start,
                "energy_updated_at": self.start - timedelta(seconds=400),
                "battery_energy_kwh": 14.14,
                "battery_capacity_kwh": 32.0,
                "battery_soc_percent": 44.2,
                "battery_power_w": -0.06,
                "battery_power_updated_at": self.start - timedelta(seconds=12),
                "max_age_seconds": 300.0,
            }
            params.update(overrides)
            return usable(**params)

        # Strict freshness still rejects the still reading (the old behavior
        # that deadlocked the loop every night the battery idled).
        self.assertFalse(
            export_scheduler.is_measurement_fresh(
                self.start,
                self.start - timedelta(seconds=400),
                max_age_seconds=300.0,
            )
        )
        # Idle battery, consistent energy: usable despite the still reading.
        self.assertTrue(check())
        # Fresh reading: usable without needing the exception.
        self.assertTrue(
            check(energy_updated_at=self.start - timedelta(seconds=10))
        )
        # Battery actually working: a still reading must not be trusted.
        self.assertFalse(check(battery_power_w=1500.0))
        self.assertFalse(check(battery_power_w=-2500.0))
        # Power telemetry stale too: nothing proves the battery is idle.
        self.assertFalse(
            check(
                battery_power_updated_at=self.start - timedelta(seconds=400)
            )
        )
        # Energy disagreeing with SoC x capacity: sensor problem, reject.
        self.assertFalse(check(battery_energy_kwh=20.0))
        self.assertFalse(check(battery_soc_percent=80.0))
        # Invalid inputs: reject.
        self.assertFalse(check(battery_energy_kwh="unknown"))
        self.assertFalse(check(battery_capacity_kwh=0.0))
        self.assertFalse(check(battery_soc_percent=133.0))
        self.assertFalse(check(battery_power_w=float("nan")))

    def test_measurement_freshness_rejects_stale_or_future_samples(self):
        is_fresh = export_scheduler.is_measurement_fresh

        self.assertTrue(
            is_fresh(
                self.start,
                self.start - timedelta(seconds=5),
                max_age_seconds=10.0,
            )
        )
        self.assertFalse(
            is_fresh(
                self.start,
                self.start - timedelta(seconds=11),
                max_age_seconds=10.0,
            )
        )
        self.assertFalse(
            is_fresh(
                self.start,
                self.start + timedelta(seconds=1),
                max_age_seconds=10.0,
            )
        )

    def test_strict_datetime_parser_rejects_naive_and_malformed_values(self):
        parse_datetime = export_scheduler.parse_aware_datetime

        self.assertEqual(parse_datetime(self.start.isoformat()), self.start)
        self.assertEqual(parse_datetime(self.start), self.start)
        self.assertIsNone(parse_datetime(self.start.replace(tzinfo=None)))
        self.assertIsNone(parse_datetime(self.start.replace(tzinfo=None).isoformat()))
        self.assertIsNone(parse_datetime("not-a-datetime"))

    def test_measurement_skew_is_none_for_mixed_timezone_awareness(self):
        aware = self.start
        naive = aware.replace(tzinfo=None)

        self.assertIsNone(
            export_scheduler.calculate_measurement_skew_seconds(aware, naive)
        )

    def test_fresh_synchronized_power_measurements_are_accepted(self):
        coherent = export_scheduler.are_power_measurements_coherent(
            current_time=self.start,
            grid_updated_at=self.start - timedelta(seconds=2),
            battery_updated_at=self.start - timedelta(seconds=4),
            max_age_seconds=10.0,
            max_skew_seconds=5.0,
        )

        self.assertTrue(coherent)

    def test_high_side_headroom_tracks_positive_live_pv_error_with_decay(self):
        self.assertTrue(
            hasattr(export_scheduler, "build_high_side_headroom_requirements")
        )
        slots = [
            ExportSchedulerSlot(
                period_start=self.start + timedelta(minutes=30 * index),
                duration_hours=0.5,
                price_per_kwh=0.10,
                baseline_setpoint_w=-100.0,
                baseline_battery_power_w=4000.0,
                baseline_grid_export_w=0.0,
            )
            for index in range(2)
        ]
        period_ends = {
            slot.period_start.isoformat(): slot.period_start + timedelta(minutes=30)
            for slot in slots
        }

        result = export_scheduler.build_high_side_headroom_requirements(
            slots=slots,
            period_ends=period_ends,
            current_time=self.start,
            live_pv_power_w=8000.0,
            forecast_pv_power_w=5000.0,
            max_grid_export_w=5000.0,
            error_decay_hours=2.0,
            maximum_error_w=4000.0,
        )

        self.assertEqual(result.observed_pv_error_w, 3000.0)
        self.assertEqual(len(result.requirements), 2)
        self.assertAlmostEqual(result.requirements[0][1], 1.0)
        self.assertAlmostEqual(result.requirements[1][1], 1.625)

    def test_high_side_headroom_offsets_baseline_import_before_counting_export(self):
        slot = ExportSchedulerSlot(
            period_start=self.start,
            duration_hours=0.5,
            price_per_kwh=0.20,
            baseline_setpoint_w=1000.0,
            baseline_battery_power_w=0.0,
            baseline_grid_export_w=0.0,
            intentional_grid_import_w=1000.0,
        )
        result = export_scheduler.build_high_side_headroom_requirements(
            slots=[slot],
            period_ends={slot.period_start.isoformat(): slot.period_start + timedelta(minutes=30)},
            current_time=self.start,
            live_pv_power_w=8000.0,
            forecast_pv_power_w=4000.0,
            max_grid_export_w=3500.0,
        )

        self.assertEqual(result.requirements, [])
        self.assertAlmostEqual(result.peak_high_side_natural_export_w, 3000.0)

    def test_high_side_headroom_is_netted_against_free_battery_room(self):
        slot = ExportSchedulerSlot(
            period_start=self.start,
            duration_hours=0.5,
            price_per_kwh=0.20,
            baseline_setpoint_w=0.0,
            baseline_battery_power_w=0.0,
            baseline_grid_export_w=4000.0,
        )
        base = dict(
            slots=[slot],
            period_ends={
                slot.period_start.isoformat(): slot.period_start
                + timedelta(minutes=30)
            },
            current_time=self.start,
            live_pv_power_w=8000.0,
            forecast_pv_power_w=4000.0,
            max_grid_export_w=3500.0,
            max_battery_charge_w=1000.0,
        )

        ample = export_scheduler.build_high_side_headroom_requirements(
            **(base | {"battery_free_capacity_kwh": 17.0})
        )
        self.assertEqual(ample.requirements, [])

        partial = export_scheduler.build_high_side_headroom_requirements(
            **(base | {"battery_free_capacity_kwh": 0.2})
        )
        self.assertEqual(len(partial.requirements), 1)
        self.assertAlmostEqual(partial.requirements[0][1], 0.3)

        unknown = export_scheduler.build_high_side_headroom_requirements(
            **(base | {"battery_free_capacity_kwh": None})
        )
        self.assertEqual(len(unknown.requirements), 1)
        self.assertAlmostEqual(unknown.requirements[0][1], 0.5)

    def test_high_side_headroom_is_bounded_by_remaining_charge_power(self):
        slot = ExportSchedulerSlot(
            period_start=self.start,
            duration_hours=0.5,
            price_per_kwh=0.10,
            baseline_setpoint_w=0.0,
            baseline_battery_power_w=1800.0,
            baseline_grid_export_w=0.0,
        )
        plan = export_scheduler.build_high_side_headroom_requirements(
            slots=[slot],
            period_ends={
                slot.period_start.isoformat(): slot.period_start
                + timedelta(minutes=30)
            },
            current_time=self.start,
            live_pv_power_w=8000.0,
            forecast_pv_power_w=4000.0,
            max_grid_export_w=3500.0,
            max_battery_charge_w=1800.0,
        )

        self.assertEqual(plan.requirements, [])

    def test_high_side_headroom_is_bounded_by_battery_charge_power(self):
        slot = ExportSchedulerSlot(
            period_start=self.start,
            duration_hours=0.5,
            price_per_kwh=0.20,
            baseline_setpoint_w=0.0,
            baseline_battery_power_w=0.0,
            baseline_grid_export_w=4000.0,
        )
        result = export_scheduler.build_high_side_headroom_requirements(
            slots=[slot],
            period_ends={slot.period_start.isoformat(): slot.period_start + timedelta(minutes=30)},
            current_time=self.start,
            live_pv_power_w=8000.0,
            forecast_pv_power_w=4000.0,
            max_grid_export_w=3500.0,
            max_battery_charge_w=1000.0,
        )

        self.assertEqual(len(result.requirements), 1)
        self.assertAlmostEqual(result.requirements[0][1], 0.5)

    def test_high_side_headroom_rejects_nonfinite_or_implausible_live_pv(self):
        slot = ExportSchedulerSlot(
            period_start=self.start,
            duration_hours=0.5,
            price_per_kwh=0.20,
            baseline_setpoint_w=0.0,
            baseline_battery_power_w=0.0,
            baseline_grid_export_w=4000.0,
        )
        base = dict(
            slots=[slot],
            period_ends={
                slot.period_start.isoformat(): slot.period_start
                + timedelta(minutes=30)
            },
            current_time=self.start,
            forecast_pv_power_w=4000.0,
            max_grid_export_w=3500.0,
            max_battery_charge_w=1000.0,
            maximum_live_pv_power_w=20000.0,
        )

        for invalid_pv in (float("nan"), float("inf"), 1_000_000_000.0):
            with self.subTest(invalid_pv=invalid_pv):
                result = export_scheduler.build_high_side_headroom_requirements(
                    **(base | {"live_pv_power_w": invalid_pv})
                )
                self.assertEqual(result.requirements, [])
                self.assertEqual(result.observed_pv_error_w, 0.0)

    def test_live_export_target_respects_quantitative_ev_adjusted_budget(self):
        limit_target = export_scheduler.limit_live_export_target_to_budget

        self.assertEqual(
            limit_target(
                desired_grid_target_w=-4000.0,
                pv_only_grid_target_w=-500.0,
                safe_export_energy_kwh=0.0,
                budget_horizon_seconds=120.0,
            ),
            -500.0,
        )
        self.assertEqual(
            limit_target(
                desired_grid_target_w=-4000.0,
                pv_only_grid_target_w=-500.0,
                safe_export_energy_kwh=0.05,
                budget_horizon_seconds=120.0,
            ),
            -2000.0,
        )
        self.assertEqual(
            limit_target(
                desired_grid_target_w=-4000.0,
                pv_only_grid_target_w=-500.0,
                safe_export_energy_kwh=1.0,
                budget_horizon_seconds=120.0,
                max_battery_discharge_w=0.0,
            ),
            -500.0,
        )
        self.assertEqual(
            limit_target(
                desired_grid_target_w=-4000.0,
                pv_only_grid_target_w=-500.0,
                safe_export_energy_kwh=1.0,
                budget_horizon_seconds=120.0,
                max_battery_discharge_w=500.0,
            ),
            -1000.0,
        )

    def test_headroom_counts_only_overflow_the_battery_can_absorb(self):
        absorbable = export_scheduler.calculate_absorbable_overflow_power

        self.assertEqual(
            absorbable(
                overflow_power_w=2000.0,
                planned_battery_charge_power_w=1800.0,
                max_battery_charge_power_w=1800.0,
            ),
            0.0,
        )
        self.assertEqual(
            absorbable(
                overflow_power_w=2000.0,
                planned_battery_charge_power_w=0.0,
                max_battery_charge_power_w=1000.0,
            ),
            1000.0,
        )

    def test_time_to_full_uses_stored_charge_rate(self):
        self.assertTrue(hasattr(export_scheduler, "calculate_time_to_full_minutes"))
        minutes = export_scheduler.calculate_time_to_full_minutes(
            battery_free_capacity_kwh=2.3,
            battery_charge_power_w=6620.0,
            charge_efficiency=0.90,
        )

        self.assertAlmostEqual(minutes, 23.1621, places=3)

    def test_live_headroom_state_activates_before_imminent_battery_saturation(self):
        self.assertIn(
            "time_to_full_minutes",
            inspect.signature(update_live_headroom_state).parameters,
        )
        state = update_live_headroom_state(
            current_time=self.start,
            previous_active=False,
            previous_target_headroom_kwh=0.0,
            previous_hold_until=None,
            requested_headroom_kwh=0.65,
            battery_free_capacity_kwh=2.3,
            natural_export_w=6600.0,
            preferred_pv_export_w=3000.0,
            time_to_full_minutes=23.0,
            activation_lead_time_minutes=60.0,
        )

        self.assertTrue(state.active)
        self.assertEqual(state.target_headroom_kwh, 0.65)

    def test_live_headroom_state_activates_near_capacity_and_holds_through_dip(self):
        activated = update_live_headroom_state(
            current_time=self.start,
            previous_active=False,
            previous_target_headroom_kwh=0.0,
            previous_hold_until=None,
            requested_headroom_kwh=0.15,
            battery_free_capacity_kwh=0.13,
            natural_export_w=3600.0,
            preferred_pv_export_w=3000.0,
        )
        dipped = update_live_headroom_state(
            current_time=self.start + timedelta(minutes=5),
            previous_active=activated.active,
            previous_target_headroom_kwh=activated.target_headroom_kwh,
            previous_hold_until=activated.hold_until,
            requested_headroom_kwh=0.0,
            battery_free_capacity_kwh=0.20,
            natural_export_w=1000.0,
            preferred_pv_export_w=3000.0,
        )

        self.assertTrue(activated.active)
        self.assertTrue(dipped.active)
        self.assertEqual(dipped.target_headroom_kwh, 0.15)
        self.assertEqual(dipped.hold_until, activated.hold_until)

    def test_live_headroom_state_does_not_activate_with_ample_capacity(self):
        state = update_live_headroom_state(
            current_time=self.start,
            previous_active=False,
            previous_target_headroom_kwh=0.0,
            previous_hold_until=None,
            requested_headroom_kwh=0.15,
            battery_free_capacity_kwh=2.0,
            natural_export_w=3600.0,
            preferred_pv_export_w=3000.0,
        )

        self.assertFalse(state.active)

    def test_live_headroom_state_releases_after_hold_and_hysteresis(self):
        state = update_live_headroom_state(
            current_time=self.start + timedelta(minutes=16),
            previous_active=True,
            previous_target_headroom_kwh=0.4,
            previous_hold_until=self.start + timedelta(minutes=15),
            requested_headroom_kwh=0.0,
            battery_free_capacity_kwh=0.4,
            natural_export_w=2000.0,
            preferred_pv_export_w=3000.0,
        )

        self.assertFalse(state.active)
        self.assertEqual(state.target_headroom_kwh, 0.0)
        self.assertIsNone(state.hold_until)

    def test_live_headroom_state_releases_after_risk_free_expiry(self):
        # 2026-09-25 17:08: natural export sat at 2841 W — below the 3000 W
        # preferred target (no current risk) but above the 2500 W release
        # threshold — so the supervisor never released: the stale 0.628 kWh
        # target kept the battery pinned at 98.3% and even discharged it to
        # "create" room for a risk that no longer existed.  Fifteen
        # continuous minutes without risk must release on the timer alone;
        # re-engagement covers risk that actually returns.
        released = update_live_headroom_state(
            current_time=self.start + timedelta(minutes=16),
            previous_active=True,
            previous_target_headroom_kwh=0.628,
            previous_hold_until=self.start + timedelta(minutes=15),
            requested_headroom_kwh=0.0,
            battery_free_capacity_kwh=0.54,
            natural_export_w=2841.0,
            preferred_pv_export_w=3000.0,
        )

        self.assertFalse(released.active)
        self.assertEqual(released.target_headroom_kwh, 0.0)
        self.assertIsNone(released.hold_until)

    def test_live_headroom_state_holds_through_short_dip_near_threshold(self):
        # The timer-only release must not give up the room during a short
        # cloud dip just under the preferred target: unexpired holds stay
        # latched with their target intact.
        dipped = update_live_headroom_state(
            current_time=self.start + timedelta(minutes=5),
            previous_active=True,
            previous_target_headroom_kwh=0.628,
            previous_hold_until=self.start + timedelta(minutes=15),
            requested_headroom_kwh=0.0,
            battery_free_capacity_kwh=0.54,
            natural_export_w=2841.0,
            preferred_pv_export_w=3000.0,
        )

        self.assertTrue(dipped.active)
        self.assertEqual(dipped.target_headroom_kwh, 0.628)
        self.assertEqual(
            dipped.hold_until, self.start + timedelta(minutes=15)
        )

    def test_live_headroom_state_raises_but_does_not_lower_active_target(self):
        raised = update_live_headroom_state(
            current_time=self.start,
            previous_active=True,
            previous_target_headroom_kwh=0.15,
            previous_hold_until=self.start + timedelta(minutes=10),
            requested_headroom_kwh=0.40,
            battery_free_capacity_kwh=0.10,
            natural_export_w=5000.0,
            preferred_pv_export_w=3000.0,
        )
        lower_risk = update_live_headroom_state(
            current_time=self.start + timedelta(minutes=1),
            previous_active=raised.active,
            previous_target_headroom_kwh=raised.target_headroom_kwh,
            previous_hold_until=raised.hold_until,
            requested_headroom_kwh=0.10,
            battery_free_capacity_kwh=0.20,
            natural_export_w=3300.0,
            preferred_pv_export_w=3000.0,
        )

        self.assertEqual(raised.target_headroom_kwh, 0.40)
        self.assertEqual(lower_risk.target_headroom_kwh, 0.40)

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

    def test_active_drive_reserves_only_the_ev_adjusted_energy(self):
        self.assertTrue(hasattr(export_scheduler, "calculate_export_energy_budget"))
        budget = export_scheduler.calculate_export_energy_budget(
            physically_available_energy_kwh=18.284,
            spendable_energy_before_ev_kwh=23.9,
            spendable_energy_after_ev_kwh=21.97,
        )

        self.assertAlmostEqual(budget.ev_reservation_energy_kwh, 1.93)
        self.assertEqual(budget.safe_export_energy_kwh, 18.284)

    def test_ev_reservation_caps_export_when_physical_energy_is_abundant(self):
        budget = export_scheduler.calculate_export_energy_budget(
            physically_available_energy_kwh=30.0,
            spendable_energy_before_ev_kwh=23.9,
            spendable_energy_after_ev_kwh=21.97,
        )

        self.assertAlmostEqual(budget.ev_reservation_energy_kwh, 1.93)
        self.assertAlmostEqual(budget.safe_export_energy_kwh, 21.97)

    def test_high_price_active_ev_may_use_battery_to_hold_grid_neutral(self):
        self.assertTrue(
            hasattr(export_scheduler, "select_ev_discharge_guard_target")
        )
        target = export_scheduler.select_ev_discharge_guard_target(
            no_battery_discharge_target_w=7500.0,
            neutral_grid_target_w=-100.0,
            ev_is_charging=True,
            electricity_price=0.2875,
            min_discharge_price=0.212,
            battery_energy_kwh=15.81,
            protected_battery_floor_kwh=2.6,
            telemetry_valid=True,
        )

        self.assertEqual(target, -100.0)
        self.assertEqual(
            export_scheduler.limit_live_export_target_to_budget(
                desired_grid_target_w=-100.0,
                pv_only_grid_target_w=target,
                safe_export_energy_kwh=0.0,
                budget_horizon_seconds=120.0,
                max_battery_discharge_w=8000.0,
            ),
            -100.0,
        )

    def test_ev_battery_assist_fails_closed_and_never_creates_export(self):
        base = {
            "no_battery_discharge_target_w": 7500.0,
            "neutral_grid_target_w": -100.0,
            "ev_is_charging": True,
            "electricity_price": 0.2875,
            "min_discharge_price": 0.212,
            "battery_energy_kwh": 15.81,
            "protected_battery_floor_kwh": 2.6,
            "telemetry_valid": True,
        }
        # Battery at/below floor, uneconomical price, and no EV charging
        # still fail closed to the no-battery import hold. A bare telemetry
        # gap while the EV charges deliberately does not: it used to swing
        # the guard between neutral and a kilowatt-scale live import hold on
        # every stale sensor cycle (setpoint oscillation), and the caller
        # already clamps the final setpoint to at most neutral.
        blocked_cases = (
            {"electricity_price": 0.10},
            {"battery_energy_kwh": 2.6},
            {"ev_is_charging": False},
            {"ev_is_charging": False, "telemetry_valid": False},
            {"electricity_price": float("nan")},
        )
        for overrides in blocked_cases:
            with self.subTest(overrides=overrides):
                arguments = dict(base)
                arguments.update(overrides)
                self.assertEqual(
                    export_scheduler.select_ev_discharge_guard_target(**arguments),
                    7500.0,
                )

        ride_through_cases = (
            {"telemetry_valid": False},
            {"telemetry_valid": False, "no_battery_discharge_target_w": 0.0},
        )
        for overrides in ride_through_cases:
            with self.subTest(overrides=overrides):
                arguments = dict(base)
                arguments.update(overrides)
                self.assertEqual(
                    export_scheduler.select_ev_discharge_guard_target(**arguments),
                    min(arguments["no_battery_discharge_target_w"], -100.0),
                )

        natural_export_target = dict(base)
        natural_export_target["no_battery_discharge_target_w"] = -1800.0
        self.assertEqual(
            export_scheduler.select_ev_discharge_guard_target(
                **natural_export_target
            ),
            -1800.0,
        )

        for invalid_target in (None, "invalid", float("nan"), float("inf")):
            with self.subTest(invalid_target=invalid_target):
                invalid_no_battery_target = dict(base)
                invalid_no_battery_target["no_battery_discharge_target_w"] = (
                    invalid_target
                )
                self.assertIsNone(
                    export_scheduler.select_ev_discharge_guard_target(
                        **invalid_no_battery_target
                    )
                )

    def test_ev_battery_assist_helper_is_pyscript_compatible(self):
        source = inspect.getsource(
            export_scheduler.select_ev_discharge_guard_target
        )
        tree = ast.parse(source)

        self.assertFalse(
            any(isinstance(node, ast.GeneratorExp) for node in ast.walk(tree))
        )

    def test_active_ev_guard_blocks_export_and_cheap_price_budget_bypass(self):
        self.assertTrue(hasattr(export_scheduler, "apply_ev_discharge_guard"))

        self.assertEqual(
            export_scheduler.apply_ev_discharge_guard(
                desired_grid_target_w=-1000.0,
                discharge_guard_target_w=-100.0,
                guard_active=True,
            ),
            -100.0,
        )
        cheap_price_target = export_scheduler.apply_ev_discharge_guard(
            desired_grid_target_w=-100.0,
            discharge_guard_target_w=7526.2,
            guard_active=True,
        )
        self.assertEqual(cheap_price_target, 7526.2)
        self.assertEqual(
            export_scheduler.limit_live_export_target_to_budget(
                desired_grid_target_w=cheap_price_target,
                pv_only_grid_target_w=7526.2,
                safe_export_energy_kwh=1.0,
                budget_horizon_seconds=120.0,
                max_battery_discharge_w=8000.0,
            ),
            7526.2,
        )

    def test_active_ev_guard_uses_conservative_telemetry_fallback(self):
        self.assertTrue(hasattr(export_scheduler, "apply_ev_discharge_guard"))
        self.assertEqual(
            export_scheduler.apply_ev_discharge_guard(
                desired_grid_target_w=-100.0,
                discharge_guard_target_w=10000.0,
                guard_active=True,
            ),
            10000.0,
        )

    def test_historical_ev_grid_import_incident_replays_grid_neutral(self):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "ev_grid_import_incident_2026-09-12.json"
        )
        incident = json.loads(fixture_path.read_text())
        measurements = incident["measurements"]
        policy = incident["policy"]
        ev = incident["ev"]
        charging_duration_seconds = (
            datetime.fromisoformat(incident["charging_stopped_at"])
            - datetime.fromisoformat(incident["charging_started_at"])
        ).total_seconds()
        self.assertGreater(charging_duration_seconds, 60.0)
        self.assertLess(charging_duration_seconds, 90.0)
        self.assertGreater(
            measurements["wallbox_power_w"],
            measurements["pv_power_w"],
        )
        self.assertGreater(measurements["grid_power_w"], 0.0)
        self.assertLess(ev["energy_kwh"], ev["smart_charge_target_kwh"])
        self.assertTrue(ev["able_to_charge_on_arrival"])
        no_battery_target_w = (
            measurements["grid_power_w"] - measurements["battery_power_w"]
        )
        self.assertAlmostEqual(
            measurements["applied_victron_setpoint_w"],
            no_battery_target_w,
            delta=50.0,
        )

        guard_target_w = export_scheduler.select_ev_discharge_guard_target(
            no_battery_discharge_target_w=no_battery_target_w,
            neutral_grid_target_w=policy["neutral_grid_target_w"],
            ev_is_charging=incident["ev"]["is_charging"],
            electricity_price=policy["electricity_price_eur_per_kwh"],
            min_discharge_price=policy["min_discharge_price_eur_per_kwh"],
            battery_energy_kwh=measurements["battery_energy_kwh"],
            protected_battery_floor_kwh=policy[
                "protected_battery_floor_kwh"
            ],
            telemetry_valid=True,
        )
        guarded_target_w = export_scheduler.apply_ev_discharge_guard(
            desired_grid_target_w=-1000.0,
            discharge_guard_target_w=guard_target_w,
            guard_active=True,
        )
        final_target_w = export_scheduler.limit_live_export_target_to_budget(
            desired_grid_target_w=guarded_target_w,
            pv_only_grid_target_w=guard_target_w,
            safe_export_energy_kwh=policy["safe_optional_export_energy_kwh"],
            budget_horizon_seconds=120.0,
            max_battery_discharge_w=policy["max_battery_discharge_w"],
        )

        self.assertEqual(final_target_w, policy["neutral_grid_target_w"])

    def test_malformed_ev_target_selects_finite_actuator_fallback(self):
        self.assertTrue(hasattr(export_scheduler, "select_finite_grid_target"))
        self.assertEqual(
            export_scheduler.select_finite_grid_target(
                candidate_grid_target_w="unavailable",
                fallback_grid_target_w=10000.0,
            ),
            10000.0,
        )

    def test_malformed_unified_plan_returns_no_target_for_fallback(self):
        valid = {
            "planned_battery_power_w": -1000.0,
            "planned_optional_export_power_w": 1000.0,
            "house_load_w": 500.0,
            "pv_power_w": 2500.0,
            "max_grid_export_w": 5500.0,
            "neutral_grid_target_w": -100.0,
        }
        for field in tuple(valid):
            for invalid_value in (None, "unavailable", float("nan")):
                with self.subTest(field=field, invalid_value=invalid_value):
                    arguments = dict(valid)
                    arguments[field] = invalid_value
                    self.assertIsNone(
                        export_scheduler.adapt_grid_target_to_live_power(
                            **arguments
                        )
                    )

    def test_arrival_precharge_is_inactive_while_ev_is_away(self):
        self.assertTrue(hasattr(export_scheduler, "is_ev_arrival_precharge_active"))
        active = export_scheduler.is_ev_arrival_precharge_active(
            able_to_charge_on_arrival=True,
            charger_ready=False,
            ev_plugged_in=False,
            ev_is_charging=False,
            ev_energy_kwh=39.0,
            target_energy_kwh=60.0,
        )

        self.assertFalse(active)

    def test_arrival_precharge_is_active_for_present_ev_below_target(self):
        active = export_scheduler.is_ev_arrival_precharge_active(
            able_to_charge_on_arrival=True,
            charger_ready=True,
            ev_plugged_in=False,
            ev_is_charging=False,
            ev_energy_kwh=39.0,
            target_energy_kwh=60.0,
        )

        self.assertTrue(active)

    def test_arrival_precharge_is_inactive_at_target_energy(self):
        active = export_scheduler.is_ev_arrival_precharge_active(
            able_to_charge_on_arrival=True,
            charger_ready=False,
            ev_plugged_in=True,
            ev_is_charging=False,
            ev_energy_kwh=60.0,
            target_energy_kwh=60.0,
        )

        self.assertFalse(active)

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
        battery_now=None,
        battery_capacity=None,
        neutral_grid_target_w=-100.0,
        battery_charge_eff=1.0,
        battery_discharge_eff=1.0,
        top_up_pv=1.0,
        allow_economic=True,
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
            current_battery_energy_kwh=battery_now,
            battery_capacity_kwh=battery_capacity,
            neutral_grid_target_w=neutral_grid_target_w,
            battery_charge_efficiency=battery_charge_eff,
            battery_discharge_efficiency=battery_discharge_eff,
            top_up_pv_confidence_factor=top_up_pv,
            allow_economic_export=allow_economic,
        )

    def test_economic_export_requires_baseline_spill(self):
        charging = self.slot(0, 0.30, hour=10, battery_power=3000.0, grid_export=0.0)
        spilling = self.slot(1, 0.12, hour=12, battery_power=0.0, grid_export=4000.0)
        evening = self.slot(2, 0.50, hour=19, battery_power=-2000.0, grid_export=0.0)
        plan = self.plan([charging, spilling, evening], energy=2.0)
        delta = plan.battery_power_delta_schedule

        # Exporting while the battery still absorbs the surplus would divert
        # charging energy for pure arbitrage. Only the spilling interval,
        # where the battery cannot take more, is eligible — even at the
        # highest evening price with sun still ahead that day.
        self.assertEqual(delta[charging.period_start.isoformat()], 0.0)
        self.assertEqual(delta[evening.period_start.isoformat()], 0.0)
        self.assertAlmostEqual(delta[spilling.period_start.isoformat()], 1500.0)
        self.assertAlmostEqual(plan.economic_energy_kwh, 0.375)

    def test_end_of_day_top_up_trims_late_arbitrage_until_full(self):
        early_spill = self.slot(0, 0.40, hour=14, battery_power=0.0, grid_export=3000.0)
        late_spill = self.slot(1, 0.50, hour=16, battery_power=0.0, grid_export=2000.0)
        evening = self.slot(2, 0.60, hour=18, battery_power=-1000.0, grid_export=0.0)
        late_surplus = self.slot(3, 0.10, hour=19, battery_power=1000.0, grid_export=0.0)
        slots = [early_spill, late_spill, evening, late_surplus]
        clamped = self.plan(slots, energy=2.0, battery_now=32.0, battery_capacity=32.0)
        unclamped = self.plan(slots, energy=2.0)
        clamped_delta = clamped.battery_power_delta_schedule
        unclamped_delta = unclamped.battery_power_delta_schedule

        # Without the top-up rule the late spill peak takes the budget.
        self.assertGreater(unclamped_delta[late_spill.period_start.isoformat()], 0.0)
        # The battery starts full, so any export breaks the end of day and
        # the latest spill yields first until the day ends at 100% again.
        self.assertEqual(clamped_delta[late_spill.period_start.isoformat()], 0.0)
        self.assertEqual(clamped_delta[early_spill.period_start.isoformat()], 0.0)
        self.assertAlmostEqual(clamped.economic_energy_kwh, 0.0)

    def test_end_of_day_top_up_skips_days_the_sun_cannot_fill(self):
        spike = self.slot(0, 0.30, hour=10, battery_power=6600.0, grid_export=1500.0)
        plan = self.plan(
            [spike],
            energy=2.0,
            battery_now=10.0,
            battery_capacity=32.0,
        )
        delta = plan.battery_power_delta_schedule

        # The baseline never reaches full, so the spilling spike keeps its
        # economic allocation untouched.
        self.assertAlmostEqual(delta[spike.period_start.isoformat()], 4000.0)
        self.assertAlmostEqual(plan.economic_energy_kwh, 1.0)

    def test_top_up_clamp_discounts_refill_against_forecast_shortfall(self):
        morning_spill = self.slot(0, 0.50, hour=9, battery_power=0.0, grid_export=3000.0)
        midday_charge = self.slot(1, 0.05, hour=12, battery_power=8000.0, grid_export=0.0)
        slots = [morning_spill, midday_charge]
        trusted = self.plan(slots, energy=2.0, battery_now=30.5, battery_capacity=32.0, top_up_pv=1.0)
        discounted = self.plan(slots, energy=2.0, battery_now=30.5, battery_capacity=32.0, top_up_pv=0.75)
        trusted_w = trusted.battery_power_delta_schedule[morning_spill.period_start.isoformat()]
        discounted_w = discounted.battery_power_delta_schedule[morning_spill.period_start.isoformat()]

        # Baseline refill is 2.0 kWh, exactly enough to cover the morning
        # pre-release and still touch full — but only if the forecast is
        # exact.  The discounted trajectory (1.5 kWh refill) exposes the
        # larger deficit and trims the morning sale harder.
        self.assertGreater(trusted_w, 0.0)
        self.assertLess(discounted_w, trusted_w)
        self.assertLess(discounted.economic_energy_kwh, trusted.economic_energy_kwh)

    def test_fill_classification_ignores_top_up_haircut(self):
        # The refill barely touches full on best estimate (30.5 + 1.8) but
        # misses it discounted (30.5 + 1.35).  The day must still count as
        # a fill day — otherwise its (physical, hard-cap) headroom
        # requirements are dropped while the clamp keeps trimming with
        # margin.  Regression test for 2026-09-22: tomorrow's 9.84 kWh
        # spill requirement allocated 0.00 kWh and the replay exceeded
        # the hard export cap.
        spill = self.slot(0, 0.50, hour=9, battery_power=0.0, grid_export=3000.0)
        charge = self.slot(1, 0.05, hour=12, battery_power=7200.0, grid_export=0.0)
        slots = [spill, charge]
        deadline = charge.period_start + timedelta(minutes=15)
        plan = self.plan(
            slots,
            energy=2.0,
            headroom_requirements=[(deadline, 0.3)],
            battery_now=30.5,
            battery_capacity=32.0,
            top_up_pv=0.75,
        )

        self.assertAlmostEqual(plan.headroom_energy_kwh, 0.3)
        self.assertAlmostEqual(plan.economic_energy_kwh, 0.0)

    def test_balance_chase_disables_economic_export_only(self):
        morning_spill = self.slot(0, 0.50, hour=9, battery_power=0.0, grid_export=3000.0)
        midday_spill = self.slot(1, 0.40, hour=12, battery_power=0.0, grid_export=3000.0)
        slots = [morning_spill, midday_spill]
        deadline = midday_spill.period_start + timedelta(minutes=15)
        kwargs = {
            "energy": 2.0,
            "headroom_requirements": [(deadline, 0.3)],
        }
        normal = self.plan(slots, allow_economic=True, **kwargs)
        chase = self.plan(slots, allow_economic=False, **kwargs)

        # Physical overflow protection still allocates in chase mode, but
        # no price-ranked arbitrage may drain the top-up.
        self.assertGreater(normal.economic_energy_kwh, 0.0)
        self.assertAlmostEqual(chase.economic_energy_kwh, 0.0)
        self.assertGreater(chase.headroom_energy_kwh, 0.0)
        self.assertAlmostEqual(chase.headroom_energy_kwh, normal.headroom_energy_kwh)

    def test_headroom_deadline_applies_without_battery_state(self):
        morning = self.slot(0, 0.30, hour=8, battery_power=3000.0, grid_export=0.0)
        evening = self.slot(1, 0.50, hour=19, battery_power=-2000.0, grid_export=0.0)
        plan = self.plan(
            [morning, evening],
            energy=2.0,
            headroom_requirements=[
                (evening.period_start + timedelta(minutes=15), 0.3)
            ],
        )
        delta = plan.battery_power_delta_schedule

        # Without battery state the touch check is skipped: requirements stay.
        self.assertGreater(delta[evening.period_start.isoformat()], 0.0)

    def test_headroom_requirement_drops_when_day_cannot_fill(self):
        morning = self.slot(0, 0.30, hour=10, battery_power=3000.0, grid_export=0.0)
        midday = self.slot(1, 0.12, hour=12, battery_power=3000.0, grid_export=0.0)
        deadline = midday.period_start + timedelta(hours=7)
        kwargs = {
            "energy": 2.0,
            "headroom_requirements": [(deadline, 0.5)],
            "battery_capacity": 32.0,
            "battery_charge_eff": 0.9,
            "battery_discharge_eff": 0.9,
        }
        short = self.plan([morning, midday], battery_now=10.0, **kwargs)
        full = self.plan([morning, midday], battery_now=31.0, **kwargs)
        short_delta = short.battery_power_delta_schedule
        full_delta = full.battery_power_delta_schedule

        # Baseline 10 + 2 x 0.75 x 0.9 = 11.35: pre-releasing would only
        # drain a top-up that never completes.
        self.assertEqual(short_delta[morning.period_start.isoformat()], 0.0)
        self.assertEqual(short_delta[midday.period_start.isoformat()], 0.0)
        self.assertAlmostEqual(short.headroom_energy_kwh, 0.0)
        # Baseline 31 + 1.35 touches full: physical protection stays.
        self.assertAlmostEqual(full_delta[morning.period_start.isoformat()], 2000.0)
        self.assertAlmostEqual(full.headroom_energy_kwh, 0.5)

    def test_headroom_touch_uses_battery_efficiencies(self):
        morning = self.slot(0, 0.30, hour=10, battery_power=3000.0, grid_export=0.0)
        midday = self.slot(1, 0.12, hour=12, battery_power=3000.0, grid_export=0.0)
        deadline = midday.period_start + timedelta(hours=7)
        kwargs = {
            "energy": 2.0,
            "headroom_requirements": [(deadline, 0.5)],
            "battery_now": 30.6,
            "battery_capacity": 32.0,
            "battery_discharge_eff": 0.9,
        }
        lossless = self.plan([morning, midday], battery_charge_eff=1.0, **kwargs)
        lossy = self.plan([morning, midday], battery_charge_eff=0.9, **kwargs)

        # 30.6 + 1.50 touches, 30.6 + 1.35 does not.
        self.assertAlmostEqual(
            lossless.battery_power_delta_schedule[morning.period_start.isoformat()],
            2000.0,
        )
        self.assertEqual(
            lossy.battery_power_delta_schedule[morning.period_start.isoformat()], 0.0
        )

    def test_explicit_empty_bounded_headroom_does_not_recreate_raw_requirement(self):
        slot = self.slot(0, 0.0)
        plan = self.plan(
            [slot],
            energy=0.5,
            headroom=0.5,
            deadline=slot.period_start + timedelta(minutes=15),
            headroom_requirements=[],
            hard_headroom_requirements=[],
        )

        self.assertEqual(plan.headroom_energy_kwh, 0.0)
        self.assertEqual(
            plan.battery_power_delta_schedule[slot.period_start.isoformat()],
            0.0,
        )

    def test_evening_incident_replay_uses_the_highest_price_buckets(self):
        slots = [
            self.slot(0, 0.32663),
            self.slot(1, 0.33885),
            self.slot(2, 0.27002),
            self.slot(3, 0.21348),
        ]
        plan = self.plan(slots, energy=2.0, neutral_grid_target_w=0.0)

        self.assertLess(plan.grid_target_schedule[slots[1].period_start.isoformat()], -3500.0)
        self.assertEqual(plan.grid_target_schedule[slots[0].period_start.isoformat()], -3600.0)
        self.assertEqual(plan.grid_target_schedule[slots[2].period_start.isoformat()], -100.0)
        self.assertEqual(plan.grid_target_schedule[slots[3].period_start.isoformat()], -100.0)

    def test_quiet_tolerance_prefers_two_efficient_bands_over_one_noisy_boost(self):
        lower = self.slot(0, 0.32663)
        higher = self.slot(1, 0.33885)
        plan = self.plan([lower, higher], energy=1.75, tolerance=0.05, neutral_grid_target_w=0.0)

        self.assertEqual(plan.grid_target_schedule[lower.period_start.isoformat()], -3600.0)
        self.assertEqual(plan.grid_target_schedule[higher.period_start.isoformat()], -3600.0)

    def test_quiet_efficiency_window_includes_the_full_23_hour(self):
        lower = self.slot(0, 0.32663, hour=23)
        higher = self.slot(1, 0.33885, hour=23)
        plan = self.plan([lower, higher], energy=1.75, tolerance=0.05, neutral_grid_target_w=0.0)

        self.assertEqual(plan.grid_target_schedule[lower.period_start.isoformat()], -3600.0)
        self.assertEqual(plan.grid_target_schedule[higher.period_start.isoformat()], -3600.0)

    def test_quiet_efficiency_window_includes_the_midnight_hour(self):
        midnight = self.slot(0, 0.17430, hour=0)
        half_past = self.slot(1, 0.17430, hour=0)
        plan = self.plan([midnight, half_past], energy=1.75, tolerance=0.15, neutral_grid_target_w=0.0)

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
        plan = self.plan([quiet, midnight], energy=0.875, tolerance=0.15, neutral_grid_target_w=0.0)

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
        plan = self.plan([quiet, midnight], energy=0.875, tolerance=0.15, neutral_grid_target_w=0.0)

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
        plan = self.plan([lower, higher], energy=1.35, tolerance=0.0, neutral_grid_target_w=0.0)

        self.assertEqual(plan.grid_target_schedule[higher.period_start.isoformat()], -5500.0)
        self.assertEqual(plan.grid_target_schedule[lower.period_start.isoformat()], -100.0)

    def test_daytime_has_no_quiet_power_penalty(self):
        high = self.slot(0, 0.30, hour=12)
        low = self.slot(1, 0.29, hour=12)
        plan = self.plan([high, low], energy=1.35, neutral_grid_target_w=0.0)

        self.assertEqual(plan.grid_target_schedule[high.period_start.isoformat()], -5500.0)
        self.assertEqual(plan.grid_target_schedule[low.period_start.isoformat()], -100.0)

    def test_ev_charging_slot_is_not_used_for_battery_export(self):
        ev_slot = self.slot(0, 0.40, ev_power=3680.0)
        other_slot = self.slot(1, 0.20)
        plan = self.plan([ev_slot, other_slot], energy=0.875, neutral_grid_target_w=0.0)

        self.assertEqual(plan.grid_target_schedule[ev_slot.period_start.isoformat()], -100.0)
        self.assertEqual(plan.grid_target_schedule[other_slot.period_start.isoformat()], -3600.0)

    def test_intentional_grid_charge_slot_is_not_used_for_export(self):
        import_slot = self.slot(0, 0.40, grid_import=4000.0)
        other_slot = self.slot(1, 0.20)
        plan = self.plan([import_slot, other_slot], energy=0.875, neutral_grid_target_w=0.0)

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
            deadline=flat_price.period_start + timedelta(minutes=15),
            hard_headroom_requirements=[
                (flat_price.period_start + timedelta(minutes=15), 0.875),
            ],
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

    def test_price_ranked_headroom_avoids_low_price_noon_slot(self):
        morning_high_price = self.slot(0, 0.40, hour=9)
        noon_low_price = self.slot(1, -0.01, hour=12)
        deadline = noon_low_price.period_start + timedelta(minutes=15)
        plan = self.plan(
            [morning_high_price, noon_low_price],
            energy=0.875,
            headroom_requirements=[(deadline, 0.875)],
            hard_headroom_requirements=[],
        )

        self.assertEqual(
            plan.battery_power_delta_schedule[
                morning_high_price.period_start.isoformat()
            ],
            3500.0,
        )
        self.assertEqual(
            plan.battery_power_delta_schedule[noon_low_price.period_start.isoformat()],
            0.0,
        )

    def test_price_ranked_headroom_is_held_through_intervening_recharge(self):
        morning_high_price = self.slot(0, 0.40)
        intervening_recharge = self.slot(
            1,
            -0.01,
            battery_power=2500.0,
            grid_export=1000.0,
        )
        residual_peak = self.slot(2, -0.02)
        deadline = residual_peak.period_start + timedelta(minutes=15)

        plan = self.plan(
            [morning_high_price, intervening_recharge, residual_peak],
            energy=0.875,
            headroom_requirements=[(deadline, 0.875)],
            hard_headroom_requirements=[],
        )

        self.assertEqual(
            plan.battery_power_delta_schedule[
                morning_high_price.period_start.isoformat()
            ],
            3500.0,
        )
        self.assertNotIn(
            intervening_recharge.period_start.isoformat(),
            plan.headroom_hold_schedule,
        )

    def test_horizon_reservation_reports_first_uncovered_departure(self):
        now = datetime(2026, 9, 14, 20, 30, tzinfo=timezone.utc)

        def event(start, required_soc=None):
            entry = SimpleNamespace(start=start, required_soc=required_soc)
            return entry

        def entry(start, ev_energy):
            return SimpleNamespace(period_start=start, ev_energy=ev_energy)

        base_detail = [
            entry(now + timedelta(hours=h), 53.4) for h in (1, 5, 10, 20, 30)
        ]
        wednesday = now + timedelta(hours=34)
        shortfall, start = export_scheduler.first_uncovered_departure_need(
            base_detail,
            [event(wednesday, 100.0)],
            now,
            80.0,
            70.0,
            60.0,
        )
        self.assertAlmostEqual(shortfall, 6.6)
        self.assertEqual(start, wednesday)

    def test_horizon_reservation_skips_covered_past_and_distant_departures(self):
        now = datetime(2026, 9, 14, 20, 30, tzinfo=timezone.utc)

        def event(start, required_soc=None):
            return SimpleNamespace(start=start, required_soc=required_soc)

        def entry(start, ev_energy):
            return SimpleNamespace(period_start=start, ev_energy=ev_energy)

        detail = [entry(now + timedelta(hours=h), 55.0) for h in (1, 10, 30)]
        # Covered near departure is free; the first uncovered one binds.
        near = event(now + timedelta(hours=2), 80.0)
        far = event(now + timedelta(hours=34), 100.0)
        shortfall, start = export_scheduler.first_uncovered_departure_need(
            detail, [near, far], now, 80.0, 70.0, 60.0
        )
        self.assertAlmostEqual(shortfall, 5.0)
        self.assertEqual(start, far.start)
        # Ongoing, past, and beyond-horizon departures never reserve.
        self.assertEqual(
            export_scheduler.first_uncovered_departure_need(
                detail,
                [
                    event(now - timedelta(hours=1), 100.0),
                    event(now, 100.0),
                    event(now + timedelta(hours=100), 100.0),
                ],
                now,
                80.0,
                70.0,
                60.0,
            ),
            (0.0, None),
        )
        # Default SOC applies when the entry carries no requirement.
        shortfall, _ = export_scheduler.first_uncovered_departure_need(
            detail, [event(now + timedelta(hours=34))], now, 80.0, 70.0, 60.0
        )
        self.assertAlmostEqual(shortfall, 0.0)

    def test_horizon_reservation_reduces_export_through_charger_losses(self):
        reserved = export_scheduler.apply_horizon_ev_reservation(13.41, 6.6, 0.9)
        self.assertAlmostEqual(reserved, 13.41 - 6.6 / 0.9)
        self.assertEqual(
            export_scheduler.apply_horizon_ev_reservation(5.0, 6.6, 0.9), 0.0
        )
        self.assertEqual(
            export_scheduler.apply_horizon_ev_reservation(13.41, 0.0, 0.9), 13.41
        )
        for bad in ("x", float("nan"), float("inf"), None):
            self.assertEqual(
                export_scheduler.apply_horizon_ev_reservation(13.41, bad, 0.9), 0.0
            )
        self.assertEqual(
            export_scheduler.apply_horizon_ev_reservation(13.41, 6.6, 0.0), 0.0
        )

    def test_hold_routes_only_surplus_beyond_charge_power(self):
        allocation_slot = self.slot(0, 0.40)
        peak_slot = self.slot(
            1,
            0.05,
            battery_power=0.0,
            grid_export=5000.0,
        )
        deadline = peak_slot.period_start + timedelta(minutes=15)
        requirements = [(deadline, 0.875)]
        delta_schedule = {
            allocation_slot.period_start.isoformat(): 3500.0,
        }

        legacy_hold = export_scheduler.build_headroom_storage_hold_schedule(
            [allocation_slot, peak_slot],
            delta_schedule,
            requirements,
            max_grid_export_w=5500.0,
            neutral_grid_target_w=-100.0,
        )
        self.assertEqual(
            legacy_hold[peak_slot.period_start.isoformat()], 4900.0
        )

        store_first_hold = export_scheduler.build_headroom_storage_hold_schedule(
            [allocation_slot, peak_slot],
            delta_schedule,
            requirements,
            max_grid_export_w=5500.0,
            neutral_grid_target_w=-100.0,
            max_battery_charge_w=6600.0,
        )
        # The whole 5000 W surplus fits within charge power, so nothing is
        # forced to the grid and the battery keeps it all.
        self.assertEqual(
            store_first_hold.get(peak_slot.period_start.isoformat(), 0.0),
            0.0,
        )

        partial_hold = export_scheduler.build_headroom_storage_hold_schedule(
            [allocation_slot, peak_slot],
            delta_schedule,
            requirements,
            max_grid_export_w=5500.0,
            neutral_grid_target_w=-100.0,
            max_battery_charge_w=2000.0,
        )
        # Only the 3000 W beyond charge power is routed out; the
        # chargeable remainder stays for the battery.
        self.assertEqual(
            partial_hold[peak_slot.period_start.isoformat()], 2900.0
        )

    def test_hold_leaves_mild_surplus_unchanged(self):
        allocation_slot = self.slot(0, 0.40)
        mild_slot = self.slot(
            1,
            0.05,
            battery_power=0.0,
            grid_export=1000.0,
        )
        deadline = mild_slot.period_start + timedelta(minutes=15)
        requirements = [(deadline, 0.875)]
        delta_schedule = {
            allocation_slot.period_start.isoformat(): 3500.0,
        }

        mild_hold = export_scheduler.build_headroom_storage_hold_schedule(
            [allocation_slot, mild_slot],
            delta_schedule,
            requirements,
            max_grid_export_w=5500.0,
            neutral_grid_target_w=-100.0,
            max_battery_charge_w=6600.0,
        )
        self.assertEqual(
            mild_hold.get(mild_slot.period_start.isoformat(), 0.0), 0.0
        )

    def test_headroom_shortfall_overflows_to_night_before_bands(self):
        # Same-day discharge strands once morning PV saturates the grid cap
        # (max delta = cap headroom).  Whatever does not fit same-day must
        # move to night-before bands so noon-full stays avoidable.
        night = self.slot(16, 0.30)
        morning1 = self.slot(52, 0.05, battery_power=0.0, grid_export=5400.0)
        morning2 = self.slot(56, 0.05, battery_power=0.0, grid_export=5400.0)
        self.assertEqual(night.period_start.date() + timedelta(days=1), morning1.period_start.date())
        deadline = morning2.period_start + timedelta(hours=4)
        plan = self.plan(
            [night, morning1, morning2],
            energy=2.0,
            headroom_requirements=[(deadline, 1.0)],
            hard_headroom_requirements=[],
        )
        delta = plan.battery_power_delta_schedule

        # Same-day bands saturate first (100 W cap headroom each), the
        # remainder overflows to the night band.
        self.assertAlmostEqual(delta[morning1.period_start.isoformat()], 100.0)
        self.assertAlmostEqual(delta[morning2.period_start.isoformat()], 100.0)
        self.assertAlmostEqual(delta[night.period_start.isoformat()], 3800.0)
        self.assertAlmostEqual(plan.unallocated_headroom_kwh, 0.0)

    def test_headroom_splits_proactive_night_before_share_by_price(self):
        # Up to half the pre-release may move to night-before bands on
        # price even when same-day could fit everything: evening peaks
        # often beat morning prices, while the locked same-day half keeps
        # a large forecast-error margin.
        self.assertEqual(export_scheduler.NIGHT_BEFORE_HEADROOM_SHARE, 0.5)
        night = self.slot(16, 0.30)
        morning = self.slot(52, 0.12)
        deadline = morning.period_start + timedelta(hours=5)
        plan = self.plan(
            [night, morning],
            energy=2.0,
            headroom_requirements=[(deadline, 1.0)],
            hard_headroom_requirements=[],
        )
        delta = plan.battery_power_delta_schedule

        self.assertAlmostEqual(delta[night.period_start.isoformat()], 2000.0)
        self.assertAlmostEqual(delta[morning.period_start.isoformat()], 2000.0)
        self.assertAlmostEqual(plan.unallocated_headroom_kwh, 0.0)

    def test_hold_absorbs_while_battery_has_room_below_ceiling(self):
        # Regression test for 2026-09-23: the hold routed 23.9 kWh of midday
        # PV to protect 4.2 kWh of room while the battery sat at 11-13 kWh.
        # Charging only eats required room once the trajectory threatens
        # the energy ceiling (capacity minus requirement); below it every
        # kWh absorbs toward the 100% top-up.
        night = self.slot(16, 0.30)
        morning1 = self.slot(52, 0.35, battery_power=3000.0, grid_export=100.0)
        morning2 = self.slot(56, 0.35, battery_power=3000.0, grid_export=100.0)
        deadline = morning2.period_start + timedelta(hours=4)
        hold = export_scheduler.build_headroom_storage_hold_schedule(
            [night, morning1, morning2],
            {night.period_start.isoformat(): 5000.0},
            [(deadline, 1.25)],
            max_grid_export_w=5500.0,
            neutral_grid_target_w=-100.0,
            max_battery_charge_w=6600.0,
            min_export_price_per_kwh=0.10,
            current_battery_energy_kwh=12.0,
            battery_capacity_kwh=32.0,
        )

        # Ceiling is 30.75 kWh against ~12 kWh stored: both mornings absorb.
        self.assertAlmostEqual(hold.get(morning1.period_start.isoformat(), 0.0), 0.0)
        self.assertAlmostEqual(hold.get(morning2.period_start.isoformat(), 0.0), 0.0)

    def test_hold_caps_charging_at_requirement_ceiling(self):
        night = self.slot(16, 0.30)
        morning1 = self.slot(52, 0.35, battery_power=3000.0, grid_export=100.0)
        morning2 = self.slot(56, 0.35, battery_power=3000.0, grid_export=100.0)
        peak = self.slot(60, 0.15, battery_power=0.0, grid_export=8000.0)
        deadline = peak.period_start + timedelta(hours=2)
        hold = export_scheduler.build_headroom_storage_hold_schedule(
            [night, morning1, morning2, peak],
            {night.period_start.isoformat(): 4000.0},
            [(deadline, 1.0)],
            max_grid_export_w=5500.0,
            neutral_grid_target_w=-100.0,
            max_battery_charge_w=6600.0,
            min_export_price_per_kwh=0.10,
            current_battery_energy_kwh=31.0,
            battery_capacity_kwh=32.0,
        )

        # Created 1.0 kWh for a 1.0 kWh requirement with the battery at the
        # 31.0 kWh ceiling, and an 8000 W breach ahead keeps it binding:
        # the first morning still fits (30.0 -> 30.75), the second would
        # breach it, so only the excess 0.5 kWh routes, and the peak itself
        # holds to the cap.
        self.assertAlmostEqual(hold.get(morning1.period_start.isoformat(), 0.0), 0.0)
        self.assertAlmostEqual(hold[morning2.period_start.isoformat()], 2000.0)
        self.assertAlmostEqual(hold[peak.period_start.isoformat()], 5400.0)

    def test_hold_yields_to_top_up_without_breach_in_window(self):
        # Soft shaping must never cost the top-up where no slot in the
        # window can breach the hard cap: the ceiling lifts regardless of
        # battery level and the surplus absorbs.
        night = self.slot(16, 0.30)
        morning = self.slot(52, 0.35, battery_power=3000.0, grid_export=100.0)
        deadline = morning.period_start + timedelta(hours=5)
        hold = export_scheduler.build_headroom_storage_hold_schedule(
            [night, morning],
            {night.period_start.isoformat(): 4000.0},
            [(deadline, 1.0)],
            max_grid_export_w=5500.0,
            neutral_grid_target_w=-100.0,
            max_battery_charge_w=6600.0,
            min_export_price_per_kwh=0.10,
            current_battery_energy_kwh=20.0,
            battery_capacity_kwh=32.0,
        )

        self.assertAlmostEqual(hold.get(morning.period_start.isoformat(), 0.0), 0.0)

    def test_hold_keeps_ceiling_with_future_breach_ahead(self):
        night = self.slot(16, 0.30)
        morning = self.slot(52, 0.35, battery_power=3000.0, grid_export=100.0)
        peak = self.slot(56, 0.15, battery_power=0.0, grid_export=8000.0)
        deadline = peak.period_start + timedelta(hours=2)
        hold = export_scheduler.build_headroom_storage_hold_schedule(
            [night, morning, peak],
            {night.period_start.isoformat(): 4000.0},
            [(deadline, 1.0)],
            max_grid_export_w=5500.0,
            neutral_grid_target_w=-100.0,
            max_battery_charge_w=6600.0,
            min_export_price_per_kwh=0.10,
            current_battery_energy_kwh=32.0,
            battery_capacity_kwh=32.0,
        )

        # The 8000 W peak at or after each slot keeps the ceiling binding
        # despite the battery sitting at it: morning routes fully and the
        # peak itself holds to the cap.
        self.assertAlmostEqual(hold[morning.period_start.isoformat()], 3000.0)
        self.assertAlmostEqual(hold[peak.period_start.isoformat()], 5400.0)

    def test_hold_preservation_ignores_export_price_floor(self):
        # The floor gates creating room, never protecting it: the morning
        # charge below would breach the requirement ceiling ahead of a
        # hard breach, so it routes even under the floor.
        night = self.slot(16, 0.30)
        morning = self.slot(52, 0.12, battery_power=3000.0, grid_export=100.0)
        peak = self.slot(56, 0.15, battery_power=0.0, grid_export=8000.0)
        deadline = peak.period_start + timedelta(hours=2)
        hold = export_scheduler.build_headroom_storage_hold_schedule(
            [night, morning, peak],
            {night.period_start.isoformat(): 4000.0},
            [(deadline, 1.0)],
            max_grid_export_w=5500.0,
            neutral_grid_target_w=-100.0,
            max_battery_charge_w=6600.0,
            min_export_price_per_kwh=0.212,
            current_battery_energy_kwh=31.5,
            battery_capacity_kwh=32.0,
        )

        # Night discharge leaves 30.5 kWh against the 31.0 kWh ceiling; the
        # 0.75 kWh morning charge only fits 0.5 kWh, so 0.25 kWh routes as
        # 1000 W despite the 0.12 price sitting under the 0.212 floor.
        self.assertAlmostEqual(hold[morning.period_start.isoformat()], 1000.0)
        self.assertAlmostEqual(hold[peak.period_start.isoformat()], 5400.0)

    def test_high_pv_day_fills_battery_and_respects_export_cap(self):
        # End-to-end forecast sanity for a strong-PV day: morning
        # pre-release plus holds must keep the noon peak under the export
        # cap while the battery still charges to ~100%.  Regression test
        # for 2026-09-23, when holds routed 23.9 kWh midday to protect
        # 4.2 kWh of room and the battery pinned at 11-13 kWh instead of
        # filling.
        day = datetime(2026, 9, 23, tzinfo=timezone.utc)

        def slot(hour, price, battery_w, export_w):
            return ExportSchedulerSlot(
                period_start=day + timedelta(hours=hour),
                duration_hours=0.5,
                price_per_kwh=price,
                baseline_setpoint_w=-100.0,
                baseline_battery_power_w=battery_w,
                baseline_grid_export_w=export_w,
            )

        slots = [
            slot(7, 0.35, -500.0, 0.0),
            slot(9, 0.15, 6000.0, 100.0),
            slot(11, 0.10, 6000.0, 100.0),
            slot(13, 0.06, 0.0, 6000.0),
            slot(15, 0.12, 3000.0, 100.0),
        ]
        plan = build_unified_export_schedule(
            slots=slots,
            available_export_energy_kwh=10.0,
            required_headroom_energy_kwh=0.0,
            headroom_deadline=None,
            min_discharge_price=0.10,
            max_grid_export_w=5500.0,
            max_battery_discharge_w=8000.0,
            efficient_discharge_w=3500.0,
            quiet_boost_penalty_fraction=0.2,
            headroom_requirements=[(day + timedelta(hours=13, minutes=30), 2.0)],
            hard_headroom_requirements=[],
            max_battery_charge_w=6600.0,
            current_battery_energy_kwh=26.0,
            battery_capacity_kwh=32.0,
        )
        self.assertAlmostEqual(plan.unallocated_headroom_kwh, 0.0)

        # Morning absorbs (no holds), only the cap-threatening peak holds.
        hold = plan.headroom_hold_schedule
        self.assertAlmostEqual(hold.get(slots[1].period_start.isoformat(), 0.0), 0.0)
        self.assertAlmostEqual(hold.get(slots[2].period_start.isoformat(), 0.0), 0.0)
        self.assertAlmostEqual(hold[slots[3].period_start.isoformat()], 5400.0)

        # Replay the day with the plan applied: deltas drain, holds set
        # the grid target (feed-in = hold power above neutral), everything
        # else follows the baseline.
        energy_kwh = 26.0
        for slot in slots:
            key = slot.period_start.isoformat()
            delta_w = plan.battery_power_delta_schedule.get(key, 0.0)
            hold_w = hold.get(key, 0.0)
            feedin_w = slot.baseline_grid_export_w + delta_w
            if hold_w > 0:
                feedin_w = hold_w + 100.0
            self.assertLessEqual(feedin_w, 5500.0)
            energy_kwh -= delta_w * slot.duration_hours / 1000
            energy_kwh -= max(0.0, -slot.baseline_battery_power_w) * slot.duration_hours / 1000
            if hold_w > 0:
                charged_w = max(0.0, slot.baseline_grid_export_w + delta_w - feedin_w)
            else:
                charged_w = max(0.0, slot.baseline_battery_power_w)
            energy_kwh += charged_w * slot.duration_hours / 1000
            energy_kwh = min(32.0, max(0.0, energy_kwh))
        self.assertGreaterEqual(energy_kwh, 31.0)

    def test_headroom_hold_is_confined_to_deadline_day(self):
        # 2026-09-25: a requirement for tomorrow's midday peak pinned the
        # battery at 31.46 all afternoon via its room ceiling, so the day
        # missed the 100% balancing top-up to protect room that the evening
        # discharge creates on its own.  Holds apply only from the
        # deadline's own day; earlier days simulate the trajectory without
        # routing, while deadline-day protection (including the breach
        # slot) stays intact.
        day1 = datetime(2026, 9, 25, tzinfo=timezone.utc)
        day2 = day1 + timedelta(days=1)

        def slot(start, price, battery_w, export_w):
            return ExportSchedulerSlot(
                period_start=start,
                duration_hours=0.5,
                price_per_kwh=price,
                baseline_setpoint_w=-100.0,
                baseline_battery_power_w=battery_w,
                baseline_grid_export_w=export_w,
            )

        early = slot(day1 + timedelta(hours=14), 0.40, 2000.0, 100.0)
        afternoon = slot(day1 + timedelta(hours=15, minutes=30), 0.05, 4000.0, 100.0)
        evening = slot(day1 + timedelta(hours=19), 0.30, -500.0, 100.0)
        night = slot(day1 + timedelta(hours=23), 0.10, -500.0, 100.0)
        morning = slot(day2 + timedelta(hours=9), 0.05, 4000.0, 100.0)
        peak = slot(day2 + timedelta(hours=13), 0.06, 0.0, 6000.0)
        slots = [early, evening, afternoon, night, morning, peak]
        plan = build_unified_export_schedule(
            slots=slots,
            available_export_energy_kwh=10.0,
            required_headroom_energy_kwh=0.0,
            headroom_deadline=None,
            min_discharge_price=0.10,
            max_grid_export_w=5500.0,
            max_battery_discharge_w=8000.0,
            efficient_discharge_w=3500.0,
            quiet_boost_penalty_fraction=0.2,
            headroom_requirements=[(day2 + timedelta(hours=13, minutes=30), 2.0)],
            hard_headroom_requirements=[],
            max_battery_charge_w=6600.0,
            current_battery_energy_kwh=31.46,
            battery_capacity_kwh=32.0,
            allow_economic_export=False,
        )
        hold = plan.headroom_hold_schedule

        # Headroom pre-releases at the best afternoon price, but the
        # same-day surplus keeps charging toward the 100% top-up.
        self.assertGreater(
            plan.battery_power_delta_schedule[early.period_start.isoformat()],
            0.0,
        )
        self.assertAlmostEqual(
            hold.get(afternoon.period_start.isoformat(), 0.0), 0.0
        )
        # On the deadline day the ceiling still binds above the created
        # room, and the cap-breach peak still routes fully.
        self.assertGreater(hold.get(morning.period_start.isoformat(), 0.0), 0.0)
        self.assertAlmostEqual(hold[peak.period_start.isoformat()], 5400.0)

    def test_headroom_hold_simulates_planned_economic_discharge(self):
        # 2026-09-26 forecast: the hold simulated only headroom allocations,
        # ignoring the 10 kWh evening economic export, so it believed the
        # battery would sit above its room ceiling at tomorrow's midday
        # peak and pinned charging while exporting 5 kW — with the replay
        # showing twice the required room already free and the battery
        # stuck at ~50% instead of filling for the 15:30 EV arrival.  The
        # hold must simulate the full planned trajectory, so the ceiling
        # only binds where the executed plan would actually breach it.
        day1 = datetime(2026, 9, 25, tzinfo=timezone.utc)
        day2 = day1 + timedelta(days=1)

        def slot(start, price, battery_w, export_w):
            return ExportSchedulerSlot(
                period_start=start,
                duration_hours=0.5,
                price_per_kwh=price,
                baseline_setpoint_w=-100.0,
                baseline_battery_power_w=battery_w,
                baseline_grid_export_w=export_w,
            )

        afternoon = slot(day1 + timedelta(hours=16), 0.05, 4000.0, 100.0)
        evening = slot(day1 + timedelta(hours=19), 0.40, -500.0, 100.0)
        night = slot(day1 + timedelta(hours=23), 0.10, -500.0, 100.0)
        morning = slot(day2 + timedelta(hours=9), 0.05, 4000.0, 100.0)
        peak = slot(day2 + timedelta(hours=13), 0.06, 0.0, 6000.0)
        slots = [afternoon, evening, night, morning, peak]
        plan = build_unified_export_schedule(
            slots=slots,
            available_export_energy_kwh=10.0,
            required_headroom_energy_kwh=0.0,
            headroom_deadline=None,
            min_discharge_price=0.10,
            max_grid_export_w=5500.0,
            max_battery_discharge_w=8000.0,
            efficient_discharge_w=3500.0,
            quiet_boost_penalty_fraction=0.2,
            headroom_requirements=[(day2 + timedelta(hours=13, minutes=30), 2.0)],
            hard_headroom_requirements=[],
            max_battery_charge_w=6600.0,
            current_battery_energy_kwh=31.46,
            battery_capacity_kwh=32.0,
            allow_economic_export=True,
        )
        hold = plan.headroom_hold_schedule

        # Evening peak carries headroom plus economic export; the hold
        # simulation must see both.
        self.assertGreater(
            plan.battery_power_delta_schedule[evening.period_start.isoformat()],
            4000.0,
        )
        # The planned trajectory sits well under the ceiling at the morning
        # ramp, so no pin: the battery keeps charging toward the EV arrival
        # instead of exporting.
        self.assertAlmostEqual(
            hold.get(morning.period_start.isoformat(), 0.0), 0.0
        )
        # The cap-breach peak itself still routes.
        self.assertGreater(hold.get(peak.period_start.isoformat(), 0.0), 0.0)

    def test_headroom_is_created_after_last_intentional_recharge_before_deadline(self):
        early_high_price = self.slot(0, 0.40)
        intentional_recharge = self.slot(
            1,
            0.05,
            battery_power=3000.0,
            grid_import=3000.0,
        )
        later_lower_price = self.slot(2, 0.10)
        deadline = later_lower_price.period_start + timedelta(minutes=15)

        plan = self.plan(
            [early_high_price, intentional_recharge, later_lower_price],
            energy=1.0,
            headroom_requirements=[(deadline, 1.0)],
            hard_headroom_requirements=[],
        )

        self.assertEqual(
            plan.battery_power_delta_schedule[early_high_price.period_start.isoformat()],
            0.0,
        )
        self.assertEqual(
            plan.battery_power_delta_schedule[later_lower_price.period_start.isoformat()],
            4000.0,
        )
        self.assertAlmostEqual(plan.unallocated_headroom_kwh, 0.0)

    def test_headroom_is_created_after_last_ev_interval_that_recharges_battery(self):
        early_high_price = self.slot(0, 0.40)
        ev_recharge_interval = self.slot(
            1,
            0.05,
            battery_power=2000.0,
            ev_power=3000.0,
        )
        later_lower_price = self.slot(2, 0.10)
        deadline = later_lower_price.period_start + timedelta(minutes=15)

        plan = self.plan(
            [early_high_price, ev_recharge_interval, later_lower_price],
            energy=1.0,
            headroom_requirements=[(deadline, 1.0)],
            hard_headroom_requirements=[],
        )

        self.assertEqual(
            plan.battery_power_delta_schedule[early_high_price.period_start.isoformat()],
            0.0,
        )
        self.assertEqual(
            plan.battery_power_delta_schedule[later_lower_price.period_start.isoformat()],
            4000.0,
        )

    def test_recharge_ending_at_deadline_makes_earlier_headroom_unusable(self):
        early_high_price = self.slot(0, 0.05)
        recharge_until_deadline = self.slot(
            1,
            0.10,
            battery_power=3000.0,
            grid_import=3000.0,
        )
        deadline = recharge_until_deadline.period_start + timedelta(
            hours=recharge_until_deadline.duration_hours
        )

        plan = self.plan(
            [early_high_price, recharge_until_deadline],
            energy=1.0,
            headroom_requirements=[(deadline, 1.0)],
            hard_headroom_requirements=[],
        )

        self.assertEqual(
            plan.battery_power_delta_schedule[
                early_high_price.period_start.isoformat()
            ],
            0.0,
        )
        self.assertAlmostEqual(plan.unallocated_headroom_kwh, 1.0)

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

    def test_headroom_hold_skips_slots_below_export_floor(self):
        allocation_slot = self.slot(0, 0.40)
        peak_slot = self.slot(
            1,
            0.05,
            battery_power=0.0,
            grid_export=5000.0,
        )
        deadline = peak_slot.period_start + timedelta(minutes=15)
        requirements = [(deadline, 0.875)]
        delta_schedule = {
            allocation_slot.period_start.isoformat(): 3500.0,
        }

        hold = export_scheduler.build_headroom_storage_hold_schedule(
            [allocation_slot, peak_slot],
            delta_schedule,
            requirements,
            max_grid_export_w=5500.0,
            neutral_grid_target_w=-100.0,
            min_export_price_per_kwh=0.212,
        )
        self.assertEqual(hold.get(peak_slot.period_start.isoformat(), 0.0), 0.0)

    def test_hard_cap_hold_skips_slots_below_export_floor(self):
        first_violation = self.slot(1, 0.05, grid_export=5200.0)

        hold = build_hard_cap_storage_hold_schedule(
            [first_violation],
            max_grid_export_w=5000.0,
            neutral_grid_target_w=-100.0,
            min_export_price_per_kwh=0.212,
        )
        self.assertEqual(hold, {})

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
        plan = self.plan([slot], energy=0.375, neutral_grid_target_w=0.0)

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
        base = dict(
            planned_ev_available=True,
            planned_ev_is_charging=False,
            planned_ev_energy_needed_kwh=1.0,
            plan_calculated_at=plan_time,
            live_ev_available=True,
            live_ev_is_charging=False,
            live_ev_energy_needed_kwh=1.0,
        )
        self.assertFalse(
            is_ev_plan_fresh(
                **(base | {"current_time": plan_time + timedelta(seconds=301)})
            )
        )

    def test_single_missed_publish_tick_keeps_matching_plan_fresh(self):
        plan_time = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        base = dict(
            planned_ev_available=True,
            planned_ev_is_charging=False,
            planned_ev_energy_needed_kwh=1.0,
            plan_calculated_at=plan_time,
            live_ev_available=True,
            live_ev_is_charging=False,
            live_ev_energy_needed_kwh=1.0,
        )
        # One missed 120-second publish tick plus runtime margin must not
        # pin optional export at the neutral target while EV inputs agree.
        for age_seconds in (181, 185, 240):
            with self.subTest(age_seconds=age_seconds):
                self.assertTrue(
                    is_ev_plan_fresh(
                        **(
                            base
                            | {
                                "current_time": plan_time
                                + timedelta(seconds=age_seconds)
                            }
                        )
                    )
                )

    def test_future_or_nonfinite_ev_plan_is_stale(self):
        plan_time = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        base = dict(
            planned_ev_available=True,
            planned_ev_is_charging=False,
            planned_ev_energy_needed_kwh=1.0,
            plan_calculated_at=plan_time,
            live_ev_available=True,
            live_ev_is_charging=False,
            live_ev_energy_needed_kwh=1.0,
            current_time=plan_time,
        )
        invalid_cases = (
            {"plan_calculated_at": plan_time + timedelta(seconds=1)},
            {"plan_calculated_at": plan_time.replace(tzinfo=None)},
            {"plan_calculated_at": "not-a-datetime"},
            {
                "planned_ev_energy_needed_kwh": float("nan"),
                "live_ev_energy_needed_kwh": float("nan"),
            },
            {"planned_ev_energy_needed_kwh": float("inf")},
            {"live_ev_energy_needed_kwh": float("inf")},
            {
                "planned_ev_energy_needed_kwh": -0.1,
                "live_ev_energy_needed_kwh": -0.1,
            },
        )

        for override in invalid_cases:
            with self.subTest(override=override):
                self.assertFalse(is_ev_plan_fresh(**(base | override)))

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


class RoomOverflowRequirementTests(unittest.TestCase):
    def setUp(self):
        self.day = datetime(2026, 9, 18, tzinfo=timezone.utc)

    def _entry(self, hour, battery_kwh, pv_feedin_w, duration=0.5):
        start = self.day + timedelta(hours=hour)
        return (start, battery_kwh, pv_feedin_w, duration)

    def _base(self, **overrides):
        args = dict(
            entries=[],
            battery_capacity_kwh=32.0,
            spill_target_w=3000.0,
            pv_discount=0.75,
            current_date=self.day.date(),
        )
        args.update(overrides)
        return args

    def test_no_fill_returns_none(self):
        entries = [self._entry(8, 24.0, 500.0), self._entry(9, 25.0, 800.0)]
        self.assertIsNone(
            export_scheduler.compute_room_overflow_requirement(
                **self._base(entries=entries)
            )
        )

    def test_spill_above_target_is_discounted(self):
        entries = [
            self._entry(12, 31.0, 4000.0),
            self._entry(13, 32.0, 6000.0),
            self._entry(14, 32.0, 5000.0),
        ]
        deadline, kwh = export_scheduler.compute_room_overflow_requirement(
            **self._base(entries=entries)
        )
        self.assertEqual(deadline, self.day + timedelta(hours=12, minutes=30))
        # spill = (0.75*6000-3000)*0.5 + (0.75*5000-3000)*0.5 = 1125 Wh
        self.assertAlmostEqual(kwh, 1.125, places=3)

    def test_production_shaping_factor_stays_below_forecast_discount(self):
        # Forced discharge through the PV ramp also diverts rising PV to the
        # grid, so the pre-release hole outgrows the scheduled energy.  The
        # production shaping factor must therefore stay strictly below the
        # forecast-accuracy discount (0.75): on 2026-09-19 a 0.75-sized
        # 4.4 kWh release dug ~9.8 kWh and the battery missed 100% EOD.
        self.assertLess(
            export_scheduler.SHAPING_PV_CONFIDENCE_FACTOR,
            0.75,
        )
        entries = [
            self._entry(12, 31.0, 4000.0),
            self._entry(13, 32.0, 6000.0),
            self._entry(14, 32.0, 5000.0),
        ]
        _deadline, kwh = export_scheduler.compute_room_overflow_requirement(
            **self._base(
                entries=entries,
                pv_discount=export_scheduler.SHAPING_PV_CONFIDENCE_FACTOR,
            )
        )
        # spill = (0.6*6000-3000)*0.5 + (0.6*5000-3000)*0.5 = 300 Wh,
        # well below the 1125 Wh the 0.75 discount would release.
        self.assertAlmostEqual(kwh, 0.3, places=3)
        self.assertLess(kwh, 1.125)

    def test_zero_discount_returns_none(self):
        entries = [
            self._entry(12, 31.0, 4000.0),
            self._entry(13, 32.0, 6000.0),
        ]
        self.assertIsNone(
            export_scheduler.compute_room_overflow_requirement(
                **self._base(entries=entries, pv_discount=0.0)
            )
        )

    def test_full_discount_keeps_raw_spill(self):
        entries = [
            self._entry(12, 31.0, 4000.0),
            self._entry(13, 32.0, 6000.0),
        ]
        _deadline, kwh = export_scheduler.compute_room_overflow_requirement(
            **self._base(entries=entries, pv_discount=1.0)
        )
        # spill = (6000-3000)*0.5 = 1500 Wh
        self.assertAlmostEqual(kwh, 1.5, places=3)

    def test_already_full_returns_none(self):
        entries = [self._entry(8, 32.0, 5000.0)]
        self.assertIsNone(
            export_scheduler.compute_room_overflow_requirement(
                **self._base(entries=entries)
            )
        )

    def test_fill_tomorrow_returns_none(self):
        entries = [self._entry(30, 32.0, 5000.0), self._entry(31, 32.0, 4000.0)]
        self.assertIsNone(
            export_scheduler.compute_room_overflow_requirement(
                **self._base(entries=entries)
            )
        )

    def test_no_spill_after_fill_returns_none(self):
        entries = [self._entry(12, 31.0, 4000.0), self._entry(13, 32.0, 2000.0)]
        self.assertIsNone(
            export_scheduler.compute_room_overflow_requirement(
                **self._base(entries=entries)
            )
        )


class EconomicDischargeFillDayTests(unittest.TestCase):
    def _slot(self, start, price, battery_w, grid_export_w=0.0):
        return ExportSchedulerSlot(
            period_start=start,
            duration_hours=0.5,
            price_per_kwh=price,
            baseline_setpoint_w=-100.0,
            baseline_battery_power_w=battery_w,
            baseline_grid_export_w=grid_export_w,
        )

    def _day_slots(self, day):
        slots = [self._slot(day + timedelta(hours=9), 0.35, -500.0)]
        for hour in (10, 11, 12):
            slots.append(self._slot(day + timedelta(hours=hour), 0.10, 4000.0))
            slots.append(
                self._slot(day + timedelta(hours=hour, minutes=30), 0.10, 4000.0)
            )
        slots.append(self._slot(day + timedelta(hours=19), 0.40, -800.0))
        return slots

    def _plan(self, slots, current_kwh):
        return build_unified_export_schedule(
            slots=slots,
            available_export_energy_kwh=10.0,
            required_headroom_energy_kwh=0.0,
            headroom_deadline=None,
            min_discharge_price=0.212,
            max_grid_export_w=5500.0,
            max_battery_discharge_w=5000.0,
            efficient_discharge_w=3000.0,
            quiet_boost_penalty_fraction=0.2,
            current_battery_energy_kwh=current_kwh,
            battery_capacity_kwh=32.0,
        )

    def test_morning_peak_sells_on_fill_day(self):
        day = datetime(2026, 9, 19, tzinfo=timezone.utc)
        plan = self._plan(self._day_slots(day), current_kwh=30.0)
        key = (day + timedelta(hours=9)).isoformat()
        self.assertGreater(plan.battery_power_delta_schedule[key], 0.0)

    def test_evening_peak_sells_on_fill_day(self):
        day = datetime(2026, 9, 19, tzinfo=timezone.utc)
        plan = self._plan(self._day_slots(day), current_kwh=30.0)
        key = (day + timedelta(hours=19)).isoformat()
        self.assertGreater(plan.battery_power_delta_schedule[key], 0.0)

    def test_peaks_hold_on_no_fill_day(self):
        day = datetime(2026, 9, 19, tzinfo=timezone.utc)
        plan = self._plan(self._day_slots(day), current_kwh=10.0)
        morning = (day + timedelta(hours=9)).isoformat()
        evening = (day + timedelta(hours=19)).isoformat()
        self.assertEqual(plan.battery_power_delta_schedule[morning], 0.0)
        self.assertEqual(plan.battery_power_delta_schedule[evening], 0.0)

    def test_spill_slot_still_sells_on_no_fill_day(self):
        day = datetime(2026, 9, 19, tzinfo=timezone.utc)
        slots = self._day_slots(day)
        slots.append(self._slot(day + timedelta(hours=13), 0.25, 0.0, 4000.0))
        plan = self._plan(slots, current_kwh=10.0)
        key = (day + timedelta(hours=13)).isoformat()
        self.assertGreater(plan.battery_power_delta_schedule[key], 0.0)

    def test_preferred_headroom_uses_low_price_slots_for_spill_shaping(self):
        day = datetime(2026, 9, 19, tzinfo=timezone.utc)
        low = self._slot(day + timedelta(hours=11), 0.05, -500.0)
        high = self._slot(day + timedelta(hours=12), 0.30, -500.0)
        plan = build_unified_export_schedule(
            slots=[low, high],
            available_export_energy_kwh=10.0,
            required_headroom_energy_kwh=0.0,
            headroom_deadline=None,
            min_discharge_price=0.212,
            max_grid_export_w=5500.0,
            max_battery_discharge_w=5000.0,
            efficient_discharge_w=3000.0,
            quiet_boost_penalty_fraction=0.2,
            headroom_requirements=[
                (day + timedelta(hours=13), 3.0),
            ],
        )
        low_key = low.period_start.isoformat()
        high_key = high.period_start.isoformat()
        # high slot caps at 4500 W * 0.5 h = 2.25 kWh; shaping obeys the
        # configured feed-in target at any price, so the rest lands low.
        self.assertGreater(plan.battery_power_delta_schedule[high_key], 0.0)
        self.assertGreater(plan.battery_power_delta_schedule[low_key], 0.0)

    def test_requirement_day_start_times(self):
        day = datetime(2026, 9, 19, tzinfo=timezone.utc)
        self.assertEqual(
            export_scheduler.requirement_day_start_times(
                [
                    (day + timedelta(hours=12), 2.0),
                    (day + timedelta(hours=14), 1.0),
                    ("not-a-date", 1.0),
                ]
            ),
            [day],
        )
        self.assertEqual(export_scheduler.requirement_day_start_times([]), [])

    def test_next_day_requirement_splits_proactive_night_share_by_price(self):
        # Policy since 2026-09-23: up to NIGHT_BEFORE_HEADROOM_SHARE of a
        # requirement may move to night-before bands on price, even when
        # same-day could fit everything.  The old "stays home" rule assumed
        # morning sun pointlessly refills night release; preservation holds
        # now protect created room through the morning, so the evening
        # share earns the price spread instead.
        tonight = datetime(2026, 9, 18, 23, 0, tzinfo=timezone.utc)
        morning = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)
        night_slot = ExportSchedulerSlot(
            period_start=tonight,
            duration_hours=0.5,
            price_per_kwh=0.40,
            baseline_setpoint_w=-100.0,
            baseline_battery_power_w=-500.0,
            baseline_grid_export_w=0.0,
        )
        morning_slot = ExportSchedulerSlot(
            period_start=morning,
            duration_hours=0.5,
            price_per_kwh=0.30,
            baseline_setpoint_w=-100.0,
            baseline_battery_power_w=-500.0,
            baseline_grid_export_w=0.0,
        )
        plan = build_unified_export_schedule(
            slots=[night_slot, morning_slot],
            available_export_energy_kwh=10.0,
            required_headroom_energy_kwh=0.0,
            headroom_deadline=None,
            min_discharge_price=0.0,
            max_grid_export_w=5500.0,
            max_battery_discharge_w=5000.0,
            efficient_discharge_w=3000.0,
            quiet_boost_penalty_fraction=0.2,
            headroom_requirements=[
                (datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc), 2.0),
            ],
        )
        # Night price wins the proactive half; the locked same-day half
        # keeps the forecast-error margin in the morning.
        self.assertEqual(
            plan.battery_power_delta_schedule[tonight.isoformat()], 2000.0
        )
        self.assertEqual(
            plan.battery_power_delta_schedule[morning.isoformat()], 2000.0
        )

    def test_hard_headroom_ignores_price_floor(self):
        day = datetime(2026, 9, 19, tzinfo=timezone.utc)
        low = self._slot(day + timedelta(hours=11), 0.05, -500.0)
        plan = build_unified_export_schedule(
            slots=[low],
            available_export_energy_kwh=10.0,
            required_headroom_energy_kwh=0.0,
            headroom_deadline=None,
            min_discharge_price=0.212,
            max_grid_export_w=5500.0,
            max_battery_discharge_w=5000.0,
            efficient_discharge_w=3000.0,
            quiet_boost_penalty_fraction=0.2,
            hard_headroom_requirements=[
                (day + timedelta(hours=12), 1.0),
            ],
        )
        self.assertGreater(
            plan.battery_power_delta_schedule[low.period_start.isoformat()], 0.0
        )


class HeadroomReleaseLedgerTests(unittest.TestCase):
    # 2026-10-02 incident: headroom pre-release exported ~4.5 kWh against a
    # 0.9-1.9 kWh requirement.  On a strong-PV day the battery fills by late
    # morning from any evening level, so draining does not shrink predicted
    # midday spill and every run re-allocated ~0.5-1 kWh fresh.  The ledger
    # tracks already-released energy per spill event so allocation converges.
    def setUp(self):
        self.t0 = datetime(2026, 10, 2, 19, 0, tzinfo=timezone.utc)
        self.key = "2026-10-03"

    def test_fresh_state_starts_at_zero(self):
        ledger = update_headroom_release_ledger(
            self.key, 2000.0, self.t0, ""
        )
        self.assertTrue(ledger.valid)
        self.assertEqual(ledger.released_kwh, 0.0)
        self.assertIn(self.key, ledger.state_value)

    def test_accumulates_commanded_headroom_power_over_time(self):
        first = update_headroom_release_ledger(
            self.key, 2000.0, self.t0, ""
        )
        second = update_headroom_release_ledger(
            self.key, 2000.0, self.t0 + timedelta(seconds=300), first.state_value
        )
        self.assertTrue(second.valid)
        self.assertAlmostEqual(second.released_kwh, 2000.0 * 300 / 3600000)

    def test_event_change_resets_the_ledger(self):
        first = update_headroom_release_ledger(
            self.key, 2000.0, self.t0, ""
        )
        second = update_headroom_release_ledger(
            self.key, 2000.0, self.t0 + timedelta(seconds=300), first.state_value
        )
        self.assertGreater(second.released_kwh, 0.0)
        reset = update_headroom_release_ledger(
            "2026-10-04", 2000.0, self.t0 + timedelta(seconds=310), second.state_value
        )
        self.assertTrue(reset.valid)
        self.assertEqual(reset.released_kwh, 0.0)

    def test_invalid_state_and_clock_back_fail_safe(self):
        bad = update_headroom_release_ledger(
            self.key, 2000.0, self.t0, "garbage"
        )
        self.assertFalse(bad.valid)
        self.assertEqual(bad.released_kwh, 0.0)
        first = update_headroom_release_ledger(
            self.key, 2000.0, self.t0, ""
        )
        back = update_headroom_release_ledger(
            self.key, 2000.0, self.t0 - timedelta(seconds=10), first.state_value
        )
        self.assertFalse(back.valid)

    def test_accounting_gap_and_negative_power_are_bounded(self):
        first = update_headroom_release_ledger(
            self.key, 2000.0, self.t0, ""
        )
        capped = update_headroom_release_ledger(
            self.key,
            2000.0,
            self.t0 + timedelta(seconds=3600),
            first.state_value,
            max_accounting_gap_seconds=600.0,
        )
        self.assertTrue(capped.valid)
        self.assertAlmostEqual(capped.released_kwh, 2000.0 * 600 / 3600000)
        idle = update_headroom_release_ledger(
            self.key, -100.0, self.t0 + timedelta(seconds=300), first.state_value
        )
        self.assertTrue(idle.valid)
        self.assertEqual(idle.released_kwh, 0.0)

    def test_read_released_returns_zero_for_other_events(self):
        first = update_headroom_release_ledger(
            self.key, 2000.0, self.t0, ""
        )
        second = update_headroom_release_ledger(
            self.key, 2000.0, self.t0 + timedelta(seconds=300), first.state_value
        )
        self.assertAlmostEqual(
            read_headroom_released_kwh(self.key, second.state_value),
            second.released_kwh,
            places=5,
        )
        self.assertEqual(read_headroom_released_kwh("2026-10-04", second.state_value), 0.0)
        self.assertEqual(read_headroom_released_kwh(self.key, "garbage"), 0.0)
        self.assertEqual(read_headroom_released_kwh(self.key, ""), 0.0)

    def test_release_factor_scales_requirements_to_converge(self):
        self.assertEqual(headroom_release_factor(1.7, 0.0), 1.0)
        self.assertAlmostEqual(headroom_release_factor(1.7, 0.85), 0.5)
        self.assertEqual(headroom_release_factor(1.7, 1.7), 0.0)
        self.assertEqual(headroom_release_factor(1.7, 5.0), 0.0)
        self.assertEqual(headroom_release_factor(0.0, 0.0), 1.0)
        self.assertEqual(headroom_release_factor(1.7, -1.0), 1.0)

    def test_scheduler_exposes_per_slot_headroom_power(self):
        start = datetime(2026, 10, 2, 19, 0, tzinfo=timezone.utc)
        slots = [
            ExportSchedulerSlot(
                period_start=start + timedelta(minutes=30 * index),
                duration_hours=0.5,
                price_per_kwh=0.25 - 0.01 * index,
                baseline_setpoint_w=-100.0,
                baseline_battery_power_w=-500.0,
                baseline_grid_export_w=0.0,
            )
            for index in range(4)
        ]
        plan = build_unified_export_schedule(
            slots=slots,
            available_export_energy_kwh=5.0,
            required_headroom_energy_kwh=1.0,
            headroom_deadline=start + timedelta(hours=18),
            min_discharge_price=0.10,
            max_grid_export_w=5500.0,
            max_battery_discharge_w=8000.0,
            efficient_discharge_w=3500.0,
            quiet_boost_penalty_fraction=0.05,
            headroom_reference_price=0.0,
            headroom_min_price_spread=0.01,
            headroom_requirements=None,
            hard_headroom_requirements=None,
            current_battery_energy_kwh=20.0,
            battery_capacity_kwh=32.0,
            neutral_grid_target_w=-100.0,
            battery_charge_efficiency=1.0,
            battery_discharge_efficiency=1.0,
            top_up_pv_confidence_factor=1.0,
            allow_economic_export=False,
        )
        self.assertGreater(plan.headroom_energy_kwh, 0.0)
        total = 0.0
        for key, headroom_w in plan.headroom_power_schedule.items():
            self.assertLessEqual(
                headroom_w, plan.battery_power_delta_schedule[key]
            )
            total += headroom_w * 0.5 / 1000
        self.assertAlmostEqual(total, plan.headroom_energy_kwh, places=6)

    def test_planner_wires_the_release_ledger(self):
        source = Path(__file__).parents[1].joinpath("energy.py").read_text()
        tree = ast.parse(source)
        planner = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "auto_setpoint_target"
        )
        planner_source = ast.unparse(planner)
        self.assertIn("headroom_release_ledger", planner_source)
        self.assertIn("headroom_release_factor", planner_source)
        self.assertIn("headroom_power_schedule", planner_source)


if __name__ == "__main__":
    unittest.main()
