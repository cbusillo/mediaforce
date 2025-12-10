# AGENTS.md — Operator Notes

Keep this file short and evergreen. Follow the links for specifics; avoid hardcoding
values that change (versions, hostnames, mounts).

## Start Here

- Read `README.md` for the product pitch, pipeline, and usage examples.
- See `docs/architecture.md` for the current code layout and refactor plan.
- Roadmap lives in `todo.md` (do not duplicate tasks here).
- Local host inventory: `docs/hosts.local.md` (gitignored).

## Codebase Map (high level)

- `src/mediaforce/core.py` — main CLI logic (scanner/queue/encode/watch).
- `src/mediaforce/web/app.py` — FastAPI web UI; templates in `src/mediaforce/web/templates/`.
- `src/mediaforce/db/models.py` — SQLModel models + settings/storage helpers.
- `src/mediaforce/config/` — shared runtime helpers: `settings.py` (AppSettings, engine bootstrap) and `logging.py` (structured JSON stdout + optional JSONL file sink via `MEDIAFORCE_LOG_FILE`).
- `src/mediaforce/cli/main.py` — console shim (`mediaforce` entrypoint).
- Placeholder packages `services/`, `domain/` exist for ongoing extraction from `core.py`.

## Operational Guardrails

- Settings and inventory live in `~/.config/mediaforce/mediaforce.db`. Do not invent new config files; use the settings
  API/UI instead.
- Paths must stay portable between macOS `/Volumes/...` and Linux `/mnt/...`; use `iter_libraries_for_current_host`
  and `normalize_path` helpers rather than hardcoding.
- Web autoupdate only serves files in the allowlist inside `src/`; expand that list intentionally if workers need more.
- When touching queue/encode logic, keep docs in sync (`README.md`, `docs/architecture.md`).

## Style Baseline (borrowed from odoo-ai)

- See `../odoo-ai/docs/policies/coding-standards.md` for naming/DRY rules and docs-as-code mindset.
- Python style: `../odoo-ai/docs/style/python.md` — type hints everywhere, f-strings only, early returns, small
  single-purpose functions, avoid blanket `except Exception`, prefer descriptive names over comments.
- Testing style: `../odoo-ai/docs/style/testing.md` and `testing-advanced.md` for patterns; adapt when we add a test
  suite here.
- Keep this section thin; if we adopt repo-local style pages, link them here and deprecate cross-repo references.

## How to Work Day-to-Day

- Run through `uv run`: `uv run mediaforce ...` for CLI, `uv run mediaforce-web ...` for the UI. Shims keep `src/` on
  `sys.path` for editable checkouts.
- Prefer `apply_patch` for edits; preserve history with `git mv` when relocating files.
- Run focused tests for the area you touch (add `tests/` as it appears). If no tests exist, add minimal coverage near
  the change.
- Tailwind CSS: keep `src/mediaforce/web/static/css/tailwind.css` readable in git. Use `npm run tailwind:dev` while
  iterating; only run `tailwind:build` for release output and avoid committing the minified one-line CSS. If you do
  build, rerun `tailwind:dev` before committing.
- Logging: avoid `print`; use structured logging helpers from `config/logging.py` (`log_info`, `log_warn`, `log_error`). Configure via env: `MEDIAFORCE_LOG_LEVEL` (default INFO), optional `MEDIAFORCE_LOG_FILE=/path/to/mediaforce.jsonl`.
- Python version baseline is modern; avoid adding `from __future__` imports in new code.

## Future-Proofing

- Link, don’t duplicate: when adding features, update the most specific doc (e.g., `docs/architecture.md` for layout
  changes, README for user-facing workflow) and reference it here if needed.
- Keep AGENTS.md scoped to process and pointers so it stays valid as the project evolves.

## Style Reminders (local)

- Prefer descriptive identifiers over comments/docstrings; only add them when they clarify non-obvious intent.
- Use full words in names; avoid cryptic abbreviations.
