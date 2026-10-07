"""Room model: kind, number, Exely labels, status with its origin (last change wins)."""
from __future__ import annotations

from homeassistant.core import Context
from pytest_homeassistant_custom_component.common import async_fire_time_changed  # noqa: F401

from custom_components.hotel_sense.const import DOMAIN
from custom_components.hotel_sense.rooms import KIND_COMMON, KIND_ROOM, RoomRegistry

from .test_exely import _exely_hotel, _post
from .test_stage_a import _options_menu, _state


async def _registry(hass) -> RoomRegistry:
    registry = RoomRegistry(hass, "entry-1")
    await registry.async_load()
    return registry


async def test_rooms_are_created_from_areas(hass):
    registry = await _registry(hass)
    room = registry.ensure("room_01", "Room 01")
    assert (room.kind, room.number, room.area_id) == (KIND_ROOM, "01", "room_01")
    assert registry.ensure("admin_house", "Admin House").kind == KIND_COMMON
    assert registry.ensure("room_01", "Room 01 Lux").name == "Room 01 Lux"  # Area renamed


async def test_status_keeps_its_origin_and_last_change_wins(hass):
    registry = await _registry(hass)
    registry.ensure("room_01", "Room 01")
    assert registry.set_status("room_01", "checked_in", "exely", booking="B-1")
    status = registry.get("room_01").status
    assert (status.value, status.source, status.booking) == ("checked_in", "exely", "B-1")
    assert status.changed_at.endswith("+00:00")
    assert not registry.set_status("room_01", "checked_in", "manual")  # unchanged
    assert registry.set_status("room_01", "checked_out", "manual", user_id="u1")
    status = registry.get("room_01").status
    assert (status.value, status.source, status.user_id, status.booking) == (
        "checked_out", "manual", "u1", None)
    assert not registry.set_status("unknown", "checked_in", "manual")


async def test_legacy_options_wait_for_their_room(hass):
    registry = await _registry(hass)
    registry.import_legacy(["admin_house"], {"101": "Room 01", "lux": "Room 99"})
    room = registry.ensure("room_01", "Room 01")
    assert room.exely_room_ids == ["101"] and room.kind == KIND_ROOM
    assert registry.ensure("admin_house", "Admin House").kind == KIND_COMMON
    assert registry.is_pending_label("Lux") and registry.find_by_exely_label("101") is room
    assert registry.room_map_text() == "101 = Room 01\nlux = Room 99"


async def test_csv_round_trip_and_validation(hass):
    registry = await _registry(hass)
    registry.ensure("room_01", "Room 01")
    registry.ensure("room_02", "Room 02")
    assert registry.export_csv() == (
        "room_id;name;number;kind;exely_room_ids\n"
        "room_01;Room 01;01;room;\nroom_02;Room 02;02;room;\n")
    errors, changed = registry.import_csv(
        "room_id;name;number;kind;exely_room_ids\n"
        "room_01;Room 01;101;room;4503599627373585, Lux 1\n"
        "room_02;Room 02;;common;\n")
    assert errors == [] and changed
    assert registry.get("room_01").exely_room_ids == ["4503599627373585", "Lux 1"]
    assert registry.get("room_01").number == "101" and registry.get("room_02").is_common
    # Errors: nothing is saved.
    errors, _ = registry.import_csv("room_09;x;;room;\nroom_01;;;hotel;\nroom_02;;;room;Lux 1")
    assert len(errors) == 3 and registry.get("room_02").is_common


async def test_status_survives_a_restart_without_the_select(hass, make_entry, patch_api,
                                                             hass_client_no_auth):
    entry = await _exely_hotel(hass, make_entry)
    await _post(await hass_client_no_auth(), entry,
                {"eventType": "CheckIn", "BookingNumber": "B-7", "room": "06"})
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _state(hass, "select.room_06_status") == "checked_in"
    attrs = hass.states.get("select.room_06_status").attributes
    assert (attrs["source"], attrs["booking"]) == ("exely", "B-7")


async def test_manual_change_records_the_user(hass, make_entry, patch_api, hass_admin_user):
    await _exely_hotel(hass, make_entry)
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_06_status", "option": "checked_in"}, blocking=True,
        context=Context(user_id=hass_admin_user.id))
    attrs = hass.states.get("select.room_06_status").attributes
    assert attrs["source"] == "manual" and attrs["user_id"] == hass_admin_user.id
    assert attrs["changed_at"].endswith("+00:00")


async def test_rooms_options_step(hass, make_entry, patch_api, hass_client_no_auth):
    entry = await _exely_hotel(hass, make_entry)
    result = await _options_menu(hass, entry, "rooms")
    csv = next(k for k in result["data_schema"].schema if k == "csv").description["suggested_value"]
    assert "room_06;Room 06;06;room;" in csv
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "csv": "room_06;Room 06;06;room;Lux 6\nroom_07;Room 07;07;common;"})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()   # kinds changed: reloaded
    assert hass.states.get("select.room_07_status") is None  # common area now
    # The Exely label of the room is used.
    await _post(await hass_client_no_auth(), entry, {"eventType": "CheckIn", "room": "Lux 6"})
    await hass.async_block_till_done()
    assert _state(hass, "select.room_06_status") == "checked_in"

    result = await _options_menu(hass, entry, "rooms")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"csv": "room_99;x;;room;"})
    assert result["type"] == "form" and result["errors"] == {"base": "rooms_csv_errors"}
    assert "room_99" in result["description_placeholders"]["errors"]
    assert hass.data[DOMAIN][entry.entry_id].presence.registry.get("room_07").is_common
