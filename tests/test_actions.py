"""Device actions (reconnect / block / unblock) and SSIDs per access point."""
from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from tplink_omada_client.exceptions import RequestFailed

from custom_components.hotel_sense.const import DOMAIN

from .fakes import AP_WF06, AP_WF07, GUEST_PHONE
from .test_stage_a import _hotel


async def _call(hass, service, data, response=False):
    return await hass.services.async_call(DOMAIN, service, data, blocking=True,
                                          return_response=response)


@pytest.mark.parametrize("service", ["reconnect_client", "block_client", "unblock_client"])
async def test_client_commands(hass, make_entry, patch_api, service):
    await _hotel(hass, make_entry())
    await _call(hass, service, {"mac": "02:00:00:00:00:01"})  # any notation
    assert patch_api.commands == [(service.split("_")[0], GUEST_PHONE)]


async def test_client_command_errors(hass, make_entry, patch_api):
    await _hotel(hass, make_entry())
    with pytest.raises(ServiceValidationError):
        await _call(hass, "block_client", {"mac": "not-a-mac"})
    patch_api.raise_on_command = [RequestFailed(-1, "Operation forbidden")]
    with pytest.raises(HomeAssistantError, match="Omada controller: reconnect"):
        await _call(hass, "reconnect_client", {"mac": GUEST_PHONE})
    assert patch_api.commands == []


async def test_ap_ssids_report(hass, make_entry, patch_api):
    await _hotel(hass, make_entry())
    patch_api.ap_details[AP_WF07]["ssidOverrides"][0]["ssidEnable"] = False
    resp = await _call(hass, "ap_ssids", {}, response=True)
    rows = {r["name"]: r for r in resp["access_points"]}
    assert set(rows) == {"WF06", "WF07"}
    assert rows["WF06"]["area"] == "room_06"
    assert rows["WF07"]["ssids"] == [{"ssid": "Guest", "enabled": False},
                                     {"ssid": "Staff", "enabled": True}]
    one = await _call(hass, "ap_ssids", {"access_point": "Room 06"}, response=True)
    assert [r["mac"] for r in one["access_points"]] == [AP_WF06]


@pytest.mark.parametrize("target", [AP_WF06, "aa:aa:aa:00:00:06", "WF06", "wf06", "Room 06", "room_06"])
async def test_set_ap_ssid_by_mac_name_or_room(hass, make_entry, patch_api, target):
    await _hotel(hass, make_entry())
    resp = await _call(hass, "set_ap_ssid", {"access_point": target, "ssid": "Guest",
                                             "enabled": False}, response=True)
    assert resp == {"changed": [AP_WF06], "unchanged": []}
    (url, body), = patch_api.patches
    assert url.endswith(f"/eaps/{AP_WF06}")
    # The whole override list goes back; only this SSID's flag changes.
    assert body["wlanId"] == "wlan-1"
    assert body["ssidOverrides"] == [
        {"index": 0, "globalSsid": "Guest", "ssidEnable": False, "supportVlan": False},
        {"index": 1, "globalSsid": "Staff", "ssidEnable": True, "supportVlan": False}]


async def test_set_ap_ssid_unchanged_and_errors(hass, make_entry, patch_api):
    await _hotel(hass, make_entry())
    resp = await _call(hass, "set_ap_ssid", {"access_point": "WF06", "ssid": "Guest",
                                             "enabled": True}, response=True)
    assert resp == {"changed": [], "unchanged": [AP_WF06]} and patch_api.patches == []
    with pytest.raises(ServiceValidationError, match="not configured"):
        await _call(hass, "set_ap_ssid", {"access_point": "WF06", "ssid": "Nope", "enabled": False})
    with pytest.raises(ServiceValidationError, match="No access point"):
        await _call(hass, "set_ap_ssid", {"access_point": "Room 99", "ssid": "Guest", "enabled": False})
    patch_api.raise_on_command = [RequestFailed(-1, "Operation forbidden")]
    with pytest.raises(HomeAssistantError, match="setting SSID"):
        await _call(hass, "set_ap_ssid", {"access_point": "WF06", "ssid": "Guest", "enabled": False})
