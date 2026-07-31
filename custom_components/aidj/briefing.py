"""Provider-neutral briefing data and Home Assistant-backed sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from homeassistant.core import HomeAssistant


@dataclass(frozen=True, slots=True)
class BriefingItem:
    """One normalized fact that may be used in a DJ briefing."""

    provider: str
    title: str
    summary: str
    occurred_at: datetime | None = None
    source: str | None = None


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
        """Return one compact weather fact when the entity is available."""
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
            errors[provider.name] = str(err)
    return items, errors
