
[![License](https://img.shields.io/github/license/Spuds/ha_EasyTouchRV_MicroAir_MZ.svg?style=flat-square)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-default-orange.svg?style=flat-square)](https://hacs.xyz)
[![GitHub Release](https://img.shields.io/github/release/Spuds/ha_EasyTouchRV_MicroAir_MZ.svg?style=flat-square)](https://github.com/Spuds/ha_EasyTouchRV_MicroAir_MZ/releases)
# ha-micro-air-easytouch
Home Assistant Integration for the Mutli-Zone Micro-Air EasyTouch RV Thermostat

This integration implements a Home Assistant climate entity for control of your Micro-Air EasyTouch RV thermostat. 

It is a fork of the original [micro-air-easytouch](https://github.com/k3vmcd/micro-air-easytouch) integration by [k3vmcd](https://github.com/k3vmcd). This fork is an **experimental** update to the multi-zone branch (which is also experimental) and will only be tested against the 357 model thermostat.  

**Do not use** This its only a test to determine what may work for my RV. In addition to zones, mine also has dual heat sources with both heat-pumps and an aqua-hot/furnace.

Core Features:
- Bluetooth connectivity
- Zone support
- Temperature monitoring via faceplate sensor
- Basic HVAC modes (Heat, Cool, Auto, Dry, Off)
- Fan mode settings
- Temperature setpoint controls
- Uses a Climate entity and is represented as an HVAC device in Home Assistant
- Service to configure device location

Additional Features:
- Device reboot functionality
- Device Off (separate from zone off)
- Service to configure device location for the device to display the local weather

Known Limitations:
- The device responds slowly to commands - please wait a few seconds between actions
- When the unit is powered off from the device itself, this state is not reflected in Home Assistant
- Not all fan modes are settable in Home Assistant, "Cycled High" and "Cycled Low" are not available in Home Assistant - this is most likely due to limitations in the Home Assistant Climate entity
- Whenever the mobile app connects to the device via bluetooth, Home Assistant will be disconnected and does not receive data until the app is disconnected.

The integration works through Home Assistant's climate interface. You can control your thermostat through the Home Assistant UI or include it in automatons, keeping in mind the device's response limitations.

## Current Decoded Zine Structure

```
Zone Status Array (16 values):
────────────────────────────────────────────────────────────────────────────────────
Index  Value   Meaning
────────────────────────────────────────────────────────────────────────────────────
  0      67    Auto mode heat setpoint (°F)
  1      76    Auto mode cool setpoint (°F)
  2      76    Cool mode setpoint (°F)
  3      73    Heat mode setpoint (°F)
  4      72    Dry/Dehumidify temperature setpoint (°F)
  5      45    **Unknown** (always 45 in my samples / ? %RH humidity value)
  6       2    Fan-Only mode speed (0=off, 1=low, 2=high) - when mode 10=1
  7     128    Cool mode fan (0/1/2/65/66/128) - when mode 10=2
  8     128    Heat mode fan (0/1/2/128) - when mode 10=5,7
  9     128    Auto mode fan (0/1/2/3/128) - when mode auto 10=(8,10,11)
 10       5    User Selected Mode (what you chose)
 11       1    Furnace mode fan (0/1/2/128) when mode 10=(3,4)
 12      64    Current actual temperature (°F)
 13     255    **Unknown** (always 255 null/unset in my samples))
 14       0    **Unknown** (always 0 in my samples / ? Error code (0=no error))
 15       4    Active State (0=idle, 2=cooling, 4=heating, ...)
────────────────────────────────────────────────────────────────────────────────────
```

## Available Modes (Actual support depends on the installed system)

```
Manually setting modes resulted in the following mode Icons
────────────────────────────────────────────────────────────────────────────────────
Index  Meaning
────────────────────────────────────────────────────────────────────────────────────
 0     Off (power off) can be used for zone off OR system off based on power flag
 1     Fan Only
 2     Cooling
 3     Gas/Furnace Heat (mode 3) - probably propane furnace
 4     Gas/Furnace Heat (mode 4) - my AquaHot
 5     Heat Pump Heat
 6     Dry/Dehumidify
 7     Heat Strip (Electric)
 8     Auto Heat/Cool
 9     Auto Heat/Cool with Heat Strip preference
10     Auto Heat/Cool with Heat Pump preference
11     Auto Heat/Cool with Gas/Furnace preference
12     Electric Heat
```

## Available PRM (Actual support depends on the installed system)

```
Manually setting modes resulted in the following observations
────────────────────────────────────────────────────────────────────────────────────
Index  Meaning
────────────────────────────────────────────────────────────────────────────────────
 0     Last zone to receive a command
 1     3 when unit was off / 11 when it was on
 2     Outdoor temperature as received from weather
 3     Indoor Temperature
```