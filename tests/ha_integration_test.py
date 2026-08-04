"""Runtime tests for the AI DJ integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import STATE_IDLE, STATE_PLAYING
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import event as event_helper
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import aidj
from custom_components.aidj.ha_music_assistant import HaMusicAssistantQueue
from custom_components.aidj.config_flow import _options_errors
from custom_components.aidj.music_assistant import MusicAssistantQueueAdapter, QueueMedia
from custom_components.aidj.music_context import QueueContext, TrackContext
from custom_components.aidj.briefing_assembly import BriefingCollection, build_briefing_providers
from custom_components.aidj.briefing_generation import BriefingGenerationService, GeneratedBriefing
from custom_components.aidj.prompt import (
    briefing_needs_grounding_retry,
    build_briefing_prompt,
    music_required_terms,
)
from custom_components.aidj.story import record_story, select_feed_story
from custom_components.aidj.briefing import (
    AqiEntityProvider,
    BriefingItem,
    CalendarEventProvider,
    EntityStateProvider,
    FeedreaderEventProvider,
    HaConversationBriefingGenerator,
    QueueProvider,
    WeatherEntityProvider,
    async_collect_briefing,
)
from custom_components.aidj.const import (
    ATTR_MESSAGE,
    CONF_AGENT,
    CONF_AQI,
    CONF_AQI_THRESHOLD,
    CONF_CALENDARS,
    CONF_FEEDS,
    CONF_HA_TOKEN,
    CONF_MA_PLAYER,
    CONF_MA_TOKEN,
    CONF_MA_URL,
    CONF_NAME,
    CONF_PLAYER,
    CONF_TTS,
    CONF_WEATHER,
    CONF_PERSONALITY,
    CONF_CUSTOM_PERSONALITY,
    CONF_JINGLE_URLS,
    CONF_STINGER_URLS,
    CONF_CADENCE_ENABLED,
    CONF_CADENCE_MIN_TRACKS,
    CONF_CADENCE_MAX_TRACKS,
    CONF_CADENCE_CONTENT,
    CADENCE_CONTENT_FULL,
    CADENCE_CONTENT_MUSIC,
    DEFAULT_PERSONALITY,
    DOMAIN,
    RECENT_STORY_LIMIT,
    SERVICE_QUEUE_ADD,
    SERVICE_ANNOUNCE,
    SERVICE_ANNOUNCE_NEXT,
    SERVICE_BRIEFING,
    SERVICE_BRIEFING_NEXT,
    StationSettings,
)
from music_assistant_models.enums import ContentType, MediaType, QueueOption



def test_station_settings_normalizes_effective_config() -> None:
    """Internal settings normalize whitespace, lists, and numeric values once."""
    settings = StationSettings.from_mapping(
        {
            CONF_NAME: "  Living Room Radio  ",
            CONF_PLAYER: " media_player.living_room ",
            CONF_TTS: " tts.openai ",
            CONF_MA_URL: " http://music-assistant:8095 ",
            CONF_MA_TOKEN: " ma-token ",
            CONF_HA_TOKEN: " ha-token ",
            CONF_MA_PLAYER: " wiim-player ",
            CONF_FEEDS: [" event.news ", "", 7],
            CONF_CALENDARS: (" calendar.home ",),
            CONF_AQI_THRESHOLD: "151",
            CONF_CADENCE_ENABLED: True,
            CONF_CADENCE_MIN_TRACKS: 2,
            CONF_CADENCE_MAX_TRACKS: 6,
            CONF_CADENCE_CONTENT: CADENCE_CONTENT_FULL,
        }
    )

    assert settings.name == "Living Room Radio"
    assert settings.player_entity_id == "media_player.living_room"
    assert settings.feed_entity_ids == ("event.news",)
    assert settings.calendar_entity_ids == ("calendar.home",)
    assert settings.aqi_relevance_threshold == 151.0
    assert settings.personality == DEFAULT_PERSONALITY
    assert "balanced radio-host" in settings.personality_instructions
    assert settings.cadence_enabled is True
    assert (settings.cadence_min_tracks, settings.cadence_max_tracks) == (2, 6)
    assert settings.cadence_content == CADENCE_CONTENT_FULL
    assert settings.music_assistant_enabled is True


def test_station_settings_normalizes_url_pools() -> None:
    """URL pools are clearable multiline values with one trimmed URL per line."""
    settings = StationSettings.from_mapping(
        {
            CONF_JINGLE_URLS: "  /local/intro.mp3  \n\n/local/second.mp3\n  ",
            CONF_STINGER_URLS: [" /local/outro.mp3 ", "", " /local/final.mp3 "],
        }
    )

    assert settings.jingle_urls == ("/local/intro.mp3", "/local/second.mp3")
    assert settings.stinger_urls == ("/local/outro.mp3", "/local/final.mp3")
    assert StationSettings.from_mapping(
        {CONF_JINGLE_URLS: " \n ", CONF_STINGER_URLS: ""}
    ).jingle_urls == ()


def test_station_settings_normalizes_personality() -> None:
    """Preset and custom personalities produce safe prompt instructions."""
    preset = StationSettings.from_mapping({CONF_PERSONALITY: "crisp_direct"})
    custom = StationSettings.from_mapping(
        {
            CONF_PERSONALITY: "custom",
            CONF_CUSTOM_PERSONALITY: "  Sound curious, but never breathless.  ",
        }
    )
    malformed = StationSettings.from_mapping({CONF_PERSONALITY: "future_unknown"})
    empty_custom = StationSettings.from_mapping({CONF_PERSONALITY: "custom"})

    assert "compact factual sentences" in preset.personality_instructions
    assert custom.personality_instructions == "Sound curious, but never breathless."
    assert malformed.personality == DEFAULT_PERSONALITY
    assert empty_custom.personality == DEFAULT_PERSONALITY


def test_station_settings_defaults_malformed_optional_values() -> None:
    """Malformed legacy optional values cannot leak into runtime consumers."""
    settings = StationSettings.from_mapping(
        {
            CONF_NAME: "AI DJ",
            CONF_PLAYER: "media_player.living_room",
            CONF_TTS: "tts.openai",
            CONF_FEEDS: "event.not-a-list",
            CONF_AQI_THRESHOLD: "not-a-number",
            CONF_MA_URL: "http://music-assistant:8095",
        }
    )

    assert settings.feed_entity_ids == ()
    assert settings.calendar_entity_ids == ()
    assert settings.aqi_relevance_threshold == 101.0
    assert settings.cadence_enabled is False
    assert (settings.cadence_min_tracks, settings.cadence_max_tracks) == (3, 5)
    assert settings.cadence_content == CADENCE_CONTENT_MUSIC
    assert settings.music_assistant_enabled is False
    assert StationSettings.from_mapping({CONF_AQI_THRESHOLD: "nan"}).aqi_relevance_threshold == 101.0
    assert StationSettings.from_mapping({CONF_AQI_THRESHOLD: "501"}).aqi_relevance_threshold == 101.0


@pytest.mark.asyncio
async def test_music_assistant_queue_adapter_inserts_next_without_replacing() -> None:
    """Native MA transport uses one add-only NEXT queue operation."""
    class Media:
        item_id = "http://ha.local/tts/clip.mp3"
        uri = "builtin://track/http://ha.local/tts/clip.mp3"

    class Item:
        queue_item_id = "aidj-item-1"
        name = "AI DJ Announcement"
        uri = Media.uri
        media_item = Media()

    class Queue:
        queue_id = "queue-1"
        current_item = None
        next_item = None
        elapsed_time = 12

    queues = type("Queues", (), {})()
    queues.get_active_queue = AsyncMock(return_value=Queue())
    queues.play_media = AsyncMock()
    queues.get_queue_items = AsyncMock(side_effect=[[], [Item()]])
    client = type("Client", (), {"player_queues": queues})()

    item_id = await MusicAssistantQueueAdapter(client, "player-1").async_insert_next(
        " http://ha.local/tts/clip.mp3 "
    )

    assert item_id == "aidj-item-1"
    queues.play_media.assert_awaited_once()
    call = queues.play_media.await_args.kwargs
    assert call["queue_id"] == "queue-1"
    assert call["option"] is QueueOption.NEXT
    track = call["media"][0]
    assert track.name == "AI DJ Announcement"
    assert track.item_id == "http://ha.local/tts/clip.mp3"
    assert track.uri == "builtin://sound_effect/http://ha.local/tts/clip.mp3"
    assert track.media_type is MediaType.SOUND_EFFECT
    assert track.duration == 0
    assert track.provider_mappings.pop().audio_format.content_type is ContentType.MP3


@pytest.mark.asyncio
async def test_music_assistant_queue_adapter_inserts_ordered_sequence() -> None:
    """Native MA transport inserts all effects in one ordered queue operation."""
    class QueueItem:
        def __init__(self, queue_item_id: str, name: str, uri: str) -> None:
            self.queue_item_id = queue_item_id
            self.name = name
            self.uri = uri
            self.media_item = None

    class Queue:
        queue_id = "queue-1"

    queues = type("Queues", (), {})()
    queues.get_active_queue = AsyncMock(return_value=Queue())
    queues.play_media = AsyncMock()
    inserted_items = [
        QueueItem(
            "stinger-id",
            "AI DJ Stinger",
            "builtin://sound_effect/http://ha.local/stinger.mp3",
        ),
        QueueItem(
            "jingle-id",
            "AI DJ Jingle",
            "builtin://sound_effect/http://ha.local/jingle.mp3",
        ),
    ]
    queues.get_queue_items = AsyncMock(side_effect=[[], inserted_items])
    client = type("Client", (), {"player_queues": queues})()

    queue_ids = await MusicAssistantQueueAdapter(client, "player-1").async_insert_sequence(
        [
            QueueMedia(
                "http://ha.local/jingle.mp3",
                "AI DJ Jingle",
                duration=2,
                content_type=ContentType.MP3,
            ),
            QueueMedia(
                "http://ha.local/stinger.mp3",
                "AI DJ Stinger",
                duration=3,
                content_type=ContentType.MP3,
            ),
        ]
    )

    assert queue_ids == ["jingle-id", "stinger-id"]
    queues.play_media.assert_awaited_once()
    call = queues.play_media.await_args.kwargs
    assert call["option"] is QueueOption.NEXT
    assert [track.name for track in call["media"]] == ["AI DJ Jingle", "AI DJ Stinger"]
    assert [track.duration for track in call["media"]] == [2, 3]
    assert all(track.media_type is MediaType.SOUND_EFFECT for track in call["media"])


@pytest.mark.asyncio
async def test_ha_music_assistant_queue_targets_configured_player(
    hass: HomeAssistant,
) -> None:
    """HA fallback queue operations ignore other players in the response."""
    hass.states.async_set("media_player.living_room_streamer_2", STATE_IDLE)
    get_queue = AsyncMock(
        return_value={
            "service_response": {
                "media_player.other": {
                    "next_item": {"media_item": {"uri": "library://track/duplicate"}}
                },
                "media_player.living_room_streamer_2": {
                    "next_item": {"media_item": {"uri": "library://track/target"}}
                },
            }
        }
    )
    play_media = AsyncMock()
    hass.services.async_register(
        "music_assistant", "get_queue", get_queue, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register("music_assistant", "play_media", play_media)

    queue = HaMusicAssistantQueue(hass, "media_player.living_room_streamer_2")
    assert await queue.async_add("library://track/duplicate") is True
    assert await queue.async_add("library://track/target") is False
    play_media.assert_awaited_once()
    assert play_media.await_args.args[0].data["media_id"] == "library://track/duplicate"


@pytest.mark.asyncio
async def test_entity_state_provider_normalizes_existing_entities(
    hass: HomeAssistant,
) -> None:
    """The HA provider emits stable facts and skips missing entities."""
    hass.states.async_set(
        "sensor.weather_temperature",
        "23",
        {"friendly_name": "Outdoor Temperature", "unit_of_measurement": "°F"},
    )
    provider = EntityStateProvider(
        hass,
        ("sensor.weather_temperature", "sensor.missing"),
    )

    items = await provider.async_collect()

    assert items == [
        BriefingItem(
            provider="home_assistant",
            title="Outdoor Temperature",
            summary="Outdoor Temperature: 23",
            occurred_at=items[0].occurred_at,
            source="sensor.weather_temperature",
        )
    ]


@pytest.mark.asyncio
async def test_weather_provider_normalizes_common_weather_attributes(
    hass: HomeAssistant,
) -> None:
    """Weather facts retain useful units while staying provider-neutral."""
    hass.states.async_set(
        "weather.forecast_home",
        "sunny",
        {
            "friendly_name": "Forecast Home",
            "temperature": 89,
            "temperature_unit": "°F",
            "humidity": 46,
            "wind_speed": 7.15,
            "wind_speed_unit": "mph",
        },
    )

    items = await WeatherEntityProvider(hass, "weather.forecast_home").async_collect()

    assert len(items) == 1
    assert items[0].summary == (
        "Forecast Home: conditions: sunny, temperature: 89°F, "
        "humidity: 46%, wind: 7.15mph"
    )
    assert items[0].source == "weather.forecast_home"


@pytest.mark.asyncio
async def test_weather_provider_includes_today_forecast_before_noon(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Morning weather context includes current conditions and today's forecast."""
    hass.states.async_set(
        "weather.forecast_home",
        "sunny",
        {
            "friendly_name": "Forecast Home",
            "temperature": 72,
            "temperature_unit": "°F",
            "humidity": 45,
            "wind_speed": 4,
            "wind_speed_unit": "mph",
        },
    )
    forecast = AsyncMock(
        return_value={
            "weather.forecast_home": {
                "forecast": [
                    {"datetime": "2026-08-01", "condition": "sunny", "temperature": 80, "templow": 61, "precipitation_probability": 5},
                    {"datetime": "2026-08-02", "condition": "cloudy", "temperature": 75, "templow": 60},
                ]
            }
        }
    )
    hass.services.async_register("weather", "get_forecasts", forecast, supports_response=SupportsResponse.ONLY)
    monkeypatch.setattr(
        "custom_components.aidj.briefing.dt_util.now",
        lambda: datetime(2026, 8, 1, 9, tzinfo=timezone.utc),
    )

    items = await WeatherEntityProvider(hass, "weather.forecast_home").async_collect()

    assert "temperature: 72°F" in items[0].summary
    assert "forecast today" in items[0].summary
    assert "forecast tomorrow" not in items[0].summary
    assert forecast.await_args.args[0].data == {"type": "daily", "entity_id": "weather.forecast_home"}


