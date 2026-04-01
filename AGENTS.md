# Mediaforce Agent Guide

Only session-start facts that are easy to miss belong here.

## Naming

- Product/repo name: `mediaforce`
- Internal Python package: `mediaforce`
- Preferred CLI entrypoints: `mediaforce`, `mediaforce-web`

## Repo facts

- Runtime state and review media live outside the repo
- Machine-local paths come from config/runtime settings, not code-level
  invariants
- Do not reintroduce checked-in runtime state, SQLite databases, or review
  media artifacts into the repo

## Defaults

- Backend targeted tests:
  `uv run --with pytest pytest tests/test_encode_queue_recovery.py tests/test_tuning_runtime.py`
- Frontend checks: `cd frontend && npm run check`
- CLI smoke: `uv run mediaforce --help`
- UI changes: validate in a real browser
- For browser exploration by subagents, explicitly use the `browser-ui-review`
  skill and follow the browser review launch contract below.
- Follow `docs/style/index.md` plus
  `docs/policies/coding-standards.md`
- Before commits or ending a session, satisfy
  `docs/policies/acceptance-gate.md`

## Browser Review Launch Contract

- For the first browser-review subagent in a session, run a tiny smoke pass
  before the full critique: open the page, wait for a known selector, capture a
  screenshot, and confirm the page title/URL.
- Launch browser-review subagents with `write: true` and explicitly mention the
  `browser-ui-review` skill in the task prompt.
- The prompt should require this exact order:
  1. open the live page in a browser
  2. wait for an app-specific ready selector
  3. interact enough to inspect the real UI state
  4. capture at least one screenshot artifact under `scratch/ui-checks/`
  5. report findings from the browser-visible result
- The prompt should also require the result to begin with a one-line browser
  status: `Browser review succeeded` or `BROWSER BLOCKED`.
- If `BROWSER BLOCKED` occurs, fix the browser-launch problem and rerun the
  review. Do not present the blocked subagent's code-informed notes as the
  requested review.

## See also

- `README.md`: durable operator and developer overview
- `docs/README.md`: docs table of contents
- `docs/TODO.md`: current priorities
- `docs/architecture/module-boundaries.md`: durable backend/frontend module
  boundaries after the structural refactor pass
