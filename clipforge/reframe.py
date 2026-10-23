"""Speaker-aware reframing for "fill" layout (crop 16:9 → 9:16 following the face).

Kaleidly's template editor lists Fill as "coming soon" (clips use Fit + blurred
background today). This implements it: faces are detected at ~2 fps with
OpenCV, the horizontal centre is median-filtered per sentence, and the crop
"cuts" between positions on sentence boundaries — like a camera operator
switching shots — instead of drifting.
"""
from __future__ import annotations

import os
from statistics import median

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")   # hide harmless dnn "Targets are not supported" warning


YUNET_URL = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")


def _detector():
    """YuNet (OpenCV DNN face detector, ~230 KB) with Haar-cascade fallback for OpenCV 4."""
    import os
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


def face_track(video: str, start: float, end: float, sample_fps: float = 2.0) -> list[tuple[float, float]]:
    """Return [(t_rel, x_centre_norm)] samples where a face was found."""
    import cv2

    detect = _detector()
    if detect is None:
        return []
    cap = cv2.VideoCapture(video)
    out: list[tuple[float, float]] = []
    t = start
    while t < end:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            break
        h, w = frame.shape[:2]
        scale = 640 / max(w, h)
        small = cv2.resize(frame, (int(w * scale), int(h * scale)))
        faces = detect(small)
        if len(faces):
            x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])  # largest face ≈ active speaker
            out.append((t - start, float(x + fw / 2) / small.shape[1]))
        t += 1.0 / sample_fps
    cap.release()
    return out


def crop_keyframes(samples: list[tuple[float, float]], boundaries: list[float],
                   min_shift: float = 0.06) -> list[tuple[float, float]]:
    """Collapse samples into one x-centre per sentence span; drop tiny moves."""
    if not samples:
        return [(0.0, 0.5)]
    keys: list[tuple[float, float]] = []
    edges = sorted(set([0.0] + boundaries))
    last = median(x for _, x in samples)
    for a, b in zip(edges, edges[1:] + [1e9]):
        xs = [x for t, x in samples if a <= t < b]
        x = median(xs) if xs else last
        if not keys or abs(x - keys[-1][1]) > min_shift:
            keys.append((a, x))
        last = keys[-1][1]
    return keys


def x_expr(keys: list[tuple[float, float]], src_w: int, crop_w: int) -> str:
    """ffmpeg expression for crop x (per-frame) from keyframes."""
    def px(c):
        return int(min(max(c * src_w - crop_w / 2, 0), src_w - crop_w))
    expr = str(px(keys[-1][1]))
    for i in range(len(keys) - 2, -1, -1):
        c, nxt = keys[i][1], keys[i + 1][0]
        expr = f"if(lt(t,{nxt:.3f}),{px(c)},{expr})"
    return expr
