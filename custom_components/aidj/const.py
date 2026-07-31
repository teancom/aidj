"""Constants for the AI DJ integration."""

from typing import Final

DOMAIN: Final = "aidj"
SERVICE_ANNOUNCE: Final = "announce"
SERVICE_ANNOUNCE_NEXT: Final = "announce_next"
SERVICE_BRIEFING: Final = "briefing"
SERVICE_BRIEFING_NEXT: Final = "briefing_next"
SERVICE_START: Final = "start"
SERVICE_STOP: Final = "stop"
SERVICE_QUEUE_ADD: Final = "queue_add"

CONF_NAME: Final = "name"
CONF_PLAYER: Final = "player"
CONF_TTS: Final = "tts"
CONF_WEATHER: Final = "weather_entity_id"
CONF_AGENT: Final = "agent_id"
CONF_CONFIG_ENTRY_ID: Final = "config_entry_id"

ATTR_MESSAGE: Final = "message"
ATTR_MEDIA_ID: Final = "media_id"

DEFAULT_NAME: Final = "AI DJ"
