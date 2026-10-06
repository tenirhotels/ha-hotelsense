"""MAC normalisation and the fixed/employee device list (CSV import/export)."""
from __future__ import annotations

import pytest

from custom_components.hotel_sense.device_list import (
    CATEGORY_EMPLOYEE, CATEGORY_FIXED, DeviceList, KnownDevice, parse_category,
)
from custom_components.hotel_sense.mac import is_random_mac, is_valid_mac, parse_mac


@pytest.mark.parametrize("raw", [
    "aa:bb:cc:dd:ee:01", "AA-BB-CC-DD-EE-01", "aa-bb-cc-dd-ee-01", "aabbccddee01",
    "AABB.CCDD.EE01", "  aa:bb:cc:dd:ee:01 \n", "Aa:bB:cC:Dd:eE:01",
])
def test_parse_mac_accepts_common_notations(raw):
    assert parse_mac(raw) == "AA-BB-CC-DD-EE-01"


@pytest.mark.parametrize("raw", [
    "", "aa:bb:cc:dd:ee", "aa:bb:cc:dd:ee:01:02", "gg:bb:cc:dd:ee:01",
    "aabb:cc-ddee01", "AA-BB-CC-DD-EE-0", None, 42,
])
def test_parse_mac_rejects_garbage(raw):
    with pytest.raises(ValueError):
        parse_mac(raw)
    assert not is_valid_mac(raw)


def test_random_mac_detection():
    assert is_random_mac("02-00-00-00-00-01")   # locally administered bit set
    assert is_random_mac("DA:A1:19:00:00:01")
    assert not is_random_mac("AA-AA-AA-00-00-06".replace("AA", "A8", 1))
    assert not is_random_mac("00-11-22-33-44-55")
    assert not is_random_mac("not a mac")


def test_category_aliases():
    assert parse_category("Fixed") == CATEGORY_FIXED
    assert parse_category("сотрудник") == CATEGORY_EMPLOYEE
    assert parse_category("", default=CATEGORY_FIXED) == CATEGORY_FIXED
    with pytest.raises(ValueError):
        parse_category("")
    with pytest.raises(ValueError):
        parse_category("guest")


def test_known_device_normalises_fields():
    d = KnownDevice(mac="aa:bb:cc:dd:ee:01", category="Staff", name=" AC01 ")
    assert (d.mac, d.category, d.name) == ("AA-BB-CC-DD-EE-01", CATEGORY_EMPLOYEE, "AC01")


def test_import_mixed_notations_with_header():
    """Devices.csv style: MACs written with both colons and dashes."""
    csv_text = (
        "name,mac,category,note\n"
        "AC01,aa:bb:cc:00:00:01,fixed,Room 01\n"
        "HF01,AA-BB-CC-00-01-01,fixed,Room 01\n"
        "Maid phone,aabbcc000201,employee,\n"
    )
    devices = DeviceList()
    result = devices.import_csv(csv_text)
    assert (result.added, result.updated, result.errors) == (3, 0, [])
    assert devices.category_of("AA:BB:CC:00:00:01") == CATEGORY_FIXED
    assert devices.get("aa-bb-cc-00-01-01").note == "Room 01"
    assert devices.category_of("AA-BB-CC-00-02-01") == CATEGORY_EMPLOYEE


def test_import_semicolon_russian_header_and_bom():
    csv_text = "﻿Устройство;MAC;Категория\nКондиционер;aa:bb:cc:00:00:02;Фиксированное\n"
    devices = DeviceList()
    assert devices.import_csv(csv_text).added == 1
    assert devices.get("AA-BB-CC-00-00-02").name == "Кондиционер"


def test_import_without_header_and_default_category():
    devices = DeviceList()
    result = devices.import_csv("aa:bb:cc:00:00:03,AC03\n", default_category=CATEGORY_FIXED)
    assert result.added == 1 and devices.get("AA-BB-CC-00-00-03").name == "AC03"


