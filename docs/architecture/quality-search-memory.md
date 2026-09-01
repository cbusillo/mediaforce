# Quality-Search Memory

## Purpose

Quality-search memory turns accepted encode outcomes into explainable historical
context. Its current phase is deliberately passive: it can report a central CRF
hint, confidence, dispersion, invalidation reasons, and a stable future-study
arm, but it cannot change an encode policy, search bound, quality floor,
transform, stream plan, or runtime decision.

## Authority Boundary

Measured facts have higher authority than inferred guidance.

Measured facts are:

- a queue or CLI encode completed, validated, and was promoted
- its source codec and promoted output resolution
- its search objective, quality metric, target, minimum score, and selected CRF
- for target-size searches, its total size goal and duration-normalized target
  video bitrate
- the effective encoder, pixel format, preset, encoder parameters, video filter,
  and output container in the final command
- its validation, promotion, content-version, and source-fingerprint identity

Inferred guidance is:

- a median CRF for a compatible exact-item, season, or series cohort
- confidence derived from sample count and scope
- dispersion derived from interquartile range and median absolute deviation

Inferred guidance is advisory. Current operator policy, persisted stream-budget
and transform ledgers, quality floors, validation, and promotion rules remain
authoritative.

## Accepted Historical Outcomes

The read-only loader uses the latest `staged_artifacts` row for each library
item. A row is eligible only when all of these conditions hold:

- `encode_origin` is `queue` or `cli`
- the library item is still `promoted`
- validation reports `passed: true`
- encode completion, staging, validation, and promotion timestamps are present
  and ordered
- the staged row's last update is the promotion itself, so retained acceptance
  markers cannot authorize a later upserted encode
- promotion is no more than 180 days old
- the library item is not missing and its content did not change after
  promotion
- CRF, metric, target, score, codec, resolution, and effective command context
  are complete and finite

These checks are intentionally strict. `staged_artifacts` is updated in place,
so a later encode can otherwise retain older validation or promotion markers.
Infrastructure, scheduling, cancellation, stale-lease, host, transport,
storage, resource-busy, calibration, unvalidated, and unpromoted outcomes never
enter the accepted distribution. A matching `encoding_completed` event binds
the staged row to its actual search objective. Legacy events without the
`target_size_trace` key predate target-size search and are treated as
quality-only; current target-size events must provide their structured target
and quality-floor facts.

## Search Signature

The quality-search signature is a stable hash of facts that must remain
compatible before CRFs can be compared:

- metric and target
- minimum quality score and search objective
- target size and duration-normalized video bitrate when size is an objective
- source codec and promoted output resolution
- encoder, pixel format, preset, and encoder parameters
- resolved video filter
- output container
- signature schema version

Paths, hosts, timestamps, chosen CRF, measured score, output size, and
MediaForce metadata are not signature inputs. They describe one outcome rather
than the assumptions that made outcomes comparable.

## Append-Only Observations

Structured quality-search observations are stored in
`quality_search_observations`. Each logical search run records one terminal
snapshot after a staged encode succeeds or after a deterministic search-domain
failure can be reconstructed. Target relaxation, CRF-bound expansion,
target-size candidates, and the bounded final-size retry remain part of that
single run rather than becoming separate observations.

When bounded final-size retry planning declines, the full target-size trace in
the needs-review event retains the structured `final_retry_skip` details. The
terminal observation projects the stable `needs_review` status and
`final_retry_skipped_*` selection reason, so analysis can classify the decline
without replaying the planner while keeping the observation payload bounded.

Final-size retry planning uses the completed output as a measured calibration
anchor. It first prefers an already-measured directional candidate or an
interpolated candidate inside an existing calibrated bracket. When neither is
available, it measures a bounded directional seed. That seed uses calibrated
same-side evidence when available and the conservative bitrate-halving prior
otherwise. It may then place a second sample with a content-derived log-space
secant using the completed-output anchor and the first measured/calibrated
point. A third sample is permitted only when the two real measured/calibrated
probes are monotonic and straddle the final band; it uses strict log
interpolation between those points and never a same-side extrapolation. The
retry planner permits at most three sample measurements and one replacement
full-output encode. A measured sample authorizes that retry
only when its real quality score still meets the floor and its calibrated
projection remains inside the unchanged final-size band and source cap. Missing
bounds, invalid or non-monotonic evidence, exhausted bounds, quality failure,
crossing to the opposite side without a valid interior integer, or a projected
miss all remain fail-closed needs-review outcomes.

The log stores typed identity, signature, selected-result, timing, and output
fields alongside bounded JSON for context, bounds, candidates, outcome, and
provenance. Candidate JSON contains parsed measurements only; raw tool stdout is
never persisted. Schedule interruption, cancellation, resource contention,
stale leases, host or transport failures, storage failures, malformed output,
and unrelated encode failures do not create observations.

