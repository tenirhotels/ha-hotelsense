"""History database: Omada events, presence sessions, room states / statuses, Exely events.

Tests run on SQLite (same SQLAlchemy tables); production uses MariaDB via PyMySQL.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from sqlalchemy import create_engine, func, select

from custom_components.hotel_sense import history
from custom_components.hotel_sense.const import (
    CONF_DB_HOST, CONF_DB_NAME, CONF_DB_PASSWORD, CONF_DB_PORT, CONF_DB_RETENTION,
    CONF_DB_USERNAME, DOMAIN,
)
from custom_components.hotel_sense.diagnostics import async_get_config_entry_diagnostics
from custom_components.hotel_sense.history import HistoryWriter, build_url
from custom_components.hotel_sense.omada_events import (
    EVENT_OFFLINE, EVENT_ONLINE, EVENT_ROAMING, parse_message, parse_payload,
)

from .fakes import AP_WF06, AP_WF07, GUEST_PHONE, client_raw
from .test_exely import _post as _exely_post
from .test_omada_webhook import _post as _omada_post
from .test_stage_a import _hotel, _options_menu, _poll, _set_clients, _state

DB = {CONF_DB_HOST: "core-mariadb", CONF_DB_PORT: 3306, CONF_DB_USERNAME: "hotel_sense",
      CONF_DB_PASSWORD: "pw", CONF_DB_NAME: "hotel_sense"}
CLIENT = "02-11-22-33-44-55"


# --------------------------------------------------------------------------- #
# Omada messages (pure)
# --------------------------------------------------------------------------- #
def test_online_offline_roaming_messages():
    online = parse_message(f'[client:Ivan-iPhone:{CLIENT}] (IP: 10.0.0.5) went online on '
                           f'[ap:WF06:{AP_WF06}] with SSID "Guest"')
    assert (online.event, online.client_mac, online.ap_mac, online.ssid) == (
        EVENT_ONLINE, CLIENT, AP_WF06, "Guest")

    offline = parse_message(f'[client:{CLIENT}:{CLIENT}] (IP: 10.0.0.5) went offline from SSID '
                            f'"Guest" on [ap:WF06:{AP_WF06}] (1h 2m 10s connected, 1.5MB).')
    assert (offline.event, offline.ap_mac, offline.connected_seconds, offline.traffic_kb) == (
        EVENT_OFFLINE, AP_WF06, 3730, 1536.0)

    roaming = parse_message(f'[client:bao-bao:{CLIENT}] is roaming from [ap:WF06:{AP_WF06}]'
                            f'[Channel 36] to [ap:WF07:{AP_WF07}][Channel 1] with SSID "Guest"')
    assert (roaming.event, roaming.from_ap_mac, roaming.ap_mac) == (EVENT_ROAMING, AP_WF06, AP_WF07)

    # Names and IP addresses are not kept.
    assert "Ivan" not in repr(online) and "10.0.0.5" not in repr(offline)


def test_short_and_unknown_messages():
    short = parse_message(f"[client:{CLIENT}] connected to [ap:WF06]")
    assert (short.event, short.client_mac, short.ap_mac) == (EVENT_ONLINE, CLIENT, None)
    assert parse_message("[ap:WF06] was upgraded") is None
    assert parse_message(None) is None
    assert parse_payload({"description": "This is a webhook test message"}) == []
    assert len(parse_payload({"text": f"[client:{CLIENT}] went offline"})) == 1


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #
def _sqlite(tmp_path, name="history.db") -> str:
    return f"sqlite:///{tmp_path / name}"


def _rows(url: str, table: str) -> list[dict]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(history.TABLES[table]))]
    finally:
        engine.dispose()


def test_mariadb_url():
    url = build_url("core-mariadb", 3306, "hotel_sense", "p@ss:word", "hotel_sense")
    assert url.drivername == "mysql+pymysql" and url.host == "core-mariadb"
    assert url.password == "p@ss:word" and url.query["charset"] == "utf8mb4"


async def test_rows_are_written_in_batches(hass, tmp_path):
    url = _sqlite(tmp_path)
    writer = HistoryWriter(hass, url)
    writer.add("room_status", area_id="room_06", status="checked_in", source="manual")
    writer.add("wifi_events", event="online", client_mac=CLIENT, ap_mac=AP_WF06, ssid="Guest")
    assert writer.queued == 2
    await writer.async_flush()
    assert writer.queued == 0 and writer.written == 2 and writer.connected
    assert _rows(url, "room_status")[0]["status"] == "checked_in"
    assert _rows(url, "wifi_events")[0]["client_mac"] == CLIENT
    await writer.async_stop()


async def test_unreachable_database_keeps_rows(hass, tmp_path, caplog):
    writer = HistoryWriter(hass, f"sqlite:///{tmp_path}/missing/dir/history.db")
    writer.add("room_status", area_id="room_06", status="checked_in", source="manual")
    await writer.async_flush()
    assert writer.queued == 1 and not writer.connected and writer.last_error
    assert "rows are queued" in caplog.text

    writer.url = _sqlite(tmp_path)  # database back
    await writer.async_flush()
    assert writer.queued == 0 and writer.last_error is None
    assert len(_rows(writer.url, "room_status")) == 1
    await writer.async_stop()


async def test_queue_is_bounded(hass, tmp_path, monkeypatch):
    monkeypatch.setattr(history, "MAX_QUEUE", 3)
    monkeypatch.setattr(history, "BATCH_SIZE", 100)
    writer = HistoryWriter(hass, _sqlite(tmp_path))
    for i in range(5):
        writer.add("wifi_events", event="online", client_mac=f"02-00-00-00-00-0{i}")
    assert writer.queued == 3 and writer.dropped == 2
    await writer.async_flush()
    assert [r["client_mac"][-1] for r in _rows(writer.url, "wifi_events")] == ["2", "3", "4"]


async def test_full_batch_is_written_without_waiting(hass, tmp_path, monkeypatch):
    monkeypatch.setattr(history, "BATCH_SIZE", 2)
    writer = HistoryWriter(hass, _sqlite(tmp_path))
    writer.add("wifi_events", event="online", client_mac=CLIENT)
    writer.add("wifi_events", event="offline", client_mac=CLIENT)
    await hass.async_block_till_done()
    assert writer.written == 2


async def test_old_rows_are_purged(hass, tmp_path, monkeypatch):
    monkeypatch.setattr(history, "PURGE_BATCH", 2)
    url = _sqlite(tmp_path)
    writer = HistoryWriter(hass, url, retention_months=12)
    now = history.now()
    for days in (400, 400, 400, 10):
        writer.add("room_status", ts=now - timedelta(days=days), area_id="room_06",
                   status="checked_in", source="manual")
    writer.add("presence_sessions", client_mac=CLIENT, area_id="room_06", category="guest",
               started=now - timedelta(days=401), ended=now - timedelta(days=400), seconds=86400)
    await writer.async_flush()
    assert await writer.async_purge() == 4
    assert len(_rows(url, "room_status")) == 1 and _rows(url, "presence_sessions") == []
    writer.retention_months = 0  # off
    assert await writer.async_purge() == 0


def test_error_reasons():
    class Orig(Exception):
        pass

    def err(code):
        e = Exception("x")
        e.orig = Orig(code, "message")
        return e
    assert history.error_reason(err(1045)) == "auth"
    assert history.error_reason(err(1049)) == "database"
    assert history.error_reason(err(2003)) == "connect"
    assert history.error_reason(ModuleNotFoundError("pymysql")) == "connect"


# --------------------------------------------------------------------------- #
# In Home Assistant
# --------------------------------------------------------------------------- #
@pytest.fixture
def db_url(tmp_path):
    url = _sqlite(tmp_path)
    with (patch("custom_components.hotel_sense.build_url", return_value=url),
          patch.object(history, "build_url", return_value=url)):
        yield url


def _entry_with_db(make_entry, options=None) -> MockConfigEntry:
    base = make_entry(options)
    return MockConfigEntry(domain=DOMAIN, title=base.title, data={**base.data, **DB},
                           options=dict(base.options))


async def _db_hotel(hass, entry):
    return await _hotel(hass, entry)


async def test_hotel_history_is_recorded(hass, make_entry, patch_api, db_url, freezer,
                                         hass_client_no_auth):
    entry = _entry_with_db(make_entry)
    controller = await _db_hotel(hass, entry)
    writer = controller.history
    assert writer is not None and writer.retention_months == 12

    # Manual status -> room_status + room_states (violation -> checked_in).
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_06_status", "option": "checked_in"}, blocking=True)
    # Guest phone moves to Room 07: its Room 06 session ends.
    moved = [client_raw(GUEST_PHONE, "Guest-Phone", ap_mac=AP_WF07)
             if c["mac"] == GUEST_PHONE else c for c in patch_api.responses["/clients"]["data"]]
    _set_clients(patch_api, moved)
    freezer.tick(timedelta(seconds=10))
    await _poll(hass, controller)
    freezer.tick(timedelta(seconds=31))
    await _poll(hass, controller)
    assert _state(hass, "sensor.room_07_guest_devices") == "1"

    client = await hass_client_no_auth()
    await _omada_post(client, entry, {"text": [
        f'[client:Guest-Phone:{GUEST_PHONE}] is roaming from [ap:WF06:{AP_WF06}][Channel 36] '
        f'to [ap:WF07:{AP_WF07}][Channel 1] with SSID "Guest"']})
    await _exely_post(client, entry, [{"eventId": "e1", "eventType": "CheckOut",
                                       "payload": {"RoomNumber": "06", "BookingNumber": "B-1",
                                                   "PropertyId": "77"}}])
    await hass.async_block_till_done()
    await writer.async_flush()
    assert writer.written and writer.last_error is None

    status = _rows(db_url, "room_status")
    assert [(r["area_id"], r["status"], r["source"], r["booking"]) for r in status] == [
        ("room_06", "checked_in", "manual", None), ("room_06", "checked_out", "exely", "B-1")]
    states = _rows(db_url, "room_states")
    assert ("room_06", "violation", "checked_in") in [
        (r["area_id"], r["old_state"], r["state"]) for r in states]
    sessions = _rows(db_url, "presence_sessions")
    assert [(r["client_mac"], r["area_id"], r["category"]) for r in sessions] == [
        (GUEST_PHONE, "room_06", "guest")]
    assert sessions[0]["seconds"] >= 41
    wifi = _rows(db_url, "wifi_events")
    assert [(r["event"], r["client_mac"], r["from_ap_mac"], r["ap_mac"]) for r in wifi] == [
        ("roaming", GUEST_PHONE, AP_WF06, AP_WF07)]
    pms = _rows(db_url, "pms_events")
    assert [(r["event_id"], r["booking"], r["property_id"], r["result"], r["rooms"])
            for r in pms] == [("e1", "B-1", "77", "applied", "Room 06")]

    # Unload: open sessions (phone in Room 07) are written, nothing stays queued.
    assert await hass.config_entries.async_unload(entry.entry_id)
    sessions = _rows(db_url, "presence_sessions")
    assert (GUEST_PHONE, "room_07") in [(r["client_mac"], r["area_id"]) for r in sessions]
    assert writer.queued == 0


async def test_no_history_without_database(hass, make_entry, patch_api):
    controller = await _db_hotel(hass, make_entry())
    assert controller.history is None
    diag = await async_get_config_entry_diagnostics(hass, controller.entry)
    assert diag["history"] is None


async def test_diagnostics_redact_database_login(hass, make_entry, patch_api, db_url):
    entry = _entry_with_db(make_entry)
    await _db_hotel(hass, entry)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"][CONF_DB_PASSWORD] == "**REDACTED**"
    assert diag["entry"][CONF_DB_USERNAME] == "**REDACTED**"
    assert diag["history"]["retention_months"] == 12


async def test_home_assistant_stop_writes_open_sessions(hass, make_entry, patch_api, db_url):
    entry = _entry_with_db(make_entry)
    await _db_hotel(hass, entry)
    hass.bus.async_fire("homeassistant_stop")
    await hass.async_block_till_done()
    assert GUEST_PHONE in [r["client_mac"] for r in _rows(db_url, "presence_sessions")]


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #
async def _db_step(hass, entry, user_input):
    result = await _options_menu(hass, entry, "database")
    assert result["step_id"] == "database"
    return await hass.config_entries.options.async_configure(result["flow_id"], user_input)


async def test_options_set_up_the_database(hass, make_entry, patch_api, db_url):
    entry = make_entry()
    await _db_hotel(hass, entry)
    result = await _db_step(hass, entry, {
        CONF_DB_HOST: "core-mariadb", CONF_DB_PORT: 3306, CONF_DB_USERNAME: "hotel_sense",
        CONF_DB_PASSWORD: "pw", CONF_DB_NAME: "hotel_sense", CONF_DB_RETENTION: 6})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert {k: entry.data[k] for k in DB} == DB
    assert entry.options[CONF_DB_RETENTION] == 6
    controller = hass.data[DOMAIN][entry.entry_id]  # reloaded with the database
    assert controller.history is not None and controller.history.retention_months == 6
    engine = create_engine(db_url)  # tables created by the check
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(history.schema_version)).scalar() == 1
    engine.dispose()

    # Empty password keeps the saved one; retention alone needs no reload.
    result = await _db_step(hass, entry, {**DB, CONF_DB_PASSWORD: "", CONF_DB_RETENTION: 24})
    assert result["type"] == "create_entry" and entry.data[CONF_DB_PASSWORD] == "pw"
    await hass.async_block_till_done()
    assert hass.data[DOMAIN][entry.entry_id].history.retention_months == 24

    # Empty user: history off.
    result = await _db_step(hass, entry, {CONF_DB_USERNAME: ""})
    await hass.async_block_till_done()
    assert CONF_DB_USERNAME not in entry.data and CONF_DB_PASSWORD not in entry.data
    assert hass.data[DOMAIN][entry.entry_id].history is None


@pytest.mark.parametrize(("code", "error"), [
    (1045, "db_auth"), (1049, "db_database"), (2003, "db_connect")])
async def test_options_show_database_errors(hass, make_entry, patch_api, code, error):
    class Orig(Exception):
        pass

    failure = Exception("failed")
    failure.orig = Orig(code, "message")
    entry = make_entry()
    await _db_hotel(hass, entry)
    with patch.object(history, "check_connection", side_effect=failure):
        result = await _db_step(hass, entry, {**DB})
    assert result["type"] == "form" and result["errors"] == {"base": error}
    assert CONF_DB_USERNAME not in entry.data


async def test_cleared_user_switches_the_history_off(hass, make_entry, patch_api, db_url):
    entry = _entry_with_db(make_entry)
    await _db_hotel(hass, entry)
    result = await _db_step(hass, entry, {})  # every field cleared
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert CONF_DB_USERNAME not in entry.data and CONF_DB_PASSWORD not in entry.data
    assert hass.data[DOMAIN][entry.entry_id].history is None
