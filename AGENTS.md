# Mediaforce Agent Guide

Only session-start facts that are easy to miss belong here.

## Name and shape

- Product/repo name: `mediaforce`
- Internal Python package: `mediaforce`
- Preferred CLI entrypoints: `mediaforce`, `mediaforce-web`
- Legacy compatibility CLI entrypoints still exist: `media-harness`,
  `media-harness-web`
- Backend/API lives in `mediaforce/web/app.py`
- Frontend lives in `frontend/`
- FastAPI serves `frontend/build/` when that bundle exists

## Runtime gotchas

- Durable state lives outside the repo under
  `~/Library/Application Support/media-harness/`
- Review media cache lives under `~/Library/Caches/media-harness/review/`
- Source libraries are expected at `/Volumes/media/movies` and
  `/Volumes/media/tv`
- Staging root is expected at `/Volumes/media/transcode`
- Do not reintroduce checked-in runtime state, SQLite databases, or review
  media artifacts into the repo

## Validation defaults

- Backend targeted tests:
  `uv run --with pytest pytest tests/test_encode_queue_recovery.py tests/test_tuning_runtime.py`
- Frontend checks: `cd frontend && npm run check`
- CLI smoke: `uv run mediaforce --help`

## UI dev reminder

- Backend dev launcher: `scripts/mediaforce-web-dev.sh start`
- Frontend dev server: `cd frontend && npm run dev`
- Vite proxies `/api/*` and `/review-media/*` to `127.0.0.1:8777`
- When changing UI, validate in a real browser

## Further reading

- `README.md`: durable operator and developer overview
- `TODO.md`: current priorities
- `HANDOFF.md`: current-session handoff notes
