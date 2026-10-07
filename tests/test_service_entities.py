"""Service health entities: controller, Omada webhook, history database, Exely API."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from tplink_omada_client.exceptions import ConnectionFailed

from custom_components.hotel_sense.const import CONF_EXELY_CLIENT_ID, CONF_EXELY_CLIENT_SECRET, CONF_EXELY_PROPERTY_ID

from .test_history import _db_hotel, _entry_with_db, db_url  # noqa: F401  (fixture)
from .test_omada_webhook import MESSAGE, _post
from .test_stage_a import _hotel, _poll


def _state(hass, entry, unique_id):
    entity_id = er.async_get(hass).async_get_entity_id(
        unique_id.split("|")[0], "hotel_sense", unique_id.split("|")[1])
    assert entity_id, unique_id
    return hass.states.get(entity_id)


async def _tick(hass):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()


async def test_controller_online(hass, make_entry, patch_api):
    entry = make_entry()
    controller = await _hotel(hass, entry)
    cid = controller.controller_id
    state = _state(hass, entry, f"binary_sensor|controller_online-{cid}")
    assert state.state == "on" and state.attributes["device_class"] == "connectivity"
    patch_api.raise_on_status = [ConnectionFailed("down")]
    await _poll(hass, controller)
    assert _state(hass, entry, f"binary_sensor|controller_online-{cid}").state == "off"


async def test_omada_webhook_last_message(hass, make_entry, patch_api, hass_client_no_auth):
    entry = make_entry()
    controller = await _hotel(hass, entry)
    uid = f"sensor|omada_webhook_last_message-{controller.controller_id}"
    assert _state(hass, entry, uid).state == "unknown"
    await _post(await hass_client_no_auth(), entry, MESSAGE)
    await _tick(hass)
    state = _state(hass, entry, uid)
    assert dt_util.parse_datetime(state.state) is not None
    assert state.attributes["received"] == 1


async def test_exely_api_states(hass, make_entry, patch_api):
    entry = make_entry()
    controller = await _hotel(hass, entry)
    uid = f"sensor|exely_api-{controller.controller_id}"
    assert _state(hass, entry, uid).state == "not_set_up"
    hass.config_entries.async_update_entry(entry, data={
        **entry.data, CONF_EXELY_CLIENT_ID: "c", CONF_EXELY_CLIENT_SECRET: "s",
        CONF_EXELY_PROPERTY_ID: "1"})
    await _tick(hass)
    assert _state(hass, entry, uid).state == "ok"
    controller.exely.api.rooms_error = "ExelyApiError: HTTP 500"
    await _tick(hass)
    state = _state(hass, entry, uid)
    assert state.state == "error" and state.attributes["rooms_error"] == "ExelyApiError: HTTP 500"


async def test_history_database_only_when_set_up(hass, make_entry, patch_api, db_url):  # noqa: F811
    entry = make_entry()
    controller = await _hotel(hass, entry)
    assert er.async_get(hass).async_get_entity_id(
        "binary_sensor", "hotel_sense", f"history_database-{controller.controller_id}") is None


async def test_history_database_connected(hass, make_entry, patch_api, db_url):  # noqa: F811
    entry = _entry_with_db(make_entry)
    controller = await _db_hotel(hass, entry)
    uid = f"binary_sensor|history_database-{controller.controller_id}"
    controller.history.add("room_status", area_id="room_06", status="checked_in", source="manual")
    await controller.history.async_flush()
    await _tick(hass)
    state = _state(hass, entry, uid)
    assert state.state == "on" and state.attributes["written"] >= 1
    assert state.attributes["queued"] == 0 and state.attributes["last_error"] is None
