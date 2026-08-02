"""Typed queue snapshots and boundary adapters for Music Assistant."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .music_context import QueueContext, native_queue_context, artist_names


@dataclass(frozen=True, slots=True)
class QueueItemIdentity:
    """Stable, comparable identity for one queue item."""

    queue_item_id: str
    uri: str
    track: str
    artists: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """Comparable boundary representation of the active queue."""

    queue_id: str
    current_index: int
    current_item: QueueItemIdentity | None
    next_item: QueueItemIdentity | None


def _as_dict(item: Any) -> Mapping[str, Any] | None:
    if hasattr(item, "to_dict"):
        try:
            item = item.to_dict()
        except Exception:  # noqa: BLE001 - malformed provider object
            return None
    return item if isinstance(item, Mapping) else None


def queue_item_identity(item: Any) -> QueueItemIdentity | None:
    """Parse the comparable identity shared by HA and native queue items."""
    item_dict = _as_dict(item)
    media_item = item_dict.get("media_item") if item_dict else None
    if not isinstance(media_item, Mapping):
        return None
    queue_item_id = item_dict.get("queue_item_id")
    uri = media_item.get("uri")
    track = media_item.get("name") or item_dict.get("name")
    if not all(isinstance(value, str) and value.strip() for value in (queue_item_id, uri, track)):
        return None
    return QueueItemIdentity(
        queue_item_id=queue_item_id.strip(),
        uri=uri.strip(),
        track=track.strip(),
        artists=artist_names(media_item.get("artists")),
    )


def parse_ha_queue_snapshot(queue: Mapping[str, Any] | Any) -> QueueSnapshot | None:
    """Adapt an HA service queue dictionary into a typed snapshot."""
    if not isinstance(queue, Mapping):
        return None
    queue_id = queue.get("queue_id")
    current_index = queue.get("current_index")
    if not isinstance(queue_id, str) or not queue_id.strip() or not isinstance(current_index, int):
        return None
    current_item = queue_item_identity(queue.get("current_item"))
    next_item = queue_item_identity(queue.get("next_item"))
    if current_item is None or next_item is None:
        return None
    return QueueSnapshot(queue_id.strip(), current_index, current_item, next_item)


def parse_native_queue_snapshot(queue: Any, items: Sequence[Any]) -> tuple[QueueSnapshot, QueueContext] | None:
    """Adapt native MA queue objects/items into a typed snapshot and context."""
    queue_id = getattr(queue, "queue_id", None)
    current_index = getattr(queue, "current_index", None)
    if not isinstance(queue_id, str) or not queue_id.strip() or not isinstance(current_index, int):
        return None
    context = native_queue_context(items, current_index)
    current = next((item for item in items if getattr(item, "index", None) == current_index), None)
    upcoming = next(
        (item for item in items if isinstance(getattr(item, "index", None), int) and item.index > current_index),
        None,
    )
    current_identity = queue_item_identity(current)
    next_identity = queue_item_identity(upcoming)
    if context.current is None or not context.next or current_identity is None or next_identity is None:
        return None
    return QueueSnapshot(queue_id.strip(), current_index, current_identity, next_identity), context


def snapshot_mismatches(ha: QueueSnapshot, native: QueueSnapshot) -> dict[str, tuple[Any, Any]]:
    """Return only fields that differ between two already-parsed snapshots."""
    values = {
        "queue_id": (ha.queue_id, native.queue_id),
        "current_index": (ha.current_index, native.current_index),
        "current_item": (ha.current_item, native.current_item),
        "next_item": (ha.next_item, native.next_item),
    }
    return {name: pair for name, pair in values.items() if pair[0] != pair[1]}