@pytest.mark.asyncio
async def test_weather_provider_includes_tomorrow_after_noon(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Afternoon weather context includes today's and tomorrow's forecast."""
    hass.states.async_set("weather.forecast_home", "sunny", {"friendly_name": "Forecast Home"})
    forecast = AsyncMock(
        return_value={"weather.forecast_home": {"forecast": [{"condition": "sunny"}, {"condition": "cloudy"}]}}
    )
    hass.services.async_register("weather", "get_forecasts", forecast, supports_response=SupportsResponse.ONLY)
    monkeypatch.setattr(
        "custom_components.aidj.briefing.dt_util.now",
        lambda: datetime(2026, 8, 1, 21, tzinfo=timezone.utc),
    )

    items = await WeatherEntityProvider(hass, "weather.forecast_home").async_collect()

    assert "forecast today" in items[0].summary
    assert "forecast tomorrow" in items[0].summary


@pytest.mark.asyncio
async def test_weather_provider_keeps_current_conditions_when_forecast_fails(
    hass: HomeAssistant,
) -> None:
    """A forecast outage does not discard current weather context."""
    hass.states.async_set("weather.forecast_home", "rainy", {"friendly_name": "Forecast Home", "temperature": 65})
    hass.services.async_register(
        "weather", "get_forecasts", AsyncMock(side_effect=RuntimeError("unavailable"))
    )

    items = await WeatherEntityProvider(hass, "weather.forecast_home").async_collect()

    assert len(items) == 1
    assert "conditions: rainy" in items[0].summary
    assert "temperature: 65" in items[0].summary


def test_briefing_provider_assembly_preserves_source_order_and_settings(
    hass: HomeAssistant,
) -> None:
    """Station assembly builds weather, feeds, calendars, AQI, then queue."""
    providers = build_briefing_providers(
        hass,
        StationSettings.from_mapping(
            {
                CONF_FEEDS: ["event.local_news"],
                CONF_CALENDARS: ["calendar.david", "calendar.home_calendar"],
                CONF_AQI: "sensor.outdoor_us_aqi",
                CONF_AQI_THRESHOLD: "101",
            }
        ),
        weather_entity_id="weather.forecast_home",
        player_entity_id="media_player.living_room_streamer_2",
        music_assistant_client=None,
    )

    assert [provider.name for provider in providers] == [
        "weather",
        "feedreader:event.local_news",
        "calendar:calendar.david",
        "calendar:calendar.home_calendar",
        "air_quality",
        "music_assistant_queue",
    ]
    assert providers[4].relevance_threshold == 101
    assert providers[5].player_entity_id == "media_player.living_room_streamer_2"


@pytest.mark.asyncio
async def test_queue_provider_normalizes_current_and_next_tracks(
    hass: HomeAssistant,
) -> None:
    """Fallback queue context uses structured current and next media facts."""
    get_queue = AsyncMock(
        return_value={
            "service_response": {
                "media_player.living_room_streamer_2": {
                    "queue_id": "queue-1",
                    "current_index": 26,
                    "current_item": {
                        "queue_item_id": "item-26",
                        "media_item": {
                            "uri": "library://track/26",
                            "name": "Current Song",
                            "artists": [{"name": "Current Artist"}],
                        },
                    },
                    "next_item": {
                        "queue_item_id": "item-27",
                        "media_item": {
                            "uri": "library://track/27",
                            "name": "Next Song",
                            "artists": [{"name": "Next Artist"}],
                        },
                    },
                }
            }
        }
    )
    class Client:
        class Queues:
            async def get_active_queue(self, player_id: str):
                assert player_id == "wiim-player"
                return type("Queue", (), {"queue_id": "queue-1", "current_index": 26})()

            async def get_queue_items(self, queue_id: str, *, limit: int, offset: int):
                assert (queue_id, limit, offset) == ("queue-1", 7, 23)
                def item(absolute_index: int):
                    title = (
                        "Current Song" if absolute_index == 26
                        else "Next Song" if absolute_index == 27
                        else f"Track {absolute_index}"
                    )
                    artist = (
                        "Current Artist" if absolute_index == 26
                        else "Next Artist" if absolute_index == 27
                        else f"Artist {absolute_index}"
                    )
                    return type(
                        "Item",
                        (),
                        {
                            # The real MA client currently loses the absolute index
                            # when deserializing a paginated queue response.
                            "index": 0,
                            "queue_item_id": f"item-{absolute_index}",
                            "to_dict": lambda self: {
                                "queue_item_id": self.queue_item_id,
                                "media_item": {
                                    "uri": f"library://track/{absolute_index}",
                                    "name": title,
                                    "artists": [{"name": artist}],
                                },
                            },
                        },
                    )()
                return [item(index) for index in range(23, 30)]

        player_queues = Queues()

    hass.services.async_register(
        "music_assistant",
        "get_queue",
        get_queue,
        supports_response=SupportsResponse.ONLY,
    )

    items = await QueueProvider(
        hass,
        "media_player.living_room_streamer_2",
        Client(),
        "wiim-player",
    ).async_collect()

    assert [item.title for item in items] == ["Music context"]
    assert items[0].music_context == QueueContext(
        current=TrackContext("Current Song", artist="Current Artist"),
        previous=(
            TrackContext("Track 23", artist="Artist 23"),
            TrackContext("Track 24", artist="Artist 24"),
            TrackContext("Track 25", artist="Artist 25"),
        ),
        next=(
            TrackContext("Next Song", artist="Next Artist"),
            TrackContext("Track 28", artist="Artist 28"),
            TrackContext("Track 29", artist="Artist 29"),
        ),
    )
    call: ServiceCall = get_queue.await_args.args[0]
    assert call.data == {"entity_id": "media_player.living_room_streamer_2"}



@pytest.mark.asyncio
async def test_queue_provider_collects_native_three_track_window(hass: HomeAssistant) -> None:
    """Native MA queue data yields up to three structured tracks on each side."""
    class Client:
        class Queues:
            def __init__(self) -> None:
                self.active_queue_player_id = None

            async def get_active_queue(self, player_id: str):
                self.active_queue_player_id = player_id
                return type("Queue", (), {"queue_id": "queue-1", "current_index": 3})()

            async def get_queue_items(self, queue_id: str, *, limit: int, offset: int):
                assert (queue_id, limit, offset) == ("queue-1", 7, 0)
                def item(index: int, title: str, genre: str | None = None):
                    return type(
                        "Item",
                        (),
                        {
                            "index": index,
                            "queue_item_id": f"item-{index}",
                            "to_dict": lambda self: {
                                "queue_item_id": self.queue_item_id,
                                "media_item": {
                                    "uri": f"library://track/{index}",
                                    "name": title,
                                    "artists": [{"name": f"Artist {index}"}],
                                    "album": {"name": f"Album {index}", "year": 2000 + index},
                                    "metadata": {"genres": [genre]} if genre else {},
                                },
                                "name": title,
                            },
                        },
                    )()
                return [item(i, f"Track {i}", "micro-genre" if i == 2 else None) for i in range(7)]

        player_queues = Queues()

    get_queue = AsyncMock(
        return_value={
            "service_response": {
                "media_player.living_room_streamer_2": {
                    "queue_id": "queue-1",
                    "current_index": 3,
                    "current_item": {
                        "queue_item_id": "item-3",
                        "media_item": {
                            "uri": "library://track/3",
                            "name": "Track 3",
                            "artists": [{"name": "Artist 3"}],
                        },
                    },
                    "next_item": {
                        "queue_item_id": "item-4",
                        "media_item": {
                            "uri": "library://track/4",
                            "name": "Track 4",
                            "artists": [{"name": "Artist 4"}],
                        },
                    },
                }
            }
        }
    )
    hass.services.async_register(
        "music_assistant", "get_queue", get_queue, supports_response=SupportsResponse.ONLY
    )

    client = Client()
    items = await QueueProvider(
        hass,
        "media_player.living_room_streamer_2",
        client,
        "wiim_uuid:FF98F09C-3C05-E614-5D61-85A6FF98F09C",
    ).async_collect()

    assert client.player_queues.active_queue_player_id == (
        "wiim_uuid:FF98F09C-3C05-E614-5D61-85A6FF98F09C"
    )
    assert [item.title for item in items] == ["Music context"]
    context = items[0].music_context
    assert context is not None
    assert context.current == TrackContext(
        "Track 3", artist="Artist 3", album="Album 3", year=2003
    )
    assert [track.track for track in context.previous] == ["Track 0", "Track 1", "Track 2"]
    assert context.previous[-1].genre == "micro-genre"
    assert context.previous[-1].year == 2002
    assert [track.track for track in context.next] == ["Track 4", "Track 5", "Track 6"]


def test_music_context_accepts_nested_music_assistant_model_objects() -> None:
    """Native queue models need not eagerly serialize nested media objects."""
    from custom_components.aidj.music_context import track_context
    from custom_components.aidj.queue_snapshot import queue_item_identity

    artist = type("Artist", (), {"name": "  Cass McCombs  "})()
    media = type(
        "Media",
        (),
        {
            "to_dict": lambda self: {
                "uri": "library://track/80683",
                "name": "My Pilgrim Dear",
                "artists": [artist],
            }
        },
    )()
    item = type(
        "QueueItem",
        (),
        {
            "to_dict": lambda self: {
                "queue_item_id": "queue-item-1",
                "name": "Cass McCombs - My Pilgrim Dear",
                "media_item": media,
            }
        },
    )()

    assert track_context(item) == TrackContext(
        "My Pilgrim Dear", artist="Cass McCombs"
    )
    identity = queue_item_identity(item)
    assert identity is not None
    assert identity.uri == "library://track/80683"
    assert identity.artists == ("Cass McCombs",)


@pytest.mark.asyncio
async def test_queue_provider_fails_closed_when_ha_and_native_disagree(
    hass: HomeAssistant,
) -> None:
    """A disagreement in current track identity must not produce music facts."""
    get_queue = AsyncMock(
        return_value={
            "service_response": {
                "media_player.living_room_streamer_2": {
                    "queue_id": "queue-1",
                    "current_index": 3,
                    "current_item": {
                        "queue_item_id": "item-3",
                        "media_item": {
                            "uri": "library://track/wrong",
                            "name": "Wrong Song",
                            "artists": [{"name": "Wrong Artist"}],
                        },
                    },
                    "next_item": {
                        "queue_item_id": "item-4",
                        "media_item": {
                            "uri": "library://track/4",
                            "name": "Track 4",
                            "artists": [{"name": "Artist 4"}],
                        },
                    },
                }
            }
        }
    )
    hass.services.async_register(
        "music_assistant", "get_queue", get_queue, supports_response=SupportsResponse.ONLY
    )

    class Queues:
        async def get_active_queue(self, player_id: str):
            return type("Queue", (), {"queue_id": "queue-1", "current_index": 3})()

        async def get_queue_items(self, queue_id: str, *, limit: int, offset: int):
            assert (queue_id, limit, offset) == ("queue-1", 7, 0)
            def item(index: int, title: str, artist: str):
                return type(
                    "Item",
                    (),
                    {
                        "index": index,
                        "queue_item_id": f"item-{index}",
                        "to_dict": lambda self: {
                            "queue_item_id": self.queue_item_id,
                            "media_item": {
                                "uri": f"library://track/{index}",
                                "name": title,
                                "artists": [{"name": artist}],
                            },
                        },
                    },
                )()
            return [item(3, "Track 3", "Artist 3"), item(4, "Track 4", "Artist 4")]

    client = type("Client", (), {"player_queues": Queues()})()
    items = await QueueProvider(
        hass, "media_player.living_room_streamer_2", client, "wiim-player"
    ).async_collect()

    assert items == []


@pytest.mark.asyncio
async def test_feedreader_provider_normalizes_latest_event(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feedreader event data becomes compact local-news context."""
    monkeypatch.setattr(
        "custom_components.aidj.briefing.dt_util.now",
        lambda: datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
    )
    hass.states.async_set(
        "event.san_diego_news",
        "2026-07-31T12:00:00+00:00",
        {
            "event_type": "feedreader",
            "event_data": {
                "title": "San Diego council approves new park",
                "description": "  A local update with   extra whitespace.  ",
                "published": "2026-08-01T11:30:00+00:00",
                "link": "https://example.test/news",
            },
        },
    )

    items = await FeedreaderEventProvider(hass, "event.san_diego_news").async_collect()

    assert items == [
        BriefingItem(
            provider="feedreader",
            title="San Diego council approves new park",
            summary=(
                "Latest local news: San Diego council approves new park. "
                "A local update with extra whitespace."
            ),
            occurred_at=items[0].occurred_at,
            source="https://example.test/news",
            identity="https://example.test/news",
        )
    ]


@pytest.mark.asyncio
async def test_feedreader_provider_uses_native_coordinator_entry_pool(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feedreader's configured max_entries pool is used instead of only latest."""
    monkeypatch.setattr(
        "custom_components.aidj.briefing.dt_util.now",
        lambda: datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
    )
    feed_entry = MockConfigEntry(domain="feedreader", data={"url": "https://example.test/feed"})
    feed_entry.add_to_hass(hass)
    feed_entry.runtime_data = type(
        "FeedCoordinator",
        (),
        {
            "url": "https://example.test/feed",
            "data": [
                {
                    "title": "Story one",
                    "published": "2026-08-01T11:00:00+00:00",
                    "link": "https://example.test/one",
                },
                {
                    "title": "Story two",
                    "published": "2026-07-31T11:00:00+00:00",
                    "link": "https://example.test/two",
                },
            ],
        },
    )()
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "event", "feedreader", "feedreader_latest_feed", config_entry=feed_entry
    )
    entity_id = registry.async_get_entity_id(
        "event", "feedreader", "feedreader_latest_feed"
    )
    assert entity_id is not None
    hass.states.async_set(entity_id, "2026-07-31T12:00:00+00:00", {"event_type": "feedreader"})

    items = await FeedreaderEventProvider(hass, entity_id).async_collect()

    assert [item.title for item in items] == ["Story one", "Story two"]
    assert [item.identity for item in items] == [
        "https://example.test/one",
        "https://example.test/two",
    ]


@pytest.mark.asyncio
async def test_feedreader_provider_skips_stale_missing_and_future_articles(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feedreader facts require an article date inside the recent window."""
    monkeypatch.setattr(
        "custom_components.aidj.briefing.dt_util.now",
        lambda: datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
    )
    feed_entry = MockConfigEntry(domain="feedreader", data={"url": "https://example.test/feed"})
    feed_entry.add_to_hass(hass)
    feed_entry.runtime_data = type(
        "FeedCoordinator",
        (),
        {
            "url": "https://example.test/feed",
            "data": [
                {
                    "title": "Recent",
                    "published_parsed": (2026, 8, 1, 10, 0, 0, 5, 213, 0),
                    "link": "https://example.test/recent",
                },
                {"title": "Stale", "published": "2026-04-01T10:00:00+00:00"},
                {"title": "Undated", "link": "https://example.test/undated"},
                {"title": "Future", "published": "2026-09-01T10:00:00+00:00"},
            ],
        },
    )()
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "event", "feedreader", "feedreader_dated_feed", config_entry=feed_entry
    )
    entity_id = registry.async_get_entity_id("event", "feedreader", "feedreader_dated_feed")
    assert entity_id is not None
    hass.states.async_set(entity_id, "2026-08-01T12:00:00+00:00", {"event_type": "feedreader"})

    items = await FeedreaderEventProvider(hass, entity_id).async_collect()

    assert [item.title for item in items] == ["Recent"]
    assert items[0].identity == "https://example.test/recent"


@pytest.mark.asyncio
async def test_feedreader_provider_skips_missing_or_empty_event(
    hass: HomeAssistant,
) -> None:
    """A missing or empty feed entity is an optional-provider no-op."""
    assert await FeedreaderEventProvider(hass, "event.missing").async_collect() == []
    hass.states.async_set("event.empty_feed", "unknown", {"event_type": "feedreader"})
    assert await FeedreaderEventProvider(hass, "event.empty_feed").async_collect() == []


@pytest.mark.asyncio
async def test_calendar_provider_collects_upcoming_timed_and_all_day_events(
    hass: HomeAssistant,
) -> None:
    """Calendar facts come from the native get_events service response."""
    hass.states.async_set(
        "calendar.david",
        "on",
        {"friendly_name": "David & Beloved"},
    )
    get_events = AsyncMock(
        return_value={
            "calendar.david": {
                "events": [
                    {
                        "summary": "Spider-Man 🕷️🕸️",
                        "start": {"dateTime": "2026-08-01T14:10:00-07:00"},
                        "end": {"dateTime": "2026-08-01T16:45:00-07:00"},
                        "location": "Angelika Film Center & Café - Carmel Mountain",
                        "uid": "spider-man-1",
                    },
                    {
                        "summary": "Birthday",
                        "start": {"date": "2026-08-02"},
                        "end": {"date": "2026-08-03"},
                        "uid": "birthday-1",
                    },
                ]
            }
        }
    )
    hass.services.async_register(
        "calendar",
        "get_events",
        get_events,
        supports_response=SupportsResponse.ONLY,
    )

    items = await CalendarEventProvider(hass, "calendar.david").async_collect()

    assert [item.title for item in items] == ["Spider-Man 🕷️🕸️", "Birthday"]
    assert "David & Beloved: Spider-Man 🕷️🕸️" in items[0].summary
    assert "starts 2026-08-01T14:10:00-07:00" in items[0].summary
    assert "location: Angelika Film Center & Café - Carmel Mountain" in items[0].summary
    assert "all day on 2026-08-02" in items[1].summary
    assert items[0].identity == "calendar.david:spider-man-1"
    call: ServiceCall = get_events.await_args.args[0]
    assert call.data["entity_id"] == "calendar.david"
    assert call.data["end_date_time"]


@pytest.mark.asyncio
async def test_calendar_provider_skips_missing_and_distant_events(
    hass: HomeAssistant,
) -> None:
    """Calendar relevance keeps only today/tomorrow events."""
    hass.states.async_set("calendar.home", "on", {"friendly_name": "Home"})
    today = dt_util.now().date()
    get_events = AsyncMock(
        return_value={
            "calendar.home": {
                "events": [
                    {"summary": "Today", "start": {"date": today.isoformat()}},
                    {
                        "summary": "Tomorrow",
                        "start": {"date": (today + timedelta(days=1)).isoformat()},
                    },
                    {
                        "summary": "Later",
                        "start": {"date": (today + timedelta(days=5)).isoformat()},
                    },
                ]
            }
        }
    )
    hass.services.async_register(
        "calendar", "get_events", get_events, supports_response=SupportsResponse.ONLY
    )

    items = await CalendarEventProvider(hass, "calendar.home").async_collect()

    assert [item.title for item in items] == ["Today", "Tomorrow"]
    assert await CalendarEventProvider(hass, "calendar.missing").async_collect() == []


@pytest.mark.asyncio
async def test_aqi_provider_only_returns_relevant_air_quality(
    hass: HomeAssistant,
) -> None:
    """Good AQI is silent; moderate and worse AQI is interpreted."""
    hass.states.async_set("sensor.outdoor_us_aqi", "50", {"friendly_name": "Outdoor AQI"})
    assert await AqiEntityProvider(hass, "sensor.outdoor_us_aqi").async_collect() == []

    hass.states.async_set("sensor.outdoor_us_aqi", "125", {"friendly_name": "Outdoor AQI"})
    items = await AqiEntityProvider(hass, "sensor.outdoor_us_aqi").async_collect()

    assert len(items) == 1
    assert items[0].summary == "Outdoor AQI: AQI 125, unhealthy for sensitive groups"


def test_build_briefing_prompt_requires_specific_music_references() -> None:
    """Prompt instructions require completed and upcoming track references."""
    item = BriefingItem(
        provider="music_assistant_queue",
        title="Music context",
        summary="Verified Music Assistant queue context",
        music_context=QueueContext(
            current=TrackContext("Current Song"),
            next=(TrackContext("Next Song"),),
        ),
    )

    prompt = build_briefing_prompt([item])

    assert "identify the completed song or artist" in prompt
    assert "coming-up-next reference" in prompt
    assert "Completed/current track: Current Song" in prompt
    assert "Upcoming track: Next Song" in prompt
    assert music_required_terms([item]) == ("Current Song", "Next Song")
    assert briefing_needs_grounding_retry("You heard Current Song; Next Song is coming up.", ("Current Song", "Next Song")) is False
    assert briefing_needs_grounding_retry("You heard [insert completed song title].", ("Current Song", "Next Song")) is True


def test_build_briefing_prompt_layers_task_personality_and_facts() -> None:
    """One-off task and station personality retain shared rules and facts."""
    item = BriefingItem(provider="weather", title="Weather", summary="Weather: sunny")

    prompt = build_briefing_prompt(
        [item],
        "Keep this break to one sentence.",
        "Use calm, flowing sentences.",
    )

    assert prompt.startswith("Keep this break to one sentence.\n")
    assert "Presentation personality (style only;" in prompt
    assert "Use calm, flowing sentences." in prompt
    assert "write like a human local radio DJ" in prompt
    assert "Facts:\n- Weather: sunny" in prompt


@pytest.mark.asyncio
async def test_conversation_generator_extracts_plain_speech(
    hass: HomeAssistant,
) -> None:
    """The generator calls HA's response-capable conversation action."""
    conversation = AsyncMock(
        return_value={
            "response": {
                "speech": {"plain": {"speech": "  A concise briefing.  "}}
            }
        }
    )
    hass.services.async_register(
        "conversation",
        "process",
        conversation,
        supports_response=SupportsResponse.ONLY,
    )

    generator = HaConversationBriefingGenerator(hass, "conversation.openai_conversation")
    result = await generator.async_generate("  Summarize the facts.  ")

    assert result == "A concise briefing."
    call: ServiceCall = conversation.await_args.args[0]
    assert call.data == {
        "text": "Summarize the facts.",
        "agent_id": "conversation.openai_conversation",
    }


@pytest.mark.asyncio
async def test_conversation_generator_rejects_empty_speech(
    hass: HomeAssistant,
) -> None:
    """Malformed provider output cannot become spoken content."""
    conversation = AsyncMock(return_value={"response": {"speech": {}}})
    hass.services.async_register(
        "conversation",
        "process",
        conversation,
        supports_response=SupportsResponse.ONLY,
    )

    generator = HaConversationBriefingGenerator(hass, "conversation.openai_conversation")

    with pytest.raises(Exception, match="returned no plain speech"):
        await generator.async_generate("Summarize the facts.")


@pytest.mark.asyncio
async def test_briefing_collection_isolates_provider_failures(
    hass: HomeAssistant,
) -> None:
    """One failed optional provider does not discard successful facts."""
    class FailingProvider:
        name = "broken"

        async def async_collect(self) -> list[BriefingItem]:
            raise RuntimeError("feed unavailable")

    hass.states.async_set("sensor.temperature", "70")
    items, errors = await async_collect_briefing(
        (
            EntityStateProvider(hass, ("sensor.temperature",)),
            FailingProvider(),
        )
    )

    assert len(items) == 1
    assert items[0].source == "sensor.temperature"
    assert errors == {"broken": "feed unavailable"}


@pytest.mark.asyncio
async def test_config_flow_discovers_named_ma_players_and_rejects_duplicate(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """Setup authenticates once, then stores the selected MA player ID."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    user_schema = result["data_schema"].schema
    assert user_schema["music_assistant_token"].config["type"] == "password"
    assert user_schema["home_assistant_token"].config["type"] == "password"

    with patch(
        "custom_components.aidj.config_flow._async_get_ma_players",
        new=AsyncMock(return_value=[{"value": "wiim-player", "label": "Living Room Streamer (WiiM Pro)"}]),
    ), patch(
        "custom_components.aidj.runtime.AiDjRuntime.async_start_music_assistant",
        new=AsyncMock(),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "music_assistant_url": "http://ma.local:8095",
                "music_assistant_token": "ma-secret",
                "home_assistant_token": "ha-secret",
            },
        )
        assert result["type"] == "form"
        assert result["step_id"] == "station"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_NAME: "Living Room Radio",
                CONF_PLAYER: "media_player.living_room_streamer",
                CONF_TTS: "tts.openai_tts",
                "music_assistant_player": "wiim-player",
            },
        )

    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert result["title"] == "Living Room Radio"
    assert result["data"]["music_assistant_player"] == "wiim-player"
    assert result["data"]["music_assistant_token"] == "ma-secret"
    assert result["data"]["home_assistant_token"] == "ha-secret"

    duplicate = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    assert duplicate["type"] == "form"


@pytest.mark.asyncio
async def test_setup_failure_cleans_up_partial_runtime(
    hass: HomeAssistant,
) -> None:
    """A failed platform setup cannot leave listeners or a published runtime behind."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
    )
    entry.add_to_hass(hass)
    await aidj.async_setup(hass, {})
    unload = Mock()

    with patch(
        "custom_components.aidj.runtime.AiDjRuntime.async_start_music_assistant",
        new=AsyncMock(),
    ), patch(
        "custom_components.aidj.runtime.AiDjRuntime.async_initialize_controller",
        new=AsyncMock(),
    ), patch(
        "custom_components.aidj.runtime.AiDjRuntime.async_unload",
        new=unload,
    ), patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        new=AsyncMock(side_effect=RuntimeError("platform setup failed")),
    ):
        with pytest.raises(RuntimeError, match="platform setup failed"):
            await aidj.async_setup_entry(hass, entry)

    assert entry.entry_id not in hass.data[DOMAIN]
    unload.assert_called_once_with()


@pytest.mark.asyncio
async def test_migrates_legacy_credentials_into_entry_data(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """Legacy options are moved to entry data while station options remain local."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer",
            CONF_TTS: "tts.openai_tts",
        },
        options={
            CONF_MA_URL: "http://ma.local:8095",
            CONF_MA_TOKEN: "ma-secret",
            CONF_HA_TOKEN: "ha-secret",
            CONF_MA_PLAYER: "wiim-player",
            CONF_WEATHER: "weather.forecast_home",
        },
    )
    entry.add_to_hass(hass)

    assert await aidj.async_migrate_entry(hass, entry)

    assert entry.version == 2
    assert entry.data[CONF_MA_URL] == "http://ma.local:8095"
    assert entry.data[CONF_MA_TOKEN] == "ma-secret"
    assert entry.data[CONF_HA_TOKEN] == "ha-secret"
    assert entry.data[CONF_MA_PLAYER] == "wiim-player"
    assert entry.options == {CONF_WEATHER: "weather.forecast_home"}


@pytest.mark.asyncio
async def test_options_flow_exposes_briefing_source_fields(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """Existing stations expose optional briefing source settings."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
        options={
            CONF_AQI_THRESHOLD: "nan",
            CONF_PERSONALITY: "calm_intimate",
            CONF_CUSTOM_PERSONALITY: "Can this be deleted?",
            CONF_JINGLE_URLS: " /local/intro.mp3 \n\n/local/second.mp3 ",
            CONF_STINGER_URLS: " /local/outro.mp3 ",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert CONF_WEATHER in result["data_schema"].schema
    assert CONF_AGENT in result["data_schema"].schema
    assert CONF_FEEDS in result["data_schema"].schema
    assert result["data_schema"].schema[CONF_FEEDS].config["multiple"] is True
    assert CONF_CALENDARS in result["data_schema"].schema
    assert result["data_schema"].schema[CONF_CALENDARS].config["multiple"] is True
    assert CONF_AQI in result["data_schema"].schema
    assert CONF_AQI_THRESHOLD in result["data_schema"].schema
    assert CONF_PERSONALITY in result["data_schema"].schema
    personality_options = result["data_schema"].schema[CONF_PERSONALITY].config["options"]
    assert {option["value"] for option in personality_options} == {
        "balanced",
        "bright_brisk",
        "refined_reflective",
        "warm_neighborly",
        "dry_understated",
        "calm_intimate",
        "crisp_direct",
        "custom",
    }
    assert CONF_CUSTOM_PERSONALITY in result["data_schema"].schema
    assert result["data_schema"].schema[CONF_CUSTOM_PERSONALITY].config["multiline"] is True
    custom_marker = next(
        marker
        for marker in result["data_schema"].schema
        if marker.schema == CONF_CUSTOM_PERSONALITY
    )
    assert custom_marker.default is vol.UNDEFINED
    assert custom_marker.description["suggested_value"] == "Can this be deleted?"
    for field in (CONF_JINGLE_URLS, CONF_STINGER_URLS):
        assert field in result["data_schema"].schema
        assert result["data_schema"].schema[field].config["multiline"] is True
        marker = next(item for item in result["data_schema"].schema if item.schema == field)
        assert marker.default is vol.UNDEFINED
    jingle_marker = next(
        marker for marker in result["data_schema"].schema if marker.schema == CONF_JINGLE_URLS
    )
    stinger_marker = next(
        marker for marker in result["data_schema"].schema if marker.schema == CONF_STINGER_URLS
    )
    assert jingle_marker.description["suggested_value"] == "/local/intro.mp3\n/local/second.mp3"
    assert stinger_marker.description["suggested_value"] == "/local/outro.mp3"
    assert CONF_CADENCE_ENABLED in result["data_schema"].schema
    assert CONF_CADENCE_MIN_TRACKS in result["data_schema"].schema
    assert CONF_CADENCE_MAX_TRACKS in result["data_schema"].schema
    assert CONF_CADENCE_CONTENT in result["data_schema"].schema
    assert {
        option["value"]
        for option in result["data_schema"].schema[CONF_CADENCE_CONTENT].config["options"]
    } == {CADENCE_CONTENT_MUSIC, CADENCE_CONTENT_FULL}
    threshold_marker = next(
        marker
        for marker in result["data_schema"].schema
        if marker.schema == CONF_AQI_THRESHOLD
    )
    assert threshold_marker.default() == "101"
    assert any(
        option["value"] == "101"
        for option in result["data_schema"].schema[CONF_AQI_THRESHOLD].config["options"]
    )
    assert "music_assistant_url" not in result["data_schema"].schema
    assert "music_assistant_token" not in result["data_schema"].schema
    assert "home_assistant_token" not in result["data_schema"].schema
    assert "music_assistant_player" not in result["data_schema"].schema

    assert _options_errors(
        {CONF_PERSONALITY: "custom", CONF_CUSTOM_PERSONALITY: "   "}
    ) == {CONF_CUSTOM_PERSONALITY: "custom_personality_required"}
    assert _options_errors(
        {
            CONF_PERSONALITY: "custom",
            CONF_CUSTOM_PERSONALITY: "Measured, playful, and concise.",
        }
    ) == {}
    assert _options_errors(
        {CONF_CADENCE_MIN_TRACKS: 6, CONF_CADENCE_MAX_TRACKS: 2}
    ) == {CONF_CADENCE_MAX_TRACKS: "cadence_max_below_min"}
    assert _options_errors(
        {CONF_CADENCE_MIN_TRACKS: None, CONF_CADENCE_MAX_TRACKS: "invalid"}
    ) == {CONF_CADENCE_MAX_TRACKS: "cadence_invalid_range"}
    assert _options_errors(
        {
            CONF_JINGLE_URLS: "https://ha.local/local/intro.wav\nhttp://ha.local/local/other.flac",
            CONF_STINGER_URLS: "https://ha.local/local/end.opus",
        }
    ) == {}
    assert _options_errors({CONF_JINGLE_URLS: "/local/intro.wav"}) == {
        CONF_JINGLE_URLS: "audio_url_invalid"
    }


@pytest.mark.asyncio
async def test_announce_calls_tts_speak_with_configured_targets(
    hass: HomeAssistant,
) -> None:
    """The announce service forwards the message to HA's TTS action."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer",
            CONF_TTS: "tts.openai_tts",
        },
    )
    entry.add_to_hass(hass)

    hass.states.async_set("media_player.living_room_streamer", STATE_IDLE)
    hass.states.async_set("tts.openai_tts", STATE_IDLE)

    tts_speak = AsyncMock()
    hass.services.async_register("tts", "speak", tts_speak)
    assert await aidj.async_setup(hass, {})
    assert await aidj.async_setup_entry(hass, entry)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_ANNOUNCE,
        {ATTR_MESSAGE: "  Welcome to the living room.  "},
        blocking=True,
    )

    tts_speak.assert_awaited_once()
    call: ServiceCall = tts_speak.await_args.args[0]
    assert call.data == {
        "entity_id": "tts.openai_tts",
        "media_player_entity_id": "media_player.living_room_streamer",
        ATTR_MESSAGE: "Welcome to the living room.",
        "cache": False,
    }


@pytest.mark.asyncio
async def test_announce_rejects_wrong_entity_domains(hass: HomeAssistant) -> None:
    """Malformed legacy settings fail before calling an unrelated entity domain."""
    from custom_components.aidj.runtime import AiDjRuntime

    wrong_player = AiDjRuntime(
        hass,
        MockConfigEntry(
            domain=DOMAIN,
            data={CONF_NAME: "Radio", CONF_PLAYER: "switch.speaker", CONF_TTS: "tts.voice"},
        ),
    )
    with pytest.raises(Exception, match="not a media_player entity"):
        await wrong_player.async_announce("Hello")

    hass.states.async_set("media_player.speaker", STATE_IDLE)
    wrong_tts = AiDjRuntime(
        hass,
        MockConfigEntry(
            domain=DOMAIN,
            data={CONF_NAME: "Radio", CONF_PLAYER: "media_player.speaker", CONF_TTS: "sensor.voice"},
        ),
    )
    with pytest.raises(Exception, match="not a tts entity"):
        await wrong_tts.async_announce("Hello")


def _set_playing_track(hass: HomeAssistant, entity_id: str, media_id: str, title: str) -> None:
    """Set a realistic playing media-player state for transition tests."""
    hass.states.async_set(
        entity_id,
        STATE_PLAYING,
        {
            "media_content_id": media_id,
            "media_artist": "Test Artist",
            "media_title": title,
        },
    )


@pytest.mark.asyncio
async def test_announce_next_waits_for_a_different_playing_track(
    hass: HomeAssistant,
) -> None:
    """Boundary announcements speak only after the configured player advances."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
    )
    entry.add_to_hass(hass)
    player = "media_player.living_room_streamer_2"
    _set_playing_track(hass, player, "library://track/current", "Current")
    hass.states.async_set("tts.openai_tts", STATE_IDLE)
    tts_speak = AsyncMock()
    hass.services.async_register("tts", "speak", tts_speak)
    assert await aidj.async_setup(hass, {})
    assert await aidj.async_setup_entry(hass, entry)

    with patch(
        "custom_components.aidj.runtime.event_helper.async_track_state_change_event",
        wraps=event_helper.async_track_state_change_event,
    ) as track_state:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ANNOUNCE_NEXT,
            {ATTR_MESSAGE: "Coming up next."},
            blocking=True,
        )
        track_state.assert_called_once()
        assert track_state.call_args.args[1] == player
    _set_playing_track(hass, "sensor.unrelated", "ignored", "Ignored")
    _set_playing_track(hass, player, "library://track/current", "Current")
    await hass.async_block_till_done()
    tts_speak.assert_not_awaited()

    _set_playing_track(hass, player, "library://track/next", "Next")
    await hass.async_block_till_done()

    tts_speak.assert_awaited_once()
    call: ServiceCall = tts_speak.await_args.args[0]
    assert call.data[ATTR_MESSAGE] == "Coming up next."


@pytest.mark.asyncio
async def test_announce_next_isolates_tts_delivery_failure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A TTS failure at the boundary is logged, not leaked from a task."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
    )
    entry.add_to_hass(hass)
    player = "media_player.living_room_streamer_2"
    _set_playing_track(hass, player, "library://track/current", "Current")
    hass.states.async_set("tts.openai_tts", STATE_IDLE)
    tts_speak = AsyncMock(side_effect=RuntimeError("speaker unavailable"))
    hass.services.async_register("tts", "speak", tts_speak)
    assert await aidj.async_setup(hass, {})
    assert await aidj.async_setup_entry(hass, entry)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_ANNOUNCE_NEXT,
        {ATTR_MESSAGE: "This will fail safely."},
        blocking=True,
    )
    _set_playing_track(hass, player, "library://track/next", "Next")
    await hass.async_block_till_done()

    tts_speak.assert_awaited_once()
    assert "Boundary announcement failed" in caplog.text
    assert "Living Room Radio" in caplog.text
    assert "media_player.living_room_streamer_2" in caplog.text


