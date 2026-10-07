"""Device identities: one physical device, one or more MACs (MAC -> identity)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.exceptions import ServiceValidationError
from sqlalchemy import create_engine, insert, text

from custom_components.hotel_sense import history
from custom_components.hotel_sense.const import DOMAIN, STORAGE_KEY_DEVICES
from custom_components.hotel_sense.device_list import (
    CATEGORY_EMPLOYEE, CATEGORY_FIXED, DeviceList, KnownDevice,
)
from custom_components.hotel_sense.queries import device_route

from .fakes import GUEST_PHONE
from .test_history import _db_hotel, _entry_with_db, _sqlite, db_url  # noqa: F401
from .test_reports import T0
from .test_stage_a import _hotel, _options_menu, _state

PHONE_OLD, PHONE_NEW, AC = "02-00-00-00-00-71", "02-00-00-00-00-72", "00-11-22-00-00-73"
OLD_FIELDS = {"mac", "name", "category", "owner", "note", "room", "device_type"}


def test_legacy_storage_becomes_one_identity_per_mac():
    devices = DeviceList.from_storage({"devices": [
        {"mac": AC, "category": "fixed", "name": "AC06"},
        {"mac": PHONE_OLD, "category": "employee", "name": "Maid phone", "owner": "Maid 1"}]})
    # Numbered in order, one series for every category.
    assert [(i.id, i.macs) for i in devices.identities()] == [("1", [AC]), ("2", [PHONE_OLD])]
    assert devices.get(PHONE_OLD).identity == "2"
    data = devices.to_storage()
    # The flat rows of 0.11 stay, without the new field: a downgrade keeps the list.
    assert {frozenset(r) for r in data["devices"]} == {frozenset(OLD_FIELDS)}
    assert DeviceList.from_storage(data).to_storage() == data


def test_one_device_several_macs():
    devices = DeviceList([KnownDevice(PHONE_OLD, CATEGORY_EMPLOYEE, "Maid phone")])
    ident = devices.identity_of(PHONE_OLD).id
    assert ident == "1"
    assert devices.link_mac("#1", PHONE_NEW.lower()) is None
    identity = devices.identity(ident)
    assert identity.macs == [PHONE_OLD, PHONE_NEW] and len(devices) == 2
    assert devices.category_of(PHONE_NEW) == CATEGORY_EMPLOYEE
    assert devices.get(PHONE_NEW).name == "Maid phone"
    # Renaming through one MAC renames the device.
    devices.upsert(KnownDevice(PHONE_NEW, CATEGORY_EMPLOYEE, "Housekeeping 1"))
    assert devices.get(PHONE_OLD).name == "Housekeeping 1"
    # The old MAC goes; the device stays with the new one.
    assert devices.remove(PHONE_OLD) and devices.identity(ident).macs == [PHONE_NEW]
    assert devices.remove(PHONE_NEW) and devices.identity(ident) is None
    with pytest.raises(KeyError):
        devices.link_mac("9999", PHONE_OLD)
    with pytest.raises(KeyError):
        devices.link_mac("EMP-1", PHONE_OLD)  # numbers only


def test_link_moves_a_mac_and_ids_are_never_reused():
    devices = DeviceList([KnownDevice(PHONE_OLD, CATEGORY_EMPLOYEE, "A"),
                          KnownDevice(PHONE_NEW, CATEGORY_EMPLOYEE, "B")])
    assert devices.link_mac("1", PHONE_NEW) == "2"
    assert devices.identity("2") is None  # left without MACs
    devices.upsert(KnownDevice(AC, CATEGORY_FIXED))
    assert devices.get(AC).identity == "3"  # not 2 again
    reloaded = DeviceList.from_storage(devices.to_storage())
    reloaded.remove(AC)
    reloaded.upsert(KnownDevice(PHONE_NEW, CATEGORY_EMPLOYEE, "C", identity=""))
    assert reloaded.get(PHONE_NEW).identity == "1"  # still linked
    reloaded.upsert(KnownDevice("02-00-00-00-00-74", CATEGORY_EMPLOYEE))
    assert reloaded.get("02-00-00-00-00-74").identity == "4"
    # A corrected category keeps the number.
    reloaded.upsert(KnownDevice("02-00-00-00-00-74", CATEGORY_FIXED))
    assert reloaded.get("02-00-00-00-00-74").identity == "4"


def test_csv_rows_with_one_identity_are_one_device():
    devices = DeviceList()
    result = devices.import_csv(
        "mac,name,category,owner,identity\n"
        f"{PHONE_OLD},Maid phone,employee,,007\n"
        f"{PHONE_NEW},,employee,Maid 1,#7\n"
        f"{AC},AC06,fixed,,\n")
    assert (result.added, result.errors) == (3, [])
    identity = devices.identity("7")
    assert identity.macs == [PHONE_OLD, PHONE_NEW]
    assert (identity.name, identity.owner) == ("Maid phone", "Maid 1")  # merged
    # The generated ID never takes one a row names.
    assert devices.get(AC).identity == "8"
    copy = DeviceList()
    copy.import_csv(devices.export_csv())
    assert copy.to_storage() == devices.to_storage()


def test_csv_identity_rows_must_agree_on_category():
    devices = DeviceList()
    result = devices.import_csv("mac,category,identity\n"
                                f"{PHONE_OLD},employee,1\n{PHONE_NEW},fixed,1\n"
                                f"{AC},fixed,bad id!\n")
    assert result.added == 1 and len(result.errors) == 2
    assert "earlier line" in result.errors[0] and "Invalid device number" in result.errors[1]


def test_storage_with_a_mac_twice_keeps_the_first():
    data = {"identities": [
        {"id": "1", "category": "employee", "macs": [PHONE_OLD]},
        {"id": "2", "category": "employee", "macs": [PHONE_OLD, PHONE_NEW]},
        {"id": "3", "category": "employee", "macs": [PHONE_OLD]},
        {"id": "", "category": "employee", "macs": [AC]}]}
    devices = DeviceList.from_storage(data)
    assert [(i.id, i.macs) for i in devices.identities()] == [
        ("1", [PHONE_OLD]), ("2", [PHONE_NEW])]


def test_route_of_an_identity_joins_its_macs(tmp_path):
    url = _sqlite(tmp_path)
    history.check_connection(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(insert(history.rooms), [
            {"room_id": r, "number": None, "name": r.title(), "kind": "common", "updated": T0}
            for r in ("lobby", "office")])
        conn.execute(insert(history.presence_sessions), [
            {"client_mac": PHONE_OLD, "area_id": "lobby", "category": "employee",
             "started": T0, "ended": T0 + timedelta(hours=1), "seconds": 3600},
            # The phone changed its MAC, then went on to the office.
            {"client_mac": PHONE_NEW, "area_id": "lobby", "category": "guest",
             "started": T0 + timedelta(minutes=62), "ended": T0 + timedelta(hours=2),
             "seconds": 3480}])
    with engine.connect() as conn:
        route = device_route(conn, [PHONE_OLD, PHONE_NEW], T0, T0 + timedelta(hours=4),
                             current=[("office", T0 + timedelta(hours=3))])
    assert [(s["room_id"], s["seconds"], s["ended"]) for s in route["stops"]] == [
        ("lobby", 7200, "2026-01-01T02:00:00+00:00"), ("office", 3600, None)]


async def test_link_and_unlink_actions(hass, make_entry, patch_api, hass_storage):
    await _hotel(hass, make_entry())
    await hass.services.async_call(DOMAIN, "import_devices", {
        "csv": f"mac,name,category,identity\n{PHONE_OLD},Maid phone,employee,7\n"},
        blocking=True, return_response=True)
    guests = int(_state(hass, "sensor.room_06_guest_devices"))
    linked = await hass.services.async_call(DOMAIN, "link_mac", {
        "identity": "7", "mac": GUEST_PHONE}, blocking=True, return_response=True)
    assert linked["previous_identity"] is None
    assert linked["identity"]["macs"] == [PHONE_OLD, GUEST_PHONE]
    # The guest phone in Room 06 is now this employee's.
    assert _state(hass, "sensor.room_06_employee_devices") == "1"
    assert _state(hass, "sensor.room_06_guest_devices") == str(guests - 1)
    stored = hass_storage[STORAGE_KEY_DEVICES]["data"]
    assert stored["identities"][0]["macs"] == [PHONE_OLD, GUEST_PHONE]
    exported = await hass.services.async_call(DOMAIN, "export_devices", {}, blocking=True,
                                              return_response=True)
    assert [i["id"] for i in exported["identities"]] == ["7"]
    assert exported["csv"].splitlines()[1].endswith(",7")

    with pytest.raises(ServiceValidationError, match="Unknown device identity"):
        await hass.services.async_call(DOMAIN, "link_mac", {"identity": "9", "mac": AC},
                                       blocking=True, return_response=True)
    unlinked = await hass.services.async_call(DOMAIN, "unlink_mac", {"mac": GUEST_PHONE},
                                              blocking=True, return_response=True)
    assert unlinked == {"mac": GUEST_PHONE, "previous_identity": "7",
                        "identity_removed": False}
    assert _state(hass, "sensor.room_06_guest_devices") == str(guests)
    with pytest.raises(ServiceValidationError, match="not on the device list"):
        await hass.services.async_call(DOMAIN, "unlink_mac", {"mac": GUEST_PHONE},
                                       blocking=True, return_response=True)


async def test_device_route_by_identity_and_devices_table(hass, make_entry, patch_api,
                                                          db_url):  # noqa: F811
    entry = _entry_with_db(make_entry)
    controller = await _db_hotel(hass, entry)
    await hass.services.async_call(DOMAIN, "import_devices", {
        "csv": f"mac,name,category,identity\n{PHONE_OLD},Maid phone,employee,7\n"
               f"{GUEST_PHONE},,employee,7\n"}, blocking=True, return_response=True)
    route = await hass.services.async_call(DOMAIN, "device_route", {"identity": "#7"},
                                           blocking=True, return_response=True)
    assert (route["identity"], route["name"], route["macs"]) == (
        "7", "Maid phone", [PHONE_OLD, GUEST_PHONE])
    assert [s["room_id"] for s in route["stops"]] == ["room_06"]  # the open session
    by_mac = await hass.services.async_call(DOMAIN, "device_route", {"mac": GUEST_PHONE},
                                            blocking=True, return_response=True)
    assert by_mac["identity"] == "7" and by_mac["macs"] == [GUEST_PHONE]
    for data, match in (({}, "mac or a device"), ({"identity": "99"}, "Unknown device")):
        with pytest.raises(ServiceValidationError, match=match):
            await hass.services.async_call(DOMAIN, "device_route", data, blocking=True,
                                           return_response=True)

    await controller.history.async_flush()
    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT identity_id, mac, name, category FROM v1_device_macs "
                                 "ORDER BY mac")).fetchall()
    engine.dispose()
    assert [tuple(r) for r in rows] == [("7", GUEST_PHONE, "Maid phone", "employee"),
                                        ("7", PHONE_OLD, "Maid phone", "employee")]


async def test_options_form_links_a_mac_and_keeps_the_id_on_mac_edit(hass, make_entry,
                                                                      patch_api, hass_storage):
    await _hotel(hass, make_entry())
    flow = await _options_menu(hass, hass.config_entries.async_entries(DOMAIN)[0],
                               "device_list", "device_add")
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "mac": PHONE_OLD, "name": "Maid phone", "category": "employee"})
    assert result["step_id"] == "device_list"
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "device_add"})
    # The device is chosen from the list (nothing typed): a made-up ID is refused.
    form = result["data_schema"].schema
    choices = next(v for k, v in form.items() if k == "identity").config["options"]
    assert [c["value"] for c in choices] == ["new", "1"]
    assert choices[1]["label"] == "#1 Maid phone (employee)"
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(flow["flow_id"], {
            "mac": AC, "category": "fixed", "identity": "9999"})
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "mac": PHONE_NEW, "name": "Other name", "category": "fixed", "identity": "1"})
    assert result["step_id"] == "device_list"
    identities = hass_storage[STORAGE_KEY_DEVICES]["data"]["identities"]
    assert [(i["id"], i["macs"]) for i in identities] == [("1", [PHONE_OLD, PHONE_NEW])]
    # Linked to an existing device: its own name and category stay.
    assert (identities[0]["name"], identities[0]["category"]) == ("Maid phone", "employee")

    # Editing a MAC keeps the device: the new MAC joins the same identity.
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "device_edit_select"})
    result = await hass.config_entries.options.async_configure(flow["flow_id"],
                                                               {"mac": PHONE_OLD})
    assert result["step_id"] == "device_edit"
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "mac": "02-00-00-00-00-75", "name": "Maid phone", "category": "employee"})
    identities = hass_storage[STORAGE_KEY_DEVICES]["data"]["identities"]
    assert [(i["id"], sorted(i["macs"])) for i in identities] == [
        ("1", ["02-00-00-00-00-72", "02-00-00-00-00-75"])]
    # Editing another device's row to point at EMP-0001 moves its MAC there.
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "device_add"})
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "mac": AC, "name": "AC06", "category": "fixed"})
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "device_edit_select"})
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {"mac": AC})
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "mac": AC, "name": "AC06", "category": "fixed", "identity": "1"})
    identities = hass_storage[STORAGE_KEY_DEVICES]["data"]["identities"]
    assert [(i["id"], i["name"], len(i["macs"])) for i in identities] == [
        ("1", "Maid phone", 3)]
    # "New device" on a MAC of a device with several MACs splits it off (next ID).
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "device_edit_select"})
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {"mac": AC})
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "mac": AC, "name": "AC06", "category": "fixed", "identity": "new"})
    identities = hass_storage[STORAGE_KEY_DEVICES]["data"]["identities"]
    assert [(i["id"], i["name"], i["macs"]) for i in identities] == [
        ("1", "Maid phone", ["02-00-00-00-00-72", "02-00-00-00-00-75"]),
        ("3", "AC06", [AC])]
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
