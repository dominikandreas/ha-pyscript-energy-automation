# ruff: noqa: I001  # disable reordering of imports
from typing import TYPE_CHECKING


class State:
    garage_pv_energy = "sensor.garage_pv_energy"
    garage_pv_energy_today = "sensor.garage_pv_energy_today"
    shed_pv_energy_today = "sensor.shed_pv_energy_today"


if TYPE_CHECKING:
    # the type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    # Therefore during type checking we pretend to import them from modules.utils, which it can resolve.
    from modules.utils import get, set_state

    # These are provided by typescript and do not need to be imported in the actual script
    # They are only needed for type checking (linting), which development easier
    from modules.utils import time_trigger

else:
    from utils import get, set_state


@time_trigger
@time_trigger("cron(*/2 * * * *)")
def garage_energy():
    energies = [get(f"sensor.hms_1600_4t_{idx}_yieldtotal", -1.0) for idx in range(1, 4)]
    if min(energies) == -1.0:
        return

    garage_energy = sum(energies)
    set_state(
        State.garage_pv_energy,
        round(garage_energy, 2),
        state_class="total_increasing",
        unit_of_measurement="kWh",
        device_class="energy",
        icon="mdi:solar-power-variant",
        friendly_name="Garage PV Energy Today",
    )


@time_trigger
@time_trigger("cron(*/2 * * * *)")
def garage_energy_today():
    energies = [get(f"sensor.hms_1600_4t_{idx}_yieldday", -1.0) for idx in range(1, 4)]
    if min(energies) == -1:
        return

    garage_energy = sum(energies) / 1000
    set_state(
        State.garage_pv_energy_today,
        round(garage_energy, 2),
        state_class="total",
        unit_of_measurement="kWh",
        device_class="energy",
        icon="mdi:solar-power-variant",
        friendly_name="Garage PV Energy Today",
    )


@time_trigger
@time_trigger("cron(*/2 * * * *)")
def shed_pv_energy_today():
    daily_yield = get("sensor.hms_800w_t2_hms_pv_daily_yield", -1)
    if daily_yield == -1:
        return

    set_state(
        State.shed_pv_energy_today,
        round(daily_yield / 1000, 2),
        state_class="total",
        unit_of_measurement="kWh",
        device_class="energy",
        icon="mdi:solar-power-variant",
        friendly_name="Shed PV Energy Today",
    )
