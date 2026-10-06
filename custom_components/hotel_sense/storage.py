"""Persistence of the known-device list in Home Assistant ``.storage``."""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_KEY_DEVICES, STORAGE_VERSION
from .device_list import DeviceList, ImportResult, KnownDevice

DATA_DEVICE_STORE = f"{DOMAIN}_device_store"


class DeviceListStore:
    """The hotel-wide device list (shared by all config entries)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, STORAGE_KEY_DEVICES)
        self.devices = DeviceList()
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> None:
        self.devices = DeviceList.from_storage(await self._store.async_load())

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def _async_changed(self) -> None:
        await self._store.async_save(self.devices.to_storage())
        for listener in list(self._listeners):
            listener()

    async def async_upsert(self, device: KnownDevice, *, replace_mac: str | None = None) -> None:
        """Add or update; ``replace_mac`` removes the old entry when a MAC is edited."""
        if replace_mac and replace_mac != device.mac:
            self.devices.remove(replace_mac)
        self.devices.upsert(device)
        await self._async_changed()

    async def async_remove(self, macs: list[str]) -> int:
        removed = sum(1 for mac in macs if self.devices.remove(mac))
        if removed:
            await self._async_changed()
        return removed

    async def async_import_csv(self, text: str, *, replace: bool = False,
                               default_category: str | None = None) -> ImportResult:
        result = self.devices.import_csv(text, replace=replace, default_category=default_category)
        if result.added or result.updated or result.removed:
            await self._async_changed()
        return result


async def async_get_device_store(hass: HomeAssistant) -> DeviceListStore:
    if (store := hass.data.get(DATA_DEVICE_STORE)) is None:
        store = DeviceListStore(hass)
        await store.async_load()
        hass.data[DATA_DEVICE_STORE] = store
    return store
