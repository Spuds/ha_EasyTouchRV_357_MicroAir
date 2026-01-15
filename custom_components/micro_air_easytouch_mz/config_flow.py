"""Config flow for MicroAirEasyTouch integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.const import CONF_ADDRESS

from .micro_air_easytouch.parser import MicroAirEasyTouchBluetoothDeviceData
from .const import DOMAIN

# Custom keys for credentials
KEY_EMAIL = "email"
KEY_PASSWORD = "password"


class MicroAirEasyTouchConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MicroAirEasyTouch."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_device: MicroAirEasyTouchBluetoothDeviceData | None = None
        self._discovered_devices: dict[str, str] = {}

    # ----- Bluetooth discovery step -----
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        device = MicroAirEasyTouchBluetoothDeviceData(password=None, email=None)
        if not device.supported(discovery_info):
            return self.async_abort(reason="not_supported")

        self._discovery_info = discovery_info
        self._discovered_device = device

        # Jump to **custom credentials step** instead of the generic password step
        return await self.async_step_credentials()

    # ----- Custom credentials step -----
    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask the user for email and password (custom UI)."""
        errors = {}
        if user_input is not None:
            try:
                assert self._discovered_device is not None
                self._discovered_device._email = user_input[KEY_EMAIL]
                self._discovered_device._password = user_input[KEY_PASSWORD]
                return await self.async_step_bluetooth_confirm(user_input)
            except Exception:
                errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(
                {
                    vol.Required(KEY_EMAIL): str,
                    vol.Required(KEY_PASSWORD): str,
                }
            ),
            errors=errors,
            description_placeholders={},
        )

    # ----- Bluetooth confirm step -----
    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm device."""
        assert self._discovered_device is not None
        device = self._discovered_device
        assert self._discovery_info is not None
        discovery_info = self._discovery_info

        title = device.title or device.get_device_name() or discovery_info.name

        if user_input is not None:
            return self.async_create_entry(
                title=title,
                data={
                    KEY_EMAIL: self._discovered_device._email,
                    KEY_PASSWORD: self._discovered_device._password,
                    CONF_ADDRESS: discovery_info.address,
                },
            )

        self._set_confirm_only()
        placeholders = {"name": title}
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=placeholders,
        )

    # ----- User selection step (manual device pick) -----
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle user step to pick a discovered device."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()

            device = MicroAirEasyTouchBluetoothDeviceData(password=None, email=None)
            self._discovered_device = device
            return await self.async_step_credentials()

        current_addresses = self._async_current_ids()
        for discovery_info in async_discovered_service_info(self.hass, False):
            address = discovery_info.address
            if address in current_addresses or address in self._discovered_devices:
                continue

            device = MicroAirEasyTouchBluetoothDeviceData(password=None)
            if device.supported(discovery_info):
                self._discovered_devices[address] = (
                    device.title or device.get_device_name() or discovery_info.name
                )

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(self._discovered_devices)}
            ),
        )
