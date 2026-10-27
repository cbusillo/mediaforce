# Mediaforce Agent Guide

Only session-start facts that are easy to miss belong here.

Read the Director's [overall direction](https://github.com/cbusillo/direction/blob/main/DIRECTION.md)
first, then this repository's [DIRECTION.md](DIRECTION.md). Those files own
purpose, work order, and stop boundaries; they take precedence over issues,
plans, and other docs.

`AGENTS.md` is the only agent-instruction file. Keep any nested instructions
in an `AGENTS.md` too.

## What Mediaforce does

Mediaforce reclaims media-library space by converting videos to AV1 and audio to Opus, while checking quality before publishing each file.
Its native Mac app direction calls for automatic handling of measured problems, with the Director deciding judgment calls and rollback-copy cleanup.
Evidence: [DIRECTION.md](DIRECTION.md); [README.md](README.md) describes implementation details.

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
- Use `.github/github.json` for repo commands and quality gates

## Workflow Notes

- Local macOS launch item: use `uv run mediaforce service status` to inspect
  `~/Library/LaunchAgents/com.mediaforce.web.plist`. It may be disabled at
  session start when Mediaforce is not active work, so do not assume the local
  server is running; enable it intentionally when needed.
- Before changing primary user surfaces, read
  `docs/style/workstation-ui.md`
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

Follow the shared Tests rules in the global agent instructions
([`instructions/global.md`](https://github.com/cbusillo/codex-skills/blob/main/instructions/global.md#tests)).
Here that also means tests pin or stub the host platform, the developer's
`~/.ssh` and remote encode hosts; they never reach a real one.

## See also

- `README.md`: durable user and developer overview
- `docs/README.md`: docs table of contents
- `docs/style/workstation-ui.md`: primary user-surface design doctrine
- `docs/architecture/module-boundaries.md`: durable backend module
  boundaries after the structural refactor pass
