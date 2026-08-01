"""Prompt construction for AI DJ briefing generation."""

from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Any

from .briefing import BriefingItem

DEFAULT_BRIEFING_PROMPT = (
    "Write a concise, friendly radio DJ briefing for an announcement "
    "that plays after the current song has finished. Refer to the completed song "
    "in the past tense (for example, 'You were listening to...'), not 'You're listening to...'."
)

BRIEFING_STYLE_INSTRUCTIONS = (
    "Use the supplied facts as source material, but write like a human local radio DJ. "
    "When music context includes a current track, naturally identify the completed song "
    "or artist in the past tense. When an upcoming track is supplied, include a natural "
    "coming-up-next reference to at least one of those tracks. Use the exact supplied "
    "song and artist names exactly as written. Do not replace those specific references "
    "with generic phrases like 'that was great' or 'more great music'. "
    "The broader structured music context is optional flavor: do not mention every "
    "previous track or genre at every break, and only add those details when natural. "
    "Treat the facts as a coherent briefing, not as an isolated checklist. When two or "
    "more facts naturally relate—for example, an upcoming calendar event and the day's "
    "weather—make a useful, human connection between them. It is fine to mention "
    "several relevant facts in one break, and it is fine to leave out irrelevant facts. "
    "For local news, explain the development naturally in one or two conversational "
    "sentences; paraphrase the headline when that sounds better, and do not say "
    "'there is a headline' or 'in local news, there is a headline'. Do not read RSS "
    "boilerplate such as 'the post appeared first on'. Keep the facts accurate and "
    "do not invent details or assume calendar details that are not supplied."
)


def _music_fact_lines(summary: str) -> list[str] | None:
    """Turn the structured queue JSON into explicit grounding lines."""
    try:
        context = json.loads(summary)
    except (TypeError, ValueError):
        return None
    if not isinstance(context, dict):
        return None

    def describe(label: str, track: Any) -> str | None:
        if not isinstance(track, dict) or not track.get("track"):
            return None
        title = str(track["track"])
        artist = track.get("artist")
        details = f"{title} by {artist}" if artist else title
        return f"{label}: {details}"

    lines: list[str] = []
    if current := describe("Completed/current track", context.get("current")):
        lines.append(current)
    for track in context.get("previous", []):
        if previous := describe("Previous track", track):
            lines.append(previous)
    for track in context.get("next", []):
        if upcoming := describe("Upcoming track", track):
            lines.append(upcoming)
    return lines or None


def _fact_lines(item: BriefingItem) -> list[str]:
    """Return readable, model-grounding lines for one briefing fact."""
    if item.provider == "music_assistant_queue":
        if music_lines := _music_fact_lines(item.summary):
            return music_lines
    return [item.summary]


def music_required_terms(items: Sequence[BriefingItem]) -> tuple[str, ...]:
    """Return exact current and first-upcoming track titles for grounding checks."""
    terms: list[str] = []
    for item in items:
        if item.provider != "music_assistant_queue":
            continue
        try:
            context = json.loads(item.summary)
        except (TypeError, ValueError):
            continue
        if not isinstance(context, dict):
            continue
        current = context.get("current")
        upcoming = context.get("next")
        for track in (current, upcoming[0] if isinstance(upcoming, list) and upcoming else None):
            if isinstance(track, dict) and isinstance(track.get("track"), str):
                title = track["track"].strip()
                if title:
                    terms.append(title)
    return tuple(dict.fromkeys(terms))


def briefing_needs_grounding_retry(speech: str, required_terms: Sequence[str]) -> bool:
    """Reject placeholder or music-free output when exact track facts exist."""
    lowered = speech.casefold()
    placeholders = ("[artist", "[song", "[insert ", "song title", "more great music")
    return bool(
        any(marker in lowered for marker in placeholders)
        or any(term.casefold() not in lowered for term in required_terms)
    )


def build_briefing_prompt(
    items: Sequence[BriefingItem],
    custom_prompt: str | None = None,
) -> str:
    """Build the exact prompt sent to the configured conversation agent."""
    facts = "\n".join(
        f"- {line}" for item in items for line in _fact_lines(item)
    )
    opening = (custom_prompt or DEFAULT_BRIEFING_PROMPT).strip()
    return f"{opening}\n{BRIEFING_STYLE_INSTRUCTIONS}\n\nFacts:\n{facts}"
