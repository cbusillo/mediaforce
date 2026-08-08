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
  - managed subprocess cancellation, absolute deadlines, containment status,
    and command helpers
- `_process_deadline.py`
  - private per-command supervisor that keeps ownership until every observed
    descendant exits
  - Linux uses a scoped child subreaper plus pidfds; procfs disappearance is an
    exit race only when the pinned pidfd independently proves exit
  - macOS uses Darwin unique parent identities plus audit-token signaling; a
    uniquely live process that cannot provide a signal token remains live and
    makes cleanup unprovable rather than being classified as exited
  - each supervisor receives the read side of a parent-liveness pipe whose write
    side exists only in the Mediaforce parent; parent exit therefore produces
    EOF even when the target forks or detaches, and a surviving supervisor must
    terminate and reap its entire observed tree before exiting
  - the target closes the liveness descriptor before `exec`, so target
    descendants cannot delay parent-death detection
  - if the supervisor or containment status is lost before a complete/expired
    proof, the parent immediately performs best-effort private-process-group
    cleanup, then discards the bare numeric process-group ID and permanently
    poisons the managed controller; reset and reuse fail closed, future calls
    cannot signal an unrelated group after PGID reuse, and cancellation,
    deadline, or command success cannot replace the primary
    containment-enforcement error
  - arbitrary supervisor `SIGKILL` remains a deliberate residual-risk boundary:
    a descendant that already created a new session can outlive the private
    process group because the supervisor's pidfds or Darwin identity tokens die
    with it. Without a kernel-owned job container established before launch,
    the parent cannot safely rediscover ownership after reparenting and PID
    reuse, so Mediaforce reports cleanup as unproven instead of claiming it
  - Darwin `EVFILT_PROC` fork notifications are aggregated, and the local SDK
    documents kernel child tracking (`NOTE_TRACK`) as unsupported since macOS
    10.5. Every fork event therefore triggers global identity reconciliation
    and permanently marks ownership unproven. Compatibility consequence: a
    managed command that forks cannot report successful completion on macOS;
    unproven cleanup remains the primary enforcement failure even when a
    deadline or cancellation also occurred
  - containment fails closed before command success when required host
    primitives or descendant ownership proof are unavailable; it never falls
    back to same-user process scans or signals

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
    - new scan sidecars persist an explicit `scan_id` identical to the
      `scan_runs` primary key; startup reconciliation resolves that exact ID
      before any fallback, while legacy sidecars without an ID may use
      scope/time only to inherit a conservative non-success result,
    - workers revalidate sidecar ownership under the process-wide state lock
      before startup and every terminal save, so an older worker cannot overwrite
      a newer scan's sidecar; dead database rows are committed terminal before
      sidecar repair, so they remain terminalized even when sidecar persistence
      fails or the sidecar belongs to another scan,
      restores a completed database result over a stale active sidecar, and lets
      a matching or later database failure override contradictory legacy
      sidecar state
  - `calibration_runtime.py`
  - `av1_validation_derivation.py`
    - isolated derivation-only execution adapter over sampled calibration
    - shared-lock verdict/finalization transactions and current-input rechecks
  - `runtime_lock.py`
    - shared process exclusivity for web and bounded operator runtimes
    - stable parent-directory guard against lock-file unlink/recreate splits
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
  - managed web scans cooperatively honor cancellation across enumeration,
    database progress, and ffprobe; web shutdown waits only for a bounded grace
    period before forcing process exit while the runtime lease is still held,
    because Python cannot kill a thread blocked inside filesystem calls on an
    unavailable media mount
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
  - deterministic AV1 preregistration manifests, private redacted evidence
    validation, privacy-safe held-out range/candidate-work/safety aggregation,
    and non-mutating publication-review reports
- `av1_trait_projection.py`
  - versioned deterministic AV1 cell identity, review-only advisory separation,
    retained-analysis reclassification, and current producer reachability
