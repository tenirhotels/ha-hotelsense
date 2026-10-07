"""End to end through the real ``tplink-omada-client`` HTTP code, against a
simulated Omada 6.3 controller (the version at the hotel): login, site lookup,
device list and the OpenAPI v2 client list used from 6.2 on."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState

from custom_components.hotel_sense.const import DOMAIN

from .fakes import AP_WF06, AP_WF07, GUEST_PHONE, SITE_ID, SITE_NAME, WIRED_PC, ap_raw, client_raw

URL = "https://omada.test:8043"
CID = "ctrl-1"


def _ok(result):
    return {"errorCode": 0, "msg": "Success.", "result": result}


class _Json:
    """aioclient_mock answering with the JSON content type, like the controller."""

    def __init__(self, mock):
        self._mock = mock

    def __getattr__(self, method):
        return lambda url, **kw: getattr(self._mock, method)(
            url, headers={"Content-Type": "application/json"}, **kw)


def _controller(mock, clients):
    api = f"{URL}/{CID}/api/v2"
    aioclient_mock = _Json(mock)
    aioclient_mock.get(f"{URL}/api/info", json=_ok({"controllerVer": "6.3.0.45", "omadacId": CID}))
    aioclient_mock.post(f"{api}/login", json=_ok({"token": "csrf-token"}))
    aioclient_mock.get(f"{api}/maintenance/uiInterface", json=_ok({"controllerName": "Omada OC200"}))
    aioclient_mock.get(f"{api}/users/current", json=_ok(
        {"privilege": {"sites": [{"name": "Other", "key": "other-key"},
                                 {"name": SITE_NAME, "key": SITE_ID}]}}))
    aioclient_mock.get(f"{api}/sites/{SITE_ID}/devices",
                       json=_ok([ap_raw(AP_WF06, "WF06"), ap_raw(AP_WF07, "WF07")]))
    aioclient_mock.post(f"{URL}/openapi/v2/{CID}/sites/{SITE_ID}/clients", json=_ok(
        {"totalRows": len(clients), "currentPage": 1, "currentSize": 100, "data": clients}))


async def test_omada_6_3_end_to_end(hass, make_entry, aioclient_mock):
    clients = [client_raw(GUEST_PHONE, "Guest-Phone"),
               client_raw(WIRED_PC, "office-pc", wireless=False, connect_type=2)]
    _controller(aioclient_mock, clients)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    controller = hass.data[DOMAIN][entry.entry_id]
    assert (controller.controller_id, controller.site_id) == (CID, SITE_ID)
    assert controller.hub.version == "6.3.0.45"
    assert set(controller.access_points) == {AP_WF06, AP_WF07}
    phone = controller.clients[GUEST_PHONE]
    assert (phone.ap_mac, phone.ssid, phone.rssi, phone.wireless) == (AP_WF06, "Guest", -55, True)
    assert controller.clients[WIRED_PC].wireless is False

    # The client list is requested the 6.2+ way: POST, active clients only.
    calls = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/clients")]
    method, _, body, *_ = calls[-1]
    assert method.upper() == "POST" and body["filters"] == {"active": True}
    # Authenticated with the CSRF token from the login.
    assert calls[-1][3]["Csrf-Token"] == "csrf-token"
