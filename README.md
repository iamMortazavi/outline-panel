# Outline Panel

A self-hosted **web dashboard + Telegram bot** for managing users on one or many
[Outline](https://getoutline.org) VPN servers through the official
[Management API](https://github.com/OutlineFoundation/outline-server/tree/master/src/shadowbox#access-keys-management-api).

- 🌐 **Web dashboard** — mobile-friendly panel to create and hand out keys.
- 🤖 **Telegram bot** — the same actions from inside Telegram, configured from the panel.
- One SQLite store, one expiry/notification engine; run the web app, the bot, or both.

## Features

- **Multiple servers** — manage any number of Outline servers from one panel; paste an API URL or the full Outline Manager access config (`{"apiUrl":…,"certSha256":…}`).
- **Per-user keys** — name, data limit, validity period; copy-ready `ss://` link + QR.
- **Time limit from first connection** — the countdown starts on first use; expired keys are auto-disabled (not deleted) and can be re-enabled by extending.
- **Monthly quota** + **manual usage reset** — recurring or on-demand fresh allowance.
- **Subscription links** — a stable per-user URL that keeps working even if the underlying key changes.
- **Telegram bot, configured from the panel** — paste the token in Settings → the bot runs inside the panel, no extra process. Admin alerts when a key nears its limit/expiry or is auto-disabled.
- **Advanced stats** (experimental metrics): live/peak bandwidth, 30-day transfer, online-now, per-key last-seen & peak devices, connections by country/ISP.
- **Security** — password login with per-IP rate limiting, **change password from the panel**, optional **two-factor (TOTP)**, `HttpOnly`+`Secure` cookies, and **TLS certificate pinning** via `certSha256`.
- **Backup & restore** — download/restore everything (servers, keys, settings) as JSON.
- Search & sort, live usage bars, active/pending/disabled/online status.

## Quick install (Debian/Ubuntu)

```bash
git clone https://github.com/iamMortazavi/outline-panel.git
cd outline-panel
sudo bash install.sh
```

The script installs into `/opt/outline-panel`, creates a venv, asks for an admin
password, generates a session secret, and starts a `systemd` service. Then open
`http://YOUR_SERVER_IP:8000` and add your servers + bot token from the UI.

> **Serve over HTTPS** in production. The session cookie defaults to
> `COOKIE_SECURE=auto` — it works over plain `http://IP:8000` for first setup and
> automatically becomes `Secure` once you're behind HTTPS.

### Automatic HTTPS (Caddy)

Point your domain's DNS at the server, then:

```bash
# 1) bind the panel to localhost (in .env), then restart
sed -i 's/^HOST=.*/HOST=127.0.0.1/' /opt/outline-panel/.env && systemctl restart outline-panel
# 2) install Caddy (https://caddyserver.com/docs/install) and configure it
printf 'your-domain.com {\n\treverse_proxy 127.0.0.1:8000\n}\n' > /etc/caddy/Caddyfile
systemctl restart caddy
```

Caddy fetches and auto-renews a Let's Encrypt certificate and redirects HTTP→HTTPS.
See [`deploy/Caddyfile.example`](deploy/Caddyfile.example).

Manage it: `systemctl {status|restart|stop} outline-panel`
Locked out? `/opt/outline-panel/.venv/bin/outline-panel-admin reset-password`

## Docker

```bash
cp .env.example .env     # set ADMIN_PASSWORD + SESSION_SECRET
docker compose up -d --build
```

Runs the dashboard and the bot with a shared DB volume.

## Run from source (dev)

```bash
pip install -e ".[dev]"
export ADMIN_PASSWORD=changeme COOKIE_SECURE=false
outline-panel            # web dashboard on :8000
outline-panel-bot        # (optional) standalone bot
pytest                   # tests
```

## Telegram bot

Open **Settings → Telegram bot**, paste your token from
[@BotFather](https://t.me/BotFather) and the numeric admin IDs (send `/id` to the
bot to find one), then Save. The bot starts immediately. Alternatively run it as a
separate process with `outline-panel-bot`.

## Configuration

Almost everything (servers, bot token, admin IDs, password, 2FA) is managed from
the panel and stored in the DB. The `.env` only holds bootstrap/runtime values:

| Variable | Description |
|----------|-------------|
| `ADMIN_PASSWORD` | Initial panel password (seeds the DB on first run; later change it in the panel) |
| `SESSION_SECRET` | Long random string; **required** in production / multi-worker |
| `COOKIE_SECURE` | `auto` (default, Secure only over HTTPS), or force `true`/`false` |
| `DB_PATH` | SQLite file path |
| `HOST` / `PORT` | Bind address for `outline-panel` (default `0.0.0.0:8000`) |
| `ENABLE_SCHEDULER` | Run the background scheduler in the web app (set `false` when the standalone bot already runs it) |
| `OUTLINE_API_URL` / `OUTLINE_CERT_SHA256` | Optional: import one server on first run |
| `BOT_TOKEN` / `ADMIN_IDS` | Optional: seed the bot config on first run |
| `NOTIFY_LIMIT_PERCENT` / `NOTIFY_EXPIRY_DAYS` | Alert thresholds (default 80% / 3 days) |

## How time & quota work

Outline has no expiry and a cumulative (non-resettable) usage counter, so the
panel stores each key's duration/quota in SQLite and a background task:
activates keys on first connection, applies expiry (data limit → 0), refreshes
monthly quotas, and sends alerts. A "reset" raises the limit to *current usage +
allowance*. Keep the panel running as a service.

Advanced stats and per-key online/last-seen/devices need **metrics sharing**
enabled on the server (toggle per server in Settings); otherwise the panel
degrades gracefully.

## Project structure

```
src/outline_panel/
  __init__.py            package version
  cli.py                 `outline-panel-admin` management CLI
  core/                  domain logic, framework-agnostic
    config.py settings.py security.py db.py outline_api.py scheduler.py utils.py
  web/                   FastAPI dashboard
    app.py deps.py registry.py run.py
    routers/  auth.py servers.py keys.py stats.py settings.py subscription.py backup.py
  bot/                   Telegram bot (aiogram)
    core.py  build_dispatcher (handlers)
    manager.py  in-process start/stop
    run.py  standalone runner
  static/index.html      single-file web UI
tests/                   pytest suite
install.sh  Dockerfile  docker-compose.yml  deploy/  .github/workflows/ci.yml
```

The `core/` package holds all domain logic (storage, Outline API client,
scheduler, security) with no web/bot dependencies, so both interfaces build on
the same foundation. Code and comments are English; the Telegram bot's
user-facing text is Persian by design.

## Security notes

- Provide `certSha256` (in the access config) to **pin** the server's self-signed cert; otherwise the client falls back to `verify=False`.
- The panel cookie is `HttpOnly`+`Secure`; logins are rate-limited; enable 2FA for an extra factor.
- Never commit `.env`. Always serve the dashboard over HTTPS.

## License

MIT — see [LICENSE](LICENSE).
