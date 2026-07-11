# Mediaforce Docs

Use this directory for guidance that should not live in `AGENTS.md`.

## Start here

- `docs/policies/acceptance-gate.md`: commit/session-finish bar
- `docs/policies/coding-standards.md`: repo-wide coding expectations
- `docs/style/index.md`: entry point for language and testing style guides
- `docs/style/workstation-ui.md`: UI doctrine for operator-facing workstation
  surfaces
- `.github/github.json`: canonical repo commands, quality gates,
  workflow metadata, and cleanup policy

## Architecture

- `docs/architecture/module-boundaries.md`: durable backend/frontend module
  boundary map after the structural refactor pass
- `docs/architecture/representative-evidence.md`: deterministic representative
  selection, coverage, and the versioned evidence contract

## Design briefs

- `docs/design/README.md`: source-of-truth routing for current versus
  historical UI reset guidance
- `docs/design/workstation-reset-plan.md`: active frontend replacement plan
- `docs/design/basic-user-vocabulary.md`: user-facing vocabulary and workflow
  state reference for the UI/UX reset
- Historical/superseded briefs live in `docs/design/archive/`; read
  `docs/design/README.md` before using them as evidence.

## Developer workflows

- `docs/development/database-tooling.md`: SQLAlchemy/Alembic schema workflow,
  legacy-bridge notes, and migration validation commands
- `docs/development/browser-review-guidance.md`: browser-review subagent launch
  contract for UI exploration and critique
- `docs/development/browser-qa-matrix.md`: repeatable browser route, fixture,
  and narrow-layout validation matrix
