"""Shot-aware vertical reframing for "fill" layout (16:9 → 9:16 that follows the people).

Like a human editor reframing for Shorts:
  1. Every frame is read once; hard cuts are found from a tiny greyscale thumbnail diff.
  2. Faces are detected (~6 fps, YuNet) and the clip is split into shots at the cuts.
  3. Shots with people get a crop that *moves* with them — smoothed, with a dead zone so a
     still speaker gives a locked-off shot; two people are kept together when they fit.
  4. Shots without people (cars, b-roll, graphics) show the whole frame over a blurred copy
     instead of a blind centre crop.
The per-frame crop x is fed to ffmpeg with sendcmd, so the movement is frame-accurate.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")   # hide harmless dnn "Targets are not supported" warning

YUNET_URL = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")


def _detector():
    """YuNet (OpenCV DNN face detector, ~230 KB) with Haar-cascade fallback for OpenCV 4."""
    import urllib.request
    from pathlib import Path

    import cv2

    model = Path(os.environ.get("CLIPFORGE_YUNET",
                                Path.home() / ".cache/clipforge/face_detection_yunet_2023mar.onnx"))
    if hasattr(cv2, "FaceDetectorYN"):
        try:
            if not model.exists() or model.stat().st_size < 100_000:
                model.parent.mkdir(parents=True, exist_ok=True)
                urllib.request.urlretrieve(YUNET_URL, model)
            det = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.7)

            def yunet(img):
                det.setInputSize((img.shape[1], img.shape[0]))
                _, faces = det.detect(img)
                return [] if faces is None else [tuple(f[:4]) for f in faces]
            return yunet
        except Exception:  # noqa: BLE001 - fall through to Haar
            pass
    if hasattr(cv2, "CascadeClassifier"):
        cas = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        return lambda img: list(cas.detectMultiScale(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 1.1, 5))
    return None


@dataclass
class Shot:
    start: float                      # seconds, relative to clip start
    end: float
    mode: str = "fit"                 # "fill" (crop follows people) | "fit" (whole frame over blur)
    xs: list[float] = field(default_factory=list)   # fill: crop centre (0-1) per output frame


@dataclass
class Plan:
    fps: float
    shots: list[Shot]

    @property
    def fill_shots(self) -> list[Shot]:
        return [s for s in self.shots if s.mode == "fill"]


# --------------------------------------------------------------------------- analysis
def analyze(video: str, start: float, end: float, crop_frac: float,
            sample_fps: float = 6.0, out_fps: float = 30.0) -> Plan:
    """Read the clip once: detect cuts + faces, return a per-shot framing plan."""
    import cv2

    detect = _detector()
    cap = cv2.VideoCapture(video)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    step = max(1, round(src_fps / sample_fps))
    n_frames = int((end - start) * src_fps)

    diffs: list[float] = []
    samples: list[tuple[float, list[tuple[float, float, float]]]] = []   # (t, [(cx, w, area)])
    prev = None
    for i in range(n_frames):
        ok, frame = cap.read()
        if not ok:
            break
        t = i / src_fps
        thumb = cv2.cvtColor(cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        diffs.append(0.0 if prev is None else float(np.abs(thumb.astype(np.int16) - prev).mean()))
        prev = thumb.astype(np.int16)
        if detect is not None and i % step == 0:
            h, w = frame.shape[:2]
            scale = 640 / max(w, h)
            small = cv2.resize(frame, (int(w * scale), int(h * scale)))
            faces = []
            for (x, y, fw, fh) in detect(small):
                if fw * fh > 0.002 * small.shape[0] * small.shape[1]:          # ignore tiny background faces
                    faces.append(((x + fw / 2) / small.shape[1], fw / small.shape[1], fw * fh))
            samples.append((t, faces))
    cap.release()

    dur = end - start
    cuts = _cuts(diffs, src_fps)
    edges = [0.0] + cuts + [dur]
    shots = []
    for a, b in zip(edges, edges[1:]):
        shot = Shot(a, b)
        ss = [(t, f) for t, f in samples if a <= t < b]
        with_face = [(t, f) for t, f in ss if f]
        if ss and len(with_face) >= max(1, 0.4 * len(ss)):
            shot.mode = "fill"
            shot.xs = _track(with_face, a, b, crop_frac, out_fps)
        shots.append(shot)
    return Plan(out_fps, _merge_short(shots))


def _cuts(diffs: list[float], fps: float, min_gap: float = 0.4) -> list[float]:
    """Hard cuts: thumbnail diff spikes well above the local level."""
    if len(diffs) < 3:
        return []
    d = np.array(diffs)
    base = np.median(d) + 1e-3
    out: list[float] = []
    for i in range(1, len(d)):
        if d[i] > max(18.0, 4 * base) and d[i] > 2.5 * max(d[i - 1], d[i + 1] if i + 1 < len(d) else 0):
            t = i / fps
            if not out or t - out[-1] >= min_gap:
                out.append(t)
    return out


def _target(faces: list[tuple[float, float, float]], crop_frac: float) -> float:
    """Where the crop should be centred for one sample's faces."""
    big = max(a for _, _, a in faces)
    main = [f for f in faces if f[2] >= 0.25 * big]
    left = min(cx - w / 2 for cx, w, _ in main)
    right = max(cx + w / 2 for cx, w, _ in main)
    if right - left <= 0.8 * crop_frac:            # everyone fits → frame the group
        return (left + right) / 2
    return max(faces, key=_salience)[0]            # otherwise the main subject


