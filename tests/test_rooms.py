"""Room model: kind, number, Exely labels, status with its origin (last change wins)."""
from __future__ import annotations

from homeassistant.core import Context
from pytest_homeassistant_custom_component.common import async_fire_time_changed  # noqa: F401

from custom_components.hotel_sense.const import DOMAIN
from custom_components.hotel_sense.rooms import (
    KIND_COMMON, KIND_ROOM, STATUS_METADATA_CHANGED, STATUS_VALUE_CHANGED, RoomRegistry,
)

from .fakes import AP_WF06, AP_WF07
from .test_exely import _exely_hotel, _post
from .test_history import db_url  # noqa: F401  (fixture)
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


async def test_status_keeps_its_origin_and_last_change_wins(hass, freezer):
    registry = await _registry(hass)
    registry.ensure("room_01", "Room 01")
    assert registry.set_status("room_01", "checked_in", "manual") == STATUS_VALUE_CHANGED
    first = registry.get("room_01").status
    assert first.changed_at.endswith("+00:00") and first.booking is None
    freezer.tick(60)
    # Same value, Exely confirms with a booking: metadata only, changed_at kept.
    assert registry.set_status("room_01", "checked_in", "exely",
                               booking="B-123") == STATUS_METADATA_CHANGED
    status = registry.get("room_01").status
    assert (status.value, status.source, status.booking) == ("checked_in", "exely", "B-123")
    assert status.changed_at == first.changed_at
    # Same value, new booking / new user: metadata.
    assert registry.set_status("room_01", "checked_in", "exely",
                               booking="B-124") == STATUS_METADATA_CHANGED
    assert registry.set_status("room_01", "checked_in", "manual",
                               user_id="u1") == STATUS_METADATA_CHANGED
    assert registry.get("room_01").status.user_id == "u1"
    # Identical: no-op.
    assert registry.set_status("room_01", "checked_in", "manual", user_id="u1") is None
    # Transition: changed_at moves.
    freezer.tick(60)
    assert registry.set_status("room_01", "checked_out", "exely",
                               booking="B-124") == STATUS_VALUE_CHANGED
    assert registry.get("room_01").status.changed_at > first.changed_at
    assert registry.set_status("unknown", "checked_in", "manual") is None


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
    exported = registry.export_csv()
    assert exported == (
        "room_id;name;number;kind;exely_room_ids;exely_room_name\n"
        "room_01;Room 01;01;room;;\nroom_02;Room 02;02;room;;\n")
    assert registry.import_csv(exported) == ([], False)  # round trip: no change
    errors, changed = registry.import_csv(
        "room_id;name;number;kind;exely_room_ids;exely_room_name\n"
        "room_01;Renamed;101;room;4503599627373585, 4503599627373585 , ;Lux 1\n"
        "room_02;Room 02;;common;\n")  # 5 columns: still accepted
    assert errors == [] and changed
    room = registry.get("room_01")
    assert room.exely_room_ids == ["4503599627373585"] and room.exely_room_name == "Lux 1"
    assert room.number == "101" and registry.get("room_02").is_common
    # The name belongs to the HA Area: read only in the CSV, identity unchanged.
    assert (room.room_id, room.area_id, room.name) == ("room_01", "room_01", "Room 01")
    # Errors: nothing is saved (unknown room, bad kind, label on two rooms).
    errors, _ = registry.import_csv("room_09;x;;room;\nroom_01;;;hotel;\nroom_02;;;room;;Lux 1")
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


