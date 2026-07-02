import logging
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from math import sqrt
from typing import Any, Literal


@dataclass(kw_only=True)
class ForecastEntry:
    t_start: datetime
    import_price: int
    export_price: int
    pv_total_power: int


@dataclass(kw_only=True)
class Forecast:
    _interval_hours: int | None = None
    entries: list[ForecastEntry]

    @property
    def interval_hours(self) -> int:
        if self._interval_hours is not None:
            return self._interval_hours

        assert len(self.entries) >= 2, "Cannot determine interval from less than 2 entries."

        return (self.entries[1].t_start - self.entries[0].t_start).total_seconds() / 60 / 60


@dataclass(kw_only=True)
class Statistic:
    mean: float
    std: float
    trend: float
    min_val: float
    max_val: float
    n_samples: int
    window_hours: int
    first_updated: datetime
    last_updated: datetime
    last_value: float
    samples: deque[tuple[datetime, float]] = field(default_factory=deque, repr=False)

    def __post_init__(self) -> None:
        if self.samples:
            self.samples = deque(sorted(self.samples, key=lambda sample: sample[0]))
            self._trim_window(self.samples[-1][0])
            self._recalculate()
            return

        if self.n_samples > 0 and not self.samples:
            logging.warning(
                "Statistic initialized without sample history; seeding rolling window from last_value only. "
                "Use Statistic.from_samples() or Statistic.from_dict() for exact restoration."
            )
            self.samples.append((self.last_updated, self.last_value))
            self._recalculate()

    @classmethod
    def empty(cls, window_hours: int, observed_at: datetime | None = None) -> "Statistic":
        observed_at = observed_at or datetime.now()
        return cls(
            mean=0.0,
            std=0.0,
            trend=0.0,
            min_val=0.0,
            max_val=0.0,
            n_samples=0,
            window_hours=window_hours,
            first_updated=observed_at,
            last_updated=observed_at,
            last_value=0.0,
        )

    @classmethod
    def from_samples(
        cls, samples: list[tuple[datetime, float]], window_hours: int, observed_at: datetime | None = None
    ) -> "Statistic":
        observed_at = observed_at or (samples[-1][0] if samples else datetime.now())
        statistic = cls.empty(window_hours=window_hours, observed_at=observed_at)
        statistic.samples = deque(sorted(samples, key=lambda sample: sample[0]))
        statistic._trim_window(observed_at)
        statistic._recalculate()
        return statistic

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_hours": self.window_hours,
            "first_updated": self.first_updated.isoformat(),
            "last_updated": self.last_updated.isoformat(),
            "last_value": self.last_value,
            "samples": [
                {"observed_at": observed_at.isoformat(), "value": value} for observed_at, value in self.samples
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Statistic":
        samples = [
            (datetime.fromisoformat(sample["observed_at"]), float(sample["value"]))
            for sample in data.get("samples", [])
        ]
        observed_at_raw = data.get("last_updated")
        observed_at = datetime.fromisoformat(observed_at_raw) if observed_at_raw else None
        return cls.from_samples(samples=samples, window_hours=int(data["window_hours"]), observed_at=observed_at)

    def update(self, new_value: float, observed_at: datetime | None = None) -> None:
        """Update the statistic with a new value inside the active time window."""
        observed_at = observed_at or datetime.now()
        self.samples.append((observed_at, new_value))
        self._trim_window(observed_at)
        self._recalculate()

    def _trim_window(self, observed_at: datetime) -> None:
        window_start = observed_at - timedelta(hours=self.window_hours)
        while self.samples and self.samples[0][0] < window_start:
            self.samples.popleft()

    def _recalculate(self) -> None:
        if not self.samples:
            self.mean = 0.0
            self.std = 0.0
            self.trend = 0.0
            self.min_val = 0.0
            self.max_val = 0.0
            self.n_samples = 0
            return

        timestamps, values = zip(*self.samples)
        self.n_samples = len(values)
        self.first_updated = timestamps[0]
        self.last_updated = timestamps[-1]
        self.last_value = values[-1]
        self.mean = sum(values) / self.n_samples
        self.std = sqrt(sum((value - self.mean) ** 2 for value in values) / self.n_samples)
        self.min_val = min(values)
        self.max_val = max(values)
        self.trend = self._calculate_trend()

    def _calculate_trend(self) -> float:
        if len(self.samples) < 2:
            return 0.0

        start_time = self.samples[0][0]
        x_values = [(timestamp - start_time).total_seconds() / 60 for timestamp, _ in self.samples]
        y_values = [value for _, value in self.samples]
        x_mean = sum(x_values) / len(x_values)
        y_mean = sum(y_values) / len(y_values)
        denominator = sum((x_value - x_mean) ** 2 for x_value in x_values)
        if denominator == 0:
            return 0.0

        numerator = sum((x_value - x_mean) * (y_value - y_mean) for x_value, y_value in zip(x_values, y_values))
        return numerator / denominator


@dataclass(kw_only=True)
class Statistics:
    hourly: Statistic | None = None
    daily: Statistic | None = None
    weekly: Statistic | None = None
    monthly: Statistic | None = None
    quaterly: Statistic | None = None
    yearly: Statistic | None = None


@dataclass(kw_only=True)
class Device:
    name: str
    """The name of the device, e.g. "washing_machine"."""
    power: int
    """The current power of the device in W, where a positive value means consuming and a negative value means generating."""
    variable_load: bool = False
    """Whether the device is a variable load or not, which means that its consumption can be shifted in time."""

    def act(self, state: "State") -> "State":
        """Act on the given state based on the current power."""
        return self.__call__(state)

    def __call__(self, state: "State") -> "State":
        """The actual implementation of the act method, which can be overridden by subclasses."""
        return state


@dataclass(kw_only=True)
class Generator:
    name: str
    """The name of the generator, e.g. Roof PV."""
    power: int
    """The current power of the generator in W, where a positive value means generating and a negative value means consuming."""
    energy: int
    """The current energy generated by the generator in kWh."""
    derated_power: int
    """The derated power of the generator in W, which is the maximum power it can generate."""
    power_limit: int = -1
    """The power limit for the generator in W, which is the maximum power it can generate. -1 means no limit."""
    mode: Literal["off",]


@dataclass(kw_only=True)
class BatteryInverter(Generator):
    setpoint: int
    """The current / target setpoint for the battery in W."""
    power: int
    """The current power of the battery in W, where a positive value means charging and a negative value means discharging."""
    energy: int
    """The current energy stored in the battery in kWh."""
    capacity: int
    """The capacity of the battery in kWh."""
    battery_floor: int = 5
    """The minimum state of charge of the battery in kWh."""

    @property
    def soc(self) -> float:
        """The state of charge of the battery in percentage."""
        return self.energy / self.capacity * 100

    def act(self, state: "State") -> "State":
        """Act on the given state based on the current setpoint and power."""
        if self.energy <= self.battery_floor:
            # Don't charge the battery if we're at or below the battery floor
            print("Battery is at or below floor, not charging.")
            return state


class RawMeasurements:
    production: int
    """The current renewable production in W."""
    consumption: int
    """The current consumption in W."""
    import_price: float
    """The current import price in EUR/kWh."""
    export_price: float
    """The current export price in EUR/kWh."""


@dataclass(kw_only=True)
class PVInverterState:
    mode: str
    """The current mode of the PV inverter, e.g. "off", "feed_in", "self_consumption", etc."""
    power: int
    """The current power of the PV inverter in W"""
    power_limit: int
    """The power limit for the PV inverter in W, which is the maximum power it can generate."""


@dataclass(kw_only=True)
class PVInverter:
    state: PVInverterState

    def act(self, state: "State") -> "State":
        """Act on the given state based on the current mode and power."""
        return state


@dataclass
class State:
    raw: RawMeasurements

    # Historical values
    price_import_history: Statistics
    """The historical statistics for the price of importing energy from the grid."""
    price_export_history: Statistics
    """The historical statistics for the price of exporting energy to the grid."""

    avg_energy_to_grid: int
    """The average energy fed into the grid."""
    avg_production: int
    """The average production."""
    avg_consumption: int
    """The average consumption."""

    # Estimated values
    surplus: int
    """The estimated surplus (kWh) considering the production and consumption."""
    surplus_wo_variable_loads: int
    """The estimated surplus without considering variable loads."""

    # Forecast
    forecast: Forecast
    """The forecast for the next 24 hours or so."""

    # Derived from forecast and historical values
    expected_surplus_today: int
    """Computed by estimating the future demand for the next days and how much we'll have at the end of the day."""
    expected_vl_consumption: int
    """The expected variable load consumption to subtract from the expected surplus."""
    expected_surplus_after_vl: int
    """The expected surplus after considering the variable load consumption."""

    # Logical Entities
    @property
    def is_surplus(self) -> bool:
        """Whether we have a surplus of energy or not."""
        return self.surplus > 0

    @property
    def is_low_price(self) -> bool:
        """Whether the current price is low or not."""
        weekly_stats = self.price_import_history.weekly
        if weekly_stats is None:
            return False

        low_price_threshold = max(weekly_stats.min_val, weekly_stats.mean - weekly_stats.std / 2)
        return self.raw.import_price <= low_price_threshold


@dataclass(kw_only=True)
class Constraint:
    name: str
    description: str
    priority: int
    """The priority of the constraint, where a lower number means a higher priority."""

    def apply(self, state: State) -> State:
        """Apply the constraint to the given state and return the modified state."""
        # This is a placeholder implementation. The actual logic will depend on the specific constraint.
        return state


@dataclass
class Constraints:
    entries: list[Constraint]

    def __iter__(self):
        return (e for e in sorted(self.entries, key=lambda c: c.priority))

    def apply(self, state: State) -> State:
        """Apply all constraints to the given state in order of priority."""
        for constraint in self:
            state = constraint.apply(state)
        return state


#### Active Constraints ####


@dataclass(kw_only=True)
class BaseLoadConsumption(Constraint):
    name: str = "house_consumption"
    avg_daily_power: int
    avg_nightly_power: int
    description: str = "House power must be accounted for"


@dataclass(kw_only=True)
class DontUseBatteryDuringCheapPricesIfNoSurplus(Constraint):
    name: str = "no_battery_during_cheap_prices_if_no_surplus"
    description: str = "Don't use the battery to charge during cheap prices if we don't have a surplus."

    def apply(self, state: State) -> State:
        if state.expected_surplus_after_vl <= 0:
            # This is a placeholder implementation. The actual logic will depend on how the battery usage is represented in the state.
            print("Applying constraint: Don't use battery during cheap prices if no surplus.")
            replace(state, inverter_mode="off")
        return state