- `av1_trait_feasibility.py`
  - read-only public-safe aggregate inventory and coherent derivation preflight
    for proposed AV1 validation cells
- `av1_validation_harness.py`
  - isolated reviewed-lock candidate contexts, validation-only warm-start hints,
    and canonical machine-bound target-size trace validation
- `av1_validation_v2.py`
  - immutable v2 preregistration, digest-bound aggregate eligibility,
    explicit excluded-cell records, and fail-closed derivation-only authority
- `av1_validation_derivation.py`
  - immutable partition-global plan, attempt, terminal, proposal, review-claim,
    review-evidence, and candidate-lock contracts
  - preregistered candidate statistics, affected-cell stop semantics, and
    authorization-bound Every Code runner identity
- `av1_validation_partition.py`
  - pure private v2 partition schema, domain-separated HMAC selection/tokens,
    canonical selection-lock digests, and fail-closed identity constraints
- `av1_validation_partition_inventory.py`
  - read-only current fingerprint/config projection into private partition
    candidates without runtime, observation, authorization, or encode authority
- `av1_validation_v3_tier2_inventory.py`
  - claim-context-gated read-only in-memory Tier 2 private-ineligible candidate
    projection from current measured fingerprints, with no keys, selector call,
    serialization, publication, filesystem/media access, or runtime authority
- `av1_validation_v3_tier2_inventory_authorization.py`
  - pure owner-only A0 request, grant, claim, and read-context contracts for one
    private inventory read, with no database, filesystem, key, selection,
    publication, media, runtime, or downstream execution authority
- `av1_validation_v3_tier2_inventory_publication.py`
  - owner-only request/grant/claim artifact publication using hardened artifact
    helpers, including grant-ID-exclusive read-claim consumption and
    read-specific conflict signaling
- `av1_validation_v3_tier2_inventory_operation.py`
  - claim-consuming wrapper that publishes the read claim before invoking the
    inventory adapter and returns only a privacy-safe non-serializing summary
- `av1_validation_v3_tier2_selection.py`
  - pure owner-only v3 Tier 2 typed-source selection records, canonical
    candidate/set commitments, and privacy-safe non-authorizing summaries
