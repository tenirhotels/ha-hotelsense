"""Unique-ID namespaces for Hotel Omada.

Access points (infrastructure / location sources) and Wi-Fi clients (observed
devices) are different domain objects and must never share an identifier, even
when the same MAC address shows up on both sides.

Format (Master ТЗ v1.0, section 6.2)::

    ap:<site_id>:<mac>[:<key>]        AP-scoped entity (tracker has no key)
    client:<site_id>:<mac>[:<key>]    client-scoped entity (tracker has no key)
    update:<site_id>:<mac>            AP firmware update entity
    room:<site_id>:<area_id>[:<key>]  room (HA Area) presence entity / device

``<mac>`` is always the Omada notation (upper-case, dash separated) so that
``:`` is an unambiguous separator. ``<key>`` is the entity description key
(``download``, ``rx``, ``ssid_<name>`` ...) and is free-form.

This module deliberately has no Home Assistant imports: it is pure logic that
is unit-tested in isolation, and it is the single place to change if the
identifier scheme is ever revised.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

NS_AP = "ap"
NS_CLIENT = "client"
NS_UPDATE = "update"
NS_ROOM = "room"  # keyed by HA area_id, not by MAC: see make_room_unique_id

NAMESPACES = (NS_AP, NS_CLIENT, NS_UPDATE)

_MAC = r"[0-9A-F]{2}(?:-[0-9A-F]{2}){5}"

_NEW_RE = re.compile(
    rf"^(?P<ns>{'|'.join(NAMESPACES)}):(?P<site>.+?):(?P<mac>{_MAC})(?::(?P<key>.+))?$",
    re.IGNORECASE,
)


def normalize_mac(mac: str) -> str:
    """Return the MAC in Omada notation: ``AA-BB-CC-DD-EE-FF``."""
    return mac.strip().upper().replace(":", "-")


@dataclass(frozen=True)
class ParsedId:
    """A decomposed unique ID."""

    namespace: str
    site_id: str
    mac: str
    key: str | None


def make_unique_id(namespace: str, site_id: str, mac: str, key: str | None = None) -> str:
    """Build a namespaced unique ID."""
    if namespace not in NAMESPACES:
        raise ValueError(f"Unknown unique-id namespace: {namespace!r}")
    if not site_id:
        raise ValueError("site_id is required")
    uid = f"{namespace}:{site_id}:{normalize_mac(mac)}"
    return f"{uid}:{key}" if key else uid


def parse_unique_id(unique_id: str) -> ParsedId | None:
    """Parse a unique ID. Returns None if unrecognised."""
    if not unique_id:
        return None

    if (m := _NEW_RE.match(unique_id)) is not None:
        return ParsedId(
            namespace=m["ns"].lower(),
            site_id=m["site"],
            mac=normalize_mac(m["mac"]),
            key=m["key"],
        )


    return None


def make_room_unique_id(site_id: str, area_id: str, key: str | None = None) -> str:
    """Unique ID of a room entity (``key``) or of the room device (no key)."""
    if not site_id or not area_id:
        raise ValueError("site_id and area_id are required")
    uid = f"{NS_ROOM}:{site_id}:{area_id}"
    return f"{uid}:{key}" if key else uid
