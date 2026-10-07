"""Reports through the v1 views: room_report / hotel_report (queries + actions)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.exceptions import ServiceValidationError
from sqlalchemy import create_engine, insert, select

from custom_components.hotel_sense import history
from custom_components.hotel_sense.const import DOMAIN
from custom_components.hotel_sense.queries import (
    device_candidates, device_route, hotel_report, room_report,
)

from .fakes import GUEST_PHONE
from .test_history import _db_hotel, _entry_with_db, _sqlite, db_url  # noqa: F401

T0 = datetime(2026, 1, 1, 0, 0)
STAFF, GUEST = "02-00-00-00-00-0E", "02-00-00-00-00-0A"


def _db(tmp_path):
    url = _sqlite(tmp_path)
    history.check_connection(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(insert(history.rooms), [
            {"room_id": "room_06", "number": "06", "name": "Room 06", "kind": "room", "updated": T0},
            {"room_id": "admin_house", "number": None, "name": "Admin House", "kind": "common",
             "updated": T0}])
        conn.execute(insert(history.room_status), [
            {"ts": T0 + timedelta(hours=1), "area_id": "room_06", "status": "checked_out",
             "source": "exely", "booking": "B-1"}])
        conn.execute(insert(history.room_states), [
            {"ts": T0 + timedelta(hours=2), "area_id": "room_06", "status": "checked_out",
             "old_state": "empty", "state": "violation", "guest_devices": 1, "employee_devices": 0},
            {"ts": T0 + timedelta(hours=3), "area_id": "room_06", "status": "checked_out",
             "old_state": "violation", "state": "empty", "guest_devices": 0, "employee_devices": 0}])
        conn.execute(insert(history.presence_sessions), [
            {"client_mac": STAFF, "area_id": "room_06", "category": "employee",
             "started": T0 - timedelta(minutes=30), "ended": T0 + timedelta(minutes=30),
             "seconds": 3600},
            {"client_mac": GUEST, "area_id": "room_06", "category": "guest",
             "started": T0 + timedelta(hours=2), "ended": T0 + timedelta(hours=3), "seconds": 3600}])
        conn.execute(insert(history.room_traffic), [
            {"ts": T0 + timedelta(hours=h), "area_id": "room_06", "category": "guest",
             "down_bytes": 1000, "up_bytes": 100, "devices": 1} for h in (2, 3)])
    return engine


def test_room_report(tmp_path):
    engine = _db(tmp_path)
    with engine.connect() as conn:
        report = room_report(conn, "room_06", T0, T0 + timedelta(days=1),
                             lambda mac: "Housekeeping phone" if mac == STAFF else None)
    assert report["period"] == {"start": "2026-01-01T00:00:00+00:00",
                                "end": "2026-01-02T00:00:00+00:00"}
    assert report["status_changes"] == [{"ts": "2026-01-01T01:00:00+00:00", "status": "checked_out",
                                         "source": "exely", "booking": "B-1"}]
    assert [s["state"] for s in report["state_changes"]] == ["violation", "empty"]
    assert report["violations"] == [{"started": "2026-01-01T02:00:00+00:00",
                                     "ended": "2026-01-01T03:00:00+00:00", "guest_devices": 1}]
    # Staff session clipped to the period (30 of 60 minutes).
    assert report["presence"]["employee"] == {"devices": 1, "sessions": 1, "seconds": 1800}
    assert report["presence"]["guest"]["seconds"] == 3600
    assert report["staff_visits"][0]["name"] == "Housekeeping phone"
    assert report["traffic"] == {"guest": {"down_bytes": 2000, "up_bytes": 200}}


def test_hotel_report(tmp_path):
    engine = _db(tmp_path)
    with engine.connect() as conn:
        report = hotel_report(conn, T0, T0 + timedelta(days=1))
    room = report["rooms"]["room_06"]
    assert (room["number"], room["violations"], room["status_changes"]) == ("06", 1, 1)
    assert (room["staff_seconds"], room["staff_visits"], room["guest_down_bytes"]) == (1800, 1, 2000)
    assert report["rooms"]["admin_house"]["kind"] == "common"


def test_aware_times_are_utc(tmp_path):
    engine = _db(tmp_path)
    almaty = timezone(timedelta(hours=5))
    with engine.connect() as conn:  # 05:00 +05:00 = 00:00 UTC
        report = room_report(conn, "room_06", datetime(2026, 1, 1, 5, tzinfo=almaty),
                             datetime(2026, 1, 1, 7, tzinfo=almaty))
    assert report["period"]["start"] == "2026-01-01T00:00:00+00:00"
    assert [s["status"] for s in report["status_changes"]] == ["checked_out"]


async def test_report_actions(hass, make_entry, patch_api, db_url):  # noqa: F811
    entry = _entry_with_db(make_entry)
    await _db_hotel(hass, entry)
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_06_status", "option": "checked_in"}, blocking=True)
    report = await hass.services.async_call(DOMAIN, "room_report", {"room": "06", "hours": 1},
                                            blocking=True, return_response=True)
    assert report["room"]["room_id"] == "room_06" and report["room"]["status"]["value"] == "checked_in"
    assert [s["source"] for s in report["status_changes"]] == ["manual"]
    assert report["presence"]["guest"]["devices"] == 0  # sessions are written when they end
    # The room model is in the database (rooms table / v1_rooms).
    engine = create_engine(db_url)
    with engine.connect() as conn:
        assert ("room_06", "06", "room") in [tuple(r) for r in conn.execute(
            select(history.rooms.c.room_id, history.rooms.c.number, history.rooms.c.kind))]
    engine.dispose()

    hotel = await hass.services.async_call(DOMAIN, "hotel_report", {}, blocking=True,
                                           return_response=True)
    assert hotel["rooms"]["room_06"]["status_changes"] == 1
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "room_report", {"room": "99"}, blocking=True,
                                       return_response=True)


async def test_reports_need_the_database(hass, make_entry, patch_api):
    await _db_hotel(hass, make_entry())
    with pytest.raises(ServiceValidationError, match="not set up"):
        await hass.services.async_call(DOMAIN, "hotel_report", {}, blocking=True,
                                       return_response=True)


# -- device routes and unregistered staff / fixed devices -------------------- #

MAID, TV, LONG_STAY, SHORT_STAY = ("02-00-00-00-00-1A", "00-11-22-00-00-1B",
                                   "02-00-00-00-00-1C", "02-00-00-00-00-1D")
ROOMS = ("room_01", "room_02", "room_03", "room_04")


def _devices_db(tmp_path):
    url = _sqlite(tmp_path)
    history.check_connection(url)
    engine = create_engine(url)
    rooms = [{"room_id": r, "number": r[-2:], "name": f"Room {r[-2:]}", "kind": "room",
              "updated": T0} for r in ROOMS]
    rooms += [{"room_id": a, "number": None, "name": n, "kind": "common", "updated": T0}
              for a, n in (("lobby", "Lobby"), ("admin_house", "Admin House"))]
    sessions = []

    def stay(mac, area, day, hour, minutes, category="guest"):
        started = T0 + timedelta(days=day, hours=hour)
        ended = started + timedelta(minutes=minutes)
        sessions.append({"client_mac": mac, "area_id": area, "category": category,
                         "started": started, "ended": ended, "seconds": minutes * 60})

    for day in range(6):  # housekeeping: admin house, then three guest rooms, every day
        stay(MAID, "admin_house", day, 8, 60)
        for i, room in enumerate(ROOMS[:3]):
            stay(MAID, room, day, 9 + i, 40)
        stay(TV, "lobby", day, 0, 24 * 60)
        stay(LONG_STAY, "room_04", day, 18, 14 * 60)
        stay(LONG_STAY, "lobby", day, 9, 60)
    stay(SHORT_STAY, "room_01", 0, 18, 600)
    stay(SHORT_STAY, "room_01", 1, 18, 600)
    with engine.begin() as conn:
        conn.execute(insert(history.rooms), rooms)
        conn.execute(insert(history.presence_sessions), sessions)
        conn.execute(insert(history.wifi_events), [
            {"ts": T0 + timedelta(hours=8), "event": "connected", "client_mac": MAID,
             "ssid": "Staff"}])
    return engine


def test_device_route(tmp_path):
    engine = _devices_db(tmp_path)
    with engine.connect() as conn:
        route = device_route(conn, MAID, T0 + timedelta(hours=8, minutes=30),
                             T0 + timedelta(hours=12))
    stops = route["stops"]
    assert [s["room_id"] for s in stops] == ["admin_house", *ROOMS[:3]]
    assert stops[0]["started"] == "2026-01-01T08:30:00+00:00"  # clipped to the period
    assert stops[0]["seconds"] == 1800 and stops[0]["gap_seconds"] is None
    assert stops[1]["gap_seconds"] == 0 and stops[2]["gap_seconds"] == 20 * 60
    assert stops[1]["name"] == "Room 01" and stops[1]["kind"] == "room"
    assert route["guest_rooms_visited"] == 3
    assert route["seconds_per_zone"]["room_01"] == 2400


def test_device_route_merges_short_drops_and_adds_open_session(tmp_path):
    engine = _devices_db(tmp_path)
    with engine.begin() as conn:  # dropped off Wi-Fi for 2 minutes, same room
        conn.execute(insert(history.presence_sessions), [
            {"client_mac": GUEST, "area_id": "room_02", "category": "guest",
             "started": T0, "ended": T0 + timedelta(minutes=10), "seconds": 600},
            {"client_mac": GUEST, "area_id": "room_02", "category": "guest",
             "started": T0 + timedelta(minutes=12), "ended": T0 + timedelta(minutes=30),
             "seconds": 1080}])
    with engine.connect() as conn:
        route = device_route(conn, GUEST, T0, T0 + timedelta(hours=2),
                             current=("lobby", T0 + timedelta(hours=1)))
    assert [(s["room_id"], s["seconds"], s["ended"]) for s in route["stops"]] == [
        ("room_02", 1800, "2026-01-01T00:30:00+00:00"),
        ("lobby", 3600, None)]  # still there
    assert route["stops"][1]["gap_seconds"] == 1800 and route["stops"][1]["name"] == "Lobby"


def test_device_candidates(tmp_path):
    engine = _devices_db(tmp_path)
    with engine.connect() as conn:
        result = device_candidates(conn, T0, T0 + timedelta(days=7), known=())
    found = {c["mac"]: c for c in result["candidates"]}
    # The long-staying guest (own room most of the time) and the short stay are not staff.
    assert set(found) == {MAID, TV}
    assert found[TV]["suggest"] == "fixed" and found[TV]["main_zone"] == "Lobby"
    assert found[TV]["random_mac"] is False
    maid = found[MAID]
    assert maid["suggest"] == "employee" and maid["days"] == 6
    assert maid["max_guest_rooms_per_day"] == 3 and maid["guest_rooms"] == 3
    assert maid["ssids"] == ["Staff"] and maid["random_mac"] is True
    assert maid["reasons"][0] == "3 guest rooms in one day"
    assert result["candidates"][0]["mac"] == TV  # fixed first
    lines = result["import_csv"].splitlines()
    assert lines[0] == "mac,name,category,note" and lines[1].startswith(f"{TV},,fixed,")


def test_device_candidates_skip_known_and_use_thresholds(tmp_path):
    engine = _devices_db(tmp_path)
    with engine.connect() as conn:
        assert {c["mac"] for c in device_candidates(
            conn, T0, T0 + timedelta(days=7), known={TV})["candidates"]} == {MAID}
        # One day of housekeeping is enough by rooms; by days alone it is not.
        one_day = device_candidates(conn, T0, T0 + timedelta(days=1), known={TV})
        assert [c["mac"] for c in one_day["candidates"]] == [MAID]
        none = device_candidates(conn, T0, T0 + timedelta(days=1), known={TV},
                                 min_rooms_per_day=4)
        assert none == {"period": none["period"], "candidates": [], "import_csv": ""}
        # The long stay counts once it is not mostly in one guest room.
        assert LONG_STAY in {c["mac"] for c in device_candidates(
            conn, T0 + timedelta(hours=8), T0 + timedelta(hours=11), known=(),
            min_days=1)["candidates"]}


async def test_device_actions(hass, make_entry, patch_api, db_url):  # noqa: F811
    entry = _entry_with_db(make_entry)
    controller = await _db_hotel(hass, entry)
    assert controller.presence.sessions.get(GUEST_PHONE)  # open session, not in the DB yet
    route = await hass.services.async_call(DOMAIN, "device_route",
                                           {"mac": GUEST_PHONE.lower().replace("-", ":")},
                                           blocking=True, return_response=True)
    assert route["macs"] == [GUEST_PHONE] and route["name"] is None and route["identity"] is None
    assert [s["room_id"] for s in route["stops"]] == ["room_06"]
    assert route["stops"][0]["ended"] is None
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "device_route", {"mac": "nope"}, blocking=True,
                                       return_response=True)
    found = await hass.services.async_call(DOMAIN, "device_candidates", {}, blocking=True,
                                           return_response=True)
    assert found["candidates"] == [] and found["import_csv"] == ""
    # With devices in the list (the list yields devices, not MACs).
    await hass.services.async_call(DOMAIN, "import_devices",
                                   {"csv": f"mac,name,category\n{MAID},Maid,employee\n"},
                                   blocking=True, return_response=True)
    found = await hass.services.async_call(DOMAIN, "device_candidates", {}, blocking=True,
                                           return_response=True)
    assert found["candidates"] == []
    route = await hass.services.async_call(DOMAIN, "device_route", {"mac": MAID}, blocking=True,
                                           return_response=True)
    assert (route["name"], route["category"], route["stops"]) == ("Maid", "employee", [])


async def test_candidate_csv_imports(hass, make_entry, patch_api, db_url, tmp_path):  # noqa: F811
    await _db_hotel(hass, _entry_with_db(make_entry))
    engine = _devices_db(tmp_path)
    with engine.connect() as conn:
        csv_text = device_candidates(conn, T0, T0 + timedelta(days=7), known=())["import_csv"]
    result = await hass.services.async_call(DOMAIN, "import_devices", {"csv": csv_text},
                                            blocking=True, return_response=True)
    assert not result.get("errors"), result
    exported = await hass.services.async_call(DOMAIN, "export_devices", {}, blocking=True,
                                              return_response=True)
    assert {(d["mac"], d["category"]) for d in exported["devices"]} >= {
        (MAID, "employee"), (TV, "fixed")}
