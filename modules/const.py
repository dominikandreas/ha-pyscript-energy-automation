
class EV:
    ev_capacity = 60
    """The capacity of the EV battery in kWh"""
    ev_consumption_per_drive = 55
    """How much percent of the battery is consumed per drive"""
    ev_days_allowed_to_reach_target = 7
    """The average days it should take to reach the target state of charge.

    In order to get the vehicle charged, the schedule allows to define the next
    planned drive. This variable is a fallback to be used when no schedule is defined,
    to control how much energy shall be requested for the vehicle per day.
    """

    ev_phase_switch_delay = 20
    """The delay in seconds between switching the number of phases of the EV charger"""
    max_current = 16
    """The maximum current the EV charger can provide"""
    min_current = 6