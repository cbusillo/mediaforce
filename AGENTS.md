# Mediaforce Agent Guide

Only session-start facts that are easy to miss belong here.

## Naming

- Product/repo name: `mediaforce`
- Internal Python package: `mediaforce`
- Preferred CLI entrypoints: `mediaforce`, `mediaforce-web`
- Legacy compatibility CLI entrypoints still exist: `media-harness`,
  `media-harness-web`

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
  skill. Run a tiny smoke test only for the first such task in a session or
  when browser access/approvals are uncertain.
- When launching browser subagents via `agent.create`, prefer `write: true`
  even for read-only exploration. In this harness, read-only subagents may hit
  browser-tool permission blockers that writable worktree agents avoid.
- Do not assume `agent.create` subagents share the main session browser tool;
  if browser access is blocked, treat it as a harness/permissions issue rather
  than evidence that the site itself is broken.
- Follow `docs/style/index.md` plus
  `docs/policies/coding-standards.md`
- Before commits or ending a session, satisfy
  `docs/policies/acceptance-gate.md`

## See also

- `README.md`: durable operator and developer overview
- `docs/README.md`: docs table of contents
- `docs/TODO.md`: current priorities
- `docs/HANDOFF.md`: current-session handoff notes
