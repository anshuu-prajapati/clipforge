"""Brand template: how every clip looks.

Mirrors the sections Kaleidly exposes in its "Brand templates" editor
(Layout, Captions, Headline/Hook, Logo, Intro & outro, Music) and adds two
motion-graphic extras (progress bar, punch-in zooms).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Literal, Optional


@dataclass
class LayoutStyle:
    aspect: Literal["9:16", "1:1", "16:9"] = "9:16"
    # fit  = whole frame, letterboxed over a blurred copy (Kaleidly's current mode)
    # fill = cropped to fill, following the speaker's face (Kaleidly marks this "coming soon")
    framing: Literal["fit", "fill"] = "fill"
    blur_background: bool = True
    # fit mode: how much of the frame height the foreground video takes (zoomed fit)
    fit_zoom: float = 1.0


@dataclass
class CaptionStyle:
    show: bool = True
    font: str = "Poppins"
    weight: int = 800                      # 400/600/700/800/900
    size: Literal["s", "m", "l"] = "m"
    position_pct: float = 68.0             # % from the top
    text_color: str = "#FFFFFF"
    highlight_color: str = "#FFD60A"
    outline: bool = True
    outline_color: str = "#000000"
    background: Literal["none", "shadow", "box"] = "shadow"
    background_color: str = "#000000"
    max_words: int = 3                     # words on screen
    uppercase: bool = True
    # none | word (one word at a time) | karaoke (line + active word) | pop (words pop in as spoken)
    animation: Literal["none", "word", "karaoke", "pop"] = "pop"
    emphasis: bool = True                  # LLM-picked keywords get the highlight colour + scale


@dataclass
class HeadlineStyle:
    show: bool = True
    position: Literal["top", "bottom"] = "top"
    style: Literal["card", "band", "plain"] = "card"
    size_px: int = 72
    duration_s: float = 4.0                # time on screen
    text_color: str = "#111111"
    background_color: str = "#FFFFFF"
    uppercase: bool = False


@dataclass
class LogoStyle:
    path: Optional[str] = None
    position: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = "top-right"
    width_pct: float = 0.16
    opacity: float = 0.9


@dataclass
class IntroOutro:
    intro_path: Optional[str] = None
    outro_path: Optional[str] = None


@dataclass
class MusicStyle:
    path: Optional[str] = None
    volume: float = 0.12
    duck: bool = True                      # sidechain-duck under speech


@dataclass
class MotionStyle:
    progress_bar: bool = True
    progress_color: str = "#FFD60A"
    progress_height: int = 10
    punch_in: bool = True                  # alternate 1.0x / 1.1x zoom at sentence boundaries
    punch_scale: float = 1.10


@dataclass
class BrandTemplate:
    name: str = "Default"
    layout: LayoutStyle = field(default_factory=LayoutStyle)
    captions: CaptionStyle = field(default_factory=CaptionStyle)
    headline: HeadlineStyle = field(default_factory=HeadlineStyle)
    logo: LogoStyle = field(default_factory=LogoStyle)
    intro_outro: IntroOutro = field(default_factory=IntroOutro)
    music: MusicStyle = field(default_factory=MusicStyle)
    motion: MotionStyle = field(default_factory=MotionStyle)
    fonts_dir: Optional[str] = None
    resolution: int = 1080                 # short side in px (720 = "free tier")

    # ---- geometry -------------------------------------------------------
    @property
    def size(self) -> tuple[int, int]:
        r = self.resolution
        return {"9:16": (r, round(r * 16 / 9)), "1:1": (r, r), "16:9": (round(r * 16 / 9), r)}[self.layout.aspect]

    # ---- (de)serialisation ---------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def from_dict(cls, d: dict) -> "BrandTemplate":
        return _build(cls, d)

    @classmethod
    def load(cls, path: str | Path) -> "BrandTemplate":
        # bytes → json auto-detects UTF-8/16 + BOM (PowerShell 5's `>` writes UTF-16)
        return cls.from_dict(json.loads(Path(path).read_bytes()))


def _build(cls, d):
    kwargs = {}
    for f in fields(cls):
        if f.name not in d:
            continue
        v = d[f.name]
        default = f.default_factory() if callable(getattr(f, "default_factory", None)) else None  # type: ignore[misc]
        if is_dataclass(default) and isinstance(v, dict):
            v = _build(type(default), v)
        kwargs[f.name] = v
    return cls(**kwargs)
