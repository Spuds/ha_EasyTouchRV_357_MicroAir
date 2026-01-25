"""MicroAirEasyTouch Integration"""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback

from .micro_air_easytouch.parser import MicroAirEasyTouchBluetoothDeviceData
from .const import DOMAIN
from .services import async_register_services, async_unregister_services

PLATFORMS: Final = [Platform.BUTTON, Platform.CLIMATE]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MicroAirEasyTouch from a config entry."""
    address = entry.unique_id
    assert address is not None
    password = entry.data.get(CONF_PASSWORD)
    email = entry.data.get(CONF_USERNAME)
    data = MicroAirEasyTouchBluetoothDeviceData(password=password, email=email)

    # Store the device address for persistent connection attempts
    data.set_device_address(address)

    # Clear any cached BLE device so we reacquire a new handle after reload
    try:
        data.set_ble_device(None)
    except Exception:
        pass

    # Re-fetch zone configurations if zones were detected during setup
    detected_zones = entry.data.get("detected_zones", [])

    if detected_zones:
        _LOGGER.debug(
            "Re-fetching zone configurations for %d zones", len(detected_zones)
        )
        try:
            from homeassistant.components.bluetooth import async_ble_device_from_address

            ble_device = async_ble_device_from_address(hass, address)
            if ble_device:
                # Re-fetch the zone configurations that were obtained during config flow
                # This ensures the runtime parser has the same MAV/FA/SPL data
                await data._refetch_zone_configurations(
                    hass, ble_device, detected_zones
                )
            else:
                _LOGGER.warning(
                    "Cannot re-fetch zone configs - BLE device not available"
                )
        except Exception as e:
            _LOGGER.warning("Failed to re-fetch zone configurations: %s", str(e))
    else:
        _LOGGER.warning(
            "No detected zones found in config entry, skipping zone config re-fetch"
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"data": data}

    # Start polling by default so we can obtain full status for devices that do not advertise
    try:
        data.start_polling(hass, startup_delay=1.0, address=address)
    except Exception as e:
        _LOGGER.debug("Failed to start polling task: %s", str(e))

    @callback
    def _handle_bluetooth_update(service_info: BluetoothServiceInfoBleak) -> None:
        """Update device info from advertisements."""
        if service_info.address == address:
            _LOGGER.debug(
                "Received BLE advertisement from %s: %s", address, service_info
            )
            data._start_update(service_info)

    hass.bus.async_listen("bluetooth_service_info", _handle_bluetooth_update)

    # Register services
    await async_register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Clean up device data
        device_data = hass.data[DOMAIN].pop(entry.entry_id, {}).get("data")
        if device_data:
            await device_data.async_shutdown()
        # Unregister services
        await async_unregister_services(hass)
    return unload_ok
