"""The English "Rooms & Wi-Fi" dashboard: devices per room grouped by SSID,
fixed equipment left out; the committed file is current and renders."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml
from homeassistant.helpers.template import Template

from custom_components.hotel_sense.storage import async_get_device_store
from custom_components.hotel_sense.device_list import KnownDevice

from .fakes import AP_WF06, AP_WF07, GUEST_PHONE, SHARED_MAC
from .test_stage_a import _hotel

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "generate_wifi_dashboard", ROOT / "scripts" / "generate_wifi_dashboard.py")
generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generator)

ENTITY_RE = re.compile(r"\b((?:sensor|binary_sensor|select)\.[a-z0-9_]+)\b")


def _markdown(text: str) -> list[str]:
    sections = yaml.safe_load(text)["views"][0]["sections"]
    return [card["content"] for s in sections for card in s["cards"]]


def test_committed_dashboard_is_up_to_date():
    committed = (ROOT / "dashboards" / "hotel_sense_wifi_en.yaml").read_text()
    assert committed == generator.build_dashboard(generator.DEFAULT_ROOMS, generator.DEFAULT_COMMON)
    contents = _markdown(committed)
    assert len(contents) == 1 + 10 + 1  # overview, Room 01 … Room 10, Admin House
    for number, content in enumerate(contents[1:11], start=1):
        assert f"sensor.room_{number:02d}_state" in content
    assert "fixed_devices" not in committed  # fixed equipment is not shown


async def test_renders_devices_grouped_by_ssid_without_fixed(hass, make_entry, patch_api):
    await _hotel(hass, make_entry(), {AP_WF06: "Room 06", AP_WF07: "Admin House"})
    store = await async_get_device_store(hass)
    # One of the two Room 06 devices is fixed equipment: it must not be listed.
    await store.async_upsert(KnownDevice(mac=SHARED_MAC, category="fixed", name="Air conditioner"))
    await hass.async_block_till_done()

    text = generator.build_dashboard(["room_06"], ["admin_house"])
    for entity_id in set(ENTITY_RE.findall(text)):
        assert hass.states.get(entity_id) is not None, entity_id
    overview, room, admin = (Template(c, hass).async_render(parse_result=False)
                             for c in _markdown(text))

    assert "Room 06 — VIOLATION" in room and 'alert-type="error"' in room
    assert "📶 Guest" in room and "| 🟢 | Guest-Phone |" in room
    assert "Air conditioner" not in room
    assert "🧳 Guest" in room and "dBm" in room
    assert "Admin House" in admin and "No guest or staff devices" in admin
    assert "| 📶 Guest | 1 | 0 | Room 06 |" in overview
    assert "Possible violations: 1" in overview


async def test_staff_and_other_ssid_and_device_that_left(hass, make_entry, patch_api, freezer):
    from .fakes import client_raw
    from .test_stage_a import _poll, _set_clients

    from custom_components.hotel_sense.const import CONF_TRACK_CLIENTS

    controller = await _hotel(hass, make_entry({CONF_TRACK_CLIENTS: False}),
                              {AP_WF06: "Room 06", AP_WF07: "Room 07"})
    store = await async_get_device_store(hass)
    staff = "AA-BB-CC-00-00-99"
    await store.async_upsert(KnownDevice(mac=staff, category="employee", name="Maid tablet",
                                         device_type="tablet"))
    _set_clients(patch_api, [client_raw(GUEST_PHONE, "Guest-Phone"),
                             client_raw(staff, "maid", ssid="Staff")])
    await _poll(hass, controller)
    freezer.tick(120)
    _set_clients(patch_api, [client_raw(staff, "maid", ssid="Staff")])  # guest phone left
    await _poll(hass, controller)

    text = generator.build_dashboard(["room_06"], [])
    overview, room = (Template(c, hass).async_render(parse_result=False) for c in _markdown(text))
    assert "**📶 Guest** · 2 devices" in room and "**📶 Staff** · 1 device" in room
    assert "| 🟢 | Maid tablet | 📱 Tablet | 🧑‍💼 Staff |" in room
    assert "| ⚪ | Guest-Phone<br><sub>left 2 minutes ago</sub> | 📱 Personal device | 🧳 Guest | — |" in room
    assert "| 📶 Staff | 0 | 1 | Room 06 |" in overview
