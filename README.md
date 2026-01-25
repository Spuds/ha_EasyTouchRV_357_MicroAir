![HACS](https://img.shields.io/badge/HACS-default-blue.svg?style=for-the-badge)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.10+-blue.png?style=for-the-badge)
![Status](https://img.shields.io/badge/Status-Unofficial-lightgrey.png?style=for-the-badge)
![Device](https://img.shields.io/badge/Device-EasyTouch%20357-blue.png?style=for-the-badge)
![Warning](https://img.shields.io/badge/Warning-Experimental-yellow.png?style=for-the-badge)
![License](https://img.shields.io/github/license/Spuds/ha_EasyTouchRV_MicroAir_MZ.svg?style=for-the-badge)
---
![Logo](https://raw.githubusercontent.com/Spuds/ha_EasyTouchRV_MicroAir_MZ/refs/heads/main/custom_components/micro_air_easytouch_mz/icon.png)

# Micro-Air EasyTouch Multi-Zone (MZ)
Home Assistant Integration for Multi-Zone Micro-Air EasyTouch RV Thermostats

This integration provides Home Assistant control for Micro-Air EasyTouch RV thermostats with enhanced Bluetooth stability, automatic capability discovery, and multi-zone support.  This means the Home Assistant UI will only show controls that your device supports.

As I only have a model 357 (Dometic CCC1) that is all I've been able to test.  Please see the [WIKI](https://github.com/Spuds/ha_EasyTouchRV_MicroAir_MZ/wiki) for what protocol information has been reverse engineered.

Originally forked from [micro-air-easytouch](https://github.com/k3vmcd/micro-air-easytouch) by [k3vmcd](https://github.com/k3vmcd), this version has been extensively hacked in an effort to provide BLE stability, Optimistic UI, Feature discovery, Heat presets and Improved zone discovery.

## Key Features 

### 🏠 **Multi-Zone Support**
- Automatic zone detection during setup
- Individual climate entities per zone

### 🔗 **Enhanced Bluetooth Stability**
- Persistent connection management with idle timeout
- Command serialization to prevent device lockups

### ⚡ **Smart Capability Discovery**
- Automatic detection of available HVAC modes per zone
- Fan control for all operating modes

### 🚀 **Optimistic UI Updates**
- Immediate UI feedback on command execution
- Background verification with device state polling

### 🎛️ **HVAC Control**
- Support for all device modes (Off, Fan, Cool, Heat variants, Auto variants, Dry)
- Multiple heat mode support (heat pump / furnace) with heat presets

## Installation

### Via HACS (Recommended)
1. Add this repository to HACS as a custom repository
2. Search for "Micro-Air EasyTouch MZ" 
3. Install the integration
4. Restart Home Assistant

### Manual Installation
1. Copy the `micro_air_easytouch_mz` folder to your `custom_components` directory
2. Restart Home Assistant
3. Configure through the integrations page

## Configuration

1. **Enable Bluetooth**: Ensure your Home Assistant instance supports **GATT** Bluetooth connections and that the thermostat is connected via GATT.
2. **Device Selection**: Select your EasyTouch device from discovered devices
3. **Authentication**: Enter your EasyTouch account email and password
4. **Automatic Setup**: The integration will automatically detect zones and fetch device capabilities

## Supported Features

### HVAC Modes
- **Off**: Zone off
- **Fan Only**: Circulation without heating/cooling
- **Cool**: Air conditioning mode
- **Heat**: Heating (Presets allow selecting source Heat Pump vs Furnace/AquaHot)
- **Auto**: Automatic heating/cooling (if supported by device)
- **Dry**: Dehumidification mode (if supported by device)

### Fan Modes
- **Auto**: Automatic fan speed control
- **Low/High**: Manual fan speed settings
- **Cycled Low/High**: Energy-saving cycled operation (device-dependent)

### Services
- **Location Service**: Configure device GPS coordinates for weather display
- **All Off**: Centralized "All Off" button for system

## Known Problems

### Device Limitations
- Commands can take 1-2 seconds to validate (device protocol limitation)
- Device responds to only one Bluetooth connection at a time
- Mobile app usage will temporarily disconnect Home Assistant

### Home Assistant Integration Limitations
- Temperature display resolution limited to whole degrees
- Some fan modes do not have Home Assistant equivalents

## Common Issues
- **Connection Timeout**: Ensure the thermostat is powered and connected to HA with a GATT connection.
- **Authentication Failed**: Verify micro-air email/password credentials
- **Missing Modes**: Check your device configuration - some modes may not be supported
- **Slow Response**: Normal behavior - device protocol requires 1-2 second delays to validate the sent command was successful.  The command is sent right away, but the system must wait to see if it was "accepted"

### Device Reset
If the device becomes unresponsive, try the reboot service or power cycle the device.

## Contributing
Issues, feature requests, and pull requests are welcome.
