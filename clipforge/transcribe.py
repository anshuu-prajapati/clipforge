"""Step 1 — transcribe: word-level timestamps with faster-whisper."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Sentence:
    idx: int
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    energy: float = 0.0          # 0..1 loudness rank within the episode
    rate: float = 0.0            # words per second


@dataclass
class Transcript:
    language: str
    duration: float
    words: list[Word]

    def save(self, path: Path) -> None:
        path.write_text(json.dumps({"language": self.language, "duration": self.duration,
                                    "words": [asdict(w) for w in self.words]}), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Transcript":
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(d["language"], d["duration"], [Word(**w) for w in d["words"]])

    def words_between(self, start: float, end: float) -> list[Word]:
        return [w for w in self.words if start <= (w.start + w.end) / 2 <= end]


def _add_cuda_dlls() -> None:
    """Windows: make pip-installed nvidia-cublas/cudnn DLLs findable by ctranslate2."""
    if os.name != "nt":
        return
    try:
        import nvidia
    except ImportError:
        return
    for root in nvidia.__path__:
        for bin_dir in Path(root).glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def _device() -> str:
    """GPU when available (CLIPFORGE_CUDA=0 forces CPU, =1 requires GPU)."""
    flag = os.environ.get("CLIPFORGE_CUDA")
    if flag == "0":
        return "cpu"
    import ctranslate2
    if flag == "1" or ctranslate2.get_cuda_device_count() > 0:
        _add_cuda_dlls()
        return "cuda"
    return "cpu"


def transcribe(audio: Path, model_size: str | None = None, language: str | None = None,
               on_progress=None) -> Transcript:
    """on_progress(pct) is called every 10%; default prints to the console."""
    on_progress = on_progress or (lambda pct: print(f"  transcribe   {pct}%", flush=True))
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    model_size = model_size or os.environ.get("CLIPFORGE_WHISPER", "small")
    device = _device()
    print(f"  transcribe   whisper {model_size} on {device}", flush=True)
    model = WhisperModel(model_size, device=device, compute_type="float16" if device == "cuda" else "int8")
    # pass decoded 16 kHz mono samples (avoids PyAV version issues inside faster-whisper)
    import wave

    import numpy as np
    with wave.open(str(audio)) as w:
        samples = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    if device == "cuda":   # batched: decodes VAD chunks in parallel, many × faster than sequential
        segments, info = BatchedInferencePipeline(model).transcribe(
            samples, word_timestamps=True, language=language, beam_size=5, batch_size=16)
    else:
        segments, info = model.transcribe(samples, word_timestamps=True, vad_filter=True,
                                          language=language, beam_size=5, condition_on_previous_text=False)
    words: list[Word] = []
    shown = 0
    for seg in segments:
        pct = int(100 * seg.end / max(1.0, info.duration))
        if pct >= shown + 10:
            shown = pct - pct % 10
            on_progress(shown)
        for w in seg.words or []:
            t = w.word.strip()
            if not t:
                continue
            # whisper splits "12,000" → "12" ",000" and "don't" → "don" "'t": glue them back
            if words and t[0] in ",.!?%'’:;)":
                words[-1] = Word(words[-1].text + t, words[-1].start, round(w.end, 3))
            else:
                words.append(Word(t, round(w.start, 3), round(w.end, 3)))
    return Transcript(info.language, info.duration, words)


_END = re.compile(r"[.!?…]['\")\]]*$")


def sentences(tr: Transcript, max_len: float = 14.0, pause: float = 0.75) -> list[Sentence]:
    """Group words into sentence-like units (punctuation, long pauses, or max length)."""
    out: list[Sentence] = []
    cur: list[Word] = []

    def flush():
        if cur:
            out.append(Sentence(len(out), cur[0].start, cur[-1].end,
                                " ".join(w.text for w in cur), list(cur)))
            cur.clear()

    for i, w in enumerate(tr.words):
        if cur and (w.start - cur[-1].end > pause or w.end - cur[0].start > max_len):
            flush()
        cur.append(w)
        if _END.search(w.text):
            flush()
    flush()
    return out


def annotate_energy(sents: list[Sentence], env_db, hop: float) -> None:
    import numpy as np
    vals = []
    for s in sents:
        a, b = int(s.start / hop), max(int(s.end / hop), int(s.start / hop) + 1)
        seg = env_db[a:b]
        vals.append(float(np.percentile(seg, 75)) if len(seg) else -60.0)
        s.rate = len(s.words) / max(0.3, s.end - s.start)
    order = np.argsort(np.argsort(vals))
    for s, r in zip(sents, order):
        s.energy = round(float(r) / max(1, len(sents) - 1), 2)
