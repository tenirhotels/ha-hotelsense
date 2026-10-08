"""MAC registry: every Wi-Fi MAC seen (macs table), network roles in candidates."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, insert, select, text, update

from custom_components.hotel_sense import history
from custom_components.hotel_sense.const import DOMAIN
from custom_components.hotel_sense.history import HistoryWriter, merge_mac
from custom_components.hotel_sense.mac_registry import MacTracker
from custom_components.hotel_sense.omada_hub import client_from
from custom_components.hotel_sense.queries import device_candidates, suggestion_data
from custom_components.hotel_sense.ssid_roles import SsidRoles

from .fakes import GUEST_PHONE, client_raw
from .test_history import _db_hotel, _entry_with_db, _sqlite, db_url  # noqa: F401
from .test_stage_a import _hotel, _options_menu, _poll

T0 = datetime(2026, 1, 1, 8)
PHONE, SENSOR = "02-00-00-00-00-b1", "00-11-22-00-00-b2"


def _client(mac, name="Phone", ssid="Guest", **extra):
    return client_from({**client_raw(mac.upper(), name, ssid=ssid), **extra})


def test_omada_fingerprint_fields():
    phone = _client(PHONE, vendor="Samsung", model="Galaxy A16", osName="Android")
    assert (phone.vendor, phone.model, phone.os) == ("Samsung", "Galaxy A16", "Android")
    unknown = _client(PHONE, vendor="-", model="Unknown", osName="")
    assert (unknown.vendor, unknown.model, unknown.os) == (None, None, None)


def test_tracker_counts_time_on_the_wifi_and_hands_over_every_5_minutes():
    tracker = MacTracker()
    phone = _client(PHONE, model="Galaxy A16")
    for minute in (0, 1, 2):
        tracker.observe(T0 + timedelta(minutes=minute), [phone])
    tracker.observe(T0 + timedelta(minutes=3), [])  # gone
    tracker.observe(T0 + timedelta(minutes=30), [_client(PHONE, ssid="Staff")])  # back
    [row] = tracker.take(T0 + timedelta(minutes=30), lambda mac: "7")
    assert (row["first_seen"], row["last_seen"]) == (T0, T0 + timedelta(minutes=30))
    assert row["seconds"] == 120  # the absence is not counted
    assert row["random"] is True and row["model"] == "Galaxy A16"
    assert row["ssids"] == {"Guest", "Staff"} and row["identity_id"] == "7"
    tracker.observe(T0 + timedelta(minutes=31), [phone])
    assert tracker.take(T0 + timedelta(minutes=32), lambda mac: None) == []  # < 5 min
    assert len(tracker.take(T0 + timedelta(minutes=32), lambda mac: None, force=True)) == 1
    assert tracker.take(T0 + timedelta(hours=1), lambda mac: None) == []  # nothing new
    wired = client_from(client_raw("10-20-30-40-50-60", "PC", wireless=False))
    tracker.observe(T0 + timedelta(hours=1), [wired])
    assert tracker.take(T0 + timedelta(hours=2), lambda mac: None) == []


def test_merge_keeps_earliest_latest_and_adds_up():
    old = {"mac": PHONE, "first_seen": T0, "last_seen": T0, "seconds": 60, "random": True,
           "name": "Phone", "model": None, "ssids": "Guest", "identity_id": None}
    new = {"mac": PHONE, "first_seen": T0 + timedelta(days=1), "last_seen": T0 + timedelta(days=1),
           "seconds": 30, "random": True, "model": "Galaxy A16", "ssids": {"Staff"},
           "identity_id": "7"}
    merged = merge_mac(old, new)
    assert (merged["first_seen"], merged["last_seen"], merged["seconds"]) == (
        T0, T0 + timedelta(days=1), 90)
    assert merged["name"] == "Phone" and merged["model"] == "Galaxy A16"
    assert merged["ssids"] == {"Guest", "Staff"} and merged["identity_id"] == "7"


def _row(mac, first, last, **extra):
    return {"mac": mac, "first_seen": first, "last_seen": last, "seconds": 60,
            "random": True, "name": "Someone's phone", "ssids": {"Guest"}, **extra}


def test_writer_merges_rows_and_keeps_guest_names_for_a_set_time(tmp_path):
    url = _sqlite(tmp_path)
    history.check_connection(url)
    writer = HistoryWriter(None, url, 12)
    writer._engine = create_engine(url)
    now = history.now()
    old = now - timedelta(days=100)
    writer._write([], None, {PHONE: _row(PHONE, old, old, identity_id=None),
                             SENSOR: _row(SENSOR, old, old, identity_id="3", random=False)})
    writer._write([], None, {PHONE: _row(PHONE, now, now, ssids={"Staff"}, identity_id=None)})
    rows = {r.mac: r for r in writer._engine.connect().execute(select(history.macs))}
    assert rows[PHONE].first_seen == old and rows[PHONE].last_seen == now
    assert rows[PHONE].seconds == 120 and rows[PHONE].ssids == "Guest,Staff"

    # 90 days: the guest's name (seen 100 days ago) goes, the listed device's stays.
    writer.guest_name_days = 90
    with writer._engine.begin() as conn:
        conn.execute(update(history.macs).where(history.macs.c.mac == PHONE)
                     .values(last_seen=old))
    writer._purge(now - timedelta(days=365))
    rows = {r.mac: r for r in writer._engine.connect().execute(select(history.macs))}
    assert rows[PHONE].name is None and rows[SENSOR].name == "Someone's phone"
    # Older than the retention: the row goes.
    writer._purge(now - timedelta(days=50))
    assert [r.mac for r in writer._engine.connect().execute(select(history.macs))] == []
    # 0 days: guests' names are never written.
    writer.guest_name_days = 0
    writer._write([], None, {PHONE: _row(PHONE, now, now, identity_id=None)})
    assert writer._engine.connect().execute(select(history.macs.c.name)).scalar() is None
    writer._engine.dispose()


async def test_polls_fill_the_registry(hass, make_entry, patch_api, db_url,  # noqa: F811
                                       freezer):
    controller = await _db_hotel(hass, _entry_with_db(make_entry))
    await hass.services.async_call(DOMAIN, "import_devices", {
        "csv": f"mac,name,category\n{GUEST_PHONE},Admin,employee\n"}, blocking=True,
        return_response=True)
    for _ in range(3):
        freezer.tick(timedelta(seconds=60))
        await _poll(hass, controller)
    await controller.async_stop_history()  # HA stop: hands over and writes everything
    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = {r.mac: r for r in conn.execute(text("SELECT * FROM v1_macs"))}
    engine.dispose()
    phone = rows[GUEST_PHONE]
    assert phone.identity_id == "1" and phone.name == "Guest-Phone" and phone.random
    assert phone.ssids == "Guest" and phone.seconds >= 120


def test_network_roles_make_candidates(tmp_path):
    url = _sqlite(tmp_path)
    history.check_connection(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(insert(history.presence_sessions), [
            {"client_mac": m, "area_id": "room_03", "category": "guest", "started": T0,
             "ended": T0 + timedelta(hours=h), "seconds": h * 3600}
            for m, h in ((SENSOR.upper(), 2), (PHONE.upper(), 30))])
        conn.execute(insert(history.macs), [
            {"mac": SENSOR.upper(), "first_seen": T0, "last_seen": T0, "seconds": 7200,
             "random": False, "ssids": "iot", "updated": T0},
            {"mac": PHONE.upper(), "first_seen": T0, "last_seen": T0, "seconds": 7200,
             "random": True, "ssids": "hotel-service", "updated": T0}])
    roles = SsidRoles.parse("iot = equipment\nhotel-service = staff\nGUEST{n} = guest_room")
    with engine.connect() as conn:
        found = {c["mac"]: c for c in device_candidates(
            conn, T0, T0 + timedelta(days=3), known=(), roles=roles)["candidates"]}
        without = device_candidates(conn, T0, T0 + timedelta(days=3), known=())
    assert found[SENSOR.upper()]["suggest"] == "fixed"
    assert found[SENSOR.upper()]["reasons"] == ["on an equipment Wi-Fi network"]
    assert found[PHONE.upper()]["suggest"] == "employee"  # 2 days on the staff network
    assert found[PHONE.upper()]["network_roles"] == ["staff"]
    assert without["candidates"] == []  # no roles: no evidence


def test_a_mac_known_to_the_registry_is_not_new(tmp_path):
    url = _sqlite(tmp_path)
    history.check_connection(url)
    engine = create_engine(url)
    now = history.now()
    with engine.begin() as conn:
        conn.execute(insert(history.macs), [{
            "mac": PHONE.upper(), "first_seen": now - timedelta(days=200), "last_seen": now,
            "seconds": 1, "random": True, "model": "Galaxy A16", "updated": now}])
    with engine.connect() as conn:
        data = suggestion_data(conn, now - timedelta(days=30), now - timedelta(days=7), (), (),
                               open_macs={PHONE.upper()})
    assert data["candidates"] == []


async def test_network_roles_option(hass, make_entry, patch_api):
    entry = make_entry()
    controller = await _hotel(hass, entry)
    flow = await _options_menu(hass, entry, "presence")
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "ssid_roles": "iot = boss"})
    assert result["errors"] == {"ssid_roles": "invalid_ssid_roles"}
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "ssid_roles": "GUEST{n} = guest_room\niot = equipment"})
    await hass.async_block_till_done()
    assert entry.options["ssid_roles"].startswith("GUEST{n}")
    assert controller.presence.ssid_roles.role_of("guest4").room == "4"
    flow = await _options_menu(hass, entry, "presence")
    await hass.config_entries.options.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    assert entry.options["ssid_roles"] == "" and not controller.presence.ssid_roles
