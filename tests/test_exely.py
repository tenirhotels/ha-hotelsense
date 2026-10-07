"""Exely PMS webhook: check-in / check-out sets the room status.

The real Exely payload schema is not documented; the shapes below are
plausible variants. Unknown or ambiguous events are never applied.
"""
from __future__ import annotations

import pytest
from homeassistant.helpers import area_registry as ar

from custom_components.hotel_sense.const import (
    CONF_EXELY_API_KEY, CONF_EXELY_ROOM_MAP, CONF_EXELY_WEBHOOK_ID, DOMAIN, EVENT_EXELY,
)
from custom_components.hotel_sense.diagnostics import async_get_config_entry_diagnostics
from custom_components.hotel_sense.exely import (
    parse_event, parse_room_map, room_number, status_from_event,
)
from custom_components.hotel_sense.presence import STATUS_CHECKED_IN, STATUS_CHECKED_OUT

from .fakes import AP_WF06, AP_WF07
from .test_stage_a import _hotel, _options_menu, _state


# --------------------------------------------------------------------------- #
# Parser (pure)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("name", "expected"), [
    ("CheckIn", STATUS_CHECKED_IN), ("booking.checked_in", STATUS_CHECKED_IN),
    ("GUEST_ARRIVAL", STATUS_CHECKED_IN), ("Заезд гостя", STATUS_CHECKED_IN),
    ("check-out", STATUS_CHECKED_OUT), ("CheckedOut", STATUS_CHECKED_OUT),
    ("Выезд", STATUS_CHECKED_OUT), ("departure", STATUS_CHECKED_OUT),
    ("BookingCreated", None), ("booking.cancelled", None),
    # Cancellations undo the previous status.
    ("CheckInCancelled", STATUS_CHECKED_OUT), ("check_in.cancel", STATUS_CHECKED_OUT),
    ("Отмена заезда", STATUS_CHECKED_OUT), ("UndoCheckIn", STATUS_CHECKED_OUT),
    ("CheckOutCancelled", STATUS_CHECKED_IN), ("Отмена выезда", STATUS_CHECKED_IN),
    ("checkout.reverted", STATUS_CHECKED_IN),
    ("checkin_and_checkout", None),  # ambiguous
])
def test_status_from_event(name, expected):
    assert status_from_event(name) == expected


def test_parse_nested_payload():
    payload = {"eventType": "CheckIn", "data": {"booking": {
        "number": "B-1", "guest": {"name": "***"},
        "rooms": [{"roomNumber": "101", "roomType": "Standard", "roomId": 555, "roomsCount": 1}]}}}
    parsed = parse_event(payload)
    assert (parsed.event, parsed.status, parsed.rooms) == ("CheckIn", STATUS_CHECKED_IN, ["101"])


def test_parse_flat_payload_with_int_room():
    parsed = parse_event({"event": "check_out", "room": 6})
    assert (parsed.status, parsed.rooms) == (STATUS_CHECKED_OUT, ["6"])


def test_parse_unrelated_event_has_no_status():
    parsed = parse_event({"event": "BookingCreated", "room": "101"})
    assert parsed.status is None and parsed.event == "BookingCreated"


def test_parse_conflicting_events_is_not_applied():
    parsed = parse_event({"event": "CheckIn", "data": {"type": "CheckOut"}, "room": "1"})
    assert parsed.status is None


def test_parse_non_dict_payloads():
    assert parse_event([]).status is None
    assert parse_event("text").rooms == []
    assert parse_event(None).event is None


def test_room_map_and_number():
    assert parse_room_map("101 = Room 01\n102;Room 02\n# comment\n\nbad\nLux -> Room 10") == {
        "101": "Room 01", "102": "Room 02", "lux": "Room 10"}
    assert room_number("Room 01") == 1 and room_number("№ 7") == 7 and room_number("Lux") is None


# --------------------------------------------------------------------------- #
# Webhook in Home Assistant
# --------------------------------------------------------------------------- #
async def _exely_hotel(hass, make_entry, options=None):
    entry = make_entry(options)
    await _hotel(hass, entry, {AP_WF06: "Room 06", AP_WF07: "Room 07"})
    return entry


