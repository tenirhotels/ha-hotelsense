"""Draw the Hotel Sense brand icon (building + Wi-Fi waves).

Writes custom_components/hotel_sense/brand/icon.png (256x256) and
icon@2x.png (512x512): square, transparent background, as Home Assistant
and HACS expect. Run: python scripts/generate_brand.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "custom_components" / "hotel_sense" / "brand"
BLUE = (3, 169, 244, 255)   # Home Assistant blue
DARK = (13, 71, 161, 255)
WHITE = (255, 255, 255, 255)
S = 2048  # draw large, downscale for smooth edges


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = S / 32

    # Wi-Fi waves above the roof.
    cx, cy = 16 * u, 12.5 * u
    for i, r in enumerate((11.5, 8.5, 5.5)):
        d.arc((cx - r * u, cy - r * u, cx + r * u, cy + r * u), 225, 315,
              fill=BLUE, width=int(1.6 * u))
    d.ellipse((cx - 1.4 * u, cy - 1.4 * u - 0.3 * u, cx + 1.4 * u, cy + 1.4 * u - 0.3 * u), fill=BLUE)

    # Building.
    d.rounded_rectangle((7 * u, 15 * u, 25 * u, 31 * u), radius=int(1.2 * u), fill=DARK)
    # Windows: 3 floors x 3, the middle one "occupied" (lit).
    for row in range(3):
        for col in range(3):
            x = 9.5 * u + col * 5 * u
            y = 17.2 * u + row * 3.6 * u
            lit = (row, col) == (1, 1)
            d.rounded_rectangle((x, y, x + 3 * u, y + 2.3 * u), radius=int(0.4 * u),
                                fill=BLUE if lit else WHITE)
    # Door.
    d.rounded_rectangle((14 * u, 27.6 * u, 18 * u, 31 * u), radius=int(0.5 * u), fill=WHITE)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    img = draw()
    img = img.crop(img.getbbox())  # trimmed, then centred on a square canvas
    side = max(img.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        square.resize((size, size), Image.LANCZOS).save(OUT / name, optimize=True)
        print(OUT / name)


if __name__ == "__main__":
    main()
