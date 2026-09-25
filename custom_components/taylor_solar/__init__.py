"""The Taylor Solar integration."""

from __future__ import annotations

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import TaylorClient
from .coordinator import STORAGE_VERSION, TaylorConfigEntry, TaylorCoordinator, storage_key

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: TaylorConfigEntry) -> bool:
    """Set up Taylor Solar from a config entry."""
    client = TaylorClient(
        async_get_clientsession(hass), entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
    )
    coordinator = TaylorCoordinator(hass, entry, client)
    await coordinator.importer.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TaylorConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: TaylorConfigEntry) -> None:
    """Remove the importer cursor; imported statistics are kept."""
    await Store(hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()
