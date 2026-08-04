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
CONF_PERSONALITY: Final = "personality"
CONF_CUSTOM_PERSONALITY: Final = "custom_personality"
CONF_JINGLE_URLS: Final = "jingle_urls"
CONF_STINGER_URLS: Final = "stinger_urls"
CONF_CADENCE_ENABLED: Final = "cadence_enabled"
CONF_CADENCE_MIN_TRACKS: Final = "cadence_min_tracks"
CONF_CADENCE_MAX_TRACKS: Final = "cadence_max_tracks"
CONF_CADENCE_CONTENT: Final = "cadence_content"
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
DEFAULT_PERSONALITY: Final = "balanced"
CUSTOM_PERSONALITY: Final = "custom"
CADENCE_CONTENT_MUSIC: Final = "music"
CADENCE_CONTENT_FULL: Final = "full"
DEFAULT_CADENCE_MIN_TRACKS: Final = 3
DEFAULT_CADENCE_MAX_TRACKS: Final = 5
PERSONALITY_LABELS: Final[dict[str, str]] = {
    "balanced": "Balanced",
    "bright_brisk": "Bright & brisk",
    "refined_reflective": "Refined & reflective",
    "warm_neighborly": "Warm & neighborly",
    "dry_understated": "Dry & understated",
    "calm_intimate": "Calm & intimate",
    "crisp_direct": "Crisp & direct",
    "custom": "Custom instructions",
}
PERSONALITY_INSTRUCTIONS: Final[dict[str, str]] = {
    "balanced": (
        "Use a balanced radio-host presentation: friendly, concise, conversational, "
        "and energetic only when the material calls for it."
    ),
    "bright_brisk": (
        "Use a bright, brisk presentation: short energetic sentences, quick transitions, "
        "and friendly momentum. Be lively without shouting, catchphrases, or exaggerated hype."
    ),
    "refined_reflective": (
        "Use a refined, reflective presentation: measured sentences, precise vocabulary, "
        "restrained warmth, and graceful transitions. Never sound academic or pretentious."
    ),
    "warm_neighborly": (
        "Use a warm, neighborly presentation: inclusive language, practical relevance, and "
        "a relaxed conversational rhythm. Avoid stereotypes and forced local color."
    ),
    "dry_understated": (
        "Use a dry, understated presentation: calm delivery, economical wording, and at most "
        "one subtle observational aside. Avoid broad jokes, sarcasm, or mockery."
    ),
    "calm_intimate": (
        "Use a calm, intimate presentation: unhurried flowing sentences, gentle transitions, "
        "low-key warmth, and occasional direct listener address. Avoid melodrama."
    ),
    "crisp_direct": (
        "Use a crisp, direct presentation: compact factual sentences, clean signposting, "
        "minimal adjectives, and a composed broadcast rhythm. Still sound human."
    ),
}
RECENT_STORY_LIMIT: Final[int] = 10


def _bounded_int(value: Any, default: int, minimum: int = 1, maximum: int = 20) -> int:
    """Normalize one bounded integer option."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default


def _string_list(value: Any) -> tuple[str, ...]:
    """Normalize a config-entry multi-entity value to non-empty strings."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _url_pool(value: Any) -> tuple[str, ...]:
    """Normalize a multiline URL pool to one non-empty URL per line."""
    if isinstance(value, str):
        return tuple(line.strip() for line in value.splitlines() if line.strip())
    return _string_list(value)


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
    personality: str = DEFAULT_PERSONALITY
    custom_personality: str = ""
    jingle_urls: tuple[str, ...] = ()
    stinger_urls: tuple[str, ...] = ()
    cadence_enabled: bool = False
    cadence_min_tracks: int = DEFAULT_CADENCE_MIN_TRACKS
    cadence_max_tracks: int = DEFAULT_CADENCE_MAX_TRACKS
    cadence_content: str = CADENCE_CONTENT_MUSIC

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
        personality = text(CONF_PERSONALITY)
        if personality not in PERSONALITY_INSTRUCTIONS and personality != CUSTOM_PERSONALITY:
            personality = DEFAULT_PERSONALITY
        custom_personality = text(CONF_CUSTOM_PERSONALITY)
        if personality == CUSTOM_PERSONALITY and not custom_personality:
            personality = DEFAULT_PERSONALITY
        cadence_min = _bounded_int(
            values.get(CONF_CADENCE_MIN_TRACKS), DEFAULT_CADENCE_MIN_TRACKS
        )
        cadence_max = _bounded_int(
            values.get(CONF_CADENCE_MAX_TRACKS), DEFAULT_CADENCE_MAX_TRACKS
        )
        if cadence_max < cadence_min:
            cadence_max = cadence_min
        cadence_content = text(CONF_CADENCE_CONTENT)
        if cadence_content not in (CADENCE_CONTENT_MUSIC, CADENCE_CONTENT_FULL):
            cadence_content = CADENCE_CONTENT_MUSIC
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
            personality=personality,
            custom_personality=custom_personality,
            jingle_urls=_url_pool(values.get(CONF_JINGLE_URLS)),
            stinger_urls=_url_pool(values.get(CONF_STINGER_URLS)),
            cadence_enabled=values.get(CONF_CADENCE_ENABLED) is True,
            cadence_min_tracks=cadence_min,
            cadence_max_tracks=cadence_max,
            cadence_content=cadence_content,
        )

    @property
    def personality_instructions(self) -> str:
        """Return normalized presentation instructions for prompt construction."""
        if self.personality == CUSTOM_PERSONALITY:
            return self.custom_personality
        return PERSONALITY_INSTRUCTIONS[self.personality]

    @property
    def music_assistant_enabled(self) -> bool:
        """Return whether every native Music Assistant setting is present."""
        return bool(
            self.music_assistant_url
            and self.music_assistant_token
            and self.home_assistant_token
            and self.music_assistant_player_id
        )
