#!/usr/bin/env python3
"""Generate the Hotel Sense "Rooms & Wi-Fi" dashboard (Lovelace YAML, English).

    python scripts/generate_wifi_dashboard.py > dashboards/hotel_sense_wifi_en.yaml

One card per room: the room state, then the devices in the room grouped by the
Wi-Fi network (SSID) they are connected to: who (guest / staff), name, type,
signal and whether the device is still online. Fixed equipment (air
conditioners, humidifiers ...) is left out. A network overview on top counts
devices per SSID across the hotel.

Only built-in cards (markdown) are used. Rooms are HA area IDs; pass your own
with ``--rooms room_01 ... --common admin_house`` if they differ.
"""
from __future__ import annotations

import argparse
import sys

DEFAULT_ROOMS = [f"room_{i:02d}" for i in range(1, 11)]
DEFAULT_COMMON = ["admin_house"]


def title_of(area_id: str) -> str:
    return " ".join(p.capitalize() if not p.isdigit() else p for p in area_id.split("_"))


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in text.splitlines())


# Shared Jinja: guests + staff of one area (fixed equipment excluded), with the
# SSID filled in, grouped by SSID; then one table per network.
_DEVICES = """\
{%- set ns = namespace(all=[]) -%}
{%- for d in state_attr('sensor.@AREA@_guest_devices', 'devices') or [] -%}
{%- set ns.all = ns.all + [dict(d, who='Guest', ssid=d.ssid or 'No SSID')] -%}
{%- endfor -%}
{%- for d in state_attr('sensor.@AREA@_employee_devices', 'devices') or [] -%}
{%- set ns.all = ns.all + [dict(d, who='Staff', ssid=d.ssid or 'No SSID')] -%}
{%- endfor -%}
{%- set kinds = {'phone': '📱 Phone', 'tablet': '📱 Tablet', 'computer': '💻 Computer',
   'watch': '⌚ Watch', 'tv': '📺 TV', 'appliance': '🔌 Appliance', 'printer': '🖨️ Printer',
   'pos': '💳 Terminal', 'personal': '📱 Personal device', 'other': 'Other', 'unknown': '❔ Unknown'} -%}
{%- if not ns.all %}
_No guest or staff devices._
{%- endif %}
{%- for ssid, items in ns.all | groupby('ssid') %}

**📶 {{ ssid }}** · {{ items | count }} device{{ 's' if items | count != 1 }}

| | Device | Type | Who | Signal |
|:-:|:--|:--|:--|:--|
{%- for d in items | sort(attribute='who') %}
{%- set r = d.rssi -%}
{%- set signal = '—' if r is none or not d.connected else (r ~ ' dBm ' ~ ('▂▄▆█' if r >= -60 else '▂▄▆' if r >= -70 else '▂▄' if r >= -80 else '▂')) -%}
{%- set seen = as_datetime(d.last_seen) if d.last_seen else none %}
| {{ '🟢' if d.connected else '⚪' }} | {{ d.name or d.mac }}{% if not d.connected and seen %}<br><sub>left {{ relative_time(seen) }} ago</sub>{% endif %} | {{ kinds.get(d.type, d.type) }} | {{ '🧑‍💼 Staff' if d.who == 'Staff' else '🧳 Guest' }} | {{ signal }} |
{%- endfor %}
{%- endfor %}"""

_STATES = ("{'empty': 'Empty', 'violation': 'VIOLATION', 'staff_visit': 'Staff visit', "
           "'checked_in': 'Checked in'}")
_ALERTS = "{'violation': 'error', 'staff_visit': 'info', 'checked_in': 'success'}"


def room_card(area_id: str) -> str:
    title = title_of(area_id)
    content = f"""\
{{%- set st = states('sensor.{area_id}_state') -%}}
{{%- set status = states('select.{area_id}_status') -%}}
{{%- set label = {_STATES}.get(st, st) -%}}
{{%- set line = 'Status: ' ~ ('checked in' if status == 'checked_in' else 'checked out') ~ ' · Guests: ' ~ states('sensor.{area_id}_guest_devices') ~ ' · Staff: ' ~ states('sensor.{area_id}_employee_devices') -%}}
<ha-alert alert-type="{{{{ {_ALERTS}.get(st, 'info') }}}}" title="{title} — {{{{ label }}}}">{{{{ line }}}}</ha-alert>
{{%- if state_attr('sensor.{area_id}_state', 'data_stale') %}}
<ha-alert alert-type="warning">Controller unreachable: data as of the connection loss</ha-alert>
{{%- endif %}}
{_DEVICES.replace("@AREA@", area_id)}"""
    return _section(content)


