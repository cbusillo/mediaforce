# AV1 Cold-Start Controlled Validation

Issue #277 owns the controlled evidence that may prove or reject public AV1
cold-start cells. The checked-in preregistration freezes the experiment design
without selecting media, starting Mediaforce, or authorizing any encode.

## V1 historical protocol

`docs/validation/av1-cold-start-preregistration-v1.json` is canonical JSON with
a deterministic manifest ID and payload digest. It declares:

- two activation-eligible `balanced` cells for exact `animation` and `typical`
  trait sets
- sixteen independently mapped held-out case slots per activation candidate
- fallback-conformance coverage for darkness, motion, grain/noise,
  texture/detail, low-motion dialogue, mixed, and unknown content
- explicit no-recommendation coverage for `reference`, `transparent`, and
  `perceptual_floor` intentions
- alternating baseline-first and guided-first order for paired candidate cases
- a six-month expiration that requires re-review if execution has not started

The manifest contains no title, path, source ID, fingerprint, review-media
identity, operator note, runtime database value, or case-to-media mapping. The
case IDs are protocol slots, not hashes of media.

Validate it without loading Mediaforce config or state:

```bash
uv run python -I -S scripts/verify_av1_cold_start_preregistration.py \
  validate docs/validation/av1-cold-start-preregistration-v1.json --json
```

The validator reports `runtime_execution_authorized=false`. V1 remains immutable
historical no-go evidence; its underfilled and unreachable cells are not
reinterpreted, relabeled, or backfilled.

## V2 successor preregistration

`docs/validation/av1-cold-start-preregistration-v2.json` is the canonical issue
`#285` successor. It binds `acstp1`, `acstf1`, `av1vh1`, the unchanged production
predictor contract `acsp1`, the validation-candidate contract `acsvp1`, and the
derivation-authorization contract `acsvda1`. It registers:

- sixteen paired holdout slots each for exact `motion` and exact `darkness`
  `balanced` candidates
- three baseline-only fallback-conformance slots each for grain/noise, mixed,
  texture/detail, and the `reference`, `transparent`, and `perceptual_floor`
  directional-intent paths
- fifty total case slots, with candidate ordinals alternating baseline-first
  and guided-first and duplicate review fixed at ordinals 4, 8, 12, and 16
- explicit exclusions for animation and typical candidate cells, low-motion
  dialogue, and unknown fallback rather than silently dropping them

The selection was frozen from a fresh read-only aggregate inventory at
`2026-07-27T21:08:41Z`. The count-bearing eligibility attestation remains in
machine-local state outside the repository. The checked-in manifest carries
only its deterministic ID and SHA-256 digest; it contains no runtime database
values, media identities, paths, titles, fingerprints, or mappings.

Validate the successor without loading runtime state:

```bash
uv run python -I -S scripts/verify_av1_cold_start_preregistration.py \
  validate docs/validation/av1-cold-start-preregistration-v2.json --json
```

The v2 manifest reports both `runtime_execution_authorized=false` and
`holdout_execution_authorized=false`. Its authority is preregistration only. A
merged v2 permits #286 to prepare the private source partition while Mediaforce
remains paused; it does not authorize observations, encodes, review media, or a
candidate probe.

The separate `acsvda1` authorization may later permit ordinary sampled
calibration and unchanged measured full search on selection-lock-reserved
derivation sources only. It hard-rejects validation candidates, guided probes,
holdout case execution, and public bundle activation. A different, later
authorization must bind the reviewed candidate locks before any v2 holdout can
run. The verifier therefore refuses the v2 `report` action at this phase.

## V3 owner-approved protocol

`docs/validation/av1-cold-start-preregistration-v3.json` is the canonical
non-executing protocol approved through issues `#302` and `#303`. V3 is a new
experiment, not a repair or reinterpretation of v2. The protocol uses an
explicit `protocol_version` and experiment identity in every v3 ID and HMAC
domain while retaining the immutable v2 manifest ID and digest as
`supersedes_*` provenance.

The checked-in protocol freezes:

- exact `darkness/balanced` and `motion/balanced` powered cells
- exact `animation/balanced` and `typical/balanced` Tier 2 qualification
  strata, one private source per stratum, with both candidate configurations
  traversed only after gate A0
- a distinct qualification HMAC key/domain that is committed before any Tier 2
  private inventory read and is never reused for empirical selection
- twelve required derivation observations plus two ranked reserves per cell, a
  two-void per-cell cap, and a same-stage global stop on the third void
- sixteen paired holdouts per cell, the inherited thirteen-hit threshold, the
  full non-tied sign-test table, and nine-decimal component-power disclosure
- independent qualification namespace/authority and `evidence_eligible`
  barriers, with missing eligibility failing closed
- exact A0/A/B/C/D/E/F chronology, holdout-first one-event partitioning,
  privacy boundaries, and non-automatic publication/activation

The protocol also freezes the exhaustive terminal disposition over the existing
derivation reason vocabulary. Only machine-proven pre-measurement failures may
void an assignment. At-or-post-measurement, operator-controlled, unknown, and
unproven terminals are cell-fatal; bare `runtime_failure` is v3 protocol
nonconformance. Failing to reach twelve usable observations inside the fourteen
frozen assignments is `technically_infeasible` and non-evidentiary.

Validate the canonical protocol without loading runtime config or state:

```bash
uv run python -I -S scripts/verify_av1_cold_start_preregistration.py \
  validate docs/validation/av1-cold-start-preregistration-v3.json --json
```

Validation reports every execution authority as false. The checked-in protocol,
its builder, synthetic Tier 2 selector tests, chronology checks, evidence
barriers, and terminal helpers create no private inventory, key, qualification,
partition, derivation, holdout, publication, or activation authority. Those
actions remain in separately gated issues `#304` through `#310`.
Canonical loading remains valid for historical audit after expiration. Every
future gate that can create execution authority must separately call the
deterministic protocol-active check with its frozen as-of timestamp.

Issue `#303` also defines the non-executing A0/A qualification contracts in
`mediaforce/tuning/av1_validation_v3_qualification.py`. A qualification plan
binds the v3 protocol digest, qualification-key ID, eligibility-predicate,
repository commit/tree, configuration, toolchain, and fixture-matrix digests to
an explicit validity window. Its attestation requires the complete frozen path
matrix: Tier 1 success coverage for both candidates; every registered runtime
failure, non-runtime terminal, and recovery path; plus Tier 2 success coverage
for both candidates in each frozen stratum. The contract rejects missing,
duplicated, private fault-injection, stale, re-bound, non-canonical, or
non-paused/unclean attestations.

These are pure data contracts and public-safe summaries only. They do not load
private inventory, select a source, create a qualification key, run fixtures,
or create evidence or empirical authority. Tier 2 qualification execution remains
blocked in issue `#305`; a future executor must supply separate
owner authorization and validate both the plan and attestation against the
frozen protocol at its explicit timestamp.

Issue `#305` begins with the pure Tier 2 selection-record contract in
`mediaforce/tuning/av1_validation_v3_tier2_selection.py`. The builder accepts
only caller-supplied typed `AV1ValidationV3QualificationSource` records, an
active qualification plan, and the already committed in-memory qualification
key; it stores no key and delegates the actual choice to the
frozen `select_av1_validation_v3_tier2_sources` selector. The owner-only
record commits to the complete sorted deduplicated candidate source records,
candidate count, canonical selections, selected timestamp, record ID, and
payload digest. Loading requires exact canonical bytes, and source validation
redraws the record from the supplied candidates so candidate-set drift changes
the record ID. Its public summary exposes only fixed non-executing status,
protocol/plan identities, and frozen stratum names; it carries no record ID,
timestamp, fingerprints, ranks, inventory digest/count, paths, titles, or key
material.
The contract performs no inventory, database, or media scan; its loader reads
only the caller-supplied selection-record path. It creates no publication,
execution, empirical, derivation, holdout, grant, claim, lock, subprocess, or
feature-flag authority.

The second bounded `#305` slice adds
`mediaforce/tuning/av1_validation_v3_tier2_inventory.py`, a read-only private
inventory projection adapter for Tier 2 private-ineligible candidates. It
directly reads current measured fingerprint rows, keeps only the dominant
compatible evidence cohort, validates strict 40-hex content-version identities,
drops duplicate identity groups, confirms `balanced` compression intent,
projects exact traits, and requires one frozen Tier 2 stratum with no powered
candidate-cell overlap and a feasible stream budget. Its in-memory private
entries contain only local item ID, content-version identity, current evidence
digest, and the v3 qualification source. The derived source fingerprint is
domain-separated from a canonical JSON payload containing only v3 protocol
identity and the content-version fingerprint; paths, titles, series, group
identity, v2 history, qualification keys, selection output, files, media, and
runtime state are not stored, hashed, opened, or published.
`pipeline_ready` in this adapter means only that the persisted policy, quality,
and stream-budget projection is candidate-feasible; it makes no claim about
current runtime or tool availability and grants no execution authority.

The third bounded `#305` slice adds
`mediaforce/tuning/av1_validation_v3_tier2_inventory_authorization.py`, a
pure owner-only private inventory read authorization contract. It defines three
frozen/slots dataclasses — request, grant, and claim — plus one exception type
(`AV1ValidationV3Tier2InventoryAuthorizationError`). All artifacts carry
content-addressed IDs and payload digests computed with `av1_validation_v3_id`
and `stable_json_hash`.

The request binds the exact protocol and qualification plan IDs/digests, the
plan's `qualification_key_id` and `eligibility_predicate_sha256`, repository
commit/tree, config SHA, a recomputed frozen Tier 2 scope/ranking digest, and a
recomputed inventory-projection-contract digest; `requested_at` and `valid_until`
must fall within the plan window. The scope digest covers the exact Tier 2
strata, candidate powered cells excluded by the adapter, total slot count and
per-stratum slot expectations, and the HMAC ranking algorithm and domain already
frozen by the selection contract. The projection-contract digest is a pure
constant that binds the adapter's eligibility rules: fingerprint domain, strict
40-hex identity contract, dominant-cohort tie-break rule, required balanced
intent, exact Tier 2 stratum, no powered overlap, complete quality contract,
feasible stream budget, no candidate cap, duplicate-identity drop-all semantics,
fingerprint-collision failure, `pipeline_ready` semantics, and the frozen
exclusion-counter vocabulary.

The request carries `private_inventory_read_authorized: False`. The grant binds
the request digest, owner principal, and authorization window; the grant and
claim are the only artifacts that set `private_inventory_read_authorized: True`.
The claim binds
the full plan/request/grant chain and a `claimed_at` timestamp that must fall
within the grant window. The request, grant, and claim explicitly bind one read;
the fourth bounded `#305` slice adds the durable publisher and adapter guard
that enforce that one-read claim before the inventory adapter can query the
database. All other authority bits — Tier 1/2 execution, selection, runtime,
raw-media read, key creation/loading, private-inventory serialization, evidence,
retry, derivation, holdout, publication, activation, and public-bundle
activation — remain constant `False` and are parser-validated. No key bytes are
accepted or stored by any public API, and this module performs no filesystem
I/O.

The fourth bounded `#305` slice adds
`mediaforce/tuning/av1_validation_v3_tier2_inventory_publication.py` and
`mediaforce/tuning/av1_validation_v3_tier2_inventory_operation.py`, and updates
`load_av1_validation_v3_tier2_inventory(...)` to require a pure read context,
the exact frozen config snapshot bytes, and an injectable clock. The adapter
validates the current timestamp, full plan/request/grant/claim chain, protocol
scope/projection contract, config snapshot bytes, and plan config SHA before
the first measured-fingerprint DB row can be requested. The publisher reuses the
hardened owner-only artifact helpers for request, grant, and claim artifacts.
Request and grant artifacts are idempotent by request ID; claim consumption is
exclusive by grant ID. The publisher can reconcile identical claim bytes, but
the operation rejects any non-new claim before entering the adapter; a distinct
claim for the same grant fails with read-specific
`inventory_read_already_claimed`.
The operation wrapper validates the full read boundary, publishes/consumes the
claim, and then calls the adapter, retaining the private inventory only in
memory while exposing a privacy-safe public summary with opaque artifact IDs
and authority bits but no inventory counts. It still performs no live private
DB/media read in tests and adds no
selection, CLI, subprocess, ffmpeg, inventory serialization, execution,
evidence, retry, or downstream authority.

The fifth bounded `#305` slice adds
`mediaforce/tuning/av1_validation_v3_tier2_selection_authorization.py`,
`mediaforce/tuning/av1_validation_v3_tier2_selection_publication.py`, and
`mediaforce/tuning/av1_validation_v3_tier2_selection_operation.py`. It composes
the unchanged one-read inventory chain with a separate owner grant for exactly
one qualification-key read and one HMAC selection. The outer request binds the
same protocol, plan, repository, config, Tier 2 scope, inventory projection,
selection algorithm, committed key ID, and inner inventory request. Its claim
also binds the inner inventory claim, and cross-chain validation requires the
same plan and owner. The outer artifacts never confer private-inventory or media
read authority; the inner artifacts never confer key-read or selection
authority.

