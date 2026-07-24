# Mediaforce Docs

Use this directory for guidance that should not live in `AGENTS.md`.

## Start here

- `docs/policies/acceptance-gate.md`: commit/session-finish bar
- `docs/policies/coding-standards.md`: repo-wide coding expectations
- `docs/style/index.md`: entry point for language and testing style guides
- `docs/style/workstation-ui.md`: UI doctrine for operator-facing workstation
  surfaces
- `.github/github.json`: canonical repo commands, quality gates,
  workflow metadata, and cleanup policy

## Architecture

- `docs/architecture/module-boundaries.md`: durable backend/frontend module
  boundary map after the structural refactor pass
- `docs/architecture/representative-evidence.md`: deterministic representative
  selection, coverage, and the versioned evidence contract
- `docs/architecture/cadence-evidence.md`: measured cadence classification,
  versioned evidence, and safe allow-listed transforms
- `docs/architecture/media-fingerprint-evidence.md`: measured visual/audio
  complexity, versioned media fingerprint evidence, and review-moment selection
- `docs/architecture/evidence-state.md`: additive per-item cadence/fingerprint
  lifecycle state, identities, retry foundation, and media-free rebuilds
- `docs/architecture/evidence-worker.md`: explicit paused evidence batches,
  single-concurrency claims, retries, cancellation, and source-safe commits
- `docs/architecture/stream-budget-ledger.md`: production stream selection,
  non-video overhead, uncertainty, and deterministic size feasibility
- `docs/architecture/quality-risk-contract.md`: versioned quality-risk facts,
  gates, interpretation, and current review authority
- `docs/architecture/quality-search-memory.md`: read-only accepted-outcome
  cohorts, signature invalidation, robust CRF summaries, and authority limits
- `docs/architecture/advisor-routing.md`: evaluated task routing, Codex Lab
  execution, deterministic bypasses, privacy-safe telemetry, and eval operation
- `docs/architecture/library-lifecycle-policy.md`: current-season protection,
  acquisition guards, Plex/TMDB metadata, age ranking, and manifest provenance
- `docs/architecture/typed-library-settings.md`: ordered typed roots,
  availability states, type-change compatibility, and credential boundaries

## Design briefs

- `docs/design/README.md`: source-of-truth routing for current versus
  historical UI reset guidance
- `docs/design/workstation-reset-plan.md`: active frontend replacement plan
- `docs/design/basic-user-vocabulary.md`: user-facing vocabulary and workflow
  state reference for the UI/UX reset
- Historical/superseded briefs live in `docs/design/archive/`; read
  `docs/design/README.md` before using them as evidence.

## Developer workflows

- `docs/development/database-tooling.md`: SQLAlchemy/Alembic schema workflow,
  legacy-bridge notes, and migration validation commands
- `docs/development/browser-review-guidance.md`: browser-review subagent launch
  contract for UI exploration and critique
- `docs/development/browser-qa-matrix.md`: repeatable browser route, fixture,
  and narrow-layout validation matrix
- `docs/development/catalog-refresh-benchmark.md`: generated catalog-scale
  inventory benchmark and subprocess/resource metrics
