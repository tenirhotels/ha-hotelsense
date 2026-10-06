"""Select platform: manual room status (Свободен / Продан / Уборка)."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .room_entity import async_setup_room_platform, select_factory


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_setup_room_platform(hass, config_entry, async_add_entities, select_factory)
