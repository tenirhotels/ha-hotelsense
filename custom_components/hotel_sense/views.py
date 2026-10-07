"""SQL views ``v1_*``: the stable interface of the history database.

Grafana, a future web admin and Hotel Sense's own reports read these views,
not the tables: the tables may change, a view keeps its name, columns and
meaning. A change of meaning gets a new version (``v2_*``) next to the old one.

All times are UTC. ``room_id`` is the Hotel Sense room (= HA area id); the
``rooms`` table adds the room number, name and kind.

The SQL is plain enough for MariaDB / MySQL and SQLite (tests).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

_ROOM = "LEFT JOIN rooms r ON r.room_id = {alias}.area_id"

# name -> SELECT; created in this order (later views use earlier ones).
VIEWS_V1: dict[str, str] = {
    "v1_rooms": "SELECT room_id, number, name, kind FROM rooms",
    "v1_room_status": (
        "SELECT s.ts, s.area_id AS room_id, r.number, r.name, s.status, s.source, s.booking "
        "FROM room_status s " + _ROOM.format(alias="s")),
    "v1_room_states": (
        "SELECT s.ts, s.area_id AS room_id, r.number, r.name, s.status, s.old_state, s.state, "
        "s.guest_devices, s.employee_devices FROM room_states s " + _ROOM.format(alias="s")),
    "v1_violations": (
        "SELECT s.area_id AS room_id, r.number, r.name, s.ts AS started, "
        "(SELECT MIN(n.ts) FROM room_states n WHERE n.area_id = s.area_id AND n.ts > s.ts) "
        "AS ended, s.guest_devices FROM room_states s " + _ROOM.format(alias="s")
        + " WHERE s.state = 'violation'"),
    "v1_presence": (
        "SELECT p.client_mac, p.area_id AS room_id, r.number, r.name, p.category, p.started, "
        "p.ended, p.seconds FROM presence_sessions p " + _ROOM.format(alias="p")),
    "v1_staff_visits": "SELECT * FROM v1_presence WHERE category = 'employee'",
    "v1_room_traffic_hourly": (
        "SELECT t.ts AS hour, t.area_id AS room_id, r.number, r.name, t.category, "
        "SUM(t.down_bytes) AS down_bytes, SUM(t.up_bytes) AS up_bytes, MAX(t.devices) AS devices "
        "FROM room_traffic t " + _ROOM.format(alias="t")
        + " GROUP BY t.ts, t.area_id, r.number, r.name, t.category"),
    "v1_room_traffic_daily": (
        "SELECT DATE(hour) AS day, room_id, number, name, category, "
        "SUM(down_bytes) AS down_bytes, SUM(up_bytes) AS up_bytes "
        "FROM v1_room_traffic_hourly GROUP BY DATE(hour), room_id, number, name, category"),
    "v1_pms_events": (
        "SELECT ts, event_id, event, booking, status, result, rooms FROM pms_events"),
    "v1_wifi_events": (
        "SELECT ts, event, client_mac, ap_mac, from_ap_mac, ssid, connected_seconds, traffic_kb "
        "FROM wifi_events"),
}


def create_views(conn: Connection) -> None:
    """(Re)create the views: their definitions always match this version."""
    for name in reversed(VIEWS_V1):
        conn.execute(text(f"DROP VIEW IF EXISTS {name}"))
    for name, select_sql in VIEWS_V1.items():
        conn.execute(text(f"CREATE VIEW {name} AS {select_sql}"))
