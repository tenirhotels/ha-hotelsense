from __future__ import annotations

import logging

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.button import (
    DOMAIN,
    ButtonEntity,
    ButtonEntityDescription
)
from homeassistant.core import callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .controller import OmadaController
from .api.controller import Controller

from .const import DOMAIN as OMADA_DOMAIN
from .ids import NS_AP, NS_CLIENT
from .omada_entity import (OmadaEntity, OmadaEntityDescription, device_device_info_fn, client_device_info_fn)

from .omada_controller_entity import (
    OmadaControllerEntity,
    OmadaControllerEntityDescription,
    device_info_fn as controller_device_info_fn,
    unique_id_fn as controller_unique_id_fn
)

REBOOT_BUTTON = "reboot"

LOGGER = logging.getLogger(__name__)

@callback
async def reboot_device_fn(api: Controller, mac: str) -> None:
    await api.devices.trigger_reboot(mac)


@dataclass
class OmadaButtonEntityDescriptionMixin():
    activate_fn: Callable[[OmadaController], None]


@dataclass
class OmadaButtonEntityDescription(
    OmadaEntityDescription,
    ButtonEntityDescription,
    OmadaButtonEntityDescriptionMixin
):
    """Omada Button Entity Description"""


@dataclass
class OmadaDeviceButtonEntityDescriptionMixin:
    activate_fn: Callable[[OmadaController], None]


@dataclass
class OmadaDeviceButtonEntityDescription(
    ButtonEntityDescription,
    OmadaEntityDescription,
    OmadaDeviceButtonEntityDescriptionMixin,
):
    """Omada Device Button Entity Description"""

@dataclass
class OmadaControllerButtonEntityDescriptionMixin:
    activate_fn: Callable[[OmadaController], None]


@dataclass
class OmadaControllerButtonEntityDescription(
    ButtonEntityDescription,
    OmadaControllerEntityDescription,
    OmadaControllerButtonEntityDescriptionMixin,
):
    """Omada Controller Button Entity Description"""

    pass


# Hotel Sense: no controller buttons. "Start WLAN Optimization" and "Reconnect
# All Clients" were removed (not needed; reconnect also fails on controller v6).
CONTROLLER_ENTITY_DESCRIPTIONS: dict[
    str, OmadaControllerButtonEntityDescription
] = {}

DEVICE_ENTITY_DESCRIPTIONS: dict[
    str, OmadaDeviceButtonEntityDescription
] = {
    REBOOT_BUTTON: OmadaDeviceButtonEntityDescription(
        domain=DOMAIN,
        key=REBOOT_BUTTON,
        entity_category=EntityCategory.DIAGNOSTIC,
        has_entity_name=True,
        icon="mdi:restart",
        allowed_fn=lambda controller, _: (controller.option_device_controls and
                                          controller.option_track_devices),
        supported_fn=lambda controller, mac: True,
        available_fn=lambda controller, mac: controller.available,
        device_info_fn=device_device_info_fn,
        name_fn=lambda *_: "Reboot",
        namespace=NS_AP,
        activate_fn=reboot_device_fn
    )
}

# Hotel Sense: no per-client "Reconnect" button (it drops a guest's Wi-Fi and the
# command path does not exist on Omada controller v6).
CLIENT_ENTITY_DESCRIPTIONS: dict[str, OmadaButtonEntityDescription] = {}

async def async_setup_entry(hass, config_entry, async_add_entities):
    controller: OmadaController = hass.data[OMADA_DOMAIN][config_entry.entry_id]

    # Set up Controller Entities
    for description in CONTROLLER_ENTITY_DESCRIPTIONS.values():
        entity = OmadaControllerButtonEntity(controller, description)
        async_add_entities([entity])

    @callback
    def items_added() -> None:

        if controller.option_track_clients:
            controller.register_platform_entities(
                controller.api.clients,
                OmadaButtonEntity,
                CLIENT_ENTITY_DESCRIPTIONS,
                async_add_entities)
        if controller.option_track_devices:
            controller.register_platform_entities(
                controller.api.devices,
                OmadaDeviceButtonEntity,
                DEVICE_ENTITY_DESCRIPTIONS,
                async_add_entities)

    for signal in (controller.signal_update, controller.signal_options_update):
        config_entry.async_on_unload(
            async_dispatcher_connect(hass, signal, items_added))

    if controller.option_track_clients:
        controller.restore_cleanup_platform_entities(
            DOMAIN,
            controller.api.clients,
            controller.api.known_clients,
            controller.api.devices,
            OmadaButtonEntity,
            CLIENT_ENTITY_DESCRIPTIONS,
            config_entry,
            async_add_entities
        )

    items_added()


class OmadaButtonEntity(OmadaEntity, ButtonEntity):

    entity_description: OmadaControllerButtonEntityDescription

    def __init__(self, mac: str, controller: OmadaController, description: OmadaEntityDescription) -> None:
        super().__init__(mac, controller, description)

    async def async_press(self) -> None:
        await self.entity_description.activate_fn(self.controller.api, self._mac)


class OmadaDeviceButtonEntity(OmadaEntity, ButtonEntity):

    entity_description: OmadaControllerButtonEntityDescription

    def __init__(
            self, mac: str, controller: OmadaController, description: OmadaDeviceButtonEntityDescription
    ) -> None:

        super().__init__(mac, controller, description)

    async def async_press(self) -> None:
        await self.entity_description.activate_fn(self.controller.api, self._mac)


class OmadaControllerButtonEntity(OmadaControllerEntity, ButtonEntity):
    controller: OmadaController
    entity_description: OmadaControllerButtonEntityDescription

    def __init__(
        self, controller: OmadaController, description: OmadaControllerEntityDescription
    ) -> None:

        super().__init__(controller, description)

    async def async_press(self) -> None:
        await self.entity_description.activate_fn(self.controller.api)
