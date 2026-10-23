"""Fast unit tests (no models, no network). Run: python -m pytest -q"""
import json
import types

from clipforge import hooks
from clipforge.captions import build_ass, group_lines
from clipforge.reframe import crop_keyframes, x_expr
from clipforge.template import BrandTemplate
from clipforge.transcribe import Sentence, Word


def _sents(n=30, dur=4.0):
    out = []
    for i in range(n):
        ws = [Word(f"w{i}_{k}", i * dur + k * 0.4, i * dur + k * 0.4 + 0.35) for k in range(8)]
        ws[-1].text += "."
        out.append(Sentence(i, ws[0].start, ws[-1].end, " ".join(w.text for w in ws), ws, energy=i / n))
    return out


def test_template_roundtrip(tmp_path):
    t = BrandTemplate()
    t.captions.animation = "karaoke"
    t.layout.aspect = "1:1"
    t.save(tmp_path / "t.json")
    t2 = BrandTemplate.load(tmp_path / "t.json")
    assert t2.captions.animation == "karaoke" and t2.size == (1080, 1080)


def test_group_lines_respects_max_words():
    words = [Word(f"x{i}", i * 0.3, i * 0.3 + 0.25) for i in range(10)]
    lines = group_lines(words, 0, 3, max_words=3)
    assert all(len(l.words) <= 3 for l in lines) and lines[0].start == 0


def test_build_ass_all_animations():
    words = [Word("Fifty", 0.1, 0.4), Word("episodes", 0.45, 0.9), Word("first.", 0.95, 1.3)]
    for anim in ("none", "word", "karaoke", "pop"):
        for style in ("card", "band", "plain"):
            t = BrandTemplate()
            t.captions.animation, t.headline.style = anim, style
            ass = build_ass(group_lines(words, 0, 2, 3), "Publish fifty episodes first", t, ["fifty"])
            assert "[Events]" in ass and "Dialogue: 3" in ass and "Dialogue: 2" in ass


def test_claude_path_parses_and_ranks(monkeypatch):
    sents = _sents()
    fake = [
        dict(start_idx=2, end_idx=7, title="A", hook="Hook A", alt_hooks=["a2"], why="w",
             hook_score=90, flow_score=80, value_score=85, trend_score=60, emphasis_words=["W2_1"]),
        dict(start_idx=3, end_idx=8, title="B (overlaps A)", hook="Hook B", alt_hooks=[], why="w",
             hook_score=50, flow_score=50, value_score=50, trend_score=50, emphasis_words=[]),
        dict(start_idx=15, end_idx=40, title="C (too long, out of range)", hook="Hook C", alt_hooks=[],
             why="w", hook_score=70, flow_score=70, value_score=70, trend_score=70, emphasis_words=[]),
    ]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(hooks, "_claude", lambda *a, **k: fake)
    clips, n = hooks.find_moments(sents, length="auto", max_clips=5)
    assert n == 3
    assert [c.title for c in clips] == ["A", "C (too long, out of range)"]   # B suppressed by NMS
    assert all(15 <= c.duration <= 60.5 for c in clips)
    assert clips[0].score > clips[1].score and clips[0].emphasis_words == ["w2_1"]


def test_heuristic_fallback(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    clips, _ = hooks.find_moments(_sents(), length="short", max_clips=3)
    assert clips and all(c.scorer == "heuristic" for c in clips)


def test_reframe_expression():
    keys = crop_keyframes([(0.5, 0.8), (1.0, 0.8), (6.0, 0.2), (6.5, 0.2)], [5.0])
    assert len(keys) == 2
    e = x_expr(keys, 1920, 608)
    assert e.startswith("if(lt(t,5.000),")
