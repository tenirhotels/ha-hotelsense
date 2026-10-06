from __future__ import annotations

from typing import Any

import homeassistant.helpers.config_validation as cv
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
)

from .api.errors import (
    LoginFailed,
    LoginRequired,
    OmadaApiException,
    RequestError,
    SSLError,
    InvalidURLError,
    UnknownSite,
    UnsupportedVersion,
)
from .const import (
    DOMAIN as OMADA_DOMAIN,
    CONF_SITE,
    CONF_SSID_FILTER,
    CONF_DISCONNECT_TIMEOUT,
    CONF_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL_DETAILS,
    CONF_TRACK_CLIENTS,
    CONF_TRACK_DEVICES,
    CONF_ENABLE_CLIENT_BANDWIDTH_SENSORS,
    CONF_ENABLE_CLIENT_UPTIME_SENSORS,
    CONF_ENABLE_CLIENT_BLOCK_SWITCH,
    CONF_ENABLE_DEVICE_BANDWIDTH_SENSORS,
    CONF_ENABLE_DEVICE_RADIO_UTILIZATION_SENSORS,
    CONF_ENABLE_DEVICE_CONTROLS,
    CONF_ENABLE_DEVICE_STATISTICS_SENSORS,
    CONF_ENABLE_DEVICE_CLIENTS_SENSORS,
    CONF_PRESENCE_TIMEOUT,
    CONF_ROAMING_DEBOUNCE,
    CONF_MIN_RSSI,
    CONF_COMMON_AREAS,
    DEFAULT_PRESENCE_TIMEOUT,
    DEFAULT_ROAMING_DEBOUNCE,
    DEFAULT_MIN_RSSI,
)
from .areas import resolve_ap_areas
from .controller import OmadaController, get_api_controller
from .device_list import CATEGORIES, CATEGORY_FIXED, KnownDevice
from .storage import async_get_device_store

CONF_MAC = "mac"
CONF_NAME = "name"
CONF_CATEGORY = "category"
CONF_OWNER = "owner"
CONF_NOTE = "note"
CONF_DEVICES = "devices"
CONF_CSV = "csv"
CONF_REPLACE = "replace"
CONF_DEFAULT_CATEGORY = "default_category"


