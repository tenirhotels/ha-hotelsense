"""Staff devices from Omada's known-client list, before our own history has any.

Omada remembers every client it has seen (within its data retention) with the
total time connected. Guests stay days, staff for months and years, so a
device with hundreds of hours on the hotel Wi-Fi that is still around is the
hotel's own - long before ``device_candidates`` has the weeks of history it
needs. Omada has no "first seen" in that list, so total hours are the measure.

Pure logic (the Omada request is in ``omada_hub``), unit-tested in isolation.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
from datetime import datetime, timezone

from .mac import is_random_mac
from .omada_hub import KnownClient

# Under this much traffic per hour connected, a device is suggested as fixed
# equipment (sensor, lock, display): a phone in use moves far more.
FIXED_BYTES_PER_HOUR = 1_000_000

# Total hours connected: how the site's Wi-Fi clients spread (counts only).
BUCKETS = ((24, "under_24h"), (120, "1_to_5_days"), (500, "5_to_21_days"),
           (2000, "21_to_83_days"), (None, "over_83_days"))


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _bucket(hours: float) -> str:
    return next(name for limit, name in BUCKETS if limit is None or hours < limit)


def omada_candidates(clients: Iterable[KnownClient], known: Collection[str], now: float, *,
                     min_hours: int = 500, seen_days: int = 30, wired: bool = False,
                     limit: int = 100) -> dict:
    """Devices not in the device list with at least ``min_hours`` on the Wi-Fi.

    Only those seen in the last ``seen_days`` days (staff who left drop out)
    and only Wi-Fi clients unless ``wired`` (presence counts Wi-Fi only).
    ``suggest`` is ``fixed`` for a device with almost no traffic for its hours
    (under ``FIXED_BYTES_PER_HOUR``), else ``employee``. Devices are never
    merged by name: two phones can carry the same default name.
    ``import_csv`` takes the Omada name where Omada has one; check the list
    and import it with ``hotel_sense.import_devices``.
    """
    known = set(known)
    spread = {name: 0 for _, name in BUCKETS}
    candidates = []
    total = 0
    for client in clients:
        if not (client.wireless or wired):
            continue
        total += 1
        hours = client.duration / 3600
        spread[_bucket(hours)] += 1
        if client.mac in known or hours < min_hours:
            continue
        if client.last_seen is None or now - client.last_seen > seen_days * 86400:
            continue
        name = client.name if client.name and client.name.upper().replace(":", "-") \
            != client.mac else None
        per_hour = (client.download + client.upload) / hours
        suggest = "fixed" if per_hour < FIXED_BYTES_PER_HOUR else "employee"
        candidates.append({
            "mac": client.mac, "omada_name": name, "suggest": suggest,
            "reason": (f"{round(hours)} h on Wi-Fi with almost no traffic" if suggest == "fixed"
                       else f"{round(hours)} h on Wi-Fi"),
            "hours": round(hours),
            "last_seen": _iso(client.last_seen), "first_seen": _iso(client.first_seen),
            "download_gb": round(client.download / 1e9, 1),
            "upload_gb": round(client.upload / 1e9, 1),
            "wireless": client.wireless, "guest_network": client.guest,
            "random_mac": is_random_mac(client.mac),
        })
    candidates.sort(key=lambda c: (-c["hours"], c["mac"]))
    candidates = candidates[:limit]
    lines = ["mac,name,category,note"] + [
        f"{c['mac']},{(c['omada_name'] or '').replace(',', ' ')},{c['suggest']},"
        f"Omada: {c['reason'].replace(',', ' ')}; last seen {(c['last_seen'] or '')[:10]}"
        for c in candidates]
    return {"clients": total, "hours_spread": spread, "candidates": candidates,
            "import_csv": "\n".join(lines) + "\n" if candidates else ""}
