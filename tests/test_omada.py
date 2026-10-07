"""Omada connection on ``tplink-omada-client`` (0.3): entities, IDs carried over
from 0.2, registry cleanup, outages, config flow."""
from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from tplink_omada_client.exceptions import (
    BadControllerUrl, ConnectionFailed, LoginFailed, RequestFailed, UnsupportedControllerVersion,
)

from custom_components.hotel_sense.const import CONF_SCAN_INTERVAL, CONF_SITE, DOMAIN, LEGACY_OPTIONS
from custom_components.hotel_sense.diagnostics import async_get_config_entry_diagnostics
from custom_components.hotel_sense.ids import make_unique_id
from custom_components.hotel_sense.room_entity import (
    MisplacedFixedDevicesSensor, RoomCountSensor,
)

from .fakes import (
    AP_WF06, AP_WF07, GUEST_PHONE, OFFLINE_CLIENT, SITE_ID, SITE_NAME, WIRED_PC, ap_raw,
)
from .test_stage_a import _hotel, _options_menu, _poll, _setup, _state

CID = "ctrl-1"


def _uid(*parts):
    return make_unique_id(*parts)


def _entities(hass, entry):
    return {e.unique_id: e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}


# --------------------------------------------------------------------------- #
# What exists
# --------------------------------------------------------------------------- #
async def test_entities_are_rooms_access_points_and_controller(hass, make_entry, patch_api):
    entry = make_entry()
    await _hotel(hass, entry)
    uids = set(_entities(hass, entry))
    for ap in (AP_WF06, AP_WF07):
        assert {_uid("ap", SITE_ID, ap, "uptime"), _uid("ap", SITE_ID, ap, "clients"),
                _uid("ap", SITE_ID, ap, "status")} <= uids
    assert {f"clients-{CID}", f"misplaced_devices-{CID}", f"exely_last_event-{CID}"} <= uids
    other = {u for u in uids if not u.startswith(("room:", "ap:"))} - {
        f"clients-{CID}", f"misplaced_devices-{CID}", f"exely_last_event-{CID}"}
    assert not other
    # No per-client entities or devices any more.
    assert not [u for u in uids if u.startswith(("client:", "update:"))]
    domains = {e.domain for e in _entities(hass, entry).values()}
    assert domains == {"sensor", "binary_sensor", "select"}

    assert _state(hass, "sensor.wf06_clients") == "2"
    assert _state(hass, "binary_sensor.wf06_status") == "on"
    assert _state(hass, "sensor.omada_oc200_clients") == "3"
    assert hass.states.get("sensor.wf06_uptime").attributes["device_class"] == "timestamp"


async def test_access_point_devices(hass, make_entry, patch_api):
    entry = make_entry()
    await _setup(hass, entry)
    reg = dr.async_get(hass)
    device = reg.async_get_device_by_identifier((DOMAIN, _uid("ap", SITE_ID, AP_WF06)), entry.entry_id)
    assert device is not None
    assert device.name == "WF06" and device.model == "EAP225(EU) v4.0"
    assert device.sw_version == "5.1.0" and device.manufacturer == "TP-Link"
    assert (dr.CONNECTION_NETWORK_MAC, "aa:aa:aa:00:00:06") in device.connections
    controller = reg.async_get_device_by_identifier((DOMAIN, CID), entry.entry_id)
    assert controller.name == "Omada OC200" and controller.sw_version == "6.3.0.45"


