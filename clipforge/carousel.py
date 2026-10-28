"""Carousel posts: Claude writes the slides, Pillow draws them as 1080×1350 JPEGs (Instagram's 4:5 feed size).

  write()    from a prompt ("5 mistakes beginners make…") or a video transcript → slide plan + caption
  render()   slide plan + look (colors and font from the user's brand style) → slide_01.jpg … slide_NN.jpg

Words wrapped in *asterisks* in a slide title are drawn in the highlight color.
"""
from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

W, H = 1080, 1350
PAD = 96
MIN_SLIDES, MAX_SLIDES = 3, 10
IMAGE_FILE = re.compile(r"img_[A-Za-z0-9_-]{1,40}\.jpg")      # a slide's photo, stored next to the slides

IMAGES_NOTE = """
image_query: for every slide except the cta, a 2-4 word English stock-photo search phrase for a concrete,
photographable scene or object that fits the slide (e.g. "person editing video laptop"). No text, logos,
brands or famous people. Empty string for the cta slide."""

SYSTEM = """You design Instagram carousel posts that people save and share.
Write in the requested language. Every slide must earn the swipe to the next one.

Slides, in order:
1. kind "hook": a scroll-stopping title (max 12 words) that promises a specific payoff; body = one short
   line that creates curiosity (or empty).
2. kind "point" slides: one idea each. title max 8 words; body max 35 words — concrete, specific,
   actionable (numbers, names, examples). No filler, no repetition between slides.
3. last slide kind "cta": a one-line recap title, body asks to save / share / follow for more.

Wrap the 1-3 most important words of a title in *asterisks* so they are highlighted.
No emojis in titles, no hashtags on slides, no ALL CAPS sentences.

caption: line 1 = hook; then 2-4 short lines of context; end with a question that invites comments.
No hashtags in the caption (they are added separately).
hashtags: 3-8 relevant hashtags without '#', mostly niche/topic-specific. Never fyp, viral, explore, trending."""

TOOL = {
    "name": "submit_carousel",
    "description": "Submit the carousel.",
    "input_schema": {
        "type": "object",
        "properties": {
            "slides": {"type": "array", "items": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["hook", "point", "cta"]},
                "title": {"type": "string"}, "body": {"type": "string"}, "image_query": {"type": "string"}},
                "required": ["kind", "title", "body"]}},
            "caption": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["slides", "caption", "hashtags"],
    },
}


class CarouselError(RuntimeError):
    pass


