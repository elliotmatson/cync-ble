"""Fan platform for Cync BLE integration — Cync fan controllers.

Fans (Capabilities["FAN"] device types) have no dedicated speed opcode in
the mesh protocol — speed is just the same CMD_BRIGHTNESS command (0-100)
reinterpreted as percentage. See CyncBLEDevice.percentage/set_percentage.
"""
import logging
from typing import Any, Optional

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN
from .coordinator import CyncBLECoordinator, CyncBLEDevice

_LOGGER = logging.getLogger(__name__)

# Push-based integration — coordinator handles serialisation; no cap needed.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    **kwargs: Any,
) -> None:
    """Set up fan entities (Cync fan controllers) from a config entry."""
    coordinator: CyncBLECoordinator = config_entry.runtime_data
    entities = [
        CyncBLEFan(coordinator, device)
        for device in coordinator.get_devices().values()
        if device.is_fan
    ]
    async_add_entities(entities)


class CyncBLEFan(FanEntity):
    """Representation of a Cync BLE fan controller."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: CyncBLECoordinator, device: CyncBLEDevice):
        self._coordinator = coordinator
        self._device = device
        self._attr_unique_id = f"{DOMAIN}_{device.mac_address}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.mac_address)},
            name=self._device.name,
            manufacturer="GE Lighting",
            model="Cync BLE Fan",
        )

    @property
    def available(self) -> bool:
        return self._device.is_available

    @property
    def is_on(self) -> bool:
        return self._device.is_on

    @property
    def percentage(self) -> Optional[int]:
        return self._device.percentage if self._device.is_on else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "mac_address": self._device.mac_address,
            "mesh_name": self._device.mesh_name,
            "device_id": self._device.device_id,
        }

    async def async_turn_on(
        self,
        percentage: Optional[int] = None,
        preset_mode: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Turn on, then apply the requested speed, if any."""
        if not self.is_on:
            if not await self._device.turn_on():
                _LOGGER.error("Failed to turn on %s", self._device.name)
                return

        if percentage is not None:
            if not await self._device.set_percentage(percentage):
                _LOGGER.error("Failed to set speed on %s", self._device.name)

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if not await self._device.turn_off():
            _LOGGER.error("Failed to turn off %s", self._device.name)
            return
        self.async_write_ha_state()

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            await self.async_turn_off()
            return

        if not self.is_on:
            if not await self._device.turn_on():
                _LOGGER.error("Failed to turn on %s", self._device.name)
                return

        if not await self._device.set_percentage(percentage):
            _LOGGER.error("Failed to set speed on %s", self._device.name)

        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )

    async def async_will_remove_from_hass(self) -> None:
        await self._device.disconnect()
