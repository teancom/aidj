"""Runtime tests for the AI DJ integration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import STATE_IDLE, STATE_PLAYING
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import aidj
from custom_components.aidj.briefing import (
    BriefingItem,
    EntityStateProvider,
    HaConversationBriefingGenerator,
    WeatherEntityProvider,
    async_collect_briefing,
)
from custom_components.aidj.const import (
    ATTR_MESSAGE,
    CONF_NAME,
    CONF_PLAYER,
    CONF_TTS,
    DOMAIN,
    SERVICE_QUEUE_ADD,
    SERVICE_ANNOUNCE,
    SERVICE_ANNOUNCE_NEXT,
    SERVICE_START,
    SERVICE_STOP,
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
async def test_config_flow_creates_entry_and_rejects_duplicate(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """The UI flow stores station settings and enforces one station for now."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Living Room Radio",
            CONF_PLAYER: "media_player.living_room_streamer",
            CONF_TTS: "tts.openai_tts",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert result["title"] == "Living Room Radio"
    assert result["data"] == {
        CONF_NAME: "Living Room Radio",
        CONF_PLAYER: "media_player.living_room_streamer",
        CONF_TTS: "tts.openai_tts",
    }

    duplicate = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    duplicate = await hass.config_entries.flow.async_configure(
        duplicate["flow_id"],
        {
            CONF_NAME: "Second Radio",
            CONF_PLAYER: "media_player.living_room_streamer_2",
            CONF_TTS: "tts.home_assistant_cloud",
        },
    )
    assert duplicate["type"] == "abort"
    assert duplicate["reason"] == "already_configured"


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
async def test_start_and_stop_call_only_the_configured_player(
    hass: HomeAssistant,
) -> None:
    """Station lifecycle services target the configured media player."""
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

    media_play = AsyncMock()
    media_stop = AsyncMock()
    hass.services.async_register("media_player", "media_play", media_play)
    hass.services.async_register("media_player", "media_stop", media_stop)
    assert await aidj.async_setup(hass, {})
    assert await aidj.async_setup_entry(hass, entry)

    await hass.services.async_call(DOMAIN, SERVICE_START, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_STOP, blocking=True)

    media_play.assert_awaited_once()
    media_stop.assert_awaited_once()
    assert media_play.await_args.args[0].data == {
        "entity_id": "media_player.living_room_streamer_2"
    }
    assert media_stop.await_args.args[0].data == {
        "entity_id": "media_player.living_room_streamer_2"
    }
