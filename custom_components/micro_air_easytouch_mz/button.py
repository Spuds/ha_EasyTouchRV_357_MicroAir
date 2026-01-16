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
    entities = [MicroAirEasyTouchRebootButton(data, mac_address), MicroAirEasyTouchPowerToggleButton(data, mac_address)]
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


class MicroAirEasyTouchPowerToggleButton(ButtonEntity):
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
        # Initialize with default values - will update when device data is available
        self._attr_name = "System Power Toggle"
        self._attr_icon = "mdi:power"

    async def async_added_to_hass(self) -> None:
        """Subscribe to device data updates when entity is added to hass."""
        self._data.async_subscribe_updates(self._handle_update)
        # Update attributes when first added (device data should be available now)
        self._update_attributes()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from updates when entity is removed."""
        # Note: The parser doesn't currently provide an unsubscribe method
        # but this is here for future compatibility
        pass

    def _handle_update(self, device_state=None) -> None:
        """Handle device state updates and refresh entity state."""
        self._update_attributes()
        self.async_write_ha_state()

    def _update_attributes(self) -> None:
        """Update the name and icon attributes based on current device state."""
        try:
            if self._is_unit_on():
                self._attr_name = "All Zones Off"
                self._attr_icon = "mdi:power-off"
            else:
                self._attr_name = "All Zones On"
                self._attr_icon = "mdi:power-on"
        except Exception as e:
            _LOGGER.debug("Error updating power toggle attributes: %s", str(e))
            # Fallback to generic names if device data isn't available
            self._attr_name = "System Power Toggle"
            self._attr_icon = "mdi:power"

    @property
    def name(self) -> str:
        """Return the current name attribute."""
        return self._attr_name

    @property
    def icon(self) -> str:
        """Return the current icon attribute."""
        return self._attr_icon

    def _is_unit_on(self) -> bool:
        """Check if unit is currently on based on PRM[1] value."""
        device_data = self._data.async_get_device_data()
        prm_data = device_data.get('PRM', [])
        if len(prm_data) > 1:
            unit_state = prm_data[1]
            return unit_state == 11  # 11=on, 3=off
        return False  # Default to off if no data available

    def _get_zone_count(self) -> int:
        """Get the number of available zones from device data."""
        device_data = self._data.async_get_device_data()
        available_zones = device_data.get('available_zones', [0])
        return len(available_zones)

    async def async_press(self) -> None:
        """Toggle system-wide power (all zones on/off).

        Sends command to a non-existent zone (zone_count) to toggle system power
        without affecting individual zone states. This allows zones to retain
        their last state when the system comes back online.
        
        Checks current state from PRM[1] and toggles:
        - If currently on (PRM[1]=11), send mode=0, power=0, zone=zone_count (turn off)
        - If currently off (PRM[1]=3), send mode=0, power=1, zone=zone_count (turn on)
        """
        is_on = self._is_unit_on()
        new_power_state = 0 if is_on else 1
        zone_count = self._get_zone_count()
        action = "OFF" if is_on else "ON"
        
        _LOGGER.debug("Power toggle button pressed - current state: %s, setting to: %s, using zone: %d", 
                     "ON" if is_on else "OFF", action, zone_count)
        
        ble_device = async_ble_device_from_address(self.hass, self._mac_address)
        if not ble_device:
            _LOGGER.error("Could not find BLE device to send power toggle: %s", self._mac_address)
            return
        
        # Send to non-existent zone (zone_count) to avoid affecting individual zone states
        cmd = {"Type": "Change", "Changes": {"mode": 0, "zone": zone_count, "power": new_power_state}}
        success = await self._data.send_command(self.hass, ble_device, cmd)
        if success:
            _LOGGER.info("Sent system-wide %s (mode=0, zone=%d, power=%d) to device %s", 
                        action, zone_count, new_power_state, self._mac_address)
            # Update attributes immediately and trigger a state update
            self._update_attributes()
            self.async_write_ha_state()
        else:
            _LOGGER.error("Failed to send system-wide %s to device %s", action, self._mac_address)