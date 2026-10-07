"""Reports through the v1 views: room_report / hotel_report (queries + actions)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.exceptions import ServiceValidationError
from sqlalchemy import create_engine, insert, select

from custom_components.hotel_sense import history
from custom_components.hotel_sense.const import DOMAIN
from custom_components.hotel_sense.queries import hotel_report, room_report

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
