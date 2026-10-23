"""Orchestrates the pipeline. Step names mirror Kaleidly's run panel:

    transcribe → hook-finder → clip-cutter → captions → render

Everything a clip editor needs (score breakdown, hooks, caption lines, trim)
is written to clips.json, and `rerender()` rebuilds one clip after edits.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

from .captions import build_ass, group_lines, lines_from_json, lines_to_json
from .hooks import candidate_to_dict, find_moments
from .media import ClipforgeError, extract_audio, fetch, preflight, probe, rms_envelope
from .render import render_clip
from .template import BrandTemplate
from .transcribe import Transcript, annotate_energy, sentences, transcribe

Progress = Callable[[str, str, str], None]   # (step, status, detail)


def _noop(step: str, status: str, detail: str = "") -> None:
    print(f"  {step:<12} {status:<28} {detail}", flush=True)


def run_pipeline(source: str, out_dir: str | Path, tpl: Optional[BrandTemplate] = None,
                 length: str = "auto", max_clips: int = 6, language: Optional[str] = None,
                 whisper_model: Optional[str] = None, progress: Progress = _noop) -> dict:
    tpl = tpl or BrandTemplate()
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    _reset_if_new_source(source, out)
    if source.startswith(("http://", "https://")):
        progress("download", "downloading video", "")
    src = fetch(source, out).resolve()
    info = probe(src)
    preflight(info)

    # 1. transcribe ---------------------------------------------------------
    tr_path = out / "transcript.json"
    wav = extract_audio(src, out / "audio16k.wav")
    if tr_path.exists():
        tr = Transcript.load(tr_path)
        progress("transcribe", "cached", f"{len(tr.words)} words")
    else:
        progress("transcribe", "transcribing audio", f"{info.duration / 60:.0f} min")
        tr = transcribe(wav, whisper_model, language,
                        on_progress=lambda pct: progress("transcribe", f"transcribing audio {pct}%",
                                                         f"{info.duration / 60:.0f} min"))
        tr.save(tr_path)
    if len(tr.words) < 25:
        raise ClipforgeError("no_speech", "Nobody speaks in this recording, so there's nothing to cut.")
    sents = sentences(tr)
    env, hop = rms_envelope(wav)
    annotate_energy(sents, env, hop)

    # 2. hook-finder --------------------------------------------------------
    progress("hook-finder", "scoring moments", f"{len(sents)} sentences")
    clips, n_raw = find_moments(sents, tr.language, length, max_clips,
                                progress=lambda m: progress("hook-finder", m, ""))
    progress("hook-finder", "done", f"{n_raw} candidates → {len(clips)} clips")

    # 3-5. cut, caption, render ---------------------------------------------
    manifest = {"source": str(src), "duration": info.duration, "language": tr.language,
                "template": tpl.to_dict(), "candidates_scored": n_raw, "clips": []}
    for k, c in enumerate(clips, 1):
        cid = f"clip_{k:02d}"
        words = [w for s in sents[c.start_idx: c.end_idx + 1] for w in s.words]
        lines = group_lines(words, c.start, c.end, tpl.captions.max_words)
        boundaries = [s.start - c.start for s in sents[c.start_idx + 1: c.end_idx + 1]]
        entry = candidate_to_dict(c) | {
            "id": cid, "file": f"{cid}.mp4", "lines": lines_to_json(lines),
            "boundaries": [round(b, 3) for b in boundaries],
        }
        progress("clip-cutter", "cutting vertical clips", f"{k} of {len(clips)}")
        progress("captions", "captions timed to every word", f"{len(lines)} lines")
        progress("render", f"rendering {tpl.size[0]}x{tpl.size[1]}", f"{k} of {len(clips)}")
        entry |= _render(entry, src, info, tpl, out)
        manifest["clips"].append(entry)
        _write(out, manifest)

    manifest["elapsed_s"] = round(time.time() - t0, 1)
    _write(out, manifest)
    return manifest


def _reset_if_new_source(source: str, out: Path) -> None:
    """Reusing an out dir for a different video: drop the cached download, audio and transcript."""
    marker = out / "source.txt"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == source:
        return
    for p in [*out.glob("source.*"), out / "audio16k.wav", out / "transcript.json"]:
        if p != marker:
            try:
                p.unlink(missing_ok=True)
            except PermissionError:
                raise ClipforgeError("folder_busy", f"{p} is open in another program (another clipforge run "
                                                    f"using {out.name}?). Use a different -o folder.") from None
    marker.write_text(source, encoding="utf-8")


def _render(entry: dict, src: Path, info, tpl: BrandTemplate, out: Path) -> dict:
    ass = out / f"{entry['id']}.ass"
    ass.write_text(build_ass(lines_from_json(entry["lines"]), entry["hook"], tpl,
                             entry.get("emphasis_words", [])), encoding="utf-8")
    meta = render_clip(str(src), info, tpl, entry["start"], entry["end"], ass,
                       out / entry["file"], entry.get("boundaries", []))
    return meta


def _write(out: Path, manifest: dict) -> None:
    (out / "clips.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def rerender(out_dir: str | Path, clip_id: str, *, hook: Optional[str] = None,
             trim: Optional[tuple[float, float]] = None, lines: Optional[list[dict]] = None,
             tpl: Optional[BrandTemplate] = None) -> dict:
    """Clip editor back-end: change hook text, caption lines, trim or style, then re-render."""
    out = Path(out_dir).resolve()
    manifest = json.loads((out / "clips.json").read_text(encoding="utf-8"))
    entry = next(c for c in manifest["clips"] if c["id"] == clip_id)
    tpl = tpl or BrandTemplate.from_dict(manifest["template"])
    if hook is not None:
        alts = entry.get("alt_hooks", [])
        if hook in alts:                              # picked an alternative: swap the old headline in
            alts[alts.index(hook)] = entry["hook"]
        entry["hook"] = hook
    if lines is not None:
        entry["lines"] = lines
    if trim is not None:                              # new absolute start/end in the source
        tr = Transcript.load(out / "transcript.json")
        old_start = entry["start"]
        entry["start"], entry["end"] = trim
        entry["duration"] = round(trim[1] - trim[0], 2)
        if lines is None:
            entry["lines"] = lines_to_json(group_lines(tr.words_between(*trim), trim[0], trim[1],
                                                       tpl.captions.max_words))
        entry["boundaries"] = [round(b + old_start - trim[0], 3) for b in entry.get("boundaries", [])
                               if 0 < b + old_start - trim[0] < trim[1] - trim[0]]
    manifest["template"] = tpl.to_dict()
    src = Path(manifest["source"])
    entry |= _render(entry, src, probe(src), tpl, out)
    _write(out, manifest)
    return entry
