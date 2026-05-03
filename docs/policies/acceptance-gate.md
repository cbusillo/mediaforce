# Acceptance Gate

We do not stop at "it works." We stop when we fully like the result.

## Required bar

- We should 100% love the code we are about to commit
- Remove code smells, bad practices, stale naming, and obvious design issues
- Run the full available test suite before commits or ending a session
- Run browser validation for UI changes; for primary operator surfaces, read
  `docs/style/workstation-ui.md` together with `docs/style/frontend.md`
- Use the `qualityGate` section in `.github/github-repo-workflow.json` as the
  canonical source for repo validation commands

## Minimum checklist

- Backend tests pass
- Frontend checks, lint, unit tests, and build pass
- CLI smoke passes
- Docs are updated when behavior, workflow, operations, or operator-facing
  expectations change
- The final code is something we would be happy to own long term
