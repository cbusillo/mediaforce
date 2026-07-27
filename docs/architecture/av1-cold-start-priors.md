# AV1 Cold-Start Priors

Mediaforce can choose one better first AV1 calibration probe from a versioned,
privacy-safe prior, then lets the existing measured target-size search accept or
reject that probe. The prior never accepts quality, changes the operator's
target, expands a size limit, narrows search bounds, or replaces full measured
calibration.

## Authority and flow

The cold-start path has three separate artifacts and lifecycles:

1. The checked-in public bundle contains aggregate bounded-table cells only.
2. The runtime database contains private append-only operator observations and
   replay-derived local personalization.
3. The predictor reads both sources and returns either one bounded CRF range or
   an explicit no-recommendation reason.

Public bundle rows never become local observations, and local observations are
never written back into the checked-in bundle. A recommendation becomes a
`QualitySearchWarmStart` for one measured probe. The existing quality floor,
source cap, target band, compression-intent policy, and transform-plan checks
decide whether that probe is usable. A rejected or failed probe continues into
the unchanged full search.

## Initial bundle state

`mediaforce/tuning/data/av1_cold_start_priors_v1.json` is the only shipped
public artifact. Bundle `1.0.0` is intentionally `validation_pending` with no
active cells because Mediaforce does not yet have approved public aggregate
evidence. It therefore returns `no_public_evidence` on an untrained install.

This is deliberate rather than a placeholder recommendation. Issue #277 owns
the controlled animation/live-action holdouts that may authorize active cells.
Private runtime observations, historical unlabeled rows, and hand-authored CRF
guesses cannot populate the public artifact.

## Version and serialization contract

The bundle has independent versions for:

- the JSON schema
- the bundle contents
- the predictor contract

The file is canonical UTF-8 JSON with sorted keys, compact separators, finite
numbers, and one trailing newline. `payload_sha256` covers the complete semantic
payload except the digest field itself. The loader rejects unsupported fields,
schema or predictor versions, digest drift, non-canonical bytes, malformed
values, local paths, runtime artifacts, and private identifier fields.

Missing or invalid bundle data is a runtime no-recommendation outcome, not a
calibration failure. Package verification is stricter: a wheel or sdist must
contain exactly one byte-identical copy of the public artifact.

## Compatibility

Public compatibility is semantic and non-identifying. Every cell binds all of
these dimensions:

- SVT-AV1 encoder version range
- FFmpeg runtime major plus the `ab-av1` quality-tool identity and major
- sample-projection measurement basis and `operator_visual_v1` assessment
  contract
- preset, pixel format, and bit depth
- exact output width and height
- frame-rate rational
- cadence transform and resolved video filter
- complete sorted encoder parameters plus explicit grain strength and denoise
- quality metric, target, and minimum score
- output container
- authoritative target-video bitrate range

The public key intentionally excludes local runtime signatures, stream-plan
identities, policy hashes, source identities, and machine paths. Private replay
continues to use #275's stricter exact compatibility key. A mismatch in either
layer returns no recommendation; compatibility is never relaxed to make a cell
match. Runtime version strings that cannot satisfy the public semantic-version
contract make only the public layer incompatible; private exact-key replay
remains available.

## Content and intent

Prospective validation uses the `acstp1` AV1 trait projection rather than the
fingerprint decision's raw finding list. The projection is canonical JSON with
a hash-bound `projection_id`; it separates stable cell identity from review-only
advisories before any validation cell is selected.

Stable non-advisory `dark_luma`, `high_motion`, and `high_texture` findings can
produce darkness, motion, and texture/detail identity at the contract's
confidence floor. `animation_cues` and `likely_film_grain` are the only
advisories explicitly promotable into identity, and only at the higher
promotion floor. Audio complexity, duplicate cadence, banding risk, analog or
ambiguous noise, and low-confidence animation/grain findings remain review
advisories. An audio-only or advisory-only decision therefore remains
`typical`; an unrecognized or low-confidence identity finding becomes
`unknown` rather than being mislabeled as typical.

The projected public multi-label vocabulary is:

- `animation`
- `darkness`
- `motion`
- `grain_noise`
- `texture_detail`
- `low_motion_dialogue`
- `mixed`
- `typical`
- `unknown`

`mixed` is added when multiple measured identity traits are present. `unknown`
is an explicit state, not an inferred genre. The current analyzer has no
low-motion-dialogue producer, and the current request contract rejects
unmeasured evidence before it can create an unknown fallback request. The
feasibility gate therefore marks both paths unavailable instead of accepting an
unexecutable preregistration cell. Public cells require an exact match to the
complete sorted trait set, so a narrow animation-only cell cannot generalize
into unvalidated dark, grainy, textured, or mixed content.

Historical content-intent observations remain append-only. Feasibility can
join an observation to current retained fingerprint analysis only when the
content-version binding still matches, then apply `acstp1` in memory without
rewriting the observation or reopening the media.

Each confirmed compression intent has one exact optimization objective:

- `reference`: maximize measured fidelity inside the authoritative size limit
- `transparent`: minimize size while preserving source indistinguishability
- `balanced`: minimize target distance subject to the measured quality floor
- `perceptual_floor`: minimize size subject to explicit visual acceptability

