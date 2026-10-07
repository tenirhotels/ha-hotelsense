"""Presence engine: which devices are in which room, and what that means.

Input on every poll: the Wi-Fi clients Omada currently reports (MAC, access
point, RSSI), the AP -> Area map and the known-device list. Output: per-area
device sets split into guest / employee / fixed, plus a room state derived
from the manually set room status.

Rules (owner decisions, see README "Stage A"):

* Only Wi-Fi clients count. Location source is the AP the client is on.
* A client that disappears stays in its last room for ``timeout`` seconds
  (default 5 min) - phones sleep and drop off Wi-Fi briefly. A client whose
  last observation was in Wi-Fi power save (Omada ``powerSave``) can be given
  its own ``sleep_timeout`` (``None`` = same as ``timeout``).
* Roaming to an AP in another area is accepted only once the client has stayed
  there for ``debounce`` seconds (default 30 s), so flapping between
  neighbouring APs does not move it back and forth.
* Observations weaker than ``min_rssi`` (dBm, ``None`` = off) do not move a
  client: a phone in Room 02 heard faintly by WF01 is not "in Room 01".
* Random (locally administered) MACs are counted like any other device but
  reported separately; they are never merged automatically.

No Home Assistant imports: pure logic, unit-tested in isolation.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from .device_list import CATEGORY_EMPLOYEE, CATEGORY_FIXED
from .mac import is_random_mac

CATEGORY_GUEST = "guest"  # guest or unknown device

DEFAULT_TIMEOUT = 5 * 60
DEFAULT_DEBOUNCE = 30

# Room status: Exely PMS check-in / check-out (manual select until the webhook).
STATUS_CHECKED_IN = "checked_in"
STATUS_CHECKED_OUT = "checked_out"
STATUSES = (STATUS_CHECKED_IN, STATUS_CHECKED_OUT)
# Statuses saved by 0.2.0 (restored select state) -> current status.
LEGACY_STATUSES = {"vacant": STATUS_CHECKED_OUT, "sold": STATUS_CHECKED_IN,
                   "cleaning": STATUS_CHECKED_OUT}

# Room state (result of the rules).
STATE_EMPTY = "empty"              # checked out, nothing but fixed equipment
STATE_VIOLATION = "violation"      # checked out, guest/unknown device inside
STATE_STAFF_VISIT = "staff_visit"  # checked out, only employee devices (logged)
STATE_CHECKED_IN = "checked_in"    # guest checked in: presence is expected
ROOM_STATES = (STATE_EMPTY, STATE_VIOLATION, STATE_STAFF_VISIT, STATE_CHECKED_IN)


@dataclass(frozen=True)
class Observation:
    """One Wi-Fi client as reported by the controller in this poll."""

    mac: str
    ap_mac: str | None
    rssi: int | None = None
    ssid: str | None = None
    power_save: bool | None = None


@dataclass
class Track:
    mac: str
    area_id: str | None
    last_seen: float
    ap_mac: str | None = None
    rssi: int | None = None
    ssid: str | None = None
    power_save: bool | None = None
    pending_area_id: str | None = None
    pending_since: float | None = None


@dataclass
class AreaPresence:
    """Devices currently attributed to one area."""

    guest: set[str] = field(default_factory=set)
    employee: set[str] = field(default_factory=set)
    fixed: set[str] = field(default_factory=set)

    @property
    def guest_count(self) -> int:
        return len(self.guest)

    @property
    def employee_count(self) -> int:
        return len(self.employee)

    @property
    def fixed_count(self) -> int:
        return len(self.fixed)

    @property
    def random_mac_count(self) -> int:
        return sum(1 for mac in self.guest if is_random_mac(mac))

    def add(self, category: str, mac: str) -> None:
        {CATEGORY_FIXED: self.fixed, CATEGORY_EMPLOYEE: self.employee}.get(
            category, self.guest).add(mac)


def evaluate_room(status: str | None, presence: AreaPresence) -> str:
    """Apply the violation rules to one room."""
    if status == STATUS_CHECKED_IN:
        return STATE_CHECKED_IN
    # Checked out (also the fallback for an unknown status: err on the side of alerting).
    if presence.guest_count:
        return STATE_VIOLATION
    if presence.employee_count:
        return STATE_STAFF_VISIT
    return STATE_EMPTY


class PresenceEngine:
    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT,
                 debounce: float = DEFAULT_DEBOUNCE, min_rssi: int | None = None,
                 sleep_timeout: float | None = None) -> None:
        self.timeout = timeout
        self.debounce = debounce
        self.min_rssi = min_rssi
        self.sleep_timeout = sleep_timeout
        self.tracks: dict[str, Track] = {}

    def configure(self, *, timeout: float, debounce: float, min_rssi: int | None,
                  sleep_timeout: float | None = None) -> None:
        self.timeout = timeout
        self.debounce = debounce
        self.min_rssi = min_rssi
        self.sleep_timeout = sleep_timeout

    def timeout_for(self, track: Track) -> float:
        """How long a device that disappeared is kept in its room."""
        if track.power_save and self.sleep_timeout is not None:
            return self.sleep_timeout
        return self.timeout

    def _weak(self, rssi: int | None) -> bool:
        return self.min_rssi is not None and rssi is not None and rssi < self.min_rssi

    def update(self, now: float, observations: list[Observation],
               ap_areas: Mapping[str, str | None]) -> None:
        """Feed one poll. ``ap_areas`` maps AP MAC -> area_id (None = no area)."""
        seen: set[str] = set()
        for obs in observations:
            seen.add(obs.mac)
            weak = self._weak(obs.rssi)
            candidate = None if weak else ap_areas.get(obs.ap_mac) if obs.ap_mac else None
            track = self.tracks.get(obs.mac)

            if track is None:
                self.tracks[obs.mac] = Track(obs.mac, candidate, now, obs.ap_mac, obs.rssi,
                                             obs.ssid, obs.power_save)
                continue

            track.last_seen = now
            track.ap_mac = obs.ap_mac
            track.rssi = obs.rssi
            track.ssid = obs.ssid
            track.power_save = obs.power_save

            if weak or candidate == track.area_id:
                # A weak signal never moves a device; a matching one cancels roaming.
                if not weak:
                    track.pending_area_id = track.pending_since = None
                continue
            if track.area_id is None:
                track.area_id = candidate  # first real location: no debounce
                track.pending_area_id = track.pending_since = None
                continue
            if track.pending_area_id != candidate or track.pending_since is None:
                track.pending_area_id, track.pending_since = candidate, now
            if now - track.pending_since >= self.debounce:
                track.area_id = candidate
                track.pending_area_id = track.pending_since = None

        for mac in [m for m, t in self.tracks.items()
                    if m not in seen and now - t.last_seen >= self.timeout_for(t)]:
            del self.tracks[mac]

    def presence_by_area(self, category_of: Callable[[str], str | None]) -> dict[str, AreaPresence]:
        result: dict[str, AreaPresence] = {}
        for track in self.tracks.values():
            if track.area_id is None:
                continue
            result.setdefault(track.area_id, AreaPresence()).add(
                category_of(track.mac) or CATEGORY_GUEST, track.mac)
        return result
