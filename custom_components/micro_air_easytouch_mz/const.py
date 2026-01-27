"""Constants for MicroAirEasyTouch"""

from homeassistant.components.climate import HVACMode

DOMAIN = "micro_air_easytouch_mz"

# Map our modes to Home Assistant fan icons
FAN_MODE_ICONS = {
    "off": "mdi:fan-off",
    "low": "mdi:fan-speed-1",
    "medium": "mdi:fan-speed-2",
    "high": "mdi:fan-speed-3",
    "manualL": "mdi:fan-speed-1",
    "manualH": "mdi:fan-speed-3",
    "cycledL": "mdi:fan-clock",
    "cycledH": "mdi:fan-clock",
    "auto": "mdi:fan-auto",
}

# Map HVAC modes to icons
HVAC_MODE_ICONS = {
    HVACMode.OFF: "mdi:power",
    HVACMode.HEAT: "mdi:fire",
    HVACMode.COOL: "mdi:snowflake",
    HVACMode.AUTO: "mdi:autorenew",
    HVACMode.FAN_ONLY: "mdi:fan",
    HVACMode.DRY: "mdi:water-percent",
}

# Map heat preset modes (heat source types) to icons
PRESET_MODE_ICONS = {
    "Heat Pump": "mdi:heat-pump",
    "Gas Furnace": "mdi:gas-burner",
    "Furnace": "mdi:gas-burner",
    "Heat Strip": "mdi:radiator",
    "Electric Heat": "mdi:heat-wave",
}
