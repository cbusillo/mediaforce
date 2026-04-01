# Repo Structure Audit

This audit turns the module-refactor plan into an execution map. The goal is to
reduce oversized files, clarify ownership, and make future typing and behavior
changes cheaper without changing product behavior during the structural pass.

## Current shape

### Oversized backend files

- `mediaforce/web/app.py` (`5225` lines)
  - current concerns: FastAPI app factory, route handlers, queue workers,
    scan orchestration, review-state shaping, folder payload assembly,
    host-status cache, calibration helpers
  - recommended target: thin app factory plus `web/routes/` and
    `web/runtime/` packages
- `mediaforce/remote.py` (`1615` lines)
  - current concerns: SSH transport, lifecycle commands, wake behavior,
    readiness probes, setup/bootstrap, host status shaping
  - recommended target: `mediaforce/hosts/` package
- `mediaforce/execution.py` (`1209` lines)
  - current concerns: ffmpeg command building, mounted vs streamed
    execution, progress parsing, staging/promotion, manifest item execution
  - recommended target: `mediaforce/encoding/` package
- `mediaforce/advisor.py` (`986` lines)
  - current concerns: prompt assembly, structured response parsing,
    Codex subprocess invocation, schema shaping
  - recommended target: `mediaforce/advisor/` package
- `mediaforce/review.py` (`947` lines)
  - current concerns: preview rendering, compare generation,
    spectrogram rendering, remote review helpers
  - recommended target: `mediaforce/reviewing/` package
- `mediaforce/cli.py` (`647` lines)
  - current concerns: command registration, command handlers,
    formatting/output helpers
  - recommended target: `mediaforce/cli/` package if growth continues
- `mediaforce/web/settings_runtime.py` (`630` lines)
  - current concerns: runtime settings normalization, settings payload
    shaping, schedule rows, library rows, host rows
  - recommended target: keep as `web/runtime/settings.py` after the
    `web/` breakup

### Oversized frontend files

- `frontend/src/routes/folders/[...prefix]/+page.svelte` (`4519` lines)
  - current concerns: folder details, review state, queue controls,
    modals, policy editing, telemetry
  - recommended target: route page plus focused folder
    components/stores
- `frontend/src/routes/+page.svelte` (`1250` lines)
  - current concerns: dashboard orchestration, queue display, host
    cards, alerts, polling
  - recommended target: route page plus dashboard sections/stores
- `frontend/src/routes/settings/+page.svelte` (`1167` lines)
  - current concerns: settings forms, host editing, scheduler settings,
    secrets handling, validation
  - recommended target: route page plus settings form sections/components

## Backend package map

### Keep thin at top level

- `mediaforce/config.py`
- `mediaforce/db.py`
- `mediaforce/models.py`
- `mediaforce/utils.py`
- `mediaforce/type_defs.py`

These are already foundational enough that package extraction is optional.

### Target packages

#### `mediaforce/web/`

- `app.py`
  - app factory
  - middleware/static mounting
  - route registration only
- `routes/`
  - `dashboard.py`
  - `folders.py`
  - `settings.py`
  - `queue.py`
  - `review.py`
  - `artifacts.py`
- `runtime/`
  - `folder_cards.py`
  - `host_status.py`
  - `encode_queue.py`
  - `calibration_queue.py`
  - `review_state.py`
  - `scan_runtime.py`
  - `settings.py`
- `serializers.py`
  - API payload shaping shared across routes

#### `mediaforce/hosts/`

- `transport.py`
  - SSH command execution, SCP/rsync helpers, shared shell/path helpers
- `readiness.py`
  - host probe scripts, capability checks, status parsing
- `lifecycle.py`
  - wake/start/stop commands, cooldown behavior
- `setup.py`
  - prepare/reset trust/bootstrap flows
- `models.py`
  - `HostStatus`, `HostSetupResult`, host constants

#### `mediaforce/encoding/`

- `commands.py`
  - ffmpeg argument and preset construction