@pytest.mark.asyncio
async def test_announce_next_is_cancelled_when_entry_unloads(
    hass: HomeAssistant,
) -> None:
    """A reload cannot leave a pending announcement speaking later."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
    )
    entry.add_to_hass(hass)
    player = "media_player.living_room_streamer_2"
    _set_playing_track(hass, player, "library://track/current", "Current")
    hass.states.async_set("tts.openai_tts", STATE_IDLE)
    tts_speak = AsyncMock()
    hass.services.async_register("tts", "speak", tts_speak)
    assert await aidj.async_setup(hass, {})
    assert await aidj.async_setup_entry(hass, entry)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_ANNOUNCE_NEXT,
        {ATTR_MESSAGE: "Do not speak."},
        blocking=True,
    )
    assert await aidj.async_unload_entry(hass, entry)
    _set_playing_track(hass, player, "library://track/next", "Next")
    await hass.async_block_till_done()

    tts_speak.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_add_uses_music_assistant_without_replacing_queue(
    hass: HomeAssistant,
) -> None:
    """Queue additions use HA's Music Assistant action and append safely."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
    )
    entry.add_to_hass(hass)
    hass.states.async_set("media_player.living_room_streamer_2", STATE_IDLE)

    play_media = AsyncMock()
    get_queue = AsyncMock(return_value={"media_player.living_room_streamer_2": {}})
    hass.services.async_register("music_assistant", "play_media", play_media)
    hass.services.async_register(
        "music_assistant",
        "get_queue",
        get_queue,
        supports_response=SupportsResponse.ONLY,
    )
    assert await aidj.async_setup(hass, {})
    assert await aidj.async_setup_entry(hass, entry)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_QUEUE_ADD,
        {"media_id": "  spotify://track/example  "},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_QUEUE_ADD,
        {"media_id": "spotify://track/example"},
        blocking=True,
    )

    play_media.assert_awaited_once()
    call: ServiceCall = play_media.await_args.args[0]
    assert call.data == {
        "media_id": "spotify://track/example",
        "enqueue": "add",
        "entity_id": "media_player.living_room_streamer_2",
    }
    get_queue.assert_awaited_once()


