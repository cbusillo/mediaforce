# Module Boundaries

This document records the durable module boundaries after the structural
refactor pass that split oversized backend modules and frontend routes into
stable package and component seams.

## Intent

- Keep thin compatibility surfaces at the top level where tests patch directly.
- Prefer adding new runtime logic under focused backend packages instead of
  re-bloating compatibility wrappers.
- Keep Svelte route files thin and move substantial behavior or markup into
  dedicated component and helper layers.

## Backend boundaries

### Keep thin at top level

- `mediaforce/advisor.py`
- `mediaforce/cli.py`
- `mediaforce/execution.py`
- `mediaforce/remote.py`
- `mediaforce/review.py`
- `mediaforce/state_cleanup.py`

These are the intentional top-level facades and entry surfaces that remain
after the package consolidation pass. Avoid growing them with new helper logic.

### `mediaforce/core/`

- `config.py`
  - config loading, runtime settings, path resolution
- `db.py`
  - SQLite schema and DB open helpers
- `models.py`
  - small shared dataclasses such as `ProbeSummary`
- `type_defs.py`
  - JSON-oriented type aliases
- `utils.py`
  - generic utility helpers such as timestamps and file fingerprints
- `evidence.py`
  - stable versioned evidence envelopes and source/policy/tool staleness checks
- `binaries.py`
  - ffmpeg/ffprobe binary discovery
- `process_control.py`
  - managed subprocess cancellation and command helpers

Guidance:

- Keep generic cross-cutting helpers under `mediaforce/core/` instead of
  reintroducing standalone root modules.

### `mediaforce/web/`

- `app.py`
  - app factory
  - middleware/static mounting
  - startup and compatibility wrappers for test-facing helpers
- `routes/`
  - `dashboard.py`
  - `folders.py`
  - `settings.py`
  - `hosts.py`
  - `queues.py`
  - `frontend.py`
- `runtime/`
  - `folder_cards.py`
  - `host_status.py`
  - `host_runtime.py`
  - `encode_scheduler.py`
  - `queue_actions.py`
  - `job_runtime.py`
  - `calibration_runtime.py`
  - `encode_runtime.py`
  - `folder_state.py`
  - `folder_actions.py`
  - `folder_tuning_helpers.py`
  - `folder_tuning_advice.py`
  - `folder_tuning_runtime.py`
  - `folder_ai_tuning.py`
  - `dashboard_payloads.py`
  - `settings_payloads.py`
  - `tool_capabilities.py`
    - explicit-lifecycle ffmpeg capability snapshots used by read-only payloads
- `serializers.py`
  - shared API payload shaping across routes

Guidance:

- Treat the `web/` split as complete for the structural refactor baseline.
- Add new route-specific behavior in `web/routes/` or `web/runtime/` rather
  than growing `web/app.py`.
- Only trim more from `web/app.py` when a clearly mechanical helper cluster or
  removable compatibility wrapper appears.

### `mediaforce/hosts/`

- `transport.py`
  - SSH command execution, SCP/rsync helpers, shell/path helpers
- `readiness.py`
  - host probe scripts, capability checks, status parsing
- `lifecycle.py`
  - wake/start/stop commands, cooldown behavior
- `mount_runtime.py`
  - controller SMB mount discovery and password-free remote Finder mount recovery
  - bounded transient LaunchAgent execution, cleanup, and Keychain recovery messages
- `setup.py`
  - prepare/reset trust/bootstrap flows
- `models.py`
  - host datatypes and constants

`mediaforce.remote` remains the stable compatibility wrapper surface for direct
test patch targets.

### `mediaforce/encoding/`

- `encode_queue.py`
  - encode queue state, job persistence, and summaries
- `duration_estimate.py`
  - conservative host-specific search-plus-encode admission estimates from
    recent successful runs
- `ffmpeg.py`
  - ffmpeg capability and hwaccel helpers
