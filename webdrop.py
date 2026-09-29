"""Accept images dragged out of a browser.

A browser does not hand over a file on drop. Depending on the browser and
what you grabbed, the payload is one of:

  * a plain image URL                     -> fetch it
  * an HTML fragment wrapping an <img>    -> pull the src, then fetch
  * a data: URI with the bytes inline     -> decode, no network
  * an ordinary file path                 -> the browser cached it already

This module turns any of those into a real file on disk so the rest of the
app can treat it like any other source image.
"""

import base64
import binascii
import hashlib
import mimetypes
import re
import urllib.parse
import urllib.request
from pathlib import Path

__all__ = ["looks_like_web_drop", "extract_sources", "fetch"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

EXT_FOR = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/avif": ".avif",
    "image/bmp": ".bmp", "image/tiff": ".tiff", "image/x-icon": ".ico",
    "image/svg+xml": ".svg", "image/heic": ".heic",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp",
              ".tif", ".tiff", ".ico", ".svg", ".heic", ".jfif"}

_SRCSET_SPLIT = re.compile(r"\s+\d+[wx]\s*$")

# Windows CF_HTML wraps the markup in a header block, so the payload does not
# start with '<'. The header also carries SourceURL, which is the correct
# Referer for fetching the image.
_CF_HTML_KEY = re.compile(
    r"^(Version|StartHTML|EndHTML|StartFragment|EndFragment|StartSelection|"
    r"EndSelection|SourceURL):", re.I)


def split_cf_html(text):
    """Return (markup, source_url) for a CF_HTML payload, else (text, None)."""
    if not text or "StartHTML:" not in text[:400]:
        return text, None
    source_url = None
    body = []
    for line in text.splitlines():
        if _CF_HTML_KEY.match(line.strip()):
            if line.strip().lower().startswith("sourceurl:"):
                source_url = line.split(":", 1)[1].strip() or None
            continue
        body.append(line)
    return "\n".join(body).strip(), source_url


def looks_like_web_drop(text):
    t = (text or "").strip()
    if t.startswith(("http://", "https://", "data:image/", "<")):
        return True
    # CF_HTML, or a uri-list with comment lines
    if "StartHTML:" in t[:400] or "SourceURL:" in t[:400]:
        return True
    return bool(re.search(r"https?://\S+\.(?:png|jpe?g|gif|webp|avif|bmp|svg)",
                          t, re.I))


def _from_html(fragment):
    """Pull image sources out of an HTML fragment, best first."""
    found = []
    for match in re.finditer(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""",
                             fragment, re.I):
        found.append(match.group(1))
    if not found:
        for match in re.finditer(r"""\bsrcset\s*=\s*["']([^"']+)["']""",
                                 fragment, re.I):
            # Take the largest candidate in the srcset.
            parts = [p.strip() for p in match.group(1).split(",") if p.strip()]
            if parts:
                found.append(_SRCSET_SPLIT.sub("", parts[-1]).strip())
    if not found:
        for match in re.finditer(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["']""",
                                 fragment, re.I):
            found.append(match.group(1))
    return found


def extract_sources(payload):
    """Return (sources, referer) found in a dropped payload."""
    text = (payload or "").strip()
    if not text:
        return [], None

    text, referer = split_cf_html(text)

    if "<" in text[:200]:
        candidates = _from_html(text)
        if candidates:
            sources, inner_ref = _resolve(candidates, referer)
            return sources, (referer or inner_ref)

    # uri-list and plain drops: one per line, '#' lines are comments.
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    urls = [ln for ln in lines
            if ln.startswith(("http://", "https://", "data:image/"))]
    if not urls:
        urls = re.findall(r"https?://[^\s\"'<>]+", text)
    if not urls:
        return [], referer

    sources, inner_ref = _resolve(urls, referer)
    return sources, (inner_ref or referer)


# Search engines hand over a wrapper page, not the picture. The real image
# URL rides in a query parameter, and the page it came from rides alongside -
# which is the Referer those hosts want to see.
_IMAGE_PARAMS = ("imgurl", "mediaurl", "image_url", "imgurlreferer", "murl")
_LOOSE_PARAMS = ("u", "url", "q", "img")
_REFERER_PARAMS = ("imgrefurl", "ru", "referrer", "referer", "ref")
_WRAPPER_HOSTS = ("google.", "bing.com", "duckduckgo.com", "yandex.",
                  "search.brave.com", "ecosia.org", "startpage.com",
                  "pinterest.", "lens.google")


