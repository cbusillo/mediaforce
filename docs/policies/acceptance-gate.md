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

- PyCharm is the repository's JetBrains inspection route for Python and general
  IDE findings.
- `npm --prefix frontend run check` is the semantic authority for Svelte files,
  with `npm --prefix frontend run lint` covering ESLint and formatting policy.
- A clean JetBrains inspection does not replace the frontend checks. WebStorm
  may be used interactively, but its inspection API is not a required merge gate
  unless a future bounded evaluation proves that it reports actionable Svelte
  findings beyond the native checks.
- Browser validation remains required for user-visible frontend changes even
  when all static checks pass.