Runtime-native observations have higher authority than reconstructed staged
history. Startup maintenance backfills only outcomes that already satisfy the
strict accepted-history rules above, marks them `staged_backfill`, leaves
unavailable policy and search-timing facts explicitly absent, and is safe to
rerun. Once a staged-backfill run identity exists, later reruns preserve that
immutable projection even when extraction code adds optional fields, while
continuing to append newly accepted run identities. Schema migration remains
independent of application extraction code; a
failed backfill is logged and retried at the next startup. A later native row
for the same run wins during current-revision resolution without mutating the
historical row.

The table is database-enforced append-only: updates and deletes are rejected.
Corrections append a complete successor with the same run identity, the next
revision, a predecessor pointer, and a machine-readable reason. Library-item
deletion does not cascade into this audit history; item identity and path are
copied into every observation.

The append-only observation phase remains passive. Active warm-start behavior is
separately qualified from prior passive shadow evidence and never mutates an
observation, saved policy, or search bound.

## Shadow Recommendations

Runtime-native selected observations now carry an immutable `shadow_json`
payload. The payload records the first CRF that earlier compatible evidence
would have suggested, its scope, confidence, sample count, dispersion, typed
fallback reason, and a comparison with the search that actually ran. Passive
shadow evaluation happens after the production search is complete. Active
planning evaluates the same immutable history before search so it can authorize
the isolated probe. Both paths use an explicit evidence cutoff at the current
search's start time, so the current result cannot recommend itself.

Shadow evidence comes from the highest-authority append-only revision that
already existed at the search-start cutoff. It must be selected,
learning-eligible, within the age limit, and compatible with the current search
signature. Exact-item evidence also requires the same source fingerprint.
Historical traces that are non-monotonic, changed their quality target during
the run, or contain conflicting quality-floor measurements are excluded before
cohort statistics are computed. A terminal result selected by the bounded
final-size retry is also excluded from first-CRF guidance: it remains valid
final-size and safety evidence, but its retry-calibrated CRF is not treated as
the CRF that should have started the original measured search. Those rows also
do not satisfy passive benchmark completeness or influence baseline candidate
and search-time medians used to estimate first-probe savings. Runtime and
correction rows require the current policy hash; historical staged backfill
without one remains eligible through its complete search signature.

First-CRF inference, primary passive metrics, and readiness calculations
deduplicate terminal observations by normalized source path, source
fingerprint/content version, exact search signature, and policy hash. The newest
current correction or runtime result wins; raw evaluated observation count
remains diagnostic so retry volume cannot inflate readiness.

Warm-start readiness uses only passive rows whose recommendation scope and
prefix match the current recommendation. Item-, season-, and series-derived
recommendations remain separate evidence groups; narrower historical guidance
cannot silently qualify a broader scope. Scope-less fallback rows remain inside
the already-selected media scope as fail-closed safety context, but never count
as recommendation-bearing accuracy or savings evidence.

## Exact-Item Target Containment

Each production manifest records a schema-versioned target provenance payload for
every item. It identifies whether the resolved target came from an exact-item
override, an ancestor override, or the configured default, without changing the
resolved target or its quality floor.

For an exact item, Mediaforce may use current quality observations only when the
observation has the identical source path and source fingerprint. A quality-safe
minimum is the smallest measured whole-episode candidate that met the recorded
quality floor and did not violate the source cap. Sibling, season, series, and
stale-fingerprint observations cannot authorize the item.

When an inherited ancestor/default target is below that compatible exact-item
minimum, production returns the typed
`exact_item_target_below_quality_safe_minimum` blocker. It does not widen the
target, rewrite an ancestor override, or relax the quality floor. If no compatible
measured minimum exists, the provenance remains visible but no minimum is invented.

`qsh2` supersedes `qsh1` evidence for current readiness. Historical `qsh1` rows
remain available for audit and safety reporting, but they cannot satisfy current
recommendation, benchmark, distinct-item, or cluster-readiness thresholds. A
deployment of `qsh2` therefore intentionally restarts passive qualification.

The shadow payload also exposes `analysis_family_id` for reporting only. It is a
separate `qaf1_` namespace derived from broad context categories: compression
intent, encoder family and preset, pixel/bit-depth and resolution/cadence
buckets, metric/floor policy, and coarse filter/grain categories. It omits exact
target bytes/bitrate, patch versions, and full stream-plan facts. It never enters
`QualitySearchWarmStart`, never replaces the exact search signature or group
key, and cannot authorize execution.

The measured comparison records candidate count, search wall time, eventual CRF,
quality margin, size error, within-one-CRF accuracy, fallback need, and projected
candidate/time savings. The projection models the planned warm-start contract:
a within-one hint replaces the baseline search with one first-candidate probe;
a miss adds one probe before the unchanged full fallback. Negative savings are
retained rather than hidden.