# =========================================================================== writing
def write(prompt: str = "", transcript: str = "", title: str = "", slides: int = 7, language: str = "",
          handle: str = "", images: bool = False) -> dict:
    """Slide plan {slides: [{kind, title, body, image_query?}], caption, hashtags} from a prompt and/or a
    transcript; images=True also asks for a stock-photo search phrase per slide."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise CarouselError("Writing posts needs ANTHROPIC_API_KEY in the server's .env.")
    import anthropic

    slides = max(MIN_SLIDES, min(MAX_SLIDES, int(slides)))
    parts = [f"Number of slides: exactly {slides} (1 hook, {slides - 2} points, 1 cta)."]
    if language:
        parts.append(f"Language: {language}")
    if handle:
        parts.append(f"Account handle (for the cta): {handle}")
    if prompt:
        parts.append(f"What the post should be about:\n{prompt.strip()[:2000]}")
    if transcript:
        parts.append(f"Turn the most valuable ideas of this video into the carousel. Use what is actually said — "
                     f"specifics, numbers, examples — not generic advice.\nVideo title: {title}\n\n"
                     f"Transcript:\n{transcript[:60000]}")
    try:
        msg = anthropic.Anthropic().messages.create(
            model=os.environ.get("CLIPFORGE_MODEL", "claude-sonnet-4-5"), max_tokens=4000,
            system=SYSTEM + (IMAGES_NOTE if images else ""),
            tools=[TOOL], tool_choice={"type": "tool", "name": "submit_carousel"},
            messages=[{"role": "user", "content": "\n\n".join(parts)}])
        data = next(b.input for b in msg.content if b.type == "tool_use")
    except Exception as e:  # noqa: BLE001 - bad key, no credit, network
        raise CarouselError(f"Claude couldn't write the post: {str(e)[:200]}") from None
    return clean(data)


def clean(plan: dict) -> dict:
    """Normalize a plan (from Claude or from the editor): 1 hook, points, 1 cta, sane lengths."""
    from .social import _clean_tags
    s = []
    for x in plan.get("slides") or []:
        if not isinstance(x, dict) or not (str(x.get("title", "")).strip() or str(x.get("body", "")).strip()):
            continue
        y = {"kind": x.get("kind", "point"), "title": str(x.get("title", "")).strip()[:160],
             "body": str(x.get("body", "")).strip()[:400]}
        if x.get("image_query"):
            y["image_query"] = str(x["image_query"]).strip()[:80]
        im = x.get("image")
        if isinstance(im, dict) and IMAGE_FILE.fullmatch(str(im.get("file", ""))):
            y["image"] = {k: str(im[k])[:200] for k in ("file", "source", "credit", "page") if im.get(k)}
            if not y["image"].get("page", "").startswith("https://"):
                y["image"].pop("page", None)
        s.append(y)
    s = s[:MAX_SLIDES]
    if not s:
        raise CarouselError("The post has no slides.")
    for i, x in enumerate(s):
        x["kind"] = "hook" if i == 0 else "cta" if i == len(s) - 1 and len(s) > 2 else "point"
    return {"slides": s, "caption": str(plan.get("caption", "")).strip()[:2000],
            "hashtags": _clean_tags([str(t) for t in plan.get("hashtags") or []])}


def full_caption(plan: dict) -> str:
    tags = " ".join("#" + t for t in plan.get("hashtags", []))
    return f"{plan.get('caption', '')}\n\n{tags}".strip()[:2200]


# =========================================================================== drawing
@dataclass
class Look:
    bg: str = "#0E0E10"
    text: str = "#FFFFFF"
    muted: str = "#A1A1AA"
    accent: str = "#FFD60A"
    font: str = "Arial Black"
    handle: str = ""
    logo: Optional[str] = None


THEMES = ("dark", "light", "brand")


def look_for(theme: str, tpl=None, handle: str = "") -> Look:
    """dark / light / brand (the brand style's card color as background)."""
    h = tpl.headline if tpl else None
    accent, card, font = (h.highlight_color, h.background_color, h.font) if h else ("#FFD60A", "#E11D48", "Arial Black")
    logo = tpl.logo.path if tpl and tpl.logo.path and Path(tpl.logo.path).exists() else None
    if theme == "light":
        return Look("#F6F4EF", "#111111", "#55555C", card, font, handle, logo)
    if theme == "brand":
        return Look(card, "#FFFFFF", "#F4F4F5", accent if accent.lower() != card.lower() else "#111111", font, handle, logo)
    return Look("#0E0E10", "#FFFFFF", "#A1A1AA", accent, font, handle, logo)


_BOLD = {"arial black": ["ariblk.ttf", "Arial_Black.ttf"], "impact": ["impact.ttf", "Impact.ttf"],
         "segoe ui black": ["seguibl.ttf"], "poppins": ["Poppins-Bold.ttf", "Poppins-ExtraBold.ttf"],
         "montserrat": ["Montserrat-Bold.ttf", "Montserrat-ExtraBold.ttf"], "georgia": ["georgiab.ttf", "Georgia_Bold.ttf"],
         "verdana": ["verdanab.ttf", "Verdana_Bold.ttf"], "bahnschrift": ["bahnschrift.ttf"]}
_BOLD_FALLBACK = ["arialbd.ttf", "Arial_Bold.ttf", "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"]
_REGULAR = ["arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf", "DejaVuSans.ttf"]
_DEVANAGARI = ["NotoSansDevanagari-Bold.ttf", "NotoSansDevanagari-Regular.ttf", "Nirmala.ttc", "Mangal.ttf"]


@functools.cache
def _font_index() -> dict[str, str]:
    dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
            Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts"]
    idx: dict[str, str] = {}
    for d in dirs:
        if d.is_dir():
            for p in d.rglob("*"):
                if p.suffix.lower() in (".ttf", ".otf", ".ttc"):
                    idx.setdefault(p.name.lower(), str(p))
    return idx


def _font_file(names: list[str]) -> Optional[str]:
    idx = _font_index()
    return next((idx[n.lower()] for n in names if n.lower() in idx), None)


@functools.cache
def _font(family: str, bold: bool, size: int, script: str = "latin"):
    from PIL import ImageFont
    if script == "devanagari":
        names = _DEVANAGARI
    elif bold:
        names = _BOLD.get(family.lower(), []) + _BOLD_FALLBACK
    else:
        names = _REGULAR
    path = _font_file(names) or _font_file(_BOLD_FALLBACK + _REGULAR)
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size)


