import unittest
from datetime import datetime, timedelta, timezone

from modules.setpoint_control import (
    FeedinCandidate,
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


if __name__ == "__main__":
    unittest.main()
