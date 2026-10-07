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
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.engine import Connection

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
