"""Hotel Sense services: AP -> Area diagnostics/assignment, device list CSV."""
from __future__ import annotations

from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.util import dt as dt_util

from .areas import (
    access_point_macs, ap_area_report, assign_ap_areas, find_access_points, parse_mac_table,
    resolve_ap_areas,
)
from .const import DOMAIN
from .device_list import CATEGORIES
from .mac import parse_mac
from .omada_hub import OmadaClientException
from .omada_known import omada_candidates
from .history import HistoryUnavailable
from .queries import device_candidates, device_route, hotel_report, room_report
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
SERVICE_EXELY_API_PROBE = "exely_api_probe"
SERVICE_ROOM_REPORT = "room_report"
SERVICE_HOTEL_REPORT = "hotel_report"
SERVICE_DEVICE_ROUTE = "device_route"
SERVICE_DEVICE_CANDIDATES = "device_candidates"
SERVICE_OMADA_KNOWN_DEVICES = "omada_known_devices"
SERVICE_LINK_MAC = "link_mac"
SERVICE_UNLINK_MAC = "unlink_mac"
ATTR_BOOKING = "booking"
ATTR_ROOM = "room"
ATTR_START = "start"
ATTR_END = "end"
ATTR_HOURS = "hours"
ATTR_DAYS = "days"
ATTR_MIN_DAYS = "min_days"
ATTR_MIN_ROOMS_PER_DAY = "min_rooms_per_day"
ATTR_MIN_HOURS = "min_hours"
ATTR_SEEN_DAYS = "seen_days"
ATTR_WIRED = "wired"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_MAPPING = "mapping"
ATTR_CSV = "csv"
ATTR_CREATE_MISSING_AREAS = "create_missing_areas"
ATTR_REPLACE = "replace"
ATTR_DEFAULT_CATEGORY = "default_category"
ATTR_MAC = "mac"
ATTR_IDENTITY = "identity"
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
                "devices": [d.as_dict() for d in store.devices],
                "identities": [i.as_dict() for i in store.devices.identities()]}

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

    async def exely_api_probe(call: ServiceCall) -> ServiceResponse:
        receiver = _controller(hass, call).exely
        if not receiver.api.configured or not receiver.property_id:
            raise ServiceValidationError("Exely API is not set up (Hotel Sense options)")
        booking = (call.data.get(ATTR_BOOKING) or "").strip() or None
        return await receiver.api.async_probe(receiver.property_id, booking)

    def _period(call: ServiceCall):
        end = dt_util.as_utc(call.data.get(ATTR_END) or dt_util.utcnow())
        start = call.data.get(ATTR_START)
        start = dt_util.as_utc(start) if start else end - timedelta(hours=call.data[ATTR_HOURS])
        if start >= end:
            raise ServiceValidationError("start must be before end")
        return start, end

    async def _read(controller, func):
        if controller.history is None:
            raise ServiceValidationError("The history database is not set up (Hotel Sense options)")
        try:
            return await controller.history.async_read(func)
        except HistoryUnavailable as err:
            raise HomeAssistantError(f"History database unavailable: {err}") from err

    async def room_report_service(call: ServiceCall) -> ServiceResponse:
        controller = _controller(hass, call)
        registry = controller.presence.registry
        wanted = call.data[ATTR_ROOM].strip().lower()
        room = next((r for r in registry.rooms.values()
                     if wanted in (r.room_id.lower(), r.name.lower(), (r.number or "").lower())),
                    None)
        if room is None:
            raise ServiceValidationError(f"Unknown room {call.data[ATTR_ROOM]!r}")
        start, end = _period(call)
        devices = (await async_get_device_store(hass)).devices

        def name(mac: str) -> str | None:
            known = devices.get(mac)  # names of the hotel's own devices only
            return known.name if known else None

        report = await _read(controller, lambda conn: room_report(conn, room.room_id, start, end,
                                                                  name))
        status = room.status
        return {"room": {"room_id": room.room_id, "number": room.number, "name": room.name,
                         "kind": room.kind,
                         "status": None if status is None else {
                             "value": status.value, "source": status.source,
                             "changed_at": status.changed_at, "booking": status.booking}},
                **report}

    async def hotel_report_service(call: ServiceCall) -> ServiceResponse:
        controller = _controller(hass, call)
        start, end = _period(call)
        return await _read(controller, lambda conn: hotel_report(conn, start, end))

    async def device_route_service(call: ServiceCall) -> ServiceResponse:
        controller = _controller(hass, call)
        devices = (await async_get_device_store(hass)).devices
        if call.data.get(ATTR_IDENTITY):
            identity = devices.identity(call.data[ATTR_IDENTITY])
            if identity is None:
                raise ServiceValidationError(f"Unknown device {call.data[ATTR_IDENTITY]!r}")
            macs = list(identity.macs)
        elif call.data.get(ATTR_MAC):
            macs = [_mac(call)]
            identity = devices.identity_of(macs[0])
        else:
            raise ServiceValidationError("Give a mac or a device identity")
        start, end = _period(call)
        current = [s for mac in macs if (s := controller.presence.sessions.get(mac))]
        route = await _read(controller, lambda conn: device_route(conn, macs, start, end, current))
        return {"identity": identity.id if identity else None,
                "name": identity.name if identity else None,
                "category": identity.category if identity else None, **route}

    async def link_mac(call: ServiceCall) -> ServiceResponse:
        store = await async_get_device_store(hass)
        mac = _mac(call)
        try:
            before = await store.async_link_mac(call.data[ATTR_IDENTITY], mac)
        except KeyError as err:
            raise ServiceValidationError(err.args[0]) from err
        return {"mac": mac, "previous_identity": before,
                "identity": store.devices.identity_of(mac).as_dict()}

    async def unlink_mac(call: ServiceCall) -> ServiceResponse:
        store = await async_get_device_store(hass)
        mac = _mac(call)
        identity = store.devices.identity_of(mac)
        if identity is None:
            raise ServiceValidationError(f"{mac} is not on the device list")
        await store.async_remove([mac])
        return {"mac": mac, "previous_identity": identity.id,
                "identity_removed": store.devices.identity(identity.id) is None}

    async def device_candidates_service(call: ServiceCall) -> ServiceResponse:
        controller = _controller(hass, call)
        end = dt_util.utcnow()
        start = end - timedelta(days=call.data[ATTR_DAYS])
        known = {d.mac for d in (await async_get_device_store(hass)).devices}
        return await _read(controller, lambda conn: device_candidates(
            conn, start, end, known, min_days=call.data[ATTR_MIN_DAYS],
            min_rooms_per_day=call.data[ATTR_MIN_ROOMS_PER_DAY]))

    async def omada_known_devices(call: ServiceCall) -> ServiceResponse:
        controller = _controller(hass, call)
        clients = await _omada(controller.hub.async_known_clients(), "reading known clients")
        known = {d.mac for d in (await async_get_device_store(hass)).devices}
        return omada_candidates(clients, known, dt_util.utcnow().timestamp(),
                                min_hours=call.data[ATTR_MIN_HOURS],
                                seen_days=call.data[ATTR_SEEN_DAYS], wired=call.data[ATTR_WIRED])

    period_fields = {
        vol.Optional(ATTR_START): cv.datetime,
        vol.Optional(ATTR_END): cv.datetime,
        vol.Optional(ATTR_HOURS, default=24): vol.All(vol.Coerce(int), vol.Range(min=1, max=24 * 366)),
    }
    hass.services.async_register(
        DOMAIN, SERVICE_ROOM_REPORT, room_report_service,
        schema=vol.Schema({**entry_field, vol.Required(ATTR_ROOM): cv.string, **period_fields}),
        supports_response=SupportsResponse.ONLY)
    hass.services.async_register(
        DOMAIN, SERVICE_HOTEL_REPORT, hotel_report_service,
        schema=vol.Schema({**entry_field, **period_fields}),
        supports_response=SupportsResponse.ONLY)
    hass.services.async_register(
        DOMAIN, SERVICE_DEVICE_ROUTE, device_route_service,
        schema=vol.Schema({**entry_field, vol.Optional(ATTR_MAC): cv.string,
                           vol.Optional(ATTR_IDENTITY): cv.string, **period_fields}),
        supports_response=SupportsResponse.ONLY)
    hass.services.async_register(
        DOMAIN, SERVICE_LINK_MAC, link_mac,
        schema=vol.Schema({vol.Required(ATTR_IDENTITY): cv.string,
                           vol.Required(ATTR_MAC): cv.string}),
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(
        DOMAIN, SERVICE_UNLINK_MAC, unlink_mac,
        schema=vol.Schema({vol.Required(ATTR_MAC): cv.string}),
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(
        DOMAIN, SERVICE_DEVICE_CANDIDATES, device_candidates_service,
        schema=vol.Schema({
            **entry_field,
            vol.Optional(ATTR_DAYS, default=14): vol.All(vol.Coerce(int), vol.Range(min=1, max=366)),
            vol.Optional(ATTR_MIN_DAYS, default=5): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional(ATTR_MIN_ROOMS_PER_DAY, default=3): vol.All(vol.Coerce(int),
                                                                    vol.Range(min=2)),
        }),
        supports_response=SupportsResponse.ONLY)
    hass.services.async_register(
        DOMAIN, SERVICE_OMADA_KNOWN_DEVICES, omada_known_devices,
        schema=vol.Schema({
            **entry_field,
            vol.Optional(ATTR_MIN_HOURS, default=500): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional(ATTR_SEEN_DAYS, default=30): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional(ATTR_WIRED, default=False): cv.boolean,
        }),
        supports_response=SupportsResponse.ONLY)
    hass.services.async_register(
        DOMAIN, SERVICE_EXELY_API_PROBE, exely_api_probe,
        schema=vol.Schema({**entry_field, vol.Optional(ATTR_BOOKING): cv.string}),
        supports_response=SupportsResponse.ONLY)
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
