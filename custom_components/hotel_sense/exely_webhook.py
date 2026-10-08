"""Receive Exely PMS webhooks and set the room status (check-in / check-out).

Exely posts JSON over HTTPS with an ``API-KEY`` header and expects 200 OK.
The webhook URL is ``<external URL>/api/webhook/<webhook_id>``; both the id and
the key are random secrets generated once per config entry.

Exely events name the booking, not the room: when the Exely API is set up,
the room is looked up in the background (``exely_api``) and the webhook is
answered at once. Repeated deliveries (same ``eventId``) are ignored.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from collections import deque
from collections.abc import Mapping
from http import HTTPStatus
from json import JSONDecodeError
from urllib.parse import urlsplit

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.util import dt as dt_util

from .const import (
    CONF_EXELY_API_KEY, CONF_EXELY_CLIENT_ID, CONF_EXELY_CLIENT_SECRET, CONF_EXELY_PROPERTY_ID,
    CONF_EXELY_WEBHOOK_ID, DOMAIN, EVENT_EXELY, STATUS_SOURCE_EXELY,
)
from .exely import ExelyEvent, parse_event, room_number
from .exely_api import STAY_CANCELLED, ExelyApi, ExelyApiError, RoomStay
from .exely_sync import ExelySync

LOGGER = logging.getLogger(__name__)

API_KEY_HEADER = "API-KEY"
RECENT_EVENTS = 20

RESULT_APPLIED = "applied"
RESULT_PARTIAL = "partial"
RESULT_UNMATCHED = "unmatched"  # not a check-in/check-out or room not found
RESULT_INVALID = "invalid"      # not JSON
RESULT_API_ERROR = "api_error"  # the Exely API could not tell the room

SEEN_EVENTS = 500  # eventIds remembered against repeated deliveries


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
        self.api = ExelyApi(hass, self._credentials)
        self.sync = ExelySync(hass, self)
        self._seen: deque[str] = deque(maxlen=SEEN_EVENTS)
        self.duplicates = 0

    def _credentials(self) -> tuple[str, str] | None:
        client_id = self.entry.data.get(CONF_EXELY_CLIENT_ID)
        secret = self.entry.data.get(CONF_EXELY_CLIENT_SECRET)
        return (client_id, secret) if client_id and secret else None

    @property
    def property_id(self) -> str | None:
        return self.entry.data.get(CONF_EXELY_PROPERTY_ID) or None

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
        """Public https:// address for Exely (it accepts only HTTPS).

        An ``http://`` external URL usually sits behind an HTTPS proxy or tunnel
        (e.g. Cloudflare) that serves the same host on port 443, so the address
        is shown as ``https://<host>`` rather than as the unusable http one.
        """
        path = webhook.async_generate_path(self.webhook_id)
        try:
            url = webhook.async_generate_url(self.hass, self.webhook_id,
                                             allow_internal=False, prefer_external=True)
        except NoURLAvailableError:
            return f"https://<your HA address>{path}"
        parsed = urlsplit(url)
        if parsed.scheme == "https":
            return url
        return f"https://{parsed.hostname}{path}"

    @callback
    def async_start(self) -> None:
        self._register()
        self.entry.async_on_unload(self._unregister)
        self.sync.async_start(self.entry)

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
        self.process(payload)
        return web.Response(status=HTTPStatus.OK)

    @callback
    def process(self, payload) -> None:
        """Apply each event; those naming only a booking are looked up via the API."""
        items = (payload if isinstance(payload, list) and payload
                 and all(isinstance(i, Mapping) for i in payload) else [payload])
        for item in items:
            parsed = parse_event(item)
            if parsed.event_id:
                if parsed.event_id in self._seen:
                    self.duplicates += 1
                    continue
                self._seen.append(parsed.event_id)
            prop = parsed.property_id or self.property_id
            if parsed.status and not parsed.rooms and parsed.booking:
                if not self.api.configured or not prop:
                    result = self._result(parsed, RESULT_UNMATCHED)
                    result["reason"] = "Exely API not set up (Hotel Sense options)"
                    self._record(item, result)
                    continue
                self.entry.async_create_background_task(
                    self.hass, self._async_lookup(item, parsed, prop), f"{DOMAIN} Exely lookup")
                continue
            if parsed.booking and prop and parsed.status is None:
                self.api.forget(prop, parsed.booking)  # booking changed: look up afresh
            self._record(item, self._apply(parsed))

    @staticmethod
    def _result(parsed: ExelyEvent, result: str | None = None) -> dict:
        data = {"event": parsed.event, "status": parsed.status, "rooms": list(parsed.rooms),
                "applied": [], "unresolved": []}
        if parsed.booking:
            data["booking"] = parsed.booking
        if result:
            data["result"] = result
        return data

    async def _stays(self, prop: str, booking: str) -> tuple[list[RoomStay], str]:
        """A single-room booking already looked up needs no new request."""
        cached = self.api.cached_stays(prop, booking)
        if cached is not None and len([s for s in cached if s.status != STAY_CANCELLED]) == 1:
            return cached, "cache"
        return await self.api.async_reservation(prop, booking), "api"

    async def _async_lookup(self, item, parsed: ExelyEvent, prop: str) -> None:
        result = self._result(parsed)
        try:
            stays, result["lookup"] = await self._stays(prop, parsed.booking)
            active = [s for s in stays if s.status != STAY_CANCELLED]
            # Several rooms in one booking: the stays whose status the event set.
            chosen = ([s for s in active if s.status == parsed.status]
                      or (active if len(active) == 1 else []))
            labels: list[list[str]] = []
            for stay in chosen:
                name = stay.room_name
                if name is None and stay.room_id:
                    name = await self.api.async_room_name(prop, stay.room_id)
                labels.append([v for v in (name, stay.room_id) if v])
            self.api.last_error = None
        except ExelyApiError as err:
            self.api.last_error = f"{type(err).__name__}: {err}"
            LOGGER.warning("Exely API: room of booking %s not found: %s", parsed.booking, err)
            result.update(result=RESULT_API_ERROR, error=self.api.last_error)
            self._record(item, result)
            return
        if not chosen:
            result["reason"] = ("no room in the booking" if not active
                                else "no room stay matches the event")
        for candidates in labels:
            result["rooms"].append(candidates[0] if candidates else "?")
            area_id = next((a for a in map(self.resolve_room, candidates) if a), None)
            if area_id is None:
                result["unresolved"].append(candidates[0] if candidates else "?")
                continue
            self.manager.async_set_status(area_id, parsed.status, STATUS_SOURCE_EXELY,
                                          booking=parsed.booking)
            result["applied"].append(self.manager.rooms[area_id].name)
        result["result"] = self._outcome(result)
        self._record(item, result)

    @staticmethod
    def _outcome(result: dict) -> str:
        return (RESULT_UNMATCHED if not result["applied"]
                else RESULT_PARTIAL if result["unresolved"] else RESULT_APPLIED)

    def resolve_room(self, label: str) -> str | None:
        """Exely room label -> area_id of a hotel room (not a common area)."""
        rooms = {a: r for a, r in self.manager.rooms.items() if not r.is_common}
        registry = self.manager.registry
        # 1. Exely roomId, 2. Exely room name (room model; a common area is no room).
        for find in (registry.find_by_exely_id, registry.find_by_exely_name):
            if (room := find(label)) is not None:
                return room.room_id if room.room_id in rooms else None
        # 3. explicit mapping to a room that does not exist (yet): do not guess.
        if registry.is_pending_label(label):
            return None
        # 4. HA Area id / name.
        areas = ar.async_get(self.hass)
        area = areas.async_get_area(label) or areas.async_get_area_by_name(label)
        if area and area.id in rooms:
            return area.id
        # 5. Room number, last resort (only when exactly one room has it).
        number = room_number(label)
        if number is None:
            return None
        matches = [a for a, r in rooms.items()
                   if room_number((r.room.number if r.room else None) or r.name) == number]
        if len(matches) == 1:
            LOGGER.info("Exely room %r matched to %s by its number only (no Exely mapping)",
                        label, matches[0])
            return matches[0]
        return None

    @callback
    def _apply(self, parsed: ExelyEvent) -> dict:
        """An event naming the room itself."""
        result = self._result(parsed)
        if parsed.status is None or not parsed.rooms:
            result["result"] = RESULT_UNMATCHED
            return result
        for label in parsed.rooms:
            if (area_id := self.resolve_room(label)) is None:
                result["unresolved"].append(label)
                continue
            self.manager.async_set_status(area_id, parsed.status, STATUS_SOURCE_EXELY,
                                          booking=parsed.booking)
            result["applied"].append(self.manager.rooms[area_id].name)
        result["result"] = self._outcome(result)
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
        if (history := self.manager.controller.history) is not None:
            parsed = parse_event(payload)
            history.add("pms_events", event_id=parsed.event_id, event=(result.get("event") or "")[:64],
                        booking=parsed.booking, property_id=parsed.property_id,
                        status=result.get("status"), result=result["result"],
                        rooms=", ".join(result.get("applied", []) + result.get("unresolved", []))[:255])
        async_dispatcher_send(self.hass, self.signal)
