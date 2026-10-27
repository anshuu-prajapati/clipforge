#!/usr/bin/env bash
# ClipForge server setup for Ubuntu — idempotent, safe to run again after updates.
#   sudo bash deploy/setup.sh clip.example.com
# Expects the code in /opt/clipforge and the secrets in /var/lib/clipforge/.env (chmod 600).
set -euo pipefail
DOMAIN="${1:?usage: sudo bash setup.sh <domain>}"
APP=/opt/clipforge
DATA=/var/lib/clipforge
export DEBIAN_FRONTEND=noninteractive

echo "== swap (keeps small instances from running out of memory)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== system packages"
apt-get update -q
apt-get install -y -q ffmpeg curl ca-certificates debian-keyring debian-archive-keyring apt-transport-https \
  fonts-dejavu-core fonts-liberation2 fonts-noto-core >/dev/null
# Arial Black / Impact for the default caption + hook fonts (optional; falls back to Liberation/Noto)
echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections
apt-get install -y -q ttf-mscorefonts-installer >/dev/null 2>&1 || echo "   (Microsoft core fonts unavailable — using fallback fonts)"
if ! command -v caddy >/dev/null; then
  apt-get install -y -q caddy >/dev/null 2>&1 || {
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -q && apt-get install -y -q caddy >/dev/null; }
fi

echo "== service user and folders"
id clipforge >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --create-home --shell /usr/sbin/nologin clipforge
mkdir -p "$DATA/jobs"
chown -R clipforge:clipforge "$DATA"
chmod 750 "$DATA"
[ -f "$DATA/.env" ] && chown clipforge:clipforge "$DATA/.env" && chmod 600 "$DATA/.env"

echo "== Python 3.12 environment (uv)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
fi
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
[ -x "$APP/.venv/bin/python" ] || uv venv --python 3.12 "$APP/.venv" >/dev/null
uv pip install --python "$APP/.venv/bin/python" -q -r "$APP/requirements.txt"
chmod -R a+rX /opt/uv-python "$APP"

echo "== systemd service"
install -m 644 "$APP/deploy/clipforge.service" /etc/systemd/system/clipforge.service
systemctl daemon-reload
systemctl enable --now clipforge >/dev/null
systemctl restart clipforge

echo "== Caddy (HTTPS for $DOMAIN)"
sed "s/__DOMAIN__/$DOMAIN/" "$APP/deploy/Caddyfile" > /etc/caddy/Caddyfile
mkdir -p /var/log/caddy && chown caddy:caddy /var/log/caddy 2>/dev/null || true
caddy validate --config /etc/caddy/Caddyfile >/dev/null
systemctl enable --now caddy >/dev/null
systemctl reload caddy || systemctl restart caddy

echo "== CI deploy user (GitHub Actions: upload a release + run the update script, nothing else)"
echo "$DOMAIN" > /etc/clipforge-domain
install -m 755 "$APP/deploy/update.sh" /usr/local/sbin/clipforge-update
id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
touch /home/deploy/.ssh/authorized_keys && chown deploy:deploy /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys
echo "deploy ALL=(root) NOPASSWD: /usr/local/sbin/clipforge-update" > /etc/sudoers.d/clipforge-deploy
chmod 440 /etc/sudoers.d/clipforge-deploy
visudo -cf /etc/sudoers.d/clipforge-deploy >/dev/null

echo "== done: https://$DOMAIN"
