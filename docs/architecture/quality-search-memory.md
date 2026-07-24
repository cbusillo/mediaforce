# Quality-Search Memory

## Purpose

Quality-search memory turns accepted encode outcomes into explainable historical
context. Its first phase is deliberately read-only: it can report a central CRF
hint, confidence, dispersion, and invalidation reasons, but it cannot change an
encode policy, search bound, quality floor, transform, stream plan, or runtime
decision.

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
rerun. Schema migration remains independent of application extraction code; a
failed backfill is logged and retried at the next startup. A later native row
for the same run wins during current-revision resolution without mutating the
historical row.

The table is database-enforced append-only: updates and deletes are rejected.
Corrections append a complete successor with the same run identity, the next
revision, a predecessor pointer, and a machine-readable reason. Library-item
deletion does not cascade into this audit history; item identity and path are
copied into every observation.

This phase remains passive. `load_quality_memory` continues to use the existing
accepted staged-outcome path, and observations do not alter search ordering,
bounds, targets, policy, or fallback behavior until shadow evaluation is
completed in a later phase.

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
  the append-only log is not yet an active guidance source.
- full validation replaces the encode-time validation payload, so older staged
  rows may not retain target-size search traces.
- historical backfill can reconstruct only accepted successes and cannot recover
  unavailable policy hashes, search wall time, or ambiguous historical failures.

Shadow recommendations, operator explainability, and active warm starts remain
separate later phases. Passive evidence must prove useful before any encode
behavior changes.
