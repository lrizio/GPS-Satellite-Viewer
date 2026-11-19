#!/usr/bin/env python3
"""Generates icon.ico -- a miniature of the app's own sky plot: horizon
circle, elevation rings and N/S/E/W crosshair, with glowing satellites in
the constellation colours and one satellite's track arc.

Same construction as the sibling apps' scripts/make_icon.py (supersample,
theme.py-palette rounded-square background, glow dots, downsample for
anti-aliasing), different imagery.

Usage (from the project root):  python scripts/make_icon.py
Requires Pillow (build-time only).
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFilter

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "icon.ico")
S = 1024

BG = (20, 24, 29, 255)            # theme.BG
PANEL_EDGE = (58, 65, 76, 255)    # theme.METAL_HI
SCREEN = (8, 13, 16, 255)         # theme.SCREEN
GRID = (70, 80, 94, 255)
HORIZON = (167, 179, 192, 255)    # theme.FG_DIM
GREEN = (87, 227, 154)            # GPS
RED = (255, 122, 107)             # GLONASS
BLUE = (108, 182, 255)            # Galileo
AMBER = (255, 191, 95)            # BeiDou


def sky_xy(cx, cy, r, az, el):
    rr = r * (90 - el) / 90
    a = math.radians(az)
    return cx + rr * math.sin(a), cy - rr * math.cos(a)


def glow(img: Image.Image, x: float, y: float, rad: float, color) -> None:
    halo = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse([x - rad * 2.1, y - rad * 2.1, x + rad * 2.1, y + rad * 2.1],
                                 fill=color + (150,))
    halo = halo.filter(ImageFilter.GaussianBlur(rad * 0.9))
    img.alpha_composite(halo)
    d = ImageDraw.Draw(img)
    d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=color + (255,),
              outline=(238, 243, 248, 255), width=max(3, S // 200))
    hr = rad * 0.35
    d.ellipse([x - rad * 0.35 - hr, y - rad * 0.35 - hr, x - rad * 0.35 + hr, y - rad * 0.35 + hr],
              fill=(255, 255, 255, 140))


def main() -> None:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rad = int(S * 0.22)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=rad, fill=BG)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=rad, outline=PANEL_EDGE, width=S // 110)

    cx = cy = S / 2
    r = S * 0.40
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=SCREEN, outline=HORIZON, width=S // 70)
    for el in (30, 60):
        rr = r * (90 - el) / 90
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=GRID, width=S // 140)
    d.line([cx - r, cy, cx + r, cy], fill=GRID, width=S // 140)
    d.line([cx, cy - r, cx, cy + r], fill=GRID, width=S // 140)

    # one satellite's pass across the sky, ending at its current position
    track = [sky_xy(cx, cy, r, az, 12 + 60 * math.sin(math.radians((az - 200) * 1.3)))
             for az in range(205, 318, 3)]
    d.line(track, fill=BLUE + (170,), width=S // 60, joint="curve")

    sats = [(track[-1], BLUE), (sky_xy(cx, cy, r, 40, 55), GREEN),
            (sky_xy(cx, cy, r, 150, 30), AMBER), (sky_xy(cx, cy, r, 250, 22), RED),
            (sky_xy(cx, cy, r, 100, 78), GREEN)]
    for (x, y), col in sats:
        glow(img, x, y, S * 0.058, col)

    sizes = [256, 128, 64, 48, 32, 24, 16]
    frames = [img.resize((s, s), Image.LANCZOS) for s in sizes]
    frames[0].save(OUT_PATH, format="ICO", sizes=[(s, s) for s in sizes], append_images=frames[1:])
    img.resize((256, 256), Image.LANCZOS).save(os.path.join(os.path.dirname(__file__), "icon_preview.png"))
    print("wrote", os.path.abspath(OUT_PATH))


if __name__ == "__main__":
    main()
