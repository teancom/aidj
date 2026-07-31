"""Runtime behavior for an AI DJ config entry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import CONF_NAME, CONF_PLAYER, CONF_TTS


@dataclass(slots=True)
class AiDjRuntime:
    """Runtime object for one configured AI DJ station."""

    hass: HomeAssistant
    entry: object

    @property
    def settings(self) -> dict[str, str]:
        """Return current config data with options overriding initial values."""
        return {**self.entry.data, **self.entry.options}

    @property
    def name(self) -> str:
        """Return the station name."""
        return self.settings[CONF_NAME]

    @property
    def player_entity_id(self) -> str:
        """Return the configured media player."""
        return self.settings[CONF_PLAYER]

    @property
    def tts_entity_id(self) -> str:
        """Return the configured TTS entity."""
        return self.settings[CONF_TTS]

    async def async_start(self) -> None:
        """Resume playback on the configured media player."""
        await self._async_call_player_service("media_play")

    async def async_get_queue(self) -> Any:
        """Read the active Music Assistant queue through Home Assistant."""
        self._require_player()
        try:
            return await self.hass.services.async_call(
                "music_assistant",
                "get_queue",
                target={"entity_id": self.player_entity_id},
                blocking=True,
                return_response=True,
            )
        except Exception as err:
            raise HomeAssistantError(
                f"Unable to read the Music Assistant queue for {self.player_entity_id}: {err}"
            ) from err

    async def async_queue_add(self, media_id: str) -> None:
        """Add one media item to the configured Music Assistant queue."""
        media_id = media_id.strip()
        if not media_id:
            raise ServiceValidationError("The media ID must not be empty")

        self._require_player()
        try:
            await self.hass.services.async_call(
                "music_assistant",
                "play_media",
                {"media_id": media_id, "enqueue": "add"},
                target={"entity_id": self.player_entity_id},
                blocking=True,
            )
        except Exception as err:
            raise HomeAssistantError(
                f"Unable to add media to the Music Assistant queue for "
                f"{self.player_entity_id}: {err}"
            ) from err

    async def async_stop(self) -> None:
        """Stop playback on the configured media player."""
        await self._async_call_player_service("media_stop")

    def _require_player(self) -> None:
        """Raise when the configured media player is not available in HA."""
        if self.hass.states.get(self.player_entity_id) is None:
            raise ServiceValidationError(
                f"Configured media player does not exist: {self.player_entity_id}"
            )

    async def _async_call_player_service(self, service: str) -> None:
        """Call a media-player service for this station's configured player."""
        self._require_player()

        try:
            await self.hass.services.async_call(
                "media_player",
                service,
                target={"entity_id": self.player_entity_id},
                blocking=True,
            )
        except Exception as err:
            raise HomeAssistantError(
                f"Unable to call media_player.{service} on {self.player_entity_id}: {err}"
            ) from err

    async def async_announce(self, message: str) -> None:
        """Speak a one-shot message on the configured media player."""
        message = message.strip()
        if not message:
            raise ServiceValidationError("The announcement message must not be empty")

        self._require_player()

        tts_entity = self.hass.states.get(self.tts_entity_id)
        if tts_entity is None:
            raise ServiceValidationError(
                f"Configured TTS entity does not exist: {self.tts_entity_id}"
            )

        try:
            await self.hass.services.async_call(
                "tts",
                "speak",
                {
                    "media_player_entity_id": self.player_entity_id,
                    "message": message,
                    "cache": False,
                },
                target={"entity_id": self.tts_entity_id},
                blocking=True,
            )
        except Exception as err:
            raise HomeAssistantError(
                f"Unable to announce on {self.player_entity_id}: {err}"
            ) from err