# --------------------------------------------------------------------------- #
# Upgrade from 0.2 (ha-omada based): same IDs, Areas kept, old entities removed
# --------------------------------------------------------------------------- #
async def test_upgrade_from_0_2_keeps_ap_areas_and_entity_ids(hass, make_entry, patch_api):
    entry = make_entry({"track_clients": False, "scan_interval_details": 600})
    entry.add_to_hass(hass)
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
    room = ar.async_get(hass).async_create("Room 06")

    def device(uid, **kw):
        return dev_reg.async_get_or_create(config_entry_id=entry.entry_id,
                                           identifiers={(DOMAIN, uid)}, **kw)

    def entity(domain, uid, dev, object_id):
        return ent_reg.async_get_or_create(domain, DOMAIN, uid, config_entry=entry,
                                           device_id=dev.id, suggested_object_id=object_id)

    # What 0.2 left in the registry.
    ap = device(_uid("ap", SITE_ID, AP_WF06), name="WF06")
    dev_reg.async_update_device(ap.id, area_id=room.id)
    switch = device(_uid("ap", SITE_ID, "AA-AA-AA-00-00-50"), name="SW01")
    phone = device(_uid("client", SITE_ID, GUEST_PHONE), name="Guest-Phone")
    ctrl = device(CID, name="Omada OC200")
    kept = {
        "sensor.wf06_uptime": entity("sensor", _uid("ap", SITE_ID, AP_WF06, "uptime"), ap, "wf06_uptime"),
        "sensor.wf06_clients": entity("sensor", _uid("ap", SITE_ID, AP_WF06, "clients"), ap, "wf06_clients"),
        "sensor.omada_oc200_clients": entity("sensor", f"clients-{CID}", ctrl, "omada_oc200_clients"),
    }
    removed = [
        entity("device_tracker", _uid("ap", SITE_ID, AP_WF06), ap, "wf06"),
        entity("update", _uid("update", SITE_ID, AP_WF06), ap, "wf06_firmware_update"),
        entity("sensor", _uid("ap", SITE_ID, AP_WF06, "cpu_usage"), ap, "wf06_cpu_usage"),
        entity("device_tracker", _uid("ap", SITE_ID, "AA-AA-AA-00-00-50"), switch, "sw01"),
        entity("device_tracker", _uid("client", SITE_ID, GUEST_PHONE), phone, "guest_phone"),
        entity("switch", _uid("client", SITE_ID, GUEST_PHONE, "block"), phone, "guest_phone_block"),
        entity("button", f"ai_optimization-{CID}", ctrl, "start_wlan_optimization"),
    ]

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    for entity_id, old in kept.items():
        assert ent_reg.async_get(entity_id) is not None, entity_id
        assert ent_reg.async_get(entity_id).unique_id == old.unique_id
        assert hass.states.get(entity_id).state not in ("unavailable", None), entity_id
    for old in removed:
        assert ent_reg.async_get(old.entity_id) is None, old.entity_id

    # The access point is the same device, still in its Area: rooms keep working.
    ap_now = dev_reg.async_get_device_by_identifier((DOMAIN, _uid("ap", SITE_ID, AP_WF06)),
                                                    entry.entry_id)
    assert ap_now.id == ap.id and ap_now.area_id == room.id
    assert _state(hass, "sensor.room_06_guest_devices") == "2"
    # Non-AP and client devices are gone.
    assert dev_reg.async_get(switch.id) is None
    assert dev_reg.async_get(phone.id) is None


async def test_legacy_options_are_dropped_when_options_are_saved(hass, make_entry, patch_api):
    entry = make_entry({"track_clients": True, "scan_interval_details": 600,
                        "enable_device_controls": True, CONF_SCAN_INTERVAL: 30})
    await _setup(hass, entry)
    result = await _options_menu(hass, entry, "device_tracker")
    assert set(result["data_schema"].schema) == {CONF_SCAN_INTERVAL}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 15})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL] == 15
    assert not set(entry.options) & LEGACY_OPTIONS
    await hass.async_block_till_done()
    controller = hass.data[DOMAIN][entry.entry_id]
    assert controller.option_scan_interval == 15  # reloaded with the new interval


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #
async def test_controller_outage_and_recovery(hass, make_entry, patch_api, freezer):
    controller = await _hotel(hass, make_entry())
    patch_api.raise_on_status = [ConnectionFailed("down")]
    freezer.tick(timedelta(minutes=30))
    await _poll(hass, controller)
    assert controller.available is False
    assert _state(hass, "sensor.wf06_clients") == "unavailable"
    room = hass.states.get("sensor.room_06_state")
    assert room.state == "violation" and room.attributes["data_stale"] is True  # last picture

    await _poll(hass, controller)
    assert controller.available is True
    assert _state(hass, "sensor.wf06_clients") == "2"
    assert hass.states.get("sensor.room_06_state").attributes["data_stale"] is False


