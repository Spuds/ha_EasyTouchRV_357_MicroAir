# Standard library imports for basic functionality
from __future__ import annotations
from functools import wraps
import logging
import asyncio
import time
import json
import base64


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
            data_bytes = str(payload).encode('utf-8', errors='replace')

        # Compute safe base64 full dump
        full_b64 = base64.b64encode(data_bytes).decode('ascii')

        # Try to parse JSON and extract Z_sts for a concise preview
        try:
            decoded = data_bytes.decode('utf-8', errors='replace')
            parsed = json.loads(decoded)
            if isinstance(parsed, dict) and 'Z_sts' in parsed:
                zsts = parsed['Z_sts']
                # Make a compact JSON preview of Z_sts
                z_preview = json.dumps({'Z_sts': zsts}, separators=(',', ':'), ensure_ascii=False)
                preview = z_preview[:200]
                return preview, full_b64
        except Exception:
            # JSON parse failed or Z_sts absent; fall back
            pass

        # Fallback: use repr of decoded text so nonprintables are visible
        try:
            text = data_bytes.decode('utf-8', errors='replace')
        except Exception:
            text = repr(data_bytes)
        preview = repr(text)[:200]
        return preview, full_b64
    except Exception:
        return ('', '')

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

from ..const import DOMAIN
from .const import UUIDS

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
                        _LOGGER.debug("Authentication successful on attempt %d/%d", attempt + 1, retries)
                        return True
                    _LOGGER.debug("Authentication returned False on attempt %d/%d", attempt + 1, retries)
                    if attempt < retries - 1:
                        await asyncio.sleep(delay)
                        continue
                except Exception as e:
                    last_exception = e
                    _LOGGER.debug("Authentication attempt %d/%d failed: %s", attempt + 1, retries, str(e))
                    if attempt < retries - 1:
                        await asyncio.sleep(delay)
                        continue
            if last_exception:
                _LOGGER.error("Authentication failed after %d attempts: %s", retries, str(last_exception))
            else:
                _LOGGER.error("Authentication failed after %d attempts with no exception", retries)
            return False
        return wrapper
    return decorator

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

        # Subscribers to device update events. Each subscriber is a callable
        # that takes no arguments and is invoked when device state changes.
        self._update_listeners: list[callable] = []

        # Synchronization primitives for multi-zone safety
        self._client_lock = asyncio.Lock()      # Prevents concurrent connection modifications
        self._command_queue = asyncio.Queue()   # FIFO command execution
        self._queue_worker_task = None          # Manages queue processing
        self._connected = False                 # Tracks persistent connection state

        # Polling configuration and runtime state
        # Polling is enabled by default because device does not advertise full state
        self._polling_enabled: bool = True
        self._poll_interval: float = 30.0  # seconds
        self._poll_task: asyncio.Task | None = None
        self._last_poll_success: bool = False
        self._last_poll_time: float | None = None

    def _get_operation_delay(self, hass, address: str, operation: str) -> float:
        """Calculate delay for specific operations from persistent storage."""
        device_delays = hass.data.setdefault(DOMAIN, {}).setdefault('device_delays', {}).get(address, {})
        return device_delays.get(operation, {}).get('delay', 0.0)

    def _increase_operation_delay(self, hass, address: str, operation: str) -> float:
        """Increase delay for specific operation and device with persistence."""
        delays = hass.data.setdefault(DOMAIN, {}).setdefault('device_delays', {})
        if address not in delays:
            delays[address] = {}
        if operation not in delays[address]:
            delays[address][operation] = {'delay': 0.0, 'failures': 0}
        current = delays[address][operation]
        current['failures'] += 1
        current['delay'] = min(0.5 * (2 ** min(current['failures'], 3)), self._max_delay)
        _LOGGER.debug("Increased delay for %s:%s to %.1fs (failures: %d)", address, operation, current['delay'], current['failures'])
        return current['delay']

    def _adjust_operation_delay(self, hass, address: str, operation: str) -> None:
        """Adjust delay for specific operation after success, reducing gradually."""
        delays = hass.data.setdefault(DOMAIN, {}).setdefault('device_delays', {})
        if address in delays and operation in delays[address]:
            current = delays[address][operation]
            if current['failures'] > 0:
                current['failures'] = max(0, current['failures'] - 1)
                current['delay'] = max(0.0, current['delay'] * 0.75)
                _LOGGER.debug("Adjusted delay for %s:%s to %.1fs (failures: %d)", address, operation, current['delay'], current['failures'])
            if current['failures'] == 0 and current['delay'] < 0.1:
                current['delay'] = 0.0
                _LOGGER.debug("Reset delay for %s:%s to 0.0s", address, operation)

    def _start_update(self, service_info: BluetoothServiceInfo) -> None:
        """Update from BLE advertisement data and notify listeners."""
        _LOGGER.debug("Parsing MicroAirEasyTouch BLE advertisement data: %s", service_info)
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

    def _notify_update(self) -> None:
        """Invoke all registered update listeners and provide the latest state.

        For backward compatibility support both zero-argument callbacks and
        single-argument callbacks that accept the full device state.
        """
        for callback in list(self._update_listeners):
            try:
                # Prefer calling with the current device state so subscribers that
                # expect the state can receive it directly.
                callback(self._device_state)
            except TypeError:
                # Callback likely expects no arguments; fall back to calling
                # without arguments for backward compatibility.
                try:
                    callback()
                except Exception as e:
                    _LOGGER.debug("Error in update listener (no-arg fallback): %s", str(e))
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
            return {'available_zones': [0], 'zones': {0: {}}}
            
        if 'Z_sts' not in status:
            _LOGGER.error("No zone status data found in device response")
            return {'available_zones': [0], 'zones': {0: {}}}
            
        param = status.get('PRM', [])
        modes = {0: "off", 5: "heat_on", 4: "heat", 3: "cool_on", 2: "cool", 1: "fan", 11: "auto", 10: "auto"}
        fan_modes_full = {0: "off", 1: "manualL", 2: "manualH", 65: "cycledL", 66: "cycledH", 128: "full auto"}
        fan_modes_fan_only = {0: "off", 1: "low", 2: "high"}
        
        hr_status = {}
        hr_status['SN'] = status.get('SN', 'Unknown')
        hr_status['ALL'] = status
        
        # Detect available zones and process each one
        available_zones = []
        zone_data = {}
        
        for zone_key in status['Z_sts'].keys():
            try:
                zone_num = int(zone_key)
                info = status['Z_sts'][zone_key]
                
                # Ensure info has enough elements
                if len(info) < 16:
                    _LOGGER.warning("Zone %s has incomplete data (%d elements), skipping", zone_num, len(info))
                    continue
                
                # Only add to available_zones after validation passes
                available_zones.append(zone_num)
                
                zone_status = {}
                zone_status['autoHeat_sp'] = info[0]
                zone_status['autoCool_sp'] = info[1]
                zone_status['cool_sp'] = info[2]
                zone_status['heat_sp'] = info[3]
                zone_status['dry_sp'] = info[4]
                zone_status['fan_mode_num'] = info[6]  # Fan setting in fan-only mode
                zone_status['cool_fan_mode_num'] = info[7]  # Fan setting in cool mode
                zone_status['auto_fan_mode_num'] = info[9]  # Fan setting in auto mode
                zone_status['mode_num'] = info[10]
                zone_status['heat_fan_mode_num'] = info[11]  # Fan setting in heat mode
                zone_status['facePlateTemperature'] = info[12]
                zone_status['current_mode_num'] = info[15] # Current operating mode when in AUTO mode

                if 7 in param:
                    zone_status['off'] = True
                if 15 in param:
                    zone_status['on'] = True

                # Map modes
                # Only use current_mode_num when non-zero; zero indicates 'no specific current mode'.
                if zone_status['current_mode_num'] in modes and zone_status['current_mode_num'] != 0:
                    zone_status['current_mode'] = modes[zone_status['current_mode_num']]
                if zone_status['mode_num'] in modes:
                    zone_status['mode'] = modes[zone_status['mode_num']]

                # For non-auto modes the device may set current_mode_num to 0.
                # In that case, the chosen 'mode' is also the current operating mode.
                if 'current_mode' not in zone_status or not zone_status.get('current_mode'):
                    zone_status['current_mode'] = zone_status.get('mode')

                # Detect heat source if mode_num indicates heat variants
                if zone_status.get('mode_num') in (4, 5):
                    zone_status['heat_source'] = 'furnace' if zone_status['mode_num'] == 4 else 'heat_pump'

                # Map fan modes based on current mode
                current_mode = zone_status.get('mode', "off")
                
                # Store the raw fan mode numbers and their string representations
                if current_mode == "fan":
                    fan_num = info[6]
                    zone_status['fan_mode_num'] = fan_num
                    zone_status['fan_mode'] = fan_modes_fan_only.get(fan_num, "off")
                elif current_mode == "cool":
                    fan_num = info[7]
                    zone_status['cool_fan_mode_num'] = fan_num
                    zone_status['cool_fan_mode'] = fan_modes_full.get(fan_num, "full auto")
                elif current_mode == "heat":
                    fan_num = info[11]
                    zone_status['heat_fan_mode_num'] = fan_num
                    zone_status['heat_fan_mode'] = fan_modes_full.get(fan_num, "full auto")
                elif current_mode == "auto":
                    fan_num = info[9]
                    zone_status['auto_fan_mode_num'] = fan_num
                    zone_status['auto_fan_mode'] = fan_modes_full.get(fan_num, "full auto")

                zone_data[zone_num] = zone_status
            except (ValueError, IndexError, KeyError) as e:
                _LOGGER.error("Error processing zone %s: %s", zone_key, str(e))
                continue

        hr_status['zones'] = zone_data
        hr_status['available_zones'] = sorted(available_zones)
        
        # Ensure we have at least one zone
        if not available_zones:
            _LOGGER.warning("No valid zones found, creating default zone 0")
            hr_status['available_zones'] = [0]
            hr_status['zones'] = {0: {}}
        
        # For backward compatibility, if zone 0 exists, copy its data to the root level
        if 0 in zone_data:
            hr_status.update(zone_data[0])

        # Compare against previous state and log mode changes per zone for easier debugging
        try:
            prev = self._device_state or {}
            diffs = []
            for z, new_zone in hr_status.get('zones', {}).items():
                try:
                    z = int(z)
                except Exception:
                    pass
                old_zone = (prev.get('zones') or {}).get(z, {})
                old_mode = old_zone.get('mode_num')
                new_mode = new_zone.get('mode_num')
                old_current = old_zone.get('current_mode_num')
                new_current = new_zone.get('current_mode_num')
                if old_mode != new_mode or old_current != new_current:
                    diffs.append({
                        'zone': z,
                        'mode_num': (old_mode, new_mode),
                        'current_mode_num': (old_current, new_current),
                    })
            if diffs:
                _LOGGER.debug("Detected mode/current_mode changes: %s", diffs)

            # Update internal device state and notify subscribers
            self._device_state = hr_status
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
                timeout=20.0
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
            password_bytes = password.encode('utf-8')
            await self._client.write_gatt_char(UUIDS["passwordCmd"], password_bytes, response=True)
            _LOGGER.debug("Authentication sent successfully")
            return True
        except Exception as e:
            _LOGGER.error("Authentication failed: %s", str(e))
            if self._client and self._client.is_connected:
                await self._client.disconnect()
            self._client = None
            return False

    async def _write_gatt_with_retry(self, hass, uuid: str, data: bytes, ble_device: BLEDevice, retries: int = 3) -> bool:
        """Write GATT characteristic with retry and adaptive delay."""
        last_error = None
        for attempt in range(retries):
            try:
                if not self._client or not self._client.is_connected:
                    if not await self._reconnect_and_authenticate(hass, ble_device):
                        return False
                write_delay = self._get_operation_delay(hass, ble_device.address, 'write')
                if write_delay > 0:
                    await asyncio.sleep(write_delay)
                await self._client.write_gatt_char(uuid, data, response=True)
                self._adjust_operation_delay(hass, ble_device.address, 'write')
                return True
            except BleakError as e:
                last_error = e
                if attempt < retries - 1:
                    delay = self._increase_operation_delay(hass, ble_device.address, 'write')
                    _LOGGER.debug("GATT write failed, attempt %d/%d. Delay: %.1f", attempt + 1, retries, delay)
                    continue
        _LOGGER.error("GATT write failed after %d attempts: %s", retries, str(last_error))
        return False

    async def _reconnect_and_authenticate(self, hass, ble_device: BLEDevice) -> bool:
        """Reconnect and re-authenticate with adaptive delays."""
        try:
            connect_delay = self._get_operation_delay(hass, ble_device.address, 'connect')
            if connect_delay > 0:
                await asyncio.sleep(connect_delay)
            self._client = await self._connect_to_device(ble_device)
            if not self._client or not self._client.is_connected:
                self._increase_operation_delay(hass, ble_device.address, 'connect')
                return False
            self._adjust_operation_delay(hass, ble_device.address, 'connect')
            auth_delay = self._get_operation_delay(hass, ble_device.address, 'auth')
            if auth_delay > 0:
                await asyncio.sleep(auth_delay)
            auth_result = await self.authenticate(self._password)
            if auth_result:
                self._adjust_operation_delay(hass, ble_device.address, 'auth')
            else:
                self._increase_operation_delay(hass, ble_device.address, 'auth')
            return auth_result
        except Exception as e:
            _LOGGER.error("Reconnection failed: %s", str(e))
            self._increase_operation_delay(hass, ble_device.address, 'connect')
            return False

    async def _read_gatt_with_retry(self, hass, characteristic, ble_device: BLEDevice, retries: int = 3) -> bytes | None:
        """Read GATT characteristic with retry and operation-specific delay."""
        last_error = None
        for attempt in range(retries):
            try:
                if not self._client or not self._client.is_connected:
                    if not await self._reconnect_and_authenticate(hass, ble_device):
                        return None
                read_delay = self._get_operation_delay(hass, ble_device.address, 'read')
                if read_delay > 0:
                    await asyncio.sleep(read_delay)
                result = await self._client.read_gatt_char(characteristic)
                self._adjust_operation_delay(hass, ble_device.address, 'read')
                return result
            except BleakError as e:
                last_error = e
                if attempt < retries - 1:
                    delay = self._increase_operation_delay(hass, ble_device.address, 'read')
                    _LOGGER.debug("GATT read failed, attempt %d/%d. Delay: %.1f", attempt + 1, retries, delay)
                    continue
        _LOGGER.error("GATT read failed after %d attempts: %s", retries, str(last_error))
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
            write_delay = self._get_operation_delay(hass, ble_device.address, 'write')
            if write_delay > 0:
                await asyncio.sleep(write_delay)
            reset_cmd = {"Type": "Change", "Changes": {"zone": 0, "reset": " OK"}}
            cmd_bytes = json.dumps(reset_cmd).encode()
            try:
                await self._client.write_gatt_char(UUIDS["jsonCmd"], cmd_bytes, response=True)
                _LOGGER.info("Reboot command sent successfully")
                return True
            except BleakError as e:
                if "Error" in str(e) and "133" in str(e):
                    _LOGGER.info("Device is rebooting as expected")
                    return True
                _LOGGER.error("Failed to send reboot command: %s", str(e))
                self._increase_operation_delay(hass, ble_device.address, 'write')
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
        """Get available zones by performing a short-lived GATT probe.

        Use a dedicated short-lived connection for probing to avoid contention
        with any persistent connection or ongoing commands. This matches the
        original behavior and keeps zone detection fast and reliable.
        """
        if ble_device is None:
            ble_device = self._ble_device
            if ble_device is None:
                _LOGGER.warning("No BLE device available to detect zones; defaulting to [0]")
                return [0]

        _LOGGER.debug("Probing device %s for available zones (short-lived connection)", ble_device.address)

        client = None
        try:
            client = await establish_connection(BleakClientWithServiceCache, ble_device, ble_device.address, timeout=10.0)
            if not client or not client.is_connected:
                _LOGGER.warning("Short-lived probe failed to connect to %s", ble_device.address)
                return [0]

            # Perform minimal authentication if credentials are available
            if self._password:
                try:
                    password_bytes = self._password.encode('utf-8')
                    await client.write_gatt_char(UUIDS["passwordCmd"], password_bytes, response=True)
                    _LOGGER.debug("Probe authentication sent")
                except Exception as e:
                    _LOGGER.debug("Probe authentication failed: %s", str(e))

            # Send status request and read response
            try:
                cmd = {"Type": "Get Status", "Zone": 0, "EM": self._email, "TM": int(time.time())}
                await client.write_gatt_char(UUIDS["jsonCmd"], json.dumps(cmd).encode('utf-8'), response=True)
                await asyncio.sleep(0.2)
                payload = await client.read_gatt_char(UUIDS["jsonReturn"])
                if payload:
                    try:
                        payload_str = payload.decode('utf-8')
                    except Exception:
                        payload_str = repr(payload)
                    preview, full_b64 = _format_payload_for_log(payload)
                    _LOGGER.debug("Probe raw payload preview: %s (len=%d)", preview, len(payload))
                    _LOGGER.debug("Probe raw payload (base64): %s", full_b64)

                    decrypted = self.decrypt(payload)
                    zones = decrypted.get('available_zones', [0])
                    _LOGGER.info("Probe detected %d zones: %s", len(zones), zones)
                    return zones
            except Exception as e:
                _LOGGER.debug("Probe read failed: %s", str(e))
                return [0]
        except Exception as e:
            _LOGGER.debug("Probe connection failed for %s: %s", ble_device.address, str(e))
            return [0]
        finally:
            try:
                if client and client.is_connected:
                    await client.disconnect()
            except Exception:
                pass

        return [0]

    def start_polling(self, hass, startup_delay: float = 1.0, address: str | None = None) -> None:
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

        _LOGGER.info("Scheduling device poll loop (interval: %.1fs) to start after %.1fs delay", self._poll_interval, startup_delay)
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
        """Continuously poll the device for full status and update internal state."""
        _LOGGER.debug("Poll loop running")
        try:
            while True:
                try:
                    # If we don't have a BLEDevice from advertisements, try to resolve it by stored address
                    if not self._ble_device and getattr(self, "_address", None):
                        try:
                            from homeassistant.components.bluetooth import async_ble_device_from_address
                            self._ble_device = async_ble_device_from_address(hass, self._address)
                            if self._ble_device:
                                _LOGGER.debug("Resolved BLE device %s for polling", self._address)
                        except Exception as e:
                            _LOGGER.debug("Error resolving BLE device from address %s: %s", getattr(self, "_address", None), str(e))

                    if not self._ble_device:
                        _LOGGER.debug("No BLE device known, skipping poll iteration")
                        self._last_poll_success = False
                    else:
                        message = {"Type": "Get Status", "Zone": 0, "EM": self._email, "TM": int(time.time())}
                        sent = await self.send_command(hass, self._ble_device, message)
                        if sent:
                            json_payload = await self._read_gatt_with_retry(hass, UUIDS["jsonReturn"], self._ble_device)
                            if json_payload:
                                preview, full_b64 = _format_payload_for_log(json_payload)
                                _LOGGER.debug("Poll raw payload preview: %s (len=%d)", preview, len(json_payload))
                                _LOGGER.debug("Poll raw payload (base64): %s", full_b64)
                                # Pass bytes directly to decrypt (it accepts bytes or str)
                                self.decrypt(json_payload)
                                self._last_poll_success = True
                                self._last_poll_time = time.time()
                            else:
                                _LOGGER.debug("Poll read returned no payload")
                                self._last_poll_success = False
                        else:
                            _LOGGER.debug("Poll send_command failed")
                            self._last_poll_success = False
                except Exception as e:
                    _LOGGER.debug("Error during poll iteration: %s", str(e))
                    self._last_poll_success = False
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            _LOGGER.info("Poll loop cancelled")
            raise
            
    async def send_command(self, hass, ble_device: BLEDevice, command: dict) -> bool:
        """Send command to device."""
        try:
            if not self._client or not self._client.is_connected:
                self._client = await self._connect_to_device(ble_device)
                if not self._client or not self._client.is_connected:
                    return False
                if not await self.authenticate(self._password):
                    return False
            command_bytes = json.dumps(command).encode()
            return await self._write_gatt_with_retry(hass, UUIDS["jsonCmd"], command_bytes, ble_device)
        except Exception as e:
            _LOGGER.error("Error sending command: %s", str(e))
            return False
        finally:
            try:
                if self._client and self._client.is_connected:
                    await self._client.disconnect()
            except Exception as e:
                _LOGGER.debug("Error disconnecting: %s", str(e))
            self._client = None