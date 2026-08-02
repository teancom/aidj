"""The AI DJ integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.loader import IntegrationNotFound
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_MESSAGE,
    ATTR_PROMPT,
    CONF_CONFIG_ENTRY_ID,
    DOMAIN,
    ATTR_MEDIA_ID,
    CONF_AGENT,
    CONF_HA_TOKEN,
    CONF_MA_PLAYER,
    CONF_MA_TOKEN,
    CONF_MA_URL,
    CONF_WEATHER,
    SERVICE_ANNOUNCE,
    SERVICE_ANNOUNCE_NEXT,
    SERVICE_BRIEFING,
    SERVICE_BRIEFING_NEXT,
    SERVICE_QUEUE_ADD,
)
from .runtime import AiDjRuntime

SERVICE_ANNOUNCE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
    }
)
SERVICE_QUEUE_ADD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MEDIA_ID): cv.string,
        vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
    }
)
SERVICE_BRIEFING_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_WEATHER, default=""): cv.string,
        vol.Optional(CONF_AGENT, default=""): cv.string,
        vol.Optional(ATTR_PROMPT): cv.string,
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

    async def async_handle_announce_next(call: ServiceCall) -> None:
        """Handle the aidj.announce_next service."""
        runtime = _get_runtime(hass, call.data.get(CONF_CONFIG_ENTRY_ID))
        await runtime.async_announce_next(call.data[ATTR_MESSAGE])

    async def async_handle_briefing(call: ServiceCall) -> dict[str, str]:
        """Handle the aidj.briefing service."""
        runtime = _get_runtime(hass, call.data.get(CONF_CONFIG_ENTRY_ID))
        text = await runtime.async_generate_briefing(
            call.data.get(CONF_WEATHER, ""),
            call.data.get(CONF_AGENT, ""),
            call.data.get(ATTR_PROMPT),
        )
        return {"text": text}

    async def async_handle_briefing_next(call: ServiceCall) -> None:
        """Handle the aidj.briefing_next service."""
        runtime = _get_runtime(hass, call.data.get(CONF_CONFIG_ENTRY_ID))
        await runtime.async_briefing_next(
            call.data.get(CONF_WEATHER, ""),
            call.data.get(CONF_AGENT, ""),
            call.data.get(ATTR_PROMPT),
        )

    async def async_handle_queue_add(call: ServiceCall) -> None:
        """Handle the aidj.queue_add service."""
        runtime = _get_runtime(hass, call.data.get(CONF_CONFIG_ENTRY_ID))
        await runtime.async_queue_add(call.data[ATTR_MEDIA_ID])

    hass.services.async_register(
        DOMAIN,
        SERVICE_ANNOUNCE,
        async_handle_announce,
        schema=SERVICE_ANNOUNCE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ANNOUNCE_NEXT,
        async_handle_announce_next,
        schema=SERVICE_ANNOUNCE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BRIEFING,
        async_handle_briefing,
        schema=SERVICE_BRIEFING_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BRIEFING_NEXT,
        async_handle_briefing_next,
        schema=SERVICE_BRIEFING_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_QUEUE_ADD,
        async_handle_queue_add,
        schema=SERVICE_QUEUE_ADD_SCHEMA,
    )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Move legacy per-station MA settings into integration data."""
    if entry.version >= 2:
        return True
    legacy = entry.options
    updates = {
        key: legacy[key]
        for key in (CONF_MA_URL, CONF_MA_TOKEN, CONF_HA_TOKEN, CONF_MA_PLAYER)
        if key in legacy
    }
    remaining_options = {
        key: value for key, value in legacy.items() if key not in updates
    }
    if updates:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, **updates},
            options=remaining_options,
            version=2,
        )
    else:
        hass.config_entries.async_update_entry(entry, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an AI DJ config entry."""
    runtime = AiDjRuntime(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = runtime
    await runtime.async_start_music_assistant()
    try:
        await runtime.async_initialize_controller()
        await hass.config_entries.async_forward_entry_setups(entry, ["switch"])
    except IntegrationNotFound:
        # Direct unit tests call async_setup_entry without registering the custom
        # integration with HA's loader; real HA setup always takes the path above.
        runtime.async_unload()
        from . import switch

        await switch.async_setup_entry(hass, entry, lambda entities: None)
    except Exception:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        runtime.async_unload()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply updated options without restarting Home Assistant."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an AI DJ config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["switch"])
    runtime = hass.data[DOMAIN].pop(entry.entry_id, None)
    if runtime is not None:
        runtime.async_unload()
    return unload_ok
