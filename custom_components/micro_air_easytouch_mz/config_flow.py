"""Config flow for MicroAirEasyTouch integration."""

from __future__ import annotations

from typing import Any

import logging
import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD, CONF_USERNAME

from .micro_air_easytouch.parser import MicroAirEasyTouchBluetoothDeviceData
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class MicroAirEasyTouchConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MicroAirEasyTouch."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_device: MicroAirEasyTouchBluetoothDeviceData | None = None
        self._discovered_devices: dict[str, str] = {}

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
        return await self.async_step_password()

    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle password and email entry with zone detection."""
        errors = {}
        if user_input is not None:
            try:
                assert self._discovered_device is not None
                self._discovered_device._email = user_input[CONF_USERNAME]
                self._discovered_device._password = user_input[CONF_PASSWORD]

                # Validate credentials and detect zones during setup
                # This ensures zones are properly detected before entity creation
                if self._discovery_info:
                    from homeassistant.components.bluetooth import (
                        async_ble_device_from_address,
                    )

                    ble_device = async_ble_device_from_address(
                        self.hass, self._discovery_info.address
                    )
                    if ble_device:
                        # Perform zone detection during credential validation
                        available_zones = (
                            await self._discovered_device.get_available_zones(
                                self.hass, ble_device
                            )
                        )
                        if available_zones:
                            # Store detected zones in the device config for later use
                            self._discovered_device._detected_zones = available_zones
                            _LOGGER.info(
                                "Config flow detected %d zones during setup: %s",
                                len(available_zones),
                                available_zones,
                            )
                            
                            # Extract and store zone configs for persistence
                            zone_configs = self._discovered_device._device_state.get("zone_configs", {})
                            if zone_configs:
                                self._discovered_device._zone_configs = zone_configs
                            else:
                                _LOGGER.debug(
                                    "No zone configs available after zone detection"
                                )
                        else:
                            _LOGGER.warning(
                                "Config flow could not detect zones; will fallback to single zone"
                            )
                            self._discovered_device._detected_zones = [0]

                return await self.async_step_bluetooth_confirm(user_input)
            except Exception as e:
                _LOGGER.error(
                    "Credential validation or zone detection failed: %s", str(e)
                )
                errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
            description_placeholders={},
        )

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery."""
        assert self._discovered_device is not None
        device = self._discovered_device
        assert self._discovery_info is not None
        discovery_info = self._discovery_info
        title = device.title or device.get_device_name() or discovery_info.name
        if user_input is not None:
            # Include detected zones in the config entry data
            config_data = {
                CONF_USERNAME: self._discovered_device._email,
                CONF_PASSWORD: self._discovered_device._password,
                CONF_ADDRESS: discovery_info.address,
            }

            # Add detected zones if available
            if (
                hasattr(self._discovered_device, "_detected_zones")
                and self._discovered_device._detected_zones
            ):
                config_data["detected_zones"] = self._discovered_device._detected_zones
                _LOGGER.info(
                    "Storing detected zones in config entry: %s",
                    self._discovered_device._detected_zones,
                )
            
            # Add zone configs if available
            if (
                hasattr(self._discovered_device, "_zone_configs")
                and self._discovered_device._zone_configs
            ):
                config_data["zone_configs"] = self._discovered_device._zone_configs
                _LOGGER.info(
                    "Storing zone configs in config entry for zones: %s",
                    list(self._discovered_device._zone_configs.keys()),
                )

            return self.async_create_entry(title=title, data=config_data)

        self._set_confirm_only()
        placeholders = {"name": title}
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders=placeholders
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step to pick discovered device."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            device = MicroAirEasyTouchBluetoothDeviceData(password=None, email=None)
            self._discovered_device = device

            # Create a minimal discovery info for the selected address
            # This is needed for zone detection in the password step
            class MockDiscoveryInfo:
                def __init__(self, address, name):
                    self.address = address
                    self.name = name

            self._discovery_info = MockDiscoveryInfo(
                address, self._discovered_devices.get(address, "MicroAir EasyTouch")
            )

            return await self.async_step_password()

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
