"""Music Assistant client adapter for queue-backed AI DJ media."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from aiohttp import ClientSession
from music_assistant_client import MusicAssistantClient
from music_assistant_models.enums import ContentType, QueueOption
from music_assistant_models.media_items import AudioFormat, ProviderMapping
from music_assistant_models.media_items.media_item import SoundEffect


@dataclass(frozen=True, slots=True)
class QueueMedia:
    """One finite audio effect to insert into the Music Assistant queue."""

    uri: str
    name: str
    duration: int = 0
    content_type: ContentType | None = None


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

    async def async_insert_sequence(self, media: Sequence[QueueMedia]) -> list[str]:
        """Insert ordered finite sound effects and return their queue IDs in order."""
        if not media:
            raise ValueError("media must not be empty")
        entries = list(media)
        tracks: list[SoundEffect] = []
        for entry in entries:
            media_uri = entry.uri.strip()
            if not media_uri:
                raise ValueError("media URI must not be empty")
            if entry.duration < 0:
                raise ValueError("media duration must not be negative")
            mapping_kwargs: dict[str, object] = {
                "item_id": media_uri,
                "provider_domain": "builtin",
                "provider_instance": "builtin",
            }
            content_type = entry.content_type or ContentType.try_parse(media_uri)
            if content_type is not ContentType.UNKNOWN:
                mapping_kwargs["audio_format"] = AudioFormat(content_type=content_type)
            tracks.append(
                SoundEffect(
                    item_id=media_uri,
                    provider="builtin",
                    name=entry.name,
                    duration=entry.duration,
                    provider_mappings={ProviderMapping(**mapping_kwargs)},
                )
            )

        queue = await self.client.player_queues.get_active_queue(self.player_id)
        if queue is None:
            raise RuntimeError(f"No active Music Assistant queue for {self.player_id}")
        existing_items = await self.client.player_queues.get_queue_items(queue.queue_id)
        existing_ids = {
            item_id
            for item in existing_items
            if isinstance((item_id := getattr(item, "queue_item_id", None)), str)
        }
        await self.client.player_queues.play_media(
            queue_id=queue.queue_id,
            media=tracks,
            option=QueueOption.NEXT,
        )

        items = await self.client.player_queues.get_queue_items(queue.queue_id)
        queue_ids: list[str] = []
        used_queue_item_ids: set[str] = set()
        for track in tracks:
            matching = next(
                (
                    item
                    for item in items
                    if (queue_item_id := getattr(item, "queue_item_id", None))
                    and queue_item_id not in existing_ids
                    and queue_item_id not in used_queue_item_ids
                    and (
                        getattr(item, "uri", None) == track.uri
                        or (
                            getattr(item, "media_item", None) is not None
                            and getattr(item.media_item, "item_id", None) == track.item_id
                        )
                        or getattr(item, "name", "") == track.name
                    )
                ),
                None,
            )
            if matching is None:
                raise RuntimeError(
                    f"Music Assistant did not expose inserted AI DJ queue item {track.name}"
                )
            queue_item_id = matching.queue_item_id
            used_queue_item_ids.add(queue_item_id)
            queue_ids.append(queue_item_id)
        return queue_ids

    async def async_insert_next(self, media_uri: str) -> str:
        """Insert a rendered announcement as the next queue item."""
        return (
            await self.async_insert_sequence(
                [QueueMedia(media_uri, "AI DJ Announcement", content_type=ContentType.MP3)]
            )
        )[0]

    async def async_remove(self, queue_item_id: str) -> None:
        """Remove an AI DJ-owned queue item after it has played."""
        queue = await self.client.player_queues.get_active_queue(self.player_id)
        if queue is None:
            return
        await self.client.player_queues.delete_item(queue.queue_id, queue_item_id)
