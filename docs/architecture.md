# Mediaforce Architecture (WIP)

This repo now uses a package layout under `src/mediaforce` to keep the CLI,
web UI, and domain logic organized while we continue the refactor.

- `core.py` – legacy monolith; holds most logic (settings, queue, encoder, scan).
- `db/` – SQLModel models and engine helpers (`models.py`).
- `cli/` – console entrypoint shim; calls into `core.main`.
- `web/` – FastAPI app (`app.py`), templates, static assets.
- `services/`, `domain/`, `config/` – placeholders for extracting logic from
  `core.py` during the ongoing refactor (queue, scanner, encoder, watchers,
  classification).

Entry points

- CLI: `uv run mediaforce` (shims exist for editable checkout) → `mediaforce.core.main`
- Web: `uv run mediaforce-web` → `mediaforce.web.app:app`

Data locations

- Config/DB: `~/.config/mediaforce/mediaforce.db` (settings + inventory)
- Templates: `src/mediaforce/web/templates/`
- Static assets: `src/mediaforce/web/static/`

Next refactor steps (suggested)

1) Split `core.py` into `services/` (scanner, encoder, queue, watch) and
   `domain/` (classification, tier rules, normalization).
2) Replace direct SQL calls in the web app with service functions + typed
   models.
3) Introduce structured logging helpers in `config/logging.py` and swap out the
   remaining `print` uses.
