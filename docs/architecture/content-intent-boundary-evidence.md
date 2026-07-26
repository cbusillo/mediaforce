# Content-Intent Boundary Evidence

Mediaforce preserves explicit visual approvals and rejections as immutable local
evidence about the size boundary that an operator accepted for measured content
under a confirmed compression intent. The evidence supports replayable local
personalization without granting authority to inferred history, failed work, or
mutable model state.

## Authority boundary

A boundary observation is eligible only when all of these facts come from the
same completed sampled-calibration review:

- the current library item and content-version fingerprint
- a measured multi-label media fingerprint and its evidence ID
- a confirmed versioned compression intent
- the validated stream-budget ledger for the same source and policy
- the sample job ID, decision-time SHA-256 identity of the encoded and source
  review clips, measured quality result, sampled bytes, projected whole-item
  bytes, and authoritative target bytes
- the encoder, encoder runtime, quality tool, preset, pixel format, dimensions,
  frame rate, cadence transform, filters, grain parameters, metric, and
  container used for the reviewed artifact
- an explicit operator approval or rejection

The runtime records approvals from `save_profile_action` and rejections from
the explicit post-test quality-risk feedback path. Both producers call the same
contract builder and append API.

Canceled or stopped jobs, schedule closure, stale leases, storage or transport
failures, missing review media, full-run calibration payloads, stale source or
policy ledgers, unmeasured fingerprints, incompatible toolchains, and
unconfirmed legacy intent return an exclusion reason and append no observation.
No historical row is relabeled or backfilled.

## Identities and compatibility

The observation separates four kinds of identity:

1. `source_id` identifies the local library item.
2. `content_id` identifies the current content-version fingerprint, while
   `content_profile_id` hashes the measured multi-label trait set.
3. `intent_semantic_id` identifies the confirmed optimization objective;
   `intent_snapshot_id` preserves the exact frozen authority snapshot.
4. `compatibility_key` hashes the technical measurement contract. It includes
   encoder/runtime and quality-tool versions, hashed FFmpeg build and metric
   implementation signatures, preset, pixel format, encoder and grain
   parameters, output dimensions, frame rate, cadence/filter plan, container,
   source-independent stream plan, measurement basis, metric, target, and
   floor.

Replay combines `content_profile_id`, `intent_semantic_id`, and the technical
`compatibility_key` into `model_compatibility_id`. This is the effective local
model compatibility key: content features and intention never disappear into a
generic encoder cohort. Requested bytes, measured/projected bytes, CRF, job IDs,
artifact IDs, paths, and full policy hashes stay out of the technical key so
they remain observations rather than accidental cohort dimensions.

## Append-only corrections

`content_intent_boundary_observations` is a versioned append-only SQLite log.
Update and delete triggers reject mutation. A correction must:

- supersede the current active row in the same series
- advance the revision by exactly one and advance the UTC timestamp
- preserve source, content, intent, technical compatibility, policy, job,
  artifact, evidence IDs, and all measured boundary facts
- change only correction-authorized assessment, disposition, eligibility, or
  provenance fields

A unique predecessor index prevents forks. Withdrawal is a correction row with
`disposition=withdrawn`; history remains auditable. Deterministic payload hashes
and IDs make producer retries idempotent, and replay rejects rows whose stored
hashes do not reconstruct.

The migration can downgrade while the table is empty. Once evidence exists it
refuses downgrade rather than silently discarding immutable operator evidence.

## Replay and local personalization

Replay collapses every series to its highest revision before filtering active,
eligible, hash-valid rows. It then derives four nested local scopes:

- item: exact source and content version; one valid boundary can remain an
  item-local exception
- folder: compatible content profiles under the same folder prefix
- content class: the same measured multi-label profile across folders
- operator: all local evidence for the same profile, intent, and technical
  contract

An approval is an upper bound on the unknown minimum acceptable total size. A
rejection is a lower bound. The derived bitrate posterior uses projected AV1
video bytes rather than total bytes, so copied audio and attachment size do not
pollute the encoder starting point. Audio-only rejection feedback is excluded.
Crossing bounds are reported as conflicting and are not actionable. Broader
scopes require at least three independent source IDs and bounded dispersion
before becoming actionable; repeated observations from one item cannot unlock
a folder, class, or operator prior.

The replay result is advisory starting-point state for the bounded prior work.
It does not authorize size growth, alter quality floors, or bypass measured
search. Combining this private local posterior with a shipped cold-start prior
and activating a first-probe recommendation belongs to the separate cold-start
predictor contract.

## Privacy and storage

All observations stay in the configured runtime database outside the
repository. They may contain local relative paths and hashed local identities,
but never raw operator notes, media bytes, review clips, or automatic
cross-user exports. Review media remains in the configured runtime review
directory and is represented only by a composite SHA-256 identity in the
observation. The identity is recomputed from retained source and encoded clips
only when an explicit approval or rejection is being recorded.

## Validation contract

Coverage includes schema and migration parity, append-only triggers, correction
linearity, timestamped time travel, withdrawal replay, deterministic retry,
stream-budget and compatibility staleness, approval and rejection producers,
toolchain capture, review artifact fingerprints, multi-label and unknown
content, independent-source confidence, item exception isolation, privacy, and
the stopped-work exclusion path.