The combined operation validates both chains before private I/O, consumes the
outer selection claim, consumes the inner inventory claim, performs one
read-only inventory projection, loads and verifies the committed qualification
key, applies the frozen selector in memory, and publishes the canonical full
selection record through owner-only artifact helpers. The returned operation
result retains neither the inventory nor key bytes, and its public summary
contains no candidate count, fingerprint, rank, key ID, record identity,
digest, or path. Any replay, missing or mismatched key, unfillable stratum, or
publication failure is terminal for those claims; no retry is authorized. This
slice uses synthetic SQLite and temporary artifact roots only and adds no CLI,
live execution, media access, encoding, evidence, empirical partitioning,
derivation, holdout, publication, or activation authority.

The first `#304` preparation artifact is a Tier 1 owner-authorization request
contract in `mediaforce/tuning/av1_validation_v3_tier1_request.py`. It binds a
future real qualification plan to the exact protocol, commit/tree,
configuration, toolchain, fixture-matrix, and path-matrix identities while
declaring deterministic synthetic/public fixtures only. Its canonical payload
is intentionally **not** a grant: private inventory, key creation, media reads,
qualification execution, evidence creation, empirical authority, and activation
are all false, and a separate owner authorization remains required. A request
must be generated from actual future machine-local inputs outside the repository
and is invalid when its bound qualification plan changes or expires.

Tier 1 uses the checked-in deterministic synthetic fixture matrix at
`docs/validation/av1-tier1-synthetic-fixture-matrix-v1.json`: flat-field,
high-motion, high-detail/noise, and scene-change fixtures. It does not use the
operator media drive, library inventory, or external media sources.

The executable successor matrix is
`docs/validation/av1-tier1-synthetic-fixture-matrix-v2.json`. It preserves v1
as immutable history while freezing exact Lavfi graphs, FFV1/NUT intermediate
representation, decoded-frame content hashing, and `ffprobe -count_frames`
verification. No runner may infer or substitute generator semantics.

Matrix v2 and its consumed cohort remain immutable history. The next frozen
matrix is `docs/validation/av1-tier1-synthetic-fixture-matrix-v3.json`. It keeps
the four graphs, frame specification, probe, and decoded-stream hash contract
unchanged, but uses the exact measured-compatible intermediate: FFV1 in
Matroska with explicit BT.709 primaries, transfer, colorspace, and limited-range
output flags. Its provenance binds the diagnostic compatibility result that
showed both NUT variants dropping all four color fields while the selected
Matroska representation preserved them at stream and first-frame surfaces.

Matrix v3 authoring is non-executing. The separate residual public-synthetic
diagnostic is frozen at
`docs/validation/av1-tier1-residual-probe-matrix-v1.json`. It copies the four
matrix-v3 Lavfi fixtures byte-for-byte and in the same order, uses exactly the
selected explicit-color FFV1/Matroska representation, and verifies each
288-frame fixture through stream probing, first-frame probing, and streamed
decoded rawvideo SHA-256 byte counting. The residual matrix is
diagnostic-only, evidence-ineligible, and binds the matrix-v3 digest in
`informed_by`; it does not create Tier 1 coverage or qualify any candidate by
itself.

`mediaforce/tuning/av1_validation_v3_tier1_executor.py` is the isolated
contract-only matrix-to-command and output-verification boundary. It loads only
the frozen v3 matrix, builds argv lists without invoking a shell, validates
`ffprobe` output,
and accepts a streaming SHA-256 result from a dependency-injected command
executor. It rederives every executable command from the frozen matrix, proves
the active Tier 1 request/grant chain before each command boundary, rejects an
existing or symlink output for the selected fixture, and requires the exact
decoded-frame byte count.
Outcomes bind the request, grant, repository commit, toolchain, matrix, and
command-plan identities. It has no database, private inventory, web-runtime,
v2, derivation, or holdout imports; the later runtime adapter must still prove
the pause lock and provide the shell-free subprocess implementation. Its
`run_streaming_sha256` implementation must hash and count the same decoded byte
stream incrementally with bounded memory; this contract intentionally carries
no decoded `stdout` buffer.

`mediaforce/tuning/av1_validation_v3_tier1_runtime.py` is the concrete,
library-only paused-runtime adapter for that contract. It has no CLI entrypoint
and does not itself start qualification. A session requires a fresh active Tier
1 request/grant context, an exact toolchain binding, the frozen matrix, an
absolute external output directory, and a newly acquired exclusive Mediaforce
runtime lease. It accepts only the exact generation, probe, and content-hash
argv derived from the frozen matrix; resolves `ffmpeg` and `ffprobe` to bound
absolute executables; strips the child environment; bounds probe output and
diagnostics; incrementally hashes and counts decoded bytes; kills timed-out or
over-limit process groups; and removes only regular fixture outputs that it
tracked. The later execution entrypoint must construct this adapter from a
fresh post-merge qualification plan/request/grant and must not bypass the
session factory.

`mediaforce/tuning/av1_validation_v3_tier1_coverage.py` defines the canonical
receipt for one complete synthetic Tier 1 run. The receipt remains at gate A0:
it records the four fixture outcomes and their plan/request/grant, repository,
config, toolchain, and matrix bindings, but explicitly claims no qualification
path coverage, Tier 2 completion, evidence eligibility, publication, or
activation authority. It can be produced only while the grant is active and
only when runtime pause and output cleanup both succeeded. The full
qualification attestation contract does not accept this receipt.

Future receipt publications atomically include a private-safe companion
`tier1-run-diagnostics.json`. It binds to the receipt and records only a closed
allowlist of scalar probe observations plus bounded command metadata (program,
argv digest, outcome, return code, byte counts, and truncation state). It never
stores stderr text, paths, private media data, or additional authority. The
coverage-receipt schema remains unchanged so historical Gate A0 receipts stay
canonically loadable.

`mediaforce/tuning/av1_validation_v3_tier1_operation.py` is the bounded
orchestrator for that receipt. It enters the existing paused-runtime adapter,
publishes a durable single-execution claim before the first fixture command,
runs the four frozen fixture IDs once in canonical order, rechecks grant time at
each fixture boundary, and builds a receipt only after tracked-output cleanup
succeeds. A retained claim blocks an automatic retry after interruption or
failure; recovery requires a separately reviewed successor authorization.

The owner-only runner exposes two separate actions. `authorize-tier1-execution`
loads the exact post-merge protocol, plan, request, effective-config snapshot,
repository identity, and toolchain before publishing one grant keyed by request
ID. `run-tier1-synthetic-qualification` safely reloads that grant, acquires the
pause lease, publishes the single-execution claim, runs only the frozen v3
synthetic matrix, and publishes the Gate A0 coverage receipt. Neither action
reads private inventory or media, starts Tier 2, creates empirical evidence, or
confers derivation, holdout, publication, or activation authority.

The consumed Tier 1 cohort showed that generation, decode, byte-count, content
hashing, and cleanup completed while the FFV1/NUT intermediate did not expose
the expected BT.709 color description. The successor compatibility probe is a
separate, non-evidentiary diagnostic contract. Its frozen matrix at
`docs/validation/av1-tier1-compat-probe-matrix-v1.json` creates one two-frame
public flat field in three representations: baseline FFV1/NUT, explicit-color
FFV1/NUT, and explicit-color FFV1/Matroska. It records stream-level and
first-frame `ffprobe` observations for each representation. It does not repeat
content hashing, rerun the consumed cohort, or qualify any candidate.

The compatibility workflow uses structurally disjoint request, grant, claim,
and result schemas and artifact directories. `publish-tier1-compat-request`
binds the diagnostic matrix to an active exact-machine qualification plan but
confers no execution authority. `authorize-tier1-compat-probe` grants only the
bounded public-synthetic diagnostic. `run-tier1-compat-probe` acquires the same
fresh pause lease, durably consumes one claim before the first command, executes
the three representations once, removes tracked outputs, and publishes six
observations plus bounded command metadata. A retained claim forbids retry.
Every compatibility artifact declares Tier 1/Tier 2 qualification, evidence,
derivation, holdout, publication, and activation authority false. Measured
results require a separate reviewed matrix successor before qualification can
continue.

The residual workflow mirrors the compatibility workflow with distinct request,
grant, claim, result schemas and artifact directories.
`publish-tier1-residual-request` binds the residual matrix to the active
exact-machine qualification plan and confers no execution authority.
`authorize-tier1-residual-probe` grants only the bounded public-synthetic
residual diagnostic. `run-tier1-residual-probe` acquires the fresh pause lease,
publishes one durable no-retry claim, executes the four fixture plans once in
canonical order, and publishes exactly twelve variant records in stream,
first-frame, then hash order for each fixture. The runtime allowlist contains
twelve buffered commands and four streaming hash commands, all derived from the
frozen residual matrix. Every residual artifact declares Tier 1/Tier 2
qualification, coverage, evidence, derivation, holdout, publication, and
activation authority false.

`mediaforce/tuning/av1_validation_v3_tier1_preparation.py` defines the pure,
non-executing preparation inputs for Tier 1. It freezes a machine-checkable
eligibility predicate, domain-separated identity for the exact config bytes,
the canonical fixture matrix digest, and the machine-local toolchain binding
into the qualification plan. Request construction must be given the config
bytes and toolchain again and rejects drift before producing the owner-action
request. This layer performs no filesystem publication, grant creation, runtime
locking, or subprocess execution.

## Private v2 source partition

Issue `#286` uses the `av1vsp1` partition contract to freeze all fifty holdout
slots and two twelve-source derivation reservations while Mediaforce remains
paused. The pure contract lives in
`mediaforce/tuning/av1_validation_partition.py`; the separate read-only
inventory adapter lives in
`mediaforce/tuning/av1_validation_partition_inventory.py`. Neither module
starts runtime work, probes media, creates observations, builds a candidate
lock, or creates either execution authorization.

Create a dedicated owner-only directory outside the repository, then create its
HMAC key once:

```bash
uv run python -I -S scripts/verify_av1_cold_start_preregistration.py \
  create-partition-key /private/owner-only/av1-v2/partition.key \
  --config config/defaults.toml --json
```

The command emits an opaque `token_key_id`. Record that ID on issue `#286`
before reading the private inventory. The later build requires the same ID, so
replacing or rerolling the HMAC key after its durable commitment fails closed.
Key creation and partition construction both acquire the same exclusive runtime
lock as `mediaforce-web`; they fail closed unless Mediaforce is paused.
Key publication uses a hidden owner-only temporary file, durable file sync,
exclusive atomic rename, and parent-directory sync. Retrying the same path
validates the existing key and returns the same ID with `created=false`.

Build the immutable private partition from the checked-in manifest, pinned
machine-local eligibility attestation, current measured fingerprint inventory,
and an explicit canonical UTC timestamp:

```bash
uv run python -I -S scripts/verify_av1_cold_start_preregistration.py \
  build-partition docs/validation/av1-cold-start-preregistration-v2.json \
  /private/owner-only/eligibility-attestation-v1.json \
  --key /private/owner-only/av1-v2/partition.key \
  --expected-token-key-id av1vkey1_<committed-id> \
  --output /private/owner-only/av1-v2/source-partition-v1.json \
  --selected-at 2026-07-27T23:00:00Z --json
```

The selection timestamp must be at or after the manifest registration and
strictly before its expiration. Both the initial build and every embedded or
current-input replay reject an out-of-window timestamp.

Validate both the embedded immutable private inventory snapshot and the current
read-only database/config projection. A narrowed inventory, selected-source
change, taxonomy change, policy change, or compatibility change fails the
current-input gate:

```bash
uv run python -I -S scripts/verify_av1_cold_start_preregistration.py \
  validate-partition docs/validation/av1-cold-start-preregistration-v2.json \
  /private/owner-only/eligibility-attestation-v1.json \
  /private/owner-only/av1-v2/source-partition-v1.json \
  --key /private/owner-only/av1-v2/partition.key --json
```

The partition requires owner-only directory and file permissions, rejects any
private artifact path inside the repository, and never prints the output path,
local item IDs, identity tokens, fingerprints, titles, series, or source-group
values. Its public-safe output is limited to the manifest ID, the selection-lock
and derivation-partition digests, preregistered counts, and false execution
authority flags.

Selection uses domain-separated HMAC-SHA256 ranking and identity tokens. It
enforces exact trait selectors for publication candidates, the registered
`contains_all` selectors for fallback cells, and global uniqueness across all
seventy-four reservations by content version, logical title, and series.
Publication holdout and derivation cohorts separately enforce the six-group
minimum and one-third concentration maximum, and no derivation source group may
overlap any holdout source group. Ambiguous duplicate content versions are
excluded before the immutable inventory snapshot is frozen. Within a plan,
holdout slots receive selection priority over derivation reservations so the
holdout cohort cannot be shaped by later derivation needs.

