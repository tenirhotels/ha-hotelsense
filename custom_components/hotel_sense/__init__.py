"""Hotel Sense: room presence from TP-Link Omada Wi-Fi, room status from Exely PMS."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PLATFORMS
from .controller import OmadaController
from .exely_webhook import ExelyReceiver, async_ensure_secrets
from .presence_manager import PresenceManager
from .services import async_register_services
from .storage import async_get_device_store

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Exely secrets first: the controller's update listener reacts to entry updates.
    async_ensure_secrets(hass, entry)
    controller = OmadaController(hass, entry)
    await controller.async_setup()
    controller.presence = PresenceManager(hass, entry, controller, await async_get_device_store(hass))
    controller.exely = ExelyReceiver(hass, entry, controller.presence)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller
    controller.async_cleanup_registry()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Access point devices exist now, so AP -> Area (rooms) can be resolved.
    controller.presence.async_start()
    controller.exely.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    controller: OmadaController = hass.data[DOMAIN][entry.entry_id]
    if unloaded := await controller.async_close():
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
