"""HTTP API (FastAPI) + the web dashboard at /.

Accounts: email + password (MongoDB), session cookie. Every project belongs to a user.
Data:     MongoDB — users, sessions, jobs (progress, clips manifest, post copy, publish status),
          schedule. Finished clips + covers go to Cloudinary when configured.
Work dir: CLIPFORGE_DATA (default ./clipforge_jobs) keeps sources and renders for processing.

POST /auth/signup | /auth/login | /auth/logout      GET /auth/me | /auth/config
POST /jobs                       multipart file=… or form url=…, length, max_clips
GET  /jobs                       your projects, newest first
GET  /jobs/{id}                  status + per-step progress
DELETE /jobs/{id}                delete a project (database, cloud files, local files)
GET  /jobs/{id}/clips            clips manifest (scores, hooks, caption lines, cloud URLs)
GET  /jobs/{id}/files/{name}     local file (Range requests; ?download=1 → save-as)
GET  /jobs/{id}/zip?ids=a,b      several clips as one .zip
POST /jobs/{id}/clips/{clip_id}  JSON {hook?, trim?, lines?, template?} → re-render
GET/PUT /brand, POST /brand/preview, POST /brand/asset   your brand style
/publish/…, /jobs/{id}/publish   YouTube / Instagram (owner account only)

Jobs interrupted by a restart are resumed on startup; finished steps are cached on disk.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import auth, storage
from .db import DatabaseError, db, public
from .hooks import parse_length
from .media import ClipforgeError
from .pipeline import rerender, run_pipeline
from .template import BrandTemplate

ROOT = Path(os.environ.get("CLIPFORGE_DATA", "clipforge_jobs")).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).parent / "static"
GPU = threading.Lock()          # one pipeline at a time: whisper + ffmpeg already use the whole machine
LIST_FIELDS = {"manifest": 0, "social": 0, "publish": 0, "input": 0}
MAX_UPLOAD_MB = int(os.environ.get("CLIPFORGE_MAX_UPLOAD_MB", "4096"))
app = FastAPI(title="clipforge", docs_url=None, redoc_url=None, openapi_url=None)   # no public API explorer

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: blob: https://res.cloudinary.com; "
       "media-src 'self' blob: https://res.cloudinary.com; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'self'; object-src 'none'; form-action 'self'")


def _origin(url: str) -> str:
    m = re.match(r"^(https?://[^/]+)", url.strip())
    return m[1].lower() if m else ""


@app.middleware("http")
async def security(request: Request, call_next):
    """Same-origin check for state-changing requests (CSRF) + browser security headers."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        allowed = {_origin(str(request.base_url)), _origin(os.environ.get("PUBLIC_URL", ""))} - {""}
        if origin and origin.lower() not in allowed:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Request blocked: it came from another website."}, status_code=403)
    response = await call_next(request)
    h = response.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.url.scheme == "https" or os.environ.get("PUBLIC_URL", "").startswith("https://"):
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith(("/auth/", "/settings", "/accounts")):
        h.setdefault("Cache-Control", "no-store")
    return response


# =========================================================================== auth
def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def current_user(request: Request) -> dict:
    try:
        user = auth.user_for(request.cookies.get(auth.COOKIE), _ip(request))
    except DatabaseError as e:
        raise HTTPException(503, str(e))
    if not user:
        raise HTTPException(401, "Please log in.")
    return user


User = Annotated[dict, Depends(current_user)]


class Credentials(BaseModel):
    email: str
    password: str
    name: str = ""
    code: str = ""
    remember: bool = True


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or os.environ.get("PUBLIC_URL", "").startswith("https://")


def _login_response(user: dict, request: Request, response: Response, remember: bool = True) -> dict:
    token = auth.new_session(user["_id"], remember, _ip(request), request.headers.get("user-agent", ""))
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400 if remember else None,
                        httponly=True, samesite="lax", path="/", secure=_https(request))
    return auth.safe_user(user)


@app.get("/auth/config")
def auth_config():
    try:
        first = db().users.estimated_document_count() == 0
    except DatabaseError as e:
        return {"database": False, "error": str(e), "cloud": storage.enabled()}
    from . import mailer
    return {"database": True, "first_user": first, "signup": auth.signup_mode(), "cloud": storage.enabled(),
            "email": mailer.configured()}


@app.post("/auth/signup")
def signup(c: Credentials, request: Request, response: Response):
    try:
        user = auth.create_user(c.email, c.password, c.name, c.code)
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    if user["role"] == "owner":
        _claim_local_projects(user["_id"])
    return _login_response(user, request, response, c.remember)


@app.post("/auth/login")
def login(c: Credentials, request: Request, response: Response):
    try:
        user = auth.login(c.email, c.password, _ip(request))
    except auth.AuthError as e:
        time.sleep(0.4)                           # slow down password guessing a little
        raise HTTPException(429 if "Too many" in str(e) else 400, str(e))
    return _login_response(user, request, response, c.remember)


# --------------------------------------------------------------------------- forgot / reset password
def _public_base(request: Request) -> str:
    return (os.environ.get("PUBLIC_URL") or str(request.base_url)).rstrip("/")


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:2]}{'•' * max(1, len(name) - 2)}@{domain}"


def _send_safely(to: str, subject: str, text: str, html: str) -> None:
    from . import mailer
    try:
        mailer.send(to, subject, text, html)
    except Exception as e:  # noqa: BLE001 - never leak SMTP problems to the requester
        print(f"  !! email to {_mask(to)} failed: {e}", flush=True)


def _notify_password_changed(user: dict, request: Request) -> None:
    from . import mailer
    when = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    text = (f"The password of your ClipForge account ({user['email']}) was changed on {when} "
            f"from IP {_ip(request) or 'unknown'}. All other devices were logged out.\n\n"
            f"If this wasn't you, reset your password now: {_public_base(request)}/#/forgot")
    html = mailer.html_page("Your password was changed",
                            f"The password of <b>{user['email']}</b> was changed on {when} from IP "
                            f"{_ip(request) or 'unknown'}. All other devices were logged out.<br><br>"
                            "If this wasn't you, reset your password right away.",
                            ("Reset my password", f"{_public_base(request)}/#/forgot"))
    threading.Thread(target=_send_safely, args=(user["email"], "Your ClipForge password was changed", text, html),
                     daemon=True).start()


