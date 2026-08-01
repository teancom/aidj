"""Track media-player boundaries for deferred AI DJ announcements."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.const import STATE_PLAYING
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import event as event_helper


def track_identity(state: Any) -> str | None:
    """Return a stable identity for the currently playing media item."""
    attributes = getattr(state, "attributes", {})
    parts = tuple(
        str(attributes.get(key, ""))
        for key in ("media_content_id", "media_artist", "media_title")
    )
    return ":".join(parts) if any(parts) else None


class AnnouncementController:
    """Arm one message and deliver it after the configured player changes track."""

    def __init__(
        self,
        hass: HomeAssistant,
        player_entity_id: str,
        deliver: Callable[[str], Awaitable[None]],
    ) -> None:
        self.hass = hass
        self.player_entity_id = player_entity_id
        self._deliver = deliver
        self._pending: tuple[str, str] | None = None
        self._unsubscribe: Callable[[], None] | None = None

    @property
    def pending(self) -> bool:
        """Return whether a message is waiting for a track boundary."""
        return self._pending is not None

    async def async_arm(self, message: str) -> None:
        """Validate the current player and arm a message for the next track."""
        message = message.strip()
        if not message:
            raise ServiceValidationError("The announcement message must not be empty")

        state = self.hass.states.get(self.player_entity_id)
        if state is None:
            raise ServiceValidationError(
                f"Configured media player does not exist: {self.player_entity_id}"
            )
        if state.state != STATE_PLAYING:
            raise ServiceValidationError(
                "announce_next requires the configured media player to be playing"
            )

        baseline = track_identity(state)
        if baseline is None:
            raise ServiceValidationError(
                "The configured media player has no identifiable current track"
            )

        self._cancel_listener()
        self._pending = (baseline, message)
        self._unsubscribe = event_helper.async_track_state_change_event(
            self.hass,
            self.player_entity_id,
            self._async_handle_state_changed,
        )

    @callback
    def _async_handle_state_changed(self, event: Event) -> None:
        """Deliver the pending message when a different track starts playing."""
        if self._pending is None:
            return
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state != STATE_PLAYING:
            return

        baseline, message = self._pending
        current = track_identity(new_state)
        if current is None or current == baseline:
            return

        self._pending = None
        self._cancel_listener()
        self.hass.async_create_task(self._deliver(message))

    @callback
    def async_cancel(self) -> None:
        """Cancel any pending announcement and its state listener."""
        self._pending = None
        self._cancel_listener()

    @callback
    def _cancel_listener(self) -> None:
        """Remove the state listener if one is registered."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
