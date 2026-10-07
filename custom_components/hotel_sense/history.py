"""History database (MariaDB add-on): what happened in which room, kept for months.

Hotel Sense writes to its own database (not the recorder's), e.g. the MariaDB
add-on on the same machine (host ``core-mariadb``). Writing never blocks Home
Assistant and never loses the live picture:

* rows are queued in memory and written in batches (every ``FLUSH_INTERVAL``
  seconds or ``BATCH_SIZE`` rows) in the executor;
* while the database is unreachable the queue keeps up to ``MAX_QUEUE`` rows
  (the oldest are dropped beyond that) and is written once it is back;
* rows older than the retention period are deleted once a day at night.

Privacy: MACs, SSIDs, room (Area) IDs and booking numbers only - no client
names, IP addresses or guest data. Times are UTC.

Tables (schema version 3):

``wifi_events``        Omada webhook client events (online / offline / roaming)
``presence_sessions``  a device was in a room from ... to ... (written when it ends)
``room_states``        room state changes (empty / violation / staff_visit / checked_in)
``room_status``        status changes (checked_in / checked_out) and who set them
``pms_events``         Exely webhook events and what came of them
``room_traffic``       Wi-Fi traffic per room, device category and hour
``rooms``              the room model: number, name, kind (replaced when it changes)

Read through the ``v1_*`` views (``views.py``), the stable interface.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.util import dt as dt_util
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, Integer, MetaData, String, Table, create_engine,
    delete, func, insert, select,
)
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import SQLAlchemyError

from .views import create_views

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 3  # 2: room_traffic, 3: rooms + views v1
DRIVER = "mysql+pymysql"
FLUSH_INTERVAL = timedelta(seconds=15)
BATCH_SIZE = 200
MAX_QUEUE = 20000
PURGE_BATCH = 5000
DEFAULT_RETENTION_MONTHS = 12

_ID = BigInteger().with_variant(Integer, "sqlite")  # SQLite autoincrements INTEGER only
_TABLE_ARGS = {"mysql_charset": "utf8mb4", "mysql_engine": "InnoDB"}

metadata = MetaData()

schema_version = Table(
    "schema_version", metadata,
    Column("version", Integer, primary_key=True, autoincrement=False),
    Column("applied", DateTime, nullable=False),
    **_TABLE_ARGS,
)
wifi_events = Table(
    "wifi_events", metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("event", String(16), nullable=False),
    Column("client_mac", String(17), nullable=False, index=True),
    Column("ap_mac", String(17)),
    Column("from_ap_mac", String(17)),
    Column("ssid", String(64)),
    Column("connected_seconds", Integer),
    Column("traffic_kb", Float),
    **_TABLE_ARGS,
)
presence_sessions = Table(
    "presence_sessions", metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("client_mac", String(17), nullable=False, index=True),
    Column("area_id", String(64), nullable=False, index=True),
    Column("category", String(16), nullable=False),  # guest / employee / fixed
    Column("started", DateTime, nullable=False),
    Column("ended", DateTime, nullable=False, index=True),
    Column("seconds", Integer, nullable=False),
    **_TABLE_ARGS,
)
room_states = Table(
    "room_states", metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("area_id", String(64), nullable=False, index=True),
    Column("status", String(16)),
    Column("old_state", String(16)),
    Column("state", String(16), nullable=False),
    Column("guest_devices", Integer),
    Column("employee_devices", Integer),
    **_TABLE_ARGS,
)
room_status = Table(
    "room_status", metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("area_id", String(64), nullable=False, index=True),
    Column("status", String(16), nullable=False),
    Column("source", String(16), nullable=False),  # manual / exely
    Column("booking", String(64)),
    **_TABLE_ARGS,
)
pms_events = Table(
    "pms_events", metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("event_id", String(64), index=True),
    Column("event", String(64)),
    Column("booking", String(64), index=True),
    Column("property_id", String(32)),
    Column("status", String(16)),
    Column("result", String(16), nullable=False),
    Column("rooms", String(255)),
    **_TABLE_ARGS,
)
room_traffic = Table(
    "room_traffic", metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),  # start of the hour (UTC)
    Column("area_id", String(64), nullable=False, index=True),
    Column("category", String(16), nullable=False),  # guest / employee / fixed
    Column("down_bytes", BigInteger, nullable=False),
    Column("up_bytes", BigInteger, nullable=False),
    Column("devices", Integer, nullable=False),
    **_TABLE_ARGS,
)
# The room model (number, name, kind), replaced as a whole when it changes.
rooms = Table(
    "rooms", metadata,
    Column("room_id", String(64), primary_key=True),
    Column("number", String(16)),
    Column("name", String(128), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("updated", DateTime, nullable=False),
    **_TABLE_ARGS,
)
TABLES = {t.name: t for t in (wifi_events, presence_sessions, room_states, room_status,
                              pms_events, room_traffic)}
# Column that ages a row out.
_AGE_COLUMN = {"presence_sessions": "ended"}


class HistoryUnavailable(Exception):
    """The history database cannot be read now."""


def build_url(host: str, port: int, username: str, password: str, database: str) -> URL:
    return URL.create(DRIVER, username=username, password=password, host=host, port=int(port),
                      database=database, query={"charset": "utf8mb4"})


def now() -> datetime:
    """UTC without tzinfo, as stored (DATETIME has no time zone)."""
    return dt_util.utcnow().replace(tzinfo=None)


def make_engine(url: URL | str) -> Engine:
    return create_engine(url, pool_pre_ping=True, pool_recycle=3600)


def _create_schema(engine: Engine) -> None:
    metadata.create_all(engine)
    try:
        with engine.begin() as conn:
            create_views(conn)
    except SQLAlchemyError as err:  # e.g. no CREATE VIEW right: the tables still work
        LOGGER.warning("History database: views not created: %s", str(err).splitlines()[0][:300])
    with engine.begin() as conn:
        # New tables are created by create_all; record the version once.
        current = conn.execute(select(func.max(schema_version.c.version))).scalar()
        if current is None or current < SCHEMA_VERSION:
            conn.execute(insert(schema_version).values(version=SCHEMA_VERSION, applied=now()))


def check_connection(url: URL | str) -> None:
    """Connect and create the tables (executor). Raises SQLAlchemyError."""
    engine = make_engine(url)
    try:
        _create_schema(engine)
    finally:
        engine.dispose()


def error_reason(err: Exception) -> str:
    """auth / database / connect - from the MySQL error code."""
    code = None
    orig = getattr(err, "orig", None)
    if orig is not None and getattr(orig, "args", None):
        code = orig.args[0]
    if code in (1044, 1045):
        return "auth"
    if code == 1049:
        return "database"
    return "connect"


class HistoryWriter:
    def __init__(self, hass: HomeAssistant, url: URL | str,
                 retention_months: int = DEFAULT_RETENTION_MONTHS) -> None:
        self.hass = hass
        self.url = url
        self.retention_months = retention_months
        self._engine: Engine | None = None
        self._queue: deque[tuple[str, dict]] = deque()
        self._rooms: list[dict] | None = None  # room model snapshot to write
        self._lock = asyncio.Lock()  # one flush / purge at a time
        self._unsubs: list[CALLBACK_TYPE] = []
        self.connected = False
        self.written = 0
        self.dropped = 0
        self.purged = 0
        self.last_write: str | None = None
        self.last_purge: str | None = None
        self.last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------- #
    @callback
    def async_start(self) -> None:
        """Start the timers; the first flush connects (setup never waits for the database)."""
        self._unsubs = [
            async_track_time_interval(self.hass, self._async_flush_timer, FLUSH_INTERVAL),
            async_track_time_change(self.hass, self._async_purge_timer, hour=4, minute=17, second=0),
        ]

    async def _async_connect(self) -> bool:
        if self._engine is not None:
            return True
        try:
            engine = await self.hass.async_add_executor_job(make_engine, self.url)
        except Exception as err:  # noqa: BLE001 - e.g. the driver is missing
            self._failed(err)
            return False
        try:
            await self.hass.async_add_executor_job(_create_schema, engine)
        except Exception as err:  # noqa: BLE001 - never break Home Assistant

            self._failed(err)
            await self.hass.async_add_executor_job(engine.dispose)
            return False
        self._engine = engine
        LOGGER.info("History database connected")
        self.connected = True
        return True

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self.async_flush()
        async with self._lock:
            await self._async_reset()

    def _failed(self, err: Exception) -> None:
        message = f"{type(err).__name__}: {str(err).splitlines()[0][:300]}"
        if self.connected or self.last_error is None:
            LOGGER.warning("History database unavailable, rows are queued: %s", message)
        self.connected = False
        self.last_error = message

    # -- writing ------------------------------------------------------------ #
    @callback
    def set_rooms(self, room_rows: list[dict]) -> None:
        """The room model changed: write this snapshot with the next flush."""
        self._rooms = room_rows

    @callback
    def add(self, table: str, **row: Any) -> None:
        """Queue one row (``ts`` defaults to now)."""
        if table not in TABLES:
            raise ValueError(f"unknown table {table}")
        if "ts" in TABLES[table].c and "ts" not in row:
            row["ts"] = now()
        if len(self._queue) >= MAX_QUEUE:
            self._queue.popleft()
            self.dropped += 1
        self._queue.append((table, row))
        if len(self._queue) >= BATCH_SIZE and not self._lock.locked():
            self.hass.async_create_task(self.async_flush(), eager_start=False)

    async def _async_flush_timer(self, _now=None) -> None:
        await self.async_flush()

    async def async_flush(self) -> None:
        """Write the queue (waits for a flush already running)."""
        async with self._lock:
            if (not self._queue and self._rooms is None) or not await self._async_connect():
                return
            batch = list(self._queue)
            room_rows = self._rooms
            try:
                await self.hass.async_add_executor_job(self._write, batch, room_rows)
            except Exception as err:  # noqa: BLE001 - kept queued, retried next time
                self._failed(err)
                await self._async_reset()
                return
            for _ in batch:
                self._queue.popleft()
            if self._rooms is room_rows:
                self._rooms = None
            self.written += len(batch)
            self.last_write = dt_util.utcnow().isoformat()
            self.last_error = None
            self.connected = True

    async def async_read(self, func):
        """Run ``func(connection)`` in the executor, after writing what is queued.

        For reports (``queries.py``): reads through the ``v1_*`` views.
        """
        await self.async_flush()
        if not await self._async_connect():
            raise HistoryUnavailable(self.last_error or "history database unavailable")
        engine = self._engine

        def _run():
            with engine.connect() as conn:
                return func(conn)

        return await self.hass.async_add_executor_job(_run)

    async def _async_reset(self) -> None:
        if self._engine is not None:
            engine, self._engine = self._engine, None
            await self.hass.async_add_executor_job(engine.dispose)

    def _write(self, batch: list[tuple[str, dict]], room_rows: list[dict] | None = None) -> None:
        grouped: dict[str, list[dict]] = {}
        for table, row in batch:
            grouped.setdefault(table, []).append(row)
        assert self._engine is not None
        with self._engine.begin() as conn:
            for table, rows in grouped.items():
                conn.execute(insert(TABLES[table]), rows)
            if room_rows is not None:
                conn.execute(delete(rooms))
                if room_rows:
                    stamp = now()
                    conn.execute(insert(rooms), [dict(r, updated=stamp) for r in room_rows])

    # -- retention ------------------------------------------------------------ #
    async def _async_purge_timer(self, _now=None) -> None:
        await self.async_purge()

    async def async_purge(self) -> int:
        """Delete rows older than the retention period (in batches)."""
        async with self._lock:
            if not self.retention_months or not await self._async_connect():
                return 0
            cutoff = now() - timedelta(days=30 * self.retention_months)
            try:
                removed = await self.hass.async_add_executor_job(self._purge, cutoff)
            except Exception as err:  # noqa: BLE001
                self._failed(err)
                await self._async_reset()
                return 0
        self.purged += removed
        self.last_purge = dt_util.utcnow().isoformat()
        if removed:
            LOGGER.info("History database: %s rows older than %s removed", removed, cutoff.date())
        return removed

    def _purge(self, cutoff: datetime) -> int:
        assert self._engine is not None
        removed = 0
        for name, table in TABLES.items():
            column = table.c[_AGE_COLUMN.get(name, "ts")]
            while True:
                with self._engine.begin() as conn:
                    ids = [r[0] for r in conn.execute(
                        select(table.c.id).where(column < cutoff).limit(PURGE_BATCH))]
                    if not ids:
                        break
                    conn.execute(delete(table).where(table.c.id.in_(ids)))
                removed += len(ids)
                if len(ids) < PURGE_BATCH:
                    break
        return removed

    @property
    def queued(self) -> int:
        return len(self._queue)

    def diagnostics(self) -> dict:
        return {
            "connected": self.connected,
            "queued": self.queued,
            "written": self.written,
            "dropped": self.dropped,
            "purged": self.purged,
            "retention_months": self.retention_months,
            "last_write": self.last_write,
            "last_purge": self.last_purge,
            "last_error": self.last_error,
        }
