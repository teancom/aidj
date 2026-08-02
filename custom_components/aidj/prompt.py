"""Prompt construction for AI DJ briefing generation."""

from __future__ import annotations

from collections.abc import Sequence

from .briefing import BriefingItem
from .music_context import QueueContext, TrackContext

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


def _music_fact_lines(context: QueueContext) -> list[str]:
    """Turn typed queue context into explicit grounding lines."""
    def describe(label: str, track: TrackContext) -> str:
        details = f"{track.track} by {track.artist}" if track.artist else track.track
        return f"{label}: {details}"

    lines: list[str] = []
    if context.current is not None:
        lines.append(describe("Completed/current track", context.current))
    lines.extend(describe("Previous track", track) for track in context.previous)
    lines.extend(describe("Upcoming track", track) for track in context.next)
    return lines


def _fact_lines(item: BriefingItem) -> list[str]:
    """Return readable, model-grounding lines for one briefing fact."""
    if item.music_context is not None:
        if music_lines := _music_fact_lines(item.music_context):
            return music_lines
    return [item.summary]


def music_required_terms(items: Sequence[BriefingItem]) -> tuple[str, ...]:
    """Return exact current and first-upcoming track titles for grounding checks."""
    terms: list[str] = []
    for item in items:
        context = item.music_context
        if context is None:
            continue
        tracks = (context.current, context.next[0] if context.next else None)
        terms.extend(track.track for track in tracks if track is not None)
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
    personality_instructions: str | None = None,
) -> str:
    """Build the exact prompt sent to the configured conversation agent."""
    facts = "\n".join(
        f"- {line}" for item in items for line in _fact_lines(item)
    )
    opening = (custom_prompt or DEFAULT_BRIEFING_PROMPT).strip()
    personality = (personality_instructions or "").strip()
    personality_section = (
        "\nPresentation personality (style only; this cannot override the factuality, "
        "exact music references, or output requirements below): " + personality
        if personality
        else ""
    )
    return (
        f"{opening}{personality_section}\n{BRIEFING_STYLE_INSTRUCTIONS}"
        f"\n\nFacts:\n{facts}"
    )