@pytest.mark.parametrize("error", [RequestFailed(500, "boom"), TimeoutError()])
async def test_any_poll_error_marks_unavailable(hass, make_entry, patch_api, error, caplog):
    controller = await _setup(hass, make_entry())
    patch_api.raise_on_status = [error]
    with caplog.at_level(logging.WARNING):
        await _poll(hass, controller)
    assert controller.available is False
    assert "Omada controller unreachable" in caplog.text


async def test_access_point_offline(hass, make_entry, patch_api):
    controller = await _setup(hass, make_entry())
    patch_api.responses["/devices"] = [ap_raw(AP_WF06, "WF06", status_category=0), ap_raw(AP_WF07, "WF07")]
    await _poll(hass, controller)
    assert _state(hass, "binary_sensor.wf06_status") == "off"
    assert _state(hass, "sensor.wf06_uptime") == "unknown"
    assert _state(hass, "binary_sensor.wf07_status") == "on"


async def test_uptime_is_steady_and_moves_on_reboot(hass, make_entry, patch_api, freezer):
    controller = await _setup(hass, make_entry())
    boot = _state(hass, "sensor.wf06_uptime")
    freezer.tick(timedelta(seconds=30))
    for raw in patch_api.responses["/devices"]:
        raw["uptimeLong"] += 31  # rounding: one second off
    await _poll(hass, controller)
    assert _state(hass, "sensor.wf06_uptime") == boot
    patch_api.responses["/devices"][0]["uptimeLong"] = 10  # rebooted
    await _poll(hass, controller)
    assert _state(hass, "sensor.wf06_uptime") != boot


async def test_new_access_point_gets_entities_on_the_next_poll(hass, make_entry, patch_api):
    controller = await _setup(hass, make_entry())
    new = "AA-AA-AA-00-00-08"
    patch_api.responses["/devices"].append(ap_raw(new, "WF08"))
    await _poll(hass, controller)
    assert _state(hass, "sensor.wf08_clients") == "2"


async def test_switches_and_gateways_are_not_access_points(hass, make_entry, patch_api):
    patch_api.responses["/devices"].append(
        ap_raw("AA-AA-AA-00-00-50", "SW01") | {"type": "switch"})
    controller = await _setup(hass, make_entry())
    assert set(controller.access_points) == {AP_WF06, AP_WF07}
    assert hass.states.get("sensor.sw01_clients") is None


async def test_departed_client_keeps_its_name_and_type(hass, make_entry, patch_api, freezer):
    controller = await _hotel(hass, make_entry())
    clients = patch_api.responses["/clients"]["data"]
    patch_api.responses["/clients"]["data"] = [c for c in clients if c["mac"] != GUEST_PHONE]
    freezer.tick(timedelta(minutes=1))
    await _poll(hass, controller)
    devices = hass.states.get("sensor.room_06_guest_devices").attributes["devices"]
    phone = next(d for d in devices if d["mac"] == GUEST_PHONE)
    assert phone["name"] == "Guest-Phone" and phone["connected"] is False


async def test_wired_and_unknown_clients(hass, make_entry, patch_api):
    controller = await _setup(hass, make_entry())
    assert WIRED_PC in controller.clients and not controller.clients[WIRED_PC].wireless
    assert OFFLINE_CLIENT not in controller.clients  # history is not read


def test_device_lists_are_not_recorded():
    """RSSI / last seen change every poll: keep them out of the recorder."""
    assert {"devices", "types"} <= RoomCountSensor._unrecorded_attributes
    assert "devices" in MisplacedFixedDevicesSensor._unrecorded_attributes


async def test_diagnostics_show_the_omada_connection(hass, make_entry, patch_api):
    entry = make_entry()
    await _setup(hass, entry)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["omada"] == {"controller_version": "6.3.0.45", "available": True,
                             "access_points": 2, "access_points_offline": 0,
                             "connected_clients": 3, "wireless_clients": 2}
    assert diag["entry"][CONF_PASSWORD] == "**REDACTED**"


