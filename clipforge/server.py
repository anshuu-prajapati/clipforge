"""HTTP API (FastAPI) + the web dashboard at /.

GET  /                           dashboard (static/index.html)
POST /jobs                       multipart file=… or form url=…, length, max_clips, template (JSON)
GET  /jobs                       recent jobs, newest first (survives restarts via job.json on disk)
GET  /jobs/{id}                  status + per-step progress (download / transcribe / hook-finder / clip-cutter / captions / render)
GET  /jobs/{id}/clips            clips.json (scores, breakdown, hooks, caption lines)
GET  /jobs/{id}/files/{name}     rendered mp4 / thumbnail / source (Range requests for seeking; ?download=1 → save-as)
GET  /jobs/{id}/zip?ids=a,b      several clips as one .zip
POST /jobs/{id}/clips/{clip_id}  JSON {hook?, trim?:[s,e], lines?, template?} → re-render

Jobs interrupted by a server restart are resumed on startup; finished steps (download, transcript,
moment picks, rendered clips) are cached on disk, so only the remaining work runs again.
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
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .hooks import parse_length
from .media import ClipforgeError
from .pipeline import rerender, run_pipeline
from .template import BrandTemplate

ROOT = Path(os.environ.get("CLIPFORGE_DATA", "clipforge_jobs")).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).parent / "static"
JOBS: dict[str, dict] = {}
GPU = threading.Lock()          # one pipeline at a time: whisper + ffmpeg already use the whole machine
app = FastAPI(title="clipforge")


def _save(job: dict) -> None:
    (ROOT / job["id"] / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")


def _start(job: dict) -> None:
    threading.Thread(target=_work, args=(job["id"],), daemon=True).start()


def _restore_jobs() -> None:
    """Reload job history; resume jobs that were queued/running when the server stopped."""
    for d in ROOT.iterdir():
        meta = d / "job.json"
        if not (d.is_dir() and meta.exists()):
            continue
        job = json.loads(meta.read_text(encoding="utf-8"))
        JOBS[job["id"]] = job
        url = job.get("source", "")
        job.setdefault("origin", "YouTube" if re.search(r"youtu\.?be", url) else "Link" if url.startswith("http") else "Upload")
        if job.get("state") == "done" and url.startswith("http") and job.get("title") in (None, url):
            job.setdefault("title", url)                 # older jobs: look the real title up in the background
            threading.Thread(target=_video_meta, args=(job, url), daemon=True).start()
        if job.get("state") in ("queued", "running"):
            if job.get("input"):
                job.update(state="queued", resumed=True, steps={}, current=None)
                _start(job)
            else:   # created by an older version without saved inputs
                job.update(state="failed", error={"code": "interrupted", "message": "Server restarted mid-job."})
                _save(job)


def _video_meta(job: dict, url: str) -> None:
    """Best-effort title/duration for the dashboard (YouTube etc.)."""
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as y:
            info = y.extract_info(url, download=False)
        job.update(title=info.get("title") or job["title"], duration=info.get("duration"))
        _save(job)
    except Exception:  # noqa: BLE001
        pass


def _work(job_id: str) -> None:
    job = JOBS[job_id]
    inp = job["input"]

    def progress(step: str, status: str, detail: str = "") -> None:
        job["steps"][step] = {"status": status, "detail": detail}
        job["current"] = step

    if inp["source"].startswith(("http://", "https://")) and not job.get("duration"):
        _video_meta(job, inp["source"])
    with GPU:
        try:
            job["state"] = "running"
            _save(job)
            tpl = BrandTemplate.from_dict(inp["template"]) if inp.get("template") else BrandTemplate()
            m = run_pipeline(inp["source"], ROOT / job_id, tpl, inp["length"], inp["max_clips"], progress=progress)
            job.update(state="done", clips=len(m["clips"]), elapsed_s=m["elapsed_s"],
                       duration=job.get("duration") or m["duration"])
        except ClipforgeError as e:
            job.update(state="failed", error={"code": e.code, "message": str(e)})
        except Exception as e:  # noqa: BLE001
            job.update(state="failed", error={"code": "error", "message": str(e)})
        _save(job)


@app.on_event("startup")
def _startup() -> None:
    _restore_jobs()


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/jobs")
async def create_job(file: Optional[UploadFile] = File(None), url: Optional[str] = Form(None),
                     length: str = Form("auto"), max_clips: int = Form(6),
                     template: Optional[str] = Form(None)):
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
    job = {"id": job_id, "state": "queued", "steps": {}, "current": None,
           "source": url or (file.filename if file else ""), "title": (file.filename if file else url),
           "origin": origin, "length": length, "max_clips": max_clips, "created": time.time(),
           "input": {"source": source, "length": length, "max_clips": max_clips,
                     "template": json.loads(template) if template else _brand().to_dict()}}
    JOBS[job_id] = job
    _save(job)
    _start(job)
    return job


# =========================================================================== brand template
BRAND = ROOT / "brand.json"
BRAND_ASSETS = ROOT / "brand_assets"
_FONT_FILES = {"Arial Black": "ariblk.ttf", "Impact": "impact.ttf", "Segoe UI Black": "seguibl.ttf",
               "Segoe UI": "segoeui.ttf", "Bahnschrift": "bahnschrift.ttf", "Arial": "arial.ttf",
               "Verdana": "verdana.ttf", "Trebuchet MS": "trebuc.ttf", "Tahoma": "tahoma.ttf",
               "Georgia": "georgia.ttf", "Comic Sans MS": "comic.ttf", "Nirmala UI": "Nirmala.ttc",
               "Poppins": "Poppins-Bold.ttf", "Montserrat": "Montserrat-Bold.ttf"}


def _brand() -> BrandTemplate:
    """The saved brand style; new videos are rendered with it."""
    return BrandTemplate.load(BRAND) if BRAND.exists() else BrandTemplate()


def _fonts() -> list[str]:
    win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if not win.exists():
        return list(_FONT_FILES)
    found = [name for name, f in _FONT_FILES.items() if (win / f).exists()]
    return found or list(_FONT_FILES)


@app.get("/brand")
def get_brand():
    return {"template": _brand().to_dict(), "default": BrandTemplate().to_dict(), "fonts": _fonts(),
            "saved": BRAND.exists()}


@app.put("/brand")
def put_brand(template: dict):
    try:
        tpl = BrandTemplate.from_dict(template)
        tpl.size                                  # validates aspect
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Invalid template: {e}")
    tpl.save(BRAND)
    return {"template": tpl.to_dict()}


class PreviewReq(BaseModel):
    template: dict
    job: Optional[str] = None
    clip: Optional[str] = None
    hook: Optional[str] = None


@app.post("/brand/preview")
def brand_preview(req: PreviewReq):
    """One frame rendered with the real caption/hook renderer — what the clip will look like."""
    from .render import render_preview
    tpl = BrandTemplate.from_dict(req.template)
    src, start, lines, emph, hook = None, 0.0, None, [], req.hook
    if req.job and req.clip:
        try:
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
    from fastapi.responses import Response
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/brand/asset")
async def brand_asset(file: UploadFile = File(...)):
    """Upload a logo / music / intro / outro file; returns the path to put in the template."""
    BRAND_ASSETS.mkdir(exist_ok=True)
    name = re.sub(r"[^\w.-]", "_", Path(file.filename or "asset").name)[-80:]
    dst = BRAND_ASSETS / f"{uuid.uuid4().hex[:6]}_{name}"
    with dst.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"path": str(dst), "name": file.filename}


@app.get("/jobs")
def list_jobs():
    return sorted(JOBS.values(), key=lambda j: -j.get("created", 0))[:50]


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404)
    return JOBS[job_id]


@app.get("/jobs/{job_id}/clips")
def get_clips(job_id: str):
    p = ROOT / job_id / "clips.json"
    if not p.exists():
        raise HTTPException(404, "not ready")
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/jobs/{job_id}/files/{name}")
def get_file(job_id: str, name: str, request: Request, download: bool = False):
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
def zip_clips(job_id: str, ids: str):
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


# =========================================================================== publishing
PUB_LOCK = threading.Lock()


def _jread(p: Path, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _jwrite(p: Path, data) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _clip(job_id: str, clip_id: str) -> tuple[dict, dict]:
    m = get_clips(job_id)
    c = next((c for c in m["clips"] if c["id"] == clip_id), None)
    if not c:
        raise HTTPException(404, "clip not found")
    return m, c


def _social_for(job_id: str, clip_id: str, regenerate: bool = False) -> dict:
    """Post copy + cover for a clip, generated once and cached in social.json."""
    from . import social
    d = ROOT / job_id
    with PUB_LOCK:
        cache = _jread(d / "social.json", {})
    m, c = _clip(job_id, clip_id)
    s = cache.get(clip_id)
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
    s["clip_v"] = [c["hook"], c["start"], c["end"]]
    with PUB_LOCK:
        cache = _jread(d / "social.json", {})
        cache[clip_id] = s
        _jwrite(d / "social.json", cache)
    return s


def _set_status(job_id: str, clip_id: str, platform: str, **kw) -> None:
    with PUB_LOCK:
        p = ROOT / job_id / "publish.json"
        data = _jread(p, {})
        data.setdefault(clip_id, {}).setdefault(platform, {}).update(kw, updated=time.time())
        _jwrite(p, data)


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
    """Runs due items from schedule.json every 20 s (Instagram has no native scheduling)."""
    while True:
        due = []
        with PUB_LOCK:
            q = _jread(ROOT / "schedule.json", [])
            now = time.time()
            due = [x for x in q if x["at"] <= now]
            if due:
                _jwrite(ROOT / "schedule.json", [x for x in q if x["at"] > now])
        for x in due:
            threading.Thread(target=_publish_one, args=(x["job"], x["clip"], x["platform"], x["opts"]),
                             daemon=True).start()
        time.sleep(20)


@app.on_event("startup")
def _start_scheduler() -> None:
    for p in ROOT.glob("*/publish.json"):        # uploads cut off by a restart: mark them so they can be retried
        data = _jread(p, {})
        for plats in data.values():
            for st in plats.values():
                if st.get("state") in ("queued", "running"):
                    st.update(state="failed", error="Interrupted by a server restart — publish again.")
        _jwrite(p, data)
    threading.Thread(target=_scheduler, daemon=True).start()


@app.get("/publish/accounts")
def publish_accounts():
    from . import publish
    return publish.accounts()


@app.post("/publish/youtube/connect")
def youtube_connect():
    from . import publish
    try:
        return publish.youtube_connect()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


@app.get("/jobs/{job_id}/publish")
def publish_state(job_id: str):
    d = ROOT / job_id
    with PUB_LOCK:
        return {"social": _jread(d / "social.json", {}), "status": _jread(d / "publish.json", {}),
                "scheduled": [x for x in _jread(ROOT / "schedule.json", []) if x["job"] == job_id]}


@app.post("/jobs/{job_id}/clips/{clip_id}/social")
def clip_social(job_id: str, clip_id: str, regenerate: bool = False):
    return _social_for(job_id, clip_id, regenerate)


class PublishReq(BaseModel):
    clips: list[str]
    platforms: list[str]
    privacy: str = "public"
    start_at: Optional[float] = None           # unix seconds; None = now
    every_hours: float = 0                     # spacing between clips when several are scheduled
    post: Optional[dict] = None                # edited copy (single clip only)


@app.post("/jobs/{job_id}/publish")
def publish_clips(job_id: str, req: PublishReq):
    if not set(req.platforms) <= {"youtube", "instagram"} or not req.platforms:
        raise HTTPException(400, "platforms must be youtube and/or instagram")
    for cid in req.clips:
        _clip(job_id, cid)
    if req.post and len(req.clips) == 1:       # save edits so the dialog shows them next time
        with PUB_LOCK:
            cache = _jread(ROOT / job_id / "social.json", {})
            s = cache.get(req.clips[0], {})
            for k in ("youtube", "instagram"):
                if k in req.post:
                    s.setdefault(k, {}).update(req.post[k])
            cache[req.clips[0]] = s
            _jwrite(ROOT / job_id / "social.json", cache)
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
                with PUB_LOCK:
                    q = _jread(ROOT / "schedule.json", [])
                    q.append({"job": job_id, "clip": cid, "platform": platform, "at": at, "opts": opts})
                    _jwrite(ROOT / "schedule.json", q)
                _set_status(job_id, cid, platform, state="waiting", pct=0, error=None, at=at)
            queued.append({"clip": cid, "platform": platform, "at": at})
    return {"queued": queued}


class Edit(BaseModel):
    hook: Optional[str] = None
    trim: Optional[tuple[float, float]] = None
    lines: Optional[list[dict]] = None
    template: Optional[dict] = None


@app.post("/jobs/{job_id}/clips/{clip_id}")
def edit_clip(job_id: str, clip_id: str, edit: Edit):
    tpl = BrandTemplate.from_dict(edit.template) if edit.template else None
    with GPU:
        try:
            return rerender(ROOT / job_id, clip_id, hook=edit.hook, trim=edit.trim, lines=edit.lines, tpl=tpl)
        except StopIteration:
            raise HTTPException(404, "clip not found")
