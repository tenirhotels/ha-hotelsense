"""Device type: owner's list -> Omada -> hostname -> private MAC -> unknown."""
from __future__ import annotations

import pytest

from custom_components.hotel_sense.device_kind import (
    KIND_APPLIANCE, KIND_COMPUTER, KIND_PERSONAL, KIND_PHONE, KIND_POS, KIND_PRINTER, KIND_TABLET,
    KIND_TV, KIND_UNKNOWN, KIND_WATCH, kind_from_name, kind_from_omada, parse_kind, resolve_kind,
)
from custom_components.hotel_sense.device_list import DeviceList

from .fakes import AP_WF06, GUEST_PHONE, SHARED_MAC, client_raw
from .test_stage_a import _hotel, _import, _poll, _set_clients

REAL_MAC = "00-11-22-33-44-55"
RANDOM_MAC = "02-11-22-33-44-55"


# Category / type / vendor / OS combinations as Omada shows them.
@pytest.mark.parametrize(("raw", "expected"), [
    ({"deviceCategory": "Mobile", "deviceType": "Mobile", "vendor": "Samsung", "osName": "Android"}, KIND_PHONE),
    ({"deviceCategory": "Mobile", "deviceType": "Mobile", "vendor": "Apple"}, KIND_PHONE),
    ({"deviceCategory": "Smart Home", "deviceType": "Smart Appliance", "vendor": "LG"}, KIND_APPLIANCE),
    ({"deviceCategory": "Smart Home", "deviceType": "Smart Cleaner", "vendor": "Xiaomi"}, KIND_APPLIANCE),
    ({"deviceCategory": "Audio & Video", "deviceType": "Television"}, KIND_TV),
    ({"deviceCategory": "Office", "deviceType": "Computer", "vendor": "Lenovo", "osName": "Windows"}, KIND_COMPUTER),
    ({"deviceCategory": "Office", "deviceType": "Raspberry Pi"}, KIND_COMPUTER),
    ({"deviceType": "Tablet", "vendor": "Apple"}, KIND_TABLET),
    ({"deviceType": "Smart Watch"}, KIND_WATCH),
    ({"osName": "Android"}, KIND_PHONE),  # only the OS known
    ({"deviceCategory": "Others", "deviceType": "Unknown", "vendor": "Unknown", "osName": "Unknown"}, None),
    ({"deviceCategory": "-", "vendor": "-"}, None),
    ({}, None), (None, None),
])
def test_kind_from_omada(raw, expected):
    assert kind_from_omada(raw) == expected


@pytest.mark.parametrize(("name", "expected"), [
    ("iPhone", KIND_PHONE), ("S24-pol-zovatela-Roman", KIND_PHONE), ("Z-Flip6-Alina", KIND_PHONE),
    ("Galaxy-Tab-S9", KIND_TABLET), ("iPad", KIND_TABLET), ("Apple Watch", KIND_WATCH),
    ("MacBook-Pro", KIND_COMPUTER), ("PC-xuxiang2", KIND_COMPUTER), ("XEROX", KIND_PRINTER),
    ("A930RTX (POS)", KIND_POS), ("TV02", KIND_TV), ("HOT1", None), ("bao-bao", None), ("", None),
])
def test_kind_from_name(name, expected):
    assert kind_from_name(name) == expected


def test_resolve_priority():
    omada_phone = {"deviceType": "Mobile"}
    assert resolve_kind(RANDOM_MAC, listed="tv", omada_raw=omada_phone, name="iPhone") == ("tv", "list")
    assert resolve_kind(RANDOM_MAC, omada_raw=omada_phone, name="MacBook") == (KIND_PHONE, "omada")
    assert resolve_kind(RANDOM_MAC, omada_raw={"deviceType": "Unknown"}, name="MacBook") == (KIND_COMPUTER, "name")
    assert resolve_kind(RANDOM_MAC, name=RANDOM_MAC) == (KIND_PERSONAL, "private_mac")
    assert resolve_kind(REAL_MAC, name="HOT1") == (KIND_UNKNOWN, "unknown")


def test_mac_shaped_name_is_not_a_hostname():
    # "TV" inside a MAC-like name must not count as a hostname hint.
    assert resolve_kind(REAL_MAC, name=REAL_MAC) == (KIND_UNKNOWN, "unknown")


def test_parse_kind_aliases_and_errors():
    assert parse_kind("Телефон") == KIND_PHONE and parse_kind("кондиционер") == KIND_APPLIANCE
    assert parse_kind("laptop") == KIND_COMPUTER and parse_kind("") == ""
    with pytest.raises(ValueError):
        parse_kind("spaceship")


def test_device_list_device_type_column():
    devices = DeviceList()
    result = devices.import_csv("mac,name,category,device_type\n"
                                f"{REAL_MAC},POS,fixed,терминал\n"
                                f"{RANDOM_MAC},X,fixed,spaceship\n")
    assert result.added == 1 and "Unknown device type" in result.errors[0]
    assert devices.get(REAL_MAC).device_type == KIND_POS
    assert devices.export_csv().splitlines()[1].endswith(",pos,FIX-0001")


# --------------------------------------------------------------------------- #
# In Home Assistant
# --------------------------------------------------------------------------- #
async def test_room_sensor_shows_device_types(hass, make_entry, patch_api):
    controller = await _hotel(hass, make_entry())
    phone = client_raw(GUEST_PHONE, "Guest-Phone") | {"deviceCategory": "Mobile", "deviceType": "Mobile"}
    anonymous = client_raw(SHARED_MAC, SHARED_MAC)  # no name, private MAC, Omada does not know it
    _set_clients(patch_api, [phone, anonymous])
    await _poll(hass, controller)

    attrs = hass.states.get("sensor.room_06_guest_devices").attributes
    by_mac = {d["mac"]: d for d in attrs["devices"]}
    assert (by_mac[GUEST_PHONE]["type"], by_mac[GUEST_PHONE]["type_source"]) == ("phone", "omada")
    assert (by_mac[SHARED_MAC]["type"], by_mac[SHARED_MAC]["type_source"]) == ("personal", "private_mac")
    assert attrs["types"] == {"personal": 1, "phone": 1}

    # The owner's list wins.
    await _import(hass, f"mac,name,category,device_type\n{SHARED_MAC},Desk PC,employee,computer\n")
    attrs = hass.states.get("sensor.room_06_employee_devices").attributes
    device = attrs["devices"][0]
    assert {k: device[k] for k in ("mac", "name", "type", "type_source")} == {
        "mac": SHARED_MAC, "name": "Desk PC", "type": "computer", "type_source": "list"}
    assert attrs["types"] == {"computer": 1}


async def test_options_form_sets_device_type(hass, make_entry, patch_api, hass_storage):
    from .test_stage_a import _options_menu
    from custom_components.hotel_sense.const import STORAGE_KEY_DEVICES
    entry = make_entry()
    await _hotel(hass, entry)
    flow = await _options_menu(hass, entry, "device_list", "device_add")
    await hass.config_entries.options.async_configure(flow["flow_id"], {
        "mac": GUEST_PHONE, "name": "Lobby TV", "category": "fixed", "device_type": "tv"})
    await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "device_add"})
    await hass.config_entries.options.async_configure(flow["flow_id"], {
        "mac": SHARED_MAC, "category": "fixed", "device_type": "auto"})
    devices = {d["mac"]: d for d in hass_storage[STORAGE_KEY_DEVICES]["data"]["devices"]}
    assert devices[GUEST_PHONE]["device_type"] == "tv"
    assert devices[SHARED_MAC]["device_type"] == ""  # auto = detect