The lock also binds one coherent pre-execution compatibility signature,
plan-specific policy signatures, target-video-bitrate ranges, and the configured
numeric quality floor before any derivation observation exists. Revalidate the
partition before creating the later derivation authorization. Any config,
policy, toolchain, or selected-source drift is a stop condition, not a reason to
rewrite the frozen snapshot or mapping.

The private partition and key are machine-local audit artifacts. Only the
reviewed `selection_lock_sha256` and `derivation_partition_sha256` may leave
owner-only storage. The lock carries `runtime_execution_authorized=false`,
`derivation_execution_authorized=false`, and
`holdout_execution_authorized=false`; creating it does not create `acsvda1`.

Validate the pinned machine-local aggregate attestation without printing its
counts:

```bash
uv run python -I -S scripts/verify_av1_cold_start_preregistration.py \
  validate-eligibility /path/to/eligibility-attestation-v1.json --json
```

Successful validation emits only `eligibility_valid=true` and false execution
authority flags. It does not emit the attestation ID, cutoff timestamp, or any
aggregate counts.

## Bounded v2 derivation workflow

Issue `#287` uses `av1vdw2` to turn the reviewed partition into one immutable,
owner-only derivation plan. Plan creation revalidates the exact partition
against the current read-only inventory before it embeds the separate
merged-shape `acsvda1` authorization, exactly twenty-four derivation
assignments, and one source commitment for each assignment. Every commitment
binds the assignment ID, local item ID, content-version identity, full source
SHA-256, source size, and frozen evidence-summary SHA-256:

```bash
uv run python -I -S scripts/verify_av1_cold_start_preregistration.py \
  create-derivation-plan \
  docs/validation/av1-cold-start-preregistration-v2.json \
  /private/owner-only/eligibility-attestation-v1.json \
  /private/owner-only/av1-v2/source-partition-v1.json \
  --key /private/owner-only/av1-v2/partition.key \
  --valid-until YYYY-MM-DDTHH:MM:SSZ --json
```

The plan authorizes only its twelve motion and twelve darkness reservations.
The command writes `plan.json` only beneath the partition-global canonical
private state root derived from `web_state_dir`; there is no caller-selected
plan path. The root is keyed by the frozen private partition and immutably bound
to the first authorization and plan, so a second authorization cannot rerun the
same reservations under another plan ID.
The canonical `plan.json` is published before its root binding. A retry loads
and validates that exact plan, requires the same authorization window, reuses
its original `authorized_at`, re-hashes all twenty-four derivation sources, and
requires the rebuilt commitments to reproduce the frozen plan exactly. When a
binding is absent, the full authorization, repository, source-quiet, and
cumulative-capacity gate runs after the binding temporary is fully written and
fsynced and immediately before its exclusive rename. The protected runner
preserves the process's incoming descriptor budget while adding capacity for
its retained Git-authority and twenty-four source/path-chain guards. Nested
monitors also count descriptors already held by runtime and source guards, then
retain a separate 128-descriptor operating reserve. The runner raises the soft
limit only when the host hard limit permits it and restores only the limit it
owns. Insufficient descriptor capacity fails closed before publication. The
renamed binding's
kernel change time must also remain strictly before the plan authorization
deadline, and the live source/repository identity is rechecked after the rename;
a boundary-crossing or drifted binding is rolled back. An existing exact binding
is validated for the same deadline, re-fsynced, and passed through the same
idempotent live source/repository identity verifier without rerunning the
pre-publication authorization callback or creating a new authorization. Exact
attempt and accepted-marker recovery follows the same rule: the full durable
composite is validated first, then the read-only post-publication identity check
is replayed once. Retrying plan
creation therefore requires
the selected media to remain present and byte-identical. The runtime clock is
sampled only for the first successful plan payload. A binding
without its plan is an explicit interrupted-state failure, not permission to
mint another authorization. The plan binds a
privacy-safe digest of the resolved database, review, and web-state locations;
every later command must use that same machine-local runtime context. Every
new v2 plan also embeds the exact repository `HEAD` commit and `HEAD^{tree}` in
its semantic payload, plan ID, and payload digest. The repo-local verifier reads
that identity with Git, rejects uncommitted tracked changes, and supplies the
values to the deterministic plan builder. Existing v2 payloads that omit either
identity fail closed rather than being compatibility-filled. Plan retries,
review claims, review envelopes, review-set verification, candidate-lock
finalization, and verified-lock loading must all resolve to the plan's exact
commit/tree pair; advancing the checkout requires a new derivation plan and
review set rather than reusing approvals from the earlier snapshot. Assignment
execution keeps Git access at the repo-local verifier boundary and injects a
live identity resolver into the runtime. Each resolution uses an independent
bounded Git probe, so cancellation or deadline state on the media controller
cannot suppress publication of an immutable stopped or expired outcome. The
Git environment is explicit and non-inherited: global and system config are
disabled, replacement refs are disabled, lazy fetch is disabled with
`GIT_NO_LAZY_FETCH=1`, and optional index locks are disabled so every read probe
is non-writing and cannot invoke a remote helper. The verifier validates the
checkout's main
or linked-worktree metadata, pins `GIT_DIR`, `GIT_COMMON_DIR`, and
`GIT_WORK_TREE` to that one authority tuple, disables system and configured
attribute and exclude files, and disables repository-configured fsmonitor and
hook paths for these probes. Local and per-worktree config remain readable only
when self-contained: any `include`/`includeIf`, promisor, or partial-clone
directive fails closed, as does a nonempty `objects/info/alternates` file.
Nested symlinks anywhere in the current
Git directory or authoritative shared metadata fail closed; the validated
top-level linked-worktree `.git` pointer remains the only indirection. Each
repository-identity or review-bundle transaction creates one
retained-descriptor authority monitor before its first Git child and keeps that
same monitor through every command and the final fail-closed drain. The monitor
recursively covers the checkout's current Git directory and the shared refs,
objects, config, logs, and packed metadata. Shared `worktrees/*` metadata is
excluded so an unrelated linked worktree can update its own `HEAD`, index, or
lock state without invalidating the current checkout; the current linked
worktree's Git directory remains separately and recursively monitored. An
in-place loose-ref, index, config, object, or packed-metadata rewrite is detected
even when the original bytes and pathname are restored. Repository-identity and
review-bundle transactions also derive their worktree watch set from Git's
binary tracked-file listing, retain the tracked files and their ancestor
directories, and deliberately do not recurse into ignored dependency trees.
macOS uses kqueue and raises the process soft file-descriptor limit only when the
complete watch set requires it; monitor close restores the previous soft limit
only when it still equals the value set by that monitor, so a concurrent
external limit change is never overwritten. Linux uses inotify. Other platforms
fail closed. Any write, rename, replacement, relink, attribute change, or
authoritative namespace mutation is a permanent authority failure. The same
frozen authority tuple is revalidated immediately before and after every Git
child and monitor drain.

