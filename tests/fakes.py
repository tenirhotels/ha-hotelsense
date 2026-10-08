"""Fake Omada controller behind the *real* ``tplink-omada-client`` model classes.

Only the controller is replaced: canned JSON (shaped like the Omada API)
goes through the library's own device / client classes and then through
Hotel Sense's ``omada_hub`` parsing, so tests exercise the production path.
"""
from __future__ import annotations

import time
from typing import Any

from awesomeversion import AwesomeVersion
from tplink_omada_client.exceptions import RequestFailed
from tplink_omada_client.clients import OmadaWiredClient, OmadaWirelessClient
from tplink_omada_client.devices import OmadaListDevice

SITE_ID = "site-key-1"
SITE_NAME = "Test Site"

# Real-world shaped MACs in Omada notation (upper-case, dashes).
AP_WF06 = "AA-AA-AA-00-00-06"
AP_WF07 = "AA-AA-AA-00-00-07"
GUEST_PHONE = "02-00-00-00-00-01"  # guest phone on WF06
OFFLINE_CLIENT = "02-00-00-00-00-02"  # known, not currently connected
WIRED_PC = "10-20-30-40-50-60"
# Collision case: a MAC that is BOTH an access point and a client.
SHARED_MAC = AP_WF07


def ap_raw(mac: str, name: str, *, status_category: int = 1) -> dict[str, Any]:
    return {
        "mac": mac,
        "name": name,
        "type": "ap",
        "compoundModel": "EAP225(EU) v4.0",
        "firmwareVersion": "5.1.0",
        "status": 14,
        "statusCategory": status_category,
        "uptimeLong": 3600,
        "needUpgrade": False,
        "clientNum": 2,
        "guestNum": 1,
        "userNum": 1,
        "download": 1000,
        "upload": 500,
        "ip": "192.168.0.10",
    }


def client_raw(mac: str, name: str, *, ap_mac: str = AP_WF06, ssid: str = "Guest",
               wireless: bool = True, connect_type: int = 1) -> dict[str, Any]:
    raw = {
        "mac": mac,
        "name": name,
        "hostName": name,
        "ip": "192.168.0.50",
        "connectType": connect_type,
        "wireless": wireless,
        "guest": False,
        "active": True,
        "trafficDown": 2048,
        "trafficUp": 1024,
        "uptime": 600,
    }
    if wireless:
        raw.update({"ssid": ssid, "apMac": ap_mac, "apName": "WF06", "rssi": -55,
                    "snr": 30, "channel": 36, "radioId": 1, "wifiMode": 5,
                    "rxRate": 866000, "txRate": 866000, "powerSave": False})
    return raw


def known_raw(mac: str, name: str, *, last_seen_ms: int | None = None,
              wireless: bool = True) -> dict[str, Any]:
    return {
        "mac": mac, "name": name, "wireless": wireless, "guest": False,
        "block": False, "download": 10, "upload": 5, "duration": 100,
        "lastSeen": last_seen_ms if last_seen_ms is not None else int(time.time() * 1000),
    }