class ForgotReq(BaseModel):
    email: str


@app.post("/auth/forgot")
def forgot_password(req: ForgotReq, request: Request):
    """Always the same answer, whether or not the email has an account (no account discovery)."""
    from . import mailer
    found = auth.create_reset(req.email, _ip(request))
    if found:
        user, token = found
        link = f"{_public_base(request)}/#/reset?token={token}"
        text = (f"Someone asked to reset the password of your ClipForge account ({user['email']}).\n\n"
                f"Choose a new password here (the link works once, for {auth.RESET_MINUTES} minutes):\n{link}\n\n"
                "If you didn't ask for this, ignore this email — your password stays the same.")
        html = mailer.html_page("Reset your password",
                                f"Someone asked to reset the password of <b>{user['email']}</b>. The button works once, "
                                f"for {auth.RESET_MINUTES} minutes.", ("Choose a new password", link))
        threading.Thread(target=_send_safely, args=(user["email"], "Reset your ClipForge password", text, html),
                         daemon=True).start()
    return {"ok": True, "message": f"If an account exists for that email, we've sent a link to reset the password. "
                                   f"It works for {auth.RESET_MINUTES} minutes."}


@app.get("/auth/reset/{token}")
def check_reset(token: str):
    user = auth.reset_user(token)
    return {"valid": bool(user), "email": _mask(user["email"]) if user else None}


class ResetReq(BaseModel):
    token: str
    password: str
    remember: bool = True


@app.post("/auth/reset")
def reset_password(req: ResetReq, request: Request, response: Response):
    try:
        user = auth.use_reset(req.token, req.password)
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    _notify_password_changed(user, request)
    return _login_response(user, request, response, req.remember)


