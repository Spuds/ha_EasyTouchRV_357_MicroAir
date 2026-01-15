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
    """Button to send a system-wide OFF (power = 0) to the device."""

    def __init__(self, data: MicroAirEasyTouchBluetoothDeviceData, mac_address: str) -> None:
        self._data = data
        self._mac_address = mac_address
        self._attr_unique_id = f"microaireasytouch_{self._mac_address}_all_off"
        self._attr_name = "All Zones Off"
        self._attr_icon = "mdi:power-off"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"MicroAirEasyTouch_{self._mac_address}")},
            name=f"EasyTouch {self._mac_address}",
            manufacturer="Micro-Air",
            model="Thermostat",
        )

    async def async_press(self) -> None:
        """Send a system-wide OFF (power=0). No confirmation.

        The device interprets power=0 as a system-wide power-off (all zones off).
        """
        _LOGGER.debug("All-zones off button pressed")
        ble_device = async_ble_device_from_address(self.hass, self._mac_address)
        if not ble_device:
            _LOGGER.error("Could not find BLE device to send all-off: %s", self._mac_address)
            return
        # Send Change with power=0 (system-wide off). No zone specified intentionally.
        cmd = {"Type": "Change", "Changes": {"power": 0}}
        success = await self._data.send_command(self.hass, ble_device, cmd)
        if success:
            _LOGGER.info("Sent system-wide OFF to device %s", self._mac_address)
        else:
            _LOGGER.error("Failed to send system-wide OFF to device %s", self._mac_address)