async def _post(client, entry, payload, key: str | None = "", raw: str | None = None):
    headers = {}
    if key is not None:
        headers["API-KEY"] = key or entry.data[CONF_EXELY_API_KEY]
    url = f"/api/webhook/{entry.data[CONF_EXELY_WEBHOOK_ID]}"
    if raw is not None:
        return await client.post(url, data=raw, headers=headers)
    return await client.post(url, json=payload, headers=headers)


async def test_secrets_are_generated_once(hass, make_entry, patch_api):
    entry = await _exely_hotel(hass, make_entry)
    webhook_id, key = entry.data[CONF_EXELY_WEBHOOK_ID], entry.data[CONF_EXELY_API_KEY]
    assert len(webhook_id) >= 32 and len(key) >= 24
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (entry.data[CONF_EXELY_WEBHOOK_ID], entry.data[CONF_EXELY_API_KEY]) == (webhook_id, key)


async def test_check_in_and_check_out_set_room_status(hass, make_entry, patch_api, hass_client_no_auth):
    entry = await _exely_hotel(hass, make_entry)
    client = await hass_client_no_auth()
    events = []
    hass.bus.async_listen(EVENT_EXELY, events.append)

    resp = await _post(client, entry, {"eventType": "CheckIn", "booking": {"roomNumber": "06"}})
    await hass.async_block_till_done()
    assert resp.status == 200
    assert _state(hass, "select.room_06_status") == "checked_in"
    assert _state(hass, "sensor.room_06_state") == "checked_in"
    last = hass.states.get("sensor.hotel_sense_exely_last_event")
    assert last.state == "applied" and last.attributes["applied"] == ["Room 06"]
    assert "payload" not in last.attributes  # guest data never lands in states

    await _post(client, entry, {"event": "CheckOut", "room": 6})
    await hass.async_block_till_done()
    assert _state(hass, "select.room_06_status") == "checked_out"
    assert _state(hass, "sensor.room_06_state") == "violation"  # guests still connected
    assert [e.data["status"] for e in events] == ["checked_in", "checked_out"]


async def test_wrong_or_missing_api_key_is_rejected(hass, make_entry, patch_api, hass_client_no_auth):
    entry = await _exely_hotel(hass, make_entry)
    client = await hass_client_no_auth()
    for key in (None, "wrong"):
        resp = await _post(client, entry, {"event": "CheckIn", "room": "06"}, key=key)
        assert resp.status == 401
    await hass.async_block_till_done()
    assert _state(hass, "select.room_06_status") == "checked_out"
    assert hass.states.get("sensor.hotel_sense_exely_last_event").state == "unknown"


async def test_unrecognised_events_are_answered_but_not_applied(hass, make_entry, patch_api,
                                                                hass_client_no_auth):
    entry = await _exely_hotel(hass, make_entry)
    client = await hass_client_no_auth()
    for payload, raw in (({"event": "BookingCreated", "room": "06"}, None),
                         ({"event": "CheckIn", "room": "Admin House"}, None),  # not a room
                         ({"event": "CheckIn", "room": "99"}, None),           # no such room
                         (None, "not json")):
        resp = await _post(client, entry, payload, raw=raw)
        assert resp.status == 200  # Exely must not retry
    await hass.async_block_till_done()
    assert _state(hass, "select.room_06_status") == "checked_out"
    assert hass.states.get("sensor.hotel_sense_exely_last_event").state == "invalid"

    diag = await async_get_config_entry_diagnostics(hass, entry)
    recent = diag["exely_recent_events"]
    assert [r["result"] for r in recent] == ["invalid", "unmatched", "unmatched", "unmatched"]
    assert recent[1]["payload"] == {"event": "CheckIn", "room": "99"}  # raw kept for analysis
    assert recent[1]["unresolved"] == ["99"]
    assert diag["entry"][CONF_EXELY_API_KEY] == "**REDACTED**"
    assert diag["entry"][CONF_EXELY_WEBHOOK_ID] == "**REDACTED**"


