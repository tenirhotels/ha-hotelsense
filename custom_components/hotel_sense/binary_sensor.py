"""Binary sensors: rooms (guest / employee presence, violation) and access
point connectivity."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ap_entity import AccessPointStatusSensor, async_setup_ap_platform
from .room_entity import async_setup_room_platform, binary_sensor_factory


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_setup_ap_platform(hass, entry, async_add_entities,
                            lambda c, mac: [AccessPointStatusSensor(c, mac)])
    async_setup_room_platform(hass, entry, async_add_entities, binary_sensor_factory)