@pytest.mark.asyncio
async def test_queue_add_skips_media_already_in_current_or_next_item(
    hass: HomeAssistant,
) -> None:
    """Existing current/next media is not appended again."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
    )
    entry.add_to_hass(hass)
    hass.states.async_set("media_player.living_room_streamer_2", STATE_IDLE)

    play_media = AsyncMock()
    get_queue = AsyncMock(
        return_value={
            "service_response": {
                "media_player.other_player": {
                    "current_item": {
                        "media_item": {"uri": "library://track/next"}
                    }
                },
                "media_player.living_room_streamer_2": {
                    "current_item": {
                        "media_item": {"uri": "library://track/current"}
                    },
                    "next_item": {"media_item": {"uri": "library://track/next"}},
                }
            }
        }
    )
    hass.services.async_register("music_assistant", "play_media", play_media)
    hass.services.async_register(
        "music_assistant",
        "get_queue",
        get_queue,
        supports_response=SupportsResponse.ONLY,
    )
    assert await aidj.async_setup(hass, {})
    assert await aidj.async_setup_entry(hass, entry)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_QUEUE_ADD,
        {"media_id": "library://track/next"},
        blocking=True,
    )

    play_media.assert_not_awaited()
    get_queue.assert_awaited_once()


@pytest.mark.asyncio
async def test_queue_read_uses_music_assistant_response(
    hass: HomeAssistant,
) -> None:
    """Queue reads use HA's response-capable Music Assistant action."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
    )
    entry.add_to_hass(hass)
    hass.states.async_set("media_player.living_room_streamer_2", STATE_IDLE)

    get_queue = AsyncMock(return_value={"media_player.living_room_streamer_2": {"items": []}})
    hass.services.async_register(
        "music_assistant",
        "get_queue",
        get_queue,
        supports_response=SupportsResponse.ONLY,
    )
    assert await aidj.async_setup(hass, {})
    assert await aidj.async_setup_entry(hass, entry)

    runtime = hass.data[DOMAIN][entry.entry_id]
    queue = await runtime.async_get_queue()

    assert queue == {"media_player.living_room_streamer_2": {"items": []}}
    get_queue.assert_awaited_once()
    call: ServiceCall = get_queue.await_args.args[0]
    assert call.data == {"entity_id": "media_player.living_room_streamer_2"}