Clean-state proof does not invoke `diff`, `status`, text conversion, clean
filters, or process filters. Binary `ls-files --stage -z` and `ls-tree -r -z`
snapshots must contain the same stage-zero mode/path/blob tuples, while binary
`ls-files -v -z` must report ordinary `H` state for every path. The verifier then
opens each regular worktree file through retained descriptor-relative,
no-follow path components, checks its mode and stable descriptor/path identity,
and computes Git's canonical `blob <size>\0<bytes>` object ID. The bootstrap
rejects every symlinked component or tracked symlink entry beneath
`mediaforce/`, `scripts/`, and `config/defaults.toml` before monitoring or
direct proof, so an intermediate directory cannot route sources, resources, or
helpers outside the watched authority. The
object-ID width selects repository SHA-1 or SHA-256. Dirty bytes therefore
cannot be normalized back to the index by a configured filter, and a malicious
filter command is never executed. NUL-delimited path output remains binary until
filesystem decoding with `surrogateescape`, so arbitrary Linux Git path bytes
either round-trip into the direct proof or produce a controlled derivation error
instead of a `UnicodeDecodeError`. Untracked state is checked with binary
built-in `ls-files` queries; Git aliases, remotes, and hooks are not invoked by
these fixed read commands. `assume-unchanged`, `skip-worktree`, unmerged,
gitlink, and other exceptional index states fail closed instead of allowing live
implementation bytes to diverge from the reviewed commit. The canonical runner
must be launched with
CPython isolated/no-site startup before importing `argparse`, dependencies, or
`mediaforce`; non-`-I -S` startup fails closed. A stdlib-only bootstrap invokes
the root-owned direct system Git binary
(`/Library/Developer/CommandLineTools/usr/bin/git` on macOS and `/usr/bin/git`
on Linux) rather than the macOS Xcode-selection shim, through a
sanitized environment with validated `GIT_DIR`, `GIT_COMMON_DIR`, and
`GIT_WORK_TREE` values pinned to the canonical checkout, so repo-local
`core.worktree` or a foreign `.git` pointer cannot redirect the proof. The
stdlib-only bootstrap establishes one recursive metadata and import-tree monitor
before its first fixed Git probe. That monitor remains active across every
bootstrap probe, all repository-local imports, and the complete command
execution, then performs a final fail-closed drain before process exit. After
the raw exact-object proof, the bootstrap retains all tracked `mediaforce`
bytes, including package resources, Alembic environment and version scripts,
SQL, and `_process_deadline.py`, plus `config/defaults.toml` and the
source-default resolver markers, canonical preregistration runner, and
`uv.lock`. It materializes those exact bytes into an
owner-only private snapshot outside the repository, monitors that snapshot
through command completion, and removes it only after the monitor closes. The
in-memory finder and loader compile from those bound bytes; module `__file__`,
package `__path__`, package resource readers, Alembic script locations, SQL
resources, and `DEFAULT_CONFIG_PATH` all resolve inside the private snapshot
rather than the mutable checkout. Unknown later-created `mediaforce` modules
cannot fall through to the worktree. A swap-and-restore during import can only
execute the previously bound authoritative source, while the retained monitor
makes the command fail closed. Bootstrap-authority failure is sticky and is
reasserted before runtime locks, artifact publication callbacks, review launch,
and immediately before every direct successful validation or report output, so
catching one authority exception cannot permit a later side effect. Transient
directory swaps, in-place Git metadata rewrites, and package replace-and-restore
attempts therefore remain observable after the last bootstrap probe. It refuses
modified, exceptional-index, untracked, or ignored state anywhere under
`mediaforce/` or `scripts/`; it also rejects repository bytecode caches and
disables bytecode writes. The bootstrap removes the repository and script
directories from normal module search, adds only trusted interpreter paths plus
the canonical checkout's `.venv` site-packages, requires the running interpreter
to use an approved `.venv/bin/python*` launcher whose opened binary is the same
inode as canonical `.venv/bin/python` and the interpreter's base executable.
That executable must be owned by the invoking account or root, must not be
group/world-writable, and must not carry set-ID bits. The bootstrap requires any
`VIRTUAL_ENV` declaration to match that environment and explicitly binds the
canonical `mediaforce` source snapshot. Under the owner-local threat
model, that canonical `.venv`/site-packages installation and the external Every
Code binary remain trusted; the verifier does not broaden its scope by hashing
dependencies or copying the Code runner. An ignored `scripts/argparse.py`,
Python source, bytecode, native extension, or package substitution therefore
cannot execute before the exact-object authority proof. Operators must remove
those artifacts before invoking this security-sensitive runner.
The runtime rechecks the plan's exact
commit/tree pair before claim publication, immediately before and after media
execution, and inside every attempt or terminal publication callback. A checkout
that drifts mid-run therefore stops closed and cannot publish favorable evidence
for the earlier snapshot. Every
execution, proposal, and finalization also re-compares the plan's complete
twenty-four-assignment payload with the frozen partition rather than trusting
top-level digests alone, and every write-capable source session revalidates the
plan's complete twenty-four-source commitment set before publication.
Each assignment has one attempt. Execution cannot select a replacement, retry a
source, use a holdout, invoke the validation harness, inject a cold-start or
guided probe, consume local personalization, backfill a historical row, or
activate the public bundle. A claim file is created before media work begins so
an interrupted process cannot silently rerun the same reservation. Attempt,
terminal-intent, terminal-record, proposal, review-claim, review, and
candidate-lock artifacts live under the partition-global canonical private
state root derived from `web_state_dir`; callers cannot select alternate
artifact directories. Each
directory carries an immutable binding to one plan or proposal, so artifacts
cannot be mixed across authorization windows or review sets. The canonical
artifact-root binding also carries a one-way digest of its resolved state-root
path. Moving the private tree to a location that selects a different
runtime-lock domain therefore fails before recovery can write. Recovery derives
the lock domain from the canonical artifact tree and requires it to match the
lock path selected from current config, so aliasing only the artifact subtree
beneath a state root in another runtime-lock domain fails at the same boundary.
Candidate-lock finalization and visual-verdict publication repeat the same check
after taking the runtime lock, canonicalize all subsequent artifact access to
the bound tree, and fail before any artifact or database write when the domains
differ. Aliases that resolve to the same canonical tree and select the same lock
file remain in the same lock domain. Artifact roots created before the
path-bound digest existed are not migrated in place. Likewise, any private
derivation tree created by the pre-remediation branch must be discarded because
its `plan.json` embeds incompatible in-place extensions of `av1vsp1` and
`acsvda1`. Regenerate those derivation artifacts from the merged implementation;
the existing schema-1 partition, token key, selection-lock digest, and
derivation-partition digest remain authoritative and must not be regenerated.
Newly created
claim files and newly created parent directories are fsynced before work can
continue, so a power loss cannot silently erase assignment or review ownership.
Every private JSON artifact is completed under a random dot-prefixed
descriptor-relative temporary name with
`O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW` and
pathname mode `0400`, fsynced, then atomically moved to its final name with the
platform's no-replace rename primitive (`renameatx_np(RENAME_EXCL)` on macOS).
macOS file payloads also receive `F_FULLFSYNC`, and the parent directory is
fsynced after publication. Only an exclusive-name collision is treated as an
idempotent concurrent publish; a write, full-sync, verification, close, or
directory-sync failure propagates even when the final name is visible and must
be re-entered through the runtime-lock-held retry or recovery path before
progress. A surviving orphaned claim is terminalized by that recovery rather
than resuming the claimed action. A retry that finds an identical published
artifact re-fsyncs its parent directory before accepting it, so a prior
directory-sync error cannot be downgraded to an unsynced idempotent success.
Authoritative attempt loaders also re-fsync the retained attempt parent before a
visible attempt can authorize verdict, proposal, or finalization work; status
reporting remains read-only. A favorable attempt is not authoritative merely
because its JSON name is visible: after the attempt's post-rename source and
repository checks and parent sync complete, a separate immutable accepted
publication marker binds the plan, authorization, assignment, attempt ID, and
attempt digest. Verdict publication explicitly requires that timely marker.
An interruption before acceptance cannot be blessed on retry; recovery writes
an immutable rejected marker and terminalizes the unchanged one-shot attempt as
`safety_stop`, or as `authorization_expired` when the attempt or acceptance
marker crossed the authorization deadline. A rejected marker plus its terminal
record remains loadable for deterministic progress accounting, while a timely
accepted marker plus any rejection is corruption. An owner-only empty or
binding-temporary-only attempt-publication directory left by a crash is treated
as unsealed and rebound during rejection recovery; any marker or unrelated file
without a binding is corruption. A second recovery recognizes the exact
rejected terminal-intent/record pair as complete and makes no writes. If an
observed terminal intent was renamed after authorization expiry but its record
was never published, recovery rolls back that incomplete intent and replaces it
with an `authorization_expired` terminal pair. If interruption occurs after the
rollback unlink, the still-frozen verdict intent is terminalized as
`authorization_expired` on the next recovery once the window is expired. Before
rollback, the late intent must match the exact plan, authorization, attempt,
payload digest, cell, ordinal, and attempt chronology and must remain backed by
its immutable verdict claim and intent. A late observed record or a
conflicting pair still fails closed. A
fresh plan, favorable `review_pending` attempt, or candidate proposal rechecks
the live authorization immediately before its exclusive rename, then verifies
that the renamed inode's kernel change time is strictly earlier than the
immutable authorization expiration. Plan and favorable-attempt loaders repeat
that same-inode publication-time check; bulk recovery loaders also repeat it for
already matching observed terminal intents and records. A process paused across
expiration therefore cannot make a late favorable artifact acceptable by
retaining an earlier payload timestamp. Unfavorable recovery attempts remain
publishable after expiration because they cannot authorize another assignment.
A
candidate-proposal retry first loads the canonical existing proposal and reuses
its original `proposed_at`; it never samples a replacement timestamp. A review
retry accepts only a complete matching immutable lane claim and envelope,
reuses the envelope's original `reviewed_at`, re-fsyncs the review parent, and
returns without launching another agent. If only the claim is complete, retry
uses its exact durable response checkpoint or publishes a rejected
`interrupted_before_durable_response` recovery; neither path probes the live
runner or launches another agent. Claim-only recovery remains deadline-bound;
a predeadline checkpoint may be sealed later only through its retained physical
artifact and exact digest, request, response, decision, and completion-time
bindings. Version-2 `invalid_durable_response` evidence explicitly records that
its review time is checkpoint-completion-bound. Legacy version-1 envelopes with
a retry timestamp at or after checkpoint completion remain readable only when
the envelope itself was
durably published before `valid_until`; they do not gain the checkpoint-based
late-sealing exception. Any conflicting final artifact is rejected. A
normal pre-publication failure durably removes its temporary and surfaces any
unlink, close, or cleanup-sync failure. A hard interruption before the rename
leaves only an ignored temporary; after the rename, the canonical name contains
a complete payload, but progress still waits for the parent directory sync.
The writer retains the opened publication-directory descriptor and compares it
with the canonical path immediately before the no-replace rename and again
after publication. If the directory was renamed away or replaced, the new file
is removed through the retained descriptor and the publication fails. Review
claim/envelope directories remain retained across one review publication, while
verdict claim/intent and terminal directories are retained from their first
authoritative write through the SQLite commit boundary. Candidate-lock
directories are retained through finalization. The final `open_db()`
pre-commit callback rechecks every active binding while rollback is still
possible; a swap rejects the operation and rolls back any observation or other
database state rather than accepting an artifact written into an orphaned
directory. The same descriptor/path check covers plans, attempts, proposals,
claims, reviews, terminal records, candidate locks, and other owner-only
authoritative JSON artifacts even when no database transaction is involved.
Reads require one owner-owned link, exact `0400` mode, stable
descriptor/path identity, and unchanged size, timestamps, and inode before and
after the complete read.
These filesystem controls assume the operator account and Mediaforce process
are trusted. Internal Python builders and writers are implementation details,
not authorization APIs; code already executing as the artifact owner can replace
owner-only state and is outside this protocol's threat model. The supported
write boundary is the locked CLI/runtime workflow described here.
On the next runtime-lock-held invocation, an orphaned assignment claim is
terminalized as an unfavorable `interrupted_claim` without rerunning media, and
a persisted non-review attempt missing its terminal record is idempotently
terminalized before any later reservation can run. Recovery first confirms the
same canonical state root and its frozen plan/partition bindings, then runs
before full runtime-context, execution-environment, statistics-contract, live
inventory, or source compatibility checks. Later database, review-directory,
toolchain, policy-code, or source drift therefore cannot strand an existing
claim. Because the state root also owns the global runtime lock, a moved
`web_state_dir` must be restored before recovery rather than silently using a
different lock domain.

Run one fresh assignment at a time. The command revalidates the partition and
current inventory before touching media, uses the exact assigned library item, forces
the existing sampled calibration path to leave the cold-start planner absent,
and preserves an owner-only attempt artifact plus review media. Derivation
review clips are redirected beneath the partition-global private artifact root;
the calibration subprocess tree inherits an owner-only `0077` umask, directories
are `0700`, and generated files begin owner-only. Before the first provenance
fingerprint, the derivation callback opens and pins every preview, source, and
compare clip through the no-follow review-root descriptor. While those
descriptors remain open, post-run validation walks the complete tree, rejects
links, hard links, permission drift, ownership drift, and identity substitution,
and seals every regular file to `0400` through its held descriptor. It performs
no pathname chmod. One macOS path-chain guard per reviewed clip is then kept
active until every clip payload is hashed and every guard passes a final quiet
check. The `cira3` review fingerprint binds all three clip roles, content, a
one-way canonical-path digest, device, inode, modification/change times, mode,
and link count. Existing non-derivation `cira1` and `cira2` records retain their
original preview/source-only recomputation contracts; new compare-aware records
and every derivation attempt use `cira3`. Persisted calibration requires
nonempty preview, source, and compare arrays with valid absolute `file:` URIs,
finite positive durations, unique nonnegative millisecond-normalized moments,
and the same moment set in all three roles before review media can be marked
ready or a `cira3` fingerprint can be accepted. Verdict-time identity checks use
the same all-clips-held procedure, closing the earlier-clip mutation window.
Descriptor/resource or integrity failure is an affected-cell `safety_stop`, not
`media_unavailable` or measured evidence.

Private partition creation first performs logical selection without hashing the
broader eligible library. The merged schema-1 `av1vsp1` payload intentionally
contains no full source SHA-256 or source-size field: its partition ID,
selection-lock digest, derivation-partition digest, and payload digest remain
byte-for-byte compatible with the partition published for issue `#286`.
Selection-time integrity remains bound by each assignment's immutable
evidence-summary SHA-256, the sampled content-version identity, and the complete
inventory snapshot digest. A newly built partition may replay selected
derivation bytes and fingerprint evidence before durable publication, but those
transient full hashes do not alter schema-1 payload bytes or digests.

Plan creation is the first durable full-byte freeze for the twenty-four
derivation assignments. The private `av1vdw2` plan commits each source's full
SHA-256 and size together with its assignment, local item, source identity, and
evidence-summary identity. These commitments contribute only to the plan ID and
plan payload digest; they do not alter `av1vsp1`, the selection lock, the
derivation-partition digest, or `acsvda1`. This timing is explicit: the plan
cannot retroactively prove the exact full bytes present when the schema-1
partition was selected. Full-byte commitments for the holdout cohort remain
out of scope until issue `#288`.

Candidate proposal construction, candidate-lock finalization and verification,
and visual-verdict publication each re-open and fully hash all twenty-four
derivation sources against the plan commitments. They never substitute the
frozen digest for a current byte-level check. Proposal construction is
non-authoritative and reads current observations and inventory through separate
read-only snapshots under the same runtime lease. Finalization, verification,
and verdict publication keep their inventory, observations, and source checks
inside one immediate database transaction. Publication payloads are first
written and fsynced under a hidden owner-only temporary name. Proposal and
candidate-lock publication keep the active source session through exclusive
atomic rename: the session performs another quiet and identity check immediately
before publication and performs its exit check before any database transaction
can commit. Exit validation also runs when publication raises, so post-rename
durability failures cannot skip the final source check. The human-verdict path
uses a different ordering because its database append must remain reversible on
a final source-session failure. After verdict intent, observation append, the
final contract check, and full source verification, the source session completes
its final quiet/exit boundary while the immediate transaction remains open. Only
after that boundary succeeds are terminal artifacts published. A retry accepts
only the exact canonical artifact already visible at the final path,
re-synchronizes its parent directory, and rejects any conflicting or malformed
artifact.

After the immutable assignment claim and before crop, search, encode, or review
work, the runtime opens the assigned source with no-follow semantics, verifies
its inode, size, sampled identity, and frozen full SHA-256, and copies the
complete file into one assignment-scoped, owner-only snapshot under the
canonical artifact root. The snapshot is created relative to a pinned directory
descriptor with `O_CREAT | O_EXCL | O_NOFOLLOW`; an existing assignment name
fails closed and is never overwritten. Its pathname mode is `0400` from the
instant of creation, while write access exists only through the original
`O_RDWR` descriptor, so hard interruption leaves read-only partial residue. The
runtime records the snapshot's full SHA-256 and size in the immutable
calibration payload, reopens it read-only, and routes every source read through
that canonical snapshot path rather than the mutable library path. A macOS
kqueue guard watches the snapshot vnode and
every canonical pathname ancestor throughout media work; write, extend,
attribute, hardlink, delete, rename, or revoke activity is latched even if bytes
or names are later restored. The snapshot must keep one link, the source and
snapshot identities are rechecked, and final guard validation runs even when
media work raises.
The assignment claim itself is authorization-deadline-bound by both a live
pre-rename check and the final inode's kernel change time. After an in-window
claim, authorization is checked again before database preparation, before the
source is opened or each snapshot chunk is copied, and before each calibration
media stage. Expiration at any boundary creates an immutable
`authorization_expired` attempt and terminal without starting the next media
step. An orphaned in-window claim still follows the existing interrupted-claim
recovery path and never reruns media.

Source snapshots are retained as private, write-once assignment artifacts after
success, failure, cancellation, or interruption and are never reused as runtime
inputs. The bounded derivation lane performs no automatic snapshot unlink,
rename, truncation, pathname chmod, directory removal, or recursive cleanup.
Interruption recovery terminalizes the owned assignment and preserves any full
or partial snapshot residue for explicit out-of-band retirement while
Mediaforce is stopped. Creating each snapshot requires free space for the
complete source plus the existing five-gibibyte safety floor. Before execution
authorization, plan publication creates and pins the canonical owner-only
snapshot directory and requires free bytes equal to the sum of all twenty-four
source-commitment sizes plus the existing five-gibibyte safety floor. The first
publication requires that directory to be empty. A plan retry credits only
complete assignment-named snapshots whose owner, mode, single-link identity,
size, sampled content identity, and full SHA-256 still match the already-bound
immutable plan. Its required free bytes are the unretained committed bytes plus
the safety floor, so retained snapshots do not require impossible duplicate
capacity. Unexpected entries, partial residue, retained snapshots before a root
binding exists, a binding without its plan, a drifted plan/root binding, and
directory or snapshot identity drift fail closed. A retry may recover the
documented plan-without-binding interruption only while the pinned snapshot
directory remains present and empty. The runtime also keeps the existing per-
assignment free-capacity recheck through the pinned snapshot-directory
descriptor immediately before each new snapshot.

