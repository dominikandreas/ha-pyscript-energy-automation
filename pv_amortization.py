"""Publish PV payback metrics from lifetime counters and recorder statistics."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from sqlalchemy import text


INVESTMENT_EUR = 17_950.0
UPDATE_INTERVAL_SECONDS = 900
LOCAL_TIMEZONE = ZoneInfo("Europe/Berlin")

STATISTICS_SQL = """
WITH ids AS (
    SELECT id, statistic_id
    FROM statistics_meta
    WHERE statistic_id IN (
        'sensor.inverter_daily_yield',
        'sensor.garage_pv_energy',
        'sensor.hoymiles_hms_wifi_41439237_total_daily_yield',
        'sensor.grid_reverse',
        'sensor.grid_reverse_compensation',
        'sensor.electricity_price',
        'sensor.battery_charged_energy',
        'sensor.battery_discharged_energy'
    )
)
SELECT
    i.statistic_id,
    min(s.start_ts) AS first_ts,
    max(s.start_ts) AS last_ts,
    min(s.sum) AS min_sum,
    max(s.sum) AS max_sum,
    avg(s.mean) AS avg_mean,
    min(s.sum) FILTER (
        WHERE s.start_ts >= extract(epoch FROM now() - interval '365 days')
    ) AS year_min_sum,
    max(s.sum) FILTER (
        WHERE s.start_ts >= extract(epoch FROM now() - interval '365 days')
    ) AS year_max_sum,
    avg(s.mean) FILTER (
        WHERE s.start_ts >= extract(epoch FROM now() - interval '365 days')
    ) AS year_avg_mean,
    count(*) AS row_count
