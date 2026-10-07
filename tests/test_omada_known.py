"""Staff devices from Omada's known-client list (omada_known_devices)."""
from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError
from tplink_omada_client.exceptions import RequestFailed

from custom_components.hotel_sense.const import DOMAIN
from custom_components.hotel_sense.omada_hub import KnownClient, known_client_from
from custom_components.hotel_sense.omada_known import omada_candidates

from .test_stage_a import _hotel

NOW = 1_800_000_000.0
DAY = 86400
STAFF, OLD_STAFF, GUEST, PRINTER = ("02-00-00-00-00-5A", "02-00-00-00-00-5B",
                                    "02-00-00-00-00-5C", "00-11-22-00-00-5D")


def _raw(mac, name, hours, last_seen_days_ago, *, wireless=True, **extra):
    return {"mac": mac, "name": name, "wireless": wireless, "guest": False,
            "duration": int(hours * 3600), "download": 12_300_000_000, "upload": 1_000_000_000,
            "lastSeen": int((NOW - last_seen_days_ago * DAY) * 1000), **extra}


KNOWN = [
    _raw(STAFF, "Housekeeping-1", 1500, 0.1),
    _raw(OLD_STAFF, "Left-in-spring", 3000, 200),  # not seen for months
    _raw(GUEST, GUEST.replace("-", ":"), 60, 1),  # Omada shows the MAC as the name
    _raw(PRINTER, "Office printer", 9000, 0, wireless=False),
]


def test_known_client_from_raw():
    client = known_client_from(_raw(STAFF, "Housekeeping-1", 2, 0, firstSeen=1_700_000_000_000))
    assert client == KnownClient(
        mac=STAFF, name="Housekeeping-1", wireless=True, guest=False, duration=7200,
        download=12_300_000_000, upload=1_000_000_000, last_seen=NOW, first_seen=1_700_000_000.0)
    assert known_client_from({"mac": "nope"}) is None
    assert known_client_from({"mac": STAFF.lower()}).last_seen is None


def test_candidates():
    clients = [known_client_from(r) for r in KNOWN]
    result = omada_candidates(clients, known=(), now=NOW)
    assert [c["mac"] for c in result["candidates"]] == [STAFF]
    staff = result["candidates"][0]
    assert staff["omada_name"] == "Housekeeping-1" and staff["hours"] == 1500
    assert staff["download_gb"] == 12.3 and staff["random_mac"] is True
    assert staff["first_seen"] is None
    # Wi-Fi clients only (3), spread by total hours.
    assert result["clients"] == 3
    assert result["hours_spread"] == {"under_24h": 0, "1_to_5_days": 1, "5_to_21_days": 0,
                                      "21_to_83_days": 1, "over_83_days": 1}
    assert result["import_csv"].splitlines() == [
        "mac,name,category,note",
        f"{STAFF},Housekeeping-1,employee,Omada: 1500 h on Wi-Fi; last seen 2027-01-15"]


def test_candidate_options():
    clients = [known_client_from(r) for r in KNOWN]
    assert omada_candidates(clients, known={STAFF}, now=NOW)["candidates"] == []
    wider = omada_candidates(clients, known=(), now=NOW, min_hours=50, seen_days=365, wired=True)
    found = {c["mac"]: c for c in wider["candidates"]}
    assert list(found) == [PRINTER, OLD_STAFF, STAFF, GUEST]  # most hours first
    assert found[GUEST]["omada_name"] is None  # a MAC is not a name
    assert f"{GUEST},,employee," in wider["import_csv"]
    assert omada_candidates([], known=(), now=NOW)["import_csv"] == ""


async def test_omada_known_devices_action(hass, make_entry, patch_api):
    await _hotel(hass, make_entry())
    patch_api.responses["/insight/clients"]["data"] = [
        _raw(STAFF, "Housekeeping-1", 1500, 0)]
    patch_api.responses["/insight/clients"]["data"][0]["lastSeen"] = int(
        __import__("time").time() * 1000)
    result = await hass.services.async_call(DOMAIN, "omada_known_devices", {}, blocking=True,
                                            return_response=True)
    assert [c["mac"] for c in result["candidates"]] == [STAFF]
    # Once imported, the device is no longer a candidate.
    await hass.services.async_call(DOMAIN, "import_devices", {"csv": result["import_csv"]},
                                   blocking=True, return_response=True)
    again = await hass.services.async_call(DOMAIN, "omada_known_devices", {}, blocking=True,
                                           return_response=True)
    assert again["candidates"] == [] and again["clients"] == 1

    patch_api.raise_on_command.append(RequestFailed(500, "boom"))
    with pytest.raises(HomeAssistantError, match="known clients"):
        await hass.services.async_call(DOMAIN, "omada_known_devices", {}, blocking=True,
                                       return_response=True)
