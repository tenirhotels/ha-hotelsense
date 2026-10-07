"""Hotel Sense room model: the business view of a room, HA Areas are its display.

A ``HotelRoom`` is keyed by ``room_id`` (= the HA area_id it was created for,
so entity unique IDs and the history keep working) and holds what Hotel Sense
knows about the room:

* ``name``: the HA Area's name (HA owns it: rename the Area to rename the room);
* ``kind``: ``room`` (status, violations) or ``common`` (presence only);
* ``number``: "01" (taken from the Area name, editable);
* ``exely_room_ids``: the room's Exely PMS roomIds. Labels entered before 0.9.1
  (roomIds or room names, mixed) stay here unchanged and keep matching;
* ``exely_room_name``: the room's name in Exely;
* ``status``: the current check-in status *with its origin*
  (value, source, changed_at, booking, user_id). The last change wins,
  whatever its source; ``changed_at`` is when the *value* last changed. The
  full history is in the history database.

``room_id`` is the domain identity, ``area_id`` the binding to the HA Area;
for now they are equal (entity unique IDs and the history use the area id).
The access points of a room come from its HA Area (where HA shows and edits
them), so there is one place for that mapping.

Stored per config entry in ``.storage/hotel_sense.rooms.<entry_id>``. Before
0.8 the room kinds were the ``common_areas`` option and the Exely labels the
``exely_room_map`` option text; both are imported once (see ``import_legacy``).
"""
from __future__ import annotations

import csv
import io
import re
import logging
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_MINOR_VERSION = 2  # 2: exely_room_name, normalised exely_room_ids
SAVE_DELAY = 5  # seconds: a burst of changes is one write

KIND_ROOM = "room"
KIND_COMMON = "common"
KINDS = (KIND_ROOM, KIND_COMMON)

# ``name`` is exported for reading only (HA's Area owns it); ``exely_room_name``
# was added in 0.9.1 at the end, so 5-column CSVs still import.
CSV_FIELDS = ("room_id", "name", "number", "kind", "exely_room_ids", "exely_room_name")

# Result of RoomRegistry.set_status()
STATUS_VALUE_CHANGED = "value"        # the status itself changed (a transition)
STATUS_METADATA_CHANGED = "metadata"  # same status, new source / booking / user
_ROOM_NAME = re.compile(r"^\s*(room|номер)\b", re.IGNORECASE)


def default_kind(name: str) -> str:
    """Kind of a newly seen Area. Existing rooms keep their stored, explicit kind.

    Next migration step: make the kind always explicit (no guess from the name).
    """
    return KIND_ROOM if _ROOM_NAME.match(name or "") else KIND_COMMON


def default_number(name: str) -> str | None:
    match = re.search(r"(\d+)\s*$", name or "")
    return match.group(1) if match else None


def _norm(label: str) -> str:
    return str(label).strip().lower()


def clean_labels(values) -> list[str]:
    """Strings, stripped, no empties, no duplicates, order kept (nothing else dropped)."""
    result: list[str] = []
    for value in values or []:
        if value is None or isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result


class _RoomStore(Store[dict]):
    async def _async_migrate_func(self, old_major_version: int, old_minor_version: int,
                                  old_data: dict) -> dict:
        if old_minor_version < 2:
            # 0.8 -> 0.9.1: new exely_room_name; existing labels are kept as they are
            # (they may be roomIds or names - not guessed), only normalised.
            for room in old_data.get("rooms", []):
                room["exely_room_ids"] = clean_labels(room.get("exely_room_ids"))
                room.setdefault("exely_room_name", None)
        return old_data


@dataclass(frozen=True)
class StatusValue:
    """A status with where it came from."""

    value: str
    source: str                    # manual / exely / restored / sync
    changed_at: str | None = None  # ISO 8601, UTC
    booking: str | None = None     # source exely
    user_id: str | None = None     # source manual: HA user who changed it

    @classmethod
    def from_dict(cls, data: dict | None) -> StatusValue | None:
        if not isinstance(data, dict) or not data.get("value"):
            return None
        return cls(value=data["value"], source=data.get("source") or "manual",
                   changed_at=data.get("changed_at"), booking=data.get("booking"),
                   user_id=data.get("user_id"))


@dataclass
class HotelRoom:
    room_id: str
    name: str
    kind: str = KIND_ROOM
    number: str | None = None
    # HA binding (= room_id for now)
    area_id: str | None = None
    # Exely
    exely_room_ids: list[str] = field(default_factory=list)
    exely_room_name: str | None = None
    # current operational status
    status: StatusValue | None = None

    @property
    def is_common(self) -> bool:
        return self.kind == KIND_COMMON

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = asdict(self.status) if self.status else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> HotelRoom:
        return cls(room_id=data["room_id"], name=data.get("name") or data["room_id"],
                   kind=data.get("kind") if data.get("kind") in KINDS else KIND_ROOM,
                   number=data.get("number"), area_id=data.get("area_id", data["room_id"]),
                   exely_room_ids=clean_labels(data.get("exely_room_ids")),
                   exely_room_name=(str(data.get("exely_room_name") or "").strip() or None),
                   status=StatusValue.from_dict(data.get("status")))


