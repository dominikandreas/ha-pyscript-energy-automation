# Home Assistant Energy Automation

Pyscript automations for coordinating my EV charging, home battery behavior, PV surplus, Victron inverter control, and dynamic electricity prices in Home Assistant.

The scripts are built around a simple idea: keep the energy decisions in readable Python, while Home Assistant provides the sensors, helpers, schedules, switches, and services that actually control the house.

The entire project is tuned for my house, but there's no reason it shouldn't work for other settings in principle. 

## Getting Started

Installation and manual optimal configuration / tuning for your home will require a lot of time and you will likely run into issues that might be hard to fix without an in-depth understanding of the codebase.

Recent AI models however are very good at understanding python, using the terminal to fetch logs and implement whatever changes you might require. They know how Home Assistant works, how to add additional sensors & capabilities to your system or even build cool dashboards for you. I therefore suggest to use a (good!) AI to do the initial setup and configuration. 

### AI - assisted (recommended)
Add the AI capability you need. My approach was the following:
 - Install https://github.com/dominikandreas/addon-ssh-debian (gives you ssh access with a debian docker image, supports remote development)
 - Install some AI coding tool (e.g. Codex app, VSCode with Copilot or Antigravity)
 - Connect it to your home assistant and open the /config folder
 - Tell your favourite AI to setup this repo and configure it for your own home assistant and hide every capability behind a switch with off by default
 - Look at the logs, ask your AI to make changes as necessary, build dashboards to inspect details (Energy history, forecast, etc)

> WARNING: Be careful with "cheap" models! I suggest to use GPT 5.5 or Opus 4.6 levels of intelligence. Cheap models can become very expensive, especially in this context.

### Manual

