from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class Statistics:
    """Statistics related states"""

    entity: str
    """State of the statistics"""
    power_consumption: int
    """Power consumption in W"""
    energy_consumption: int
    """Energy consumption in kWh"""
    energy_production: int
    """Energy production in kWh"""
    energy_surplus: int


class BaseTimeReference:
    def get_next(self, now: datetime) -> datetime:
        """Get the next time reference"""
        raise NotImplementedError("Subclasses must implement this method")


@dataclass
class TimeReference(BaseTimeReference):
    """The current time reference"""

    hour: int | None = None
    minute: int | None = None
    second: int | None = None
    weekdays: list[str] | None = None
    """The days of the week, e.g. ['Monday', 'Tuesday'] or ['Mon', 'Tue']"""
    months: list[str] | None = None
    """The months of the year, e.g. ['January', 'February'] or ['Jan', 'Feb']"""

    def __post_init__(self):
        assert self.hour is not None or self.minute is not None or self.second is not None, (
            "At least one of hour, minute or second must be set"
        )
        if self.weekdays is not None:
            assert all(day in weekdays for day in self.weekdays), (
                f"Invalid weekday(s) {self.weekdays}, valid are {weekdays}"
            )

    def get_next(self, now: datetime) -> datetime:
        """Get the next time reference"""
        next = now.replace(
            hour=self.hour if self.hour is not None else now.hour,
            minute=self.minute if self.minute is not None else now.minute,
            second=self.second if self.second is not None else now.second,
        )
        if self.months is not None and (month := now.strftime("%b") not in self.months):
            while month not in self.months:
                next = next.replace(month=now.month + 1, day=1, minute=0, hour=0)

        if self.weekdays is not None and (day := now.strftime("%a")) not in self.weekdays:
            while day not in self.weekdays:
                next = next.replace(day=now.day + 1, minute=0, hour=0)

        if next < now:
            next = next.replace(day=now.day + 1)
        return next

    def is_active(self, now: datetime) -> bool:
        """Check if the time reference is active"""
        next = self.get_next(now)
        if now > next:
            return False
        td = next - now
        return td.total_seconds() < self.slack


class Occasion:
    """Abstract class to represent a point in time, e.g. day of the week, hour of day"""

    def is_active(self, now: datetime) -> bool:
        """Check if the point in time is now"""
        raise NotImplementedError("Subclasses must implement this method")

    def when_next(self, now: datetime | None = None) -> datetime:
        """Get the next time the point in time occurs"""
        raise NotImplementedError("Subclasses must implement this method")


@dataclass
class TimeFrame(Occasion):
    """Abstract class for time frames, e.g. hour of day, day of week"""

    start: TimeReference
    end: TimeReference

    def is_active(self, now: datetime) -> bool:
        return self.start <= now <= self.end

    def when_next(self, point_of_reference: datetime | None = None) -> datetime:
        if point_of_reference is None:
            point_of_reference = datetime.now()
        return point_of_reference.replace(hour=self.start.hour, minute=self.start.minute)


class Schedule:
    """Abstract class for Schedule classes"""

    def is_active(self, now: datetime) -> bool:
        """Check if the schedule should run at the given time"""
        raise NotImplementedError("Subclasses must implement this method")

    def next_start(self, now: datetime) -> datetime:
        """Get the next time the schedule should run"""
        raise NotImplementedError("Subclasses must implement this method")

    def next_stop(self, now: datetime) -> datetime:
        """Get the next time the schedule should stop"""
        raise NotImplementedError("Subclasses must implement this method")


@dataclass
class PowerState:
    """Information about the generator state."""

    power: int
    """Power in W (positive = consuming, negative = producing)"""

    mode: str
    """Generic mode of the generator, exact meaning depends on the generator"""


@dataclass
class Device:
    active: bool | None = None
    """Information about the AC device state, `None` means unknown."""

    state: PowerState | None = None
    """Current state of the device, `None` means unknown."""

    last_state: PowerState | None = None
    """Last state of the device, `None` means unknown."""

    last_active: datetime | None = None
    """Last time the device was active, `None` means unknown."""

    power_states: list[PowerState] = field(default_factory=list)
    """Possible power states of the device"""


@dataclass
class PowerGenerator(Device):
    max_power: int
    """Maximum power of the generator in W"""