@pytest.mark.asyncio
async def test_briefing_service_uses_configured_source_defaults(
    hass: HomeAssistant,
) -> None:
    """Briefing source options are used when service fields are omitted."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
        options={
            CONF_WEATHER: "weather.forecast_home",
            CONF_AGENT: "conversation.openai_conversation",
            CONF_PERSONALITY: "crisp_direct",
        },
    )
    entry.add_to_hass(hass)
    hass.states.async_set(
        "weather.forecast_home",
        "sunny",
        {"friendly_name": "Forecast Home", "temperature": 89, "temperature_unit": "°F"},
    )
    conversation = AsyncMock(
        return_value={"response": {"speech": {"plain": {"speech": "Sunny."}}}}
    )
    hass.services.async_register(
        "conversation",
        "process",
        conversation,
        supports_response=SupportsResponse.ONLY,
    )
    assert await aidj.async_setup(hass, {})
    assert await aidj.async_setup_entry(hass, entry)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_BRIEFING,
        {"prompt": "One sentence."},
        blocking=True,
        return_response=True,
    )

    assert response == {"text": "Sunny."}
    assert conversation.await_args.args[0].data["agent_id"] == (
        "conversation.openai_conversation"
    )
    generated_prompt = conversation.await_args.args[0].data["text"]
    assert "Presentation personality (style only;" in generated_prompt
    assert "compact factual sentences" in generated_prompt
    assert generated_prompt.startswith("One sentence.\n")


@pytest.mark.asyncio
async def test_briefing_service_returns_generated_text_without_playback(
    hass: HomeAssistant,
) -> None:
    """The preparation service returns text and does not touch TTS/media services."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
    )
    entry.add_to_hass(hass)
    hass.states.async_set(
        "weather.forecast_home",
        "sunny",
        {
            "friendly_name": "Forecast Home",
            "temperature": 89,
            "temperature_unit": "°F",
        },
    )
    conversation = AsyncMock(
        return_value={
            "response": {"speech": {"plain": {"speech": "Sunny and warm."}}}
        }
    )
    tts_speak = AsyncMock()
    hass.services.async_register(
        "conversation",
        "process",
        conversation,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register("tts", "speak", tts_speak)
    assert await aidj.async_setup(hass, {})
    assert await aidj.async_setup_entry(hass, entry)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_BRIEFING,
        {
            "weather_entity_id": "weather.forecast_home",
            "agent_id": "conversation.openai_conversation",
            "prompt": "Write one sentence for radio.",
        },
        blocking=True,
        return_response=True,
    )

    assert response == {"text": "Sunny and warm."}
    conversation.assert_awaited_once()
    prompt = conversation.await_args.args[0].data["text"]
    assert "Write one sentence for radio." in prompt
    assert "Forecast Home: conditions: sunny, temperature: 89°F" in prompt
    tts_speak.assert_not_awaited()


