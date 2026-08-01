"""Pure Music Assistant queue-context normalization helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


TrackContext = dict[str, Any]
QueueContext = dict[str, list[TrackContext]]


def track_context(item: Any) -> TrackContext:
    """Normalize one MA queue item into nullable DJ context fields."""
    media_item = item.get("media_item") if isinstance(item, dict) else None
    if not isinstance(media_item, dict):
        return {"artist": None, "album": None, "track": None, "genre": None, "year": None}

    artists = media_item.get("artists") or []
    artist_names = [
        artist.get("name")
        for artist in artists
        if isinstance(artist, dict) and isinstance(artist.get("name"), str)
    ]
    album = media_item.get("album")
    album_name = album.get("name") if isinstance(album, dict) else None
    metadata = media_item.get("metadata") or {}
    genres = metadata.get("genres") if isinstance(metadata, dict) else None
    genre = ", ".join(sorted(genres)) if isinstance(genres, (list, set, tuple)) else None
    year = album.get("year") if isinstance(album, dict) else None
    return {
        "artist": ", ".join(artist_names) or None,
        "album": album_name if isinstance(album_name, str) else None,
        "track": media_item.get("name") or item.get("name"),
        "genre": genre,
        "year": year if isinstance(year, int) else None,
    }


def _context_for_items(items: Iterable[Any]) -> list[TrackContext]:
    """Normalize items and omit entries without a usable track name."""
    contexts: list[TrackContext] = []
    for item in items:
        context = track_context(item)
        if context["track"]:
            contexts.append(context)
    return contexts


def _empty_context() -> QueueContext:
    """Return the stable shape used in briefing prompt facts."""
    return {"previous": [], "next": []}


def native_queue_context(queue_items: Iterable[Any], current_index: int) -> QueueContext:
    """Select up to three native MA items on either side of the current item."""
    selected: list[tuple[str, Any]] = []
    for item in queue_items:
        index = getattr(item, "index", None)
        if not isinstance(index, int):
            continue
        if current_index - 3 <= index < current_index:
            selected.append(("previous", item))
        elif current_index < index <= current_index + 3:
            selected.append(("next", item))

    selected.sort(key=lambda pair: pair[1].index)
    context = _empty_context()
    for side, item in selected:
        item_dict = item.to_dict() if hasattr(item, "to_dict") else item
        normalized = track_context(item_dict)
        if normalized["track"]:
            context[side].append(normalized)
    return context


def fallback_queue_context(player_queue: Mapping[str, Any]) -> QueueContext:
    """Normalize the previous/next item lists returned by HA's MA service."""
    context = _empty_context()
    for side, key in (("previous", "previous_items"), ("next", "next_items")):
        queue_items = player_queue.get(key, [])
        if not isinstance(queue_items, list):
            queue_items = []
        if not queue_items and side == "previous" and player_queue.get("current_item"):
            queue_items = [player_queue["current_item"]]
        if not queue_items and side == "next" and player_queue.get("next_item"):
            queue_items = [player_queue["next_item"]]
        bounded = queue_items[-3:] if side == "previous" else queue_items[:3]
        context[side].extend(_context_for_items(bounded))
    return context


def has_tracks(context: QueueContext) -> bool:
    """Return whether a normalized queue context contains any usable tracks."""
    return bool(context["previous"] or context["next"])
