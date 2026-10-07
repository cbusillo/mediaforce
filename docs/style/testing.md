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

The development lifecycle fixture in `tests/test_dev_service.py` observes root
lifetime separately from worker lifetime. Its root handler writes a SIGTERM
receipt while the worker may still be running; preservation assertions reject
that receipt. Shutdown cases that intentionally leave a signalled root alive
explicitly require it. The final completion record derives its signal evidence
from the same receipt.
