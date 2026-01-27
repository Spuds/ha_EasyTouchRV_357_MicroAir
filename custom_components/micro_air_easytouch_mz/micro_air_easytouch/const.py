"""Constants for MicroAirEasyTouch parser"""

from homeassistant.components.climate import HVACMode
from homeassistant.components.climate.const import (
    FAN_OFF,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_HIGH,
    FAN_AUTO,
)

UUIDS = {
    "service": "000000FF-0000-1000-8000-00805F9B34FB",  # ro
    "passwordCmd": "0000DD01-0000-1000-8000-00805F9B34FB",  # rw
    "jsonCmd": "0000EE01-0000-1000-8000-00805F9B34FB",  # rw
    "jsonReturn": "0000FF01-0000-1000-8000-00805F9B34FB",
}

# Map EasyTouch modes to Home Assistant HVAC modes
HA_MODE_TO_EASY_MODE = {
    HVACMode.OFF: 0,
    HVACMode.HEAT: 5,
    HVACMode.COOL: 2,
    HVACMode.AUTO: 8,
    HVACMode.FAN_ONLY: 1,
    HVACMode.DRY: 6,
}

# Reverse mapping for reported codes -> HA modes. Add extra reported-only mappings.
EASY_MODE_TO_HA_MODE = {v: k for k, v in HA_MODE_TO_EASY_MODE.items()}

# Device may report additional heat modes, lets map them to HEAT
EASY_MODE_TO_HA_MODE[3] = HVACMode.HEAT  # Generic heating furnace
EASY_MODE_TO_HA_MODE[4] = HVACMode.HEAT  # Gas/Diesel furnace heating
EASY_MODE_TO_HA_MODE[7] = HVACMode.HEAT  # Electric heating strip
EASY_MODE_TO_HA_MODE[12] = HVACMode.HEAT  # Direct electric heating

# Device may report auto modes, lets map them to AUTO
EASY_MODE_TO_HA_MODE[9] = HVACMode.AUTO  # Auto with heat strip backup
EASY_MODE_TO_HA_MODE[10] = HVACMode.AUTO  # Auto with heat pump backup
EASY_MODE_TO_HA_MODE[11] = HVACMode.AUTO  # Auto with furnace backup

# Fan mode mappings (general and mode-specific)
FAN_MODES_FAN_ONLY = {
    FAN_OFF: 0,
    FAN_LOW: 1,  # manualL
    FAN_HIGH: 2,  # manualH
}

# Map fan modes to possible HA codes
FAN_MODE_REVERSE_MAP = {
    FAN_OFF: [0],
    FAN_LOW: [1, 65], # manualL or cycleL
    FAN_HIGH: [2, 66], # manualH or cycleH
    FAN_MEDIUM: [3],
    FAN_AUTO: [128, 64],
}

# Heat type preset mappings
HEAT_TYPE_PRESETS = {
    "Heat Pump": 5,
    "Furnace": 4,
    "Gas Furnace": 3,
    "Heat Strip": 7,
    "Electric Heat": 12,
}
HEAT_TYPE_REVERSE = {v: k for k, v in HEAT_TYPE_PRESETS.items()}
