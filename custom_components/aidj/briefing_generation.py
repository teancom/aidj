"""Generate one AI DJ briefing from collected station context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence
import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .briefing import HaConversationBriefingGenerator, BriefingItem, QueueProvider
from .briefing_assembly import async_collect_station_briefing
from .const import (
    PROVIDER_FEEDREADER_PREFIX,
    PROVIDER_MUSIC_ASSISTANT_QUEUE,
    StationSettings,
)
from .prompt import build_briefing_prompt
from .story import select_feed_story

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GeneratedBriefing:
    """Generated speech plus the optional story selected for this briefing."""

    text: str
    selected_story_id: str | None = None


@dataclass(frozen=True, slots=True)
class BriefingGenerationService:
    """Collect context and generate a grounded briefing for one station."""

    hass: HomeAssistant
    settings: StationSettings
    player_entity_id: str
    music_assistant_client: Any
    recent_story_ids: Sequence[str]

    @property
    def music_assistant_player_id(self) -> str | None:
        """Return the configured native Music Assistant player, when enabled."""
        return self.settings.music_assistant_player_id or None

    @property
    def music_assistant_enabled(self) -> bool:
        """Return whether the native Music Assistant transport is configured."""
        return self.settings.music_assistant_enabled

    async def async_generate(
        self,
        weather_entity_id: str,
        agent_id: str,
        prompt: str | None = None,
    ) -> GeneratedBriefing:
        """Collect facts and generate speech with an explicit story selection."""
        weather_entity_id = (weather_entity_id or self.settings.weather_entity_id).strip()
        agent_id = (agent_id or self.settings.agent_id).strip()
        if not weather_entity_id or not agent_id:
            raise ServiceValidationError("weather_entity_id and agent_id must not be empty")

        collection = await async_collect_station_briefing(
            self.hass,
            self.settings,
            weather_entity_id=weather_entity_id,
            player_entity_id=self.player_entity_id,
            music_assistant_client=self.music_assistant_client,
            music_assistant_player_id=self.music_assistant_player_id,
        )
        items = collection.items
        selected_story = select_feed_story(items, self.recent_story_ids)
        if selected_story is not None:
            items = [
                item for item in items
                if not item.provider.startswith(PROVIDER_FEEDREADER_PREFIX)
            ] + [selected_story]
        if collection.errors:
            _LOGGER.info(
                "Optional briefing providers unavailable for station %s: %s",
                self.settings.name,
                ", ".join(f"{name}: {error}" for name, error in collection.errors.items()),
            )
        if not collection.weather_available:
            _LOGGER.warning("Briefing weather entity is unavailable: %s", weather_entity_id)
            raise ServiceValidationError(f"Weather entity does not exist: {weather_entity_id}")
        if self.music_assistant_enabled and not any(
            item.provider == PROVIDER_MUSIC_ASSISTANT_QUEUE for item in items
        ):
            _LOGGER.error(
                "Briefing generation stopped: verified music context was unavailable for "
                "HA entity %s / MA player %s; provider errors=%s",
                self.player_entity_id,
                self.music_assistant_player_id or "",
                collection.errors,
            )
            raise HomeAssistantError(
                "Music Assistant queue context was unavailable or inconsistent; "
                "the briefing was not generated"
            )

        generated = await self._async_generate_grounded(items, agent_id, prompt)
        return GeneratedBriefing(generated, selected_story.identity if selected_story else None)

    async def async_generate_music_transition(self, agent_id: str) -> GeneratedBriefing:
        """Generate a short transition from verified music context only."""
        agent_id = (agent_id or self.settings.agent_id).strip()
        if not agent_id:
            raise ServiceValidationError("agent_id must not be empty")
        provider = QueueProvider(
            self.hass,
            self.player_entity_id,
            self.music_assistant_client,
            self.music_assistant_player_id,
        )
        items = await provider.async_collect()
        if not items:
            raise HomeAssistantError(
                "Music Assistant queue context was unavailable or inconsistent; "
                "the transition was not generated"
            )
        prompt = (
            "Write a brief radio DJ music transition that plays after the completed song. "
            "Naturally identify the completed track and introduce at least one upcoming track. "
            "Use two concise spoken sentences and no non-music topics."
        )
        generated = await self._async_generate_grounded(items, agent_id, prompt)
        return GeneratedBriefing(generated)

    async def _async_generate_grounded(
        self,
        items: Sequence[BriefingItem],
        agent_id: str,
        prompt: str | None,
    ) -> str:
        """Generate speech from verified station facts in one conversation call."""
        full_prompt = build_briefing_prompt(
            items,
            prompt,
            self.settings.personality_instructions,
        )
        generator = HaConversationBriefingGenerator(self.hass, agent_id)
        return await generator.async_generate(full_prompt)
