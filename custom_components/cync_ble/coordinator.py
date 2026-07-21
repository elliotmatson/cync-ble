"""Cync BLE coordinator — manages mesh clients and device state."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import timedelta
from typing import Any, Optional

from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothServiceInfoBleak,
    async_register_callback,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    POLL_INTERVAL,
    MIN_COLOR_TEMP,
    MAX_COLOR_TEMP,
    MAX_CONCURRENT_CONNECTIONS,
)
from .cync_mesh import CyncMeshClient, DeviceStatus

_LOGGER = logging.getLogger(__name__)


def _kelvin_to_mesh(kelvin: int) -> int:
    """Convert Kelvin (2000–6500) to mesh 0–100 scale."""
    kelvin = max(MIN_COLOR_TEMP, min(MAX_COLOR_TEMP, kelvin))
    return int((kelvin - MIN_COLOR_TEMP) / (MAX_COLOR_TEMP - MIN_COLOR_TEMP) * 100)


def _ha_brightness_to_mesh(brightness: int) -> int:
    """Convert HA brightness (0–255) to mesh scale (0–100)."""
    return int(brightness / 255 * 100)


def _mesh_brightness_to_ha(brightness: int) -> int:
    """Convert mesh brightness (0–100) to HA scale (0–255)."""
    return int(brightness / 100 * 255)


class CyncBLEDevice:
    """State container for a single Cync bulb."""

    def __init__(self, device_dict: dict[str, Any], mesh_client: CyncMeshClient) -> None:
        # Identity — keys match what cync_cloud.get_devices() returns
        self.device_id: int = device_dict["device_id"]
        self.mac: str = device_dict.get("mac", "")
        self.name: str = device_dict.get("name", f"Cync {self.device_id}")
        self.mesh_name: str = device_dict.get("mesh_name", "")   # mesh MAC string
        self.device_type: int = device_dict.get("device_type", 0)
        self.supports_rgb: bool = device_dict.get("supports_rgb", False)
        self.supports_temperature: bool = device_dict.get("supports_temperature", True)

        # Alias for compatibility with light.py
        self.mac_address = self.mac

        self._mesh_client = mesh_client

        # State (HA units)
        self._is_on: bool = False
        self._brightness: int = 255        # HA 0–255
        self._color_temp_k: int = 4000    # Kelvin
        self._rgb: Optional[tuple[int, int, int]] = None

        # Monotonic timestamp of the last status notification actually
        # received for THIS device (as opposed to just the shared mesh GATT
        # connection being up). None until the first one arrives — mirrors
        # cync2mqtt seeding every device "offline" until it reports in.
        self.last_seen: Optional[float] = None

    # ------------------------------------------------------------------
    # State properties (HA units)
    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def brightness(self) -> int:
        return self._brightness

    @property
    def color_temp(self) -> int:
        """Color temperature in Kelvin."""
        return self._color_temp_k

    @property
    def rgb(self) -> Optional[tuple[int, int, int]]:
        return self._rgb

    @property
    def is_connected(self) -> bool:
        return self._mesh_client.is_connected

    @property
    def is_available(self) -> bool:
        """Whether this specific device is believed reachable right now.

        The Telink mesh only pushes a status notification when a device is
        first subscribed to and again when its state actually changes (push
        on change, not a heartbeat — see the Telink BLE Mesh Lighting APP
        spec §3.6.2). An idle bulb that hasn't changed state simply has
        nothing to report, so silence from it is expected and not a sign
        it's gone. So: available once it has reported in at all, for as
        long as the shared mesh connection holds — no staleness timeout.
        """
        return self._mesh_client.is_connected and self.last_seen is not None

    def update_from_status(self, status: DeviceStatus) -> None:
        """Update state from a mesh notification."""
        self.last_seen = time.monotonic()
        # mesh brightness is 0–100; convert to 0–255
        self._brightness = _mesh_brightness_to_ha(status.brightness)
        self._is_on = status.brightness > 0

        if status.is_rgb:
            self._rgb = (status.red, status.green, status.blue)
        else:
            # mesh color_temp is 0–100; convert to Kelvin
            self._color_temp_k = int(
                MIN_COLOR_TEMP
                + status.color_temp / 100 * (MAX_COLOR_TEMP - MIN_COLOR_TEMP)
            )
            self._rgb = None

    # ------------------------------------------------------------------
    # Commands (convert from HA units → mesh units)
    # ------------------------------------------------------------------

    async def turn_on(self) -> bool:
        result = await self._mesh_client.set_power(self.device_id, True)
        if result:
            self._is_on = True
        return result

    async def turn_off(self) -> bool:
        result = await self._mesh_client.set_power(self.device_id, False)
        if result:
            self._is_on = False
        return result

    async def set_brightness(self, brightness: int) -> bool:
        """brightness: HA 0–255"""
        mesh_val = _ha_brightness_to_mesh(brightness)
        result = await self._mesh_client.set_brightness(self.device_id, mesh_val)
        if result:
            self._brightness = brightness
        return result

    async def set_color_temp(self, kelvin: int) -> bool:
        mesh_val = _kelvin_to_mesh(kelvin)
        result = await self._mesh_client.set_color_temp(self.device_id, mesh_val)
        if result:
            self._color_temp_k = kelvin
        return result

    async def set_rgb(self, red: int, green: int, blue: int) -> bool:
        result = await self._mesh_client.set_rgb(self.device_id, red, green, blue)
        if result:
            self._rgb = (red, green, blue)
        return result

    async def disconnect(self) -> None:
        # Mesh client is shared; coordinator handles shutdown
        pass


class CyncBLECoordinator(DataUpdateCoordinator):
    """Coordinator that manages one CyncMeshClient per mesh network."""

    def __init__(self, hass: HomeAssistant, devices_config: list[dict[str, Any]]) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL),
        )
        self._hass = hass

        def _strip_mac(s: str) -> str:
            """Remove colons/dashes from a MAC string for use as a dict key.

            CyncMeshClient also strips colons when it stores mesh_name so all
            keys (mesh_clients, devices, status callbacks) must use the same
            normalized form — otherwise status updates from the mesh client won't
            match the device entries built here.
            """
            return s.replace(":", "").replace("-", "").upper()

        # Group devices by mesh_name so we share one BLE client per mesh.
        # Use the colon-stripped form as the canonical key so it matches what
        # CyncMeshClient.DeviceStatus.mesh_name will report.
        mesh_info: dict[str, dict] = {}  # normalized mesh_name → {access_key, macs}
        for d in devices_config:
            mn = _strip_mac(d.get("mesh_name", ""))
            if not mn:
                continue
            if mn not in mesh_info:
                mesh_info[mn] = {"access_key": d.get("access_key", ""), "macs": []}
            mac = d.get("mac", "")
            if mac:
                mesh_info[mn]["macs"].append(mac)

        # One CyncMeshClient per mesh
        self._mesh_clients: dict[str, CyncMeshClient] = {
            mesh_name: CyncMeshClient(
                hass=hass,
                mesh_name=mesh_name,
                mesh_password=info["access_key"],
                mesh_macs=info["macs"],
                status_callback=self._on_device_status,
            )
            for mesh_name, info in mesh_info.items()
        }

        # Build device map — key: "{normalized_mesh_name}/{device_id}"
        self._devices: dict[str, CyncBLEDevice] = {}
        for d in devices_config:
            mn = _strip_mac(d.get("mesh_name", ""))
            did = d.get("device_id")
            if not mn or did is None or mn not in self._mesh_clients:
                continue
            key = f"{mn}/{did}"
            self._devices[key] = CyncBLEDevice(d, self._mesh_clients[mn])

        # Build reverse map: AA:BB:CC:DD:EE:FF → normalized_mesh_name, for BLE callbacks.
        # mesh_info keys are already stripped (no colons) so _mesh_clients lookups work.
        self._mac_to_mesh: dict[str, str] = {}
        for mesh_name, info in mesh_info.items():
            for mac in info["macs"]:
                raw = mac.strip().upper().replace(":", "").replace("-", "")
                if len(raw) == 12:
                    colon_mac = ":".join(raw[i:i+2] for i in range(0, 12, 2))
                else:
                    colon_mac = raw
                self._mac_to_mesh[colon_mac] = mesh_name  # mesh_name is already stripped

        # Track per-mesh unavailability so we log once on loss, once on recovery
        self._mesh_was_connected: dict[str, bool] = {mn: False for mn in mesh_info}

        # Same idea per-device: is_available depends on is_connected and
        # last_seen rather than firing an explicit event, so
        # _async_update_data diffs against this each cycle to log only the
        # edges (see CyncBLEDevice.is_available).
        self._device_was_available: dict[str, bool] = {key: False for key in self._devices}

        # Semaphore caps simultaneous BLE connection attempts across all meshes.
        # 1 connection per mesh is enough (Telink mesh routing handles the rest);
        # a small cap prevents proxy slot exhaustion during reconnect storms.
        self._connect_sem = asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)

        # Register BLE callbacks — fires the instant any proxy sees a Cync MAC
        self._cancel_ble_callbacks: list = []
        for mac in self._mac_to_mesh:
            cancel = async_register_callback(
                hass,
                self._on_ble_advertisement,
                BluetoothCallbackMatcher(address=mac),
                BluetoothChange.ADVERTISEMENT,
            )
            self._cancel_ble_callbacks.append(cancel)
        _LOGGER.debug("Registered BLE callbacks for %d MACs", len(self._mac_to_mesh))

    # ------------------------------------------------------------------
    # BLE advertisement callback — fires when any proxy sees a Cync MAC
    # ------------------------------------------------------------------

    @callback
    def _on_ble_advertisement(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """Called by HA bluetooth stack when a known Cync MAC is seen advertising."""
        mac = service_info.address.upper()
        mesh_name = self._mac_to_mesh.get(mac)
        if mesh_name is None:
            return
        client = self._mesh_clients.get(mesh_name)
        # client.connect() guards internally against concurrent/redundant calls
        if client is None or client.is_connected or client.is_connecting:
            return
        _LOGGER.debug("BLE proxy saw Cync MAC %s — triggering targeted connect", mac)
        # Pass the specific MAC so we only use ONE connection slot, not all 43
        self.hass.async_create_task(self._connect_mesh(mesh_name, client, preferred_mac=mac))

    async def _connect_mesh(
        self, mesh_name: str, client: CyncMeshClient, preferred_mac: Optional[str] = None
    ) -> None:
        """Attempt mesh connection, gated by the global connection semaphore."""
        try:
            await asyncio.wait_for(self._connect_sem.acquire(), timeout=5)
        except asyncio.TimeoutError:
            _LOGGER.debug("Connection cap reached (semaphore full), skipping mesh %s", mesh_name)
            return
        try:
            connected = await client.connect(preferred_mac=preferred_mac)
            if connected:
                _LOGGER.info("Connected to mesh %s via BLE proxy", mesh_name)
                self.async_update_listeners()
        except Exception as err:
            _LOGGER.debug("Mesh connect attempt failed for %s: %s", mesh_name, err)
        finally:
            self._connect_sem.release()

    # ------------------------------------------------------------------
    # Status callback from mesh notifications
    # ------------------------------------------------------------------

    async def _on_device_status(self, status: DeviceStatus) -> None:
        key = f"{status.mesh_name}/{status.device_id}"
        device = self._devices.get(key)
        if device is None:
            # Seeing this a lot for a device you expect to exist points at a
            # mesh_name/device_id mismatch rather than the bulb not answering.
            _LOGGER.debug("Status notification for unknown device %s — ignoring", key)
            return
        device.update_from_status(status)
        self.async_update_listeners()

    # ------------------------------------------------------------------
    # DataUpdateCoordinator poll (connect if needed; BLE is push-based)
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, CyncBLEDevice]:
        for mesh_name, client in self._mesh_clients.items():
            was_connected = self._mesh_was_connected.get(mesh_name, False)
            if not client.is_connected:
                if was_connected:
                    # Log once when the mesh goes offline
                    _LOGGER.warning(
                        "Lost connection to mesh %s — will reconnect when a device is seen",
                        mesh_name,
                    )
                    self._mesh_was_connected[mesh_name] = False
                try:
                    connected = await client.connect()
                    if connected:
                        _LOGGER.info("Reconnected to mesh %s", mesh_name)
                        self._mesh_was_connected[mesh_name] = True
                except Exception as err:
                    _LOGGER.debug("Could not connect to mesh %s: %s", mesh_name, err)
            else:
                if not was_connected:
                    # Log once when connection is (re)established
                    _LOGGER.info("Mesh %s is now connected", mesh_name)
                    self._mesh_was_connected[mesh_name] = True
        self._log_availability_changes()
        return self._devices

    def _log_availability_changes(self) -> None:
        """Debug-log per-device availability edges (see _device_was_available)."""
        for key, device in self._devices.items():
            available = device.is_available
            if available == self._device_was_available.get(key, False):
                continue
            age = None if device.last_seen is None else time.monotonic() - device.last_seen
            _LOGGER.debug(
                "%s (%s) is now %s (last status %s ago)",
                device.name, key,
                "available" if available else "unavailable",
                "unknown" if age is None else f"{age:.0f}s",
            )
            self._device_was_available[key] = available

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_device(self, key: str) -> Optional[CyncBLEDevice]:
        return self._devices.get(key)

    def get_devices(self) -> dict[str, CyncBLEDevice]:
        return self._devices

    async def async_shutdown(self, *_: Any) -> None:
        for cancel in self._cancel_ble_callbacks:
            cancel()
        self._cancel_ble_callbacks.clear()
        for client in self._mesh_clients.values():
            await client.disconnect()