FROM ids i
JOIN statistics s ON s.metadata_id = i.id
GROUP BY i.statistic_id
"""


@pyscript_compile
def _read_statistics(recorder):
    with recorder.get_session() as session:
        rows = session.execute(text(STATISTICS_SQL)).mappings().all()
        return [dict(row) for row in rows]


@pyscript_compile
def _number(value, default=0.0):
    try:
        result = float(value)
        if result != result or result in (float("inf"), float("-inf")):
            return default
        return result
    except (TypeError, ValueError):
        return default


@pyscript_compile
def _year_delta(row):
    if not row:
        return 0.0
    start = _number(row.get("year_min_sum"))
    end = _number(row.get("year_max_sum"))
    return max(end - start, 0.0)


@service
@time_trigger
@time_trigger(f"period(now, {UPDATE_INTERVAL_SECONDS}sec)")
async def update_pv_amortization():
    task.unique("update_pv_amortization", kill_me=True)

    try:
        recorder = get_instance(hass)
        rows = await recorder.async_add_executor_job(_read_statistics, recorder)
        stats = {row["statistic_id"]: row for row in rows}

        required = {
            "sensor.inverter_daily_yield",
            "sensor.garage_pv_energy",
            "sensor.hoymiles_hms_wifi_41439237_total_daily_yield",
            "sensor.grid_reverse",
            "sensor.grid_reverse_compensation",
            "sensor.electricity_price",
            "sensor.battery_charged_energy",
            "sensor.battery_discharged_energy",
        }
        missing = sorted(required - set(stats))
        if missing:
            raise RuntimeError(f"Missing recorder statistics: {', '.join(missing)}")

        roof_total_kwh = _number(state.get("sensor.inverter_total_yield"))
        garage_total_kwh = _number(state.get("sensor.garage_pv_energy"))
        shed_total_kwh = (
            _number(state.get("sensor.hoymiles_hms_wifi_41439237_pv_1_energy_total"))
            + _number(state.get("sensor.hoymiles_hms_wifi_41439237_pv_2_energy_total"))
        ) / 1000.0
        exported_total_kwh = _number(state.get("sensor.grid_reverse"))

        generated_total_kwh = roof_total_kwh + garage_total_kwh + shed_total_kwh
        self_consumed_total_kwh = max(generated_total_kwh - exported_total_kwh, 0.0)

        price_row = stats["sensor.electricity_price"]
        import_price_avg = _number(price_row.get("avg_mean"))
        import_price_year = _number(price_row.get("year_avg_mean"), import_price_avg)
        if import_price_avg <= 0:
            import_price_avg = _number(state.get("sensor.electricity_price"))
        if import_price_year <= 0:
            import_price_year = import_price_avg

        export_row = stats["sensor.grid_reverse"]
        compensation_row = stats["sensor.grid_reverse_compensation"]
        tracked_export_kwh = _number(export_row.get("max_sum"))
        tracked_compensation_eur = _number(compensation_row.get("max_sum"))
        feed_in_price_avg = (
            tracked_compensation_eur / tracked_export_kwh if tracked_export_kwh > 0 else 0.08
        )

        untracked_export_kwh = max(exported_total_kwh - tracked_export_kwh, 0.0)
        feed_in_revenue_eur = tracked_compensation_eur + untracked_export_kwh * feed_in_price_avg
        self_consumption_savings_eur = self_consumed_total_kwh * import_price_avg
        total_return_eur = self_consumption_savings_eur + feed_in_revenue_eur
        remaining_eur = max(INVESTMENT_EUR - total_return_eur, 0.0)
        progress_percent = total_return_eur / INVESTMENT_EUR * 100.0

        generated_year_kwh = (
            _year_delta(stats["sensor.inverter_daily_yield"])
            + _year_delta(stats["sensor.garage_pv_energy"])
            + _year_delta(stats["sensor.hoymiles_hms_wifi_41439237_total_daily_yield"]) / 1000.0
        )
        exported_year_kwh = _year_delta(export_row)
        self_consumed_year_kwh = max(generated_year_kwh - exported_year_kwh, 0.0)
        feed_in_revenue_year_eur = _year_delta(compensation_row)
        self_consumption_savings_year_eur = self_consumed_year_kwh * import_price_year
        annual_return_eur = self_consumption_savings_year_eur + feed_in_revenue_year_eur

        calculated_at = datetime.now(LOCAL_TIMEZONE)
        if remaining_eur <= 0:
            forecast_date = calculated_at.date().isoformat()
        elif annual_return_eur > 0:
            payback_days = remaining_eur / annual_return_eur * 365.2425
            forecast_date = (calculated_at + timedelta(days=payback_days)).date().isoformat()
        else:
            forecast_date = None

        # Split the already calculated system return without double counting:
        # battery gets only its estimated storage uplift versus direct export;
        # direct self-consumption savings and feed-in revenue are distributed
        # across PV arrays by their actual lifetime/yearly generation shares.
        battery_charged_total_kwh = _number(state.get("sensor.battery_charged_energy"))
        battery_discharged_total_kwh = _number(state.get("sensor.battery_discharged_energy"))
        battery_benefit_eur = max(
            battery_discharged_total_kwh * import_price_avg
            - battery_charged_total_kwh * feed_in_price_avg,
            0.0,
        )
        battery_benefit_eur = min(battery_benefit_eur, self_consumption_savings_eur)
        direct_self_consumption_savings_eur = max(
            self_consumption_savings_eur - battery_benefit_eur,
            0.0,
        )

        battery_charged_year_kwh = _year_delta(stats["sensor.battery_charged_energy"])
        battery_discharged_year_kwh = _year_delta(stats["sensor.battery_discharged_energy"])
        feed_in_price_year = (
            feed_in_revenue_year_eur / exported_year_kwh
            if exported_year_kwh > 0
            else feed_in_price_avg
        )
        battery_benefit_year_eur = max(
            battery_discharged_year_kwh * import_price_year
            - battery_charged_year_kwh * feed_in_price_year,
            0.0,
        )
        battery_benefit_year_eur = min(
            battery_benefit_year_eur,
            self_consumption_savings_year_eur,
        )
        direct_self_consumption_savings_year_eur = max(
            self_consumption_savings_year_eur - battery_benefit_year_eur,
            0.0,
        )

        component_specs = [
            ("roof", "Dach-PV", "mdi:home-roof", 11_000.0, roof_total_kwh, _year_delta(stats["sensor.inverter_daily_yield"])),
            ("garage", "Garage", "mdi:garage", 2_800.0, garage_total_kwh, _year_delta(stats["sensor.garage_pv_energy"])),
            (
                "shed",
                "Schuppen",
                "mdi:greenhouse",
                650.0,
                shed_total_kwh,
                _year_delta(stats["sensor.hoymiles_hms_wifi_41439237_total_daily_yield"]) / 1000.0,
            ),
        ]
        components = []
        allocated_return_eur = 0.0
        allocated_annual_return_eur = 0.0
        for key, name, icon, cost_eur, energy_kwh, energy_year_kwh in component_specs:
            lifetime_share = energy_kwh / generated_total_kwh if generated_total_kwh > 0 else 0.0
            yearly_share = energy_year_kwh / generated_year_kwh if generated_year_kwh > 0 else 0.0
            own_savings_eur = direct_self_consumption_savings_eur * lifetime_share
            component_feed_in_eur = feed_in_revenue_eur * lifetime_share
            component_return_eur = own_savings_eur + component_feed_in_eur
            component_annual_return_eur = (
                direct_self_consumption_savings_year_eur + feed_in_revenue_year_eur
            ) * yearly_share
            component_progress_percent = (
                component_return_eur / cost_eur * 100.0 if cost_eur > 0 else 0.0
            )
            component_remaining_eur = max(cost_eur - component_return_eur, 0.0)
            if component_remaining_eur <= 0:
                component_forecast_date = calculated_at.date().isoformat()
            elif component_annual_return_eur > 0:
                component_days = component_remaining_eur / component_annual_return_eur * 365.2425
                component_forecast_date = (
                    calculated_at + timedelta(days=component_days)
                ).date().isoformat()
            else:
                component_forecast_date = None
            components.append(
                {
                    "key": key,
                    "name": name,
                    "icon": icon,
                    "cost_eur": round(cost_eur, 2),
                    "return_eur": round(component_return_eur, 2),
                    "progress_percent": round(component_progress_percent, 1),
                    "forecast_date": component_forecast_date,
                    "annual_return_eur": round(component_annual_return_eur, 2),
                    "self_consumption_savings_eur": round(own_savings_eur, 2),
                    "feed_in_revenue_eur": round(component_feed_in_eur, 2),
                    "generation_share_percent": round(lifetime_share * 100.0, 1),
                }
            )
            allocated_return_eur += component_return_eur
            allocated_annual_return_eur += component_annual_return_eur

        battery_progress_percent = battery_benefit_eur / 3_500.0 * 100.0
        battery_remaining_eur = max(3_500.0 - battery_benefit_eur, 0.0)
        if battery_remaining_eur <= 0:
            battery_forecast_date = calculated_at.date().isoformat()
        elif battery_benefit_year_eur > 0:
            battery_days = battery_remaining_eur / battery_benefit_year_eur * 365.2425
            battery_forecast_date = (
                calculated_at + timedelta(days=battery_days)
            ).date().isoformat()
        else:
            battery_forecast_date = None
        components.append(
            {
                "key": "battery",
                "name": "Speicher",
                "icon": "mdi:battery-high",
                "cost_eur": 3_500.0,
                "return_eur": round(battery_benefit_eur, 2),
                "progress_percent": round(battery_progress_percent, 1),
                "forecast_date": battery_forecast_date,
                "annual_return_eur": round(battery_benefit_year_eur, 2),
                "self_consumption_savings_eur": round(battery_benefit_eur, 2),
                "feed_in_revenue_eur": 0.0,
                "generation_share_percent": None,
            }
        )
        allocated_return_eur += battery_benefit_eur
        allocated_annual_return_eur += battery_benefit_year_eur

        state.set(
            "sensor.pv_amortization",
            value=round(progress_percent, 1),
            new_attributes={
                "friendly_name": "Amortisation der PV-Anlage",
                "icon": "mdi:solar-panel-large",
                "unit_of_measurement": "%",
                "state_class": "measurement",
                "investment_eur": INVESTMENT_EUR,
                "roof_cost_eur": 11_000.0,
                "garage_cost_eur": 2_800.0,
                "shed_cost_eur": 650.0,
                "battery_cost_eur": 3_500.0,
                "generated_energy_kwh": round(generated_total_kwh, 2),
                "self_consumed_energy_kwh": round(self_consumed_total_kwh, 2),
                "exported_energy_kwh": round(exported_total_kwh, 2),
                "self_consumption_savings_eur": round(self_consumption_savings_eur, 2),
                "feed_in_revenue_eur": round(feed_in_revenue_eur, 2),
                "total_return_eur": round(total_return_eur, 2),
                "remaining_eur": round(remaining_eur, 2),
                "annual_return_eur": round(annual_return_eur, 2),
                "forecast_date": forecast_date,
                "components": components,
                "component_return_sum_eur": round(allocated_return_eur, 2),
                "component_annual_return_sum_eur": round(allocated_annual_return_eur, 2),
                "battery_benefit_eur": round(battery_benefit_eur, 2),
                "battery_benefit_year_eur": round(battery_benefit_year_eur, 2),
                "battery_charged_energy_kwh": round(battery_charged_total_kwh, 2),
                "battery_discharged_energy_kwh": round(battery_discharged_total_kwh, 2),
                "average_import_price_eur_per_kwh": round(import_price_avg, 5),
                "average_feed_in_price_eur_per_kwh": round(feed_in_price_avg, 5),
                "calculated_at": calculated_at.isoformat(),
                "data_basis": "Lifetime PV/export counters; HA recorder averages and Energy compensation statistics",
                "allocation_method": "Storage uplift first; remaining self-consumption savings and feed-in revenue allocated by PV generation share",
            },
        )
        log.info(
            "PV amortization updated: %.1f%%, total %.2f EUR, annual %.2f EUR, forecast %s",
            progress_percent,
            total_return_eur,
            annual_return_eur,
            forecast_date,
        )
    except Exception as err:
        log.error("PV amortization update failed: %s", err)
