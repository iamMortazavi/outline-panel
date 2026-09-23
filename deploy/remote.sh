#!/usr/bin/env bash
#
# Outline Panel — server side of a deploy. Run as root on Debian/Ubuntu;
# deploy/deploy.sh uploads a release and runs this for you.
#
#   bash remote.sh <release.tar.gz> <release-id> [domain]
#
# Idempotent: the first run provisions, every later run ships a release.
#
#   /opt/outline-panel/releases/<id>   unpacked releases (last 3 kept)
#   /opt/outline-panel/current         symlink to the live release
#   /opt/outline-panel/venv            one virtualenv, reused across releases
#   /etc/outline-panel/env             configuration and secrets (0640)
#   /var/lib/outline-panel/            the SQLite database and its backups
#
# A release goes live only once /healthz answers; if it does not, the previous
# release is put back and the deploy fails loudly. The database is backed up
# before every restart.
set -Eeuo pipefail

TARBALL="${1:?usage: remote.sh <release.tar.gz> <release-id> [domain]}"
REL_ID="${2:?release id}"
DOMAIN="${3:-}"

APP=outline-panel
USER_=outline-panel
BASE=/opt/outline-panel
ETC=/etc/outline-panel
DATA=/var/lib/outline-panel
ENV_FILE="$ETC/env"
PORT="${PORT:-8000}"
KEEP_RELEASES=3
KEEP_BACKUPS=7

