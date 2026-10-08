"""Room statuses kept in step with Exely (search + reservations), not only by webhooks."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.hotel_sense.const import DOMAIN
from custom_components.hotel_sense.exely_api import PMS_URL, RoomStay
from custom_components.hotel_sense.exely_sync import plan

from .test_exely_api import (  # noqa: F401
    PROP, ROOM_06, ROOM_07, ROOMS_URL, _api_hotel, _booking_url, exely, no_sleep,
)
from .test_stage_a import _hotel, _state

UTC = timezone.utc
NOW = datetime(2026, 1, 3, 12, 0, tzinfo=UTC)
SEARCH_URL = f"{PMS_URL}/v2/properties/{PROP}/reservations/search"


def _stay(room, status, check_in="2026-01-02T14:00", check_out="2026-01-04T12:00",
          actual_in=None, actual_out=None):
    return RoomStay(stay_id="s", room_id=room, status=status, check_in=check_in,
                    check_out=check_out, actual_check_in=actual_in, actual_check_out=actual_out)


def test_plan_from_stays():
    stays = [("B1", _stay("r1", "checked_in", actual_in="2026-01-02T15:00")),
             ("B2", _stay("r2", "checked_out", actual_in="2026-01-01T15:00",
                          actual_out="2026-01-03T10:30")),
             ("B3", _stay("r3", "cancelled")),
             ("B4", _stay("r4", None))]  # not arrived yet
    result = plan(stays, ["r1", "r2", "r3", "r4", "r5"], UTC, NOW, complete=True)
    assert (result["r1"].status, result["r1"].booking, result["r1"].since) == (
        "checked_in", "B1", datetime(2026, 1, 2, 15, tzinfo=UTC))
    assert result["r1"].overdue is False
    assert (result["r2"].status, result["r2"].since) == (
        "checked_out", datetime(2026, 1, 3, 10, 30, tzinfo=UTC))
    # Cancelled / not arrived / no stay: vacant, with no event time.
    assert {r: (result[r].status, result[r].since) for r in ("r3", "r4", "r5")} == {
        r: ("checked_out", None) for r in ("r3", "r4", "r5")}


def test_incomplete_picture_never_empties_a_room():
    stays = [("B1", _stay("r1", "checked_in"))]
    result = plan(stays, ["r1", "r2"], UTC, NOW, complete=False)
    assert set(result) == {"r1"}


def test_an_earlier_guest_never_checked_out_is_overdue_even_with_a_new_one_in():
    stays = [("OLD", _stay("r1", "checked_in", check_in="2026-01-02T14:00",
                           check_out="2026-01-03T10:00", actual_in="2026-01-02T15:00")),
             ("NEW", _stay("r1", "checked_in", check_out="2026-01-04T10:00",
                           actual_in="2026-01-03T11:30"))]
    result = plan(stays, ["r1"], UTC, NOW, complete=True)["r1"]
    assert (result.status, result.booking) == ("checked_in", "NEW")
    assert result.overdue_bookings == ["OLD"]


def test_a_new_guest_in_wins_over_the_last_one_out_and_overdue_is_flagged():
    stays = [("OLD", _stay("r1", "checked_out", actual_out="2026-01-03T10:00")),
             ("NEW", _stay("r1", "checked_in", check_out="2026-01-03T10:00",
                           actual_in="2026-01-01T15:00"))]
    result = plan(stays, ["r1"], UTC, NOW, complete=True)["r1"]
    assert (result.status, result.booking, result.overdue) == ("checked_in", "NEW", True)


def _local(delta: timedelta) -> str:
    return (dt_util.now() + delta).strftime("%Y-%m-%dT%H:%M")


def _reservation_json(number, room, status, actual_in=None, actual_out=None):
    return {"reservation": {"number": number, "roomStays": [{
        "pmsRoomStayId": "s1", "roomId": room, "status": status,
        "checkInDateTime": _local(timedelta(days=-1)), "checkOutDateTime": _local(timedelta(days=1)),
        "actualCheckInDateTime": actual_in, "actualCheckOutDateTime": actual_out}]}}


@pytest.fixture
def exely_now(exely):  # noqa: F811
    """Room 06 has a guest who checked in an hour ago; Room 07 nobody."""
    exely.get(SEARCH_URL, json={"reservations": [{"number": "B-06"}], "hasNextPage": False,
                                "nextPageToken": None})
    exely.get(_booking_url("B-06"), json=_reservation_json(
        "B-06", ROOM_06, "CheckedIn", actual_in=_local(timedelta(hours=-1))))
    return exely


async def test_sync_sets_statuses_and_keeps_newer_manual_ones(hass, make_entry, patch_api,
                                                              exely_now):
    await _api_hotel(hass, make_entry)
    # Set by hand before the guest checked in (an hour ago): Exely is newer.
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_06_status", "option": "checked_out"}, blocking=True)
    controller = hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))]
    room = controller.presence.registry.get("room_06")
    room.status = replace(room.status,
                          changed_at=(dt_util.utcnow() - timedelta(hours=3)).isoformat())
    # Room 07: set by hand just now, Exely has no event for it: kept.
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.room_07_status", "option": "checked_in"}, blocking=True)

    dry = await hass.services.async_call(DOMAIN, "exely_sync", {"dry_run": True},
                                         blocking=True, return_response=True)
    assert dry["changes"] == [{"room": "Room 06", "from": "checked_out", "to": "checked_in",
                               "booking": "B-06"}]
    assert _state(hass, "select.room_06_status") == "checked_out"  # dry run: nothing set

    result = await hass.services.async_call(DOMAIN, "exely_sync", {}, blocking=True,
                                            return_response=True)
    assert result["ok"] and result["complete"] and result["bookings"] == 1
    assert result["kept_manual"] == [{"room": "Room 07", "status": "checked_in",
                                      "exely": "checked_out"}]
    assert _state(hass, "select.room_06_status") == "checked_in"
    attrs = hass.states.get("select.room_06_status").attributes
    assert attrs["source"] == "exely" and attrs["booking"] == "B-06"
    assert _state(hass, "select.room_07_status") == "checked_in"

    # First sync after manual workarounds: replace them.
    result = await hass.services.async_call(DOMAIN, "exely_sync", {"override_manual": True},
                                            blocking=True, return_response=True)
    assert result["changes"] == [{"room": "Room 07", "from": "checked_in", "to": "checked_out",
                                  "booking": None}]
    assert _state(hass, "select.room_07_status") == "checked_out"


async def test_without_request_budget_nothing_is_emptied(hass, make_entry, patch_api, exely_now,
                                                         monkeypatch):
    await _api_hotel(hass, make_entry)
    controller = hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))]
    monkeypatch.setattr(controller.exely.api, "budget", lambda reserve=5: 0)
    result = await hass.services.async_call(DOMAIN, "exely_sync", {"override_manual": True},
                                            blocking=True, return_response=True)
    assert result["complete"] is False and result["looked_up"] == 0
    assert result["changes"] == []  # Room 07 not emptied on a partial picture


async def test_sync_needs_the_api(hass, make_entry, patch_api):
    await _hotel(hass, make_entry())
    with pytest.raises(ServiceValidationError, match="not set up"):
        await hass.services.async_call(DOMAIN, "exely_sync", {}, blocking=True,
                                       return_response=True)


async def test_a_dry_run_just_before_costs_no_new_requests(hass, make_entry, patch_api,
                                                           exely_now, monkeypatch):
    await _api_hotel(hass, make_entry)
    controller = hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))]
    await hass.services.async_call(DOMAIN, "exely_sync", {"dry_run": True}, blocking=True,
                                   return_response=True)
    monkeypatch.setattr(controller.exely.api, "budget", lambda reserve=5: 0)  # hour used up
    result = await hass.services.async_call(DOMAIN, "exely_sync", {"override_manual": True},
                                            blocking=True, return_response=True)
    assert (result["complete"], result["looked_up"], result["from_cache"]) == (True, 0, 1)
    assert _state(hass, "select.room_06_status") == "checked_in"
    assert _state(hass, "select.room_07_status") == "checked_out"



async def test_a_villa_never_empties_a_room_by_its_digit(hass, make_entry, patch_api,
                                                         exely):  # noqa: F811
    """Production: Exely room "V1" (a villa, no bookings) was matched to Room 01 by the
    digit and emptied it right after Room 01's guest had been set checked in.
    Here: villa "V6" next to Room 06 with a guest."""
    from .test_exely_api import ROOMS
    rooms = {"rooms": [*ROOMS["rooms"], {"id": "900099", "name": "V6", "roomTypeId": "2"}],
             "hasNextPage": False}
    exely.clear_requests()
    exely.post(f"{PMS_URL.rsplit('/api', 1)[0]}/auth/token",
               json={"access_token": "jwt", "expires_in": 900})
    exely.get(ROOMS_URL, json=rooms)
    exely.get(SEARCH_URL, json={"reservations": [{"number": "B-06"}], "hasNextPage": False})
    exely.get(_booking_url("B-06"), json=_reservation_json(
        "B-06", ROOM_06, "CheckedIn", actual_in=_local(timedelta(hours=-1))))
    await _api_hotel(hass, make_entry)
    result = await hass.services.async_call(DOMAIN, "exely_sync", {"override_manual": True},
                                            blocking=True, return_response=True)
    assert result["unresolved"] == ["V6"]
    assert _state(hass, "select.room_06_status") == "checked_in"


async def test_two_exely_rooms_on_one_room_occupied_wins(hass, make_entry, patch_api,
                                                         exely):  # noqa: F811
    from .test_exely_api import ROOMS
    rooms = {"rooms": [*ROOMS["rooms"], {"id": "900099", "name": "Annex", "roomTypeId": "2"}],
             "hasNextPage": False}
    exely.clear_requests()
    exely.post(f"{PMS_URL.rsplit('/api', 1)[0]}/auth/token",
               json={"access_token": "jwt", "expires_in": 900})
    exely.get(ROOMS_URL, json=rooms)
    exely.get(SEARCH_URL, json={"reservations": [{"number": "B-06"}], "hasNextPage": False})
    exely.get(_booking_url("B-06"), json=_reservation_json(
        "B-06", ROOM_06, "CheckedIn", actual_in=_local(timedelta(hours=-1))))
    entry = await _api_hotel(hass, make_entry)
    registry = hass.data[DOMAIN][entry.entry_id].presence.registry
    registry.import_csv('room_06;;06;room;"900006,900099";')  # a mapping mistake
    result = await hass.services.async_call(DOMAIN, "exely_sync", {"override_manual": True},
                                            blocking=True, return_response=True)
    assert result["conflicts"] == [{"room": "Room 06", "exely_rooms": ["06", "Annex"]}]
    assert _state(hass, "select.room_06_status") == "checked_in"
