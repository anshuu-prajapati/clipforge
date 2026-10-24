"""Step 4 — captions: word-timed, animated captions + hook headline as an ASS script.

ASS (Advanced SubStation Alpha) is rendered by libass inside ffmpeg, which gives
us per-word colour, scale transforms (\\t), moves (\\move), fades and vector
drawings (\\p1) — enough for "motion caption" styles:

  none     static line
  word     one word at a time, each popping in
  karaoke  whole line visible, the active word highlighted + scaled
  pop      words appear as they're spoken, each with an overshoot pop

The hook headline is a card / band / plain title that slides in and fades out.
"""
from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass

from .template import BrandTemplate, CaptionStyle, HeadlineStyle
from .transcribe import Word


@dataclass
class Line:
    """One caption line (a group of up to max_words words). Editable in the clip editor."""
    start: float
    end: float
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


# ------------------------------------------------------------------ helpers
def ass_color(hex_: str, alpha: int = 0) -> str:
    h = hex_.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_tag_color(hex_: str) -> str:
    """Inline override colour: &HBBGGRR&"""
    h = hex_.lstrip("#")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def _esc(t: str) -> str:
    return t.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _norm(w: str) -> str:
    return re.sub(r"[^\w%$€£]", "", w.lower())


# ------------------------------------------------------------------ grouping
def group_lines(words: list[Word], clip_start: float, clip_end: float, max_words: int,
                max_chars: int = 22) -> list[Line]:
    rel = [Word(w.text, max(0.0, w.start - clip_start), min(clip_end, w.end) - clip_start)
           for w in words]
    lines: list[Line] = []
    cur: list[Word] = []
    for w in rel:
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > 0.6
                    or len(" ".join(x.text for x in cur + [w])) > max_chars
                    or re.search(r"[.!?,;:]$", cur[-1].text)):
            lines.append(Line(cur[0].start, cur[-1].end, cur))
            cur = []
        cur.append(w)
    if cur:
        lines.append(Line(cur[0].start, cur[-1].end, cur))
    # close small gaps so captions don't flicker
    for a, b in zip(lines, lines[1:]):
        if b.start - a.end < 0.35:
            a.end = b.start
    return lines


def lines_to_json(lines: list[Line]) -> list[dict]:
    return [{"start": round(l.start, 3), "end": round(l.end, 3),
             "words": [asdict(w) for w in l.words]} for l in lines]


def lines_from_json(data: list[dict]) -> list[Line]:
    return [Line(d["start"], d["end"], [Word(**w) for w in d["words"]]) for d in data]


# ------------------------------------------------------------------ captions
def _font_px(c: CaptionStyle, H: int, W: int) -> int:
    base = min(W, H * 9 / 16)
    return round(base * {"s": 0.075, "m": 0.092, "l": 0.112}[c.size])


def _word_markup(text: str, *, active: bool, emph: bool, c: CaptionStyle, pop: bool) -> str:
    t = _esc(text.upper() if c.uppercase else text)
    tags = ""
    if active or emph:
        tags += f"\\c{ass_tag_color(c.highlight_color)}"
    scale_ok = c.background != "box"          # scaling inside an opaque box draws ragged boxes
    if emph and c.emphasis and scale_ok:
        tags += "\\fscx112\\fscy112"
    if active and pop and scale_ok:
        tags += "\\fscx135\\fscy135\\t(0,110,\\fscx112\\fscy112)\\t(110,180,\\fscx100\\fscy100)"
    if not tags:
        return t
    return "{" + tags + "}" + t + "{\\r}"


