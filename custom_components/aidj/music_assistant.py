"""Music Assistant client adapter for queue-backed AI DJ media."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiohttp import ClientSession
from music_assistant_client import MusicAssistantClient
from music_assistant_models.enums import QueueOption


@dataclass(slots=True)
class HaTtsUrlRenderer:
    """Render HA TTS without invoking playback on the configured player."""

    session: ClientSession
    base_url: str
    access_token: str
    engine_id: str

    async def async_render(self, message: str) -> str:
        """Return a cached HA TTS URL suitable for MA queue playback."""
        message = message.strip()
        if not message:
            raise ValueError("message must not be empty")
        url = f"{self.base_url.rstrip('/')}/api/tts_get_url"
        async with self.session.post(
            url,
            headers={"Authorization": f"Bearer {self.access_token}"},
            json={"engine_id": self.engine_id, "message": message, "cache": True},
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        rendered_url = payload.get("url")
        if not isinstance(rendered_url, str) or not rendered_url.strip():
            raise RuntimeError("Home Assistant TTS did not return a playable URL")
        return rendered_url.strip()


@dataclass(slots=True)
class MusicAssistantQueueAdapter:
    """Small domain adapter around the official Music Assistant client."""

    client: MusicAssistantClient
    player_id: str

    async def async_insert_next(self, media_uri: str) -> str:
        """Insert a prepared media URI as the next queue item.

        QueueOption.NEXT is intentionally used instead of ADD followed by a move:
        Music Assistant owns queue ordering and buffering, and a single NEXT command
        avoids exposing an intermediate append-at-the-end state.
        """
        media_uri = media_uri.strip()
        if not media_uri:
            raise ValueError("media_uri must not be empty")

        queue = await self.client.player_queues.get_active_queue(self.player_id)
        if queue is None:
            raise RuntimeError(f"No active Music Assistant queue for {self.player_id}")

        # A bare HTTP URL is parsed by MA as MediaType.UNKNOWN. Address it
        # through the built-in provider so MA resolves it as a playable track
        # and can route it to the configured player controller.
        ma_uri = f"builtin://track/{media_uri}"
        await self.client.player_queues.play_media(
            queue_id=queue.queue_id,
            media=[ma_uri],
            option=QueueOption.NEXT,
        )

        items = await self.client.player_queues.get_queue_items(queue.queue_id)
        matching = [
            item
            for item in items
            if getattr(item, "media_item", None) is not None
            and getattr(item.media_item, "uri", None) == ma_uri
        ]
        if not matching:
            raise RuntimeError(f"Music Assistant did not expose inserted queue item {ma_uri}")
        return matching[-1].queue_item_id

    async def async_remove(self, queue_item_id: str) -> None:
        """Remove an AI DJ-owned queue item after it has played."""
        queue = await self.client.player_queues.get_active_queue(self.player_id)
        if queue is None:
            return
        await self.client.player_queues.delete_item(queue.queue_id, queue_item_id)

    async def async_get_current_and_next(self) -> dict[str, Any]:
        """Return the current and next MA queue items for briefing context."""
        queue = await self.client.player_queues.get_active_queue(self.player_id)
        if queue is None:
            return {}
        return {
            "queue_id": queue.queue_id,
            "current_item": queue.current_item,
            "next_item": queue.next_item,
            "current_item_id": (
                queue.current_item.queue_item_id if queue.current_item is not None else None
            ),
            "next_item_id": (
                queue.next_item.queue_item_id if queue.next_item is not None else None
            ),
            "elapsed_time": queue.elapsed_time,
        }
