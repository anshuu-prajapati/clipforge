"""HTTP API (FastAPI) + the web dashboard at /.

GET  /                           dashboard (static/index.html)
POST /jobs                       multipart file=… or form url=…, length, max_clips, template (JSON)
GET  /jobs                       recent jobs, newest first (survives restarts via clips.json on disk)
GET  /jobs/{id}                  status + per-step progress (download / transcribe / hook-finder / clip-cutter / captions / render)
GET  /jobs/{id}/clips            clips.json (scores, breakdown, hooks, caption lines)
GET  /jobs/{id}/files/{name}     rendered mp4 / thumbnail (?download=1 → save-as)
POST /jobs/{id}/clips/{clip_id}  JSON {hook?, trim?:[s,e], lines?, template?} → re-render
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

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


def _restore_jobs() -> None:
    """Rebuild finished jobs from disk so the dashboard keeps its history across restarts."""
    for d in ROOT.iterdir():
        meta = d / "job.json"
        if d.is_dir() and meta.exists():
            job = json.loads(meta.read_text(encoding="utf-8"))
            if job.get("state") in ("queued", "running"):
                job.update(state="failed", error={"code": "interrupted", "message": "Server restarted mid-job."})
            JOBS[job["id"]] = job


def _save(job: dict) -> None:
    (ROOT / job["id"] / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")


def _work(job_id: str, source: str, tpl: BrandTemplate, length: str, max_clips: int) -> None:
    job = JOBS[job_id]

    def progress(step: str, status: str, detail: str = "") -> None:
        job["steps"][step] = {"status": status, "detail": detail}
        job["current"] = step

    with GPU:
        try:
            job["state"] = "running"
            m = run_pipeline(source, ROOT / job_id, tpl, length, max_clips, progress=progress)
            job.update(state="done", clips=len(m["clips"]), elapsed_s=m["elapsed_s"])
        except ClipforgeError as e:
            job.update(state="failed", error={"code": e.code, "message": str(e)})
        except Exception as e:  # noqa: BLE001
            job.update(state="failed", error={"code": "error", "message": str(e)})
        _save(job)


_restore_jobs()


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "index.html")


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
    tpl = BrandTemplate.from_dict(json.loads(template)) if template else BrandTemplate()
    JOBS[job_id] = {"id": job_id, "state": "queued", "steps": {}, "current": None,
                    "source": url or (file.filename if file else ""), "length": length,
                    "max_clips": max_clips, "created": time.time()}
    _save(JOBS[job_id])
    threading.Thread(target=_work, args=(job_id, source, tpl, length, max_clips), daemon=True).start()
    return JOBS[job_id]


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
def get_file(job_id: str, name: str, download: bool = False):
    p = (ROOT / job_id / name).resolve()
    if p.parent != (ROOT / job_id).resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=name if download else None)


class Edit(BaseModel):
    hook: Optional[str] = None
    trim: Optional[tuple[float, float]] = None
    lines: Optional[list[dict]] = None
    template: Optional[dict] = None


@app.post("/jobs/{job_id}/clips/{clip_id}")
def edit_clip(job_id: str, clip_id: str, edit: Edit):
    tpl = BrandTemplate.from_dict(edit.template) if edit.template else None
    try:
        return rerender(ROOT / job_id, clip_id, hook=edit.hook, trim=edit.trim, lines=edit.lines, tpl=tpl)
    except StopIteration:
        raise HTTPException(404, "clip not found")
