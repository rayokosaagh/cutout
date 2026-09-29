"""Model files on disk: download, resume, check for updates, delete.

Models live in ~/.u2net (rembg's cache), one <name>.onnx each. Alongside them,
cutout-models.json records what each download was (size, ETag, Last-Modified),
so a later check can tell an unchanged file from an updated one without
downloading it: a one-byte ranged request returns the remote size and ETag.

Updates replace the file in place, so rembg's own pinned checksums would reject
them; the app turns those off (MODEL_CHECKSUM_DISABLED) and relies on the size
check here instead.
"""

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone

import cutout_core as core

BASE = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/"

# Download locations, as pinned by rembg 2.0.x.
URLS = {
    "isnet-general-use": BASE + "isnet-general-use.onnx",
    "u2net": BASE + "u2net.onnx",
    "u2net_human_seg": BASE + "u2net_human_seg.onnx",
    "birefnet-general": BASE + "BiRefNet-general-epoch_244.onnx",
    "isnet-anime": BASE + "isnet-anime.onnx",
}

MANIFEST = core.MODEL_CACHE / "cutout-models.json"
_lock = threading.Lock()


class Cancelled(Exception):
    pass


def url(name):
    return URLS.get(name) or core.model_url(name)


def path(name):
    return core.MODEL_CACHE / f"{name}.onnx"


def local_size(name):
    try:
        return path(name).stat().st_size
    except OSError:
        return None


def is_downloaded(name):
    return path(name).exists()


def _read():
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _record(name, info):
    with _lock:
        data = _read()
        if info is None:
            data.pop(name, None)
        else:
            data[name] = info
        core.MODEL_CACHE.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _open(name, start=0, timeout=30):
    request = urllib.request.Request(url(name), headers={"User-Agent": "Cutout"})
    if start is not None:
        request.add_header("Range", f"bytes={start}-")
    return urllib.request.urlopen(request, timeout=timeout)


def _total(response, start):
    """Full file size from a (possibly ranged) response."""
    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range:
        return int(content_range.rsplit("/", 1)[1])
    return int(response.headers.get("Content-Length", 0)) + (start if response.status == 206 else 0)


def probe(name):
    """What the server has now: {size, etag, modified}. Raises when offline."""
    request = urllib.request.Request(url(name), headers={"User-Agent": "Cutout", "Range": "bytes=0-0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return {"size": _total(response, 0),
                "etag": response.headers.get("ETag", ""),
                "modified": response.headers.get("Last-Modified", "")}


def download(name, report, cancel=None):
    """Download (or resume) a model; report(got, total, mb_per_s). Replaces any existing
    file only once the new one is complete."""
    if not url(name):
        raise RuntimeError("no download address for this model")
    core.MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    part = path(name).with_suffix(".onnx.part")
    start = part.stat().st_size if part.exists() else 0
    response = _open(name, start)
    with response:
        if start and response.status != 206:        # server ignored the range: start over
            start = 0
        total = _total(response, start)
        etag = response.headers.get("ETag", "")
        modified = response.headers.get("Last-Modified", "")
        got, began = start, time.time()
        with open(part, "ab" if start else "wb") as handle:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                got += len(chunk)
                report(got, total, (got - start) / max(time.time() - began, 0.1) / 1048576)
    if total and got != total:
        raise RuntimeError(f"download incomplete ({got} of {total} bytes)")
    core._sessions.pop(name, None)                    # a loaded copy would be stale
    os.replace(part, path(name))
    _record(name, {"size": got, "etag": etag, "modified": modified,
                   "downloaded": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return True


def check(name):
    """Compare the local file with the server's. Returns one of:
    'missing', 'current', 'update', 'damaged'. Raises when the server can't be reached."""
    size = local_size(name)
    if size is None:
        return "missing"
    remote = probe(name)
    record = _read().get(name)
    if record:
        if size != record.get("size"):
            return "damaged"
        changed = (remote["etag"] and record.get("etag") and remote["etag"] != record["etag"]) \
            or remote["size"] != record.get("size")
        return "update" if changed else "current"
    # Downloaded before this record existed: same size means the same file.
    if size == remote["size"]:
        _record(name, {"size": size, "etag": remote["etag"], "modified": remote["modified"],
                       "downloaded": None})
        return "current"
    return "update"


def delete(name):
    core._sessions.pop(name, None)
    for p in (path(name), path(name).with_suffix(".onnx.part")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    _record(name, None)


def disk_usage():
    return sum(local_size(name) or 0 for name, _label, _note in core.MODELS)


def human(size):
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.1f} GB"
    return f"{size / 1024 ** 2:.0f} MB"