@pytest.mark.asyncio
async def test_music_transition_generation_uses_verified_queue_without_weather(
    hass: HomeAssistant,
) -> None:
    """Music cadence uses queue facts and personality without collecting weather."""
    settings = StationSettings.from_mapping(
        {
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
            CONF_AGENT: "conversation.agent",
            CONF_MA_URL: "http://ma.local:8095",
            CONF_MA_TOKEN: "ma-secret",
            CONF_HA_TOKEN: "ha-secret",
            CONF_MA_PLAYER: "wiim-player",
        }
    )
    queue_item = BriefingItem(
        provider="music_assistant_queue",
        title="Music context",
        summary="Verified Music Assistant queue context",
        music_context=QueueContext(
            current=TrackContext("Current Song", "Current Artist"),
            next=(TrackContext("Next Song", "Next Artist"),),
        ),
    )
    conversation = AsyncMock(
        return_value={
            "response": {
                "speech": {
                    "plain": {
                        "speech": "Current Song by Current Artist just played; Next Song is next."
                    }
                }
            }
        }
    )
    hass.services.async_register(
        "conversation",
        "process",
        conversation,
        supports_response=SupportsResponse.ONLY,
    )
    service = BriefingGenerationService(hass, settings, settings.player_entity_id, object(), ())
    with patch(
        "custom_components.aidj.briefing_generation.QueueProvider.async_collect",
        new=AsyncMock(return_value=[queue_item]),
    ):
        generated = await service.async_generate_music_transition("")

    assert generated.text.startswith("Current Song")
    prompt = conversation.await_args.args[0].data["text"]
    assert "no non-music topics" in prompt
    assert "Current Song by Current Artist" in prompt
    assert "Upcoming track: Next Song by Next Artist" in prompt
    assert "Weather" not in prompt


@pytest.mark.asyncio
async def test_native_announcement_inserts_random_imaging_as_one_ordered_sequence(
    hass: HomeAssistant,
) -> None:
    """Configured pools surround TTS in one native MA queue operation."""
    from custom_components.aidj.runtime import AiDjRuntime

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Radio",
            CONF_PLAYER: "media_player.radio",
            CONF_TTS: "tts.voice",
            CONF_MA_URL: "http://ma.local:8095",
            CONF_MA_TOKEN: "ma-secret",
            CONF_HA_TOKEN: "ha-secret",
            CONF_MA_PLAYER: "player-1",
        },
        options={
            CONF_JINGLE_URLS: "https://ha.local/local/aidj/a.wav\nhttps://ha.local/local/aidj/b.flac",
            CONF_STINGER_URLS: "https://ha.local/local/aidj/end.opus",
        },
    )
    runtime = AiDjRuntime(hass, entry)
    runtime._ma_tts = type("Tts", (), {"async_render": AsyncMock(return_value="https://ha.local/tts.mp3")})()
    insert = AsyncMock(return_value=["jingle-id", "tts-id", "stinger-id"])
    runtime._ma_queue = type("Queue", (), {"async_insert_sequence": insert})()

    with patch("custom_components.aidj.runtime.choice", side_effect=lambda values: values[-1]):
        item_ids = await runtime.async_queue_announcement_next("Hello")

    assert item_ids == ["jingle-id", "tts-id", "stinger-id"]
    sequence = insert.await_args.args[0]
    assert [(item.uri, item.name) for item in sequence] == [
        ("https://ha.local/local/aidj/b.flac", "AI DJ Jingle"),
        ("https://ha.local/tts.mp3", "AI DJ Announcement"),
        ("https://ha.local/local/aidj/end.opus", "AI DJ Stinger"),
    ]
    assert [item.duration for item in sequence] == [0, 0, 0]
    assert [item.content_type for item in sequence] == [None, None, None]
    assert [ContentType.try_parse(item.uri) for item in sequence] == [
        ContentType.FLAC,
        ContentType.MP3,
        ContentType.OPUS,
    ]


@pytest.mark.asyncio
async def test_native_announcement_without_imaging_inserts_only_tts(
    hass: HomeAssistant,
) -> None:
    """Empty imaging pools preserve the existing TTS-only behavior."""
    from custom_components.aidj.runtime import AiDjRuntime

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_NAME: "Radio", CONF_PLAYER: "media_player.radio", CONF_TTS: "tts.voice"},
    )
    runtime = AiDjRuntime(hass, entry)
    runtime._ma_tts = type("Tts", (), {"async_render": AsyncMock(return_value="https://ha.local/tts.mp3")})()
    insert = AsyncMock(return_value=["tts-id"])
    runtime._ma_queue = type("Queue", (), {"async_insert_sequence": insert})()

    assert await runtime.async_queue_announcement_next("Hello") == ["tts-id"]
    sequence = insert.await_args.args[0]
    assert [(item.uri, item.name) for item in sequence] == [
        ("https://ha.local/tts.mp3", "AI DJ Announcement")
    ]


@pytest.mark.asyncio
async def test_imaging_sequence_retires_owned_items_in_playback_order(
    hass: HomeAssistant,
) -> None:
    """Jingle, announcement, and stinger ownership retires FIFO."""
    from custom_components.aidj.runtime import AiDjRuntime

    runtime = AiDjRuntime(
        hass,
        MockConfigEntry(
            domain=DOMAIN,
            data={CONF_NAME: "Radio", CONF_PLAYER: "media_player.radio", CONF_TTS: "tts.voice"},
        ),
    )
    runtime._store = type("Store", (), {"async_save": AsyncMock()})()
    runtime._record_owned_sequence(
        ["jingle-id", "tts-id", "stinger-id"], {"cadence": "true"}
    )

    await runtime._async_retire_playing_announcement()
    assert list(runtime._owned_queue_items) == ["tts-id", "stinger-id"]
    await runtime._async_retire_playing_announcement()
    assert list(runtime._owned_queue_items) == ["stinger-id"]
    await runtime._async_retire_playing_announcement()
    assert runtime._owned_queue_items == {}


@pytest.mark.asyncio
async def test_native_briefing_rejects_missing_music_context(
    hass: HomeAssistant,
) -> None:
    """Native transport never lets the conversation agent invent track facts."""
    from custom_components.aidj.runtime import AiDjRuntime

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
            CONF_MA_URL: "http://ma.local:8095",
            CONF_MA_TOKEN: "ma-secret",
            CONF_HA_TOKEN: "ha-secret",
            CONF_MA_PLAYER: "wiim-player",
        },
    )
    entry.add_to_hass(hass)
    conversation = AsyncMock()
    hass.services.async_register("conversation", "process", conversation, supports_response=SupportsResponse.ONLY)
    runtime = AiDjRuntime(hass, entry)
    with patch(
        "custom_components.aidj.briefing_generation.async_collect_station_briefing",
        return_value=BriefingCollection(
            [BriefingItem("weather", "Weather", "Weather: sunny")], {}
        ),
    ):
        with pytest.raises(HomeAssistantError, match="queue context was unavailable"):
            await runtime._async_generate_briefing(
                "weather.forecast_home", "conversation.agent"
            )
    conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_briefing_service_rejects_missing_weather_entity(
    hass: HomeAssistant,
) -> None:
    """A missing source fails before the conversation agent is called."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
    )
    entry.add_to_hass(hass)
    conversation = AsyncMock()
    hass.services.async_register(
        "conversation",
        "process",
        conversation,
        supports_response=SupportsResponse.ONLY,
    )
    assert await aidj.async_setup(hass, {})
    assert await aidj.async_setup_entry(hass, entry)

    with pytest.raises(Exception, match="Weather entity does not exist"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BRIEFING,
            {
                "weather_entity_id": "weather.missing",
                "agent_id": "conversation.openai_conversation",
            },
            blocking=True,
            return_response=True,
        )
    conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_briefing_next_generates_and_arms_without_immediate_tts(
    hass: HomeAssistant,
) -> None:
    """Generated briefings wait for a track transition before speaking."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
    )
    entry.add_to_hass(hass)
    player = "media_player.living_room_streamer_2"
    _set_playing_track(hass, player, "library://track/current", "Current")
    hass.states.async_set(
        "weather.forecast_home",
        "sunny",
        {"friendly_name": "Forecast Home", "temperature": 89, "temperature_unit": "°F"},
    )
    hass.states.async_set("tts.openai_tts", STATE_IDLE)
    conversation = AsyncMock(
        return_value={
            "response": {
                "speech": {"plain": {"speech": "Sunny and warm."}}
            }
        }
    )
    tts_speak = AsyncMock()
    hass.services.async_register(
        "conversation",
        "process",
        conversation,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register("tts", "speak", tts_speak)
    assert await aidj.async_setup(hass, {})
    assert await aidj.async_setup_entry(hass, entry)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BRIEFING_NEXT,
        {
            "weather_entity_id": "weather.forecast_home",
            "agent_id": "conversation.openai_conversation",
        },
        blocking=True,
    )
    tts_speak.assert_not_awaited()

    _set_playing_track(hass, player, "library://track/next", "Next")
    await hass.async_block_till_done()

    tts_speak.assert_awaited_once()
    assert tts_speak.await_args.args[0].data[ATTR_MESSAGE] == "Sunny and warm."


