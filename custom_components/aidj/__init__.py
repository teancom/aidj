"""The AI DJ integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import ATTR_MESSAGE, CONF_CONFIG_ENTRY_ID, DOMAIN, SERVICE_ANNOUNCE
from .runtime import AiDjRuntime

SERVICE_ANNOUNCE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
    }
)


def _get_runtime(hass: HomeAssistant, entry_id: str | None) -> AiDjRuntime:
    """Resolve the configured station for a service call."""
    runtimes: dict[str, AiDjRuntime] = hass.data[DOMAIN]
    if entry_id:
        runtime = runtimes.get(entry_id)
        if runtime is None:
            raise HomeAssistantError(f"Unknown AI DJ config entry: {entry_id}")
        return runtime

    if len(runtimes) != 1:
        raise HomeAssistantError(
            "Specify config_entry_id when zero or multiple AI DJ stations are configured"
        )
    return next(iter(runtimes.values()))


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the AI DJ integration domain."""
    hass.data.setdefault(DOMAIN, {})

    async def async_handle_announce(call: ServiceCall) -> None:
        """Handle the aidj.announce service."""
        runtime = _get_runtime(hass, call.data.get(CONF_CONFIG_ENTRY_ID))
        await runtime.async_announce(call.data[ATTR_MESSAGE])

    hass.services.async_register(
        DOMAIN,
        SERVICE_ANNOUNCE,
        async_handle_announce,
        schema=SERVICE_ANNOUNCE_SCHEMA,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an AI DJ config entry."""
    runtime = AiDjRuntime(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = runtime
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply updated options without restarting Home Assistant."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an AI DJ config entry."""
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True
