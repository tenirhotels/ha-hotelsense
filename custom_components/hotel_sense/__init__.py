"""Hotel Sense: room presence from TP-Link Omada Wi-Fi, room status from Exely PMS."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_COMMON_AREAS, CONF_DB_HOST, CONF_DB_NAME, CONF_EXELY_ROOM_MAP, CONF_DB_PASSWORD, CONF_DB_PORT, CONF_DB_RETENTION,
    CONF_DB_USERNAME, DEFAULT_DB_PORT, DOMAIN, LEGACY_OPTIONS, PLATFORMS,
)
from .controller import OmadaController
from .exely import parse_room_map
from .rooms import RoomRegistry
from .history import DEFAULT_RETENTION_MONTHS, HistoryWriter, build_url
from .exely_webhook import ExelyReceiver, async_ensure_secrets
from .omada_webhook import OmadaWebhook, async_ensure_omada_secrets
from .presence_manager import PresenceManager
from .services import async_register_services
from .storage import async_get_device_store

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Options of the ha-omada features removed in 0.3 are dropped (before the
    # controller registers its update listener, so this does not reload).
    if LEGACY_OPTIONS & set(entry.options):
        hass.config_entries.async_update_entry(entry, options={
            k: v for k, v in entry.options.items() if k not in LEGACY_OPTIONS})
    # The room model; the room options of 0.7 and older move into it once.
    registry = RoomRegistry(hass, entry.entry_id)
    await registry.async_load()
    entry.async_on_unload(registry.async_flush)
    if CONF_COMMON_AREAS in entry.options or CONF_EXELY_ROOM_MAP in entry.options:
        registry.import_legacy(entry.options.get(CONF_COMMON_AREAS),
                               parse_room_map(entry.options.get(CONF_EXELY_ROOM_MAP)))
        hass.config_entries.async_update_entry(entry, options={
            k: v for k, v in entry.options.items()
            if k not in (CONF_COMMON_AREAS, CONF_EXELY_ROOM_MAP)})
    # Exely secrets first: the controller's update listener reacts to entry updates.
    async_ensure_secrets(hass, entry)
    async_ensure_omada_secrets(hass, entry)
    controller = OmadaController(hass, entry)
    await controller.async_setup()
    controller.history = history_writer(hass, entry)
    controller.presence = PresenceManager(hass, entry, controller,
                                          await async_get_device_store(hass), registry)
    controller.exely = ExelyReceiver(hass, entry, controller.presence)
    controller.omada_webhook = OmadaWebhook(hass, entry, controller)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller
    controller.async_cleanup_registry()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Access point devices exist now, so AP -> Area (rooms) can be resolved.
    controller.presence.async_start()
    controller.exely.async_start()
    controller.omada_webhook.async_start()
    if controller.history is not None:
        controller.history.async_start()

        stopped = False

        async def _async_stop(_event: Event | None = None) -> None:
            # Home Assistant does not unload entries when it stops: write what is open.
            nonlocal stopped
            stopped = True
            controller.presence.async_close_sessions()
            await controller.history.async_stop()

        unsub_stop = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
        entry.async_on_unload(lambda: None if stopped else unsub_stop())
        controller.async_stop_history = _async_stop
    return True


def history_writer(hass: HomeAssistant, entry: ConfigEntry) -> HistoryWriter | None:
    """The history database writer, if the database is set up in the options."""
    data = entry.data
    if not (data.get(CONF_DB_HOST) and data.get(CONF_DB_USERNAME) and data.get(CONF_DB_NAME)):
        return None
    url = build_url(data[CONF_DB_HOST], data.get(CONF_DB_PORT) or DEFAULT_DB_PORT,
                    data[CONF_DB_USERNAME], data.get(CONF_DB_PASSWORD) or "", data[CONF_DB_NAME])
    return HistoryWriter(hass, url, int(entry.options.get(CONF_DB_RETENTION,
                                                          DEFAULT_RETENTION_MONTHS)))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    controller: OmadaController = hass.data[DOMAIN][entry.entry_id]
    if controller.history is not None:
        await controller.async_stop_history()
    if unloaded := await controller.async_close():
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
