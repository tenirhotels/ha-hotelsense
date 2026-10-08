"""What a Wi-Fi network (SSID) says about a device: guest room, staff, equipment.

Configured as lines ``pattern = role``; ``{n}`` in a pattern stands for a room
number, ``*`` for anything (case does not matter)::

    GUEST{n}       = guest_room
    hotel-service  = staff
    iot            = equipment

A role is a hint, never a rule: staff also use the guest networks, guests are
sometimes given the staff password. Used to weigh suggestions and candidates
only; presence still comes from the access point.

No Home Assistant imports: pure logic, unit-tested in isolation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ROLE_GUEST_ROOM = "guest_room"
ROLE_STAFF = "staff"
ROLE_EQUIPMENT = "equipment"
ROLES = (ROLE_GUEST_ROOM, ROLE_STAFF, ROLE_EQUIPMENT)
_ALIASES = {
    "guest_room": ROLE_GUEST_ROOM, "guest": ROLE_GUEST_ROOM, "room": ROLE_GUEST_ROOM,
    "гость": ROLE_GUEST_ROOM, "гостевая": ROLE_GUEST_ROOM, "номер": ROLE_GUEST_ROOM,
    "staff": ROLE_STAFF, "employee": ROLE_STAFF, "service": ROLE_STAFF,
    "персонал": ROLE_STAFF, "сотрудники": ROLE_STAFF, "служебная": ROLE_STAFF,
    "equipment": ROLE_EQUIPMENT, "fixed": ROLE_EQUIPMENT, "iot": ROLE_EQUIPMENT,
    "оборудование": ROLE_EQUIPMENT,
}


@dataclass(frozen=True)
class NetworkRole:
    role: str
    room: str | None = None  # the room number of a guest_room network ({n})


class SsidRoles:
    def __init__(self, rules: list[tuple[re.Pattern, str]] | None = None) -> None:
        self._rules = rules or []

    def __bool__(self) -> bool:
        return bool(self._rules)

    @classmethod
    def parse(cls, text: str | None) -> "SsidRoles":
        """Raises ValueError naming the line that is wrong."""
        rules = []
        for line_no, line in enumerate((text or "").splitlines(), start=1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            pattern, sep, role = line.rpartition("=")
            pattern, role = pattern.strip(), _ALIASES.get(role.strip().casefold())
            if not sep or not pattern or role is None:
                raise ValueError(f"line {line_no}: expected 'network = "
                                 f"{' / '.join(ROLES)}', got {line!r}")
            regex = re.escape(pattern).replace(r"\{n\}", r"(?P<n>\d+)").replace(r"\*", ".*")
            rules.append((re.compile(f"^{regex}$", re.IGNORECASE), role))
        return cls(rules)

    def role_of(self, ssid: str | None) -> NetworkRole | None:
        if not ssid:
            return None
        for regex, role in self._rules:
            if match := regex.match(ssid):
                room = match.groupdict().get("n")
                return NetworkRole(role, str(int(room)) if room else None)
        return None

    def roles_of(self, ssids) -> set[str]:
        return {r.role for s in ssids or () if (r := self.role_of(s))}
