"""Sensors: rooms (device counts, state), access points (uptime, clients),
site clients, misplaced equipment and the last Exely event."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ap_entity import (
    AccessPointClientsSensor, AccessPointUptimeSensor, ControllerClientsSensor,
    async_setup_ap_platform,
)
from .const import DOMAIN
from .controller import OmadaController
from .service_entity import DeviceSuggestionsSensor, ExelyApiSensor, OmadaWebhookSensor
from .room_entity import (
    ExelyLastEventSensor, MisplacedFixedDevicesSensor, async_setup_room_platform, sensor_factory,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    controller: OmadaController = hass.data[DOMAIN][entry.entry_id]
    device_info = controller.controller_device_info()
    async_add_entities([
        ControllerClientsSensor(controller),
        MisplacedFixedDevicesSensor(controller.presence, device_info),
        ExelyLastEventSensor(controller.exely, device_info),
        OmadaWebhookSensor(controller),
        ExelyApiSensor(controller),
        DeviceSuggestionsSensor(controller),
    ])
    async_setup_ap_platform(hass, entry, async_add_entities, lambda c, mac: [
        AccessPointUptimeSensor(c, mac), AccessPointClientsSensor(c, mac)])
    async_setup_room_platform(hass, entry, async_add_entities, sensor_factory)
