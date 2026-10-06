"""What kind of device is it: phone, computer, TV ...?

Sources, in priority order (the first that knows wins):

1. ``list``        - the type set by the owner in the device list;
2. ``omada``       - the controller's own classification (category / type /
                     vendor / OS, as in the Omada client export);
3. ``name``        - well-known hostnames (``iPhone``, ``Galaxy-S24`` ...);
4. ``private_mac`` - a locally administered (random) MAC: only personal
                     devices (phones, tablets, laptops, watches) use them;
5. ``unknown``.

No Home Assistant imports: pure logic, unit-tested in isolation.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from .mac import is_random_mac

KIND_PHONE = "phone"
KIND_TABLET = "tablet"
KIND_COMPUTER = "computer"
KIND_WATCH = "watch"
KIND_TV = "tv"
KIND_APPLIANCE = "appliance"  # AC, humidifier, smart home
KIND_PRINTER = "printer"
KIND_POS = "pos"  # payment terminal
KIND_PERSONAL = "personal"  # phone / tablet / laptop, not further known
KIND_OTHER = "other"
KIND_UNKNOWN = "unknown"

KINDS = (KIND_PHONE, KIND_TABLET, KIND_COMPUTER, KIND_WATCH, KIND_TV, KIND_APPLIANCE,
         KIND_PRINTER, KIND_POS, KIND_PERSONAL, KIND_OTHER, KIND_UNKNOWN)
# Kinds the owner can set in the device list.
LISTABLE_KINDS = tuple(k for k in KINDS if k not in (KIND_PERSONAL, KIND_UNKNOWN))

SOURCE_LIST = "list"
SOURCE_OMADA = "omada"
SOURCE_NAME = "name"
SOURCE_PRIVATE_MAC = "private_mac"
SOURCE_UNKNOWN = "unknown"

# Accepted values in the device list (lower-cased), English and Russian.
_KIND_ALIASES = {
    KIND_PHONE: ("phone", "mobile", "smartphone", "телефон", "смартфон"),
    KIND_TABLET: ("tablet", "ipad", "планшет"),
    KIND_COMPUTER: ("computer", "laptop", "notebook", "pc", "desktop", "компьютер", "ноутбук", "пк"),
    KIND_WATCH: ("watch", "smartwatch", "часы"),
    KIND_TV: ("tv", "television", "телевизор", "тв"),
    KIND_APPLIANCE: ("appliance", "ac", "air conditioner", "humidifier", "smart home", "iot",
                     "кондиционер", "увлажнитель", "техника", "умный дом"),
    KIND_PRINTER: ("printer", "mfp", "принтер", "мфу"),
    KIND_POS: ("pos", "terminal", "payment terminal", "терминал", "pos-терминал"),
    KIND_OTHER: ("other", "другое", "прочее"),
}
_ALIAS_TO_KIND = {alias: kind for kind, aliases in _KIND_ALIASES.items() for alias in aliases}


def parse_kind(value: str | None) -> str:
    """Device-list value -> kind ("" when empty). Raises ValueError if unknown."""
    key = (value or "").strip().lower()
    if not key:
        return ""
    if key in KINDS:
        return key
    if key not in _ALIAS_TO_KIND:
        raise ValueError(f"Unknown device type: {value!r}")
    return _ALIAS_TO_KIND[key]


# Omada classification (client category / type / OS / vendor) -> kind.
# Checked in this order; the first matching rule wins.
_OMADA_RULES: tuple[tuple[str, re.Pattern], ...] = (
    (KIND_WATCH, re.compile(r"watch|wearable")),
    (KIND_TABLET, re.compile(r"tablet|ipad|pad\b")),
    (KIND_PHONE, re.compile(r"mobile|phone|android|\bios\b")),
    (KIND_TV, re.compile(r"television|\btv\b|audio\s*&\s*video|media player|set.?top")),
    (KIND_PRINTER, re.compile(r"printer|print")),
    (KIND_COMPUTER, re.compile(r"computer|laptop|notebook|desktop|windows|mac\s?os|linux|"
                               r"raspberry|server|\bpc\b")),
    (KIND_APPLIANCE, re.compile(r"smart|appliance|cleaner|humidifier|conditioner|iot|camera|"
                                r"sensor|plug|light|home")),
)
# Raw API keys that may carry Omada's classification (newest first).
OMADA_KEYS = ("deviceCategory", "clientCategory", "category", "deviceType", "clientType",
              "osName", "os", "deviceOs", "vendor", "manufacturer")
_UNKNOWN_WORDS = {"", "-", "unknown", "others", "other", "none", "null"}


def omada_info(raw: Mapping | None) -> dict[str, str]:
    """The non-empty classification fields of a raw Omada client."""
    info = {}
    for key in OMADA_KEYS:
        value = (raw or {}).get(key)
        if isinstance(value, str) and value.strip().lower() not in _UNKNOWN_WORDS:
            info[key] = value.strip()
    return info


def kind_from_omada(raw: Mapping | None) -> str | None:
    info = omada_info(raw)
    # Category/type first: "Smart Home | Smart Appliance | LG" is an appliance even
    # though LG also makes phones; vendor and OS only if nothing else matched.
    for keys in (OMADA_KEYS[:5], OMADA_KEYS[5:]):
        text = " | ".join(info[k] for k in keys if k in info).lower()
        if not text:
            continue
        for kind, pattern in _OMADA_RULES:
            if pattern.search(text):
                return kind
    return None


_NAME_RULES: tuple[tuple[str, re.Pattern], ...] = (
    (KIND_WATCH, re.compile(r"watch", re.I)),
    (KIND_TABLET, re.compile(r"ipad|galaxy.?tab|\btab\b|tablet|matepad", re.I)),
    (KIND_PHONE, re.compile(r"iphone|android|galaxy|pixel|redmi|poco|honor|oneplus|"
                            r"z.?flip|z.?fold|\bs\d{2}\b|^s\d{2}-|^a\d{2}-", re.I)),
    (KIND_COMPUTER, re.compile(r"macbook|imac|laptop|notebook|desktop|^pc-|thinkpad", re.I)),
    (KIND_TV, re.compile(r"\btv\b|^tv\d|bravia|webos", re.I)),
    (KIND_PRINTER, re.compile(r"printer|xerox|epson|canon|laserjet|\bhp-", re.I)),
    (KIND_POS, re.compile(r"\bpos\b|terminal", re.I)),
)


def kind_from_name(name: str | None) -> str | None:
    if not name:
        return None
    for kind, pattern in _NAME_RULES:
        if pattern.search(name):
            return kind
    return None


def resolve_kind(mac: str, *, listed: str | None = None, omada_raw: Mapping | None = None,
                 name: str | None = None) -> tuple[str, str]:
    """(kind, source) by the priority above."""
    if listed:
        return listed, SOURCE_LIST
    if kind := kind_from_omada(omada_raw):
        return kind, SOURCE_OMADA
    if name and name.replace(":", "-").upper() != mac.upper() and (kind := kind_from_name(name)):
        return kind, SOURCE_NAME
    if is_random_mac(mac):
        return KIND_PERSONAL, SOURCE_PRIVATE_MAC
    return KIND_UNKNOWN, SOURCE_UNKNOWN
