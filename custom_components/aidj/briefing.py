"""Provider-neutral briefing data and Home Assistant-backed sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Protocol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BriefingItem:
    """One normalized fact that may be used in a DJ briefing."""

    provider: str
    title: str
    summary: str
    occurred_at: datetime | None = None
    source: str | None = None


class BriefingProvider(Protocol):
    """Source of normalized briefing facts."""

    name: str

    async def async_collect(self) -> list[BriefingItem]:
        """Collect current facts from this provider."""


@dataclass(frozen=True, slots=True)
class WeatherEntityProvider:
    """Expose the current state of one Home Assistant weather entity."""

    hass: HomeAssistant
    entity_id: str
    name: str = "weather"

    async def async_collect(self) -> list[BriefingItem]:
        """Return one compact weather fact when the entity is available."""
        state = self.hass.states.get(self.entity_id)
        if state is None:
            return []

        attributes = state.attributes
        friendly_name = attributes.get("friendly_name", self.entity_id)
        details = [f"conditions: {state.state}"]
        if (temperature := attributes.get("temperature")) is not None:
            unit = attributes.get("temperature_unit", "")
            details.append(f"temperature: {temperature}{unit}")
        if (humidity := attributes.get("humidity")) is not None:
            details.append(f"humidity: {humidity}%")
        if (wind_speed := attributes.get("wind_speed")) is not None:
            unit = attributes.get("wind_speed_unit", "")
            details.append(f"wind: {wind_speed}{unit}")

        return [
            BriefingItem(
                provider=self.name,
                title=str(friendly_name),
                summary=f"{friendly_name}: {', '.join(details)}",
                occurred_at=state.last_updated,
                source=self.entity_id,
            )
        ]


@dataclass(frozen=True, slots=True)
class EntityStateProvider:
    """Expose selected Home Assistant entity states as briefing facts."""

    hass: HomeAssistant
    entity_ids: tuple[str, ...]
    name: str = "home_assistant"

    async def async_collect(self) -> list[BriefingItem]:
        """Return readable state facts for entities that exist."""
        items: list[BriefingItem] = []
        for entity_id in self.entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            friendly_name = state.attributes.get("friendly_name", entity_id)
            items.append(
                BriefingItem(
                    provider=self.name,
                    title=str(friendly_name),
                    summary=f"{friendly_name}: {state.state}",
                    occurred_at=state.last_updated,
                    source=entity_id,
                )
            )
        return items


@dataclass(frozen=True, slots=True)
class QueueProvider:
    """Expose current and next Music Assistant queue items through HA."""

    hass: HomeAssistant
    player_entity_id: str
    name: str = "music_assistant_queue"

    async def async_collect(self) -> list[BriefingItem]:
        """Return current/next queue facts without mutating playback."""
        response = await self.hass.services.async_call(
            "music_assistant",
            "get_queue",
            target={"entity_id": self.player_entity_id},
            blocking=True,
            return_response=True,
        )
        if not isinstance(response, dict):
            return []
        queue_data = response.get("service_response", response)
        if not isinstance(queue_data, dict):
            return []
        player_queue = queue_data.get(self.player_entity_id)
        if not isinstance(player_queue, dict):
            return []

        items: list[BriefingItem] = []
        for label, key in (("Now playing", "current_item"), ("Up next", "next_item")):
            item = player_queue.get(key)
            if not isinstance(item, dict):
                continue
            media_item = item.get("media_item")
            if not isinstance(media_item, dict):
                continue
            title = media_item.get("name") or item.get("name")
            if not isinstance(title, str) or not title.strip():
                continue
            artists = media_item.get("artists", [])
            artist_names = [
                artist.get("name")
                for artist in artists
                if isinstance(artist, dict) and isinstance(artist.get("name"), str)
            ]
            artist_text = f" by {', '.join(artist_names)}" if artist_names else ""
            items.append(
                BriefingItem(
                    provider=self.name,
                    title=label,
                    summary=f"{label}: {title}{artist_text}",
                    source=media_item.get("uri") if isinstance(media_item.get("uri"), str) else None,
                )
            )
        return items


@dataclass(frozen=True, slots=True)
class HaConversationBriefingGenerator:
    """Generate validated briefing text through HA's conversation service."""

    hass: HomeAssistant
    agent_id: str
    name: str = "home_assistant_conversation"

    async def async_generate(self, prompt: str) -> str:
        """Ask HA's configured conversation agent for plain speech."""
        prompt = prompt.strip()
        if not prompt:
            raise ServiceValidationError("The briefing prompt must not be empty")

        try:
            response = await self.hass.services.async_call(
                "conversation",
                "process",
                {"text": prompt, "agent_id": self.agent_id},
                blocking=True,
                return_response=True,
            )
        except Exception as err:
            _LOGGER.warning(
                "Briefing generation failed via conversation agent %s: %s",
                self.agent_id,
                err,
            )
            raise HomeAssistantError(
                f"Unable to generate a briefing with {self.agent_id}: {err}"
            ) from err

        speech = (
            response.get("response", {})
            .get("speech", {})
            .get("plain", {})
            .get("speech")
            if isinstance(response, dict)
            else None
        )
        if not isinstance(speech, str) or not speech.strip():
            _LOGGER.warning(
                "Conversation agent %s returned no plain speech for briefing",
                self.agent_id,
            )
            raise HomeAssistantError(
                f"The conversation agent {self.agent_id} returned no plain speech"
            )
        return speech.strip()


async def async_collect_briefing(
    providers: tuple[BriefingProvider, ...],
) -> tuple[list[BriefingItem], dict[str, str]]:
    """Collect provider facts while isolating failures by provider."""
    items: list[BriefingItem] = []
    errors: dict[str, str] = {}
    for provider in providers:
        try:
            items.extend(await provider.async_collect())
        except Exception as err:  # noqa: BLE001 - providers are optional boundaries
            _LOGGER.warning("Briefing provider %s failed: %s", provider.name, err)
            errors[provider.name] = str(err)
    return items, errors
