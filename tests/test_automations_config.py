import unittest
from pathlib import Path

import yaml


class AutomationConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(__file__).parents[2].joinpath("automations.yaml").read_text()
        cls.automations = yaml.safe_load(cls.source)

    def automation_by_id(self, automation_id):
        return next(automation for automation in self.automations if str(automation.get("id")) == automation_id)

    def dictionary_nodes(self, value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from self.dictionary_nodes(child)
        elif isinstance(value, list):
            for child in value:
                yield from self.dictionary_nodes(child)

    def assert_recurtailment_path_confirms_final_off(self, actions):
        self.assertEqual(actions[0]["action"], "switch.turn_off")
        self.assertTrue(actions[0]["continue_on_error"])
        self.assertIn("wait_template", actions[1])
        self.assertIn("'off'", actions[1]["wait_template"])
        self.assertTrue(actions[1]["continue_on_timeout"])

        confirmation = actions[2]
        confirmed_off = confirmation["choose"][0]
        self.assertEqual(confirmed_off["conditions"][0]["condition"], "state")
        self.assertEqual(
            confirmed_off["conditions"][0]["entity_id"],
            "switch.inverter_inverter_on_off",
        )
        self.assertEqual(confirmed_off["conditions"][0]["state"], "off")
        self.assertIn(
            "remains off",
            confirmed_off["sequence"][0]["data"]["message"],
        )
        self.assertIn(
            "not confirmed off",
            confirmation["default"][0]["data"]["message"],
        )

    def test_hms_limit_updates_use_absolute_deadband(self):
        self.assertNotIn("diff > 15 or diff < 15", self.source)
        self.assertEqual(self.source.count("diff | abs > 15"), 3)

    def test_roof_pv_emergency_curtailment_confirms_switch_off_before_marking_recovery_pending(self):
        emergency = self.automation_by_id("1778329059064")
        trigger = emergency["triggers"][0]
        actions = emergency["actions"]

        self.assertIn("5500", trigger["value_template"])
        self.assertEqual(trigger["for"]["minutes"], 1)
        self.assertEqual(actions[0]["action"], "switch.turn_off")
        self.assertTrue(actions[0]["continue_on_error"])
        self.assertIn("wait_template", actions[1])
        self.assertIn("is_state", actions[1]["wait_template"])
        self.assertIn("'off'", actions[1]["wait_template"])
        self.assertTrue(actions[1]["continue_on_timeout"])
        self.assertNotIn("delay", {key for action in actions for key in action})

        observation = actions[2]
        confirmed_off = observation["choose"][0]
        self.assertEqual(confirmed_off["conditions"][0]["condition"], "state")
        self.assertEqual(
            confirmed_off["conditions"][0]["entity_id"],
            "switch.inverter_inverter_on_off",
        )
        self.assertEqual(confirmed_off["conditions"][0]["state"], "off")

        marker_action = confirmed_off["sequence"][0]
        self.assertEqual(
            marker_action["action"],
            "pyscript.set_roof_pv_emergency_pending",
        )
        self.assertIs(marker_action["data"]["pending"], True)
        self.assertTrue(marker_action["continue_on_error"])
        marker_actions = [
            node
            for node in self.dictionary_nodes(emergency)
            if node.get("action") == "pyscript.set_roof_pv_emergency_pending"
        ]
        self.assertEqual(marker_actions, [marker_action])
        marker_wait = confirmed_off["sequence"][1]
        self.assertIn("wait_template", marker_wait)
        self.assertIn("pyscript.roof_pv_emergency_pending", marker_wait["wait_template"])
        self.assertIn("'on'", marker_wait["wait_template"])
        self.assertTrue(marker_wait["continue_on_timeout"])
        marker_result = confirmed_off["sequence"][2]
        marker_confirmed = marker_result["choose"][0]
        self.assertIn("pyscript.roof_pv_emergency_pending", str(marker_confirmed["conditions"]))
        self.assertIn("curtailed", marker_confirmed["sequence"][0]["data"]["message"])
        self.assertIn("marker failed", marker_result["default"][0]["data"]["message"])
        self.assertIn("remains off", marker_result["default"][0]["data"]["message"])
        self.assertIn(
            "not confirmed off",
            observation["default"][0]["data"]["message"],
        )

    def test_roof_pv_recovery_reconciles_reload_and_serializes_retries(self):
        recovery = self.automation_by_id("1788952400000")
        triggers_by_id = {trigger["id"]: trigger for trigger in recovery["triggers"]}

        self.assertEqual(
            set(triggers_by_id),
            {"off_for_15_minutes", "startup", "reload", "retry"},
        )
        self.assertEqual(triggers_by_id["startup"]["trigger"], "homeassistant")
        self.assertEqual(triggers_by_id["startup"]["event"], "start")
        self.assertEqual(triggers_by_id["reload"]["trigger"], "event")
        self.assertEqual(
            triggers_by_id["reload"]["event_type"],
            "automation_reloaded",
        )
        self.assertEqual(triggers_by_id["retry"]["trigger"], "time_pattern")
        self.assertEqual(len(recovery["conditions"]), 1)
        marker_condition = recovery["conditions"][0]
        self.assertEqual(marker_condition["condition"], "state")
        self.assertEqual(
            marker_condition["entity_id"],
            "pyscript.roof_pv_emergency_pending",
        )
        self.assertEqual(marker_condition["state"], "on")
        self.assertNotIn("last_triggered", str(recovery))
        self.assertNotIn("initial_state", recovery)
        self.assertEqual(recovery["mode"], "single")

    def test_roof_pv_recovery_recurtails_unexpected_on_before_any_probe(self):
        recovery = self.automation_by_id("1788952400000")
        dispatch = recovery["actions"][0]
        unexpected_on = dispatch["choose"][0]
        route_source = str(unexpected_on["conditions"])

        self.assertIn("not is_state", route_source)
        self.assertIn("switch.inverter_inverter_on_off", route_source)
        self.assert_recurtailment_path_confirms_final_off(unexpected_on["sequence"])

        probe_route = dispatch["choose"][1]
        off_long_enough = probe_route["conditions"][0]
        self.assertEqual(off_long_enough["condition"], "state")
        self.assertEqual(off_long_enough["state"], "off")
        self.assertEqual(off_long_enough["for"]["minutes"], 15)

    def test_roof_pv_recovery_branches_before_settling_if_turn_on_is_unconfirmed(self):
        recovery = self.automation_by_id("1788952400000")
        actions = recovery["actions"][0]["choose"][1]["sequence"]

        self.assertEqual(actions[0]["action"], "switch.turn_on")
        self.assertTrue(actions[0]["continue_on_error"])
        self.assertIn("wait_template", actions[1])
        self.assertIn("'on'", actions[1]["wait_template"])
        self.assertEqual(actions[1]["timeout"]["seconds"], 30)
        self.assertTrue(actions[1]["continue_on_timeout"])
        self.assertEqual(len(actions), 3)

        confirmation = actions[2]
        confirmed_on = confirmation["choose"][0]
        confirmation_source = str(confirmed_on["conditions"])
        self.assertIn("wait.completed", confirmation_source)
        self.assertIn("is_state", confirmation_source)
        self.assertIn("'on'", confirmation_source)
        observation = confirmed_on["sequence"][0]
        self.assertIn("wait_template", observation)
        self.assertEqual(observation["timeout"]["minutes"], 1)
        self.assertTrue(observation["continue_on_timeout"])
        unsafe_source = observation["wait_template"]
        self.assertIn("sensor.ac_power", unsafe_source)
        self.assertIn("not is_state", unsafe_source)
        self.assertIn("unknown", unsafe_source)
        self.assertIn("unavailable", unsafe_source)
        self.assertIn("is_number", unsafe_source)
        self.assertIn("last_updated", unsafe_source)
        self.assertIn("-5000", unsafe_source)
        self.assertFalse(any("delay" in node for node in self.dictionary_nodes(confirmed_on["sequence"])))
        self.assertFalse(any("delay" in node for node in self.dictionary_nodes(confirmation["default"])))
        self.assert_recurtailment_path_confirms_final_off(confirmation["default"])

    def test_roof_pv_recovery_accepts_only_fresh_finite_safe_grid_data(self):
        recovery = self.automation_by_id("1788952400000")
        probe = recovery["actions"][0]["choose"][1]["sequence"]
        confirmed_on = probe[2]["choose"][0]
        safety_decision = confirmed_on["sequence"][1]
        safe = safety_decision["choose"][0]
        safety_source = str(safe["conditions"])

        self.assertIn("sensor.ac_power", safety_source)
        self.assertIn("is_number", safety_source)
        self.assertIn("float(none)", safety_source)
        self.assertIn("nan", safety_source)
        self.assertIn("inf", safety_source)
        self.assertIn("last_updated", safety_source)
        self.assertIn("as_timestamp", safety_source)
        self.assertIn("0 <= age <= 10", safety_source)
        self.assertIn("-5000", safety_source)
        self.assertIn("switch.inverter_inverter_on_off", safety_source)
        self.assertIn("not wait.completed", safety_source)

        self.assert_recurtailment_path_confirms_final_off(safety_decision["default"])
        safe_actions = safe["sequence"]
        clear_marker = safe_actions[0]
        self.assertEqual(
            clear_marker["action"],
            "pyscript.set_roof_pv_emergency_pending",
        )
        self.assertIs(clear_marker["data"]["pending"], False)
        self.assertTrue(clear_marker["continue_on_error"])
        clear_wait = safe_actions[1]
        self.assertIn("wait_template", clear_wait)
        self.assertIn("pyscript.roof_pv_emergency_pending", clear_wait["wait_template"])
        self.assertIn("'off'", clear_wait["wait_template"])
        self.assertTrue(clear_wait["continue_on_timeout"])
        clear_result = safe_actions[2]
        marker_cleared = clear_result["choose"][0]
        self.assertEqual(marker_cleared["conditions"][0]["condition"], "state")
        self.assertEqual(
            marker_cleared["conditions"][0]["entity_id"],
            "pyscript.roof_pv_emergency_pending",
        )
        self.assertEqual(marker_cleared["conditions"][0]["state"], "off")
        restored_notification = marker_cleared["sequence"][0]
        self.assertEqual(restored_notification["action"], "notify.send_message")
        self.assertTrue(restored_notification["continue_on_error"])
        self.assertIn("restored", restored_notification["data"]["message"])
        self.assert_recurtailment_path_confirms_final_off(clear_result["default"])

        automation_marker_actions = [
            node
            for node in self.dictionary_nodes(recovery)
            if node.get("action") in {"automation.turn_on", "automation.turn_off"}
            and node.get("target", {}).get("entity_id")
            == "automation.recover_roof_pv_after_emergency_feed_in_curtailment"
        ]
        self.assertEqual(automation_marker_actions, [])

    def test_auto_lock_covers_unlock_while_already_empty(self):
        # 2026-10-05 incident: the door unlocked itself at 06:56 with nobody
        # home and stayed unlocked for 6h, because the template trigger only
        # fires on the transition to "both away".  An unlock-while-empty
        # trigger with a 5-minute grace period closes that hole.
        auto_lock = self.automation_by_id("1771535444483")
        unlock_triggers = [
            trigger
            for trigger in auto_lock["triggers"]
            if trigger.get("trigger") == "state"
            and trigger.get("entity_id") == "lock.home"
            and trigger.get("to") == "unlocked"
        ]
        self.assertEqual(len(unlock_triggers), 1)
        self.assertEqual(unlock_triggers[0]["for"]["minutes"], 5)
        conditions_source = str(auto_lock["conditions"])
        self.assertIn("person.dominik", conditions_source)
        self.assertIn("person.tanja", conditions_source)
        self.assertIn("not_home", conditions_source)
        lock_actions = [
            node
            for node in self.dictionary_nodes(auto_lock["actions"])
            if node.get("action") == "lock.lock"
        ]
        self.assertEqual(len(lock_actions), 1)

    def test_roof_pv_recovery_switch_actions_all_continue_on_error(self):
        recovery = self.automation_by_id("1788952400000")
        switch_actions = [
            node
            for node in self.dictionary_nodes(recovery["actions"])
            if str(node.get("action", "")).startswith("switch.")
        ]

        self.assertGreaterEqual(len(switch_actions), 4)
        for action in switch_actions:
            self.assertTrue(action.get("continue_on_error"), action)


if __name__ == "__main__":
    unittest.main()
