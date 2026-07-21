"""Cync BLE integration for Home Assistant."""
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PLATFORMS, CONF_DEVICES
from .coordinator import CyncBLECoordinator

_LOGGER = logging.getLogger(__name__)

# Type alias so platforms can annotate entry.runtime_data correctly
type CyncBLEConfigEntry = ConfigEntry[CyncBLECoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Cync BLE integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CyncBLEConfigEntry) -> bool:
    """Set up a config entry."""
    devices_config = entry.data.get(CONF_DEVICES, [])

    if not devices_config:
        _LOGGER.error("No devices found in config entry — re-add the integration")
        return False

    try:
        coordinator = CyncBLECoordinator(hass, devices_config)
        entry.runtime_data = coordinator
        await coordinator.async_refresh()
    except Exception as err:
        _LOGGER.exception("Error setting up Cync BLE: %s", err)
        raise ConfigEntryNotReady(f"Could not set up Cync BLE: {err}") from err

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(
        hass.bus.async_listen_once("homeassistant_stop", coordinator.async_shutdown)
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: CyncBLEConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: CyncBLEConfigEntry) -> None:
    """Reload a config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
