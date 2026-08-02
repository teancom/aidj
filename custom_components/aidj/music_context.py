"""Pure Music Assistant queue-context normalization helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TrackContext:
    """Normalized track metadata used for prompts and grounding checks."""

    track: str
    artist: str | None = None
    album: str | None = None
    genre: str | None = None
    year: int | None = None


@dataclass(frozen=True, slots=True)
class QueueContext:
    """Typed queue window around the track that has just completed."""

    current: TrackContext | None = None
    previous: tuple[TrackContext, ...] = ()
    next: tuple[TrackContext, ...] = ()


def as_mapping(value: Any) -> Mapping[str, Any] | None:
    """Adapt Music Assistant models and dictionaries to a mapping boundary."""
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "to_dict"):
        try:
            serialized = value.to_dict()
        except Exception:  # noqa: BLE001 - malformed provider object
            return None
        return serialized if isinstance(serialized, Mapping) else None
    return None


def artist_names(raw: Any) -> tuple[str, ...]:
    """Extract stable artist names from MA metadata."""
    if not isinstance(raw, (list, tuple, set)):
        return ()
    names: list[str] = []
    for artist in raw:
        artist_data = as_mapping(artist)
        name = artist_data.get("name") if artist_data else getattr(artist, "name", None)
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return tuple(names)


def track_context(item: Any) -> TrackContext | None:
    """Normalize one MA queue item, rejecting values without a track name."""
    item_data = as_mapping(item)
    if item_data is None:
        return None
    media_item = as_mapping(item_data.get("media_item"))
    if media_item is None:
        return None

    track = media_item.get("name") or item_data.get("name")
    if not isinstance(track, str) or not track.strip():
        return None
    artists = artist_names(media_item.get("artists"))
    album = as_mapping(media_item.get("album"))
    album_name = album.get("name") if album else None
    metadata = as_mapping(media_item.get("metadata"))
    genres = metadata.get("genres") if metadata else None
    genre = ", ".join(sorted(genres)) if isinstance(genres, (list, set, tuple)) else None
    year = album.get("year") if isinstance(album, dict) else None
    return TrackContext(
        track=track.strip(),
        artist=", ".join(artists) or None,
        album=album_name if isinstance(album_name, str) else None,
        genre=genre,
        year=year if isinstance(year, int) else None,
    )


def _context_for_items(items: Iterable[Any]) -> tuple[TrackContext, ...]:
    """Normalize items and omit entries without a usable track name."""
    return tuple(context for item in items if (context := track_context(item)) is not None)


def _normalize_item(item: Any) -> TrackContext | None:
    """Normalize native objects and dictionary queue items alike."""
    return track_context(item.to_dict() if hasattr(item, "to_dict") else item)


def native_queue_context(queue_items: Iterable[Any], current_index: int) -> QueueContext:
    """Select current, previous, and up to three upcoming native MA tracks."""
    current: TrackContext | None = None
    previous: list[TrackContext] = []
    upcoming: list[TrackContext] = []
    for item in queue_items:
        index = getattr(item, "index", None)
        if not isinstance(index, int):
            continue
        normalized = _normalize_item(item)
        if normalized is None:
            continue
        if index == current_index:
            current = normalized
        elif current_index - 3 <= index < current_index:
            previous.append(normalized)
        elif current_index < index <= current_index + 3:
            upcoming.append(normalized)
    return QueueContext(current, tuple(previous), tuple(upcoming))
