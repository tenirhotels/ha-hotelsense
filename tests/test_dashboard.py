"""The Lovelace dashboard: generated file is current, its templates render
against real Hotel Sense entities, and violations are highlighted red."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml
from homeassistant.helpers.template import Template

from custom_components.hotel_sense.const import DOMAIN
from custom_components.hotel_sense.storage import async_get_device_store

from .fakes import AP_WF06, AP_WF07, GUEST_PHONE
from .test_stage_a import _hotel

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("generate_dashboard", ROOT / "scripts" / "generate_dashboard.py")
generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generator)

ENTITY_RE = re.compile(r"\b((?:sensor|binary_sensor|select)\.[a-z0-9_]+)\b")


def _cards(dashboard: dict) -> list[dict]:
    out = []

    def walk(cards):
        for card in cards:
            out.append(card)
            walk(card.get("cards", []))

    walk(dashboard["views"][0]["cards"])
    return out


def test_committed_dashboard_is_up_to_date():
    committed = (ROOT / "dashboards" / "hotel_sense_rooms.yaml").read_text()
    assert committed == generator.build_dashboard(generator.DEFAULT_ROOMS, generator.DEFAULT_COMMON)
    dashboard = yaml.safe_load(committed)
    referenced = set(ENTITY_RE.findall(committed))
    for room in generator.DEFAULT_ROOMS:
        assert f"select.{room}_status" in referenced
        assert f"sensor.{room}_state" in referenced
    assert "sensor.admin_house_guest_devices" in referenced
    assert len(dashboard["views"][0]["cards"]) == 1 + 10 + 1


async def test_dashboard_renders_and_highlights_violations(hass, make_entry, patch_api):
    await _hotel(hass, make_entry(), {AP_WF06: "Room 06", AP_WF07: "Admin House"})
    text = generator.build_dashboard(["room_06"], ["admin_house"])
    for entity_id in set(ENTITY_RE.findall(text)):
        assert hass.states.get(entity_id) is not None, entity_id

    cards = _cards(yaml.safe_load(text))
    rendered = [Template(c["content"], hass).async_render(parse_result=False)
                for c in cards if c["type"] == "markdown"]
    summary, room, admin = rendered
    assert 'alert-type="error"' in summary and "Возможные нарушения: 1" in summary
    assert "Room 06" in summary
    assert 'alert-type="error"' in room and "Room 06 — НАРУШЕНИЕ" in room
    assert "Гости: 2" in room
    assert "Admin House" in admin and "Гости: 0" in admin

    assert "не на своём месте" not in summary

    await hass.services.async_call(DOMAIN, "import_devices", {
        "csv": f"mac,name,category,room\n{GUEST_PHONE},AC07,fixed,Room 07\n"}, blocking=True)
    await hass.async_block_till_done()
    summary = Template(cards[0]["content"], hass).async_render(parse_result=False)
    assert 'alert-type="warning"' in summary
    assert "AC07: ожидается Room 07, видно в Room 06" in summary
    await (await async_get_device_store(hass)).async_remove([GUEST_PHONE])

    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_06_status", "option": "sold"}, blocking=True)
    await hass.async_block_till_done()
    summary = Template(cards[0]["content"], hass).async_render(parse_result=False)
    room = Template(cards[2]["content"], hass).async_render(parse_result=False)
    assert "Нарушений нет" in summary
    assert "ha-alert" not in room and "**Room 06** — Продан" in room

    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_06_status", "option": "cleaning"}, blocking=True)
    await hass.async_block_till_done()
    room = Template(cards[2]["content"], hass).async_render(parse_result=False)
    assert 'alert-type="warning"' in room and "Уборка не началась" in room
