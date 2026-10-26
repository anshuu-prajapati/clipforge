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


from .env import load as _load_dotenv


def _publish_cli(a) -> None:
    from pathlib import Path

    from . import publish, social
    out = Path(a.out)
    m = json.loads((out / "clips.json").read_text(encoding="utf-8"))
    c = next(x for x in m["clips"] if x["id"] == a.clip_id)
    s = social.generate(c, m.get("language", "en"))
    cover = out / f"{a.clip_id}.cover.jpg"
    cover_ms = c["cover_ms"] if "cover_ms" in c and cover.exists() else social.pick_cover(out / c["file"], cover)
    show = lambda st, pc: print(f"\r  {st:<36} {pc * 100:3.0f}%", end="", flush=True)  # noqa: E731
    for platform in a.to.split(","):
        print(f"{platform}:")
        if platform == "youtube":
            y = s["youtube"]
            r = publish.youtube_upload(out / c["file"], y["title"], y["description"], y["tags"],
                                       a.privacy, thumbnail=cover, progress=show)
        else:
            r = publish.instagram_upload(out / c["file"], s["instagram"]["caption"], cover_ms, progress=show)
        print(f"\n  {r['url']}" + (f"\n  note: {r['note']}" if r.get("note") else ""))


def main(argv=None) -> int:
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning, module=r"google\.")   # "upgrade Python by 2027" notice
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

    sub.add_parser("accounts", help="show connected YouTube / Instagram accounts")
    c = sub.add_parser("connect", help="connect a platform account")
    c.add_argument("platform", choices=["youtube"])
    p = sub.add_parser("publish", help="upload a rendered clip")
    p.add_argument("out")
    p.add_argument("clip_id")
    p.add_argument("--to", default="youtube,instagram", help="youtube,instagram")
    p.add_argument("--privacy", default="public", choices=["public", "unlisted", "private"])
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

            from . import storage
            from .db import DatabaseError, db
            try:
                d = db()
                print(f"  MongoDB      connected (database '{d.name}', {d.users.estimated_document_count()} users)")
            except DatabaseError as err:
                print(f"  MongoDB      NOT connected — {err}")
            if storage.enabled():
                try:
                    print(f"  Cloudinary   connected (cloud '{storage.check()}')")
                except Exception as err:  # noqa: BLE001
                    print(f"  Cloudinary   NOT connected — {str(err)[:160]}")
            else:
                print("  Cloudinary   off — add CLOUDINARY_URL to .env (clips stay on this PC)")
            from . import mailer
            print("  Email        " + (f"SMTP {os.environ['SMTP_HOST']}" if mailer.configured()
                                      else "off — password-reset links are printed here (add SMTP_HOST to .env)"))
            print(f"  Dashboard    http://localhost:{a.port}\n", flush=True)
            uvicorn.run("clipforge.server:app", host="0.0.0.0", port=a.port)
        elif a.cmd == "accounts":
            from .publish import accounts
            for name, st in accounts().items():
                state = f"connected as {st['name']}" if st["connected"] else st.get("error") or (
                    "not connected" if st["configured"] else "not configured")
                if st["connected"] and st.get("error"):
                    state += f"  ⚠ {st['error']}"
                print(f"  {name:<10} {state}")
        elif a.cmd == "connect":
            from .publish import youtube_connect
            acct = youtube_connect()
            print(f"YouTube connected: {acct.get('title')}")
        elif a.cmd == "publish":
            _publish_cli(a)
    except ClipforgeError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    except RuntimeError as err:          # PublishError and friends: one clear line, no traceback
        if type(err).__name__ != "PublishError":
            raise
        print(f"\nerror: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
