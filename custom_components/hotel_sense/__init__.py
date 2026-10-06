import logging

from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.core import HomeAssistant

from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, PLATFORMS
from .controller import OmadaController
from .exely_webhook import ExelyReceiver, async_ensure_secrets
from .presence_manager import PresenceManager
from .services import async_register_services
from .storage import async_get_device_store

LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

async def async_setup(hass, config) -> bool:
    async_register_services(hass)

    conf = config.get(DOMAIN)
    if conf is None:
        return True

    domains_list = hass.config_entries.async_domains()
    if DOMAIN in domains_list:
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT}, data=conf)
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Before the controller registers its update listener (it reacts to entry updates).
    async_ensure_secrets(hass, entry)
    omada_controller = OmadaController(hass, entry)
    await omada_controller.async_setup()

    omada_controller.presence = PresenceManager(
        hass, entry, omada_controller, await async_get_device_store(hass))
    omada_controller.exely = ExelyReceiver(hass, entry, omada_controller.presence)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = omada_controller
    omada_controller.async_remove_hidden_devices()
    omada_controller.async_remove_retired_entities()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # AP devices exist in the registry now, so AP -> Area can be resolved.
    omada_controller.presence.async_start()
    omada_controller.exely.async_start()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    omada_controller = hass.data[DOMAIN].pop(entry.entry_id)
    return await omada_controller.async_close()


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    pass
