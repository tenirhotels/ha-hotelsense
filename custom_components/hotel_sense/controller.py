"""The Omada controller of one config entry: connection, polling, cleanup.

Polls the site's access points and connected clients (``omada_hub``) every
scan interval and signals ``signal_update``; the presence manager and the
entities read the snapshots from here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    ATTR_CONTROLLER_MODEL, ATTR_MANUFACTURER, CONF_DB_RETENTION, CONF_SCAN_INTERVAL, CONF_SITE,
    DB_KEYS, DOMAIN, PLATFORMS,
)
from .history import DEFAULT_RETENTION_MONTHS
from .ids import NS_AP, make_unique_id, parse_unique_id
from .omada_hub import (
    AccessPoint, ConnectedClient, LoginFailed, OmadaClientException, OmadaHub,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL = 30
# entry.data keys of the controller connection: a change reloads the entry
# (Exely secrets live in entry.data too, but rotating them needs no reload).
CONNECTION_KEYS = (CONF_URL, CONF_SITE, CONF_USERNAME, CONF_PASSWORD, CONF_VERIFY_SSL)
# The history database connection (entry.data) reloads the entry as well.
RELOAD_KEYS = CONNECTION_KEYS + DB_KEYS
# Keys of the entities each access point has (see sensor.py / binary_sensor.py).
# Room entities only hotel rooms have (not common areas).
ROOM_ONLY_KEYS = {"room_status", "room_state", "violation"}
AP_ENTITY_KEYS = {"sensor": {"uptime", "clients"}, "binary_sensor": {"status"}}
# Names/types of clients that just left are kept this long (presence keeps a
# departed device in its room for the disconnect timeout).
_CLIENT_CACHE_SECONDS = 24 * 3600


def build_hub(hass: HomeAssistant, data) -> OmadaHub:
    return OmadaHub(hass, data[CONF_URL], data[CONF_USERNAME], data[CONF_PASSWORD],
                    data[CONF_SITE], data[CONF_VERIFY_SSL])


class OmadaController:
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = config_entry
        self.hub: OmadaHub = build_hub(hass, config_entry.data)
        self._connection = {k: config_entry.data.get(k) for k in RELOAD_KEYS}
        self.available = True
        self.access_points: dict[str, AccessPoint] = {}
        self.clients: dict[str, ConnectedClient] = {}
        self._recent_clients: dict[str, tuple[ConnectedClient, float]] = {}
        self._on_close: list[CALLBACK_TYPE] = []
        self.presence = None  # PresenceManager, set up in __init__.async_setup_entry
        self.exely = None  # ExelyReceiver, set up in __init__.async_setup_entry
        self.omada_webhook = None  # OmadaWebhook, set up in __init__.async_setup_entry
        self.suggestions = None  # DeviceSuggestions, set up in __init__.async_setup_entry
        self.history = None  # HistoryWriter when the history database is set up
        self.async_stop_history = None  # closes presence sessions, flushes, disconnects
        self.option_scan_interval = DEFAULT_SCAN_INTERVAL
        self.load_config_entry_options()

    def load_config_entry_options(self) -> None:
        self.option_scan_interval = int(
            self.entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))

    # -- identity ----------------------------------------------------------- #
    @property
    def controller_id(self) -> str:
        return self.hub.controller_id

    @property
    def site_id(self) -> str:
        """Stable site identifier (the Omada site key) used in unique IDs."""
        return self.hub.site_id

    @property
    def scan_interval(self) -> timedelta:
        return timedelta(seconds=self.option_scan_interval)

    @property
    def signal_update(self) -> str:
        return f"{DOMAIN}-update-{self.entry.entry_id}"

    @property
    def signal_options_update(self) -> str:
        return f"{DOMAIN}-options-{self.entry.entry_id}"

    def controller_device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.controller_id)},
            manufacturer=ATTR_MANUFACTURER,
            model=ATTR_CONTROLLER_MODEL,
            sw_version=self.hub.version,
            name=self.hub.name,
        )

    def ap_device_info(self, mac: str) -> DeviceInfo:
        ap = self.access_points.get(mac)
        info = DeviceInfo(
            identifiers={(DOMAIN, make_unique_id(NS_AP, self.site_id, mac))},
            connections={(CONNECTION_NETWORK_MAC, mac.lower().replace("-", ":"))},
            manufacturer=ATTR_MANUFACTURER,
        )
        if ap is not None:
            info.update(name=ap.name, model=ap.model, sw_version=ap.firmware)
        return info

    # -- clients ------------------------------------------------------------ #
    def client(self, mac: str) -> ConnectedClient | None:
        """The connected client, or the last snapshot of one that just left."""
        if (client := self.clients.get(mac)) is not None:
            return client
        recent = self._recent_clients.get(mac)
        return recent[0] if recent else None

    # -- lifecycle ---------------------------------------------------------- #
    async def async_setup(self) -> None:
        try:
            await self.hub.async_connect()
        except LoginFailed as err:
            raise ConfigEntryAuthFailed from err
        except (OmadaClientException, TimeoutError) as err:
            raise ConfigEntryNotReady(f"Omada controller not reachable: {err}") from err

        await self.async_update()
        if not self.available:
            # No access point list yet: retry later rather than start (and clean
            # up the registry) on empty data.
            raise ConfigEntryNotReady("Omada controller did not answer the first poll")

        self.async_on_close(async_track_time_interval(
            self.hass, self.async_update, self.scan_interval))
        self.async_on_close(self.entry.add_update_listener(self.async_config_entry_updated))

    async def async_update(self, now: datetime | None = None) -> None:
        try:
            async with asyncio.timeout(max(self.option_scan_interval - 1, 9)):
                aps, clients = await self.hub.async_poll()
        except (OmadaClientException, TimeoutError) as err:
            if self.available:
                LOGGER.warning("Omada controller unreachable: %s", err or type(err).__name__)
            self.available = False
        else:
            if not self.available:
                LOGGER.info("Omada controller reachable again")
            self.available = True
            self.access_points = aps
            self.clients = clients
            stamp = time.monotonic()
            for mac, client in clients.items():
                self._recent_clients[mac] = (client, stamp)
            for mac in [m for m, (_, t) in self._recent_clients.items()
                        if stamp - t > _CLIENT_CACHE_SECONDS]:
                del self._recent_clients[mac]
        async_dispatcher_send(self.hass, self.signal_update)

    @callback
    def async_on_close(self, func: CALLBACK_TYPE) -> None:
        self._on_close.append(func)

    async def async_close(self) -> bool:
        for func in self._on_close:
            func()
        self._on_close.clear()
        return await self.hass.config_entries.async_unload_platforms(self.entry, PLATFORMS)

    @staticmethod
    async def async_config_entry_updated(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        if not (controller := hass.data.get(DOMAIN, {}).get(config_entry.entry_id)):
            return
        old_interval = controller.option_scan_interval
        controller.load_config_entry_options()
        connection = {k: config_entry.data.get(k) for k in RELOAD_KEYS}
        if controller.history is not None:
            controller.history.retention_months = int(
                config_entry.options.get(CONF_DB_RETENTION, DEFAULT_RETENTION_MONTHS))
        if controller.option_scan_interval != old_interval or connection != controller._connection:
            hass.config_entries.async_schedule_reload(config_entry.entry_id)
            return
        async_dispatcher_send(hass, controller.signal_options_update)

    # -- registry cleanup ---------------------------------------------------- #
    def _is_current_entity(self, domain: str, unique_id: str) -> bool:
        cid = self.controller_id
        if unique_id.startswith("room:"):
            # room:<site>:<area_id>[:<key>]; a common area has no status / state / violation.
            parts = unique_id.split(":")
            if len(parts) == 4 and parts[3] in ROOM_ONLY_KEYS and self.presence is not None:
                room = self.presence.registry.get(parts[2])
                return room is None or not room.is_common
            return True
        if unique_id in {f"clients-{cid}", f"exely_last_event-{cid}", f"misplaced_devices-{cid}",
                         f"controller_online-{cid}", f"omada_webhook_last_message-{cid}",
                         f"exely_api-{cid}", f"device_suggestions-{cid}"}:
            return True
        if unique_id == f"history_database-{cid}":
            return self.history is not None
        parsed = parse_unique_id(unique_id)
        return (parsed is not None and parsed.namespace == NS_AP
                and parsed.key in AP_ENTITY_KEYS.get(domain, ()))

    @callback
    def async_cleanup_registry(self) -> None:
        """Remove what Hotel Sense no longer provides.

        Up to 0.2 Hotel Sense carried the ha-omada entities: per-client trackers,
        sensors and switches, AP device trackers, firmware updates, buttons ...
        Only rooms, the access points' uptime / clients / status and the
        controller sensors remain. Access point devices (and so their Areas)
        are kept: their identifiers are unchanged.
        """
        ent_reg = entity_registry.async_get(self.hass)
        for entry in entity_registry.async_entries_for_config_entry(ent_reg, self.entry.entry_id):
            if not self._is_current_entity(entry.domain, entry.unique_id):
                ent_reg.async_remove(entry.entity_id)

        dev_reg = device_registry.async_get(self.hass)
        for device in device_registry.async_entries_for_config_entry(dev_reg, self.entry.entry_id):
            for domain, identifier in device.identifiers:
                if domain != DOMAIN or (parsed := parse_unique_id(identifier)) is None:
                    continue
                stale = (parsed.namespace != NS_AP or (
                    parsed.mac not in self.access_points
                    and not entity_registry.async_entries_for_device(ent_reg, device.id, True)))
                if stale:
                    dev_reg.async_remove_device(device.id)
                break
