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
    HVACMode.AUTO: 8, # Try 8 generic heat/cool .. Use 10 for AUTO pairing with heatpump
    HVACMode.FAN_ONLY: 1,
    HVACMode.DRY: 6,
}

# Reverse mapping for reported codes -> HA modes. Add extra reported-only mappings.
EASY_MODE_TO_HA_MODE = {v: k for k, v in HA_MODE_TO_EASY_MODE.items()}

# Device may report mode 4 for heat (furnace) and 8/11 for auto — map them to HA modes for status
EASY_MODE_TO_HA_MODE[4] = HVACMode.HEAT # furnace
EASY_MODE_TO_HA_MODE[11] = HVACMode.AUTO # auto (AC/Furnace)
EASY_MODE_TO_HA_MODE[10] = HVACMode.AUTO # auto (AC/HeatPump)

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

# Mapping of numeric mode codes to readable internal names (used by parser)
MODE_NUM_TO_NAME = {
    0: "off",
    5: "heat_pump",
    4: "heat",
    3: "cool_on",
    2: "cool",
    1: "fan",
    8: "auto",
    11: "auto",
    6: "dry",
    10: "auto",
}

# Z_sts array indexes (indexes in the device's Z_sts list)
Z_STS_IDX_AUTO_HEAT_SP = 0
Z_STS_IDX_AUTO_COOL_SP = 1
Z_STS_IDX_COOL_SP = 2
Z_STS_IDX_HEAT_SP = 3
Z_STS_IDX_DRY_SP = 4
Z_STS_IDX_UNKNOWN_5 = 5
Z_STS_IDX_FAN_ONLY_MODE = 6
Z_STS_IDX_COOL_FAN = 7
Z_STS_IDX_HEATPUMP_FAN = 8  # observed for heat pump mode (5)
Z_STS_IDX_AUTO_FAN = 9
Z_STS_IDX_MODE = 10
Z_STS_IDX_HEAT_FAN = 11     # observed for furnace mode (4)
Z_STS_IDX_FACEPLATE_TEMP = 12
Z_STS_IDX_UNKNOWN_13 = 13
Z_STS_IDX_UNKNOWN_14 = 14
Z_STS_IDX_CURRENT_MODE = 15