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
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
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
app = FastAPI(title="clipforge")


# =========================================================================== auth
def current_user(request: Request) -> dict:
    try:
        user = auth.user_for(request.cookies.get(auth.COOKIE))
    except DatabaseError as e:
        raise HTTPException(503, str(e))
    if not user:
        raise HTTPException(401, "Please log in.")
    return user


User = Annotated[dict, Depends(current_user)]


def owner(user: User) -> dict:
    if user.get("role") != "owner":
        raise HTTPException(403, "Only the owner account can publish — the connected YouTube and "
                                 "Instagram accounts belong to them.")
    return user


Owner = Annotated[dict, Depends(owner)]


class Credentials(BaseModel):
    email: str
    password: str
    name: str = ""
    code: str = ""


def _login_response(user: dict, response: Response) -> dict:
    response.set_cookie(auth.COOKIE, auth.new_session(user["_id"]), max_age=auth.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", path="/")
    return auth.safe_user(user)


@app.get("/auth/config")
def auth_config():
    try:
        first = db().users.estimated_document_count() == 0
    except DatabaseError as e:
        return {"database": False, "error": str(e), "cloud": storage.enabled()}
    return {"database": True, "first_user": first, "signup": auth.signup_mode(), "cloud": storage.enabled()}


@app.post("/auth/signup")
def signup(c: Credentials, response: Response):
    try:
        user = auth.create_user(c.email, c.password, c.name, c.code)
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    if user["role"] == "owner":
        _claim_local_projects(user["_id"])
    return _login_response(user, response)


@app.post("/auth/login")
def login(c: Credentials, response: Response):
    try:
        user = auth.login(c.email, c.password)
    except auth.AuthError as e:
        time.sleep(0.4)                           # slow down password guessing a little
        raise HTTPException(400, str(e))
    return _login_response(user, response)


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
                          "bytes": v["bytes"], "version": v["version"]}
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
async def create_job(user: User, file: Optional[UploadFile] = File(None), url: Optional[str] = Form(None),
                     length: str = Form("auto"), max_clips: int = Form(6), template: Optional[str] = Form(None)):
    if not file and not url:
        raise HTTPException(400, "Send a file or a url")
    try:
        parse_length(length)
    except ClipforgeError as e:
        raise HTTPException(400, str(e))
    job_id = uuid.uuid4().hex[:12]
    d = ROOT / job_id
    d.mkdir(parents=True)
    source = url
    if file:
        dst = d / f"upload{Path(file.filename or 'video.mp4').suffix}"
        with dst.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        source = str(dst)
    origin = "Upload" if file else ("YouTube" if re.search(r"youtu\.?be", url or "") else "Link")
    job = {"_id": job_id, "user_id": user["_id"], "state": "queued", "steps": {}, "current": None,
           "source": url or (file.filename if file else ""), "title": (file.filename if file else url),
           "origin": origin, "length": length, "max_clips": max_clips, "created": time.time(),
           "input": {"source": source, "length": length, "max_clips": max_clips,
                     "template": json.loads(template) if template else _brand(user).to_dict()}}
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
    tpl = BrandTemplate.from_dict(edit.template) if edit.template else None
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
    return BrandTemplate.from_dict(user["brand"]) if user.get("brand") else BrandTemplate()


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
    try:
        tpl = BrandTemplate.from_dict(template)
        tpl.size                                  # validates aspect
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Invalid template: {e}")
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
    tpl = BrandTemplate.from_dict(req.template)
    src, start, lines, emph, hook = None, 0.0, None, [], req.hook
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


def _set_status(job_id: str, clip_id: str, platform: str, **kw) -> None:
    kw["updated"] = time.time()
    _set(job_id, **{f"publish.{clip_id}.{platform}.{k}": v for k, v in kw.items()})


