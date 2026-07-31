"""Runtime tests for the AI DJ integration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import STATE_IDLE
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import aidj
from custom_components.aidj.const import (
    ATTR_MESSAGE,
    CONF_NAME,
    CONF_PLAYER,
    CONF_TTS,
    DOMAIN,
    SERVICE_QUEUE_ADD,
    SERVICE_ANNOUNCE,
    SERVICE_START,
    SERVICE_STOP,
)


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
