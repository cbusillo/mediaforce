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
- `uv build` now auto-builds `frontend/` during wheel packaging; do not rely on
  stale checked-in or local `frontend/build/` artifacts
- Use `.github/github.json` for repo commands and quality gates

## Workflow Notes

- UI changes: validate in a real browser per
  `docs/policies/acceptance-gate.md`
- Before changing primary operator surfaces, read
  `docs/style/workstation-ui.md` together with `docs/style/frontend.md`
- For browser exploration by subagents, explicitly use the `browser-ui-review`
  skill and follow `docs/development/browser-review-guidance.md`.
- Follow `docs/style/index.md` plus
  `docs/policies/coding-standards.md`
- Before commits or ending a session, satisfy
  `docs/policies/acceptance-gate.md`
- Prefer making commits in smaller logical chunks as work is completed.
- Before finalizing a change when practical, run PyCharm inspections in addition
  to the required checks.
- The checked-in Git hook lives at `.githooks/pre-commit`; fresh clones should
  enable it with `git config core.hooksPath .githooks`

## See also

- `README.md`: durable operator and developer overview
- `docs/README.md`: docs table of contents
- `docs/style/workstation-ui.md`: primary operator-surface design doctrine
- `docs/architecture/module-boundaries.md`: durable backend/frontend module
  boundaries after the structural refactor pass
