"""Fake Omada controller built on the *real* API model classes.

Only the HTTP layer is replaced: canned JSON goes through the real
Clients / Devices / KnownClients parsing and polling code, so these tests
exercise the same code paths as production.
"""
from __future__ import annotations

import time
from typing import Any

from custom_components.hotel_sense.api.clients import Clients
from custom_components.hotel_sense.api.devices import Devices
from custom_components.hotel_sense.api.known_clients import KnownClients

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


class _NoDetailsDevices(Devices):
    async def update_details(self, key: str, item) -> None:  # no per-AP detail calls
        return None


class FakeApi:
    """Stand-in for api.controller.Controller."""

    controller_id = "ctrl-1"
    version = "5.2.2"
    site = SITE_NAME
    site_id = SITE_ID

    def __init__(self, aps, clients, known):
        self.name = "Omada OC200"
        self.ssids = {"Guest"}
        self.rf_planning = None
        self.responses: dict[str, Any] = {}
        self.set_data(aps, clients, known)
        self.devices = _NoDetailsDevices(self._request)
        self.clients = Clients(self._request)
        self.known_clients = KnownClients(self._request)
        # failure injection / call accounting
        self.raise_on_status: list[Exception] = []
        self.login_calls = 0
        self.status_calls = 0

    def set_data(self, aps, clients, known) -> None:
        self.responses = {
            "/devices": aps,
            "/clients": {"data": clients},
            "/insight/clients": {"data": known},
        }

    async def _request(self, method, end_point, params=None, json=None):
        return self.responses.get(end_point, {})

    async def update_status(self):
        self.status_calls += 1
        if self.raise_on_status:
            raise self.raise_on_status.pop(0)

    async def login(self):
        self.login_calls += 1


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
