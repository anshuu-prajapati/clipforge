"""Step 2 — hook-finder: propose candidate moments and score them.

Kaleidly's UI exposes a 0-100 score per clip with a "Score breakdown" of
Hook / Flow / Value / Trend and a "Why this clip" explanation, plus a headline
hook with "Other hooks". This module reproduces that with Claude:

  1. The transcript is split into sentences annotated with timing, loudness
     rank ("energy") and speech rate, then chunked (~10 min, with overlap).
  2. Claude reads each chunk and returns candidate moments as sentence-index
     ranges, each graded on the four axes, with hooks + emphasis keywords.
  3. Candidates are snapped to sentence boundaries, length-constrained,
     de-duplicated (temporal NMS) and ranked by a weighted overall score.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .transcribe import Sentence

LENGTHS = {"auto": (15, 60), "short": (12, 30), "medium": (30, 60), "long": (60, 90)}
WEIGHTS = {"hook": 0.35, "flow": 0.20, "value": 0.30, "trend": 0.15}


@dataclass
class Candidate:
    start_idx: int
    end_idx: int
    start: float
    end: float
    title: str
    hook: str
    alt_hooks: list[str]
    why: str
    grades: dict[str, int]
    emphasis_words: list[str] = field(default_factory=list)
    social_caption: str = ""
    score: int = 0
    scorer: str = "claude"

    @property
    def duration(self) -> float:
        return self.end - self.start


SYSTEM = """You are a senior short-form video editor who finds the moments in long \
recordings (podcasts, interviews, talks) that will perform as standalone vertical \
clips on TikTok, Reels, Shorts, LinkedIn and X.

Grade every candidate 0-100 on four axes:
- hook: do the FIRST 3 SECONDS stop the scroll? (bold claim, question, conflict, \
surprising number, strong emotion). Weak openers ("so", "um", "anyway", "like I said") score low.
- flow: is it self-contained? Starts at the beginning of a thought, needs no earlier \
context, no dangling "he/that/this" references, ends on a payoff or punchline, not mid-thought.
- value: insight, story, humour, emotion, contrarian take or actionable advice a stranger \
would care about.
- trend: shareability / relevance to topics people currently discuss online.

