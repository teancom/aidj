"""Runtime behavior for an AI DJ config entry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import CONF_NAME, CONF_PLAYER, CONF_TTS


def _track_identity(state: Any) -> str | None:
    """Return a stable identity for the currently playing media item."""
    attributes = getattr(state, "attributes", {})
    for key in ("media_content_id", "media_artist", "media_title"):
        value = attributes.get(key)
        if value:
            return ":".join(str(attributes.get(part, "")) for part in (
                "media_content_id", "media_artist", "media_title"
            ))
    return None


def queue_media_ids(queue: Any) -> set[str]:
    """Extract media URIs and queue item IDs from an HA queue response."""
    if not isinstance(queue, dict):
        return set()

    queue_data = queue.get("service_response", queue)
    if not isinstance(queue_data, dict):
        return set()

    player_queue = next(iter(queue_data.values()), queue_data)
    if not isinstance(player_queue, dict):
        return set()

    media_ids: set[str] = set()
    for item_key in ("current_item", "next_item"):
        item = player_queue.get(item_key)
        if not isinstance(item, dict):
            continue
        for key in ("queue_item_id", "media_item_id"):
            value = item.get(key)
            if isinstance(value, str):
                media_ids.add(value)
        media_item = item.get("media_item")
        if isinstance(media_item, dict):
            uri = media_item.get("uri")
            if isinstance(uri, str):
                media_ids.add(uri)

    return media_ids


@dataclass(slots=True)
class AiDjRuntime:
    """Runtime object for one configured AI DJ station."""

    hass: HomeAssistant
    entry: object
    _owned_media_ids: set[str] = field(default_factory=set)
    _pending_announcement: tuple[str, str] | None = None
    _announcement_unsub: Any = None

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

    async def async_announce_next(self, message: str) -> None:
        """Speak a message when the configured player advances to another track."""
        message = message.strip()
        if not message:
            raise ServiceValidationError("The announcement message must not be empty")

        self._require_player()
        state = self.hass.states.get(self.player_entity_id)
        assert state is not None
        if state.state != "playing":
            raise ServiceValidationError(
                "announce_next requires the configured media player to be playing"
            )

        baseline = _track_identity(state)
        if baseline is None:
            raise ServiceValidationError(
                "The configured media player has no identifiable current track"
            )

        if self._announcement_unsub is not None:
            self._announcement_unsub()
        self._pending_announcement = (baseline, message)
        self._announcement_unsub = self.hass.bus.async_listen(
            "state_changed", self._async_handle_state_changed
        )

    @callback
    def _async_handle_state_changed(self, event: Event) -> None:
        """Handle a configured-player track transition asynchronously."""
        if self._pending_announcement is None:
            return
        data = event.data
        if data.get("entity_id") != self.player_entity_id:
            return

        new_state = data.get("new_state")
        if new_state is None or new_state.state != "playing":
            return

        baseline, message = self._pending_announcement
        current = _track_identity(new_state)
        if current is None or current == baseline:
            return

        self._pending_announcement = None
        if self._announcement_unsub is not None:
            self._announcement_unsub()
            self._announcement_unsub = None
        self.hass.async_create_task(self.async_announce(message))

    @callback
    def async_unload(self) -> None:
        """Cancel pending announcement listeners during unload."""
        self._pending_announcement = None
        if self._announcement_unsub is not None:
            self._announcement_unsub()
            self._announcement_unsub = None

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

    async def async_queue_add(self, media_id: str) -> bool:
        """Add one media item unless it is already present in the active queue."""
        media_id = media_id.strip()
        if not media_id:
            raise ServiceValidationError("The media ID must not be empty")

        if media_id in self._owned_media_ids:
            return False

        queue = await self.async_get_queue()
        if media_id in queue_media_ids(queue):
            self._owned_media_ids.add(media_id)
            return False

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

        self._owned_media_ids.add(media_id)
        return True

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
