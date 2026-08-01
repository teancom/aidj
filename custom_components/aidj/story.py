"""Pure local-news story rotation helpers."""

from __future__ import annotations

from collections.abc import Sequence

from .briefing import BriefingItem


FEEDREADER_PROVIDER_PREFIX = "feedreader:"


def select_feed_story(
    items: Sequence[BriefingItem],
    recent_story_ids: Sequence[str],
) -> BriefingItem | None:
    """Choose an unseen feed story, or the least-recently-used available one."""
    feed_items = [
        item
        for item in items
        if item.provider.startswith(FEEDREADER_PROVIDER_PREFIX) and item.identity
    ]
    if not feed_items:
        return None

    recent = set(recent_story_ids)
    for item in feed_items:
        if item.identity not in recent:
            return item

    return min(
        feed_items,
        key=lambda item: recent_story_ids.index(item.identity)
        if item.identity in recent_story_ids
        else -1,
    )


def record_story(
    recent_story_ids: Sequence[str],
    story_id: str | None,
    limit: int,
) -> list[str]:
    """Return bounded FIFO story history after recording one selected story."""
    if story_id is None:
        return list(recent_story_ids)[-limit:]
    updated = [existing for existing in recent_story_ids if existing != story_id]
    updated.append(story_id)
    return updated[-limit:]
