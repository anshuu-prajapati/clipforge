"""Steps 3+5 — clip-cutter and render: cut, reframe, motion graphics, burn captions, mix audio."""
from __future__ import annotations

import copy
import functools
import os
import shutil
from pathlib import Path

from .media import ClipforgeError, MediaInfo, probe, run
from .reframe import analyze, crop_commands, enable_expr
from .template import BrandTemplate


def _hex(c: str) -> str:
    return "0x" + c.lstrip("#")


def _filter_path(p: str) -> str:
    """Path inside a filtergraph option: forward slashes, escaped drive colon (C\\:/...)."""
    return p.replace("\\", "/").replace(":", "\\:")


def _punch_expr(boundaries: list[float], dur: float, scale: float) -> str:
    """Alternate 1.0x / scale zoom on every other sentence span (jump-cut punch-ins)."""
    edges = [b for b in sorted(set(boundaries)) if 0.5 < b < dur - 0.5][:40]
    spans = list(zip([0.0] + edges, edges + [dur]))
    on = [f"between(t,{a:.3f},{b:.3f})" for k, (a, b) in enumerate(spans) if k % 2 == 1]
    if not on:
        return "1"
    return f"if(gt({'+'.join(on)},0),{scale},1)"


def _fit_chain(src: str, tpl: BrandTemplate, out: str) -> str:
    """Whole frame centred over a blurred, darkened copy of itself (or black)."""
    W, H = tpl.size
    lay = tpl.layout
    fg_w = int(round(W * lay.fit_zoom / 2) * 2)
    if lay.blur_background:
        # blur a quarter-size copy and scale it back up: same look, a fraction of the cost
        bg = (f"[{src}]split[fa][fb];[fa]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,"
              f"crop={W // 4}:{H // 4},boxblur=6:3,eq=brightness=-0.10:saturation=1.15,"
              f"scale={W}:{H}:flags=bilinear[fbg];")
    else:
        bg = f"[{src}]null[fb];color=black:s={W}x{H}:r=30[fbg];"
    return (bg + f"[fb]scale={fg_w}:-2:flags=lanczos[ffg];"
            f"[fbg][ffg]overlay=x='(W-w)/2':y='(H-h)/2':shortest=1,setsar=1[{out}]")


def _video_chain(info: MediaInfo, tpl: BrandTemplate, start: float, end: float,
                 boundaries: list[float], cmd_path: Path) -> tuple[str, str]:
    """Return (filter_complex part producing [vbase], framing actually used)."""
    W, H = tpl.size
    mo = tpl.motion
    dur = end - start
    z = _punch_expr(boundaries, dur, mo.punch_scale) if mo.punch_in else "1"
    punch = (f",scale=w='trunc({W}*{z}/2)*2':h='trunc({H}*{z}/2)*2':eval=frame,"
             f"crop={W}:{H}:'(iw-{W})/2':'(ih-{H})/2'") if z != "1" else ""

    src_ar, dst_ar = info.width / info.height, W / H
    if tpl.layout.framing == "fill" and abs(src_ar - dst_ar) <= 0.01:
        return f"[0:v]scale={W}:{H},setsar=1{punch}[vbase]", "fill"
    if tpl.layout.framing != "fill" or src_ar < dst_ar:
        return _fit_chain("0:v", tpl, "vfit") + f";[vfit]null{punch}[vbase]", "fit"

    # fill: shot-aware — moving crop on shots with people, whole frame on the rest
    crop_w = int(round(info.height * dst_ar / 2) * 2)
    plan = analyze(info.path, start, end, crop_w / info.width)
    fills = plan.fill_shots
    n_fill, n_all = len(fills), len(plan.shots)
    label = f"smart ({n_fill}/{n_all} shots follow people)"
    if not fills:
        return _fit_chain("0:v", tpl, "vfit") + f";[vfit]null{punch}[vbase]", label
    x0, cmds = crop_commands(plan, info.width, crop_w)
    cmd_path.write_text(cmds, encoding="utf-8")
    crop = (f"sendcmd=f={cmd_path.name},crop@rf=w={crop_w}:h={info.height}:x={x0}:y=0,"
            f"scale={W}:{H}:flags=lanczos,setsar=1")
    if n_fill == n_all:
        return f"[0:v]{crop}{punch}[vbase]", label
    return (f"[0:v]split[vs0][vs1];[vs0]{crop}[vfill];" + _fit_chain("vs1", tpl, "vfit") +
            f";[vfit][vfill]overlay=0:0:enable='{enable_expr(fills)}'{punch}[vbase]"), label


COVER_T0, COVER_STEP, COVER_N = 0.4, 0.5, 7      # thumbnail candidates at 0.4 s, 0.9 s … 3.4 s
# within Instagram Reels / YouTube Shorts upload specs: AAC 48 kHz ≤128 kb/s stereo, TV-range H.264, faststart
DELIVERY = ("-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-color_range", "tv",
            "-movflags", "+faststart")


