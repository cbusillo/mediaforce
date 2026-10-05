# Mediaforce Agent Guide

Only session-start facts that are easy to miss belong here.

Read the Director's [overall direction](https://github.com/cbusillo/direction/blob/main/DIRECTION.md)
first, then this repository's [DIRECTION.md](DIRECTION.md). Those files own
purpose, work order, and stop boundaries; they take precedence over issues,
plans, and other docs.

`AGENTS.md` is the only agent-instruction file. Keep any nested instructions
in an `AGENTS.md` too.

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
- Local macOS launch item: use `uv run mediaforce service status` to inspect
  `~/Library/LaunchAgents/com.mediaforce.web.plist`. It may be disabled at
  session start when Mediaforce is not active work, so do not assume the local
  web server is running; enable it intentionally when needed.
- Before changing primary user surfaces, read
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

## Tests

- A test must fail when the product is broken and pass when someone makes an
  intended change.
- Do not assert a literal that is defined elsewhere (versions, toolchains, URLs,
  hashes, config values); check agreement with the one source of truth instead.
- Do not assert workflow or config text (`.github/`, `github.json`, IDE
  profiles); enforce those rules where they execute (the workflow itself,
  `actionlint`, or a helper script with its own behavior test).
- Tests and verification code must not depend on working-tree or host state:
  no `git ls-files`, generated IDE files, the developer's `~/.ssh`, the host
  platform, installed tools, or real SSH hosts. Pin or stub them.
- Keep byte-exact and hash checks on real artifacts and immutable evidence.

## See also

- `README.md`: durable user and developer overview
- `docs/README.md`: docs table of contents
- `docs/style/workstation-ui.md`: primary user-surface design doctrine
- `docs/architecture/module-boundaries.md`: durable backend/frontend module
  boundaries after the structural refactor pass
