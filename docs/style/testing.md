# Testing Style

Tests should prove the behavior that changed, not just exercise code paths.

## Purpose

- Keep test work focused on behavioral proof, not nominal coverage.

## Default checks

- Backend: `uv run --with pytest pytest`
- Frontend types: `cd frontend && npm run check`
- Frontend lint: `cd frontend && npm run lint`
- Frontend unit tests: `cd frontend && npm test`
- Frontend build: `cd frontend && npm run build`
- CLI smoke: `uv run mediaforce --help`
- Web route smoke: `npm --prefix frontend run smoke:web`
- Whole local gate: `bash scripts/pre-commit-check.sh`

`.github/github.json` `qualityGate` is the canonical command list.

## Rules

Follow the "Tests" section of `AGENTS.md`: what a test must prove, and what it
must not assert or depend on.

## Expectations

- Run the full available test suite before commits or ending a session
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
