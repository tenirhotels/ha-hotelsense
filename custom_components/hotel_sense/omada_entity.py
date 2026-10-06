from __future__ import annotations

import logging

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.entity_registry import async_entries_for_device
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import Entity, EntityDescription, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import ATTR_MANUFACTURER as ATTR_OMADA_MANUFACTURER, DOMAIN as OMADA_DOMAIN
from .ids import NS_AP, NS_CLIENT, make_unique_id
from .api.controller import Controller
from .api.clients import Client
from .api.devices import Device

if TYPE_CHECKING:
    from .controller import OmadaController


LOGGER = logging.getLogger(__name__)

@callback
def entity_available_fn(controller: OmadaController, mac: str) -> bool:
    return controller.available


@callback
def device_device_info_fn(api: Controller, mac: str) -> DeviceInfo:
    """Device registry info for an access point (infrastructure / location source)."""
    device: Device = api.devices[mac]

    return DeviceInfo(
        identifiers={(OMADA_DOMAIN, make_unique_id(NS_AP, api.site_id, mac))},
        connections={(CONNECTION_NETWORK_MAC, mac)},
        manufacturer=ATTR_OMADA_MANUFACTURER,
        model=device.model,
        sw_version=device.firmware,
        name=device.name,
    )


@callback
def client_device_info_fn(api: Controller, mac: str) -> DeviceInfo:
    """Device registry info for an observed client.

    Clients get their own namespaced identifier so they can never be merged with
    an AP device. The MAC connection is omitted if the MAC is currently an AP, as
    the registry keeps one device per connection within a config entry.
    """
    client: Client = api.known_clients[mac]

    info = DeviceInfo(
        identifiers={(OMADA_DOMAIN, make_unique_id(NS_CLIENT, api.site_id, mac))},
        # Plain `name`: `default_name` is deprecated and removed in HA 2027.9.
        name=client.name or mac,
    )
    if mac not in api.devices:
        info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
    return info


@dataclass
class OmadaDescriptionMixin():
    domain: str
    allowed_fn: Callable[[OmadaController, str], bool]
    supported_fn: Callable[[OmadaController, str], bool]
    available_fn: Callable[[OmadaController, str], bool]
    device_info_fn: Callable[[Controller, str], bool]
    name_fn: Callable[[Controller, str, str], str]
    namespace: str  # ids.NS_AP / ids.NS_CLIENT / ids.NS_UPDATE


@dataclass
class OmadaEntityDescription(EntityDescription, OmadaDescriptionMixin):
    """Omada enitty description"""

    @property
    def bucket(self) -> str:
        """Key for the controller's in-memory entity index.

        Namespaced so AP and client descriptions that share a key (``download``,
        ``rx``, ``clients`` ...) never share an index bucket.
        """
        return f"{self.namespace}:{self.key}"


# One tracker / one firmware-update entity exists per AP or client, so their
# unique IDs carry no description key (ТЗ 6.2: ``ap:<site>:<mac>``).
PRIMARY_ENTITY_DOMAINS = (Platform.DEVICE_TRACKER, Platform.UPDATE)


class OmadaEntity(Entity):

    entity_description: OmadaEntityDescription

    _attr_should_poll = False
    _attr_unique_id: str

    def __init__(self, mac: str, controller: OmadaController, description: OmadaEntityDescription) -> None:

        self._mac = mac
        self.controller = controller
        self.entity_description = description

        self._attr_available = description.available_fn(controller, mac)
        self._attr_device_info = description.device_info_fn(
            controller.api, mac)
        self._attr_unique_id = make_unique_id(
            description.namespace,
            controller.site_id,
            mac,
            None if description.domain in PRIMARY_ENTITY_DOMAINS else description.key,
        )
        self._attr_name = description.name_fn(controller.api, mac, description.key)

        self.controller.entities[self.entity_description.domain][self.entity_description.bucket].add(self._mac)

    async def async_added_to_hass(self) -> None:
        for signal, method in (
            (self.controller.signal_options_update, self.options_updated),
            (self.controller.signal_update, self.handle_signal_update)
        ):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, method))

    async def async_will_remove_from_hass(self) -> None:
        self.controller.entities[self.entity_description.domain][self.entity_description.bucket].remove(
            self._mac)
        
    @callback
    async def handle_signal_update(self):
        if (self._mac not in self.controller.api.known_clients and
            self._mac not in self.controller.api.devices and
            self._mac not in self.controller.api.clients):
            await self.remove() # Remove entity if device no longer exists in Omada
        else:
            await self.async_update()

    @callback
    async def async_update(self):
        self.async_write_ha_state()

    @callback
    async def options_updated(self):
        """Remove entity if options updated to disable entity type"""
        if not self.entity_description.allowed_fn(self.controller, self._mac):
            await self.remove()

    async def remove(self):
        er = entity_registry.async_get(self.hass)

        entity_entry = er.async_get(self.entity_id)
        if not entity_entry:
            await self.async_remove(force_remove=True)
            return

        dr = device_registry.async_get(self.hass)
        device_entry = dr.async_get(entity_entry.device_id)
        if not device_entry:
            await self.async_remove(force_remove=True)
            er.async_remove(self.entity_id)
            return

        if (
            len(
                entries_for_device := async_entries_for_device(
                    er,
                    entity_entry.device_id,
                    include_disabled_entities=True,
                )
            )
        ) == 1:
            er.async_remove(self.entity_id)
            dr.async_remove_device(device_entry.id)
            await self.async_remove(force_remove=True)
            return

        if (
            len(
                entries_for_device_from_this_config_entry := [
                    entry_for_device
                    for entry_for_device in entries_for_device
                    if entry_for_device.config_entry_id
                    == self.controller._config_entry.entry_id
                ]
            )
            != len(entries_for_device)
            and len(entries_for_device_from_this_config_entry) == 1
        ):
            dr.async_update_device(
                entity_entry.device_id,
                remove_config_entry_id=self.controller._config_entry.entry_id,
            )

        er.async_remove(self.entity_id)
        await self.async_remove(force_remove=True)
