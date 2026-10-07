from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_URL, CONF_USERNAME, CONF_PASSWORD, CONF_VERIFY_SSL
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

import asyncio

from aiohttp import ClientSSLError

from .const import (
    DOMAIN as OMADA_DOMAIN,
    CONF_SITE,
    CONF_SCAN_INTERVAL,
    CONF_PRESENCE_TIMEOUT,
    CONF_ROAMING_DEBOUNCE,
    CONF_MIN_RSSI,
    CONF_COMMON_AREAS,
    CONF_SLEEP_TIMEOUT,
    DEFAULT_SLEEP_TIMEOUT,
    CONF_DB_HOST,
    CONF_DB_NAME,
    CONF_DB_PASSWORD,
    CONF_DB_PORT,
    CONF_DB_RETENTION,
    CONF_DB_USERNAME,
    DB_KEYS,
    DEFAULT_DB_HOST,
    DEFAULT_DB_NAME,
    DEFAULT_DB_PORT,
    CONF_EXELY_CLIENT_ID,
    CONF_EXELY_CLIENT_SECRET,
    CONF_EXELY_PROPERTY_ID,
    CONF_EXELY_NEW_KEY,
    CONF_EXELY_NEW_URL,
    CONF_EXELY_ROOM_MAP,
    CONF_OMADA_NEW_SECRET,
    LEGACY_OPTIONS,
    DEFAULT_PRESENCE_TIMEOUT,
    DEFAULT_ROAMING_DEBOUNCE,
    DEFAULT_MIN_RSSI,
)
from .areas import resolve_ap_areas
from .controller import OmadaController, build_hub
from .omada_hub import (
    BadControllerUrl, ConnectionFailed, LoginFailed, OmadaClientException, OmadaHub,
    SiteNotFound, UnsupportedControllerVersion,
)
from .device_kind import LISTABLE_KINDS
from .exely import parse_room_map
from .exely_api import ExelyApiError, ExelyAuthError, ExelyNotFound
from . import history
from .device_list import CATEGORIES, CATEGORY_FIXED, KnownDevice
from .storage import async_get_device_store

LOGGER = logging.getLogger(__name__)

CONF_MAC = "mac"
CONF_NAME = "name"
CONF_CATEGORY = "category"
CONF_OWNER = "owner"
CONF_NOTE = "note"
CONF_ROOM = "room"
CONF_DEVICE_TYPE = "device_type"
CONF_DEVICES = "devices"
CONF_CSV = "csv"
CONF_REPLACE = "replace"
CONF_DEFAULT_CATEGORY = "default_category"




async def async_validate_connection(hass, data: dict[str, Any]) -> tuple[OmadaHub | None, str | None]:
    """Log in with ``data``: (hub, None) or (None, error key)."""
    hub = build_hub(hass, data)
    try:
        async with asyncio.timeout(30):
            await hub.async_connect()
    except LoginFailed:
        return None, "faulty_credentials"
    except BadControllerUrl:
        return None, "invalid_url"
    except SiteNotFound:
        return None, "unknown_site"
    except UnsupportedControllerVersion:
        return None, "unsupported_version"
    except ConnectionFailed as err:
        return None, "ssl_error" if isinstance(err.__cause__, ClientSSLError) else "service_unavailable"
    except TimeoutError:
        return None, "service_unavailable"
    except OmadaClientException:
        return None, "api_error"
    return hub, None


def _connection_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    """Controller URL, site, account; ``defaults`` pre-fill (no password)."""
    return vol.Schema({
        vol.Required(CONF_URL, default=defaults.get(CONF_URL, "")): str,
        vol.Required(CONF_SITE, default=defaults.get(CONF_SITE, "Default")): str,
        vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): bool,
    })


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    return {**user_input, CONF_URL: user_input[CONF_URL].strip().rstrip("/")}


