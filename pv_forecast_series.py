"""Publish daily PV forecast-vs-actual series from recorder statistics.

The PV dashboard chart reads these pre-computed arrays from the sensor
attributes, so the card itself needs no recorder queries.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from sqlalchemy import text

UPDATE_INTERVAL_SECONDS = 900
SERIES_DAYS = 30
LOCAL_TIMEZONE = ZoneInfo("Europe/Berlin")

FC_ENTITY = "sensor.solcast_pv_forecast_forecast_today"
INV_ENTITY = "sensor.inverter_daily_yield"
DTU_ENTITY = "sensor.opendtu_6dd1ec_yield_total"

DAILY_SERIES_SQL = """
WITH fc AS (
    SELECT DISTINCT ON ((to_timestamp(start_ts) AT TIME ZONE 'Europe/Berlin')::date)
        (to_timestamp(start_ts) AT TIME ZONE 'Europe/Berlin')::date AS d,
        state AS fc
    FROM statistics s JOIN statistics_meta m ON m.id = s.metadata_id
    WHERE m.statistic_id = 'sensor.solcast_pv_forecast_forecast_today'
      AND start_ts > extract(epoch from now() - interval '32 days')
    ORDER BY (to_timestamp(start_ts) AT TIME ZONE 'Europe/Berlin')::date, start_ts
),
inv AS (
    SELECT (to_timestamp(start_ts) AT TIME ZONE 'Europe/Berlin')::date AS d,
           MAX(sum) - MIN(sum) AS kwh
    FROM statistics s JOIN statistics_meta m ON m.id = s.metadata_id
    WHERE m.statistic_id = 'sensor.inverter_daily_yield'
      AND start_ts > extract(epoch from now() - interval '32 days')
    GROUP BY 1
),
dtu AS (
    SELECT (to_timestamp(start_ts) AT TIME ZONE 'Europe/Berlin')::date AS d,
           MAX(sum) - MIN(sum) AS kwh
    FROM statistics s JOIN statistics_meta m ON m.id = s.metadata_id
    WHERE m.statistic_id = 'sensor.opendtu_6dd1ec_yield_total'
      AND start_ts > extract(epoch from now() - interval '32 days')
    GROUP BY 1
)
SELECT fc.d AS d, fc.fc AS fc, inv.kwh + dtu.kwh AS act
FROM fc JOIN inv USING (d) JOIN dtu USING (d)
WHERE fc.d < (now() AT TIME ZONE 'Europe/Berlin')::date
  AND fc.d >= (now() AT TIME ZONE 'Europe/Berlin')::date - 30
ORDER BY d
"""


def build_series(rows):
    """Turn daily (d, fc, act) rows into chart-ready arrays.

    Days with missing values are skipped. Timestamps are local noon
    in epoch milliseconds so every series shares identical x values.
    """
    days, t, fc, act, dev = [], [], [], [], []
    for row in rows:
        d = row.get("d")
        try:
            f = float(row.get("fc"))
            a = float(row.get("act"))
        except (TypeError, ValueError):
            continue
        if d is None:
            continue
        noon = datetime.combine(d, time(12, 0), tzinfo=LOCAL_TIMEZONE)
        days.append(d.isoformat())
        t.append(int(noon.timestamp() * 1000))
        fc.append(round(f, 2))
        act.append(round(a, 2))
        dev.append(round(a - f, 2))
    return {"days": days, "t": t, "fc": fc, "act": act, "dev": dev}


@pyscript_compile
def _read_daily_rows(recorder):
    with recorder.get_session() as session:
        rows = session.execute(text(DAILY_SERIES_SQL)).mappings().all()
        return [dict(row) for row in rows]


@service
@time_trigger
@time_trigger(f"period(now, {UPDATE_INTERVAL_SECONDS}sec)")
async def update_pv_forecast_series():
    task.unique("update_pv_forecast_series", kill_me=True)

    try:
        recorder = get_instance(hass)
        rows = await recorder.async_add_executor_job(_read_daily_rows, recorder)
        series = build_series(rows)
        if not series["days"]:
            raise RuntimeError("no complete forecast/actual days in recorder statistics")
        state.set(
            "sensor.pv_forecast_daily",
            value=series["dev"][-1],
            new_attributes={
                "friendly_name": "PV forecast daily series",
                "icon": "mdi:solar-power",
                "device_class": "energy",
                "unit_of_measurement": "kWh",
                "days": series["days"],
                "t": series["t"],
                "fc": series["fc"],
                "act": series["act"],
                "dev": series["dev"],
                "calculated_at": datetime.now(LOCAL_TIMEZONE).isoformat(),
            },
        )
        log.info("PV forecast series updated: %d days", len(series["days"]))
    except Exception as err:
        log.error("PV forecast series update failed: %s", err)
