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
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import event as event_helper
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .music_assistant import HaTtsUrlRenderer, MusicAssistantClient, MusicAssistantQueueAdapter

from .briefing import (
    AqiEntityProvider,
    BriefingItem,
    CalendarEventProvider,
    FeedreaderEventProvider,
    HaConversationBriefingGenerator,
    QueueProvider,
    WeatherEntityProvider,
    async_collect_briefing,
)
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
from .prompt import build_briefing_prompt


_LOGGER = logging.getLogger(__name__)


def _track_identity(state: Any) -> str | None:
    """Return a stable identity for the currently playing media item."""
    attributes = getattr(state, "attributes", {})
    parts = tuple(
        str(attributes.get(key, ""))
        for key in ("media_content_id", "media_artist", "media_title")
    )
    return ":".join(parts) if any(parts) else None


def queue_media_ids(queue: Any) -> set[str]:
    """Extract media URIs and queue item IDs from an HA queue response."""
    if not isinstance(queue, dict):
        return set()

    queue_data = queue.get("service_response", queue)
    if not isinstance(queue_data, dict):
        return set()

    player_queue = next(iter(queue_data.values()), queue_data)
    if not isinstance(player_queue, dict):
        return set()

    media_ids: set[str] = set()
    for item_key in ("current_item", "next_item"):
        item = player_queue.get(item_key)
        if not isinstance(item, dict):
            continue
        for key in ("queue_item_id", "media_item_id"):
            value = item.get(key)
            if isinstance(value, str):
                media_ids.add(value)
        media_item = item.get("media_item")
        if isinstance(media_item, dict):
            uri = media_item.get("uri")
            if isinstance(uri, str):
                media_ids.add(uri)

    return media_ids


