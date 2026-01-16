"""Support for MicroAirEasyTouch climate control."""
from __future__ import annotations

import logging
import time
import asyncio
from typing import Any
from datetime import timedelta

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN
from .micro_air_easytouch.parser import MicroAirEasyTouchBluetoothDeviceData
from .micro_air_easytouch.const import (
    UUIDS,
    HA_MODE_TO_EASY_MODE,
    EASY_MODE_TO_HA_MODE,
    FAN_MODES_FAN_ONLY,
    FAN_MODES_REVERSE,
)

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(seconds=60)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MicroAirEasyTouch climate platform."""
    data = hass.data[DOMAIN][config_entry.entry_id]["data"]
    mac_address = config_entry.unique_id
    
    # Get BLE device to probe for available zones
    ble_device = async_ble_device_from_address(hass, mac_address)
    if not ble_device:
        _LOGGER.error("Could not find BLE device to detect zones: %s", mac_address)
        # Fall back to single zone if device not found
        entity = MicroAirEasyTouchClimate(data, mac_address, 0)
        async_add_entities([entity])
        return
    
    # Store the BLE device for persistent use
    data.set_ble_device(ble_device)
    
    # Probe device for available zones
    try:
        available_zones = await data.get_available_zones(hass, ble_device)
        _LOGGER.info("Detected zones for device %s: %s", mac_address, available_zones)
        
        entities = []
        for zone in available_zones:
            entity = MicroAirEasyTouchClimate(data, mac_address, zone)
            entities.append(entity)
        
        async_add_entities(entities)
    except Exception as e:
        _LOGGER.error("Failed to detect zones for device %s: %s", mac_address, str(e))
        # Fall back to single zone if detection fails
        entity = MicroAirEasyTouchClimate(data, mac_address, 0)
        async_add_entities([entity])

class MicroAirEasyTouchClimate(ClimateEntity):
    """Representation of MicroAirEasyTouch Climate."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
    )
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_hvac_modes = list(HA_MODE_TO_EASY_MODE.keys())
    _attr_should_poll = False
    _attr_min_temp = 55
    _attr_max_temp = 85
    _attr_target_temperature_step = 1.0

    # Map our modes to Home Assistant fan icons
    _FAN_MODE_ICONS = {
        "off": "mdi:fan-off",
        "low": "mdi:fan-speed-1",
        "high": "mdi:fan-speed-3",
        "manualL": "mdi:fan-speed-1",
        "manualH": "mdi:fan-speed-3",
        "cycledL": "mdi:fan-clock",
        "cycledH": "mdi:fan-clock",
        "full auto": "mdi:fan-auto",
    }

    # Map HVAC modes to icons
    _HVAC_MODE_ICONS = {
        HVACMode.OFF: "mdi:power",
        HVACMode.HEAT: "mdi:fire",
        HVACMode.COOL: "mdi:snowflake",
        HVACMode.AUTO: "mdi:autorenew",
        HVACMode.FAN_ONLY: "mdi:fan",
        HVACMode.DRY: "mdi:water-percent",
    }

    # Map device fan modes to Home Assistant standard names
    _FAN_MODE_MAP = {
        "off": "off",
        "low": "low",
        "manualL": "low",
        "cycledL": "low",
        "high": "high",
        "manualH": "high",
        "cycledH": "high",
        "full auto": "auto",
    }
    _FAN_MODE_REVERSE_MAP = {
        "off": [0],
        "low": [1, 65],
        "high": [2, 66],
        "auto": [128],
    }

    def __init__(self, data: MicroAirEasyTouchBluetoothDeviceData, mac_address: str, zone: int) -> None:
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
        self._unsubscribe_updates = self._data.async_subscribe_updates(self._handle_device_update)

    def _handle_device_update(self, device_state: dict) -> None:
        """Handle updates from the device data shared across all zones."""
        try:
            # Update zone-specific state from shared device state
            if 'zones' in device_state and self._zone in device_state['zones']:
                old_state = self._state.copy()
                self._state = device_state['zones'][self._zone].copy()
                
                # Only trigger state write if something actually changed
                if old_state != self._state:
                    _LOGGER.debug("Zone %s state updated: %s", self._zone, 
                                list(self._state.keys()) if self._state else "empty")
                    self.async_write_ha_state()
            elif self._zone == 0 and device_state:
                # Fallback for single-zone compatibility
                old_state = self._state.copy()
                self._state = device_state.copy()
                
                if old_state != self._state:
                    _LOGGER.debug("Zone %s state updated (fallback): %s", self._zone, 
                                list(self._state.keys()) if self._state else "empty")
                    self.async_write_ha_state()
        except Exception as e:
            _LOGGER.debug("Error updating zone %s state: %s", self._zone, str(e))

    @property
    def icon(self) -> str:
        """Return the entity icon."""
        return self._HVAC_MODE_ICONS.get(self.hvac_mode, "mdi:thermostat")

    @property
    def entity_picture(self) -> str | None:
        """Return the entity picture."""
        if self.fan_mode:
            return f"mdi:{self._FAN_MODE_ICONS.get(self.fan_mode, 'fan')}"
        return None

    @property
    def current_fan_icon(self) -> str:
        """Return the icon to use for the current fan mode."""
        return self._FAN_MODE_ICONS.get(self.fan_mode, "mdi:fan")

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        # Entity is now subscribed to updates via _handle_device_update
        # Initial state will come from the polling loop
        pass

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity is being removed from hass."""
        if hasattr(self, '_unsubscribe_updates'):
            self._unsubscribe_updates()

    async def _async_fetch_initial_state(self) -> None:
        """DEPRECATED: Fetch the initial state from the device.
        
        This method is no longer used. State updates come from the shared
        polling loop to prevent connection conflicts.
        """
        _LOGGER.debug("_async_fetch_initial_state called for zone %s - using shared polling instead", self._zone)

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
        if self.hvac_mode == HVACMode.FAN_ONLY:
            fan_mode_num = self._state.get("fan_mode_num", 0)
            mode = FAN_MODES_FAN_ONLY.get(fan_mode_num, "off")
        elif self.hvac_mode == HVACMode.COOL:
            fan_mode_num = self._state.get("cool_fan_mode_num", 128)
            mode = FAN_MODES_REVERSE.get(fan_mode_num, "full auto")
        elif self.hvac_mode == HVACMode.HEAT:
            fan_mode_num = self._state.get("heat_fan_mode_num", 128)
            mode = FAN_MODES_REVERSE.get(fan_mode_num, "full auto")
        elif self.hvac_mode == HVACMode.AUTO:
            fan_mode_num = self._state.get("auto_fan_mode_num", 128)
            mode = FAN_MODES_REVERSE.get(fan_mode_num, "full auto")
        else:
            mode = "full auto"
        return self._FAN_MODE_MAP.get(mode, "auto")

    @property
    def fan_modes(self) -> list[str]:
        """Return available fan modes as standard Home Assistant names."""
        if self.hvac_mode == HVACMode.FAN_ONLY:
            return ["off", "low", "high"]
        return ["off", "low", "high", "auto"]

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        ble_device = async_ble_device_from_address(self.hass, self._mac_address)
        if not ble_device:
            _LOGGER.error("Could not find BLE device")
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
            message = {"Type": "Change", "Changes": changes}
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
                    _LOGGER.debug("Temperature set successfully for zone %s, immediate status update applied", self._zone)
                except Exception as e:
                    _LOGGER.debug("Failed to apply optimistic temperature update: %s", str(e))
                # Note: Command execution automatically reads response for immediate verification
            else:
                _LOGGER.warning("Failed to set temperature for zone %s", self._zone)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        ble_device = async_ble_device_from_address(self.hass, self._mac_address)
        if not ble_device:
            _LOGGER.error("Could not find BLE device")
            return

        mode = HA_MODE_TO_EASY_MODE.get(hvac_mode)
        if mode is not None:
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

            # Optimistically update local state for immediate UI feedback and schedule a verification fetch
            if success:
                try:
                    # Set expected local state so UI updates immediately
                    self._state["mode_num"] = mode
                    if hvac_mode == HVACMode.OFF:
                        self._state["off"] = True
                    else:
                        self._state["on"] = True
                    self.async_write_ha_state()
                    _LOGGER.debug("HVAC mode set successfully for zone %s, immediate status update applied", self._zone)
                except Exception as e:
                    _LOGGER.debug("Failed to apply optimistic hvac_mode update: %s", str(e))
                # Note: Command execution automatically reads response for immediate verification
            else:
                _LOGGER.warning("Failed to set HVAC mode for zone %s", self._zone)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode using standard Home Assistant names."""
        ble_device = async_ble_device_from_address(self.hass, self._mac_address)
        if not ble_device:
            _LOGGER.error("Could not find BLE device")
            return

        # Map standard name to device value
        if self.hvac_mode == HVACMode.FAN_ONLY:
            if fan_mode == "off":
                fan_value = 0
            elif fan_mode == "low":
                fan_value = 1
            elif fan_mode == "high":
                fan_value = 2
            else:
                fan_value = 0
            message = {"Type": "Change", "Changes": {"zone": self._zone, "fanOnly": fan_value}}
            success = await self._data.send_command(self.hass, ble_device, message)

            if success:
                try:
                    # Optimistically set expected fan mode in local state
                    self._state["fan_mode_num"] = fan_value
                    self.async_write_ha_state()
                    _LOGGER.debug("Fan-only mode set successfully for zone %s, immediate status update applied", self._zone)
                except Exception as e:
                    _LOGGER.debug("Failed to apply optimistic fan-only update: %s", str(e))
                # Note: Command execution automatically reads response for immediate verification
            else:
                _LOGGER.warning("Failed to set fan-only mode for zone %s", self._zone)
        else:
            if fan_mode == "off":
                fan_value = 0
            elif fan_mode == "low":
                fan_value = 1  # manualL
            elif fan_mode == "high":
                fan_value = 2  # manualH
            elif fan_mode == "auto":
                fan_value = 128  # full auto
            else:
                fan_value = 128
            changes = {"zone": self._zone}
            if self.hvac_mode == HVACMode.COOL:
                changes["coolFan"] = fan_value
            elif self.hvac_mode == HVACMode.HEAT:
                # For heat mode, we need to use the correct fan field based on the specific heat mode
                # Mode 3,4 = furnace modes, Mode 5 = heat pump, Mode 7 = heat strip
                mode_num = self._state.get("mode_num", 5)
                if mode_num in (3, 4):
                    # Furnace modes use a different fan field
                    changes["furnaceFan"] = fan_value
                else:
                    # Heat pump (5) and heat strip (7) use heatFan
                    changes["heatFan"] = fan_value
            elif self.hvac_mode == HVACMode.AUTO:
                changes["autoFan"] = fan_value
            message = {"Type": "Change", "Changes": changes}
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
                    _LOGGER.debug("Fan mode set successfully for zone %s, immediate status update applied", self._zone)
                except Exception as e:
                    _LOGGER.debug("Failed to apply optimistic fan update: %s", str(e))
                # Note: Command execution automatically reads response for immediate verification
            else:
                _LOGGER.warning("Failed to set fan mode for zone %s", self._zone)

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional state attributes."""
        attrs: dict = {}
        # Expose raw fields useful for debugging and automation
        for k in (
            "mode_num",
            "current_mode_num",
            "heat_source",
            "on",
            "off",
            "facePlateTemperature",
        ):
            if k in self._state:
                attrs[k] = self._state.get(k)
        # Indicate whether device supports notifications
        attrs["notifications_supported"] = getattr(self._data, "_notifications_supported", None)
        return attrs

    async def async_update(self) -> None:
        """Update the entity state manually if needed."""
        _LOGGER.debug("Updating state for zone %s", self._zone)
        await self._async_fetch_initial_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # perform initial fetch
        await self._async_fetch_initial_state()
        # subscribe to parser updates (store unsubscribe)
        self._unsub_updates = self._data.async_subscribe_updates(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
      
        # Unsubscribe from updates
        if self._unsub_updates:
            self._unsub_updates()
            self._unsub_updates = None

    def _handle_update(self, full_state) -> None:
        # Update self._state from parser (guard for missing data)
        new_zone_state = full_state.get("zones", {}).get(self._zone) if full_state is not None else None
        if new_zone_state is None:
            _LOGGER.debug("No state for zone %s in update; full_state present: %s", self._zone, bool(full_state))
            return
            
        prev_state = dict(self._state) if self._state else {}
        prev_mode = prev_state.get("mode_num")
        prev_hvac = EASY_MODE_TO_HA_MODE.get(prev_mode, HVACMode.OFF) if prev_mode is not None else None

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
        except Exception as e:
            _LOGGER.debug("Error writing HA state for zone %s: %s", self._zone, str(e))
