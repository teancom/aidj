"""Prompt construction for AI DJ briefing generation."""

from __future__ import annotations

from collections.abc import Sequence

from .briefing import BriefingItem

DEFAULT_BRIEFING_PROMPT = (
    "Write a concise, friendly radio DJ briefing for an announcement "
    "that plays after the current song has finished. Refer to the completed song "
    "in the past tense (for example, 'You were listening to...'), not 'You're listening to...'."
)

BRIEFING_STYLE_INSTRUCTIONS = (
    "Use the supplied facts as source material, but write like a human local radio DJ. "
    "The structured music context is optional flavor: do not mention it at every break, "
    "and only comment on it when an observation is genuinely interesting and natural. "
    "For local news, explain the development naturally in one or two conversational "
    "sentences; paraphrase the headline when that sounds better, and do not say "
    "'there is a headline' or 'in local news, there is a headline'. Do not read RSS "
    "boilerplate such as 'the post appeared first on'. Keep the facts accurate and "
    "do not invent details."
)


def build_briefing_prompt(
    items: Sequence[BriefingItem],
    custom_prompt: str | None = None,
) -> str:
    """Build the exact prompt sent to the configured conversation agent."""
    facts = "\n".join(f"- {item.summary}" for item in items)
    opening = (custom_prompt or DEFAULT_BRIEFING_PROMPT).strip()
    return f"{opening}\n{BRIEFING_STYLE_INSTRUCTIONS}\n\nFacts:\n{facts}"
