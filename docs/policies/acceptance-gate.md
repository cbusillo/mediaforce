# Acceptance Gate

We do not stop at "it works." We stop when we fully like the result.

## Required bar

- We should 100% love the code we are about to commit
- Remove code smells, bad practices, stale naming, and obvious design issues
- Follow the [local/CI test selection](../style/testing.md) before commits or ending a session
- Run browser validation for UI changes; for primary user surfaces, read
  `docs/style/workstation-ui.md` together with `docs/style/frontend.md`
- Use `docs/development/browser-qa-matrix.md` for route, fixture, and narrow
  browser coverage expectations
- Use the `qualityGate` section in `.github/github.json` as the
  canonical source for repo validation commands

## Minimum checklist

Local checks apply before committing. CI checks apply before ready or merge.

- Focused local tests and complete affected safety modules pass
- Frontend checks, lint, and build pass; full backend/frontend tests pass in CI
- CLI smoke passes
- The complete web route smoke passes in CI; local UI review covers changed routes at desktop width and 390px
- `bash scripts/pre-commit-check.sh` runs static checks and CLI smoke; select local tests separately
- Package-sensitive changes pass `uv build` and
  `scripts/verify_package_contents.py`
- Docs are updated when behavior, workflow, operations, or user-facing
  expectations change
- The final code is something we would be happy to own long term

## Inspection authority

- The required Python lane and its `prepare.python` settings are declared in
  `.github/github.json`, with `config/jetbrains/Mediaforce.xml` as the tracked
  profile. The shared [jetbrains-inspection skill](https://github.com/cbusillo/codex-skills/blob/main/skills/jetbrains-inspection/SKILL.md)
  owns native preparation, execution, coverage, and cleanup.
- The nested WebStorm lane remains non-blocking and UNKNOWN because the native
  helper does not support its `projectPath`. Svelte coverage stays UNKNOWN
  without a Svelte plugin, per the Director's decision on
  [codex-skills#2803](https://github.com/cbusillo/codex-skills/issues/2803).
  Frontend-only changes must not be routed through PyCharm.
- Frontend dependencies and Svelte generated state come from the frontend's
  npm gates. `npm --prefix frontend run check` is the semantic authority for
  Svelte files; `npm --prefix frontend run lint` covers ESLint and formatting.
- Changes to the shared inspection profile's approved baseline require another
  clean/defect qualification.
- Browser validation remains required for user-visible frontend changes even
  when all static checks pass.
