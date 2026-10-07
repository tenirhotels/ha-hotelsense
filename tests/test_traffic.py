"""Traffic per room and hour from the controller's per-client counters."""
from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.hotel_sense.traffic import TrafficMeter

from .fakes import GUEST_PHONE, SHARED_MAC, client_raw
from .test_history import _db_hotel, _entry_with_db, _rows, db_url  # noqa: F401  (fixture)
from .test_stage_a import _poll, _set_clients

H1 = datetime(2026, 1, 1, 10)
H2 = H1 + timedelta(hours=1)
A, B = "02-00-00-00-00-0A", "02-00-00-00-00-0B"


def _meter_update(meter, hour, counters, rooms, categories=None):
    return meter.update(hour, counters, rooms.get, lambda mac: (categories or {}).get(mac, "guest"))


def test_growth_is_added_to_the_room_and_hour():
    meter = TrafficMeter()
    rooms = {A: "room_01", B: "room_02"}
    assert _meter_update(meter, H1, {A: (5000, 900)}, rooms) == []  # baseline after start
    _meter_update(meter, H1, {A: (6000, 1000), B: (300, 30)}, rooms)  # B connected later
    _meter_update(meter, H1, {A: (6500, 1000), B: (400, 40)}, rooms)
    rows = _meter_update(meter, H2, {A: (6600, 1000)}, rooms)  # new hour: H1 is written
    assert rows == [
        {"ts": H1, "area_id": "room_01", "category": "guest", "down_bytes": 1500, "up_bytes": 100,
         "devices": 1},
        {"ts": H1, "area_id": "room_02", "category": "guest", "down_bytes": 400, "up_bytes": 40,
         "devices": 1}]
    assert meter.flush() == [{"ts": H2, "area_id": "room_01", "category": "guest",
                              "down_bytes": 100, "up_bytes": 0, "devices": 1}]


def test_reconnect_counter_reset_and_roaming():
    meter = TrafficMeter()
    rooms = {A: "room_01"}
    _meter_update(meter, H1, {A: (1000, 100)}, rooms)
    _meter_update(meter, H1, {A: (200, 20)}, rooms)        # counter went down: new connection
    rooms[A] = "room_02"                                    # moved
    _meter_update(meter, H1, {A: (700, 20)}, rooms)
    _meter_update(meter, H1, {}, rooms)                     # left ...
    _meter_update(meter, H1, {A: (50, 5)}, rooms)           # ... and came back: from zero
    assert meter.flush() == [
        {"ts": H1, "area_id": "room_01", "category": "guest", "down_bytes": 200, "up_bytes": 20,
         "devices": 1},
        {"ts": H1, "area_id": "room_02", "category": "guest", "down_bytes": 550, "up_bytes": 5,
         "devices": 1}]


def test_no_room_no_traffic_and_categories():
    meter = TrafficMeter()
    _meter_update(meter, H1, {A: (0, 0), B: (0, 0)}, {})
    rows_rooms = {B: "room_01"}
    _meter_update(meter, H1, {A: (100, 10), B: (100, 10)}, rows_rooms, {B: "employee"})
    assert meter.flush() == [{"ts": H1, "area_id": "room_01", "category": "employee",
                              "down_bytes": 100, "up_bytes": 10, "devices": 1}]
    assert meter.flush() == []


async def test_traffic_is_written_to_the_database(hass, make_entry, patch_api, db_url):  # noqa: F811
    entry = _entry_with_db(make_entry)
    controller = await _db_hotel(hass, entry)   # first poll: baseline
    clients = patch_api.responses["/clients"]["data"]
    _set_clients(patch_api, [
        dict(c, trafficDown=c["trafficDown"] + 10_000, trafficUp=c["trafficUp"] + 500)
        if c["mac"] in (GUEST_PHONE, SHARED_MAC) else c for c in clients])
    await _poll(hass, controller)
    assert await hass.config_entries.async_unload(entry.entry_id)  # writes the hour so far
    rows = _rows(db_url, "room_traffic")
    assert [(r["area_id"], r["category"], r["down_bytes"], r["up_bytes"], r["devices"])
            for r in rows] == [("room_06", "guest", 20_000, 1_000, 2)]
    assert rows[0]["ts"].minute == 0 and rows[0]["ts"].second == 0
