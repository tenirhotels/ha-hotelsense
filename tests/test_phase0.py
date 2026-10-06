"""Hotel Sense - Phase 0 acceptance tests (Master ТЗ section 50).

Runs against the real Home Assistant version the target HA runs (2026.9.x).
Only the Omada HTTP layer is faked (see fakes.py).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

from custom_components.hotel_sense.api.errors import LoginRequired, RequestError
from custom_components.hotel_sense.const import DOMAIN
from custom_components.hotel_sense.ids import make_unique_id, parse_unique_id

from .fakes import (
    AP_WF06, AP_WF07, GUEST_PHONE, OFFLINE_CLIENT, SHARED_MAC, SITE_ID, WIRED_PC,
    known_raw,
)

INTEGRATION_DIR = Path(__file__).parent.parent / "custom_components" / "hotel_sense"


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return hass.data[DOMAIN][entry.entry_id]


def _uid(ns, mac, key=None):
    return make_unique_id(ns, SITE_ID, mac, key)


# --------------------------------------------------------------------------- #
# Acceptance: "no duplicate AP/client IDs"
# --------------------------------------------------------------------------- #
async def test_ap_and_client_with_same_mac_get_separate_trackers(hass, make_entry, patch_api):
    await _setup(hass, make_entry())
    reg = er.async_get(hass)
    ap = reg.async_get_entity_id("device_tracker", DOMAIN, _uid("ap", SHARED_MAC))
    client = reg.async_get_entity_id("device_tracker", DOMAIN, _uid("client", SHARED_MAC))
    assert ap and client and ap != client
    assert hass.states.get(ap) and hass.states.get(client)


async def test_shared_description_keys_do_not_collide(hass, make_entry, patch_api):
    """`downloaded`, `rx`, `uptime` ... exist for both APs and clients."""
    await _setup(hass, make_entry())
    reg = er.async_get(hass)
    for key in ("downloaded", "uploaded", "rx", "tx", "uptime"):
        ap = reg.async_get_entity_id("sensor", DOMAIN, _uid("ap", SHARED_MAC, key))
        client = reg.async_get_entity_id("sensor", DOMAIN, _uid("client", SHARED_MAC, key))
        assert ap and client and ap != client, key


async def test_every_per_mac_entity_is_namespaced(hass, make_entry, patch_api):
    entry = make_entry()
    await _setup(hass, entry)
    reg = er.async_get(hass)
    entries = er.async_entries_for_config_entry(reg, entry.entry_id)
    assert entries
    per_mac = [e for e in entries if not e.unique_id.endswith("-ctrl-1")]  # skip controller-level
    assert per_mac
    for e in per_mac:
        parsed = parse_unique_id(e.unique_id)
        assert parsed, e.unique_id
        assert parsed.namespace in ("ap", "client", "update"), e.unique_id
        assert parsed.site_id == SITE_ID
    # namespaces line up with what the entity is
    assert {parse_unique_id(e.unique_id).namespace for e in entries if e.domain == "update"} == {"update"}
    for e in entries:
        if e.domain == "device_tracker":
            assert parse_unique_id(e.unique_id).key is None  # ТЗ 6.2: ap:<site>:<mac>


async def test_ap_and_client_are_separate_devices(hass, make_entry, patch_api):
    entry = make_entry()
    await _setup(hass, entry)
    dev_reg = dr.async_get(hass)

    def dev(ns, mac):
        return dev_reg.async_get_device_by_identifier((DOMAIN, _uid(ns, mac)), entry.entry_id)

    ap_dev = dev("ap", SHARED_MAC)
    client_dev = dev("client", SHARED_MAC)
    assert ap_dev and client_dev and ap_dev.id != client_dev.id
    # AP keeps its MAC connection (needed later for AP -> HA Area resolution) ...
    assert (CONNECTION_NETWORK_MAC, dr.format_mac(SHARED_MAC)) in ap_dev.connections
    # ... the colliding client does not steal it; ordinary clients still carry theirs.
    assert not client_dev.connections
    phone = dev("client", GUEST_PHONE)
    assert (CONNECTION_NETWORK_MAC, dr.format_mac(GUEST_PHONE)) in phone.connections


# --------------------------------------------------------------------------- #
# Acceptance: no deprecated ScannerEntity / default_name warnings
# --------------------------------------------------------------------------- #
async def test_no_deprecation_or_unique_id_warnings(hass, make_entry, patch_api, caplog):
    await _setup(hass, make_entry())
    noisy = [
        r.getMessage() for r in caplog.records
        if r.levelname in ("WARNING", "ERROR")
        and any(t in r.getMessage().lower() for t in
                ("deprecat", "default_name", "unique id", "does not generate", "will stop working"))
    ]
    assert noisy == []


def test_scanner_entity_is_imported_from_supported_location():
    src = (INTEGRATION_DIR / "device_tracker.py").read_text()
    assert "device_tracker.config_entry" not in src  # deprecated alias, removed in HA 2027.6
    assert "device_tracker.const" not in src
    assert "from homeassistant.components.device_tracker import" in src


def test_no_default_name_in_source():
    for path in INTEGRATION_DIR.glob("*.py"):
        text = path.read_text()
        assert "default_name=" not in text, path.name  # removed in HA 2027.9
        assert "default_manufacturer=" not in text and "default_model=" not in text, path.name


# --------------------------------------------------------------------------- #
# Acceptance: "current AP/client data continue to arrive"
# --------------------------------------------------------------------------- #
async def test_ap_and_client_data_flow_through(hass, make_entry, patch_api):
    await _setup(hass, make_entry())
    assert hass.states.get("sensor.wf06_clients").state == "2"
    assert hass.states.get("sensor.guest_phone_rssi").state == "-55"
    phone = hass.states.get("device_tracker.guest_phone")
    assert phone.state == "home"
    assert phone.attributes["ap_mac"] == dr.format_mac(AP_WF06)
    assert phone.attributes["ssid"] == "Guest"
    assert hass.states.get("device_tracker.wf06").state == "home"  # AP online


async def test_ap_tracker_goes_not_home_when_ap_is_offline(hass, make_entry, patch_api):
    from .fakes import ap_raw
    controller = await _setup(hass, make_entry())
    api = patch_api
    aps = [ap_raw(AP_WF06, "WF06", status_category=2), ap_raw(AP_WF07, "WF07")]
    api.set_data(aps, list(api.responses["/clients"]["data"]), list(api.responses["/insight/clients"]["data"]))
    await controller.async_update()
    await hass.async_block_till_done()
    assert hass.states.get("device_tracker.wf06").state == "not_home"
    assert hass.states.get("device_tracker.wf07").state == "home"


# --------------------------------------------------------------------------- #
# Acceptance: "controller reconnect works"
# --------------------------------------------------------------------------- #
async def test_controller_relogin_after_token_expiry(hass, make_entry, patch_api):
    controller = await _setup(hass, make_entry())
    patch_api.raise_on_status = [LoginRequired()]
    logins = patch_api.login_calls
    await controller.async_update()
    assert patch_api.login_calls == logins + 1
    assert controller.available is True


async def test_controller_outage_and_recovery(hass, make_entry, patch_api):
    """Outage keeps last-known states (no mass flip); recovery applies fresh data."""
    controller = await _setup(hass, make_entry())
    phone = "device_tracker.guest_phone"
    assert hass.states.get(phone).state == "home"

    patch_api.raise_on_status = [RequestError("u", "down"), RequestError("u", "down")]
    await controller.async_update()
    await hass.async_block_till_done()
    assert controller.available is False
    # Current upstream behaviour (kept in Phase 0): entity availability is fixed at
    # creation, so trackers hold their last state through an outage. This matches
    # ТЗ 22 (no mass OFFLINE) but is NOT a substitute for the Phase 1 outage handling.
    assert hass.states.get(phone).state == "home"

    # Controller comes back and the phone has meanwhile left an hour ago.
    clients = [c for c in patch_api.responses["/clients"]["data"] if c["mac"] != GUEST_PHONE]
    known = [known_raw(GUEST_PHONE, "Guest-Phone", last_seen_ms=int((time.time() - 3600) * 1000)) if k["mac"] == GUEST_PHONE else k
             for k in patch_api.responses["/insight/clients"]["data"]]
    patch_api.set_data(patch_api.responses["/devices"], clients, known)
    await controller.async_update()
    await hass.async_block_till_done()
    assert controller.available is True
    assert hass.states.get(phone).state == "not_home"


# --------------------------------------------------------------------------- #
# (no migration section: fresh domain)
# --------------------------------------------------------------------------- #






# --------------------------------------------------------------------------- #
# Characterization of existing behaviour that Phase 0 must not change
# --------------------------------------------------------------------------- #
async def test_offline_known_client_is_restored_from_registry(hass, make_entry, patch_api):
    """Registry entry of a known-but-disconnected client is restored."""
    entry = make_entry()
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    e = reg.async_get_or_create("device_tracker", DOMAIN, _uid("client", OFFLINE_CLIENT),
                                suggested_object_id="offline_phone", config_entry=entry)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(e.entity_id)
    assert state is not None and state.state == "not_home"  # last seen 1h ago > 5 min timeout


async def test_recently_seen_client_stays_home_within_disconnect_timeout(hass, make_entry, patch_api):
    api = patch_api
    known = list(api.responses["/insight/clients"]["data"])
    known.append(known_raw(OFFLINE_CLIENT, "just-left", last_seen_ms=int((time.time() - 120) * 1000)))
    api.set_data(api.responses["/devices"], list(api.responses["/clients"]["data"]), known)
    entry = make_entry()
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    e = reg.async_get_or_create("device_tracker", DOMAIN, _uid("client", OFFLINE_CLIENT),
                                suggested_object_id="just_left", config_entry=entry)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(e.entity_id).state == "home"  # 2 min < 5 min timeout


async def test_stale_client_entities_are_cleaned_up(hass, make_entry, patch_api):
    entry = make_entry()
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    gone = "FF-EE-DD-CC-BB-AA"  # not in known_clients any more
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, _uid("client", gone))}, name="gone")
    e = reg.async_get_or_create("device_tracker", DOMAIN, _uid("client", gone),
                                config_entry=entry, device_id=device.id)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert reg.async_get(e.entity_id) is None


async def test_ssid_filter_and_wired_clients(hass, make_entry, patch_api):
    controller = await _setup(hass, make_entry())
    assert controller.is_client_allowed(GUEST_PHONE) is True          # no filter
    assert controller.is_client_allowed(WIRED_PC) is True             # wired always allowed
    controller.option_ssid_filter = {"Staff"}
    assert controller.is_client_allowed(GUEST_PHONE) is False         # wrong SSID
    assert controller.is_client_allowed(WIRED_PC) is True
    assert controller.is_client_allowed(OFFLINE_CLIENT) is False      # not currently connected