def test_import_reports_bad_rows_and_keeps_good_ones():
    devices = DeviceList()
    result = devices.import_csv(
        "mac,name,category\nnot-a-mac,x,fixed\naa:bb:cc:00:00:04,y,unknown\n"
        "aa:bb:cc:00:00:05,z,fixed\n# comment line\n\n")
    assert result.added == 1
    assert len(result.errors) == 2 and result.errors[0].startswith("line 2")


def test_import_update_and_replace():
    devices = DeviceList([KnownDevice("AA-BB-CC-00-00-01", CATEGORY_FIXED, "old"),
                          KnownDevice("AA-BB-CC-00-00-09", CATEGORY_FIXED, "stale")])
    result = devices.import_csv("mac,name,category\naa:bb:cc:00:00:01,new,fixed\n", replace=True)
    assert (result.added, result.updated, result.removed) == (0, 1, 1)
    assert [d.name for d in devices] == ["new"]


def test_replace_with_only_broken_rows_keeps_list():
    devices = DeviceList([KnownDevice("AA-BB-CC-00-00-01", CATEGORY_FIXED)])
    result = devices.import_csv("mac\nbroken\n", replace=True)
    assert result.errors and len(devices) == 1


def test_export_import_roundtrip_and_storage():
    devices = DeviceList([
        KnownDevice("AA-BB-CC-00-00-01", CATEGORY_FIXED, "AC01", room="Room 01"),
        KnownDevice("AA-BB-CC-00-02-01", CATEGORY_EMPLOYEE, "Phone", owner="Maid 1"),
    ])
    text = devices.export_csv()
    assert text.splitlines()[0] == "mac,name,category,owner,note,room,device_type"
    copy = DeviceList()
    copy.import_csv(text)
    assert copy.to_storage() == devices.to_storage()
    assert DeviceList.from_storage(devices.to_storage()).to_storage() == devices.to_storage()


def test_from_storage_skips_corrupt_rows():
    data = {"devices": [{"mac": "bad", "category": "fixed"},
                        {"mac": "AA-BB-CC-00-00-01", "category": "fixed"},
                        {"nonsense": 1}]}
    assert len(DeviceList.from_storage(data)) == 1
    assert len(DeviceList.from_storage(None)) == 0


def test_remove():
    devices = DeviceList([KnownDevice("AA-BB-CC-00-00-01", CATEGORY_FIXED)])
    assert devices.remove("aa:bb:cc:00:00:01") is True
    assert devices.remove("aa:bb:cc:00:00:01") is False
    assert "AA-BB-CC-00-00-01" not in devices


def test_note_and_room_are_separate_columns():
    """Owner's format: department in `note`, installation room in `room`."""
    devices = DeviceList()
    result = devices.import_csv(
        "mac,name,category,owner,note,room\n"
        "AA-BB-CC-00-00-01,AC01,fixed,,,Room 01\n"
        "AA-BB-CC-00-02-01,Admin phone,employee,Admin,Superviser,\n")
    assert (result.added, result.errors) == (2, [])
    ac, phone = devices.get("AA-BB-CC-00-00-01"), devices.get("AA-BB-CC-00-02-01")
    assert (ac.room, ac.note) == ("Room 01", "")
    assert (phone.owner, phone.note, phone.room) == ("Admin", "Superviser", "")


def test_room_only_and_russian_headers():
    devices = DeviceList()
    devices.import_csv("mac;Устройство;Категория;Номер;Отдел\naa:bb:cc:00:00:01;AC01;fixed;Room 01;Техслужба\n")
    d = devices.get("AA-BB-CC-00-00-01")
    assert (d.room, d.note) == ("Room 01", "Техслужба")


def test_storage_from_before_room_field_still_loads():
    old = {"devices": [{"mac": "AA-BB-CC-00-00-01", "category": "fixed", "name": "AC01",
                        "owner": "", "note": "Room 01"}]}
    d = DeviceList.from_storage(old).get("AA-BB-CC-00-00-01")
    assert (d.note, d.room) == ("Room 01", "")
