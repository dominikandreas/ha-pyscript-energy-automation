from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    # Therefore during type checking we pretend to import them from modules.utils, which it can resolve.

    # These are provided by typescript and do not need to be imported in the actual script
    # They are only needed for type checking (linting), which development easier
    from modules.utils import log, service, state, state_trigger, get, set, time_trigger
    from modules.victron import Victron
else:
    from victron import Victron
    from utils import get, set

# Mapping between mode names and MQTT payloads
# MODE_TO_PAYLOAD = {
#     "Off": "4",
#     "Inverter only": "2",
#     "Charger only": "1",
#     "On": "3",
# }


power_kw_attributes = {
    "unit_of_measurement": "kW",
    "device_class": "power",
    "state_class": "measurement",
}


@state_trigger(Victron.inverter_mode_sensor)
def sync_input_select_from_sensor():
    """Synchronize the input_select state with the MQTT sensor state."""
    sensor_value = state.get(Victron.inverter_mode_sensor)
    Victron.set_inverter_mode(sensor_value)


@state_trigger(Victron.inverter_mode_input_select)
def publish_mqtt_on_input_select_change():
    """
    Publish the input_select state change to the MQTT topic.
    """
    new_mode = state.get(Victron.inverter_mode_input_select)
    log.warning(f"new mode for publish inverter mode: {new_mode}")
    if new_mode and new_mode in Victron.MODE_TO_PAYLOAD:
        payload = '{"value": %s}' % Victron.MODE_TO_PAYLOAD.get(new_mode)
        log.warning(f"publishing new mode for inverter '{payload}'")
        service.call(
            "mqtt",
            "publish",
            topic=Victron.inverter_mode_mqtt_write_topic,
            payload=payload,
            retain=True,
        )

moving_averages = {}

def update_moving_average_power(
    state_name, factor=0.1, avg_state_name=None, new_val=None, slack: int | float = None,
    decimals=2, **attributes
):
    """Set the average power of the Victron inverter."""
    power_now = new_val or get(state_name, None, mapper=float)
    avg_state_name = avg_state_name or (state_name + "_average")
    if power_now is not None:
        prev_power = get(avg_state_name, -1)
        if prev_power == -1:
            slack = None
            prev_power = power_now
        avg_power = moving_averages.get(avg_state_name, prev_power)
        a, b = 1-factor, factor
        update = a * avg_power + b * power_now
        moving_averages[avg_state_name] = update
        # log.warning(f"set {avg_state_name} power: {update}")
        if slack is not None and abs(prev_power - power_now) <= slack:
            return
        # log.info(f"{avg_state_name}: slack  {abs(prev_power - power_now):.3f} > {slack:2.2f} : {prev_power:.3f}-> {update:.3f}")
        set(avg_state_name, round(update, decimals), **attributes)


@time_trigger
@time_trigger("period(now, 2sec)")
def set_average_power():
    """Set the average power of the Victron inverter."""

    update_moving_average_power("sensor.victron_dc_power", 0.01, **power_kw_attributes, decimals=3, slack=0.05)
    update_moving_average_power(
        "sensor.victron_battery_power", 0.01, **power_kw_attributes, decimals=3, slack=0.05
    )


@time_trigger
@time_trigger("period(now, 5sec)")
def set_victron_efficiency():
    """Set the efficiency of the Victron inverter."""

    battery_power = get("sensor.victron_battery_power_average", -1)
    dc_power = get("sensor.victron_dc_power_average", -1)
    if dc_power == -1 or battery_power == -1:
        return
    if dc_power > 0:
        efficiency = round(battery_power / dc_power * 100, 4)
    else:
        efficiency = round(dc_power / battery_power * 100, 4)
    # outlier filtering
    if max(abs(dc_power), abs(battery_power)) < 50 or efficiency < 50:
        return
    # log.warning(f"efficiency: {efficiency}")
    update_moving_average_power(
        Victron.inverter_efficiency,
        0.1,
        Victron.inverter_efficiency,
        efficiency,
        unit_of_measurement="%",
        state_class="measurement",
        slack=1
    )


@time_trigger
@time_trigger("period(now, 5sec)")
def set_victron_power():
    """Set the power used by the Victron inverter."""

    battery_power = get("sensor.victron_battery_power_average", -1)
    dc_power = get("sensor.victron_dc_power_average", -1)
    if dc_power == -1 or battery_power == -1:
        return
    power = abs(dc_power - battery_power)
    # log.warning(f"dc power {dc_power} battery power {battery_power} power: {power}")
    update_moving_average_power(
        Victron.inverter_power,
        0.1,
        Victron.inverter_power,
        power * 1000,
        unit_of_measurement="W",
        state_class="measurement",
        slack=5,
    )
