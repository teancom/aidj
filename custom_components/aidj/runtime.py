"""Runtime behavior for an AI DJ config entry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_PLAYING
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.util import dt as dt_util
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import event as event_helper
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .ha_music_assistant import HaMusicAssistantQueue
from .music_assistant import HaTtsUrlRenderer, MusicAssistantClient, MusicAssistantQueueAdapter

from .announcement import AnnouncementController
from .briefing_generation import BriefingGenerationService, GeneratedBriefing
from .const import (
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
    RECENT_STORY_LIMIT,
)
from .story import record_story


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ControllerState:
    """Validated persisted state for one station controller."""

    enabled: bool = False
    owned_queue_items: dict[str, dict[str, str]] = field(default_factory=dict)
    recent_story_ids: tuple[str, ...] = ()

    @classmethod
    def from_storage(cls, stored: Any) -> ControllerState:
        """Parse untrusted storage data, dropping malformed records."""
        if not isinstance(stored, dict):
            return cls()
        raw_items = stored.get("owned_queue_items", {})
        owned_items: dict[str, dict[str, str]] = {}
        if isinstance(raw_items, dict):
            for item_id, metadata in raw_items.items():
                if not isinstance(item_id, str) or not isinstance(metadata, dict):
                    continue
                normalized = {
                    key: str(value)
                    for key, value in metadata.items()
                    if isinstance(key, str) and isinstance(value, (str, int, float))
                }
                owned_items[item_id] = normalized
        raw_story_ids = stored.get("recent_story_ids", [])
        story_ids = (
            tuple(story_id for story_id in raw_story_ids if isinstance(story_id, str))
            if isinstance(raw_story_ids, list)
            else ()
        )
        return cls(
            enabled=stored.get("enabled") is True,
            owned_queue_items=owned_items,
            recent_story_ids=story_ids[-RECENT_STORY_LIMIT:],
        )

    def as_storage(self) -> dict[str, Any]:
        """Return the stable Home Assistant storage representation."""
        return {
            "enabled": self.enabled,
            "owned_queue_items": self.owned_queue_items,
            "recent_story_ids": list(self.recent_story_ids[-RECENT_STORY_LIMIT:]),
        }


@dataclass(slots=True)
class AiDjRuntime:
    """Runtime object for one configured AI DJ station."""

    hass: HomeAssistant
    entry: ConfigEntry
    _ha_queue: HaMusicAssistantQueue = field(init=False)
    _owned_queue_items: dict[str, dict[str, str]] = field(default_factory=dict)
    _recent_story_ids: list[str] = field(default_factory=list)
    _enabled: bool = False
    _store: Store[dict[str, Any]] | None = None
    _schedule_unsub: Any = None
    _player_unsub: Any = None
    _preparing_boundary: str | None = None
    _announcement: AnnouncementController = field(init=False)
    _ma_client: MusicAssistantClient | None = None
    _ma_listener_task: Any = None
    _ma_queue: MusicAssistantQueueAdapter | None = None
    _ma_tts: HaTtsUrlRenderer | None = None

    def __post_init__(self) -> None:
        """Create the boundary announcement controller after dataclass init."""
        self._announcement = AnnouncementController(
            self.hass,
            self.player_entity_id,
            self._async_deliver_announcement,
        )
        self._ha_queue = HaMusicAssistantQueue(self.hass, self.player_entity_id)

    @property
    def settings(self) -> dict[str, Any]:
        """Return current config data with options overriding initial values."""
        return {**self.entry.data, **self.entry.options}

    @property
    def enabled(self) -> bool:
        """Return whether scheduled AI DJ breaks are enabled."""
        return self._enabled

    @property
    def music_assistant_enabled(self) -> bool:
        """Return whether the optional native MA transport is configured."""
        settings = self.settings
        return bool(
            settings.get(CONF_MA_URL, "").strip()
            and settings.get(CONF_MA_TOKEN, "").strip()
            and settings.get(CONF_HA_TOKEN, "").strip()
            and settings.get(CONF_MA_PLAYER, "").strip()
        )

    async def async_initialize_controller(self) -> None:
        """Restore controller state and install the schedule/state listeners."""
        self._store = Store(self.hass, 1, f"aidj.{self.entry.entry_id}")
        state = ControllerState.from_storage(await self._store.async_load())
        self._enabled = state.enabled
        self._owned_queue_items = state.owned_queue_items
        self._recent_story_ids = list(state.recent_story_ids)
        self._schedule_unsub = event_helper.async_track_time_change(
            self.hass, self._async_handle_schedule, minute={25, 55}, second=0
        )
        self._player_unsub = event_helper.async_track_state_change_event(
            self.hass, self.player_entity_id, self._async_handle_player_state_changed
        )
        if self._enabled:
            await self._async_recover_controller()

    async def async_set_enabled(self, enabled: bool) -> None:
        """Enable or disable scheduled station breaks."""
        if self._enabled == enabled:
            if not enabled:
                await self._async_remove_owned_queue_items()
            return
        self._enabled = enabled
        await self._async_save_controller_state()
        if not enabled:
            self._preparing_boundary = None
            await self._async_remove_owned_queue_items()
        else:
            # Preparation is driven by the next :25/:55 time callback.
            return

    async def async_start_music_assistant(self) -> None:
        """Start the official MA client when native transport is configured."""
        if not self.music_assistant_enabled or self._ma_client is not None:
            return
        settings = self.settings
        self._ma_client = MusicAssistantClient(
            settings[CONF_MA_URL].strip(),
            async_get_clientsession(self.hass),
            token=settings[CONF_MA_TOKEN].strip(),
        )
        init_ready = asyncio.Event()
        self._ma_listener_task = self.entry.async_create_background_task(
            self.hass,
            self._ma_client.start_listening(init_ready=init_ready),
            name="music_assistant_listener",
        )
        try:
            await asyncio.wait_for(init_ready.wait(), timeout=15)
        except TimeoutError:
            _LOGGER.warning(
                "Music Assistant did not become ready within 15 seconds for station %s; "
                "continuing setup while the background listener reconnects",
                self.name,
            )
        self._ma_queue = MusicAssistantQueueAdapter(
            self._ma_client,
            settings[CONF_MA_PLAYER].strip(),
        )
        self._ma_tts = HaTtsUrlRenderer(
            async_get_clientsession(self.hass),
            self.hass.config.internal_url,
            settings[CONF_HA_TOKEN].strip(),
            self.tts_entity_id,
        )

    def _record_story(self, story_id: str | None) -> None:
        """Commit one story after its briefing side effect succeeds."""
        self._recent_story_ids = record_story(
            self._recent_story_ids, story_id, RECENT_STORY_LIMIT
        )

    async def _async_save_controller_state(self) -> None:
        """Persist enabled state and AI DJ-owned queue items."""
        if self._store is None:
            return
        state = ControllerState(
            enabled=self._enabled,
            owned_queue_items=self._owned_queue_items,
            recent_story_ids=tuple(self._recent_story_ids),
        )
        await self._store.async_save(state.as_storage())

    async def _async_recover_controller(self) -> None:
        """Recover enabled state without generating a briefing on restart."""
        state = self.hass.states.get(self.player_entity_id)
        if state is None or state.state != STATE_PLAYING:
            await self._async_remove_owned_queue_items()
            return
        # The next :25/:55 callback will prepare a fresh briefing.

    async def _async_handle_schedule(self, now: datetime) -> None:
        """Prepare a break at :25 for :30, or :55 for the next :00."""
        if now.minute not in (25, 55):
            return
        target = now.replace(minute=30 if now.minute == 25 else 0, second=0, microsecond=0)
        if now.minute == 55:
            target += timedelta(hours=1)
        await self._async_prepare_boundary(target)

    async def _async_prepare_boundary(self, target: datetime) -> None:
        """Generate and queue one fresh briefing for a clock boundary."""
        boundary = target.isoformat()
        if (
            not self._enabled
            or not self.music_assistant_enabled
            or boundary in {metadata.get("boundary") for metadata in self._owned_queue_items.values()}
            or self._preparing_boundary == boundary
        ):
            return
        state = self.hass.states.get(self.player_entity_id)
        if state is None or state.state != STATE_PLAYING:
            return
        self._preparing_boundary = boundary
        try:
            briefing = await self._async_generate_briefing(
                self.settings.get(CONF_WEATHER, ""),
                self.settings.get(CONF_AGENT, ""),
            )
            state = self.hass.states.get(self.player_entity_id)
            if not self._enabled or state is None or state.state != STATE_PLAYING:
                return
            queue_item_id = await self.async_queue_announcement_next(briefing.text)
            self._owned_queue_items[queue_item_id] = {
                "boundary": boundary,
                "created_at": dt_util.now().isoformat(),
            }
            self._record_story(briefing.selected_story_id)
            await self._async_save_controller_state()
        except Exception:  # noqa: BLE001 - failed preparation must not interrupt music
            _LOGGER.exception(
                "AI DJ preparation failed for station %s on %s; skipping break",
                self.name,
                self.player_entity_id,
            )
        finally:
            self._preparing_boundary = None

    async def _async_handle_player_state_changed(self, event: Event) -> None:
        """Remove owned breaks whenever playback is no longer active."""
        new_state = event.data.get("new_state")
        if new_state is not None and new_state.state == STATE_PLAYING:
            return
        self._preparing_boundary = None
        await self._async_remove_owned_queue_items()

    async def _async_remove_owned_queue_items(self) -> None:
        """Remove only queue items created by this station."""
        if not self._owned_queue_items:
            await self._async_save_controller_state()
            return
        item_ids = list(self._owned_queue_items)
        self._owned_queue_items.clear()
        await self._async_save_controller_state()
        if self._ma_queue is None:
            return
        for item_id in item_ids:
            try:
                await self._ma_queue.async_remove(item_id)
            except Exception:  # noqa: BLE001 - item may already have played or vanished
                _LOGGER.debug("AI DJ queue item %s was already unavailable", item_id)

    async def async_queue_announcement_next(self, message: str) -> str:
        """Render a briefing and insert it as the next MA queue item."""
        if self._ma_queue is None or self._ma_tts is None:
            raise ServiceValidationError(
                "Music Assistant native transport is not configured for this station"
            )
        media_uri = await self._ma_tts.async_render(message)
        return await self._ma_queue.async_insert_next(media_uri)

    @property
    def name(self) -> str:
        """Return the station name."""
        return self.settings[CONF_NAME]

    @property
    def player_entity_id(self) -> str:
        """Return the configured media player."""
        return self.settings[CONF_PLAYER]

    @property
    def tts_entity_id(self) -> str:
        """Return the configured TTS entity."""
        return self.settings[CONF_TTS]

    async def _async_generate_briefing(
        self,
        weather_entity_id: str,
        agent_id: str,
        prompt: str | None = None,
    ) -> GeneratedBriefing:
        """Delegate fact collection and grounded speech generation."""
        generator = BriefingGenerationService(
            self.hass,
            self.settings,
            self.player_entity_id,
            self._ma_client,
            self._recent_story_ids,
        )
        return await generator.async_generate(weather_entity_id, agent_id, prompt)

    async def async_generate_briefing(
        self,
        weather_entity_id: str,
        agent_id: str,
        prompt: str | None = None,
    ) -> str:
        """Collect facts and return generated speech without playback side effects."""
        briefing = await self._async_generate_briefing(weather_entity_id, agent_id, prompt)
        self._record_story(briefing.selected_story_id)
        await self._async_save_controller_state()
        return briefing.text

    async def async_briefing_next(
        self,
        weather_entity_id: str,
        agent_id: str,
        prompt: str | None = None,
    ) -> None:
        """Generate a briefing and arm it for the next track boundary."""
        briefing = await self._async_generate_briefing(weather_entity_id, agent_id, prompt)
        if self.music_assistant_enabled:
            await self.async_queue_announcement_next(briefing.text)
            self._record_story(briefing.selected_story_id)
            await self._async_save_controller_state()
            return
        await self.async_announce_next(briefing.text)
        self._record_story(briefing.selected_story_id)
        await self._async_save_controller_state()

    async def async_announce_next(self, message: str) -> None:
        """Speak a message when the configured player advances to another track."""
        self._require_player()
        await self._announcement.async_arm(message)

    async def _async_deliver_announcement(self, message: str) -> None:
        """Deliver a boundary announcement without leaking task failures."""
        try:
            await self.async_announce(message)
        except Exception:  # noqa: BLE001 - background delivery must be isolated
            _LOGGER.exception(
                "Boundary announcement failed for AI DJ station %s on %s",
                self.name,
                self.player_entity_id,
            )

    @callback
    def async_unload(self) -> None:
        """Cancel listeners and stop native MA transport during unload."""
        self._announcement.async_cancel()
        if self._schedule_unsub is not None:
            self._schedule_unsub()
            self._schedule_unsub = None
        if self._player_unsub is not None:
            self._player_unsub()
            self._player_unsub = None
        if self._ma_listener_task is not None:
            self._ma_listener_task.cancel()
            self._ma_listener_task = None
        if self._ma_client is not None:
            self.hass.async_create_background_task(
                self._ma_client.disconnect(),
                "music_assistant_disconnect",
            )
            self._ma_client = None
            self._ma_queue = None

    async def async_get_queue(self) -> Any:
        """Read the active Music Assistant queue through Home Assistant."""
        return await self._ha_queue.async_get_queue()

    async def async_queue_add(self, media_id: str) -> bool:
        """Add one media item unless it is already present in the active queue."""
        return await self._ha_queue.async_add(media_id)

    def _require_player(self) -> None:
        """Raise when the configured media player is not available in HA."""
        if self.hass.states.get(self.player_entity_id) is None:
            raise ServiceValidationError(
                f"Configured media player does not exist: {self.player_entity_id}"
            )

    async def async_announce(self, message: str) -> None:
        """Speak a one-shot message on the configured media player."""
        message = message.strip()
        if not message:
            raise ServiceValidationError("The announcement message must not be empty")

        self._require_player()

        tts_entity = self.hass.states.get(self.tts_entity_id)
        if tts_entity is None:
            raise ServiceValidationError(
                f"Configured TTS entity does not exist: {self.tts_entity_id}"
            )

        try:
            await self.hass.services.async_call(
                "tts",
                "speak",
                {
                    "media_player_entity_id": self.player_entity_id,
                    "message": message,
                    "cache": False,
                },
                target={"entity_id": self.tts_entity_id},
                blocking=True,
            )
        except Exception as err:
            raise HomeAssistantError(
                f"Unable to announce on {self.player_entity_id}: {err}"
            ) from err