@app.post("/auth/logout")
def logout(request: Request, response: Response):
    auth.end_session(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/auth/me")
def me(user: User):
    return auth.safe_user(user)


def _claim_local_projects(user_id: str) -> None:
    """First (owner) account: import projects and brand style made before accounts existed."""
    for d in ROOT.iterdir():
        meta = d / "job.json"
        if not (d.is_dir() and meta.exists()) or db().jobs.find_one({"_id": d.name}, {"_id": 1}):
            continue
        job = json.loads(meta.read_text(encoding="utf-8"))
        job.pop("id", None)
        doc = {**job, "_id": d.name, "user_id": user_id}
        for key, name in (("manifest", "clips.json"), ("social", "social.json"), ("publish", "publish.json")):
            if (d / name).exists():
                doc[key] = json.loads((d / name).read_text(encoding="utf-8"))
        if doc.get("state") in ("queued", "running"):
            doc.update(state="failed", error={"code": "interrupted", "message": "Interrupted before accounts existed."})
        db().jobs.insert_one(doc)
    legacy_brand = ROOT / "brand.json"
    if legacy_brand.exists():
        db().users.update_one({"_id": user_id}, {"$set": {"brand": BrandTemplate.load(legacy_brand).to_dict()}})


# =========================================================================== input validation
_ENUMS = {("layout", "aspect"): {"9:16", "1:1", "16:9"}, ("layout", "framing"): {"fit", "fill"},
          ("captions", "size"): {"s", "m", "l"}, ("captions", "background"): {"none", "shadow", "box"},
          ("captions", "animation"): {"none", "word", "karaoke", "pop"}, ("headline", "position"): {"top", "bottom"},
          ("headline", "style"): {"bold", "card", "band", "plain"},
          ("logo", "position"): {"top-left", "top-right", "bottom-left", "bottom-right"}}
_PATHS = {("logo", "path"), ("music", "path"), ("intro_outro", "intro_path"), ("intro_outro", "outro_path")}


def _check_values(obj, default, section: str = "") -> None:
    """Every template value has the type of its default — template values end up inside ffmpeg
    filter strings, so a crafted "colour" or "number" must never get through."""
    import math
    from dataclasses import fields, is_dataclass
    for f in fields(obj):
        v, d, key = getattr(obj, f.name), getattr(default, f.name), (section, f.name)
        if is_dataclass(d):
            if not is_dataclass(v):
                raise ValueError(f"{f.name} must be an object")
            _check_values(v, d, f.name)
        elif key in _ENUMS:
            if v not in _ENUMS[key]:
                raise ValueError(f"{section}.{f.name} must be one of {sorted(_ENUMS[key])}")
        elif f.name.endswith("color"):
            if not (isinstance(v, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", v)):
                raise ValueError(f"{section}.{f.name} must be a colour like #FFD60A")
        elif isinstance(d, bool):
            if not isinstance(v, bool):
                raise ValueError(f"{section}.{f.name} must be true or false")
        elif isinstance(d, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 10000:
                raise ValueError(f"{section}.{f.name} must be a number")
        elif key in _PATHS or f.name == "fonts_dir":
            if v is not None and not isinstance(v, str):
                raise ValueError(f"{f.name} must be a file path")
        elif isinstance(d, str):
            if not isinstance(v, str) or len(v) > 80 or re.search(r"[\x00-\x1f{}\\;,]", v):
                raise ValueError(f"{section}.{f.name} contains characters that aren't allowed")


def _safe_template(data: Optional[dict], user: dict, strict: bool = True) -> BrandTemplate:
    """Validated template whose files (logo, music, intro, outro) are the user's own uploads."""
    if not data:
        return BrandTemplate()
    try:
        tpl = BrandTemplate.from_dict(data)
        _check_values(tpl, BrandTemplate())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Invalid style: {e}")
    mine = (BRAND_ASSETS / user["_id"]).resolve()
    for section, attr in _PATHS:
        obj = getattr(tpl, section)
        p = getattr(obj, attr)
        if p and not (mine in Path(p).resolve().parents and Path(p).is_file()):
            if strict:
                raise HTTPException(400, "Logo, music, intro and outro must be files you uploaded in Brand style.")
            setattr(obj, attr, None)              # e.g. a style imported from before accounts existed
    if tpl.fonts_dir:
        if strict:
            raise HTTPException(400, "Custom font folders can't be set from the dashboard.")
        tpl.fonts_dir = None
    return tpl


def _resolve(host: str) -> list[str]:
    import socket
    return [i[4][0] for i in socket.getaddrinfo(host, None)]


def _check_link(url: str) -> str:
    """Only public web links: no local files, no localhost / private-network targets (SSRF)."""
    import ipaddress
    from urllib.parse import urlparse
    url = url.strip()
    if not re.match(r"^https?://", url, re.I) or len(url) > 2048:
        raise HTTPException(400, "Paste a web link that starts with https://")
    host = urlparse(url).hostname
    if not host:
        raise HTTPException(400, "That link has no website in it.")
    try:
        addrs = _resolve(host)
    except OSError:
        raise HTTPException(400, "That link's website can't be found.")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise HTTPException(400, "Links to private or local network addresses aren't allowed.")
    return url


# =========================================================================== jobs
def _job(job_id: str, user: dict, fields: Optional[dict] = None) -> dict:
    j = db().jobs.find_one({"_id": job_id, "user_id": user["_id"]}, fields)
    if not j:
        raise HTTPException(404, "Project not found.")
    return j


def _set(job_id: str, **fields) -> None:
    db().jobs.update_one({"_id": job_id}, {"$set": fields})


def _start(job_id: str) -> None:
    threading.Thread(target=_work, args=(job_id,), daemon=True).start()


def _video_meta(job_id: str, url: str, title: str) -> None:
    """Best-effort title/duration for the dashboard (YouTube etc.)."""
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as y:
            info = y.extract_info(url, download=False)
        _set(job_id, title=info.get("title") or title, duration=info.get("duration"))
    except Exception:  # noqa: BLE001
        pass


def _save_manifest(job_id: str, m: dict) -> None:
    (ROOT / job_id / "clips.json").write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    _set(job_id, manifest=m)


def _cloud_sync(job_id: str, user_id: str, m: dict, clip_ids: Optional[set] = None, progress=None) -> dict:
    """Upload rendered clips + covers to Cloudinary and record their URLs in the manifest."""
    if not storage.enabled():
        return m
    d, folder = ROOT / job_id, f"clipforge/{user_id}/{job_id}"
    todo = [c for c in m["clips"] if clip_ids is None or c["id"] in clip_ids]
    for k, c in enumerate(todo, 1):
        if progress:
            progress("upload", "saving to your library", f"{k} of {len(todo)}")
        try:
            v = storage.upload(d / c["file"], folder, "video")
            thumb = d / c.get("thumbnail", "")
            t = storage.upload(thumb, folder, "image") if c.get("thumbnail") and thumb.exists() else None
            c["cloud"] = {"video": v["url"], "thumb": t and t["url"], "public_id": v["public_id"],
                          "bytes": v["bytes"], "version": v["version"], "type": v.get("type", "upload"),
                          "format": v.get("format", "mp4")}
            c.pop("cloud_error", None)
        except Exception as e:  # noqa: BLE001 - the clip is still usable locally
            c["cloud_error"] = str(e)[:300]
    return m


def _work(job_id: str) -> None:
    job = db().jobs.find_one({"_id": job_id})
    inp = job["input"]

    def progress(step: str, status: str, detail: str = "") -> None:
        _set(job_id, **{f"steps.{step}": {"status": status, "detail": detail}, "current": step})

    if inp["source"].startswith(("http://", "https://")) and not job.get("duration"):
        _video_meta(job_id, inp["source"], job.get("title", ""))
    with GPU:
        try:
            _set(job_id, state="running")
            tpl = BrandTemplate.from_dict(inp["template"]) if inp.get("template") else BrandTemplate()
            m = run_pipeline(inp["source"], ROOT / job_id, tpl, inp["length"], inp["max_clips"], progress=progress)
            m = _cloud_sync(job_id, job["user_id"], m, progress=progress)
            _save_manifest(job_id, m)
            dur = db().jobs.find_one({"_id": job_id}, {"duration": 1}).get("duration")
            _set(job_id, state="done", clips=len(m["clips"]), elapsed_s=m["elapsed_s"], duration=dur or m["duration"])
        except ClipforgeError as e:
            _set(job_id, state="failed", error={"code": e.code, "message": str(e)})
        except Exception as e:  # noqa: BLE001
            _set(job_id, state="failed", error={"code": "error", "message": str(e)})


@app.on_event("startup")
def _startup() -> None:
    try:
        db()
    except DatabaseError as e:
        print(f"\n  !! {e}\n  The dashboard will show this until MongoDB is reachable.\n", flush=True)
        return
    for j in db().jobs.find({"state": {"$in": ["queued", "running"]}}, {"_id": 1}):
        _set(j["_id"], state="queued", resumed=True, steps={}, current=None)
        _start(j["_id"])
    for j in db().jobs.find({"publish": {"$exists": True}}, {"publish": 1}):   # uploads cut off by a restart
        for clip, plats in (j.get("publish") or {}).items():
            for platform, st in plats.items():
                if st.get("state") in ("queued", "running"):
                    _set(j["_id"], **{f"publish.{clip}.{platform}.state": "failed",
                                      f"publish.{clip}.{platform}.error": "Interrupted by a server restart — publish again."})
    threading.Thread(target=_scheduler, daemon=True).start()


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/jobs")
async def create_job(request: Request, user: User, file: Optional[UploadFile] = File(None),
                     url: Optional[str] = Form(None), length: str = Form("auto"), max_clips: int = Form(6),
                     template: Optional[str] = Form(None)):
    if not file and not url:
        raise HTTPException(400, "Send a file or a url")
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"That file is larger than {MAX_UPLOAD_MB} MB.")
    try:
        parse_length(length)
    except ClipforgeError as e:
        raise HTTPException(400, str(e))
    if not 1 <= max_clips <= 20:
        raise HTTPException(400, "max_clips must be between 1 and 20")
    if url and not file:
        url = _check_link(url)                    # never a local path or an internal address
    try:
        tpl = _safe_template(json.loads(template), user) if template else _safe_template(user.get("brand"), user, strict=False)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid style JSON.")
    job_id = uuid.uuid4().hex[:12]
    d = ROOT / job_id
    d.mkdir(parents=True)
    source = url
    if file:
        ext = Path(file.filename or "video.mp4").suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,6}", ext):
            ext = ".mp4"
        dst = d / f"upload{ext}"
        with dst.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        source = str(dst)
    origin = "Upload" if file else ("YouTube" if re.search(r"youtu\.?be", url or "") else "Link")
    job = {"_id": job_id, "user_id": user["_id"], "state": "queued", "steps": {}, "current": None,
           "source": url or (file.filename if file else ""), "title": (file.filename if file else url),
           "origin": origin, "length": length, "max_clips": max_clips, "created": time.time(),
           "input": {"source": source, "length": length, "max_clips": max_clips, "template": tpl.to_dict()}}
    db().jobs.insert_one(job)
    _start(job_id)
    return public(job, ("input",))


@app.get("/jobs")
def list_jobs(user: User):
    return [public(j) for j in db().jobs.find({"user_id": user["_id"]}, LIST_FIELDS).sort("created", -1).limit(50)]


@app.get("/jobs/{job_id}")
def get_job(job_id: str, user: User):
    return public(_job(job_id, user, LIST_FIELDS))


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str, user: User):
    j = _job(job_id, user, {"state": 1})
    if j["state"] in ("queued", "running"):
        raise HTTPException(409, "This project is still processing.")
    if storage.enabled():
        storage.delete_folder(f"clipforge/{user['_id']}/{job_id}")
    db().schedule.delete_many({"job": job_id})
    db().jobs.delete_one({"_id": job_id})
    shutil.rmtree(ROOT / job_id, ignore_errors=True)
    return {"ok": True}


@app.get("/jobs/{job_id}/clips")
def get_clips(job_id: str, user: User):
    m = _job(job_id, user, {"manifest": 1}).get("manifest")
    if not m:
        raise HTTPException(404, "not ready")
    return m


def _manifest(job_id: str) -> dict:
    j = db().jobs.find_one({"_id": job_id}, {"manifest": 1})
    if not j or not j.get("manifest"):
        raise HTTPException(404, "not ready")
    return j["manifest"]


def _clip(job_id: str, clip_id: str) -> tuple[dict, dict]:
    m = _manifest(job_id)
    c = next((c for c in m["clips"] if c["id"] == clip_id), None)
    if not c:
        raise HTTPException(404, "clip not found")
    return m, c


@app.get("/jobs/{job_id}/files/{name}")
def get_file(job_id: str, name: str, request: Request, user: User, download: bool = False):
    _job(job_id, user, {"_id": 1})
    p = (ROOT / job_id / name).resolve()
    if p.parent != (ROOT / job_id).resolve() or not p.exists():
        raise HTTPException(404)
    headers = {"Accept-Ranges": "bytes"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers.get("range", ""))
    if not m or not (m[1] or m[2]):
        return FileResponse(p, headers=headers)
    size = p.stat().st_size
    start, end = (int(m[1]), int(m[2]) if m[2] else size - 1) if m[1] else (max(0, size - int(m[2])), size - 1)
    end = min(end, size - 1)
    if start > end:
        raise HTTPException(416)

    def chunks():
        with p.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                b = f.read(min(1 << 20, left))
                if not b:
                    break
                left -= len(b)
                yield b

    headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(end - start + 1)})
    return StreamingResponse(chunks(), status_code=206, headers=headers,
                             media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream")


@app.get("/jobs/{job_id}/clips/{clip_id}/download")
def download_clip(job_id: str, clip_id: str, user: User):
    """Owner-only download: a short hop to a signed Cloudinary link, or the file from this PC."""
    _job(job_id, user, {"_id": 1})
    _, c = _clip(job_id, clip_id)
    cloud = c.get("cloud") or {}
    if cloud.get("public_id") and storage.enabled():
        url = storage.signed_url(cloud["public_id"], "video", cloud.get("version"), cloud.get("format", "mp4"),
                                 download=True, delivery=cloud.get("type", "upload"))
        return RedirectResponse(url, status_code=302)
    p = ROOT / job_id / c["file"]
    if not p.exists():
        raise HTTPException(404, "The file isn't on this server any more.")
    return FileResponse(p, filename=f"{clip_id}.mp4")


@app.get("/jobs/{job_id}/zip")
def zip_clips(job_id: str, ids: str, user: User):
    _job(job_id, user, {"_id": 1})
    d = ROOT / job_id
    files = [d / f"{i}.mp4" for i in ids.split(",") if re.fullmatch(r"clip_\d+", i)]
    files = [f for f in files if f.exists()]
    if not files:
        raise HTTPException(404)
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:   # mp4 is already compressed
        for f in files:
            z.write(f, f.name)
    return FileResponse(tmp, filename="clipforge_clips.zip", background=BackgroundTask(os.unlink, tmp))


class Edit(BaseModel):
    hook: Optional[str] = None
    trim: Optional[tuple[float, float]] = None
    lines: Optional[list[dict]] = None
    template: Optional[dict] = None


@app.post("/jobs/{job_id}/clips/{clip_id}")
def edit_clip(job_id: str, clip_id: str, edit: Edit, user: User):
    _job(job_id, user, {"_id": 1})
    tpl = _safe_template(edit.template, user) if edit.template else None
    if edit.trim and not (0 <= edit.trim[0] < edit.trim[1] and edit.trim[1] - edit.trim[0] <= 600):
        raise HTTPException(400, "Trim must be a start before the end, at most 10 minutes long.")
    with GPU:
        try:
            rerender(ROOT / job_id, clip_id, hook=edit.hook, trim=edit.trim, lines=edit.lines, tpl=tpl)
        except StopIteration:
            raise HTTPException(404, "clip not found")
    m = json.loads((ROOT / job_id / "clips.json").read_text(encoding="utf-8"))
    m = _cloud_sync(job_id, user["_id"], m, {clip_id})
    _save_manifest(job_id, m)
    return next(c for c in m["clips"] if c["id"] == clip_id)


# =========================================================================== brand template
BRAND_ASSETS = ROOT / "brand_assets"
_FONT_FILES = {"Arial Black": "ariblk.ttf", "Impact": "impact.ttf", "Segoe UI Black": "seguibl.ttf",
               "Segoe UI": "segoeui.ttf", "Bahnschrift": "bahnschrift.ttf", "Arial": "arial.ttf",
               "Verdana": "verdana.ttf", "Trebuchet MS": "trebuc.ttf", "Tahoma": "tahoma.ttf",
               "Georgia": "georgia.ttf", "Comic Sans MS": "comic.ttf", "Nirmala UI": "Nirmala.ttc",
               "Poppins": "Poppins-Bold.ttf", "Montserrat": "Montserrat-Bold.ttf"}


def _brand(user: dict) -> BrandTemplate:
    """The user's saved brand style; their new videos are rendered with it."""
    return _safe_template(user.get("brand"), user, strict=False)


def _fonts() -> list[str]:
    win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if not win.exists():
        return list(_FONT_FILES)
    found = [name for name, f in _FONT_FILES.items() if (win / f).exists()]
    return found or list(_FONT_FILES)


@app.get("/brand")
def get_brand(user: User):
    return {"template": _brand(user).to_dict(), "default": BrandTemplate().to_dict(), "fonts": _fonts(),
            "saved": bool(user.get("brand"))}


@app.put("/brand")
def put_brand(template: dict, user: User):
    tpl = _safe_template(template, user)
    db().users.update_one({"_id": user["_id"]}, {"$set": {"brand": tpl.to_dict()}})
    return {"template": tpl.to_dict()}


class PreviewReq(BaseModel):
    template: dict
    job: Optional[str] = None
    clip: Optional[str] = None
    hook: Optional[str] = None


@app.post("/brand/preview")
def brand_preview(req: PreviewReq, user: User):
    """One frame rendered with the real caption/hook renderer — what the clip will look like."""
    from .render import render_preview
    tpl = _safe_template(req.template, user)
    src, start, lines, emph, hook = None, 0.0, None, [], (req.hook or "")[:200] if req.hook is not None else None
    if req.job and req.clip:
        try:
            _job(req.job, user, {"_id": 1})
            m, c = _clip(req.job, req.clip)
            src, start, lines, emph = m["source"], c["start"], c["lines"], c.get("emphasis_words", [])
            hook = hook if hook is not None else c["hook"]
        except HTTPException:
            pass
    BRAND_ASSETS.mkdir(exist_ok=True)
    out = BRAND_ASSETS / f"_preview_{uuid.uuid4().hex[:8]}.jpg"
    try:
        render_preview(tpl, hook if hook is not None else "Your hook goes here", lines, emph, src, start, out)
        data = out.read_bytes()
    except ClipforgeError as e:
        raise HTTPException(400, str(e))
    finally:
        out.unlink(missing_ok=True)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/brand/asset")
async def brand_asset(user: User, file: UploadFile = File(...)):
    """Upload a logo / music / intro / outro file; returns the path to put in the template."""
    folder = BRAND_ASSETS / user["_id"]
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.-]", "_", Path(file.filename or "asset").name)[-80:]
    if Path(name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".mp3", ".wav", ".m4a", ".aac", ".ogg",
                                         ".mp4", ".mov", ".webm", ".mkv"}:
        raise HTTPException(400, "Upload an image (PNG/JPG/WebP), audio (MP3/WAV/M4A) or video (MP4/MOV/WebM).")
    dst = folder / f"{uuid.uuid4().hex[:6]}_{name}"
    with dst.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"path": str(dst), "name": file.filename}


