"""Detail enhancement for cutouts.

This is classical image processing, not AI super-resolution: it sharpens and
lifts local contrast in detail that is already there. It will make a decent
photo crisper; it cannot invent detail a soft or low-resolution source never
captured.

Applied after segmentation and alpha-aware, so edges the model produced are
not re-sharpened into halos.
"""

from PIL import Image, ImageEnhance, ImageFilter

__all__ = ["enhance_rgba", "STRENGTHS"]

# radius/percent pairs for a coarse pass and a fine pass, plus colour tweaks
STRENGTHS = {
    "subtle": dict(coarse=(2.2, 70), fine=(0.8, 45),
                   contrast=1.02, colour=1.02),
    "normal": dict(coarse=(2.0, 115), fine=(0.7, 70),
                   contrast=1.05, colour=1.05),
    "strong": dict(coarse=(1.8, 175), fine=(0.6, 105),
                   contrast=1.09, colour=1.08),
}


def enhance_rgba(img, strength="normal", upscale=1):
    """Sharpen detail while leaving the alpha channel untouched.

    The alpha is carried through unmodified. Sharpening a matte would crunch
    the soft edge the model worked to produce, which looks worse, not better.
    """
    settings = STRENGTHS.get(strength, STRENGTHS["normal"])
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    alpha = img.getchannel("A")
    rgb = img.convert("RGB")

    if upscale and upscale > 1:
        size = (rgb.width * upscale, rgb.height * upscale)
        rgb = rgb.resize(size, Image.LANCZOS)
        alpha = alpha.resize(size, Image.LANCZOS)

    radius, percent = settings["coarse"]
    rgb = rgb.filter(ImageFilter.UnsharpMask(
        radius=radius, percent=percent, threshold=3))

    radius, percent = settings["fine"]
    rgb = rgb.filter(ImageFilter.UnsharpMask(
        radius=radius, percent=percent, threshold=2))

    if settings["contrast"] != 1.0:
        rgb = ImageEnhance.Contrast(rgb).enhance(settings["contrast"])
    if settings["colour"] != 1.0:
        rgb = ImageEnhance.Color(rgb).enhance(settings["colour"])

    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out