def caption_events(lines: list[Line], c: CaptionStyle, W: int, H: int, emphasis: set[str]) -> list[str]:
    x, y = W // 2, round(H * c.position_pct / 100)
    pos = f"\\an5\\pos({x},{y})"
    ev = []
    for ln in lines:
        ws = ln.words
        if c.animation == "none":
            body = " ".join(_word_markup(w.text, active=False, emph=_norm(w.text) in emphasis, c=c, pop=False)
                            for w in ws)
            ev.append(_dlg(3, ln.start, ln.end, "Caption", "{" + pos + "\\fad(60,40)}" + body))
            continue
        for j, w in enumerate(ws):
            t0 = ln.start if j == 0 else w.start
            t1 = ws[j + 1].start if j + 1 < len(ws) else ln.end
            if t1 - t0 < 0.02:
                continue
            emph = lambda ww: _norm(ww.text) in emphasis  # noqa: E731
            if c.animation == "word":
                body = _word_markup(w.text, active=False, emph=emph(w), c=c, pop=False)
                anim = "\\fscx70\\fscy70\\t(0,90,\\fscx108\\fscy108)\\t(90,150,\\fscx100\\fscy100)"
                ev.append(_dlg(3, t0, t1, "Caption", "{" + pos + anim + "}" + body))
            elif c.animation == "karaoke":
                body = " ".join(_word_markup(x.text, active=(k == j), emph=emph(x), c=c, pop=False)
                                for k, x in enumerate(ws))
                enter = "\\fad(80,0)\\fscx88\\fscy88\\t(0,100,\\fscx100\\fscy100)" if j == 0 else ""
                ev.append(_dlg(3, t0, t1, "Caption", "{" + pos + enter + "}" + body))
            else:  # pop: progressive reveal, newest word pops
                shown = ws[: j + 1]
                hidden = ws[j + 1:]
                body = " ".join(_word_markup(x.text, active=(k == j), emph=emph(x), c=c, pop=True)
                                for k, x in enumerate(shown))
                if hidden:  # keep layout stable: reserve space for words not yet spoken
                    body += "{\\alpha&HFF&} " + " ".join(_esc(x.text.upper() if c.uppercase else x.text)
                                                        for x in hidden)
                ev.append(_dlg(3, t0, t1, "Caption", "{" + pos + "}" + body))
    return ev


# ------------------------------------------------------------------ headline (hook)
def _wrap(text: str, max_chars: int, limit: int = 3) -> list[str]:
    out, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > max_chars:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out[:limit]


def _rounded_rect(w: int, h: int, r: int) -> str:
    return (f"m {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "
            f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} l 0 {r} b 0 0 0 0 {r} 0")


def _bold_headline(text: str, hs: HeadlineStyle, W: int, H: int, emphasis: set[str]) -> list[str]:
    """Big outlined hook, numbers + emphasis words in the highlight colour, pops in then fades."""
    fs = hs.size_px * W / 1080
    for _ in range(4):                           # shrink until it fits in 3 lines
        lines = _wrap(text, max(8, int(W * 0.86 / (fs * 0.68))), limit=99)
        if len(lines) <= 3:
            break
        fs *= 0.86
    lines = lines[:3]
    hi, base = ass_tag_color(hs.highlight_color), ass_tag_color(hs.text_color)

    def word(w: str) -> str:
        t = _esc(w)
        if any(ch.isdigit() for ch in w) or _norm(w) in emphasis:
            return f"{{\\c{hi}}}{t}{{\\c{base}}}"
        return t

    body = "\\N".join(" ".join(word(w) for w in ln.split()) for ln in lines)
    cy = round(H * 0.17) if hs.position == "top" else round(H * 0.80)
    bord, shad = max(4, round(fs * 0.11)), max(2, round(fs * 0.05))
    pop = "\\fscx65\\fscy65\\t(0,170,\\fscx108\\fscy108)\\t(170,280,\\fscx100\\fscy100)"
    return [_dlg(2, 0, hs.duration_s, "Headline",
                 f"{{\\an5\\pos({W // 2},{cy})\\fs{round(fs)}\\c{base}\\3c{ass_tag_color(hs.outline_color)}"
                 f"\\4c&H000000&\\bord{bord}\\shad{shad}\\q2{pop}\\fad(90,260)}}{body}")]


