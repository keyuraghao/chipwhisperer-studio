#!/usr/bin/env python3
"""Draw the ChipWhisperer Studio application icon from the logo used in the UI.

The logo is the 32 x 32 SVG in static/index.html: a rounded square with a teal to blue diagonal gradient and a white waveform. This script draws it with Pillow (supersampled for smooth edges) and writes:

- packaging/icon.ico: Windows executable icon, 16 to 256 px
- packaging/icon.icns: macOS icon
- packaging/icon.png and src/cwstudio/resources/icon.png: 512 px PNG (Linux desktop entries made by ``cw-studio --install-desktop``)
- docs/wiki/images/logo.png: the same PNG, shown at the top of the README and the wiki

    python tools/make_icon.py
"""
from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "packaging")
TOP_LEFT, BOTTOM_RIGHT = (0x2F, 0xBF, 0x9B), (0x2A, 0x7F, 0xD6)
POINTS = [(4, 16), (9, 16), (12, 7), (16, 25), (20, 11), (23, 19), (25, 16), (28, 16)]
RADIUS, STROKE = 7, 2.6


def draw(size: int) -> Image.Image:
    """The logo at size x size pixels, drawn 8 times larger and scaled down for anti-aliasing."""
    ss = 8
    big = size * ss
    k = big / 32
    # Diagonal gradient (x1=0, y1=0 to x2=1, y2=1): the colour depends on (x + y)
    yy, xx = np.mgrid[0:big, 0:big]
    t = ((xx + yy) / (2 * (big - 1)))[..., None]
    grad = Image.fromarray((np.array(TOP_LEFT) * (1 - t) + np.array(BOTTOM_RIGHT) * t).round().astype(np.uint8), "RGB")
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=round(RADIUS * k), fill=255)
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    pts = [(x * k, y * k) for x, y in POINTS]
    w = round(STROKE * k)
    d.line(pts, fill="white", width=w, joint="curve")
    for x, y in (pts[0], pts[-1]):  # round line caps
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill="white")
    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    master = draw(1024)
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    # Each ICO size is drawn on its own, so small sizes stay crisp instead of being one big image scaled down by Windows.
    frames = [draw(s) for s in sizes]
    frames[-1].save(os.path.join(OUT, "icon.ico"), format="ICO", sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    master.save(os.path.join(OUT, "icon.icns"), format="ICNS")
    png = master.resize((512, 512), Image.LANCZOS)
    png.save(os.path.join(OUT, "icon.png"), optimize=True)
    png.save(os.path.join(ROOT, "src", "cwstudio", "resources", "icon.png"), optimize=True)
    png.save(os.path.join(ROOT, "docs", "wiki", "images", "logo.png"), optimize=True)
    for name in ("icon.ico", "icon.icns", "icon.png"):
        print(f"wrote packaging/{name} ({os.path.getsize(os.path.join(OUT, name)) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
