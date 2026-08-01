"""Config flow for the AI DJ integration."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import EntitySelectorConfig
from music_assistant_client import MusicAssistantClient

from .const import (
    CONF_AGENT,
    CONF_AQI,
    CONF_AQI_THRESHOLD,
    CONF_CALENDARS,
    CONF_FEEDS,
    CONF_HA_TOKEN,
    CONF_MA_PLAYER,
    CONF_MA_TOKEN,
    CONF_MA_URL,
    CONF_NAME,
    CONF_PLAYER,
    CONF_TTS,
    CONF_WEATHER,
    DEFAULT_MA_URL,
    DEFAULT_NAME,
    DOMAIN,
)


async def _async_get_ma_players(
    hass: Any, server_url: str, token: str
) -> list[dict[str, str]]:
    """Authenticate to MA and return human-readable player selector options."""
    client = MusicAssistantClient(
        server_url.strip(),
        async_get_clientsession(hass),
        token=token.strip(),
    )
    ready = asyncio.Event()
    startup_error: list[BaseException] = []

    async def listen() -> None:
        try:
            await client.start_listening(init_ready=ready)
        except BaseException as err:  # noqa: BLE001 - propagate through the flow
            startup_error.append(err)
            ready.set()
            raise

    task = hass.async_create_task(listen())
    try:
        await asyncio.wait_for(ready.wait(), timeout=15)
        if startup_error:
            raise startup_error[0]
        players = list(client.players.players)
    finally:
        await client.disconnect()
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    options: list[dict[str, str]] = []
    for player in sorted(players, key=lambda item: item.name.casefold()):
        model = getattr(getattr(player, "device_info", None), "model", "")
        label = player.name
        if model and model.casefold() not in label.casefold():
            label = f"{label} ({model})"
        options.append({"value": player.player_id, "label": label})
    return options


def _ma_player_selector(
    options: list[dict[str, str]], default: str = ""
) -> selector.SelectSelector:
    """Build a dropdown that stores the MA ID but displays a player name."""
    if default and not any(item["value"] == default for item in options):
        options = [
            *options,
            {"value": default, "label": f"{default} (currently configured; unavailable)"},
        ]
    return selector.SelectSelector(
        {
            "options": options,
            "mode": selector.SelectSelectorMode.DROPDOWN,
        }
    )


class AiDjConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the AI DJ config flow."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the flow."""
        self._integration_data: dict[str, str] = {}
        self._ma_player_options: list[dict[str, str]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect integration-level credentials first."""
        if user_input is not None:
            try:
                self._ma_player_options = await _async_get_ma_players(
                    self.hass, user_input[CONF_MA_URL], user_input[CONF_MA_TOKEN]
                )
            except Exception:  # noqa: BLE001 - surface connection/auth failure in UI
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._integration_schema(user_input),
                    errors={"base": "cannot_connect"},
                )
            if not self._ma_player_options:
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._integration_schema(user_input),
                    errors={"base": "no_players"},
                )
            self._integration_data = {
                CONF_MA_URL: user_input[CONF_MA_URL].strip(),
                CONF_MA_TOKEN: user_input[CONF_MA_TOKEN].strip(),
                CONF_HA_TOKEN: user_input[CONF_HA_TOKEN].strip(),
            }
            return self.async_show_form(
                step_id="station",
                data_schema=self._station_schema(),
            )

        return self.async_show_form(step_id="user", data_schema=self._integration_schema())

    async def async_step_station(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect station settings and the selected MA player."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data={
                    **self._integration_data,
                    **user_input,
                },
            )
        return self.async_show_form(step_id="station", data_schema=self._station_schema())

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update integration-level MA credentials and player selection."""
        entry = self._get_reconfigure_entry()
        current = dict(entry.data)
        if user_input is not None:
            try:
                options = await _async_get_ma_players(
                    self.hass, user_input[CONF_MA_URL], user_input[CONF_MA_TOKEN]
                )
            except Exception:  # noqa: BLE001 - surface connection/auth failure in UI
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=self._integration_schema(user_input),
                    errors={"base": "cannot_connect"},
                )
            if not options:
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=self._integration_schema(user_input),
                    errors={"base": "no_players"},
                )
            self._integration_data = {
                CONF_MA_URL: user_input[CONF_MA_URL].strip(),
                CONF_MA_TOKEN: user_input[CONF_MA_TOKEN].strip(),
                CONF_HA_TOKEN: user_input[CONF_HA_TOKEN].strip(),
            }
            self._ma_player_options = options
            return self.async_show_form(
                step_id="reconfigure_player",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_MA_PLAYER,
                            default=current.get(CONF_MA_PLAYER, ""),
                        ): _ma_player_selector(
                            options, current.get(CONF_MA_PLAYER, "")
                        ),
                    }
                ),
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._integration_schema(current),
        )

    async def async_step_reconfigure_player(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the selected MA player after credential validation."""
        if user_input is not None:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(),
                data_updates={
                    **self._integration_data,
                    CONF_MA_PLAYER: user_input[CONF_MA_PLAYER],
                },
            )
        return self.async_show_form(
            step_id="reconfigure_player",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MA_PLAYER): _ma_player_selector(
                        self._ma_player_options
                    ),
                }
            ),
        )

    def _integration_schema(self, current: dict[str, Any] | None = None) -> vol.Schema:
        """Return the integration-level credential schema."""
        current = current or {}
        return vol.Schema(
            {
                vol.Required(
                    CONF_MA_URL,
                    default=current.get(CONF_MA_URL, DEFAULT_MA_URL),
                ): selector.TextSelector({"type": selector.TextSelectorType.URL}),
                vol.Required(
                    CONF_MA_TOKEN,
                    default=current.get(CONF_MA_TOKEN, ""),
                ): selector.TextSelector({"type": selector.TextSelectorType.PASSWORD}),
                vol.Required(
                    CONF_HA_TOKEN,
                    default=current.get(CONF_HA_TOKEN, ""),
                ): selector.TextSelector({"type": selector.TextSelectorType.PASSWORD}),
            }
        )

    def _station_schema(self) -> vol.Schema:
        """Return station settings plus the token-resolved MA player dropdown."""
        return vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): selector.TextSelector(),
                vol.Required(CONF_PLAYER): selector.EntitySelector(
                    EntitySelectorConfig(domain="media_player")
                ),
                vol.Required(CONF_TTS): selector.EntitySelector(
                    EntitySelectorConfig(domain="tts")
                ),
                vol.Required(CONF_MA_PLAYER): _ma_player_selector(
                    self._ma_player_options
                ),
            }
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the station options flow."""
        return AiDjOptionsFlow()


class AiDjOptionsFlow(config_entries.OptionsFlow):
    """Handle station-specific AI DJ options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle station options without shared credentials."""
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
                vol.Optional(CONF_WEATHER, default=current.get(CONF_WEATHER, "")): selector.EntitySelector(
                    EntitySelectorConfig(domain="weather")
                ),
                vol.Optional(CONF_AGENT, default=current.get(CONF_AGENT, "")): selector.ConversationAgentSelector(),
                vol.Optional(
                    CONF_FEEDS,
                    default=current.get(CONF_FEEDS, []),
                ): selector.EntitySelector(
                    EntitySelectorConfig(domain="event", multiple=True)
                ),
                vol.Optional(
                    CONF_CALENDARS,
                    default=current.get(CONF_CALENDARS, []),
                ): selector.EntitySelector(
                    EntitySelectorConfig(domain="calendar", multiple=True)
                ),
                vol.Optional(
                    CONF_AQI,
                    default=current.get(CONF_AQI, ""),
                ): selector.EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(
                    CONF_AQI_THRESHOLD,
                    default=current.get(CONF_AQI_THRESHOLD, "101"),
                ): selector.SelectSelector(
                    {
                        "options": [
                            {"value": "51", "label": "51+ (moderate)"},
                            {"value": "101", "label": "101+ (unhealthy for sensitive groups)"},
                            {"value": "151", "label": "151+ (unhealthy)"},
                            {"value": "201", "label": "201+ (very unhealthy)"},
                            {"value": "301", "label": "301+ (hazardous)"},
                        ],
                        "mode": selector.SelectSelectorMode.DROPDOWN,
                    }
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
