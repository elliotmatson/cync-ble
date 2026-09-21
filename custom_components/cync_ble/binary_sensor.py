"""Binary sensor platform for Cync BLE — overall mesh connectivity.

One at-a-glance "is the integration actually talking to anything" signal,
suitable for an automation or alert template. The numeric detail lives on
the sensor entities; this exists so you don't have to compare a count
against a total to answer the only question that matters during an outage.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
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
    """Set up the system-status binary sensors."""
    coordinator: CyncBLECoordinator = config_entry.runtime_data
    async_add_entities([CyncBLEMeshConnectivitySensor(coordinator)])


class CyncBLEMeshConnectivitySensor(CyncBLESystemEntity, BinarySensorEntity):
    """On when at least one mesh GATT session is up.

    Deliberately "any", not "all": with one mesh connected the integration
    can still control every bulb on it, so treating a partial outage as a
    full one would misreport the common case of one mesh out of several
    dropping. The per-mesh breakdown is in the attributes and the exact
    count is on the connected_meshes sensor.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: CyncBLECoordinator) -> None:
        super().__init__(coordinator, "mesh_connectivity")

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._coordinator.connected_mesh_count > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        meshes = self._status["meshes"]
        return {
            "connected": meshes["connected"],
            "total": meshes["total"],
            "connecting": meshes["connecting"],
            "meshes": meshes["detail"],
        }
