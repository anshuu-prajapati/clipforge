#!/usr/bin/env bash
# Installed as /usr/local/sbin/clipforge-update (root-owned). CI uploads the release to
# /home/deploy/release.tgz, then runs:  sudo /usr/local/sbin/clipforge-update
# Swaps in the new code, installs dependencies, restarts, and rolls back if the app doesn't come up.
set -euo pipefail
REL=/home/deploy/release.tgz
APP=/opt/clipforge
NEXT=/opt/clipforge.next
PREV=/opt/clipforge.prev
DOMAIN=$(cat /etc/clipforge-domain)
export UV_PYTHON_INSTALL_DIR=/opt/uv-python

[ -s "$REL" ] || { echo "no release uploaded at $REL"; exit 1; }
rm -rf "$NEXT" && mkdir -p "$NEXT"
tar -xzf "$REL" -C "$NEXT" && rm -f "$REL"
[ -f "$NEXT/clipforge/server.py" ] || { echo "release is missing clipforge/server.py"; rm -rf "$NEXT"; exit 1; }

healthy() {
  for _ in $(seq 1 40); do
    curl -sf -m 3 http://127.0.0.1:8000/auth/config >/dev/null && return 0
    sleep 3
  done
  return 1
}

echo "== switching to $(cat "$NEXT/REVISION" 2>/dev/null || echo new release)"
mv "$APP/.venv" "$NEXT/.venv"                 # keep the installed Python environment
rm -rf "$PREV" && mv "$APP" "$PREV" && mv "$NEXT" "$APP"

echo "== dependencies"
uv pip install --python "$APP/.venv/bin/python" -q -r "$APP/requirements.txt"
chmod -R a+rX "$APP"

echo "== service + proxy config"
install -m 644 "$APP/deploy/clipforge.service" /etc/systemd/system/clipforge.service
install -m 755 "$APP/deploy/update.sh" /usr/local/sbin/clipforge-update.next && mv /usr/local/sbin/clipforge-update.next /usr/local/sbin/clipforge-update
sed "s/__DOMAIN__/$DOMAIN/" "$APP/deploy/Caddyfile" > /etc/caddy/Caddyfile.next
if caddy validate --config /etc/caddy/Caddyfile.next >/dev/null 2>&1; then
  mv /etc/caddy/Caddyfile.next /etc/caddy/Caddyfile && systemctl reload caddy
else
  echo "   new Caddyfile invalid — keeping the current one"; rm -f /etc/caddy/Caddyfile.next
fi
systemctl daemon-reload
systemctl restart clipforge

if healthy; then
  echo "== deployed $(cat "$APP/REVISION" 2>/dev/null || true) — healthy"
  exit 0
fi

echo "!! new version did not start — rolling back"
journalctl -u clipforge -n 40 --no-pager -o cat || true
mv "$APP/.venv" "$PREV/.venv"
rm -rf "$NEXT" && mv "$APP" "$NEXT" && mv "$PREV" "$APP"
systemctl restart clipforge
healthy && echo "== rolled back to the previous version" || echo "!! rollback also unhealthy — check: journalctl -u clipforge"
exit 1
