"""Support for MicroAirEasyTouch climate control."""

from __future__ import annotations

import logging
import asyncio
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
    PRESET_NONE,
)
from homeassistant.components.climate.const import (
    FAN_OFF,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_HIGH,
    FAN_AUTO,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature

from .const import DOMAIN, FAN_MODE_ICONS, HVAC_MODE_ICONS, PRESET_MODE_ICONS
from .micro_air_easytouch.parser import MicroAirEasyTouchBluetoothDeviceData
from .micro_air_easytouch.const import (
    HA_MODE_TO_EASY_MODE,
    EASY_MODE_TO_HA_MODE,
    HEAT_TYPE_PRESETS,
    HEAT_TYPE_REVERSE,
    FAN_MODE_REVERSE_MAP,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MicroAirEasyTouch climate platform from a config entry.

    Detects available zones for the device and creates a climate entity
    for each zone. Zone detection is attempted in the following order:
    1. Pre-detected zones stored in config entry during setup
    2. Dynamic probing via BLE device discovery
    3. Fallback to single zone 0 if detection fails

    Args:
        hass: The Home Assistant instance.
        config_entry: The config entry for this MicroAirEasyTouch device,
            containing the entry_id and unique_id (MAC address).
        async_add_entities: Callback to add climate entities to Home Assistant.

    Returns:
        None. Entities are added via async_add_entities callback.
    """
    data = hass.data[DOMAIN][config_entry.entry_id]["data"]
    mac_address = config_entry.unique_id
    if not mac_address:
        _LOGGER.error("MAC address not found in config entry")
        return

    # Try to get zones from config entry first (detected during setup)
    available_zones = config_entry.data.get("detected_zones", None)
    if available_zones:
        entities = []
        for zone in available_zones:
            entity = MicroAirEasyTouchClimate(data, mac_address, zone)
            entities.append(entity)
        async_add_entities(entities)
        return

    # Fallback: Get BLE device to probe for available zones
    ble_device = async_ble_device_from_address(hass, mac_address)
    if not ble_device:
        _LOGGER.warning("Could not find BLE device to detect zones: %s", mac_address)
        # Fall back to single zone 0 if device not found
        entity = MicroAirEasyTouchClimate(data, mac_address, 0)
        async_add_entities([entity])
        return

    # Store the BLE device for persistent use
    data.set_ble_device(ble_device)

    # Probe device for available zones
    try:
        available_zones = await data.get_available_zones(hass, ble_device)
        entities = []
        for zone in available_zones:
            entity = MicroAirEasyTouchClimate(data, mac_address, zone)
            entities.append(entity)

        async_add_entities(entities)
    except (TimeoutError, asyncio.TimeoutError, OSError) as e:
        _LOGGER.error("Failed to detect zones for device %s: %s", mac_address, str(e))
        # Fall back to single zone 0 if detection fails
        entity = MicroAirEasyTouchClimate(data, mac_address, 0)
        async_add_entities([entity])


class MicroAirEasyTouchClimate(ClimateEntity):
    """Representation of a MicroAirEasyTouch climate zone entity.

    This class models a single zone of a MicroAirEasyTouch thermostat system,
    providing control and monitoring of HVAC modes, fan speeds, target temperatures,
    and heat type presets. Each zone operates independently with its own settings.

    The entity:
    - Subscribes to device state updates for synchronization
    - Maintains local state with optimistic updates for immediate UI feedback
    - Sends commands via BLE to the physical device
    - Supports multiple HVAC modes (Cool, Heat, Auto, Fan Only, Dry, Off)
    - Adapts available fan modes based on current HVAC mode and device configuration
    - Provides heat type preset selection when in heating mode (furnace, heat pump, etc.)
    - Handles temperature setpoint control for single-mode and dual-setpoint (auto) scenarios

    Attributes:
        _data: The shared MicroAirEasyTouchBluetoothDeviceData instance for device communication.
        _mac_address: The Bluetooth MAC address of the device.
        _zone: The zone number (0-based) this entity represents.
        _state: Dict containing the current zone state (temperature, setpoints, mode, etc.).
    """
    _attr_has_entity_name = True
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
    )
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_should_poll = False
    # @todo consider using SPL values to set the gauge range 
    _attr_min_temp = 50
    _attr_max_temp = 85
    _attr_target_temperature_step = 1.0

    def __init__(
        self, data: MicroAirEasyTouchBluetoothDeviceData, mac_address: str, zone: int
    ) -> None:
        """Initialize the climate."""
        self._data = data
        self._mac_address = mac_address
        self._zone = zone
        self._attr_unique_id = f"microaireasytouch_{mac_address}_climate_zone_{zone}"
        self._attr_name = f"Zone {zone}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"MicroAirEasyTouch_{mac_address}_zone_{zone}")},
            name=f"EasyTouch Zone {zone}",
            manufacturer="Micro-Air",
            model="EasyTouch Thermostat Zone",
            via_device=(DOMAIN, f"MicroAirEasyTouch_{mac_address}"),
        )
        self._state = {}

        # Subscribe to device updates instead of individual polling
        self._unsubscribe_updates = self._data.async_subscribe_updates(
            self._handle_device_update
        )

    def _handle_device_update(self, device_state: dict) -> None:
        """Handle updates from the device data shared across all zones."""
        try:
            # Update zone-specific state from shared device state
            if "zones" in device_state and self._zone in device_state["zones"]:
                old_state = self._state.copy()
                self._state = device_state["zones"][self._zone].copy()

                # Only trigger state write if something actually changed
                if old_state != self._state:
                    _LOGGER.debug(
                        "Zone %s state updated: %s",
                        self._zone,
                        list(self._state.keys()) if self._state else "empty",
                    )
                    self.async_write_ha_state()
            elif self._zone == 0 and device_state:
                # Fallback for single-zone compatibility
                old_state = self._state.copy()
                self._state = device_state.copy()

                if old_state != self._state:
                    _LOGGER.debug(
                        "Zone %s state updated (fallback): %s",
                        self._zone,
                        list(self._state.keys()) if self._state else "empty",
                    )
                    self.async_write_ha_state()
        except (AttributeError, KeyError, TypeError) as e:
            _LOGGER.debug("Error updating zone %s state: %s", self._zone, str(e))

    def _get_speed_name_map(self, max_speed: int, available_speeds: list[int]) -> dict[int, str]:
        """Build dynamic speed-to-name mapping based on max_speed capability and available speeds.
        
        Only includes mappings for speeds that are actually available in available_speeds.
        
        Args:
            max_speed: Maximum manual speed supported (1, 2, 3+)
            available_speeds: List of numeric speeds actually available for this zone/mode
        
        Returns dict mapping numeric speed values to HA standard fan mode names:
        - max_speed=1: {1: 'high'} (single speed is always 'high')
        - max_speed=2: {1: 'low', 2: 'high'}
        - max_speed=3+: {1: 'low', 2: 'medium', 3+: 'high'}
        - Includes 0: 'off', 64: 'auto', 128: 'auto' only if present in available_speeds
        """
        speed_map = {}
        
        # Only include speeds that are actually available
        for speed in available_speeds:
            if speed == 0:
                speed_map[0] = FAN_OFF
            elif speed == 64 or speed == 128:
                speed_map[speed] = FAN_AUTO
            elif speed >= 1:
                # Map manual speeds based on max_speed capability
                if max_speed == 1:
                    # Single speed systems: any manual speed is 'high'
                    speed_map[speed] = FAN_HIGH
                elif max_speed == 2:
                    # Two speed systems: 1=low, 2+=high
                    if speed == 1:
                        speed_map[speed] = FAN_LOW
                    else:
                        speed_map[speed] = FAN_HIGH
                elif max_speed >= 3:
                    # Three+ speed systems: 1=low, 2=medium, 3+=high
                    if speed == 1:
                        speed_map[speed] = FAN_LOW
                    elif speed == 2:
                        speed_map[speed] = FAN_MEDIUM
                    else:
                        speed_map[speed] = FAN_HIGH
        
        return speed_map

    @property
    def icon(self) -> str:
        """Return the entity icon."""
        # Prefer preset-specific icons when in heat mode with a selected preset
        # Note: these do not effect the UI but are available in the entity state
        # This is a climate framework limitation
        try:
            if self.hvac_mode == HVACMode.HEAT:
                preset = self.preset_mode
                if preset and preset in PRESET_MODE_ICONS:
                    return PRESET_MODE_ICONS.get(self.preset_mode, "mdi:heat-wave")
        except (AttributeError, KeyError, TypeError):
            pass
        return HVAC_MODE_ICONS.get(self.hvac_mode, "mdi:thermostat")

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Return the list of supported features."""
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
            | ClimateEntityFeature.FAN_MODE
        )

        if self.hvac_mode == HVACMode.HEAT:
            features |= ClimateEntityFeature.PRESET_MODE

        if self.hvac_mode == HVACMode.DRY:
            features |= ClimateEntityFeature.TARGET_HUMIDITY    

        return features

    @property
    def entity_picture(self) -> str | None:
        """Return the entity icon."""
        if self.fan_mode:
            return f"mdi:{FAN_MODE_ICONS.get(self.fan_mode, 'fan')}"
        return None

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        # Entity is subscribed to updates via _handle_device_update
        # Initial state will come from the polling loop
        pass

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity is being removed from hass."""
        if hasattr(self, "_unsubscribe_updates"):
            self._unsubscribe_updates()

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._state.get("facePlateTemperature")

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        if self.hvac_mode == HVACMode.COOL:
            return self._state.get("cool_sp")
        elif self.hvac_mode == HVACMode.HEAT:
            return self._state.get("heat_sp")
        elif self.hvac_mode == HVACMode.DRY:
            return self._state.get("dry_sp")
        return None

    @property
    def target_temperature_high(self) -> float | None:
        """Return the high target temperature."""
        if self.hvac_mode == HVACMode.AUTO:
            return self._state.get("autoCool_sp")
        return None

    @property
    def target_temperature_low(self) -> float | None:
        """Return the low target temperature."""
        if self.hvac_mode == HVACMode.AUTO:
            return self._state.get("autoHeat_sp")
        return None

    @property
    def hvac_mode(self) -> HVACMode:
        """Return hvac operation mode."""
        mode_num = self._state.get("mode_num", 0)
        return EASY_MODE_TO_HA_MODE.get(mode_num, HVACMode.OFF)

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action."""
        current_mode = self._state.get("current_mode")
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        elif current_mode == "fan":
            return HVACAction.FAN
        elif current_mode in ["cool", "cool_on"]:
            return HVACAction.COOLING
        elif current_mode in ["heat", "heat_on"]:
            return HVACAction.HEATING
        elif current_mode == "dry":
            return HVACAction.DRYING
        elif current_mode == "auto":
            # In auto mode, determine action based on temperature
            current_temp = self.current_temperature
            low = self.target_temperature_low
            high = self.target_temperature_high
            if current_temp is not None and low is not None and high is not None:
                if current_temp < low:
                    return HVACAction.HEATING
                elif current_temp > high:
                    return HVACAction.COOLING
            return HVACAction.IDLE
        return HVACAction.IDLE

    @property
    def fan_mode(self) -> str | None:
        """Return the current fan mode as a standard Home Assistant name."""
        # Get the appropriate fan mode based on current HVAC mode
        if self.hvac_mode == HVACMode.FAN_ONLY:
            fan_mode_num = self._state.get("fan_mode_num", 0)
        elif self.hvac_mode == HVACMode.COOL:
            fan_mode_num = self._state.get("cool_fan_mode_num", 128)
        elif self.hvac_mode == HVACMode.HEAT:
            fan_mode_num = self._state.get("heat_fan_mode_num", 128)
        elif self.hvac_mode == HVACMode.AUTO:
            fan_mode_num = self._state.get("auto_fan_mode_num", 128)
        else:
            return FAN_AUTO

        # Use direct mapping from numeric value to Home Assistant fan mode
        current_mode_num = self._state.get("mode_num", 1)
        available_speeds = self._data.get_available_fan_speeds(
            self._zone, current_mode_num
        )

        # Special handling for auto furnace (speeds [0, 128] only)
        if set(available_speeds) == {0, 128}:
            # This is an auto furnace - ignore reported fan_mode_num and use simplified logic
            if fan_mode_num == 0:
                return FAN_OFF
            else:
                return FAN_AUTO

        # Normal mapping for other modes
        for ha_mode, numeric_values in FAN_MODE_REVERSE_MAP.items():
            if fan_mode_num in numeric_values:
                _LOGGER.debug(
                    "Zone %d fan_mode: %s mode fan_mode_num=%s -> '%s'",
                    self._zone,
                    self.hvac_mode,
                    fan_mode_num,
                    ha_mode,
                )
                return ha_mode

        # Fallback if no direct mapping found
        _LOGGER.debug(
            "Zone %d fan_mode: No direct mapping for fan_mode_num=%s, defaulting to 'auto'",
            self._zone,
            fan_mode_num,
        )
        return FAN_AUTO

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return available HVAC modes based on zone configuration."""
        # Called by Home Assistant's climate entity framework
        available_modes = self._data.get_available_modes(self._zone)

        if not available_modes:
            # Fallback to default modes if no config available
            return list(HA_MODE_TO_EASY_MODE.keys())

        # Filter HA modes based on device's available modes
        supported_hvac_modes = []
        for ha_mode, device_mode in HA_MODE_TO_EASY_MODE.items():
            if device_mode in available_modes:
                supported_hvac_modes.append(ha_mode)

        # Also check reverse mappings for additional device modes
        for device_mode in available_modes:
            if device_mode in EASY_MODE_TO_HA_MODE:
                ha_mode = EASY_MODE_TO_HA_MODE[device_mode]
                if ha_mode not in supported_hvac_modes:
                    supported_hvac_modes.append(ha_mode)

        _LOGGER.debug(
            "Zone %d HVAC modes filtered by MAV: %s",
            self._zone,
            [mode.value for mode in supported_hvac_modes],
        )
        return (
            supported_hvac_modes
            if supported_hvac_modes
            else list(HA_MODE_TO_EASY_MODE.keys())
        )

    @property
    def fan_modes(self) -> list[str]:
        """Return available fan modes based on zone configuration and current HVAC mode."""
        # Get current device mode number
        current_mode_num = self._state.get("mode_num")
        if current_mode_num is None:
            # Fallback to default modes if no state available
            if self.hvac_mode == HVACMode.FAN_ONLY:
                return [FAN_OFF, FAN_LOW, FAN_HIGH]
            return [FAN_OFF, FAN_LOW, FAN_HIGH, FAN_AUTO]

        # Get available fan speeds for current mode from configuration
        available_speeds = self._data.get_available_fan_speeds(
            self._zone, current_mode_num
        )

        if not available_speeds:
            # Fallback if no config available
            if self.hvac_mode == HVACMode.FAN_ONLY:
                return [FAN_OFF, FAN_LOW, FAN_HIGH]
            return [FAN_OFF, FAN_LOW, FAN_HIGH, FAN_AUTO]

        # Get max_speed from capabilities to build dynamic mapping
        capabilities = self._data.get_fan_capabilities(self._zone, current_mode_num)
        max_speed = capabilities.get("max_speed", 2)
        speed_map = self._get_speed_name_map(max_speed, available_speeds)

        # Map device fan speeds to HA fan mode names using dynamic mapping
        fan_mode_names = []
        for speed in available_speeds:
            if speed in speed_map:
                fan_mode_names.append(speed_map[speed])

        # Remove duplicates while preserving order
        unique_modes = []
        for mode in fan_mode_names:
            if mode not in unique_modes:
                unique_modes.append(mode)

        # Allow empty fan_mode values gracefully (HA UI can send blank during mode transitions)
        if "" not in unique_modes:
            unique_modes.append("")

        return unique_modes if unique_modes else [FAN_AUTO]
    @property
    def preset_modes(self) -> list[str]:
        """Return available heat type presets based on zone configuration."""
        if not self._data:
            return []

        # Only show heat type presets when in heating mode
        if self.hvac_mode != HVACMode.HEAT:
            return []

        available_presets = []
        for preset_name, mode_num in HEAT_TYPE_PRESETS.items():
            if self._data.is_mode_available(self._zone, mode_num):
                available_presets.append(preset_name)

        return available_presets

    @property
    def preset_mode(self) -> str:
        """Return current heat type preset."""
        if self.hvac_mode != HVACMode.HEAT:
            return PRESET_NONE

        current_mode = self._state.get("mode_num")
        if current_mode is None:
            return PRESET_NONE

        return HEAT_TYPE_REVERSE.get(current_mode, PRESET_NONE)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set heat type preset."""
        if preset_mode == PRESET_NONE:
            return

        if preset_mode not in HEAT_TYPE_PRESETS:
            _LOGGER.warning("Unknown heat type preset: %s", preset_mode)
            return

        heat_mode = HEAT_TYPE_PRESETS[preset_mode]

        # Check if this heat mode is available
        if not self._data.is_mode_available(self._zone, heat_mode):
            _LOGGER.warning(
                "Heat type %s (mode %d) not available for zone %s",
                preset_mode,
                heat_mode,
                self._zone,
            )
            return

        ble_device = self._data.get_ble_device(self.hass)
        if not ble_device:
            ble_device = async_ble_device_from_address(self.hass, self._mac_address)
            if ble_device:
                self._data.set_ble_device(ble_device)

        if not ble_device:
            _LOGGER.error("Could not find BLE device for heat type change")
            return

        message = {
            "Type": "Change",
            "Changes": {
                "zone": self._zone,
                "power": 1,
                "mode": heat_mode,
            },
        }

        _LOGGER.debug(
            "Setting heat type %s (mode %d) for zone %s",
            preset_mode,
            heat_mode,
            self._zone,
        )

        success = await self._data.send_command(self.hass, ble_device, message)

        # Optimistically update local state for immediate UI feedback
        if success:
            try:
                self._state["mode_num"] = heat_mode
                self._state["on"] = True
                self.async_write_ha_state()
                _LOGGER.debug(
                    "Heat type set to %s for zone %s", preset_mode, self._zone
                )
            except (AttributeError, KeyError, TypeError) as e:
                _LOGGER.debug("Failed to apply optimistic heat type update: %s", str(e))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        ble_device = self._data.get_ble_device(self.hass)
        if not ble_device:
            ble_device = async_ble_device_from_address(self.hass, self._mac_address)
            if ble_device:
                self._data.set_ble_device(ble_device)

        if not ble_device:
            _LOGGER.error("Could not find BLE device for temperature change")
            return

        changes = {"zone": self._zone, "power": 1}
        if ATTR_TEMPERATURE in kwargs:
            temp = int(kwargs[ATTR_TEMPERATURE])
            if self.hvac_mode == HVACMode.COOL:
                changes["cool_sp"] = temp
            elif self.hvac_mode == HVACMode.HEAT:
                changes["heat_sp"] = temp
            elif self.hvac_mode == HVACMode.DRY:
                changes["dry_sp"] = temp
        elif "target_temp_high" in kwargs and "target_temp_low" in kwargs:
            changes["autoCool_sp"] = int(kwargs["target_temp_high"])
            changes["autoHeat_sp"] = int(kwargs["target_temp_low"])

        if changes:
            # Store original state for potential rollback
            original_state = self._state.copy()

            message = {"Type": "Change", "Changes": changes}
            _LOGGER.debug(
                "Sending temperature command for zone %s: %s", self._zone, changes
            )
            success = await self._data.send_command(self.hass, ble_device, message)

            if success:
                try:
                    # Optimistically update set-points in local state
                    if "cool_sp" in changes:
                        self._state["cool_sp"] = changes["cool_sp"]
                    if "heat_sp" in changes:
                        self._state["heat_sp"] = changes["heat_sp"]
                    if "dry_sp" in changes:
                        self._state["dry_sp"] = changes["dry_sp"]
                    if "autoCool_sp" in changes:
                        self._state["autoCool_sp"] = changes["autoCool_sp"]
                    if "autoHeat_sp" in changes:
                        self._state["autoHeat_sp"] = changes["autoHeat_sp"]
                    self.async_write_ha_state()
                    _LOGGER.debug(
                        "Temperature set successfully for zone %s, immediate status update applied",
                        self._zone,
                    )

                    # Schedule a rollback check in case the device didn't actually change
                    async def _check_and_rollback():
                        await asyncio.sleep(3.0)  # Wait 3 seconds
                        # If our optimistic state hasn't been updated by device response, consider rolling back
                        current_device_state = self._data.async_get_device_data()
                        if (
                            "zones" in current_device_state
                            and self._zone in current_device_state["zones"]
                        ):
                            device_zone_state = current_device_state["zones"][
                                self._zone
                            ]
                            # Check if device state matches our optimistic changes
                            rollback_needed = False
                            if (
                                "cool_sp" in changes
                                and device_zone_state.get("cool_sp")
                                != changes["cool_sp"]
                            ):
                                rollback_needed = True
                            elif (
                                "heat_sp" in changes
                                and device_zone_state.get("heat_sp")
                                != changes["heat_sp"]
                            ):
                                rollback_needed = True

                            if rollback_needed:
                                _LOGGER.warning(
                                    "Device did not accept temperature change for zone %s, rolling back UI",
                                    self._zone,
                                )
                                # Restore original state
                                for key in [
                                    "cool_sp",
                                    "heat_sp",
                                    "dry_sp",
                                    "autoCool_sp",
                                    "autoHeat_sp",
                                ]:
                                    if key in original_state:
                                        self._state[key] = original_state[key]
                                self.async_write_ha_state()

                    asyncio.create_task(_check_and_rollback())

                except (AttributeError, KeyError, TypeError) as e:
                    _LOGGER.debug(
                        "Failed to apply optimistic temperature update: %s", str(e)
                    )
                # Note: Command execution automatically reads response for immediate verification
            else:
                _LOGGER.warning("Failed to set temperature for zone %s", self._zone)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        # Validate mode is available for this zone
        if hvac_mode not in self.hvac_modes:
            _LOGGER.warning(
                "HVAC mode %s not available for zone %s (available: %s)",
                hvac_mode,
                self._zone,
                self.hvac_modes,
            )
            return

        ble_device = self._data.get_ble_device(self.hass)
        if not ble_device:
            ble_device = async_ble_device_from_address(self.hass, self._mac_address)
            if ble_device:
                self._data.set_ble_device(ble_device)

        if not ble_device:
            _LOGGER.error("Could not find BLE device for HVAC mode change")
            return

        mode = HA_MODE_TO_EASY_MODE.get(hvac_mode)
        if mode is not None:
            # Double-check that the device mode is actually available
            if not self._data.is_mode_available(self._zone, mode):
                # For HEAT mode, try to find an available alternative heat mode
                if hvac_mode == HVACMode.HEAT:
                    alternative_mode = None
                    # Iterate through heat modes in preference order
                    for heat_preset, heat_mode_num in HEAT_TYPE_PRESETS.items():
                        if self._data.is_mode_available(self._zone, heat_mode_num):
                            alternative_mode = heat_mode_num
                            break

                    if alternative_mode is None:
                        _LOGGER.warning(
                            "Device mode %d not available for zone %s, and no alternative heat modes available",
                            mode,
                            self._zone,
                        )
                        return

                    mode = alternative_mode
                else:
                    _LOGGER.warning(
                        "Device mode %d not available for zone %s (MAV check failed)",
                        mode,
                        self._zone,
                    )
                    return
            # Note: For zone-specific OFF we must send power=1 with mode=0; power=0 is a system-wide OFF (all zones).
            message = {
                "Type": "Change",
                "Changes": {
                    "zone": self._zone,
                    "power": 1,
                    "mode": mode,
                },
            }
            success = await self._data.send_command(self.hass, ble_device, message)

            # Optimistically update local state for immediate UI feedback
            if success:
                try:
                    # Set expected local state so UI updates immediately
                    old_hvac_mode = self.hvac_mode
                    self._state["mode_num"] = mode
                    if hvac_mode == HVACMode.OFF:
                        self._state["off"] = True
                    else:
                        self._state["on"] = True

                    # Clear fan mode state when changing HVAC modes to let device determine appropriate fan mode
                    # This prevents validation errors when switching between modes with different fan capabilities
                    if old_hvac_mode != hvac_mode:
                        # Clear mode-specific fan state so it gets refreshed from device
                        for fan_key in [
                            "fan_mode_num",
                            "cool_fan_mode_num",
                            "heat_fan_mode_num",
                            "auto_fan_mode_num",
                        ]:
                            self._state.pop(fan_key, None)
                        _LOGGER.debug(
                            "HVAC mode changed from %s to %s, cleared fan mode state for refresh",
                            old_hvac_mode,
                            hvac_mode,
                        )

                    self.async_write_ha_state()
                    _LOGGER.debug(
                        "HVAC mode set successfully for zone %s, immediate status update applied",
                        self._zone,
                    )
                except (AttributeError, KeyError, TypeError) as e:
                    _LOGGER.debug(
                        "Failed to apply optimistic hvac_mode update: %s", str(e)
                    )
                # Note: Command execution automatically reads response for immediate verification
            else:
                _LOGGER.warning("Failed to set HVAC mode for zone %s", self._zone)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode using standard Home Assistant names."""
        _LOGGER.debug(
            "Fan mode change requested for zone %d: %s -> %s",
            self._zone,
            self.fan_mode,
            fan_mode,
        )

        # Ignore empty fan_mode requests that can occur during UI transitions
        if not fan_mode:
            _LOGGER.debug(
                "Zone %d ignoring empty fan_mode request during transition", self._zone
            )
            return

        # Validate fan mode is available for current HVAC mode
        if fan_mode not in self.fan_modes:
            _LOGGER.warning(
                "Fan mode %s not available for zone %s in mode %s (available: %s)",
                fan_mode,
                self._zone,
                self.hvac_mode,
                self.fan_modes,
            )
            return

        ble_device = self._data.get_ble_device(self.hass)
        if not ble_device:
            ble_device = async_ble_device_from_address(self.hass, self._mac_address)
            if ble_device:
                self._data.set_ble_device(ble_device)

        if not ble_device:
            _LOGGER.error("Could not find BLE device for fan mode change")
            return

        # Map standard name to device value and validate
        current_mode_num = self._state.get(
            "mode_num", 1
        )  # Default to fan-only if unknown
        available_speeds = self._data.get_available_fan_speeds(
            self._zone, current_mode_num
        )
        _LOGGER.debug(
            "Zone %d current mode %d, available speeds: %s",
            self._zone,
            current_mode_num,
            available_speeds,
        )

        # Get capabilities and build dynamic speed mapping
        capabilities = self._data.get_fan_capabilities(self._zone, current_mode_num)
        max_speed = capabilities.get("max_speed", 2)
        speed_map = self._get_speed_name_map(max_speed, available_speeds)
        
        # Build reverse map (name -> speeds)
        name_to_speeds = {}
        for speed, name in speed_map.items():
            if name not in name_to_speeds:
                name_to_speeds[name] = []
            name_to_speeds[name].append(speed)

        # Map fan mode name to speed value
        if fan_mode == FAN_OFF:
            fan_value = 0
        elif fan_mode == FAN_AUTO:
            # Use manual auto (64) if available, fallback to full auto (128)
            fan_value = 64 if 64 in available_speeds else 128
        elif fan_mode in (FAN_LOW, FAN_MEDIUM, FAN_HIGH):
            # Get candidate speeds for this mode name from dynamic mapping
            candidate_speeds = name_to_speeds.get(fan_mode, [])
            
            # For cool mode with low/high, preserve manual vs cycled setting
            if self.hvac_mode == HVACMode.COOL and fan_mode in (FAN_LOW, FAN_HIGH):
                current_fan_num = self._state.get("cool_fan_mode_num", 1)
                # Check if currently using cycled variant (65=cycled low, 66=cycled high)
                if fan_mode == FAN_LOW:
                    fan_value = 65 if current_fan_num == 65 else 1
                elif fan_mode == FAN_HIGH:
                    # For high, use the appropriate speed based on max_speed
                    base_high_speed = max_speed if max_speed <= 2 else 2
                    fan_value = 66 if current_fan_num == 66 else base_high_speed
                _LOGGER.debug(
                    "Zone %d cool mode: current_fan_num=%d, fan_mode='%s', using fan_value=%d",
                    self._zone,
                    current_fan_num,
                    fan_mode,
                    fan_value,
                )
            else:
                # Use first available candidate speed that's actually available
                fan_value = None
                for speed in candidate_speeds:
                    if speed in available_speeds:
                        fan_value = speed
                        break
                if fan_value is None:
                    _LOGGER.warning(
                        "No available speed for fan mode %s (candidates: %s, available: %s)",
                        fan_mode,
                        candidate_speeds,
                        available_speeds,
                    )
                    return
        else:
            _LOGGER.warning("Unknown fan mode: %s", fan_mode)
            return

        # Validate the fan speed is actually available for this mode
        if fan_value not in available_speeds:
            _LOGGER.warning(
                "Fan speed %d not available for zone %s mode %d (available: %s)",
                fan_value,
                self._zone,
                current_mode_num,
                available_speeds,
            )
            return
        if self.hvac_mode == HVACMode.FAN_ONLY:
            _LOGGER.debug(
                "Zone %d current fan_mode_num before command: %s",
                self._zone,
                self._state.get("fan_mode_num"),
            )
            message = {
                "Type": "Change",
                "Changes": {"zone": self._zone, "fanOnly": fan_value},
            }
            _LOGGER.debug(
                "Sending fan-only command for zone %d: %s", self._zone, message
            )
            success = await self._data.send_command(self.hass, ble_device, message)

            if success:
                try:
                    # Optimistically set expected fan mode in local state
                    self._state["fan_mode_num"] = fan_value
                    self.async_write_ha_state()
                    _LOGGER.debug(
                        "Fan-only mode set successfully for zone %s, immediate status update applied",
                        self._zone,
                    )
                except (AttributeError, KeyError, TypeError) as e:
                    _LOGGER.debug(
                        "Failed to apply optimistic fan-only update: %s", str(e)
                    )
                # Note: Command execution automatically reads response for immediate verification
            else:
                _LOGGER.warning("Failed to set fan-only mode for zone %s", self._zone)
        else:
            # For other HVAC modes, use the determined fan_value with mode-specific commands
            changes = {"zone": self._zone}
            if self.hvac_mode == HVACMode.COOL:
                changes["coolFan"] = fan_value
                _LOGGER.debug(
                    "Sending cool fan command for zone %d: coolFan=%d",
                    self._zone,
                    fan_value,
                )
            elif self.hvac_mode == HVACMode.HEAT:
                # For heat mode, we need to use the correct fan field based on the specific heat mode
                # Mode 3,4 = furnace modes
                mode_num = self._state.get("mode_num", 5)
                if mode_num in (3, 4):
                    # Furnace modes use a different fan field
                    changes["heatFan"] = fan_value
                else:
                    # Heat pump (5) heat strip (7) and electric heat (12) use eleFan (electric fan)
                    changes["eleFan"] = fan_value
            elif self.hvac_mode == HVACMode.AUTO:
                changes["autoFan"] = fan_value
            message = {"Type": "Change", "Changes": changes}
            _LOGGER.debug(
                "Sending non-fan-only command for zone %d: %s", self._zone, message
            )
            success = await self._data.send_command(self.hass, ble_device, message)

            if success:
                try:
                    # Optimistically set expected fan mode in correct slot
                    if self.hvac_mode == HVACMode.COOL:
                        self._state["cool_fan_mode_num"] = fan_value
                    elif self.hvac_mode == HVACMode.HEAT:
                        self._state["heat_fan_mode_num"] = fan_value
                    elif self.hvac_mode == HVACMode.AUTO:
                        self._state["auto_fan_mode_num"] = fan_value
                    self.async_write_ha_state()
                    _LOGGER.debug(
                        "Fan mode set successfully for zone %s, immediate status update applied",
                        self._zone,
                    )
                except (AttributeError, KeyError, TypeError) as e:
                    _LOGGER.debug("Failed to apply optimistic fan update: %s", str(e))
                # Note: Command execution automatically reads response for immediate verification
            else:
                _LOGGER.warning("Failed to set fan mode for zone %s", self._zone)

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional state attributes."""
        attrs: dict = {}
        # Expose some raw fields that maybe useful for debug/automation
        for k in (
            "mode_num",
            "active_state_num",
            "heat_source",
            "on",
            "off",
            "facePlateTemperature",
        ):
            if k in self._state:
                attrs[k] = self._state[k]

        # Add zone configuration info for debugging
        zone_config = self._data.get_zone_config(self._zone)
        if zone_config:
            attrs["zone_config"] = {
                "available_modes": self._data.get_available_modes(self._zone),
                "mav_bitmask": zone_config.get("MAV", 0),
                "setpoint_limits": zone_config.get("SPL", []),
                "fan_config_sample": zone_config.get("FA", [])[:6],  
            }

        return attrs

    def _handle_update(self, full_state) -> None:
        # Update self._state from parser (guard for missing data)
        new_zone_state = (
            full_state.get("zones", {}).get(self._zone)
            if full_state is not None
            else None
        )
        if new_zone_state is None:
            _LOGGER.debug(
                "No state for zone %s in update; full_state present: %s",
                self._zone,
                bool(full_state),
            )
            return

        prev_state = dict(self._state) if self._state else {}
        prev_mode = prev_state.get("mode_num")
        prev_hvac = (
            EASY_MODE_TO_HA_MODE.get(prev_mode, HVACMode.OFF)
            if prev_mode is not None
            else None
        )

        self._state = new_zone_state

        new_mode = self._state.get("mode_num")
        new_hvac = EASY_MODE_TO_HA_MODE.get(new_mode, HVACMode.OFF)

        if prev_mode != new_mode:
            _LOGGER.debug(
                "Zone %s updated: mode_num %s -> %s, hvac %s -> %s",
                self._zone,
                prev_mode,
                new_mode,
                prev_hvac,
                new_hvac,
            )

        try:
            self.async_write_ha_state()
        except (AttributeError, KeyError, TypeError) as e:
            _LOGGER.debug("Error writing HA state for zone %s: %s", self._zone, str(e))