- `paths.py`
  - mounted/stream source and staging resolution
- `progress.py`
  - ffmpeg progress parsing and progress callback shaping
- `runner.py`
  - local vs remote execution entry points
- `staging.py`
  - validation, promotion, staging cleanup helpers
- `manifest.py`
  - encode-one / encode-many orchestration

#### `mediaforce/reviewing/`

- `previews.py`
  - encoded preview and source clip rendering
- `compare.py`
  - compare clip generation
- `audio.py`
  - spectrogram and audio-review outputs
- `timestamps.py`
  - review timestamp recommendation
- `remote.py`
  - remote preview helpers if still needed after extraction

#### `mediaforce/advisor/`

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

#### `mediaforce/cli/`

If `cli.py` keeps growing, move to:

- `__init__.py`
- `main.py`
- `commands/scan.py`
- `commands/review.py`
- `commands/run.py`
- `output.py`

## Recommended extraction sequence

### Stage 1: finish the `web/` split

1. Extract route-local payload and cache helpers from `mediaforce/web/app.py` into
   `mediaforce/web/runtime/` modules.
2. Move route handlers into `mediaforce/web/routes/` grouped by page/API area.
3. Keep `app.py` as registration + startup only.

Why first:

- It removes the biggest maintenance hotspot.
- It gives the rest of the backend a stable import surface.
- It reduces the risk that later domain extraction causes route-order breakage.

### Stage 2: split `mediaforce/remote.py` into `mediaforce/hosts/`

Prioritize these seams:

- transport helpers
- lifecycle commands
- readiness probe building/parsing
- setup/trust-reset flows

### Stage 3: split `mediaforce/execution.py` into `mediaforce/encoding/`

Prioritize these seams:

- path resolution
- command building
- progress parsing
- runner/execution mode differences
- staging/promotion

### Stage 4: split review/tuning pipelines

- `mediaforce/review.py` into `mediaforce/reviewing/`
- `mediaforce/advisor.py` into `mediaforce/advisor/`

### Stage 5: frontend route breakup

Do this after backend seams settle so route payloads stop moving under the UI at
the same time the components are being split.

## First extraction slice

The safest next code slice is inside `mediaforce/web/app.py`:

- extract folder-card cache and payload shaping to `mediaforce/web/runtime/folder_cards.py`
- extract host-status cache/refresh logic to `mediaforce/web/runtime/host_status.py`
- leave route registration and public API response shapes untouched

Why this slice:

- it is mostly helper logic, not route semantics
- it reduces global-state clutter in `app.py`
- it creates the first real `web/runtime/` package without forcing immediate
  route-handler moves

## Frontend split recommendations

### `frontend/src/routes/folders/[...prefix]/+page.svelte`

Split into:

- `FolderHeader`
- `FolderTelemetryCard`
- `FolderPolicyEditor`
- `FolderQueueActions`
- `FolderReviewPanel`
- `FolderApprovalPanel`
- modal components under `frontend/src/lib/components/folders/`

### `frontend/src/routes/+page.svelte`

Split into:

- `DashboardSummary`
- `DashboardQueuePanel`
- `DashboardHostGrid`
- `DashboardAlerts`

### `frontend/src/routes/settings/+page.svelte`

Split into:

- `SettingsLibrarySection`
- `SettingsSchedulerSection`
- `SettingsHostSection`
- `SettingsArchiveSection`

## Audit heuristics for future passes

A file should be considered for extraction when any of these is true:

- over ~800 lines and still growing
- mixes transport + policy + presentation concerns
- owns multiple caches/executors/globals plus public API handlers
- cannot be typed or tested locally without importing half the app
- changes frequently for unrelated features

## Validation gates

After each extraction slice:

- `uv run --with pytest pytest tests/test_encode_queue_recovery.py \
tests/test_tuning_runtime.py tests/test_scanner_runtime.py`
- `uv run --with ruff ruff check <touched files>`
- `cd frontend && npm run check` when route payloads or frontend code changes
- browser verification when visible route behavior changes