- `quality.py`
  - quality-search and sample-encode helpers
- `quality_search.py`
  - quality-search orchestration and target-size sample search handoff
- `commands.py`
  - ffmpeg argument and preset construction
- `streams.py`
  - immutable production audio, subtitle, and attachment selection plan
  - copy/transcode/drop decisions consumed by command construction and budgeting
- `cadence.py`
  - measured cadence classification, evidence identity, transform gates, and
    the allow-listed cadence filter compiler
- `fingerprint.py`
  - bounded visual/audio complexity measurement, versioned media fingerprint
    evidence, advisory confidence gates, and safe source-scoped decisions
- `paths.py`
  - mounted/stream source and staging resolution
- `progress.py`
  - ffmpeg progress parsing and callback shaping
- `runner.py`
  - local vs remote execution entry points
- `staging.py`
  - validation, promotion, staging cleanup helpers
- `manifest.py`
  - encode-one and encode-many orchestration
  - final output size verification and bounded measured retry logging for
    ledger-backed production encodes

Guidance:

- Production commands consume the stream plan embedded in the persisted stream
  budget ledger. Do not independently reselect streams or reconstruct codec and
  bitrate decisions in sample, queue, API, or production code.

`mediaforce.execution` remains the stable compatibility wrapper surface.

### `mediaforce/library/`

- `planner.py`
  - item recommendation and manifest-item shaping
- `probe.py`
  - inventory-only ffprobe inspection and audio, subtitle, attachment track summaries
  - explicitly invoked per-kind cadence and fingerprint analysis for the worker
- `scanner.py`
  - library inventory orchestration and catalog updates
  - all inventory rows preserve canonical evidence without launching deep analysis
- `evidence_state.py`
  - rebuildable per-item/per-kind projection of canonical cadence and
    fingerprint JSON
  - compact freshness reasons, source/analyzer/policy identities, retry timing,
    and media-free status/count queries
- `evidence_queue.py`
  - explicit scoped batch state, atomic single-concurrency claims, pause/cancel,
    lease recovery, retry readiness, and aggregate status
- `evidence_worker.py`
  - foreground bounded execution, heartbeat-managed subprocess cancellation,
    policy-only reclassification, retry backoff, and source-safe canonical commits
- `media_scopes.py`
  - canonical operator grouping for TV, movie, and generic media roots
  - exact-item versus descendant matching, SQL-safe boundaries, and API scope payloads
- `library_settings.py`
  - ordered typed-root schema, legacy inference, safe availability defaults,
    type-specific policies, and profile registries
- `other_profiles.py`
  - generic Other profile probe requirements, canonical scope boundaries, and
    bounded folder/catalog limits
- `other_library.py`
  - Other folder/file grouping, Library payloads, exact scope membership, and
    processing confirmation guards; action callers consume membership tokens
    rather than minting replacements
- `folder_profiles.py`
  - folder inspection and suggested override shaping
- `run_manifests.py`
  - candidate selection and run-manifest creation
- `representatives.py`
  - deterministic representative selection, measured content coverage,
    rationale, and safe public payload shaping

Guidance:

- Keep scan, probe, planning, and manifest orchestration logic under
  `mediaforce/library/` instead of spreading it back across the top-level
  package.
- Resolve operator paths through `MediaScope` before selecting items, loading
  staged artifacts, or comparing queue jobs. File scopes match one exact
  `rel_path`; folder scopes match case-sensitive, `/`-bounded descendants.
- TV item grouping remains series/season oriented, while an explicitly
  requested nested TV path retains its own bounded folder scope instead of
  widening to the season.

### `mediaforce/tuning/`

- `calibration_jobs.py`
  - calibration job persistence and queue helpers
- `size_goals.py`
  - canonical decimal-byte target intent and per-item runtime resolution
- `compression_intent.py`
  - versioned compression strategy identity, typed evidence references,
    deterministic size-change authorization, and acceptable-candidate ordering