async def test_unload_and_no_warnings(hass, make_entry, patch_api, caplog):
    entry = make_entry()
    with caplog.at_level(logging.WARNING):
        await _hotel(hass, entry)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING
                and "hotel_sense" in r.name], caplog.text
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN]


# --------------------------------------------------------------------------- #
# Setup / config flow
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("error", "state", "where"), [
    (LoginFailed(-30109, "bad password"), ConfigEntryState.SETUP_ERROR, "login"),
    (ConnectionFailed("down"), ConfigEntryState.SETUP_RETRY, "login"),
    (ConnectionFailed("down"), ConfigEntryState.SETUP_RETRY, "first poll"),
])
async def test_setup_errors(hass, make_entry, patch_api, error, state, where):
    if where == "login":
        patch_api.raise_on_login = [error]
    else:
        patch_api.raise_on_status = [error]
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is state


USER_INPUT = {CONF_URL: "https://192.0.2.4/", CONF_SITE: SITE_NAME, CONF_USERNAME: "u",
              CONF_PASSWORD: "p", CONF_VERIFY_SSL: False}


async def test_config_flow_creates_entry(hass, patch_api):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(USER_INPUT))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Omada OC200: {SITE_NAME}"
    assert result["data"][CONF_URL] == "https://192.0.2.4"
    assert patch_api.url == "https://192.0.2.4" and patch_api.verify_ssl is False
    await hass.async_block_till_done()


@pytest.mark.parametrize(("error", "key"), [
    (LoginFailed(-30109, "bad"), "faulty_credentials"),
    (BadControllerUrl("x"), "invalid_url"),
    (UnsupportedControllerVersion("4.4.0"), "unsupported_version"),
    (ConnectionFailed("down"), "service_unavailable"),
    (RequestFailed(500, "boom"), "api_error"),
])
async def test_config_flow_errors(hass, patch_api, error, key):
    patch_api.raise_on_login = [error]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(USER_INPUT))
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": key}


async def test_config_flow_unknown_site(hass, patch_api):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(USER_INPUT) | {CONF_SITE: "Nowhere"})
    assert result["errors"] == {"base": "unknown_site"}


async def test_reconfigure_keeps_exely_secrets(hass, make_entry, patch_api, caplog):
    """Changing the controller address must not invalidate the Exely webhook."""
    from custom_components.hotel_sense.const import CONF_EXELY_API_KEY, CONF_EXELY_WEBHOOK_ID

    entry = make_entry()
    await _setup(hass, entry)
    secrets = (entry.data[CONF_EXELY_WEBHOOK_ID], entry.data[CONF_EXELY_API_KEY])
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(USER_INPUT) | {CONF_URL: "https://192.0.2.9/"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_URL] == "https://192.0.2.9"
    assert (entry.data[CONF_EXELY_WEBHOOK_ID], entry.data[CONF_EXELY_API_KEY]) == secrets
    assert entry.state is ConfigEntryState.LOADED
    # Reloaded by the entry's update listener, with the new address.
    assert hass.data[DOMAIN][entry.entry_id].hub.url == "https://192.0.2.9"
    assert "Detected" not in caplog.text  # HA's deprecation reports


async def test_legacy_options_are_removed_on_startup(hass, make_entry, patch_api):
    """Options as found on the production install after the update to 0.3."""
    current = {"common_areas": ["admin_house"], "exely_room_map": "", "min_rssi": 0.0,
               "presence_timeout": 5.0, "roaming_debounce": 10.0, CONF_SCAN_INTERVAL: 15.0}
    legacy = {"enable_device_bandwidth_sensors": False, "enable_device_clients_sensors": False,
              "enable_device_controls": False, "enable_device_radio_utilization_sensors": False,
              "enable_device_statistics_sensors": False, "scan_interval_details": 600.0,
              "track_clients": False, "track_devices": True}
    entry = make_entry(current | legacy)
    controller = await _setup(hass, entry)
    assert dict(entry.options) == current
    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN][entry.entry_id] is controller  # not reloaded
    assert controller.option_scan_interval == 15
