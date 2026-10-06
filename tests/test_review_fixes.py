"""Fixes from the code review: controller outage, recorder load, AP devices."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.data_entry_flow import FlowResultType

from custom_components.hotel_sense.api.errors import RequestError
from custom_components.hotel_sense.const import CONF_TRACK_CLIENTS, CONF_TRACK_DEVICES
from custom_components.hotel_sense.room_entity import (
    MisplacedFixedDevicesSensor,
    RoomCountSensor,
)

from .test_stage_a import _hotel, _poll, _state


async def test_controller_down_when_login_fails_too(hass, make_entry, patch_api, freezer):
    """Controller unreachable: the re-login fails as well. The poll must still
    finish, mark the controller unavailable and flag the rooms as stale."""
    controller = await _hotel(hass, make_entry())
    patch_api.raise_on_status = [RequestError("u", "down"), RequestError("u", "down")]
    patch_api.raise_on_login = [RequestError("u", "down"), RequestError("u", "down")]
    freezer.tick(timedelta(minutes=30))
    await _poll(hass, controller)  # must not raise
    assert controller.available is False
    assert hass.states.get("sensor.room_06_state").attributes["data_stale"] is True
    assert _state(hass, "sensor.room_06_state") == "violation"  # last picture kept

    await _poll(hass, controller)  # back
    assert controller.available is True
    assert hass.states.get("sensor.room_06_state").attributes["data_stale"] is False


def test_device_lists_are_not_recorded():
    """RSSI / last seen change every poll: keep them out of the recorder."""
    assert {"devices", "types"} <= RoomCountSensor._unrecorded_attributes
    assert "devices" in MisplacedFixedDevicesSensor._unrecorded_attributes
    assert "data_stale" not in RoomCountSensor._unrecorded_attributes


async def test_rooms_work_even_if_ap_devices_were_switched_off(hass, make_entry, patch_api):
    """An old 'track devices: off' option would leave the APs without HA
    devices, hence without Areas, hence no rooms. It is ignored now."""
    controller = await _hotel(hass, make_entry({CONF_TRACK_DEVICES: False}))
    assert controller.option_track_devices is True
    assert _state(hass, "sensor.room_06_guest_devices") == "2"


async def test_options_no_longer_offer_turning_ap_devices_off(hass, make_entry, patch_api):
    entry = make_entry({CONF_TRACK_CLIENTS: False})
    await _hotel(hass, entry)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "device_tracker"})
    assert CONF_TRACK_DEVICES not in {str(k) for k in result["data_schema"].schema}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TRACK_CLIENTS: False})
    assert result["step_id"] == "device_options"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TRACK_DEVICES] is True
