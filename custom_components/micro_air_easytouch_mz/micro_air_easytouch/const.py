"""Constants for MicroAirEasyTouch parser"""
from homeassistant.components.climate import HVACMode

UUIDS = {
    "service":    '000000FF-0000-1000-8000-00805F9B34FB', #ro
    "passwordCmd": '0000DD01-0000-1000-8000-00805F9B34FB', #rw
    "jsonCmd":    '0000EE01-0000-1000-8000-00805F9B34FB', #rw
    "jsonReturn": '0000FF01-0000-1000-8000-00805F9B34FB',
    "unknown":    '00002a05-0000-1000-8000-00805f9b34fb',
}

# Map EasyTouch modes to Home Assistant HVAC modes
HA_MODE_TO_EASY_MODE = {
    HVACMode.OFF: 0,
    HVACMode.HEAT: 5,
    HVACMode.COOL: 2,
    HVACMode.AUTO: 8, # Try 8 generic heat/cool
    HVACMode.FAN_ONLY: 1,
    HVACMode.DRY: 6,
}

# Reverse mapping for reported codes -> HA modes. Add extra reported-only mappings.
EASY_MODE_TO_HA_MODE = {v: k for k, v in HA_MODE_TO_EASY_MODE.items()}

# Device may report mode additonal heat modes and auto modes
EASY_MODE_TO_HA_MODE[3] = HVACMode.HEAT # furnace
EASY_MODE_TO_HA_MODE[4] = HVACMode.HEAT # furnace
EASY_MODE_TO_HA_MODE[7] = HVACMode.HEAT # heat strip 
EASY_MODE_TO_HA_MODE[12] = HVACMode.HEAT # electric heat 

EASY_MODE_TO_HA_MODE[9] = HVACMode.AUTO # auto (AC/Strip)
EASY_MODE_TO_HA_MODE[10] = HVACMode.AUTO # auto (AC/HeatPump)
EASY_MODE_TO_HA_MODE[11] = HVACMode.AUTO # auto (AC/Furnace)

# Fan mode mappings (general and mode-specific)
FAN_MODES_FULL = {
    "off": 0,
    "manualL": 1,
    "manualH": 2,
    "cycledL": 65,
    "cycledH": 66,
    "full auto": 128,
}

FAN_MODES_FAN_ONLY = {
    "off": 0,
    "low": 1,  # manualL
    "high": 2,  # manualH
}

FAN_MODES_REVERSE = {v: k for k, v in FAN_MODES_FULL.items()}