"""Exely Connect API: which room a booking is in.

The Exely webhook names the booking (``BookingNumber``), not the room. The
room comes from the PMS API: the reservation's room stays give ``roomId``,
the property's room list gives its name, which is then matched to an Area.

Requests are kept to a minimum - Exely is called only when a webhook needs a
room, never in the background:

* one OAuth token (client credentials) per 14 minutes, fetched when needed;
* a reservation is cached for the stay (check-in and check-out of a
  single-room booking cost one request), concurrent lookups share one call;
* the room list is stored on disk and refreshed at most once a day, or once
  an hour when an unknown room appears;
* own ceiling of 1 request per second and 30 per hour (Exely allows 3000),
  ``429 retry-after`` is honoured, at most 2 retries on errors.

Only room stay identifiers, statuses and dates are kept; guest data in the
responses is dropped (the diagnostics show the response *shape*, not values).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STORAGE_VERSION
from .presence import STATUS_CHECKED_IN, STATUS_CHECKED_OUT

LOGGER = logging.getLogger(__name__)

AUTH_URL = "https://connect.hopenapi.com/auth/token"
PMS_URL = "https://connect.hopenapi.com/api/pms"
STORAGE_KEY_ROOMS = f"{DOMAIN}.exely_rooms"

STAY_CANCELLED = "cancelled"

TOKEN_LIFETIME = 14 * 60       # Exely tokens live 15 minutes
MIN_INTERVAL = 1.0             # seconds between two API requests
HOURLY_LIMIT = 30              # API requests per hour (token requests not counted)
MAX_QUEUE_WAIT = 600           # wait at most this long for a free slot, else fail
MAX_RETRY_AFTER = 120          # a longer 429 retry-after fails the lookup
RETRIES = 2                    # on network errors / 5xx / 429
RETRY_DELAY = 5
REQUEST_TIMEOUT = 20
BOOKING_CACHE_SECONDS = 30 * 24 * 3600
BOOKING_CACHE_SIZE = 300
ROOMS_MAX_AGE = 24 * 3600
ROOMS_UNKNOWN_REFRESH = 3600   # refresh for an unknown roomId at most this often
ROOMS_PAGE_SIZE = 100
RECENT_LOOKUPS = 10

_sleep = asyncio.sleep  # patched in tests


class ExelyApiError(Exception):
    """The Exely API could not answer (network, HTTP error, rate limit)."""


class ExelyAuthError(ExelyApiError):
    """client_id / client_secret rejected."""


class ExelyNotFound(ExelyApiError):
    """Unknown property or reservation (HTTP 404)."""


# --------------------------------------------------------------------------- #
# Response parsing (pure). The API documents no response schemas, so the
# fields are looked up by a few plausible names; diagnostics show the shape.
# --------------------------------------------------------------------------- #
def _norm(key: Any) -> str:
    return re.sub(r"[\s_\-.]", "", str(key)).lower()


def _first(item: Mapping, *names: str) -> Any:
    wanted = {_norm(n) for n in names}
    for key, value in item.items():
        if _norm(key) in wanted and value not in (None, ""):
            return value
    return None


def _find_list(node: Any, names: set[str], depth: int = 0) -> list | None:
    """First list under one of ``names`` (normalised keys), breadth first."""
    if depth > 3 or not isinstance(node, Mapping):
        return None
    for key, value in node.items():
        if _norm(key) in names and isinstance(value, list):
            return value
    for value in node.values():
        if isinstance(value, Mapping) and (found := _find_list(value, names, depth + 1)) is not None:
            return found
    return None


def stay_status(value: Any) -> str | None:
    """Exely room stay status -> checked_in / checked_out / cancelled / None."""
    text = _norm(value or "")
    if "cancel" in text:
        return STAY_CANCELLED
    if "checkedout" in text or "checkout" in text or "departed" in text:
        return STATUS_CHECKED_OUT
    if "checkedin" in text or "checkin" in text or "inhouse" in text:
        return STATUS_CHECKED_IN
    return None


@dataclass(frozen=True)
class RoomStay:
    stay_id: str | None
    room_id: str | None
    room_name: str | None = None
    status: str | None = None          # checked_in / checked_out / cancelled / None
    raw_status: str | None = None
    check_in: str | None = None         # planned, as Exely sends it (local time)
    check_out: str | None = None
    actual_check_in: str | None = None
    actual_check_out: str | None = None


def _dates(item: Mapping) -> dict[str, str | None]:
    found: dict[str, str | None] = {"check_in": None, "check_out": None,
                                    "actual_check_in": None, "actual_check_out": None}
    for key, value in item.items():
        if not isinstance(value, str):
            continue
        nk = _norm(key)
        if not ("date" in nk or "time" in nk):
            continue
        if "checkin" in nk or "arrival" in nk:
            name = "check_in"
        elif "checkout" in nk or "departure" in nk:
            name = "check_out"
        else:
            continue
        if "actual" in nk:
            name = f"actual_{name}"
        found[name] = found[name] or value
    return found


def _str(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value).strip() or None


def parse_reservation(data: Any) -> list[RoomStay]:
    """Room stays of a reservation (``roomStays`` anywhere near the top)."""
    items = _find_list(data, {"roomstays", "stays"}) or []
    stays = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        room = item.get("room") if isinstance(item.get("room"), Mapping) else {}
        raw_status = _first(item, "status", "roomStayStatus", "stayStatus", "state")
        stays.append(RoomStay(
            stay_id=_str(_first(item, "pmsRoomStayId", "roomStayId", "id")),
            room_id=_str(_first(item, "roomId") or _first(room, "id", "roomId")),
            room_name=_str(_first(item, "roomName", "roomNumber")
                           or _first(room, "name", "roomName", "number")),
            status=stay_status(raw_status),
            raw_status=_str(raw_status),
            **_dates(item),
        ))
    return stays


def parse_rooms(data: Any) -> tuple[dict[str, str], str | None]:
    """``{roomId: name}`` of one page and the next page token (or None)."""
    items = _find_list(data, {"rooms", "items", "data", "results"}) or (
        data if isinstance(data, list) else [])
    rooms = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        room_id = _str(_first(item, "id", "roomId"))
        name = _str(_first(item, "name", "roomName", "displayName", "number"))
        if room_id and name:
            rooms[room_id] = name
    token = _first(data, "nextPageToken") if isinstance(data, Mapping) else None
    more = _first(data, "hasNextPage") if isinstance(data, Mapping) else None
    return rooms, (str(token) if token and more is not False else None)


def shape(node: Any, depth: int = 0) -> Any:
    """Keys and value types only (no values): the response format, no guest data."""
    if depth > 6:
        return "..."
    if isinstance(node, Mapping):
        return {str(k): shape(v, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [shape(node[0], depth + 1)] if node else []
    return type(node).__name__


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class ExelyApi:
    def __init__(self, hass: HomeAssistant, credentials: Callable[[], tuple[str, str] | None],
                 *, clock: Callable[[], float] = time.monotonic) -> None:
        self.hass = hass
        self._credentials = credentials
        self._clock = clock
        self._session = async_get_clientsession(hass)
        self._token: str | None = None
        self._token_for: tuple[str, str] | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()
        self._slot_lock = asyncio.Lock()
        self._last_request = -MIN_INTERVAL
        self._window: deque[float] = deque()
        self._bookings: dict[tuple[str, str], tuple[list[RoomStay], float]] = {}
        self._inflight: dict[tuple[str, str], asyncio.Future] = {}
        self._rooms_store: Store[dict] = Store(hass, STORAGE_VERSION, STORAGE_KEY_ROOMS)
        self._rooms: dict[str, dict] | None = None  # property -> {"fetched", "rooms"}
        self._rooms_checked: dict[str, float] = {}
        self.min_interval = MIN_INTERVAL
        self.retry_delay = RETRY_DELAY
        self.requests = 0
        self.token_requests = 0
        self.last_error: str | None = None
        self.recent: deque[dict] = deque(maxlen=RECENT_LOOKUPS)

    @property
    def configured(self) -> bool:
        return self._credentials() is not None

    # -- rate limiting ------------------------------------------------------- #
    async def _slot(self) -> None:
        """Wait for a free request slot: MIN_INTERVAL apart, HOURLY_LIMIT per hour."""
        async with self._slot_lock:
            now = self._clock()
            while self._window and now - self._window[0] >= 3600:
                self._window.popleft()
            wait = 0.0
            if len(self._window) >= HOURLY_LIMIT:
                wait = self._window[0] + 3600 - now
                if wait > MAX_QUEUE_WAIT:
                    raise ExelyApiError("own hourly request limit reached")
            wait = max(wait, self._last_request + self.min_interval - now)
            if wait > 0:
                await _sleep(wait)
            self._last_request = self._clock()
            self._window.append(self._last_request)
            self.requests += 1

    def requests_last_hour(self) -> int:
        now = self._clock()
        return sum(1 for t in self._window if now - t < 3600)

    # -- token ------------------------------------------------------------- #
    async def _get_token(self) -> str:
        creds = self._credentials()
        if creds is None:
            raise ExelyAuthError("Exely API is not configured")
        async with self._token_lock:
            if self._token and self._token_for == creds and self._clock() < self._token_expires:
                return self._token
            client_id, client_secret = creds
            self.token_requests += 1
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    resp = await self._session.post(AUTH_URL, data={
                        "grant_type": "client_credentials", "client_id": client_id,
                        "client_secret": client_secret})
                    if resp.status in (400, 401, 403):
                        raise ExelyAuthError(f"token request rejected (HTTP {resp.status})")
                    if resp.status >= 400:
                        raise ExelyApiError(f"token request failed (HTTP {resp.status})")
                    body = await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError) as err:
                raise ExelyApiError(f"token request failed: {err or type(err).__name__}") from err
            token = body.get("access_token") if isinstance(body, Mapping) else None
            if not token:
                raise ExelyAuthError("no access_token in the token response")
            lifetime = TOKEN_LIFETIME
            if isinstance(expires := body.get("expires_in"), (int, float)) and expires > 120:
                lifetime = min(lifetime, expires - 60)
            self._token, self._token_for = token, creds
            self._token_expires = self._clock() + lifetime
            return token

    # -- requests ---------------------------------------------------------- #
    async def _get(self, path: str, params: dict | None = None) -> Any:
        error: Exception | None = None
        for attempt in range(RETRIES + 1):
            if attempt:
                await _sleep(self.retry_delay)
            token = await self._get_token()
            await self._slot()
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    resp = await self._session.get(
                        f"{PMS_URL}{path}", params=params,
                        headers={"Authorization": f"Bearer {token}"})
                    if resp.status == 401 and attempt < RETRIES:
                        self._token = None  # expired early: one new token
                        error = ExelyAuthError("HTTP 401")
                        continue
                    if resp.status == 429:
                        retry_after = _retry_after(resp.headers.get("retry-after"))
                        error = ExelyApiError("HTTP 429 (Exely rate limit)")
                        if retry_after > MAX_RETRY_AFTER or attempt == RETRIES:
                            break
                        await _sleep(retry_after)
                        continue
                    if resp.status == 404:
                        raise ExelyNotFound(f"not found: {path}")
                    if resp.status in (401, 403):
                        raise ExelyAuthError(f"HTTP {resp.status}")
                    if resp.status >= 500:
                        error = ExelyApiError(f"HTTP {resp.status}")
                        continue
                    if resp.status >= 400:
                        raise ExelyApiError(f"HTTP {resp.status}")
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError) as err:
                error = ExelyApiError(f"request failed: {err or type(err).__name__}")
        raise error or ExelyApiError("request failed")

    # -- rooms ------------------------------------------------------------- #
    async def _rooms_data(self) -> dict[str, dict]:
        if self._rooms is None:
            self._rooms = await self._rooms_store.async_load() or {}
        return self._rooms

    async def async_rooms(self, property_id: str, *, refresh: bool = False) -> dict[str, str]:
        """``{roomId: name}``, from disk unless older than a day (or ``refresh``)."""
        data = await self._rooms_data()
        cached = data.get(property_id)
        fetched = dt_util.parse_datetime(cached["fetched"]) if cached else None
        fresh = fetched is not None and (dt_util.utcnow() - fetched).total_seconds() < ROOMS_MAX_AGE
        if cached and fresh and not refresh:
            return cached["rooms"]
        rooms: dict[str, str] = {}
        params: dict[str, Any] = {"maxPageSize": ROOMS_PAGE_SIZE}
        for _ in range(20):  # 2000 rooms at most
            page, token = parse_rooms(await self._get(f"/v2/properties/{property_id}/rooms", params))
            rooms.update(page)
            if not token:
                break
            params = {"pageToken": token}
        data[property_id] = {"fetched": dt_util.utcnow().isoformat(), "rooms": rooms}
        self._rooms_checked[property_id] = self._clock()
        await self._rooms_store.async_save(data)
        return rooms

    async def async_room_name(self, property_id: str, room_id: str) -> str | None:
        rooms = await self.async_rooms(property_id)
        if room_id in rooms:
            return rooms[room_id]
        last = self._rooms_checked.get(property_id)
        if last is not None and self._clock() - last < ROOMS_UNKNOWN_REFRESH:
            return None
        return (await self.async_rooms(property_id, refresh=True)).get(room_id)

    # -- reservations ------------------------------------------------------ #
    def forget(self, property_id: str, number: str) -> None:
        """The booking changed (other webhook event): look it up again next time."""
        self._bookings.pop((property_id, number), None)

    def cached_stays(self, property_id: str, number: str) -> list[RoomStay] | None:
        cached = self._bookings.get((property_id, number))
        if cached and self._clock() - cached[1] < BOOKING_CACHE_SECONDS:
            return cached[0]
        return None

    async def async_reservation(self, property_id: str, number: str) -> list[RoomStay]:
        """Room stays of a reservation, fetched now (concurrent calls share one request)."""
        key = (property_id, number)
        if (pending := self._inflight.get(key)) is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future = self.hass.loop.create_future()
        self._inflight[key] = future
        try:
            data = await self._get(f"/v2/properties/{property_id}/reservations/{number}")
            stays = parse_reservation(data)
            self.recent.appendleft({
                "time": dt_util.utcnow().isoformat(), "booking": number,
                "stays": [asdict(s) for s in stays], "shape": shape(data)})
            self._bookings[key] = (stays, self._clock())
            while len(self._bookings) > BOOKING_CACHE_SIZE:
                self._bookings.pop(next(iter(self._bookings)))
            future.set_result(stays)
            return stays
        except Exception as err:
            future.set_exception(err)
            future.exception()  # retrieved: no "never retrieved" warning
            raise
        finally:
            self._inflight.pop(key, None)

    async def async_check(self, property_id: str) -> int:
        """Connection test for the options: token + room list. Returns the room count."""
        return len(await self.async_rooms(property_id, refresh=True))

    def diagnostics(self) -> dict:
        rooms = self._rooms or {}
        return {
            "configured": self.configured,
            "requests": self.requests,
            "requests_last_hour": self.requests_last_hour(),
            "token_requests": self.token_requests,
            "last_error": self.last_error,
            "cached_bookings": len(self._bookings),
            "rooms": {p: {"fetched": r.get("fetched"), "rooms": r.get("rooms")}
                      for p, r in rooms.items()},
            "recent_lookups": list(self.recent),
        }


def _retry_after(value: str | None) -> float:
    try:
        return max(float(value), 1.0) if value else float(RETRY_DELAY)
    except ValueError:
        return float(MAX_RETRY_AFTER + 1)  # an HTTP date: too long, give up