Aggregate shadow metrics report recommendation coverage, within-one hit rate,
false-narrow rate, fallback need, median projected candidate/time savings, and
safety outcomes. Performance thresholds require at least ten recommendations,
at least 70% within-one accuracy, at least 20% median candidate and time savings,
and zero measured quality-floor or active-attributable final-size violations. Those
thresholds authorize only the isolated measured probe described below; they do
not authorize bound narrowing, target changes, or selector changes. The safety
counters keep baseline final-size health separate from failures after a warm
probe actually ran. Signatureless legacy failures remain visible as
unattributed data-quality facts but cannot be assigned to a current cohort by
metric and objective alone. Active rows do not replace the passive benchmark;
their safety facts remain visible while passive recommendation projections and
concurrent holdouts continue to determine whether a cohort is eligible.

## Future Warm-Start Study

The generic warm-start engine and exact execution contracts remain available for
a future isolated study, but ordinary production encoding is passive. The
quality-memory plan always has `execution_mode=passive`, `experiment_arm` is
`passive` or `ineligible`, and `search_hint()` returns no hint. Production search
does not receive either a warm-start hint or expected warm-start signature from
the plan.

When a plan is otherwise eligible, it records a future assignment instead. The
assignment uses normalized source path plus source fingerprint/content version,
not a search-run ID, so retries of the same item/version stay in one future arm.
The configured holdout percentage remains unchanged. Existing exact execution
compatibility must still be satisfied before any separately authorized study.

A future active warm start would be a single optimization probe in front of the
existing search and would require all of these conditions:

- a pre-search context can be built from the same resolved metric, target,
  quality floor, preset, encoder parameters, video filter, container, and
  deterministic output dimensions used by the final command signature
- configured minimum and maximum CRF bounds are available for deterministic
  rounding and clamping
- evidence is runtime-native or corrected history with the exact current video
  policy hash; unhashed staged backfill cannot authorize active behavior
- the current item, season, or series recommendation still passes sample-count
  and dispersion checks at the search-start evidence cutoff
- that exact media scope, search signature, and policy has at least ten passive
  recommendations, at least 70% within-one accuracy, at least 20% median
  candidate and search-time savings, and zero quality-floor or prior active
  final-size violations
- at least ten compatible passive rows carry usable paired candidate-count and
  search-duration benchmarks for honest median savings estimates
- the median CRF can be rounded and clamped to the configured CRF range without
  moving more than one CRF point

Quality-only searches run one `ab-av1 sample-encode` at the remembered CRF with
the original samples, preset, encoder settings, and transform. The candidate is
accepted only when that measurement meets the original strict quality target
and encoded-size cap. A failure or rejected measurement is discarded before the
normal target-relaxation and CRF-bound sequence starts with fresh state.
`ab-av1 crf-search` is not used for this probe because it rejects identical
minimum and maximum CRF bounds; `sample-encode` is its native single-CRF
measurement surface.

Target-size searches measure the hint in isolation and accept it only through
the existing target-band, quality-floor, and source-cap selector. A miss is not
added to the six-candidate curve or retry brackets; the normal target-size
search starts fresh. If a warm-selected final output misses its final size band,
Mediaforce removes that speculative output, runs the full baseline search from
fresh state, and then resumes the existing final verification and bounded retry
path.

Warm-selected observations remain append-only audit facts but are marked
learning-ineligible and are excluded from historical staged backfill. This
prevents recommendations from training on their own decisions. Outcomes selected
by the unchanged full fallback remain eligible because the baseline search was
still authoritative. A future study uses the recorded stable item/version
assignment for its unchanged full-search holdout.
Holdouts remain learning-eligible, preserve a current baseline, and prevent the
post-activation evidence set from containing only warm-start failures.

The immutable shadow payload carries a separate active block with eligibility,
experiment arm, requested and attempted CRF, probe status, fallback reason,
total candidate work, and estimates against the pinned passive baseline. The
observation timing payload also records one end-to-end workflow duration from
quality-search start through terminal encode outcome so nested fallback searches
or retry measurements are not double-counted. Passive projected savings are
never presented as observed active savings.

## Acceptance Evidence

`mediaforce quality-memory` is the read-only acceptance report. `--json`
returns the same stable typed payload used by tests, and `--prefix` restricts
the report to one library path. The stable group identity is scope kind, scope
prefix, search signature, and policy hash. Cohort IDs are intentionally not
group keys because their evidence-observation membership changes over time.

Each group keeps three sections separate:

- passive readiness: recommendation coverage, accuracy, projected savings,
  benchmark completeness, and authorization blockers
