"""Business queries on the history database, through the ``v1_*`` views.

The functions behind ``hotel_sense.room_report`` / ``hotel_sense.hotel_report``
(and later notifications, a web admin ...): callers get plain dicts and never
see table or column names, so the schema can change underneath.

Periods are [start, end) in UTC; times in the results are ISO 8601 UTC.
Presence and staff time are clipped to the period. Device MACs are shown;
names only for the hotel's own devices (employee / fixed), never for guests.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Collection
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.engine import Connection

from .mac import is_random_mac

CATEGORIES = ("guest", "employee", "fixed")


def _naive(value: datetime) -> datetime:
    """Aware -> naive UTC, as stored."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc).isoformat()


def _dt(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _clip(started, ended, start: datetime, end: datetime) -> int:
    started, ended = max(_dt(started), start), min(_dt(ended), end)
    return max(int((ended - started).total_seconds()), 0)


def _rows(conn: Connection, sql: str, **params) -> list[dict]:
    stmt = text(sql)
    for key in ("start", "end"):
        if f":{key}" in sql:
            stmt = stmt.bindparams(bindparam(key, type_=DateTime))
    if ":macs" in sql:
        stmt = stmt.bindparams(bindparam("macs", expanding=True))
    return [dict(r._mapping) for r in conn.execute(stmt, params)]


def room_report(conn: Connection, room_id: str, start: datetime, end: datetime,
                device_name: Callable[[str], str | None] = lambda mac: None) -> dict:
    """Everything that happened in one room in the period."""
    start, end = _naive(start), _naive(end)
    p = {"room": room_id, "start": start, "end": end}
    status = _rows(conn, "SELECT ts, status, source, booking FROM v1_room_status "
                         "WHERE room_id = :room AND ts >= :start AND ts < :end ORDER BY ts", **p)
    states = _rows(conn, "SELECT ts, old_state, state, guest_devices, employee_devices "
                         "FROM v1_room_states WHERE room_id = :room AND ts >= :start AND ts < :end "
                         "ORDER BY ts", **p)
    violations = _rows(conn, "SELECT started, ended, guest_devices FROM v1_violations "
                             "WHERE room_id = :room AND started < :end "
                             "AND (ended IS NULL OR ended > :start) ORDER BY started", **p)
    sessions = _rows(conn, "SELECT client_mac, category, started, ended FROM v1_presence "
                           "WHERE room_id = :room AND started < :end AND ended > :start "
                           "ORDER BY started", **p)
    traffic = _rows(conn, "SELECT category, SUM(down_bytes) AS down_bytes, "
                          "SUM(up_bytes) AS up_bytes FROM v1_room_traffic_hourly "
                          "WHERE room_id = :room AND hour >= :start AND hour < :end "
                          "GROUP BY category", **p)

    presence: dict[str, dict] = {}
    for category in CATEGORIES:
        rows = [s for s in sessions if s["category"] == category]
        presence[category] = {
            "devices": len({s["client_mac"] for s in rows}),
            "sessions": len(rows),
            "seconds": sum(_clip(s["started"], s["ended"], start, end) for s in rows),
        }
    staff = [{"mac": s["client_mac"], "name": device_name(s["client_mac"]),
              "started": _iso(s["started"]), "ended": _iso(s["ended"]),
              "seconds": _clip(s["started"], s["ended"], start, end)}
             for s in sessions if s["category"] == "employee"]
    return {
        "period": {"start": _iso(start), "end": _iso(end)},
        "status_changes": [{**r, "ts": _iso(r["ts"])} for r in status],
        "state_changes": [{**r, "ts": _iso(r["ts"])} for r in states],
        "violations": [{"started": _iso(v["started"]), "ended": _iso(v["ended"]),
                        "guest_devices": v["guest_devices"]} for v in violations],
        "presence": presence,
        "staff_visits": staff,
        "traffic": {r["category"]: {"down_bytes": int(r["down_bytes"] or 0),
                                    "up_bytes": int(r["up_bytes"] or 0)} for r in traffic},
    }


def hotel_report(conn: Connection, start: datetime, end: datetime) -> dict:
    """Per room: violations, staff time, guest traffic, status changes."""
    start, end = _naive(start), _naive(end)
    p = {"start": start, "end": end}
    rooms = {r["room_id"]: {"number": r["number"], "name": r["name"], "kind": r["kind"],
                            "violations": 0, "status_changes": 0, "staff_seconds": 0,
                            "staff_visits": 0, "guest_down_bytes": 0, "guest_up_bytes": 0}
             for r in _rows(conn, "SELECT room_id, number, name, kind FROM v1_rooms")}

    def room(room_id):
        return rooms.setdefault(room_id, {
            "number": None, "name": room_id, "kind": None, "violations": 0, "status_changes": 0,
            "staff_seconds": 0, "staff_visits": 0, "guest_down_bytes": 0, "guest_up_bytes": 0})

    for r in _rows(conn, "SELECT room_id, COUNT(*) AS n FROM v1_violations "
                         "WHERE started >= :start AND started < :end GROUP BY room_id", **p):
        room(r["room_id"])["violations"] = int(r["n"])
    for r in _rows(conn, "SELECT room_id, COUNT(*) AS n FROM v1_room_status "
                         "WHERE ts >= :start AND ts < :end GROUP BY room_id", **p):
        room(r["room_id"])["status_changes"] = int(r["n"])
    staff: dict[str, list[int]] = defaultdict(list)
    for r in _rows(conn, "SELECT room_id, started, ended FROM v1_staff_visits "
                         "WHERE started < :end AND ended > :start", **p):
        staff[r["room_id"]].append(_clip(r["started"], r["ended"], start, end))
    for room_id, seconds in staff.items():
        room(room_id).update(staff_seconds=sum(seconds), staff_visits=len(seconds))
    for r in _rows(conn, "SELECT room_id, SUM(down_bytes) AS d, SUM(up_bytes) AS u "
                         "FROM v1_room_traffic_hourly WHERE category = 'guest' "
                         "AND hour >= :start AND hour < :end GROUP BY room_id", **p):
        room(r["room_id"]).update(guest_down_bytes=int(r["d"] or 0), guest_up_bytes=int(r["u"] or 0))
    return {"period": {"start": _iso(start), "end": _iso(end)},
            "rooms": dict(sorted(rooms.items(), key=lambda kv: (kv[1]["number"] or "", kv[0])))}


# -- devices: routes and unregistered staff / fixed devices ------------------ #

_SESSIONS = ("SELECT p.client_mac, p.room_id, p.number, p.name, r.kind, p.started, p.ended "
             "FROM v1_presence p LEFT JOIN v1_rooms r ON r.room_id = p.room_id "
             "WHERE p.started < :end AND p.ended > :start")


def _zone(row) -> dict:
    return {"room_id": row["room_id"], "number": row["number"],
            "name": row["name"] or row["room_id"], "kind": row["kind"]}


def device_route(conn: Connection, macs: str | Collection[str], start: datetime, end: datetime,
                 current: tuple[str, datetime] | Collection[tuple[str, datetime]] | None = None,
                 merge_gap: int = 300) -> dict:
    """Where one device was in the period, in order.

    ``macs``: the device's MAC, or all MACs of a device identity (a phone that
    changed its private MAC) - their sessions form one route.
    ``current`` is the session still open in memory (area_id, since), or one
    per MAC - sessions reach the database only when they end. Stops in the
    same zone less than ``merge_gap`` seconds apart (or overlapping) are
    merged; ``gap_seconds`` is the time the device was nowhere before a stop.
    """
    start, end = _naive(start), _naive(end)
    macs = [macs] if isinstance(macs, str) else list(macs)
    if current is None:
        current = []
    elif isinstance(current, tuple) and len(current) == 2 and isinstance(current[0], str):
        current = [current]
    rows = _rows(conn, _SESSIONS + " AND p.client_mac IN :macs", start=start, end=end,
                 macs=macs) if macs else []
    for area_id, since in current:
        if _naive(since) >= end:
            continue
        zone = next(iter(_rows(conn, "SELECT room_id, number, name, kind FROM v1_rooms "
                                     "WHERE room_id = :room", room=area_id)), None)
        rows.append({"room_id": area_id, "number": zone and zone["number"],
                     "name": zone and zone["name"], "kind": zone and zone["kind"],
                     "started": _naive(since), "ended": None})
    rows.sort(key=lambda r: _dt(r["started"]))

    stops: list[dict] = []
    for row in rows:
        started = max(_dt(row["started"]), start)
        ended = min(_dt(row["ended"]) or end, end)
        last = stops[-1] if stops else None
        if last and last["room_id"] == row["room_id"] \
                and (started - last["_ended"]).total_seconds() < merge_gap:
            last["_ended"] = max(last["_ended"], ended)
            last["open"] = last["open"] or row["ended"] is None
            continue
        gap = int((started - last["_ended"]).total_seconds()) if last else None
        stops.append({**_zone(row), "_started": started, "_ended": ended,
                      "open": row["ended"] is None, "gap_seconds": max(gap, 0) if gap else gap})

    zones: dict[str, int] = defaultdict(int)
    for stop in stops:
        stop["seconds"] = int((stop["_ended"] - stop["_started"]).total_seconds())
        stop["started"], stop["ended"] = _iso(stop.pop("_started")), _iso(stop.pop("_ended"))
        if stop.pop("open"):
            stop["ended"] = None  # still there
        zones[stop["room_id"]] += stop["seconds"]
    return {
        "macs": macs,
        "period": {"start": _iso(start), "end": _iso(end)},
        "stops": stops,
        "seconds_per_zone": dict(sorted(zones.items(), key=lambda kv: -kv[1])),
        "guest_rooms_visited": len({s["room_id"] for s in stops if s["kind"] == "room"}),
    }


def _days(started: datetime, ended: datetime):
    day = started.date()
    while day <= (ended - timedelta(microseconds=1)).date():
        yield day
        day += timedelta(days=1)


def device_candidates(conn: Connection, start: datetime, end: datetime,
                      known: Collection[str], *, min_days: int = 5,
                      min_rooms_per_day: int = 3, limit: int = 50) -> dict:
    """Devices not in the device list that behave like the hotel's own.

    Guests stay a few nights, mostly in their own room; staff come back day
    after day and go from room to room. Per unknown device:

    * ``fixed``: seen on ``min_days`` days or more, 90 % of the time in one zone
      and about all day (20 h+ per day seen) - a TV, a printer, a router.
    * ``employee``: in ``min_rooms_per_day`` guest rooms or more on one day
      (rooms do not hear each other's devices, so this is real movement), or
      seen on ``min_days`` days or more without living in one guest room (a
      long-staying guest spends most of the time in their room).

    ``import_csv`` has the candidates in the device-list CSV format (names
    empty) for ``hotel_sense.import_devices`` once checked.
    """
    start, end = _naive(start), _naive(end)
    known = set(known)
    per_mac: dict[str, dict] = {}
    for row in _rows(conn, _SESSIONS, start=start, end=end):
        mac = row["client_mac"]
        if mac in known:
            continue
        started, ended = max(_dt(row["started"]), start), min(_dt(row["ended"]), end)
        seconds = max(int((ended - started).total_seconds()), 0)
        d = per_mac.setdefault(mac, {"days": set(), "zones": defaultdict(int), "zone": {},
                                     "rooms_per_day": defaultdict(set)})
        d["zones"][row["room_id"]] += seconds
        d["zone"][row["room_id"]] = _zone(row)
        for day in _days(started, ended):
            d["days"].add(day)
            if row["kind"] == "room":
                d["rooms_per_day"][day].add(row["room_id"])
    ssids: dict[str, set[str]] = defaultdict(set)
    if per_mac:
        for row in _rows(conn, "SELECT DISTINCT client_mac, ssid FROM v1_wifi_events "
                               "WHERE ts >= :start AND ts < :end AND ssid IS NOT NULL",
                         start=start, end=end):
            if row["client_mac"] in per_mac:
                ssids[row["client_mac"]].add(row["ssid"])

    candidates = []
    for mac, d in per_mac.items():
        total = sum(d["zones"].values())
        if not total:
            continue
        top_id = max(d["zones"], key=d["zones"].get)
        top, share = d["zone"][top_id], d["zones"][top_id] / total
        days = len(d["days"])
        hours_per_day = total / 3600 / days
        rooms_per_day = max((len(r) for r in d["rooms_per_day"].values()), default=0)
        guest_rooms = len({r for rooms in d["rooms_per_day"].values() for r in rooms})
        reasons, suggest = [], None
        if days >= min_days and share >= 0.9 and hours_per_day >= 20:
            suggest = "fixed"
            reasons.append(f"always on in {top['name']}: {days} days, "
                           f"{hours_per_day:.0f} h a day, {share:.0%} of the time there")
        else:
            if rooms_per_day >= min_rooms_per_day:
                suggest = "employee"
                reasons.append(f"{rooms_per_day} guest rooms in one day")
            if days >= min_days and not (top["kind"] == "room" and share >= 0.6):
                suggest = "employee"
                reasons.append(f"seen on {days} days, mostly in {top['name']} ({share:.0%})")
        if suggest is None:
            continue
        candidates.append({
            "mac": mac, "suggest": suggest, "reasons": reasons, "days": days,
            "hours": round(total / 3600, 1), "guest_rooms": guest_rooms,
            "max_guest_rooms_per_day": rooms_per_day,
            "main_zone": top["name"], "main_zone_share": round(share, 2),
            "random_mac": is_random_mac(mac), "ssids": sorted(ssids.get(mac, ())),
        })
    candidates.sort(key=lambda c: (c["suggest"] != "fixed", -c["days"],
                                   -c["max_guest_rooms_per_day"], c["mac"]))
    candidates = candidates[:limit]
    csv_lines = ["mac,name,category,note"] + [
        f"{c['mac']},,{c['suggest']},{c['reasons'][0].replace(',', ';')}" for c in candidates]
    return {"period": {"start": _iso(start), "end": _iso(end)},
            "candidates": candidates,
            "import_csv": "\n".join(csv_lines) + "\n" if candidates else ""}
