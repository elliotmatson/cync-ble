"""Cync BLE integration for Home Assistant."""
import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_DEVICES,
    FIRMWARE_QUERY_WINDOW,
    FIRMWARE_QUERY_WINDOW_MIN,
    FIRMWARE_QUERY_WINDOW_MAX,
)
from .coordinator import CyncBLECoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_QUERY_FIRMWARE_VERSIONS = "query_firmware_versions"
ATTR_WINDOW = "window"

SERVICE_QUERY_FIRMWARE_VERSIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_WINDOW, default=FIRMWARE_QUERY_WINDOW): vol.All(
            vol.Coerce(float),
            vol.Range(min=FIRMWARE_QUERY_WINDOW_MIN, max=FIRMWARE_QUERY_WINDOW_MAX),
        ),
    }
)

# Type alias so platforms can annotate entry.runtime_data correctly
type CyncBLEConfigEntry = ConfigEntry[CyncBLECoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Cync BLE integration."""
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the integration-wide services, once.

    These are integration-wide rather than per-entry, so the has_service
    guard keeps a second config entry from re-registering them. They are
    deliberately NOT removed in async_unload_entry either: a reload unloads
    and re-sets-up every entry, and tearing the service down in between would
    make it briefly vanish from the service registry — long enough for an
    automation that fires in that window to fail with an unknown-service
    error.
    """
    if hass.services.has_service(DOMAIN, SERVICE_QUERY_FIRMWARE_VERSIONS):
        return

    async def _handle_query_firmware_versions(call: ServiceCall) -> ServiceResponse:
        """Collect firmware versions from every loaded Cync BLE entry."""
        window: float = call.data[ATTR_WINDOW]

        coordinators: list[CyncBLECoordinator] = [
            entry.runtime_data
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
            if isinstance(getattr(entry, "runtime_data", None), CyncBLECoordinator)
        ]
        if not coordinators:
            return {"devices": [], "responded": 0, "total": 0, "window": window}

        # Entries are independent sets of meshes, so run them together rather
        # than paying the collection window once per entry.
        results = await asyncio.gather(
            *(c.async_query_firmware_versions(window) for c in coordinators),
            return_exceptions=True,
        )

        merged: dict[str, Any] = {
            "devices": [],
            "responded": 0,
            "total": 0,
            "window": window,
            "meshes_queried": [],
            "meshes_failed": [],
            "meshes_not_connected": [],
        }
        for result in results:
            if isinstance(result, BaseException):
                _LOGGER.warning("Firmware version query failed for an entry: %s", result)
                continue
            merged["devices"].extend(result["devices"])
            merged["responded"] += result["responded"]
            merged["total"] += result["total"]
            for key in ("meshes_queried", "meshes_failed", "meshes_not_connected"):
                merged[key].extend(result[key])
        return merged

    hass.services.async_register(
        DOMAIN,
        SERVICE_QUERY_FIRMWARE_VERSIONS,
        _handle_query_firmware_versions,
        schema=SERVICE_QUERY_FIRMWARE_VERSIONS_SCHEMA,
        # ONLY: this is a read. It returns data and changes nothing, so
        # there is no meaningful non-response form of the call.
        supports_response=SupportsResponse.ONLY,
    )


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

    _async_register_services(hass)

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