# =========================================================================== publishing
def _social_for(job_id: str, clip_id: str, regenerate: bool = False) -> dict:
    """Post copy + cover for a clip, generated once and kept on the job document."""
    from . import social
    d = ROOT / job_id
    m, c = _clip(job_id, clip_id)
    s = (db().jobs.find_one({"_id": job_id}, {f"social.{clip_id}": 1}).get("social") or {}).get(clip_id)
    stale = s and s.get("clip_v") != [c["hook"], c["start"], c["end"]]
    if s and not regenerate and not stale:
        return s
    s = social.generate(c, m.get("language", "en"))
    cover = d / f"{clip_id}.cover.jpg"
    if "cover_ms" in c and cover.exists():       # clean cover rendered with the clip (hook, no captions)
        s["cover_ms"] = c["cover_ms"]
    else:
        s["cover_ms"] = social.pick_cover(d / c["file"], cover)
    s["cover"] = cover.name
    s["cover_url"] = (c.get("cloud") or {}).get("thumb")
    s["clip_v"] = [c["hook"], c["start"], c["end"]]
    _set(job_id, **{f"social.{clip_id}": s})
    return s


def _set_status(job_id: str, clip_id: str, key: str, **kw) -> None:
    """Publish status of one clip on one account (key = account id)."""
    kw["updated"] = time.time()
    _set(job_id, **{f"publish.{clip_id}.{key}.{k}": v for k, v in kw.items()})


