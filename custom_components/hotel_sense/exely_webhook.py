"""Receive Exely PMS webhooks and set the room status (check-in / check-out).

Exely posts JSON over HTTPS with an ``API-KEY`` header and expects 200 OK.
The webhook URL is ``<external URL>/api/webhook/<webhook_id>``; both the id and
the key are random secrets generated once per config entry.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from collections import deque
from http import HTTPStatus
from json import JSONDecodeError

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.util import dt as dt_util

from .const import (
    CONF_EXELY_API_KEY, CONF_EXELY_ROOM_MAP, CONF_EXELY_WEBHOOK_ID, DOMAIN, EVENT_EXELY,
)
from .exely import parse_event, parse_room_map, room_number

LOGGER = logging.getLogger(__name__)

API_KEY_HEADER = "API-KEY"
RECENT_EVENTS = 20

RESULT_APPLIED = "applied"
RESULT_PARTIAL = "partial"
RESULT_UNMATCHED = "unmatched"  # not a check-in/check-out or room not found
RESULT_INVALID = "invalid"      # not JSON


@callback
def async_ensure_secrets(hass: HomeAssistant, entry: ConfigEntry) -> None:
    if CONF_EXELY_WEBHOOK_ID in entry.data and CONF_EXELY_API_KEY in entry.data:
        return
    hass.config_entries.async_update_entry(entry, data={
        **entry.data,
        CONF_EXELY_WEBHOOK_ID: entry.data.get(CONF_EXELY_WEBHOOK_ID) or secrets.token_hex(16),
        CONF_EXELY_API_KEY: entry.data.get(CONF_EXELY_API_KEY) or secrets.token_urlsafe(24),
    })


class ExelyReceiver:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, manager) -> None:
        self.hass = hass
        self.entry = entry
        self.manager = manager
        self.recent: deque[dict] = deque(maxlen=RECENT_EVENTS)
        self.last: dict | None = None
        self._registered_id: str | None = None

    @property
    def webhook_id(self) -> str:
        return self.entry.data[CONF_EXELY_WEBHOOK_ID]

    @property
    def api_key(self) -> str:
        return self.entry.data[CONF_EXELY_API_KEY]

    @property
    def signal(self) -> str:
        return f"{DOMAIN}-exely-{self.entry.entry_id}"

    def url(self) -> str:
        try:
            return webhook.async_generate_url(self.hass, self.webhook_id,
                                              allow_internal=False, prefer_external=True)
        except NoURLAvailableError:
            return f"https://<your HA address>{webhook.async_generate_path(self.webhook_id)}"

    @callback
    def async_start(self) -> None:
        self._register()
        self.entry.async_on_unload(self._unregister)

    @callback
    def _register(self) -> None:
        webhook.async_register(self.hass, DOMAIN, "Hotel Sense: Exely PMS", self.webhook_id,
                               self._handle, local_only=False, allowed_methods=["POST"])
        self._registered_id = self.webhook_id

    @callback
    def _unregister(self) -> None:
        if self._registered_id:
            webhook.async_unregister(self.hass, self._registered_id)
            self._registered_id = None

    @callback
    def async_rotate(self, *, api_key: bool = False, url: bool = False) -> None:
        """Replace a compromised key and/or webhook address. Old ones stop working at once."""
        data = dict(self.entry.data)
        if api_key:
            data[CONF_EXELY_API_KEY] = secrets.token_urlsafe(24)
        if url:
            data[CONF_EXELY_WEBHOOK_ID] = secrets.token_hex(16)
        if data == dict(self.entry.data):
            return
        self.hass.config_entries.async_update_entry(self.entry, data=data)
        if url and self._registered_id:
            self._unregister()
            self._register()
        LOGGER.warning("Exely webhook secrets regenerated (key: %s, address: %s); "
                       "update them in Exely", api_key, url)

    # ------------------------------------------------------------------ #
    async def _handle(self, hass: HomeAssistant, webhook_id: str,
                      request: web.Request) -> web.Response:
        given = request.headers.get(API_KEY_HEADER, "")
        if not hmac.compare_digest(given.encode(), self.api_key.encode()):
            LOGGER.warning("Exely webhook: request with a missing or wrong %s header rejected",
                           API_KEY_HEADER)
            return web.Response(status=HTTPStatus.UNAUTHORIZED)
        try:
            payload = await request.json()
        except (JSONDecodeError, ValueError, UnicodeDecodeError):
            self._record(None, {"result": RESULT_INVALID})
            return web.Response(status=HTTPStatus.OK)  # never make Exely retry garbage
        self._record(payload, self.apply(payload))
        return web.Response(status=HTTPStatus.OK)

    def resolve_room(self, label: str) -> str | None:
        """Exely room label -> area_id of a hotel room (not a common area)."""
        rooms = {a: r for a, r in self.manager.rooms.items() if not r.is_common}
        mapped = parse_room_map(self.entry.options.get(CONF_EXELY_ROOM_MAP)).get(label.strip().lower())
        target = mapped or label
        areas = ar.async_get(self.hass)
        area = areas.async_get_area(target) or areas.async_get_area_by_name(target)
        if area and area.id in rooms:
            return area.id
        if mapped:
            return None  # explicit mapping to an unknown room: do not guess
        number = room_number(label)
        matches = [a for a, r in rooms.items() if number is not None and room_number(r.name) == number]
        return matches[0] if len(matches) == 1 else None

    @callback
    def apply(self, payload) -> dict:
        parsed = parse_event(payload)
        result = {"event": parsed.event, "status": parsed.status, "rooms": parsed.rooms,
                  "applied": [], "unresolved": []}
        if parsed.status is None or not parsed.rooms:
            result["result"] = RESULT_UNMATCHED
            return result
        for label in parsed.rooms:
            if (area_id := self.resolve_room(label)) is None:
                result["unresolved"].append(label)
                continue
            self.manager.async_set_status(area_id, parsed.status)
            result["applied"].append(self.manager.rooms[area_id].name)
        result["result"] = (RESULT_UNMATCHED if not result["applied"]
                            else RESULT_PARTIAL if result["unresolved"] else RESULT_APPLIED)
        return result

    @callback
    def _record(self, payload, result: dict) -> None:
        result = {"received": dt_util.utcnow().isoformat(), **result}
        if result["result"] != RESULT_APPLIED:
            LOGGER.info("Exely webhook not applied: %s", result)
        self.last = result
        # Raw payloads (guest data) stay in memory only, for config entry diagnostics.
        self.recent.appendleft({**result, "payload": payload})
        self.hass.bus.async_fire(EVENT_EXELY, result)
        async_dispatcher_send(self.hass, self.signal)
