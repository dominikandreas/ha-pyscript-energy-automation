"""Run production forecast functions offline with all HA mutations forbidden."""
import ast
import builtins
import json
import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parents[1]

def forbidden(*args, **kwargs):
    raise AssertionError("Offline forecast attempted Home Assistant access")

def aware(value):
    if isinstance(value, str): value = datetime.fromisoformat(value)
    return value.replace(tzinfo=ZoneInfo("Europe/Berlin")) if value.tzinfo is None else value

def load_energy():
    sys.path.insert(0, str(ROOT / "modules"))
    builtins.pyscript_compile = lambda f: f
    utils = types.ModuleType("utils")
    utils.get = utils.get_attr = utils.set_state = utils.now = forbidden
    utils.clip = lambda x, lo, hi: max(lo, min(hi, x))
    utils.with_timezone = aware
    sys.modules["utils"] = utils
    tree = ast.parse((ROOT / "energy.py").read_text())
    body = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.If, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node.decorator_list = []
            body.append(node)
        elif isinstance(node, ast.Assign):
            try: ast.literal_eval(node.value)
            except (ValueError, TypeError):
                if not (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "define_interfaces"): continue
            body.append(node)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + body, type_ignores=[])
    ast.fix_missing_locations(module)
    energy = types.ModuleType("offline_energy")
    sys.modules[energy.__name__] = energy
    exec(compile(module, str(ROOT / "energy.py"), "exec"), energy.__dict__)
    return energy

def incident_inputs(energy):
    data = json.loads((ROOT / "tests/fixtures/ev_policy_2026_10_17.json").read_text())
    data["forecast"] = [energy.PVForecastWithPrices(datetime.fromisoformat(e["period_start"]), e["pv_estimate"], e["price_per_kwh"]) for e in data["forecast"]]
    data["ev_schedule"] = [energy.EVScheduleEntry(datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"]),e["distance"],e["required_soc"]) for e in data["ev_schedule"]]
    for key in ("t_now", "charger_ready_since", "precharge_arrival_time"):
        if data.get(key): data[key] = datetime.fromisoformat(data[key])
    return data