def common_card(area_id: str) -> str:
    title = title_of(area_id)
    content = f"""\
### 🏢 {title}
Common area · Guests: {{{{ states('sensor.{area_id}_guest_devices') }}}} · Staff: {{{{ states('sensor.{area_id}_employee_devices') }}}}
{_DEVICES.replace("@AREA@", area_id)}"""
    return _section(content)


def _section(content: str) -> str:
    return f"""\
- type: grid
  cards:
    - type: markdown
      content: |
{_indent(content, 8)}
"""


def overview_card(areas: list[str]) -> str:
    sensors = ", ".join(f"'sensor.{a}_guest_devices', 'sensor.{a}_employee_devices'" for a in areas)
    content = f"""\
{{%- set ns = namespace(rows={{}}, guests=0, staff=0) -%}}
{{%- for s in [{sensors}] -%}}
{{%- set staff = s.endswith('_employee_devices') -%}}
{{%- for d in state_attr(s, 'devices') or [] -%}}
{{%- set key = d.ssid or 'No SSID' -%}}
{{%- set row = ns.rows.get(key, {{'guests': 0, 'staff': 0, 'rooms': []}}) -%}}
{{%- set room = area_name(s) or s -%}}
{{%- set ns.rows = dict(ns.rows, **{{key: {{'guests': row.guests + (0 if staff else 1), 'staff': row.staff + (1 if staff else 0), 'rooms': row.rooms + ([room] if room not in row.rooms else [])}}}}) -%}}
{{%- if staff %}}{{% set ns.staff = ns.staff + 1 %}}{{% else %}}{{% set ns.guests = ns.guests + 1 %}}{{% endif -%}}
{{%- endfor -%}}
{{%- endfor -%}}
{{%- set v = integration_entities('hotel_sense') | select('match', 'binary_sensor\\\\..*_violation$') | select('is_state', 'on') | list -%}}
## 📶 Rooms & Wi-Fi
**{{{{ ns.guests }}}}** guest devices · **{{{{ ns.staff }}}}** staff devices · fixed equipment not shown
{{% if v | count %}}<ha-alert alert-type="error" title="Possible violations: {{{{ v | count }}}}">{{{{ v | map('device_attr', 'name') | join(', ') }}}}</ha-alert>
{{% else %}}<ha-alert alert-type="success">No violations</ha-alert>
{{% endif %}}
{{%- if ns.rows %}}

| Network (SSID) | Guests | Staff | Rooms |
|:--|:-:|:-:|:--|
{{%- for ssid in ns.rows | sort %}}
| 📶 {{{{ ssid }}}} | {{{{ ns.rows[ssid].guests }}}} | {{{{ ns.rows[ssid].staff }}}} | {{{{ ns.rows[ssid].rooms | sort | join(', ') }}}} |
{{%- endfor %}}
{{%- endif %}}"""
    return f"""\
- type: grid
  column_span: 4
  cards:
    - type: markdown
      content: |
{_indent(content, 8)}
"""


def build_dashboard(rooms: list[str], common: list[str]) -> str:
    sections = (overview_card(rooms + common) + "".join(room_card(r) for r in rooms)
                + "".join(common_card(c) for c in common))
    return f"""\
# Hotel Sense - rooms & Wi-Fi. Generated by scripts/generate_wifi_dashboard.py, do not edit by hand.
# Add in HA: Settings -> Dashboards -> Add dashboard -> New dashboard from scratch ->
# (three dots) Edit -> Raw configuration editor -> paste this file.
title: Rooms & Wi-Fi
views:
  - title: Rooms & Wi-Fi
    path: wifi
    icon: mdi:wifi
    type: sections
    max_columns: 4
    sections:
{_indent(sections, 6)}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rooms", nargs="*", default=DEFAULT_ROOMS)
    parser.add_argument("--common", nargs="*", default=DEFAULT_COMMON)
    args = parser.parse_args(argv)
    sys.stdout.write(build_dashboard(args.rooms, args.common))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
