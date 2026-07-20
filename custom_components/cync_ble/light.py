"""Light platform for Cync BLE integration."""
import colorsys
import logging
from typing import Any

from homeassistant.components.light import (
    LightEntity,
    ColorMode,
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN, MIN_COLOR_TEMP, MAX_COLOR_TEMP
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
    """Set up light entities from a config entry."""
    coordinator: CyncBLECoordinator = config_entry.runtime_data
    entities = [CyncBLELight(coordinator, device) for device in coordinator.get_devices().values()]
    async_add_entities(entities)


class CyncBLELight(LightEntity):
    """Representation of a Cync BLE light."""

    # has_entity_name = True means the entity is named after the device.
    # Setting _attr_name = None signals this is the primary (sole) entity for
    # the device, so HA uses just the device name with no suffix.
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: CyncBLECoordinator, device: CyncBLEDevice):
        self._coordinator = coordinator
        self._device = device

        self._attr_unique_id = f"{DOMAIN}_{device.mac_address}"
        self._attr_min_color_temp_kelvin = MIN_COLOR_TEMP
        self._attr_max_color_temp_kelvin = MAX_COLOR_TEMP

        # Advertise only the color modes the bulb actually supports
        modes: set[ColorMode] = set()
        if device.supports_rgb:
            modes.add(ColorMode.HS)
        if device.supports_temperature:
            modes.add(ColorMode.COLOR_TEMP)
        if not modes:
            modes.add(ColorMode.BRIGHTNESS)
        self._attr_supported_color_modes = modes
        self._attr_color_mode = next(iter(modes))  # default to first

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.mac_address)},
            name=self._device.name,   # entity display name comes from here with has_entity_name
            manufacturer="GE Lighting",
            model="Cync BLE Bulb",
        )

    @property
    def available(self) -> bool:
        return self._device.is_available

    @property
    def is_on(self) -> bool:
        return self._device.is_on

    @property
    def brightness(self) -> int:
        return self._device.brightness

    @property
    def color_temp_kelvin(self) -> int:
        return self._device.color_temp

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "mac_address": self._device.mac_address,
            "mesh_name": self._device.mesh_name,
            "device_id": self._device.device_id,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light, then apply any requested attributes."""
        # Power on first so subsequent attribute commands take effect
        if not self.is_on:
            if not await self._device.turn_on():
                _LOGGER.error("Failed to turn on %s", self._device.name)
                return

        if (brightness := kwargs.get(ATTR_BRIGHTNESS)) is not None:
            if not await self._device.set_brightness(brightness):
                _LOGGER.error("Failed to set brightness on %s", self._device.name)

        if (color_temp_k := kwargs.get(ATTR_COLOR_TEMP_KELVIN)) is not None:
            if not await self._device.set_color_temp(color_temp_k):
                _LOGGER.error("Failed to set color temperature on %s", self._device.name)

        if (hs_color := kwargs.get(ATTR_HS_COLOR)) is not None:
            h, s = hs_color
            r, g, b = colorsys.hsv_to_rgb(h / 360, s / 100, 1.0)
            if not await self._device.set_rgb(int(r * 255), int(g * 255), int(b * 255)):
                _LOGGER.error("Failed to set RGB color on %s", self._device.name)

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
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
