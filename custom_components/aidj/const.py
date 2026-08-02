"""Constants for the AI DJ integration."""

from dataclasses import dataclass
from math import isfinite
from typing import Any, Final, Mapping

DOMAIN: Final = "aidj"
SERVICE_ANNOUNCE: Final = "announce"
SERVICE_ANNOUNCE_NEXT: Final = "announce_next"
SERVICE_BRIEFING: Final = "briefing"
SERVICE_BRIEFING_NEXT: Final = "briefing_next"
SERVICE_QUEUE_ADD: Final = "queue_add"

CONF_NAME: Final = "name"
CONF_PLAYER: Final = "player"
CONF_MA_URL: Final = "music_assistant_url"
CONF_MA_TOKEN: Final = "music_assistant_token"
CONF_HA_TOKEN: Final = "home_assistant_token"
CONF_MA_PLAYER: Final = "music_assistant_player"
CONF_TTS: Final = "tts"
CONF_WEATHER: Final = "weather_entity_id"
CONF_AGENT: Final = "agent_id"
CONF_FEEDS: Final = "feed_entity_ids"
CONF_CALENDARS: Final = "calendar_entity_ids"
CONF_AQI: Final = "aqi_entity_id"
CONF_AQI_THRESHOLD: Final = "aqi_relevance_threshold"
CONF_CONFIG_ENTRY_ID: Final = "config_entry_id"

ATTR_MESSAGE: Final = "message"
ATTR_MEDIA_ID: Final = "media_id"
ATTR_PROMPT: Final = "prompt"

PROVIDER_WEATHER: Final = "weather"
PROVIDER_FEEDREADER: Final = "feedreader"
PROVIDER_FEEDREADER_PREFIX: Final = f"{PROVIDER_FEEDREADER}:"
PROVIDER_MUSIC_ASSISTANT_QUEUE: Final = "music_assistant_queue"


DEFAULT_NAME: Final = "AI DJ"
DEFAULT_MA_URL: Final = "http://homeassistant.local:8095"
RECENT_STORY_LIMIT: Final[int] = 10


def _string_list(value: Any) -> tuple[str, ...]:
    """Normalize a config-entry multi-entity value to non-empty strings."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


@dataclass(frozen=True, slots=True)
class StationSettings:
    """Typed, normalized settings for one configured AI DJ station."""

    name: str
    player_entity_id: str
    tts_entity_id: str
    music_assistant_url: str = ""
    music_assistant_token: str = ""
    home_assistant_token: str = ""
    music_assistant_player_id: str = ""
    weather_entity_id: str = ""
    agent_id: str = ""
    feed_entity_ids: tuple[str, ...] = ()
    calendar_entity_ids: tuple[str, ...] = ()
    aqi_entity_id: str = ""
    aqi_relevance_threshold: float = 101.0

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> StationSettings:
        """Normalize config-entry data at the Home Assistant boundary."""
        def text(key: str) -> str:
            value = values.get(key, "")
            return value.strip() if isinstance(value, str) else ""

        try:
            aqi_threshold = float(values.get(CONF_AQI_THRESHOLD, 101))
            if not isfinite(aqi_threshold) or not 0 <= aqi_threshold <= 500:
                raise ValueError
        except (TypeError, ValueError):
            aqi_threshold = 101.0
        return cls(
            name=text(CONF_NAME),
            player_entity_id=text(CONF_PLAYER),
            tts_entity_id=text(CONF_TTS),
            music_assistant_url=text(CONF_MA_URL),
            music_assistant_token=text(CONF_MA_TOKEN),
            home_assistant_token=text(CONF_HA_TOKEN),
            music_assistant_player_id=text(CONF_MA_PLAYER),
            weather_entity_id=text(CONF_WEATHER),
            agent_id=text(CONF_AGENT),
            feed_entity_ids=_string_list(values.get(CONF_FEEDS)),
            calendar_entity_ids=_string_list(values.get(CONF_CALENDARS)),
            aqi_entity_id=text(CONF_AQI),
            aqi_relevance_threshold=aqi_threshold,
        )

    @property
    def music_assistant_enabled(self) -> bool:
        """Return whether every native Music Assistant setting is present."""
        return bool(
            self.music_assistant_url
            and self.music_assistant_token
            and self.home_assistant_token
            and self.music_assistant_player_id
        )
