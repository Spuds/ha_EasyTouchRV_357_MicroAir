
[![License](https://img.shields.io/github/license/k3vmcd/ha-micro-air-easytouch.svg?style=flat-square)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-default-orange.svg?style=flat-square)](https://hacs.xyz)

# ha-micro-air-easytouch
Home Assistant Integration for the Mutli-Zone Micro-Air EasyTouch RV Thermostat

This integration implements a Home Assistant climate entity for basic control of your Micro-Air EasyTouch RV thermostat. 

It is a fork of the original [micro-air-easytouch](https://github.com/k3vmcd/micro-air-easytouch) integration by [k3vmcd](https://github.com/k3vmcd). This fork is an **experimental** update to the multizone branch and will only be tested against the 357 model thermostat.  **Do not use** this its only a test branch to determine what may work.

Core Features:
- Bluetooth connectivity
- Zone support
- Temperature monitoring via faceplate sensor
- Basic HVAC modes (Heat, Cool, Auto, Dry)
- Fan mode settings
- Temperature setpoint controls
- Ises a Climate entity and is represented as an HVAC device in Home Assistant
- Service to configure device location

Additional Features:
- Device reboot functionality
- Service to configure device location for the device to display the local weather

Known Limitations:
- The device responds slowly to commands - please wait a few seconds between actions
- When the unit is powered off from the device itself, this state is not reflected in Home Assistant
- Not all fan modes are settable in Home Assistant, "Cycled High" and "Cycled Low" are not available in Home Assistant - this is most likely due to limitations in the Home Assistant Climate entity
- Whenever the manufacturer mobile app connects to the device via bluetooth, Home Assistant will be temporarily disconnected and does not receive data

The integration works through Home Assistant's climate interface. You can control your thermostat through the Home Assistant UI or include it in automations, keeping in mind the device's response limitations.