def _publish_one(job_id: str, clip_id: str, platform: str, opts: dict) -> None:
    """Upload one clip to one platform (runs in a background thread)."""
    from . import publish
    d = ROOT / job_id
    try:
        _, c = _clip(job_id, clip_id)
        s = _social_for(job_id, clip_id)
        post = (opts.get("post") or {})
        video = d / c["file"]
        prog = lambda st, p: _set_status(job_id, clip_id, platform, state="running", step=st, pct=round(p, 3))  # noqa: E731
        _set_status(job_id, clip_id, platform, state="running", step="starting", pct=0, error=None)
        if platform == "youtube":
            yt = {**s["youtube"], **(post.get("youtube") or {})}
            at = opts.get("publish_at")
            r = publish.youtube_upload(video, yt["title"], yt["description"], yt.get("tags", []),
                                       privacy=opts.get("privacy", "public"),
                                       publish_at=datetime.fromtimestamp(at, timezone.utc) if at else None,
                                       thumbnail=d / s["cover"], progress=prog)
        else:
            ig = {**s["instagram"], **(post.get("instagram") or {})}
            r = publish.instagram_upload(video, ig["caption"], s.get("cover_ms", 0), progress=prog)
        _set_status(job_id, clip_id, platform, state="scheduled" if opts.get("publish_at") and platform == "youtube"
                    else "done", pct=1, step="done", **r)
    except Exception as e:  # noqa: BLE001
        _set_status(job_id, clip_id, platform, state="failed", error=str(e)[:500])


def _scheduler() -> None:
    """Posts due items from the schedule collection (Instagram has no native scheduling)."""
    from pymongo import ASCENDING
    while True:
        try:
            while x := db().schedule.find_one_and_delete({"at": {"$lte": time.time()}}, sort=[("at", ASCENDING)]):
                threading.Thread(target=_publish_one, args=(x["job"], x["clip"], x["platform"], x["opts"]),
                                 daemon=True).start()
        except Exception:  # noqa: BLE001 - database hiccup: try again next round
            pass
        time.sleep(20)


@app.get("/publish/accounts")
def publish_accounts(user: Owner):
    from . import publish
    return publish.accounts()


@app.post("/publish/youtube/connect")
def youtube_connect(user: Owner):
    from . import publish
    try:
        return publish.youtube_connect()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


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
    platforms: list[str]
    privacy: str = "public"
    start_at: Optional[float] = None           # unix seconds; None = now
    every_hours: float = 0                     # spacing between clips when several are scheduled
    post: Optional[dict] = None                # edited copy (single clip only)


@app.post("/jobs/{job_id}/publish")
def publish_clips(job_id: str, req: PublishReq, user: Owner):
    _job(job_id, user, {"_id": 1})
    if not set(req.platforms) <= {"youtube", "instagram"} or not req.platforms:
        raise HTTPException(400, "platforms must be youtube and/or instagram")
    for cid in req.clips:
        _clip(job_id, cid)
    if req.post and len(req.clips) == 1:       # keep edits so the dialog shows them next time
        _set(job_id, **{f"social.{req.clips[0]}.{k}.{f}": v for k in ("youtube", "instagram")
                        for f, v in (req.post.get(k) or {}).items()})
    now = time.time()
    queued = []
    for i, cid in enumerate(req.clips):
        at = (req.start_at or now) + i * req.every_hours * 3600
        for platform in req.platforms:
            opts = {"privacy": req.privacy, "post": req.post if len(req.clips) == 1 else None}
            if at <= now + 60:
                _set_status(job_id, cid, platform, state="queued", pct=0, error=None, at=None)
                threading.Thread(target=_publish_one, args=(job_id, cid, platform, opts), daemon=True).start()
            elif platform == "youtube":         # YouTube: upload now, YouTube publishes at the time
                opts["publish_at"] = at
                _set_status(job_id, cid, platform, state="queued", pct=0, error=None, at=at)
                threading.Thread(target=_publish_one, args=(job_id, cid, platform, opts), daemon=True).start()
            else:                               # Instagram: our scheduler posts at the time
                db().schedule.insert_one({"job": job_id, "user_id": user["_id"], "clip": cid,
                                          "platform": platform, "at": at, "opts": opts})
                _set_status(job_id, cid, platform, state="waiting", pct=0, error=None, at=at)
            queued.append({"clip": cid, "platform": platform, "at": at})
    return {"queued": queued}
