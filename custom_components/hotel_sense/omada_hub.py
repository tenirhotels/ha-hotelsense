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
from tplink_omada_client import OmadaClient, OmadaSite, OmadaSiteClient
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
    """Login, site selection and polling. Raises ``tplink_omada_client`` exceptions."""

    def __init__(self, hass: HomeAssistant, url: str, username: str, password: str,
                 site: str, verify_ssl: bool) -> None:
        self.url = url.rstrip("/")
        self.site_name = site
        # A dedicated session: the controller is usually addressed by IP, whose
        # cookies the shared session's (safe) cookie jar would refuse.
        session = aiohttp_client.async_create_clientsession(
            hass, verify_ssl=verify_ssl, cookie_jar=CookieJar(unsafe=True))
        self._client = OmadaClient(self.url, username, password, websession=session,
                                   verify_ssl=verify_ssl)
        self._site: OmadaSiteClient | None = None
        self.controller_id: str = ""
        self.site_id: str = ""
        self.version: str | None = None
        self.name: str = "Omada Controller"

    async def async_connect(self) -> None:
        """Log in and select the site (by its display name, as configured)."""
        self.controller_id = await self._client.login()
        self.version = str(await self._client.get_controller_version())
        try:
            self.name = await self._client.get_controller_name() or self.name
        except OmadaClientException:
            pass  # cosmetic only
        sites = await self._client.get_sites()
        site = next((s for s in sites if s.name == self.site_name), None)
        if site is None:
            raise SiteNotFound(f"Site '{self.site_name}' not found")
        self.site_id = site.id
        self._site = await self._client.get_site_client(OmadaSite(site.name, site.id))

    async def async_poll(self) -> tuple[dict[str, AccessPoint], dict[str, ConnectedClient]]:
        """Access points and connected clients of the site (re-logs in as needed)."""
        assert self._site is not None, "async_connect() first"
        aps: dict[str, AccessPoint] = {}
        for device in await self._site.get_devices():
            if (ap := access_point_from(device.raw_data)) is not None:
                aps[ap.mac] = ap
        clients: dict[str, ConnectedClient] = {}
        async for client in self._site.get_connected_clients():
            if (item := client_from(client.raw_data)) is not None:
                clients[item.mac] = item
        return aps, clients
