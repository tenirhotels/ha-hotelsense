"""Client entities on (diagnostics option) and a brand-new device that Omada
already lists as connected but not yet in its client history (known clients)."""
from __future__ import annotations

import logging

from homeassistant.helpers import entity_registry as er

from custom_components.hotel_sense.const import CONF_TRACK_CLIENTS, DOMAIN
from custom_components.hotel_sense.ids import make_unique_id

from .fakes import SITE_ID, client_raw, known_raw
from .test_stage_a import _hotel, _poll, _state

NEW = "02-00-00-00-00-77"


async def test_new_client_not_yet_in_history_does_not_break_setup(
        hass, make_entry, patch_api, caplog):
    controller = await _hotel(hass, make_entry({CONF_TRACK_CLIENTS: True}))
    clients = patch_api.responses["/clients"]["data"]
    known = patch_api.responses["/insight/clients"]["data"]
    patch_api.set_data(patch_api.responses["/devices"],
                       clients + [client_raw(NEW, "New-Phone")], known)
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        await _poll(hass, controller)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], caplog.text
    # Presence counts it right away ...
    assert _state(hass, "sensor.room_06_guest_devices") == "3"
    # ... its own entities wait until Omada knows the client.
    reg = er.async_get(hass)
    uid = make_unique_id("client", SITE_ID, NEW)
    assert reg.async_get_entity_id("device_tracker", DOMAIN, uid) is None

    patch_api.set_data(patch_api.responses["/devices"], clients + [client_raw(NEW, "New-Phone")],
                       known + [known_raw(NEW, "New-Phone")])
    await _poll(hass, controller)
    assert reg.async_get_entity_id("device_tracker", DOMAIN, uid) is not None
