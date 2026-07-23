"""Switch platform for Cync BLE integration — Cync smart plugs.

Plugs (Capabilities["PLUG"] device types) are on/off only, using the same
CMD_POWER opcode as a bulb's power state — see CyncBLEDevice.turn_on/turn_off.
"""
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    """Set up switch entities (Cync smart plugs) from a config entry."""
    coordinator: CyncBLECoordinator = config_entry.runtime_data
    entities = [
        CyncBLESwitch(coordinator, device)
        for device in coordinator.get_devices().values()
        if device.is_plug
    ]
    async_add_entities(entities)


class CyncBLESwitch(SwitchEntity):
    """Representation of a Cync BLE smart plug."""

    _attr_has_entity_name = True
    _attr_name = None

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
            model="Cync BLE Plug",
        )

    @property
    def available(self) -> bool:
        return self._device.is_available

    @property
    def is_on(self) -> bool:
        return self._device.is_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "mac_address": self._device.mac_address,
            "mesh_name": self._device.mesh_name,
            "device_id": self._device.device_id,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        if not await self._device.turn_on():
            _LOGGER.error("Failed to turn on %s", self._device.name)
            return
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if not await self._device.turn_off():
            _LOGGER.error("Failed to turn off %s", self._device.name)
            return
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
