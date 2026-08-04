"""Runtime behavior for an AI DJ config entry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from random import choice, randint
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
from .music_assistant import (
    HaTtsUrlRenderer,
    MusicAssistantClient,
    MusicAssistantQueueAdapter,
    QueueMedia,
)

from .announcement import AnnouncementController, track_identity
from .briefing_generation import BriefingGenerationService, GeneratedBriefing
from .const import CADENCE_CONTENT_FULL, RECENT_STORY_LIMIT, StationSettings
from .story import record_story


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ControllerState:
    """Validated persisted state for one station controller."""

    enabled: bool = False
    owned_queue_items: dict[str, dict[str, str]] = field(default_factory=dict)
    recent_story_ids: tuple[str, ...] = ()
    cadence_track_count: int = 0
    cadence_target: int = 0
    cadence_last_track: str = ""

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
            cadence_track_count=max(0, stored.get("cadence_track_count", 0))
            if isinstance(stored.get("cadence_track_count"), int)
            else 0,
            cadence_target=max(0, stored.get("cadence_target", 0))
            if isinstance(stored.get("cadence_target"), int)
            else 0,
            cadence_last_track=stored.get("cadence_last_track", "")
            if isinstance(stored.get("cadence_last_track"), str)
            else "",
        )

    def as_storage(self) -> dict[str, Any]:
        """Return the stable Home Assistant storage representation."""
        return {
            "enabled": self.enabled,
            "owned_queue_items": self.owned_queue_items,
            "recent_story_ids": list(self.recent_story_ids[-RECENT_STORY_LIMIT:]),
            "cadence_track_count": self.cadence_track_count,
            "cadence_target": self.cadence_target,
            "cadence_last_track": self.cadence_last_track,
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
    _preparing_cadence: bool = False
    _preparation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _operation_generation: int = 0
    _cadence_track_count: int = 0
    _cadence_target: int = 0
    _cadence_last_track: str = ""
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
    def settings(self) -> StationSettings:
        """Return normalized config data with options overriding initial values."""
        return StationSettings.from_mapping({**self.entry.data, **self.entry.options})

    @property
    def enabled(self) -> bool:
        """Return whether scheduled AI DJ breaks are enabled."""
        return self._enabled

    @property
    def music_assistant_enabled(self) -> bool:
        """Return whether the optional native MA transport is configured."""
        return self.settings.music_assistant_enabled

    async def async_initialize_controller(self) -> None:
        """Restore controller state and install the schedule/state listeners."""
        self._store = Store(self.hass, 1, f"aidj.{self.entry.entry_id}")
        state = ControllerState.from_storage(await self._store.async_load())
        self._enabled = state.enabled
        self._owned_queue_items = state.owned_queue_items
        self._recent_story_ids = list(state.recent_story_ids)
        self._cadence_track_count = state.cadence_track_count
        self._cadence_target = state.cadence_target
        self._cadence_last_track = state.cadence_last_track
        if any(
            metadata.get("delete_pending") == "true"
            for metadata in self._owned_queue_items.values()
        ):
            pending_ids = {
                item_id
                for item_id, metadata in self._owned_queue_items.items()
                if metadata.get("delete_pending") == "true"
            }
            await self._async_remove_owned_item_ids(pending_ids)
        current_state = self.hass.states.get(self.player_entity_id)
        if not self._cadence_last_track and current_state is not None:
            self._cadence_last_track = track_identity(current_state) or ""
        settings = self.settings
        cadence_changed = False
        if not settings.cadence_enabled:
            cadence_changed = bool(self._cadence_track_count or self._cadence_target)
            self._cadence_track_count = 0
            self._cadence_target = 0
            await self._async_remove_owned_items(kind="cadence")
        elif not settings.cadence_min_tracks <= self._cadence_target <= settings.cadence_max_tracks:
            self._cadence_track_count = 0
            self._cadence_target = self._choose_cadence_target()
            cadence_changed = True
        self._schedule_unsub = event_helper.async_track_time_change(
            self.hass, self._async_handle_schedule, minute={25, 55}, second=0
        )
        self._player_unsub = event_helper.async_track_state_change_event(
            self.hass, self.player_entity_id, self._async_handle_player_state_changed
        )
        if cadence_changed:
            await self._async_save_controller_state()
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
            self._operation_generation += 1
            self._preparing_boundary = None
            self._preparing_cadence = False
            self._cadence_track_count = 0
            self._cadence_target = 0
            await self._async_remove_owned_queue_items()
        else:
            # Preparation is driven by the next :25/:55 time callback.
            return

    async def async_start_music_assistant(self) -> None:
        """Start and supervise the official MA client when configured."""
        if not self.music_assistant_enabled or self._ma_listener_task is not None:
            return
        init_ready = asyncio.Event()
        self._ma_listener_task = self.entry.async_create_background_task(
            self.hass,
            self._async_run_music_assistant(init_ready),
            name="music_assistant_listener",
        )
        try:
            await asyncio.wait_for(init_ready.wait(), timeout=15)
        except TimeoutError:
            _LOGGER.warning(
                "Music Assistant did not become ready within 15 seconds for station %s; "
                "the background listener will keep retrying",
                self.name,
            )

    async def _async_run_music_assistant(self, first_ready: asyncio.Event) -> None:
        """Reconnect the MA client when startup or an established listener fails."""
        retry_delay = 1
        while True:
            settings = self.settings
            client = MusicAssistantClient(
                settings.music_assistant_url,
                async_get_clientsession(self.hass),
                token=settings.music_assistant_token,
            )
            ready = asyncio.Event()
            self._ma_client = client
            self._ma_queue = MusicAssistantQueueAdapter(
                client,
                settings.music_assistant_player_id,
            )
            self._ma_tts = HaTtsUrlRenderer(
                async_get_clientsession(self.hass),
                self.hass.config.internal_url,
                settings.home_assistant_token,
                self.tts_entity_id,
            )
            listener: asyncio.Task[Any] | None = None
            ready_waiter: asyncio.Task[Any] | None = None
            try:
                listener = asyncio.create_task(client.start_listening(init_ready=ready))
                ready_waiter = asyncio.create_task(ready.wait())
                done, _ = await asyncio.wait(
                    {listener, ready_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if ready_waiter in done and ready.is_set():
                    first_ready.set()
                    retry_delay = 1
                    await listener
                else:
                    ready_waiter.cancel()
                    await listener
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - reconnect after transport failure
                _LOGGER.warning(
                    "Music Assistant listener unavailable for station %s: %s; "
                    "retrying in %s seconds",
                    self.name,
                    err,
                    retry_delay,
                )
            finally:
                if ready_waiter is not None and not ready_waiter.done():
                    ready_waiter.cancel()
                if listener is not None and not listener.done():
                    listener.cancel()
                await client.disconnect()
                if self._ma_client is client:
                    self._ma_client = None
                    self._ma_queue = None
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)

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
            cadence_track_count=self._cadence_track_count,
            cadence_target=self._cadence_target,
            cadence_last_track=self._cadence_last_track,
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

    def _has_prepared_boundary(self, boundary: str) -> bool:
        """Return whether this station already owns a break for the boundary."""
        return boundary in {
            metadata.get("boundary") for metadata in self._owned_queue_items.values()
        }

    def _is_player_playing(self) -> bool:
        """Return whether the configured HA player is actively playing."""
        state = self.hass.states.get(self.player_entity_id)
        return state is not None and state.state == STATE_PLAYING

    def _choose_cadence_target(self) -> int:
        """Choose the next song-count target from normalized station options."""
        settings = self.settings
        return randint(settings.cadence_min_tracks, settings.cadence_max_tracks)

    def _reset_cadence(self) -> None:
        """Start a fresh cadence window after one successful announcement."""
        self._cadence_track_count = 0
        self._cadence_target = self._choose_cadence_target()

    @staticmethod
    def _is_aidj_announcement(state: Any) -> bool:
        """Return whether a player state describes AI DJ's inserted audio item."""
        attributes = getattr(state, "attributes", {})
        title = str(attributes.get("media_title", ""))
        content_id = str(attributes.get("media_content_id", ""))
        return title.startswith("AI DJ ") or "AI DJ " in content_id

    def _record_owned_sequence(
        self, queue_item_ids: str | list[str], metadata: dict[str, str]
    ) -> None:
        """Record every queue item inserted for one announcement break."""
        item_ids = [queue_item_ids] if isinstance(queue_item_ids, str) else queue_item_ids
        created_at = dt_util.now()
        for position, item_id in enumerate(item_ids):
            self._owned_queue_items[item_id] = {
                **metadata,
                "created_at": (created_at + timedelta(microseconds=position)).isoformat(),
            }

    async def _async_prepare_boundary(self, target: datetime) -> None:
        """Serialize generation and queueing of one clock briefing."""
        async with self._preparation_lock:
            await self._async_prepare_boundary_locked(target)

    async def _async_prepare_boundary_locked(self, target: datetime) -> None:
        """Generate and queue one fresh briefing while holding the operation lock."""
        boundary = target.isoformat()
        if not self._enabled or not self.music_assistant_enabled:
            return
        await self._async_remove_owned_items(kind="cadence")
        if any("cadence" in metadata for metadata in self._owned_queue_items.values()):
            _LOGGER.warning(
                "Skipping scheduled AI DJ briefing for station %s because a cadence item "
                "could not be removed",
                self.name,
            )
            return
        if self._has_prepared_boundary(boundary) or self._preparing_boundary == boundary:
            return
        if not self._is_player_playing():
            return
        self._preparing_boundary = boundary
        operation_generation = self._operation_generation
        try:
            briefing = await self._async_generate_briefing(
                self.settings.weather_entity_id,
                self.settings.agent_id,
            )
            if (
                operation_generation != self._operation_generation
                or not self._enabled
                or not self._is_player_playing()
            ):
                return
            queue_item_ids = await self.async_queue_announcement_next(briefing.text)
            self._record_owned_sequence(queue_item_ids, {"boundary": boundary})
            if operation_generation != self._operation_generation:
                await self._async_remove_owned_items()
                return
            self._record_story(briefing.selected_story_id)
            self._reset_cadence()
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
        """Track completed songs for cadence and clean up when playback stops."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state != STATE_PLAYING:
            self._operation_generation += 1
            self._preparing_boundary = None
            self._preparing_cadence = False
            await self._async_remove_owned_queue_items()
            return
        current = track_identity(new_state)
        if current is None or current == self._cadence_last_track:
            return
        old_state = event.data.get("old_state")
        self._cadence_last_track = current
        if self._is_aidj_announcement(new_state):
            await self._async_retire_playing_announcement()
            return
        if (
            old_state is None
            or old_state.state != STATE_PLAYING
            or track_identity(old_state) is None
            or self._is_aidj_announcement(old_state)
        ):
            await self._async_save_controller_state()
            return
        settings = self.settings
        if not self._enabled or not settings.cadence_enabled:
            await self._async_save_controller_state()
            return
        self._cadence_track_count += 1
        if self._cadence_target <= 0:
            self._cadence_target = self._choose_cadence_target()
        await self._async_save_controller_state()
        if self._cadence_track_count >= self._cadence_target:
            await self._async_prepare_cadence()

    async def _async_prepare_cadence(self) -> None:
        """Serialize generation and queueing of one cadence transition."""
        async with self._preparation_lock:
            await self._async_prepare_cadence_locked()

    async def _async_prepare_cadence_locked(self) -> None:
        """Generate one cadence transition while holding the operation lock."""
        if self._preparing_cadence or self._preparing_boundary is not None:
            return
        if self._has_prepared_boundary_for_any_time():
            return
        self._preparing_cadence = True
        operation_generation = self._operation_generation
        try:
            settings = self.settings
            generator = BriefingGenerationService(
                self.hass,
                settings,
                self.player_entity_id,
                self._ma_client,
                self._recent_story_ids,
            )
            if settings.cadence_content == CADENCE_CONTENT_FULL:
                briefing = await generator.async_generate(
                    settings.weather_entity_id,
                    settings.agent_id,
                )
            else:
                briefing = await generator.async_generate_music_transition(
                    settings.agent_id
                )
            if (
                operation_generation != self._operation_generation
                or not self._enabled
                or not self._is_player_playing()
            ):
                return
            queue_item_ids = await self.async_queue_announcement_next(briefing.text)
            self._record_owned_sequence(queue_item_ids, {"cadence": "true"})
            if operation_generation != self._operation_generation:
                await self._async_remove_owned_items()
                return
            self._record_story(briefing.selected_story_id)
            self._reset_cadence()
            await self._async_save_controller_state()
        except Exception:  # noqa: BLE001 - cadence failure must not interrupt music
            _LOGGER.exception(
                "AI DJ cadence transition failed for station %s on %s; retrying after "
                "the next song",
                self.name,
                self.player_entity_id,
            )
        finally:
            self._preparing_cadence = False

    def _has_prepared_boundary_for_any_time(self) -> bool:
        """Return whether a scheduled clock briefing is already queued."""
        return any("boundary" in metadata for metadata in self._owned_queue_items.values())

    async def _async_retire_playing_announcement(self) -> None:
        """Forget the oldest owned item once its audio has begun playing."""
        if not self._owned_queue_items:
            return

        item_id = min(
            self._owned_queue_items,
            key=lambda key: self._owned_queue_items[key].get("created_at", ""),
        )
        self._owned_queue_items.pop(item_id, None)
        await self._async_save_controller_state()

    async def _async_remove_owned_queue_items(self) -> None:
        """Remove every queue item created by this station."""
        await self._async_remove_owned_items()

    async def _async_remove_owned_items(self, kind: str | None = None) -> None:
        """Delete selected owned items, retaining records when deletion cannot run."""
        item_ids = [
            item_id
            for item_id, metadata in self._owned_queue_items.items()
            if kind is None or kind in metadata
        ]
        if not item_ids:
            await self._async_save_controller_state()
            return
        for item_id in item_ids:
            self._owned_queue_items[item_id]["delete_pending"] = "true"
        await self._async_save_controller_state()
        if self._ma_queue is None:
            return
        await self._async_remove_owned_item_ids(set(item_ids))

    async def _async_remove_owned_item_ids(self, item_ids: set[str]) -> None:
        """Attempt deletion of already-marked items and persist confirmed removals."""
        if self._ma_queue is None:
            return
        changed = False
        for item_id in item_ids:
            try:
                await self._ma_queue.async_remove(item_id)
            except Exception:  # noqa: BLE001 - retain ownership for a later retry
                _LOGGER.debug("AI DJ queue item %s could not be removed yet", item_id)
                continue
            self._owned_queue_items.pop(item_id, None)
            changed = True
        if changed:
            await self._async_save_controller_state()

    async def async_queue_announcement_next(self, message: str) -> list[str]:
        """Render and atomically insert one imaged announcement sequence."""
        if self._ma_queue is None or self._ma_tts is None:
            raise ServiceValidationError(
                "Music Assistant native transport is not configured for this station"
            )
        settings = self.settings
        announcement_uri = await self._ma_tts.async_render(message)
        media: list[QueueMedia] = []
        if settings.jingle_urls:
            media.append(QueueMedia(choice(settings.jingle_urls), "AI DJ Jingle"))
        media.append(QueueMedia(announcement_uri, "AI DJ Announcement"))
        if settings.stinger_urls:
            media.append(QueueMedia(choice(settings.stinger_urls), "AI DJ Stinger"))
        return await self._ma_queue.async_insert_sequence(media)

    @property
    def name(self) -> str:
        """Return the station name."""
        return self.settings.name

    @property
    def player_entity_id(self) -> str:
        """Return the configured media player."""
        return self.settings.player_entity_id

    @property
    def tts_entity_id(self) -> str:
        """Return the configured TTS entity."""
        return self.settings.tts_entity_id

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
            queue_item_ids = await self.async_queue_announcement_next(briefing.text)
            self._record_owned_sequence(queue_item_ids, {"manual": "true"})
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
        self._operation_generation += 1
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
            self._ma_tts = None

    async def async_get_queue(self) -> Any:
        """Read the active Music Assistant queue through Home Assistant."""
        return await self._ha_queue.async_get_queue()

    async def async_queue_add(self, media_id: str) -> bool:
        """Add one media item unless it is already present in the active queue."""
        return await self._ha_queue.async_add(media_id)

    def _require_player(self) -> None:
        """Raise when the configured media player is not available in HA."""
        if not self.player_entity_id.startswith("media_player."):
            raise ServiceValidationError(
                f"Configured player is not a media_player entity: {self.player_entity_id}"
            )
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

        if not self.tts_entity_id.startswith("tts."):
            raise ServiceValidationError(
                f"Configured TTS target is not a tts entity: {self.tts_entity_id}"
            )
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
