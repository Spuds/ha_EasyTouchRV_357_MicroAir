"""MicroAirEasyTouch Integration"""

from __future__ import annotations
import logging
from typing import Final

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry

from homeassistant.const import (
    Platform,
    CONF_PASSWORD,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STARTED,
)

from .micro_air_easytouch.parser import MicroAirEasyTouchBluetoothDeviceData
from .const import DOMAIN
from .services import async_register_services, async_unregister_services

PLATFORMS: Final = [Platform.BUTTON, Platform.CLIMATE]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MicroAirEasyTouch from a config entry.

    This function is called to set up the MicroAirEasyTouch device from a configuration entry 
    in Home Assistant. It initializes the device data, sets up the Bluetooth 
    connection, and fetches any detected zone configurations.

    Args:
        hass (HomeAssistant): The Home Assistant instance.
        entry (ConfigEntry): The configuration entry containing device information.

    Returns:
        bool: True if the setup was successful, False otherwise.
    """
    address = entry.unique_id
    assert address is not None
    password = entry.data.get(CONF_PASSWORD)
    email = entry.data.get(CONF_USERNAME)
    data = MicroAirEasyTouchBluetoothDeviceData(password=password, email=email)

    # Store the device address for persistent connection attempts
    data.set_device_address(address)


    # Re-fetch zone configurations if they were detected during setup
    detected_zones = entry.data.get("detected_zones", [])
    if detected_zones:
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
        except (OSError, TimeoutError) as e:
            _LOGGER.warning("Failed to re-fetch zone configurations: %s", str(e))
    else:
        _LOGGER.warning(
            "No detected zones found in config entry, skipping zone config re-fetch"
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"data": data}

    @callback
    async def _refresh_zone_configs_startup(event: Event | None = None) -> None:
        """One-time config fetch at HA start to avoid missing MAV/FA/SPL data.

        This function is called once at Home Assistant startup to fetch the 
        zone configurations. It ensures that the device has the latest 
        configuration data and avoids missing any important MAV/FA/SPL data.
        """
        device_state = data.async_get_device_data()
        existing_configs = device_state.get("zone_configs", {}) if device_state else {}

        # Skip if we already have non-zero MAV configs
        if existing_configs and all(
            cfg.get("MAV", 0) != 0 for cfg in existing_configs.values()
        ):
            return

        ble_device = async_ble_device_from_address(hass, address)
        if not ble_device:
            return

        zones = (
            entry.data.get("detected_zones")
            or device_state.get("available_zones")
            or [0]
        )

        _LOGGER.debug("Startup zone config fetch for %s (zones=%s)", address, zones)
        try:
            await data._refetch_zone_configurations(hass, ble_device, zones)
        except (OSError, TimeoutError) as err:
            _LOGGER.debug("Startup zone config fetch failed: %s", err)

    # Run once after HA starts so BLE is ready
    hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STARTED, _refresh_zone_configs_startup
    )

    # Start polling by default so we obtain status (microair does not advertise)
    try:
        data.start_polling(hass, startup_delay=1.0, address=address)
    except (OSError, TimeoutError) as e:
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
    """Unload a config entry.

    This function is called to unload a configuration entry from Home Assistant.
    It ensures that all associated platforms are unloaded and performs any 
    necessary cleanup for the device data.

    Args:
        hass (HomeAssistant): The Home Assistant instance.
        entry (ConfigEntry): The configuration entry to unload.

    Returns:
        bool: True if the unload was successful, False otherwise.
    """
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        device_data = hass.data[DOMAIN].pop(entry.entry_id, {}).get("data")
        if device_data:
            await device_data.async_shutdown()
        await async_unregister_services(hass)
    return unload_ok
