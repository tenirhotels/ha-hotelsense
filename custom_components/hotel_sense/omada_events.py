"""Omada webhook messages -> Wi-Fi events (online / offline / roaming).

The controller posts ``{"Site", "description", "text": [message, ...]}`` with
messages such as::

    [client:<name>:<MAC>] (IP: ...) went online on [ap:<AP>:<MAC>] with SSID "X"
    [client:<name>:<MAC>] (IP: ...) went offline from SSID "X" on [ap:<AP>:<MAC>] (10s connected, 168.98KB).
    [client:<name>:<MAC>] is roaming from [ap:<AP>:<MAC>][Channel 36] to [ap:<AP>:<MAC>][Channel 1] with SSID "X"

Only MACs, the SSID, the duration and the traffic are kept: client names
(often the owner's name) and IP addresses are dropped.

No Home Assistant imports: pure logic, unit-tested in isolation.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_MAC = r"[0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5}"
_CLIENT = re.compile(rf"\[client:(?:[^\]]*:)?({_MAC})\]")
_AP = re.compile(rf"\[ap:(?:[^\]]*:)?({_MAC})\]")
_SSID = re.compile(r'SSID\s+"([^"]*)"')
_DURATION = re.compile(r"\((?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?\s*connected", re.I)
_TRAFFIC = re.compile(r"connected,\s*([\d.]+)\s*([KMGT]?B)\b", re.I)
_UNITS = {"B": 1 / 1024, "KB": 1, "MB": 1024, "GB": 1024 ** 2, "TB": 1024 ** 3}

EVENT_ONLINE = "online"
EVENT_OFFLINE = "offline"
EVENT_ROAMING = "roaming"


@dataclass(frozen=True)
class WifiEvent:
    event: str
    client_mac: str
    ap_mac: str | None = None        # online / offline: the AP; roaming: the new AP
    from_ap_mac: str | None = None   # roaming only
    ssid: str | None = None
    connected_seconds: int | None = None
    traffic_kb: float | None = None


def _mac(value: str) -> str:
    return value.upper().replace(":", "-")


def parse_message(text: str) -> WifiEvent | None:
    if not isinstance(text, str) or not (client := _CLIENT.search(text)):
        return None
    lowered = text.lower()
    if "is roaming from" in lowered:
        event = EVENT_ROAMING
    elif "went offline" in lowered or "disconnected" in lowered:
        event = EVENT_OFFLINE
    elif "went online" in lowered or "connected to" in lowered:
        event = EVENT_ONLINE
    else:
        return None
    aps = [_mac(m) for m in _AP.findall(text)]
    ssid = _SSID.search(text)
    seconds = None
    if (duration := _DURATION.search(text)) and any(duration.groups()):
        d, h, m, s = (int(v or 0) for v in duration.groups())
        seconds = ((d * 24 + h) * 60 + m) * 60 + s
    traffic = None
    if found := _TRAFFIC.search(text):
        traffic = round(float(found.group(1)) * _UNITS[found.group(2).upper()], 2)
    return WifiEvent(
        event=event,
        client_mac=_mac(client.group(1)),
        ap_mac=aps[-1] if aps else None,
        from_ap_mac=aps[0] if event == EVENT_ROAMING and len(aps) > 1 else None,
        ssid=ssid.group(1) if ssid else None,
        connected_seconds=seconds,
        traffic_kb=traffic,
    )


def parse_payload(payload) -> list[WifiEvent]:
    """All recognised client events of one webhook message."""
    if not isinstance(payload, Mapping):
        return []
    texts = next((v for k, v in payload.items() if str(k).lower() == "text"), None)
    if isinstance(texts, str):
        texts = [texts]
    if not isinstance(texts, list):
        return []
    return [event for text in texts if (event := parse_message(text)) is not None]
