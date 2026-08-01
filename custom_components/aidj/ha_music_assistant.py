"""Home Assistant service transport for Music Assistant queue operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError


def queue_media_ids(queue: Any, player_entity_id: str) -> set[str]:
    """Extract media URIs and queue item IDs for one player from an HA response."""
    if not isinstance(queue, dict):
        return set()

    queue_data = queue.get("service_response", queue)
    if not isinstance(queue_data, dict):
        return set()

    player_queue = queue_data.get(player_entity_id, queue_data)
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
class HaMusicAssistantQueue:
    """Add media through HA's Music Assistant services without replacing queues."""

    hass: HomeAssistant
    player_entity_id: str
    _owned_media_ids: set[str] = field(default_factory=set)

    async def async_get_queue(self) -> Any:
        """Read the configured player's active Music Assistant queue."""
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

    async def async_add(self, media_id: str) -> bool:
        """Add one media item unless it is already present in the active queue."""
        media_id = media_id.strip()
        if not media_id:
            raise ServiceValidationError("The media ID must not be empty")

        if media_id in self._owned_media_ids:
            return False

        queue = await self.async_get_queue()
        if media_id in queue_media_ids(queue, self.player_entity_id):
            self._owned_media_ids.add(media_id)
            return False

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

    def _require_player(self) -> None:
        """Raise when the configured media player is not available in HA."""
        if self.hass.states.get(self.player_entity_id) is None:
            raise ServiceValidationError(
                f"Configured media player does not exist: {self.player_entity_id}"
            )
