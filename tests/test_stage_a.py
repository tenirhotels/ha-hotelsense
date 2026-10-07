"""Stage A acceptance: room presence on a real Home Assistant (no database).

Topology of the fake controller (see fakes.py): WF06 and WF07 access points,
GUEST_PHONE and SHARED_MAC are Wi-Fi clients on WF06, WIRED_PC is wired.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import mock_restore_cache

from tplink_omada_client.exceptions import ConnectionFailed
from custom_components.hotel_sense.const import (
    CONF_COMMON_AREAS, CONF_MIN_RSSI, CONF_PRESENCE_TIMEOUT, CONF_ROAMING_DEBOUNCE, DOMAIN,
    EVENT_ROOM_STATE_CHANGED, STORAGE_KEY_DEVICES,
)
from custom_components.hotel_sense.ids import make_room_unique_id

from .fakes import AP_WF06, AP_WF07, GUEST_PHONE, SHARED_MAC, SITE_ID, WIRED_PC, client_raw

ROOMS = {AP_WF06: "Room 06", AP_WF07: "Room 07"}


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return hass.data[DOMAIN][entry.entry_id]


async def _assign(hass, mapping, **extra):
    resp = await hass.services.async_call(
        DOMAIN, "assign_ap_areas", {"mapping": mapping, **extra},
        blocking=True, return_response=True)
    await hass.async_block_till_done()
    return resp


async def _hotel(hass, entry, mapping=ROOMS):
    """Set up with Areas created and APs assigned (what the owner does once)."""
    controller = await _setup(hass, entry)
    areas = ar.async_get(hass)
    for name in set(mapping.values()):
        if not areas.async_get_area_by_name(name):
            areas.async_create(name)
    await _assign(hass, mapping)
    return controller


async def _poll(hass, controller):
    await controller.async_update()
    await hass.async_block_till_done()


def _state(hass, entity_id):
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


async def _import(hass, csv_text, **extra):
    resp = await hass.services.async_call(
        DOMAIN, "import_devices", {"csv": csv_text, **extra}, blocking=True, return_response=True)
    await hass.async_block_till_done()
    return resp


def _set_clients(api, clients):
    api.set_data(api.responses["/devices"], clients, api.responses["/insight/clients"]["data"])


# --------------------------------------------------------------------------- #
# AP -> Area
# --------------------------------------------------------------------------- #
async def test_ap_area_report_flags_missing_and_mismatched_areas(hass, make_entry, patch_api):
    await _setup(hass, make_entry())
    ar.async_get(hass).async_create("Room 06")
    report = await hass.services.async_call(
        DOMAIN, "ap_area_report",
        {"csv": f"mac,area\n{AP_WF06.lower().replace('-', ':')},Room 06\n"
                f"{AP_WF07},Room 07\nAA-AA-AA-00-00-99,Room 09\nbroken,Room 01\n"},
        blocking=True, return_response=True)
    rows = {r["mac"]: r for r in report["access_points"]}
    assert rows[AP_WF06]["status"] == "no_area" and rows[AP_WF06]["name"] == "WF06"
    assert rows["AA-AA-AA-00-00-99"]["status"] == "not_found"
    assert report["problems"] == 3
    assert report["input_errors"] and "broken" in report["input_errors"][0]

    await _assign(hass, {AP_WF06: "Room 06"})
    await _assign(hass, {AP_WF07: "Room 07"}, create_missing_areas=True)
    report = await hass.services.async_call(
        DOMAIN, "ap_area_report", {"mapping": {AP_WF06: "Room 06", AP_WF07: "Room 01"}},
        blocking=True, return_response=True)
    rows = {r["mac"]: r for r in report["access_points"]}
    assert rows[AP_WF06]["status"] == "ok" and rows[AP_WF06]["area"] == "Room 06"
    assert rows[AP_WF07]["status"] == "mismatch" and rows[AP_WF07]["area"] == "Room 07"


async def test_assign_ap_areas_matches_by_mac_not_name(hass, make_entry, patch_api):
    entry = make_entry()
    await _setup(hass, entry)
    room = ar.async_get(hass).async_create("Room 06")
    resp = await _assign(hass, {AP_WF06.lower().replace("-", ":"): "room 06",
                                "AA-AA-AA-00-00-99": "Room 06", GUEST_PHONE: "Room 06"})
    assert [a["mac"] for a in resp["assigned"]] == [AP_WF06]
    assert len(resp["errors"]) == 2  # unknown MAC, client (not an AP)
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"ap:{SITE_ID}:{AP_WF06}"), entry.entry_id)
    assert device.area_id == room.id
    # Missing area is an error unless creation is requested.
    resp = await _assign(hass, {AP_WF07: "Room 07"})
    assert resp["errors"] and not resp["assigned"]
    resp = await _assign(hass, {AP_WF06: "Room 06"})
    assert resp["unchanged"] == [AP_WF06]


# --------------------------------------------------------------------------- #
# Room entities and rules
# --------------------------------------------------------------------------- #
async def test_room_entities_are_created_with_predictable_ids(hass, make_entry, patch_api):
    entry = make_entry()
    await _hotel(hass, entry)
    for room in ("room_06", "room_07"):
        for entity_id in (f"binary_sensor.{room}_guest_presence",
                          f"binary_sensor.{room}_employee_presence",
                          f"binary_sensor.{room}_violation",
                          f"sensor.{room}_guest_devices", f"sensor.{room}_employee_devices",
                          f"sensor.{room}_fixed_devices", f"sensor.{room}_state",
                          f"select.{room}_status"):
            assert hass.states.get(entity_id) is not None, entity_id
    reg = er.async_get(hass)
    area = ar.async_get(hass).async_get_area_by_name("Room 06")
    assert reg.async_get("sensor.room_06_state").unique_id == make_room_unique_id(
        SITE_ID, area.id, "room_state")
    # Room device sits in its Area.
    dev = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, make_room_unique_id(SITE_ID, area.id)), entry.entry_id)
    assert dev.area_id == area.id and dev.name == "Room 06"


async def test_checked_out_room_with_guest_is_a_violation(hass, make_entry, patch_api):
    await _hotel(hass, make_entry())
    assert _state(hass, "select.room_06_status") == "checked_out"   # default
    assert _state(hass, "sensor.room_06_guest_devices") == "2"   # phone + SHARED_MAC client
    assert _state(hass, "binary_sensor.room_06_guest_presence") == "on"
    assert _state(hass, "sensor.room_06_state") == "violation"
    assert _state(hass, "binary_sensor.room_06_violation") == "on"
    # Empty neighbour is fine.
    assert _state(hass, "sensor.room_07_state") == "empty"
    assert _state(hass, "binary_sensor.room_07_violation") == "off"
    attrs = hass.states.get("sensor.room_06_guest_devices").attributes
    assert {d["mac"] for d in attrs["devices"]} == {GUEST_PHONE, SHARED_MAC}
    assert {d["name"] for d in attrs["devices"]} == {"Guest-Phone", "wf07-as-client"}
    # Both fake MACs (02-..., AA-...) have the locally administered bit set.
    assert attrs["random_macs"] == 2


async def test_room_status_changes_the_verdict(hass, make_entry, patch_api):
    await _hotel(hass, make_entry())
    for status, expected, violation in (("checked_in", "checked_in", "off"),
                                        ("checked_out", "violation", "on")):
        await hass.services.async_call("select", "select_option", {
            "entity_id": "select.room_06_status", "option": status}, blocking=True)
        await hass.async_block_till_done()
        assert _state(hass, "select.room_06_status") == status
        assert _state(hass, "sensor.room_06_state") == expected
        assert _state(hass, "binary_sensor.room_06_violation") == violation


async def test_fixed_and_employee_devices_are_classified(hass, make_entry, patch_api, hass_storage):
    controller = await _hotel(hass, make_entry())
    resp = await _import(hass, f"mac,name,category\n{GUEST_PHONE.lower()},AC06,fixed\n")
    assert resp["added"] == 1
    assert hass_storage[STORAGE_KEY_DEVICES]["data"]["devices"][0]["mac"] == GUEST_PHONE
    # Applied immediately, without waiting for the next poll.
    assert _state(hass, "sensor.room_06_fixed_devices") == "1"
    assert _state(hass, "sensor.room_06_state") == "violation"   # SHARED_MAC still a guest

    await _import(hass, f"{SHARED_MAC},Maid phone,employee\n")
    assert _state(hass, "sensor.room_06_guest_devices") == "0"
    assert _state(hass, "sensor.room_06_employee_devices") == "1"
    assert _state(hass, "binary_sensor.room_06_employee_presence") == "on"
    assert _state(hass, "sensor.room_06_state") == "staff_visit"   # logged, not a violation
    assert _state(hass, "binary_sensor.room_06_violation") == "off"
    names = [d["name"] for d in hass.states.get("sensor.room_06_employee_devices").attributes["devices"]]
    assert names == ["Maid phone"]

    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_06_status", "option": "checked_in"}, blocking=True)
    await hass.async_block_till_done()
    assert _state(hass, "sensor.room_06_state") == "checked_in"

    # Only fixed equipment left in a checked-out room: normal.
    await _import(hass, f"mac,category\n{SHARED_MAC},fixed\n")
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_06_status", "option": "checked_out"}, blocking=True)
    await _poll(hass, controller)
    assert _state(hass, "sensor.room_06_state") == "empty"

    export = await hass.services.async_call(
        DOMAIN, "export_devices", {}, blocking=True, return_response=True)
    assert export["csv"].startswith("mac,name,category,owner,note,room,device_type\n")
    assert len(export["devices"]) == 2


async def test_wired_clients_never_count(hass, make_entry, patch_api):
    controller = await _hotel(hass, make_entry())
    clients = [c for c in patch_api.responses["/clients"]["data"] if c["mac"] != WIRED_PC]
    wired = client_raw(WIRED_PC, "office-pc", wireless=False, connect_type=2)
    wired["apMac"] = AP_WF07  # even if the controller reports an uplink AP
    _set_clients(patch_api, clients + [wired])
    await _poll(hass, controller)
    assert _state(hass, "sensor.room_07_guest_devices") == "0"


async def test_guest_leaving_clears_after_disconnect_timeout(hass, make_entry, patch_api, freezer):
    controller = await _hotel(hass, make_entry())
    await _import(hass, f"{SHARED_MAC},AC,fixed\n")
    assert _state(hass, "sensor.room_06_state") == "violation"

    _set_clients(patch_api, [c for c in patch_api.responses["/clients"]["data"]
                             if c["mac"] != GUEST_PHONE])
    freezer.tick(timedelta(minutes=4))
    await _poll(hass, controller)
    assert _state(hass, "sensor.room_06_state") == "violation"   # phone may just be asleep
    freezer.tick(timedelta(minutes=1, seconds=1))
    await _poll(hass, controller)
    assert _state(hass, "sensor.room_06_state") == "empty"
    assert _state(hass, "binary_sensor.room_06_guest_presence") == "off"


async def test_roaming_is_debounced(hass, make_entry, patch_api, freezer):
    controller = await _hotel(hass, make_entry())
    moved = [client_raw(GUEST_PHONE, "Guest-Phone", ap_mac=AP_WF07)
             if c["mac"] == GUEST_PHONE else c for c in patch_api.responses["/clients"]["data"]]
    _set_clients(patch_api, moved)
    freezer.tick(timedelta(seconds=10))
    await _poll(hass, controller)
    assert _state(hass, "sensor.room_07_guest_devices") == "0"
    freezer.tick(timedelta(seconds=30))
    await _poll(hass, controller)
    assert _state(hass, "sensor.room_07_guest_devices") == "1"
    assert _state(hass, "sensor.room_06_guest_devices") == "1"


async def test_presence_options_are_applied(hass, make_entry, patch_api, freezer):
    controller = await _hotel(hass, make_entry({CONF_PRESENCE_TIMEOUT: 1,
                                                CONF_ROAMING_DEBOUNCE: 0,
                                                CONF_MIN_RSSI: -60}))
    engine = controller.presence.engine
    assert (engine.timeout, engine.debounce, engine.min_rssi) == (60, 0, -60)
    weak = [client_raw(GUEST_PHONE, "Guest-Phone", ap_mac=AP_WF07) | {"rssi": -80}
            if c["mac"] == GUEST_PHONE else c for c in patch_api.responses["/clients"]["data"]]
    _set_clients(patch_api, weak)
    await _poll(hass, controller)
    assert _state(hass, "sensor.room_07_guest_devices") == "0"   # weak: stays in Room 06


async def test_controller_outage_keeps_last_picture(hass, make_entry, patch_api, freezer):
    """ТЗ §22: no mass 'empty' when the controller is unreachable."""
    controller = await _hotel(hass, make_entry())
    patch_api.raise_on_status = [ConnectionFailed("down")]
    freezer.tick(timedelta(minutes=30))
    await _poll(hass, controller)
    assert controller.available is False
    assert _state(hass, "sensor.room_06_state") == "violation"
    assert hass.states.get("sensor.room_06_state").attributes["data_stale"] is True
    await _poll(hass, controller)  # recovered, same clients still there
    assert hass.states.get("sensor.room_06_state").attributes["data_stale"] is False
    assert _state(hass, "sensor.room_06_state") == "violation"


async def test_room_state_change_fires_event(hass, make_entry, patch_api):
    await _hotel(hass, make_entry())
    events = []
    hass.bus.async_listen(EVENT_ROOM_STATE_CHANGED, events.append)
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_06_status", "option": "checked_in"}, blocking=True)
    await hass.async_block_till_done()
    assert len(events) == 1
    data = events[0].data
    assert (data["room"], data["old_state"], data["new_state"]) == ("Room 06", "violation", "checked_in")


async def test_room_status_is_restored_after_restart(hass, make_entry, patch_api):
    mock_restore_cache(hass, [State("select.room_06_status", "checked_in")])
    await _hotel(hass, make_entry())
    assert _state(hass, "select.room_06_status") == "checked_in"
    assert _state(hass, "sensor.room_06_state") == "checked_in"


@pytest.mark.parametrize(("saved", "expected"), [
    ("sold", "checked_in"), ("vacant", "checked_out"), ("cleaning", "checked_out"),
    ("garbage", "checked_out"),
])
async def test_statuses_saved_by_0_2_0_are_migrated(hass, make_entry, patch_api, saved, expected):
    mock_restore_cache(hass, [State("select.room_06_status", saved)])
    await _hotel(hass, make_entry())
    assert _state(hass, "select.room_06_status") == expected


async def test_common_area_has_presence_but_no_status_or_violation(hass, make_entry, patch_api):
    await _hotel(hass, make_entry(), {AP_WF06: "Admin House", AP_WF07: "Room 07"})
    assert _state(hass, "binary_sensor.admin_house_guest_presence") == "on"
    assert _state(hass, "sensor.admin_house_guest_devices") == "2"
    for entity_id in ("select.admin_house_status", "sensor.admin_house_state",
                      "binary_sensor.admin_house_violation"):
        assert hass.states.get(entity_id) is None, entity_id
    assert hass.states.get("select.room_07_status") is not None


async def test_common_areas_option_overrides_name_heuristic(hass, make_entry, patch_api):
    areas = ar.async_get(hass)
    lobby = areas.async_create("Room 06")  # named like a room, configured as common
    await _hotel(hass, make_entry({CONF_COMMON_AREAS: [lobby.id]}))
    assert hass.states.get("select.room_06_status") is None
    assert hass.states.get("select.room_07_status") is not None


# --------------------------------------------------------------------------- #
# Options flow
# --------------------------------------------------------------------------- #
async def _options_menu(hass, entry, *steps):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    for step in steps:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": step})
    return result


async def test_options_presence_step(hass, make_entry, patch_api):
    entry = make_entry()
    await _hotel(hass, entry, {AP_WF06: "Admin House", AP_WF07: "Room 07"})
    result = await _options_menu(hass, entry, "presence")
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "presence"
    admin = ar.async_get(hass).async_get_area_by_name("Admin House")
    schema = {str(k): k for k in result["data_schema"].schema}
    assert schema[CONF_COMMON_AREAS].default() == [admin.id]  # name heuristic pre-selected

    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        CONF_PRESENCE_TIMEOUT: 10, CONF_ROAMING_DEBOUNCE: 45, CONF_MIN_RSSI: -70,
        CONF_COMMON_AREAS: [admin.id]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_PRESENCE_TIMEOUT] == 10
    engine = hass.data[DOMAIN][entry.entry_id].presence.engine
    assert (engine.timeout, engine.debounce, engine.min_rssi) == (600, 45, -70)


async def test_options_device_list_add_edit_delete(hass, make_entry, patch_api, hass_storage):
    entry = make_entry()
    await _hotel(hass, entry)
    flow = await _options_menu(hass, entry, "device_list", "device_add")
    assert flow["step_id"] == "device_add"

    result = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "mac": "not a mac", "category": "fixed"})
    assert result["errors"] == {"mac": "invalid_mac"}
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "mac": GUEST_PHONE.lower().replace("-", ":"), "name": "AC06", "category": "fixed"})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "device_list"
    assert _state(hass, "sensor.room_06_fixed_devices") == "1"

    # duplicate
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "device_add"})
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "mac": GUEST_PHONE, "category": "employee"})
    assert result["errors"] == {"mac": "duplicate_mac"}

    # edit: change category and MAC
    flow = await _options_menu(hass, entry, "device_list", "device_edit_select")
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {"mac": GUEST_PHONE})
    assert result["step_id"] == "device_edit"
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "mac": SHARED_MAC, "name": "Maid", "category": "employee", "owner": "Maid 1"})
    assert result["step_id"] == "device_list"
    devices = hass_storage[STORAGE_KEY_DEVICES]["data"]["devices"]
    assert [(d["mac"], d["category"], d["owner"]) for d in devices] == [
        (SHARED_MAC, "employee", "Maid 1")]

    # delete
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "device_delete"})
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"devices": [SHARED_MAC]})
    assert result["step_id"] == "device_list"
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert hass_storage[STORAGE_KEY_DEVICES]["data"]["devices"] == []


async def test_options_device_import_and_export(hass, make_entry, patch_api):
    entry = make_entry()
    await _hotel(hass, entry)
    flow = await _options_menu(hass, entry, "device_list", "device_import")
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "csv": f"mac;name\n{GUEST_PHONE};AC06\nbad;x\n", "default_category": "fixed"})
    assert result["errors"] == {"base": "import_errors"}
    assert "added 1" in result["description_placeholders"]["result"]
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "csv": f"mac;name\n{SHARED_MAC};HF06\n", "default_category": "fixed"})
    assert result["step_id"] == "device_list"

    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "device_export"})
    exported = {str(k): k for k in result["data_schema"].schema}["csv"].default()
    assert GUEST_PHONE in exported and SHARED_MAC in exported


async def test_edit_and_delete_abort_on_empty_list(hass, make_entry, patch_api):
    entry = make_entry()
    await _hotel(hass, entry)
    for step in ("device_edit_select", "device_delete"):
        result = await _options_menu(hass, entry, "device_list", step)
        assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_devices"


async def test_existing_options_chain_still_reachable(hass, make_entry, patch_api):
    entry = make_entry()
    await _hotel(hass, entry)
    result = await _options_menu(hass, entry, "device_tracker")
    assert result["step_id"] == "device_tracker"


async def test_unloading_removes_listeners(hass, make_entry, patch_api):
    entry = make_entry()
    controller = await _hotel(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # Store changes after unload must not touch the unloaded manager.
    await _import(hass, f"{GUEST_PHONE},AC,fixed\n")
    assert controller.presence.rooms  # left as is, no exception


async def test_no_warnings_during_room_setup(hass, make_entry, patch_api, caplog):
    await _hotel(hass, make_entry())
    noisy = [r.getMessage() for r in caplog.records
             if r.levelname in ("WARNING", "ERROR")
             and r.name.startswith(("homeassistant", "custom_components"))
             and "has not been tested by Home Assistant" not in r.getMessage()]
    assert noisy == []


async def test_misplaced_fixed_devices_double_check(hass, make_entry, patch_api):
    """Fixed equipment with a `room` seen on another room's AP is reported."""
    await _hotel(hass, make_entry())
    sensor = "sensor.hotel_sense_misplaced_devices"
    await _import(hass, "mac,name,category,room\n"
                        f"{GUEST_PHONE},AC06,fixed,Room 06\n"
                        f"{SHARED_MAC},HF06,fixed,room_06\n"     # area id works too
                        f"{WIRED_PC},AC01,fixed,Room 01\n")      # not on Wi-Fi now: skipped
    assert _state(hass, sensor) == "0"

    await _import(hass, f"mac,name,category,room\n{GUEST_PHONE},AC07,fixed,Room 07\n"
                        f"{SHARED_MAC},HF06,fixed,Room 99\n")
    assert _state(hass, sensor) == "2"
    devices = {d["name"]: d for d in hass.states.get(sensor).attributes["devices"]}
    assert devices["AC07"] | {} == {"name": "AC07", "mac": GUEST_PHONE, "expected": "Room 07",
                                    "seen": "Room 06", "reason": "wrong_room"}
    assert devices["HF06"]["reason"] == "unknown_room"

    # Employees and fixed devices without a room are never "misplaced".
    await _import(hass, f"mac,name,category,room\n{GUEST_PHONE},Maid,employee,Room 07\n"
                        f"{SHARED_MAC},HF06,fixed,\n")
    assert _state(hass, sensor) == "0"