# =========================================================================== social accounts (many per platform)
def _user_accounts(user: dict) -> list[dict]:
    """The user's attached accounts; OAuth connections from before multi-account are migrated in."""
    accts = list(user.get("accounts") or [])
    legacy = user.get("connections") or {}
    if legacy:
        for p, c in legacy.items():
            a = {"id": uuid.uuid4().hex[:12], "platform": p, "method": "oauth", "name": c.get("name"),
                 "ext_id": c.get("id"), "created": c.get("connected_at", time.time()), "status": "ok",
                 "default": not any(x["platform"] == p for x in accts)}
            if p == "youtube":
                a["refresh_token"] = c["refresh_token"]
            else:
                a.update(access_token=c["token"], refreshed=c.get("refreshed"), expires=c.get("expires"),
                         kind="instagram_login")
            accts.append(a)
        db().users.update_one({"_id": user["_id"]}, {"$set": {"accounts": accts}, "$unset": {"connections": ""}})
    return accts


def _account(user_id: str, account_id: str) -> dict:
    user = db().users.find_one({"_id": user_id}) or {}
    accts = _user_accounts(user)
    if account_id in ("youtube", "instagram"):    # schedules made before multi-account: the default one
        account_id = next((a["id"] for a in accts if a["platform"] == account_id and a.get("default")),
                          next((a["id"] for a in accts if a["platform"] == account_id), ""))
    acct = next((a for a in accts if a["id"] == account_id), None)
    if not acct:
        raise RuntimeError("This account was removed — pick another one in the publish dialog.")
    return acct


def _save_account(user_id: str, account_id: str, fields: dict) -> None:
    db().users.update_one({"_id": user_id, "accounts.id": account_id},
                          {"$set": {f"accounts.$.{k}": v for k, v in fields.items()}})


