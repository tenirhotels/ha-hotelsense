"""Editable list of known devices: fixed equipment and employee devices.

* ``fixed``    - equipment that lives in a room (air conditioners, humidifiers);
                 never counts as presence.
* ``employee`` - staff phones/tablets; presence is reported separately from
                 guests. ``owner`` groups several devices of one person (Stage C).

Anything not on the list is a guest / unknown device.

The list is persisted in Home Assistant's ``.storage`` (see ``storage.py``);
this module holds the data model and the CSV import/export and has no Home
Assistant imports.
"""
from __future__ import annotations

import csv
import io
from dataclasses import asdict, dataclass, field

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
    "area": ("area", "ha area", "помещение", "зона"),
}

CSV_FIELDS = ("mac", "name", "category", "owner", "note", "room")


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

    def __post_init__(self) -> None:
        self.mac = parse_mac(self.mac)
        self.category = parse_category(self.category)
        self.name = (self.name or "").strip()
        self.owner = (self.owner or "").strip()
        self.note = (self.note or "").strip()
        self.room = (self.room or "").strip()

    def as_dict(self) -> dict[str, str]:
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
    """In-memory list keyed by normalised MAC."""

    def __init__(self, devices: list[KnownDevice] | None = None) -> None:
        self._devices: dict[str, KnownDevice] = {}
        for device in devices or []:
            self._devices[device.mac] = device

    # -- queries ---------------------------------------------------------- #
    def __contains__(self, mac: str) -> bool:
        return self.get(mac) is not None

    def __len__(self) -> int:
        return len(self._devices)

    def __iter__(self):
        return iter(sorted(self._devices.values(), key=lambda d: (d.category, d.name, d.mac)))

    def get(self, mac: str) -> KnownDevice | None:
        try:
            return self._devices.get(parse_mac(mac))
        except ValueError:
            return None

    def category_of(self, mac: str) -> str | None:
        device = self.get(mac)
        return device.category if device else None

    # -- mutations -------------------------------------------------------- #
    def upsert(self, device: KnownDevice) -> bool:
        """Add or replace. Returns True if the MAC was new."""
        is_new = device.mac not in self._devices
        self._devices[device.mac] = device
        return is_new

    def remove(self, mac: str) -> bool:
        return self._devices.pop(parse_mac(mac), None) is not None

    def clear(self) -> None:
        self._devices.clear()

    # -- (de)serialisation ------------------------------------------------ #
    def to_storage(self) -> dict:
        return {"devices": [d.as_dict() for d in self]}

    @classmethod
    def from_storage(cls, data: dict | None) -> "DeviceList":
        devices = []
        for raw in (data or {}).get("devices", []):
            try:
                devices.append(KnownDevice(**raw))
            except (TypeError, ValueError):
                continue  # corrupt row: skip instead of failing setup
        return cls(devices)

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

        ``replace`` drops every existing entry first (only if at least one row
        is valid, so a broken file never wipes the list).
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
                ))
            except ValueError as err:
                result.errors.append(f"line {line_no}: {err}")

        if replace and parsed:
            keep = {d.mac for d in parsed}
            for mac in [m for m in self._devices if m not in keep]:
                del self._devices[mac]
                result.removed += 1
        for device in parsed:
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
