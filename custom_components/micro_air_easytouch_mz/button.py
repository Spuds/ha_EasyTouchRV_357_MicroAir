"""Support for MicroAirEasyTouch buttons."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.bluetooth import async_ble_device_from_address

from .const import DOMAIN
from .micro_air_easytouch.parser import MicroAirEasyTouchBluetoothDeviceData  # Corrected import

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MicroAirEasyTouch button based on a config entry."""
    data = hass.data[DOMAIN][config_entry.entry_id]["data"]
    mac_address = config_entry.unique_id
    assert mac_address is not None
    entities = [MicroAirEasyTouchRebootButton(data, mac_address), MicroAirEasyTouchAllOffButton(data, mac_address)]
    async_add_entities(entities)

class MicroAirEasyTouchRebootButton(ButtonEntity):
    """Representation of a reboot button for MicroAirEasyTouch."""

    def __init__(self, data: MicroAirEasyTouchBluetoothDeviceData, mac_address: str) -> None:
        """Initialize the button."""
        self._data = data
        self._mac_address = mac_address
        self._attr_unique_id = f"microaireasytouch_{self._mac_address}_reboot"
        self._attr_name = "Reboot Device"
        self._attr_icon = "mdi:restart"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"MicroAirEasyTouch_{self._mac_address}")},
            name=f"EasyTouch {self._mac_address}",
            manufacturer="Micro-Air",
            model="Thermostat",
        )

    async def async_press(self) -> None:
        """Handle the button press."""
        _LOGGER.debug("Reboot button pressed")
        ble_device = async_ble_device_from_address(self.hass, self._mac_address)
        if not ble_device:
            _LOGGER.error("Could not find BLE device for reboot: %s", self._mac_address)
            return
        await self._data.reboot_device(self.hass, ble_device)


class MicroAirEasyTouchAllOffButton(ButtonEntity):
    """Toggle button for system-wide power control (all zones on/off)."""

    def __init__(self, data: MicroAirEasyTouchBluetoothDeviceData, mac_address: str) -> None:
        self._data = data
        self._mac_address = mac_address
        self._attr_unique_id = f"microaireasytouch_{self._mac_address}_power_toggle"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"MicroAirEasyTouch_{self._mac_address}")},
            name=f"EasyTouch {self._mac_address}",
            manufacturer="Micro-Air",
            model="Thermostat",
        )

    @property
    def name(self) -> str:
        """Return dynamic name based on current state."""
        if self._is_unit_on():
            return "All Zones Off"
        else:
            return "All Zones On"

    @property
    def icon(self) -> str:
        """Return dynamic icon based on current state."""
        if self._is_unit_on():
            return "mdi:power-off"
        else:
            return "mdi:power-on"

    def _is_unit_on(self) -> bool:
        """Check if unit is currently on based on PRM[1] value."""
        device_data = self._data.async_get_device_data()
        prm_data = device_data.get('PRM', [])
        if len(prm_data) > 1:
            unit_state = prm_data[1]
            return unit_state == 11  # 11=on, 3=off
        return False  # Default to off if no data available

    async def async_press(self) -> None:
        """Toggle system-wide power (all zones on/off).

        Checks current state from PRM[1] and toggles:
        - If currently on (PRM[1]=11), send power=0 (turn off)
        - If currently off (PRM[1]=3), send power=1 (turn on)
        """
        is_on = self._is_unit_on()
        new_power_state = 0 if is_on else 1
        action = "OFF" if is_on else "ON"
        
        _LOGGER.debug("Power toggle button pressed - current state: %s, setting to: %s", 
                     "ON" if is_on else "OFF", action)
        
        ble_device = async_ble_device_from_address(self.hass, self._mac_address)
        if not ble_device:
            _LOGGER.error("Could not find BLE device to send power toggle: %s", self._mac_address)
            return
        
        # Send power command
        cmd = {"Type": "Change", "Changes": {"power": new_power_state}}
        success = await self._data.send_command(self.hass, ble_device, cmd)
        if success:
            _LOGGER.info("Sent system-wide %s to device %s", action, self._mac_address)
        else:
            _LOGGER.error("Failed to send system-wide %s to device %s", action, self._mac_address)