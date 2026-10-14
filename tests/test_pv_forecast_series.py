import ast
import unittest
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")


def load_build_series():
    source = (Path(__file__).parents[1] / "pv_forecast_series.py").read_text()
    tree = ast.parse(source)
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_series"
    )
    namespace = {"datetime": datetime, "time": time, "LOCAL_TIMEZONE": BERLIN}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "pv_forecast_series.py", "exec"), namespace)
    return namespace["build_series"]


class BuildSeriesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build_series = staticmethod(load_build_series())
        cls.source = (Path(__file__).parents[1] / "pv_forecast_series.py").read_text()

    def test_daily_rows_become_aligned_chart_arrays(self):
        rows = [
            {"d": date(2026, 10, 8), "fc": 11.39, "act": 16.80},
            {"d": date(2026, 10, 9), "fc": 11.89, "act": 7.39},
            {"d": date(2026, 10, 10), "fc": 34.60, "act": 29.74},
        ]

        series = self.build_series(rows)

        self.assertEqual(series["days"], ["2026-10-08", "2026-10-09", "2026-10-10"])
        self.assertEqual(series["fc"], [11.39, 11.89, 34.6])
        self.assertEqual(series["act"], [16.8, 7.39, 29.74])
        self.assertEqual(series["dev"], [5.41, -4.5, -4.86])
        expected_t = int(datetime(2026, 10, 8, 12, 0, tzinfo=BERLIN).timestamp() * 1000)
        self.assertEqual(series["t"][0], expected_t)
        for key in ("t", "fc", "act", "dev"):
            self.assertEqual(len(series[key]), 3)

    def test_days_with_missing_values_are_skipped(self):
        rows = [
            {"d": date(2026, 10, 9), "fc": 11.89, "act": 7.39},
            {"d": date(2026, 10, 10), "fc": None, "act": 29.74},
            {"d": date(2026, 10, 11), "fc": 26.31, "act": None},
        ]

        series = self.build_series(rows)

        self.assertEqual(series["days"], ["2026-10-09"])
        self.assertEqual(series["dev"], [-4.5])

    def test_query_covers_all_three_sensors_and_orders_by_day(self):
        self.assertIn("sensor.solcast_pv_forecast_forecast_today", self.source)
        self.assertIn("sensor.inverter_daily_yield", self.source)
        self.assertIn("sensor.opendtu_6dd1ec_yield_total", self.source)
        self.assertIn("ORDER BY d", self.source)


if __name__ == "__main__":
    unittest.main()
