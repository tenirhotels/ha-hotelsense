"""Every Wi-Fi MAC seen, for the ``macs`` table of the history database.

Omada keeps no first-seen date for a client (and forgets clients after its
data retention), so Hotel Sense keeps its own: per MAC the first and last
time it was on the Wi-Fi, the time on the Wi-Fi, the networks it used, Omada's
vendor / model / OS and the name the device gives, and the device on the list
it belongs to. Fed by every poll, handed to the history writer every few
minutes as one batch (the writer merges it into the table).

No Home Assistant imports: pure logic, unit-tested in isolation.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from .mac import is_random_mac

HAND_OVER = timedelta(minutes=5)
MAX_GAP = timedelta(minutes=10)  # longer between two sightings: not counted as time on the Wi-Fi


class MacTracker:
    def __init__(self) -> None:
        self._last_seen: dict[str, datetime] = {}
        self._pending: dict[str, dict] = {}
        self._handed_over: datetime | None = None

    def observe(self, now: datetime, clients: Iterable) -> None:
        """One poll: the Wi-Fi clients Omada reports (``ConnectedClient``)."""
        seen = set()
        for client in clients:
            if not client.wireless:
                continue
            mac = client.mac
            seen.add(mac)
            previous = self._last_seen.get(mac)
            gap = (now - previous) if previous else None
            row = self._pending.setdefault(mac, {
                "mac": mac, "first_seen": now, "last_seen": now, "seconds": 0,
                "random": is_random_mac(mac), "ssids": set()})
            row["last_seen"] = now
            if gap is not None and timedelta(0) < gap <= MAX_GAP:
                row["seconds"] += int(gap.total_seconds())
            for key in ("name", "vendor", "model", "os"):
                if value := getattr(client, key, None):
                    row[key] = value
            if client.ssid:
                row["ssids"].add(client.ssid)
            self._last_seen[mac] = now
        for mac in [m for m in self._last_seen if m not in seen]:
            del self._last_seen[mac]  # gone: the next sighting starts a new stretch

    def take(self, now: datetime, identity_of: Callable[[str], str | None],
             force: bool = False) -> list[dict]:
        """The rows collected since the last hand-over, every ``HAND_OVER``."""
        if not self._pending:
            return []
        if not force and self._handed_over is not None and now - self._handed_over < HAND_OVER:
            return []
        self._handed_over = now
        rows, self._pending = list(self._pending.values()), {}
        for row in rows:
            row["identity_id"] = identity_of(row["mac"])
        return rows
