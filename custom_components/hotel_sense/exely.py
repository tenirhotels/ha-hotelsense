"""Exely PMS webhook payloads -> room status (check-in / check-out).

Exely documents the delivery (HTTPS POST, JSON, ``API-KEY`` header, 200 OK
expected) but not the payload schema, so this parser is deliberately
conservative: it applies a status only when the event is unambiguously a
check-in or a check-out and the room can be found. Everything else is kept as
"unmatched" for inspection (see diagnostics) instead of being guessed.

No Home Assistant imports: pure logic, unit-tested in isolation.
"""
from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from .presence import STATUS_CHECKED_IN, STATUS_CHECKED_OUT

# Keys that may carry the event name (compared lower-case, without separators).
_EVENT_KEYS = {"event", "eventtype", "eventname", "type", "action", "name", "topic", "kind"}
_CHECK_IN = ("checkin", "checkedin", "arrival", "arrived", "заезд", "заселен", "заселён")
_CHECK_OUT = ("checkout", "checkedout", "departure", "departed", "выезд", "выселен")
_CANCEL = ("cancel", "undo", "revert", "rollback", "annul", "отмен", "аннулир")
# Keys that may carry the room number/name.
# (room type, room id, rooms count ... do not match).
_ROOM_KEY = re.compile(r"^(room|номер|комната)(number|num|no|name|code|title)?$")


def _norm_key(key: str) -> str:
    return re.sub(r"[\s_\-.]", "", str(key)).lower()


def _walk(node, depth: int = 0) -> Iterator[tuple[str, object, int]]:
    if depth > 8:
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield _norm_key(key), value, depth
            yield from _walk(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, depth + 1)


def status_from_event(name: str) -> str | None:
    """Check-in -> checked_in, check-out -> checked_out.

    A cancelled check-in puts the room back to checked_out and a cancelled
    check-out back to checked_in ("CheckInCancelled", "Отмена заезда" ...),
    so the word "checkin" in a cancellation must not be read as a check-in.
    """
    text = _norm_key(name)
    is_in = any(word in text for word in _CHECK_IN)
    is_out = any(word in text for word in _CHECK_OUT)
    if is_in == is_out:
        return None  # neither, or both (ambiguous)
    cancelled = any(word in text for word in _CANCEL)
    if is_in != cancelled:
        return STATUS_CHECKED_IN
    return STATUS_CHECKED_OUT


@dataclass
class ExelyEvent:
    event: str | None = None
    status: str | None = None
    rooms: list[str] = field(default_factory=list)


def parse_event(payload) -> ExelyEvent:
    result = ExelyEvent()
    statuses: set[str] = set()
    for key, value, depth in _walk(payload):
        if depth <= 2 and key in _EVENT_KEYS and isinstance(value, str) and value.strip():
            status = status_from_event(value)
            if status:
                statuses.add(status)
                result.event = result.event or value.strip()
            elif result.event is None and depth == 0:
                result.event = value.strip()
        if _ROOM_KEY.match(key):
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                text = str(value).strip()
                if text and text not in result.rooms:
                    result.rooms.append(text)
    if len(statuses) == 1:
        result.status = statuses.pop()
    return result


def parse_room_map(text: str | None) -> dict[str, str]:
    """``101 = Room 01`` lines (also ``;``, ``,`` or tab separated) -> mapping."""
    mapping: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s*(?:=|;|,|\t|->)\s*", line, maxsplit=1)
        if len(parts) == 2 and parts[0] and parts[1]:
            mapping[parts[0].strip().lower()] = parts[1].strip()
    return mapping


def room_number(value: str) -> int | None:
    """Trailing number of a room label: "Room 01" / "01" / "№1" -> 1."""
    match = re.search(r"(\d+)\s*$", value or "")
    return int(match.group(1)) if match else None
