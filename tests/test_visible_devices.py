"""Only the controller and access points become HA devices by default.

Connected clients (phones, TVs, air conditioners ...) and switches/gateways
get no devices: presence reads the client list directly from the controller.
"""
from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.hotel_sense.const import CONF_TRACK_CLIENTS, DOMAIN
from custom_components.hotel_sense.ids import make_unique_id

from .fakes import AP_WF06, AP_WF07, GUEST_PHONE, OFFLINE_CLIENT, SITE_ID, ap_raw
from .test_stage_a import _hotel, _setup, _state

SWITCH = "AA-AA-AA-00-00-50"
DEFAULTS = {CONF_TRACK_CLIENTS: False}


def _add_switch(api):
    switch = ap_raw(SWITCH, "SW01") | {"type": "switch", "compoundModel": "T1600G-28PS v3.0"}
    api.set_data(api.responses["/devices"] + [switch], api.responses["/clients"]["data"],
                 api.responses["/insight/clients"]["data"])


def _identifiers(hass, entry):
    return {ident for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
            for _, ident in d.identifiers}


async def test_default_creates_only_controller_and_access_points(hass, make_entry, patch_api):
    _add_switch(patch_api)
    entry = make_entry(DEFAULTS)
    await _setup(hass, entry)
    idents = _identifiers(hass, entry)
    assert idents == {"ctrl-1", make_unique_id("ap", SITE_ID, AP_WF06),
                      make_unique_id("ap", SITE_ID, AP_WF07)}
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert not [e for e in entities if e.unique_id.startswith(("client:", f"ap:{SITE_ID}:{SWITCH}",
                                                               f"update:{SITE_ID}:{SWITCH}"))]
    assert hass.states.get("device_tracker.guest_phone") is None
    assert hass.states.get("device_tracker.wf06") is not None


async def test_presence_works_without_client_entities(hass, make_entry, patch_api):
    await _hotel(hass, make_entry(DEFAULTS))
    assert _state(hass, "sensor.room_06_guest_devices") == "2"
    assert _state(hass, "sensor.room_06_state") == "violation"


async def test_existing_client_and_switch_devices_are_removed(hass, make_entry, patch_api):
    """Upgrade from 0.1.0: devices created back then disappear on load."""
    _add_switch(patch_api)
    entry = make_entry(DEFAULTS)
    entry.add_to_hass(hass)
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
    old = {}
    for ns, mac in (("client", GUEST_PHONE), ("client", OFFLINE_CLIENT), ("ap", SWITCH), ("ap", AP_WF06)):
        uid = make_unique_id(ns, SITE_ID, mac)
        device = dev_reg.async_get_or_create(config_entry_id=entry.entry_id,
                                             identifiers={(DOMAIN, uid)}, name=mac)
        old[(ns, mac)] = ent_reg.async_get_or_create(
            "device_tracker", DOMAIN, uid, config_entry=entry, device_id=device.id).entity_id

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    idents = _identifiers(hass, entry)
    assert make_unique_id("ap", SITE_ID, AP_WF06) in idents
    for key in (("client", GUEST_PHONE), ("client", OFFLINE_CLIENT), ("ap", SWITCH)):
        assert make_unique_id(key[0], SITE_ID, key[1]) not in idents, key
        assert ent_reg.async_get(old[key]) is None, key
    assert ent_reg.async_get(old[("ap", AP_WF06)]) is not None


async def test_client_entities_can_still_be_enabled(hass, make_entry, patch_api):
    entry = make_entry({CONF_TRACK_CLIENTS: True})
    await _setup(hass, entry)
    assert make_unique_id("client", SITE_ID, GUEST_PHONE) in _identifiers(hass, entry)
    assert hass.states.get("device_tracker.guest_phone") is not None


async def test_wlan_optimization_and_reconnect_are_gone(hass, make_entry, patch_api):
    """Removed on owner request: not needed, and reconnect fails on controller v6."""
    entry = make_entry({CONF_TRACK_CLIENTS: True})
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    # Left over from an older version.
    old = [reg.async_get_or_create("button", DOMAIN, "ai_optimization-ctrl-1", config_entry=entry),
           reg.async_get_or_create("binary_sensor", DOMAIN, "ai_optimization-ctrl-1", config_entry=entry),
           reg.async_get_or_create("button", DOMAIN, "reconnect_all_clients-ctrl-1", config_entry=entry),
           reg.async_get_or_create("button", DOMAIN, make_unique_id("client", SITE_ID, GUEST_PHONE, "reconnect"),
                                   config_entry=entry)]
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for e in old:
        assert reg.async_get(e.entity_id) is None, e.entity_id
    entities = er.async_entries_for_config_entry(reg, entry.entry_id)
    assert not [e for e in entities if "optimization" in e.unique_id or "reconnect" in e.unique_id]
    assert not [s.entity_id for s in hass.states.async_all() if "optimization" in s.entity_id
                or "reconnect" in s.entity_id]
