"""Cloudinary storage for finished clips and covers.

CLOUDINARY_URL=cloudinary://API_KEY:API_SECRET@CLOUD_NAME
  or CLOUDINARY_CLOUD_NAME + CLOUDINARY_API_KEY + CLOUDINARY_API_SECRET.
Files are stored under clipforge/<user>/<job>/<name>; re-renders overwrite the same public id.
Without credentials everything keeps working from local disk.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path


def enabled() -> bool:
    return bool(os.environ.get("CLOUDINARY_URL") or all(
        os.environ.get(k) for k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")))


@functools.cache
def _configured() -> None:
    import cloudinary
    if os.environ.get("CLOUDINARY_URL"):
        cloudinary.config(secure=True)              # the SDK reads CLOUDINARY_URL itself
    else:
        cloudinary.config(cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"], api_key=os.environ["CLOUDINARY_API_KEY"],
                          api_secret=os.environ["CLOUDINARY_API_SECRET"], secure=True)


def upload(path: Path, folder: str, kind: str) -> dict:
    """Upload (or overwrite) one file privately; kind = 'video' or 'image'.

    Files use Cloudinary's "authenticated" delivery type: they can only be fetched through URLs
    signed with the API secret, which the server hands out to the owning user (and to Instagram
    when publishing). Returns {url (signed), public_id, version, bytes, type}."""
    import cloudinary.uploader
    _configured()
    public_id = f"{folder}/{path.stem}"
    opts = dict(public_id=public_id, resource_type=kind, type="authenticated", overwrite=True, invalidate=True,
                unique_filename=False)
    if kind == "video":
        r = cloudinary.uploader.upload_large(str(path), chunk_size=20 * 1024 * 1024, **opts)
    else:
        r = cloudinary.uploader.upload(str(path), **opts)
    fmt = r.get("format") or path.suffix.lstrip(".")
    return {"url": signed_url(r["public_id"], kind, r.get("version"), fmt), "public_id": r["public_id"],
            "version": r.get("version"), "bytes": r.get("bytes"), "type": "authenticated", "format": fmt}


def signed_url(public_id: str, kind: str, version=None, fmt: str = "", download: bool = False,
               delivery: str = "authenticated") -> str:
    """Signed delivery URL for a private file (download=True → browser saves it instead of playing)."""
    from cloudinary.utils import cloudinary_url
    _configured()
    opts = dict(resource_type=kind, type=delivery, sign_url=delivery == "authenticated", secure=True, format=fmt or None)
    if version:
        opts["version"] = version
    if download:
        opts["flags"] = "attachment"
    return cloudinary_url(public_id, **opts)[0]


def delete_folder(folder: str) -> None:
    import cloudinary.api
    _configured()
    for kind in ("video", "image"):
        for delivery in ("authenticated", "upload"):       # private files, and public ones from older versions
            try:
                cloudinary.api.delete_resources_by_prefix(folder + "/", resource_type=kind, type=delivery)
            except Exception:  # noqa: BLE001 - nothing stored of that kind
                pass


def check() -> str:
    """Credential check for `clipforge doctor`-style status; returns the cloud name."""
    import cloudinary
    import cloudinary.api
    _configured()
    cloudinary.api.ping()
    return cloudinary.config().cloud_name