def render_clip(src: str, info: MediaInfo, tpl: BrandTemplate, start: float, end: float,
                ass_path: Path, out_path: Path, boundaries: list[float],
                cover_ass: Path | None = None) -> dict:
    W, H = tpl.size
    dur = end - start
    inputs = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]
    vchain, framing = _video_chain(info, tpl, start, end, boundaries,
                                   ass_path.with_name(f"{out_path.stem}.crop.txt"))
    fonts = f":fontsdir='{_filter_path(tpl.fonts_dir)}'" if tpl.fonts_dir else ""
    g = [vchain, f"[vbase]ass='{ass_path.name}'{fonts}[vcap]"]
    cover_out: list[str] = []
    if cover_ass:   # same framing, hook only, no captions: a few candidate thumbnails in the first seconds
        g[1:2] = ["[vbase]split[vmain][vcov]", f"[vmain]ass='{ass_path.name}'{fonts}[vcap]",
                  f"[vcov]select='between(t,{COVER_T0},{COVER_T0 + COVER_N * COVER_STEP})*"
                  f"(isnan(prev_selected_t)+gte(t-prev_selected_t,{COVER_STEP}))',"
                  f"ass='{cover_ass.name}'{fonts}[vcovout]"]
        cover_out = ["-map", "[vcovout]", "-fps_mode", "vfr", "-frames:v", str(COVER_N), "-q:v", "2",
                     f"{out_path.stem}.cand_%d.jpg"]
    last = "vcap"
    if tpl.motion.progress_bar:
        h = tpl.motion.progress_height
        g.append(f"[{last}]drawbox=x=0:y={H - h}:w='{W}*t/{dur:.3f}':h={h}:"
                 f"color={_hex(tpl.motion.progress_color)}@0.95:t=fill[vpb]")
        last = "vpb"
    n = 1
    if tpl.logo.path:
        inputs += ["-i", tpl.logo.path]
        lw = int(W * tpl.logo.width_pct)
        m = int(W * 0.045)
        pos = {"top-left": (f"{m}", f"{m}"), "top-right": (f"W-w-{m}", f"{m}"),
               "bottom-left": (f"{m}", f"H-h-{m}"), "bottom-right": (f"W-w-{m}", f"H-h-{m}")}[tpl.logo.position]
        g.append(f"[{n}:v]scale={lw}:-1,format=rgba,colorchannelmixer=aa={tpl.logo.opacity}[lg]")
        g.append(f"[{last}][lg]overlay=x={pos[0]}:y={pos[1]}[vlogo]")
        last = "vlogo"
        n += 1
    # limited ("TV") range 4:2:0 — full-range yuvj420p sources otherwise leak through, and Instagram rejects them
    g.append(f"[{last}]scale=out_range=tv,format=yuv420p,fade=t=in:st=0:d=0.15[vout]")

    # audio: loudness-normalise speech; optional music bed ducked under the voice.
    # loudnorm resamples to 192 kHz internally, so resample back to 48 kHz (Instagram's maximum).
    g.append(f"[0:a]aresample=48000,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
             f"afade=t=out:st={max(0, dur - 0.3):.3f}:d=0.3[voice]")
    aout = "voice"
    if tpl.music.path:
        inputs += ["-stream_loop", "-1", "-i", tpl.music.path]
        g.append(f"[{n}:a]aresample=48000,volume={tpl.music.volume},atrim=0:{dur:.3f}[mus]")
        if tpl.music.duck:
            g.append("[voice]asplit[v1][v2]")
            g.append("[mus][v2]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[mduck]")
            g.append("[v1][mduck]amix=inputs=2:duration=first:normalize=0[aout]")
        else:
            g.append("[voice][mus]amix=inputs=2:duration=first:normalize=0[aout]")
        aout = "aout"
        n += 1

    tmp = out_path.with_suffix(".body.mp4") if (tpl.intro_outro.intro_path or tpl.intro_outro.outro_path) else out_path
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(g),
         "-map", "[vout]", "-map", f"[{aout}]", "-r", "30", *_video_codec(), *DELIVERY,
         "-t", f"{dur:.3f}", tmp.name, *cover_out], cwd=ass_path.parent)
    if tmp != out_path:
        _concat_intro_outro(tpl, ass_path.parent / tmp.name, out_path)
        (ass_path.parent / tmp.name).unlink(missing_ok=True)
    meta = {"framing": framing}
    cands = sorted(ass_path.parent.glob(f"{out_path.stem}.cand_*.jpg"), key=lambda p: int(p.stem.rsplit("_", 1)[1]))
    if cands:
        from .social import best_frame
        i = best_frame(cands)
        cover = out_path.with_suffix(".cover.jpg")
        cands[i].replace(cover)
        for p in cands:
            p.unlink(missing_ok=True)
        meta |= {"thumbnail": cover.name, "cover_ms": int((COVER_T0 + i * COVER_STEP) * 1000)}
    else:
        thumb = out_path.with_suffix(".jpg")
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{min(1.2, dur / 2):.2f}", "-i", str(out_path),
             "-frames:v", "1", "-q:v", "3", str(thumb)])
        meta["thumbnail"] = thumb.name
    return meta