def _script(text: str) -> str:
    return "devanagari" if re.search(r"[\u0900-\u097F]", text) else "latin"


def _tokens(title: str) -> list[tuple[str, bool]]:
    """'Stop *wasting* time' → [(Stop, False), (wasting, True), (time, False)]."""
    out, hot = [], False
    for part in re.split(r"(\*)", title):
        if part == "*":
            hot = not hot
            continue
        out += [(w, hot) for w in part.split()]
    return out


def _wrap(tokens: list[tuple[str, bool]], font, width: int) -> list[list[tuple[str, bool]]]:
    lines, cur = [], []
    for tok in tokens:
        trial = " ".join(w for w, _ in cur + [tok])
        if cur and font.getlength(trial) > width:
            lines.append(cur)
            cur = [tok]
        else:
            cur.append(tok)
    return lines + ([cur] if cur else [])


def _fit(tokens, family: str, bold: bool, width: int, max_h: int, start: int, low: int, script: str):
    """Largest font size whose wrapped text fits the box."""
    size = start
    while True:
        f = _font(family, bold, size, script)
        lines = _wrap(tokens, f, width)
        lh = int(size * 1.18)
        if (len(lines) * lh <= max_h and all(f.getlength(w) <= width for line in lines for w, _ in line)) or size <= low:
            return f, lines, lh
        size -= 4


def _draw_lines(d, lines, font, lh: int, x: int, y: int, color: str, hot: str) -> int:
    space = font.getlength(" ")
    for line in lines:
        cx = x
        for w, is_hot in line:
            d.text((cx, y), w, font=font, fill=hot if is_hot else color)
            cx += font.getlength(w) + space
        y += lh
    return y


PHOTO_H = 600                                        # point/cta slides: photo band at the top


def _cover(path: Path, w: int, h: int):
    from PIL import Image, ImageOps
    return ImageOps.fit(Image.open(path).convert("RGB"), (w, h), Image.LANCZOS, centering=(0.5, 0.4))


def _shade(img, y0: int, y1: int, a0: int, a1: int) -> None:
    """Black vertical gradient (alpha a0 → a1) over rows y0..y1, so text on a photo stays readable."""
    from PIL import Image
    h = max(1, y1 - y0)
    band = Image.new("L", (1, h))
    band.putdata([int(a0 + (a1 - a0) * k / max(1, h - 1)) for k in range(h)])
    img.paste(Image.new("RGB", (img.width, h), "black"), (0, y0), band.resize((img.width, h)))


def _photo(plan_slide: dict, out: Path) -> Optional[Path]:
    f = (plan_slide.get("image") or {}).get("file")
    p = out / f if f and IMAGE_FILE.fullmatch(f) else None
    return p if p and p.exists() else None


