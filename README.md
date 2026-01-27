![Status](https://img.shields.io/badge/Status-Unsupported-lightgrey.png?style=for-the-badge)
![Device](https://img.shields.io/badge/Device-EasyTouch%20357-blue.png?style=for-the-badge)
![Warning](https://img.shields.io/badge/Warning-Experimental-yellow.png?style=for-the-badge)
---

# Micro-Air EasyTouch Multi-Zone (MZ)
Home Assistant Integration for Multi-Zone Micro-Air EasyTouch RV Thermostats

This integration provides Home Assistant control for Micro-Air EasyTouch RV thermostats with enhanced Bluetooth stability, automatic capability discovery, and multi-zone support.  This means the Home Assistant UI will only show controls that your device supports.

As I only have a model 357 (Dometic CCC1) that is all I've been able to test.

Originally forked from [micro-air-easytouch](https://github.com/k3vmcd/micro-air-easytouch) by [k3vmcd](https://github.com/k3vmcd), this version has been modified in an effort to improve BLE stability, Optimistic/Responsive UI, Supported HVAC mode discovery, Supported Fan mode discovery, Provide Heat presets for multi-fuel setups (Heat-pump + Aqua-hot) and Improved zone discovery.

# Installation Via HACS
1. Add this repository to HACS as a custom repository
2. Search for "Micro-Air EasyTouch MZ" 
3. Install the integration
4. Restart Home Assistant
