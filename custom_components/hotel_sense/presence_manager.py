"""Glue between the Omada controller, HA Areas and the presence engine."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.util import dt as dt_util

from .areas import resolve_ap_areas
from .const import (
    CONF_COMMON_AREAS, CONF_MIN_RSSI, CONF_PRESENCE_TIMEOUT, CONF_ROAMING_DEBOUNCE,
    DEFAULT_MIN_RSSI, DEFAULT_PRESENCE_TIMEOUT, DEFAULT_ROAMING_DEBOUNCE, DOMAIN,
    EVENT_ROOM_STATE_CHANGED,
)
from .mac import parse_mac
from .presence import AreaPresence, Observation, PresenceEngine, evaluate_room
from .storage import DeviceListStore

LOGGER = logging.getLogger(__name__)

# Default for "which areas are not hotel rooms" until the owner sets it.
_ROOM_NAME = re.compile(r"^\s*(room|номер)\b", re.IGNORECASE)


@dataclass
class RoomSnapshot:
    area_id: str
    name: str
    is_common: bool
    presence: AreaPresence = field(default_factory=AreaPresence)
    status: str | None = None
    state: str | None = None  # None for common areas


class PresenceManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller,
                 store: DeviceListStore) -> None:
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self.store = store
        self.engine = PresenceEngine()
        self.rooms: dict[str, RoomSnapshot] = {}
        self.statuses: dict[str, str] = {}
        self.data_stale = False
        self.common_areas: set[str] | None = None
        self.load_options()

    # -- signals ---------------------------------------------------------- #
    @property
    def signal_rooms_added(self) -> str:
        return f"{DOMAIN}-rooms-{self.entry.entry_id}"

    @property
    def signal_presence(self) -> str:
        return f"{DOMAIN}-presence-{self.entry.entry_id}"

    @property
    def site_id(self) -> str:
        return self.controller.site_id

    # -- lifecycle -------------------------------------------------------- #
    def load_options(self) -> None:
        options = self.entry.options
        min_rssi = int(options.get(CONF_MIN_RSSI, DEFAULT_MIN_RSSI) or 0)
        self.engine.configure(
            timeout=float(options.get(CONF_PRESENCE_TIMEOUT, DEFAULT_PRESENCE_TIMEOUT)) * 60,
            debounce=float(options.get(CONF_ROAMING_DEBOUNCE, DEFAULT_ROAMING_DEBOUNCE)),
            min_rssi=min_rssi if min_rssi < 0 else None,
        )
        common = options.get(CONF_COMMON_AREAS)
        self.common_areas = set(common) if common is not None else None

    @callback
    def async_start(self) -> None:
        unsubs = (
            async_dispatcher_connect(self.hass, self.controller.signal_update, self.async_process),
            async_dispatcher_connect(self.hass, self.controller.signal_options_update,
                                     self._async_options_updated),
            self.store.async_add_listener(self.async_refresh),
        )
        for unsub in unsubs:
            self.entry.async_on_unload(unsub)
        self.async_process()

    @callback
    def _async_options_updated(self) -> None:
        old_common = self.common_areas
        self.load_options()
        if self.common_areas != old_common and self.rooms:
            # Rooms and common areas expose different entities: rebuild them.
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
            return
        self.async_refresh()

    # -- processing ------------------------------------------------------- #
    def is_common_area(self, area_id: str, name: str) -> bool:
        if self.common_areas is not None:
            return area_id in self.common_areas
        return not _ROOM_NAME.match(name or "")

    def _observations(self) -> list[Observation]:
        result = []
        for mac, client in self.controller.api.clients.items.items():
            if not client.wireless:
                continue  # owner decision: Wi-Fi clients only
            try:
                ap_mac = parse_mac(client.ap_mac) if client.ap_mac else None
                result.append(Observation(parse_mac(mac), ap_mac, client.rssi))
            except ValueError:
                continue
        return result

    @callback
    def async_process(self) -> None:
        """Run after every controller poll."""
        if not self.controller.available:
            # ТЗ §22: keep the last known picture, flag it, no mass "empty".
            self.data_stale = True
            async_dispatcher_send(self.hass, self.signal_presence)
            return
        self.data_stale = False
        ap_areas = resolve_ap_areas(self.hass, self.controller)
        self.engine.update(dt_util.utcnow().timestamp(), self._observations(), ap_areas)

        areas = ar.async_get(self.hass)
        new = []
        for area_id in {a for a in ap_areas.values() if a}:
            if area_id in self.rooms:
                continue
            if (area := areas.async_get_area(area_id)) is None:
                continue
            self.rooms[area_id] = RoomSnapshot(area_id, area.name,
                                               self.is_common_area(area_id, area.name))
            new.append(area_id)
        if new:
            async_dispatcher_send(self.hass, self.signal_rooms_added, new)
        self.async_refresh()

    @callback
    def async_refresh(self) -> None:
        """Recompute room snapshots from the engine (no new poll)."""
        devices = self.store.devices
        by_area = self.engine.presence_by_area(devices.category_of)
        areas = ar.async_get(self.hass)
        for room in self.rooms.values():
            if area := areas.async_get_area(room.area_id):
                room.name = area.name
            room.is_common = self.is_common_area(room.area_id, room.name)
            room.presence = by_area.get(room.area_id, AreaPresence())
            room.status = self.statuses.get(room.area_id)
            old = room.state
            room.state = None if room.is_common else evaluate_room(room.status, room.presence)
            if old is not None and room.state is not None and old != room.state:
                self.hass.bus.async_fire(EVENT_ROOM_STATE_CHANGED, {
                    "area_id": room.area_id,
                    "room": room.name,
                    "status": room.status,
                    "old_state": old,
                    "new_state": room.state,
                    "guest_devices": room.presence.guest_count,
                    "employee_devices": room.presence.employee_count,
                })
        async_dispatcher_send(self.hass, self.signal_presence)

    @callback
    def async_set_status(self, area_id: str, status: str) -> None:
        if self.statuses.get(area_id) == status:
            return
        self.statuses[area_id] = status
        self.async_refresh()

    # -- helpers for entities -------------------------------------------- #
    def client_name(self, mac: str) -> str:
        if (known := self.store.devices.get(mac)) and known.name:
            return known.name
        api = self.controller.api
        for collection in (api.known_clients, api.clients):
            try:
                if mac in collection and collection[mac].name:
                    return collection[mac].name
            except (KeyError, TypeError):
                continue
        return mac
