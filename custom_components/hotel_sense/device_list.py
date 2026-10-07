"""Editable list of known devices: fixed equipment and employee devices.

* ``fixed``    - equipment that lives in a room (air conditioners, humidifiers);
                 never counts as presence.
* ``employee`` - staff phones/tablets; presence is reported separately from
                 guests. ``owner`` groups several devices of one person (Stage C).

Anything not on the list is a guest / unknown device.

A MAC is an observation, not a device: phones use a private (random) MAC per
network and get a new one when the network is forgotten or the phone reset.
So the list holds **device identities** (``EMP-0007``: name, category, owner
...) and each identity has one or more MACs - MAC -> identity, never the other
way round. A MAC belongs to at most one identity; MACs are linked to an
identity only by the owner (``link_mac`` / the CSV ``identity`` column), never
merged automatically. Callers that think in MACs still see one row
(``KnownDevice``) per MAC, carrying the identity's attributes and its ID.

The list is persisted in Home Assistant's ``.storage`` (see ``storage.py``);
this module holds the data model and the CSV import/export and has no Home
Assistant imports.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass, field

from .device_kind import parse_kind
from .mac import parse_mac

CATEGORY_FIXED = "fixed"
CATEGORY_EMPLOYEE = "employee"
CATEGORIES = (CATEGORY_FIXED, CATEGORY_EMPLOYEE)

# Values accepted in the CSV "category" column (lower-cased).
_CATEGORY_ALIASES = {
    "fixed": CATEGORY_FIXED,
    "stationary": CATEGORY_FIXED,
    "equipment": CATEGORY_FIXED,
    "фикс": CATEGORY_FIXED,
    "фиксированное": CATEGORY_FIXED,
    "фиксированный": CATEGORY_FIXED,
    "оборудование": CATEGORY_FIXED,
    "employee": CATEGORY_EMPLOYEE,
    "staff": CATEGORY_EMPLOYEE,
    "сотрудник": CATEGORY_EMPLOYEE,
    "персонал": CATEGORY_EMPLOYEE,
}

# Accepted header names per field (lower-cased, stripped).
_HEADER_ALIASES = {
    "mac": ("mac", "mac address", "mac-address", "mac_address", "macaddress", "мак", "mac адрес"),
    "name": ("name", "device", "device name", "имя", "название", "устройство"),
    "category": ("category", "type", "kind", "категория", "тип"),
    "owner": ("owner", "person", "employee", "владелец", "сотрудник"),
    "note": ("note", "notes", "comment", "department", "примечание", "комментарий", "отдел"),
    "room": ("room", "номер", "комната"),
    "device_type": ("device_type", "device type", "devicetype", "тип устройства", "вид"),
    "area": ("area", "ha area", "помещение", "зона"),
    "identity": ("identity", "identity_id", "device_id", "device id", "id", "идентификатор"),
}

# ``identity`` last: CSVs without it (positional or with a header) still import.
CSV_FIELDS = ("mac", "name", "category", "owner", "note", "room", "device_type", "identity")

# Generated identity IDs: EMP-0001 (employee), FIX-0001 (fixed equipment).
ID_PREFIX = {CATEGORY_EMPLOYEE: "EMP", CATEGORY_FIXED: "FIX"}
_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.-]{0,31}$")
_GENERATED_ID_RE = re.compile(r"^(?:EMP|FIX)-(\d+)$")
# Identity attributes (everything of a row but the MAC and the identity ID).
ATTRIBUTES = ("category", "name", "owner", "note", "room", "device_type")


def parse_identity_id(value: str | None) -> str:
    """Normalise an identity ID (upper case); "" stays "" (= assign one)."""
    ident = (value or "").strip().upper()
    if ident and not _ID_RE.match(ident):
        raise ValueError(f"Invalid identity ID: {value!r} (letters, digits, - _ ., max 32)")
    return ident


def parse_category(value: str | None, default: str | None = None) -> str:
    key = (value or "").strip().lower()
    if not key:
        if default in CATEGORIES:
            return default
        raise ValueError("Category is empty")
    if key not in _CATEGORY_ALIASES:
        raise ValueError(f"Unknown category: {value!r}")
    return _CATEGORY_ALIASES[key]


@dataclass
class KnownDevice:
    mac: str
    category: str
    name: str = ""
    owner: str = ""
    note: str = ""
    room: str = ""  # where the device is installed (reference only; location comes from the AP)
    device_type: str = ""  # device_kind.KINDS value set by the owner; "" = detect
    identity: str = ""  # the device identity this MAC belongs to ("" = keep / assign one)

    def __post_init__(self) -> None:
        self.mac = parse_mac(self.mac)
        self.category = parse_category(self.category)
        self.name = (self.name or "").strip()
        self.owner = (self.owner or "").strip()
        self.note = (self.note or "").strip()
        self.room = (self.room or "").strip()
        self.device_type = parse_kind(self.device_type)
        self.identity = parse_identity_id(self.identity)

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class DeviceIdentity:
    """One physical device of the hotel and the MACs it has been seen with."""

    id: str
    category: str
    name: str = ""
    owner: str = ""
    note: str = ""
    room: str = ""
    device_type: str = ""
    macs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.id = parse_identity_id(self.id)
        if not self.id:
            raise ValueError("Identity ID is empty")
        self.category = parse_category(self.category)
        self.device_type = parse_kind(self.device_type)
        macs: list[str] = []
        for mac in self.macs:
            if (mac := parse_mac(mac)) not in macs:
                macs.append(mac)
        self.macs = macs

    def row(self, mac: str) -> KnownDevice:
        return KnownDevice(mac=mac, identity=self.id,
                           **{a: getattr(self, a) for a in ATTRIBUTES})

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ImportResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class DeviceList:
    """Device identities and the index MAC -> identity."""

    def __init__(self, devices: list[KnownDevice] | None = None) -> None:
        self._identities: dict[str, DeviceIdentity] = {}
        self._by_mac: dict[str, str] = {}
        self._last_number = 0  # highest generated ID number ever: IDs are never reused
        for device in devices or []:
            self.upsert(device)

    # -- queries ---------------------------------------------------------- #
    def __contains__(self, mac: str) -> bool:
        return self.get(mac) is not None

    def __len__(self) -> int:
        """Number of MACs on the list."""
        return len(self._by_mac)

    def __iter__(self):
        """One row per MAC."""
        rows = [self._identities[ident].row(mac) for mac, ident in self._by_mac.items()]
        return iter(sorted(rows, key=lambda d: (d.category, d.name, d.mac)))

    def identities(self) -> list[DeviceIdentity]:
        return sorted(self._identities.values(), key=lambda i: (i.category, i.name, i.id))

    def identity(self, identity_id: str) -> DeviceIdentity | None:
        try:
            return self._identities.get(parse_identity_id(identity_id))
        except ValueError:
            return None

    def identity_of(self, mac: str) -> DeviceIdentity | None:
        try:
            ident = self._by_mac.get(parse_mac(mac))
        except ValueError:
            return None
        return self._identities[ident] if ident else None

    def get(self, mac: str) -> KnownDevice | None:
        identity = self.identity_of(mac)
        return identity.row(parse_mac(mac)) if identity else None

    def category_of(self, mac: str) -> str | None:
        identity = self.identity_of(mac)
        return identity.category if identity else None

    # -- mutations -------------------------------------------------------- #
    def _highest_number(self) -> int:
        """Highest number of an EMP-/FIX- ID given out or in use."""
        numbers = [int(m.group(1)) for ident in self._identities
                   if (m := _GENERATED_ID_RE.match(ident))]
        return max(self._last_number, *numbers, 0)

    def _new_id(self, category: str) -> str:
        self._last_number = self._highest_number() + 1
        return f"{ID_PREFIX[category]}-{self._last_number:04d}"

    def _detach(self, mac: str) -> None:
        ident = self._by_mac.pop(mac, None)
        if ident is None:
            return
        identity = self._identities[ident]
        identity.macs.remove(mac)
        if not identity.macs:
            del self._identities[ident]

    def _attach(self, identity: DeviceIdentity, mac: str) -> None:
        if self._by_mac.get(mac) == identity.id:
            return
        self._detach(mac)
        self._identities[identity.id] = identity
        identity.macs.append(mac)
        self._by_mac[mac] = identity.id

    def upsert(self, device: KnownDevice) -> bool:
        """Add or update one MAC row. Returns True if the MAC was new.

        The row's attributes become those of its identity: ``device.identity``
        (created if new), else the MAC's current identity, else a new one.
        """
        is_new = device.mac not in self._by_mac
        ident = device.identity or self._by_mac.get(device.mac) or self._new_id(device.category)
        identity = self._identities.get(ident) or DeviceIdentity(id=ident,
                                                                 category=device.category)
        for attr in ATTRIBUTES:
            setattr(identity, attr, getattr(device, attr))
        self._attach(identity, device.mac)
        return is_new

    def link_mac(self, identity_id: str, mac: str) -> str | None:
        """Give ``mac`` to an existing identity. Returns the identity it had before."""
        identity = self.identity(identity_id)
        if identity is None:
            raise KeyError(f"Unknown device identity: {identity_id}")
        mac = parse_mac(mac)
        before = self._by_mac.get(mac)
        self._attach(identity, mac)
        return before

    def remove(self, mac: str) -> bool:
        """Take ``mac`` off the list (an identity left without MACs goes too)."""
        mac = parse_mac(mac)
        if mac not in self._by_mac:
            return False
        self._detach(mac)
        return True

    def clear(self) -> None:
        self._identities.clear()
        self._by_mac.clear()

    # -- (de)serialisation ------------------------------------------------ #
    def to_storage(self) -> dict:
        """Identities, plus the flat rows of 0.11 and before (so a downgrade
        keeps every device, only without the grouping)."""
        legacy = []
        for row in self:
            data = row.as_dict()
            data.pop("identity")
            legacy.append(data)
        return {"identities": [i.as_dict() for i in self.identities()],
                "last_id_number": self._highest_number(), "devices": legacy}

    @classmethod
    def from_storage(cls, data: dict | None) -> "DeviceList":
        data = data or {}
        result = cls()
        result._last_number = int(data.get("last_id_number") or 0)
        if "identities" not in data:  # 0.11 and before: one row per MAC
            for raw in data.get("devices", []):
                try:
                    result.upsert(KnownDevice(**raw))
                except (TypeError, ValueError):
                    continue  # corrupt row: skip instead of failing setup
            return result
        for raw in data["identities"]:
            try:
                identity = DeviceIdentity(**raw)
            except (TypeError, ValueError):
                continue  # corrupt entry: skip instead of failing setup
            if identity.id in result._identities:
                continue
            # A MAC stays with the first identity that has it.
            identity.macs = [m for m in identity.macs if m not in result._by_mac]
            if identity.macs:
                result._identities[identity.id] = identity
                result._by_mac.update(dict.fromkeys(identity.macs, identity.id))
        return result

    def export_csv(self) -> str:
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for device in self:
            writer.writerow(device.as_dict())
        return out.getvalue()

    def import_csv(self, text: str, *, replace: bool = False,
                   default_category: str | None = None) -> ImportResult:
        """Import rows; invalid rows are reported, valid ones applied.

        Rows with the same ``identity`` are MACs of one device: their attributes
        are merged (the last non-empty value wins) and must agree on the
        category. ``replace`` drops every existing entry first (only if at least
        one row is valid, so a broken file never wipes the list).
        """
        result = ImportResult()
        parsed: list[KnownDevice] = []
        for line_no, row in parse_csv_rows(text):
            try:
                parsed.append(KnownDevice(
                    mac=row.get("mac", ""),
                    category=parse_category(row.get("category"), default_category),
                    name=row.get("name", ""),
                    owner=row.get("owner", ""),
                    note=row.get("note", ""),
                    room=row.get("room", ""),
                    device_type=row.get("device_type", ""),
                    identity=row.get("identity", ""),
                ))
            except ValueError as err:
                result.errors.append(f"line {line_no}: {err}")
                continue
            device = parsed[-1]
            first = next((d for d in parsed[:-1] if device.identity
                          and d.identity == device.identity), None)
            if first is not None and first.category != device.category:
                result.errors.append(
                    f"line {line_no}: {device.identity} is {first.category} on an earlier line")
                parsed.pop()

        merged: dict[str, dict[str, str]] = {}
        for device in parsed:
            if device.identity:
                attrs = merged.setdefault(device.identity, {})
                attrs.update({a: v for a in ATTRIBUTES if (v := getattr(device, a))})
        for device in parsed:
            for attr, value in merged.get(device.identity, {}).items():
                setattr(device, attr, value)

        if replace and parsed:
            keep = {d.mac for d in parsed}
            for mac in [m for m in self._by_mac if m not in keep]:
                self._detach(mac)
                result.removed += 1
        # Rows naming their identity first, so an ID generated for a row without
        # one can never be an ID a later row names.
        for device in sorted(parsed, key=lambda d: not d.identity):
            if self.upsert(device):
                result.added += 1
            else:
                result.updated += 1
        return result


def _sniff_dialect(text: str) -> type[csv.Dialect] | csv.Dialect:
    sample = text[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel


def _map_header(header: list[str]) -> dict[int, str] | None:
    mapping: dict[int, str] = {}
    for idx, col in enumerate(header):
        key = col.strip().strip("﻿").lower()
        for field_name, aliases in _HEADER_ALIASES.items():
            if key in aliases and field_name not in mapping.values():
                mapping[idx] = field_name
                break
    return mapping if "mac" in mapping.values() else None


def parse_csv_rows(text: str, fields: tuple[str, ...] = CSV_FIELDS):
    """Yield ``(line_no, {field: value})`` for every non-empty data row.

    A header row is recognised by column names (English or Russian aliases).
    Without a header, columns are taken positionally in ``fields`` order.
    """
    text = (text or "").lstrip("﻿")
    if not text.strip():
        return
    reader = csv.reader(io.StringIO(text), _sniff_dialect(text))
    mapping: dict[int, str] | None = None
    for line_no, row in enumerate(reader, start=1):
        cells = [c.strip() for c in row]
        if not any(cells) or cells[0].startswith("#"):
            continue
        if mapping is None:
            mapping = _map_header(cells)
            if mapping is not None:
                continue  # header row consumed
            mapping = dict(enumerate(fields))
        yield line_no, {name: cells[idx] for idx, name in mapping.items() if idx < len(cells)}
