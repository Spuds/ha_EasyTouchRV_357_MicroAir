"""MicroAirEasyTouch Integration"""
from __future__ import annotations

import logging
from typing import Final

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
)
import asyncio
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback

from .micro_air_easytouch.parser import MicroAirEasyTouchBluetoothDeviceData
from .const import DOMAIN
from .services import async_register_services, async_unregister_services

PLATFORMS: Final = [Platform.BUTTON, Platform.CLIMATE]
_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    address = entry.unique_id
    if address is None:
        _LOGGER.error("Config entry %s has no unique_id", entry.entry_id)
        return False

    password = entry.data.get(CONF_PASSWORD)
    email = entry.data.get(CONF_USERNAME)

    data = MicroAirEasyTouchBluetoothDeviceData(
        password=password,
        email=email
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "data": data,
    }

    # Register services once globally
    if "services_registered" not in hass.data[DOMAIN]:
        await async_register_services(hass)
        hass.data[DOMAIN]["services_registered"] = True

    @callback
    def _handle_bluetooth_update(service_info: BluetoothServiceInfoBleak) -> None:
        if service_info.address == address:
            data._start_update(service_info)
            # Best-effort: trigger an accelerated quick poll when an advertisement is seen
            try:
                ble_dev = async_ble_device_from_address(hass, service_info.address)
                if ble_dev:
                    asyncio.create_task(data.request_quick_poll(hass, ble_dev, interval=3.0, repeats=8))
            except Exception as e:
                _LOGGER.debug("Failed to trigger quick poll from advertisement: %s", str(e))

    unsub = hass.bus.async_listen(
        "bluetooth_service_info",
        _handle_bluetooth_update
    )

    hass.data[DOMAIN][entry.entry_id]["unsub_ble"] = unsub

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        entry_data = hass.data[DOMAIN].pop(entry.entry_id)

        # Unsubscribe BLE listener
        unsub = entry_data.get("unsub_ble")
        if unsub:
            unsub()

        # If last entry unloaded, unregister services
        if not any(
            key not in ("services_registered",)
            for key in hass.data[DOMAIN]
        ):
            await async_unregister_services(hass)
            hass.data[DOMAIN].pop("services_registered", None)

    return unload_ok