def render_preview(tpl: BrandTemplate, hook: str, lines_json: list[dict] | None, emphasis: list[str],
                   src: str | None, start: float, out: Path) -> None:
    """Single frame for the brand editor, rendered with the real ASS/libass pipeline.

    Shows the moment the hook has popped in and a caption is on screen. Framing is a centre crop
    (final clips follow the people in each shot)."""
    from .captions import build_ass, group_lines
    from .transcribe import Word

    W, H = tpl.size
    if lines_json:
        words = [Word(**w) for ln in lines_json for w in ln["words"]]
    else:
        sample = "This is how your captions will look on every single clip".split()
        words = [Word(w, 0.5 + i * 0.3, 0.5 + i * 0.3 + 0.28) for i, w in enumerate(sample)]
    lines = group_lines(words, 0, max((w.end for w in words), default=10), tpl.captions.max_words)
    first = next((ln for ln in lines if ln.end > 0.5), None)
    t = max(0.6, (first.words[min(1, len(first.words) - 1)].start + 0.2) if first else 1.2)
    shown = copy.deepcopy(tpl)                   # keep the hook on screen at t even if speech starts late
    shown.headline.duration_s = max(tpl.headline.duration_s, t + 0.5)

    ass = out.with_suffix(".ass")
    ass.write_text(build_ass(lines, hook, shown, emphasis), encoding="utf-8")
    fonts = f":fontsdir='{_filter_path(tpl.fonts_dir)}'" if tpl.fonts_dir else ""
    if src and Path(src).exists():
        inputs = ["-ss", f"{start + t:.3f}", "-i", str(src)]
        if tpl.layout.framing == "fill":
            base = (f"[0:v]crop='min(iw,ih*{W}/{H})':'min(ih,iw*{H}/{W})',"
                    f"scale={W}:{H}:flags=lanczos,setsar=1[vb]")
        else:
            base = _fit_chain("0:v", tpl, "vb")
    else:                                         # no project yet: neutral studio backdrop
        inputs = ["-f", "lavfi", "-i", f"gradients=s={W}x{H}:c0=0x3b3934:c1=0x111111:d=1:speed=0"]
        base = "[0:v]setsar=1[vb]"
    g = [base, f"[vb]setpts=PTS-STARTPTS+{t:.3f}/TB,ass='{ass.name}'{fonts}[vc]"]
    last = "vc"
    if tpl.motion.progress_bar:
        h = tpl.motion.progress_height
        g.append(f"[{last}]drawbox=x=0:y={H - h}:w={int(W * 0.35)}:h={h}:"
                 f"color={_hex(tpl.motion.progress_color)}@0.95:t=fill[vpb]")
        last = "vpb"
    if tpl.logo.path and Path(tpl.logo.path).exists():
        inputs += ["-i", tpl.logo.path]
        lw, m = int(W * tpl.logo.width_pct), int(W * 0.045)
        pos = {"top-left": (f"{m}", f"{m}"), "top-right": (f"W-w-{m}", f"{m}"),
               "bottom-left": (f"{m}", f"H-h-{m}"), "bottom-right": (f"W-w-{m}", f"H-h-{m}")}[tpl.logo.position]
        g.append(f"[1:v]scale={lw}:-1,format=rgba,colorchannelmixer=aa={tpl.logo.opacity}[lg]")
        g.append(f"[{last}][lg]overlay=x={pos[0]}:y={pos[1]}[vlogo]")
        last = "vlogo"
    try:
        run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(g), "-map", f"[{last}]",
             "-frames:v", "1", "-q:v", "3", out.name], cwd=out.parent)
    finally:
        ass.unlink(missing_ok=True)


def _concat_intro_outro(tpl: BrandTemplate, body: Path, out: Path) -> None:
    W, H = tpl.size
    parts = [p for p in (tpl.intro_outro.intro_path, str(body), tpl.intro_outro.outro_path) if p]
    args, g, labels = [], [], ""
    for i, p in enumerate(parts):
        is_img = Path(p).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
        args += (["-loop", "1", "-t", "2", "-i", p] if is_img else ["-i", p])
        info = None if is_img else probe(p)
        d = 2.0 if is_img else info.duration
        g.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
                 f"setsar=1,fps=30,format=yuv420p[v{i}]")
        if info and info.has_audio:
            g.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        else:
            g.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{d:.3f}[a{i}]")
        labels += f"[v{i}][a{i}]"
    g.append(f"{labels}concat=n={len(parts)}:v=1:a=1[v][a]")
    run(["ffmpeg", "-y", "-v", "error", *args, "-filter_complex", ";".join(g), "-map", "[v]", "-map", "[a]",
         *_video_codec(), *DELIVERY, str(out)])


@functools.cache
def _video_codec() -> tuple[str, ...]:
    """NVIDIA hardware encoder when it works (several × faster), else libx264. CLIPFORGE_NVENC=0 disables."""
    if os.environ.get("CLIPFORGE_NVENC") != "0":
        try:
            run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=256x256:d=0.1", "-c:v", "h264_nvenc",
                 "-f", "null", "-"])
            return ("-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0",
                    "-pix_fmt", "yuv420p")
        except ClipforgeError:
            pass
    return ("-c:v", "libx264", "-preset", "veryfast", "-crf", "20")


def check_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH")
