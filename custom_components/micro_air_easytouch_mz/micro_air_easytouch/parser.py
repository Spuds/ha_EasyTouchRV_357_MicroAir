# Standard library imports for basic functionality
from __future__ import annotations
from functools import wraps
import logging
import asyncio
import time
import json
import base64

# Bluetooth-related imports for device communication
from bleak import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
    retry_bluetooth_connection_error,
)
from bluetooth_data_tools import short_address
from bluetooth_sensor_state_data import BluetoothData
from home_assistant_bluetooth import BluetoothServiceInfo
from sensor_state_data.enum import StrEnum

# Local imports for constants and domain-specific functionality
from ..const import DOMAIN
from .const import UUIDS, FAN_MODES_FULL, FAN_MODES_FAN_ONLY

_LOGGER = logging.getLogger(__name__)


def retry_authentication(retries=3, delay=1):
    """Custom retry decorator for authentication attempts."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(retries):
                try:
                    result = await func(*args, **kwargs)
                    if result:
                        return True
                    if attempt < retries - 1:
                        await asyncio.sleep(delay)
                        continue
                except Exception as e:
                    last_exception = e
                    if attempt < retries - 1:
                        await asyncio.sleep(delay)
                        continue
            if last_exception:
                _LOGGER.error(
                    "Authentication failed after %d attempts: %s",
                    retries,
                    str(last_exception),
                )
            else:
                _LOGGER.error(
                    "Authentication failed after %d attempts with no exception", retries
                )
            return False

        return wrapper

    return decorator


def _format_payload_for_log(payload: bytes) -> tuple[str, str]:
    """Return a short printable preview and a base64-encoded full payload.

    Preferentially extract the `Z_sts` section if the payload is valid JSON so
    the preview focuses on the zone status values instead of raw escaped
    whitespace characters like '\n' and '\t'. Fall back to a generic repr
    preview when parsing fails or Z_sts is not present.
    """
    try:
        if isinstance(payload, (bytes, bytearray)):
            data_bytes = bytes(payload)
        else:
            # fallback: convert string to bytes
            data_bytes = str(payload).encode("utf-8", errors="replace")

        # Compute safe base64 full dump
        full_b64 = base64.b64encode(data_bytes).decode("ascii")

        # Try to parse JSON and extract Z_sts for a concise preview
        try:
            decoded = data_bytes.decode("utf-8", errors="replace")
            parsed = json.loads(decoded)
            if isinstance(parsed, dict) and "Z_sts" in parsed:
                zsts = parsed["Z_sts"]
                prm = parsed.get("PRM")
                ci = parsed.get("CI")
                ha = parsed.get("hA") if "hA" in parsed else parsed.get("HA")
                # Make a compact JSON preview of Z_sts and selected metadata (PRM/CI/hA) when present
                preview_obj = {"Z_sts": zsts}
                if "PRM" in parsed:
                    preview_obj["PRM"] = prm
                if ci is not None:
                    preview_obj["CI"] = ci
                if ha is not None:
                    preview_obj["hA"] = ha
                z_preview = json.dumps(
                    preview_obj, separators=(",", ":"), ensure_ascii=False
                )
                preview = z_preview[:250]
                return preview, full_b64
        except Exception:
            # JSON parse failed or Z_sts absent; fall back
            pass

        # Fallback: use repr of decoded text so non-printable characters are visible
        try:
            text = data_bytes.decode("utf-8", errors="replace")
        except Exception:
            text = repr(data_bytes)
        preview = repr(text)[:200]
        return preview, full_b64
    except Exception:
        return ("", "")


class MicroAirEasyTouchSensor(StrEnum):
    """Enumeration of all available sensors for the MicroAir EasyTouch device."""

    FACE_PLATE_TEMPERATURE = "face_plate_temperature"
    CURRENT_MODE = "current_mode"
    MODE = "mode"
    FAN_MODE = "fan_mode"
    AUTO_HEAT_SP = "autoHeat_sp"
    AUTO_COOL_SP = "autoCool_sp"
    COOL_SP = "cool_sp"
    HEAT_SP = "heat_sp"
    DRY_SP = "dry_sp"


class MicroAirEasyTouchBluetoothDeviceData(BluetoothData):
    """Main class for handling MicroAir EasyTouch device data and communication."""

    def __init__(self, password: str | None = None, email: str | None = None) -> None:
        """Initialize the device data handler with optional credentials."""
        super().__init__()
        self._password = password
        self._email = email
        self._client = None
        self._ble_device = None
        self._max_delay = 6.0
        self._notification_task = None

        # Latest parsed device state (populated by `decrypt`)
        self._device_state: dict = {}

        # Zone configuration data (MAV bitmasks, FA arrays, SPL limits)
        # Structure: {'zone_configs': {0: {'MAV': 1023, 'FA': [...], 'SPL': [...]}, 1: {...}}}
        self._zone_configs: dict = {}

        # Subscribers to device update events. Each subscriber is a callable
        # that takes no arguments and is invoked when device state changes.
        self._update_listeners: list[callable] = []

        # Store BLE device object for persistence across operations
        self._stored_ble_device: BLEDevice | None = None
        self._stored_address: str | None = None

        # Synchronization primitives for multi-zone safety
        self._client_lock = (
            asyncio.Lock()
        )  # Prevents concurrent connection modifications
        self._command_queue = asyncio.Queue()  # FIFO command execution
        self._queue_worker_task = None  # Manages queue processing
        self._connected = False  # Tracks persistent connection state
        self._connection_health_check_task = None  # Monitors connection health
        self._last_activity_time = 0.0  # Track last successful operation
        self._connection_idle_timeout = (
            120.0  # Disconnect after 2 minutes of inactivity
        )
        self._health_check_interval = 60.0  # Check connection health every 60 seconds

        # Polling is enabled by default because device does not advertise full state
        self._polling_enabled: bool = True
        self._poll_interval: float = 30.0  # seconds
        self._poll_task: asyncio.Task | None = None
        self._last_poll_success: bool = False
        self._last_poll_time: float | None = None
        self._poll_in_progress = False  # Prevent poll/command conflicts

    def _get_operation_delay(self, hass, address: str, operation: str) -> float:
        """Calculate delay for specific operations from persistent storage."""
        device_delays = (
            hass.data.setdefault(DOMAIN, {})
            .setdefault("device_delays", {})
            .get(address, {})
        )
        return device_delays.get(operation, {}).get("delay", 0.0)

    def _increase_operation_delay(self, hass, address: str, operation: str) -> float:
        """Increase delay for specific operation and device with persistence."""
        delays = hass.data.setdefault(DOMAIN, {}).setdefault("device_delays", {})
        if address not in delays:
            delays[address] = {}
        if operation not in delays[address]:
            delays[address][operation] = {"delay": 0.0, "failures": 0}
        current = delays[address][operation]
        current["failures"] += 1
        current["delay"] = min(
            0.5 * (2 ** min(current["failures"], 3)), self._max_delay
        )
        _LOGGER.debug(
            "Increased delay for %s:%s to %.1fs (failures: %d)",
            address,
            operation,
            current["delay"],
            current["failures"],
        )
        return current["delay"]

    def _adjust_operation_delay(self, hass, address: str, operation: str) -> None:
        """Adjust delay for specific operation after success, reducing gradually."""
        delays = hass.data.setdefault(DOMAIN, {}).setdefault("device_delays", {})
        if address in delays and operation in delays[address]:
            current = delays[address][operation]
            if current["failures"] > 0:
                current["failures"] = max(0, current["failures"] - 1)
                current["delay"] = max(0.0, current["delay"] * 0.75)
                _LOGGER.debug(
                    "Adjusted delay for %s:%s to %.1fs (failures: %d)",
                    address,
                    operation,
                    current["delay"],
                    current["failures"],
                )
            if current["failures"] == 0 and current["delay"] < 0.1:
                current["delay"] = 0.0
                _LOGGER.debug("Reset delay for %s:%s to 0.0s", address, operation)

    def _start_update(self, service_info: BluetoothServiceInfo) -> None:
        """Update from BLE advertisement data and notify listeners."""
        _LOGGER.debug(
            "Parsing MicroAirEasyTouch BLE advertisement data: %s", service_info
        )
        self.set_device_manufacturer("MicroAirEasyTouch")
        self.set_device_type("Thermostat")
        name = f"{service_info.name} {short_address(service_info.address)}"
        self.set_device_name(name)
        self.set_title(name)

        # Notify any subscribers that new data is available (advertisement-driven)
        self._notify_update()

    def async_subscribe_updates(self, callback: callable) -> callable:
        """Subscribe to device update notifications.

        Returns an unsubscribe callable that removes the callback when invoked.
        """
        self._update_listeners.append(callback)

        def _unsubscribe() -> None:
            try:
                self._update_listeners.remove(callback)
            except ValueError:
                pass

        return _unsubscribe

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Store BLE device object for persistent use."""
        self._stored_ble_device = ble_device
        self._stored_address = ble_device.address if ble_device else None
        self._ble_device = ble_device  # Keep existing reference too
        _LOGGER.debug("Stored BLE device %s for persistent use", self._stored_address)

    def set_device_address(self, address: str) -> None:
        """Store device address for creating minimal BLE devices when needed."""
        self._stored_address = address
        _LOGGER.debug("Stored device address %s for minimal device creation", address)

    def get_ble_device(self, hass) -> BLEDevice | None:
        """Get stored BLE device or try to resolve it."""
        # First try stored device
        if self._stored_ble_device:
            return self._stored_ble_device

        # Try current device reference
        if self._ble_device:
            return self._ble_device

        # Try to resolve from stored address
        if self._stored_address:
            from homeassistant.components.bluetooth import async_ble_device_from_address

            try:
                device = async_ble_device_from_address(hass, self._stored_address)
                if device:
                    self._stored_ble_device = device
                    self._ble_device = device
                    return device
            except Exception:
                pass

            # If Home Assistant can't find the device, create a minimal one for connection attempts
            # This allows us to try to wake up devices that have gone into low-power mode
            try:
                from bleak import BLEDevice

                minimal_device = BLEDevice(
                    address=self._stored_address,
                    name="EasyTouch",  # Generic name
                    details={},
                    rssi=-60,  # Reasonable default
                )
                _LOGGER.debug(
                    "Created minimal BLE device for %s (device may be in low-power mode)",
                    self._stored_address,
                )
                return minimal_device
            except Exception as e:
                _LOGGER.debug("Failed to create minimal BLE device: %s", str(e))

        return None

    def _notify_update(self) -> None:
        """Invoke all registered update listeners and provide the latest state.

        Also support both zero-argument callbacks and single-argument callbacks
        that accept the full device state.
        """
        for callback in list(self._update_listeners):
            try:
                # Prefer calling with the current device state so subscribers that
                # expect the state can receive it directly.
                callback(self._device_state)
            except TypeError:
                # Callback likely expects no arguments; fall back to calling without arguments.
                try:
                    callback()
                except Exception as e:
                    _LOGGER.debug(
                        "Error in update listener (no-arg fallback): %s", str(e)
                    )
            except Exception as e:
                _LOGGER.debug("Error in update listener: %s", str(e))

    def async_get_device_data(self) -> dict:
        """Return the last parsed device state."""
        return self._device_state

    def decrypt(self, data: bytes) -> dict:
        """Parse and decode the device status data."""
        try:
            status = json.loads(data)
        except json.JSONDecodeError as e:
            _LOGGER.error("Failed to parse JSON data: %s", str(e))
            return {"available_zones": [0], "zones": {0: {}}}

        if "Z_sts" not in status:
            _LOGGER.error("No zone status data found in device response")
            return {"available_zones": [0], "zones": {0: {}}}

        param = status.get("PRM", [])
        modes = {
            0: "off",
            5: "heat_on",
            4: "heat",
            3: "cool_on",
            2: "cool",
            1: "fan",
            8: "auto",
            10: "auto",
            11: "auto",
        }

        hr_status = {}
        hr_status["SN"] = status.get("SN", "Unknown")
        hr_status["ALL"] = status
        hr_status["PRM"] = param
        hr_status["CI"] = status.get("CI")
        ha_val = status.get("hA") if "hA" in status else status.get("HA")
        hr_status["hA"] = ha_val

        # Detect available zones and process each one
        available_zones = []
        zone_data = {}

        for zone_key in status["Z_sts"].keys():
            try:
                zone_num = int(zone_key)
                info = status["Z_sts"][zone_key]

                # Ensure info has enough elements
                if len(info) < 16:
                    _LOGGER.warning(
                        "Zone %s has incomplete data (%d elements), skipping",
                        zone_num,
                        len(info),
                    )
                    continue

                # Only add to available_zones after validation passes
                available_zones.append(zone_num)

                zone_status = {}
                zone_status["autoHeat_sp"] = info[0]  # Auto mode heat setpoint
                zone_status["autoCool_sp"] = info[1]  # Auto mode cool setpoint
                zone_status["cool_sp"] = info[2]  # Cool mode setpoint
                zone_status["heat_sp"] = info[3]  # Heat mode setpoint
                zone_status["dry_sp"] = info[4]  # Dry/Dehumidify setpoint
                zone_status["fan_mode_num"] = info[6]  # Fan setting in fan-only mode
                zone_status["cool_fan_mode_num"] = info[7]  # Fan setting in cool mode
                zone_status["heat_fan_mode_num"] = info[
                    8
                ]  # Fan setting in heat modes (5=heat pump, 7=heat strip)
                zone_status["auto_fan_mode_num"] = info[9]  # Fan setting in auto mode
                zone_status["mode_num"] = info[10]  # User selected mode
                zone_status["furnace_fan_mode_num"] = info[
                    11
                ]  # Fan setting in furnace heating modes (3=propane, 4=aquahot)
                zone_status["facePlateTemperature"] = info[
                    12
                ]  # Current actual temperature
                zone_status["active_state_num"] = info[
                    15
                ]  # Active state (0=idle, 2=cooling, 4=heating, etc.)

                # Check unit power state from PRM[1] bit 3 (System Power flag)
                if len(param) > 1:
                    flags_register = param[1]
                    system_power_on = (flags_register & 8) > 0  # Bit 3
                    zone_status["off"] = not system_power_on
                    zone_status["on"] = system_power_on

                # Map modes
                if zone_status["mode_num"] in modes:
                    zone_status["mode"] = modes[zone_status["mode_num"]]

                # Map active state to current operating mode
                # Active state indicates what the unit is actually doing
                active_state_map = {0: "off", 2: "cool", 4: "heat"}
                if zone_status["active_state_num"] in active_state_map:
                    zone_status["current_mode"] = active_state_map[
                        zone_status["active_state_num"]
                    ]
                else:
                    # Fallback: use selected mode if active state is unknown
                    zone_status["current_mode"] = zone_status.get("mode", "off")

                # Detect heat source if mode_num indicates heat variants
                if zone_status.get("mode_num") in (4, 5):
                    zone_status["heat_source"] = (
                        "furnace" if zone_status["mode_num"] == 4 else "heat_pump"
                    )

                # Map fan modes based on current mode
                current_mode = zone_status.get("mode", "off")

                # Store the raw fan mode numbers and their string representations
                if current_mode == "fan":
                    fan_num = info[6]
                    zone_status["fan_mode_num"] = fan_num
                    zone_status["fan_mode"] = FAN_MODES_FAN_ONLY.get(fan_num, "off")
                elif current_mode == "cool":
                    fan_num = info[7]
                    zone_status["cool_fan_mode_num"] = fan_num
                    zone_status["cool_fan_mode"] = FAN_MODES_FULL.get(
                        fan_num, "full auto"
                    )
                elif current_mode in ("heat_on", "heat"):
                    # For heat modes, use different fan index based on specific mode
                    if zone_status.get("mode_num") in (3, 4):  # Furnace heat modes
                        fan_num = info[11]
                        zone_status["furnace_fan_mode_num"] = fan_num
                        zone_status["heat_fan_mode"] = FAN_MODES_FULL.get(
                            fan_num, "full auto"
                        )
                    elif zone_status.get("mode_num") in (
                        5,
                        7,
                    ):  # Heat pump (5) or heat strip (7)
                        fan_num = info[8]
                        zone_status["heat_fan_mode_num"] = fan_num
                        zone_status["heat_fan_mode"] = FAN_MODES_FULL.get(
                            fan_num, "full auto"
                        )
                elif current_mode == "auto":
                    fan_num = info[9]
                    zone_status["auto_fan_mode_num"] = fan_num
                    zone_status["auto_fan_mode"] = FAN_MODES_FULL.get(
                        fan_num, "full auto"
                    )

                zone_data[zone_num] = zone_status
            except (ValueError, IndexError, KeyError) as e:
                _LOGGER.error("Error processing zone %s: %s", zone_key, str(e))
                continue

        hr_status["zones"] = zone_data
        hr_status["available_zones"] = sorted(available_zones)

        # Ensure we have at least one zone
        if not available_zones:
            _LOGGER.warning("No valid zones found, creating default zone 0")
            hr_status["available_zones"] = [0]
            hr_status["zones"] = {0: {}}

        # For backward compatibility, if zone 0 exists, copy its data to the root level
        if 0 in zone_data:
            hr_status.update(zone_data[0])

        # Apply parsed state as authoritative and notify subscribers
        try:
            _LOGGER.debug(
                "Applying parsed device state (zones=%d)",
                len(hr_status.get("zones", {})),
            )
            # Preserve zone configuration data when updating state from polls
            preserved_zone_configs = self._device_state.get("zone_configs", {})
            self._device_state = hr_status
            if preserved_zone_configs:
                self._device_state["zone_configs"] = preserved_zone_configs
                _LOGGER.debug(
                    "Preserved zone configurations for %d zones",
                    len(preserved_zone_configs),
                )
            self._notify_update()
        except Exception as e:
            _LOGGER.debug("Failed to notify subscribers of decrypted state: %s", str(e))

        return hr_status

    @retry_bluetooth_connection_error(attempts=7)
    async def _connect_to_device(self, ble_device: BLEDevice):
        """Connect to the device with retries."""
        try:
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                ble_device.address,
                timeout=20.0,
            )
            if not self._client.services:
                await asyncio.sleep(2)
            if not self._client.services:
                _LOGGER.error("No services available after connecting")
                return False
            return self._client
        except Exception as e:
            _LOGGER.error("Connection error: %s", str(e))
            raise

    @retry_authentication(retries=3, delay=2)
    async def authenticate(self, password: str) -> bool:
        """Authenticate with the device using the provided password."""
        try:
            if not self._client or not self._client.is_connected:
                await asyncio.sleep(1)
                if not self._client or not self._client.is_connected:
                    await self._connect_to_device(self._ble_device)
                    await asyncio.sleep(0.5)
                if not self._client or not self._client.is_connected:
                    _LOGGER.error("Client not connected after reconnecting")
                    return False
            if not self._client.services:
                await self._client.discover_services()
                await asyncio.sleep(1)
                if not self._client.services:
                    _LOGGER.error("Services not discovered")
                    return False
            password_bytes = password.encode("utf-8")
            await self._client.write_gatt_char(
                UUIDS["passwordCmd"], password_bytes, response=True
            )
            _LOGGER.debug("Authentication sent successfully")
            return True
        except Exception as e:
            _LOGGER.error("Authentication failed: %s", str(e))
            if self._client and self._client.is_connected:
                await self._client.disconnect()
            self._client = None
            return False

    async def _write_gatt_with_retry(
        self, hass, uuid: str, data: bytes, ble_device: BLEDevice, retries: int = 3
    ) -> bool:
        """Write GATT characteristic with retry and adaptive delay."""
        last_error = None
        for attempt in range(retries):
            try:
                if not self._client or not self._client.is_connected:
                    if not await self._reconnect_and_authenticate(hass, ble_device):
                        return False
                write_delay = self._get_operation_delay(
                    hass, ble_device.address, "write"
                )
                if write_delay > 0:
                    await asyncio.sleep(write_delay)
                await self._client.write_gatt_char(uuid, data, response=True)
                self._adjust_operation_delay(hass, ble_device.address, "write")
                return True
            except BleakError as e:
                last_error = e
                if attempt < retries - 1:
                    delay = self._increase_operation_delay(
                        hass, ble_device.address, "write"
                    )
                    _LOGGER.debug(
                        "GATT write failed, attempt %d/%d. Delay: %.1f",
                        attempt + 1,
                        retries,
                        delay,
                    )
                    continue
        _LOGGER.error(
            "GATT write failed after %d attempts: %s", retries, str(last_error)
        )
        return False

    async def _reconnect_and_authenticate(self, hass, ble_device: BLEDevice) -> bool:
        """Reconnect and re-authenticate with adaptive delays."""
        try:
            connect_delay = self._get_operation_delay(
                hass, ble_device.address, "connect"
            )
            if connect_delay > 0:
                await asyncio.sleep(connect_delay)
            self._client = await self._connect_to_device(ble_device)
            if not self._client or not self._client.is_connected:
                self._increase_operation_delay(hass, ble_device.address, "connect")
                return False
            self._adjust_operation_delay(hass, ble_device.address, "connect")
            auth_delay = self._get_operation_delay(hass, ble_device.address, "auth")
            if auth_delay > 0:
                await asyncio.sleep(auth_delay)
            auth_result = await self.authenticate(self._password)
            if auth_result:
                self._adjust_operation_delay(hass, ble_device.address, "auth")
            else:
                self._increase_operation_delay(hass, ble_device.address, "auth")
            return auth_result
        except Exception as e:
            _LOGGER.error("Reconnection failed: %s", str(e))
            self._increase_operation_delay(hass, ble_device.address, "connect")
            return False

    async def _read_gatt_with_retry(
        self, hass, characteristic, ble_device: BLEDevice, retries: int = 3
    ) -> bytes | None:
        """Read GATT characteristic with retry and operation-specific delay."""
        last_error = None
        for attempt in range(retries):
            try:
                if not self._client or not self._client.is_connected:
                    if not await self._reconnect_and_authenticate(hass, ble_device):
                        return None
                read_delay = self._get_operation_delay(hass, ble_device.address, "read")
                if read_delay > 0:
                    await asyncio.sleep(read_delay)
                result = await self._client.read_gatt_char(characteristic)
                self._adjust_operation_delay(hass, ble_device.address, "read")
                return result
            except BleakError as e:
                last_error = e
                if attempt < retries - 1:
                    delay = self._increase_operation_delay(
                        hass, ble_device.address, "read"
                    )
                    _LOGGER.debug(
                        "GATT read failed, attempt %d/%d. Delay: %.1f",
                        attempt + 1,
                        retries,
                        delay,
                    )
                    continue
        _LOGGER.error(
            "GATT read failed after %d attempts: %s", retries, str(last_error)
        )
        return None

    async def reboot_device(self, hass, ble_device: BLEDevice) -> bool:
        """Reboot the device by sending reset command."""
        try:
            self._ble_device = ble_device
            self._client = await self._connect_to_device(ble_device)
            if not self._client or not self._client.is_connected:
                _LOGGER.error("Failed to connect for reboot")
                return False
            if not await self.authenticate(self._password):
                _LOGGER.error("Failed to authenticate for reboot")
                return False
            write_delay = self._get_operation_delay(hass, ble_device.address, "write")
            if write_delay > 0:
                await asyncio.sleep(write_delay)
            reset_cmd = {"Type": "Change", "Changes": {"zone": 0, "reset": " OK"}}
            cmd_bytes = json.dumps(reset_cmd).encode()
            try:
                await self._client.write_gatt_char(
                    UUIDS["jsonCmd"], cmd_bytes, response=True
                )
                _LOGGER.info("Reboot command sent successfully")
                return True
            except BleakError as e:
                if "Error" in str(e) and "133" in str(e):
                    _LOGGER.info("Device is rebooting as expected")
                    return True
                _LOGGER.error("Failed to send reboot command: %s", str(e))
                self._increase_operation_delay(hass, ble_device.address, "write")
                return False
        except Exception as e:
            _LOGGER.error("Error during reboot: %s", str(e))
            return False
        finally:
            try:
                if self._client and self._client.is_connected:
                    await self._client.disconnect()
            except Exception as e:
                _LOGGER.debug("Error disconnecting after reboot: %s", str(e))
            self._client = None
            self._ble_device = None

    async def get_available_zones(self, hass, ble_device: BLEDevice) -> list[int]:
        """Get available zones by performing a robust GATT probe.

        Use multiple probe attempts with proper timing to ensure zone detection
        works reliably during device setup. Addresses intermittent issues where
        only the first zone is detected on initial installation.
        """
        if ble_device is None:
            ble_device = self._ble_device
            if ble_device is None:
                _LOGGER.warning(
                    "No BLE device available to detect zones; defaulting to [0]"
                )
                return [0]

        _LOGGER.debug(
            "Probing device %s for available zones",
            ble_device.address,
        )

        # Try multiple probe attempts to handle timing variability
        for attempt in range(3):
            client = None
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    ble_device.address,
                    timeout=15.0,
                )
                if not client or not client.is_connected:
                    _LOGGER.warning(
                        "Zone probe attempt %d failed to connect to %s",
                        attempt + 1,
                        ble_device.address,
                    )
                    continue

                # Perform authentication with longer delay for processing
                if self._password:
                    try:
                        password_bytes = self._password.encode("utf-8")
                        await client.write_gatt_char(
                            UUIDS["passwordCmd"], password_bytes, response=True
                        )
                        # Allow more time for authentication to be processed
                        await asyncio.sleep(0.8)
                        _LOGGER.debug(
                            "Zone probe authentication sent (attempt %d)", attempt + 1
                        )
                    except Exception as e:
                        _LOGGER.debug(
                            "Zone probe authentication failed (attempt %d): %s",
                            attempt + 1,
                            str(e),
                        )

                # Try different zone query approaches
                probe_commands = [
                    # First try: Request all zone data (no specific zone)
                    {"Type": "Get Status", "EM": self._email, "TM": int(time.time())},
                    # Second try: Request zone 0 data (traditional approach)
                    {
                        "Type": "Get Status",
                        "Zone": 0,
                        "EM": self._email,
                        "TM": int(time.time()),
                    },
                ]

                for cmd_index, cmd in enumerate(probe_commands):
                    try:
                        _LOGGER.debug(
                            "Zone probe command %d/%d (attempt %d): %s",
                            cmd_index + 1,
                            len(probe_commands),
                            attempt + 1,
                            cmd,
                        )
                        await client.write_gatt_char(
                            UUIDS["jsonCmd"],
                            json.dumps(cmd).encode("utf-8"),
                            response=True,
                        )
                        # Give device more time to respond with complete data
                        await asyncio.sleep(0.5)

                        payload = await client.read_gatt_char(UUIDS["jsonReturn"])
                        if payload:
                            preview, full_b64 = _format_payload_for_log(payload)
                            _LOGGER.debug(
                                "Zone probe response (attempt %d, cmd %d): %s (len=%d)",
                                attempt + 1,
                                cmd_index + 1,
                                preview,
                                len(payload),
                            )

                            decrypted = self.decrypt(payload)
                            zones = decrypted.get("available_zones", [0])
                            _LOGGER.debug(
                                "Zone probe found zones (attempt %d, cmd %d): %s",
                                attempt + 1,
                                cmd_index + 1,
                                zones,
                            )

                            # Fetch configuration data for detected zones
                            if zones and len(zones) > 0:
                                await self._fetch_zone_configurations(client, zones)

                            # If we found multiple zones, we're done
                            if len(zones) > 1:
                                _LOGGER.info(
                                    "Zone probe successful: detected %d zones: %s (attempt %d)",
                                    len(zones),
                                    zones,
                                    attempt + 1,
                                )
                                return zones
                            # If we found at least one zone and this is the last command, use it
                            elif zones and cmd_index == len(probe_commands) - 1:
                                _LOGGER.info(
                                    "Zone probe completed: found %d zone(s): %s (attempt %d)",
                                    len(zones),
                                    zones,
                                    attempt + 1,
                                )
                                return zones

                        else:
                            _LOGGER.debug(
                                "No payload received for command %d (attempt %d)",
                                cmd_index + 1,
                                attempt + 1,
                            )

                    except Exception as e:
                        _LOGGER.debug(
                            "Zone probe command %d failed (attempt %d): %s",
                            cmd_index + 1,
                            attempt + 1,
                            str(e),
                        )
                        continue

            except Exception as e:
                _LOGGER.debug(
                    "Zone probe attempt %d failed for %s: %s",
                    attempt + 1,
                    ble_device.address,
                    str(e),
                )
            finally:
                try:
                    if client and client.is_connected:
                        await client.disconnect()
                except Exception:
                    pass

            # Wait between attempts to let device settle
            if attempt < 2:  # Don't wait after the last attempt
                await asyncio.sleep(1.0)

        # All attempts failed, fallback to single zone
        _LOGGER.warning(
            "All zone detection attempts failed for %s, defaulting to single zone",
            ble_device.address,
        )
        return [0]

    async def _fetch_zone_configurations(self, client, zones: list[int]) -> None:
        """Fetch configuration data (MAV, FA, SPL) for detected zones.

        This retrieves the device capabilities that determine which modes and fan speeds
        are available for each zone, enabling proper UI filtering.
        """
        _LOGGER.debug("Fetching configuration data for zones: %s", zones)

        for zone in zones:
            try:
                # Send Get Config request for this zone
                config_cmd = {"Type": "Get Config", "Zone": zone}
                _LOGGER.debug("Requesting config for zone %d: %s", zone, config_cmd)

                await client.write_gatt_char(
                    UUIDS["jsonCmd"],
                    json.dumps(config_cmd).encode("utf-8"),
                    response=True,
                )
                await asyncio.sleep(0.5)  # Allow device time to prepare response

                payload = await client.read_gatt_char(UUIDS["jsonReturn"])
                if payload:
                    try:
                        response = json.loads(payload.decode("utf-8"))
                        if (
                            response.get("Type") == "Response"
                            and response.get("RT") == "Config"
                        ):
                            cfg_str = response.get("CFG", "{}")
                            cfg_data = (
                                json.loads(cfg_str)
                                if isinstance(cfg_str, str)
                                else cfg_str
                            )

                            # Store configuration data for this zone
                            if "zone_configs" not in self._device_state:
                                self._device_state["zone_configs"] = {}

                            self._device_state["zone_configs"][zone] = {
                                "MAV": cfg_data.get("MAV", 0),  # Mode available bitmask
                                "FA": cfg_data.get(
                                    "FA", [0] * 16
                                ),  # Fan array (16 elements)
                                "SPL": cfg_data.get(
                                    "SPL", [60, 85, 60, 85]
                                ),  # Setpoint limits
                                "MA": cfg_data.get(
                                    "MA", [0] * 16
                                ),  # Mode array (currently unused)
                            }

                            _LOGGER.debug(
                                "Zone %d config: MAV=%d, FA=%s, SPL=%s",
                                zone,
                                cfg_data.get("MAV", 0),
                                cfg_data.get("FA", [])[:4],
                                cfg_data.get("SPL", []),
                            )
                        else:
                            _LOGGER.debug(
                                "Unexpected config response for zone %d: %s",
                                zone,
                                response,
                            )
                    except (json.JSONDecodeError, KeyError) as e:
                        _LOGGER.debug(
                            "Failed to parse config response for zone %d: %s",
                            zone,
                            str(e),
                        )
                else:
                    _LOGGER.debug("No config response received for zone %d", zone)

            except Exception as e:
                _LOGGER.debug("Error fetching config for zone %d: %s", zone, str(e))
                continue

        _LOGGER.debug(
            "Configuration fetch complete. Stored configs for zones: %s",
            list(self._device_state.get("zone_configs", {}).keys()),
        )

    async def _refetch_zone_configurations(
        self, hass, ble_device: BLEDevice, zones: list[int]
    ) -> None:
        """Re-fetch zone configurations during setup to ensure runtime parser has config data.

        This solves the issue where config flow fetches zone configs in a temporary parser,
        but the runtime parser instance needs the same configuration data.
        """
        _LOGGER.info(
            "Re-fetching zone configurations for runtime parser: zones %s", zones
        )

        client = None
        try:
            # Connect and authenticate
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                ble_device.address,
                timeout=15.0,
            )
            if not client or not client.is_connected:
                _LOGGER.warning("Could not connect to re-fetch zone configs")
                return

            if self._password:
                password_bytes = self._password.encode("utf-8")
                await client.write_gatt_char(
                    UUIDS["passwordCmd"], password_bytes, response=True
                )
                await asyncio.sleep(0.5)
                _LOGGER.debug("Re-fetch authentication completed")

            # Fetch config for each detected zone
            await self._fetch_zone_configurations(client, zones)
            _LOGGER.info("Runtime zone configuration re-fetch completed successfully")

        except Exception as e:
            _LOGGER.warning("Error re-fetching zone configurations: %s", str(e))
        finally:
            try:
                if client and client.is_connected:
                    await client.disconnect()
            except Exception:
                pass

    async def _process_command_queue(self, hass, ble_device: BLEDevice) -> None:
        """Process commands from the queue serially to prevent device conflicts.

        This worker ensures all commands are executed in FIFO order with proper
        connection management and error handling.
        """
        _LOGGER.debug("Command queue worker started")
        try:
            while True:
                try:
                    # Wait for next command with timeout
                    command_item = await asyncio.wait_for(
                        self._command_queue.get(), timeout=60.0
                    )

                    # Get the best available BLE device
                    current_ble_device = self.get_ble_device(hass)
                    if not current_ble_device:
                        # Try the originally provided device as fallback
                        current_ble_device = ble_device

                    if not current_ble_device:
                        _LOGGER.error("No BLE device available for command execution")
                        if not command_item["result_future"].done():
                            command_item["result_future"].set_result(False)
                        self._command_queue.task_done()
                        continue

                    # Execute command with connection management
                    result = await self._execute_command_safely(
                        hass, current_ble_device, command_item["command"]
                    )

                    # Return result to caller
                    if not command_item["result_future"].done():
                        command_item["result_future"].set_result(result)

                    # Mark queue task as done
                    self._command_queue.task_done()

                    # Small delay between commands to prevent overwhelming device
                    await asyncio.sleep(0.1)

                except asyncio.TimeoutError:
                    # No commands for 60 seconds, check if we should keep connection alive
                    if (
                        time.time() - self._last_activity_time
                        > self._connection_idle_timeout
                    ):
                        await self._disconnect_safely()
                    continue
                except Exception as e:
                    _LOGGER.error("Error in command queue worker: %s", str(e))
                    # Try to set error on pending command if available
                    try:
                        if not command_item["result_future"].done():
                            command_item["result_future"].set_result(False)
                        self._command_queue.task_done()
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)  # Brief recovery delay
        except asyncio.CancelledError:
            _LOGGER.debug("Command queue worker cancelled")
            # Clean up any remaining commands
            while not self._command_queue.empty():
                try:
                    item = self._command_queue.get_nowait()
                    if not item["result_future"].done():
                        item["result_future"].set_result(False)
                    self._command_queue.task_done()
                except Exception:
                    break
            raise
        finally:
            await self._disconnect_safely()

    async def _execute_command_safely(
        self, hass, ble_device: BLEDevice, command: dict
    ) -> bool:
        """Execute a single command with proper connection and error handling.

        Automatically reads status response after commands to provide immediate
        UI feedback while maintaining thread-safe execution.
        """
        async with self._client_lock:
            try:
                # Ensure we have a valid connection
                if not await self._ensure_connected(hass, ble_device):
                    return False

                # Send command
                command_bytes = json.dumps(command).encode()
                _LOGGER.debug("Sending command: %s", command)
                if not await self._write_gatt_with_retry(
                    hass, UUIDS["jsonCmd"], command_bytes, ble_device
                ):
                    _LOGGER.warning("Failed to write command to device: %s", command)
                    return False

                # For change commands, immediately read response to provide instant UI feedback
                if command.get("Type") == "Change":
                    try:
                        # Longer delay to let device fully process the command
                        await asyncio.sleep(0.3)

                        # Send a specific status request for the zone that was changed
                        zone = command.get("Changes", {}).get("zone", 0)
                        status_cmd = {
                            "Type": "Get Status",
                            "Zone": zone,
                            "EM": self._email,
                            "TM": int(time.time()),
                        }

                        status_cmd_bytes = json.dumps(status_cmd).encode()
                        if await self._write_gatt_with_retry(
                            hass, UUIDS["jsonCmd"], status_cmd_bytes, ble_device
                        ):
                            # Small delay before reading response
                            await asyncio.sleep(0.1)

                            # Read the response and update state immediately
                            json_payload = await self._read_gatt_with_retry(
                                hass, UUIDS["jsonReturn"], ble_device
                            )
                            if json_payload:
                                preview, full_b64 = _format_payload_for_log(
                                    json_payload
                                )
                                _LOGGER.debug(
                                    "Command verification response for zone %d: %s (len=%d)",
                                    zone,
                                    preview,
                                    len(json_payload),
                                )
                                _LOGGER.debug(
                                    "Command verification response (base64): %s",
                                    full_b64,
                                )

                                # Apply immediate state update for responsive UI
                                self.decrypt(json_payload)
                                _LOGGER.debug(
                                    "Applied immediate status verification after command for zone %d",
                                    zone,
                                )
                            else:
                                _LOGGER.warning(
                                    "No response payload after command verification for zone %d",
                                    zone,
                                )
                        else:
                            _LOGGER.debug(
                                "Failed to send status verification command for zone %d",
                                zone,
                            )
                    except Exception as e:
                        _LOGGER.warning(
                            "Error reading command verification (will rely on polling): %s",
                            str(e),
                        )

                # Update activity timestamp
                self._last_activity_time = time.time()
                return True

            except Exception as e:
                _LOGGER.error("Failed to execute command safely: %s", str(e))
                # Force disconnect on errors to ensure clean state
                await self._disconnect_safely()
                return False

    async def _ensure_connected(self, hass, ble_device: BLEDevice) -> bool:
        """Ensure we have a valid, authenticated connection."""
        try:
            # Check if current connection is valid
            if self._client and self._client.is_connected:
                # Verify connection is actually working
                if hasattr(self._client, "services") and self._client.services:
                    return True
                else:
                    _LOGGER.debug("Connection has no services, reconnecting")
                    await self._disconnect_safely()

            # Need to establish new connection
            _LOGGER.debug("Establishing new persistent connection")

            connect_delay = self._get_operation_delay(
                hass, ble_device.address, "connect"
            )
            if connect_delay > 0:
                await asyncio.sleep(connect_delay)

            self._client = await self._connect_to_device(ble_device)
            if not self._client or not self._client.is_connected:
                self._increase_operation_delay(hass, ble_device.address, "connect")
                return False

            # Authenticate
            auth_delay = self._get_operation_delay(hass, ble_device.address, "auth")
            if auth_delay > 0:
                await asyncio.sleep(auth_delay)

            if not await self.authenticate(self._password):
                self._increase_operation_delay(hass, ble_device.address, "auth")
                await self._disconnect_safely()
                return False

            # Success - reset delays and mark as connected
            self._adjust_operation_delay(hass, ble_device.address, "connect")
            self._adjust_operation_delay(hass, ble_device.address, "auth")
            self._connected = True
            self._last_activity_time = time.time()

            # Start health monitoring
            await self._start_connection_health_monitor(hass, ble_device)

            _LOGGER.debug("Persistent connection established successfully")
            return True

        except Exception as e:
            _LOGGER.error("Failed to ensure connection: %s", str(e))
            await self._disconnect_safely()
            return False

    async def _resolve_ble_device_with_retry(
        self, hass, address: str, retries: int = 3
    ) -> BLEDevice | None:
        """Resolve BLE device with retry logic for devices in low-power mode."""
        from homeassistant.components.bluetooth import async_ble_device_from_address

        for attempt in range(retries):
            try:
                ble_device = async_ble_device_from_address(hass, address)
                if ble_device:
                    return ble_device

                # Device not found, might be in low-power mode
                if attempt < retries - 1:
                    wait_time = 2.0**attempt  # Exponential backoff: 1s, 2s, 4s
                    await asyncio.sleep(wait_time)

            except Exception:
                if attempt < retries - 1:
                    await asyncio.sleep(1.0)

        _LOGGER.warning(
            "Failed to resolve BLE device %s after %d attempts - device may be in low-power mode",
            address,
            retries,
        )
        return None

    async def _disconnect_safely(self) -> None:
        """Safely disconnect and clean up connection state."""
        try:
            if self._client and self._client.is_connected:
                await self._client.disconnect()
        except Exception as e:
            _LOGGER.debug("Error during safe disconnect: %s", str(e))
        finally:
            self._client = None
            self._connected = False

    async def _start_connection_health_monitor(
        self, hass, ble_device: BLEDevice
    ) -> None:
        """Start background health monitoring for the persistent connection."""
        if (
            self._connection_health_check_task
            and not self._connection_health_check_task.done()
        ):
            return  # Already running

        async def _health_check_loop():
            while True:
                try:
                    await asyncio.sleep(self._health_check_interval)

                    # Check if connection has been idle too long
                    time_since_activity = time.time() - self._last_activity_time
                    if time_since_activity > self._connection_idle_timeout:
                        _LOGGER.debug(
                            "Connection idle for %.1fs, disconnecting to save resources",
                            time_since_activity,
                        )
                        await self._disconnect_safely()
                        continue

                    # If we have an active connection, verify it's still working
                    if self._client and self._client.is_connected:
                        try:
                            # Simple health check - verify services are still available
                            if not self._client.services:
                                _LOGGER.debug(
                                    "Connection health check failed - no services, reconnecting"
                                )
                                await self._disconnect_safely()
                        except Exception as e:
                            _LOGGER.debug(
                                "Connection health check failed: %s, reconnecting",
                                str(e),
                            )
                            await self._disconnect_safely()

                except asyncio.CancelledError:
                    _LOGGER.debug("Connection health monitor cancelled")
                    break
                except Exception as e:
                    _LOGGER.debug("Error in connection health monitor: %s", str(e))
                    await asyncio.sleep(30)  # Wait before retrying

        self._connection_health_check_task = asyncio.create_task(_health_check_loop())

    async def async_shutdown(self) -> None:
        """Clean shutdown of all tasks and connections."""
        _LOGGER.debug("Shutting down MicroAirEasyTouch device data")

        # Stop polling
        await self.stop_polling()

        # Stop queue worker
        if self._queue_worker_task and not self._queue_worker_task.done():
            self._queue_worker_task.cancel()
            try:
                await self._queue_worker_task
            except asyncio.CancelledError:
                pass

        # Stop health check task
        if (
            self._connection_health_check_task
            and not self._connection_health_check_task.done()
        ):
            self._connection_health_check_task.cancel()
            try:
                await self._connection_health_check_task
            except asyncio.CancelledError:
                pass

        # Clean disconnect
        await self._disconnect_safely()

        _LOGGER.debug("Device shutdown complete")

    def start_polling(
        self, hass, startup_delay: float = 1.0, address: str | None = None
    ) -> None:
        """Start background polling loop (non-blocking) with a configurable startup delay.

        If `address` is provided, the poll loop will attempt to resolve the BLE
        device from the bluetooth integration if no advertisement has been seen.
        The tiny delay (default 1s) lets Home Assistant finish platform setup before we
        start potentially slow GATT connect/read operations. Tests can pass a
        `startup_delay=0` to run immediately.
        """
        if not self._polling_enabled:
            _LOGGER.debug("Polling disabled for device")
            return
        if self._poll_task and not self._poll_task.done():
            _LOGGER.debug("Polling already running")
            return

        # Store the address to allow the poll loop to resolve the BLE device
        if address:
            self._address = address

        async def _starter():
            # Give the system a moment to finish setup to avoid blocking time-sensitive startup
            if startup_delay and startup_delay > 0:
                await asyncio.sleep(startup_delay)
            await self._poll_loop(hass)

        _LOGGER.info(
            "Scheduling device poll loop (interval: %.1fs) to start after %.1fs delay",
            self._poll_interval,
            startup_delay,
        )
        self._poll_task = asyncio.create_task(_starter())

    async def stop_polling(self) -> None:
        """Stop the background polling task if running."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                _LOGGER.debug("Polling task cancelled cleanly")
        self._poll_task = None

    async def _poll_loop(self, hass) -> None:
        """Continuously poll the device for full status and update internal state.

        This polling loop respects the command queue to prevent conflicts and
        uses the persistent connection when available.
        """
        _LOGGER.debug("Poll loop running")
        try:
            while True:
                try:
                    # Skip this poll iteration if commands are being processed
                    if not self._command_queue.empty():
                        await asyncio.sleep(
                            self._poll_interval / 4
                        )  # Check again sooner
                        continue

                    # Mark polling in progress to prevent conflicts
                    self._poll_in_progress = True

                    # Get the best available BLE device
                    current_ble_device = self.get_ble_device(hass)
                    if not current_ble_device and getattr(self, "_address", None):
                        # Try to resolve by address as fallback
                        current_ble_device = await self._resolve_ble_device_with_retry(
                            hass, self._address
                        )
                        if current_ble_device:
                            self.set_ble_device(current_ble_device)

                    if not current_ble_device:
                        _LOGGER.debug("No BLE device known, skipping poll iteration")
                        self._last_poll_success = False
                    else:
                        # Use the persistent connection for polling if available
                        async with self._client_lock:
                            try:
                                if await self._ensure_connected(
                                    hass, current_ble_device
                                ):
                                    # Send status request
                                    message = {
                                        "Type": "Get Status",
                                        "Zone": 0,
                                        "EM": self._email,
                                        "TM": int(time.time()),
                                    }
                                    command_bytes = json.dumps(message).encode()

                                    if await self._write_gatt_with_retry(
                                        hass,
                                        UUIDS["jsonCmd"],
                                        command_bytes,
                                        current_ble_device,
                                    ):
                                        # Read response
                                        json_payload = await self._read_gatt_with_retry(
                                            hass,
                                            UUIDS["jsonReturn"],
                                            current_ble_device,
                                        )
                                        if json_payload:
                                            # preview, full_b64 = _format_payload_for_log(json_payload)
                                            # Pass bytes directly to decrypt (it accepts bytes or str)
                                            self.decrypt(json_payload)
                                            self._last_poll_success = True
                                            self._last_poll_time = time.time()
                                            self._last_activity_time = time.time()
                                        else:
                                            self._last_poll_success = False
                                    else:
                                        _LOGGER.debug("Poll send_command failed")
                                        self._last_poll_success = False
                                else:
                                    _LOGGER.debug("Poll failed to establish connection")
                                    self._last_poll_success = False
                            except Exception as e:
                                _LOGGER.debug("Error during poll execution: %s", str(e))
                                self._last_poll_success = False

                except Exception as e:
                    _LOGGER.debug("Error during poll iteration: %s", str(e))
                    self._last_poll_success = False
                finally:
                    self._poll_in_progress = False

                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            _LOGGER.info("Poll loop cancelled")
            raise
        finally:
            self._poll_in_progress = False

    async def send_command(self, hass, ble_device: BLEDevice, command: dict) -> bool:
        """Send command to device using persistent connection and command queue.

        This method ensures thread-safe command execution and maintains a
        persistent connection to prevent device instability from connection thrashing.
        """
        # Store the BLE device for future use
        if ble_device:
            self.set_ble_device(ble_device)

        # Start the queue worker if not already running
        if not self._queue_worker_task or self._queue_worker_task.done():
            self._queue_worker_task = asyncio.create_task(
                self._process_command_queue(hass, ble_device)
            )

        # Queue the command for serialized execution
        result_future = asyncio.Future()
        command_item = {
            "command": command,
            "result_future": result_future,
            "timestamp": time.time(),
        }

        await self._command_queue.put(command_item)

        try:
            # Wait for command to be processed with timeout
            return await asyncio.wait_for(result_future, timeout=30.0)
        except asyncio.TimeoutError:
            _LOGGER.error("Command timeout after 30s: %s", command)
            return False
        except Exception as e:
            _LOGGER.error("Command execution failed: %s", str(e))
            return False

    def get_zone_config(self, zone: int) -> dict:
        """Get configuration data for a specific zone."""
        return self._device_state.get("zone_configs", {}).get(zone, {})

    def is_mode_available(self, zone: int, mode: int) -> bool:
        """Check if a specific mode is available for a zone based on MAV bitmask."""
        config = self.get_zone_config(zone)
        mav = config.get("MAV", 0)
        return (mav & (1 << mode)) > 0

    def get_available_modes(self, zone: int) -> list[int]:
        """Get list of available modes for a zone."""
        config = self.get_zone_config(zone)
        mav = config.get("MAV", 0)
        available_modes = []
        for mode in range(16):
            if (mav & (1 << mode)) > 0:
                available_modes.append(mode)
        return available_modes

    def get_fan_capabilities(self, zone: int, mode: int) -> dict:
        """Get fan speed capabilities for a zone/mode based on FA array.

        Returns dict with: max_speed, fixed_speed, allow_off, allow_manual_auto, allow_full_auto
        """
        config = self.get_zone_config(zone)
        fa_array = config.get("FA", [0] * 16)
        if mode >= len(fa_array):
            return {
                "max_speed": 0,
                "fixed_speed": True,
                "allow_off": False,
                "allow_manual_auto": False,
                "allow_full_auto": False,
            }

        fa_value = fa_array[mode]
        return {
            "max_speed": fa_value & 15,  # Lower 4 bits
            "fixed_speed": (fa_value & 16) > 0,  # Bit 4
            "allow_off": (fa_value & 32) > 0,  # Bit 5
            "allow_manual_auto": (fa_value & 64) > 0,  # Bit 6
            "allow_full_auto": (fa_value & 128) > 0,  # Bit 7
        }

    def get_available_fan_speeds(self, zone: int, mode: int) -> list[int]:
        """Get list of available fan speeds for a zone/mode."""
        capabilities = self.get_fan_capabilities(zone, mode)

        if capabilities["fixed_speed"]:
            # Fixed speed mode - return only the max speed
            return [capabilities["max_speed"]] if capabilities["max_speed"] > 0 else [0]

        speeds = []

        # Add off speed if allowed
        if capabilities["allow_off"]:
            speeds.append(0)

        # Add manual speeds 1 through max_speed
        for speed in range(1, capabilities["max_speed"] + 1):
            speeds.append(speed)

        # Special case: Autonomous furnace mode (FA=32, max_speed=0 but allow_off=True)
        # This represents a furnace where fan is autonomous but UI needs to shows on/off
        if (
            capabilities["max_speed"] == 0
            and capabilities["allow_off"]
            and not capabilities["allow_manual_auto"]
            and not capabilities["allow_full_auto"]
        ):
            speeds.append(128)  # Provide autonomous "auto" state

        # Add auto modes if allowed
        if capabilities["allow_manual_auto"]:
            speeds.append(64)  # Manual auto

        if capabilities["allow_full_auto"]:
            speeds.append(128)  # Full auto

        return speeds
