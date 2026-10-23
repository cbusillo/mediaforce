# Testing Style

Tests should prove the behavior that changed, not just exercise code paths.

## Purpose

- Keep test work focused on behavioral proof, not nominal coverage.

## Local tests and CI

`.github/github.json` `qualityGate` is the canonical command list. Follow the
[local/CI test contract](https://github.com/cbusillo/codex-skills/blob/main/skills/github/references/config-schema.md#workflow-metadata).
Replace `{test_paths}` with a nonempty selection in `test.local` for backend paths, or in
`test.frontendLocal` with affected paths relative to `frontend/`.

File-integrity and database-custody changes include the complete modules in
`test.fileIntegrityMacos`; trace callers for other affected safety modules.
Keep lint, types, inspection, build and applicable smoke checks alongside local
tests. The pre-commit hook runs static checks and CLI smoke; agents select local
tests separately. CI runs `test.ci` for the complete backend/frontend suites,
the complete web route smoke and its macOS file-integrity lane before ready or
merge. Local UI checks exercise the changed routes and fixture states in a
headless real browser at desktop width and 390px; see the browser QA matrix.

## Rules

Follow the "Tests" section of `AGENTS.md`: what a test must prove, and what it
must not assert or depend on.

## Expectations

- Add or update targeted tests when behavior changes
- Use browser validation for UI work in addition to automated checks
- Prefer the narrowest concrete acceptance check that proves the change
- Keep test doubles and fixtures typed enough to stay inspection- and Ruff-clean
- Prefer real reusable helpers over repeated ad hoc setup in each test

Inspection preparation uses Python tests with pinned UV, npm and Node stubs for
orchestration, profile byte preservation and dependency-cache invalidation.
The existing Vitest lane exercises the actual JavaScript module normalization
and manifest digest, including its CLI, using temporary inputs.

Development launcher behavior tests use temporary checkouts and pipe-owned
server fixtures. The fixtures expose native PGIDs through a pinned process
inventory command and close their lifetime pipe for independent teardown.
They cover reuse, restart, server crash with orphan workers, launcher loss,
stubborn descendants, foreign listeners and preservation of legacy records.
Actual backend/reload/npm/Vite process-group qualification remains separate
from the isolated fixture tests.