def _publish_one(job_id: str, clip_id: str, account_id: str, opts: dict) -> None:
    """Upload one clip to one of the job owner's accounts (runs in a background thread)."""
    from . import accounts, publish
    d = ROOT / job_id
    try:
        user_id = db().jobs.find_one({"_id": job_id}, {"user_id": 1})["user_id"]
        acct = _account(user_id, account_id)
        key, platform = account_id, acct["platform"]
        prog = lambda st, p: _set_status(job_id, clip_id, key, state="running", step=st, pct=round(p, 3))  # noqa: E731
        _set_status(job_id, clip_id, key, platform=platform, account=acct.get("label") or acct.get("name"),
                    state="running", step="starting", pct=0, error=None)
        _, c = _clip(job_id, clip_id)
        s = _social_for(job_id, clip_id)
        post = (opts.get("post") or {})
        video = d / c["file"]
        if platform == "youtube":
            yt = {**s["youtube"], **(post.get("youtube") or {})}
            at = opts.get("publish_at")
            r = publish.youtube_upload(video, yt["title"], yt["description"], yt.get("tags", []),
                                       privacy=opts.get("privacy", "public"),
                                       publish_at=datetime.fromtimestamp(at, timezone.utc) if at else None,
                                       thumbnail=d / s["cover"], progress=prog, creds=accounts.youtube_creds(acct))
        else:
            ig = {**s["instagram"], **(post.get("instagram") or {})}
            token = accounts.instagram_token(acct, lambda upd: _save_account(user_id, acct["id"], upd))
            if acct.get("kind") == "facebook_login":   # EAA token: resumable upload straight from disk
                r = publish.instagram_upload(video, ig["caption"], s.get("cover_ms", 0), progress=prog,
                                             token=token, ig_user_id=acct["ext_id"])
            else:                                      # Instagram-Login token: Instagram fetches a public URL
                if not (c.get("cloud") or {}).get("video"):
                    if not storage.enabled():
                        raise RuntimeError("This Instagram login posts from a public link — add CLOUDINARY_URL to "
                                           "the server's .env, or use a Facebook-Login token (EAA…).")
                    prog("uploading to cloud storage", 0.03)
                    m = _cloud_sync(job_id, user_id, _manifest(job_id), {clip_id})
                    _save_manifest(job_id, m)
                    c = next(x for x in m["clips"] if x["id"] == clip_id)
                    if not (c.get("cloud") or {}).get("video"):
                        raise RuntimeError("Cloud upload failed: " + c.get("cloud_error", "unknown error"))
                r = publish.instagram_publish_url(token, acct["ext_id"], c["cloud"]["video"], ig["caption"],
                                                  (c.get("cloud") or {}).get("thumb"), s.get("cover_ms", 0), progress=prog)
        _set_status(job_id, clip_id, key, state="scheduled" if opts.get("publish_at") and platform == "youtube"
                    else "done", pct=1, step="done", **r)
    except Exception as e:  # noqa: BLE001
        _set_status(job_id, clip_id, account_id, state="failed", error=str(e)[:500])


def _scheduler() -> None:
    """Posts due items from the schedule collection (Instagram has no native scheduling)."""
    from pymongo import ASCENDING
    while True:
        try:
            while x := db().schedule.find_one_and_delete({"at": {"$lte": time.time()}}, sort=[("at", ASCENDING)]):
                threading.Thread(target=_publish_one, args=(x["job"], x["clip"], x.get("account") or x["platform"],
                                                            x["opts"]), daemon=True).start()
        except Exception:  # noqa: BLE001 - database hiccup: try again next round
            pass
        time.sleep(20)


def _accounts_payload(user: dict) -> dict:
    from . import accounts, connections
    accts = [accounts.public(a) for a in _user_accounts(user)]
    for a in accts:
        if a["platform"] == "instagram" and a.get("kind") != "facebook_login" and not storage.enabled():
            a["warning"] = "Posts from a public link — needs CLOUDINARY_URL on the server (or an EAA… token)."
    return {"accounts": accts, "cloud": storage.enabled(),
            "oauth": {"youtube": bool(connections.google_client()), "instagram": bool(connections.instagram_app())},
            "env": [p for p, _ in accounts.from_env()] if user.get("role") == "owner" else []}


@app.get("/publish/accounts")
@app.get("/accounts")
def list_accounts(user: User):
    return _accounts_payload(user)


class AccountReq(BaseModel):
    platform: Optional[str] = None
    label: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    refresh_token: Optional[str] = None
    access_token: Optional[str] = None
    ig_user_id: Optional[str] = None
    default: Optional[bool] = None


def _store_accounts(user_id: str, accts: list[dict]) -> None:
    for p in ("youtube", "instagram"):               # exactly one default per platform that has accounts
        mine = [a for a in accts if a["platform"] == p]
        if mine and not any(a.get("default") for a in mine):
            mine[0]["default"] = True
    db().users.update_one({"_id": user_id}, {"$set": {"accounts": accts}})


@app.post("/accounts")
def add_account(req: AccountReq, user: User):
    from . import accounts
    from .publish import PublishError
    try:
        acct = accounts.build(req.platform or "", req.model_dump())
    except PublishError as e:
        raise HTTPException(400, str(e))
    accts = _user_accounts(user)
    dup = next((a for a in accts if a["platform"] == acct["platform"] and a.get("ext_id")
                and a.get("ext_id") == acct.get("ext_id")), None)
    if dup:
        raise HTTPException(400, f"{acct['name']} is already added — edit it instead.")
    acct["default"] = not any(a["platform"] == acct["platform"] for a in accts)
    _store_accounts(user["_id"], accts + [acct])
    return accounts.public(acct)


@app.patch("/accounts/{account_id}")
def edit_account(account_id: str, req: AccountReq, user: User):
    from . import accounts
    from .publish import PublishError
    accts = _user_accounts(user)
    i = next((k for k, a in enumerate(accts) if a["id"] == account_id), None)
    if i is None:
        raise HTTPException(404, "Account not found.")
    fields = req.model_dump()
    creds_changed = any(fields.get(f) for f in ("client_id", "client_secret", "refresh_token", "access_token")) \
        or fields.get("ig_user_id") is not None
    if creds_changed:
        try:
            accts[i] = accounts.build(accts[i]["platform"], fields, accts[i])
        except PublishError as e:
            raise HTTPException(400, str(e))
    elif req.label is not None:
        accts[i]["label"] = req.label.strip()[:40]
    if req.default:
        for a in accts:
            if a["platform"] == accts[i]["platform"]:
                a["default"] = a["id"] == account_id
    _store_accounts(user["_id"], accts)
    return accounts.public(accts[i])


