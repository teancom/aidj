"""AI DJ station enable switch."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.switch import SwitchEntity

from .const import DOMAIN
from .runtime import AiDjRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the station enable switch."""
    runtime: AiDjRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AiDjEnabledSwitch(runtime, entry)])


class AiDjEnabledSwitch(SwitchEntity):
    """Expose whether scheduled AI DJ breaks are enabled."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:radio"
    _attr_has_entity_name = False

    def __init__(self, runtime: AiDjRuntime, entry: ConfigEntry) -> None:
        """Initialize the switch."""
        self._runtime = runtime
        self._attr_name = runtime.name
        self._attr_unique_id = f"{entry.entry_id}_enabled"

    @property
    def is_on(self) -> bool:
        """Return whether scheduled AI DJ breaks are enabled."""
        return self._runtime.enabled

    async def async_turn_on(self, **kwargs: object) -> None:
        """Enable scheduled AI DJ breaks."""
        await self._runtime.async_set_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        """Disable scheduled AI DJ breaks and remove owned queue items."""
        await self._runtime.async_set_enabled(False)
        self.async_write_ha_state()
