"""Access point and controller entities: uptime, connected clients, status.

Unique IDs are unchanged from Hotel Sense 0.2 (``ap:<site>:<mac>:uptime`` ...,
``clients-<controller id>``), so existing entities, their history and the
access points' Areas carry over.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import timedelta

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CLIENTS, DOMAIN
from .controller import OmadaController
from .ids import NS_AP, make_unique_id

# An access point's boot time moves by a second or two between polls (uptime is
# rounded): only a jump beyond this counts as a reboot.
_REBOOT_TOLERANCE = timedelta(minutes=1)


class ControllerEntity(Entity):
    """State pushed after every controller poll."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller: OmadaController) -> None:
        self.controller = controller

    @property
    def available(self) -> bool:
        return self.controller.available

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, self.controller.signal_update, self._handle_update))
        self._handle_update(write=False)

    @callback
    def _handle_update(self, write: bool = True) -> None:
        if write:
            self.async_write_ha_state()


class AccessPointEntity(ControllerEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _key: str

    def __init__(self, controller: OmadaController, mac: str) -> None:
        super().__init__(controller)
        self.mac = mac
        self._attr_unique_id = make_unique_id(NS_AP, controller.site_id, mac, self._key)
        self._attr_device_info = controller.ap_device_info(mac)

    @property
    def ap(self):
        return self.controller.access_points.get(self.mac)

    @property
    def available(self) -> bool:
        return super().available and self.ap is not None


class AccessPointUptimeSensor(AccessPointEntity, SensorEntity):
    _key = "uptime"
    _attr_translation_key = "ap_uptime"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @callback
    def _handle_update(self, write: bool = True) -> None:
        ap = self.ap
        if ap is None or not ap.online or ap.uptime is None:
            boot = None
        else:
            boot = dt_util.utcnow() - timedelta(seconds=ap.uptime)
            old = self._attr_native_value
            if old is not None and abs(boot - old) < _REBOOT_TOLERANCE:
                boot = old  # same boot: keep the state steady
        self._attr_native_value = boot
        super()._handle_update(write)


class AccessPointClientsSensor(AccessPointEntity, SensorEntity):
    _key = "clients"
    _attr_translation_key = "ap_clients"
    _attr_native_unit_of_measurement = CLIENTS
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | None:
        return self.ap.clients if self.ap else None


class AccessPointStatusSensor(AccessPointEntity, BinarySensorEntity):
    """Is the access point connected to the controller (an offline AP blinds its room)."""

    _key = "status"
    _attr_translation_key = "ap_status"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool | None:
        return self.ap.online if self.ap else None


class ControllerClientsSensor(ControllerEntity, SensorEntity):
    """Connected clients of the whole site."""

    _attr_translation_key = "controller_clients"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = CLIENTS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, controller: OmadaController) -> None:
        super().__init__(controller)
        self._attr_unique_id = f"clients-{controller.controller_id}"
        self._attr_device_info = controller.controller_device_info()

    @property
    def native_value(self) -> int:
        return len(self.controller.clients)


def async_setup_ap_platform(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[OmadaController, str], Iterable[Entity]],
) -> None:
    """Add ``factory`` entities for every access point now and when one appears."""
    controller: OmadaController = hass.data[DOMAIN][entry.entry_id]
    added: set[str] = set()

    @callback
    def aps_changed() -> None:
        new = [mac for mac in controller.access_points if mac not in added]
        added.update(new)
        entities = [e for mac in new for e in factory(controller, mac)]
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(async_dispatcher_connect(hass, controller.signal_update, aps_changed))
    aps_changed()
