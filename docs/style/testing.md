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

## Expectations

- Run the full available test suite before commits or ending a session
- Add or update targeted tests when behavior changes
- Use browser validation for UI work in addition to automated checks
- Prefer the narrowest concrete acceptance check that proves the change
- Keep test doubles and fixtures typed enough to stay inspection- and Ruff-clean
- Prefer real reusable helpers over repeated ad hoc setup in each test
