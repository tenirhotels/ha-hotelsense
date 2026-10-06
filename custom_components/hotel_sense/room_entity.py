"""Per-room (HA Area) entities: presence, device counts, status, violation.

Each room gets a Hotel Sense device named after its Area. Entity IDs are set
explicitly from the (stable) ``area_id`` so the dashboard can rely on them
whatever the user's entity-naming settings: ``binary_sensor.room_01_violation``,
``sensor.room_01_state``, ``select.room_01_status`` ...
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .ids import make_room_unique_id
from .presence import (LEGACY_STATUSES, ROOM_STATES, STATE_VIOLATION, STATUS_CHECKED_OUT,
                       STATUSES)
from .presence_manager import PresenceManager, RoomSnapshot

MANUFACTURER = "Hotel Sense"


def room_device_info(manager: PresenceManager, room: RoomSnapshot) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, make_room_unique_id(manager.site_id, room.area_id))},
        name=room.name,
        manufacturer=MANUFACTURER,
        model="Common area" if room.is_common else "Hotel room",
        suggested_area=room.name,
    )


class RoomEntity(Entity):
    """Base class: state is pushed by the presence manager."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _platform_domain: str

    def __init__(self, manager: PresenceManager, area_id: str, key: str,
                 object_key: str | None = None) -> None:
        self.manager = manager
        self.area_id = area_id
        room = manager.rooms[area_id]
        self._attr_translation_key = key
        self.entity_id = f"{self._platform_domain}.{area_id}_{object_key or key}"
        self._attr_unique_id = make_room_unique_id(manager.site_id, area_id, key)
        self._attr_device_info = room_device_info(manager, room)

    @property
    def room(self) -> RoomSnapshot:
        return self.manager.rooms[self.area_id]

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, self.manager.signal_presence, self.async_write_ha_state))


def async_setup_room_platform(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[PresenceManager, RoomSnapshot], Iterable[RoomEntity]],
) -> None:
    """Create ``factory`` entities for every room now and whenever one appears."""
    manager: PresenceManager = hass.data[DOMAIN][entry.entry_id].presence
    added: set[str] = set()

    @callback
    def rooms_added(area_ids: Iterable[str]) -> None:
        entities = []
        for area_id in area_ids:
            if area_id in added or area_id not in manager.rooms:
                continue
            added.add(area_id)
            entities.extend(factory(manager, manager.rooms[area_id]))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(async_dispatcher_connect(
        hass, manager.signal_rooms_added, rooms_added))
    rooms_added(list(manager.rooms))


# --------------------------------------------------------------------------- #
# binary_sensor
# --------------------------------------------------------------------------- #
class RoomBinarySensor(RoomEntity, BinarySensorEntity):
    _platform_domain = "binary_sensor"
    def __init__(self, manager, area_id, key, device_class, is_on_fn) -> None:
        super().__init__(manager, area_id, key)
        self._attr_device_class = device_class
        self._is_on_fn = is_on_fn

    @property
    def is_on(self) -> bool:
        return self._is_on_fn(self.room)

    @property
    def extra_state_attributes(self):
        return {"data_stale": self.manager.data_stale}


def binary_sensor_factory(manager: PresenceManager, room: RoomSnapshot) -> list[RoomEntity]:
    occupancy = BinarySensorDeviceClass.OCCUPANCY
    entities = [
        RoomBinarySensor(manager, room.area_id, "guest_presence", occupancy,
                         lambda r: r.presence.guest_count > 0),
        RoomBinarySensor(manager, room.area_id, "employee_presence", occupancy,
                         lambda r: r.presence.employee_count > 0),
    ]
    if not room.is_common:
        entities.append(RoomBinarySensor(
            manager, room.area_id, "violation", BinarySensorDeviceClass.PROBLEM,
            lambda r: r.state == STATE_VIOLATION))
    return entities


