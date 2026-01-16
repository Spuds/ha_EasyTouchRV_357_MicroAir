"""Service handlers for MicroAirEasyTouch integration."""
from __future__ import annotations

import logging
import time
import voluptuous as vol

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN
from .micro_air_easytouch.parser import MicroAirEasyTouchBluetoothDeviceData

_LOGGER = logging.getLogger(__name__)


def _normalize(s: str) -> str:
    """Normalize identifiers for loose matching (lowercase, strip separators)."""
    return str(s).lower().replace(':', '').replace('-', '').replace('.', '')

# Service schema for validation
SERVICE_SET_LOCATION_SCHEMA = vol.Schema(
    {
        vol.Required("address"): cv.string,
        vol.Required("latitude"): vol.All(vol.Coerce(float), vol.Range(min=-90.0, max=90.0)),
        vol.Required("longitude"): vol.All(vol.Coerce(float), vol.Range(min=-180.0, max=180.0)),
    }
)

async def async_register_services(hass: HomeAssistant) -> None:
    """Register services for the MicroAirEasyTouch integration."""
    async def handle_set_location(call: ServiceCall) -> None:
        """Handle the set_location service call."""
        address = call.data.get("address")
        latitude = call.data.get("latitude")
        longitude = call.data.get("longitude")

        # Find the config entry by MAC address (unique_id)
        config_entry = None
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.unique_id == address:
                config_entry = entry
                break

        if not config_entry:
            _LOGGER.error("No MicroAirEasyTouch config entry found for address %s", address)
            return

        # Get the device data
        device_data: MicroAirEasyTouchBluetoothDeviceData = hass.data[DOMAIN][config_entry.entry_id]["data"]
        mac_address = config_entry.unique_id
        assert mac_address is not None

        # Get BLE device - try stored device first, then fresh lookup, then minimal device
        ble_device = device_data.get_ble_device(hass)
        if not ble_device:
            ble_device = async_ble_device_from_address(hass, mac_address)
            if ble_device:
                device_data.set_ble_device(ble_device)
        
        if not ble_device:
            _LOGGER.warning("Could not find BLE device for address %s - attempting connection with minimal device", mac_address)
            # The get_ble_device method will create a minimal device if it has the address stored
            ble_device = device_data.get_ble_device(hass)
        
        if not ble_device:
            _LOGGER.error("Could not create any BLE device reference for address %s", mac_address)
            return

        # Construct the command
        command = {
            "Type": "Get Status",
            "Zone": 0, # Location setting is global, not zone-specific
            "LAT": f"{latitude:.5f}",
            "LON": f"{longitude:.5f}",
            "TM": int(time.time())
        }

        # Send the command
        try:
            success = await device_data.send_command(hass, ble_device, command)
            if success:
                _LOGGER.info("Successfully sent location (LAT: %s, LON: %s) to device %s", latitude, longitude, mac_address)
            else:
                _LOGGER.error("Failed to send location command to device %s", mac_address)
        except Exception as e:
            _LOGGER.error("Error sending location command to device %s: %s", mac_address, str(e))

    # Register the set_location service
    hass.services.async_register(
        DOMAIN,
        "set_location",
        handle_set_location,
        schema=SERVICE_SET_LOCATION_SCHEMA,
    )

    # Schema and handler for sending a raw mode integer to a zone (developer testing)
    SERVICE_TEST_SET_MODE_SCHEMA = vol.Schema(
        {
            vol.Optional("address"): cv.string,
            vol.Optional("entity_id"): cv.entity_id,
            vol.Required("zone"): vol.All(int, vol.Range(min=0)),
            vol.Required("mode"): vol.All(int, vol.Range(min=0)),
            vol.Optional("power", default=1): vol.Any(0, 1),
        }
    )

    async def handle_test_set_mode(call: ServiceCall) -> None:
        """Handle the test_set_mode service call (developer-only)."""
        address = call.data.get("address")
        entity_id = call.data.get("entity_id")
        zone = int(call.data.get("zone"))
        mode = int(call.data.get("mode"))
        power = int(call.data.get("power", 1))

        # Resolve config entry similar to request_quick_poll
        config_entry = None
        for entry in hass.config_entries.async_entries(DOMAIN):
            if address and entry.unique_id == address:
                config_entry = entry
                break

        if not config_entry and entity_id:
            try:
                from homeassistant.helpers import entity_registry as er
                registry = er.async_get(hass)
                entity = registry.async_get(entity_id)
                if entity and entity.config_entry_id:
                    config_entry = hass.config_entries.async_get_entry(entity.config_entry_id)
            except Exception:
                pass

        if not config_entry and address:
            norm = _normalize(address)
            for entry in hass.config_entries.async_entries(DOMAIN):
                if entry.unique_id and _normalize(str(entry.unique_id)) == norm:
                    config_entry = entry
                    break

        if not config_entry:
            known = [entry.unique_id for entry in hass.config_entries.async_entries(DOMAIN)]
            _LOGGER.error(
                "No MicroAirEasyTouch config entry found for address/entity '%s' (known entries: %s)",
                address or entity_id,
                known,
            )
            return

        device_data: MicroAirEasyTouchBluetoothDeviceData = hass.data[DOMAIN][config_entry.entry_id]["data"]

        # Determine BLE address to use and get stored device with fallback
        ble_address = address or config_entry.unique_id
        ble_device = device_data.get_ble_device(hass)
        if not ble_device:
            ble_device = async_ble_device_from_address(hass, ble_address)
            if ble_device:
                device_data.set_ble_device(ble_device)
        
        if not ble_device:
            _LOGGER.warning("Could not find BLE device for address %s - attempting connection with minimal device", ble_address)
            # Ensure address is stored for minimal device creation
            device_data.set_device_address(ble_address)
            ble_device = device_data.get_ble_device(hass)
        
        if not ble_device:
            _LOGGER.error("Could not create any BLE device reference for address %s", ble_address)
            return

        # Build and send raw Change command
        command = {"Type": "Change", "Changes": {"zone": zone, "mode": mode, "power": power}}
        try:
            success = await device_data.send_command(hass, ble_device, command)
            if success:
                _LOGGER.info("Sent test mode %d to zone %d on device %s", mode, zone, ble_address)
            else:
                _LOGGER.error("Failed to send test mode %d to device %s", mode, ble_address)
        except Exception as e:
            _LOGGER.error("Error sending test mode to device %s: %s", ble_address, str(e))

    hass.services.async_register(
        DOMAIN,
        "test_set_mode",
        handle_test_set_mode,
        schema=SERVICE_TEST_SET_MODE_SCHEMA,
    )

    # Schema and handler for requesting device status (developer testing)
    SERVICE_TEST_GET_STATUS_SCHEMA = vol.Schema(
        {
            vol.Optional("address"): cv.string,
            vol.Optional("entity_id"): cv.entity_id,
            vol.Optional("zone", default=0): vol.All(int, vol.Range(min=0)),
            vol.Optional("type", default="Get Status"): cv.string,
        }
    )

    async def handle_test_get_status(call: ServiceCall) -> None:
        """Handle the test_get_status service call (developer-only)."""
        address = call.data.get("address")
        entity_id = call.data.get("entity_id")
        zone = int(call.data.get("zone", 0))
        command_type = call.data.get("type", "Get Status")

        # Resolve config entry similar to other services
        config_entry = None
        for entry in hass.config_entries.async_entries(DOMAIN):
            if address and entry.unique_id == address:
                config_entry = entry
                break

        if not config_entry and entity_id:
            try:
                from homeassistant.helpers import entity_registry as er
                registry = er.async_get(hass)
                entity = registry.async_get(entity_id)
                if entity and entity.config_entry_id:
                    config_entry = hass.config_entries.async_get_entry(entity.config_entry_id)
            except Exception:
                pass

        if not config_entry and address:
            norm = _normalize(address)
            for entry in hass.config_entries.async_entries(DOMAIN):
                if entry.unique_id and _normalize(str(entry.unique_id)) == norm:
                    config_entry = entry
                    break

        if not config_entry:
            known = [entry.unique_id for entry in hass.config_entries.async_entries(DOMAIN)]
            _LOGGER.error(
                "No MicroAirEasyTouch config entry found for address/entity '%s' (known entries: %s)",
                address or entity_id,
                known,
            )
            return

        device_data: MicroAirEasyTouchBluetoothDeviceData = hass.data[DOMAIN][config_entry.entry_id]["data"]

        # Determine BLE address to use and get stored device with fallback
        ble_address = address or config_entry.unique_id
        ble_device = device_data.get_ble_device(hass)
        if not ble_device:
            ble_device = async_ble_device_from_address(hass, ble_address)
            if ble_device:
                device_data.set_ble_device(ble_device)
        
        if not ble_device:
            _LOGGER.warning("Could not find BLE device for address %s - attempting connection with minimal device", ble_address)
            # Ensure address is stored for minimal device creation
            device_data.set_device_address(ble_address)
            ble_device = device_data.get_ble_device(hass)
        
        if not ble_device:
            _LOGGER.error("Could not create any BLE device reference for address %s", ble_address)
            return

        # Build and send command with configurable type
        command = {
            "Type": command_type, 
            "Zone": zone, 
            "EM": device_data._email, 
            "TM": int(time.time())
        }
        
        _LOGGER.info("Requesting '%s' for zone %d from device %s", command_type, zone, ble_address)
        try:
            success = await device_data.send_command(hass, ble_device, command)
            if success:
                # Read the response and log it for debugging
                from .micro_air_easytouch.const import UUIDS
                json_payload = await device_data._read_gatt_with_retry(hass, UUIDS["jsonReturn"], ble_device)
                if json_payload:
                    try:
                        payload_str = json_payload.decode('utf-8')
                        _LOGGER.debug("Raw status response for zone %d: %s", zone, payload_str)
                        
                        # Also parse and log in a more readable format
                        parsed_data = device_data.decrypt(json_payload)
                        _LOGGER.info("Parsed response for '%s' zone %d from device %s:", command_type, zone, ble_address)
                        _LOGGER.info("  Available zones: %s", parsed_data.get('available_zones', 'unknown'))
                        if 'zones' in parsed_data and zone in parsed_data['zones']:
                            zone_data = parsed_data['zones'][zone]
                            _LOGGER.info("  Zone %d data: %s", zone, zone_data)
                        else:
                            _LOGGER.info("  Full device data: %s", parsed_data)
                    except Exception as e:
                        _LOGGER.debug("Error decoding status response: %s", str(e))
                        _LOGGER.debug("Raw bytes: %s", json_payload)
                else:
                    _LOGGER.warning("No response payload received for status request")
            else:
                _LOGGER.error("Failed to send status request to device %s", ble_address)
        except Exception as e:
            _LOGGER.error("Error requesting status from device %s: %s", ble_address, str(e))

    hass.services.async_register(
        DOMAIN,
        "test_get_status",
        handle_test_get_status,
        schema=SERVICE_TEST_GET_STATUS_SCHEMA,
    )

    # Schema and handler for sending arbitrary Change commands (developer testing)
    SERVICE_TEST_SEND_CHANGES_SCHEMA = vol.Schema(
        {
            vol.Optional("address"): cv.string,
            vol.Optional("entity_id"): cv.entity_id,
            vol.Required("zone"): vol.All(int, vol.Range(min=0)),
            vol.Optional("changes"): dict,  # Arbitrary key-value pairs for Changes
        }
    )

    async def handle_test_send_changes(call: ServiceCall) -> None:
        """Handle the test_send_changes service call (developer-only)."""
        address = call.data.get("address")
        entity_id = call.data.get("entity_id")
        zone = int(call.data.get("zone"))
        changes_dict = call.data.get("changes", {})

        # Resolve config entry similar to other services
        config_entry = None
        for entry in hass.config_entries.async_entries(DOMAIN):
            if address and entry.unique_id == address:
                config_entry = entry
                break

        if not config_entry and entity_id:
            try:
                from homeassistant.helpers import entity_registry as er
                registry = er.async_get(hass)
                entity = registry.async_get(entity_id)
                if entity and entity.config_entry_id:
                    config_entry = hass.config_entries.async_get_entry(entity.config_entry_id)
            except Exception:
                pass

        if not config_entry and address:
            norm = _normalize(address)
            for entry in hass.config_entries.async_entries(DOMAIN):
                if entry.unique_id and _normalize(str(entry.unique_id)) == norm:
                    config_entry = entry
                    break

        if not config_entry:
            known = [entry.unique_id for entry in hass.config_entries.async_entries(DOMAIN)]
            _LOGGER.error(
                "No MicroAirEasyTouch config entry found for address/entity '%s' (known entries: %s)",
                address or entity_id,
                known,
            )
            return

        device_data: MicroAirEasyTouchBluetoothDeviceData = hass.data[DOMAIN][config_entry.entry_id]["data"]

        # Determine BLE address to use and get stored device with fallback
        ble_address = address or config_entry.unique_id
        ble_device = device_data.get_ble_device(hass)
        if not ble_device:
            ble_device = async_ble_device_from_address(hass, ble_address)
            if ble_device:
                device_data.set_ble_device(ble_device)
        
        if not ble_device:
            _LOGGER.warning("Could not find BLE device for address %s - attempting connection with minimal device", ble_address)
            # Ensure address is stored for minimal device creation
            device_data.set_device_address(ble_address)
            ble_device = device_data.get_ble_device(hass)
        
        if not ble_device:
            _LOGGER.error("Could not create any BLE device reference for address %s", ble_address)
            return

        # Build and send custom Change command with arbitrary changes
        changes = {"zone": zone}
        changes.update(changes_dict)
        command = {"Type": "Change", "Changes": changes}
        
        _LOGGER.info("Sending test changes to zone %d on device %s: %s", zone, ble_address, changes_dict)
        try:
            success = await device_data.send_command(hass, ble_device, command)
            if success:
                _LOGGER.info("Successfully sent test changes to zone %d on device %s", zone, ble_address)
            else:
                _LOGGER.error("Failed to send test changes to device %s", ble_address)
        except Exception as e:
            _LOGGER.error("Error sending test changes to device %s: %s", ble_address, str(e))

    hass.services.async_register(
        DOMAIN,
        "test_send_changes",
        handle_test_send_changes,
        schema=SERVICE_TEST_SEND_CHANGES_SCHEMA,
    )

async def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister services for the MicroAirEasyTouch integration."""
    hass.services.async_remove(DOMAIN, "set_location")
    hass.services.async_remove(DOMAIN, "test_set_mode")
    hass.services.async_remove(DOMAIN, "test_get_status")
    hass.services.async_remove(DOMAIN, "test_send_changes")
