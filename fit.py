"""Normalise cutouts to a fixed square, the way product cards want them.

A plain resize keeps whatever framing the original photo had, so a grid of
cards ends up with subjects at wildly different scales. This trims to the
subject first, then scales it into a consistent padded square - so every
card reads the same size regardless of how the source was shot.
"""

from PIL import Image

__all__ = ["fit_square", "fit_aspect", "crop_to", "scale_long_edge", "flatten", "SIZES"]

SIZES = [512, 800, 1000, 1200, 1600, 2000]


def subject_box(img):
    """Bounding box of the visible subject, from the alpha channel."""
    if img.mode != "RGBA":
        return img.getbbox()
    box = img.getchannel("A").getbbox()
    return box or img.getbbox()


def fit_aspect(img, ratio, long_edge=None, padding=0.06, background=None,
               allow_upscale=True):
    """Centre the subject on a canvas whose width : height is `ratio`.

    long_edge    the canvas's longer side in px; None sizes the canvas to the
                 subject at full resolution (plus padding)
    padding, background, allow_upscale   as for fit_square
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    k = 1.0 - 2.0 * padding
    box = subject_box(img)
    subject = img.crop(box) if box else None

    if long_edge:
        width, height = (long_edge, long_edge / ratio) if ratio >= 1 else (long_edge * ratio, long_edge)
    elif subject is not None:
        width = max(subject.width / k, subject.height / k * ratio)
        height = width / ratio
    else:
        width, height = (800, 800 / ratio) if ratio >= 1 else (800 * ratio, 800)
    width, height = max(int(round(width)), 1), max(int(round(height)), 1)

    fill = (0, 0, 0, 0) if background is None else (
        background if len(background) == 4 else tuple(background) + (255,))
    canvas = Image.new("RGBA", (width, height), fill)
    if subject is None:
        # Nothing visible - hand back an empty canvas rather than crashing.
        return canvas

    scale = min(width * k / subject.width, height * k / subject.height)
    if not allow_upscale:
        scale = min(scale, 1.0)
    new_w = max(int(round(subject.width * scale)), 1)
    new_h = max(int(round(subject.height * scale)), 1)
    if (new_w, new_h) != subject.size:
        subject = subject.resize((new_w, new_h), Image.LANCZOS if scale < 1 else Image.BICUBIC)
    canvas.alpha_composite(subject, ((width - new_w) // 2, (height - new_h) // 2))
    return canvas


def crop_to(img, box):
    """Crop to `box` = (x0, y0, x1, y1) given as fractions of the image size."""
    x0, y0, x1, y1 = box
    w, h = img.size
    left, top = int(round(x0 * w)), int(round(y0 * h))
    right, bottom = max(int(round(x1 * w)), left + 1), max(int(round(y1 * h)), top + 1)
    return img.crop((left, top, min(right, w), min(bottom, h)))


def scale_long_edge(img, long_edge):
    """Resize so the longer side is `long_edge` px, keeping the aspect ratio."""
    scale = long_edge / max(img.size)
    if abs(scale - 1) < 1e-6:
        return img
    size = (max(int(round(img.width * scale)), 1), max(int(round(img.height * scale)), 1))
    return img.resize(size, Image.LANCZOS if scale < 1 else Image.BICUBIC)


def flatten(img, colour):
    """Lay an RGBA image over a solid (R, G, B) colour."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    canvas = Image.new("RGBA", img.size, tuple(colour[:3]) + (255,))
    canvas.alpha_composite(img)
    return canvas


def fit_square(img, size=800, padding=0.06, background=None,
               allow_upscale=True):
    """Centre the subject on a `size` x `size` canvas.

    padding      fraction of the canvas left as margin on each side
    background   None for transparency, or an (R, G, B) / (R, G, B, A) tuple
    allow_upscale  False keeps small subjects at native scale rather than
                   enlarging them into softness
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    box = subject_box(img)
    if not box:
        # Nothing visible - hand back an empty canvas rather than crashing.
        fill = background or (0, 0, 0, 0)
        return Image.new("RGBA", (size, size),
                         fill if len(fill) == 4 else fill + (255,))

    subject = img.crop(box)

    inner = max(int(round(size * (1.0 - 2.0 * padding))), 1)
    scale = min(inner / subject.width, inner / subject.height)
    if not allow_upscale:
        scale = min(scale, 1.0)

    new_w = max(int(round(subject.width * scale)), 1)
    new_h = max(int(round(subject.height * scale)), 1)
    resample = Image.LANCZOS if scale < 1 else Image.BICUBIC
    subject = subject.resize((new_w, new_h), resample)

    if background is None:
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    else:
        fill = background if len(background) == 4 else background + (255,)
        canvas = Image.new("RGBA", (size, size), fill)

    canvas.alpha_composite(subject,
                           ((size - new_w) // 2, (size - new_h) // 2))
    return canvas