def _salience(f: tuple[float, float, float]) -> float:
    """Big, fully in frame and near the centre wins; a half face cut by the frame edge loses."""
    cx, w, area = f
    clipped = cx - w / 2 < 0.02 or cx + w / 2 > 0.98
    return area * (0.35 if clipped else 1.0) * (1.2 - abs(cx - 0.5))


def _track(samples, a: float, b: float, crop_frac: float, out_fps: float) -> list[float]:
    """Smooth per-frame crop centre for one shot."""
    ts = np.array([t for t, _ in samples])
    xs = np.array([_target(f, crop_frac) for _, f in samples])
    if len(xs) >= 3:                                # median-of-3 kills single-frame detector jumps
        xs = np.array([np.median(xs[max(0, i - 1): i + 2]) for i in range(len(xs))])
    n = max(1, int(round((b - a) * out_fps)))
    tf = a + np.arange(n) / out_fps
    x = np.interp(tf, ts, xs)
    if x.max() - x.min() < 0.04 * crop_frac / 0.32:  # barely moves → locked-off shot
        return [float(np.median(x))] * n
    k = max(1, int(out_fps * 0.6))                   # ~0.6 s moving average = smooth camera pan
    pad = np.pad(x, (k, k), mode="edge")
    x = np.convolve(pad, np.ones(2 * k + 1) / (2 * k + 1), mode="same")[k:-k]
    return [float(v) for v in x]


def _merge_short(shots: list[Shot], min_len: float = 0.5) -> list[Shot]:
    """Fold tiny shots (flashes, false cuts) into their neighbour so framing doesn't flicker."""
    out: list[Shot] = []
    for s in shots:
        if out and (s.end - s.start < min_len) and out[-1].mode == "fit" and s.mode == "fit":
            out[-1].end = s.end
        else:
            out.append(s)
    return out


# --------------------------------------------------------------------------- ffmpeg glue
def crop_commands(plan: Plan, src_w: int, crop_w: int) -> tuple[int, str]:
    """(initial x, sendcmd script) that moves crop@rf to the planned x every frame it changes."""
    def px(c: float) -> int:
        return int(min(max(c * src_w - crop_w / 2, 0), src_w - crop_w))

    lines, last = [], None
    first = None
    for s in plan.fill_shots:
        for i, c in enumerate(s.xs):
            x = px(c)
            if first is None:
                first = x
            if x != last or i == 0:
                lines.append(f"{s.start + i / plan.fps:.3f} crop@rf x {x};")
                last = x
    return (first if first is not None else (src_w - crop_w) // 2), "\n".join(lines) + "\n"


def enable_expr(shots: list[Shot]) -> str:
    return "+".join(f"between(t,{s.start:.3f},{s.end - 0.001:.3f})" for s in shots)
