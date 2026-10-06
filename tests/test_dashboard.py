"""The Lovelace dashboard: generated file is current, its templates render
against real Hotel Sense entities, and violations are highlighted red."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml
from homeassistant.helpers.template import Template

from custom_components.hotel_sense.const import DOMAIN
from custom_components.hotel_sense.storage import async_get_device_store

from .fakes import AP_WF06, AP_WF07, GUEST_PHONE, SHARED_MAC
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

    walk(dashboard["views"][0]["sections"])
    return out


@pytest.mark.parametrize(("lang", "filename"), [("ru", "hotel_sense_rooms.yaml"),
                                                ("en", "hotel_sense_rooms_en.yaml")])
def test_committed_dashboard_is_up_to_date(lang, filename):
    committed = (ROOT / "dashboards" / filename).read_text()
    assert committed == generator.build_dashboard(
        generator.DEFAULT_ROOMS, generator.DEFAULT_COMMON, lang)
    dashboard = yaml.safe_load(committed)
    referenced = set(ENTITY_RE.findall(committed))
    for room in generator.DEFAULT_ROOMS:
        assert f"select.{room}_status" in referenced
        assert f"sensor.{room}_state" in referenced
    assert "sensor.admin_house_guest_devices" in referenced
    view = dashboard["views"][0]
    assert view["type"] == "sections"
    sections = view["sections"]
    assert len(sections) == 1 + 10 + 1
    # Strict order: summary, Room 01 … Room 10 (state card, then its status), Admin House.
    for number, section in enumerate(sections[1:11], start=1):
        state_card, status_card = section["cards"]
        assert f"sensor.room_{number:02d}_state" in state_card["content"]
        assert status_card["entity"] == f"select.room_{number:02d}_status"
        assert status_card["features"] == [{"type": "select-options"}]
    assert "admin_house" in sections[-1]["cards"][0]["content"]


async def test_dashboard_renders_and_highlights_violations(hass, make_entry, patch_api):
    await _hotel(hass, make_entry(), {AP_WF06: "Room 06", AP_WF07: "Admin House"})
    text = generator.build_dashboard(["room_06"], ["admin_house"])
    for entity_id in set(ENTITY_RE.findall(text)):
        assert hass.states.get(entity_id) is not None, entity_id

    cards = _cards(yaml.safe_load(text))
    markdown = [c for c in cards if c["type"] == "markdown"]
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
    summary = Template(markdown[0]["content"], hass).async_render(parse_result=False)
    assert 'alert-type="warning"' in summary
    assert "AC07: ожидается Room 07, видно в Room 06" in summary
    await (await async_get_device_store(hass)).async_remove([GUEST_PHONE])

    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_06_status", "option": "checked_in"}, blocking=True)
    await hass.async_block_till_done()
    summary = Template(markdown[0]["content"], hass).async_render(parse_result=False)
    room = Template(markdown[1]["content"], hass).async_render(parse_result=False)
    assert "Нарушений нет" in summary
    assert "ha-alert" not in room and "**Room 06** — Гость заселён" in room

    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_06_status", "option": "checked_out"}, blocking=True)
    await (await async_get_device_store(hass)).async_import_csv(
        f"mac,name,category\n{GUEST_PHONE},Maid,employee\n{SHARED_MAC},AC,fixed\n")
    await hass.async_block_till_done()
    room = Template(markdown[1]["content"], hass).async_render(parse_result=False)
    assert 'alert-type="info"' in room and "Визит сотрудника" in room


async def test_english_dashboard_renders(hass, make_entry, patch_api):
    await _hotel(hass, make_entry(), {AP_WF06: "Room 06", AP_WF07: "Admin House"})
    cards = _cards(yaml.safe_load(generator.build_dashboard(["room_06"], ["admin_house"], "en")))
    summary, room, admin = [Template(c["content"], hass).async_render(parse_result=False)
                            for c in cards if c["type"] == "markdown"]
    assert "Possible violations: 1" in summary
    assert "Room 06 — VIOLATION" in room and "Guests: 2" in room
    assert "Staff: 0" in admin
    assert [c["name"] for c in cards if c["type"] == "tile"] == ["Status"]
    # No Russian left in the English version.
    text = generator.build_dashboard(generator.DEFAULT_ROOMS, generator.DEFAULT_COMMON, "en")
    assert not any("а" <= ch.lower() <= "я" for ch in text)
