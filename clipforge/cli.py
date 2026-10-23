"""clipforge CLI.

  python -m clipforge run  VIDEO_OR_URL -o out/ [--template brand.json] [--length auto|short|medium|long]
  python -m clipforge rerender out/ clip_02 [--hook "New hook"] [--trim 61.2 95.0] [--template brand.json]
  python -m clipforge template > brand.json      # dump the default template to edit
  python -m clipforge serve [--port 8000]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .media import ClipforgeError
from .template import BrandTemplate


def _load_dotenv(path: str = ".env") -> None:
    """Read KEY=VALUE lines from ./.env into the environment (real env vars win)."""
    try:
        text = open(path, encoding="utf-8-sig").read()
    except OSError:
        return
    for line in text.splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k and not k.startswith("#"):
            k, v = k.strip(), v.strip().strip("'\"")
            if k.endswith("_KEY") and os.environ.get(k, v) != v:
                print(f"warning: {k} is set in this terminal and overrides .env "
                      f"(run `Remove-Item Env:{k}` to use .env)", file=sys.stderr)
            os.environ.setdefault(k, v)


def main(argv=None) -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser(prog="clipforge")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="find the best moments and render clips")
    r.add_argument("source")
    r.add_argument("-o", "--out", default="clipforge_out")
    r.add_argument("--template")
    r.add_argument("--length", default="auto",
                   help="auto | short | medium | long, or a custom range in seconds like 50-60")
    r.add_argument("--max-clips", type=int, default=6)
    r.add_argument("--language")
    r.add_argument("--whisper-model")

    e = sub.add_parser("rerender", help="re-render one clip after edits")
    e.add_argument("out")
    e.add_argument("clip_id")
    e.add_argument("--hook")
    e.add_argument("--trim", nargs=2, type=float, metavar=("START", "END"))
    e.add_argument("--template")

    sub.add_parser("template", help="print the default brand template JSON")
    s = sub.add_parser("serve", help="start the HTTP API")
    s.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):   # Windows consoles/pipes default to cp1252 (no “→”)
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        if a.cmd == "template":
            print(json.dumps(BrandTemplate().to_dict(), indent=2))
        elif a.cmd == "run":
            from .hooks import parse_length
            from .pipeline import run_pipeline
            parse_length(a.length)                   # fail fast, before download/transcription
            tpl = BrandTemplate.load(a.template) if a.template else None
            m = run_pipeline(a.source, a.out, tpl, a.length, a.max_clips, a.language, a.whisper_model)
            print(f"\n{len(m['clips'])} clips in {a.out}  ({m['elapsed_s']}s)")
            for c in m["clips"]:
                g = c["grades"]
                print(f"  {c['id']}  score {c['score']:>3}  [hook {g['hook']} flow {g['flow']} value {g['value']} "
                      f"trend {g['trend']}]  {c['start']:.1f}-{c['end']:.1f}s  “{c['hook']}”")
        elif a.cmd == "rerender":
            from .pipeline import rerender
            tpl = BrandTemplate.load(a.template) if a.template else None
            c = rerender(a.out, a.clip_id, hook=a.hook, trim=tuple(a.trim) if a.trim else None, tpl=tpl)
            print(f"re-rendered {c['file']}")
        elif a.cmd == "serve":
            import uvicorn
            uvicorn.run("clipforge.server:app", host="0.0.0.0", port=a.port)
    except ClipforgeError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
