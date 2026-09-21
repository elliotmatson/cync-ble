"""Cync BLE coordinator — manages mesh clients and device state."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any, Optional

from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothServiceInfoBleak,
    async_register_callback,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    FIRMWARE_QUERY_WINDOW,
    ISSUE_UNKNOWN_DEVICES,
    MAX_COLOR_TEMP,
    MAX_CONCURRENT_CONNECTIONS,
    MIN_COLOR_TEMP,
    POLL_INTERVAL,
    PROBE_INTERVAL,
    PROBE_MISS_THRESHOLD,
    PROBE_QUIET_THRESHOLD,
    STATUS_CACHE_TTL,
)
from .cync_mesh import CyncMeshClient, DeviceStatus, DeviceVersion

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
        self.is_plug: bool = device_dict.get("is_plug", False)
        self.is_fan: bool = device_dict.get("is_fan", False)

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
        # Wall-clock companion to last_seen, for display only. The two are
        # kept separately on purpose: monotonic time is correct for the
        # interval math that drives probing and availability (it can't jump
        # when the system clock is adjusted or NTP steps), but it isn't a
        # real timestamp and can't be shown to a user. This one is only ever
        # rendered, never compared against a threshold.
        self.last_seen_utc: Optional[Any] = None

        # Liveness-probe state (see probe_if_quiet) — a device that's gone
        # quiet under the push-on-change protocol isn't necessarily gone,
        # but PROBE_MISS_THRESHOLD consecutive unanswered probes is real
        # evidence it might be, unlike mere silence.
        self._probe_miss_count: int = 0
        self._last_probe_attempt: Optional[float] = None
        self._confirmed_unreachable: bool = False

        # Firmware version, populated only by an explicit
        # query_firmware_versions run — nothing reports it unsolicited. Both
        # stay None until this device answers one. firmware_version is the
        # best-effort decode and can stay None even after a reply arrives;
        # firmware_version_raw is the hex of the reply's parameter bytes and
        # is the authoritative record, because the reply opcode and payload
        # layout are inferred rather than documented (see
        # cync_mesh.decode_firmware_version). A reply having been received at
        # all is therefore signalled by firmware_version_raw, not by
        # firmware_version.
        self.firmware_version: Optional[str] = None
        self.firmware_version_raw: Optional[str] = None

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
    def percentage(self) -> int:
        """Fan speed, 0-100 — the mesh protocol has no distinct speed opcode,
        it's the same brightness command (0-100) reused, so this is just
        `brightness` on the mesh's native scale instead of HA's 0-255."""
        return round(self._brightness / 255 * 100)

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
        spec §3.6.2), so mere silence from an idle bulb isn't evidence it's
        gone — hence no staleness timeout here. _confirmed_unreachable is a
        stronger signal: PROBE_MISS_THRESHOLD consecutive unanswered direct
        probes (see probe_if_quiet), which push-on-change alone can't give us.

        A mesh connection drop doesn't flip this immediately either —
        recently_disconnected absorbs a blip that self-heals within
        RECONNECT_GRACE_PERIOD, so a brief reconnect doesn't flip every
        device on the mesh unavailable and back for no meaningful reason.
        """
        if self._confirmed_unreachable or self.last_seen is None:
            return False
        return self._mesh_client.is_connected or self._mesh_client.recently_disconnected

    @property
    def unavailable_reason(self) -> Optional[str]:
        """Why this device is unavailable, or None if it isn't.

        is_available folds three genuinely different situations into one
        boolean, and they call for different fixes: a device that has never
        reported is probably misconfigured or out of range, one that failed
        repeated probes is likely powered off, and one whose mesh is down is
        not the device's fault at all. Keeping them apart is most of the
        value of a diagnostic entity.
        """
        if self._confirmed_unreachable:
            return "probe_failed"
        if self.last_seen is None:
            return "never_reported"
        if not (self._mesh_client.is_connected
                or self._mesh_client.recently_disconnected):
            return "mesh_disconnected"
        return None

    @property
    def probe_miss_count(self) -> int:
        return self._probe_miss_count

    @property
    def seconds_since_seen(self) -> Optional[float]:
        if self.last_seen is None:
            return None
        return round(time.monotonic() - self.last_seen, 1)

    def _clear_probe_state(self) -> None:
        self._probe_miss_count = 0
        self._confirmed_unreachable = False

    def mark_seen(self) -> None:
        """Record a liveness confirmation that isn't a full status update —
        i.e. a successful probe reply (see probe_if_quiet)."""
        self.last_seen = time.monotonic()
        self.last_seen_utc = dt_util.utcnow()
        self._clear_probe_state()

    def update_from_version(self, version: Optional[str], raw: str) -> None:
        """Record a firmware version reply (see CyncMeshClient.query_firmware_versions).

        Also marks the device seen: the query is a directed read and
        answering it is liveness evidence exactly like a probe reply is, so a
        device that reports its firmware shouldn't keep accumulating probe
        misses toward _confirmed_unreachable.
        """
        self.firmware_version = version
        self.firmware_version_raw = raw
        self.mark_seen()

    def clear_firmware_version(self) -> None:
        """Forget a previously collected firmware version.

        Called before a fresh query so a device that has since gone silent
        reads as unanswered rather than reporting a stale value from an
        earlier run — "which bulbs stayed silent" is half the signal.
        """
        self.firmware_version = None
        self.firmware_version_raw = None

    async def probe_if_quiet(self) -> None:
        """Send a direct liveness probe (opcode 0xDA) if this device has
        been quiet long enough to warrant one, tracking consecutive misses
        toward PROBE_MISS_THRESHOLD. Called from the coordinator's poll
        cycle — see CyncMeshClient.query_device_status for what "quiet"
        actually means here and why it needs a probe instead of just waiting.
        """
        if not self._mesh_client.is_connected or self.last_seen is None:
            return
        now = time.monotonic()
        if now - self.last_seen < PROBE_QUIET_THRESHOLD:
            return
        if self._last_probe_attempt is not None and now - self._last_probe_attempt < PROBE_INTERVAL:
            return
        self._last_probe_attempt = now
        if await self._mesh_client.query_device_status(self.device_id):
            self.mark_seen()
        else:
            self._probe_miss_count += 1
            if self._probe_miss_count >= PROBE_MISS_THRESHOLD:
                self._confirmed_unreachable = True

    def update_from_status(self, status: DeviceStatus) -> None:
        """Update state from a mesh notification."""
        self.last_seen = time.monotonic()
        self.last_seen_utc = dt_util.utcnow()
        self._clear_probe_state()
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

    async def set_percentage(self, percentage: int) -> bool:
        """percentage: fan speed 0-100 — sent straight through as mesh
        brightness (see the `percentage` property for why no conversion
        is needed)."""
        result = await self._mesh_client.set_brightness(self.device_id, percentage)
        if result:
            self._brightness = _mesh_brightness_to_ha(percentage)
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

    def __init__(
        self,
        hass: HomeAssistant,
        devices_config: list[dict[str, Any]],
        entry_id: Optional[str] = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL),
        )
        self._hass = hass
        # Scopes the unknown-devices repair issue to this entry, so two
        # accounts don't overwrite each other's issue.
        self._entry_id = entry_id

        # Device keys we've already reported as unconfigured. Status
        # notifications arrive continuously, so without this the repair
        # issue would be recreated on every single notification rather than
        # raised once per newly-seen device.
        self._unknown_device_keys: set[str] = set()

        # Memoised system_status() result — see STATUS_CACHE_TTL.
        self._status_cache: Optional[dict[str, Any]] = None
        self._status_cache_at: float = 0.0

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
                version_callback=self._on_device_version,
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
                # Both reconnect paths land here now, so record the recovery
                # edge here rather than in _async_update_data — otherwise a
                # reconnect driven by an advertisement callback leaves
                # _mesh_was_connected stale and the next poll cycle logs a
                # "Lost connection" that already healed.
                self._mesh_was_connected[mesh_name] = True
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
            # This is everything the mesh protocol's status broadcast carries
            # about a device — it has no name/MAC/product fields, only
            # device_id and current brightness/color state, so this is the
            # most we can report without also parsing raw BLE advertisement
            # scan-response data (which does carry ProductUUID and MAC, but
            # isn't captured anywhere today).
            _LOGGER.warning(
                "Status notification for unrecognized device %s — not in your "
                "configured device list: %s. If you paired this device in the "
                "Cync app after setting up the integration, run Reconfigure on "
                "the Cync BLE integration to re-sync your device list.",
                key, status,
            )
            self._async_note_unknown_device(key)
            return
        device.update_from_status(status)
        self._log_device_availability_edge(key, device)
        self.async_update_listeners()

    async def _on_device_version(self, version: DeviceVersion) -> None:
        """Route a firmware version reply to the device that sent it."""
        key = f"{version.mesh_name}/{version.device_id}"
        device = self._devices.get(key)
        if device is None:
            # DEBUG, not WARNING. We just broadcast this query to 0xFFFF, so
            # hearing back from a device that isn't in the configured list is
            # an expected outcome of asking rather than an anomaly — and
            # _on_device_status already warns about unrecognized devices
            # separately, so warning again here would only duplicate it once
            # per query run.
            _LOGGER.debug(
                "Firmware version reply from unconfigured device %s: version=%s raw=%s",
                key, version.version, version.raw,
            )
            return
        device.update_from_version(version.version, version.raw)
        self._log_device_availability_edge(key, device)
        self.async_update_listeners()

    # ------------------------------------------------------------------
    # Repairs — surface unconfigured devices so the user knows to re-sync
    # ------------------------------------------------------------------

    @callback
    def _async_note_unknown_device(self, key: str) -> None:
        """Record an unconfigured device and (re)raise the repair issue.

        The log warning above is easy to miss — someone who pairs a bulb in
        the Cync app has no reason to be reading the HA log, so without a
        repair card the re-sync feature only helps users who already know it
        exists. This puts the discovery where they will actually see it and
        points at the fix.

        Seen keys are tracked in a set so the issue is raised once per newly
        seen device rather than once per notification: the mesh pushes status
        continuously, and re-creating the issue on every one would churn the
        issue registry for no added information.
        """
        if key in self._unknown_device_keys or self._entry_id is None:
            return
        self._unknown_device_keys.add(key)

        ir.async_create_issue(
            self._hass,
            DOMAIN,
            f"{ISSUE_UNKNOWN_DEVICES}_{self._entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_UNKNOWN_DEVICES,
            translation_placeholders={
                "count": str(len(self._unknown_device_keys)),
                "devices": ", ".join(sorted(self._unknown_device_keys)),
            },
        )

    @callback
    def _async_clear_unknown_device_issue(self) -> None:
        """Drop the repair issue.

        Called from async_shutdown, which a successful re-sync triggers via
        the entry reload. That makes the card dismiss itself once the device
        list is actually fixed, rather than leaving a stale warning the user
        has to clear by hand — and if the device is still unconfigured after
        the reload, the next status notification simply raises it again.
        """
        if self._entry_id is None:
            return
        ir.async_delete_issue(
            self._hass, DOMAIN, f"{ISSUE_UNKNOWN_DEVICES}_{self._entry_id}"
        )

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
                # Reconnect off the coordinator's critical path. Awaiting
                # connect() inline meant one all-MACs sweep — up to
                # BLE_TIMEOUT per GATT op, for every MAC in the mesh — could
                # outlast POLL_INTERVAL and stall the whole update cycle. That
                # starved the per-device probes below, and worse, starved the
                # *fast* recovery path: _on_ble_advertisement bails out while
                # is_connecting is True, so every advertisement that arrived
                # during a long sweep was dropped. The mesh could then sit
                # unavailable indefinitely while the slow path kept fighting
                # congestion it was itself creating.
                #
                # _connect_mesh is gated by _connect_sem so this can't flood
                # proxy slots, and the is_connecting guard keeps successive
                # poll cycles from stacking redundant attempts.
                if not client.is_connecting:
                    self.hass.async_create_task(self._connect_mesh(mesh_name, client))
            else:
                if not was_connected:
                    # Log once when connection is (re)established
                    _LOGGER.info("Mesh %s is now connected", mesh_name)
                    self._mesh_was_connected[mesh_name] = True
                # Probe any device on this mesh that's been quiet long
                # enough to warrant one — see CyncBLEDevice.probe_if_quiet.
                # Each call is a cheap no-op unless that device is actually
                # due, so this is fine to check every cycle.
                for key, device in self._devices.items():
                    if key.split("/", 1)[0] == mesh_name:
                        await device.probe_if_quiet()
        self._log_availability_changes()
        return self._devices

    def _log_availability_changes(self) -> None:
        """Debug-log per-device availability edges (see _device_was_available).

        This periodic sweep is what catches the "went unavailable" edge —
        nothing pushes an offline event, it's inferred from the mesh
        connection dropping, so it can only be noticed on a poll cycle. The
        "became available" edge is also logged immediately from
        _on_device_status as soon as a status notification arrives, so it
        doesn't have to wait for the next cycle here.
        """
        for key, device in self._devices.items():
            self._log_device_availability_edge(key, device)

    def _log_device_availability_edge(self, key: str, device: CyncBLEDevice) -> None:
        available = device.is_available
        if available == self._device_was_available.get(key, False):
            return
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

    async def async_query_firmware_versions(
        self, window: float = FIRMWARE_QUERY_WINDOW
    ) -> dict[str, Any]:
        """Ask every connected mesh to report its devices' firmware versions.

        Groundwork for OTA support, not OTA itself: this only reads.

        Prior results are cleared first, so a device that answered a previous
        run but stays silent this time reads as unanswered rather than
        reporting a stale value.

        The returned summary deliberately includes devices that did NOT
        answer. On a congested mesh a broadcast read is lossy, and "which
        bulbs stayed silent" is half the signal — a list of only the
        responders would look like a clean result while quietly hiding the
        devices you most wanted to know about.

        Note that the reply opcode and payload layout this depends on are
        inferred rather than documented (see cync_mesh.decode_firmware_version),
        so `version` may be None while `version_raw` is populated. Every
        reply is also logged at INFO with its raw bytes.
        """
        connected = {
            mesh_name: client
            for mesh_name, client in self._mesh_clients.items()
            if client.is_connected
        }
        skipped = [mn for mn in self._mesh_clients if mn not in connected]

        for device in self._devices.values():
            device.clear_firmware_version()

        # Query all connected meshes concurrently — each one sleeps for the
        # full collection window, so doing them in series would multiply the
        # service call's duration by the number of meshes for no benefit.
        # return_exceptions=True so one mesh failing still lets the rest
        # report; a raised exception is treated the same as a failed send.
        results = await asyncio.gather(
            *(client.query_firmware_versions(window) for client in connected.values()),
            return_exceptions=True,
        )

        queried: list[str] = []
        failed: list[str] = []
        # strict=True: gather() was given exactly connected.values(), so the
        # lengths match by construction. Asserting it means a future refactor
        # that breaks the pairing fails loudly instead of silently dropping
        # the tail of whichever list is longer.
        for mesh_name, result in zip(connected, results, strict=True):
            if isinstance(result, BaseException):
                _LOGGER.warning(
                    "Firmware version query failed on mesh %s: %s", mesh_name, result
                )
                failed.append(mesh_name)
            elif result:
                queried.append(mesh_name)
            else:
                failed.append(mesh_name)

        devices: list[dict[str, Any]] = []
        for key, device in self._devices.items():
            # firmware_version_raw, not firmware_version, is what says a
            # reply arrived — an undecodable payload is still a reply.
            responded = device.firmware_version_raw is not None
            devices.append({
                "key": key,
                "name": device.name,
                "mac": device.mac_address,
                "mesh_name": device.mesh_name,
                "device_id": device.device_id,
                "responded": responded,
                "firmware_version": device.firmware_version,
                "firmware_version_raw": device.firmware_version_raw,
            })
        devices.sort(key=lambda d: (not d["responded"], d["name"]))

        responded_count = sum(1 for d in devices if d["responded"])
        _LOGGER.info(
            "Firmware version query: %d of %d devices answered within %ss "
            "(%d mesh(es) queried, %d failed, %d not connected)",
            responded_count, len(devices), window,
            len(queried), len(failed), len(skipped),
        )

        return {
            "devices": devices,
            "responded": responded_count,
            "total": len(devices),
            "window": window,
            "meshes_queried": queried,
            "meshes_failed": failed,
            "meshes_not_connected": skipped,
        }

    @property
    def entry_id(self) -> Optional[str]:
        return self._entry_id

    @property
    def mesh_count(self) -> int:
        return len(self._mesh_clients)

    @property
    def connected_mesh_count(self) -> int:
        return sum(1 for c in self._mesh_clients.values() if c.is_connected)

    @property
    def unknown_device_keys(self) -> list[str]:
        """Device keys seen on the mesh but absent from the config entry.

        The same set that drives the unknown_devices repair issue — exposed
        so the system-status entity can show the count without the user
        having to notice the repair card.
        """
        return sorted(self._unknown_device_keys)

    def system_status(self) -> dict[str, Any]:
        """Aggregate health snapshot, backing the system-status entities.

        Built here rather than in the entities so there is one definition of
        each metric, and so the per-mesh and per-device breakdowns come from
        the same pass — a summary whose counts disagree with its own detail
        lists is worse than no summary.

        Memoised for STATUS_CACHE_TTL. Each diagnostic entity reads this from
        both its state value and its attributes, so a single listener
        dispatch asked for it 21 times — every one of them inside the same
        synchronous block, and therefore guaranteed to produce identical
        output. See STATUS_CACHE_TTL for why the reuse window is time-based
        rather than invalidation-based.
        """
        now = time.monotonic()
        if self._status_cache is not None and \
                now - self._status_cache_at < STATUS_CACHE_TTL:
            return self._status_cache

        meshes: dict[str, Any] = {
            mesh_name: client.debug_state()
            for mesh_name, client in self._mesh_clients.items()
        }

        # Group unavailable devices by *why*, not just how many. See
        # CyncBLEDevice.unavailable_reason.
        by_reason: dict[str, list[str]] = {}
        quiet: list[dict[str, Any]] = []
        firmware: dict[str, Optional[str]] = {}
        available = 0

        for device in self._devices.values():
            if device.is_available:
                available += 1
            reason = device.unavailable_reason
            if reason is not None:
                by_reason.setdefault(reason, []).append(device.name)

            # A device that is nominally available but has been silent past
            # the probe threshold is the interesting middle case: still
            # believed up, but the push-on-change protocol means silence
            # alone isn't proof either way.
            if device.last_seen is not None:
                age = now - device.last_seen
                if age >= PROBE_QUIET_THRESHOLD:
                    quiet.append({
                        "name": device.name,
                        "seconds_since_seen": round(age, 1),
                        "probe_misses": device.probe_miss_count,
                    })

            if device.firmware_version_raw is not None:
                firmware[device.name] = device.firmware_version

        quiet.sort(key=lambda d: -d["seconds_since_seen"])
        for names in by_reason.values():
            names.sort()

        # Most recent status notification across every device — if this
        # stops advancing while meshes report connected, the GATT session is
        # up but nothing is actually coming through it.
        seen = [d.last_seen_utc for d in self._devices.values() if d.last_seen_utc]
        last_activity = max(seen) if seen else None

        status: dict[str, Any] = {
            "meshes": {
                "total": len(self._mesh_clients),
                "connected": self.connected_mesh_count,
                "connecting": sum(
                    1 for c in self._mesh_clients.values() if c.is_connecting
                ),
                "detail": meshes,
            },
            "devices": {
                "total": len(self._devices),
                "available": available,
                "unavailable": len(self._devices) - available,
                "unavailable_by_reason": {k: len(v) for k, v in by_reason.items()},
                "unavailable_detail": by_reason,
                "quiet": quiet,
            },
            "unknown_devices": {
                "count": len(self._unknown_device_keys),
                "keys": self.unknown_device_keys,
            },
            "firmware": {
                "known": len(firmware),
                "total": len(self._devices),
                "versions": firmware,
            },
            "last_activity": last_activity,
        }
        self._status_cache = status
        self._status_cache_at = now
        return status

    def get_device(self, key: str) -> Optional[CyncBLEDevice]:
        return self._devices.get(key)

    def get_devices(self) -> dict[str, CyncBLEDevice]:
        return self._devices

    async def async_shutdown(self, *_: Any) -> None:
        self._async_clear_unknown_device_issue()
        for cancel in self._cancel_ble_callbacks:
            cancel()
        self._cancel_ble_callbacks.clear()
        for client in self._mesh_clients.values():
            await client.disconnect()