class HotelSenseConfigFlow(config_entries.ConfigFlow, domain=OMADA_DOMAIN):
    """Add Hotel Sense: one config entry per Omada controller site."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HotelSenseOptionsFlow:
        return HotelSenseOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _clean(user_input)
            hub, errors["base"] = await async_validate_connection(self.hass, data)
            if hub is not None:
                return self.async_create_entry(title=f"{hub.name}: {data[CONF_SITE]}", data=data)
        return self.async_show_form(step_id="user", errors=errors,
                                    data_schema=_connection_schema(user_input or {}))

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the controller address, site or account of an existing entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **_clean(user_input)}
            hub, errors["base"] = await async_validate_connection(self.hass, data)
            if hub is not None:
                # The entry's update listener reloads it (connection changed).
                return self.async_update_and_abort(
                    entry, title=f"{hub.name}: {data[CONF_SITE]}", data=data)
        return self.async_show_form(step_id="reconfigure", errors=errors,
                                    data_schema=_connection_schema(user_input or entry.data))


class HotelSenseOptionsFlow(config_entries.OptionsFlow):
    """Configure: polling, presence, known devices, Exely."""

    def __init__(self) -> None:
        self.options: dict[str, Any] | None = None
        self.controller: OmadaController | None = None
        self._editing_mac: str | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self.options:
            # Options of the ha-omada entities removed in 0.3 are dropped.
            self.options = {k: v for k, v in self.config_entry.options.items()
                            if k not in LEGACY_OPTIONS}

        self.controller: OmadaController = self.hass.data[OMADA_DOMAIN][
            self.config_entry.entry_id
        ]

        return self.async_show_menu(
            step_id="init",
            menu_options=["device_tracker", "omada_webhook", "presence", "rooms", "device_list",
                          "exely", "exely_api", "database"],
            description_placeholders={
                "omada_webhook": self._omada_webhook_status(),
                "exely_webhook": self._exely_webhook_status(),
                "exely_api": self._exely_api_status(),
                "database": self._database_status(),
            },
        )

    # Current state of each part, shown in the menu and on its page: what is
    # already set up and whether it works.
    def _omada_webhook_status(self) -> str:
        hook = self.controller.omada_webhook
        if not hook.received:
            return "no messages yet"
        return f"working, {hook.received} messages received"

    def _exely_webhook_status(self) -> str:
        last = self.controller.exely.last
        if not last:
            return "no events yet"
        return f"last event {last.get('received', '')[:16].replace('T', ' ')} UTC: {last['result']}"

    def _exely_api_status(self) -> str:
        entry = self.config_entry
        client_id = entry.data.get(CONF_EXELY_CLIENT_ID)
        api = self.controller.exely.api
        if not client_id:
            # A failed check is not saved: still say why it failed.
            return f"not set up; last error: {api.last_error}" if api.last_error else "not set up"
        parts = [f"set up (client ID {client_id[:4]}…, property "
                 f"{entry.data.get(CONF_EXELY_PROPERTY_ID) or '?'})"]
        if api.last_error:
            parts.append(f"last error: {api.last_error}")
        elif api.last_check:
            parts.append(f"last check: {api.last_check}")
        parts.append(f"requests in the last hour: {api.requests_last_hour()}")
        return "; ".join(parts)

    async def async_step_device_tracker(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Polling: how often the controller is asked for its clients."""
        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        return self.async_show_form(
            step_id="device_tracker",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=self.controller.option_scan_interval,
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=10, max=300, mode=NumberSelectorMode.BOX, unit_of_measurement="seconds"
                    )
                ),
            }),
        )

    async def async_step_omada_webhook(self, user_input: dict[str, Any] | None = None,
                                       step_id: str = "omada_webhook") -> ConfigFlowResult:
        """Show the webhook address and Shard Secret to enter in the Omada controller."""
        receiver = self.controller.omada_webhook
        if user_input is not None:
            if not user_input.get(CONF_OMADA_NEW_SECRET):
                return await self._update_options()
            receiver.async_rotate_secret()
            step_id = "omada_webhook_rotated"
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Optional(CONF_OMADA_NEW_SECRET, default=False): bool}),
            description_placeholders={"url": receiver.url(), "secret": receiver.secret,
                                      "received": str(receiver.received)},
        )

    async def async_step_omada_webhook_rotated(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_omada_webhook(user_input)

    async def _update_options(self) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=self.options)

    # ------------------------------------------------------------------ #
    # Presence (Stage A)
    # ------------------------------------------------------------------ #
    async def async_step_presence(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        manager = self.controller.presence
        if user_input is not None:
            # Room kinds live in the room model; the other values are options.
            common = set(user_input.pop(CONF_COMMON_AREAS, []) or [])
            self.options.update(user_input)
            if manager.registry.set_kinds(common):
                # Rooms and common areas expose different entities: rebuild them.
                self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
            return await self._update_options()

        areas = ar.async_get(self.hass)
        area_ids = sorted({a for a in resolve_ap_areas(self.hass, self.controller).values() if a}
                          | set(manager.registry.rooms))
        area_options = [
            SelectOptionDict(value=area_id, label=(area.name if (area := areas.async_get_area(area_id)) else area_id))
            for area_id in area_ids
        ]
        common_default = [a for a in area_ids
                          if manager.is_common_area(a, next((o["label"] for o in area_options if o["value"] == a), a))]

        return self.async_show_form(
            step_id="presence",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_PRESENCE_TIMEOUT,
                    default=self.options.get(CONF_PRESENCE_TIMEOUT, DEFAULT_PRESENCE_TIMEOUT),
                ): NumberSelector(NumberSelectorConfig(
                    min=1, max=120, mode=NumberSelectorMode.BOX, unit_of_measurement="min")),
                vol.Optional(
                    CONF_ROAMING_DEBOUNCE,
                    default=self.options.get(CONF_ROAMING_DEBOUNCE, DEFAULT_ROAMING_DEBOUNCE),
                ): NumberSelector(NumberSelectorConfig(
                    min=0, max=600, mode=NumberSelectorMode.BOX, unit_of_measurement="s")),
                vol.Optional(
                    CONF_MIN_RSSI,
                    default=self.options.get(CONF_MIN_RSSI, DEFAULT_MIN_RSSI),
                ): NumberSelector(NumberSelectorConfig(
                    min=-100, max=0, mode=NumberSelectorMode.BOX, unit_of_measurement="dBm")),
                vol.Optional(
                    CONF_SLEEP_TIMEOUT,
                    default=self.options.get(CONF_SLEEP_TIMEOUT, DEFAULT_SLEEP_TIMEOUT),
                ): NumberSelector(NumberSelectorConfig(
                    min=0, max=120, mode=NumberSelectorMode.BOX, unit_of_measurement="min")),
                vol.Optional(CONF_COMMON_AREAS, default=common_default): SelectSelector(
                    SelectSelectorConfig(options=area_options, multiple=True,
                                         mode=SelectSelectorMode.LIST)),
            }),
        )

    # ------------------------------------------------------------------ #
    # Known devices: fixed equipment / employee devices
    # ------------------------------------------------------------------ #
    async def async_step_device_list(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        store = await async_get_device_store(self.hass)
        fixed = sum(1 for d in store.devices if d.category == CATEGORY_FIXED)
        return self.async_show_menu(
            step_id="device_list",
            menu_options=["device_add", "device_edit_select", "device_delete",
                          "device_import", "device_export", "finish"],
            description_placeholders={
                "fixed": str(fixed),
                "employee": str(len(store.devices) - fixed),
            },
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._update_options()

    @staticmethod
    def _device_schema(device: KnownDevice | None = None) -> vol.Schema:
        d = device
        return vol.Schema({
            vol.Required(CONF_MAC, default=d.mac if d else ""): str,
            vol.Optional(CONF_NAME, default=d.name if d else ""): str,
            vol.Required(CONF_CATEGORY, default=d.category if d else CATEGORY_FIXED): SelectSelector(
                SelectSelectorConfig(options=list(CATEGORIES), translation_key=CONF_CATEGORY,
                                     mode=SelectSelectorMode.LIST)),
            vol.Optional(CONF_OWNER, default=d.owner if d else ""): str,
            vol.Optional(CONF_NOTE, default=d.note if d else ""): str,
            vol.Optional(CONF_ROOM, default=d.room if d else ""): str,
            vol.Optional(CONF_DEVICE_TYPE, default=(d.device_type if d else "") or "auto"): SelectSelector(
                SelectSelectorConfig(options=["auto", *LISTABLE_KINDS], translation_key=CONF_DEVICE_TYPE,
                                     mode=SelectSelectorMode.DROPDOWN)),
        })

    async def _save_device(self, user_input: dict[str, Any], replace_mac: str | None,
                           errors: dict[str, str]) -> bool:
        try:
            device = KnownDevice(
                mac=user_input[CONF_MAC], category=user_input[CONF_CATEGORY],
                name=user_input.get(CONF_NAME, ""), owner=user_input.get(CONF_OWNER, ""),
                note=user_input.get(CONF_NOTE, ""), room=user_input.get(CONF_ROOM, ""),
                device_type="" if user_input.get(CONF_DEVICE_TYPE, "auto") == "auto"
                else user_input[CONF_DEVICE_TYPE])
        except ValueError:
            errors[CONF_MAC] = "invalid_mac"
            return False
        store = await async_get_device_store(self.hass)
        if device.mac != replace_mac and device.mac in store.devices:
            errors[CONF_MAC] = "duplicate_mac"
            return False
        await store.async_upsert(device, replace_mac=replace_mac)
        return True

    async def async_step_device_add(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and await self._save_device(user_input, None, errors):
            return await self.async_step_device_list()
        schema = self._device_schema()
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="device_add", data_schema=schema, errors=errors)

    def _device_choices(self, store) -> list[SelectOptionDict]:
        return [SelectOptionDict(value=d.mac, label=f"{d.name or d.mac} ({d.mac}, {d.category})")
                for d in store.devices]

    async def async_step_device_edit_select(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        store = await async_get_device_store(self.hass)
        if not len(store.devices):
            return self.async_abort(reason="no_devices")
        if user_input is not None:
            self._editing_mac = user_input[CONF_MAC]
            return await self.async_step_device_edit()
        return self.async_show_form(
            step_id="device_edit_select",
            data_schema=vol.Schema({vol.Required(CONF_MAC): SelectSelector(
                SelectSelectorConfig(options=self._device_choices(store),
                                     mode=SelectSelectorMode.DROPDOWN))}),
        )

    async def async_step_device_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        store = await async_get_device_store(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None and await self._save_device(user_input, self._editing_mac, errors):
            return await self.async_step_device_list()
        schema = self._device_schema(store.devices.get(self._editing_mac))
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="device_edit", data_schema=schema, errors=errors)

    async def async_step_device_delete(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        store = await async_get_device_store(self.hass)
        if not len(store.devices):
            return self.async_abort(reason="no_devices")
        if user_input is not None:
            await store.async_remove(user_input.get(CONF_DEVICES, []))
            return await self.async_step_device_list()
        return self.async_show_form(
            step_id="device_delete",
            data_schema=vol.Schema({vol.Optional(CONF_DEVICES, default=[]): SelectSelector(
                SelectSelectorConfig(options=self._device_choices(store), multiple=True,
                                     mode=SelectSelectorMode.LIST))}),
        )

    async def async_step_device_import(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"result": ""}
        if user_input is not None:
            store = await async_get_device_store(self.hass)
            result = await store.async_import_csv(
                user_input[CONF_CSV], replace=user_input.get(CONF_REPLACE, False),
                default_category=user_input.get(CONF_DEFAULT_CATEGORY))
            if not result.errors:
                return await self.async_step_device_list()
            errors["base"] = "import_errors"
            placeholders["result"] = (
                f"added {result.added}, updated {result.updated}, removed {result.removed}\n"
                + "\n".join(result.errors[:20]))
        schema = vol.Schema({
            vol.Required(CONF_CSV): TextSelector(TextSelectorConfig(multiline=True)),
            vol.Optional(CONF_DEFAULT_CATEGORY, default=CATEGORY_FIXED): SelectSelector(
                SelectSelectorConfig(options=list(CATEGORIES), translation_key=CONF_CATEGORY,
                                     mode=SelectSelectorMode.LIST)),
            vol.Optional(CONF_REPLACE, default=False): bool,
        })
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="device_import", data_schema=schema,
                                    errors=errors, description_placeholders=placeholders)

    async def async_step_device_export(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_device_list()
        store = await async_get_device_store(self.hass)
        return self.async_show_form(
            step_id="device_export",
            data_schema=vol.Schema({vol.Optional(CONF_CSV, default=store.devices.export_csv()):
                                    TextSelector(TextSelectorConfig(multiline=True))}),
        )

    # ------------------------------------------------------------------ #
    # Exely PMS webhook (check-in / check-out)
    # ------------------------------------------------------------------ #
    async def async_step_exely(self, user_input: dict[str, Any] | None = None,
                               step_id: str = "exely") -> ConfigFlowResult:
        receiver = self.controller.exely
        registry = self.controller.presence.registry
        if user_input is not None:
            # The mapping lives in the room model (Exely labels of each room).
            registry.set_room_map(parse_room_map(user_input.get(CONF_EXELY_ROOM_MAP, "")))
            new_key = user_input.get(CONF_EXELY_NEW_KEY, False)
            new_url = user_input.get(CONF_EXELY_NEW_URL, False)
            if not (new_key or new_url):
                return await self._update_options()
            # Rotate now (the old key/address stops working) and show the new values
            # on a step whose (translated) description says they are new.
            receiver.async_rotate(api_key=new_key, url=new_url)
            step_id = "exely_rotated"
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({
                vol.Optional(CONF_EXELY_ROOM_MAP, description={
                    "suggested_value": registry.room_map_text()}):
                    TextSelector(TextSelectorConfig(multiline=True)),
                vol.Optional(CONF_EXELY_NEW_KEY, default=False): bool,
                vol.Optional(CONF_EXELY_NEW_URL, default=False): bool,
            }),
            description_placeholders={"url": receiver.url(), "api_key": receiver.api_key},
        )

    async def async_step_exely_rotated(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_exely(user_input)

    # ------------------------------------------------------------------ #
    # Exely Connect API (room of a booking)
    # ------------------------------------------------------------------ #
    async def async_step_exely_api(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """client_id / client_secret / property; saving checks them with one room-list call."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            client_id = (user_input.get(CONF_EXELY_CLIENT_ID) or "").strip()
            secret = (user_input.get(CONF_EXELY_CLIENT_SECRET) or "").strip()
            prop = (user_input.get(CONF_EXELY_PROPERTY_ID) or "").strip()
            old = dict(entry.data)
            data = {k: v for k, v in old.items() if k not in (
                CONF_EXELY_CLIENT_ID, CONF_EXELY_CLIENT_SECRET, CONF_EXELY_PROPERTY_ID)}
            if not client_id:  # cleared: Exely API off
                self.hass.config_entries.async_update_entry(entry, data=data)
                return await self._update_options()
            secret = secret or old.get(CONF_EXELY_CLIENT_SECRET, "")
            if not secret:
                errors[CONF_EXELY_CLIENT_SECRET] = "exely_secret_required"
            elif not prop:
                errors[CONF_EXELY_PROPERTY_ID] = "exely_property_required"
            else:
                self.hass.config_entries.async_update_entry(entry, data={
                    **data, CONF_EXELY_CLIENT_ID: client_id, CONF_EXELY_CLIENT_SECRET: secret,
                    CONF_EXELY_PROPERTY_ID: prop})
                try:
                    await self.controller.exely.api.async_check(prop)
                except ExelyAuthError:
                    errors["base"] = "exely_invalid_auth"
                except ExelyNotFound:
                    errors[CONF_EXELY_PROPERTY_ID] = "exely_property_not_found"
                except ExelyApiError:
                    errors["base"] = "exely_cannot_connect"
                if errors:
                    LOGGER.warning("Exely API check failed: %s",
                                           self.controller.exely.api.last_error)
                if not errors:
                    return await self._update_options()
                self.hass.config_entries.async_update_entry(entry, data=old)

        current = user_input or entry.data
        return self.async_show_form(
            step_id="exely_api",
            errors=errors,
            data_schema=vol.Schema({
                # Suggested, not default: a cleared field must stay empty
                # (empty client ID = Exely API off).
                vol.Optional(CONF_EXELY_CLIENT_ID, description={
                    "suggested_value": current.get(CONF_EXELY_CLIENT_ID) or ""}): str,
                # Never shown again: empty keeps the saved secret.
                vol.Optional(CONF_EXELY_CLIENT_SECRET):
                    TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Optional(CONF_EXELY_PROPERTY_ID, description={
                    "suggested_value": current.get(CONF_EXELY_PROPERTY_ID) or ""}): str,
            }),
            description_placeholders={
                "secret_saved": "yes" if entry.data.get(CONF_EXELY_CLIENT_SECRET) else "no",
                "status": self._exely_api_status()},
        )

    # ------------------------------------------------------------------ #
    # History database (MariaDB add-on)
    # ------------------------------------------------------------------ #
    async def async_step_database(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Connection (checked on save, tables created) and retention."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            username = (user_input.get(CONF_DB_USERNAME) or "").strip()
            self.options[CONF_DB_RETENTION] = int(user_input.get(
                CONF_DB_RETENTION, history.DEFAULT_RETENTION_MONTHS))
            data = {k: v for k, v in entry.data.items() if k not in DB_KEYS}
            if not username:  # cleared: history off
                self.hass.config_entries.async_update_entry(entry, data=data)
                return await self._update_options()
            db = {
                CONF_DB_HOST: (user_input.get(CONF_DB_HOST) or "").strip() or DEFAULT_DB_HOST,
                CONF_DB_PORT: int(user_input.get(CONF_DB_PORT) or DEFAULT_DB_PORT),
                CONF_DB_USERNAME: username,
                # Never shown again: empty keeps the saved password.
                CONF_DB_PASSWORD: user_input.get(CONF_DB_PASSWORD)
                or entry.data.get(CONF_DB_PASSWORD, ""),
                CONF_DB_NAME: (user_input.get(CONF_DB_NAME) or "").strip() or DEFAULT_DB_NAME,
            }
            url = history.build_url(db[CONF_DB_HOST], db[CONF_DB_PORT], username,
                                    db[CONF_DB_PASSWORD], db[CONF_DB_NAME])
            try:
                await self.hass.async_add_executor_job(history.check_connection, url)
            except Exception as err:  # noqa: BLE001 - shown as a form error
                errors["base"] = f"db_{history.error_reason(err)}"
                LOGGER.warning("History database check failed: %s",
                                       str(err).splitlines()[0][:300])
            else:
                self.hass.config_entries.async_update_entry(entry, data={**data, **db})
                return await self._update_options()

        current = {**entry.data, **(user_input or {})}
        return self.async_show_form(
            step_id="database",
            errors=errors,
            data_schema=vol.Schema({
                # Suggested, not default: a cleared user must stay empty (= history off).
                vol.Optional(CONF_DB_HOST, description={
                    "suggested_value": current.get(CONF_DB_HOST) or DEFAULT_DB_HOST}): str,
                vol.Optional(CONF_DB_PORT, description={
                    "suggested_value": int(current.get(CONF_DB_PORT) or DEFAULT_DB_PORT)}):
                    NumberSelector(NumberSelectorConfig(min=1, max=65535,
                                                        mode=NumberSelectorMode.BOX)),
                vol.Optional(CONF_DB_USERNAME, description={
                    "suggested_value": current.get(CONF_DB_USERNAME) or ""}): str,
                vol.Optional(CONF_DB_PASSWORD):
                    TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Optional(CONF_DB_NAME, description={
                    "suggested_value": current.get(CONF_DB_NAME) or DEFAULT_DB_NAME}): str,
                vol.Optional(CONF_DB_RETENTION, default=int(self.options.get(
                    CONF_DB_RETENTION, history.DEFAULT_RETENTION_MONTHS))):
                    NumberSelector(NumberSelectorConfig(min=1, max=120,
                                                        mode=NumberSelectorMode.BOX,
                                                        unit_of_measurement="months")),
            }),
            description_placeholders={
                "status": self._database_status(),
                "password_saved": "yes" if entry.data.get(CONF_DB_PASSWORD) else "no"},
        )

    def _database_status(self) -> str:
        writer = self.controller.history
        if writer is None:
            return "not set up"
        state = "connected" if writer.connected else (writer.last_error or "not connected yet")
        return f"{state}; rows written: {writer.written}, queued: {writer.queued}"

    # ------------------------------------------------------------------ #
    # Rooms (the room model): number, kind, Exely labels as CSV
    # ------------------------------------------------------------------ #
    async def async_step_rooms(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        registry = self.controller.presence.registry
        errors: dict[str, str] = {}
        placeholders = {"errors": ""}
        if user_input is not None:
            problems, kinds_changed = registry.import_csv(user_input.get(CONF_CSV, ""))
            if not problems:
                if kinds_changed:
                    self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
                else:
                    self.controller.presence.async_refresh()
                return await self._update_options()
            errors["base"] = "rooms_csv_errors"
            placeholders["errors"] = "\n\n" + "\n".join(problems[:10])
        return self.async_show_form(
            step_id="rooms",
            errors=errors,
            data_schema=vol.Schema({vol.Optional(CONF_CSV, description={
                "suggested_value": (user_input or {}).get(CONF_CSV) or registry.export_csv()}):
                TextSelector(TextSelectorConfig(multiline=True))}),
            description_placeholders=placeholders,
        )
