"""Runtime tests for the AI DJ integration."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import STATE_IDLE, STATE_PLAYING
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import aidj
from custom_components.aidj.music_assistant import MusicAssistantQueueAdapter
from custom_components.aidj.briefing import (
    BriefingItem,
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
    CONF_FEED,
    CONF_HA_TOKEN,
    CONF_MA_PLAYER,
    CONF_MA_TOKEN,
    CONF_MA_URL,
    CONF_NAME,
    CONF_PLAYER,
    CONF_TTS,
    CONF_WEATHER,
    DOMAIN,
    SERVICE_QUEUE_ADD,
    SERVICE_ANNOUNCE,
    SERVICE_ANNOUNCE_NEXT,
    SERVICE_BRIEFING,
    SERVICE_BRIEFING_NEXT,
)
from music_assistant_models.enums import QueueOption



@pytest.mark.asyncio
async def test_music_assistant_queue_adapter_inserts_next_without_replacing() -> None:
    """Native MA transport uses one add-only NEXT queue operation."""
    class Media:
        uri = "http://ha.local/tts/clip.mp3"

    class Item:
        queue_item_id = "aidj-item-1"
        media_item = Media()

    class Queue:
        queue_id = "queue-1"
        current_item = None
        next_item = None
        elapsed_time = 12

    queues = type("Queues", (), {})()
    queues.get_active_queue = AsyncMock(return_value=Queue())
    queues.play_media = AsyncMock()
    queues.get_queue_items = AsyncMock(return_value=[Item()])
    client = type("Client", (), {"player_queues": queues})()

    item_id = await MusicAssistantQueueAdapter(client, "player-1").async_insert_next(
        " http://ha.local/tts/clip.mp3 "
    )

    assert item_id == "aidj-item-1"
    queues.play_media.assert_awaited_once_with(
        queue_id="queue-1",
        media=["http://ha.local/tts/clip.mp3"],
        option=QueueOption.NEXT,
    )


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
async def test_queue_provider_normalizes_current_and_next_tracks(
    hass: HomeAssistant,
) -> None:
    """Queue context includes only current and next media facts."""
    get_queue = AsyncMock(
        return_value={
            "service_response": {
                "media_player.living_room_streamer_2": {
                    "current_item": {
                        "media_item": {
                            "uri": "library://track/current",
                            "name": "Current Song",
                            "artists": [{"name": "Current Artist"}],
                        }
                    },
                    "next_item": {
                        "media_item": {
                            "uri": "library://track/next",
                            "name": "Next Song",
                            "artists": [{"name": "Next Artist"}],
                        }
                    },
                }
            }
        }
    )
    hass.services.async_register(
        "music_assistant",
        "get_queue",
        get_queue,
        supports_response=SupportsResponse.ONLY,
    )

    items = await QueueProvider(
        hass, "media_player.living_room_streamer_2"
    ).async_collect()

    assert [(item.title, item.summary, item.source) for item in items] == [
        ("Now playing", "Now playing: Current Song by Current Artist", "library://track/current"),
        ("Up next", "Up next: Next Song by Next Artist", "library://track/next"),
    ]
    call: ServiceCall = get_queue.await_args.args[0]
    assert call.data == {"entity_id": "media_player.living_room_streamer_2"}


@pytest.mark.asyncio
async def test_feedreader_provider_normalizes_latest_event(
    hass: HomeAssistant,
) -> None:
    """Feedreader event data becomes compact local-news context."""
    hass.states.async_set(
        "event.san_diego_news",
        "2026-07-31T12:00:00+00:00",
        {
            "event_type": "feedreader",
            "event_data": {
                "title": "San Diego council approves new park",
                "description": "  A local update with   extra whitespace.  ",
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
        )
    ]


@pytest.mark.asyncio
async def test_feedreader_provider_skips_missing_or_empty_event(
    hass: HomeAssistant,
) -> None:
    """A missing or empty feed entity is an optional-provider no-op."""
    assert await FeedreaderEventProvider(hass, "event.missing").async_collect() == []
    hass.states.async_set("event.empty_feed", "unknown", {"event_type": "feedreader"})
    assert await FeedreaderEventProvider(hass, "event.empty_feed").async_collect() == []


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
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert CONF_WEATHER in result["data_schema"].schema
    assert CONF_AGENT in result["data_schema"].schema
    assert "music_assistant_url" not in result["data_schema"].schema
    assert "music_assistant_token" not in result["data_schema"].schema
    assert "home_assistant_token" not in result["data_schema"].schema
    assert "music_assistant_player" not in result["data_schema"].schema


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

    await hass.services.async_call(
        DOMAIN,
        SERVICE_ANNOUNCE_NEXT,
        {ATTR_MESSAGE: "Coming up next."},
        blocking=True,
    )
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
    tts_speak = AsyncMock()
    hass.services.async_register("tts", "speak", tts_speak)
    generate = AsyncMock(return_value="Sunny and warm.")
    queue_announcement = AsyncMock(return_value="queue-item")
    announce_next = AsyncMock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(AiDjRuntime, "async_generate_briefing", generate)
        patch.setattr(AiDjRuntime, "async_queue_announcement_next", queue_announcement)
        patch.setattr(AiDjRuntime, "async_announce_next", announce_next)
        await runtime.async_briefing_next("weather.forecast_home", "conversation.agent")

    queue_announcement.assert_awaited_once_with("Sunny and warm.")
    announce_next.assert_not_awaited()
    tts_speak.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_native_settings_keep_boundary_tts_fallback(
    hass: HomeAssistant,
) -> None:
    """Without the HA URL token, briefing_next retains legacy boundary delivery."""
    from custom_components.aidj.runtime import AiDjRuntime

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
    generate = AsyncMock(return_value="Sunny and warm.")
    queue_announcement = AsyncMock()
    announce_next = AsyncMock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(AiDjRuntime, "async_generate_briefing", generate)
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
async def test_controller_prepares_once_at_half_hour_window_and_persists_enabled_state(
    hass: HomeAssistant,
) -> None:
    """The station switch enables scheduling without interrupting playback."""
    from datetime import datetime, timezone
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
            CONF_WEATHER: "weather.forecast_home",
            CONF_AGENT: "conversation.agent",
        },
    )
    entry.add_to_hass(hass)
    player = entry.data[CONF_PLAYER]
    _set_playing_track(hass, player, "library://track/current", "Current")
    runtime = AiDjRuntime(hass, entry)
    runtime._ma_queue = type("Queue", (), {"async_remove": AsyncMock()})()

    generate = AsyncMock(return_value="A fresh briefing.")
    queue = AsyncMock(return_value="queue-item-1")
    with patch.object(AiDjRuntime, "async_generate_briefing", generate), patch.object(
        AiDjRuntime, "async_queue_announcement_next", queue
    ):
        await runtime.async_initialize_controller()
        await runtime.async_set_enabled(True)
        assert runtime.enabled is True
        boundary = datetime(2026, 7, 31, 12, 25, tzinfo=timezone.utc)
        await runtime._async_handle_schedule(boundary)
        await runtime._async_handle_schedule(boundary)

    generate.assert_awaited_once()
    assert (await runtime._store.async_load())["enabled"] is True
    assert runtime._owned_queue_items == {
        "queue-item-1": {
            "boundary": "2026-07-31T12:30:00+00:00",
            "created_at": runtime._owned_queue_items["queue-item-1"]["created_at"],
        }
    }
    runtime.async_unload()


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
