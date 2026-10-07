"""Hotel Sense: room presence from TP-Link Omada Wi-Fi, room status from Exely PMS."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, LEGACY_OPTIONS, PLATFORMS
from .controller import OmadaController
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
    # Exely secrets first: the controller's update listener reacts to entry updates.
    async_ensure_secrets(hass, entry)
    async_ensure_omada_secrets(hass, entry)
    controller = OmadaController(hass, entry)
    await controller.async_setup()
    controller.presence = PresenceManager(hass, entry, controller, await async_get_device_store(hass))
    controller.exely = ExelyReceiver(hass, entry, controller.presence)
    controller.omada_webhook = OmadaWebhook(hass, entry, controller)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller
    controller.async_cleanup_registry()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Access point devices exist now, so AP -> Area (rooms) can be resolved.
    controller.presence.async_start()
    controller.exely.async_start()
    controller.omada_webhook.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    controller: OmadaController = hass.data[DOMAIN][entry.entry_id]
    if unloaded := await controller.async_close():
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
