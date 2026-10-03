r"""Draw the application icon: a hexagon mark, written to icon.ico.

Deliberately original artwork rather than a copy of the Archipelago logo - it
borrows the hexagon idea, which is not anyone's to own, and nothing else. The
motif is three linked nodes inside a ring: islands joined by routes, which is
what an archipelago is and what this app assembles.

Pillow is needed to RUN this, but not to use the app: the result is committed,
so the icon ships as a file and the app never imports Pillow.

    python make_icon.py            # rewrites icon.ico
    python make_icon.py --png out.png
"""
from __future__ import annotations

import argparse
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "icon.ico")

BG = (28, 32, 44, 255)        # deep slate, reads on light and dark taskbars
RING = (90, 155, 234, 255)    # the app's own accent blue
NODE = (122, 209, 160, 255)   # a green that stays distinct at 16px
LINK = (90, 155, 234, 210)


def _hexagon(cx: float, cy: float, r: float) -> list:
    """Flat-top hexagon, the orientation that reads as a hexagon when tiny."""
    return [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
            for a in range(0, 360, 60)]


def draw(size: int):
    from PIL import Image, ImageDraw

    # 8x supersampling: a 16px icon drawn directly has ragged edges, and the
    # taskbar is exactly where it would show.
    scale = 8
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = s * 0.06
    d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=s * 0.22, fill=BG)

    cx = cy = s / 2
    # Below about 24px the spokes and the centre dot merge into a blob, so the
    # small sizes get a deliberately coarser drawing rather than a shrunk one:
    # a heavier ring and three larger nodes, no links. An icon that is mush at
    # 16px is mush in the taskbar, which is where it is actually seen.
    small = size <= 24
    outer = s * (0.34 if small else 0.31)
    width = max(scale, int(s * (0.085 if small else 0.055)))
    d.polygon(_hexagon(cx, cy, outer), outline=RING, width=width)

    nodes = [(cx + outer * 0.60 * math.cos(math.radians(a)),
              cy + outer * 0.60 * math.sin(math.radians(a)))
             for a in (90, 210, 330)]
    if not small:
        for x, y in nodes:
            d.line([cx, cy, x, y], fill=LINK, width=max(scale, int(s * 0.03)))
    r = s * (0.105 if small else 0.065)
    for x, y in nodes:
        d.ellipse([x - r, y - r, x + r, y + r], fill=NODE)
    if not small:
        r2 = s * 0.05
        d.ellipse([cx - r2, cy - r2, cx + r2, cy + r2], fill=RING)

    return img.resize((size, size), Image.LANCZOS)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Draw the app icon.")
    ap.add_argument("--png", help="also write a PNG preview here")
    ap.add_argument("--out", default=ICON)
    args = ap.parse_args(argv)

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        print("Pillow is needed to redraw the icon: pip install pillow",
              file=sys.stderr)
        return 1

    sizes = (256, 128, 64, 48, 32, 24, 16)
    images = [draw(n) for n in sizes]
    images[0].save(args.out, format="ICO",
                   sizes=[(n, n) for n in sizes], append_images=images[1:])
    print(f"wrote {args.out} ({os.path.getsize(args.out):,} bytes, "
          f"{len(sizes)} sizes)")
    if args.png:
        draw(256).save(args.png)
        print(f"wrote {args.png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