The repository keeps the full protocol suite active on Linux with test-only
seams for macOS capability admission, the same-filesystem mutation probe, and
kqueue event delivery. Descriptor/path identity checks and all protocol logic
remain active in those fixtures. A focused `file-integrity-macos` CI lane
separately runs the real kqueue capability, probe, transient-write, hardlink,
path-chain, partition, and derivation tests. Production capability admission
and runtime monitoring remain fail closed and are never bypassed by these test
seams.

The `av1vdw2` plan binds the resolved database, review, and state roots together
with the current machine, Python executable, every regular non-cache file under
the Mediaforce package tree, the repository-owned preregistration runner,
`pyproject.toml`, `uv.lock`, the shared CLI/runtime-lock implementation, AV1
encoder/metric toolchain, source-integrity guard contract, and merged
statistical contract. Adding, removing, or changing any bound implementation
file changes the plan's execution-environment digest. The plan also binds
SHA-256 digests of the canonical Every Code executable path and binary without
persisting or printing the private path. The embedded merged-shape `acsvda1`
authorization continues to bind only the manifest, selection lock, derivation
partition, authorization window, and derivation-only authority. Because that
authorization payload is itself embedded in the plan, the plan digest binds the
authorization, runtime context, execution environment, statistics contract,
review runner, and source commitments as one immutable unit. Proposal and
review publication require the executing verifier to be the canonical
repository file and re-check the plan-bound execution environment immediately
before each immutable write. Proposal publication also resolves the live Git
commit and tree before evaluation, inside the final publication callback, and
after write recovery; every result must equal the plan snapshot. A
real same-filesystem kqueue mutation probe must pass before the immutable
assignment claim is written. The probe creates a private owner-only file,
sets its pathname mode to `0400` from creation, mutates only its original
`O_EXCL | O_RDWR` descriptor, and retains the tiny probe directory instead of
recursively deleting a mutable pathname. Independent review execution instead
uses one private copied Every Code runner and one owner-only committed
repository snapshot per lane. Both must pass unchanged post-run identity checks
before their local cleanup can complete. Assignment execution holds the same
exclusive runtime lock as
`mediaforce-web`. Every write-capable `mediaforce` CLI command and each direct
partition-key, partition-build, and derivation artifact-publication action
acquires that lock too, so web, staging, database, cleanup, proposal, and
review-publication work cannot overlap the bounded derivation case. Config
loading itself is read-only; legacy state-path migration runs only after the
exclusive lock is held. Machine or toolchain drift before the claim or after
measurement is a safety stop rather than a newly compatible cohort.
The shared lock uses a stable parent-directory guard in addition to the metadata
file. It also holds deterministic persistent reservations under the configured
`state.runtime_reservation_dir`, with path-local anchors for the writable
database and metadata-lock namespaces. Config directories remain read-only.
Normalized namespace keys are represented only by SHA-256 directory names;
reservation directories are owner-only, their lock files are empty and
owner-only, and runtime shutdown never unlinks either one.
Each reservation locks both the key directory and its lock file, so removing and
recreating the lock-file pathname cannot split an active reservation. Existing
config inodes are also locked directly. Existing databases additionally receive
a nonblocking open-file-description byte-range lock on one Mediaforce-owned byte
that is disjoint from SQLite's lock bytes. That inode-bound reservation works
across hardlink aliases and configured reservation roots without interfering
with SQLite WAL locking. A missing database is materialized and receives the
same reservation after legacy state-path migration and before any other database
work, while the global runtime lock remains held. The stable configured
reservation root covers lock-parent replacement,
while descriptor-relative `O_NOFOLLOW` opens and device/inode rechecks protect
every reservation, direct config lock, and metadata-lock handoff. Unlinking the
metadata lock, changing configured state paths, or renaming and replacing its
parent cannot create a second lock domain for another compliant runtime. The
lock path resolves `web_state_dir` symlinks before choosing that domain, so
aliases cannot create a second one. Guarded SQLite engines verify the reserved
database identity before and after connection creation, every cursor operation,
writable transaction commit, and the raw legacy-schema bootstrap. Connection
creation pins the resolved parent directory and expected database inode, then
opens SQLite through the parent directory's stable kernel identity: `/.vol`
directory identity on macOS and a retained `/proc/self/fd` directory handle on
Linux. The original read-only or read-write URI query is preserved, so WAL and
read-only enforcement remain SQLite-native. The canonical parent device/inode
and leaf device, inode, change time, and link count are frozen before open. The
opened parent descriptor must match that expected parent, and both parent and
leaf descriptors stay retained for the SQLite connection lifetime. After DBAPI
connect, the canonical parent/leaf, descriptor-relative leaf, descriptor
identities, and platform-pinned parent/leaf path must all still match.
Unsupported pinned-path identity inspection fails closed. A database
swap-and-restore, substituted parent, hardlink-backed alternate WAL namespace,
transient ancestor substitution, or unrelated concurrent open therefore cannot
redirect or spoof the returned connection.
Persistent or transient path replacement during connect, query, migration, or
commit fails closed before later evidence publication can proceed without
weakening read-only URI or WAL behavior.
Fresh execution installs its absolute authorization deadline before toolchain
preflight. Every managed command then starts through Mediaforce's private
isolated deadline runner. The runner refuses to execute the target after the
deadline, arms an independently scheduled process-exit watchdog first, and the
watchdog immediately kills the complete target process group at expiration even
if the parent operator process is stopped. If a group leader exits while its
descendants remain, the watchdog keeps policing the process group until every
descendant exits or the deadline kills the group. A dedicated status pipe
reports explicit clean completion, deadline expiry, or enforcement failure.
When `mediaforce.core.process_control` is imported from the bound snapshot, it
captures `_process_deadline.py` once into an owner-only anonymous, unlinked
descriptor. Managed launches pass that descriptor explicitly and execute it with
`python -I -S` through `/dev/fd/<fd>` on macOS or `/proc/self/fd/<fd>` on Linux;
they never launch the mutable repository helper pathname. A later helper-path
swap therefore cannot change watchdog code, and no site initialization runs in
the helper interpreter.
The watchdog sends the deadline kill before reporting expiry; a kill error other
than an already-absent process group reports enforcement unavailable instead.
The parent consumes that pipe on a dedicated monitor while `communicate()` runs
in bounded polling slices. Empty or unexpected status, status-read interruption,
and watchdog unavailability are therefore observed while a target is still
running; the monitor starts parent-side group cleanup immediately rather than
waiting for target output EOF. Parent communication and reap attempts remain
bounded even when cleanup cannot kill the target. Non-`ESRCH` `SIGTERM` and
`SIGKILL` failures propagate explicitly and remain cleanup notes on the original
deadline or cancellation exception. A group is released only after cleanup can
prove success. If cleanup remains unproven after the immediate bounded attempts,
the controller is permanently poisoned but discards the bare numeric PGID so a
later cancellation cannot signal an unrelated process group after ID reuse.
Ordinary
cancellation, deadline-expired, and deadline-enforcement classifications remain
distinct. Toolchain and quality-metric capability probes use
that same controller and deadline; the assignment's already-frozen quality
metric and target are selected without launching a second unmanaged probe.
Fresh execution samples its authorization
timestamp only after all preflight checks and immediately before the immutable
claim.
`scripts/mediaforce-dev.sh` leaves that persistent lock path in place when
stopping the backend.

```bash
uv run python -I -S scripts/verify_av1_cold_start_preregistration.py \
  run-derivation-assignment \
  docs/validation/av1-cold-start-preregistration-v2.json \
  /private/owner-only/av1-v2/source-partition-v1.json \
  '<web_state_dir>/av1-validation-derivation/<partition_id>/plan.json' \
  av1vderive1_<opaque-slot> \
  --key /private/owner-only/av1-v2/partition.key \
  --config /private/owner-only/mediaforce.toml --json
```

The command's privacy-safe result includes the immutable attempt `reason_code`.
Known outcomes retain their existing categories, including `timeout`,
`storage_stop`, `metrics_incomplete`, and `safety_stop`. Unexpected future
failures use an allowlisted stage-specific `runtime_*_failure` code for
preflight, source snapshot, crop detection, toolchain validation, quality
search, sample encode, review generation, result validation, or cleanup. The
legacy `runtime_failure` value remains valid for existing evidence and is never
retrospectively reclassified. The allowlist is an additive reason vocabulary
inside the existing exact-key v2 envelope, not a new artifact shape; execution
plans remain bound to their exact repository identity, so binaries predating
this vocabulary are not rollback executors for future plans. Reason codes never
contain exception text, commands, paths, hostnames, or media metadata, and they
do not authorize a retry or successor cohort.

Remote process-probe failures during transient cleanup emit only a redacted
`Nonfatal cleanup reachability warning`; the affected remote staging root is
kept rather than pruned, and that warning is not the attempt's terminal cause.
A raised local restore, purge, or activity-guard cleanup exception remains a
terminal `runtime_cleanup_failure` when the main path otherwise succeeded; it
does not erase an already classified unfavorable terminal cause.

`derivation-status` is a read-only recovery diagnostic. It reports privacy-safe
counts for assignment claims, attempts, terminal intents and records, verdict
claims and intents, review claims, plus unresolved counts and
`recovery_required`. It also reports separate aggregate attempt and terminal
reason-code counts so one copied terminal cannot be mistaken for another
attempt. An orphan assignment claim, unaccepted attempt, terminal
intent without its record, verdict claim/intent without a terminal, or review
claim without its matching envelope sets `recovery_required=true` and returns
exit status `2`. Late observed terminal intents are counted separately and also
require recovery; the status command never performs recovery writes.

Successful technical attempts remain `review_pending` until a human records an
explicit visual verdict. The verdict path appends the existing current-contract
`ContentIntentBoundaryObservation`; it never infers a verdict. Derivation rows
are active evidence for the bound candidate snapshot but are explicitly marked
ineligible for local personalization, so later planning and holdout execution
cannot consume them as warm-start evidence. The complete derivation terminal is
preceded first by an immutable verdict claim. The claim exists before database
open, `BEGIN IMMEDIATE`, inventory reload, current-input validation, or review
media verification; a hard interruption before the later verdict intent is
recovered as the affected cell's immutable `safety_stop`, never as a retryable
human verdict. A first verdict whose claim publication reaches authorization
expiry publishes no claim and terminalizes the affected cell before database
open. A claim published in-window without a matching verdict intent remains an
interrupted safety stop, while an immutable in-window verdict intent remains
eligible for idempotent reuse without extending authorization. Every favorable
observation commit and each newly published `observed` terminal artifact samples
authorization again; an `observed` artifact's inode must predate `valid_until`,
including when an existing artifact is reused on retry. Failed, stopped, and
excluded terminal evidence remains publishable after expiry. After those checks,
an immutable verdict intent freezes the first human input and runtime timestamp.
The validated observation is appended inside
the immediate database transaction, then the execution contract is checked one
final time. The source cohort then completes full verification and a final quiet
preflight while that transaction remains open. Its guard stays live through
both immutable terminal publication callbacks and closes before the database
transaction can commit. A quiet or repository-authority failure before terminal
publication starts therefore rolls back the observation before publishing the
separate immutable stopped `safety_stop` terminal for the affected cell. Once
terminal publication has started, a terminal-write, guard-close, or later
authority failure is retryable: the frozen verdict intent and any already
published exact terminal artifact are reused, and the database projection
remains uncommitted. An append conflict or earlier contract drift uses the
rollback-and-stop path rather than producing a false observed terminal.
A database-open, transaction-begin, inventory-load, current-input, contract,
review-media, or other pre-publication failure uses the same conversion; when a
transaction exists, rollback completes before the stopped terminal is
published.
A terminal-intent or terminal-record I/O failure occurs after the final source
quiet boundary, rolls back the database append, and propagates as a retryable
publication failure rather than being converted into a safety stop. The retry
reuses the frozen verdict intent and accepts only identical terminal artifacts.
If the database commit fails after the immutable record exists, an interrupted
retry likewise reuses the frozen verdict timestamp and idempotently completes
the same observation without replacing or duplicating evidence. If interruption
occurs after the terminal intent but before its terminal record, recovery copies
that exact immutable intent into the canonical terminal-record directory before
considering any later assignment. An observed terminal whose database projection
is still absent continues to block progress until the frozen verdict retry
idempotently commits it. The recovered terminal record does not prevent that
same frozen verdict from running again; it prevents only later assignments from
advancing around the missing database projection. Review-media
identity is freshly recomputed
immediately before the verdict and must still match the frozen attempt. The
complete verdict claim, media recheck, immutable verdict intent, immediate
database transaction, observation append, and terminal-intent/terminal-record
path holds the shared runtime lock.
Inside that lock it reloads the canonical plan and attempt, revalidates the
current partition inputs and execution contract, and repeatedly resolves the
live repository commit/tree before the verdict claim, verdict intent, terminal
intent, terminal record, and database commit boundaries. It fails closed on
drift. An existing immutable in-window verdict intent intentionally skips only
fresh authorization sampling; repository, source, and publication-directory
authority remain mandatory for idempotent completion.

