import time
import typing
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo
from collections import defaultdict

if TYPE_CHECKING:
    import asyncio

if TYPE_CHECKING:
    # for type inference only
    def time_trigger(pattern: str):
        pass

    def state_trigger(pattern: str):
        pass

    def state_active(str_exp: str):
        pass

    class service:
        def call(domain: str, service: str, **kwargs):
            pass

    class task:
        @staticmethod
        def create(func: typing.Callable, *args, **kwargs) -> "asyncio.Task":
            """Create a new task to run the given function with arguments. Returns asyncio Task object."""

        @staticmethod
        def cancel(task_id: "asyncio.Task | None" = None) -> None:
            """Cancel specified task or current task if None."""

        @staticmethod
        def current_task() -> "asyncio.Task":
            """Return currently executing task."""

        @staticmethod
        def name2id(name: str | None = None) -> dict[str, "asyncio.Task"] | "asyncio.Task":
            """Get task ID by name or return all name mappings if no name provided."""

        @staticmethod
        def wait(task_set: typing.Set["asyncio.Task"]) -> tuple[typing.Set["asyncio.Task"], typing.Set["asyncio.Task"]]:
            """Wait for set of tasks to complete. Returns (done, pending) sets."""

        @staticmethod
        def add_done_callback(task_id: "asyncio.Task", func: typing.Callable, *args, **kwargs) -> None:
            """Add callback function to be called when task completes."""

        @staticmethod
        def remove_done_callback(task_id: "asyncio.Task", func: typing.Callable) -> None:
            """Remove previously added done callback."""

        @staticmethod
        def executor(func: typing.Callable, *args, **kwargs) -> "asyncio.Task":
            """Run blocking function in executor thread and return its result."""

        @staticmethod
        def sleep(seconds: float) -> None:
            """Non-blocking async sleep."""

        @staticmethod
        def unique(task_name: str, kill_me: bool = False) -> None:
            """Kill existing tasks with same name and claim ownership of name."""

        @staticmethod
        def wait_until(
            state_trigger: str | list[str] | None = None,
            time_trigger: str | list[str] | None = None,
            event_trigger: str | list[str] | None = None,
            mqtt_trigger: str | list[str] | None = None,
            webhook_trigger: str | list[str] | None = None,
            webhook_local_only: bool = True,
            webhook_methods: set[str] = {"POST", "PUT"},
            timeout: float | None = None,
            state_check_now: bool = True,
            state_hold: float | None = None,
            state_hold_false: float | None = None,
        ) -> dict:
            """Wait for trigger conditions to occur. Returns trigger info dict."""

    class state:
        @staticmethod
        def set(id: str, value):
            pass

        @staticmethod
        def setattr(id: str, **attributes):
            pass

        @staticmethod
        def get(id: str) -> str:
            pass

        @staticmethod
        def getattr(id: str) -> Any:
            pass

    class log:
        @staticmethod
        def error(msg: str):
            pass

        @staticmethod
        def info(msg: str):
            pass

        @staticmethod
        def warning(msg: str):
            pass

    def pyscript_compile(func):
        return func


local_timezone_name = time.tzname[0]  # Returns the system's timezone name

# Use ZoneInfo to create a timezone object
local_timezone = ZoneInfo(local_timezone_name)


def now():
    return datetime.now(local_timezone)


def with_timezone(naive_datetime):
    if naive_datetime is None:
        return None
    if isinstance(naive_datetime, str):
        naive_datetime = datetime.fromisoformat(naive_datetime)
    if not isinstance(naive_datetime, datetime):
        log.error(f"Expected datetime, got {type(naive_datetime)}: {naive_datetime}")  # type: ignore  # noqa: F821
    if naive_datetime.tzinfo is not None:
        return naive_datetime

    # Localize the naive datetime
    return naive_datetime.replace(tzinfo=local_timezone)


start_time = now()

