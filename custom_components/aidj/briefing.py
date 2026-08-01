"""Provider-neutral briefing data and Home Assistant-backed sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import logging
from typing import Any, Protocol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
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
    identity: str | None = None


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
        """Return current conditions plus the relevant daily forecast."""
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

        now = datetime.now().astimezone()
        forecast_days = 1 if now.hour < 12 else 2
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "daily", "entity_id": self.entity_id},
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - forecast is optional context
            _LOGGER.info("Weather forecast unavailable for %s: %s", self.entity_id, err)
            response = None

        forecast = response.get(self.entity_id, {}).get("forecast", []) if isinstance(response, dict) else []
        if isinstance(forecast, list):
            for index, period in enumerate(forecast[:forecast_days]):
                if not isinstance(period, dict):
                    continue
                period_date = period.get("datetime") or period.get("date")
                label = "today" if index == 0 else "tomorrow"
                forecast_details = [label]
                if period_date:
                    forecast_details.append(f"date: {period_date}")
                if (condition := period.get("condition")):
                    forecast_details.append(f"conditions: {condition}")
                if (high := period.get("temperature")) is not None:
                    forecast_details.append(f"high: {high}{attributes.get('temperature_unit', '')}")
                if (low := period.get("templow")) is not None:
                    forecast_details.append(f"low: {low}{attributes.get('temperature_unit', '')}")
                if (precipitation := period.get("precipitation_probability")) is not None:
                    forecast_details.append(f"precipitation probability: {precipitation}%")
                details.append("forecast " + ", ".join(str(value) for value in forecast_details))

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
class AqiEntityProvider:
    """Expose AQI only when it reaches the moderate-or-worse range."""

    hass: HomeAssistant
    entity_id: str
    relevance_threshold: float = 51
    name: str = "air_quality"

    async def async_collect(self) -> list[BriefingItem]:
        """Return an interpreted AQI fact when outdoor air quality matters."""
        state = self.hass.states.get(self.entity_id)
        if state is None:
            return []
        try:
            aqi = float(state.state)
        except (TypeError, ValueError):
            return []
        if aqi < self.relevance_threshold:
            return []

        if aqi <= 100:
            category = "moderate"
        elif aqi <= 150:
            category = "unhealthy for sensitive groups"
        elif aqi <= 200:
            category = "unhealthy"
        elif aqi <= 300:
            category = "very unhealthy"
        else:
            category = "hazardous"
        friendly_name = state.attributes.get("friendly_name", self.entity_id)
        return [
            BriefingItem(
                provider=self.name,
                title=str(friendly_name),
                summary=f"{friendly_name}: AQI {aqi:g}, {category}",
                occurred_at=state.last_updated,
                source=self.entity_id,
            )
        ]


def _feed_item_identity(entry: dict[str, Any], feed_source: str) -> str:
    """Return a stable identity for one Feedreader item."""
    for key in ("link", "id"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    fallback = "|".join(
        str(entry.get(key) or "").strip()
        for key in ("title", "published", "updated", "description")
    )
    return "sha256:" + hashlib.sha256(f"{feed_source}|{fallback}".encode()).hexdigest()


def _normalize_feed_entry(
    entry: dict[str, Any],
    *,
    feed_source: str,
    occurred_at: datetime | None,
    provider_name: str,
) -> BriefingItem | None:
    """Normalize one native Feedreader coordinator entry."""
    title = entry.get("title") or entry.get("name")
    if not isinstance(title, str) or not title.strip():
        return None
    description = (
        entry.get("description")
        or entry.get("summary")
        or entry.get("content")
        or ""
    )
    if isinstance(description, list) and description and isinstance(description[0], dict):
        description = description[0].get("value", "")
    if not isinstance(description, str):
        description = str(description)
    description = " ".join(description.split())[:600]
    title = title.strip()
    summary = f"Latest local news: {title}"
    if description:
        summary = f"{summary}. {description}"
    link = entry.get("link")
    source = link.strip() if isinstance(link, str) and link.strip() else feed_source
    return BriefingItem(
        provider=provider_name,
        title=title,
        summary=summary,
        occurred_at=occurred_at,
        source=source,
        identity=_feed_item_identity(entry, feed_source),
    )


@dataclass(frozen=True, slots=True)
class FeedreaderEventProvider:
    """Expose Feedreader's bounded native coordinator list as briefing facts."""

    hass: HomeAssistant
    entity_id: str
    name: str = "feedreader"

    async def async_collect(self) -> list[BriefingItem]:
        """Return all entries retained by Feedreader, newest first."""
        state = self.hass.states.get(self.entity_id)
        if state is None:
            return []

        feed_source = self.entity_id
        entries: list[dict[str, Any]] | None = None
        registry_entry = er.async_get(self.hass).async_get(self.entity_id)
        if registry_entry and registry_entry.config_entry_id:
            config_entry = self.hass.config_entries.async_get_entry(
                registry_entry.config_entry_id
            )
            coordinator = getattr(config_entry, "runtime_data", None) if config_entry else None
            configured_url = getattr(coordinator, "url", None)
            if isinstance(configured_url, str) and configured_url:
                feed_source = configured_url
            coordinator_data = getattr(coordinator, "data", None)
            if isinstance(coordinator_data, list):
                entries = [entry for entry in coordinator_data if isinstance(entry, dict)]

        if entries is None:
            event_data = state.attributes.get("event_data")
            if not isinstance(event_data, dict):
                event_data = state.attributes
            entries = [event_data]

        return [
            item
            for entry in entries
            if (item := _normalize_feed_entry(
                entry,
                feed_source=feed_source,
                occurred_at=state.last_updated,
                provider_name=self.name,
            ))
            is not None
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
class CalendarEventProvider:
    """Expose only near-term events from one Home Assistant calendar entity."""

    hass: HomeAssistant
    entity_id: str
    days: int = 7
    relevance_days: int = 2
    max_results: int = 20
    name: str = "calendar"

    async def async_collect(self) -> list[BriefingItem]:
        """Return upcoming calendar events without changing the calendar."""
        state = self.hass.states.get(self.entity_id)
        if state is None:
            return []

        start = datetime.now().astimezone()
        end = start + timedelta(days=self.days)
        response = await self.hass.services.async_call(
            "calendar",
            "get_events",
            {
                "entity_id": self.entity_id,
                "start_date_time": start.isoformat(),
                "end_date_time": end.isoformat(),
            },
            blocking=True,
            return_response=True,
        )
        if not isinstance(response, dict):
            return []
        calendar_data = response.get(self.entity_id)
        if not isinstance(calendar_data, dict):
            return []
        events = calendar_data.get("events")
        if not isinstance(events, list):
            return []

        cutoff = start + timedelta(days=self.relevance_days)
        friendly_name = state.attributes.get("friendly_name", self.entity_id)
        items: list[BriefingItem] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            start_value = event.get("start")
            event_start: datetime | None = None
            if isinstance(start_value, dict):
                raw_start = start_value.get("dateTime")
                if isinstance(raw_start, str):
                    try:
                        event_start = datetime.fromisoformat(raw_start)
                    except ValueError:
                        continue
                elif isinstance(start_value.get("date"), str):
                    try:
                        event_start = datetime.fromisoformat(start_value["date"]).replace(
                            tzinfo=start.tzinfo
                        )
                    except ValueError:
                        continue
            if event_start is None or event_start > cutoff:
                continue
            if len(items) >= self.max_results:
                break

            summary = event.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                continue
            start_value = event.get("start")
            end_value = event.get("end")
            details = f"{friendly_name}: {summary.strip()}"
            if isinstance(start_value, dict) and start_value.get("date"):
                details += f" (all day on {start_value['date']})"
                occurred_at = None
            elif isinstance(start_value, dict) and start_value.get("dateTime"):
                details += f" (starts {start_value['dateTime']})"
                occurred_at = None
            else:
                occurred_at = None
            if isinstance(end_value, dict) and end_value.get("dateTime"):
                details += f", ends {end_value['dateTime']}"
            location = event.get("location")
            if isinstance(location, str) and location.strip():
                details += f", location: {location.strip()}"
            uid = event.get("uid")
            identity = f"{self.entity_id}:{uid}" if isinstance(uid, str) and uid else None
            items.append(
                BriefingItem(
                    provider=self.name,
                    title=summary.strip(),
                    summary=details,
                    source=self.entity_id,
                    identity=identity,
                )
            )
        return items


@dataclass(frozen=True, slots=True)
class QueueProvider:
    """Expose a structured ±3 Music Assistant queue window through HA."""

    hass: HomeAssistant
    player_entity_id: str
    music_assistant_client: Any = None
    name: str = "music_assistant_queue"

    @staticmethod
    def _track_context(item: Any) -> dict[str, Any]:
        """Normalize one MA queue item into nullable DJ context fields."""
        media_item = item.get("media_item") if isinstance(item, dict) else None
        if not isinstance(media_item, dict):
            return {"artist": None, "album": None, "track": None, "genre": None, "year": None}
        artists = media_item.get("artists") or []
        artist_names = [
            artist.get("name")
            for artist in artists
            if isinstance(artist, dict) and isinstance(artist.get("name"), str)
        ]
        album = media_item.get("album")
        album_name = album.get("name") if isinstance(album, dict) else None
        metadata = media_item.get("metadata") or {}
        genres = metadata.get("genres") if isinstance(metadata, dict) else None
        genre = ", ".join(sorted(genres)) if isinstance(genres, (list, set, tuple)) else None
        year = album.get("year") if isinstance(album, dict) else None
        return {
            "artist": ", ".join(artist_names) or None,
            "album": album_name if isinstance(album_name, str) else None,
            "track": media_item.get("name") or item.get("name"),
            "genre": genre,
            "year": year if isinstance(year, int) else None,
        }

    async def _async_collect_native(self) -> list[BriefingItem]:
        """Collect the absolute ±3 window from the official MA client."""
        queue = await self.music_assistant_client.player_queues.get_active_queue(
            self.player_entity_id
        )
        if queue is None or queue.current_index is None:
            return []
        queue_items = await self.music_assistant_client.player_queues.get_queue_items(
            queue.queue_id
        )
        current_index = queue.current_index
        selected = [
            ("previous", item)
            for item in queue_items
            if current_index - 3 <= item.index < current_index
        ] + [
            ("next", item)
            for item in queue_items
            if current_index < item.index <= current_index + 3
        ]
        selected.sort(key=lambda pair: pair[1].index)
        context_by_side: dict[str, list[dict[str, Any]]] = {"previous": [], "next": []}
        for label, item in selected:
            context = self._track_context(item.to_dict())
            if context["track"]:
                context_by_side[label].append(context)
        if not context_by_side["previous"] and not context_by_side["next"]:
            return []
        return [
            BriefingItem(
                provider=self.name,
                title="Music context",
                summary=json.dumps(context_by_side, sort_keys=True),
            )
        ]

    async def async_collect(self) -> list[BriefingItem]:
        """Return up to three previous and next structured queue tracks."""
        if self.music_assistant_client is not None:
            return await self._async_collect_native()
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

        context_by_side: dict[str, list[dict[str, Any]]] = {"previous": [], "next": []}
        for label, key in (("previous", "previous_items"), ("next", "next_items")):
            queue_items = player_queue.get(key, [])
            if not isinstance(queue_items, list):
                queue_items = []
            if not queue_items and key == "previous_items" and player_queue.get("current_item"):
                queue_items = [player_queue["current_item"]]
            if not queue_items and key == "next_items" and player_queue.get("next_item"):
                queue_items = [player_queue["next_item"]]
            for offset, item in enumerate(queue_items[-3:] if label == "previous" else queue_items[:3], 1):
                context = self._track_context(item)
                if not context["track"]:
                    continue
                context_by_side[label].append(context)
        if not context_by_side["previous"] and not context_by_side["next"]:
            return []
        return [
            BriefingItem(
                provider=self.name,
                title="Music context",
                summary=json.dumps(context_by_side, sort_keys=True),
            )
        ]


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
