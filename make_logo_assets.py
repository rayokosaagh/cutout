"""Cut the logo artwork (assets/logo-source.png) into the layers the app animates.

    python make_logo_assets.py

Writes to assets/:
  mark-body.png      the white C as an alpha mask (recoloured to the theme ink at runtime)
  mark-flap.png      the purple-to-blue peeled flap, in colour
  wordmark-mask.png  the "cutout" letters as an alpha mask
  wordmark-flap.png  the gradient flap on the wordmark's c, in colour
  icon.png / cutout.ico   the rounded-square app icon

The mark layers are square and centred on the C's ring, so rotating them about
their centre moves the flap around the ring.
"""

from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
SOURCE = ASSETS / "logo-source.png"

# Regions of the source artwork (2172 x 724).
ICON_BOX = (1450, 100, 1962, 605)          # the rounded-square icon
RING_CENTRE = (245.8, 258.9)                # centre of the C's ring, in ICON_BOX coordinates
MARK_HALF = 160                             # half the side of the square mark crop
WORDMARK_BOX = (168, 225, 1326, 510)        # the "cutout" letters, with a margin


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def split_colour(rgb, alpha):
    """Weight of the gradient flap per pixel (1 = flap, 0 = plain body/letters)."""
    mx = rgb.max(-1)
    sat = (mx - rgb.min(-1)) / np.maximum(mx, 1)
    # Near-black pixels have noisy hue; only bright colour counts as the flap.
    return smoothstep(.12, .30, sat) * smoothstep(70, 130, mx) * (alpha > 0)


def rgba(rgb, alpha):
    out = np.dstack([np.clip(rgb, 0, 255), np.clip(alpha * 255, 0, 255)])
    return Image.fromarray(out.astype(np.uint8), "RGBA")


def mask(alpha):
    white = np.full(alpha.shape + (3,), 255.0)
    return rgba(white, alpha)


def main():
    src = Image.open(SOURCE).convert("RGBA")

    # ---- icon -------------------------------------------------------------
    icon = src.crop(ICON_BOX)
    side = max(icon.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(icon, ((side - icon.width) // 2, (side - icon.height) // 2))
    square = square.resize((512, 512), Image.LANCZOS)
    square.save(ASSETS / "icon.png")
    square.save(ASSETS / "cutout.ico", sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])

    # ---- mark: the C lifted off the dark tile -------------------------------
    cx, cy = RING_CENTRE
    box = (int(round(cx - MARK_HALF)), int(round(cy - MARK_HALF)),
           int(round(cx + MARK_HALF)), int(round(cy + MARK_HALF)))
    a = np.asarray(icon.crop(box)).astype(np.float64)
    rgb, src_a = a[..., :3], a[..., 3] / 255
    bg = np.array([18.0, 18.0, 20.0])                   # the tile colour
    # Solid from well below the body's white, so its soft shading stays opaque;
    # the tile's own noise (up to ~30) stays clear.
    alpha = np.clip((rgb.max(-1) - 34) / (200 - 34), 0, 1) * src_a
    safe = np.maximum(alpha, 1e-3)[..., None]
    colour = (rgb - bg * (1 - safe)) / safe             # un-mix the tile out of the edges
    flap = split_colour(colour, alpha)
    mask(alpha * (1 - flap)).save(ASSETS / "mark-body.png")
    rgba(colour, alpha * flap).save(ASSETS / "mark-flap.png")

    # ---- wordmark: dark letters on transparent --------------------------------
    w = np.asarray(src.crop(WORDMARK_BOX)).astype(np.float64)
    rgb, alpha = w[..., :3], w[..., 3] / 255
    flap = split_colour(rgb, alpha)
    mask(alpha * (1 - flap)).save(ASSETS / "wordmark-mask.png")
    rgba(rgb, alpha * flap).save(ASSETS / "wordmark-flap.png")
    print("assets written to", ASSETS)


if __name__ == "__main__":
    main()
