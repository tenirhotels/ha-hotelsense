from __future__ import annotations

import logging

from collections.abc import Callable
from dataclasses import dataclass
from typing import Dict

from homeassistant.components.binary_sensor import (
    DOMAIN,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .controller import OmadaController

from .const import DOMAIN as OMADA_DOMAIN
from .omada_controller_entity import (OmadaControllerEntity, OmadaControllerEntityDescription,
                                      device_info_fn as controller_device_info_fn,
                                      unique_id_fn as controller_unique_id_fn)
from .ids import NS_CLIENT
from .omada_entity import (OmadaEntity, OmadaEntityDescription, client_device_info_fn)

AI_OPTIMIZATION_SENSOR = "ai_optimization"
POWER_SAVE_SENSOR = "power_save"

LOGGER = logging.getLogger(__name__)


@callback
def rf_planning_state_value_fn(controller: OmadaController) -> bool | None:
    """Retrieve AI Optimization Status"""
    rf_planning_state = controller.api.rf_planning
    if rf_planning_state is not None:
        return rf_planning_state.status == 2
    else:
        return None


@callback
def client_power_save_value_fn(controller: OmadaController, mac: str) -> bool | None:
    """Retrieve client power save state"""

    if mac in controller.api.clients:
        return controller.api.clients[mac].power_save
    else:
        return None


@dataclass
class OmadaControllerBinarySensorEntityDescriptionMixin:
    value_fn: Callable[[OmadaController], bool | None]


@dataclass
class OmadaControllerBinarySensorEntityDescription(
    BinarySensorEntityDescription,
    OmadaControllerEntityDescription,
    OmadaControllerBinarySensorEntityDescriptionMixin,
):
    """Omada Controller Binary Sensor Entity Description"""

    value_fn: Callable[[OmadaController], bool | None]


@dataclass
class OmadaBinarySensorEntityDescriptionMixin:
    value_fn: Callable[[OmadaController], bool | None]


@dataclass
class OmadaBinarySensorEntityDescription(
    BinarySensorEntityDescription,
    OmadaEntityDescription,
    OmadaBinarySensorEntityDescriptionMixin,
):
    """Omada Controller Binary Sensor Entity Description"""

    value_fn: Callable[[OmadaController], bool | None]


CONTROLLER_ENTITY_DESCRIPTIONS: dict[
    str, OmadaControllerBinarySensorEntityDescription
] = {
    AI_OPTIMIZATION_SENSOR: OmadaControllerBinarySensorEntityDescription(
        domain=DOMAIN,
        key=AI_OPTIMIZATION_SENSOR,
        entity_category=EntityCategory.DIAGNOSTIC,
        has_entity_name=True,
        icon="mdi:chart-box",
        available_fn=lambda controller: controller.available,
        device_info_fn=controller_device_info_fn,
        name_fn=lambda *_: "WLAN Optimization Running",
        unique_id_fn=controller_unique_id_fn,
        value_fn=rf_planning_state_value_fn,
    )
}

CLIENT_ENTITY_DESCRIPTIONS: Dict[str, OmadaBinarySensorEntityDescription] = {
    POWER_SAVE_SENSOR: OmadaBinarySensorEntityDescription(
        domain=DOMAIN,
        key=POWER_SAVE_SENSOR,
        entity_category=EntityCategory.DIAGNOSTIC,
        has_entity_name=True,
        icon="mdi:leaf",
        allowed_fn=lambda controller, mac: (controller.option_device_clients_sensors and
                                            controller.option_track_clients and
                                            controller.is_client_allowed(mac)),
        supported_fn=lambda *_: True,
        available_fn=lambda controller, _: controller.available,
        device_info_fn=client_device_info_fn,
        name_fn=lambda *_: "Power Save",
        namespace=NS_CLIENT,
        value_fn=client_power_save_value_fn
    )
}


async def async_setup_entry(hass, config_entry, async_add_entities):
    controller: OmadaController = hass.data[OMADA_DOMAIN][config_entry.entry_id]

    # Set up Controller Entities
    for description in CONTROLLER_ENTITY_DESCRIPTIONS.values():
        entity = OmadaControllerBinarySensorEntity(controller, description)
        async_add_entities([entity])

    @callback
    def items_added() -> None:

        if controller.option_track_clients:
            controller.register_platform_entities(
                controller.api.clients,
                OmadaBinarySensorEntity,
                CLIENT_ENTITY_DESCRIPTIONS,
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
            OmadaBinarySensorEntity,
            CLIENT_ENTITY_DESCRIPTIONS,
            config_entry,
            async_add_entities
        )

    items_added()


class OmadaControllerBinarySensorEntity(OmadaControllerEntity, BinarySensorEntity):
    controller: OmadaController
    entity_description: OmadaControllerBinarySensorEntityDescription

    def __init__(
        self, controller: OmadaController, description: OmadaControllerEntityDescription
    ) -> None:

        super().__init__(controller, description)

        self.update_value(force_update=True)

    def update_value(self, force_update=False) -> bool:
        """Update value. Returns true if state should update."""
        prev_value = self._attr_is_on
        next_value = self.entity_description.value_fn(self.controller)

        if prev_value != next_value:
            self._attr_is_on = next_value
            return True

        return False

    @callback
    async def async_update(self):
        if self.update_value():
            await super().async_update()


class OmadaBinarySensorEntity(OmadaEntity, BinarySensorEntity):

    entity_description: OmadaBinarySensorEntityDescription

    def __init__(self, mac: str, controller: OmadaController, description: OmadaEntityDescription) -> None:

        super().__init__(mac, controller, description)
        self.update_value(force_update=True)

    def update_value(self, force_update=False) -> bool:
        """Update value. Returns true if state should update."""
        prev_value = self._attr_is_on
        next_value = self.entity_description.value_fn(self.controller, self._mac)

        if prev_value != next_value:
            self._attr_is_on = next_value
            return True

        return False

    @callback
    async def async_update(self):
        if self.update_value():
            await super().async_update()
