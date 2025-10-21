from datetime import timedelta
from typing import TYPE_CHECKING

if not TYPE_CHECKING:
    # pyscript providese these imports from the subfolder modules directly
    from electricity_price import get_price, is_low_price
    from states import EV, ElectricityPrices
    from utils import now, set_state
else:
    # The type checker (linter) does not know that utils can directly be imported in the pyscript engine.
    from modules.electricity_price import get_price, is_low_price
    from modules.states import EV, ElectricityPrices
    from modules.utils import log, now, set_state, time_trigger


@time_trigger
async def set_pv_opportunistic_price():
    set_state(EV.pv_opportunistic_price, 0.08, unit_of_measurement="EUR/kWh")


@time_trigger
@time_trigger("cron(*/3 * * * *)")
async def set_prices():
    t = now()
    today, tomorrow = [], []

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

    set_state(
        ElectricityPrices.current_price,
        price,
        unit_of_measurement="EUR/kWh",
        state_class="measurement",
        device_class="monetary",
        today=today,
        tomorrow=tomorrow,
    )

    is_low = is_low_price(price)

    set_state(ElectricityPrices.low_price, "on" if is_low else "off")
    set_state(ElectricityPrices.high_price, "off" if is_low else "on")
