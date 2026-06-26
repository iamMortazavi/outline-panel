#!/usr/bin/env bash
#
# Outline Panel — one-command installer for Debian/Ubuntu.
#
#   sudo bash install.sh            # install / update
#   sudo bash install.sh uninstall  # remove service (keeps /opt/outline-panel)
#
# Installs into /opt/outline-panel, creates a venv, writes .env, and sets up a
# systemd service (outline-panel). Run as root.
set -euo pipefail

APP_DIR="/opt/outline-panel"
SERVICE="outline-panel"
PORT="${PORT:-8000}"

need_root() { [ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)."; exit 1; }; }

uninstall() {
  need_root
  systemctl disable --now "$SERVICE" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SERVICE}.service"
  systemctl daemon-reload
  echo "Removed the $SERVICE service. App files remain in $APP_DIR."
  exit 0
}

[ "${1:-}" = "uninstall" ] && uninstall

need_root
echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync >/dev/null

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "==> Copying source to $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete --exclude '.git' --exclude '.venv' --exclude '*.db*' \
      "$SRC_DIR/" "$APP_DIR/"

echo "==> Creating virtualenv & installing"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet "$APP_DIR"

ENV_FILE="$APP_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "==> First-time setup"
  read -rp "Admin password for the panel: " ADMIN_PW
  SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
  cat > "$ENV_FILE" <<EOF
ADMIN_PASSWORD=${ADMIN_PW}
SESSION_SECRET=${SECRET}
# auto = Secure cookie only over HTTPS, so login works on http://IP:8000 too.
# Behind an HTTPS reverse proxy it becomes Secure automatically.
COOKIE_SECURE=auto
DB_PATH=${APP_DIR}/outline_bot.db
HOST=0.0.0.0
PORT=${PORT}
EOF
  chmod 600 "$ENV_FILE"
  echo "    Wrote $ENV_FILE (servers, bot token & password are managed from the panel)."
else
  echo "==> Keeping existing $ENV_FILE"
fi

echo "==> Installing systemd service"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Outline Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/outline-panel
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE"
sleep 2
systemctl --no-pager --lines=0 status "$SERVICE" || true

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "============================================================"
echo " Outline Panel is running."
echo "   URL:    http://${IP:-YOUR_SERVER_IP}:${PORT}"
echo "   Login:  the admin password you just set"
echo
echo " Put it behind HTTPS (Caddy/Nginx/Cloudflare) before public use."
echo " Manage:  systemctl {status|restart|stop} ${SERVICE}"
echo " Reset pw: ${APP_DIR}/.venv/bin/outline-panel-admin reset-password"
echo "============================================================"
