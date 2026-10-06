"""MAC address parsing for user-supplied data (CSV, forms, services).

Device lists arrive in every notation people (and exporters) produce:
``aa:bb:cc:dd:ee:ff``, ``AA-BB-CC-DD-EE-FF``, ``aabb.ccdd.eeff``,
``AABBCCDDEEFF``. Everything is normalised to the Omada notation used by the
API and by :mod:`ids` (upper-case, dash separated).

No Home Assistant imports: pure logic, unit-tested in isolation.
"""
from __future__ import annotations

import re

_HEX12 = re.compile(r"^[0-9A-F]{12}$")
_SEPARATORS = re.compile(r"[\s:\-.]")


def parse_mac(value: str) -> str:
    """Return ``value`` as ``AA-BB-CC-DD-EE-FF`` or raise ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid MAC address: {value!r}")
    raw = value.strip().upper()
    digits = _SEPARATORS.sub("", raw)
    if not _HEX12.match(digits):
        raise ValueError(f"Invalid MAC address: {value!r}")
    # Reject mixed/garbled grouping such as "AABB:CC-DDEEFF" only when the
    # separators split the digits unevenly; common notations all pass.
    groups = [g for g in _SEPARATORS.split(raw) if g]
    if len(groups) not in (1, 3, 6) or len({len(g) for g in groups}) != 1:
        raise ValueError(f"Invalid MAC address: {value!r}")
    return "-".join(digits[i:i + 2] for i in range(0, 12, 2))


def is_valid_mac(value: str) -> bool:
    try:
        parse_mac(value)
    except ValueError:
        return False
    return True


def is_random_mac(mac: str) -> bool:
    """Whether ``mac`` is locally administered (phone "private"/random MAC).

    Such addresses change per network / over time, so one physical phone can
    show up as several devices. They are never merged automatically.
    """
    try:
        first_octet = int(parse_mac(mac)[:2], 16)
    except ValueError:
        return False
    return bool(first_octet & 0b10)
