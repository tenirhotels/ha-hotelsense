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

from .const import DOMAIN, STATUS_SOURCE_RESTORED
from .ids import make_room_unique_id
from .presence import (LEGACY_STATUSES, ROOM_STATES, STATE_VIOLATION, STATUS_CHECKED_OUT,
                       STATUSES)
from .presence_manager import PresenceManager, RoomSnapshot

MANUFACTURER = "Hotel Sense"

# Room status select attributes: who set the status and when.
ATTR_SOURCE = "source"          # manual / exely / restored
ATTR_CHANGED_AT = "changed_at"  # time of the last real change (UTC, ISO)
ATTR_SET_BY = "set_by"
ATTR_BOOKING = "booking"         # Exely booking that set the status
ATTR_USER_ID = "user_id"         # HA user who set it manually          # for "restored": who had set it (manual / exely)


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
    # Live view only: the device list (RSSI, last seen ...) changes on every poll
    # and would bloat the recorder database; the count itself is recorded.
    _unrecorded_attributes = frozenset({"devices", "types"})

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
    _unrecorded_attributes = frozenset({"devices"})

    def __init__(self, manager: PresenceManager, device_info: DeviceInfo) -> None:
        self.manager = manager
        self.entity_id = "sensor.hotel_sense_misplaced_devices"
        self._attr_unique_id = f"misplaced_devices-{manager.controller.controller_id}"
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
        self._attr_unique_id = f"exely_last_event-{receiver.manager.controller.controller_id}"
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
        self._restored_from: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.manager.status_of(self.area_id) is not None:
            return  # kept in the room model (0.8+)
        # First start after the upgrade: the status of this entity before it.
        last = await self.async_get_last_state()
        status = LEGACY_STATUSES.get(last.state, last.state) if last else None
        if status not in STATUSES:
            status = STATUS_CHECKED_OUT
        attrs = last.attributes if last else {}
        self.manager.async_set_status(self.area_id, status, STATUS_SOURCE_RESTORED,
                                      attrs.get(ATTR_CHANGED_AT))
        if prev := attrs.get(ATTR_SOURCE):
            self._restored_from = prev if prev != STATUS_SOURCE_RESTORED else attrs.get(ATTR_SET_BY)

    @property
    def current_option(self) -> str | None:
        status = self.manager.status_of(self.area_id)
        return status.value if status else None

    @property
    def extra_state_attributes(self):
        status = self.manager.status_of(self.area_id)
        if status is None:
            return {ATTR_SOURCE: None, ATTR_CHANGED_AT: None}
        attrs = {ATTR_SOURCE: status.source, ATTR_CHANGED_AT: status.changed_at,
                 ATTR_BOOKING: status.booking, ATTR_USER_ID: status.user_id}
        if status.source == STATUS_SOURCE_RESTORED:
            # Who set it originally (manual / exely), if known.
            attrs[ATTR_SET_BY] = self._restored_from
        return attrs

    async def async_select_option(self, option: str) -> None:
        user_id = self._context.user_id if self._context else None
        self.manager.async_set_status(self.area_id, option, user_id=user_id)


def select_factory(manager: PresenceManager, room: RoomSnapshot) -> list[RoomEntity]:
    return [] if room.is_common else [RoomStatusSelect(manager, room.area_id)]