```bash
uv run python -I -S scripts/verify_av1_cold_start_preregistration.py \
  record-derivation-verdict \
  docs/validation/av1-cold-start-preregistration-v2.json \
  /private/owner-only/av1-v2/source-partition-v1.json \
  '<web_state_dir>/av1-validation-derivation/<partition_id>/plan.json' \
  av1vderive1_<opaque-slot> \
  --key /private/owner-only/av1-v2/partition.key \
  --verdict approved \
  --config /private/owner-only/mediaforce.toml --json
```

Rejected, excluded, failed, stopped, and missing outcomes remain visible and
cannot be replaced. The next reservation cannot run until every prior technical
attempt has its human terminal. In accordance with the preregistered
`safety_stop_scope=affected_cell`, a terminal unsuccessful attempt or
unfavorable reviewed record permanently stops only that candidate cell. The
same immutable stopped terminal is written when current-input drift,
execution-contract drift, review-media identity failure, descriptor/resource
failure, or observation conflict prevents a pending human verdict from being
published; those failures cannot be retried as a more favorable review.
The
global immutable assignment order remains authoritative: later assignments in
the stopped cell are never allowed, while the next assignment in another cell
may proceed only at its original canonical position. Because each candidate has
exactly twelve reservations and requires twelve eligible observations, the
affected candidate is an exact no-go without stopping collection for the other
candidate.

Candidate derivation uses every retained terminal record. The proposal and final
candidate lock retain opaque source, logical-title, series, and source-group
token sets plus the sorted source-group token for every derivation observation.
That multiplicity-preserving projection makes the six-group minimum and
one-third concentration maximum independently verifiable after serialization,
while later holdout evidence can recheck within-holdout title uniqueness and
every derivation/holdout overlap dimension. No raw title or source identity
enters those artifacts. For an eligible cell, the CRF bounds are the full
observed minimum and maximum with the median center;
no trimming is allowed. CRF MAD must be at most `2.0`, the full span must be at
most `6`, and the range must contain an executable integer CRF. The bitrate
applicability range is the minimum and maximum frozen assignment
`target_video_bitrate_bps`, not the measured visual boundary bitrate. Measured
boundary bitrates remain the dispersion and confidence input. The quality floor
remains the numeric value frozen in the partition. Confidence reuses the
existing bitrate relative-MAD categories and records
`round(1 - relative_mad, 3)` after the hard evidence and preregistered
independent-source minima; limited confidence or a score below `0.7` is a no-go.
Proposal and lock types independently enforce exactly one series token per
observation, the preregistered source minimum, a CRF span no wider than `6`, and
freshness at proposal and lock time. The proposal records the measured CRF MAD,
bitrate relative MAD, derived conflict count, and pre-run statistical-contract
digest. A
proposal exists only when the conflict count is zero, so reviewers can verify
the exact applied dispersion and confidence contract from immutable evidence.

The full-search proof validates the native target-size trace contract, including
the first `target_seed`, any later `compression_floor`, `expanded_bound`, or
`refine` measurements, the native curve/retry policy, and exact selected CRF,
quality, and bitrate projection values. The persisted sample stream-budget
ledger must also retain the exact frozen assignment
`target_video_bitrate_bps`; a changed or missing remaining-video-bitrate value
invalidates the attempt. A one-point `target_seed` trace remains valid when the
unchanged search naturally selects its first measured candidate; the workflow
does not force exploratory probes that production search would not perform.

The resulting proposal is explicitly non-authoritative. Finalization requires
five proposal-bound approvals from distinct completed no-tool Every Code
structured requests: architecture, statistical/model-contract,
privacy/security, experimental-design, and adversarial.
`record-derivation-review` invokes `code llm request`, never `code exec`, and
accepts no caller-supplied result path. Before launch it atomically creates one
immutable proposal/lane claim that binds the run nonce, authorization, proposal,
lane, canonical runner-path digest, runner-binary digest, repository commit, and
repository tree. A concurrent or repeated lane cannot replace that claim.
A successful command response is checkpointed immutably before it is parsed or
used to construct the lane envelope. The checkpoint binds the original claim,
request digest, decoded stdout response, stderr digest, completion time,
repository identity, and runner identity. A crash before a complete lane
envelope exists
leaves recovery state that `derivation-status` reports. If the checkpoint is
present, retry parses that exact response and reuses its original completion
time without launching Code again. The recovered evidence binds the exact
checkpoint digest, request digest, response, stderr digest, and completion time.
If the checkpoint was durably published before `valid_until`, that deterministic
checkpoint-to-envelope sealing may finish after the deadline; every later
envelope load requires the same retained checkpoint and revalidates those
bindings. If only the claim is present, retry terminalizes the lane as a rejected
`interrupted_before_durable_response` recovery without launching Code. An
invalid checkpointed response is likewise terminalized as a rejected
`invalid_durable_response` recovery bound to the checkpoint digest and original
completion time, with version-2 evidence carrying an explicit
`checkpoint_completion_bound` marker. A previously sealed version-1
invalid-response recovery that used its retry timestamp remains compatible only
under the original strict envelope publication deadline, including when
second-level timestamp resolution made the retry and completion times equal.
Therefore a completed or interrupted request can never be rerolled under the
same claim.
Recovery validates the runner digests already sealed by the plan, claim, and
checkpoint; it does not require or probe the live runner executable.
The first valid envelope remains immutable. A retry after the complete matching
claim and envelope are visible validates both, re-fsyncs the envelope parent,
returns the original decision and `reviewed_at`, and does not launch a second
request.
A claim-only review recovery envelope remains subject to the same strict
publication deadline as a normal review. If authorization expires before a
response checkpoint becomes durable, the unresolved claim remains a visible
fail-closed status and cannot be converted into an approval under that plan.
A rejected review remains terminal. The review command and its
repository/toolchain probes remain under the plan's absolute authorization
deadline. Before claim publication, the verifier checks that the live Git commit
and tree equal the immutable plan snapshot, rejects uncommitted tracked changes,
and repeats that identity check after the request. It never launches Code in a
live source worktree. It builds a deterministic lane-specific bundle directly
from the claimed Git commit instead: raw `cat-file` commit data supplies the
tree, `ls-tree -z` resolves each allowlisted entry from that exact tree, and raw
`cat-file` size/blob reads supply its bytes. Replacement refs remain disabled
for all of those probes, and no diff, textconv, attribute, or worktree-filter
path participates in bundle construction. Every entry must be an exact
allowlisted regular tracked blob with its Git blob ID, path, byte size, SHA-256
digest, and UTF-8 text. Per-blob and total-bundle bounds apply. Code receives no
repository directory, so an untracked live-worktree file cannot affect the
review. The
lane allowlists include the relevant implementation, runtime, verifier, and
protocol files while remaining bounded to 384 KiB per blob and 768 KiB per
request bundle. The
canonical request binds the proposal, immutable claim, and exact safe bundle to
that same commit and tree, so later validation cannot reinterpret an approval
against another repository snapshot.
All five immutable claims must name exactly that same repository commit and
tree; both review-set validation and candidate finalization reject a divergent
lane, and the review-set digest includes the plan ID, plan digest, unanimous
commit, and unanimous tree explicitly. The exact PATH-selected runner must
match the authorization
before launch and again after completion. The already-verified authorized bytes
must be a native Mach-O executable, so a shebang wrapper cannot delegate to an
unbound interpreter. Its canonical path must also match the active ancestor Code
process that is conducting the operator session, so a different PATH-selected
native binary cannot establish its own trust root. The verifier invokes that
authorized runner directly as `code llm request` from an owner-only temporary
request directory. The request file is owner-read-only after a durable write,
is verified before launch, and is unlinked with its empty directory immediately
after use. No ephemeral shell home, repository checkout, agent shell, or tool
permission is created for the model. The runner receives a minimal allowlisted
environment and the real account home only so the trusted Code process can use
its existing authentication; endpoint overrides, caller secrets, and unrelated
environment variables are not inherited. The request supplies deterministic developer
text plus a strict dynamic JSON Schema whose constants bind the lane, proposal,
claim, run, repository commit, and tree. Before publishing the immutable claim,
the verifier checks the installed `code llm request --help` contract for the
required no-tool and JSON format-type/schema options. The model must return one
structured JSON response;
the verifier rejects duplicate keys or extra output and canonicalizes the parsed
object for evidence. Its decision must agree with its finding severities:
approval has no blocking finding, while rejection has at least one. Each
attestation binds the claim plus the SHA-256 digest of canonical
immutable owner-only completion evidence. That evidence contains the exact safe
bundle; proposal, claim, request, developer-text, and response-schema bindings;
the exact parsed model response; and the necessary SHA-256 commitments. It stores
no shell/tool transcript, raw command output, or parent stderr text; it retains
only the zero return code and a stderr SHA-256 digest. The loader
recomputes the canonical evidence digest before trusting it. The attestation and
completion evidence are written together as one immutable atomic lane envelope.
Duplicate run IDs, evidence digests, and normalized analysis digests are rejected.
All five immutable claims must name the same commit and tree; every response must
be substantive and the five lane analyses must be distinct. The decision is
extracted rather than supplied by the operator. Public summaries expose only
that the runner identity is bound, never the canonical private path.
Review, verdict, proposal, lock, and assignment-claim payload timestamps come
from the runtime clock only for their first immutable publication; recovery
reuses persisted timestamps. Fresh assignment-claim, proposal, candidate-lock,
verdict-claim, and verdict-intent publication samples the live clock immediately
before the exclusive rename and fails closed if authorization expired during
preparation. Review claims and envelopes are produced by the bounded reviewer
path and rely on the stronger final-inode publication receipt. After every
deadline-bound rename, the writer verifies the inode's kernel change time
against the same expiration; every loader repeats that check. Recovery of an
already-published authoritative artifact skips live-clock sampling but still
rejects a kernel-observable post-expiration publication. Exact plan, binding,
attempt, and accepted-marker recovery also reruns its idempotent read-only
source/repository post-publication verifier after canonical-byte and durability
checks; it never reissues the pre-publication callback. Assignment claims are
the narrow recovery exception: a claim whose rename landed after expiration is
loaded only as terminal evidence, marked late in memory, and recovered as
`failed/authorization_expired`. It can never authorize work or permanently
poison recovery. A deadline expiry raised by the final execution-contract
recheck keeps that same `failed/authorization_expired` classification; deadline
enforcement failure remains a wrapped contract safety stop rather than being
misreported as ordinary expiry.
Finalization requires exactly five resolved claims and five matching approvals
over the plan's unanimous repository commit/tree identity; any unresolved,
divergent, or rejected claim blocks it permanently. The repo-local command
injects the same live Git resolver used by assignment execution. The
runtime-owned finalization API resolves and enforces the plan-pinned `HEAD` and
tree before locked evaluation, after source verification, inside the final
candidate-lock publication callback, and after write recovery. A checkout
advanced from snapshot A to snapshot B therefore cannot finalize snapshot A's
reviews, even if every stored claim and envelope is internally self-consistent.
Finalization loads configuration once, opens an immediate database write
transaction, revalidates the frozen partition against inventory in that same
transaction, resolves every frozen source commitment, rereads current
observations, and fully verifies the source cohort. A new `locked_at` is sampled
exactly once only after those current inputs and evidence are loaded and
verified; freshness and authorization are then evaluated at that timestamp
before publication. The live authorization is checked again immediately before
the first exclusive candidate-lock rename. Recovery of an existing
candidate-lock envelope instead
reuses its persisted `locked_at` and never calls the runtime clock. The lock is
compatible with the canonical V1 chronology
`locked_at <= reviewed_at <= authorized_at`; because these artifacts use whole
UTC seconds, a review and its later authorization may legitimately share the
same serialized second. The lock is computed and written through one
runtime-owned finalization API. That API
derives the canonical root from config and the immutable plan path, loads the
attempts, terminals,
proposal, review claims, and review envelopes itself, and exposes no
write-capable caller path that accepts a precomputed evaluation. A finalized
candidate lock still does not authorize holdout execution or mutate the shipped
public bundle.