@app.post("/accounts/{account_id}/check")
def check_account(account_id: str, user: User):
    """Log in again with the stored credentials and refresh the name/status."""
    from . import accounts
    accts = _user_accounts(user)
    a = next((x for x in accts if x["id"] == account_id), None)
    if not a:
        raise HTTPException(404, "Account not found.")
    try:
        info = accounts.verify_youtube(a) if a["platform"] == "youtube" else accounts.verify_instagram(a)
        a.update({k: v for k, v in info.items() if k != "warning"}, status="ok", last_error=info.get("warning"))
    except Exception as e:  # noqa: BLE001
        a.update(status="error", last_error=str(e)[:300])
    a["checked"] = time.time()
    _store_accounts(user["_id"], accts)
    return accounts.public(a)


@app.delete("/accounts/{account_id}")
def remove_account(account_id: str, user: User):
    from . import connections
    accts = _user_accounts(user)
    a = next((x for x in accts if x["id"] == account_id), None)
    if not a:
        raise HTTPException(404, "Account not found.")
    if a["platform"] == "youtube" and a.get("method") == "oauth":
        connections.google_revoke(a)              # only revoke logins ClipForge created itself
    _store_accounts(user["_id"], [x for x in accts if x["id"] != account_id])
    db().schedule.delete_many({"user_id": user["_id"], "account": account_id})
    return {"ok": True}


@app.post("/accounts/import-env")
def import_env_accounts(user: User):
    """Owner: add the YouTube / Instagram accounts configured in the server's .env."""
    from . import accounts
    if user.get("role") != "owner":
        raise HTTPException(403, "Only the owner can import the server's .env accounts.")
    added, errors = [], []
    for platform, fields in accounts.from_env():
        try:
            r = add_account(AccountReq(platform=platform, **fields), db().users.find_one({"_id": user["_id"]}))
            added.append(r["name"])
        except HTTPException as e:
            errors.append(f"{platform}: {e.detail}")
    return {"added": added, "errors": errors}


@app.get("/jobs/{job_id}/publish")
def publish_state(job_id: str, user: User):
    j = _job(job_id, user, {"social": 1, "publish": 1})
    return {"social": j.get("social") or {}, "status": j.get("publish") or {},
            "scheduled": [public(x) for x in db().schedule.find({"job": job_id}, {"opts": 0})]}


@app.post("/jobs/{job_id}/clips/{clip_id}/social")
def clip_social(job_id: str, clip_id: str, user: User, regenerate: bool = False):
    _job(job_id, user, {"_id": 1})
    return _social_for(job_id, clip_id, regenerate)


class PublishReq(BaseModel):
    clips: list[str]
    accounts: list[str]                        # account ids from /accounts (any mix of platforms)
    privacy: str = "public"
    start_at: Optional[float] = None           # unix seconds; None = now
    every_hours: float = 0                     # spacing between clips when several are scheduled
    post: Optional[dict] = None                # edited copy (single clip only)


@app.post("/jobs/{job_id}/publish")
def publish_clips(job_id: str, req: PublishReq, user: User):
    _job(job_id, user, {"_id": 1})
    mine = {a["id"]: a for a in _user_accounts(user)}
    if not req.accounts:
        raise HTTPException(400, "Pick at least one account.")
    if any(a not in mine for a in req.accounts):
        raise HTTPException(400, "Add your YouTube / Instagram accounts in Settings first.")
    for cid in req.clips:
        _clip(job_id, cid)
    if req.post and len(req.clips) == 1:       # keep edits so the dialog shows them next time
        _set(job_id, **{f"social.{req.clips[0]}.{k}.{f}": v for k in ("youtube", "instagram")
                        for f, v in (req.post.get(k) or {}).items()})
    now = time.time()
    queued = []
    for i, cid in enumerate(req.clips):
        at = (req.start_at or now) + i * req.every_hours * 3600
        for aid in req.accounts:
            a = mine[aid]
            meta = {"platform": a["platform"], "account": a.get("label") or a.get("name")}
            opts = {"privacy": req.privacy, "post": req.post if len(req.clips) == 1 else None}
            if at <= now + 60:
                _set_status(job_id, cid, aid, state="queued", pct=0, error=None, at=None, **meta)
                threading.Thread(target=_publish_one, args=(job_id, cid, aid, opts), daemon=True).start()
            elif a["platform"] == "youtube":    # YouTube: upload now, YouTube publishes at the time
                opts["publish_at"] = at
                _set_status(job_id, cid, aid, state="queued", pct=0, error=None, at=at, **meta)
                threading.Thread(target=_publish_one, args=(job_id, cid, aid, opts), daemon=True).start()
            else:                               # Instagram: our scheduler posts at the time
                db().schedule.insert_one({"job": job_id, "user_id": user["_id"], "clip": cid, "account": aid,
                                          "platform": a["platform"], "at": at, "opts": opts})
                _set_status(job_id, cid, aid, state="waiting", pct=0, error=None, at=at, **meta)
            queued.append({"clip": cid, "account": aid, "at": at})
    return {"queued": queued}


# =========================================================================== connect accounts (OAuth)
PROVIDERS = ("youtube", "instagram")


def _redirect_uri(request: Request, provider: str) -> str:
    return f"{_public_base(request)}/connect/{provider}/callback"


def _back(next_hash: str, **params) -> RedirectResponse:
    q = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    target = next_hash if next_hash.startswith("#/") else "#/settings"
    return RedirectResponse(f"/{target}{'&' if '?' in target else '?'}{q}", status_code=303)


@app.get("/connect/{provider}/start")
def connect_start(provider: str, request: Request, user: User, next: str = "#/settings"):
    """Send the user to Google / Instagram to grant ClipForge access to their own account."""
    from . import connections
    if provider not in PROVIDERS:
        raise HTTPException(404)
    if provider == "youtube" and not connections.google_client():
        return _back(next, error="YouTube sign-in isn't set up on this server yet (GOOGLE_OAUTH_CLIENT_ID).")
    if provider == "instagram" and not connections.instagram_app():
        return _back(next, error="Instagram sign-in isn't set up on this server yet (INSTAGRAM_APP_ID).")
    state = uuid.uuid4().hex + uuid.uuid4().hex
    redirect = _redirect_uri(request, provider)
    doc = {"_id": state, "user_id": user["_id"], "provider": provider, "next": next, "redirect": redirect,
           "expires": datetime.now(timezone.utc) + timedelta(minutes=15)}
    if provider == "youtube":
        url, doc["verifier"] = connections.google_auth_url(redirect, state)
    else:
        url = connections.instagram_auth_url(redirect, state)
    db().oauth_states.insert_one(doc)
    return RedirectResponse(url, status_code=303)