async def test_devices_show_access_point_ssid_and_signal(hass, make_entry, patch_api, freezer):
    controller = await _hotel(hass, make_entry())
    on_wf07 = client_raw(GUEST_PHONE, "Guest-Phone", ap_mac=AP_WF07, ssid="Guest-07") | {"rssi": -48}
    _set_clients(patch_api, [on_wf07, client_raw(SHARED_MAC, "wf07-as-client")])
    await _poll(hass, controller)            # roaming to WF07 starts
    freezer.tick(timedelta(seconds=31))      # past the 30 s roaming debounce
    await _poll(hass, controller)
    devices = {d["mac"]: d for d in hass.states.get("sensor.room_07_guest_devices").attributes["devices"]}
    phone = devices[GUEST_PHONE]
    assert (phone["ap"], phone["ssid"], phone["rssi"], phone["connected"]) == ("WF07", "Guest-07", -48, True)
    assert phone["last_seen"].endswith("+00:00")

    # Gone from Wi-Fi: kept in the room for the timeout, shown as not connected
    # with the last known access point / SSID.
    _set_clients(patch_api, [client_raw(SHARED_MAC, "wf07-as-client")])
    freezer.tick(timedelta(minutes=1))
    await _poll(hass, controller)
    phone = {d["mac"]: d for d in
             hass.states.get("sensor.room_07_guest_devices").attributes["devices"]}[GUEST_PHONE]
    assert (phone["connected"], phone["ap"], phone["ssid"]) == (False, "WF07", "Guest-07")
