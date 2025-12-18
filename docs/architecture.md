# Mediaforce Architecture (WIP)

This repo now uses a package layout under `src/mediaforce` to keep the CLI,
web UI, and domain logic organized while we continue the refactor.

- `core.py` – legacy CLI entry + orchestration glue. Encoding loop and some
  higher-level flows still live here, but core scanning/queue/progress/watch
  helpers have been extracted into `services/`.
- `config/` – shared runtime config (`settings.py`) and structured logging
  helpers (`logging.py`).
- `config/paths.py` – portable path helpers (mac `/Volumes/...` ↔ linux
  `/mnt/...`) and library root detection.
- `db/` – SQLModel models and engine helpers (`models.py`).
- `cli/` – console entrypoint shim; calls into `core.main`.
- `web/` – FastAPI app (`app.py`), templates, static assets.
- `services/` – extracted logic:
  - `scanner.py` (scan + priority inputs)
  - `queue.py` (claim/release, missing outputs, recalc priorities)
  - `encoder.py` + `progress.py` (ffmpeg progress parsing, result recording)
  - `watch.py` (watchfiles-based auto-queue)
- `domain/` – shared types and small pure helpers.
- `db/repository/` – SQLModel repositories for web read paths.

Repository layer

- `db/repository/` holds SQLModel query helpers used by the web app. Prefer
  adding a repository method over duplicating query logic in routes.

Entry points

- CLI: `uv run mediaforce` (shims exist for editable checkout) → `mediaforce.core.main`
- Web: `uv run mediaforce-web` → `mediaforce.web.app:app`

Data locations

- Config/DB: `~/.config/mediaforce/mediaforce.db` (settings + inventory)
- Templates: `src/mediaforce/web/templates/`
- Static assets: `src/mediaforce/web/static/`

Completed Refactor

- `core.py` is now a thin CLI wiring layer.
- All heavy orchestration logic has been moved to `services/orchestrator.py` and `services/worker_service.py`.
- Quality metrics and HTML comparison generation are centralized in `services/metrics.py`.
- Database access is strictly typed and passes `mypy`.

Next steps

1) Replace remaining ad-hoc SQL in web routes with repositories/services.
2) Add more unit tests around the new service modules.