def headline_events(text: str, hs: HeadlineStyle, W: int, H: int, emphasis: set[str] = frozenset()) -> list[str]:
    if not hs.show or not text.strip():
        return []
    text = text.upper() if hs.uppercase else text
    if hs.style == "bold":
        return _bold_headline(text, hs, W, H, emphasis)
    fs = hs.size_px * W / 1080
    lines = _wrap(text, max(10, int(W * 0.78 / (fs * 0.55))))
    lh = fs * 1.18
    pad = fs * 0.55
    box_h = round(len(lines) * lh + pad * 1.4)
    longest = max(len(l) for l in lines)
    box_w = round(min(W * 0.88, longest * fs * 0.56 + pad * 2))
    top = round(H * 0.11) if hs.position == "top" else round(H * 0.80 - box_h)
    t1 = hs.duration_s
    slide = 50
    ev = []
    joined = "\\N".join(_esc(l) for l in lines)
    if hs.style == "card":
        x0 = (W - box_w) // 2
        ev.append(_dlg(1, 0, t1, "Shape",
                       f"{{\\an7\\move({x0},{top - slide},{x0},{top},0,320)\\fad(220,250)"
                       f"\\c{ass_tag_color(hs.background_color)}\\bord0\\shad0\\p1}}"
                       f"{_rounded_rect(box_w, box_h, round(fs * 0.4))}{{\\p0}}"))
    elif hs.style == "band":
        ev.append(_dlg(1, 0, t1, "Shape",
                       f"{{\\an7\\move({-W},{top},0,{top},0,300)\\fad(0,250)"
                       f"\\c{ass_tag_color(hs.background_color)}\\bord0\\shad0\\p1}}"
                       f"m 0 0 l {W} 0 l {W} {box_h} l 0 {box_h}{{\\p0}}"))
    cx, cy = W // 2, top + box_h // 2
    border = "\\bord5\\3c&H000000&" if hs.style == "plain" else "\\bord0"
    ev.append(_dlg(2, 0, t1, "Headline",
                   f"{{\\an5\\move({cx},{cy - slide},{cx},{cy},0,320)\\fad(220,250)"
                   f"\\fs{round(fs)}\\c{ass_tag_color(hs.text_color)}"
                   f"{border}\\shad0}}{joined}"))
    return ev


def build_cover_ass(hook: str, tpl: BrandTemplate, emphasis: list[str]) -> str:
    """Hook only, fully shown and static — for the thumbnail (no captions, no animation)."""
    t = copy.deepcopy(tpl)
    t.captions.show, t.headline.duration_s = False, 3600
    ass = build_ass([], hook, t, emphasis)
    ass = re.sub(r"\\move\((-?\d+),(-?\d+),(-?\d+),(-?\d+)(?:,\d+,\d+)?\)", r"\\pos(\3,\4)", ass)
    return re.sub(r"\\fscx65\\fscy65(\\t\([^)]*\))*|\\fad\(\d+,\d+\)", "", ass)


# ------------------------------------------------------------------ document
def _dlg(layer: int, a: float, b: float, style: str, text: str) -> str:
    return f"Dialogue: {layer},{_ts(a)},{_ts(b)},{style},,0,0,0,,{text}"


def build_ass(lines: list[Line], hook: str, tpl: BrandTemplate, emphasis: list[str]) -> str:
    W, H = tpl.size
    c, hs = tpl.captions, tpl.headline
    fs = _font_px(c, H, W)
    bold = -1 if c.weight >= 600 else 0
    if c.background == "box":
        border_style, outline, shadow = 3, round(fs * 0.18), 0
        outline_col, back_col = ass_color(c.background_color, 0x30), ass_color(c.background_color, 0x30)
    else:
        border_style = 1
        outline = round(fs * 0.075) if c.outline else 0
        shadow = round(fs * 0.06) if c.background == "shadow" else 0
        outline_col, back_col = ass_color(c.outline_color), ass_color(c.background_color, 0x60)
    # libass picks the bold face via the Bold flag; for 800/900 use a font name like
    # "Poppins ExtraBold" / "Montserrat Black" in the template (installed or in fonts_dir).
    font = c.font
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{fs},{ass_color(c.text_color)},{ass_color(c.highlight_color)},{outline_col},{back_col},{bold},0,0,0,100,100,0,0,{border_style},{outline},{shadow},5,{round(W*0.06)},{round(W*0.06)},0,1
Style: Headline,{hs.font or font},{hs.size_px},{ass_color(hs.text_color)},{ass_color(hs.text_color)},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,{round(W*0.08)},{round(W*0.08)},0,1
Style: Shape,{font},20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    emph = {_norm(e) for e in emphasis}
    ev = headline_events(hook, hs, W, H, emph)
    if c.show:
        ev += caption_events(lines, c, W, H, emph)
    return head + "\n".join(ev) + "\n"

