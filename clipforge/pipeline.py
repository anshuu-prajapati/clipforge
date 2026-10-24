"""Orchestrates the pipeline. Step names mirror Kaleidly's run panel:

    transcribe → hook-finder → clip-cutter → captions → render

Everything a clip editor needs (score breakdown, hooks, caption lines, trim)
is written to clips.json, and `rerender()` rebuilds one clip after edits.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Callable, Optional

from .captions import build_ass, build_cover_ass, group_lines, lines_from_json, lines_to_json
from .hooks import Candidate, candidate_to_dict, find_moments
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
    wav = out / "audio16k.wav"
    if not wav.exists():
        extract_audio(src, wav)
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
    moments = out / "moments.json"
    key = {"length": length, "max_clips": max_clips}
    cached = json.loads(moments.read_text(encoding="utf-8")) if moments.exists() else {}
    if cached.get("key") == key:
        clips = [Candidate(**{f.name: d[f.name] for f in fields(Candidate)}) for d in cached["clips"]]
        n_raw = cached["n_raw"]
        progress("hook-finder", "cached", f"{len(clips)} clips")
    else:
        progress("hook-finder", "scoring moments", f"{len(sents)} sentences")
        clips, n_raw = find_moments(sents, tr.language, length, max_clips,
                                    progress=lambda m: progress("hook-finder", m, ""))
        progress("hook-finder", "done", f"{n_raw} candidates → {len(clips)} clips")
        if all(c.scorer == "claude" for c in clips):   # heuristic is cheap; retry Claude next time
            moments.write_text(json.dumps({"key": key, "n_raw": n_raw, "clips": [asdict(c) for c in clips]},
                                          ensure_ascii=False), encoding="utf-8")

    # 3-5. cut, caption, render ---------------------------------------------
    prev = _previous_renders(out, src)
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
        old = prev.get(cid)
        if old and (old["start"], old["end"], old["hook"]) == (entry["start"], entry["end"], entry["hook"]) \
                and old.get("rendered_with", {}).get("tpl") == _tpl_hash(tpl):
            progress("render", "already rendered", f"{k} of {len(clips)}")
            entry |= {k2: old[k2] for k2 in ("framing", "thumbnail", "rendered_with") if k2 in old}
        else:
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
    for p in [*out.glob("source.*"), out / "audio16k.wav", out / "transcript.json",
              out / "moments.json", out / "clips.json"]:
        if p != marker:
            try:
                p.unlink(missing_ok=True)
            except PermissionError:
                raise ClipforgeError("folder_busy", f"{p} is open in another program (another clipforge run "
                                                    f"using {out.name}?). Use a different -o folder.") from None
    marker.write_text(source, encoding="utf-8")


def _tpl_hash(tpl: BrandTemplate) -> str:
    return hashlib.sha1(json.dumps(tpl.to_dict(), sort_keys=True).encode()).hexdigest()[:12]


def _previous_renders(out: Path, src: Path) -> dict[str, dict]:
    """Clips from an earlier (interrupted) run of the same source whose files still exist."""
    p = out / "clips.json"
    if not p.exists():
        return {}
    m = json.loads(p.read_text(encoding="utf-8"))
    if Path(m.get("source", "")) != src:
        return {}
    return {c["id"]: c for c in m["clips"]
            if (out / c["file"]).exists() and (out / c.get("thumbnail", "-")).exists()}


def _render(entry: dict, src: Path, info, tpl: BrandTemplate, out: Path) -> dict:
    ass = out / f"{entry['id']}.ass"
    ass.write_text(build_ass(lines_from_json(entry["lines"]), entry["hook"], tpl,
                             entry.get("emphasis_words", [])), encoding="utf-8")
    cover_ass = out / f"{entry['id']}.cover.ass"
    cover_ass.write_text(build_cover_ass(entry["hook"], tpl, entry.get("emphasis_words", [])), encoding="utf-8")
    meta = render_clip(str(src), info, tpl, entry["start"], entry["end"], ass,
                       out / entry["file"], entry.get("boundaries", []), cover_ass)
    # what the dashboard needs to know whether an export must re-render
    meta["rendered_with"] = {"aspect": tpl.layout.aspect, "resolution": tpl.resolution,
                             "captions": tpl.captions.show, "minimal": tpl.captions.animation == "none",
                             "max_words": tpl.captions.max_words, "tpl": _tpl_hash(tpl)}
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
    regroup = tpl.captions.max_words != entry.get("rendered_with", {}).get("max_words", tpl.captions.max_words)
    if trim is not None:                              # new absolute start/end in the source
        old_start = entry["start"]
        entry["start"], entry["end"] = trim
        entry["duration"] = round(trim[1] - trim[0], 2)
        entry["boundaries"] = [round(b + old_start - trim[0], 3) for b in entry.get("boundaries", [])
                               if 0 < b + old_start - trim[0] < trim[1] - trim[0]]
        regroup = True
    if regroup and lines is None:                     # new range or words-per-line → rebuild caption lines
        tr = Transcript.load(out / "transcript.json")
        s, e = entry["start"], entry["end"]
        entry["lines"] = lines_to_json(group_lines(tr.words_between(s, e), s, e, tpl.captions.max_words))
    manifest["template"] = tpl.to_dict()
    src = Path(manifest["source"])
    entry |= _render(entry, src, probe(src), tpl, out)
    _write(out, manifest)
    return entry
