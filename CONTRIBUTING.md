# Contributing

Thanks for your interest in improving Outline Panel!

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export ADMIN_PASSWORD=dev COOKIE_SECURE=false
outline-panel        # http://127.0.0.1:8000
```

## Before opening a PR

- Run the test suite: `pytest`
- Add tests for new behavior (see `tests/`).
- Keep changes focused; match the existing style (stdlib-first, no heavy deps).

## Layout

- `src/outline_panel/web/` — FastAPI app + routers
- `src/outline_panel/bot/` — Telegram bot (core handlers, in-process manager, runner)
- `src/outline_panel/{db,scheduler,settings,security,outline_api}.py` — shared core

## Reporting issues

Include your deployment method (install.sh / Docker / source), Python version,
and relevant logs (`journalctl -u outline-panel` for the systemd service).
