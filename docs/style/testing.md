# Testing Style

Tests should prove the behavior that changed, not just exercise code paths.

## Purpose

- Keep test work focused on behavioral proof, not nominal coverage.

## Local tests and CI

`.github/github.json` `qualityGate` is the canonical command list. Follow the
[local/CI test contract](https://github.com/cbusillo/codex-skills/blob/main/skills/github/references/config-schema.md#workflow-metadata).
Replace `{test_paths}` with a nonempty selection in `test.local` for backend paths.

File-integrity and database-custody changes include the complete modules in
`test.fileIntegrityMacos`; trace callers for other affected safety modules.
Keep inspection, build and applicable smoke checks alongside local tests. The
pre-commit hook runs CLI smoke; agents select local tests separately. CI runs
`test.ci` for the complete backend suite, the Mac app tests and the macOS
file-integrity lane before ready or merge.

## Rules

Follow the "Tests" section of `AGENTS.md`: what a test must prove, and what it
must not assert or depend on.

## Expectations

- Add or update targeted tests when behavior changes
- Prefer the narrowest concrete acceptance check that proves the change
- Keep test doubles and fixtures typed enough to stay inspection- and Ruff-clean
- Prefer real reusable helpers over repeated ad hoc setup in each test

Development launcher behavior tests use temporary checkouts and pipe-owned
server fixtures. The fixtures expose native PGIDs through a pinned process
inventory command and close their lifetime pipe for independent teardown.
They cover reuse, restart, server crash with orphan workers, launcher loss,
stubborn descendants, foreign listeners and preservation of legacy records.
Actual backend/reload process-group qualification remains separate
from the isolated fixture tests.
