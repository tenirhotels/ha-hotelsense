#!/usr/bin/env python3
"""Generate the Hotel Sense rooms dashboard (Lovelace YAML).

    python scripts/generate_dashboard.py > dashboards/hotel_sense_rooms.yaml
    python scripts/generate_dashboard.py --lang en > dashboards/hotel_sense_rooms_en.yaml

Dashboard text is static YAML (HA does not translate it per user), so there is
one file per language: Russian (default) and English.

Rooms are given as HA area IDs (``room_01`` for an Area named "Room 01").
Check the IDs under Settings -> Areas if yours differ and pass them:

    python scripts/generate_dashboard.py --rooms room_01 room_02 --common admin_house

Only built-in cards are used (markdown + tile) in a ``sections`` view, so the
order is fixed: summary, Room 01 … Room 10 (each: state, then its status
select), then common areas. A violation is a red ``ha-alert`` box, a staff
visit in a checked-out room is blue. The status select is set automatically by
the Exely webhook; changing it by hand is the fallback.
"""
from __future__ import annotations

import argparse
import sys

DEFAULT_ROOMS = [f"room_{i:02d}" for i in range(1, 11)]
DEFAULT_COMMON = ["admin_house"]

LANGUAGES = ("ru", "en")
TEXT = {
    "ru": {
        "states": "{'empty': 'Пусто', 'violation': 'НАРУШЕНИЕ', 'staff_visit': 'Визит сотрудника', "
                  "'checked_in': 'Гость заселён'}",
        "guests": "Гости", "staff": "Сотрудники", "fixed": "Фикс.", "status": "Статус",
        "stale": "Нет связи с контроллером: данные на момент потери связи",
        "violations": "Возможные нарушения", "no_violations": "Нарушений нет",
        "misplaced": "Оборудование не на своём месте", "expected": "ожидается", "seen": "видно в",
        "view": "Номера",
    },
    "en": {
        "states": "{'empty': 'Empty', 'violation': 'VIOLATION', 'staff_visit': 'Staff visit', "
                  "'checked_in': 'Checked in'}",
        "guests": "Guests", "staff": "Staff", "fixed": "Fixed", "status": "Status",
        "stale": "Controller unreachable: data as of the connection loss",
        "violations": "Possible violations", "no_violations": "No violations",
        "misplaced": "Equipment out of place", "expected": "expected in", "seen": "seen in",
        "view": "Rooms",
    },
}
ALERT_KINDS = (
    "{'violation': 'error', 'staff_visit': 'info'}"
)


def title_of(area_id: str) -> str:
    return " ".join(p.capitalize() if not p.isdigit() else p for p in area_id.split("_"))


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in text.splitlines())


def room_card(area_id: str, lang: str = "ru") -> str:
    t = TEXT[lang]
    title = title_of(area_id)
    content = f"""\
{{%- set st = states('sensor.{area_id}_state') -%}}
{{%- set g = states('sensor.{area_id}_guest_devices') -%}}
{{%- set e = states('sensor.{area_id}_employee_devices') -%}}
{{%- set f = states('sensor.{area_id}_fixed_devices') -%}}
{{%- set label = {t["states"]}.get(st, st) -%}}
{{%- set kind = {ALERT_KINDS}.get(st) -%}}
{{%- set line = '{t["guests"]}: ' ~ g ~ ' · {t["staff"]}: ' ~ e ~ ' · {t["fixed"]}: ' ~ f -%}}
{{% if kind %}}<ha-alert alert-type="{{{{ kind }}}}" title="{title} — {{{{ label }}}}">{{{{ line }}}}</ha-alert>
{{% else %}}**{title}** — {{{{ label }}}}<br>{{{{ line }}}}
{{% endif %}}
{{%- if state_attr('sensor.{area_id}_state', 'data_stale') %}}
<ha-alert alert-type="warning">{t["stale"]}</ha-alert>
{{%- endif %}}"""
    return f"""\
- type: grid
  cards:
    - type: markdown
      content: |
{_indent(content, 8)}
    - type: tile
      entity: select.{area_id}_status
      name: {t["status"]}
      hide_state: true
      grid_options:
        columns: full
      features_position: bottom
      features:
        - type: select-options
"""


def common_card(area_id: str, lang: str = "ru") -> str:
    t = TEXT[lang]
    title = title_of(area_id)
    content = f"""\
**{title}**<br>
{t["guests"]}: {{{{ states('sensor.{area_id}_guest_devices') }}}} · {t["staff"]}: {{{{ states('sensor.{area_id}_employee_devices') }}}} · {t["fixed"]}: {{{{ states('sensor.{area_id}_fixed_devices') }}}}"""
    return f"""\
- type: grid
  cards:
    - type: markdown
      content: |
{_indent(content, 8)}
"""


_SUMMARY = """\
- type: grid
  column_span: 4
  cards:
    - type: markdown
      content: |
        {%- set v = integration_entities('hotel_sense') | select('match', 'binary_sensor\\\\..*_violation$') | select('is_state', 'on') | list -%}
        {% if v | count %}<ha-alert alert-type="error" title="@violations@: {{ v | count }}">{{ v | map('device_attr', 'name') | join(', ') }}</ha-alert>
        {% else %}<ha-alert alert-type="success">@no_violations@</ha-alert>
        {% endif %}
        {%- set m = state_attr('sensor.hotel_sense_misplaced_devices', 'devices') or [] %}
        {%- if m %}
        <ha-alert alert-type="warning" title="@misplaced@: {{ m | count }}">{% for d in m %}{{ d.name }}: @expected@ {{ d.expected }}, @seen@ {{ d.seen }}<br>{% endfor %}</ha-alert>
        {%- endif %}
"""


def summary_card(lang: str = "ru") -> str:
    text = _SUMMARY
    for key in ("violations", "no_violations", "misplaced", "expected", "seen"):
        text = text.replace(f"@{key}@", TEXT[lang][key])
    return text


def build_dashboard(rooms: list[str], common: list[str], lang: str = "ru") -> str:
    sections = (summary_card(lang) + "".join(room_card(r, lang) for r in rooms)
                + "".join(common_card(c, lang) for c in common))
    return f"""\
# Hotel Sense - rooms matrix. Generated by scripts/generate_dashboard.py, do not edit by hand.
# Add in HA: Settings -> Dashboards -> Add dashboard -> New dashboard from scratch ->
# (three dots) Edit -> Raw configuration editor -> paste this file.
title: Hotel Sense
views:
  - title: {TEXT[lang]["view"]}
    path: rooms
    icon: mdi:bed
    type: sections
    max_columns: 4
    sections:
{_indent(sections, 6)}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rooms", nargs="*", default=DEFAULT_ROOMS)
    parser.add_argument("--common", nargs="*", default=DEFAULT_COMMON)
    parser.add_argument("--lang", choices=LANGUAGES, default="ru")
    args = parser.parse_args(argv)
    sys.stdout.write(build_dashboard(args.rooms, args.common, args.lang))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