- `stream_budget.py`
  - versioned deterministic whole-item budget and feasibility ledger
  - non-video estimates, provenance, uncertainty, source-relative video caps,
    and the remaining video bytes/bitrate for the persisted production plan
- `target_size_search.py`
  - deterministic target-size candidate search, typed search traces, final
    output verification, and bounded retry candidate selection
- `tuning_memory.py`
  - learned-memory session recording and artifact promotion helpers
- `quality_risk.py`
  - versioned quality-risk facts/gates/interpretation/operator-decision
    contract
  - allow-listed transform compilation checks, typed risk shaping, and
    evidence-bound review-record precedence
- `content_intent_observations.py`
  - append-only approved/rejected content-by-intent size boundaries,
    correction-safe persistence, compatibility identities, and deterministic
    local item/folder/content-class/operator replay
- `av1_cold_start.py`
  - versioned public prior schema/resource loading, semantic compatibility,
    private replay overlay, bounded CRF prediction, confidence, provenance, and
    no-recommendation behavior
- `av1_cold_start_evaluation.py`
  - privacy-safe held-out range, candidate-work, and safety-regression
    aggregation for controlled prior publication decisions
- `quality_memory.py`
  - read-only accepted-outcome cohorts, command-derived search signatures,
    confidence, dispersion, and explainable central CRF hints
- `quality_observations.py`
  - append-only structured search observations, bounded candidate traces,
    native-versus-backfill authority, corrections, and historical backfill
- `quality_shadow.py`
  - passive first-CRF recommendations, historical trace rejection, measured
    shadow comparisons, and aggregate readiness metrics
- `quality_warm_start.py`
  - active cohort qualification, exact-policy evidence gating, configured-bound
    CRF normalization, and passive-baseline benchmarks

Guidance:

- Keep calibration queue state, canonical size contracts, deterministic budget
  arithmetic, target-size candidate search, and learned-memory helpers under
  `mediaforce/tuning/`.
- Keep compression direction and contrary-movement authorization under
  `mediaforce/tuning/compression_intent.py`; routes, recovery, prompts, and the
  frontend consume its typed snapshots and decisions rather than deriving
  authority independently.
- Keep quality-risk facts, deterministic gates, typed risk normalization, and
  current review authority under `mediaforce/tuning/quality_risk.py` instead of
  re-deriving them independently in routes, prompts, or the frontend.
- Keep content-by-intent boundary identity, eligibility, append-only correction,
  and replay under `mediaforce/tuning/content_intent_observations.py`. Runtime
  review actions may emit explicit events, but they must not independently
  infer compatibility, relabel history, or mutate derived personalization.
- Keep shipped AV1 prior parsing, matching, public/local separation, and
  first-probe recommendation under `mediaforce/tuning/av1_cold_start.py`.
  Public bundle rows never enter the observation log, and local replay never
  writes back into package data.
- Keep install-safe package defaults under `mediaforce/package_defaults/`.
  Operator config, folder overrides, and machine-local paths remain under root
  `config/` and never become package data.
- Keep quality-memory retrieval read-only and under
  `mediaforce/tuning/quality_memory.py`; keep passive persistence under
  `mediaforce/tuning/quality_observations.py`; keep passive recommendation and
  evaluation under `mediaforce/tuning/quality_shadow.py`; keep active
  qualification under `mediaforce/tuning/quality_warm_start.py`; keep read-only
  acceptance aggregation under `mediaforce/tuning/quality_acceptance.py`.
  Persisted observations, shadow inference, active qualification, acceptance
  reporting, and search execution remain separate explicit phases.
- The stream budget ledger is the only non-video arithmetic authority. Search,
  manifests, queued jobs, production, and API payloads may project compatibility
  fields from it, but must not add audio, subtitle, attachment, or container
  overhead independently.
- Target-size search consumes an already validated stream budget ledger and
  already resolved transform plan. It may choose only CRF candidates inside the
  approved policy range; cadence, cleanup, stream selection, quality floors, and
  size caps remain upstream operator or ledger decisions.

