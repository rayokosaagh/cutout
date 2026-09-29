"""Cutout's engine basics: the model list, where models live, loaded model
sessions, which image formats open, and small file helpers.

Segmentation runs locally; a loaded model stays resident between runs.
"""

import inspect
import os
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

import webdrop  # noqa: F401  (the app reaches browser-drop parsing through here)

try:
    from send2trash import send2trash  # noqa: F401
    HAS_TRASH = True
except ImportError:
    HAS_TRASH = False

try:
    # HEIC / HEIF (iPhone photos) through libheif.
    import pillow_heif
    pillow_heif.register_heif_opener()
    HAS_HEIF = True
except ImportError:
    HAS_HEIF = False

# (rembg model name, label, note)
MODELS = [
    ("isnet-general-use", "General", "balanced, 170 MB"),
    ("u2net", "Classic", "fastest, 170 MB"),
    ("u2net_human_seg", "People", "portraits, 170 MB"),
    ("birefnet-general", "Highest quality", "slow, 928 MB"),
    ("isnet-anime", "Illustration", "game art, 170 MB"),
]

HERE = Path(__file__).resolve().parent
HOME = Path(os.path.expanduser("~"))
MODEL_CACHE = HOME / ".u2net"
INTER = HERE / "fonts" / "Inter-Variable.ttf"
# Optional local converter for camera RAW, SVG and JPEG XR (CUTOUT_TOPNG overrides its
# path); those formats are only offered where it is installed.
TOPNG = Path(os.environ.get("CUTOUT_TOPNG") or HOME / "Tools" / "topng" / "topng.ps1")

PIL_NATIVE = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".bmp", ".webp", ".tif", ".tiff",
              ".gif", ".avif", ".ico"} | ({".heic", ".heif"} if HAS_HEIF else set())
CONVERTIBLE = {".svg", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf",
               ".srw", ".pef", ".jxr"} if TOPNG.exists() else set()

_sessions = {}


def is_downloaded(model_name):
    if model_name in _sessions:
        return True
    return (MODEL_CACHE / f"{model_name}.onnx").exists()


def model_url(model_name):
    """Download address for a model rembg knows but model_store doesn't list."""
    try:
        from rembg.sessions import sessions_class
        for cls in sessions_class:
            try:
                if cls.name() != model_name:
                    continue
            except Exception:
                continue
            found = re.findall(r"https?://[^\s'\"\)]+", inspect.getsource(cls))
            if found:
                return found[0]
    except Exception:
        pass
    return None


def get_session(model_name):
    if model_name not in _sessions:
        from rembg import new_session
        _sessions[model_name] = new_session(model_name)
    return _sessions[model_name]


def ensure_openable(path, staging):
    """A path Pillow can open: the file itself, or a PNG converted into `staging`."""
    ext = path.suffix.lower()
    if ext in PIL_NATIVE:
        return path
    if ext not in CONVERTIBLE:
        raise ValueError(f"unsupported format: {ext}")
    out = Path(staging) / (path.stem + ".png")
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(TOPNG), str(path), "-OutDir", staging, "-Overwrite"],
        capture_output=True, timeout=180,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if not out.exists():
        raise ValueError("conversion failed")
    return out


def unique(path):
    """`path`, or name_1, name_2, ... if it already exists."""
    if not path.exists():
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def checkerboard(size, square=11, dark=False):
    """Transparency backdrop. Goes dark-on-dark so it doesn't glare."""
    light_sq, dark_sq = ((58, 60, 63), (44, 46, 49)) if dark else \
                        ((252, 252, 253), (225, 229, 237))
    w, h = size
    img = Image.new("RGB", size, light_sq)
    d = ImageDraw.Draw(img)
    for y in range(0, h, square):
        for x in range(0, w, square):
            if (x // square + y // square) % 2:
                d.rectangle([x, y, x + square - 1, y + square - 1], fill=dark_sq)
    return img
