from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hotel_sense.const import CONF_SITE, DOMAIN

from .fakes import SITE_NAME, FakeApi, default_api


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    """Allow HA to load custom_components/hotel_sense."""


@pytest.fixture
def fake_api() -> FakeApi:
    return default_api()


@pytest.fixture
def make_entry():
    def _make(options: dict | None = None) -> MockConfigEntry:
        return MockConfigEntry(
            domain=DOMAIN,
            title="Omada",
            data={
                CONF_URL: "https://omada.test:8043", CONF_USERNAME: "u", CONF_PASSWORD: "p",
                CONF_VERIFY_SSL: False, CONF_SITE: SITE_NAME,
            },
            options=dict(options or {}),
        )
    return _make


@pytest.fixture
def patch_api(fake_api):
    """The library's OmadaClient replaced by the fake controller."""
    with patch("custom_components.hotel_sense.omada_hub.OmadaClient", new=fake_api):
        yield fake_api
