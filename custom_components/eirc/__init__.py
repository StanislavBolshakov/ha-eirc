"""The EIRC integration."""

from __future__ import annotations
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .const import DOMAIN
from .api import EIRCApiClient


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from config entry."""
    hass.data.setdefault(DOMAIN, {})
    client = EIRCApiClient(hass, entry.data["username"], entry.data["password"])
    hass.data[DOMAIN][entry.entry_id] = client
    return await hass.config_entries.async_forward_entry_setup(entry, "sensor")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, ["sensor"]):
        client = hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
