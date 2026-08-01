"""Pure Music Assistant queue-context normalization helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


TrackContext = dict[str, Any]
QueueContext = dict[str, Any]


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
    return {"current": None, "previous": [], "next": []}


def _normalize_item(item: Any) -> TrackContext | None:
    """Normalize one item and discard it when it has no track name."""
    context = track_context(item.to_dict() if hasattr(item, "to_dict") else item)
    return context if context["track"] else None


def native_queue_context(queue_items: Iterable[Any], current_index: int) -> QueueContext:
    """Select current, previous, and up to three upcoming native MA tracks."""
    context = _empty_context()
    for item in queue_items:
        index = getattr(item, "index", None)
        if not isinstance(index, int):
            continue
        normalized = _normalize_item(item)
        if normalized is None:
            continue
        if index == current_index:
            context["current"] = normalized
        elif current_index - 3 <= index < current_index:
            context["previous"].append(normalized)
        elif current_index < index <= current_index + 3:
            context["next"].append(normalized)
    return context


def fallback_queue_context(player_queue: Mapping[str, Any]) -> QueueContext:
    """Normalize current, previous, and next items returned by HA's MA service."""
    context = _empty_context()
    current_item = player_queue.get("current_item")
    if current_item:
        context["current"] = _normalize_item(current_item)

    for side, key in (("previous", "previous_items"), ("next", "next_items")):
        queue_items = player_queue.get(key, [])
        if not isinstance(queue_items, list):
            queue_items = []
        if not queue_items and side == "next" and player_queue.get("next_item"):
            queue_items = [player_queue["next_item"]]
        bounded = queue_items[-3:] if side == "previous" else queue_items[:3]
        context[side].extend(_context_for_items(bounded))
    return context


def has_tracks(context: QueueContext) -> bool:
    """Return whether a normalized queue context contains any usable tracks."""
    return bool(context["current"] or context["previous"] or context["next"])
