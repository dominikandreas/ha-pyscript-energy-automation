# Home Assistant Energy Automation
This repo contains my scriots to manage ev charging and home battery charging / discharging with dynamic electricity prices and solar excess charging.

## Installation
Install pyscript via hacs

Use ssh or vs code addon to open a terminal in your home assistant

Navigate to the user config folder
`cd /config`

Clone this repo into the config/pyscript folder
`git clone https://github.com/dominikandreas/ha-pyscript-energy-automation.git pyscript`

Modify the files as required for your home. Especially modules/states.py for the entity definitions.

Some entities need to be created as helpers in home assistant, this still needs to be documented. Reading source code should be easy enough and informative

## Overview of files:
modules/states.py: entity id definitions
modules/utils.py: get and set functions handle entity states
ev_charging.py  : ev charge automation. requires a home assistant schedule component for defining planned drives
pv_prediction.py: uses forecast from solcast to estimate excess energy for the next days

electricity_price.py : defines high and low price entities that are used for charge automation
tibber_price.py: acquire tibber prices via http api (alternative to tibber integration)
energy.py: main battery charge / discharge automations
pv.py: defines some solar entity derivative sensors
victron.py: victron / venus gx specific things, e.g. setting the inverter mode to save energy when discharge is not needed

