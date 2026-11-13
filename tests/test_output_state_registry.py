import ast
import unittest
from collections import defaultdict
from pathlib import Path


class OutputStateRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project_root = Path(__file__).parents[1]
        cls.utils_source = (cls.project_root / "modules" / "utils.py").read_text()
        cls.utils_tree = ast.parse(cls.utils_source)

    def _load_registry_schema_methods(self):
        source_class = next(
            node
            for node in self.utils_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "OutputStateRegistry"
        )
        selected_names = {"set", "get_all_states"}
        selected_body = [
            node
            for node in source_class.body
            if isinstance(node, ast.Assign)
            or isinstance(node, ast.FunctionDef) and node.name in selected_names
        ]
        test_class = ast.ClassDef(
            name="OutputStateRegistry",
            bases=[],
            keywords=[],
            body=selected_body,
            decorator_list=[],
        )
        namespace = {}
        module = ast.Module(body=[test_class], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "modules/utils.py", "exec"), namespace)
        return namespace["OutputStateRegistry"]

    def test_partial_runtime_attributes_preserve_input_number_schema(self):
        registry_class = self._load_registry_schema_methods()
        registry = registry_class.__new__(registry_class)
        registry._state_attributes = defaultdict(dict)
        registry._state_attributes["input_number.grid_setpoint_basis"] = {
            "min": -5500,
            "max": 5500,
            "step": 1,
            "unit_of_measurement": "W",
        }

        registry.set(
            "input_number.grid_setpoint_basis",
            {
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
            },
        )

        definition = registry.get_all_states()["input_number.grid_setpoint_basis"]
        self.assertEqual(definition["min"], -5500)
        self.assertEqual(definition["max"], 5500)
        self.assertEqual(definition["step"], 1)

    def test_registry_set_preserves_loaded_name(self):
        registry_class = self._load_registry_schema_methods()
        registry = registry_class.__new__(registry_class)
        registry._state_attributes = defaultdict(dict)
        registry._state_attributes["input_number.grid_setpoint_basis"] = {
            "name": "Grid Setpoint Basis",
            "min": -5500,
            "max": 5500,
        }

        registry.set(
            "input_number.grid_setpoint_basis",
            {"unit_of_measurement": "W"},
        )

        definition = registry.get_all_states()["input_number.grid_setpoint_basis"]
        self.assertEqual(definition["name"], "Grid Setpoint Basis")
        self.assertEqual(definition["min"], -5500)
        self.assertEqual(definition["max"], 5500)

    def _load_boolean_filter(self):
        helper = next(
            node
            for node in self.utils_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "filter_input_boolean_definition"
        )
        helper.decorator_list = []
        namespace = {}
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                "modules/utils.py",
                "exec",
            ),
            namespace,
        )
        return namespace["filter_input_boolean_definition"]

    def test_boolean_filter_keeps_only_helper_schema(self):
        filtr = self._load_boolean_filter()
        self.assertEqual(
            filtr(
                "auto_force_charge_target",
                {
                    "name": "Auto force charge target",
                    "icon": "mdi:battery-charging",
                    "unit_of_measurement": "W",
                    "device_class": "power",
                    "min": 0,
                    "friendly_name": "junk",
                },
            ),
            {"name": "Auto force charge target", "icon": "mdi:battery-charging"},
        )
        self.assertEqual(
            filtr("auto_force_charge_target", {"icon": "mdi:battery-charging"}),
            {"name": "Auto Force Charge Target", "icon": "mdi:battery-charging"},
        )
        for initial in ("on", "off", True, False):
            self.assertEqual(
                filtr("flag", {"initial": initial})["initial"], initial
            )
        with self.assertRaisesRegex(ValueError, "initial must be on or off"):
            filtr("flag", {"initial": "maybe"})

    def test_write_output_states_routes_input_boolean(self):
        write_fn = next(
            node
            for node in self.utils_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "write_output_states"
        )
        tuples = [
            {elt.value for elt in node.elts if isinstance(elt, ast.Constant)}
            for node in ast.walk(write_fn)
            if isinstance(node, ast.Tuple)
        ]
        self.assertIn({"input_number", "input_boolean"}, tuples)
        self.assertTrue(
            any(
                isinstance(node, ast.Name) and node.id == "filter_input_boolean_definition"
                for node in ast.walk(write_fn)
            )
        )
        # Routed domains are deleted from state_attributes, so booleans cannot
        # leak into the template-sensor list built from it afterwards.
        self.assertTrue(
            any(
                "state_attributes" in ast.unparse(node)
                for node in ast.walk(write_fn)
                if isinstance(node, ast.Delete)
            )
        )

    def test_flag_registration_declares_schema_without_state(self):
        energy_tree = ast.parse((self.project_root / "energy.py").read_text())
        registrations = [
            node
            for node in ast.walk(energy_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "register_output_state"
        ]
        self.assertEqual(len(registrations), 1)
        call = registrations[0]
        self.assertEqual(ast.unparse(call.args[0]), "Automation.auto_force_charge_target")
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        self.assertIn("name", keywords)
        self.assertIn("icon", keywords)
        # No initial: fresh installs default off, otherwise the state restores
        # across reloads instead of resetting.
        self.assertNotIn("initial", keywords)
        holder = next(
            node for node in energy_tree.body
            if isinstance(node, ast.FunctionDef)
            and any(call is descendant for descendant in ast.walk(node))
        )
        self.assertTrue(
            any(
                isinstance(deco, ast.Name) and deco.id == "time_trigger"
                for deco in holder.decorator_list
            )
        )

    def test_write_output_states_strips_mode_from_template_sensors(self):
        write_fn = next(
            node
            for node in self.utils_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "write_output_states"
        )
        popped_keys = {
            elt.value
            for node in ast.walk(write_fn)
            if isinstance(node, ast.Tuple)
            for elt in node.elts
            if isinstance(elt, ast.Constant)
        }

        self.assertIn("mode", popped_keys)

    def test_new_input_number_without_bounds_is_rejected(self):
        registry_class = self._load_registry_schema_methods()
        registry = registry_class.__new__(registry_class)
        registry._state_attributes = defaultdict(dict)

        with self.assertRaisesRegex(ValueError, "must define min and max"):
            registry.set("input_number.invalid", {"unit_of_measurement": "W"})

    def test_schema_changes_trigger_rewrite_and_writes_use_copies(self):
        source_class = next(
            node
            for node in self.utils_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "OutputStateRegistry"
        )
        write_method = next(
            node
            for node in source_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "write_if_necessary"
        )
        get_method = next(
            node
            for node in source_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "get_all_states"
        )

        self.assertIn("all_states != self._last_written", ast.unparse(write_method))
        self.assertIn("write_payload", ast.unparse(write_method))
        write_source = ast.unparse(write_method)
        self.assertLess(
            write_source.index("self._last_written ="),
            write_source.index("hass.async_add_executor_job"),
        )
        self.assertIn("dict(value)", ast.unparse(get_method))

    def test_every_grid_basis_publication_declares_bounds(self):
        energy_tree = ast.parse((self.project_root / "energy.py").read_text())
        publications = [
            node
            for node in ast.walk(energy_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "set_state"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and ast.unparse(node.args[0]) == "Grid.power_setpoint_basis"
        ]

        self.assertGreaterEqual(len(publications), 3)
        for publication in publications:
            self.assertTrue(
                any(
                    keyword.arg is None
                    and isinstance(keyword.value, ast.Name)
                    and keyword.value.id == "grid_setpoint_basis_attributes"
                    for keyword in publication.keywords
                )
            )


if __name__ == "__main__":
    unittest.main()
