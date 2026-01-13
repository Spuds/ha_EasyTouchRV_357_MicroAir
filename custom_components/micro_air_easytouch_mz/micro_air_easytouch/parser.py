# Standard library imports for basic functionality
from __future__ import annotations
from functools import wraps
import logging
import asyncio
import time
import json

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

        # Notification/persistent connection flag
        self._notifications_enabled: bool = False
        # Notification support: None=unknown, False=not supported, True=supported
        self._notifications_supported: bool | None = None

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

        # Quick poll (burst) task for accelerated probing when changes are suspected
        self._quick_poll_task: asyncio.Task | None = None

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
        """Invoke all registered update listeners and handle errors."""
        for callback in list(self._update_listeners):
            try:
                callback()
            except Exception as e:
                _LOGGER.debug("Error in update listener: %s", str(e))

    def async_get_device_data(self) -> dict:
        """Return the last parsed device state."""
        return self._device_state

    def decrypt(self, data: bytes | str) -> dict:
        """Parse and decode the device status data with change detection."""
        try:
            # Accept both bytes and str inputs
            if isinstance(data, (bytes, bytearray)):
                data_text = data.decode('utf-8')
            else:
                data_text = str(data)
            status = json.loads(data_text)
        except json.JSONDecodeError as e:
            _LOGGER.error("Failed to parse JSON data: %s", str(e))
            return {'available_zones': [0], 'zones': {0: {}}}
        except Exception as e:
            _LOGGER.error("Unexpected error parsing data: %s", str(e))
            return {'available_zones': [0], 'zones': {0: {}}}

        if 'Z_sts' not in status:
            _LOGGER.error("No zone status data found in device response")
            return {'available_zones': [0], 'zones': {0: {}}}

        param = status.get('PRM', [])
        modes = {0: "off", 5: "heat_on", 4: "heat", 3: "cool_on", 2: "cool", 1: "fan", 11: "auto"}
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
                zone_status['current_mode_num'] = info[15]

                if 7 in param:
                    zone_status['off'] = True
                if 15 in param:
                    zone_status['on'] = True

                # Map modes
                if zone_status['current_mode_num'] in modes:
                    zone_status['current_mode'] = modes[zone_status['current_mode_num']]
                if zone_status['mode_num'] in modes:
                    zone_status['mode'] = modes[zone_status['mode_num']]

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

        # Update internal device state and notify subscribers only on change
        try:
            if hr_status != self._device_state:
                # Simple delta logging for important changes (zones on/off)
                try:
                    old_zones = self._device_state.get('zones', {}) if self._device_state else {}
                    for z, zstate in hr_status.get('zones', {}).items():
                        prev = old_zones.get(z, {})
                        if prev.get('on') != zstate.get('on'):
                            _LOGGER.debug("Zone %s on-state changed: %s -> %s", z, prev.get('on'), zstate.get('on'))
                except Exception:
                    pass

                self._device_state = hr_status
                self._notify_update()
            else:
                _LOGGER.debug("Received state identical to current, not notifying subscribers")
        except Exception:
            _LOGGER.debug("Failed to notify subscribers of decrypted state")

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

    def _on_notification(self, sender, data: bytes | bytearray) -> None:
        """Callback invoked by Bleak when a notification is received."""
        try:
            payload = bytes(data)
            # Process in background so callback returns immediately
            asyncio.create_task(self._process_notification(payload))
        except Exception as e:
            _LOGGER.debug("Error scheduling notification processing: %s", str(e))

    async def _process_notification(self, payload: bytes) -> None:
        """Process a notification payload asynchronously."""
        try:
            _LOGGER.debug("Processing notification payload: %s", payload)
            self.decrypt(payload)
        except Exception as e:
            _LOGGER.debug("Failed to process notification payload: %s", str(e))

    async def start_notifications(self, hass, ble_device: BLEDevice) -> bool:
        """Start GATT notifications on the device's jsonReturn characteristic.

        This will create and hold a persistent connection used to receive push
        updates from the device. If notifications are already started, this is a no-op.
        """
        async with self._client_lock:
            if self._notifications_enabled and self._client and self._client.is_connected:
                _LOGGER.debug("Notifications already enabled for %s", ble_device.address)
                return True
            try:
                self._ble_device = ble_device
                client = await self._connect_to_device(ble_device)
                if not client or not client.is_connected:
                    _LOGGER.error("Failed to connect to enable notifications for %s", ble_device.address)
                    # Mark unsupported to avoid repeated attempts
                    self._notifications_supported = False
                    return False
                self._client = client

                # If we already know notifications are unsupported, skip
                if self._notifications_supported is False:
                    _LOGGER.debug("Notifications previously detected as unsupported for %s, skipping", ble_device.address)
                    return False

                # Try lightweight authentication if credentials present
                if self._password:
                    try:
                        await asyncio.sleep(0.1)
                        await self.authenticate(self._password)
                    except Exception as e:
                        _LOGGER.debug("Notification auth failed: %s", str(e))

                # Check characteristic properties for notify/indicate support
                try:
                    char = self._client.services.get_characteristic(UUIDS['jsonReturn'])
                    props = getattr(char, 'properties', []) or []
                    if not any(p in props for p in ('notify', 'indicate')):
                        _LOGGER.info(
                            "Device %s does not advertise notify/indicate on %s; falling back to quick poll",
                            ble_device.address,
                            UUIDS['jsonReturn'],
                        )
                        self._notifications_supported = False
                        return False
                except Exception as e:
                    _LOGGER.debug("Could not determine characteristic properties: %s", str(e))
                    # Be conservative - mark unsupported
                    self._notifications_supported = False
                    return False

                await self._client.start_notify(UUIDS['jsonReturn'], self._on_notification)
                self._notifications_enabled = True
                self._notifications_supported = True
                _LOGGER.info("Notifications enabled for %s", ble_device.address)

                # Also trigger a quick poll immediately to prime the state
                try:
                    asyncio.create_task(self.request_quick_poll(hass, ble_device, interval=2.0, repeats=3))
                except Exception:
                    pass

                return True
            except Exception as e:
                _LOGGER.error("Failed to start notifications for %s: %s", getattr(ble_device, 'address', str(ble_device)), str(e))
                self._notifications_supported = False
                return False
    async def stop_notifications(self) -> None:
        """Stop notifications and disconnect the persistent client if present."""
        async with self._client_lock:
            try:
                if self._client and self._client.is_connected:
                    try:
                        await self._client.stop_notify(UUIDS['jsonReturn'])
                    except Exception:
                        pass
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                self._client = None
                self._notifications_enabled = False
                _LOGGER.info("Notifications disabled")
            except Exception as e:
                _LOGGER.debug("Error stopping notifications: %s", str(e))

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
                    _LOGGER.debug("Probe raw payload: %s", payload_str)
                    decrypted = self.decrypt(payload_str)
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

    async def request_quick_poll(self, hass, ble_device: BLEDevice, interval: float = 5.0, repeats: int = 12) -> bool:
        """Request a short burst of frequent polls to detect external or phone-driven changes.

        This schedules a background task that will run `repeats` iterations
        at `interval` seconds apart. If a quick poll is already running,
        this is a no-op and returns True.
        """
        if self._quick_poll_task and not self._quick_poll_task.done():
            _LOGGER.debug("Quick poll already in progress")
            return True

        async def _runner():
            _LOGGER.info("Starting quick poll burst: %d x %.1fs", repeats, interval)
            try:
                for i in range(repeats):
                    try:
                        async with self._client_lock:
                            message = {"Type": "Get Status", "Zone": 0, "EM": self._email, "TM": int(time.time())}
                            sent = await self.send_command(hass, ble_device, message)
                            if sent:
                                json_payload = await self._read_gatt_with_retry(hass, UUIDS["jsonReturn"], ble_device)
                                if json_payload:
                                    try:
                                        payload_str = json_payload.decode('utf-8')
                                    except Exception:
                                        payload_str = repr(json_payload)
                                    _LOGGER.debug("Quick poll raw payload: %s", payload_str)
                                    self.decrypt(payload_str)
                                    self._last_poll_success = True
                                    self._last_poll_time = time.time()
                                else:
                                    _LOGGER.debug("Quick poll read returned no payload")
                                    self._last_poll_success = False
                            else:
                                _LOGGER.debug("Quick poll send failed")
                                self._last_poll_success = False
                    except Exception as e:
                        _LOGGER.debug("Error during quick poll iteration: %s", str(e))
                        self._last_poll_success = False
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                _LOGGER.info("Quick poll burst cancelled")
                raise
            finally:
                _LOGGER.info("Quick poll burst finished")

        self._quick_poll_task = asyncio.create_task(_runner())
        return True

    def start_polling(self, hass, startup_delay: float = 1.0) -> None:
        """Start background polling loop (non-blocking) with a configurable startup delay.

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
                    if not self._ble_device:
                        _LOGGER.debug("No BLE device known, skipping poll iteration")
                        self._last_poll_success = False
                    else:
                        message = {"Type": "Get Status", "Zone": 0, "EM": self._email, "TM": int(time.time())}
                        sent = await self.send_command(hass, self._ble_device, message)
                        if sent:
                            json_payload = await self._read_gatt_with_retry(hass, UUIDS["jsonReturn"], self._ble_device)
                            if json_payload:
                                try:
                                    payload_str = json_payload.decode('utf-8')
                                except Exception:
                                    payload_str = repr(json_payload)
                                _LOGGER.debug("Poll raw payload: %s", payload_str)
                                self.decrypt(payload_str)
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

                # If a quick poll is active, don't sleep for the main interval
                if self._quick_poll_task and not self._quick_poll_task.done():
                    _LOGGER.debug("Quick poll active, yielding to quick poll")
                    await asyncio.sleep(0.5)
                else:
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
                if self._client and self._client.is_connected and not getattr(self, "_notifications_enabled", False):
                    await self._client.disconnect()
                    self._client = None
            except Exception as e:
                _LOGGER.debug("Error disconnecting: %s", str(e))