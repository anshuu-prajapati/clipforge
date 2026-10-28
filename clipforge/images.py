"""Photos for carousel slides from free stock-photo libraries (licensed for commercial use and editing).

PIXABAY_API_KEY  free key, instant: https://pixabay.com/api/docs/ (sign in → the key is shown on that page)
PEXELS_API_KEY   https://www.pexels.com/api/ (new keys are paused at the moment; existing keys keep working)

Whichever key is set is used (Pixabay first). Photo ids carry their library ("pixabay:123", "pexels:456"),
only that library's image servers are ever downloaded from, and every file is re-encoded as a plain JPEG.
Pixabay asks API users to cache searches for 24 h and not to hotlink: results are cached in memory and
photos are downloaded to the server; search thumbnails are only shown temporarily, as allowed.
"""
from __future__ import annotations

import io
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

MAX_BYTES = 25 * 1024 * 1024
MAX_SIDE = 2000
CACHE_S = 24 * 3600
HOSTS = {"pixabay": {"pixabay.com", "cdn.pixabay.com"}, "pexels": {"images.pexels.com"}}
_cache: dict[tuple, tuple[float, object]] = {}


class ImageError(RuntimeError):
    pass


def provider() -> str | None:
    if os.environ.get("PIXABAY_API_KEY"):
        return "pixabay"
    if os.environ.get("PEXELS_API_KEY"):
        return "pexels"
    return None


def enabled() -> bool:
    return provider() is not None


def _cached(key: tuple, fetch):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    val = fetch()
    if len(_cache) > 2000:                                  # keep memory bounded
        _cache.clear()
    _cache[key] = (time.time(), val)
    return val


def _get(url: str, params: dict, headers: dict | None = None, name: str = "the photo library") -> dict:
    try:
        r = httpx.get(url, params=params, headers=headers, timeout=15)
    except httpx.HTTPError:
        raise ImageError(f"Couldn't reach {name} — try again.") from None
    if r.status_code == 429:
        raise ImageError("Photo search limit reached — try again in a minute.")
    if r.status_code in (400, 401, 403) and "key" in r.text.lower():
        raise ImageError(f"{name} rejected the API key — check it in the server's .env.")
    if r.status_code == 404:
        raise ImageError("That photo no longer exists.")
    if r.status_code >= 400:
        raise ImageError(f"{name} answered HTTP {r.status_code}.")
    return r.json()


# --------------------------------------------------------------------------- Pixabay
def _pixabay(params: dict) -> dict:
    return _get("https://pixabay.com/api/", {"key": os.environ["PIXABAY_API_KEY"], "image_type": "photo",
                                             "safesearch": "true", **params}, name="Pixabay")


def _pixabay_view(h: dict) -> dict:
    return {"id": f"pixabay:{h['id']}", "thumb": h.get("webformatURL") or h.get("previewURL"),
            "full": h.get("largeImageURL") or h.get("webformatURL"), "alt": h.get("tags") or "",
            "credit": f"Image by {h.get('user') or 'unknown'} from Pixabay", "page": h.get("pageURL") or "",
            "source": "pixabay"}


# --------------------------------------------------------------------------- Pexels
def _pexels(path: str, params: dict | None = None) -> dict:
    return _get("https://api.pexels.com/v1" + path, params or {},
                {"Authorization": os.environ["PEXELS_API_KEY"]}, name="Pexels")


def _pexels_view(p: dict) -> dict:
    return {"id": f"pexels:{p['id']}", "thumb": p["src"]["medium"], "full": p["src"]["large2x"],
            "alt": p.get("alt") or "", "credit": f"Photo by {p.get('photographer') or 'unknown'} on Pexels",
            "page": p.get("url") or "", "source": "pexels"}


# --------------------------------------------------------------------------- public
def search(query: str, n: int = 15, page: int = 1) -> list[dict]:
    query = " ".join(query.split())[:100]
    if not query:
        return []
    src = provider()
    if src is None:
        raise ImageError("Web photos need PIXABAY_API_KEY in the server's .env (free at pixabay.com/api/docs).")
    n, page = max(3, min(40, n)), max(1, min(50, page))
    if src == "pixabay":
        d = _cached(("pb", query.lower(), n, page), lambda: _pixabay(
            {"q": query, "per_page": n, "page": page, "orientation": "vertical"}))
        hits = [_pixabay_view(h) for h in d.get("hits", [])]
        if not hits and page == 1:                          # few portrait shots for this: allow any orientation
            d = _cached(("pb", query.lower(), n, 0), lambda: _pixabay({"q": query, "per_page": n}))
            hits = [_pixabay_view(h) for h in d.get("hits", [])]
        return hits
    d = _cached(("px", query.lower(), n, page), lambda: _pexels(
        "/search", {"query": query, "per_page": n, "page": page, "orientation": "portrait"}))
    return [_pexels_view(p) for p in d.get("photos", [])]


def photo(photo_id: str) -> dict:
    src, _, num = str(photo_id).partition(":")
    if src not in HOSTS or not num.isdigit():
        raise ImageError("Unknown photo.")
    if src == "pixabay":
        if not os.environ.get("PIXABAY_API_KEY"):
            raise ImageError("Pixabay isn't set up on this server.")
        hits = _cached(("pb-id", num), lambda: _pixabay({"id": num})).get("hits", [])
        if not hits:
            raise ImageError("That photo no longer exists.")
        return _pixabay_view(hits[0])
    if not os.environ.get("PEXELS_API_KEY"):
        raise ImageError("Pexels isn't set up on this server.")
    return _pexels_view(_cached(("px-id", num), lambda: _pexels(f"/photos/{int(num)}")))


def file_name(photo_id: str) -> str:
    src, _, num = str(photo_id).partition(":")
    return f"img_{'pb' if src == 'pixabay' else 'px'}_{int(num)}.jpg"


def save_jpeg(data: bytes, dest: Path) -> None:
    """Decode any common image format, drop metadata, cap the size, save as JPEG."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    Image.MAX_IMAGE_PIXELS = 60_000_000                     # refuse decompression bombs
    try:
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ImageError("That file isn't an image ClipForge can read (use JPG, PNG or WebP).") from None
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, "JPEG", quality=90)


def download(p: dict, dest: Path) -> None:
    u = urlparse(p.get("full") or "")
    if u.scheme != "https" or (u.hostname or "") not in HOSTS.get(p.get("source", ""), set()):
        raise ImageError("Unexpected photo address.")
    try:
        with httpx.stream("GET", p["full"], timeout=30, follow_redirects=False) as r:
            if r.status_code == 429:
                raise ImageError("The photo site is limiting downloads from this network right now — wait a few "
                                 "minutes and try again, or upload your own photo for this slide.")
            if r.status_code >= 400:
                raise ImageError(f"Couldn't download the photo (HTTP {r.status_code}).")
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    raise ImageError("That photo is too large.")
    except httpx.HTTPError:
        raise ImageError("Couldn't download the photo — try again.") from None
    save_jpeg(bytes(buf), dest)
