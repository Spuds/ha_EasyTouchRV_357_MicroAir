[![hacs](https://img.shields.io/badge/HACS-default-orange.svg?style=flat-square)](https://hacs.xyz)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.12+-blue.png)
![Status](https://img.shields.io/badge/Status-Unofficial-lightgrey.png)
![Device](https://img.shields.io/badge/Device-Micro--Air%20EasyTouch%20357-blueviolet.png)
![Warning](https://img.shields.io/badge/Warning-Experimental-green.png)

# Micro-Air EasyTouch Multi-Zone (MZ)
Home Assistant Integration for Multi-Zone Micro-Air EasyTouch RV Thermostats

This integration provides Home Assistant control for Micro-Air EasyTouch RV thermostats with enhanced Bluetooth stability, automatic capability discovery, and multi-zone support.  As I only have a model 357 (Dometic CCC1) that is all I've been able to test.  Please see the WIKI for what protocol information has been reverse engineered.

Originally forked from [micro-air-easytouch](https://github.com/k3vmcd/micro-air-easytouch) by [k3vmcd](https://github.com/k3vmcd), this version has been extensively hacked on in an effort to provide BLE stability fixes, advanced protocol support, and add new features.

## Key Features

### 🏠 **Multi-Zone Support**
- Automatic zone detection during setup
- Individual climate entities per zone
- Zone-specific capability filtering
- Centralized "All Off" button for system shutdown

### 🔗 **Enhanced Bluetooth Stability**
- Persistent connection management with 2-minute idle timeout
- Command serialization to prevent device lockups
- Connection pooling across all zones
- Automatic reconnection with exponential backoff

### ⚡ **Smart Capability Discovery**
- Automatic detection of available HVAC modes per zone
- Dynamic fan speed filtering based on device configuration
- Temperature limit enforcement (SPL arrays)
- UI elements automatically filtered based on device capabilities

### 🚀 **Optimistic UI Updates**
- Immediate UI feedback on command execution
- Background verification with device state polling
- Seamless user experience with responsive controls

### 🎛️ **Advanced HVAC Control**
- Full protocol support for all device modes (Off, Fan, Cool, Heat variants, Auto variants, Dry)
- Intelligent heat source detection (Heat Pump vs Furnace/AquaHot)
- Proper fan control for all operating modes
- Multiple auto mode support with heat source selection

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

1. **Enable Bluetooth**: Ensure your Home Assistant instance can support GATT Bluetooth connections and that the thermostat is connected via GATT.
2. **Add Integration**: Go to Settings → Devices & Services → Add Integration
3. **Device Selection**: Select your EasyTouch device from discovered devices
4. **Authentication**: Enter your EasyTouch account email and password
5. **Automatic Setup**: The integration will automatically detect zones and fetch device capabilities

## Supported Features

### HVAC Modes
- **Off**: Complete system shutdown
- **Fan Only**: Circulation without heating/cooling
- **Cool**: Air conditioning mode
- **Heat**: Heating (automatically detects Heat Pump vs Furnace/AquaHot)
- **Auto**: Automatic heating/cooling (if supported by device)
- **Dry**: Dehumidification mode (if supported by device)

### Fan Modes
- **Auto**: Automatic fan speed control
- **Low/High**: Manual fan speed settings
- **Cycled Low/High**: Energy-saving cycled operation (device-dependent)

### Services
- **Location Service**: Configure device GPS coordinates for weather display
- **Test Set Mode**: Debug service for testing specific mode/zone combinations

## Device Capability System

The integration automatically discovers and respects your device's specific capabilities:

- **MAV Bitmask**: Determines which HVAC modes are available for each zone
- **FA Arrays**: Control available fan speeds for different operating modes  
- **SPL Arrays**: Enforce temperature setpoint limits (min/max cool/heat)

This means the Home Assistant UI will only show controls that your specific device supports, preventing invalid commands.

## Known Limitations

### Device Limitations
- Commands take 1-2 seconds to validate (device protocol limitation)
- Device responds to only one Bluetooth connection at a time
- Mobile app usage will disconnect Home Assistant temporarily
- Some older firmware may not support all auto modes

### Home Assistant Integration Limitations
- Temperature display resolution limited to whole degrees
- Some advanced fan modes may not have Home Assistant equivalents
- Zone detection requires device to be powered on during setup

## Troubleshooting

### Debug Logging
Enable debug logging to troubleshoot issues:
```yaml
logger:
  logs:
    custom_components.micro_air_easytouch_mz: debug
```

### Common Issues
- **Connection Timeout**: Ensure device is powered and in range
- **Authentication Failed**: Verify email/password credentials
- **Missing Modes**: Check your device MAV configuration - some modes may be disabled
- **Slow Response**: Normal behavior - device protocol requires 1-2 second delays to validate the sent command was successful.  The command is sent right away, but the system must wait to see if it was "accepted"

### Device Reset
If the device becomes unresponsive, use the reboot service or power cycle the device.

## Contributing
Issues, feature requests, and pull requests are welcome.

---

**Note**: This integration communicates locally with your device via Bluetooth and does not send data to external services beyond the device's own cloud features.
