# ruff: noqa: I001

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # ruff: noqa: I
    pass


class ElectricityPrices:
    low_price = "binary_sensor.low_electricity_price"
    high_price = "binary_sensor.high_electricity_price"
    current_price = "sensor.electricity_price"
    epex_forecast_prices = "sensor.epex_spot_data_price"


class Environment:
    """Environmental states"""

    temperature_outside = "sensor.outside_temperature"
    """The current outside temperature"""


class Battery:
    """Battery states"""

    soc = "sensor.battery_soc_2"
    """The state of charge of the battery"""
    capacity = "input_number.battery_total_capacity"
    """The total capacity of the battery"""
    time_until_charged = "sensor.battery_time_until_charged"
    """The time until the battery is fully charged"""
    time_until_discharged = "sensor.battery_time_until_discharged"
    """The time until the battery is fully discharged"""
    cells_balanced = "input_boolean.battery_cells_balanced"
    """Whether the battery cells are balanced"""
    energy = "sensor.battery_energy"
    """Calculated battery energy in kWh"""
    use_until_pv_meets_demand = "sensor.battery_use_until_pv_meets_demand"
    """Battery energy to use until PV production meets demand"""

    max_charge_price = "input_number.max_victron_charge_price"
    """Maximum price to charge the battery"""
    force_charge_up_to = "input_number.battery_force_charge_up_to"
    """Force the battery to charge up to a certain SOC"""
    force_charge_switch = "switch.victron_victron_force_charge"
    """Switch to force the battery to charge up to a certain SOC"""
    discharge_limit = "input_number.victron_discharge_power_limit"
    """The discharge power limit of the battery"""
    charge_limit = "input_number.victron_charge_power_limit"
    """The charge power limit of the battery"""
    power = "sensor.victron_battery_power"
    """The current power of the battery (positive when charging, negative when discharging)."""


class Charger:
    """Charger configuration states"""

    phases = "sensor.vestel_ecv04_num_phases"
    """The number of phases the EV charger is configured to use"""
    current_limitation = "sensor.vestel_ecv04_current"
    """The current the EV charger is configured to use (sensor)"""
    current_setting = "number.charger_maximum_current"
    """The current the EV charger is configured to use (number entity to set value)"""
    status_connector = "sensor.charger_status_connector"
    """The status of the charger connector"""
    control_switch = "switch.charger_charge_control"
    """The switch to control the charger"""
    power = "sensor.wallbox_power"
    """The power being drawn by the charger"""
    ready = "binary_sensor.ev_charger_ready"
    """Whether the EV charger is ready to charge the vehicle"""
    force_charge = "input_boolean.ev_force_charge"
    """Force EV charging, disables current / phase / surplus charging automations"""
    turned_on_by_automation = "input_boolean.ev_charging_turned_on_by_automation"
    """Whether EV charging was turned on by automation"""


class EV:
    """Electric vehicle related states"""

    battery_soc = "sensor.tesla_battery_level"
    """The current state of charge of the EV battery (tesla integration)"""
    required_soc = "input_number.tesla_requried_soc"
    """The required state of charge of the EV battery for the next planned drive"""
    capacity = 60
    """The total capacity of the EV battery in kWh"""
    auto_soc_limit = "sensor.tesla_auto_soc_limit"
    """The automatic state of charge limit for the EV"""
    planned_drives = "schedule.tesla_planned_drives"
    """The planned drives schedule for the EV"""
    planned_distance = "input_number.tesla_planned_distance"
    """The planned distance of the next drive"""
    short_term_demand = "sensor.tesla_short_term_demand"
    """The short term demand for the EV"""
    is_charging = "binary_sensor.is_charging"
    """Whether the battery is currently charging"""
    soc = "sensor.tesla_usable_battery_level"
    """The state of charge of the EV battery (usable level)"""
    pv_opportunistic_price = "sensor.pv_opportunistic_price"
    """The opportunistic price for PV energy"""
    electricity_price = "sensor.electricity_price"
    """The current electricity price"""
    energy = "sensor.ev_energy"
    """The energy of the EV"""
    energy_until_full = "sensor.ev_energy_until_full"
    """The energy the EV needs until it is full"""
    smart_charge_limit = "sensor.tesla_smart_charge_limit"
    """The smart charge limit for the EV"""
    energy_needed = "sensor.tesla_energy_needed"
    """The energy needed for the next drive (set automatically)."""


class House:
    """House energy consumption related states"""

    loads = "sensor.house_loads"
    """All power consumption belonging to the house (i.e. excluding wallbox)"""
    loads_known_devices = "sensor.house_loads_known_devices"
    """Power consumption of known devices in the house"""
    nightly_average_power = "input_number.house_nightly_average_power"
    """The average power consumption of the house during the night"""
    daily_average_power = "input_number.house_daily_average_power"
    """The average power consumption of the house during the day"""
    energy_demand = "sensor.house_energy_until_production_meets_demand"
    """The energy the house needs until the PV production next meets the demand"""
    last_washing = "input_datetime.last_washing"
    """The last time the washing machine was used"""
    upcoming_demand = "sensor.upcoming_demand"
    """The upcoming energy extra demand to plan for (e.g. washing machine, EV charging)"""
    energy_surplus = "sensor.energy_surplus"
    """Energy surplus in kWh, energy that exceeds the expected house consumption (excluding EV charging)"""
    energy_to_burn = "sensor.energy_to_burn"
    """Energy to burn in kWh, energy that exceeds expected consumption and storage capacity (including EV). 
    This can be used to e.g. heat water or drive dehumidifiers."""


