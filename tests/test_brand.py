"""Brand icon shipped with the integration (HACS 'brands' check)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

BRAND = Path(__file__).resolve().parent.parent / "custom_components" / "hotel_sense" / "brand"


def test_brand_icons_are_square_transparent_png():
    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        with Image.open(BRAND / name) as img:
            assert img.format == "PNG", name
            assert img.size == (size, size), name
            assert img.mode == "RGBA", name
            assert img.getpixel((0, 0))[3] == 0, name  # transparent background