# --------------------------------------------------------------------------- #
# sensor
# --------------------------------------------------------------------------- #
class RoomCountSensor(RoomEntity, SensorEntity):
    _platform_domain = "sensor"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "devices"

    def __init__(self, manager, area_id, category: str) -> None:
        super().__init__(manager, area_id, f"{category}_devices")
        self._category = category

    def _macs(self) -> set[str]:
        return getattr(self.room.presence, self._category)

    @property
    def native_value(self) -> int:
        return len(self._macs())

    @property
    def extra_state_attributes(self):
        devices, types = [], {}
        for mac in sorted(self._macs()):
            kind, source = self.manager.device_kind(mac)
            devices.append({"mac": mac, "name": self.manager.client_name(mac),
                            "type": kind, "type_source": source,
                            **self.manager.connection_info(mac)})
            types[kind] = types.get(kind, 0) + 1
        attrs = {
            "devices": devices,
            "types": dict(sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))),
            "data_stale": self.manager.data_stale,
        }
        if self._category == "guest":
            attrs["random_macs"] = self.room.presence.random_mac_count
        return attrs


class RoomStateSensor(RoomEntity, SensorEntity):
    _platform_domain = "sensor"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(ROOM_STATES)

    def __init__(self, manager, area_id) -> None:
        super().__init__(manager, area_id, "room_state", "state")

    @property
    def native_value(self) -> str | None:
        return self.room.state

    @property
    def extra_state_attributes(self):
        room = self.room
        return {
            "status": room.status,
            "guest_devices": room.presence.guest_count,
            "employee_devices": room.presence.employee_count,
            "fixed_devices": room.presence.fixed_count,
            "data_stale": self.manager.data_stale,
        }


def sensor_factory(manager: PresenceManager, room: RoomSnapshot) -> list[RoomEntity]:
    entities: list[RoomEntity] = [
        RoomCountSensor(manager, room.area_id, category)
        for category in ("guest", "employee", "fixed")
    ]
    if not room.is_common:
        entities.append(RoomStateSensor(manager, room.area_id))
    return entities


class MisplacedFixedDevicesSensor(SensorEntity):
    """Hotel-wide double check: fixed devices seen outside their ``room``."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "misplaced_devices"
    _attr_native_unit_of_measurement = "devices"
    _attr_icon = "mdi:map-marker-alert"

    def __init__(self, manager: PresenceManager, device_info: DeviceInfo) -> None:
        self.manager = manager
        self.entity_id = "sensor.hotel_sense_misplaced_devices"
        self._attr_unique_id = f"misplaced_devices-{manager.controller.api.controller_id}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, self.manager.signal_presence, self.async_write_ha_state))

    @property
    def native_value(self) -> int:
        return len(self.manager.misplaced_fixed_devices())

    @property
    def extra_state_attributes(self):
        return {"devices": self.manager.misplaced_fixed_devices(),
                "data_stale": self.manager.data_stale}


class ExelyLastEventSensor(SensorEntity):
    """Result of the last Exely webhook: applied / partial / unmatched / invalid."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "exely_last_event"
    _attr_icon = "mdi:webhook"

    def __init__(self, receiver, device_info: DeviceInfo) -> None:
        self.receiver = receiver
        self.entity_id = "sensor.hotel_sense_exely_last_event"
        self._attr_unique_id = f"exely_last_event-{receiver.manager.controller.api.controller_id}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, self.receiver.signal, self.async_write_ha_state))

    @property
    def native_value(self) -> str | None:
        return self.receiver.last["result"] if self.receiver.last else None

    @property
    def extra_state_attributes(self):
        # Extracted fields only; raw payloads (guest data) are not stored in states.
        return dict(self.receiver.last or {})


# --------------------------------------------------------------------------- #
# select: room status, Exely check-in / check-out (manual until the webhook)
# --------------------------------------------------------------------------- #
class RoomStatusSelect(RoomEntity, SelectEntity, RestoreEntity):
    _platform_domain = "select"
    _attr_options = list(STATUSES)

    def __init__(self, manager, area_id) -> None:
        super().__init__(manager, area_id, "room_status", "status")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        status = LEGACY_STATUSES.get(last.state, last.state) if last else None
        if status not in STATUSES:
            status = STATUS_CHECKED_OUT
        self.manager.async_set_status(self.area_id, status)

    @property
    def current_option(self) -> str | None:
        return self.manager.statuses.get(self.area_id)

    async def async_select_option(self, option: str) -> None:
        self.manager.async_set_status(self.area_id, option)


def select_factory(manager: PresenceManager, room: RoomSnapshot) -> list[RoomEntity]:
    return [] if room.is_common else [RoomStatusSelect(manager, room.area_id)]
