"""Room = Home Assistant Area. Location source = the access point's Area.

The AP -> Area link lives in Home Assistant (device registry), so the owner
can change it in the UI. This module resolves it, reports problems
("AP without Area", "Area differs from the expected table") and can assign
Areas from a MAC -> Room table (the hotel's device list), matching APs by
MAC, never by their (editable) name in Omada.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .device_list import parse_csv_rows
from .ids import NS_AP, make_unique_id
from .mac import parse_mac

STATUS_OK = "ok"
STATUS_NO_AREA = "no_area"
STATUS_MISMATCH = "mismatch"
STATUS_NOT_FOUND = "not_found"  # expected MAC is not an AP on this controller
STATUS_NO_DEVICE = "no_device"  # AP known to Omada but not (yet) in HA


def access_point_macs(controller) -> dict[str, str]:
    """MAC -> name of every access point (switches/gateways are not locations)."""
    return {mac: ap.name for mac, ap in controller.access_points.items()}


def ap_device(hass: HomeAssistant, controller, mac: str) -> dr.DeviceEntry | None:
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, make_unique_id(NS_AP, controller.site_id, mac)),
        controller.entry.entry_id)


def resolve_ap_areas(hass: HomeAssistant, controller) -> dict[str, str | None]:
    """AP MAC -> area_id (None when the AP has no Area)."""
    return {mac: (dev.area_id if (dev := ap_device(hass, controller, mac)) else None)
            for mac in access_point_macs(controller)}


def resolve_area(hass: HomeAssistant, value: str, *, create: bool = False) -> ar.AreaEntry | None:
    """Find an Area by id or (case-insensitive) name, optionally creating it."""
    areas = ar.async_get(hass)
    value = value.strip()
    if area := areas.async_get_area(value):
        return area
    if area := areas.async_get_area_by_name(value):
        return area
    if create and value:
        return areas.async_create(value)
    return None


def parse_mac_table(mapping: Mapping[str, str] | None = None,
                    csv_text: str | None = None) -> tuple[dict[str, str], list[str]]:
    """Merge a ``{mac: room}`` mapping and/or a CSV with ``mac`` + ``area``/``room``
    columns into ``{normalised mac: room}``. Returns (table, errors)."""
    table: dict[str, str] = {}
    errors: list[str] = []
    for mac, area in (mapping or {}).items():
        try:
            table[parse_mac(mac)] = str(area).strip()
        except ValueError as err:
            errors.append(str(err))
    if csv_text:
        for line_no, row in parse_csv_rows(csv_text, fields=("mac", "area")):
            area = row.get("area") or row.get("room") or ""
            try:
                mac = parse_mac(row.get("mac", ""))
            except ValueError as err:
                errors.append(f"line {line_no}: {err}")
                continue
            if not area:
                errors.append(f"line {line_no}: no room/area for {mac}")
                continue
            table[mac] = area.strip()
    return table, errors


@dataclass
class ApAreaRow:
    mac: str
    name: str | None
    area_id: str | None
    area: str | None
    expected: str | None
    status: str


def ap_area_report(hass: HomeAssistant, controller,
                   expected: Mapping[str, str] | None = None) -> dict:
    """Diagnostic "AP -> Area" report."""
    areas = ar.async_get(hass)
    expected = expected or {}
    rows: list[ApAreaRow] = []
    aps = access_point_macs(controller)

    for mac, name in sorted(aps.items(), key=lambda i: (i[1] or "", i[0])):
        device = ap_device(hass, controller, mac)
        area = areas.async_get_area(device.area_id) if device and device.area_id else None
        want = expected.get(mac)
        want_area = resolve_area(hass, want) if want else None
        if device is None:
            status = STATUS_NO_DEVICE
        elif area is None:
            status = STATUS_NO_AREA
        elif want and (want_area is None or want_area.id != area.id):
            status = STATUS_MISMATCH
        else:
            status = STATUS_OK
        rows.append(ApAreaRow(mac, name, area.id if area else None,
                              area.name if area else None, want, status))

    for mac, want in sorted(expected.items()):
        if mac not in aps:
            rows.append(ApAreaRow(mac, None, None, None, want, STATUS_NOT_FOUND))

    problems = [r for r in rows if r.status != STATUS_OK]
    return {
        "access_points": [asdict(r) for r in rows],
        "problems": len(problems),
        "summary": {s: sum(1 for r in rows if r.status == s) for s in
                    (STATUS_OK, STATUS_NO_AREA, STATUS_MISMATCH, STATUS_NOT_FOUND, STATUS_NO_DEVICE)},
    }


def assign_ap_areas(hass: HomeAssistant, controller, table: Mapping[str, str],
                    *, create_missing_areas: bool = False) -> dict:
    """Set each AP's Area from ``{mac: area name or id}``."""
    devices = dr.async_get(hass)
    aps = access_point_macs(controller)
    result = {"assigned": [], "unchanged": [], "errors": []}
    for mac, area_name in table.items():
        if mac not in aps:
            result["errors"].append(f"{mac}: not an access point on this controller")
            continue
        device = ap_device(hass, controller, mac)
        if device is None:
            result["errors"].append(f"{mac}: access point is not registered in Home Assistant yet")
            continue
        area = resolve_area(hass, area_name, create=create_missing_areas)
        if area is None:
            result["errors"].append(f"{mac}: area '{area_name}' does not exist")
            continue
        if device.area_id == area.id:
            result["unchanged"].append(mac)
            continue
        devices.async_update_device(device.id, area_id=area.id)
        result["assigned"].append({"mac": mac, "ap": aps[mac], "area": area.name})
    return result
