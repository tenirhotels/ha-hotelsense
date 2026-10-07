"""Device identity suggestions: "this new private MAC is probably device #7".

Phones change their private (random) MAC now and then. When an unknown random
MAC turns up, it is compared with the staff devices on the list and, if it
looks like one of them, suggested - never linked automatically: linking a
guest's phone to a staff device would hide that guest in an empty room. The
owner links it (or ignores the suggestion).

A pair is never suggested if the two were on the Wi-Fi at the same time (one
phone has one MAC at a time on one network). Otherwise each check that holds
adds its weight and each check that fails takes it away (0-100) - a balance of
evidence, not a probability. A check that cannot be judged yet (not enough
data) counts neither way and is reported as such:

* ``name``    the name the phone gives (Omada) is one of the device's - 35
* ``handoff`` the device went quiet before the new MAC appeared (≤ 7 days) - 25
* ``zones``   the new MAC is mostly in zones the device uses - 15
* ``hours``   ... at hours of the day the device is active - 15
* ``ssid``    on a Wi-Fi network the device uses - 10

No Home Assistant imports: pure logic, unit-tested in isolation.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

WEIGHTS = {"name": 35, "handoff": 25, "zones": 15, "hours": 15, "ssid": 10}
MIN_SCORE = 60
HANDOFF = timedelta(days=7)
MIN_HOURS_DATA = 1.0  # hours of presence before zones / hours can be judged


@dataclass
class Session:
    zone: str
    started: datetime
    ended: datetime  # "now" for a session still open

    @property
    def seconds(self) -> float:
        return max((self.ended - self.started).total_seconds(), 0.0)


@dataclass
class Profile:
    """What is known about one MAC, or all MACs of one device."""

    names: set[str] = field(default_factory=set)  # normalise_name() forms
    label: str | None = None  # the name as Omada shows it (for display)
    ssids: set[str] = field(default_factory=set)
    sessions: list[Session] = field(default_factory=list)

    @property
    def first_seen(self) -> datetime | None:
        return min((s.started for s in self.sessions), default=None)

    @property
    def last_seen(self) -> datetime | None:
        return max((s.ended for s in self.sessions), default=None)

    @property
    def hours(self) -> float:
        return sum(s.seconds for s in self.sessions) / 3600

    def zone_seconds(self) -> dict[str, float]:
        result: dict[str, float] = {}
        for s in self.sessions:
            result[s.zone] = result.get(s.zone, 0.0) + s.seconds
        return result

    def hour_seconds(self) -> dict[int, float]:
        """Seconds present per hour of the day (UTC)."""
        result: dict[int, float] = {}
        for s in self.sessions:
            t = s.started
            while t < s.ended:
                nxt = min(t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1),
                          s.ended)
                result[t.hour] = result.get(t.hour, 0.0) + (nxt - t).total_seconds()
                t = nxt
        return result


def normalise_name(name: str | None) -> str | None:
    """Comparable form of a device name; None for empty names and bare MACs."""
    if not name:
        return None
    key = name.strip().casefold()
    bare = key.replace(":", "").replace("-", "").replace(".", "")
    if not key or (len(bare) == 12 and all(c in "0123456789abcdef" for c in bare)):
        return None
    return key


def _overlap(a: Iterable[Session], b: list[Session]) -> bool:
    return any(x.started < y.ended and y.started < x.ended for x in a for y in b)


def compare(new: Profile, device: Profile) -> tuple[int, list[dict]] | None:
    """Score and checks for "``new`` is a MAC of ``device``"; None if impossible."""
    if _overlap(new.sessions, device.sessions):
        return None
    checks: list[dict] = []

    def check(name: str, ok: bool | None, detail=None) -> None:
        checks.append({"check": name, "ok": ok, "weight": WEIGHTS[name], "detail": detail})

    same = sorted(n for n in new.names & device.names)
    check("name", bool(same) if new.names else None, same[0] if same else None)

    first, last = new.first_seen, device.last_seen
    if first is None or last is None:
        check("handoff", None)
    else:
        gap = first - last
        check("handoff", timedelta(0) <= gap <= HANDOFF, round(gap.total_seconds() / 3600, 1))

    enough = new.hours >= MIN_HOURS_DATA and device.hours >= MIN_HOURS_DATA
    if enough:
        own = new.zone_seconds()
        known = device.zone_seconds()
        shared = sum(v for z, v in own.items() if z in known) / sum(own.values())
        check("zones", shared >= 0.6, sorted(z for z in own if z in known))
        hours = new.hour_seconds()
        active = {h for h, v in device.hour_seconds().items() if v >= 600}
        within = sum(v for h, v in hours.items() if h in active) / sum(hours.values())
        check("hours", within >= 0.6, round(within, 2))
    else:
        check("zones", None)
        check("hours", None)

    if new.ssids and device.ssids:
        common = sorted(new.ssids & device.ssids)
        check("ssid", bool(common), common[0] if common else None)
    else:
        check("ssid", None)
    score = sum(c["weight"] for c in checks if c["ok"]) - sum(
        c["weight"] for c in checks if c["ok"] is False)
    return max(score, 0), checks


def suggest(new_macs: dict[str, Profile], devices: dict[str, tuple[dict, Profile]],
            ignored: Collection[tuple[str, str]] = (), *,
            min_score: int = MIN_SCORE) -> list[dict]:
    """The best device for each new MAC that scores ``min_score`` or more.

    ``devices``: identity id -> (summary {name, category}, profile of all its MACs).
    ``ignored``: (mac, identity id) pairs the owner turned down.
    """
    result = []
    for mac, new in new_macs.items():
        best = None
        for ident, (summary, profile) in devices.items():
            if (mac, ident) in ignored:
                continue
            outcome = compare(new, profile)
            if outcome is None or outcome[0] < min_score:
                continue
            if best is None or outcome[0] > best["score"] or (
                    outcome[0] == best["score"] and int(ident) < int(best["identity"])):
                best = {"mac": mac, "name": new.label,
                        "identity": ident, "identity_name": summary.get("name"),
                        "category": summary.get("category"), "score": outcome[0],
                        "checks": outcome[1]}
        if best is not None:
            result.append(best)
    result.sort(key=lambda s: (-s["score"], s["mac"]))
    return result