- active observed: assigned and attempted warm runs, accepted probes,
  fallbacks, holdouts, and medians for candidate count, search duration, and
  end-to-end workflow duration
- safety and data quality: passive and active quality-floor violations,
  baseline and active-attributable final-size misses, and report-level
  signatureless failures

The passive section also reports distinct passive units, distinct items, natural
series/season/film-root clusters, and largest-cluster concentration. These are
dependence diagnostics only and never gate eligibility. Acceptance reporting
also carries global operator outcomes from current authoritative content-intent
boundary observations: rejection means current unacceptable/rejected visual
evidence; additional attention conservatively includes rejection, correction,
withdrawal, or approved evidence with concern tags. These outcomes stay global
when exact quality-group attribution is unsafe.

The issue #256 completion protocol is fixed before active evidence is reviewed:

- one exact scope, signature, and policy group must remain passively eligible
- at least 20 warm-arm attempts on 20 distinct items and ten full-search
  holdouts on ten distinct items must complete with candidate-count,
  search-duration, and end-to-end workflow telemetry
- observed median candidate-count, search-time, and end-to-end workflow savings
  against the concurrent holdout must each be at least 20%
- warm-arm medians include only attempted probes that either succeeded or
  reached a confirmed full-search fallback, with complete telemetry
- active quality-floor and final-size violations must remain zero
- every rejected or failed probe must preserve the unchanged full-search
  fallback; terminal, signature-mismatched, legacy-unlabeled, or unclassified
  warm runs block completion, as do `encoding_failed` events carrying the
  current warm-arm plan identity

These are operational acceptance thresholds, not a claim of inferential
statistical significance. Only rows carrying the exact current experiment
version and arm enter observed treatment/control medians; older active rows
remain visible as safety evidence.

The report is production evidence, not a deterministic repository quality gate;
it is not wired into CI and does not write an aggregate table or checked-in
runtime artifact.

## Narrowed-Window Decision

Issue #262 remains an evidence-gated evaluation. Historical replay may report
selected-CRF capture, in-window selectable candidates, edge hits, unknowns, and
fallback rate, but it must not claim replayed wall-time savings because
per-candidate sample durations are not persisted. Active narrowed bounds remain
out of scope until at least 30 compatible comparisons show material incremental
benefit over the single-candidate probe.

On July 25, 2026, an isolated `ab-av1 0.11.3` smoke test found CRF 41 with a
wide 10–55 range. Ranges 10–20 and 45–55 both exited nonzero with `Failed to find
a suitable crf`, confirming that an unsatisfied constrained search fails rather
than silently selecting a boundary. This verifies the tool contract only; it
does not justify implementing narrowed bounds.

## Folder Studio Projection

Folder Studio reads the newest current selected observation inside the active
media scope when it is either learning-eligible or an audited warm-start
selection, then projects its immutable shadow record. The
surface keeps the production result and the counterfactual recommendation
separate: chosen CRF, measured quality, final size, candidate count, and search
time describe what actually ran; the shadow first CRF, evidence scope, sample
count, confidence, dispersion, and comparison describe what memory would have
tried first.

When no recommendation was safe, Folder Studio shows the stored typed fallback
as sparse, stale, or conflicting evidence instead of recomputing guidance in the
web layer. A folder with no shadow-bearing observation gets a compact empty
state. Every state says that the evidence is observation-only: quality floors,
saved policy, production search order, bounds, and fallback behavior remain
unchanged. For active runs it instead says whether the first candidate passed or
was discarded before the full baseline fallback, while keeping measured output
facts separate from historical guidance.

## Cohorts And Confidence

Compatible outcomes are evaluated in this order:

1. exact item, with a matching source fingerprint
2. season
3. series

The first cohort with enough evidence and bounded dispersion supplies the
central CRF hint. Exact-item and season cohorts require at least four accepted
outcomes; series cohorts require at least six. A hint also requires an
interquartile range no greater than 4 CRF points and median absolute deviation
no greater than 2 CRF points.

Confidence is limited for one to three outcomes, moderate for four to nine,
and high for ten or more. Series evidence is capped at moderate confidence
because it is broader than season or item evidence. Global history exposes only
aggregate metric evidence counts and never produces CRF guidance.

## Current Limitations

- `load_quality_memory` still reads the latest accepted staged artifact per item;
  active qualification and shadow inference use the append-only log.
- full validation replaces the encode-time validation payload, so older staged
  rows may not retain target-size search traces.
- historical backfill can reconstruct only accepted successes and cannot recover
  unavailable policy hashes, search wall time, or ambiguous historical failures;
  those rows remain passive-only.
- pre-search dimension identity is intentionally unavailable when FFmpeg must
  resolve an aspect-ratio scale width, so those searches continue through the
  full baseline path.