def get(id, default="unknown", mapper=None):
    if mapper is None:
        mapper = bool if type(default) is bool else float if isinstance(default, (int, float)) else None
    try:
        val = state.get(id)  # type: ignore
    except NameError:
        # only print an error if 5 minutes passed since startup
        if (now() - start_time) > timedelta(minutes=5):
            log.warning(f"Error getting {id}, returning default: {default}")
        return default

    if type(default) is bool:
        if val in ("on", "On", "off", "Off"):
            val = val.lower() == "on"
        if val in ("true", "True", "false", "False"):
            val = val.lower() == "true"

    if isinstance(val, str) and val.lower() in ("unknown", "unavailable", None):
        return default
    try:
        return mapper(val) if mapper and val else type(default)(val) if default is not None else val
    except Exception as e:
        log.error(f"Error getting {id}, converting {val} to {type(default)} failed: {e}")
        raise


def get_attr(id, name=None, default=None, mapper=None) -> dict | None:
    if name is None:
        val = state.getattr(id)  # type: ignore  # noqa: F821
    else:
        val = (state.getattr(id) or {}).get(name, default)  # type: ignore  # noqa: F821
    if mapper:
        return mapper(val)
    return val


@pyscript_compile
def indent(str, indentation):
    return "\n".join(indentation + line for line in str.split("\n"))

@pyscript_compile
def write_output_states(state_attributes):
    import yaml
    from pathlib import Path

    input_number_states = []

    for k, v in state_attributes.items():
        id = k.split(".")[1]
        if "name" not in v:
            v["name"] = id.replace("_", " ").title()
        if "unique_id" not in v:
            v['unique_id'] = id

    for k, v in list(state_attributes.items()):
        if k.startswith("input_number"):
            input_number_states.append(v)
            del state_attributes[k]

    if input_number_states:
        Path("/config/pyscript_input_numbers.yaml").write_text(
            indent(yaml.dump(input_number_states), "  ")
        )

    sensors = []
    for k, v in state_attributes.items():
        id = k.split(".")[1]
        if not "name" in v:
            v["name"] = id.replace("_", " ").title()
        v['unique_id'] = id
        v['template'] = "{}"
        sensors.append(v)
        
    Path("/config/pyscript_template_sensors.yaml").write_text(
        indent(yaml.dump([dict(sensor=sensors)]), "  ")
    )

def load_output_states():
    import yaml
    from pathlib import Path

    state_attributes = defaultdict(dict)

    input_number_states = yaml.safe_load(Path("/config/pyscript_input_numbers.yaml").read_text())
    for v in input_number_states:
        state_attributes[f"input_number.{v['unique_id']}"] = v

    sensors = yaml.safe_load(Path("/config/pyscript_template_sensors.yaml").read_text())
    if type(sensors) is list and len(sensors) > 0:
        for v in sensors[0].get('sensor', []):
            state_attributes[f"sensor.{v['unique_id']}"] = v

    return state_attributes

class OutputStateRegistry:
    _attribute_keys = {"device_class", "friendly_name", "icon", "state_class", "unit_of_measurement"}
    def __init__(self):
        self._last_written_keys = None
        self._state_attributes = defaultdict(dict)
        for k, v in load_output_states().items():
            self._state_attributes[k] = v

        self._last_written = {*self._state_attributes}

    def set(self, id, attributes):
        self._state_attributes[id] = {k: v for k, v in attributes.items() if k in self._attribute_keys}

    def write(self):
        all_states = self.get_all_states()
        state_keys = {*all_states.keys()}
        if self._last_written_keys is None or state_keys > self._last_written_keys:
            awaitable = hass.async_add_executor_job(write_output_states, all_states)
            self._last_written = state_keys
            return awaitable

    def get_all_states(self) -> dict:
        return dict(**self._state_attributes)

output_state_registry = OutputStateRegistry()

async def set_state(id: str, value, **attributes):
    state.set(id, value)  # type: ignore # noqa: F821
    if attributes:
        output_state_registry.set(id, attributes)
        await output_state_registry.write()

    if id.startswith("input_boolean"):
        if type(value) is bool:
            value = "on" if value else "off"
        else:
            assert value in ("on", "off"), f"got {value}, expected boolean or 'on' or 'off'"
    if attributes:
        if "friendly_name" not in attributes:
            attributes["friendly_name"] = id.split(".")[-1].replace("_", " ").title()
        set_attr(id, **attributes)


def set_attr(id: str, **attributes):
    for name, value in attributes.items():
        state.setattr(f"{id}.{name}", value)  # type: ignore # noqa: F821


def clip(val, minv, maxv):
    return max(minv, min(val, maxv))