@app.get("/connect/{provider}/callback")
def connect_callback(provider: str, state: str = "", code: str = "", error: str = "",
                     error_description: str = "", error_reason: str = ""):
    """Provider sends the user back here; the one-time state ties the result to the right account."""
    from . import connections
    st = db().oauth_states.find_one_and_delete({"_id": state, "provider": provider}) if state else None
    if not st or st["expires"].replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return _back("#/settings", error="That sign-in link expired — click Connect again.")
    if error or not code:
        return _back(st["next"], error=error_description or error_reason or error or "Access was not granted.")
    try:
        if provider == "youtube":
            conn = connections.google_finish(st["redirect"], code, st["verifier"])
        else:
            conn = connections.instagram_finish(st["redirect"], code)
    except Exception as e:  # noqa: BLE001
        return _back(st["next"], error=f"Couldn't connect: {str(e)[:200]}")
    user = db().users.find_one({"_id": st["user_id"]})
    accts = _user_accounts(user)
    acct = next((a for a in accts if a["platform"] == provider and a.get("ext_id") == conn["id"]), None)
    if not acct:                                  # a new account; connecting the same one again refreshes it
        acct = {"id": uuid.uuid4().hex[:12], "platform": provider, "method": "oauth", "created": time.time(),
                "default": not any(a["platform"] == provider for a in accts)}
        accts.append(acct)
    acct.update(ext_id=conn["id"], name=conn["name"], status="ok", last_error=None, checked=time.time())
    if provider == "youtube":
        acct.update(refresh_token=conn["refresh_token"], client_id=None, client_secret=None)
    else:
        acct.update(access_token=conn["token"], refreshed=conn["refreshed"], expires=conn["expires"],
                    kind="instagram_login")
    _store_accounts(user["_id"], accts)
    return _back(st["next"], connected=provider, name=conn.get("name", ""))


# =========================================================================== settings: profile & account
@app.get("/settings")
def get_settings(request: Request, user: User):
    return {"profile": {**auth.safe_user(user), "created": user.get("created")}, **_accounts_payload(user),
            "storage": "cloudinary" if storage.enabled() else "local",
            "public_url": os.environ.get("PUBLIC_URL") or None,
            "redirects": {p: _redirect_uri(request, p) for p in PROVIDERS}}


class ProfileReq(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None


@app.patch("/settings/profile")
def update_profile(req: ProfileReq, user: User):
    upd = {}
    if req.name is not None:
        if not req.name.strip():
            raise HTTPException(400, "Name can't be empty.")
        upd["name"] = req.name.strip()[:60]
    if req.email is not None and req.email.strip().lower() != user["email"]:
        email = req.email.strip().lower()
        if not auth.EMAIL_RE.match(email):
            raise HTTPException(400, "Enter a valid email address.")
        if db().users.find_one({"email": email}, {"_id": 1}):
            raise HTTPException(400, "Another account already uses that email.")
        upd["email"] = email
    if upd:
        db().users.update_one({"_id": user["_id"]}, {"$set": upd})
    return auth.safe_user({**user, **upd})


class PasswordReq(BaseModel):
    current: str
    new: str


@app.post("/settings/password")
def change_password(req: PasswordReq, request: Request, user: User):
    if not auth.check_password(req.current, user["password"]):
        raise HTTPException(400, "Your current password isn't right.")
    if problem := auth.password_problem(req.new, user["email"]):
        raise HTTPException(400, problem)
    db().users.update_one({"_id": user["_id"]}, {"$set": {"password": auth.hash_password(req.new)}})
    keep = auth._token_hash(request.cookies.get(auth.COOKIE, ""))   # other devices are logged out
    db().sessions.delete_many({"user_id": user["_id"], "token": {"$ne": keep}})
    db().password_resets.delete_many({"user_id": user["_id"]})        # outstanding reset links stop working
    _notify_password_changed(user, request)
    return {"ok": True}


@app.get("/settings/sessions")
def list_sessions(request: Request, user: User):
    """Where this account is logged in (device, IP, last activity)."""
    return auth.sessions_of(user["_id"], request.cookies.get(auth.COOKIE))


@app.delete("/settings/sessions/{sid}")
def end_one_session(sid: str, user: User):
    if not auth.end_session_id(user["_id"], sid):
        raise HTTPException(404, "That session already ended.")
    return {"ok": True}


@app.post("/settings/logout-all")
def logout_all(request: Request, user: User):
    keep = auth._token_hash(request.cookies.get(auth.COOKIE, ""))
    n = db().sessions.delete_many({"user_id": user["_id"], "token": {"$ne": keep}}).deleted_count
    return {"ended": n}


class DeleteReq(BaseModel):
    password: str


@app.post("/settings/delete-account")
def delete_account(req: DeleteReq, response: Response, user: User):
    from . import connections
    if not auth.check_password(req.password, user["password"]):
        raise HTTPException(400, "Wrong password.")
    if db().jobs.find_one({"user_id": user["_id"], "state": {"$in": ["queued", "running"]}}, {"_id": 1}):
        raise HTTPException(409, "A video is still processing — wait for it to finish first.")
    for a in _user_accounts(user):
        if a["platform"] == "youtube" and a.get("method") == "oauth":
            connections.google_revoke(a)
    for j in db().jobs.find({"user_id": user["_id"]}, {"_id": 1}):
        shutil.rmtree(ROOT / j["_id"], ignore_errors=True)
    if storage.enabled():
        storage.delete_folder(f"clipforge/{user['_id']}")
    shutil.rmtree(BRAND_ASSETS / user["_id"], ignore_errors=True)
    for coll in ("jobs", "schedule", "sessions", "oauth_states"):
        db()[coll].delete_many({"user_id": user["_id"]})
    db().users.delete_one({"_id": user["_id"]})
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}
