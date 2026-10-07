"""Wi-Fi traffic per room and hour, from the controller's per-client counters.

Omada reports, for every connected client, the bytes down / up of its current
connection (``trafficDown`` / ``trafficUp``). Each poll adds the growth since
the previous poll to the room the client is in now; an hour's totals per room
and device category become one ``room_traffic`` row.

* A counter that went down means a new connection: its whole value is new.
* A client seen for the first time after a restart only sets the baseline
  (its counter holds traffic from before, possibly from another room); one
  that connects later counts from zero.
* Clients outside a room (AP without an Area) are not counted.

No Home Assistant imports: pure logic, unit-tested in isolation.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class _Bucket:
    down: int = 0
    up: int = 0
    macs: set[str] = field(default_factory=set)


class TrafficMeter:
    def __init__(self) -> None:
        self._last: dict[str, tuple[int, int]] = {}
        self._started = False
        self.hour: datetime | None = None
        self.buckets: dict[tuple[str, str], _Bucket] = {}

    def update(self, hour: datetime, counters: Mapping[str, tuple[int, int]],
               area_of: Callable[[str], str | None],
               category_of: Callable[[str], str]) -> list[dict]:
        """Feed one poll; returns the rows of an hour that just ended."""
        rows = self.flush() if self.hour is not None and hour != self.hour else []
        self.hour = hour
        for mac, (down, up) in counters.items():
            previous = self._last.get(mac)
            self._last[mac] = (down, up)
            if previous is None:
                if not self._started:
                    continue  # baseline after a restart
                previous = (0, 0)
            d_down = down - previous[0] if down >= previous[0] else down
            d_up = up - previous[1] if up >= previous[1] else up
            if not (d_down or d_up) or not (area := area_of(mac)):
                continue
            bucket = self.buckets.setdefault((area, category_of(mac)), _Bucket())
            bucket.down += d_down
            bucket.up += d_up
            bucket.macs.add(mac)
        for mac in [m for m in self._last if m not in counters]:
            del self._last[mac]  # disconnected: a reconnect counts from zero
        self._started = True
        return rows

    def flush(self) -> list[dict]:
        """Rows of the current hour so far (and start a new accumulation)."""
        rows = [{"ts": self.hour, "area_id": area, "category": category,
                 "down_bytes": b.down, "up_bytes": b.up, "devices": len(b.macs)}
                for (area, category), b in sorted(self.buckets.items()) if b.down or b.up]
        self.buckets = {}
        return rows
