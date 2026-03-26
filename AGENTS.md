# Mediaforce Agent Guide

This file is authoritative for project-specific facts that are easy to miss at
session start.

## Canonical identity

- GitHub repo: `cbusillo/mediaforce`
- Branch: `main`

## Naming that is intentionally mixed

- Product/repo name: `mediaforce`
- Internal Python package: `media_harness`
- Preferred CLI entrypoints: `mediaforce`, `mediaforce-web`
- Compatibility CLI entrypoints still exist: `media-harness`,
  `media-harness-web`

Do not start a rename sweep from `media_harness` to `mediaforce` unless the
task explicitly calls for it. The mixed naming is deliberate for now.

## Architecture

- Backend/API: FastAPI in `media_harness/web/app.py`
- Core backend logic: `media_harness/`
- Frontend: SvelteKit SPA in `frontend/`
- Built frontend bundle is served by FastAPI from `frontend/build/` when
  present, or from the packaged wheel include path when installed.

## Runtime and state

- Durable state lives outside the repo under
  `~/Library/Application Support/media-harness/`
- Review media cache lives under `~/Library/Caches/media-harness/review/`
- Source libraries are expected at `/Volumes/media/movies` and
  `/Volumes/media/tv`
- Staging root is expected at `/Volumes/media/transcode`

Do not reintroduce checked-in runtime state, SQLite databases, or review media
artifacts into the repo.

## Current working assumptions

- The current product is semi-automated, not fully unattended.
- Promotion remains an explicit operator action after review.
- The active frontend is the SvelteKit UI, not the older refreshing page flow.
- The most likely near-term work is UI/UX regression cleanup after the move to
  SvelteKit.

## Validation commands

- Backend targeted tests:
  `uv run --with pytest pytest tests/test_encode_queue_recovery.py tests/test_tuning_runtime.py`
- Frontend type/Svelte checks:
  `cd frontend && npm run check`
- CLI smoke:
  `uv run mediaforce --help`

Prefer these targeted checks unless the task clearly needs something broader.

## Frontend workflow

- Start backend from repo root with `scripts/mediaforce-web-dev.sh start`
- Start frontend dev server from `frontend/` with `npm run dev`
- Vite proxies `/api/*` and `/review-media/*` to the backend on `127.0.0.1:8777`

When changing UI, validate in a real browser and prefer browser-visible proof
over code-only reasoning.

## Collaboration notes

- `README.md` is the durable operator/developer overview.
- `TODO.md` is the current project priority list.
- `HANDOFF.md` is the current-session summary for the next agent.

