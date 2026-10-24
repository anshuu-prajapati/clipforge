""".env handling: load KEY=VALUE pairs into the environment, and write values back
(OAuth refresh tokens, refreshed Instagram tokens) without touching the other lines."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

ENV_FILE = Path(os.environ.get("CLIPFORGE_ENV_FILE", ".env"))
_lock = threading.Lock()


def load(path: Path = ENV_FILE) -> None:
    """Read KEY=VALUE lines from .env into the environment (real env vars win)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in text.splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k and not k.startswith("#"):
            k, v = k.strip(), v.strip().strip("'\"")
            if k.endswith(("_KEY", "_TOKEN", "_SECRET")) and os.environ.get(k, v) != v:
                print(f"warning: {k} is set in this terminal and overrides .env "
                      f"(run `Remove-Item Env:{k}` to use .env)", file=sys.stderr)
            os.environ.setdefault(k, v)


def save(key: str, value: str, path: Path = ENV_FILE) -> None:
    """Set KEY=value in .env (replace the line or append it) and in the environment."""
    with _lock:
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
        for i, line in enumerate(lines):
            if line.strip().partition("=")[0].strip() == key:
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(path)
        os.environ[key] = value
