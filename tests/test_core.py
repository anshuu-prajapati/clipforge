"""Fast unit tests (no models, no network). Run: python -m pytest -q"""
import json
import types

from clipforge import hooks, reframe
from clipforge.captions import build_ass, group_lines
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
        for style in ("bold", "card", "band", "plain"):
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


def test_cut_detection():
    diffs = [1.0] * 30 + [60.0] + [1.0] * 30 + [55.0] + [1.0] * 10
    assert reframe._cuts(diffs, fps=30) == [1.0, 61 / 30]


def test_track_locks_still_speaker_and_follows_moving_one():
    still = [(t / 6, [(0.5 + 0.005 * (t % 2), 0.1, 1.0)]) for t in range(12)]
    xs = reframe._track(still, 0, 2, 0.32, 30)
    assert len(xs) == 60 and len(set(xs)) == 1                     # locked-off shot
    moving = [(t / 6, [(0.2 + 0.05 * t, 0.1, 1.0)]) for t in range(12)]
    xs = reframe._track(moving, 0, 2, 0.32, 30)
    assert xs[-1] > xs[0] + 0.2 and all(b >= a - 1e-9 for a, b in zip(xs, xs[1:]))   # smooth pan


def test_two_people_framed_together_when_they_fit():
    assert abs(reframe._target([(0.42, 0.06, 1.0), (0.58, 0.06, 0.9)], 0.32) - 0.5) < 1e-6
    assert reframe._target([(0.1, 0.06, 1.0), (0.9, 0.06, 0.5)], 0.32) == 0.1   # too far apart → speaker
    # half a face at the frame edge loses to the person fully in shot
    assert reframe._target([(0.9, 0.21, 1.0), (0.56, 0.21, 1.0)], 0.32) == 0.56


def test_crop_commands_and_enable():
    plan = reframe.Plan(30, [reframe.Shot(0, 1, "fill", [0.5] * 30), reframe.Shot(1, 2, "fit"),
                             reframe.Shot(2, 3, "fill", [0.25] * 30)])
    x0, cmds = reframe.crop_commands(plan, 1920, 608)
    assert x0 == 656 and cmds.splitlines() == ["0.000 crop@rf x 656;", "2.000 crop@rf x 176;"]
    assert reframe.enable_expr(plan.fill_shots) == "between(t,0.000,0.999)+between(t,2.000,2.999)"


def test_bold_hook_highlights_numbers_and_emphasis():
    t = BrandTemplate()
    ass = build_ass([], "This $500K car drives on water", t, ["water"])
    head = next(l for l in ass.splitlines() if l.startswith("Dialogue: 2"))
    hi = "{\\c&H0AD6FF&}"
    assert f"{hi}$500K" in head and f"{hi}WATER" in head and "\\fscx65" in head
