"""Exely Connect API: room of a booking, with as few requests as possible.

Response shapes are assumptions (the API documents none); the parser looks
fields up by plausible names. All IDs and numbers here are made up.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientError
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.hotel_sense import exely_api
from custom_components.hotel_sense.const import (
    DOMAIN, CONF_EXELY_CLIENT_ID, CONF_EXELY_CLIENT_SECRET, CONF_EXELY_PROPERTY_ID, EVENT_EXELY,
)
from custom_components.hotel_sense.diagnostics import async_get_config_entry_diagnostics
from custom_components.hotel_sense.exely_api import (
    AUTH_URL, PMS_URL, STAY_CANCELLED, ExelyApi, ExelyApiError, ExelyAuthError, ExelyNotFound,
    parse_reservation, parse_rooms, shape, stay_status,
)
from custom_components.hotel_sense.presence import STATUS_CHECKED_IN, STATUS_CHECKED_OUT

from .test_exely import _exely_hotel, _post
from .test_stage_a import _options_menu, _state

PROP = "1234"
BOOKING = "20260101-1234-111"
ROOM_06, ROOM_07 = "900006", "900007"
ROOMS_URL = f"{PMS_URL}/v2/properties/{PROP}/rooms"


def _booking_url(number: str = BOOKING) -> str:
    return f"{PMS_URL}/v2/properties/{PROP}/reservations/{number}"


def _reservation(*stays) -> dict:
    return {"number": BOOKING, "customer": {"lastName": "***", "phone": "***"},
            "roomStays": [{"pmsRoomStayId": f"s{i}", "roomId": room, "status": status,
                           "checkInDateTime": "2026-01-01T14:00",
                           "checkOutDateTime": "2026-01-03T12:00",
                           "guests": [{"firstName": "***"}]}
                          for i, (room, status) in enumerate(stays)]}


ROOMS = {"rooms": [{"id": ROOM_06, "name": "06", "roomTypeId": "1"},
                   {"id": ROOM_07, "name": "07", "roomTypeId": "1"}],
         "hasNextPage": False}


def _event(event_type: str, number: str = BOOKING, event_id: str = "e1") -> list:
    return [{"eventId": event_id, "eventType": event_type, "creationTime": "2026-01-03T09:00:00Z",
             "payload": {"BookingNumber": number, "PropertyId": PROP}}]


def _calls(aioclient_mock, url: str) -> int:
    return sum(1 for _, u, _, _ in aioclient_mock.mock_calls if str(u).split("?")[0] == url)


@pytest.fixture
def no_sleep():
    with patch.object(exely_api, "_sleep", new=AsyncMock()) as sleep:
        yield sleep


@pytest.fixture
def exely(aioclient_mock, no_sleep):
    aioclient_mock.post(AUTH_URL, json={"access_token": "jwt", "expires_in": 900})
    aioclient_mock.get(ROOMS_URL, json=ROOMS)
    return aioclient_mock


def _with_api(entry):
    return {**entry.data, CONF_EXELY_CLIENT_ID: "client", CONF_EXELY_CLIENT_SECRET: "secret",
            CONF_EXELY_PROPERTY_ID: PROP}


async def _api_hotel(hass, make_entry, options=None):
    entry = await _exely_hotel(hass, make_entry, options)
    hass.config_entries.async_update_entry(entry, data=_with_api(entry))
    await hass.async_block_till_done()
    return entry


# --------------------------------------------------------------------------- #
# Parsing (pure)
# --------------------------------------------------------------------------- #
def test_parse_reservation_keeps_rooms_statuses_and_dates_only():
    stays = parse_reservation({"reservation": _reservation((ROOM_06, "CheckedOut"),
                                                           (ROOM_07, "Cancelled"))})
    assert [(s.stay_id, s.room_id, s.status) for s in stays] == [
        ("s0", ROOM_06, STATUS_CHECKED_OUT), ("s1", ROOM_07, STAY_CANCELLED)]
    assert stays[0].check_in == "2026-01-01T14:00" and stays[0].check_out == "2026-01-03T12:00"
    assert "***" not in repr(stays)


def test_parse_reservation_variants():
    stays = parse_reservation({"roomStays": [
        {"id": 5, "room": {"id": 77, "name": "Room 07"}, "roomStayStatus": "InHouse",
         "actualCheckInDateTime": "2026-01-01T15:10", "arrivalDateTime": "2026-01-01T14:00"},
        {"roomId": None, "status": "New"}, "garbage"]})
    assert stays[0].room_id == "77" and stays[0].room_name == "Room 07"
    assert stays[0].status == STATUS_CHECKED_IN
    assert (stays[0].actual_check_in, stays[0].check_in) == ("2026-01-01T15:10", "2026-01-01T14:00")
    assert stays[1].room_id is None and stays[1].status is None
    assert parse_reservation(None) == [] and parse_reservation({"roomStays": "x"}) == []


@pytest.mark.parametrize(("value", "expected"), [
    ("CheckedIn", STATUS_CHECKED_IN), ("checked_out", STATUS_CHECKED_OUT),
    ("Cancelled", STAY_CANCELLED), ("New", None), (None, None)])
def test_stay_status(value, expected):
    assert stay_status(value) == expected


def test_parse_rooms_and_pages():
    assert parse_rooms(ROOMS) == ({ROOM_06: "06", ROOM_07: "07"}, None)
    assert parse_rooms({"rooms": [{"id": 1, "name": "A"}], "nextPageToken": "p2",
                        "hasNextPage": True}) == ({"1": "A"}, "p2")
    assert parse_rooms([{"roomId": "9", "displayName": "Lux"}]) == ({"9": "Lux"}, None)


def test_shape_has_no_values():
    assert shape(_reservation((ROOM_06, "CheckedIn"))) == {
        "number": "str", "customer": {"lastName": "str", "phone": "str"},
        "roomStays": [{"pmsRoomStayId": "str", "roomId": "str", "status": "str",
                       "checkInDateTime": "str", "checkOutDateTime": "str",
                       "guests": [{"firstName": "str"}]}]}


# --------------------------------------------------------------------------- #
# Client: token, rate limits, caches
# --------------------------------------------------------------------------- #
def _client(hass, clock=None):
    api = ExelyApi(hass, lambda: ("client", "secret"),
                   **({"clock": clock} if clock else {}))
    api.retry_delay = 0
    return api


async def test_one_token_for_many_requests(hass, exely):
    exely.get(_booking_url(), json=_reservation((ROOM_06, "CheckedIn")))
    api = _client(hass)
    for _ in range(3):
        await api.async_reservation(PROP, BOOKING)
    assert _calls(exely, AUTH_URL) == 1 and _calls(exely, _booking_url()) == 3
    _, _, form, headers = next(c for c in exely.mock_calls if str(c[1]) == AUTH_URL)
    assert form["grant_type"] == "client_credentials"
    request_headers = [h for _, u, _, h in exely.mock_calls if str(u) == _booking_url()]
    assert request_headers[0]["Authorization"] == "Bearer jwt"


async def test_token_is_renewed_after_14_minutes(hass, exely):
    now = [1000.0]
    exely.get(_booking_url(), json=_reservation())
    api = _client(hass, clock=lambda: now[0])
    await api.async_reservation(PROP, BOOKING)
    now[0] += 13 * 60
    await api.async_reservation(PROP, BOOKING)
    assert _calls(exely, AUTH_URL) == 1
    now[0] += 2 * 60
    await api.async_reservation(PROP, BOOKING)
    assert _calls(exely, AUTH_URL) == 2


async def test_rejected_credentials(hass, aioclient_mock, no_sleep):
    aioclient_mock.post(AUTH_URL, status=401)
    with pytest.raises(ExelyAuthError):
        await _client(hass).async_reservation(PROP, BOOKING)
    assert not [u for _, u, _, _ in aioclient_mock.mock_calls if str(u).startswith(PMS_URL)]


async def test_429_waits_for_retry_after(hass, aioclient_mock, no_sleep):
    aioclient_mock.post(AUTH_URL, json={"access_token": "jwt"})
    answers = iter([(429, {"retry-after": "7"}, None),
                    (200, {}, _reservation((ROOM_06, "CheckedOut")))])

    async def side_effect(method, url, data):
        status, headers, body = next(answers)
        return AiohttpClientMockResponse(method, url, status=status, headers=headers, json=body)

    aioclient_mock.get(_booking_url(), side_effect=side_effect)
    stays = await _client(hass).async_reservation(PROP, BOOKING)
    assert stays[0].room_id == ROOM_06
    assert 7 in [c.args[0] for c in no_sleep.await_args_list]


async def test_long_retry_after_gives_up(hass, aioclient_mock, no_sleep):
    aioclient_mock.post(AUTH_URL, json={"access_token": "jwt"})
    aioclient_mock.get(_booking_url(), status=429, headers={"retry-after": "3600"})
    with pytest.raises(ExelyApiError, match="429"):
        await _client(hass).async_reservation(PROP, BOOKING)
    assert _calls(aioclient_mock, _booking_url()) == 1


async def test_errors_are_retried_twice_at_most(hass, aioclient_mock, no_sleep):
    aioclient_mock.post(AUTH_URL, json={"access_token": "jwt"})
    aioclient_mock.get(_booking_url(), exc=ClientError("down"))
    with pytest.raises(ExelyApiError):
        await _client(hass).async_reservation(PROP, BOOKING)
    assert _calls(aioclient_mock, _booking_url()) == 3


async def test_unknown_booking(hass, exely):
    exely.get(_booking_url(), status=404)
    with pytest.raises(ExelyNotFound):
        await _client(hass).async_reservation(PROP, BOOKING)
    assert _calls(exely, _booking_url()) == 1


async def test_own_limits_space_and_cap_requests(hass, exely, no_sleep, monkeypatch):
    monkeypatch.setattr(exely_api, "HOURLY_LIMIT", 3)
    now = [1000.0]
    exely.get(_booking_url(), json=_reservation())
    api = _client(hass, clock=lambda: now[0])
    await api.async_reservation(PROP, BOOKING)
    await api.async_reservation(PROP, BOOKING)
    assert 1.0 in [c.args[0] for c in no_sleep.await_args_list]  # 1 request per second
    await api.async_reservation(PROP, BOOKING)
    with pytest.raises(ExelyApiError, match="hourly"):
        await api.async_reservation(PROP, BOOKING)  # 4th within the hour: would wait ~1 h
    assert _calls(exely, _booking_url()) == 3 and api.requests_last_hour() == 3
    now[0] += 3600
    await api.async_reservation(PROP, BOOKING)
    assert _calls(exely, _booking_url()) == 4


async def test_concurrent_lookups_share_one_request(hass, exely):
    async def slow(method, url, data):
        await asyncio.sleep(0)  # the network: the second lookup starts meanwhile
        return AiohttpClientMockResponse(method, url, json=_reservation((ROOM_06, "CheckedIn")))

    exely.get(_booking_url(), side_effect=slow)
    api = _client(hass)
    first, second = await asyncio.gather(
        api.async_reservation(PROP, BOOKING), api.async_reservation(PROP, BOOKING))
    assert first == second and _calls(exely, _booking_url()) == 1


async def test_room_list_is_kept_on_disk_and_refreshed_rarely(hass, exely, hass_storage):
    now = [1000.0]
    api = _client(hass, clock=lambda: now[0])
    assert await api.async_room_name(PROP, ROOM_06) == "06"
    assert await api.async_room_name(PROP, ROOM_07) == "07"
    assert _calls(exely, ROOMS_URL) == 1
    assert hass_storage["hotel_sense.exely_rooms"]["data"][PROP]["rooms"][ROOM_06] == "06"

    assert await api.async_room_name(PROP, "unknown") is None  # checked < 1 h ago: no request
    assert _calls(exely, ROOMS_URL) == 1
    now[0] += 3601
    assert await api.async_room_name(PROP, "unknown") is None  # one refresh, then quiet again
    assert await api.async_room_name(PROP, "unknown") is None
    assert _calls(exely, ROOMS_URL) == 2

    restarted = _client(hass)  # after a restart: from disk
    assert await restarted.async_room_name(PROP, ROOM_07) == "07"
    assert _calls(exely, ROOMS_URL) == 2


async def test_room_list_pages(hass, aioclient_mock, no_sleep):
    aioclient_mock.post(AUTH_URL, json={"access_token": "jwt"})
    aioclient_mock.get(f"{ROOMS_URL}?pageToken=p2", json={"rooms": [{"id": "2", "name": "B"}]})
    aioclient_mock.get(ROOMS_URL, json={"rooms": [{"id": "1", "name": "A"}],
                                        "nextPageToken": "p2", "hasNextPage": True})
    assert await _client(hass).async_rooms(PROP) == {"1": "A", "2": "B"}


# --------------------------------------------------------------------------- #
# Webhook + API in Home Assistant
# --------------------------------------------------------------------------- #
async def test_check_out_by_booking_number(hass, make_entry, patch_api, hass_client_no_auth, exely):
    exely.get(_booking_url(), json=_reservation((ROOM_06, "CheckedIn")))
    entry = await _api_hotel(hass, make_entry)
    client = await hass_client_no_auth()
    events = []
    hass.bus.async_listen(EVENT_EXELY, events.append)

    resp = await _post(client, entry, _event("webpms:check_in", event_id="e1"))
    await hass.async_block_till_done()
    assert resp.status == 200
    assert _state(hass, "select.room_06_status") == "checked_in"
    last = hass.states.get("sensor.hotel_sense_exely_last_event")
    assert last.state == "applied" and last.attributes["booking"] == BOOKING
    assert last.attributes["lookup"] == "api" and last.attributes["applied"] == ["Room 06"]

    # Check-out of the same single-room booking: no new request.
    await _post(client, entry, _event("webpms:check_out", event_id="e2"))
    await hass.async_block_till_done()
    assert _state(hass, "select.room_06_status") == "checked_out"
    assert hass.states.get("sensor.hotel_sense_exely_last_event").attributes["lookup"] == "cache"
    assert _calls(exely, _booking_url()) == 1 and _calls(exely, ROOMS_URL) == 1

    # Exely delivering the same event again: ignored, no request.
    await _post(client, entry, _event("webpms:check_out", event_id="e2"))
    await hass.async_block_till_done()
    assert len(events) == 2

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["exely_duplicates"] == 1
    assert diag["exely_api"]["requests"] == 2 and diag["exely_api"]["token_requests"] == 1
    lookup = diag["exely_api"]["recent_lookups"][0]
    assert lookup["stays"][0]["room_id"] == ROOM_06
    assert "***" not in repr(diag["exely_api"])  # guest data never kept
    assert diag["entry"][CONF_EXELY_CLIENT_SECRET] == "**REDACTED**"
    assert diag["entry"][CONF_EXELY_CLIENT_ID] == "**REDACTED**"


async def test_changed_booking_is_looked_up_again(hass, make_entry, patch_api,
                                                  hass_client_no_auth, exely):
    exely.get(_booking_url(), json=_reservation((ROOM_06, "CheckedIn")))
    entry = await _api_hotel(hass, make_entry)
    client = await hass_client_no_auth()
    await _post(client, entry, _event("webpms:check_in", event_id="e1"))
    await _post(client, entry, _event("webpms:booking_modified", event_id="e2"))
    await _post(client, entry, _event("webpms:check_out", event_id="e3"))
    await hass.async_block_till_done()
    assert _calls(exely, _booking_url()) == 2


async def test_several_rooms_in_one_booking(hass, make_entry, patch_api, hass_client_no_auth,
                                            exely):
    exely.get(_booking_url(), json=_reservation((ROOM_06, "CheckedOut"), (ROOM_07, "CheckedIn")))
    entry = await _api_hotel(hass, make_entry)
    await hass.services.async_call("select", "select_option", {
        "entity_id": ["select.room_06_status", "select.room_07_status"],
        "option": "checked_in"}, blocking=True)
    client = await hass_client_no_auth()
    await _post(client, entry, _event("webpms:check_out"))
    await hass.async_block_till_done()
    # Only the stay Exely shows as checked out.
    assert _state(hass, "select.room_06_status") == "checked_out"
    assert _state(hass, "select.room_07_status") == "checked_in"


async def test_exely_room_names_go_through_the_room_map(hass, make_entry, patch_api,
                                                        hass_client_no_auth, aioclient_mock,
                                                        no_sleep):
    aioclient_mock.post(AUTH_URL, json={"access_token": "jwt"})
    aioclient_mock.get(ROOMS_URL, json={"rooms": [{"id": ROOM_07, "name": "Lux 1"}]})
    aioclient_mock.get(_booking_url(), json=_reservation((ROOM_07, "CheckedIn")))
    entry = await _api_hotel(hass, make_entry, {"exely_room_map": "Lux 1 = Room 07"})
    client = await hass_client_no_auth()
    await _post(client, entry, _event("webpms:check_in"))
    await hass.async_block_till_done()
    assert _state(hass, "select.room_07_status") == "checked_in"


async def test_without_api_the_event_says_why(hass, make_entry, patch_api, hass_client_no_auth,
                                              aioclient_mock):
    entry = await _exely_hotel(hass, make_entry)
    client = await hass_client_no_auth()
    await _post(client, entry, _event("webpms:check_out"))
    await hass.async_block_till_done()
    last = hass.states.get("sensor.hotel_sense_exely_last_event")
    assert last.state == "unmatched" and "not set up" in last.attributes["reason"]
    assert aioclient_mock.call_count == 0


async def test_api_failure_is_reported(hass, make_entry, patch_api, hass_client_no_auth,
                                       aioclient_mock, no_sleep):
    aioclient_mock.post(AUTH_URL, status=401)
    entry = await _api_hotel(hass, make_entry)
    client = await hass_client_no_auth()
    resp = await _post(client, entry, _event("webpms:check_out"))
    await hass.async_block_till_done()
    assert resp.status == 200
    last = hass.states.get("sensor.hotel_sense_exely_last_event")
    assert last.state == "api_error" and "ExelyAuthError" in last.attributes["error"]
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "ExelyAuthError" in diag["exely_api"]["last_error"]


async def test_unknown_room_is_unresolved(hass, make_entry, patch_api, hass_client_no_auth,
                                          exely):
    exely.get(_booking_url(), json=_reservation(("555", "CheckedIn")))
    entry = await _api_hotel(hass, make_entry)
    client = await hass_client_no_auth()
    await _post(client, entry, _event("webpms:check_in"))
    await hass.async_block_till_done()
    last = hass.states.get("sensor.hotel_sense_exely_last_event")
    assert last.state == "unmatched" and last.attributes["unresolved"] == ["555"]


# --------------------------------------------------------------------------- #
# Options: credentials are checked with one room-list request
# --------------------------------------------------------------------------- #
async def _api_step(hass, entry, user_input):
    result = await _options_menu(hass, entry, "exely_api")
    assert result["step_id"] == "exely_api"
    return await hass.config_entries.options.async_configure(result["flow_id"], user_input)


async def test_options_save_and_check_credentials(hass, make_entry, patch_api, exely):
    entry = await _exely_hotel(hass, make_entry)
    result = await _api_step(hass, entry, {CONF_EXELY_CLIENT_ID: " client ",
                                           CONF_EXELY_CLIENT_SECRET: "secret",
                                           CONF_EXELY_PROPERTY_ID: PROP})
    assert result["type"] == "create_entry"
    assert (entry.data[CONF_EXELY_CLIENT_ID], entry.data[CONF_EXELY_CLIENT_SECRET],
            entry.data[CONF_EXELY_PROPERTY_ID]) == ("client", "secret", PROP)
    assert _calls(exely, ROOMS_URL) == 1

    # Empty secret keeps the saved one.
    result = await _api_step(hass, entry, {CONF_EXELY_CLIENT_ID: "client",
                                           CONF_EXELY_CLIENT_SECRET: "",
                                           CONF_EXELY_PROPERTY_ID: PROP})
    assert result["type"] == "create_entry" and entry.data[CONF_EXELY_CLIENT_SECRET] == "secret"

    # Empty client ID switches the API off.
    result = await _api_step(hass, entry, {CONF_EXELY_CLIENT_ID: ""})
    assert result["type"] == "create_entry" and CONF_EXELY_CLIENT_ID not in entry.data
    assert CONF_EXELY_CLIENT_SECRET not in entry.data


async def test_options_reject_bad_credentials(hass, make_entry, patch_api, aioclient_mock,
                                              no_sleep):
    aioclient_mock.post(AUTH_URL, status=401)
    entry = await _exely_hotel(hass, make_entry)
    result = await _api_step(hass, entry, {CONF_EXELY_CLIENT_ID: "client",
                                           CONF_EXELY_CLIENT_SECRET: "wrong",
                                           CONF_EXELY_PROPERTY_ID: PROP})
    assert result["type"] == "form" and result["errors"] == {"base": "exely_invalid_auth"}
    assert CONF_EXELY_CLIENT_ID not in entry.data  # not saved


@pytest.mark.parametrize(("token", "rooms", "error"), [
    ({"json": {"access_token": "jwt"}}, {"status": 404},
     {CONF_EXELY_PROPERTY_ID: "exely_property_not_found"}),
    ({"exc": ClientError("down")}, {"json": ROOMS}, {"base": "exely_cannot_connect"}),
    ({"status": 500}, {"json": ROOMS}, {"base": "exely_cannot_connect"}),
])
async def test_options_property_errors(hass, make_entry, patch_api, aioclient_mock, no_sleep,
                                       token, rooms, error):
    aioclient_mock.post(AUTH_URL, **token)
    aioclient_mock.get(ROOMS_URL, **rooms)
    entry = await _exely_hotel(hass, make_entry)
    result = await _api_step(hass, entry, {CONF_EXELY_CLIENT_ID: "client",
                                           CONF_EXELY_CLIENT_SECRET: "secret",
                                           CONF_EXELY_PROPERTY_ID: PROP})
    assert result["errors"] == error


async def test_options_require_secret_and_property(hass, make_entry, patch_api, aioclient_mock):
    entry = await _exely_hotel(hass, make_entry)
    result = await _api_step(hass, entry, {CONF_EXELY_CLIENT_ID: "client"})
    assert result["errors"] == {CONF_EXELY_CLIENT_SECRET: "exely_secret_required"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        CONF_EXELY_CLIENT_ID: "client", CONF_EXELY_CLIENT_SECRET: "s"})
    assert result["errors"] == {CONF_EXELY_PROPERTY_ID: "exely_property_required"}
    assert aioclient_mock.call_count == 0


async def test_errors_carry_exely_explanation(hass, aioclient_mock, no_sleep):
    aioclient_mock.post(AUTH_URL, status=400, text='{"error": "invalid_client", "secret": "secret"}')
    with pytest.raises(ExelyAuthError, match="HTTP 400: .*invalid_client") as err:
        await _client(hass).async_reservation(PROP, BOOKING)
    assert "secret" not in str(err.value)  # the client secret is never echoed


async def test_non_json_answer_is_an_api_error(hass, aioclient_mock, no_sleep):
    aioclient_mock.post(AUTH_URL, json={"access_token": "jwt"})
    aioclient_mock.get(ROOMS_URL, text="<html>maintenance</html>")
    with pytest.raises(ExelyApiError, match="not JSON"):
        await _client(hass).async_rooms(PROP)


async def test_options_show_why_the_check_failed(hass, make_entry, patch_api, aioclient_mock,
                                                 no_sleep, caplog):
    aioclient_mock.post(AUTH_URL, status=503, text='{"message": "maintenance"}')
    entry = await _exely_hotel(hass, make_entry)
    result = await _api_step(hass, entry, {CONF_EXELY_CLIENT_ID: "client",
                                           CONF_EXELY_CLIENT_SECRET: "secret",
                                           CONF_EXELY_PROPERTY_ID: PROP})
    assert result["errors"] == {"base": "exely_cannot_connect"}
    assert "maintenance" in result["description_placeholders"]["status"]
    assert "Exely API check failed" in caplog.text and "HTTP 503" in caplog.text


ROOM_LIST_500 = {"status": 500,
                 "text": '{"errors":[{"code":"InternalError","message":"An error has occured"}]}'}


async def test_failing_room_list_does_not_block_setup(hass, make_entry, patch_api, aioclient_mock,
                                                      no_sleep):
    """Exely answered HTTP 500 for the room list: sign-in works, so save; rooms by roomId."""
    aioclient_mock.post(AUTH_URL, json={"access_token": "jwt"})
    aioclient_mock.get(ROOMS_URL, **ROOM_LIST_500)
    entry = await _exely_hotel(hass, make_entry)
    result = await _api_step(hass, entry, {CONF_EXELY_CLIENT_ID: "client",
                                           CONF_EXELY_CLIENT_SECRET: "secret",
                                           CONF_EXELY_PROPERTY_ID: PROP})
    assert result["type"] == "create_entry" and entry.data[CONF_EXELY_CLIENT_ID] == "client"
    api = hass.data[DOMAIN][entry.entry_id].exely.api
    assert api.last_error is None and "room list unavailable" in api.last_check
    assert "InternalError" in api.rooms_error


async def test_rooms_matched_by_id_when_room_list_fails(hass, make_entry, patch_api,
                                                        hass_client_no_auth, aioclient_mock,
                                                        no_sleep):
    aioclient_mock.post(AUTH_URL, json={"access_token": "jwt"})
    aioclient_mock.get(ROOMS_URL, **ROOM_LIST_500)
    aioclient_mock.get(_booking_url(), json=_reservation((ROOM_07, "CheckedIn")))
    entry = await _api_hotel(hass, make_entry, {"exely_room_map": f"{ROOM_07} = Room 07"})
    client = await hass_client_no_auth()
    await _post(client, entry, _event("webpms:check_in", event_id="e1"))
    await hass.async_block_till_done()
    assert _state(hass, "select.room_07_status") == "checked_in"
    room_list_calls = _calls(aioclient_mock, ROOMS_URL)

    # Unmapped room: unresolved with its roomId, no new room-list request within the hour.
    aioclient_mock.get(_booking_url("B-2"), json=_reservation(("555", "CheckedIn")))
    await _post(client, entry, _event("webpms:check_in", number="B-2", event_id="e2"))
    await hass.async_block_till_done()
    last = hass.states.get("sensor.hotel_sense_exely_last_event")
    assert last.state == "unmatched" and last.attributes["unresolved"] == ["555"]
    assert _calls(aioclient_mock, ROOMS_URL) == room_list_calls


async def test_menu_and_page_show_what_is_set_up(hass, make_entry, patch_api, exely):
    entry = await _exely_hotel(hass, make_entry)
    menu = await _options_menu(hass, entry)
    placeholders = menu["description_placeholders"]
    assert placeholders["exely_api"] == "not set up"
    assert placeholders["database"] == "not set up"
    assert placeholders["omada_webhook"] == "no messages yet"
    assert placeholders["exely_webhook"] == "no events yet"

    await _api_step(hass, entry, {CONF_EXELY_CLIENT_ID: "client-1234",
                                  CONF_EXELY_CLIENT_SECRET: "secret", CONF_EXELY_PROPERTY_ID: PROP})
    menu = await _options_menu(hass, entry)
    status = menu["description_placeholders"]["exely_api"]
    assert status.startswith(f"set up (client ID clie…, property {PROP})")
    assert "last check: OK" in status and "2 rooms" in status
    assert "client-1234" not in status and "secret" not in status
    page = await _options_menu(hass, entry, "exely_api")
    assert page["description_placeholders"]["status"] == status
    assert page["description_placeholders"]["secret_saved"] == "yes"


async def test_probe_service_reports_what_exely_answers(hass, make_entry, patch_api,
                                                        aioclient_mock, no_sleep):
    aioclient_mock.post(AUTH_URL, json={"access_token": "jwt"})
    aioclient_mock.get(f"{ROOMS_URL}?maxPageSize=100", json=ROOMS)
    aioclient_mock.get(ROOMS_URL, status=500, headers={"x-request-id": "abc123"},
                       text='{"errors":[{"code":"InternalError"}]}')
    aioclient_mock.get(_booking_url(), json=_reservation((ROOM_06, "CheckedIn")))
    aioclient_mock.get(f"{exely_api.API_URL}/content/v1/properties/{PROP}",
                       json={"id": PROP, "name": "***"})
    aioclient_mock.get(f"{exely_api.API_URL}/read-reservation/v1/properties/{PROP}/bookings/"
                       f"{BOOKING}", status=500, text='{"errors":[{"code":"InternalError"}]}')
    entry = await _api_hotel(hass, make_entry)
    result = await hass.services.async_call(DOMAIN, "exely_api_probe", {"booking": BOOKING},
                                            blocking=True, return_response=True)
    assert result["sign_in"] == "ok" and result["property_id"] == PROP
    assert not result["rooms"]["ok"] and "InternalError" in result["rooms"]["error"]
    assert "request_id: abc123" in result["rooms"]["error"]
    assert result["rooms_max_page_size"] == {"ok": True, "rooms": 2, "shape": shape(ROOMS)}
    assert result["reservation"]["stays"][0]["room_id"] == ROOM_06
    assert result["content_property"] == {"ok": True, "shape": {"id": "str", "name": "str"}}
    assert not result["read_reservation_booking"]["ok"]
    assert "***" not in repr(result)  # structure only, no guest values
    assert _calls(aioclient_mock, ROOMS_URL) == 2  # no retries


async def test_probe_service_needs_the_api(hass, make_entry, patch_api):
    from homeassistant.exceptions import ServiceValidationError
    await _exely_hotel(hass, make_entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "exely_api_probe", {}, blocking=True,
                                       return_response=True)