@dataclass
class BatteryInverter(PowerGenerator):
    """Information about a battery inverter."""

    operating_mode: Literal["on", "absorption", "float", "off"]
    """Operating mode of the inverter"""
    capacity: int
    """Total capacity of the battery in kWh. If there's no battery attached, set capacity to 0."""
    state_of_charge: int
    """Current state of charge of the battery in kWh"""
    max_charge_power: int
    """Maximum charge power of the battery in W"""
    setpoint: int
    """Setpoint of the inverter in W (positive = charging, negative = discharging). 
    The point of reference is assumed to be the grid. So setpoint = 0 means no power is flowing to the grid.
    The setpoint is used to control the power flow to the grid, allowing for efficient energy management."""

    optimal_power_point_factor: float = 0.5
    """Largest fraction of max power where inverter is still at ~ peak efficiency (>90%).

    Example:
   
    > If 1500W is an efficient operating point for a 3kW inverter, the optimal_power_point would be 0.5.
    
    You probably don't have to touch this, but if you're curious:

    The optimal power point is the point where the efficiency is highest (smaller loss/power), usually around half or less
    of the inverter's max power (higher -> more heat loss, lower -> overhead increases).

    Automations may use this in order to keep the inverter at an efficient
    operating point (e.g. discharge battery at most with 1500W into the grid unless really worth it)
    """

    optimal_power_point_efficiency: float = 0.95
    """Efficiency of the inverter working at optimal power point. This is usually around 0.95,
    but for some inverters it may be higher or lower. 95% is a good value to start with.
    """


@dataclass
class PVInverter(BatteryInverter):
    """Information about a PV inverter. If there's no battery attached, set capactiy to 0."""

    # TODO: which additional information do we need?


@dataclass
class StrategyContext:
    """Context for the strategy"""

    now: datetime
    """The current time"""
    pv_forecast: dict[TimeFrame, float]
    """The PV forecast for the next days"""
    feedin_prices: dict[TimeFrame, float]
    """The feed-in prices for the next days"""
    electricity_prices: dict[TimeFrame, float]
    """The electricity prices for the next days"""
    # generators: list[InverterState]

    # pv_production: PVProduction
    # """The PV production"""
    # excess: Excess
    # """The excess power"""
    # grid: Grid
    # """The grid power"""
    # house: House
    # """The house power consumption"""


class Strategy:
    def activate(self, now: datetime, context: "list[DeferrableLoad]") -> bool:
        """Check if the strategy should be activated at the given time"""
        raise NotImplementedError("Subclasses must implement this method")

    def deactivate(self, now: datetime, context: "list[DeferrableLoad]") -> bool:
        """Check if the strategy should be deactivated at the given time"""
        raise NotImplementedError("Subclasses must implement this method")


@dataclass
class DeferrableLoad:
    """Deferrable load related states"""

    entity: str
    """State of the deferrable load"""
    power_consumption: int
    """Power consumption of the deferrable load in W"""
    when_to_run: list[Schedule] | None = None
    """How often this should run"""
    turn_on_automatically: bool = True
    """Whether this should be turned on automatically"""
    turn_off_automatically: bool = True
    """Whether this should be turned off automatically"""
    last_run: datetime | None = None
    """The last time this was run"""


def fix_entry_repr(entry_repr):
    entry_repr = (
        entry_repr.replace(", tzinfo=zoneinfo.ZoneInfo(key='Europe/Berlin')", "")
        .replace("datetime.datetime", "")
        .replace("define_interfaces.<locals>.", "")
        .replace(", 0), ", ",  0), ")
    )
    import re

    entry_repr = re.sub(r"(\d+).[\d]+", r"\1", entry_repr)
    return entry_repr


@dataclass
class ForecastEntry:
    period_start: str
    pv_estimate: float
    battery_energy: float
    house_power: float
    setpoint: int
    power_draw: float
    energy_use: float
    energy_production: float
    free_capacity: float
    accumulated_energy: float
    feedin: float
    price: float
    battery_power: float
    setpoint_spread: float

    def format(self):
        return fix_entry_repr(str(self))[len(type(self).__name__) + 1 : -1]


@dataclass
class SetpointResult:
    setpoint: int
    min_bat: float
    t_min_bat: datetime
    max_bat: float
    t_max_bat: datetime
    max_feedin: float
    t_max_feedin: datetime
    setpoint_spread: float
    prices_mean: float
    prices_std: float
    detail: list[ForecastEntry]

    def format(self):
        return fix_entry_repr(str(self))


@dataclass
class PVForecastWithPrices:
    period_start: datetime
    pv_estimate: float
    price_per_kwh: float = 0