Rules:
- Use ONLY the sentence indices given. A clip is the contiguous range [start_idx, end_idx].
- Prefer starting exactly on the sentence that contains the hook line; drop throat-clearing.
- Respect the duration window you are given. Clips must not overlap much.
- hook: a punchy on-screen headline (<= 60 chars) in the speaker's language; alt_hooks: 2 \
different angles. Never use clickbait that the clip does not deliver.
- emphasis_words: 3-8 single words from the clip that deserve visual emphasis in captions \
(numbers, names, strong verbs, the key idea).
- why: one or two sentences, concrete, referencing what is said.
- social_caption: a 1-2 sentence post caption with 2-3 relevant hashtags.
Return them with the submit_candidates tool."""

TOOL = {
    "name": "submit_candidates",
    "description": "Submit candidate clip moments.",
    "input_schema": {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start_idx": {"type": "integer"},
                        "end_idx": {"type": "integer"},
                        "title": {"type": "string"},
                        "hook": {"type": "string"},
                        "alt_hooks": {"type": "array", "items": {"type": "string"}},
                        "why": {"type": "string"},
                        "hook_score": {"type": "integer"},
                        "flow_score": {"type": "integer"},
                        "value_score": {"type": "integer"},
                        "trend_score": {"type": "integer"},
                        "emphasis_words": {"type": "array", "items": {"type": "string"}},
                        "social_caption": {"type": "string"},
                    },
                    "required": ["start_idx", "end_idx", "title", "hook", "alt_hooks", "why",
                                 "hook_score", "flow_score", "value_score", "trend_score",
                                 "emphasis_words"],
                },
            }
        },
        "required": ["candidates"],
    },
}


def _fmt(s: Sentence) -> str:
    return f"[{s.idx}] {s.start:7.1f}-{s.end:7.1f}s e={s.energy:.2f} r={s.rate:.1f} | {s.text}"


def _chunks(sents: list[Sentence], span: float = 600, overlap: float = 90):
    if not sents:
        return
    t = sents[0].start
    end = sents[-1].end
    while True:
        part = [s for s in sents if s.start >= t and s.end <= t + span]
        if part:
            yield part
        if t + span >= end:
            break
        t += span - overlap


def parse_length(length: str) -> tuple[float, float, bool]:
    """Preset name or custom "MIN-MAX" seconds → (lo, hi, strict). Custom ranges are strict."""
    if length in LENGTHS:
        return (*LENGTHS[length], False)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*", length)
    if not m or float(m[1]) >= float(m[2]):
        from .media import ClipforgeError
        raise ClipforgeError("bad_length", f"--length must be one of {', '.join(LENGTHS)} or MIN-MAX seconds "
                                           f"like 50-60, got {length!r}")
    return float(m[1]), float(m[2]), True


def _overall(g: dict[str, int]) -> int:
    return round(sum(WEIGHTS[k] * max(0, min(100, g[k])) for k in WEIGHTS))


def _fit_range(c: Candidate, sents: list[Sentence], lo: float, hi: float,
               strict: bool = False) -> Optional[Candidate]:
    a, b = max(0, c.start_idx), min(len(sents) - 1, c.end_idx)
    if b < a:
        return None
    while b > a and sents[b].end - sents[a].start > hi:     # too long → trim tail
        b -= 1
    while sents[b].end - sents[a].start < lo and b + 1 < len(sents) \
            and sents[b + 1].end - sents[a].start <= hi:      # too short → extend
        b += 1
    if sents[b].end - sents[a].start < (lo - 1 if strict else lo * 0.75):
        return None
    c.start_idx, c.end_idx = a, b
    c.start = max(0.0, sents[a].start - 0.08)
    nxt = sents[b + 1].start - 0.04 if b + 1 < len(sents) else sents[b].end + 0.4
    c.end = max(sents[b].end + 0.02, min(sents[b].end + 0.3, nxt))   # don't clip into the next sentence
    return c


def _nms(cands: list[Candidate], max_iou: float = 0.3) -> list[Candidate]:
    keep: list[Candidate] = []
    for c in sorted(cands, key=lambda x: -x.score):
        ok = True
        for k in keep:
            inter = max(0.0, min(c.end, k.end) - max(c.start, k.start))
            union = max(c.end, k.end) - min(c.start, k.start)
            if union and inter / union > max_iou or inter > 0.5 * min(c.duration, k.duration):
                ok = False
                break
        if ok:
            keep.append(c)
    return keep


# --------------------------------------------------------------------------- Claude
def _claude(chunk: list[Sentence], lo: float, hi: float, n: int, language: str) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic()
    model = os.environ.get("CLIPFORGE_MODEL", "claude-sonnet-4-5")
    user = (f"Transcript language: {language}. Duration window per clip: {lo:.0f}-{hi:.0f} s.\n"
            f"Propose up to {n} candidate clips from this part of the recording, best first.\n"
            f"Columns: [idx] start-end e=loudness rank(0-1) r=words/sec | text\n\n"
            + "\n".join(_fmt(s) for s in chunk))
    msg = client.messages.create(
        model=model, max_tokens=8000, system=SYSTEM, tools=[TOOL],
        tool_choice={"type": "tool", "name": "submit_candidates"},
        messages=[{"role": "user", "content": user}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.input.get("candidates", [])
    return []


# --------------------------------------------------------------------------- offline fallback
_HOOKY = re.compile(r"\b(nobody|never|secret|mistake|truth|why|how|stop|biggest|worst|best|"
                    r"actually|million|thousand|\d+%?|you)\b", re.I)
_WEAK = re.compile(r"^(so|um|uh|and|anyway|like|yeah|okay|but)\b", re.I)


def _heuristic(sents: list[Sentence], lo: float, hi: float, n: int) -> list[dict]:
    """Keyword/energy heuristic, used only when no ANTHROPIC_API_KEY is set."""
    out = []
    for i, s in enumerate(sents):
        j = i
        while j + 1 < len(sents) and sents[j + 1].end - s.start <= (lo + hi) / 2:
            j += 1
        span = sents[i:j + 1]
        text = " ".join(x.text for x in span)
        first = s.text
        hook = 40 + 12 * len(_HOOKY.findall(first)) + (15 if first.rstrip().endswith("?") else 0) \
            - (25 if _WEAK.search(first) else 0) + 20 * s.energy
        value = 40 + 3 * len(_HOOKY.findall(text)) + 25 * sum(x.energy for x in span) / len(span)
        flow = 70 if _END.search(sents[j].text) else 45
        words = sorted({w.strip(".,!?").lower() for w in text.split() if _HOOKY.search(w) or w[:1].isdigit()})
        out.append(dict(start_idx=i, end_idx=j, title=first[:50], hook=first[:60],
                        alt_hooks=[], why="Heuristic pick: hook words + loudness.",
                        hook_score=int(min(99, hook)), flow_score=flow, value_score=int(min(99, value)),
                        trend_score=50, emphasis_words=words[:6], social_caption=""))
    return out


_END = re.compile(r"[.!?…]['\")\]]*$")


# --------------------------------------------------------------------------- entry point
def find_moments(sents: list[Sentence], language: str = "en", length: str = "auto",
                 max_clips: int = 8, min_score: int = 45, progress: Callable[[str], None] = lambda _m: None
                 ) -> tuple[list[Candidate], int]:
    """Returns (ranked clips, number of raw candidates scored)."""
    lo, hi, strict = parse_length(length)
    use_claude = bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("CLIPFORGE_SCORER") != "heuristic"
    raw: list[dict] = []
    if use_claude:
        chunks = list(_chunks(sents))
        per = max(4, round(max_clips * 2 / max(1, len(chunks))) + 2)
        try:
            for k, ch in enumerate(chunks, 1):
                progress(f"scoring chunk {k}/{len(chunks)}")
                raw += _claude(ch, lo, hi, per, language)
        except Exception as e:  # noqa: BLE001 - bad key, no credit, network … → don't lose the run
            progress(f"Claude failed ({type(e).__name__}: {str(e)[:120]}) → heuristic scorer")
            use_claude, raw = False, []
    if not use_claude:
        raw = _heuristic(sents, lo, hi, max_clips)

    cands: list[Candidate] = []
    for r in raw:
        g = {"hook": int(r.get("hook_score", 0)), "flow": int(r.get("flow_score", 0)),
             "value": int(r.get("value_score", 0)), "trend": int(r.get("trend_score", 0))}
        c = Candidate(int(r["start_idx"]), int(r["end_idx"]), 0, 0, r.get("title", ""),
                      r.get("hook", "")[:80], [h[:80] for h in r.get("alt_hooks", [])][:3],
                      r.get("why", ""), g, [w.lower() for w in r.get("emphasis_words", [])],
                      r.get("social_caption", ""), _overall(g),
                      "claude" if use_claude else "heuristic")
        c = _fit_range(c, sents, lo, hi, strict)
        if c:
            cands.append(c)
    if not cands:
        from .media import ClipforgeError
        raise ClipforgeError("no_moments", "Nothing in the recording stood out as a clip.")
    ranked = _nms(cands)
    good = [c for c in ranked if c.score >= min_score] or ranked[:1]   # always return at least one
    return good[:max_clips], len(raw)


def candidate_to_dict(c: Candidate) -> dict:
    d = asdict(c)
    d["duration"] = round(c.duration, 2)
    return d
