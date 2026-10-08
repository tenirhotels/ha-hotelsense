"""Device identity suggestions, kept up to date (see ``device_suggest``).

Once an hour (and on request) the unknown random MACs of the last week are
compared with the staff devices on the list, from the history database
(sessions, Wi-Fi networks), Omada (the names phones give) and the sessions
open right now. The owner links a suggestion (``link_mac``) or ignores it;
ignored pairs are remembered. Needs the history database.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .device_list import CATEGORY_EMPLOYEE
from .device_suggest import Profile, Session, normalise_name, suggest
from .history import HistoryUnavailable
from .history import now as history_now
from .omada_hub import OmadaClientException
from .queries import suggestion_data

LOGGER = logging.getLogger(__name__)

INTERVAL = timedelta(hours=1)
FIRST_RUN = 120  # seconds after start: let the first polls and the database settle
WINDOW = timedelta(days=30)  # history compared
NEW_WINDOW = timedelta(days=7)  # a MAC first seen within this is "new"

STATUS_OK = "ok"
STATUS_NO_HISTORY = "no_history"
STATUS_ERROR = "error"


class DeviceSuggestions:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller, device_store) -> None:
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self.device_store = device_store
        self._store: Store[dict] = Store(hass, 1, f"{DOMAIN}.suggestions.{entry.entry_id}")
        self.ignored: set[tuple[str, str]] = set()
        self.suggestions: list[dict] = []
        self.updated: str | None = None
        self.status = STATUS_NO_HISTORY if controller.history is None else STATUS_OK
        self.error: str | None = None

    @property
    def signal(self) -> str:
        return f"{DOMAIN}_suggestions_{self.entry.entry_id}"

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.ignored = {(m, i) for m, i in data.get("ignored", [])}

    @callback
    def async_start(self) -> None:
        async def _tick(_now=None) -> None:
            await self.async_refresh()

        self.entry.async_on_unload(async_track_time_interval(self.hass, _tick, INTERVAL))
        self.entry.async_on_unload(async_call_later(self.hass, FIRST_RUN, _tick))
        self.entry.async_on_unload(self.device_store.async_add_listener(self._devices_changed))

    @callback
    def _devices_changed(self) -> None:
        """A suggested MAC that is now on the list (linked) is no longer a suggestion."""
        listed = self.device_store.devices
        if any(s["mac"] in listed for s in self.suggestions):
            self.suggestions = [s for s in self.suggestions if s["mac"] not in listed]
            self._changed()

    @callback
    def _changed(self) -> None:
        async_dispatcher_send(self.hass, self.signal)

    async def _omada_names(self) -> dict[str, str]:
        names = {mac: c.name for mac, c in self.controller.clients.items() if c.name}
        try:
            for client in await self.controller.hub.async_known_clients():
                if client.name:
                    names.setdefault(client.mac, client.name)
        except OmadaClientException as err:
            LOGGER.debug("Known clients unavailable for suggestions: %s", err)
        return names

    async def async_refresh(self) -> list[dict]:
        """Recompute the suggestions."""
        history = self.controller.history
        if history is None:
            self.status, self.suggestions = STATUS_NO_HISTORY, []
            self._changed()
            return self.suggestions
        devices = self.device_store.devices
        staff = [i for i in devices.identities() if i.category == CATEGORY_EMPLOYEE]
        known = {d.mac for d in devices}
        device_macs = {m for i in staff for m in i.macs}
        open_sessions = dict(self.controller.presence.sessions)
        now = history_now()
        try:
            data = await history.async_read(lambda conn: suggestion_data(
                conn, now - WINDOW, now - NEW_WINDOW, known, device_macs,
                open_macs=set(open_sessions)))
        except HistoryUnavailable as err:
            self.status, self.error = STATUS_ERROR, str(err)
            self._changed()
            return self.suggestions
        names = await self._omada_names()
        live_ssids = {mac: c.ssid for mac, c in self.controller.clients.items() if c.ssid}

        def profile(macs, extra_names=()) -> Profile:
            result = Profile()
            for mac in macs:
                result.sessions += [Session(z, s, e) for z, s, e in data["sessions"].get(mac, [])]
                if mac in open_sessions:
                    zone, since = open_sessions[mac]
                    result.sessions.append(Session(zone, since.replace(tzinfo=None), now))
                result.ssids |= data["ssids"].get(mac, set())
                if mac in live_ssids:
                    result.ssids.add(live_ssids[mac])
                for name in (names.get(mac), *extra_names):
                    if key := normalise_name(name):
                        result.names.add(key)
            result.label = next((names[m] for m in macs if names.get(m)), None)
            return result

        new = {mac: profile([mac]) for mac in data["candidates"]}
        staff_profiles = {i.id: ({"name": i.name, "category": i.category},
                                 profile(i.macs, [i.name])) for i in staff}
        self.suggestions = suggest(new, staff_profiles, self.ignored)
        self.updated = dt_util.utcnow().isoformat(timespec="seconds")
        self.status, self.error = STATUS_OK, None
        self._changed()
        return self.suggestions

    async def async_ignore(self, mac: str, identity: str) -> None:
        self.ignored.add((mac, identity))
        await self._store.async_save({"ignored": sorted(self.ignored)})
        self.drop(mac, identity)

    @callback
    def drop(self, mac: str, identity: str | None = None) -> None:
        """Take a handled suggestion off the list (linked or ignored)."""
        self.suggestions = [s for s in self.suggestions if not (
            s["mac"] == mac and (identity is None or s["identity"] == identity))]
        self._changed()
