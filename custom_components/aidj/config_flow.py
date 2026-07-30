"""Config flow for the AI DJ integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.selector import EntitySelectorConfig

from .const import CONF_NAME, CONF_PLAYER, CONF_TTS, DEFAULT_NAME, DOMAIN


class AiDjConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the AI DJ config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial setup step."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data=user_input,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): selector.TextSelector(),
                vol.Required(CONF_PLAYER): selector.EntitySelector(
                    EntitySelectorConfig(domain="media_player")
                ),
                vol.Required(CONF_TTS): selector.EntitySelector(
                    EntitySelectorConfig(domain="tts")
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow."""
        return AiDjOptionsFlow()


class AiDjOptionsFlow(config_entries.OptionsFlow):
    """Handle AI DJ options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the options form."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=current[CONF_NAME]): selector.TextSelector(),
                vol.Required(CONF_PLAYER, default=current[CONF_PLAYER]): selector.EntitySelector(
                    EntitySelectorConfig(domain="media_player")
                ),
                vol.Required(CONF_TTS, default=current[CONF_TTS]): selector.EntitySelector(
                    EntitySelectorConfig(domain="tts")
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
