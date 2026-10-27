"""ffprobe / ffmpeg helpers, input fetching and pre-flight checks."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class ClipforgeError(RuntimeError):
    """Error with a machine-readable code (mirrors Kaleidly's failure codes)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code


def run(cmd: list[str], cwd: str | Path | None = None) -> str:
    # explicit utf-8: on Windows the default codepage can't decode ffmpeg/yt-dlp output
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise ClipforgeError("ffmpeg_failed", f"{' '.join(cmd[:4])}…\n{p.stderr[-2500:]}")
    return p.stdout


@dataclass
class MediaInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def probe(path: str | Path) -> MediaInfo:
    out = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json",
                          "-show_streams", "-show_format", str(path)]))
    v = next((s for s in out["streams"] if s["codec_type"] == "video"), None)
    a = any(s["codec_type"] == "audio" for s in out["streams"])
    if v is None:
        raise ClipforgeError("no_video", "No video stream found.")
    num, den = (v.get("avg_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    w, h = int(v["width"]), int(v["height"])
    rot = int((v.get("tags") or {}).get("rotate", 0) or 0)
    for sd in v.get("side_data_list", []) or []:
        rot = int(sd.get("rotation", rot) or rot)
    if abs(rot) in (90, 270):
        w, h = h, w
    return MediaInfo(str(path), float(out["format"]["duration"]), w, h, fps or 30.0, a)


def fetch(source: str, work: Path) -> Path:
    """Local path → itself. URL → download with yt-dlp (YouTube, TikTok, IG, X, FB …)."""
    if not source.startswith(("http://", "https://")):
        p = Path(source)
        if not p.exists():
            raise ClipforgeError("not_found", f"{source} does not exist")
        return p
    if importlib.util.find_spec("yt_dlp"):
        ytdlp = [sys.executable, "-m", "yt_dlp"]   # works even when Scripts\*.exe launchers are blocked
    elif shutil.which("yt-dlp"):
        ytdlp = ["yt-dlp"]
    else:
        raise ClipforgeError("bad_link", "Install yt-dlp to use links, or pass a local file.")
    out = work / "source.%(ext)s"
    cmd = [*ytdlp, "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
           "--no-warnings", "-o", str(out)]
    cookies = os.environ.get("YTDLP_COOKIES")    # YouTube blocks cloud servers without a logged-in session
    if cookies and Path(cookies).is_file():
        cmd += ["--cookies", cookies]
    try:
        run([*cmd, source])
    except ClipforgeError as e:
        msg = str(e)
        if "confirm you" in msg and "not a bot" in msg:
            raise ClipforgeError("youtube_blocked",
                                 "YouTube blocked this server's download (it asks cloud servers to sign in). "
                                 + ("The uploaded YouTube cookies have expired — upload fresh ones in Settings. "
                                    if cookies else "The owner can upload YouTube cookies in Settings to fix this. ")
                                 + "Meanwhile, download the video and upload the file instead.") from None
        if "Private video" in msg or "Video unavailable" in msg or "members-only" in msg.lower():
            raise ClipforgeError("bad_link", "That video is private, removed or members-only.") from None
        lines = [ln for ln in msg.splitlines() if ln.startswith("ERROR")]
        raise ClipforgeError("bad_link", "Couldn't download that link: " + (lines[-1][7:] if lines else msg[-300:])) from None
    files = sorted(work.glob("source.*"))
    if not files:
        raise ClipforgeError("bad_link", "Download produced no file.")
    return files[0]


def preflight(info: MediaInfo, min_seconds: float = 20, min_height: int = 360,
              max_seconds: float = 3 * 3600) -> None:
    if not info.has_audio:
        raise ClipforgeError("no_audio", "Your video has no sound, and clips are made from what people say.")
    if info.duration < min_seconds:
        raise ClipforgeError("too_short", f"Needs at least {min_seconds:.0f}s of video.")
    if info.duration > max_seconds:
        raise ClipforgeError("too_long", "Recording is longer than allowed. Split it into parts.")
    if min(info.width, info.height) < min_height:
        raise ClipforgeError("low_quality", f"Needs at least {min_height}p.")


def extract_audio(src: Path, dst: Path, sr: int = 16000) -> Path:
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sr), str(dst)])
    return dst


def rms_envelope(wav: Path, hop: float = 0.05) -> tuple[np.ndarray, float]:
    """Loudness envelope (dBFS per hop) used as an 'energy' signal for moment scoring."""
    import wave
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    n = max(1, int(sr * hop))
    frames = x[: len(x) // n * n].reshape(-1, n)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-6)
    return db, hop