class FakeApi:
    """The Omada controller: the library's OmadaApiConnection and the site client
    on top of it (``OmadaSiteClient(site_id, api)`` returns this object too)."""

    controller_id = "ctrl-1"
    version = "6.3.0.45"
    name = "Omada OC200"

    def __init__(self, aps, clients, known=None):
        self.responses: dict[str, Any] = {}
        self.set_data(aps, clients, known)
        # failure injection / call accounting
        self.raise_on_status: list[Exception] = []  # raised by the next polls
        self.raise_on_login: list[Exception] = []
        self.raise_on_command: list[Exception] = []
        self.login_calls = 0
        self.poll_calls = 0
        self.sites = [{"name": SITE_NAME, "key": SITE_ID}]
        self.commands: list[tuple[str, str]] = []  # (command, mac)
        self.patches: list[tuple[str, dict]] = []  # (url, json)
        # Per access point: Omada's SSID overrides (one entry per site SSID).
        self.ap_details: dict[str, dict] = {
            raw["mac"]: {"wlanId": "wlan-1", "ssidOverrides": [
                {"index": 0, "globalSsid": "Guest", "ssidEnable": True, "supportVlan": False},
                {"index": 1, "globalSsid": "Staff", "ssidEnable": True, "supportVlan": False},
            ]} for raw in aps}

    def set_data(self, aps, clients, known=None) -> None:
        """``known``: Omada's known-client list (``omada_known_devices``)."""
        self.responses = {"/devices": aps, "/clients": {"data": clients},
                          "/insight/clients": {"data": known or []}}

    # -- OmadaApiConnection -------------------------------------------------- #
    def __call__(self, url, username, password, websession=None, verify_ssl=True):
        self.url, self.verify_ssl = url, verify_ssl
        return self

    async def login(self) -> str:
        self.login_calls += 1
        if self.raise_on_login:
            raise self.raise_on_login.pop(0)
        return self.controller_id

    async def get_controller_version(self) -> AwesomeVersion:
        return AwesomeVersion(self.version)

    def format_url(self, end_point: str, site: str | None = None) -> str:
        if site:
            end_point = f"sites/{site}/{end_point}"
        return f"{self.url}/{self.controller_id}/api/v2/{end_point}"

    async def iterate_pages(self, url, params=None):
        path = url.split("/api/v2/", 1)[1]
        assert path == f"sites/{SITE_ID}/insight/clients", path
        if self.raise_on_command:
            raise self.raise_on_command.pop(0)
        for raw in self.responses["/insight/clients"]["data"]:
            yield dict(raw)

    async def request(self, method, url, params=None, json=None, data=None):
        path = url.split("/api/v2/", 1)[1]
        if path == "maintenance/uiInterface":
            return {"controllerName": self.name}
        if path == "users/current":
            return {"privilege": {"sites": list(self.sites)}}
        if path.startswith(f"sites/{SITE_ID}/clients/") and path.count("/") == 3:
            mac = path.rsplit("/", 1)[1]
            raw = next((c for c in self.responses["/clients"]["data"] if c["mac"] == mac), None)
            if raw is None:
                raise RequestFailed(-41011, "client not found")
            return dict(raw)
        if path.startswith(f"sites/{SITE_ID}/") and ("insight/past" in path
                                                     or path.endswith("/history")):
            raise RequestFailed(-1, "unsupported")
        if path.startswith(f"sites/{SITE_ID}/eaps/"):
            mac = path.rsplit("/", 1)[1]
            if self.raise_on_command:
                raise self.raise_on_command.pop(0)
            if method.lower() == "get":
                return {"mac": mac, **{k: [dict(o) for o in v] if isinstance(v, list) else v
                                       for k, v in self.ap_details[mac].items()}}
            self.patches.append((path, json))
            self.ap_details[mac].update(json)
            return {}
        raise AssertionError(f"unexpected request {method} {path}")

    # -- OmadaSiteClient ----------------------------------------------------- #
    async def get_devices(self) -> list[OmadaListDevice]:
        self.poll_calls += 1
        if self.raise_on_status:
            raise self.raise_on_status.pop(0)
        return [OmadaListDevice(dict(raw)) for raw in self.responses["/devices"]]

    async def get_connected_clients(self):
        for raw in self.responses["/clients"]["data"]:
            if raw.get("wireless"):
                yield OmadaWirelessClient(dict(raw))
            else:
                yield OmadaWiredClient(dict(raw))

    async def _command(self, name: str, mac: str) -> None:
        if self.raise_on_command:
            raise self.raise_on_command.pop(0)
        self.commands.append((name, mac))

    async def reconnect_client(self, mac: str) -> None:
        await self._command("reconnect", mac)

    async def block_client(self, mac: str) -> None:
        await self._command("block", mac)

    async def unblock_client(self, mac: str) -> None:
        await self._command("unblock", mac)


def default_api() -> FakeApi:
    """Two APs (WF06, WF07), a guest phone on WF06, a wired PC, an offline known
    client, and SHARED_MAC (== WF07) that ALSO appears as a wireless client."""
    aps = [ap_raw(AP_WF06, "WF06"), ap_raw(AP_WF07, "WF07")]
    clients = [
        client_raw(GUEST_PHONE, "Guest-Phone"),
        client_raw(WIRED_PC, "office-pc", wireless=False, connect_type=2),
        client_raw(SHARED_MAC, "wf07-as-client"),
    ]
    known = [
        known_raw(GUEST_PHONE, "Guest-Phone"),
        known_raw(WIRED_PC, "office-pc", wireless=False),
        known_raw(SHARED_MAC, "wf07-as-client"),
        known_raw(OFFLINE_CLIENT, "Offline-Phone",
                  last_seen_ms=int((time.time() - 3600) * 1000)),
    ]
    return FakeApi(aps, clients, known)