Unconfirmed legacy intent returns no recommendation. The existing target-size
warm-start path can safely accept a one-probe hint only for `balanced` intent.
`reference`, `transparent`, and `perceptual_floor` require directional search,
so v1 returns `compression_intent_requires_directional_search` and preserves
the full measured path for those intentions instead of reporting a hint that
the search layer cannot use.

## Confidence and conflicts

Public cells carry a bounded CRF range, center, confidence score, aggregate
evidence/source counts, held-out range hits, candidate work, safety-regression
counts, provenance, and review risks. A cell is actionable only with moderate
or high confidence, score at least `0.65`, at least three independent sources,
at least three held-out cases with a 50% range-hit rate, lower candidate work,
and zero quality-floor, final-size, visual-rejection, or operator-attention
regressions. Local broader-scope confidence counts independent acceptable
sources; rejection-only sources cannot unlock an accepted-CRF recommendation.

When equally specific matching cells disagree on their range, the predictor
returns `public_evidence_conflicting`. It does not average conflicting cells.
Recommended ranges must fit the currently configured CRF bounds without
clamping and must contain at least one executable integer CRF. The selected
integer probe is always inside the validated range.

Private local replay uses the first non-empty item, folder, content-class, or
operator scope. An exact current-item approval is treated as a
moderate-confidence item exception because it applies to the same content
version rather than a population. Broader scopes still require moderate/high
independent-source confidence. Stale, conflicting, limited-confidence,
CRF-incomplete, or overly dispersed local evidence vetoes a public
recommendation instead of being silently ignored. A fresh local range may stand
alone or intersect a public range. A non-overlapping public/local pair returns
no recommendation.

Freshness requires the observation timestamp to match the copy stored inside
its hash-bound provenance. Older observations without that binding remain
auditable under #275 but return `local_evidence_unversioned` and cannot authorize
a cold-start probe. Future-dated observations return
`local_evidence_future_dated`. Broader local ranges are also rejected when CRF
MAD exceeds `2.0` or the observed acceptable range exceeds six CRF points.

## Provenance and review risk

Predictor payloads expose bundle/cell provenance for public evidence and a
one-way runtime provenance token plus scope for local evidence. The local token
binds the selected scope and its hash-derived evidence snapshot, so traces
distinguish private evidence revisions without exposing observation identities.
They never expose folder paths, source IDs, observation IDs, media fingerprints,
or review artifact identities. Review-risk tags are deterministic projections
of the matched content traits and the aggregate cell contract.

The sampled calibration payload records the recommendation and the warm-start
execution result separately. This preserves the distinction between “the prior
suggested this probe” and “measurement accepted this probe.”

## Controlled evaluation

`mediaforce/tuning/av1_cold_start_evaluation.py` aggregates pre-declared held-out
cases into publication-safe counts. It compares baseline and guided candidate
work, range hits, quality-floor outcomes, final-size outcomes, visual verdicts,
and operator-attention events without carrying title, path, source, or media
identities into the public cell.

`mediaforce/tuning/av1_validation_harness.py` is a separate prospective
execution-contract boundary. It adapts reviewed validation locks into one
validation-sourced warm-start probe, validates the existing target-size search
trace, and emits a canonical machine-bound ordered-work trace. Production cold
start and calibration modules do not import it, so validation locks cannot
masquerade as packaged public cells or private replay evidence.

Synthetic unit fixtures prove arithmetic, conflict handling, deterministic
serialization, and fallback behavior. They are not training evidence and are
not reported as empirical predictor accuracy. Controlled real-media evidence
and any decision to activate bundle cells belong to #277.

Issue #277 adds a separate manifest → evidence → report lifecycle around this
arithmetic. The canonical preregistration lives outside the package under
`docs/validation/`; it freezes privacy-safe case slots, exact cohort rules,
paired arm order, strict evidence thresholds, fallback-conformance coverage,
and the requirement that runtime remain paused until private source selection
and candidate locks are independently reviewed.

Private source/title/series mappings never enter the manifest or aggregate
report. A local evidence set may use opaque random tokens plus compatibility,
policy, selection-lock, and derivation-snapshot digests. An immutable execution
authorization binds the pinned manifest, private selection lock, and every
reviewed candidate lock before any case may run. The public report strips the
private tokens, commits to the full private evidence payload digest, refuses to
pool cells, keeps missing/failed/safety-stopped cases visible, and reports
deterministic blockers. It cannot create a prior cell or mutate the checked-in
bundle.

The preregistration requires sixteen paired holdouts for each activation
candidate, fresh conflict-free derivation evidence, moderate-or-higher
confidence, an explicit numeric quality floor, one-sided exact range-hit and
candidate-work evidence, zero safety regressions, source/series independence,
and blinded duplicate-review agreement. These governance gates are
intentionally stricter than the three-case minimum that protects the generic
bundle schema.

## Failure behavior

The predictor returns no recommendation for missing, invalid, stale,
unsupported, incompatible, conflicting, low-confidence, unvalidated,
future-dated, out-of-bounds, non-executable-range, or private-local evidence
failures. Machine-readable fallback reasons remain from a bounded vocabulary;
unexpected request-shape errors become `cold_start_request_invalid`. In every
case Mediaforce retains the current measured calibration and full-search path
with unchanged quality and size safeguards.