async def test_room_mapping_option(hass, make_entry, patch_api, hass_client_no_auth):
    entry = await _exely_hotel(hass, make_entry, {CONF_EXELY_ROOM_MAP: "101 = Room 07\nLux = Room 99"})
    client = await hass_client_no_auth()
    await _post(client, entry, {"event": "CheckIn", "rooms": [{"roomNumber": "101"},
                                                               {"roomNumber": "Lux"}]})
    await hass.async_block_till_done()
    assert _state(hass, "select.room_07_status") == "checked_in"
    last = hass.states.get("sensor.hotel_sense_exely_last_event")
    assert last.state == "partial" and last.attributes["unresolved"] == ["Lux"]


async def test_ambiguous_room_number_is_not_guessed(hass, make_entry, patch_api, hass_client_no_auth):
    entry = make_entry()
    ar.async_get(hass).async_create("Room 6")  # both "Room 06" and "Room 6" end in 6
    await _hotel(hass, entry, {AP_WF06: "Room 06", AP_WF07: "Room 6"})
    client = await hass_client_no_auth()
    await _post(client, entry, {"event": "CheckIn", "room": "6"})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.hotel_sense_exely_last_event").state == "unmatched"
    # an exact Area name still works
    await _post(client, entry, {"event": "CheckIn", "room": "Room 6"})
    await hass.async_block_till_done()
    assert _state(hass, "select.room_6_status") == "checked_in"
    assert _state(hass, "select.room_06_status") == "checked_out"


async def test_options_step_shows_https_even_if_external_url_is_http(hass, make_entry, patch_api):
    """Exely accepts only https: an http external URL (HA behind a tunnel /
    proxy) is shown as https on the default port."""
    await hass.config.async_update(external_url="http://hass.example.org:8123")
    entry = await _exely_hotel(hass, make_entry)
    result = await _options_menu(hass, entry, "exely")
    assert result["description_placeholders"]["url"] == (
        f"https://hass.example.org/api/webhook/{entry.data[CONF_EXELY_WEBHOOK_ID]}")


async def test_options_step_keeps_an_https_port(hass, make_entry, patch_api):
    await hass.config.async_update(external_url="https://hass.example.org:8443")
    entry = await _exely_hotel(hass, make_entry)
    result = await _options_menu(hass, entry, "exely")
    assert result["description_placeholders"]["url"] == (
        f"https://hass.example.org:8443/api/webhook/{entry.data[CONF_EXELY_WEBHOOK_ID]}")


async def test_options_step_shows_url_and_key_and_saves_mapping(hass, make_entry, patch_api):
    await hass.config.async_update(external_url="https://hass.example.org")
    entry = await _exely_hotel(hass, make_entry)
    result = await _options_menu(hass, entry, "exely")
    assert result["step_id"] == "exely"
    placeholders = result["description_placeholders"]
    assert placeholders["url"] == (
        f"https://hass.example.org/api/webhook/{entry.data[CONF_EXELY_WEBHOOK_ID]}")
    assert placeholders["api_key"] == entry.data[CONF_EXELY_API_KEY]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_EXELY_ROOM_MAP: "101 = Room 06"})
    # Saved in the room model, not in the options.
    registry = hass.data[DOMAIN][entry.entry_id].presence.registry
    assert registry.get("room_06").exely_room_ids == ["101"]
    assert CONF_EXELY_ROOM_MAP not in entry.options
    result = await _options_menu(hass, entry, "exely")
    key = next(k for k in result["data_schema"].schema if k == CONF_EXELY_ROOM_MAP)
    assert key.description == {"suggested_value": "101 = Room 06"}


def test_number_101_is_not_room_01():
    """Hotel-style numbering is never guessed: it needs an explicit mapping."""
    assert room_number("101") == 101 != room_number("Room 01")


# --------------------------------------------------------------------------- #
# Rotating compromised secrets
# --------------------------------------------------------------------------- #
async def _rotate(hass, entry, **flags):
    result = await _options_menu(hass, entry, "exely")
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_EXELY_ROOM_MAP: "", **flags})


