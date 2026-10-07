"""Connection to the Omada controller via ``tplink-omada-client``.

The same library as Home Assistant's built-in TP-Link Omada integration
(MIT, maintained alongside it). This module turns it into two plain
snapshots per poll: the site's access points and its connected clients,
keyed by MAC in Omada notation (``AA-BB-CC-DD-EE-FF``).

Identifiers are those Omada itself uses, so unique IDs built from them are
the same as before the switch to the library: the controller ID is
``omadacId`` and the site ID is the site ``key``.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aiohttp import CookieJar
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from tplink_omada_client import OmadaSiteClient
from tplink_omada_client.omadaapiconnection import OmadaApiConnection
from tplink_omada_client.definitions import DeviceStatusCategory
from tplink_omada_client.exceptions import (
    BadControllerUrl,
    ConnectionFailed,
    LoginFailed,
    OmadaClientException,
    RequestFailed,
    SiteNotFound,
    UnsupportedControllerVersion,
)

from .mac import parse_mac

__all__ = [
    "AccessPoint", "BadControllerUrl", "ConnectedClient", "ConnectionFailed", "LoginFailed",
    "OmadaClientException", "OmadaHub", "RequestFailed", "SiteNotFound",
    "UnsupportedControllerVersion",
]

AP_TYPE = "ap"


@dataclass(frozen=True)
class AccessPoint:
    mac: str
    name: str
    model: str | None
    firmware: str | None
    online: bool
    uptime: int | None  # seconds
    clients: int


@dataclass(frozen=True)
class ConnectedClient:
    mac: str
    name: str | None
    wireless: bool
    ap_mac: str | None = None
    ssid: str | None = None
    rssi: int | None = None
    power_save: bool | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)


def _mac(value: Any) -> str | None:
    try:
        return parse_mac(value) if value else None
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def access_point_from(raw: Mapping[str, Any]) -> AccessPoint | None:
    """An access point from an entry of the site device list (others: None)."""
    mac = _mac(raw.get("mac"))
    if raw.get("type") != AP_TYPE or mac is None:
        return None
    return AccessPoint(
        mac=mac,
        name=raw.get("name") or mac,
        model=raw.get("compoundModel") or raw.get("showModel") or raw.get("model"),
        firmware=raw.get("firmwareVersion"),
        online=_int(raw.get("statusCategory")) == DeviceStatusCategory.CONNECTED,
        uptime=_int(raw.get("uptimeLong")),
        clients=_int(raw.get("clientNum")) or 0,
    )


def client_from(raw: Mapping[str, Any]) -> ConnectedClient | None:
    mac = _mac(raw.get("mac"))
    if mac is None:
        return None
    wireless = bool(raw.get("wireless"))
    return ConnectedClient(
        mac=mac,
        name=raw.get("name") or raw.get("hostName") or None,
        wireless=wireless,
        ap_mac=_mac(raw.get("apMac")) if wireless else None,
        ssid=raw.get("ssid") if wireless else None,
        rssi=_int(raw.get("rssi")) if wireless else None,
        power_save=raw.get("powerSave") if wireless else None,
        raw=dict(raw),
    )


class OmadaHub:
    """Login, site selection, polling and commands.

    Raises ``tplink_omada_client`` exceptions. Built on the library's connection
    (login, CSRF token, re-login) and site client; endpoints the library has no
    function for (SSID per access point) go through the same connection.
    """

    def __init__(self, hass: HomeAssistant, url: str, username: str, password: str,
                 site: str, verify_ssl: bool) -> None:
        self.url = url.rstrip("/")
        self.site_name = site
        # A dedicated session: the controller is usually addressed by IP, whose
        # cookies the shared session's (safe) cookie jar would refuse.
        session = aiohttp_client.async_create_clientsession(
            hass, verify_ssl=verify_ssl, cookie_jar=CookieJar(unsafe=True))
        self._api = OmadaApiConnection(self.url, username, password, websession=session,
                                       verify_ssl=verify_ssl)
        self._site: OmadaSiteClient | None = None
        self.controller_id: str = ""
        self.site_id: str = ""
        self.version: str | None = None
        self.name: str = "Omada Controller"

    async def async_connect(self) -> None:
        """Log in and select the site (by its display name, as configured)."""
        self.controller_id = await self._api.login()
        self.version = str(await self._api.get_controller_version())
        try:
            ui = await self._api.request("get", self._api.format_url("maintenance/uiInterface"))
            self.name = ui.get("controllerName") or self.name
        except OmadaClientException:
            pass  # cosmetic only
        user = await self._api.request("get", self._api.format_url("users/current"))
        sites = user.get("privilege", {}).get("sites", [])
        site = next((s for s in sites if s.get("name") == self.site_name), None)
        if site is None:
            raise SiteNotFound(f"Site '{self.site_name}' not found")
        self.site_id = site["key"]
        self._site = OmadaSiteClient(self.site_id, self._api)

    @property
    def site(self) -> OmadaSiteClient:
        assert self._site is not None, "async_connect() first"
        return self._site

    async def async_poll(self) -> tuple[dict[str, AccessPoint], dict[str, ConnectedClient]]:
        """Access points and connected clients of the site (re-logs in as needed)."""
        aps: dict[str, AccessPoint] = {}
        for device in await self.site.get_devices():
            if (ap := access_point_from(device.raw_data)) is not None:
                aps[ap.mac] = ap
        clients: dict[str, ConnectedClient] = {}
        async for client in self.site.get_connected_clients():
            if (item := client_from(client.raw_data)) is not None:
                clients[item.mac] = item
        return aps, clients

    # -- client commands ------------------------------------------------------ #
    async def async_reconnect_client(self, mac: str) -> None:
        await self.site.reconnect_client(mac)

    async def async_block_client(self, mac: str) -> None:
        await self.site.block_client(mac)

    async def async_unblock_client(self, mac: str) -> None:
        await self.site.unblock_client(mac)

    # -- SSIDs of an access point --------------------------------------------- #
    def _ap_url(self, ap_mac: str) -> str:
        return self._api.format_url(f"eaps/{ap_mac}", self.site_id)

    async def async_ap_ssids(self, ap_mac: str) -> list[dict[str, Any]]:
        """The site SSIDs as configured on one access point: name and enabled."""
        details = await self._api.request("get", self._ap_url(ap_mac))
        return [{"ssid": o.get("globalSsid"), "enabled": bool(o.get("ssidEnable", True))}
                for o in details.get("ssidOverrides") or []]

    async def async_set_ap_ssid(self, ap_mac: str, ssid: str, enabled: bool) -> bool:
        """Enable / disable ``ssid`` on one access point. Returns False if unchanged.

        Omada keeps one override entry per site SSID on each access point; the
        whole list is sent back with only ``ssidEnable`` of that SSID changed.
        """
        details = await self._api.request("get", self._ap_url(ap_mac))
        overrides = [dict(o) for o in details.get("ssidOverrides") or []]
        target = next((o for o in overrides if o.get("globalSsid") == ssid), None)
        if target is None:
            raise ValueError(f"SSID {ssid!r} is not configured on access point {ap_mac}")
        if bool(target.get("ssidEnable", True)) == enabled:
            return False
        target["ssidEnable"] = enabled
        await self._api.request("patch", self._ap_url(ap_mac), json={
            "wlanId": details.get("wlanId"), "ssidOverrides": overrides})
        return True
