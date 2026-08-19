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