@dataclass(slots=True)
class AiDjRuntime:
    """Runtime object for one configured AI DJ station."""

    hass: HomeAssistant
    entry: ConfigEntry
    _owned_media_ids: set[str] = field(default_factory=set)
    _owned_queue_items: dict[str, dict[str, str]] = field(default_factory=dict)
    _recent_story_ids: list[str] = field(default_factory=list)
    _selected_story_id: str | None = None
    _enabled: bool = False
    _store: Store[dict[str, Any]] | None = None
    _schedule_unsub: Any = None
    _player_unsub: Any = None
    _preparing_boundary: str | None = None
    _pending_announcement: tuple[str, str] | None = None
    _announcement_unsub: Any = None
    _ma_client: MusicAssistantClient | None = None
    _ma_listener_task: Any = None
    _ma_queue: MusicAssistantQueueAdapter | None = None
    _ma_tts: HaTtsUrlRenderer | None = None

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
        stored = await self._store.async_load() or {}
        self._enabled = bool(stored.get("enabled", False))
        stored_items = stored.get("owned_queue_items", {})
        if isinstance(stored_items, dict):
            self._owned_queue_items = {
                str(item_id): {
                    str(key): str(value)
                    for key, value in metadata.items()
                    if isinstance(key, str) and isinstance(value, (str, int, float))
                }
                for item_id, metadata in stored_items.items()
                if isinstance(metadata, dict)
            }
        stored_story_ids = stored.get("recent_story_ids", [])
        if isinstance(stored_story_ids, list):
            self._recent_story_ids = [
                str(story_id) for story_id in stored_story_ids if isinstance(story_id, str)
            ][-RECENT_STORY_LIMIT:]
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

    def _select_feed_story(self, items: list[BriefingItem]) -> BriefingItem | None:
        """Choose an unseen feed story, or the least-recently-used available one."""
        feed_items = [
            item for item in items
            if item.provider.startswith("feedreader:") and item.identity
        ]
        if not feed_items:
            self._selected_story_id = None
            return None
        recent = set(self._recent_story_ids)
        for item in feed_items:
            if item.identity not in recent:
                self._selected_story_id = item.identity
                return item
        oldest = min(
            feed_items,
            key=lambda item: self._recent_story_ids.index(item.identity)
            if item.identity in self._recent_story_ids
            else -1,
        )
        self._selected_story_id = oldest.identity
        return oldest

    def _record_selected_story(self) -> None:
        """Commit the selected story after successful queue/arming."""
        if self._selected_story_id is None:
            return
        self._recent_story_ids = [
            story_id for story_id in self._recent_story_ids
            if story_id != self._selected_story_id
        ]
        self._recent_story_ids.append(self._selected_story_id)
        self._recent_story_ids = self._recent_story_ids[-RECENT_STORY_LIMIT:]
        self._selected_story_id = None

    async def _async_save_controller_state(self) -> None:
        """Persist enabled state and AI DJ-owned queue items."""
        if self._store is None:
            return
        await self._store.async_save(
            {
                "enabled": self._enabled,
                "owned_queue_items": self._owned_queue_items,
                "recent_story_ids": self._recent_story_ids[-RECENT_STORY_LIMIT:],
            }
        )

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
            message = await self.async_generate_briefing(
                self.settings.get(CONF_WEATHER, ""),
                self.settings.get(CONF_AGENT, ""),
                consume_story=False,
            )
            state = self.hass.states.get(self.player_entity_id)
            if not self._enabled or state is None or state.state != STATE_PLAYING:
                return
            queue_item_id = await self.async_queue_announcement_next(message)
            self._owned_queue_items[queue_item_id] = {
                "boundary": boundary,
                "created_at": dt_util.now().isoformat(),
            }
            self._record_selected_story()
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

    async def async_generate_briefing(
        self,
        weather_entity_id: str,
        agent_id: str,
        prompt: str | None = None,
        consume_story: bool = True,
    ) -> str:
        """Collect weather and generate a briefing without playback side effects."""
        weather_entity_id = (weather_entity_id or self.settings.get(CONF_WEATHER, "")).strip()
        agent_id = (agent_id or self.settings.get(CONF_AGENT, "")).strip()
        if not weather_entity_id or not agent_id:
            raise ServiceValidationError(
                "weather_entity_id and agent_id must not be empty"
            )

        feed_entity_ids = self.settings.get(CONF_FEEDS, [])
        calendar_entity_ids = self.settings.get(CONF_CALENDARS, [])
        aqi_entity_id = self.settings.get(CONF_AQI, "").strip()
        aqi_threshold = float(self.settings.get(CONF_AQI_THRESHOLD, "101"))
        providers = [WeatherEntityProvider(self.hass, weather_entity_id)]
        providers.extend(
            FeedreaderEventProvider(self.hass, entity_id, name=f"feedreader:{entity_id}")
            for entity_id in feed_entity_ids
        )
        providers.extend(
            CalendarEventProvider(self.hass, entity_id, name=f"calendar:{entity_id}")
            for entity_id in calendar_entity_ids
        )
        if aqi_entity_id:
            providers.append(AqiEntityProvider(self.hass, aqi_entity_id, aqi_threshold))
        providers.append(QueueProvider(self.hass, self.player_entity_id, self._ma_client))

        items, errors = await async_collect_briefing(tuple(providers))
        weather_items = [item for item in items if item.provider == "weather"]
        self._selected_story_id = None
        selected_story = self._select_feed_story(items)
        if selected_story is not None:
            items = [
                item for item in items
                if not item.provider.startswith("feedreader:")
            ] + [selected_story]
        if errors:
            _LOGGER.info(
                "Optional briefing providers unavailable for station %s: %s",
                self.name,
                ", ".join(f"{name}: {error}" for name, error in errors.items()),
            )
        if not weather_items:
            _LOGGER.warning("Briefing weather entity is unavailable: %s", weather_entity_id)
            raise ServiceValidationError(
                f"Weather entity does not exist: {weather_entity_id}"
            )

        full_prompt = build_briefing_prompt(items, prompt)
        generated = await HaConversationBriefingGenerator(self.hass, agent_id).async_generate(
            full_prompt
        )
        if consume_story:
            self._record_selected_story()
            await self._async_save_controller_state()
        return generated

    async def async_briefing_next(
        self,
        weather_entity_id: str,
        agent_id: str,
        prompt: str | None = None,
    ) -> None:
        """Generate a briefing and arm it for the next track boundary."""
        message = await self.async_generate_briefing(
            weather_entity_id, agent_id, prompt, consume_story=False
        )
        if self.music_assistant_enabled:
            await self.async_queue_announcement_next(message)
            self._record_selected_story()
            await self._async_save_controller_state()
            return
        await self.async_announce_next(message)
        self._record_selected_story()
        await self._async_save_controller_state()

    async def async_announce_next(self, message: str) -> None:
        """Speak a message when the configured player advances to another track."""
        message = message.strip()
        if not message:
            raise ServiceValidationError("The announcement message must not be empty")

        self._require_player()
        state = self.hass.states.get(self.player_entity_id)
        if state is None:
            raise ServiceValidationError(
                f"Configured media player does not exist: {self.player_entity_id}"
            )
        if state.state != STATE_PLAYING:
            raise ServiceValidationError(
                "announce_next requires the configured media player to be playing"
            )

        baseline = _track_identity(state)
        if baseline is None:
            raise ServiceValidationError(
                "The configured media player has no identifiable current track"
            )

        if self._announcement_unsub is not None:
            self._announcement_unsub()
        self._pending_announcement = (baseline, message)
        self._announcement_unsub = event_helper.async_track_state_change_event(
            self.hass,
            self.player_entity_id,
            self._async_handle_state_changed,
        )

    @callback
    def _async_handle_state_changed(self, event: Event) -> None:
        """Handle a configured-player track transition asynchronously."""
        if self._pending_announcement is None:
            return
        data = event.data
        if data.get("entity_id") != self.player_entity_id:
            return

        new_state = data.get("new_state")
        if new_state is None or new_state.state != STATE_PLAYING:
            return

        baseline, message = self._pending_announcement
        current = _track_identity(new_state)
        if current is None or current == baseline:
            return

        self._pending_announcement = None
        if self._announcement_unsub is not None:
            self._announcement_unsub()
            self._announcement_unsub = None
        self.hass.async_create_task(self._async_deliver_announcement(message))

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
        self._pending_announcement = None
        if self._schedule_unsub is not None:
            self._schedule_unsub()
            self._schedule_unsub = None
        if self._player_unsub is not None:
            self._player_unsub()
            self._player_unsub = None
        if self._announcement_unsub is not None:
            self._announcement_unsub()
            self._announcement_unsub = None
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
        self._require_player()
        try:
            return await self.hass.services.async_call(
                "music_assistant",
                "get_queue",
                target={"entity_id": self.player_entity_id},
                blocking=True,
                return_response=True,
            )
        except Exception as err:
            raise HomeAssistantError(
                f"Unable to read the Music Assistant queue for {self.player_entity_id}: {err}"
            ) from err

    async def async_queue_add(self, media_id: str) -> bool:
        """Add one media item unless it is already present in the active queue."""
        media_id = media_id.strip()
        if not media_id:
            raise ServiceValidationError("The media ID must not be empty")

        if media_id in self._owned_media_ids:
            return False

        queue = await self.async_get_queue()
        if media_id in queue_media_ids(queue):
            self._owned_media_ids.add(media_id)
            return False

        self._require_player()
        try:
            await self.hass.services.async_call(
                "music_assistant",
                "play_media",
                {"media_id": media_id, "enqueue": "add"},
                target={"entity_id": self.player_entity_id},
                blocking=True,
            )
        except Exception as err:
            raise HomeAssistantError(
                f"Unable to add media to the Music Assistant queue for "
                f"{self.player_entity_id}: {err}"
            ) from err

        self._owned_media_ids.add(media_id)
        return True

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
