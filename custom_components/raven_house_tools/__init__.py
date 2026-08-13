"""The Raven House Tools integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_FEATURE, DOMAIN, FEATURE_BOTH
from .entities import async_setup_jobs_services
from .quiz_const import DOMAIN as QUIZ_DOMAIN
from .quiz_entities import async_setup_quiz_services
from .soundboard import async_setup_soundboard_services, async_unload_soundboard

PLATFORMS = ["binary_sensor", "sensor", "switch", "text", "button", "number", "select"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Raven House Tools."""
    hass.data.setdefault(DOMAIN, {})
    hass.data.setdefault(QUIZ_DOMAIN, {})
    await async_setup_soundboard_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Raven House Tools from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data.setdefault(QUIZ_DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}
    hass.data[QUIZ_DOMAIN][entry.entry_id] = {}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_setup_jobs_services(hass)
    await async_setup_quiz_services(hass)
    await async_setup_soundboard_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate legacy single-entry installs to explicit feature metadata."""
    if CONF_FEATURE in entry.data:
        return True

    data = {**entry.data, CONF_FEATURE: FEATURE_BOTH}
    hass.config_entries.async_update_entry(entry, data=data)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    hass.data.get(QUIZ_DOMAIN, {}).pop(entry.entry_id, None)

    remaining_entries = [
        loaded_entry
        for loaded_entry in hass.config_entries.async_entries(DOMAIN)
        if loaded_entry.entry_id != entry.entry_id
    ]
    if not remaining_entries:
        await async_unload_soundboard(hass)
    return True
