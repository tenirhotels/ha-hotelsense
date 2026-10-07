"""Glue between the Omada controller, HA Areas and the presence engine."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.util import dt as dt_util

from .areas import resolve_ap_areas, resolve_area
from .const import (
    CONF_MIN_RSSI, CONF_PRESENCE_TIMEOUT, CONF_ROAMING_DEBOUNCE,
    CONF_SLEEP_TIMEOUT, DEFAULT_MIN_RSSI, DEFAULT_PRESENCE_TIMEOUT, DEFAULT_ROAMING_DEBOUNCE,
    DEFAULT_SLEEP_TIMEOUT, DOMAIN,
    EVENT_ROOM_STATE_CHANGED, STATUS_SOURCE_MANUAL, STATUS_SOURCE_RESTORED,
)
from .device_kind import resolve_kind
from .device_list import CATEGORY_FIXED
from .history import now as history_now
from .rooms import RoomRegistry, StatusValue
from .presence import CATEGORY_GUEST, AreaPresence, Observation, PresenceEngine, evaluate_room
from .storage import DeviceListStore
from .traffic import TrafficMeter

LOGGER = logging.getLogger(__name__)



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
                 store: DeviceListStore, registry: RoomRegistry) -> None:
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self.store = store
        # The room model (kind, number, Exely labels, status with its origin);
        # RoomSnapshot below is the live picture of a room.
        self.registry = registry
        self._room_rows: list[dict] | None = None
        registry.async_add_listener(self._sync_rooms)
        self.engine = PresenceEngine()
        self.rooms: dict[str, RoomSnapshot] = {}
        self.data_stale = False
        # mac -> (area_id, since): open presence sessions (history database only).
        self.sessions: dict[str, tuple[str, datetime]] = {}
        self.traffic = TrafficMeter()
        self.load_options()

    @property
    def history(self):
        return self.controller.history

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
        sleep = float(options.get(CONF_SLEEP_TIMEOUT, DEFAULT_SLEEP_TIMEOUT) or 0)
        self.engine.configure(
            timeout=float(options.get(CONF_PRESENCE_TIMEOUT, DEFAULT_PRESENCE_TIMEOUT)) * 60,
            debounce=float(options.get(CONF_ROAMING_DEBOUNCE, DEFAULT_ROAMING_DEBOUNCE)),
            min_rssi=min_rssi if min_rssi < 0 else None,
            sleep_timeout=sleep * 60 if sleep > 0 else None,
        )

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
        self._sync_rooms()

    @callback
    def _async_options_updated(self) -> None:
        self.load_options()
        self.async_refresh()

    # -- processing ------------------------------------------------------- #
    def is_common_area(self, area_id: str, name: str) -> bool:
        return self.registry.ensure(area_id, name).is_common

    def status_of(self, area_id: str) -> StatusValue | None:
        room = self.registry.get(area_id)
        return room.status if room else None

    def _observations(self) -> list[Observation]:
        return [Observation(mac, client.ap_mac, client.rssi, client.ssid, client.power_save)
                for mac, client in self.controller.clients.items()
                if client.wireless]  # owner decision: Wi-Fi clients only

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
        self._update_sessions()
        self._update_traffic()

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

    # -- history: the room model as the ``rooms`` table ---------------------- #
    @callback
    def _sync_rooms(self) -> None:
        """Write the room model to the database when number / name / kind changed."""
        if self.history is None:
            return
        rows = self.registry.table_rows()
        if rows != self._room_rows:
            self._room_rows = rows
            self.history.set_rooms(rows)

    # -- history: device in room from ... to ... ---------------------------- #
    def _update_sessions(self) -> None:
        if self.history is None:
            return
        now = history_now()
        current = {mac: t.area_id for mac, t in self.engine.tracks.items() if t.area_id}
        for mac, (area_id, since) in list(self.sessions.items()):
            if current.get(mac) != area_id:
                self._end_session(mac, area_id, since, now)
        for mac, area_id in current.items():
            if mac not in self.sessions:
                self.sessions[mac] = (area_id, now)

    def _end_session(self, mac: str, area_id: str, since: datetime, now: datetime) -> None:
        del self.sessions[mac]
        self.history.add("presence_sessions", client_mac=mac, area_id=area_id,
                         category=self.store.devices.category_of(mac) or CATEGORY_GUEST,
                         started=since, ended=now, seconds=int((now - since).total_seconds()))

    # -- history: traffic per room and hour -------------------------------- #
    def _update_traffic(self) -> None:
        if self.history is None:
            return
        counters = {}
        for mac, client in self.controller.clients.items():
            if client.wireless:
                raw = client.raw
                counters[mac] = (_int(raw.get("trafficDown")), _int(raw.get("trafficUp")))
        hour = history_now().replace(minute=0, second=0, microsecond=0)
        tracks = self.engine.tracks
        for row in self.traffic.update(
                hour, counters,
                lambda mac: tracks[mac].area_id if mac in tracks else None,
                lambda mac: self.store.devices.category_of(mac) or CATEGORY_GUEST):
            self.history.add("room_traffic", **row)

    @callback
    def async_close_sessions(self) -> None:
        """End the open sessions now (unload / stop): nothing is left unwritten."""
        if self.history is None:
            return
        now = history_now()
        for mac, (area_id, since) in list(self.sessions.items()):
            self._end_session(mac, area_id, since, now)
        for row in self.traffic.flush():  # this hour so far
            self.history.add("room_traffic", **row)

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
            status = self.status_of(room.area_id)
            room.status = status.value if status else None
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
                if self.history is not None:
                    self.history.add("room_states", area_id=room.area_id, status=room.status,
                                     old_state=old, state=room.state,
                                     guest_devices=room.presence.guest_count,
                                     employee_devices=room.presence.employee_count)
        async_dispatcher_send(self.hass, self.signal_presence)

    @callback
    def async_set_status(self, area_id: str, status: str, source: str = STATUS_SOURCE_MANUAL,
                         changed_at: str | None = None, booking: str | None = None,
                         user_id: str | None = None) -> None:
        """Set a room status; the last change wins, whatever its source.

        ``source``: manual / exely / restored (status from before 0.8, kept by
        the select entity). ``changed_at`` is kept only for restored statuses.
        """
        if source != STATUS_SOURCE_RESTORED:
            changed_at = None
        if not self.registry.set_status(area_id, status, source, changed_at=changed_at,
                                        booking=booking, user_id=user_id,
                                        stamp=source != STATUS_SOURCE_RESTORED):
            return
        if source != STATUS_SOURCE_RESTORED and self.history is not None:
            self.history.add("room_status", area_id=area_id, status=status, source=source,
                             booking=booking)
        self.async_refresh()

    # -- helpers for entities -------------------------------------------- #
    def misplaced_fixed_devices(self) -> list[dict]:
        """Fixed devices with a ``room`` that are currently located elsewhere.

        A double check of the AP -> Area mapping: equipment does not move, so a
        mismatch means swapped AP Areas, a device on a neighbouring AP, or a
        device that was physically moved. Devices not seen right now are skipped.
        """
        areas = ar.async_get(self.hass)
        result = []
        for device in self.store.devices:
            if device.category != CATEGORY_FIXED or not device.room:
                continue
            track = self.engine.tracks.get(device.mac)
            if track is None or track.area_id is None:
                continue
            expected = resolve_area(self.hass, device.room)
            if expected is not None and expected.id == track.area_id:
                continue
            seen = areas.async_get_area(track.area_id)
            result.append({
                "name": device.name or device.mac,
                "mac": device.mac,
                "expected": device.room,
                "seen": seen.name if seen else track.area_id,
                "reason": "wrong_room" if expected is not None else "unknown_room",
            })
        return sorted(result, key=lambda d: d["name"])

    def _omada_raw(self, mac: str) -> dict | None:
        client = self.controller.client(mac)
        return dict(client.raw) if client is not None else None

    def connection_info(self, mac: str) -> dict:
        """Access point, SSID and signal of the last observation of ``mac``.

        ``connected`` is False while a device that left is kept in its room for
        the disconnect timeout; the values are then those last seen.
        """
        track = self.engine.tracks.get(mac)
        if track is None:
            return {}
        ap = self.controller.access_points.get(track.ap_mac) if track.ap_mac else None
        return {
            "connected": mac in self.controller.clients,
            "ap": ap.name if ap else track.ap_mac,
            "ssid": track.ssid,
            "rssi": track.rssi,
            "power_save": track.power_save,
            "last_seen": dt_util.utc_from_timestamp(track.last_seen).isoformat(),
        }

    def device_kind(self, mac: str) -> tuple[str, str]:
        """(kind, source): owner's list, then Omada, then hostname, then private MAC."""
        listed = self.store.devices.get(mac)
        return resolve_kind(mac, listed=listed.device_type if listed else None,
                            omada_raw=self._omada_raw(mac), name=self.client_name(mac))

    def client_name(self, mac: str) -> str:
        if (known := self.store.devices.get(mac)) and known.name:
            return known.name
        if (client := self.controller.client(mac)) is not None and client.name:
            return client.name
        return mac


def _int(value) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