@pytest.mark.asyncio
async def test_briefing_next_does_not_arm_when_generation_fails(
    hass: HomeAssistant,
) -> None:
    """Generation errors leave no pending boundary announcement."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
    )
    entry.add_to_hass(hass)
    player = "media_player.living_room_streamer_2"
    _set_playing_track(hass, player, "library://track/current", "Current")
    hass.states.async_set(
        "weather.forecast_home",
        "sunny",
        {"friendly_name": "Forecast Home", "temperature": 89, "temperature_unit": "°F"},
    )
    conversation = AsyncMock(
        return_value={"response": {"speech": {}}}
    )
    tts_speak = AsyncMock()
    hass.services.async_register(
        "conversation",
        "process",
        conversation,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register("tts", "speak", tts_speak)
    assert await aidj.async_setup(hass, {})
    assert await aidj.async_setup_entry(hass, entry)

    with pytest.raises(Exception, match="returned no plain speech"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BRIEFING_NEXT,
            {
                "weather_entity_id": "weather.forecast_home",
                "agent_id": "conversation.openai_conversation",
            },
            blocking=True,
        )
    _set_playing_track(hass, player, "library://track/next", "Next")
    await hass.async_block_till_done()
    tts_speak.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_briefing_next_queues_prepared_tts_url_without_tts_speak(
    hass: HomeAssistant,
) -> None:
    """Native briefing_next renders a URL and inserts it into MA's next slot."""
    from custom_components.aidj.runtime import AiDjRuntime, GeneratedBriefing

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
            "music_assistant_url": "http://ma.local:8095",
            "music_assistant_token": "ma-secret",
            "home_assistant_token": "ha-secret",
            "music_assistant_player": "wiim-player",
        },
    )
    runtime = AiDjRuntime(hass, entry)
    tts_speak = AsyncMock()
    hass.services.async_register("tts", "speak", tts_speak)
    generate = AsyncMock(return_value=GeneratedBriefing("Sunny and warm."))
    queue_announcement = AsyncMock(
        return_value=["jingle-item", "queue-item", "stinger-item"]
    )
    announce_next = AsyncMock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(AiDjRuntime, "_async_generate_briefing", generate)
        patch.setattr(AiDjRuntime, "async_queue_announcement_next", queue_announcement)
        patch.setattr(AiDjRuntime, "async_announce_next", announce_next)
        await runtime.async_briefing_next("weather.forecast_home", "conversation.agent")

    queue_announcement.assert_awaited_once_with("Sunny and warm.")
    assert list(runtime._owned_queue_items) == [
        "jingle-item",
        "queue-item",
        "stinger-item",
    ]
    assert all(
        metadata.get("manual") == "true"
        for metadata in runtime._owned_queue_items.values()
    )
    announce_next.assert_not_awaited()
    tts_speak.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_native_settings_keep_boundary_tts_fallback(
    hass: HomeAssistant,
) -> None:
    """Without the HA URL token, briefing_next retains legacy boundary delivery."""
    from custom_components.aidj.runtime import AiDjRuntime, GeneratedBriefing

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
            "music_assistant_url": "http://ma.local:8095",
            "music_assistant_token": "ma-secret",
            "music_assistant_player": "wiim-player",
        },
    )
    runtime = AiDjRuntime(hass, entry)
    generate = AsyncMock(return_value=GeneratedBriefing("Sunny and warm."))
    queue_announcement = AsyncMock()
    announce_next = AsyncMock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(AiDjRuntime, "_async_generate_briefing", generate)
        patch.setattr(AiDjRuntime, "async_queue_announcement_next", queue_announcement)
        patch.setattr(AiDjRuntime, "async_announce_next", announce_next)
        await runtime.async_briefing_next("weather.forecast_home", "conversation.agent")

    announce_next.assert_awaited_once_with("Sunny and warm.")
    queue_announcement.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_renderer_keeps_ma_and_ha_credentials_separate(
    hass: HomeAssistant,
) -> None:
    """The MA client receives only the MA token; TTS receives only the HA token."""
    from custom_components.aidj.runtime import AiDjRuntime

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
            "music_assistant_url": "http://ma.local:8095",
            "music_assistant_token": "ma-secret",
            "home_assistant_token": "ha-secret",
            "music_assistant_player": "wiim-player",
        },
    )
    runtime = AiDjRuntime(hass, entry)

    class FakeClient:
        def __init__(self, url, session, *, token):
            self.url = url
            self.token = token

        async def start_listening(self, *, init_ready):
            init_ready.set()
            await asyncio.sleep(3600)

        async def disconnect(self):
            return None

    from custom_components.aidj import runtime as runtime_module

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime_module, "MusicAssistantClient", FakeClient)
        await runtime.async_start_music_assistant()

    assert runtime._ma_client.token == "ma-secret"
    assert runtime._ma_tts.access_token == "ha-secret"
    runtime.async_unload()


@pytest.mark.asyncio
async def test_music_assistant_listener_timeout_does_not_block_setup(
    hass: HomeAssistant,
) -> None:
    """A slow MA websocket listener is a background task, not a startup blocker."""
    from custom_components.aidj import runtime as runtime_module
    from custom_components.aidj.runtime import AiDjRuntime

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
            CONF_MA_URL: "http://ma.local:8095",
            CONF_MA_TOKEN: "ma-secret",
            CONF_HA_TOKEN: "ha-secret",
            CONF_MA_PLAYER: "wiim-player",
        },
    )
    entry.add_to_hass(hass)
    runtime = AiDjRuntime(hass, entry)

    class SlowClient:
        def __init__(self, url, session, *, token):
            self.url = url
            self.token = token

        async def start_listening(self, *, init_ready):
            await asyncio.sleep(3600)

        async def disconnect(self):
            return None

    async def timeout_wait(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    with patch.object(runtime_module, "MusicAssistantClient", SlowClient), patch(
        "custom_components.aidj.runtime.asyncio.wait_for",
        new=timeout_wait,
    ):
        await runtime.async_start_music_assistant()

    assert runtime._ma_listener_task is not None
    assert runtime._ma_listener_task not in hass._tasks
    runtime.async_unload()


@pytest.mark.asyncio
async def test_music_assistant_listener_reconnects_after_failure(
    hass: HomeAssistant,
) -> None:
    """A failed MA listener is replaced instead of leaving a dead client forever."""
    from custom_components.aidj import runtime as runtime_module
    from custom_components.aidj.runtime import AiDjRuntime

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
            CONF_MA_URL: "http://ma.local:8095",
            CONF_MA_TOKEN: "ma-secret",
            CONF_HA_TOKEN: "ha-secret",
            CONF_MA_PLAYER: "wiim-player",
        },
    )
    entry.add_to_hass(hass)
    runtime = AiDjRuntime(hass, entry)
    clients = []

    class ReconnectingClient:
        def __init__(self, url, session, *, token):
            self.number = len(clients) + 1
            self.disconnected = False
            clients.append(self)

        async def start_listening(self, *, init_ready):
            if self.number == 1:
                raise ConnectionError("MA is still starting")
            init_ready.set()
            await asyncio.Event().wait()

        async def disconnect(self):
            self.disconnected = True

    async def immediate_sleep(delay):
        return None

    with patch.object(runtime_module, "MusicAssistantClient", ReconnectingClient), patch(
        "custom_components.aidj.runtime.asyncio.sleep",
        new=immediate_sleep,
    ):
        await runtime.async_start_music_assistant()

    assert len(clients) == 2
    assert clients[0].disconnected is True
    assert runtime._ma_client is clients[1]
    assert runtime._ma_queue.client is clients[1]
    runtime.async_unload()


@pytest.mark.asyncio
async def test_cadence_target_resets_when_options_change(
    hass: HomeAssistant,
) -> None:
    """Persisted targets outside new option bounds are reset and saved."""
    from custom_components.aidj.runtime import AiDjRuntime

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
        options={
            CONF_CADENCE_ENABLED: True,
            CONF_CADENCE_MIN_TRACKS: 1,
            CONF_CADENCE_MAX_TRACKS: 1,
        },
    )
    runtime = AiDjRuntime(hass, entry)
    remove = AsyncMock()
    runtime._ma_queue = type("Queue", (), {"async_remove": remove})()
    store = AsyncMock()
    store.async_load.return_value = {
        "owned_queue_items": {
            "stale-item": {"cadence": "true", "delete_pending": "true"}
        },
        "cadence_track_count": 4,
        "cadence_target": 5,
    }
    with patch("custom_components.aidj.runtime.Store", return_value=store):
        await runtime.async_initialize_controller()

    assert runtime._cadence_track_count == 0
    assert runtime._cadence_target == 1
    assert runtime._owned_queue_items == {}
    remove.assert_awaited_once_with("stale-item")
    store.async_save.assert_awaited()
    runtime.async_unload()


def test_controller_state_rejects_malformed_storage_and_clamps_history() -> None:
    """Persisted controller data is validated before entering runtime state."""
    from custom_components.aidj.runtime import ControllerState

    assert ControllerState.from_storage("not a mapping") == ControllerState()
    state = ControllerState.from_storage(
        {
            "enabled": 1,
            "owned_queue_items": {
                "valid": {"boundary": "noon", "attempt": 2, "ignored": object()},
                42: {"boundary": "invalid id"},
                "invalid metadata": "not a mapping",
            },
            "recent_story_ids": [42, *[f"story-{index}" for index in range(20)]],
        }
    )

    assert state.enabled is False
    assert state.owned_queue_items == {
        "valid": {"boundary": "noon", "attempt": "2"}
    }
    assert state.recent_story_ids == tuple(
        f"story-{index}" for index in range(20 - RECENT_STORY_LIMIT, 20)
    )
    assert ControllerState.from_storage(state.as_storage()) == state


def test_story_rotation_prefers_unseen_then_bounds_fifo() -> None:
    """Story rotation is pure and keeps only the bounded FIFO."""
    stories = [
        BriefingItem(
            provider="feedreader:event.news_archives_voice_of_san_diego",
            title=f"Story {index}",
            summary="",
            identity=f"story-{index}",
        )
        for index in range(12)
    ]

    selected = select_feed_story(stories, [])
    assert selected is not None and selected.identity == "story-0"
    recent = record_story([], selected.identity, 10)
    assert recent == ["story-0"]

    recent = [f"story-{index}" for index in range(10)]
    selected = select_feed_story(stories, recent)
    assert selected is not None and selected.identity == "story-10"
    recent = record_story(recent, selected.identity, 10)
    assert recent == [f"story-{index}" for index in range(1, 11)]

    recent = ["story-0", "story-1"]
    assert select_feed_story(stories[:2], recent) is None

    default_provider_story = BriefingItem(
        provider="feedreader", title="Default provider", summary="", identity="default"
    )
    assert select_feed_story([default_provider_story], []) == default_provider_story