say()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m  !\033[0m %s\n' "$*" >&2; }
die()  { warn "$*"; exit 1; }
trap 'warn "failed at line $LINENO: $BASH_COMMAND"' ERR

[ "$(id -u)" -eq 0 ] || die "run as root"
command -v apt-get >/dev/null || die "Debian/Ubuntu only (apt-get not found)"

# ------------------------------------------------------------------ system
say "System: $(. /etc/os-release; echo "$PRETTY_NAME") · $(nproc) CPU · $(free -m | awk '/Mem:/{print $2}') MB RAM · $(df -BM --output=avail / | tail -1 | tr -d ' ') free"

need_pkgs=()
for p in python3 python3-venv python3-pip ca-certificates curl gzip iproute2; do
  dpkg -s "$p" >/dev/null 2>&1 || need_pkgs+=("$p")
done
if [ ${#need_pkgs[@]} -gt 0 ]; then
  say "Installing ${need_pkgs[*]}"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends "${need_pkgs[@]}" >/dev/null
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || die "Python 3.10+ required, found $(python3 -V)"

# A small VPS with no swap gets OOM-killed on the first pip build or a
# memory spike. 1 GB of swap, used only under pressure, is cheap insurance.
mem_mb=$(free -m | awk '/Mem:/{print $2}')
if [ "$mem_mb" -lt 2048 ] && [ "$(swapon --noheadings | wc -l)" -eq 0 ] \
   && [ "$(df -BM --output=avail / | tail -1 | tr -dc 0-9)" -gt 3072 ] \
   && [ ! -e /swapfile ]; then
  say "No swap on a ${mem_mb} MB machine: adding a 1 GB /swapfile"
  fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
  chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  printf 'vm.swappiness=10\nvm.vfs_cache_pressure=50\n' > /etc/sysctl.d/90-outline-panel.conf
  sysctl -q --system || true
fi

# ------------------------------------------------------------ user + dirs
id "$USER_" >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$USER_"
install -d -m 0755 "$BASE" "$BASE/releases"
install -d -m 0750 -o root -g "$USER_" "$ETC"
install -d -m 0750 -o "$USER_" -g "$USER_" "$DATA" "$DATA/backups"

# -------------------------------------------- the legacy install.sh layout
# install.sh kept .env and the database inside /opt/outline-panel. Carry
# them over once; the originals are left where they were, untouched.
if [ ! -f "$ENV_FILE" ] && [ -f "$BASE/.env" ]; then
  say "Migrating the install.sh layout (originals are kept)"
  systemctl stop "$APP" 2>/dev/null || true
  systemctl disable --now outline-panel-bot 2>/dev/null || true
  legacy_db=$(sed -n 's/^DB_PATH=//p' "$BASE/.env" | tail -1)
  legacy_db="${legacy_db:-$BASE/outline_bot.db}"
  install -m 0640 -o root -g "$USER_" "$BASE/.env" "$ENV_FILE"
  if [ -f "$legacy_db" ]; then
    # the online-backup API, so a -wal file is folded in consistently
    python3 - "$legacy_db" "$DATA/outline_bot.db" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
    chown "$USER_:$USER_" "$DATA/outline_bot.db"
  fi
fi

# ------------------------------------------------------------ configuration
set_env() {  # set_env KEY VALUE — replace or append, touching nothing else
  local k="$1" v="$2"
  if grep -q "^${k}=" "$ENV_FILE"; then
    sed -i "s|^${k}=.*|${k}=${v}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$k" "$v" >> "$ENV_FILE"
  fi
}

NEW_PASSWORD=""
if [ ! -f "$ENV_FILE" ]; then
  say "First install: writing $ENV_FILE"
  NEW_PASSWORD="${ADMIN_PASSWORD:-$(python3 -c 'import secrets;print(secrets.token_urlsafe(15))')}"
  umask 027
  cat > "$ENV_FILE" <<EOF
# Outline Panel. Servers, the bot token and admins are managed in the panel.
ADMIN_PASSWORD=${NEW_PASSWORD}
SESSION_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
COOKIE_SECURE=auto
EOF
  umask 022
fi
set_env DB_PATH "$DATA/outline_bot.db"
set_env PORT "$PORT"
# one small interpreter: fewer malloc arenas is the single biggest RSS saving
# for a threaded CPython on a small box
set_env MALLOC_ARENA_MAX 2
set_env PYTHONUNBUFFERED 1
chown root:"$USER_" "$ENV_FILE"; chmod 0640 "$ENV_FILE"

# ----------------------------------------------------------------- release
REL="$BASE/releases/$REL_ID"
PREV=$(readlink -f "$BASE/current" 2>/dev/null || true)
say "Unpacking release $REL_ID"
rm -rf "$REL.tmp" && mkdir -p "$REL.tmp"
tar -xzf "$TARBALL" -C "$REL.tmp"
rm -rf "$REL" && mv "$REL.tmp" "$REL"

# Precompress the static assets once, here, so the proxy serves .gz files
# straight off disk instead of compressing on every request.
find "$REL/src/outline_panel/static" -type f \
  \( -name '*.js' -o -name '*.css' -o -name '*.html' -o -name '*.svg' -o -name '*.txt' \) \
  -size +1k -exec gzip -9 -k -f -n {} +

VENV="$BASE/venv"
if [ ! -x "$VENV/bin/python" ]; then
  say "Creating the virtualenv"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip wheel
fi
install_release() {
  # a regular (not editable) install: the venv holds a frozen copy, so a
  # half-written release directory can never be what is running
  "$VENV/bin/pip" install -q --no-cache-dir --upgrade-strategy only-if-needed "$1"
  "$VENV/bin/python" -m compileall -q -j 0 "$("$VENV/bin/python" -c 'import outline_panel,os;print(os.path.dirname(outline_panel.__file__))')" || true
}
say "Installing into $VENV"
install_release "$REL"

# ------------------------------------------------------------------ backup
if [ -f "$DATA/outline_bot.db" ]; then
  b="$DATA/backups/pre-${REL_ID}-$(date +%Y%m%d-%H%M%S).db"
  say "Backing up the database → $b"
  python3 - "$DATA/outline_bot.db" "$b" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
  chown "$USER_:$USER_" "$b"; chmod 0640 "$b"
  ls -1t "$DATA"/backups/pre-*.db 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f
fi

# ------------------------------------------------------------------ reverse proxy
port_owner() { ss -Hltnp "sport = :$1" 2>/dev/null | grep -o 'users:(("[^"]*' | head -1 | cut -d'"' -f2; }
USE_CADDY=0
if [ -n "$DOMAIN" ]; then
  o80=$(port_owner 80); o443=$(port_owner 443)
  if { [ -z "$o80" ] || [ "$o80" = caddy ]; } && { [ -z "$o443" ] || [ "$o443" = caddy ]; }; then
    USE_CADDY=1
  else
    warn "ports 80/443 are held by '${o80:-}${o443:+ / $o443}': not installing Caddy."
    warn "The panel will listen on :$PORT directly; put it behind your existing proxy."
  fi
fi
if [ "$USE_CADDY" = 1 ]; then
  set_env HOST 127.0.0.1
  set_env TRUST_PROXY true
else
  set_env HOST 0.0.0.0
  set_env TRUST_PROXY false
fi

# ------------------------------------------------------------------ systemd
say "Installing the systemd unit"
systemctl disable --now outline-panel-bot 2>/dev/null || true   # the bot runs in-process
sed -e "s|@BASE@|$BASE|g" -e "s|@ETC@|$ETC|g" -e "s|@DATA@|$DATA|g" -e "s|@USER@|$USER_|g" \
  "$REL/deploy/outline-panel.service" > /etc/systemd/system/$APP.service
systemctl daemon-reload
systemctl enable -q "$APP"

# The admin CLI, run exactly as the service runs: same user, same env file.
cat > /usr/local/bin/outline-panel-admin <<EOF
#!/bin/sh
exec systemd-run --quiet --pty --wait --collect -p User=$USER_ -p Group=$USER_ \\
  -p EnvironmentFile=$ENV_FILE -p WorkingDirectory=$DATA $VENV/bin/outline-panel-admin "\$@"
EOF
chmod 0755 /usr/local/bin/outline-panel-admin

switch_to() { ln -sfn "$1" "$BASE/current.new" && mv -Tf "$BASE/current.new" "$BASE/current"; }
healthy() {
  for _ in $(seq 1 40); do
    curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  return 1
}

switch_to "$REL"
say "Restarting $APP"
systemctl restart "$APP"
if ! healthy; then
  warn "release $REL_ID did not come up:"
  journalctl -u "$APP" -n 40 --no-pager >&2 || true
  if [ -n "$PREV" ] && [ -d "$PREV" ] && [ "$PREV" != "$REL" ]; then
    warn "rolling back to $(basename "$PREV")"
    install_release "$PREV"
    switch_to "$PREV"
    systemctl restart "$APP"
    healthy && warn "rolled back; the previous release is serving" || warn "the rollback did not come up either"
  fi
  exit 1
fi

if [ "$USE_CADDY" = 1 ]; then
  if ! command -v caddy >/dev/null; then
    say "Installing Caddy (automatic HTTPS)"
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null 2>&1 || true
    if curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
         | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null \
       && curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
         > /etc/apt/sources.list.d/caddy-stable.list \
       && apt-get update -qq && apt-get install -y -qq caddy >/dev/null; then :
    else
      # distro package as the fallback (older, but maintained)
      rm -f /etc/apt/sources.list.d/caddy-stable.list
      apt-get update -qq && apt-get install -y -qq caddy >/dev/null \
        || die "could not install Caddy; the panel is up on 127.0.0.1:$PORT"
    fi
  fi
  if [ -f /etc/caddy/Caddyfile ] && ! grep -q 'managed by outline-panel' /etc/caddy/Caddyfile; then
    cp -a /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%s)"
  fi
  sed -e "s|@DOMAIN@|$DOMAIN|g" -e "s|@PORT@|$PORT|g" -e "s|@STATIC@|$BASE/current/src/outline_panel/static|g" \
    "$REL/deploy/Caddyfile" > /etc/caddy/Caddyfile
  caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile >/dev/null
  systemctl enable -q caddy
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
fi

# ------------------------------------------------------------------ prune
ls -1dt "$BASE"/releases/*/ 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)) \
  | while read -r d; do [ "$(readlink -f "$d")" = "$(readlink -f "$BASE/current")" ] || rm -rf "$d"; done
rm -f "$TARBALL"

# ------------------------------------------------------------------ report
rss=$(ps -o rss= -p "$(systemctl show -p MainPID --value "$APP")" 2>/dev/null | awk '{printf "%d MB", $1/1024}')
echo
say "Release $REL_ID is live (${rss:-?} resident)"
if [ "$USE_CADDY" = 1 ]; then
  echo "    https://$DOMAIN"
else
  echo "    http://$(hostname -I | awk '{print $1}'):$PORT"
fi
if [ -n "$NEW_PASSWORD" ]; then
  echo "    Login: admin / $NEW_PASSWORD   (shown once; change it in the panel)"
fi
echo "    Logs:  journalctl -u $APP -f        Backups: $DATA/backups"
