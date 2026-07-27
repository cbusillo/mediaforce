# AV1 Cold-Start Controlled Validation

Issue #277 owns the controlled evidence that may prove or reject public AV1
cold-start cells. The checked-in preregistration freezes the experiment design
without selecting media, starting Mediaforce, or authorizing any encode.

## Checked-in protocol

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

The validator reports `runtime_execution_authorized=false`. A valid manifest is
permission to prepare a private mapping and candidate locks, not permission to
run encodes.

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
request path, or authorize an encode. Issue #285 owns integration of the
prospective projection into an executable successor protocol.

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

The harness remains a preregistration input for #285. Its traces do not grant
runtime authority, select media, create candidate locks, or activate the public
bundle.

## Evidence lifecycle

The execution phase has five boundaries:

1. Map the protocol's case slots to eligible media in a private local file.
   Commit only its SHA-256 selection lock to the redacted evidence set.
2. Build a candidate lock from current-contract derivation observations. Each
   candidate requires at least twelve eligible observations from six
   independent sources. The lock carries opaque derivation source, series, and
   source-group tokens so the holdout mapper can prove there is no overlap.
   Historical rows may not be relabeled or backfilled.
3. Independently review the exact traits, CRF range, compatibility and policy
   signatures, bitrate range, numeric quality floor, confidence, derivation
   freshness/conflict state, derivation snapshot, and selection lock.
4. Create one immutable execution authorization that binds the manifest,
   selection-lock digest, and every reviewed candidate-lock ID. Every result
   must be later than this authorization, and evidence finalization must be
   later than every result.
5. Record one redacted result for every preregistered case, then aggregate it
   with the verifier. Missing, failed, incompatible, contaminated, or
   safety-stopped cases remain visible and block the affected cell.

Candidate locks and result evidence are local audit artifacts. They may carry
opaque random source, series, source-group, compatibility, policy, and review
environment tokens, but never paths or human-readable media identities. The
aggregate report removes those tokens, commits to the private evidence payload
digest, and is the only publication-safe output. Rebuilding a different
self-consistent manifest is insufficient: reporting requires exact equality
with the checked-in issue #277 preregistration.

No database migration is required. The protocol and report builder are pure
Python and do not import database, scheduler, web-runtime, subprocess, or media
probing code.

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

The `0.025` per-cell threshold controls the two separately preregistered
animation/live-action publication claims. Reports remain per-cell; they do not
make a pooled claim that every AV1 prior works.

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
