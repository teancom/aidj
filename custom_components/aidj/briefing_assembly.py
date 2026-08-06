"""Assemble and collect the configured AI DJ briefing sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.core import HomeAssistant

from .briefing import (
    AqiEntityProvider,
    BriefingClock,
    BriefingItem,
    BriefingProvider,
    CalendarEventProvider,
    FeedreaderEventProvider,
    QueueProvider,
    WeatherEntityProvider,
    async_collect_briefing,
)
from .const import PROVIDER_FEEDREADER_PREFIX, PROVIDER_WEATHER, StationSettings
from .music_assistant import MusicAssistantClient


@dataclass(frozen=True, slots=True)
class BriefingCollection:
    """Normalized facts and optional-provider errors for one collection pass."""

    items: list[BriefingItem]
    errors: dict[str, str]

    @property
    def weather_available(self) -> bool:
        """Return whether the configured weather source produced a fact."""
        return any(item.provider == PROVIDER_WEATHER for item in self.items)


def build_briefing_providers(
    hass: HomeAssistant,
    settings: StationSettings,
    *,
    weather_entity_id: str,
    player_entity_id: str,
    music_assistant_client: MusicAssistantClient | None,
    music_assistant_player_id: str | None = None,
    now: datetime | None = None,
) -> tuple[BriefingProvider, ...]:
    """Build providers in the stable order used by the station prompt."""
    clock = BriefingClock(now) if now is not None else BriefingClock.capture()
    feed_entity_ids = settings.feed_entity_ids
    calendar_entity_ids = settings.calendar_entity_ids
    aqi_entity_id = settings.aqi_entity_id
    aqi_threshold = settings.aqi_relevance_threshold

    providers: list[BriefingProvider] = [
        WeatherEntityProvider(hass, weather_entity_id, clock=clock)
    ]
    providers.extend(
        FeedreaderEventProvider(
            hass,
            entity_id,
            name=f"{PROVIDER_FEEDREADER_PREFIX}{entity_id}",
            clock=clock,
        )
        for entity_id in feed_entity_ids
    )
    providers.extend(
        CalendarEventProvider(
            hass, entity_id, name=f"calendar:{entity_id}", clock=clock
        )
        for entity_id in calendar_entity_ids
    )
    if aqi_entity_id:
        providers.append(AqiEntityProvider(hass, aqi_entity_id, aqi_threshold))
    providers.append(
        QueueProvider(
            hass,
            player_entity_id,
            music_assistant_client,
            music_assistant_player_id,
        )
    )
    return tuple(providers)


async def async_collect_station_briefing(
    hass: HomeAssistant,
    settings: StationSettings,
    *,
    weather_entity_id: str,
    player_entity_id: str,
    music_assistant_client: MusicAssistantClient | None,
    music_assistant_player_id: str | None = None,
    now: datetime | None = None,
) -> BriefingCollection:
    """Build and collect all configured sources for one station pass."""
    providers = build_briefing_providers(
        hass,
        settings,
        weather_entity_id=weather_entity_id,
        player_entity_id=player_entity_id,
        music_assistant_client=music_assistant_client,
        music_assistant_player_id=music_assistant_player_id,
        now=now,
    )
    items, errors = await async_collect_briefing(providers)
    return BriefingCollection(items, errors)