The candidate-lock file is a provenance envelope, not a standalone lock. It
commits to the plan and derivation authorization, proposal, five immutable
review claims, their unanimous repository commit/tree identity, five atomic
review envelopes, canonical artifact-root binding,
candidate lock, and terminal snapshot. #288 must load it through the verified
derivation-chain loader and must reject a raw or synthetic candidate lock. That
loader also derives the
root from config, reloads the full chain, and rechecks current database evidence
inside an immediate transaction. These owner-local hashes detect
stale, copied, mixed, and partially replaced artifacts; they do not claim to
authenticate against a malicious machine owner who changes code and rebuilds a
fully self-consistent private evidence tree. The coordination and runner checks
apply to documented Mediaforce CLI and web entrypoints. Importing private Python
helpers, replacing the interpreter or verifier, or modifying source while
deliberately bypassing those entrypoints is outside the owner-local threat
model; such activity invalidates the review and requires discarding the private
evidence tree and restarting from the merged protocol.

## Trait reachability preflight

Before a future preregistration is treated as executable, run the read-only
trait feasibility report against the proposed manifest and an explicit UTC
cutoff:

```bash
uv run mediaforce av1-trait-feasibility \
  docs/validation/av1-cold-start-preregistration-v1.json \
  --as-of 2026-07-27T18:00:00Z --json
```

The `acstp1` projection reclassifies compatible retained fingerprint analysis
without reading media. The report emits only aggregate item, parent-scope,
source, series, source-group, coherent compatibility/policy cohort, and bitrate
range counts. Paths, titles, source IDs, fingerprints, review-media identities,
compatibility tokens, and policy tokens are never emitted. The report is
deterministic for the same database snapshot, manifest, and `--as-of` value.

`ready_for_private_mapping` means only that current code can produce the
requested trait/intent path and aggregate inventory and derivation upper bounds
meet the preregistered minima. Parent-scope diversity is a conservative public
preflight; the private mapping must still prove unique titles/series, source
group concentration, derivation/holdout disjointness, and every other selection
constraint. Every feasibility report carries `execution_authorized=false` and
`runtime_integration_required=true`; it cannot start Mediaforce, alter the v1
request path, or authorize an encode. The v2 successor binds this prospective
projection without changing production routing.

The current analyzer deliberately reports low-motion dialogue as unreachable,
and the current request contract reports unknown fallback cases as unreachable.
Low-confidence animation or grain/noise advisories cannot silently create those
cell identities. The v1 preregistration remains immutable; these findings guide
the versioned successor protocol rather than relabeling v1 evidence.

## Isolated validation harness

`mediaforce/tuning/av1_validation_harness.py` defines the `av1vh1`
validation-only context and trace contract. It consumes an already reviewed
candidate lock, its bound execution authorization, and a public-safe machine
binding. It does not call the normal cold-start predictor, load private replay
evidence, mutate the packaged bundle, register a web/runtime route, or discover
media.

The harness deliberately reuses the existing target-size search rather than
simulating its control flow. A guided dry run creates one
`QualitySearchWarmStart` with source `av1_validation_harness`. The existing
search records the locked first probe and, when that probe is rejected or
fails, continues through the unchanged measured full-search path. The harness
then validates and sanitizes that target-size trace into ordered work:

- exactly one `locked_first_probe` for a recommended guided case
- zero locked probes for baseline or no-recommendation cases
- one or more `measured_full_search_probe` entries whenever fallback is invoked

The canonical public trace binds the manifest plan, candidate lock,
authorization, compatibility and policy signatures, bitrate/byte target,
quality floor, transform plan, search signature, encoder/metric toolchain, and
ordered observed work. Source paths, titles, source IDs, fingerprints, private
mapping tokens, review-media identities, and raw database rows are never
copied into the trace. Noncanonical bytes, missing warm-start evidence,
tampered CRFs, mismatched machine bindings, stale or under-supported locks,
private local personalization, and operator-authored success claims fail
closed.

The harness is bound by the v2 preregistration and accepts v1 or v2 manifest and
plan IDs while preserving the same isolated execution semantics. Its traces do
not grant runtime authority, select media, create candidate locks, or activate
the public bundle. Fallback reasons are allow-listed so a trace cannot carry
operator-authored titles, paths, or arbitrary private text.

## Evidence lifecycle

The v2 evidence lifecycle has six boundaries:

1. Validate the checked-in v2 manifest against its digest-bound aggregate
   eligibility attestation. This does not authorize runtime execution.
2. Map the protocol's case slots and separate derivation pools to eligible media
   in the canonical owner-only `av1vsp1` private partition. Independently review
   the exact partition, and commit only its SHA-256 selection lock and
   derivation-partition digest to later redacted evidence.
3. Create the separate derivation-only authorization, then build a candidate
   lock from current-contract observations collected through unchanged measured
   full search. Each
   candidate requires at least twelve eligible observations from six
   independent sources. The lock carries opaque derivation source, series, and
   source-group tokens so the holdout mapper can prove there is no overlap.
   Historical rows may not be relabeled or backfilled.
4. Independently review the exact traits, CRF range, compatibility and policy
   signatures, bitrate range, numeric quality floor, confidence, derivation
   freshness/conflict state, derivation snapshot, and selection lock.
5. Create one immutable holdout execution authorization that binds the manifest,
   selection-lock digest, and every reviewed candidate-lock ID. Every result
   must be later than this authorization, and evidence finalization must be
   later than every result.
6. Record one redacted result for every preregistered case, then aggregate it
   with the verifier. Missing, failed, incompatible, contaminated, or
   safety-stopped cases remain visible and block the affected cell.

Candidate locks and result evidence are local audit artifacts. They may carry
opaque random source, series, source-group, compatibility, policy, and review
environment tokens, but never paths or human-readable media identities. The
aggregate report removes those tokens, commits to the private evidence payload
digest, and is the only publication-safe output. Rebuilding a different
self-consistent manifest is insufficient: reporting requires exact equality
with the checked-in issue #277 preregistration.

No database schema migration is required. The protocol, partition contract, and
report builder are pure Python and do not import database, scheduler,
web-runtime, subprocess, or media probing code. The partition inventory adapter
is a separate read-only database/config boundary and does not open a writable
connection. Any one-time legacy state-path migration is operational startup
work and occurs only inside the shared runtime lock.

## Paired holdout contract

One activation case compares the same content-version/intention/compatibility
unit under:

- baseline: the unchanged full measured search
- guided: one candidate first probe followed by the same measured fallback

The baseline-selected CRF is the independent value used for range-hit scoring.
The guided arm may not stop early, bypass final validation, change the quality
floor, change the size target, or use private local personalization. Every
guided result must show `local_evidence_present=false` and a blinded visual
review. The locked numeric quality floor must agree with each arm's measured
score and quality-floor flag, and both paired final outputs must independently
meet the quality, size, and visual-acceptance requirements.

Holdout cases must map to unique titles and series. At least six independent
source groups are required, and no source group may provide more than one third
of a candidate cohort. The private selection lock is the audit proof that the
mapping was frozen before results were reviewed.

## Publication decision

The preregistration is intentionally stricter than the predictor's minimum
schema gates. Each candidate cell requires:

- sixteen complete paired held-out cases
- a moderate-or-higher candidate confidence score of at least `0.7`, derived
  from at least twelve eligible observations across six independent sources
- no conflicting derivation evidence and no derivation observation older than
  180 days when the candidate is locked
- at least thirteen range hits, expressed as a one-sided exact binomial result
  against the preregistered 50% null with `p <= 0.025`
- a one-sided paired sign result with `p <= 0.025` showing more candidate-count
  wins than losses, plus lower aggregate guided candidate work
- a locked CRF span no wider than six points
- zero quality-floor, final-size, blinded-visual-rejection, or
  operator-attention regressions
- duplicate blinded review on preregistered ordinals 4, 8, 12, and 16 by a
  separate review-environment token, with at least 80% agreement and no
  duplicate-review rejection where baseline was accepted
- the exact candidate lock, compatibility, policy, trait set, bitrate range,
  quality floor, and reviewed selection lock used by every case

The `0.025` per-cell threshold controls each separately preregistered claim. V1
registered animation and typical live action; v2 registers motion and darkness.
Reports remain per-cell and do not make a pooled claim that every AV1 prior
works.

Failure does not lower a threshold or widen a cell. The permitted conclusion is
`insufficient evidence to activate this cell`; the existing no-recommendation
and full-search behavior remains in place.

Fallback-conformance cells also have one exact preregistered reason. Risk-trait
cells require `content_profile_uncovered`; directional-intent cells require
`compression_intent_requires_directional_search`. A generic
`no_public_evidence` result does not prove those guards.

The aggregate report may mark one cell ready while rejecting another. It never
creates confidence, builds a public cell, mutates the checked-in prior bundle,
or decides #256/#262 automatically. Those remain explicit governance steps.

## Reporting

When redacted evidence exists:

```bash
uv run python -I -S scripts/verify_av1_cold_start_preregistration.py \
  report docs/validation/av1-cold-start-preregistration-v1.json \
  /path/to/private/redacted-evidence.json \
  --as-of YYYY-MM-DDTHH:MM:SSZ --runtime-state paused --json
```

`--as-of` is explicit so report IDs remain deterministic and future-dated
authorizations, results, or finalization timestamps fail closed. The supplied
runtime state is report context only; it is not execution authorization.

Exit codes are:

- `0`: the manifest is valid, or a report is ready for manual publication
  review
- `2`: evidence is valid but incomplete, rejected, or still blocked
- `1`: malformed, tampered, incompatible, or non-canonical input

The runtime remains paused until the private selection mapping and every
candidate lock have been independently reviewed and the bound execution
authorization has been created. This change does not start a cohort, discover
media, generate review clips, or run a real-media experiment.

## V3 Tier 1 Gate A0 artifact publication

`create-qualification-key` creates exactly one durable v3 qualification key in
an owner-only machine-local directory. The key is 32 random bytes stored as
`qualification-key.bin`; its public `av1vqkey3_*` identifier is derived through
the protocol's qualification-key HMAC domain. Re-running the command loads the
same key and identifier rather than rotating it. The key is not a media,
encryption, Git, login, or empirical-selection key: it commits the later Tier 2
qualification ranking before private source selection. The command reads no
media or inventory, takes no runtime lock, and grants no execution authority.

`mediaforce/tuning/av1_validation_v3_tier1_config_snapshot.py` defines the
canonical effective-config snapshot used by Gate A0. The snapshot contains the
fully merged `MediaforceConfig.raw` after checked-in includes, runtime settings,
and local folder-policy overrides, plus every resolved `ConfigPaths` value. It
is private machine-local material: paths and local settings may appear in the
snapshot, but only its domain-separated digest may enter public-safe summaries.

The owner-only `write-tier1-config-snapshot` action writes that canonical JSON
as `tier1-effective-config-snapshot.json` in a content-addressed `0o700`
directory with a `0o600` file. The later Tier 1 runtime rebuilds the snapshot
from the live loaded config, compares it byte-for-byte, and requires the same
snapshot digest in the qualification plan before acquiring the runtime lock.
Changing an include, runtime setting, local override, or resolved state path
therefore invalidates the plan rather than silently changing qualification.

`mediaforce/tuning/av1_validation_v3_tier1_publication.py` implements
non-executing artifact publication for Gate A0 of the AV1 v3 Tier 1 cold-start
qualification protocol. It produces two owner-only artifacts inside one
request-addressed directory under the caller-supplied output root:

- `qualification-plan.json` — the frozen `AV1ValidationV3QualificationPlan`
  that binds protocol, key ID, repository identity, config digest, toolchain
  digest, fixture matrix digest, and eligibility predicate digest.
- `tier1-authorization-request.json` — the corresponding
  `AV1ValidationV3Tier1AuthorizationRequest` that binds the plan and adds a
  request timestamp and validity window.

Neither file grants execution authority. The directory name is
`av1-v3-tier1-preparation-<request_id>`, making the output content-addressed.

### Security properties

- The output root is absolute, mode `0o700`, owner-owned, and neither equal to,
  below, nor above the repository. Every lexical path component is opened with
  no-follow semantics; a symlink anywhere in the path is rejected.
- Each artifact is written through a randomly named staging directory
  (`secrets.token_hex(12)`) and promoted to its final name with
  `rename_exclusive`, which fails atomically if a destination already exists.
- Staging and final directories re-verify type, owner, mode, and
  `(st_dev, st_ino)`. Artifact members additionally re-verify `st_nlink` and
  `st_size` to detect TOCTOU races.
- A second toolchain fingerprint is computed immediately before publication to
  detect toolchain drift between plan construction and write.
- The output root must not overlap the repository root (no containment in
  either direction).

### Idempotency

If the final directory already exists,
`publish_av1_validation_v3_tier1_preparation` reads both stored files and
compares them byte-for-byte against the freshly computed content. An exact
match returns `created=False`; a conflict, incomplete pair, unexpected member,
unsafe mode, or unsafe link fails closed and is never overwritten or repaired.

### CLI usage

