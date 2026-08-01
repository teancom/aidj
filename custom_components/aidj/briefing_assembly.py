"""Assemble and collect the configured AI DJ briefing sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from homeassistant.core import HomeAssistant

from .briefing import (
    AqiEntityProvider,
    BriefingItem,
    BriefingProvider,
    CalendarEventProvider,
    FeedreaderEventProvider,
    QueueProvider,
    WeatherEntityProvider,
    async_collect_briefing,
)
from .const import (
    CONF_AQI,
    CONF_AQI_THRESHOLD,
    CONF_CALENDARS,
    CONF_FEEDS,
    CONF_WEATHER,
    PROVIDER_WEATHER,
)
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
    settings: Mapping[str, Any],
    *,
    weather_entity_id: str,
    player_entity_id: str,
    music_assistant_client: MusicAssistantClient | None,
    music_assistant_player_id: str | None = None,
) -> tuple[BriefingProvider, ...]:
    """Build providers in the stable order used by the station prompt."""
    feed_entity_ids = settings.get(CONF_FEEDS, [])
    calendar_entity_ids = settings.get(CONF_CALENDARS, [])
    aqi_entity_id = str(settings.get(CONF_AQI, "")).strip()
    aqi_threshold = float(settings.get(CONF_AQI_THRESHOLD, "101"))

    providers: list[BriefingProvider] = [WeatherEntityProvider(hass, weather_entity_id)]
    providers.extend(
        FeedreaderEventProvider(hass, entity_id, name=f"feedreader:{entity_id}")
        for entity_id in feed_entity_ids
    )
    providers.extend(
        CalendarEventProvider(hass, entity_id, name=f"calendar:{entity_id}")
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
    settings: Mapping[str, Any],
    *,
    weather_entity_id: str,
    player_entity_id: str,
    music_assistant_client: MusicAssistantClient | None,
    music_assistant_player_id: str | None = None,
) -> BriefingCollection:
    """Build and collect all configured sources for one station pass."""
    providers = build_briefing_providers(
        hass,
        settings,
        weather_entity_id=weather_entity_id,
        player_entity_id=player_entity_id,
        music_assistant_client=music_assistant_client,
        music_assistant_player_id=music_assistant_player_id,
    )
    items, errors = await async_collect_briefing(providers)
    return BriefingCollection(items, errors)