async def test_regenerate_api_key(hass, make_entry, patch_api, hass_client_no_auth):
    entry = await _exely_hotel(hass, make_entry)
    client = await hass_client_no_auth()
    old_key, webhook_id = entry.data[CONF_EXELY_API_KEY], entry.data[CONF_EXELY_WEBHOOK_ID]

    result = await _rotate(hass, entry, exely_new_key=True)
    # Form is shown again with the new key, so it can be copied to Exely.
    assert result["step_id"] == "exely_rotated"
    new_key = entry.data[CONF_EXELY_API_KEY]
    assert new_key != old_key and len(new_key) >= 24
    assert result["description_placeholders"]["api_key"] == new_key
    assert entry.data[CONF_EXELY_WEBHOOK_ID] == webhook_id  # URL unchanged

    assert (await _post(client, entry, {"event": "CheckIn", "room": "06"}, key=old_key)).status == 401
    assert (await _post(client, entry, {"event": "CheckIn", "room": "06"})).status == 200
    await hass.async_block_till_done()
    assert _state(hass, "select.room_06_status") == "checked_in"

    # Survives a restart.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data[CONF_EXELY_API_KEY] == new_key


async def test_regenerate_webhook_address(hass, make_entry, patch_api, hass_client_no_auth):
    entry = await _exely_hotel(hass, make_entry)
    client = await hass_client_no_auth()
    old_id = entry.data[CONF_EXELY_WEBHOOK_ID]

    result = await _rotate(hass, entry, exely_new_url=True)
    new_id = entry.data[CONF_EXELY_WEBHOOK_ID]
    assert new_id != old_id
    assert new_id in result["description_placeholders"]["url"]

    # Old address is gone (HA answers 200 for unknown webhooks, but nothing happens).
    key = entry.data[CONF_EXELY_API_KEY]
    await client.post(f"/api/webhook/{old_id}", json={"event": "CheckIn", "room": "06"},
                      headers={"API-KEY": key})
    await hass.async_block_till_done()
    assert _state(hass, "select.room_06_status") == "checked_out"
    # New address works; a missing key there is still rejected.
    assert (await _post(client, entry, {"event": "CheckIn", "room": "06"}, key=None)).status == 401
    assert (await _post(client, entry, {"event": "CheckIn", "room": "06"})).status == 200
    await hass.async_block_till_done()
    assert _state(hass, "select.room_06_status") == "checked_in"

    # Unload unregisters the new address cleanly (reload would fail on a duplicate id).
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


async def test_saving_without_checkboxes_keeps_secrets(hass, make_entry, patch_api):
    entry = await _exely_hotel(hass, make_entry)
    before = (entry.data[CONF_EXELY_API_KEY], entry.data[CONF_EXELY_WEBHOOK_ID])
    result = await _rotate(hass, entry)
    assert result["type"].value == "create_entry"
    assert (entry.data[CONF_EXELY_API_KEY], entry.data[CONF_EXELY_WEBHOOK_ID]) == before


async def test_cancelled_check_in_and_check_out_undo_the_status(hass, make_entry, patch_api,
                                                                 hass_client_no_auth):
    entry = await _exely_hotel(hass, make_entry)
    client = await hass_client_no_auth()
    steps = (("CheckIn", "checked_in"), ("CheckInCancelled", "checked_out"),
             ("CheckIn", "checked_in"), ("CheckOut", "checked_out"),
             ("CheckOutCancelled", "checked_in"))
    for event, expected in steps:
        await _post(client, entry, {"event": event, "room": "06"})
        await hass.async_block_till_done()
        assert _state(hass, "select.room_06_status") == expected, event


async def test_room_mapping_can_be_cleared(hass, make_entry, patch_api):
    entry = await _exely_hotel(hass, make_entry, {CONF_EXELY_ROOM_MAP: "101 = Room 07"})
    result = await _options_menu(hass, entry, "exely")
    registry = hass.data[DOMAIN][entry.entry_id].presence.registry
    assert registry.get("room_07").exely_room_ids == ["101"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry" and registry.get("room_07").exely_room_ids == []