```bash
uv run python -I -S scripts/verify_av1_cold_start_preregistration.py \
  create-qualification-key \
  --output-root /path/to/private/qualification-key \
  --json

uv run python -I -S scripts/verify_av1_cold_start_preregistration.py \
  write-tier1-config-snapshot \
  --config /path/to/mediaforce-config.toml \
  --output-root /path/to/private/config-snapshots \
  --json

uv run python -I -S scripts/verify_av1_cold_start_preregistration.py \
  publish-tier1-preparation \
  docs/validation/av1-cold-start-preregistration-v3.json \
  --qualification-key-id av1vqkey3_<32 hex chars> \
  --repository-commit <exact clean commit> \
  --repository-tree <exact clean tree> \
  --config-artifact /path/to/exact-tier1-config-artifact \
  --ffmpeg /path/to/ffmpeg \
  --ffprobe /path/to/ffprobe \
  --output-root /path/to/private/artifacts \
  --frozen-at YYYY-MM-DDTHH:MM:SSZ \
  --plan-valid-until YYYY-MM-DDTHH:MM:SSZ \
  --requested-at YYYY-MM-DDTHH:MM:SSZ \
  --request-valid-until YYYY-MM-DDTHH:MM:SSZ \
  --json
```

The owner-supplied repository `(commit, tree)` is cross-checked against the
runner's clean live identity before any preparation artifact is written. The
config artifact must pass the canonical effective-config snapshot contract and
is read exactly once. The same exact bytes must be supplied to the later
separately authorized execution boundary. Summaries contain no machine-local
paths and report Gate A0, Tier 1, created/no-op state, and every execution,
media, Tier 2, evidence, empirical, derivation, holdout, public-publication, and
activation authority as false.

The command remains on the owner-only preregistration runner rather than the
normal `mediaforce` operator CLI. It creates no grant, takes no Mediaforce
runtime lock, opens no database or private inventory, and does not execute
`ffmpeg` or `ffprobe`; the binaries are only inspected and hashed for the
machine-local toolchain binding. Grant issuance and fixture execution remain
separate later owner decisions.

---

## Phase 2 — V4 Qualification Search Seam (non-live implementation)

**Module:** `mediaforce/tuning/av1_validation_v4_qualification_search.py`
**Tests:** `tests/test_av1_validation_v4_qualification_search.py`
**Contract version:** `av1vq4s1`
**Source identifier:** `av1_cold_start_v4_qualification`

### Purpose

Phase 2 adds a narrow, typed, qualification-only seam that wires the production
`search_quality_for_source` callable into the v4 cold-start evaluation workflow.
The seam can invoke only the production search callable supplied explicitly by a
future qualification executor. This implementation slice and its tests execute
no media and access no database. The module is not imported by any web or
operator runtime; no implicit access to runtime state is possible.

### Two invocation modes

**Baseline** — explicit `warm_start=None` and
`expected_search_signature_id=None`. The production search runs without any
warm-start hint. The resulting `av1_cold_start_prior` execution mirror has
`execution=null`.

**Guided** — `warm_start=QualitySearchWarmStart(source="av1_cold_start_v4_qualification", ...)`.
The production search runs with a frozen, manifest-supplied warm-start hint plus
the matching `expected_search_signature_id`. The execution mirror carries the
same payload shape as the `target_size_trace["warm_start"]` sub-dict without
retaining a mutable alias to the search result.

Both modes report `status="no_recommendation"` in the cold-start prior mirror
because `plan_av1_cold_start` is deliberately bypassed; the seam calls
`unavailable_av1_cold_start_prediction` to construct the base payload.

### Invariants enforced before calling search

1. **Confirmed balanced intent** — compression intent must be `level="balanced"` and
   `confirmed=True`. Legacy, unconfirmed, or non-balanced policies raise
   `V4QualificationContractError` and the search callable is never invoked.

2. **Typed target-size route** — source identity is represented by a `Path`, and
   a non-null typed `StreamBudgetLedger` is mandatory.

3. **Guided source contract** — `warm_start.source` must equal
   `"av1_cold_start_v4_qualification"`. The v3 harness source
   (`"av1_validation_harness"`) is explicitly rejected, as is any other source
   string. `search_signature_id`, `cohort_id`, CRF values, confidence,
   provenance, and review-risk tokens are validated before the callable runs.

4. **Search-input allowlist** — `extra_search_kwargs` accepts only the production
   source/cadence/host/temp-directory inputs that cannot replace the resolved
   quality plan. `resolved_plan`, `_allow_validation_warm_start`,
   `stream_budget_ledger`, `warm_start`, `expected_search_signature_id`, and all
   unknown keys are rejected. The v3 harness flag
   `_allow_validation_warm_start=True` is never set by this seam; v4 uses a
   distinct source string instead.

### Invariants enforced after search returns

5. **Trace presence** — the callable must return a typed `QualitySearchResult`
   with a non-null `target_size_trace`.

6. **Baseline trace purity** — `target_size_trace` must not contain a `"warm_start"`
   key for baseline mode.

7. **Guided trace validity** — `target_size_trace["warm_start"]` must exist,
   have `attempted=True`, and carry `status` in `{"accepted", "rejected_fallback"}`.
   No v4-specific quality, ratio, or size threshold is applied to either status.

8. **Identity match** — The guided execution trace's source, signature, cohort,
   CRFs, confidence, provenance, and review risks must match the frozen
   `QualitySearchWarmStart` input exactly. Any mismatch raises
   `V4QualificationContractError`.

9. **Mirror status** — The cold-start prior mirror must carry
   `status="no_recommendation"`.

### The cold-start prior execution mirror

The mirror is built to match what `calibration_runtime.py` constructs at line 914:

```python
cold_start_payload["execution"] = object_dict(target_size_trace.get("warm_start")) or None
```

For baseline this is always `None`. For guided it is a deep copy of the
warm-start sub-dict from the search trace, preserving the exact runtime payload
shape without retaining a mutable alias to the search result.

### Privacy contract

The public summary returned by `V4QualificationOperationResult.public_summary`
contains only:

- `schema_version`, `contract_version`, `mode`
- `planner_bypassed: True`
- `execution_attempted` (bool)
- `execution_status` (`"accepted"` | `"rejected_fallback"` | `null`)
- false evidence, inventory/media, empirical, derivation, holdout, publication,
  activation, and retry authority fields

It never exposes source path, CRF values, signature ID, cohort ID, provenance ID,
trace internals, or quality metric scores.

### V4 vs V3 identity separation

| Constant | V3 harness | V4 seam |
|---|---|---|
| Source string | `av1_validation_harness` | `av1_cold_start_v4_qualification` |
| Contract version | `av1vh1` | `av1vq4s1` |
| Harness flag | `_allow_validation_warm_start=True` | never set |

The exact v4 source identifier is distinct from every v3 harness and packaged
prediction source. The module also exposes a deterministic invocation payload
and SHA-256 that bind the source path, frozen video policy, allowlisted search
inputs, and exact baseline or guided warm-start identity. A later manifest can
therefore prove baseline and guided invocation identities differ while the base
config SHA remains identical.

---

## Phase 5 — V4 Canonical Manifest Draft (non-executing)

**Manifest:** `docs/validation/av1-cold-start-preregistration-v4.json`
**Discovery projection:** `docs/validation/av1-v4-discovery-public-v1.json`
**Validator:** `mediaforce/tuning/av1_validation_v4.py`
**State:** `draft_unapproved`

### Purpose

Phase 5 converts the owner-approved source table and completed owner-only
discovery into canonical, reviewable repository bytes without importing any
media, machine-local paths, runtime state, or execution authority. The draft
binds all identities that are public and stable now, while explicitly requiring
a later non-executing machine-local preparation artifact before the owner can
freeze the manifest.

The draft supersedes the terminal v3 protocol for this successor workstream but
does not alter v3 history or grant permission to resume any v3 operation.

### Bound public identities

The manifest fixes:

- exactly four ordered sources across `animation_content` and
  `live_action_content`, with one primary and one confirmation per class;
- official fetch and license URLs, media byte lengths and SHA-256 values,
  complete observed stream inventories, probe digests, SDR evidence, terms
  digests, and the discovery/toolchain-probe binding;
- the safe single-entry Tears of Steel archive relationship and Sintel's
  publisher checksum corroboration;
- all eight source/configuration traversals, with both primary sources before
  either confirmation source and no adaptation, substitution, or favorable
  subset;
- confirmed `balanced` policy with frozen CRF bounds `10..45`;
- baseline search with no warm start and guided search with the exact v4 source,
  signature/cohort prefixes, matching expected signature, planner bypass, and
  mandatory target-size routing; and
- the exact byte, storage, fetch, discovery, traversal, and whole-run limits
  approved in issue `#334`.

### NASA video-only constraint

Discovery found third-party music and sample ingredients in the NASA MP4's
official metadata. The source remains eligible only under a structural
video-only rule:

- qualification video stream index: `0`;
- excluded stream index: `1`;
- excluded stream type: `audio`.

The manifest loader rejects any mutation that allows the NASA AAC track. A
later preparation artifact and executor must independently enforce the same
stream map; the repository draft alone does not authorize that executor.

### Machine-local preparation boundary

The repository manifest intentionally does not contain absolute source paths,
workspace paths, config paths, or binary paths. Before owner freeze, a separate
non-executing preparation step must bind:

- the exact repository commit and tree;
- effective config SHA-256;
- `ffmpeg`, `ffprobe`, and `ab-av1` versions and binary digests;
- HMAC-derived dedicated-instance and source-path identities;
- runtime compatibility, qualification key, guided warm-start identity, and
  concrete baseline/guided invocation digests.

That preparation may hash approved files and binaries but must not read media
content, invoke subprocesses, ingest media, run search, encode, or create
evidence. Concrete invocation digests remain machine-local because the Phase 2
seam intentionally binds absolute source paths.

### Privacy and authority contract

The public discovery projection removes the raw discovery workspace and
`ffprobe` path while retaining public-source provenance and cryptographic
identity. Both checked-in JSON files are canonical single-line JSON with a
trailing newline. Validation rejects `/Users`, `/Volumes`, `/opt/homebrew`, and
private path keys.

Validate the checked-in draft without loading site packages or runtime config:

```bash
uv run python -I -S scripts/verify_av1_v4_manifest.py \
  docs/validation/av1-cold-start-preregistration-v4.json \
  docs/validation/av1-v4-discovery-public-v1.json \
  --json
```

Every execution, evidence, private-inventory, freeze, publication, activation,
retry, and dogfood authority bit is `false`. Authoring, validating, or merging
this draft does not freeze it. A later exact owner decision is required after
machine-local preparation and independent review.

### Semantic validator coverage

The runtime loader still validates frozen identity before any semantic rule.
Tests may call `assert_av1_validation_manifest_v4_semantics(...)` directly on
mutated in-memory payloads to prove the private-path, authority, source, matrix,
invocation, rights, resource-limit, and preparation branches fail closed. This
test-only separation does not change the loader order, the isolated verifier,
or either frozen JSON artifact.

---

## Phase 6 — V4 Rights Attestation Template (non-executing)

`docs/validation/av1-v4-rights-attestation-template-v1.json` is a canonical,
digest-bound template for a later explicit owner rights review. The checked-in
template is `template_unattested`: it names no owner, carries no timestamp, and
all four source claim slots are `null`.

The contract binds the merged v4 manifest identity, the public discovery
digest, and the exact captured terms digests without storing page bodies or
verbatim terms. A later machine-local completed record must explicitly retain
the Gooseberry title grant as the Cosmos license authority, classify the
Netflix mirror as technical provenance only, and preserve the NASA video-only
stream constraint after acknowledging the discovered third-party audio
ingredients.

The validator rejects captured-page fields, long reproduced terms text,
machine-local paths, missing source claims, and any authority bit set to true.
Even a completed owner attestation grants no manifest freeze, traversal,
execution, evidence, publication, activation, retry, private-read, or dogfood
authority. No completed attestation is checked in by this phase.

---

## Phase 7 — V4 Preparation Record Contract (non-executing)

`mediaforce/tuning/av1_validation_v4_preparation.py` defines the pure contract
for the machine-local preparation record required before an owner freeze
decision. It accepts only externally measured repository, config, toolchain,
HMAC, runtime-compatibility, warm-start, invocation, qualification-key, and
completed-rights-attestation identities. It does not discover or measure them.

The module imports no filesystem, subprocess, network, or database facilities.
It accepts no source paths, binary paths, media bytes, key bytes, or executable
handles. Dedicated-instance and source paths appear only as closed-schema HMAC
IDs. Baseline and guided invocation digests must differ while their base config
digest remains identical.

The resulting machine-local record is `prepared_unfrozen`, binds the completed
rights attestation by ID and digest, and explicitly records
`media_bytes_read=false` and `subprocess_executed=false`. Every execution,
freeze, traversal, evidence, publication, activation, retry, private-read, and
dogfood authority remains false. This phase defines and tests the contract only;
it does not create or commit a real preparation record.
