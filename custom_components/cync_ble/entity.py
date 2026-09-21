"""Shared base for the Cync BLE system-status entities.

These entities describe the integration itself rather than any one bulb, so
they hang off a single service device per config entry instead of off a
mesh device.
"""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from .const import DOMAIN
from .coordinator import CyncBLECoordinator


class CyncBLESystemEntity:
    """Mixin providing the shared service-device identity and update wiring."""

    _attr_has_entity_name = True
    # DIAGNOSTIC keeps these out of the main dashboard and area cards —
    # they're for looking at when something is wrong, not for controlling
    # anything, and they'd otherwise clutter every view a bulb appears in.
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: CyncBLECoordinator, key: str) -> None:
        self._coordinator = coordinator
        self._key = key
        # entry_id scopes the device so two Cync accounts each get their own
        # system device rather than colliding on one.
        self._attr_unique_id = f"{DOMAIN}_system_{coordinator.entry_id}_{key}"
        self._attr_translation_key = key

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"system_{self._coordinator.entry_id}")},
            name="Cync BLE System",
            manufacturer="GE Lighting",
            model="Cync BLE Integration",
            # SERVICE marks this as the integration's own device rather than
            # a piece of hardware, so HA doesn't present it as a bulb.
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Always available, deliberately.

        A diagnostic entity that goes unavailable when the mesh goes down is
        useless exactly when it's needed — "how many meshes are connected"
        has a correct and interesting answer (zero) precisely in the case
        that would make a bulb entity unavailable.
        """
        return True

    @property
    def _status(self) -> dict[str, Any]:
        return self._coordinator.system_status()

    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )
