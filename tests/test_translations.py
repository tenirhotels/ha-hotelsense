"""Translation files must pass hassfest: every {placeholder} is an identifier,
and all languages share the same keys."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

DIR = Path(__file__).parent.parent / "custom_components" / "hotel_sense"
FILES = [DIR / "strings.json", *sorted((DIR / "translations").glob("*.json"))]
PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _strings(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}.{key}" if path else key)
    elif isinstance(node, str):
        yield path, node


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_placeholders_are_identifiers(path):
    for key, text in _strings(json.loads(path.read_text())):
        if text.startswith("[%key:"):
            continue
        for name in PLACEHOLDER.findall(text):
            assert IDENTIFIER.match(name), f"{path.name}: {key}: {{{name}}}"


def test_languages_have_the_same_keys():
    en = {k for k, _ in _strings(json.loads((DIR / "translations" / "en.json").read_text()))}
    ru = {k for k, _ in _strings(json.loads((DIR / "translations" / "ru.json").read_text()))}
    assert en == ru
