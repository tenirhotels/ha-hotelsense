"""Hotel Sense services: AP -> Area diagnostics/assignment, device list CSV."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv

from .areas import (
    access_point_macs, ap_area_report, assign_ap_areas, find_access_points, parse_mac_table,
    resolve_ap_areas,
)
from .const import DOMAIN
from .device_list import CATEGORIES
from .mac import parse_mac
from .omada_hub import OmadaClientException
from .storage import async_get_device_store

SERVICE_AP_AREA_REPORT = "ap_area_report"
SERVICE_ASSIGN_AP_AREAS = "assign_ap_areas"
SERVICE_IMPORT_DEVICES = "import_devices"
SERVICE_EXPORT_DEVICES = "export_devices"
SERVICE_RECONNECT_CLIENT = "reconnect_client"
SERVICE_BLOCK_CLIENT = "block_client"
SERVICE_UNBLOCK_CLIENT = "unblock_client"
SERVICE_AP_SSIDS = "ap_ssids"
SERVICE_SET_AP_SSID = "set_ap_ssid"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_MAPPING = "mapping"
ATTR_CSV = "csv"
ATTR_CREATE_MISSING_AREAS = "create_missing_areas"
ATTR_REPLACE = "replace"
ATTR_DEFAULT_CATEGORY = "default_category"
ATTR_MAC = "mac"
ATTR_ACCESS_POINT = "access_point"
ATTR_SSID = "ssid"
ATTR_ENABLED = "enabled"

_TABLE_SCHEMA = {
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Optional(ATTR_MAPPING): vol.Schema({cv.string: cv.string}),
    vol.Optional(ATTR_CSV): cv.string,
}


def _controller(hass: HomeAssistant, call: ServiceCall):
    controllers = hass.data.get(DOMAIN, {})
    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED or entry_id not in controllers:
            raise ServiceValidationError(f"Hotel Sense entry {entry_id} is not loaded")
        return controllers[entry_id]
    if len(controllers) != 1:
        raise ServiceValidationError(
            "Specify config_entry_id" if controllers else "Hotel Sense is not loaded")
    return next(iter(controllers.values()))


def _mac(call: ServiceCall) -> str:
    try:
        return parse_mac(call.data[ATTR_MAC])
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err


def _access_points(hass: HomeAssistant, controller, value: str) -> list[str]:
    if not (macs := find_access_points(hass, controller, value)):
        raise ServiceValidationError(
            f"No access point matches {value!r} (MAC, access point name or room)")
    return macs


async def _omada(coro, what: str):
    """Run a controller command; its failures become a readable error."""
    try:
        return await coro
    except OmadaClientException as err:
        raise HomeAssistantError(f"Omada controller: {what} failed: {err}") from err


def _table(call: ServiceCall) -> tuple[dict[str, str], list[str]]:
    return parse_mac_table(call.data.get(ATTR_MAPPING), call.data.get(ATTR_CSV))


def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_AP_AREA_REPORT):
        return

    async def report(call: ServiceCall) -> ServiceResponse:
        controller = _controller(hass, call)
        table, errors = _table(call)
        result = ap_area_report(hass, controller, table)
        result["input_errors"] = errors
        return result

    async def assign(call: ServiceCall) -> ServiceResponse:
        controller = _controller(hass, call)
        table, errors = _table(call)
        if not table:
            raise ServiceValidationError("Provide 'mapping' or 'csv' with MAC -> room rows")
        result = assign_ap_areas(hass, controller, table,
                                 create_missing_areas=call.data[ATTR_CREATE_MISSING_AREAS])
        result["errors"] = errors + result["errors"]
        controller.presence.async_process()
        return result

    async def import_devices(call: ServiceCall) -> ServiceResponse:
        store = await async_get_device_store(hass)
        result = await store.async_import_csv(
            call.data[ATTR_CSV], replace=call.data[ATTR_REPLACE],
            default_category=call.data.get(ATTR_DEFAULT_CATEGORY))
        return result.as_dict()

    async def export_devices(call: ServiceCall) -> ServiceResponse:
        store = await async_get_device_store(hass)
        return {"csv": store.devices.export_csv(),
                "devices": [d.as_dict() for d in store.devices]}

    def client_command(method: str, what: str):
        async def handler(call: ServiceCall) -> None:
            controller = _controller(hass, call)
            mac = _mac(call)
            await _omada(getattr(controller.hub, method)(mac), f"{what} {mac}")
        return handler

    async def ap_ssids(call: ServiceCall) -> ServiceResponse:
        controller = _controller(hass, call)
        names = access_point_macs(controller)
        macs = (_access_points(hass, controller, call.data[ATTR_ACCESS_POINT])
                if call.data.get(ATTR_ACCESS_POINT) else sorted(names, key=lambda m: names[m]))
        areas = resolve_ap_areas(hass, controller)
        result = []
        for mac in macs:
            ssids = await _omada(controller.hub.async_ap_ssids(mac), f"reading SSIDs of {mac}")
            result.append({"mac": mac, "name": names.get(mac), "area": areas.get(mac),
                           "ssids": ssids})
        return {"access_points": result}

    async def set_ap_ssid(call: ServiceCall) -> ServiceResponse:
        controller = _controller(hass, call)
        ssid, enabled = call.data[ATTR_SSID], call.data[ATTR_ENABLED]
        changed, unchanged = [], []
        for mac in _access_points(hass, controller, call.data[ATTR_ACCESS_POINT]):
            try:
                done = await _omada(controller.hub.async_set_ap_ssid(mac, ssid, enabled),
                                    f"setting SSID {ssid} on {mac}")
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
            (changed if done else unchanged).append(mac)
        return {"changed": changed, "unchanged": unchanged}

    entry_field = {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}
    for service, method, what in (
            (SERVICE_RECONNECT_CLIENT, "async_reconnect_client", "reconnect"),
            (SERVICE_BLOCK_CLIENT, "async_block_client", "block"),
            (SERVICE_UNBLOCK_CLIENT, "async_unblock_client", "unblock")):
        hass.services.async_register(
            DOMAIN, service, client_command(method, what),
            schema=vol.Schema({**entry_field, vol.Required(ATTR_MAC): cv.string}))
    hass.services.async_register(
        DOMAIN, SERVICE_AP_SSIDS, ap_ssids,
        schema=vol.Schema({**entry_field, vol.Optional(ATTR_ACCESS_POINT): cv.string}),
        supports_response=SupportsResponse.ONLY)
    hass.services.async_register(
        DOMAIN, SERVICE_SET_AP_SSID, set_ap_ssid,
        schema=vol.Schema({**entry_field, vol.Required(ATTR_ACCESS_POINT): cv.string,
                           vol.Required(ATTR_SSID): cv.string,
                           vol.Required(ATTR_ENABLED): cv.boolean}),
        supports_response=SupportsResponse.OPTIONAL)

    hass.services.async_register(
        DOMAIN, SERVICE_AP_AREA_REPORT, report, schema=vol.Schema(_TABLE_SCHEMA),
        supports_response=SupportsResponse.ONLY)
    hass.services.async_register(
        DOMAIN, SERVICE_ASSIGN_AP_AREAS, assign,
        schema=vol.Schema({**_TABLE_SCHEMA,
                           vol.Optional(ATTR_CREATE_MISSING_AREAS, default=False): cv.boolean}),
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT_DEVICES, import_devices,
        schema=vol.Schema({
            vol.Required(ATTR_CSV): cv.string,
            vol.Optional(ATTR_REPLACE, default=False): cv.boolean,
            vol.Optional(ATTR_DEFAULT_CATEGORY): vol.In(CATEGORIES),
        }),
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(
        DOMAIN, SERVICE_EXPORT_DEVICES, export_devices, schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY)
