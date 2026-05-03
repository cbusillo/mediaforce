# Mediaforce Docs

Use this directory for guidance that should not live in `AGENTS.md`.

## Start here

- `docs/policies/acceptance-gate.md`: commit/session-finish bar
- `docs/policies/coding-standards.md`: repo-wide coding expectations
- `docs/style/index.md`: entry point for language and testing style guides
- `docs/style/workstation-ui.md`: UI doctrine for operator-facing workstation
  surfaces
- `.github/github-repo-workflow.json`: canonical repo commands, quality gates,
  workflow metadata, and cleanup policy

## Architecture

- `docs/architecture/module-boundaries.md`: durable backend/frontend module
  boundary map after the structural refactor pass

## Design briefs

- `docs/design/workstation-home-screen-brief.md`: first-pass brief for the
  workstation-style home screen reset
- `docs/design/workstation-home-screen-inventory.md`: carry-forward, rewrite,
  and retire inventory for the home-screen reset
- `docs/design/operator-workstation-shell-brief.md`: durable cross-route shell
  brief for home, ops, completed, settings, and folder studio

## Developer workflows

- `docs/development/database-tooling.md`: SQLAlchemy/Alembic schema workflow,
  legacy-bridge notes, and migration validation commands
- `docs/development/browser-review-guidance.md`: browser-review subagent launch
  contract for UI exploration and critique
