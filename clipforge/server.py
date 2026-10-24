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
                     "template": json.loads(template) if template else None}}
    JOBS[job_id] = job
    _save(job)
    _start(job)
    return job


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