async def test_storage_from_0_8_is_migrated_without_loss(hass, hass_storage):
    """A 0.8 file (minor 1): mixed Exely labels kept, new field added, status kept."""
    hass_storage["hotel_sense.rooms.entry-1"] = {
        "version": 1, "minor_version": 1, "key": "hotel_sense.rooms.entry-1",
        "data": {"rooms": [
            {"room_id": "room_01", "name": "Room 01", "kind": "room", "number": "01",
             "area_id": "room_01", "exely_room_ids": ["4503599627373585", "Lux 1", " ", 101,
                                                      "Lux 1"],
             "status": {"value": "checked_in", "source": "exely",
                        "changed_at": "2026-10-07T07:00:00+00:00", "booking": "B-1",
                        "user_id": None}},
            {"room_id": "admin_house", "name": "Admin House", "kind": "common", "number": None,
             "area_id": "admin_house", "exely_room_ids": [], "status": None}],
            "pending_common": None, "pending_labels": {}}}
    registry = await _registry(hass)
    room = registry.get("room_01")
    assert room.exely_room_ids == ["4503599627373585", "Lux 1", "101"]  # nothing guessed away
    assert room.exely_room_name is None
    assert (room.status.value, room.status.booking, room.status.changed_at) == (
        "checked_in", "B-1", "2026-10-07T07:00:00+00:00")
    assert registry.get("admin_house").is_common
    # Old labels keep matching (as Exely IDs), the new name field too.
    assert registry.find_by_exely_label("lux 1") is room
    room.exely_room_name = "Deluxe 1"
    assert registry.find_by_exely_name("Deluxe 1") is room
    await registry.async_flush()
    assert hass_storage["hotel_sense.rooms.entry-1"]["minor_version"] == 2
    again = await _registry(hass)  # restart after the migration
    assert again.get("room_01").exely_room_name == "Deluxe 1"
    assert again.get("room_01").exely_room_ids == ["4503599627373585", "Lux 1", "101"]


async def test_metadata_survives_restart_and_is_no_transition(hass, make_entry, patch_api,
                                                              hass_client_no_auth, db_url):
    """Manual checked_in, then Exely confirms checked_in with a booking."""
    from .test_history import _entry_with_db, _rows
    entry = _entry_with_db(make_entry)
    from .test_stage_a import _hotel
    controller = await _hotel(hass, entry, {AP_WF06: "Room 06", AP_WF07: "Room 07"})
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_06_status", "option": "checked_in"}, blocking=True)
    changed_at = hass.states.get("select.room_06_status").attributes["changed_at"]
    await _post(await hass_client_no_auth(), entry,
                {"eventType": "CheckIn", "BookingNumber": "B-123", "room": "06"})
    await hass.async_block_till_done()
    attrs = hass.states.get("select.room_06_status").attributes
    assert (attrs["source"], attrs["booking"], attrs["changed_at"]) == (
        "exely", "B-123", changed_at)
    await controller.history.async_flush()
    # One transition in the history (the manual one), not two.
    assert [(r["status"], r["source"]) for r in _rows(db_url, "room_status")] == [
        ("checked_in", "manual")]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    attrs = hass.states.get("select.room_06_status").attributes
    assert (attrs["source"], attrs["booking"], attrs["changed_at"]) == (
        "exely", "B-123", changed_at)
    # Identity unchanged.
    from homeassistant.helpers import entity_registry as er
    assert er.async_get(hass).async_get("select.room_06_status").unique_id.endswith(
        ":room_06:room_status")


async def test_exely_resolution_order(hass, make_entry, patch_api, hass_client_no_auth, caplog):
    entry = await _exely_hotel(hass, make_entry)
    registry = hass.data[DOMAIN][entry.entry_id].presence.registry
    registry.import_csv("room_06;;06;room;900006;Lux\nroom_07;;07;room;;Deluxe")
    client = await hass_client_no_auth()
    for label, room in (("900006", "06"), ("deluxe", "07")):
        await _post(client, entry, {"eventType": "CheckIn", "room": label})
        await hass.async_block_till_done()
        assert _state(hass, f"select.room_{room}_status") == "checked_in"
    # Number fallback is logged.
    await _post(client, entry, {"eventType": "CheckOut", "room": "Apartment 6"})
    await hass.async_block_till_done()
    assert _state(hass, "select.room_06_status") == "checked_out"
    assert "matched to room_06 by its number only" in caplog.text


async def test_area_rename_changes_the_name_not_the_identity(hass, make_entry, patch_api):
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import entity_registry as er
    from .test_stage_a import _poll
    entry = await _exely_hotel(hass, make_entry)
    controller = hass.data[DOMAIN][entry.entry_id]
    before = er.async_get(hass).async_get("select.room_06_status").unique_id
    ar.async_get(hass).async_update("room_06", name="Room 06 Lux")
    await _poll(hass, controller)
    room = controller.presence.registry.get("room_06")
    assert (room.room_id, room.area_id, room.name) == ("room_06", "room_06", "Room 06 Lux")
    assert controller.presence.rooms["room_06"].name == "Room 06 Lux"  # read from the model
    entity = er.async_get(hass).async_get("select.room_06_status")
    assert entity is not None and entity.unique_id == before
