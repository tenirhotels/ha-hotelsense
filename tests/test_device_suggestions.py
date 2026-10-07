"""Device identity suggestions end to end: database, Omada, sensor, form, actions."""
from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from sqlalchemy import create_engine, insert

from custom_components.hotel_sense import history
from custom_components.hotel_sense.const import DOMAIN, STORAGE_KEY_DEVICES
from custom_components.hotel_sense.queries import suggestion_data

from .fakes import GUEST_PHONE
from .test_history import _db_hotel, _entry_with_db, _sqlite, db_url  # noqa: F401
from .test_stage_a import _hotel, _options_menu, _state

OLD_MAC = "02-00-00-00-00-91"  # the staff phone's previous private MAC
SENSOR = "sensor.omada_oc200_device_suggestions"


def _session(mac, area, started, hours):
    return {"client_mac": mac, "area_id": area, "category": "guest", "started": started,
            "ended": started + timedelta(hours=hours), "seconds": int(hours * 3600)}


def test_suggestion_data_picks_new_unknown_random_macs(tmp_path):
    url = _sqlite(tmp_path)
    history.check_connection(url)
    engine = create_engine(url)
    now = history.now()
    week_ago, month_ago = now - timedelta(days=7), now - timedelta(days=30)
    new, old, known, real, brief, online = (
        "02-00-00-00-00-a1", "02-00-00-00-00-a2", "02-00-00-00-00-a3",
        "00-11-22-00-00-a4", "02-00-00-00-00-a5", "02-00-00-00-00-a6")
    new, old, known, brief, online = (m.upper() for m in (new, old, known, brief, online))
    with engine.begin() as conn:
        conn.execute(insert(history.presence_sessions), [
            _session(new, "lobby", now - timedelta(days=2), 2),
            _session(old, "lobby", now - timedelta(days=20), 2),  # seen before: not new
            _session(old, "lobby", now - timedelta(days=1), 2),
            _session(known, "lobby", now - timedelta(days=1), 2),
            _session(real, "lobby", now - timedelta(days=1), 2),  # a real (not random) MAC
            _session(brief, "lobby", now - timedelta(days=1), 0.2),  # 12 minutes
            _session(OLD_MAC, "office", now - timedelta(days=4), 8)])
        conn.execute(insert(history.wifi_events), [
            {"ts": now - timedelta(days=2), "event": "connected", "client_mac": new,
             "ssid": "Staff"}])
    with engine.connect() as conn:
        data = suggestion_data(conn, month_ago, week_ago, known={known}, device_macs=[OLD_MAC],
                               open_macs={online})
    assert data["candidates"] == sorted([new, online])
    assert set(data["sessions"]) == {new, OLD_MAC}
    assert data["sessions"][OLD_MAC][0][0] == "office"
    assert data["ssids"] == {new: {"Staff"}}


async def _staff_phone_changed_mac(hass, db_url):  # noqa: F811
    """Device #1 "Guest-Phone" used OLD_MAC until two days ago; GUEST_PHONE is on now."""
    await hass.services.async_call(DOMAIN, "import_devices", {
        "csv": f"mac,name,category\n{OLD_MAC},Guest-Phone,employee\n"},
        blocking=True, return_response=True)
    now = history.now()
    history.check_connection(db_url)  # tables (the writer creates them on its first flush)
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(insert(history.presence_sessions), [
            _session(OLD_MAC, "room_06", now - timedelta(days=d, hours=6), 3) for d in (2, 3, 4)])
    engine.dispose()


async def test_suggestion_link_and_ignore(hass, make_entry, patch_api, db_url,  # noqa: F811
                                          hass_storage):
    entry = _entry_with_db(make_entry)
    controller = await _db_hotel(hass, entry)
    await _staff_phone_changed_mac(hass, db_url)

    result = await hass.services.async_call(DOMAIN, "device_suggestions", {}, blocking=True,
                                            return_response=True)
    assert result["status"] == "ok"
    [found] = result["suggestions"]
    assert (found["mac"], found["identity"], found["identity_name"]) == (
        GUEST_PHONE, "1", "Guest-Phone")
    checks = {c["check"]: c["ok"] for c in found["checks"]}
    assert checks["name"] is True and checks["handoff"] is True
    assert checks["hours"] is None  # minutes on the Wi-Fi so far: not judged yet
    assert found["score"] == 60
    await hass.async_block_till_done()
    assert _state(hass, SENSOR) == "1"
    assert hass.states.get(SENSOR).attributes["suggestions"][0]["identity"] == "1"

    # The form: link it.
    flow = await _options_menu(hass, entry, "device_list", "device_suggestions")
    assert flow["step_id"] == "device_suggestions"
    text = flow["description_placeholders"]["suggestions"]
    assert f"**{GUEST_PHONE}**" in text and "→ #1 Guest-Phone" in text and "60%" in text
    assert "? same working hours: not enough data" in text
    result = await hass.config_entries.options.async_configure(flow["flow_id"],
                                                               {GUEST_PHONE: "link"})
    assert result["step_id"] == "device_list"
    identities = hass_storage[STORAGE_KEY_DEVICES]["data"]["identities"]
    assert identities[0]["macs"] == [OLD_MAC, GUEST_PHONE]
    await hass.async_block_till_done()
    assert _state(hass, SENSOR) == "0"
    assert _state(hass, "sensor.room_06_employee_devices") == "1"
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # Unlinked again, then ignored: never suggested again (kept across restarts).
    await hass.services.async_call(DOMAIN, "unlink_mac", {"mac": GUEST_PHONE}, blocking=True,
                                   return_response=True)
    result = await hass.services.async_call(DOMAIN, "device_suggestions", {}, blocking=True,
                                            return_response=True)
    assert [s["mac"] for s in result["suggestions"]] == [GUEST_PHONE]
    await hass.services.async_call(DOMAIN, "ignore_device_suggestion", {
        "mac": GUEST_PHONE.lower(), "identity": "#1"}, blocking=True)
    assert controller.suggestions.suggestions == []
    assert hass_storage[f"{DOMAIN}.suggestions.{entry.entry_id}"]["data"] == {
        "ignored": [[GUEST_PHONE, "1"]]}
    result = await hass.services.async_call(DOMAIN, "device_suggestions", {}, blocking=True,
                                            return_response=True)
    assert result["suggestions"] == []
    flow = await _options_menu(hass, entry, "device_list", "device_suggestions")
    assert flow["type"] is FlowResultType.ABORT and flow["reason"] == "no_suggestions"


async def test_the_old_mac_still_online_rules_it_out(hass, make_entry, patch_api,
                                                     db_url):  # noqa: F811
    controller = await _db_hotel(hass, _entry_with_db(make_entry))
    await _staff_phone_changed_mac(hass, db_url)
    controller.presence.sessions[OLD_MAC] = ("room_07", history.now() - timedelta(minutes=5))
    result = await hass.services.async_call(DOMAIN, "device_suggestions", {}, blocking=True,
                                            return_response=True)
    assert result["suggestions"] == []


async def test_suggestions_need_the_history_database(hass, make_entry, patch_api):
    entry = make_entry()
    await _hotel(hass, entry)
    with pytest.raises(ServiceValidationError, match="history database"):
        await hass.services.async_call(DOMAIN, "device_suggestions", {}, blocking=True,
                                       return_response=True)
    flow = await _options_menu(hass, entry, "device_list", "device_suggestions")
    assert flow["type"] is FlowResultType.ABORT
    assert flow["reason"] == "suggestions_need_history"
    assert hass.states.get(SENSOR).attributes["status"] == "no_history"