@pytest.mark.asyncio
async def test_controller_prepares_once_at_half_hour_window_and_persists_enabled_state(
    hass: HomeAssistant,
) -> None:
    """The station switch enables scheduling without interrupting playback."""
    from datetime import datetime, timezone
    from custom_components.aidj.runtime import AiDjRuntime, GeneratedBriefing

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
            CONF_MA_URL: "http://ma.local:8095",
            CONF_MA_TOKEN: "ma-secret",
            CONF_HA_TOKEN: "ha-secret",
            CONF_MA_PLAYER: "wiim-player",
            CONF_WEATHER: "weather.forecast_home",
            CONF_AGENT: "conversation.agent",
        },
    )
    entry.add_to_hass(hass)
    player = entry.data[CONF_PLAYER]
    _set_playing_track(hass, player, "library://track/current", "Current")
    runtime = AiDjRuntime(hass, entry)
    runtime._ma_queue = type("Queue", (), {"async_remove": AsyncMock()})()

    generate = AsyncMock(return_value=GeneratedBriefing("A fresh briefing."))
    queue = AsyncMock(return_value="queue-item-1")
    with patch.object(AiDjRuntime, "_async_generate_briefing", generate), patch.object(
        AiDjRuntime, "async_queue_announcement_next", queue
    ):
        await runtime.async_initialize_controller()
        await runtime.async_set_enabled(True)
        assert runtime.enabled is True
        runtime._cadence_track_count = 4
        runtime._cadence_target = 4
        runtime._owned_queue_items["old-cadence"] = {
            "cadence": "true",
            "created_at": "2026-07-31T12:20:00+00:00",
        }
        boundary = datetime(2026, 7, 31, 12, 25, tzinfo=timezone.utc)
        await runtime._async_handle_schedule(boundary)
        await runtime._async_handle_schedule(boundary)

    generate.assert_awaited_once_with("weather.forecast_home", "conversation.agent")
    runtime._ma_queue.async_remove.assert_awaited_once_with("old-cadence")
    assert (await runtime._store.async_load())["enabled"] is True
    assert runtime._owned_queue_items == {
        "queue-item-1": {
            "boundary": "2026-07-31T12:30:00+00:00",
            "created_at": runtime._owned_queue_items["queue-item-1"]["created_at"],
        }
    }
    assert runtime._cadence_track_count == 0
    assert 3 <= runtime._cadence_target <= 5
    runtime.async_unload()


@pytest.mark.asyncio
async def test_disable_invalidates_inflight_clock_generation(
    hass: HomeAssistant,
) -> None:
    """A briefing generated before disable cannot queue after disable completes."""
    from custom_components.aidj.runtime import AiDjRuntime

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
            CONF_MA_URL: "http://ma.local:8095",
            CONF_MA_TOKEN: "ma-secret",
            CONF_HA_TOKEN: "ha-secret",
            CONF_MA_PLAYER: "wiim-player",
        },
        options={CONF_WEATHER: "weather.home", CONF_AGENT: "conversation.agent"},
    )
    player = entry.data[CONF_PLAYER]
    _set_playing_track(hass, player, "library://track/one", "One")
    runtime = AiDjRuntime(hass, entry)
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_generate(*args):
        started.set()
        await release.wait()
        return GeneratedBriefing("Too late")

    queue = AsyncMock(return_value="clock-item")
    with patch.object(AiDjRuntime, "_async_generate_briefing", delayed_generate), patch.object(
        AiDjRuntime, "async_queue_announcement_next", queue
    ):
        await runtime.async_initialize_controller()
        await runtime.async_set_enabled(True)
        task = hass.async_create_task(
            runtime._async_prepare_boundary(
                datetime(2026, 8, 4, 12, 30, tzinfo=timezone.utc)
            )
        )
        await started.wait()
        await runtime.async_set_enabled(False)
        release.set()
        await task

    queue.assert_not_awaited()
    assert runtime._owned_queue_items == {}
    runtime.async_unload()


@pytest.mark.asyncio
async def test_disable_during_queue_insertion_removes_stale_item(
    hass: HomeAssistant,
) -> None:
    """An item inserted after disable is immediately removed and never committed."""
    from custom_components.aidj.runtime import AiDjRuntime

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
            CONF_MA_URL: "http://ma.local:8095",
            CONF_MA_TOKEN: "ma-secret",
            CONF_HA_TOKEN: "ha-secret",
            CONF_MA_PLAYER: "wiim-player",
        },
        options={CONF_WEATHER: "weather.home", CONF_AGENT: "conversation.agent"},
    )
    player = entry.data[CONF_PLAYER]
    _set_playing_track(hass, player, "library://track/one", "One")
    runtime = AiDjRuntime(hass, entry)
    insert_started = asyncio.Event()
    insert_release = asyncio.Event()
    remove = AsyncMock()
    runtime._ma_queue = type("Queue", (), {"async_remove": remove})()

    async def delayed_insert(self, message):
        insert_started.set()
        await insert_release.wait()
        return "late-item"

    with patch.object(
        AiDjRuntime,
        "_async_generate_briefing",
        new=AsyncMock(return_value=GeneratedBriefing("Ready")),
    ), patch.object(AiDjRuntime, "async_queue_announcement_next", delayed_insert):
        await runtime.async_initialize_controller()
        await runtime.async_set_enabled(True)
        task = hass.async_create_task(
            runtime._async_prepare_boundary(
                datetime(2026, 8, 4, 12, 30, tzinfo=timezone.utc)
            )
        )
        await asyncio.wait_for(insert_started.wait(), timeout=1)
        await runtime.async_set_enabled(False)
        insert_release.set()
        await task

    remove.assert_awaited_once_with("late-item")
    assert runtime._owned_queue_items == {}
    runtime.async_unload()


@pytest.mark.asyncio
async def test_cadence_counts_tracks_and_queues_music_transition(
    hass: HomeAssistant,
) -> None:
    """Cadence queues once at its target and ignores AI DJ announcement tracks."""
    from custom_components.aidj.runtime import AiDjRuntime

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
        options={
            CONF_CADENCE_ENABLED: True,
            CONF_CADENCE_MIN_TRACKS: 2,
            CONF_CADENCE_MAX_TRACKS: 2,
            CONF_CADENCE_CONTENT: CADENCE_CONTENT_MUSIC,
            CONF_AGENT: "conversation.agent",
        },
    )
    player = entry.data[CONF_PLAYER]
    _set_playing_track(hass, player, "library://track/one", "One")
    runtime = AiDjRuntime(hass, entry)
    generate = AsyncMock(return_value=GeneratedBriefing("Two was great; Three is next."))
    queue = AsyncMock(return_value="cadence-item")
    with patch.object(
        BriefingGenerationService,
        "async_generate_music_transition",
        generate,
    ), patch.object(AiDjRuntime, "async_queue_announcement_next", queue):
        await runtime.async_initialize_controller()
        await runtime.async_set_enabled(True)
        hass.states.async_set(player, STATE_IDLE)
        await hass.async_block_till_done()
        _set_playing_track(hass, player, "library://track/resumed", "Resumed")
        await hass.async_block_till_done()
        assert runtime._cadence_track_count == 0
        _set_playing_track(hass, player, "library://track/two", "Two")
        await hass.async_block_till_done()
        _set_playing_track(hass, player, "builtin://aidj", "AI DJ Announcement")
        await hass.async_block_till_done()
        _set_playing_track(hass, player, "library://track/three", "Three")
        await hass.async_block_till_done()
        _set_playing_track(hass, player, "library://track/four", "Four")
        await hass.async_block_till_done()

    generate.assert_awaited_once_with("conversation.agent")
    queue.assert_awaited_once_with("Two was great; Three is next.")
    assert runtime._cadence_track_count == 0
    assert runtime._cadence_target == 2
    assert runtime._owned_queue_items["cadence-item"]["cadence"] == "true"
    runtime.async_unload()


@pytest.mark.asyncio
async def test_playing_announcement_retires_owned_clock_item(
    hass: HomeAssistant,
) -> None:
    """A consumed scheduled briefing cannot suppress cadence forever."""
    from custom_components.aidj.runtime import AiDjRuntime

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
    )
    player = entry.data[CONF_PLAYER]
    _set_playing_track(hass, player, "library://track/one", "One")
    runtime = AiDjRuntime(hass, entry)
    await runtime.async_initialize_controller()
    runtime._owned_queue_items = {
        "clock-item": {"boundary": "noon", "created_at": "2026-08-04T12:00:00Z"}
    }

    _set_playing_track(hass, player, "builtin://aidj", "AI DJ Announcement")
    await hass.async_block_till_done()

    assert runtime._owned_queue_items == {}
    assert runtime._has_prepared_boundary_for_any_time() is False
    runtime.async_unload()


@pytest.mark.asyncio
async def test_cadence_defers_for_clock_briefing_and_retries_after_failure(
    hass: HomeAssistant,
) -> None:
    """Scheduled breaks win, while failed cadence remains due for the next song."""
    from custom_components.aidj.runtime import AiDjRuntime

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
        options={
            CONF_CADENCE_ENABLED: True,
            CONF_CADENCE_MIN_TRACKS: 1,
            CONF_CADENCE_MAX_TRACKS: 1,
            CONF_AGENT: "conversation.agent",
        },
    )
    player = entry.data[CONF_PLAYER]
    _set_playing_track(hass, player, "library://track/one", "One")
    runtime = AiDjRuntime(hass, entry)
    generate = AsyncMock(
        side_effect=[HomeAssistantError("temporary failure"), GeneratedBriefing("Ready")]
    )
    queue = AsyncMock(return_value="cadence-item")
    with patch.object(
        BriefingGenerationService,
        "async_generate_music_transition",
        generate,
    ), patch.object(AiDjRuntime, "async_queue_announcement_next", queue):
        await runtime.async_initialize_controller()
        await runtime.async_set_enabled(True)
        runtime._owned_queue_items["clock-item"] = {"boundary": "noon"}
        _set_playing_track(hass, player, "library://track/two", "Two")
        await hass.async_block_till_done()
        assert generate.await_count == 0
        runtime._owned_queue_items.clear()
        _set_playing_track(hass, player, "library://track/three", "Three")
        await hass.async_block_till_done()
        assert runtime._cadence_track_count == 2
        _set_playing_track(hass, player, "library://track/four", "Four")
        await hass.async_block_till_done()

    assert generate.await_count == 2
    queue.assert_awaited_once_with("Ready")
    assert runtime._cadence_track_count == 0
    runtime.async_unload()


@pytest.mark.asyncio
async def test_owned_item_cleanup_retains_failed_deletions_for_retry(
    hass: HomeAssistant,
) -> None:
    """Deletion failures keep ownership so a later cleanup can retry safely."""
    from custom_components.aidj.runtime import AiDjRuntime

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
    )
    runtime = AiDjRuntime(hass, entry)
    remove = AsyncMock(side_effect=[RuntimeError("MA unavailable"), None])
    runtime._ma_queue = type("Queue", (), {"async_remove": remove})()
    runtime._owned_queue_items = {"cadence-item": {"cadence": "true"}}

    await runtime._async_remove_owned_queue_items()
    assert runtime._owned_queue_items["cadence-item"]["delete_pending"] == "true"
    await runtime._async_remove_owned_queue_items()
    assert runtime._owned_queue_items == {}


@pytest.mark.asyncio
async def test_controller_removes_owned_items_when_playback_stops(
    hass: HomeAssistant,
) -> None:
    """Stopping playback removes only the station's persisted queue items."""
    from custom_components.aidj.runtime import AiDjRuntime

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.openai_tts",
        },
    )
    entry.add_to_hass(hass)
    player = entry.data[CONF_PLAYER]
    _set_playing_track(hass, player, "library://track/current", "Current")
    runtime = AiDjRuntime(hass, entry)
    runtime._ma_queue = type("Queue", (), {"async_remove": AsyncMock()})()
    await runtime.async_initialize_controller()
    runtime._owned_queue_items = {"queue-item-1": {"boundary": "boundary"}}
    await hass.async_block_till_done()

    hass.states.async_set(player, STATE_IDLE)
    await hass.async_block_till_done()

    remove = runtime._ma_queue.async_remove
    remove.assert_awaited_once_with("queue-item-1")
    assert runtime._owned_queue_items == {}
    runtime.async_unload()
