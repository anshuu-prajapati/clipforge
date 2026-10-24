"""Post copy + cover for publishing a clip.

  generate()    Claude writes platform-specific copy from what is actually said in the clip:
                YouTube title / description / tags, Instagram caption, 3-8 hashtags.
                Without an API key it falls back to the hook + social caption.
  pick_cover()  best frame for the thumbnail: inside the first seconds (the burned-in hook is
                on screen), sharp, with a face if there is one.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

SYSTEM = """You write the post copy for short vertical clips (YouTube Shorts and Instagram Reels).
Write in the language the speaker uses. Be specific to what is said in the clip: names, numbers,
the payoff. No clickbait the clip does not deliver, no emojis spam (0-2 max), no ALL CAPS sentences.

- youtube_title: <= 70 characters, curiosity + the concrete specific. Do not add #Shorts (added later).
- youtube_description: 2-3 short lines: what happens, why watch, a question to drive comments.
- youtube_tags: 6-12 search phrases people would type (lowercase, no #).
- instagram_caption: line 1 = scroll-stopping hook; then 1-3 short lines of context or the takeaway;
  end with a question or call to comment. No hashtags inside (they are appended separately).
- hashtags: 3-8 relevant hashtags without '#': mix of 1-2 broad and the rest niche/topic-specific.
  Never generic spam like fyp, viral, explore, trending, foryou."""

TOOL = {
    "name": "submit_post",
    "description": "Submit the post copy.",
    "input_schema": {
        "type": "object",
        "properties": {
            "youtube_title": {"type": "string"},
            "youtube_description": {"type": "string"},
            "youtube_tags": {"type": "array", "items": {"type": "string"}},
            "instagram_caption": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["youtube_title", "youtube_description", "youtube_tags", "instagram_caption", "hashtags"],
    },
}

_SPAM = {"fyp", "foryou", "foryoupage", "viral", "explore", "explorepage", "trending", "reels", "shorts"}


def clip_text(entry: dict) -> str:
    return " ".join(w["text"] for ln in entry.get("lines", []) for w in ln["words"])


def _clean_tags(tags: list[str]) -> list[str]:
    out = []
    for t in tags:
        t = re.sub(r"[^\w]", "", t.lstrip("#"), flags=re.UNICODE)
        if t and t.lower() not in _SPAM and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out[:8]


def _assemble(d: dict, hook: str) -> dict:
    tags = _clean_tags(d.get("hashtags", []))
    hashline = " ".join("#" + t for t in tags)
    title = (d.get("youtube_title") or hook).strip()[:90]
    if "#shorts" not in title.lower() and len(title) <= 90:
        title = f"{title} #Shorts"
    desc = (d.get("youtube_description") or "").strip()
    return {
        "youtube": {"title": title[:100], "description": f"{desc}\n\n{hashline}".strip()[:4900],
                    "tags": [t.strip() for t in d.get("youtube_tags", []) if t.strip()][:15]},
        "instagram": {"caption": f"{(d.get('instagram_caption') or hook).strip()}\n\n{hashline}".strip()[:2200]},
        "hashtags": tags,
    }


def fallback(entry: dict) -> dict:
    words = re.findall(r"#(\w+)", entry.get("social_caption", ""))
    base = re.sub(r"#\w+", "", entry.get("social_caption", "")).strip()
    return _assemble({"youtube_title": entry.get("hook", ""), "youtube_description": base,
                      "youtube_tags": entry.get("emphasis_words", []), "instagram_caption":
                      f"{entry.get('hook', '')}\n\n{base}".strip(), "hashtags": words}, entry.get("hook", "")) \
        | {"generated_by": "fallback"}


def generate(entry: dict, language: str = "en") -> dict:
    """Platform copy for one clip (Claude when ANTHROPIC_API_KEY is set, else a fallback)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return fallback(entry)
    import anthropic

    client = anthropic.Anthropic()
    user = (f"Language: {language}\nOn-screen hook: {entry.get('hook', '')}\n"
            f"Why this clip works: {entry.get('why', '')}\n"
            f"Clip length: {round(entry.get('duration', 0))} s\n\nWhat is said in the clip:\n{clip_text(entry)}")
    try:
        msg = client.messages.create(
            model=os.environ.get("CLIPFORGE_MODEL", "claude-sonnet-4-5"), max_tokens=1500, system=SYSTEM,
            tools=[TOOL], tool_choice={"type": "tool", "name": "submit_post"},
            messages=[{"role": "user", "content": user}])
        data = next(b.input for b in msg.content if b.type == "tool_use")
    except Exception:  # noqa: BLE001 - bad key, no credit, network → still publishable
        return fallback(entry)
    return _assemble(data, entry.get("hook", "")) | {"generated_by": "claude"}


def _frame_score(img, detect) -> float:
    """Sharp, with a reasonably big face if there is one."""
    import cv2
    small = cv2.resize(img, (img.shape[1] // 3, img.shape[0] // 3))
    sharp = cv2.Laplacian(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
    faces = detect(small) if detect else []
    face = max((fw * fh for _, _, fw, fh in faces), default=0) / (small.shape[0] * small.shape[1])
    return sharp * (1.0 + 4.0 * min(face, 0.15))


def best_frame(paths: list[Path]) -> int:
    """Index of the best thumbnail candidate."""
    import cv2

    from .reframe import _detector
    detect = _detector()
    scores = [(_frame_score(img, detect) if (img := cv2.imread(str(p))) is not None else -1) for p in paths]
    return max(range(len(paths)), key=scores.__getitem__)


def pick_cover(video: Path, out_jpg: Path, start: float = 0.5, end: float = 3.6, n: int = 10) -> int:
    """Fallback for clips rendered before clean covers existed: best frame of the finished clip."""
    """Save the best cover frame; return its offset in ms (Instagram thumb_offset)."""
    import cv2

    from .reframe import _detector

    detect = _detector()
    cap = cv2.VideoCapture(str(video))
    dur = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 30)
    end = min(end, max(start + 0.1, dur - 0.1))
    best, best_score, best_t = None, -1.0, start
    for k in range(n):
        t = start + (end - start) * k / max(1, n - 1)
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        score = _frame_score(frame, detect)
        if score > best_score:
            best, best_score, best_t = frame, score, t
    cap.release()
    if best is None:
        raise RuntimeError(f"could not read frames from {video}")
    cv2.imwrite(str(out_jpg), best, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return int(best_t * 1000)
