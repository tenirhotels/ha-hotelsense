from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hotel_sense.const import (
    CONF_DISCONNECT_TIMEOUT, CONF_ENABLE_CLIENT_BANDWIDTH_SENSORS,
    CONF_ENABLE_CLIENT_BLOCK_SWITCH, CONF_ENABLE_CLIENT_UPTIME_SENSORS,
    CONF_ENABLE_DEVICE_BANDWIDTH_SENSORS, CONF_ENABLE_DEVICE_CLIENTS_SENSORS,
    CONF_ENABLE_DEVICE_CONTROLS, CONF_ENABLE_DEVICE_STATISTICS_SENSORS,
    CONF_SITE, CONF_SSID_FILTER, DOMAIN,
)

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
        opts = {
            CONF_DISCONNECT_TIMEOUT: 5,
            CONF_ENABLE_CLIENT_BANDWIDTH_SENSORS: True,
            CONF_ENABLE_CLIENT_UPTIME_SENSORS: True,
            CONF_ENABLE_CLIENT_BLOCK_SWITCH: True,
            CONF_ENABLE_DEVICE_BANDWIDTH_SENSORS: True,
            CONF_ENABLE_DEVICE_STATISTICS_SENSORS: True,
            CONF_ENABLE_DEVICE_CLIENTS_SENSORS: True,
            CONF_ENABLE_DEVICE_CONTROLS: True,
        }
        opts.update(options or {})
        return MockConfigEntry(
            domain=DOMAIN,
            title="Omada",
            data={
                CONF_URL: "https://omada.test:8043", CONF_USERNAME: "u", CONF_PASSWORD: "p",
                CONF_VERIFY_SSL: False, CONF_SITE: SITE_NAME, CONF_SSID_FILTER: [],
                CONF_DISCONNECT_TIMEOUT: 5,
            },
            options=opts,
        )
    return _make


@pytest.fixture
def patch_api(fake_api):
    with patch("custom_components.hotel_sense.controller.get_api_controller",
               new=AsyncMock(return_value=fake_api)):
        yield fake_api
