"""HA picks the interface language: Russian and English translations both load."""
from __future__ import annotations

import pytest
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component

from custom_components.hotel_sense.const import DOMAIN

PREFIX = f"component.{DOMAIN}"


@pytest.mark.parametrize(("lang", "menu", "status", "exely_title", "rotated"), [
    ("ru", "Присутствие в номерах", "Статус", "Вебхук Exely PMS", "Сгенерированы новые"),
    ("en", "Room presence", "Status", "Exely PMS webhook", "New values generated"),
])
async def test_translations_follow_interface_language(hass, lang, menu, status, exely_title,
                                                      rotated):
    assert await async_setup_component(hass, "homeassistant", {})
    options = await async_get_translations(hass, lang, "options", [DOMAIN])
    entity = await async_get_translations(hass, lang, "entity", [DOMAIN])
    assert options[f"{PREFIX}.options.step.init.menu_options.presence"] == menu
    assert options[f"{PREFIX}.options.step.exely.title"] == exely_title
    assert entity[f"{PREFIX}.entity.select.room_status.name"] == status
    assert rotated in options[f"{PREFIX}.options.step.exely_rotated.description"]


async def test_every_english_string_has_a_russian_one(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    for category in ("config", "options", "entity", "selector", "services"):
        en = await async_get_translations(hass, "en", category, [DOMAIN])
        ru = await async_get_translations(hass, "ru", category, [DOMAIN])
        missing = sorted(set(en) - set(ru))
        assert not missing, (category, missing[:5])