- `av1_validation_v4*.py`
  - canonical AV1 v4 manifest, rights, preparation, freeze, qualification
    request, and non-authorizing production-readiness contracts
  - pure production execution-plan derivation stays in
    `av1_validation_v4_execution_plan.py`, where source paths are opaque config
    strings used only for invocation identity
  - `av1_validation_v4_execution_preflight_operation.py` materializes the
    owner-only blocked revision-2 readiness artifact without qualification
    grant/claim consumption, runner callbacks, source media path probes,
    tool probes, or network access
  - `av1_validation_v4r3_manifest.py` is the pure canonical revision-3 manifest
    overlay: it binds the immutable revision-2 base by manifest ID and digest,
    embeds the complete public invocation closure, freezes the eight-window
    sequencing budget and revision-3 path-privacy domains, and keeps every v4
    authority field false; the checked-in JSON artifact and generic verifier
    own filesystem loading outside this module
  - `av1_validation_v4r3_rights.py` is the pure revision-3 rights rollover:
    it embeds one fully validated owner-attested revision-2 rights artifact as
    non-authorizing provenance, copies its frozen terms and source claims
    exactly, binds an independent revision-3 owner reaffirmation and derivative
    profile acknowledgement, and carries NASA acknowledgement/endorsement
    obligations forward without filesystem or execution imports
  - `av1_validation_v4r3_invocation_closure.py` is a pure no-media module
    (protocol v4, manifest revision 3) that freezes the full checked-in
    production video policy with the revision-2 CRF override, derives per-source
    size-goal/transform/ledger closure payloads from revision-2 public facts,
    and leaves production source, stream-plan, ledger, and quality-temp HMAC
    identities unresolved until private preparation; it imports no media,
    database, runtime, filesystem, or network surface and carries no authority
  - `av1_validation_v4r3_paths.py` owns the shared string-only canonical
    absolute-POSIX path rule used by revision-3 private identity contracts;
    `av1_validation_v4r3_path_privacy.py` binds that rule to the approved
    manifest's key, instance-path, and source-path HMAC domains and prefixes,
    without exposing key bytes or granting selection/partition authority
  - `av1_validation_v4r3_preparation_grant.py` is the pure canonical
    single-use preparation-grant contract: it binds the approved manifest,
    revision-3 rights identity, repository commit/tree, and a manifest-derived
    opaque revision-3 registry token while limiting authority to path-privacy
    key creation and non-media preparation measurements; it accepts mappings
    and canonical bytes only and creates no key, registry, artifact, or I/O
  - `av1_validation_v4r3_preparation_custody.py` owns the pure path-free
    registry marker, single-use grant claim, and path-privacy key-custody
    evidence contracts; each is revision-scoped, canonical, private-text-free,
    and carries every v4 authority field as false
  - `av1_validation_v4r3_preparation_custody_registry.py` is the owner-only
    local I/O boundary for those contracts: one private mode-`0700` registry
    outside the repository, one process lock plus `flock`, descriptor-relative
    mode-`0600` singleton records, claim-before-key ordering, exclusive 32-byte
    key creation, and claim-retaining rollback on every post-claim failure;
    tests use temporary custody only and no real owner artifacts are created
  - `av1_validation_v4r3_ordinal_window.py` is the pure public artifact module
    for revision-3 ordinal-window plan/grant/claim/started/outcome/terminal
    records; it accepts mappings and canonical bytes only, publishes only an
    opaque `run_registry_id`, validates exact revision-3 closure order from
    `build_av1_v4_r3_all_closure_payloads()`, and imports no filesystem surface
  - `av1_validation_v4r3_execution_preflight.py` is the pure public
    execution-readiness contract and plan/preflight pair validator; it binds the
    semantic plan seam, exact request/repository/toolchain/closure identities,
    deterministic ordinal windows, and unresolved runner-owned media bindings
    while preserving every execution and media authority field as false
  - `av1_validation_v4r3_execution_preflight_operation.py` is the sole
    owner-only cross-registry materializer: it custody-reconstructs the retained
    preparation/request chain under the preparation lock, releases that lock,
    verifies the path-privacy key identity and opaque ordinal-registry HMAC, then
    acquires the ordinal lock to build and publish the plan/preflight pair; the
    operation never nests the registries, reads media, or grants execution
  - `av1_validation_v4r3_execution_grant.py` is the pure per-ordinal owner
    execution-authority contract; it binds the reviewed plan/preflight and
    sequencing grant to one closure/invocation and is the only revision-3
    artifact whose three execution-envelope authority booleans are true
  - `av1_validation_v4r3_runner_admission.py` is the pure non-authorizing
    execution-claim and runtime-admission contract; it declares intent to
    consume a grant through later exclusive custody and binds resolved
    source/instance/temp, canonical production stream-plan and ledger payload
    identities, and frozen size-goal, ledger-closure, and transform-plan
    identities without performing a production runtime operation
  - `av1_validation_v4r3_ordinal_window_registry.py` is the owner-only local I/O
    boundary for those records and the canonical `preflight.json`: callers
    supply a private registry binding for an absolute owner-only mode-`0700`
    directory, and every transition runs under a process-local lock plus `flock`
    on one `O_DIRECTORY|O_NOFOLLOW` descriptor with `dir_fd` custody reads/writes
    of owner-owned mode-`0600` single-link files; grant and every later ordinal
    transition require the custody-valid plan/preflight pair, while registry
    state, not caller dictionaries, determines predecessor hashes, high-water,
    failure/lapse/crash terminal absorption, and final comparability
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
