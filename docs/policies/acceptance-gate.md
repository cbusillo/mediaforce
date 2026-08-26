# Acceptance Gate

We do not stop at "it works." We stop when we fully like the result.

## Required bar

- We should 100% love the code we are about to commit
- Remove code smells, bad practices, stale naming, and obvious design issues
- Run the full available test suite before commits or ending a session
- Run browser validation for UI changes; for primary operator surfaces, read
  `docs/style/workstation-ui.md` together with `docs/style/frontend.md`
- Use `docs/development/browser-qa-matrix.md` for route, fixture, and narrow
  browser coverage expectations
- Use the `qualityGate` section in `.github/github.json` as the
  canonical source for repo validation commands

## Minimum checklist

- Backend tests pass
- Frontend checks, lint, unit tests, and build pass
- CLI smoke passes
- Package-sensitive changes pass `uv build` and
  `scripts/verify_package_contents.py`
- Docs are updated when behavior, workflow, operations, or operator-facing
  expectations change
- The final code is something we would be happy to own long term

## Inspection authority

- JetBrains inspection uses language-owned lanes from `.github/github.json`.
  PyCharm is the required Python lane. WebStorm runs as a non-blocking frontend
  lane while its Svelte signal is qualified against the native checks.
- Linked-worktree Python IDE state, frontend dependencies, Svelte generated
  state, and the named `Mediaforce` inspection profile are produced by
  `scripts/prepare-jetbrains-inspection.sh`. Generated state stays ignored and
  must not be committed. Frontend dependencies are reinstalled only when the
  committed npm manifests change, preventing preparation from invalidating an
  IDE snapshot on every inspection run. Preparation creates one pyproject-owned
  exact-root Python module with the worktree SDK and removes stale suffixed
  modules; PyCharm's bounded SDK-registration retry handles first open. The
  named profile is copied into both project roots, and WebStorm opens
  `frontend/` through its lane `projectPath`.
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
- A changed-file inspection with no required Python files returns the helper's
  explicit `no_required_lane_files` result rather than claiming PyCharm covered
  frontend source.
- Browser validation remains required for user-visible frontend changes even
  when all static checks pass.
