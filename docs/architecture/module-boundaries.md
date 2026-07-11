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
- `setup.py`
  - prepare/reset trust/bootstrap flows
- `models.py`
  - host datatypes and constants

`mediaforce.remote` remains the stable compatibility wrapper surface for direct
test patch targets.

### `mediaforce/encoding/`

- `encode_queue.py`
  - encode queue state, job persistence, and summaries
- `ffmpeg.py`
  - ffmpeg capability and hwaccel helpers
- `quality.py`
  - quality-search and sample-encode helpers
- `commands.py`
  - ffmpeg argument and preset construction
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

`mediaforce.execution` remains the stable compatibility wrapper surface.

### `mediaforce/library/`

- `planner.py`
  - item recommendation and manifest-item shaping
- `probe.py`
  - ffprobe-based media inspection and track summaries
- `scanner.py`
  - library scan orchestration and catalog updates
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

### `mediaforce/tuning/`

- `calibration_jobs.py`
  - calibration job persistence and queue helpers
- `tuning_memory.py`
  - learned-memory session recording and artifact promotion helpers

Guidance:

- Keep calibration queue state and learned-memory helpers under
  `mediaforce/tuning/`.

### `mediaforce/reviewing/`

- `previews.py`
  - encoded preview and source clip rendering
- `compare.py`
  - compare clip generation
- `audio.py`
  - spectrogram and audio-review outputs
- `helpers.py`
  - review timestamp and measured review-moment recommendation
- `remote.py`
  - remote preview helpers when needed

`mediaforce.review` remains the stable compatibility wrapper surface.

### `mediaforce/advising/`

- `models.py`
  - response dataclasses and constants
- `prompts.py`
  - seed/tune/verdict prompt assembly
- `schemas.py`
  - structured response schemas
- `runner.py`
  - Code CLI subprocess invocation
- `parsing.py`
  - normalization and response shaping

`mediaforce.advisor` remains the stable compatibility wrapper surface.

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