class RoomRegistry:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict] = _RoomStore(hass, STORAGE_VERSION, f"{DOMAIN}.rooms.{entry_id}",
                                              minor_version=STORAGE_MINOR_VERSION)
        self.rooms: dict[str, HotelRoom] = {}
        # Before a room exists: legacy common areas / Exely labels waiting for it.
        self._pending_common: set[str] | None = None
        self._pending_labels: dict[str, str] = {}  # label -> target (area id or name)
        self.loaded_from_storage = False
        self._listeners: list = []

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data:
            self.loaded_from_storage = True
            self.rooms = {r["room_id"]: HotelRoom.from_dict(r) for r in data.get("rooms", [])}
            common = data.get("pending_common")
            self._pending_common = set(common) if common is not None else None
            self._pending_labels = dict(data.get("pending_labels") or {})

    def _data(self) -> dict:
        return {"rooms": [r.to_dict() for r in self.rooms.values()],
                "pending_common": sorted(self._pending_common)
                if self._pending_common is not None else None,
                "pending_labels": self._pending_labels}

    def async_save(self) -> None:
        self._store.async_delay_save(self._data, SAVE_DELAY)
        for listener in list(self._listeners):
            listener()

    def async_add_listener(self, listener) -> None:
        """Called after every change (rooms, kinds, labels, statuses)."""
        self._listeners.append(listener)

    def table_rows(self) -> list[dict]:
        """The rooms as rows of the history ``rooms`` table."""
        return [{"room_id": r.room_id, "number": r.number, "name": r.name, "kind": r.kind}
                for r in sorted(self.rooms.values(), key=lambda r: r.room_id)]

    async def async_flush(self) -> None:
        """Write now (entry unload: the next setup reads the file)."""
        await self._store.async_save(self._data())

    # -- migration from the options of 0.7 and older ------------------------- #
    def import_legacy(self, common_areas: list[str] | None, room_map: dict[str, str]) -> None:
        """``common_areas`` / ``exely_room_map`` options -> room kinds / Exely labels."""
        if common_areas is not None:
            self._pending_common = set(common_areas)
            for room in self.rooms.values():
                room.kind = KIND_COMMON if room.room_id in self._pending_common else KIND_ROOM
        for label, target in room_map.items():
            if (room := self._find_target(target)) is not None:
                if _norm(label) not in map(_norm, room.exely_room_ids):
                    room.exely_room_ids.append(label)
            else:
                self._pending_labels[_norm(label)] = target
        self.async_save()

    def _find_target(self, target: str) -> HotelRoom | None:
        target = _norm(target)
        return next((r for r in self.rooms.values()
                     if _norm(r.room_id) == target or _norm(r.name) == target), None)

    # -- rooms ---------------------------------------------------------------- #
    def ensure(self, area_id: str, name: str) -> HotelRoom:
        """The room of an Area, created on first sight (kind and number guessed)."""
        room = self.rooms.get(area_id)
        if room is None:
            if self._pending_common is not None:
                kind = KIND_COMMON if area_id in self._pending_common else KIND_ROOM
            else:
                kind = default_kind(name)
            room = HotelRoom(room_id=area_id, name=name, kind=kind,
                             number=default_number(name), area_id=area_id)
            self.rooms[area_id] = room
            if self._pending_common is not None and self._pending_common <= set(self.rooms):
                self._pending_common = None  # every common area of the old option exists
            for label, target in list(self._pending_labels.items()):
                if _norm(target) in (_norm(area_id), _norm(name)):
                    room.exely_room_ids.append(label)
                    del self._pending_labels[label]
            self.async_save()
        elif room.name != name:
            room.name = name
            self.async_save()
        return room

    def get(self, room_id: str) -> HotelRoom | None:
        return self.rooms.get(room_id)

    def is_pending_label(self, label: str) -> bool:
        """An Exely label mapped to a room that does not exist (yet)."""
        return _norm(label) in self._pending_labels

    def find_by_exely_id(self, label: str) -> HotelRoom | None:
        """Exact (case-insensitive) match on exely_room_ids (incl. labels from before 0.9.1)."""
        label = _norm(label)
        return next((r for r in self.rooms.values()
                     if label in (_norm(x) for x in r.exely_room_ids)), None)

    def find_by_exely_name(self, label: str) -> HotelRoom | None:
        label = _norm(label)
        return next((r for r in self.rooms.values()
                     if r.exely_room_name and _norm(r.exely_room_name) == label), None)

    def find_by_exely_label(self, label: str) -> HotelRoom | None:
        return self.find_by_exely_id(label) or self.find_by_exely_name(label)

    # -- status ------------------------------------------------------------- #
    def set_status(self, room_id: str, value: str, source: str, *, changed_at: str | None = None,
                   booking: str | None = None, user_id: str | None = None,
                   stamp: bool = True) -> str | None:
        """Last change wins.

        Returns ``STATUS_VALUE_CHANGED`` (new value: ``changed_at`` = now or the
        given time), ``STATUS_METADATA_CHANGED`` (same value, other source /
        booking / user: ``changed_at`` is kept - it is when the value changed) or
        None (identical, or unknown room). With ``stamp=False`` (a status carried
        over from before 0.8) an unknown time stays unknown.
        """
        room = self.rooms.get(room_id)
        if room is None:
            return None
        old = room.status
        if old is not None and old.value == value:
            if (old.source, old.booking, old.user_id) == (source, booking, user_id):
                return None
            room.status = replace(old, source=source, booking=booking, user_id=user_id)
            self.async_save()
            return STATUS_METADATA_CHANGED
        if changed_at is None and stamp:
            changed_at = dt_util.utcnow().isoformat()
        room.status = StatusValue(value=value, source=source, changed_at=changed_at,
                                  booking=booking, user_id=user_id)
        self.async_save()
        return STATUS_VALUE_CHANGED

    def set_kinds(self, common: set[str]) -> bool:
        """Room kinds from a set of common room_ids. Returns True if any changed."""
        changed = False
        for room in self.rooms.values():
            kind = KIND_COMMON if room.room_id in common else KIND_ROOM
            if room.kind != kind:
                room.kind, changed = kind, True
        self._pending_common = None
        if changed:
            self.async_save()
        return changed

    # -- Exely room map text ("label = room" lines) ---------------------------- #
    def room_map_text(self) -> str:
        lines = [f"{label} = {room.name}" for room in sorted(self.rooms.values(),
                                                             key=lambda r: r.name)
                 for label in room.exely_room_ids]
        lines += [f"{label} = {target}" for label, target in self._pending_labels.items()]
        return "\n".join(lines)

    def set_room_map(self, room_map: dict[str, str]) -> None:
        """Replace all Exely labels by ``room_map`` (label -> room name / id)."""
        for room in self.rooms.values():
            room.exely_room_ids = []
        self._pending_labels = {}
        self.import_legacy(None, room_map)

    # -- CSV ------------------------------------------------------------------ #
    def export_csv(self) -> str:
        out = io.StringIO()
        writer = csv.writer(out, delimiter=";", lineterminator="\n")
        writer.writerow(CSV_FIELDS)
        for room in sorted(self.rooms.values(), key=lambda r: r.name):
            writer.writerow([room.room_id, room.name, room.number or "", room.kind,
                             ", ".join(room.exely_room_ids), room.exely_room_name or ""])
        return out.getvalue()

    def import_csv(self, text: str) -> tuple[list[str], bool]:
        """Update rooms from CSV. Returns (errors, kinds changed); nothing changes on errors."""
        errors: list[str] = []
        updates: dict[str, HotelRoom] = {}
        rows = list(csv.reader(io.StringIO((text or "").strip()), delimiter=";"))
        if rows and [c.strip().lower() for c in rows[0]][:1] == ["room_id"]:
            rows = rows[1:]
        for line_no, row in enumerate(rows, start=2):
            if not any(c.strip() for c in row):
                continue
            row = [c.strip() for c in row] + [""] * (len(CSV_FIELDS) - len(row))
            # name: read only (the HA Area's name) - ignored here.
            room_id, _name, number, kind, labels, exely_name = row[:6]
            room = self.rooms.get(room_id)
            if room is None:
                errors.append(f"line {line_no}: unknown room_id {room_id!r}")
                continue
            kind = kind.lower() or room.kind
            if kind not in KINDS:
                errors.append(f"line {line_no}: kind must be room or common, not {kind!r}")
                continue
            updates[room_id] = replace(
                room, number=number or None, kind=kind,
                exely_room_ids=clean_labels(labels.split(",")),
                exely_room_name=exely_name or None)
        seen: dict[str, str] = {}
        for room in {**self.rooms, **updates}.values():
            for label in room.exely_room_ids + ([room.exely_room_name]
                                                if room.exely_room_name else []):
                if (other := seen.setdefault(_norm(label), room.room_id)) != room.room_id:
                    errors.append(f"Exely label {label!r} is on {other} and {room.room_id}")
        if errors:
            return errors, False
        kinds_changed = any(self.rooms[r].kind != u.kind for r, u in updates.items())
        self.rooms.update(updates)
        self._pending_labels = {}
        self._pending_common = None
        self.async_save()
        return [], kinds_changed

    def diagnostics(self) -> dict:
        return self._data()