def _slide(plan_slide: dict, i: int, n: int, look: Look, out: Path) -> Path:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), look.bg)
    kind, title, body = plan_slide["kind"], plan_slide["title"], plan_slide["body"]
    script = _script(title + body)
    family = look.font
    small = _font(family, False, 30, "latin")               # counter + handle
    width = W - 2 * PAD
    text, muted = look.text, look.muted
    photo = _photo(plan_slide, out)
    if photo and kind == "hook":                           # full-bleed photo, text at the bottom
        img.paste(_cover(photo, W, H))
        _shade(img, 0, 260, 150, 0)
        _shade(img, H // 3, H, 0, 235)
        text, muted = "#FFFFFF", "#E4E4E7"
    elif photo:                                            # photo band on top, text below
        img.paste(_cover(photo, W, PHOTO_H))
        _shade(img, 0, 220, 140, 0)
    d = ImageDraw.Draw(img)

    # accent bar + slide counter
    d.rectangle([PAD, PAD, PAD + 120, PAD + 14], fill=look.accent)
    counter = f"{i + 1}/{n}"
    d.text((W - PAD - small.getlength(counter), PAD - 10), counter, font=small, fill="#FFFFFF" if photo else muted)
    if look.logo:
        try:
            logo = Image.open(look.logo).convert("RGBA")
            logo.thumbnail((180, 90))
            img.paste(logo, (W - PAD - logo.width, H - PAD - logo.height), logo)
        except Exception:  # noqa: BLE001 - a broken logo file must not stop the post
            pass

    if kind == "hook":
        f, lines, lh = _fit(_tokens(title), family, True, width, 560 if photo else 720, 112 if photo else 120, 56, script)
        bf = blines = None
        if body:
            bf, blines, blh = _fit([(w, False) for w in body.split()], family, False, width, 200 if photo else 220,
                                   44, 30, script)
        if photo:                                          # sit the block just above the bottom row
            y = H - PAD - 110 - len(lines) * lh - (50 + len(blines) * blh if blines else 0)
        else:
            y = max(PAD + 120, (H - len(lines) * lh) // 2 - 80)
        y = _draw_lines(d, lines, f, lh, PAD, y, text, look.accent)
        if blines:
            _draw_lines(d, blines, bf, blh, PAD, y + 50, muted, look.accent)
        swipe = "Swipe  →"
        sf = _font(family, True, 40, "latin")
        d.text((W - PAD - sf.getlength(swipe), H - PAD - 50), swipe, font=sf, fill=look.accent)
    else:
        num_size = 96 if photo else 150
        num_h = int(num_size * 1.13) if kind == "point" else 0
        room = (H - PHOTO_H - 56 - PAD - 70) if photo else (H - 2 * PAD - 260)
        f, lines, lh = _fit(_tokens(title), family, True, width, min(420, room // 2) if photo else 420,
                            (76 if photo else 88) if kind == "point" else (88 if photo else 100), 44, script)
        bf = blines = None
        body_h = 0
        if body:
            bf, blines, blh = _fit([(w, False) for w in body.split()], family, False, width,
                                   room - num_h - len(lines) * lh - (40 if photo else 0), 46 if photo else 50, 28, script)
            blh = int(blh * 1.12)
            body_h = (40 if photo else 50) + len(blines) * blh
        block = num_h + len(lines) * lh + body_h
        if photo:
            top = PHOTO_H + 56
        else:                                              # center the whole block, a little above the middle
            top = max(PAD + 110, (H - block) // 2 - 40)
        if kind == "point":
            nf = _font(family, True, num_size, "latin")
            d.text((PAD - 6, top - num_size // 5), f"{i:02d}", font=nf, fill=look.accent)
        y = _draw_lines(d, lines, f, lh, PAD, top + num_h, text, look.accent)
        if blines:
            _draw_lines(d, blines, bf, blh, PAD, y + (40 if photo else 50), muted if kind == "cta" else text, look.accent)
        if kind == "cta" and look.handle:
            hf = _font(family, True, 48, "latin")
            d.text((PAD, H - PAD - 60), look.handle, font=hf, fill=look.accent)
    if look.handle and kind != "cta":
        d.text((PAD, H - PAD - 34), look.handle, font=small, fill=muted)

    path = out / f"slide_{i + 1:02d}.jpg"
    img.save(path, "JPEG", quality=92, optimize=True)         # Instagram carousels take JPEG only
    return path


def render(plan: dict, out: Path, look: Look) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("slide_*.jpg"):
        old.unlink()
    n = len(plan["slides"])
    return [_slide(s, i, n, look, out) for i, s in enumerate(plan["slides"])]