class PVForecast:
    """PV forecast states"""

    forecast_today_remaining = "sensor.solcast_pv_forecast_forecast_remaining_today"
    """The forecast PV production remaining today"""
    forecast_today = "sensor.solcast_pv_forecast_forecast_today"
    """The forecast for today's PV production"""
    forecast_tomorrow = "sensor.solcast_pv_forecast_forecast_tomorrow"
    """The forecast for tomorrow's PV production"""
    forecast_day_3 = "sensor.solcast_pv_forecast_forecast_day_3"
    """The forecast for the day after tomorrow's PV production"""
    forecast_day_4 = "sensor.solcast_pv_forecast_forecast_day_4"
    """The forecast for the day after the day after tomorrow's PV production"""
    forecast_today = "sensor.solcast_pv_forecast_forecast_today"
    """The forecast for today's PV production"""
    forecast_tomorrow = "sensor.solcast_pv_forecast_forecast_tomorrow"
    """The forecast for tomorrow's PV production"""
    forecast_day_3 = "sensor.solcast_pv_forecast_forecast_day_3"
    """The forecast for the day after tomorrow's PV production"""
    forecast_day_4 = "sensor.solcast_pv_forecast_forecast_day_4"
    """The forecast for the day after the day after tomorrow's PV production"""


class PVProduction:
    """PV production states"""

    total_power = "sensor.pv_total_power"
    """The total power production of the PV system"""
    next_meet_demand = "sensor.next_pv_meet_demand"
    """The next time the PV production is expected to meet the demand"""
    energy_until_production_meets_demand = "sensor.pv_energy_until_production_meets_demand"
    """The energy the PV needs to produce until it meets the demand"""
    next_meet_demand = "sensor.next_pv_meet_demand"
    """The next time the PV production is expected to meet the demand"""
    power_now_estimated = "sensor.solcast_pv_forecast_power_now"


class Excess:
    """Excess power related states"""

    power = "sensor.excess_power"
    """The excess power, i.e. the difference between production and consumption"""
    target = "input_number.excess_target"
    """The target excess power, in W"""
    power_1m_average = "sensor.excess_power_1m_average"
    """The 1 minute average of the excess power"""
    above_target_1k = "binary_sensor.excess_1k_above_target"
    """Binary sensor indicating if excess is 1kW above target"""
    above_target = "binary_sensor.excess_above_target"
    """Binary sensor indicating if excess is above target"""
    below_target = "binary_sensor.excess_below_target"
    """Binary sensor indicating if excess is below target"""
    excess_today_remaining = "sensor.excess_today_remaining"
    energy_next_day = "sensor.excess_energy_next_day"
    """The excess energy for the next day"""
    energy_two_days = "sensor.excess_energy_next_two_days"
    """The excess energy for the next two days"""
    excess_next_three_days = "sensor.excess_energy_next_three_days"
    """The excess energy for the next three days"""


class Grid:
    """Grid related states"""

    power_ac = "sensor.ac_power"  # assuming this is grid power
    """AC power from grid"""
    power_1m_average = "sensor.grid_1m_average"
    """1 minute average grid power"""
    power_setpoint_target = "sensor.grid_power_setpoint_target"
    """Target grid power setpoint"""
    power_setpoint = "input_number.victron_setpoint"
    """Active setpoint for grid power"""
    max_feedin_target = "input_number.max_feedin_target"
    """Maximum feed-in target"""
    max_pv_feedin_target = "input_number.max_pv_feedin_target"
    """Maximum feed-in target for PV"""


class Automation:
    """Automation related states and inputs"""

    auto_ev_charging = "input_boolean.auto_ev_charging"
    """Switch to enable/disable automatic EV charging"""
    auto_battery_target_soc = "input_boolean.auto_battery_target_soc"
    """Switch to enable/disable automatic battery target SOC calculation"""
    auto_excess_target = "input_boolean.auto_excess_target"
    """Switch to enable/disable automatic excess target power calculation"""
    battery_target_soc = "input_number.battery_target_soc"
    """Target SOC for the battery, set by automation or manually"""
    min_discharge_price = "input_number.min_discharge_price"
    """Minimum electricity price to discharge battery"""
    req_energy = "sensor.req_energy"
    """Required energy calculated by automation"""
    minimal_soc = "sensor.minimal_soc"
    """Minimal SOC calculated by automation"""
    auto_setpoint = "input_boolean.auto_update_setpoint"
    """Switch to enable/disable automatic setpoint updates"""
    auto_charge_limit = "input_boolean.auto_charge_limit"
    """Switch to enable/disable automatic charge limit calculation"""
