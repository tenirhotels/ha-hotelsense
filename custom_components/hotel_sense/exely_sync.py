"""Room statuses from Exely, kept in step with the PMS (not only by webhooks).

Webhooks change a status when something happens; a missed webhook, a status
set by hand to work around something, or the time before Hotel Sense was set
up leave rooms wrong until the next event. The sync asks Exely which stays
touch the last day, looks them up and sets each room:

* a stay checked in and not out -> ``checked_in`` (as of the actual check-in)
* else the last stay checked out -> ``checked_out`` (as of the actual check-out)
* no stay in the window -> ``checked_out``, but only when every reservation of
  the window could be read (an incomplete picture never empties a room); a
  reservation looked up in the last 20 minutes is not asked again

The last change wins: a status set by hand after the Exely event is kept
(``override_manual`` replaces it - for the first sync). A stay still checked
in past its planned check-out is reported as an overdue check-out.

``plan`` is pure (unit-tested); ``ExelySync`` runs it every two hours.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import STATUS_SOURCE_EXELY, STATUS_SOURCE_MANUAL
from .exely_api import STAY_CANCELLED, ExelyApiError, RoomStay
from .presence import STATUS_CHECKED_IN, STATUS_CHECKED_OUT

LOGGER = logging.getLogger(__name__)

INTERVAL = timedelta(hours=2)
FIRST_RUN = 180  # seconds after start
LOOKBACK = timedelta(days=1)
LOOKAHEAD = timedelta(hours=2)
OVERDUE_AFTER = timedelta(hours=1)  # past the planned check-out, still checked in
FRESH = 20 * 60  # a reservation looked up this recently (seconds) is not asked again


def exely_time(value: str | None, tz) -> datetime | None:
    """Exely local time ("2026-10-08T15:40") -> aware UTC."""
    if not value:
        return None
    parsed = dt_util.parse_datetime(value) if "T" in value else None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc)


@dataclass
class RoomPlan:
    """What Exely says about one room."""

    room_id: str
    status: str
    booking: str | None = None
    since: datetime | None = None  # when Exely's event happened (UTC); None = no event
    overdue: bool = False  # a stay checked in past its planned check-out
    overdue_bookings: list[str] = field(default_factory=list)


def plan(stays: list[tuple[str, RoomStay]], room_ids, tz, now: datetime, *,
         complete: bool) -> dict[str, RoomPlan]:
    """Room id -> what Exely says, from (booking, stay) pairs.

    ``room_ids``: every room of the property (rooms without a stay are vacant
    when ``complete``). ``now`` is aware.
    """
    result: dict[str, RoomPlan] = {}
    by_room: dict[str, list[tuple[str, RoomStay]]] = {}
    for booking, stay in stays:
        if stay.room_id and stay.status != STAY_CANCELLED:
            by_room.setdefault(stay.room_id, []).append((booking, stay))
    for room_id in set(room_ids) | set(by_room):
        entries = by_room.get(room_id, [])
        inside = [(b, s) for b, s in entries if s.status == STATUS_CHECKED_IN
                  and not s.actual_check_out]
        if inside:
            booking, stay = max(inside, key=lambda e: exely_time(
                e[1].actual_check_in or e[1].check_in, tz) or now)
            # Any stay still in past its planned check-out - also an earlier guest
            # never checked out in the PMS while the next one already is in.
            overdue = [b for b, s in inside if (out := exely_time(s.check_out, tz)) is not None
                       and now - out > OVERDUE_AFTER]
            result[room_id] = RoomPlan(
                room_id, STATUS_CHECKED_IN, booking,
                exely_time(stay.actual_check_in or stay.check_in, tz),
                overdue=bool(overdue), overdue_bookings=overdue)
            continue
        out = [(b, s) for b, s in entries if s.status == STATUS_CHECKED_OUT]
        if out:
            booking, stay = max(out, key=lambda e: exely_time(
                e[1].actual_check_out or e[1].check_out, tz) or now)
            result[room_id] = RoomPlan(room_id, STATUS_CHECKED_OUT, booking,
                                       exely_time(stay.actual_check_out or stay.check_out, tz))
        elif complete:
            result[room_id] = RoomPlan(room_id, STATUS_CHECKED_OUT)
    return result


@dataclass
class SyncResult:
    time: str
    ok: bool = True
    error: str | None = None
    complete: bool = True
    bookings: int = 0
    looked_up: int = 0
    from_cache: int = 0  # looked up in the last 20 minutes, not asked again
    changes: list[dict] = field(default_factory=list)
    kept_manual: list[dict] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)  # several Exely rooms -> one room
    overdue_checkouts: list[dict] = field(default_factory=list)
    dry_run: bool = False


class ExelySync:
    def __init__(self, hass: HomeAssistant, receiver) -> None:
        self.hass = hass
        self.receiver = receiver
        self.last: SyncResult | None = None

    @property
    def api(self):
        return self.receiver.api

    @callback
    def async_start(self, entry) -> None:
        async def _tick(_now=None) -> None:
            if self.api.configured and self.receiver.property_id:
                await self.async_sync()

        entry.async_on_unload(async_track_time_interval(self.hass, _tick, INTERVAL))
        entry.async_on_unload(async_call_later(self.hass, FIRST_RUN, _tick))

    async def async_sync(self, *, override_manual: bool = False,
                         dry_run: bool = False) -> SyncResult:
        prop = self.receiver.property_id
        tz = dt_util.get_default_time_zone()
        now = dt_util.utcnow()
        local = now.astimezone(tz)
        fmt = "%Y-%m-%dT%H:%M"
        result = SyncResult(time=now.isoformat(timespec="seconds"), dry_run=dry_run)
        try:
            rooms = await self.api.async_rooms(prop)
            numbers = await self.api.async_search_reservations(
                prop, (local - LOOKBACK).strftime(fmt), (local + LOOKAHEAD).strftime(fmt))
        except ExelyApiError as err:
            result.ok, result.error = False, f"{type(err).__name__}: {err}"
            self.last = result
            LOGGER.warning("Exely sync failed: %s", result.error)
            return result
        result.bookings = len(numbers)
        stays: list[tuple[str, RoomStay]] = []
        for number in numbers:
            fetched = self.api.cached_stays(prop, number, max_age=FRESH)
            if fetched is not None:
                result.from_cache += 1
                stays += [(number, s) for s in fetched]
                continue
            if self.api.budget() > 0:
                try:
                    fetched = await self.api.async_reservation(prop, number)
                    result.looked_up += 1
                except ExelyApiError as err:
                    LOGGER.debug("Exely sync: booking %s: %s", number, err)
            if fetched is None:
                result.complete = False  # this booking's stays are unknown or old
                fetched = self.api.cached_stays(prop, number) or []
            stays += [(number, s) for s in fetched]
        plans = plan(stays, rooms, tz, now, complete=result.complete)
        by_area: dict[str, list[tuple[str, RoomPlan]]] = {}
        for room_id, room_plan in sorted(plans.items()):
            name = rooms.get(room_id)
            area_id = next((a for a in (self.receiver.resolve_room(lbl) for lbl in
                                        (name, room_id) if lbl) if a), None)
            if area_id is None:
                result.unresolved.append(name or room_id)
                continue
            by_area.setdefault(area_id, []).append((name or room_id, room_plan))
        for area_id, entries in by_area.items():
            room_name = self.receiver.manager.rooms[area_id].name
            if len(entries) > 1:
                # Several Exely rooms on one hotel room (a mapping to check):
                # occupied wins - a room is never emptied by another room's plan.
                result.conflicts.append({"room": room_name,
                                         "exely_rooms": [label for label, _ in entries]})
            room_plan = next((p for _, p in entries if p.status == STATUS_CHECKED_IN),
                             entries[0][1])
            for _, other in entries:
                for booking in other.overdue_bookings:
                    result.overdue_checkouts.append({"room": room_name, "booking": booking})
            self._apply(area_id, room_name, room_plan, result, override_manual)
        self.last = result
        return result

    def _apply(self, area_id: str, room_name: str, room_plan: RoomPlan, result: SyncResult,
               override_manual: bool) -> None:
        manager = self.receiver.manager
        current = manager.status_of(area_id)
        if current is not None and current.value == room_plan.status \
                and current.source == STATUS_SOURCE_EXELY:
            return
        if current is not None and current.source == STATUS_SOURCE_MANUAL and not override_manual:
            changed_at = dt_util.parse_datetime(current.changed_at or "")
            if room_plan.since is None or (changed_at and changed_at >= room_plan.since):
                if current.value != room_plan.status:
                    result.kept_manual.append({"room": room_name, "status": current.value,
                                               "exely": room_plan.status})
                return
        change = {"room": room_name, "from": current.value if current else None,
                  "to": room_plan.status, "booking": room_plan.booking}
        if current is None or current.value != room_plan.status:
            result.changes.append(change)
        if not result.dry_run:
            manager.async_set_status(area_id, room_plan.status, STATUS_SOURCE_EXELY,
                                     booking=room_plan.booking)

    def diagnostics(self) -> dict | None:
        return None if self.last is None else asdict(self.last)
