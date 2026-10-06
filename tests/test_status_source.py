"""Room status select: who set the status (manual / Exely / restored) and when."""
from __future__ import annotations

from homeassistant.core import State
from pytest_homeassistant_custom_component.common import mock_restore_cache

from .test_exely import _exely_hotel, _post


def _attrs(hass):
    return hass.states.get("select.room_06_status").attributes


async def test_status_set_manually(hass, make_entry, patch_api, freezer):
    freezer.move_to("2026-10-07T00:38:11+00:00")
    await _exely_hotel(hass, make_entry)
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_06_status", "option": "checked_in"}, blocking=True)
    attrs = _attrs(hass)
    assert attrs["source"] == "manual"
    assert attrs["changed_at"] == "2026-10-07T00:38:11+00:00"
    assert "set_by" not in attrs


async def test_status_set_by_exely(hass, make_entry, patch_api, hass_client_no_auth):
    entry = await _exely_hotel(hass, make_entry)
    client = await hass_client_no_auth()
    await _post(client, entry, {"eventType": "CheckIn", "booking": {"roomNumber": "06"}})
    await hass.async_block_till_done()
    assert hass.states.get("select.room_06_status").state == "checked_in"
    assert _attrs(hass)["source"] == "exely"
    assert _attrs(hass)["changed_at"]


async def test_restored_status_says_so_and_keeps_origin(hass, make_entry, patch_api):
    """After a restart: source 'restored', original setter and time kept."""
    mock_restore_cache(hass, [State("select.room_06_status", "checked_in", {
        "source": "exely", "changed_at": "2026-10-06T12:00:00+00:00"})])
    await _exely_hotel(hass, make_entry)
    attrs = _attrs(hass)
    assert attrs["source"] == "restored"
    assert attrs["set_by"] == "exely"
    assert attrs["changed_at"] == "2026-10-06T12:00:00+00:00"


async def test_restored_twice_keeps_original_setter(hass, make_entry, patch_api):
    mock_restore_cache(hass, [State("select.room_06_status", "checked_in", {
        "source": "restored", "set_by": "manual", "changed_at": "2026-10-01T09:00:00+00:00"})])
    await _exely_hotel(hass, make_entry)
    assert _attrs(hass)["set_by"] == "manual"
    assert _attrs(hass)["changed_at"] == "2026-10-01T09:00:00+00:00"


async def test_restored_from_old_version_without_attributes(hass, make_entry, patch_api):
    mock_restore_cache(hass, [State("select.room_06_status", "checked_in")])
    await _exely_hotel(hass, make_entry)
    attrs = _attrs(hass)
    assert attrs["source"] == "restored"
    assert attrs["set_by"] is None and attrs["changed_at"] is None


async def test_change_after_restore_is_manual_again(hass, make_entry, patch_api):
    mock_restore_cache(hass, [State("select.room_06_status", "checked_in", {"source": "exely"})])
    await _exely_hotel(hass, make_entry)
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_06_status", "option": "checked_out"}, blocking=True)
    attrs = _attrs(hass)
    assert attrs["source"] == "manual" and "set_by" not in attrs
