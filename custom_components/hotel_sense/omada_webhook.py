"""Omada controller webhook: refresh presence as soon as something happens.

Omada (Settings -> Webhook) can push its events and alerts - a client
connected, disconnected, roamed ... - to a URL. Hotel Sense does not parse
them: any authenticated message triggers an immediate poll (debounced), so
the rooms update within seconds instead of at the next scan interval.

Omada puts the "Shard Secret" configured with the webhook into the JSON body
(``shardSecret``); a ``Shard-Secret`` header is accepted as well. Recent
messages (secret removed) are kept for diagnostics, to learn the format.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from collections import deque
from dataclasses import asdict
from http import HTTPStatus
from json import JSONDecodeError

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.util import dt as dt_util

from .const import CONF_OMADA_WEBHOOK_ID, CONF_OMADA_WEBHOOK_SECRET, DOMAIN
from .omada_events import parse_payload

LOGGER = logging.getLogger(__name__)

SECRET_FIELD = "shardSecret"
SECRET_HEADER = "Shard-Secret"
RECENT_EVENTS = 20
REFRESH_COOLDOWN = 3  # seconds: a burst of events gives one or two polls


@callback
def async_ensure_omada_secrets(hass: HomeAssistant, entry: ConfigEntry) -> None:
    if CONF_OMADA_WEBHOOK_ID in entry.data and CONF_OMADA_WEBHOOK_SECRET in entry.data:
        return
    hass.config_entries.async_update_entry(entry, data={
        **entry.data,
        CONF_OMADA_WEBHOOK_ID: entry.data.get(CONF_OMADA_WEBHOOK_ID) or secrets.token_hex(16),
        CONF_OMADA_WEBHOOK_SECRET: entry.data.get(CONF_OMADA_WEBHOOK_SECRET)
        or secrets.token_urlsafe(24),
    })


class OmadaWebhook:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller) -> None:
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self.recent: deque[dict] = deque(maxlen=RECENT_EVENTS)
        self.received = 0
        self.rejected = 0
        self._registered = False
        self._refresh = Debouncer(hass, LOGGER, cooldown=REFRESH_COOLDOWN, immediate=True,
                                  function=controller.async_update)

    @property
    def webhook_id(self) -> str:
        return self.entry.data[CONF_OMADA_WEBHOOK_ID]

    @property
    def secret(self) -> str:
        return self.entry.data[CONF_OMADA_WEBHOOK_SECRET]

    def url(self) -> str:
        """Address for the controller, which is on the local network."""
        try:
            return webhook.async_generate_url(self.hass, self.webhook_id,
                                              allow_external=True, prefer_external=False)
        except NoURLAvailableError:
            return f"http://<Home Assistant IP>:8123{webhook.async_generate_path(self.webhook_id)}"

    @callback
    def async_start(self) -> None:
        webhook.async_register(self.hass, DOMAIN, "Hotel Sense: Omada events", self.webhook_id,
                               self._handle, local_only=True, allowed_methods=["POST"])
        self._registered = True
        self.entry.async_on_unload(self._async_stop)

    @callback
    def _async_stop(self) -> None:
        if self._registered:
            webhook.async_unregister(self.hass, self.webhook_id)
            self._registered = False
        self._refresh.async_shutdown()

    @callback
    def async_rotate_secret(self) -> None:
        """New Shard Secret: the old one stops working at once."""
        self.hass.config_entries.async_update_entry(self.entry, data={
            **self.entry.data, CONF_OMADA_WEBHOOK_SECRET: secrets.token_urlsafe(24)})
        LOGGER.warning("Omada webhook secret regenerated; enter it in the Omada controller")

    async def _handle(self, hass: HomeAssistant, webhook_id: str,
                      request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except (JSONDecodeError, ValueError, UnicodeDecodeError):
            payload = None
        given = request.headers.get(SECRET_HEADER, "")
        if isinstance(payload, dict) and not given:
            given = str(payload.get(SECRET_FIELD) or "")
        if not hmac.compare_digest(given.encode(), self.secret.encode()):
            self.rejected += 1
            LOGGER.warning("Omada webhook: message with a missing or wrong Shard Secret rejected")
            return web.Response(status=HTTPStatus.UNAUTHORIZED)

        self.received += 1
        if isinstance(payload, dict):
            payload = {k: v for k, v in payload.items() if k != SECRET_FIELD}
        self.recent.append({"received": dt_util.utcnow().isoformat(), "payload": payload})
        if (history := self.controller.history) is not None:
            for event in parse_payload(payload):
                history.add("wifi_events", **asdict(event))
        await self._refresh.async_call()
        return web.Response(status=HTTPStatus.OK)

    def diagnostics(self) -> dict:
        return {"received": self.received, "rejected": self.rejected,
                "recent": list(self.recent)}