class OmadaFlowHandler(config_entries.ConfigFlow, domain=OMADA_DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OmadaOptionsFlowHandler:
        return OmadaOptionsFlowHandler(config_entry)

    def __init__(self) -> None:
        self.config: dict[str, Any] = {}

    @callback
    def _show_setup_form(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        if user_input is None:
            user_input = {}

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=user_input.get(CONF_URL, "")): str,
                    vol.Optional(
                        CONF_SITE, default=user_input.get(CONF_SITE, "Default")
                    ): str,
                    vol.Required(
                        CONF_USERNAME, default=user_input.get(CONF_USERNAME, "")
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(
                        CONF_VERIFY_SSL, default=user_input.get(CONF_VERIFY_SSL, True)
                    ): bool,
                }
            ),
            errors=errors or {},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            user_input[CONF_URL] = user_input[CONF_URL].strip("/")

            self.config = {
                CONF_URL: user_input[CONF_URL],
                CONF_SITE: user_input[CONF_SITE],
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
            }

            try:
                controller = await get_api_controller(
                    self.hass,
                    self.config[CONF_URL],
                    self.config[CONF_USERNAME],
                    self.config[CONF_PASSWORD],
                    30,
                    self.config[CONF_SITE],
                    self.config[CONF_VERIFY_SSL],
                )

                return self.async_create_entry(
                    title=f"{controller.name}: {controller.site}", data=user_input
                )

            except (LoginFailed, LoginRequired):
                errors["base"] = "faulty_credentials"
            except InvalidURLError:
                errors["base"] = "invalid_url"
            except SSLError:
                errors["base"] = "ssl_error"
            except UnknownSite:
                errors["base"] = "unknown_site"
            except UnsupportedVersion:
                errors["base"] = "unsupported_version"
            except RequestError:
                errors["base"] = "service_unavailable"
            except OmadaApiException:
                errors["base"] = "api_error"

            return self._show_setup_form(user_input, errors)

        else:
            return self._show_setup_form(user_input, errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle reconfiguration of an existing entry."""
        errors = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            user_input[CONF_URL] = user_input[CONF_URL].strip("/")

            try:
                controller = await get_api_controller(
                    self.hass,
                    user_input[CONF_URL],
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    30,
                    user_input[CONF_SITE],
                    user_input[CONF_VERIFY_SSL],
                )

                # Unload before updating the entry to avoid async_update_entry
                # firing a reload listener that races with our manual reload.
                await self.hass.config_entries.async_unload(reconfigure_entry.entry_id)
                self.hass.config_entries.async_update_entry(
                    reconfigure_entry,
                    title=f"{controller.name}: {controller.site}",
                    data=user_input,
                )
                await self.hass.config_entries.async_setup(reconfigure_entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

            except (LoginFailed, LoginRequired):
                errors["base"] = "faulty_credentials"
            except InvalidURLError:
                errors["base"] = "invalid_url"
            except SSLError:
                errors["base"] = "ssl_error"
            except UnknownSite:
                errors["base"] = "unknown_site"
            except UnsupportedVersion:
                errors["base"] = "unsupported_version"
            except RequestError:
                errors["base"] = "service_unavailable"
            except OmadaApiException:
                errors["base"] = "api_error"

        # Pre-fill form with existing config entry data
        current_data = reconfigure_entry.data

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_URL, default=current_data.get(CONF_URL, "")
                    ): str,
                    vol.Optional(
                        CONF_SITE, default=current_data.get(CONF_SITE, "Default")
                    ): str,
                    vol.Required(
                        CONF_USERNAME, default=current_data.get(CONF_USERNAME, "")
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(
                        CONF_VERIFY_SSL, default=current_data.get(CONF_VERIFY_SSL, True)
                    ): bool,
                }
            ),
            errors=errors,
        )


class OmadaOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: ConfigEntry) -> None:
        self.options: dict[str, Any] | None = None
        self.controller: OmadaController | None = None
        self._editing_mac: str | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self.options:
            self.options = dict(self.config_entry.options)

        self.controller: OmadaController = self.hass.data[OMADA_DOMAIN][
            self.config_entry.entry_id
        ]

        return self.async_show_menu(
            step_id="init",
            menu_options=["device_tracker", "presence", "device_list"],
        )

    async def async_step_device_tracker(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.options.update(user_input)
            if self.options[CONF_TRACK_CLIENTS]:
                return await self.async_step_client_options()
            elif self.options[CONF_TRACK_DEVICES]:
                return await self.async_step_device_options()
            else:
                return await self._update_options()

        return self.async_show_form(
            step_id="device_tracker",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=self.controller.option_scan_interval,
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=10, mode=NumberSelectorMode.BOX, unit_of_measurement="seconds"
                        )
                    ),
                    vol.Optional(
                        CONF_SCAN_INTERVAL_DETAILS,
                        default=self.controller.option_scan_interval_details,
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=10, mode=NumberSelectorMode.BOX, unit_of_measurement="seconds"
                        )
                    ),
                    vol.Optional(
                        CONF_TRACK_CLIENTS, default=self.controller.option_track_clients
                    ): bool,
                    vol.Optional(
                        CONF_TRACK_DEVICES, default=self.controller.option_track_devices
                    ): bool,
                }
            ),
            last_step=False,
        )

    async def async_step_client_options(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.options.update(user_input)
            if self.options[CONF_TRACK_DEVICES]:
                return await self.async_step_device_options()
            else:
                return await self._update_options()

        ssid_filter = {ssid: ssid for ssid in sorted(self.controller.api.ssids)}

        # Remove selected options that may not exist anymore.
        ssid_filter_default = list(filter(
            lambda i: i in ssid_filter, self.controller.option_ssid_filter))

        return self.async_show_form(
            step_id="client_options",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SSID_FILTER, default=ssid_filter_default
                    ): cv.multi_select(ssid_filter),
                    vol.Optional(
                        CONF_DISCONNECT_TIMEOUT,
                        default=self.controller.option_disconnect_timeout,
                    ): cv.positive_int,
                    vol.Optional(
                        CONF_ENABLE_CLIENT_BANDWIDTH_SENSORS,
                        default=self.controller.option_client_bandwidth_sensors,
                    ): bool,
                    vol.Optional(
                        CONF_ENABLE_CLIENT_UPTIME_SENSORS,
                        default=self.controller.option_client_uptime_sensor,
                    ): bool,
                    vol.Optional(
                        CONF_ENABLE_CLIENT_BLOCK_SWITCH,
                        default=self.controller.option_client_block_switch,
                    ): bool,
                }
            ),
            last_step=False,
        )

    async def async_step_device_options(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        return self.async_show_form(
            step_id="device_options",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ENABLE_DEVICE_BANDWIDTH_SENSORS,
                        default=self.controller.option_device_bandwidth_sensors,
                    ): bool,
                    vol.Optional(
                        CONF_ENABLE_DEVICE_STATISTICS_SENSORS,
                        default=self.controller.option_device_statistics_sensors,
                    ): bool,
                    vol.Optional(
                        CONF_ENABLE_DEVICE_CLIENTS_SENSORS,
                        default=self.controller.option_device_clients_sensors,
                    ): bool,
                    vol.Optional(
                        CONF_ENABLE_DEVICE_RADIO_UTILIZATION_SENSORS,
                        default=self.controller.option_device_radio_utilization_sensors,
                    ): bool,
                    vol.Optional(
                        CONF_ENABLE_DEVICE_CONTROLS,
                        default=self.controller.option_device_controls,
                    ): bool,
                }
            ),
            last_step=True,
        )

    async def _update_options(self) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=self.options)

    # ------------------------------------------------------------------ #
    # Presence (Stage A)
    # ------------------------------------------------------------------ #
    async def async_step_presence(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        manager = self.controller.presence
        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        areas = ar.async_get(self.hass)
        area_ids = sorted({a for a in resolve_ap_areas(self.hass, self.controller).values() if a}
                          | set(self.options.get(CONF_COMMON_AREAS) or []))
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
        })

    async def _save_device(self, user_input: dict[str, Any], replace_mac: str | None,
                           errors: dict[str, str]) -> bool:
        try:
            device = KnownDevice(
                mac=user_input[CONF_MAC], category=user_input[CONF_CATEGORY],
                name=user_input.get(CONF_NAME, ""), owner=user_input.get(CONF_OWNER, ""),
                note=user_input.get(CONF_NOTE, ""))
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
