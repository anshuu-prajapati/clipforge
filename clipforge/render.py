"""Steps 3+5 — clip-cutter and render: cut, reframe, motion graphics, burn captions, mix audio."""
from __future__ import annotations

import shutil
from pathlib import Path

from .media import MediaInfo, probe, run
from .reframe import crop_keyframes, face_track, x_expr
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


def _video_chain(info: MediaInfo, tpl: BrandTemplate, start: float, end: float,
                 boundaries: list[float]) -> tuple[str, str]:
    """Return (filter_complex part producing [vbase], framing actually used)."""
    W, H = tpl.size
    lay, mo = tpl.layout, tpl.motion
    dur = end - start
    z = _punch_expr(boundaries, dur, mo.punch_scale) if mo.punch_in else "1"
    punch = (f",scale=w='trunc({W}*{z}/2)*2':h='trunc({H}*{z}/2)*2':eval=frame,"
             f"crop={W}:{H}:'(iw-{W})/2':'(ih-{H})/2'") if z != "1" else ""

    src_ar, dst_ar = info.width / info.height, W / H
    framing = lay.framing
    if framing == "fill" and src_ar > dst_ar + 0.01:
        crop_w = int(round(info.height * dst_ar / 2) * 2)
        keys = crop_keyframes(face_track(info.path, start, end), boundaries)
        xe = x_expr(keys, info.width, crop_w)
        chain = (f"[0:v]crop={crop_w}:{info.height}:'{xe}':0,scale={W}:{H}:flags=lanczos,"
                 f"setsar=1{punch}[vbase]")
        return chain, f"fill ({len(keys)} shot positions)"
    if framing == "fill" and abs(src_ar - dst_ar) <= 0.01:
        return f"[0:v]scale={W}:{H},setsar=1{punch}[vbase]", "fill"

    # fit: whole frame over blurred copy (or black)
    fg_w = int(round(W * lay.fit_zoom / 2) * 2)
    if lay.blur_background:
        bg = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"boxblur=24:6,eq=brightness=-0.10:saturation=1.15[bg];")
    else:
        bg = f"[0:v]null[b];color=black:s={W}x{H}:r=30[bg];"
    fg_scale = f"[b]scale={fg_w}:-2:flags=lanczos"
    if z != "1":
        fg_scale = f"[b]scale=w='trunc({fg_w}*{z}/2)*2':h=-2:eval=frame:flags=lanczos"
    chain = (bg + fg_scale + "[fg];[bg][fg]overlay=x='(W-w)/2':y='(H-h)/2':shortest=1,setsar=1[vbase]")
    return chain, "fit"


def render_clip(src: str, info: MediaInfo, tpl: BrandTemplate, start: float, end: float,
                ass_path: Path, out_path: Path, boundaries: list[float]) -> dict:
    W, H = tpl.size
    dur = end - start
    inputs = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]
    vchain, framing = _video_chain(info, tpl, start, end, boundaries)
    fonts = f":fontsdir='{_filter_path(tpl.fonts_dir)}'" if tpl.fonts_dir else ""
    g = [vchain, f"[vbase]ass='{ass_path.name}'{fonts}[vcap]"]
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
    g.append(f"[{last}]format=yuv420p,fade=t=in:st=0:d=0.15[vout]")

    # audio: loudness-normalise speech; optional music bed ducked under the voice
    g.append(f"[0:a]aresample=48000,loudnorm=I=-14:TP=-1.5:LRA=11,"
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
         "-map", "[vout]", "-map", f"[{aout}]", "-r", "30", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-t", f"{dur:.3f}",
         tmp.name], cwd=ass_path.parent)
    if tmp != out_path:
        _concat_intro_outro(tpl, ass_path.parent / tmp.name, out_path)
        (ass_path.parent / tmp.name).unlink(missing_ok=True)
    thumb = out_path.with_suffix(".jpg")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{min(1.2, dur / 2):.2f}", "-i", str(out_path),
         "-frames:v", "1", "-q:v", "3", str(thumb)])
    return {"framing": framing, "thumbnail": thumb.name}


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
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-movflags", "+faststart",
         str(out)])


def check_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH")
