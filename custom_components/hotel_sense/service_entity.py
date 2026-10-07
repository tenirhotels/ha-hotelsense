"""Service health: is each data source working?

Omada controller reachable, last Omada webhook message, history database
connected, Exely API working. Diagnostic entities on the controller device,
meant for notifications ("controller down", "no webhook for an hour" ...).
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory

from .ap_entity import ControllerEntity

# Polled state (webhook, database, Exely API): cheap, in memory.
SCAN_INTERVAL = timedelta(seconds=30)

EXELY_NOT_SET_UP = "not_set_up"
EXELY_OK = "ok"
EXELY_ERROR = "error"


class _ServiceEntity:
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, controller, key: str) -> None:
        self.controller = controller
        self._attr_translation_key = key
        self._attr_unique_id = f"{key}-{controller.controller_id}"
        self._attr_device_info = controller.controller_device_info()


class ControllerOnlineSensor(ControllerEntity, BinarySensorEntity):
    """The Omada controller answers the polls."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "controller_online"

    def __init__(self, controller) -> None:
        super().__init__(controller)
        self._attr_unique_id = f"controller_online-{controller.controller_id}"
        self._attr_device_info = controller.controller_device_info()

    @property
    def available(self) -> bool:
        return True  # "off" is the information, not "unavailable"

    @property
    def is_on(self) -> bool:
        return self.controller.available


class OmadaWebhookSensor(_ServiceEntity, SensorEntity):
    """Time of the last authenticated Omada webhook message."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_should_poll = True

    def __init__(self, controller) -> None:
        super().__init__(controller, "omada_webhook_last_message")

    @property
    def native_value(self):
        return self.controller.omada_webhook.last_received

    @property
    def extra_state_attributes(self) -> dict:
        hook = self.controller.omada_webhook
        return {"received": hook.received, "rejected": hook.rejected}


class HistoryDatabaseSensor(_ServiceEntity, BinarySensorEntity):
    """The history database takes the rows (only when it is set up)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = True

    def __init__(self, controller) -> None:
        super().__init__(controller, "history_database")

    @property
    def is_on(self) -> bool:
        return self.controller.history.connected

    @property
    def extra_state_attributes(self) -> dict:
        info = self.controller.history.diagnostics()
        return {k: info[k] for k in ("queued", "written", "dropped", "last_write", "last_error",
                                     "retention_months")}


class ExelyApiSensor(_ServiceEntity, SensorEntity):
    """not_set_up / ok / error (sign-in, reservation or room list failing)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [EXELY_NOT_SET_UP, EXELY_OK, EXELY_ERROR]
    _attr_should_poll = True

    def __init__(self, controller) -> None:
        super().__init__(controller, "exely_api")

    @property
    def native_value(self) -> str:
        api = self.controller.exely.api
        if not api.configured:
            return EXELY_NOT_SET_UP
        return EXELY_ERROR if api.last_error or api.rooms_error else EXELY_OK

    @property
    def extra_state_attributes(self) -> dict:
        api = self.controller.exely.api
        return {"last_error": api.last_error, "rooms_error": api.rooms_error,
                "last_check": api.last_check, "requests_last_hour": api.requests_last_hour()}


class DeviceSuggestionsSensor(_ServiceEntity, SensorEntity):
    """Number of open device identity suggestions ("new MAC is probably #7")."""

    _attr_should_poll = False

    def __init__(self, controller) -> None:
        super().__init__(controller, "device_suggestions")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, self.controller.suggestions.signal, self.async_write_ha_state))

    @property
    def native_value(self) -> int:
        return len(self.controller.suggestions.suggestions)

    @property
    def extra_state_attributes(self) -> dict:
        manager = self.controller.suggestions
        return {"status": manager.status, "updated": manager.updated, "error": manager.error,
                "suggestions": [{k: s[k] for k in ("mac", "name", "identity", "identity_name",
                                                   "score")}
                                for s in manager.suggestions[:10]]}
