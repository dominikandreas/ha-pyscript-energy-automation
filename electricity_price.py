from datetime import timedelta
from typing import TYPE_CHECKING

if not TYPE_CHECKING:
    # pyscript providese these imports from the subfolder modules directly
    from states import EV, ElectricityPrices
    from utils import now, set
else:
    # The type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    from modules.states import EV, ElectricityPrices
    from modules.utils import now, set, time_trigger, log


@time_trigger
async def set_pv_opportunistic_price():
    set(EV.pv_opportunistic_price, 0.08, unit_of_measurement="EUR/kWh")


@time_trigger
@time_trigger("cron(*/3 * * * *)")
async def set_prices():
    t = now()
    today, tomorrow = [], []

    def get_price(hour: int, minute: int) -> float: 
        """Return price based on time of day."""
        # TODO: change this to hour 23
        if (hour == 23 and minute >= 30) or (hour <= 4) or (hour == 5 and minute < 30):
            return 0.175
        return 0.255
 
    for day_offset in (0, 1):
        for hour in range(0, 24):
            for minute in (0, 30):
                date = t.replace(hour=hour, minute=minute) + timedelta(days=day_offset)
                price = get_price(hour, minute)
                if day_offset == 0:
                    today.append({"startsAt": date.isoformat(), "total": price})
                else:
                    tomorrow.append({"startsAt": date.isoformat(), "total": price})

    price = get_price(t.hour, t.minute)

    # log.warning(f"{get_price(t.hour, t.minute)} {'\nt'.join([f"{el['startsAt']} - {el['total']}" for el in today + tomorrow])}")

    log.warning(f"{t.hour}:{t.minute} current price: {price}€")

    set(
        ElectricityPrices.current_price,
        price,
        unit_of_measurement="EUR/kWh",
        state_class="measurement",
        device_class="monetary",
        today=today,
        tomorrow=tomorrow,
    )

    if price < 0.2:
        set(ElectricityPrices.low_price, "on")
        set(ElectricityPrices.high_price, "off")
    else:
        set(ElectricityPrices.low_price, "off")
        set(ElectricityPrices.high_price, "on")
