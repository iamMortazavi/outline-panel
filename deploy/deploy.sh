#!/usr/bin/env bash
#
# Ship the committed HEAD to a server and make it live.
#
#   deploy/deploy.sh root@panel.example.com                 # HTTPS via Caddy
#   deploy/deploy.sh root@1.2.3.4 --no-domain               # plain :8000
#   deploy/deploy.sh root@host -p 2222 --domain other.example.com
#
# The domain defaults to the host part of the target. One SSH connection is
# reused for every step, so a password is asked for once. Only committed code
# is shipped (git archive), never a stray local file or secret.
set -euo pipefail

TARGET="${1:?usage: deploy.sh user@host [-p port] [--domain d | --no-domain]}"; shift
SSH_PORT=22
DOMAIN="${TARGET#*@}"
while [ $# -gt 0 ]; do
  case "$1" in
    -p|--port)   SSH_PORT="$2"; shift 2 ;;
    --domain)    DOMAIN="$2"; shift 2 ;;
    --no-domain) DOMAIN=""; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
# an IP address cannot get a certificate
[[ "$DOMAIN" =~ ^[0-9.]+$ ]] && DOMAIN=""

cd "$(git rev-parse --show-toplevel)"
if ! git diff --quiet HEAD -- src deploy pyproject.toml; then
  echo "note: uncommitted changes in src/, deploy/ or pyproject.toml are NOT shipped" >&2
fi
REL_ID="$(date -u +%Y%m%d%H%M%S)-$(git rev-parse --short=10 HEAD)"
PKG="$(mktemp -t outline-panel.XXXXXX.tar.gz)"
CTL="$(mktemp -u -t op-ssh.XXXXXX)"
cleanup() { ssh -S "$CTL" -O exit "$TARGET" 2>/dev/null || true; rm -f "$PKG"; }
trap cleanup EXIT

git archive --format=tar.gz -o "$PKG" HEAD pyproject.toml README.md src deploy
echo "==> Release $REL_ID ($(du -h "$PKG" | cut -f1))"

SSH=(ssh -p "$SSH_PORT" -o ControlMaster=auto -o ControlPath="$CTL" -o ControlPersist=120
     -o ServerAliveInterval=15 -o StrictHostKeyChecking=accept-new)
"${SSH[@]}" -fN "$TARGET"
"${SSH[@]}" "$TARGET" "mkdir -p /root/outline-panel-deploy"
scp -q -P "$SSH_PORT" -o ControlPath="$CTL" "$PKG" deploy/remote.sh "$TARGET:/root/outline-panel-deploy/"
"${SSH[@]}" -t "$TARGET" "bash /root/outline-panel-deploy/remote.sh \
  /root/outline-panel-deploy/$(basename "$PKG") '$REL_ID' '$DOMAIN'"