1. Install [Pyscript](https://github.com/custom-components/pyscript) in Home Assistant, usually through HACS.

2. Open a terminal in Home Assistant, for example through the SSH add-on or the VS Code add-on.

3. Clone this repository into Home Assistant's `pyscript` directory:

   ```bash
   cd /config
   git clone https://github.com/dominikandreas/ha-pyscript-energy-automation.git pyscript
   ```

4. Edit `modules/states.py` for your installation.

   This file is the main mapping between the automation code and your Home Assistant entities. Update the entity IDs for your EV, charger, battery, PV forecast, grid meter, Victron system, electricity price sensors, and helper entities.

5. Review `modules/const.py`.

   Adjust EV and charger constants such as battery capacity, consumption, voltage, minimum/maximum current, and supported phase count.

6. Create the required Home Assistant helpers.

   Several automations expect `input_number`, `input_boolean`, `input_datetime`, `schedule`, `switch`, `sensor`, and `binary_sensor` entities. The expected entity IDs are visible in `modules/states.py`; either create helpers with those IDs or change the mapping to match your existing helpers.

7. Reload Pyscript or restart Home Assistant.

   After reload, the scripts create and update derived sensors, react to state changes, and run their periodic triggers.

## Features

- Setpoint automation that forecasts whole-home energy behavior and optimizes the grid setpoint policy for cost, PV feed-in, battery reserve, and EV charging constraints.
- EV charging automation that balances planned drives, required SOC, charge limits, low-price windows, and PV surplus.
- Automatic EV charger current and phase selection based on available excess power.
- Force-charge support for bypassing surplus and price logic when the car must charge immediately.
- Home battery target and charge/discharge planning based on demand, forecast production, price, and configured limits.
- PV forecast processing using Solcast-style forecast entities to estimate when production will cover house demand.
- Derived energy sensors for battery energy, EV energy, house demand, energy surplus, and energy-to-burn decisions.
- Dynamic electricity price sensors and high/low price binary sensors for downstream charging decisions.
- Victron/Venus OS integration for inverter mode, grid setpoint publishing, efficiency estimates, and averaged power sensors.
- Battery cell balancing and monitoring helpers.
- Optional Tibber price fetching script if you do not want to rely on a Home Assistant Tibber integration.

## Setpoint Automation

The core optimization loop lives in `energy.py`. It forecasts the next day of smart-home energy behavior and turns that forecast into a live grid setpoint policy for the Victron system.

The forecast combines:

- PV production forecast and EPEX electricity prices.
- Day/night house demand estimates.
- Current battery capacity, reserve limits, charge/discharge limits, and inverter mode rules.
- EV schedule, expected driving demand, smart charge limits, wallbox availability, and surplus charging behavior.
- Grid feed-in limits and PV feed-in targets.

For each forecast period, the simulator estimates PV production, house load, EV charging, EV driving consumption, battery charge/discharge power, grid import, feed-in, surplus energy, and resulting battery state. This produces a detailed policy forecast that is also written back to Home Assistant as attributes on the setpoint and forecast sensors.

The optimizer then uses a fast heuristic instead of an expensive global optimizer:

- First binary search: find a good base grid setpoint within the configured feed-in limits.
- Second binary search: tune the setpoint spread, which controls how strongly the setpoint reacts to electricity price variation.
- Optional local re-search: if today's PV feed-in peak would exceed the configured target, re-optimize the high-production window and merge it with the rest of the forecast.
- Live mapping: map the optimized base setpoint to the current EPEX price, battery level, PV power, and house load using a Gaussian price response.
- Fast application loop: apply the target every few seconds, smoothing house-load noise and avoiding oscillation.

This is the main cost-reduction strategy of the project: it keeps the battery available when future PV or price conditions make that valuable, discharges when that is economically useful, reserves capacity for surplus PV, and coordinates EV charging with the same forecast.

## EV Schedule And Charging Policy

EV charging is driven by `ev_charging.py`, with shared decision helpers in `modules/energy_core.py`. The policy is schedule-aware: it uses the next planned drive to decide how much energy the car needs, how urgently it needs it, and whether the car should wait for PV surplus or cheap electricity.

The planned drives schedule is mapped by `EV.planned_drives` in `modules/states.py` and is expected to be a Home Assistant `schedule` entity. Schedule events may include optional `data` fields:

- `required` - target SOC percentage for that drive.
- `distance` - planned distance in km. If `required` is not set, the scripts estimate a target SOC from `modules/const.py` values such as EV capacity and `kWh/100km`, with a safety margin.

Example schedule event data:

```yaml
data:
  required: 80
  distance: 120
```

When a future drive is found, the EV controller uses that event's target SOC or distance-derived SOC instead of the default `EV.required_soc`. The setpoint forecast also uses the same schedule to simulate periods where the car is away and to subtract expected driving energy over the drive window.

The live charging policy works roughly like this:

- Maintain a smart charge limit based on time until the next drive: far-away drives are capped lower, while near-term or active schedules allow charging closer to 100%.
- Stop charging once the effective required SOC is reached.
- Emergency charge at maximum power when there is not enough time left to reach the target before the next drive.
- Prefer PV surplus charging when excess power is above the house/battery target and there is enough forecast surplus or immediate PV.
- Use cheap grid electricity when the price is low and the next drive is close enough that waiting for surplus is risky.
- While already charging, adjust current and phase count to track the excess-power target instead of blindly charging at full power.
- Turn charging off when prices are high, surplus is unavailable, and there is still time before the next drive.
- Respect force-charge mode, which sets the charger to maximum power and prevents normal automation from turning it off.

Current changes are bounded by the charger limits in `modules/const.py`. Phase changes use the charger service configured in `ev_charging.py`, wait for `ev_phase_switch_delay`, and include a cooldown so the charger is not rapidly switched between one and three phases.

## Code Landscape

### Main Automations

- `energy.py` - main house energy and battery automation. Includes the setpoint policy simulator, binary-search optimizer, live setpoint application, battery target SOC, surplus forecasting, upcoming demand, and related derived sensors.
- `ev_charging.py` - EV charging controller. Handles smart charge limits, planned-drive schedule parsing, charge decisions, force charging, current control, and phase switching.
- `pv_prediction.py` - PV forecast processing. Estimates when PV production will meet house demand and how much energy is needed until then.
- `electricity_price.py` - creates current price, low-price, high-price, and PV opportunistic price entities.
- `victron.py` - Victron-specific runtime automation for inverter mode, MQTT setpoint publishing, averaged power, and efficiency sensors.
- `pv.py` - derived PV and solar-related sensors.
- `battery_cells.py` - battery cell monitoring and balancing related helpers.
- `tibber_price.py` - optional Tibber HTTP API price source.

### Shared Modules

- `modules/states.py` - central entity ID map. This is the first file to customize for a new Home Assistant installation.
- `modules/const.py` - physical and configuration constants, especially EV and charger limits.
- `modules/utils.py` - helper functions for reading, writing, converting, and validating Home Assistant state values.
- `modules/energy_core.py` - pure-ish EV charging decision helpers used by `ev_charging.py` and `energy.py`.
- `modules/electricity_price.py` - reusable electricity price helpers.
- `modules/victron.py` - Victron constants, MQTT topics, inverter mode mappings, and helper functions.

### Experimental / Next Version

- `v2/energy_2.py` - newer energy automation work in progress.
- `v2/forecasting_interfaces.py` - forecast data interfaces.
- `v2/forecasting_utils.py` - forecasting utility functions.

### Development Files

- `.vscode/settings.json` - editor settings for local development.
- `setpoint_mapping.ipynb` - notebook for experimenting with setpoint behavior.

## Notes

This repository is intentionally installation-specific. Treat `modules/states.py` as the configuration boundary: most adaptation should happen there, with small constant changes in `modules/const.py`. The automation logic assumes the mapped entities exist and have compatible units.