### `mediaforce/reviewing/`

- `clips.py`
  - encoded preview, source review, compare orchestration, and remote artifact copies
- `renderers.py`
  - local and remote ffmpeg commands for browser-ready review media
- `assets.py`
  - contact sheets, timeline strips, spectrograms, and audio-review outputs
- `helpers.py`
  - review timestamp and measured review-moment recommendation
- `artifact_identity.py`
  - exact SHA-256 identity for the source and encoded clips bound to an
    operator review decision

`mediaforce.review` remains the stable compatibility wrapper surface.

### `mediaforce/advising/`

- `policy.py`
  - structured schemas, policy normalization, and deterministic budget checks
- `prompts.py`
  - sanitized seed, tune, artifact-critique, and note-parse prompt assembly
- `privacy.py`
  - prompt minimization, path/PII redaction, and safe evidence references
- `routing.py`
  - typed task routes, defaults, config resolution, and optional model pricing
- `runtime.py`
  - isolated Codex Lab execution, JSONL validation, and bounded fallback
- `telemetry.py`
  - bounded privacy-safe attempt telemetry and optional cost calculation
- `evals.py`
  - media-safe synthetic evaluation corpus, scorer, and CLI

`mediaforce.advisor` remains the stable compatibility wrapper surface.

Guidance:

- Keep model identifiers and fallback order in routing configuration, not in
  prompts or web handlers.
- Keep deterministic measurements, constraints, quality-risk gates, and current
  operator authority outside the model boundary.
- Do not retain prompts, model output, operator-note text, review media, or
  machine-local paths in advisor telemetry.

### `mediaforce/cli.py`

`mediaforce/cli.py` is still a single file. Split it into a package only if it
keeps growing materially.

## Frontend boundaries

### Route wrappers

- `frontend/src/routes/+page.svelte`
  - thin dashboard orchestrator
  - delegates visual sections to `frontend/src/lib/components/dashboard/`
- `frontend/src/routes/settings/+page.svelte`
  - thin wrapper around
    `frontend/src/lib/components/settings/SettingsEditor.svelte`
  - shared draft/action helpers live in
    `frontend/src/lib/settings/editor.ts`
- `frontend/src/routes/folders/[...prefix]/+page.svelte`
  - thin wrapper around
    `frontend/src/lib/components/folders/FolderStudioView.svelte`
  - shared folder workbench/display helpers live in
    `frontend/src/lib/folders/studio.ts`

Guidance:

- Treat the route breakup as complete for the structural refactor baseline.
- Keep extracting from the component layer first instead of rebuilding large
  route pages.

### Large extracted frontend views

- `frontend/src/lib/components/folders/FolderStudioView.svelte`
  - still the main decomposition target when folder feature work resumes
  - likely follow-on splits:
    - `FolderHeader`
    - `FolderTelemetryCard`
    - `FolderPolicyEditor`
    - `FolderQueueActions`
    - `FolderReviewPanel`
    - `FolderApprovalPanel`
    - folder modal components under `frontend/src/lib/components/folders/`
- `frontend/src/lib/components/settings/SettingsEditor.svelte`
  - split further only if settings UI grows again

## Extraction heuristics

Consider extracting a file when any of these is true:

- it is over roughly 800 lines and still growing
- it mixes transport, policy, and presentation concerns
- it owns multiple caches, executors, globals, and public handlers together
- it cannot be typed or tested locally without importing half the app
- unrelated features frequently touch the same file

## Validation gates for future structural changes

- `uv run --with pytest pytest tests/test_encode_queue_recovery.py \
tests/test_tuning_runtime.py tests/test_scanner_runtime.py`
- `uv run --with ruff ruff check <touched files>`
- `cd frontend && npm run check` when route payloads or frontend code change
- real browser verification when visible route behavior changes