def unwrap(url):
    """If `url` is a search-engine wrapper, dig out (image_url, referer)."""
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        return None, None
    if not parts.query:
        return None, None

    qs = urllib.parse.parse_qs(parts.query)
    host = (parts.netloc or "").lower()

    referer = None
    for key in _REFERER_PARAMS:
        for value in qs.get(key, []):
            if value.startswith(("http://", "https://")):
                referer = value
                break
        if referer:
            break

    keys = list(_IMAGE_PARAMS)
    # Generic names are only safe on hosts we know act as redirectors.
    if any(marker in host for marker in _WRAPPER_HOSTS):
        keys += list(_LOOSE_PARAMS)

    for key in keys:
        for value in qs.get(key, []):
            if value.startswith(("http://", "https://")):
                return value, referer
    return None, referer


def _resolve(candidates, base):
    """Absolutise relative srcs, then unwrap any search-engine redirectors."""
    out, referer = [], None
    for c in candidates:
        c = c.strip()
        if not c:
            continue
        if c.startswith("//"):
            c = "https:" + c
        elif c.startswith("/") and base:
            c = urllib.parse.urljoin(base, c)

        inner, inner_ref = unwrap(c)
        if inner_ref and not referer:
            referer = inner_ref
        if inner:
            out.append(inner)
            # Keep the wrapper too, in case the inner URL is dead.
            if c not in out:
                out.append(c)
        else:
            out.append(c)
    return out, referer


def _name_from(url, content_type, data):
    if url.startswith("data:"):
        stem = "pasted-" + hashlib.sha1(data).hexdigest()[:10]
    else:
        path = urllib.parse.urlparse(url).path
        stem = Path(urllib.parse.unquote(path)).stem or "image"
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem)[:60].strip("-.") or "image"

    ext = ""
    if content_type:
        ext = EXT_FOR.get(content_type.split(";")[0].strip().lower(), "")
    if not ext and not url.startswith("data:"):
        suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
        if suffix in IMAGE_EXTS:
            ext = suffix
    if not ext:
        ext = mimetypes.guess_extension(content_type or "") or ".img"
    return stem + ext


def _get(url, referer, timeout):
    """Fetch a URL, trying a few header shapes before giving up.

    Some image hosts reject anything that doesn't look like a page load from
    their own site; others reject requests that carry a Referer at all. Try
    the polite variants in order rather than assuming one works everywhere.
    """
    parts = urllib.parse.urlparse(url)
    origin = f"{parts.scheme}://{parts.netloc}/" if parts.netloc else None

    common = {
        "User-Agent": UA,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }

    attempts = []
    if referer:
        attempts.append({**common, "Referer": referer})
    if origin and origin != referer:
        attempts.append({**common, "Referer": origin,
                         "Sec-Fetch-Site": "same-origin"})
    attempts.append(common)                      # no Referer at all
    attempts.append({"User-Agent": UA})          # bare minimum

    last = None
    for headers in attempts:
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(), response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (401, 403, 405, 406, 429):
                break      # not an access problem; more headers won't help
        except Exception as exc:
            last = exc
            break
    raise last if last else ValueError("fetch failed")


def fetch(source, dest_dir, referer=None, timeout=30):
    """Materialise one URL or data URI into dest_dir. Returns a Path.

    Raises ValueError if the payload is not actually an image, so a dragged
    link to a web page fails loudly instead of queueing an HTML file.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if source.startswith("data:"):
        header, _, encoded = source.partition(",")
        content_type = header[5:].split(";")[0] or "image/png"
        try:
            data = (base64.b64decode(encoded) if ";base64" in header
                    else urllib.parse.unquote_to_bytes(encoded))
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"bad data URI: {exc}") from None
    else:
        data, content_type = _get(source, referer, timeout)

    if not data:
        raise ValueError("empty response")

    kind = (content_type or "").split(";")[0].strip().lower()
    if kind and not kind.startswith("image/"):
        raise ValueError(f"not an image ({kind or 'unknown type'})")

    # Trust the bytes over the header - servers mislabel constantly.
    if not kind:
        if data[:3] == b"\xff\xd8\xff":
            content_type = "image/jpeg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            content_type = "image/png"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            content_type = "image/gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            content_type = "image/webp"
        elif data.lstrip()[:5].lower() == b"<?xml" or b"<svg" in data[:200].lower():
            content_type = "image/svg+xml"
        else:
            raise ValueError("unrecognised image data")

    name = _name_from(source, content_type, data)
    target = dest_dir / name
    n = 1
    while target.exists():
        target = dest_dir / f"{Path(name).stem}_{n}{Path(name).suffix}"
        n += 1

    target.write_bytes(data)
    return target
