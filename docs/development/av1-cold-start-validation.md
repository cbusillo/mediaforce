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
uv run python scripts/verify_av1_cold_start_preregistration.py \
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
uv run python scripts/verify_av1_cold_start_preregistration.py \
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
uv run python scripts/verify_av1_cold_start_preregistration.py \
  create-partition-key /private/owner-only/av1-v2/partition.key --json
```

The command emits an opaque `token_key_id`. Record that ID on issue `#286`
before reading the private inventory. The later build requires the same ID, so
replacing or rerolling the HMAC key after its durable commitment fails closed.

Build the immutable private partition from the checked-in manifest, pinned
machine-local eligibility attestation, current measured fingerprint inventory,
and an explicit canonical UTC timestamp:

```bash
uv run python scripts/verify_av1_cold_start_preregistration.py \
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
uv run python scripts/verify_av1_cold_start_preregistration.py \
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
uv run python scripts/verify_av1_cold_start_preregistration.py \
  validate-eligibility /path/to/eligibility-attestation-v1.json --json
```

Successful validation emits only `eligibility_valid=true` and false execution
authority flags. It does not emit the attestation ID, cutoff timestamp, or any
aggregate counts.

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

No database migration is required. The protocol, partition contract, and report
builder are pure Python and do not import database, scheduler, web-runtime,
subprocess, or media probing code. The partition inventory adapter is a separate
read-only database/config boundary and does not open a writable connection.

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
uv run python scripts/verify_av1_cold_start_preregistration.py \
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
