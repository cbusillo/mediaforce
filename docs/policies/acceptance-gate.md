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

- JetBrains inspection uses language-owned lanes from `.github/github.json`.
  PyCharm is the required Python lane. WebStorm runs as a non-blocking frontend
  lane while its Svelte signal is qualified against the native checks.
- Native PyCharm prepares Python from `qualityGate.inspection.prepare.python`
  inside its private snapshot. It creates the declared Python environment and
  test roots and runs locked uv sync, including the development group for pytest.
  A fresh source worktree needs no `.venv` or generated `.idea` files. The helper
  registers a temporary SDK in the isolated worker and retires it after the run.
  The tracked `config/jetbrains/Mediaforce.xml` is requested directly; no profile
  copies or local preparation script are needed.
- The former shell preparation script and its state helper are retired: their
  only production caller was the inspection metadata. Native preparation owns
  the Python model, so desktop module normalization and duplicate-module checks
  are unnecessary. `requiredGeneratedState` is ignored by the native helper and
  is removed. Frontend dependencies and Svelte state come from the frontend's
  own npm gates. The nested WebStorm lane remains non-blocking and UNKNOWN;
  Svelte coverage stays UNKNOWN without a Svelte plugin, per the Director's
  decision on [codex-skills#2803](https://github.com/cbusillo/codex-skills/issues/2803).
- `npm --prefix frontend run check` is the semantic authority for Svelte files,
  with `npm --prefix frontend run lint` covering ESLint and formatting policy.
- A clean WebStorm lane does not replace the frontend checks. WebStorm findings
  are readiness evidence, but they do not block merges until a bounded clean and
  defect matrix proves reliable, non-noisy Svelte coverage beyond the native
  checks. Frontend-only changes must not be routed through PyCharm.
- The shared profile disables only the Svelte-incompatible or duplicate
  style/proofreading findings proven noisy by the bounded probe. It explicitly
  keeps ESLint, missing-alt, and unused-symbol inspections enabled; changes to
  that baseline require another clean/defect qualification.
- Frontend-only selections stay UNKNOWN on the native helper while the nested
  WebStorm model is unsupported; they do not claim PyCharm coverage.
- Browser validation remains required for user-visible frontend changes even
  when all static checks pass.
