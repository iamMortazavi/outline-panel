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

### Production deploy (recommended)

From a checkout, one command ships the committed `HEAD` to a server over SSH
and makes it live behind Caddy with automatic HTTPS:

```bash
deploy/deploy.sh root@panel.example.com            # domain = the SSH host
deploy/deploy.sh root@1.2.3.4 --domain panel.example.com
deploy/deploy.sh root@1.2.3.4 --no-domain          # plain http://IP:8000
```

Re-run it to ship an update. What it sets up (`deploy/remote.sh`), tuned for a
small VPS that also runs Outline:

- **Releases, not a working copy:** each deploy is unpacked to
  `/opt/outline-panel/releases/<id>` and installed into one venv. It goes live
  only when `/healthz` answers; otherwise the previous release is restored.
- **Data apart from code:** config in `/etc/outline-panel/env` (0640), the
  database in `/var/lib/outline-panel`, backed up before every restart (last 7
  kept). An `install.sh` layout is migrated on first run, originals untouched.
- **One lean process:** a single uvloop worker runs the dashboard, scheduler and
  bot; no access log; SQLite in WAL with `synchronous=NORMAL`; `MALLOC_ARENA_MAX=2`.
- **Caddy does the static work:** assets are precompressed at deploy time and
  served from disk with immutable caching, so Python only handles pages and the
  API. API responses are never compressed (BREACH).
- **systemd sandbox and limits:** a dedicated user, read-only system,
  `MemoryHigh=300M`/`MemoryMax=450M` and lower CPU/IO weight so Outline keeps
  priority. A 1 GB swapfile is added on machines under 2 GB with none.
- Caddy is skipped if something else already owns ports 80/443; the panel then
  listens on `:8000` for your existing proxy.

Manage it: `systemctl {status|restart|stop} outline-panel`, logs with
`journalctl -u outline-panel -f`.
Locked out? `outline-panel-admin reset-password`

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

### Telegram Mini App (Web App)

Set **Mini App URL** in Settings → Telegram bot to your panel's public HTTPS base
(e.g. `https://panel.example.com`, or via the `WEBAPP_URL` env). The bot then shows
a **🚀 Open Web App** button (and a persistent **Open** menu button) that opens a
phone-friendly Web App at `<base>/tma` — admins can create, edit and delete users,
view per-user QR codes, and watch usage and server stats without leaving Telegram.
Auth uses Telegram's signed `initData`
(validated against the bot token), so only the same admin IDs get in — no extra
login. HTTPS is required; pair it with the Caddy setup below.

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
| `WEBAPP_URL` | Public HTTPS base for the Telegram Mini App (served at `<base>/tma`) |
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
  web/                   FastAPI dashboard + Mini App API
    app.py deps.py registry.py run.py
    routers/  auth.py servers.py keys.py stats.py settings.py subscription.py
              backup.py miniapp.py
  bot/                   Telegram bot (aiogram)
    dispatcher.py  build_dispatcher (handlers)
    manager.py     in-process start/stop
    run.py         standalone runner
  static/
    index.html           dashboard shell (assets versioned by content hash)
    app.js  organic.css  the dashboard (Organic design system)
    fonts.css            self-hosted Caprasimo / Figtree / Vazirmatn faces
    i18n.js              English + Persian strings, RTL
    sub.html             customer subscription page
    miniapp.html         Telegram Mini App
    vendor/fonts/        woff2 subsets + OFL licences
    vendor/qrcode*.js    self-hosted QR generators
tests/                   pytest suite
install.sh  Dockerfile  docker-compose.yml  deploy/  .github/workflows/ci.yml
```

The `core/` package holds all domain logic (storage, Outline API client,
scheduler, security) with no web/bot dependencies, so both the dashboard and the
bot build on the same foundation. Code, comments, and all user-facing text are
in English.

## Security notes

- Provide `certSha256` (in the access config) to **pin** the server's self-signed cert; otherwise the client falls back to `verify=False`.
- The panel cookie is `HttpOnly`+`Secure`; logins are rate-limited; enable 2FA for an extra factor.
- Never commit `.env`. Always serve the dashboard over HTTPS.

## License

MIT — see [LICENSE](LICENSE).
