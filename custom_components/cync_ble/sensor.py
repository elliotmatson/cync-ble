"""Sensor platform for Cync BLE — system-status diagnostics.

One service device per config entry ("Cync BLE System") carrying the metrics
worth having when the integration misbehaves. The BLE mesh fails in ways a
per-bulb entity can't express: a mesh session can be nominally connected
while nothing flows through it, a single relay node can be in cooldown and
silently skipped on every reconnect sweep, and a bulb can be "unavailable"
for three unrelated reasons that call for three different fixes.

All metrics come from CyncBLECoordinator.system_status() so the summary
counts and the attribute detail can't disagree with each other.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import CyncBLECoordinator
from .entity import CyncBLESystemEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    **kwargs: Any,
) -> None:
    """Set up the system-status sensors."""
    coordinator: CyncBLECoordinator = config_entry.runtime_data
    async_add_entities([
        CyncBLEConnectedMeshesSensor(coordinator),
        CyncBLEAvailableDevicesSensor(coordinator),
        CyncBLEUnavailableDevicesSensor(coordinator),
        CyncBLEQuietDevicesSensor(coordinator),
        CyncBLEUnknownDevicesSensor(coordinator),
        CyncBLEFirmwareKnownSensor(coordinator),
        CyncBLELastActivitySensor(coordinator),
    ])


class CyncBLESystemSensor(CyncBLESystemEntity, SensorEntity):
    """Base for the system-status sensors."""

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class CyncBLEConnectedMeshesSensor(CyncBLESystemSensor):
    """How many mesh GATT sessions are currently up."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: CyncBLECoordinator) -> None:
        super().__init__(coordinator, "connected_meshes")

    @property
    def native_value(self) -> int:
        return self._status["meshes"]["connected"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        meshes = self._status["meshes"]
        return {
            "total": meshes["total"],
            "connecting": meshes["connecting"],
            # Per-mesh: which node is carrying the session, which nodes are
            # in failure cooldown, and how long it's been down. See
            # CyncMeshClient.debug_state.
            "meshes": meshes["detail"],
        }


class CyncBLEAvailableDevicesSensor(CyncBLESystemSensor):
    """How many configured devices are believed reachable."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: CyncBLECoordinator) -> None:
        super().__init__(coordinator, "available_devices")

    @property
    def native_value(self) -> int:
        return self._status["devices"]["available"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        devices = self._status["devices"]
        return {"total": devices["total"], "unavailable": devices["unavailable"]}


class CyncBLEUnavailableDevicesSensor(CyncBLESystemSensor):
    """How many devices are unavailable, broken down by cause."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: CyncBLECoordinator) -> None:
        super().__init__(coordinator, "unavailable_devices")

    @property
    def native_value(self) -> int:
        return self._status["devices"]["unavailable"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        devices = self._status["devices"]
        return {
            # The breakdown is the point of this entity. Counts by reason:
            #   never_reported   — never sent a status since startup; likely
            #                      misconfigured, wrong mesh, or out of range
            #   probe_failed     — failed repeated direct probes; likely
            #                      powered off at the switch
            #   mesh_disconnected — not the device's fault; its mesh is down
            "by_reason": devices["unavailable_by_reason"],
            "detail": devices["unavailable_detail"],
        }


class CyncBLEQuietDevicesSensor(CyncBLESystemSensor):
    """Devices that are nominally available but have gone quiet.

    The mesh pushes status on change rather than on a heartbeat, so silence
    isn't proof of anything on its own — which is exactly why it's worth
    being able to see it. A device sitting here with rising probe misses is
    on its way to being marked unreachable.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: CyncBLECoordinator) -> None:
        super().__init__(coordinator, "quiet_devices")

    @property
    def native_value(self) -> int:
        return len(self._status["devices"]["quiet"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        quiet = self._status["devices"]["quiet"]
        return {
            "devices": quiet,
            "quietest_seconds": quiet[0]["seconds_since_seen"] if quiet else None,
        }


class CyncBLEUnknownDevicesSensor(CyncBLESystemSensor):
    """Devices heard on the mesh that aren't in the config entry.

    Mirrors the unknown_devices repair issue. A non-zero value here means
    Reconfigure would pick something up.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: CyncBLECoordinator) -> None:
        super().__init__(coordinator, "unknown_devices")

    @property
    def native_value(self) -> int:
        return self._status["unknown_devices"]["count"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"keys": self._status["unknown_devices"]["keys"]}


class CyncBLEFirmwareKnownSensor(CyncBLESystemSensor):
    """How many devices have reported a firmware version.

    Zero until cync_ble.query_firmware_versions has been run — nothing
    reports firmware unsolicited. After a run, the gap between this and the
    device total is the set of bulbs that stayed silent.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: CyncBLECoordinator) -> None:
        super().__init__(coordinator, "firmware_known")

    @property
    def native_value(self) -> int:
        return self._status["firmware"]["known"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        firmware = self._status["firmware"]
        return {"total": firmware["total"], "versions": firmware["versions"]}


class CyncBLELastActivitySensor(CyncBLESystemSensor):
    """When the most recent status notification arrived, from any device.

    The most diagnostic single value here: if this stops advancing while the
    mesh sensors still report connected, the GATT session is up but nothing
    is actually coming through it — a failure mode that otherwise looks
    healthy from every other angle.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: CyncBLECoordinator) -> None:
        super().__init__(coordinator, "last_activity")

    @property
    def native_value(self) -> Optional[Any]:
        # None renders as "unknown", which is the honest state before any
        # device has ever reported in.
        return self._status["last_activity"]
