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

New observations also copy `recorded_at` into hash-bound provenance. The public
cold-start predictor requires that copy to match the row timestamp before local
evidence can pass its freshness gate; older rows remain replayable but do not
gain newly invented timestamp authority.

The migration can downgrade while the table is empty. Once evidence exists it
refuses downgrade rather than silently discarding immutable operator evidence.

## Replay and local personalization

Replay collapses every series to its highest revision before filtering active,
eligible, hash-valid rows. It then derives four nested local scopes:

- item: exact source and content version; one valid boundary can remain an
  item-local exception
- folder: compatible content profiles under the same folder prefix
- content class: the same measured multi-label profile outside the current
  folder, so the evidence is genuinely cross-folder
- operator: all local evidence for the same profile, intent, and technical
  contract

An approval is an upper bound on the unknown minimum acceptable total size. A
rejection is a lower bound. The derived bitrate posterior uses projected AV1
video bytes rather than total bytes, so copied audio and attachment size do not
pollute the encoder starting point. Audio-only rejection feedback is excluded.
Crossing bounds are reported as conflicting and are not actionable. Broader
scopes require at least three independent acceptable source IDs and bounded
dispersion before becoming actionable; rejection-only sources and repeated
observations from one item cannot unlock a folder, class, or operator prior.

The replay result is advisory starting-point state for bounded cold-start work.
It does not authorize size growth, alter quality floors, or bypass measured
search. `docs/architecture/av1-cold-start-priors.md` defines how this private
local evidence can provide one measured first-probe hint or an explicit
no-recommendation outcome.

## Target-default proposals

`mediaforce target-defaults <observation-id>` emits a read-only JSON report from
the configured database. The reference must be a current, eligible, hash-valid
visual boundary. Its source/content version, confirmed intent, technical
compatibility, and measured overlapping traits define the report context.
Superseded, withdrawn, quarantined, incompatible, or corrupted rows cannot vote.
The command uses the read-only database path before runtime locking, migration,
cleanup, or scheduling. It does not inspect media or change configuration.

Rule version 2 is a conservative proposal policy, not an empirically calibrated
population default. It uses total boundary bytes (including retained audio and
attachments), normalized to 2,700 seconds using decimal bytes. It never consumes
the video-bitrate or CRF posterior. Each source contributes its smallest
quality-safe visual approval; the proposal is the largest of these per-source
bounds, rounded up to a whole byte. Repeated reviews cannot weight a source more
heavily. The original authoritative target is preserved separately in the report.

| Scope | Minimum approved sources | Approved artifacts | Rejected sources | Approved folders | Maximum relative spread |
| --- | ---: | ---: | ---: | ---: | ---: |
| Exact content version | 1 | 2 | 0 | 1 | 25% |
| Same folder and measured profile | 3 | 3 | 0 | 1 | 25% |
| Same measured profile across folders | 8 | 8 | 3 | 3 | 10% |

Target-default scopes are nested: content class includes the current folder and
item, while its folder-count gate requires cross-folder support. Folder and
class evidence must have runtime between 80% and 125% of the reference runtime;
this limits extrapolation of total bytes, including fixed non-video overhead.
Invalid nonpositive sizes or nonfinite/nonpositive durations cannot vote, and
the report counts excluded measurements and runtimes. These are separate target-intent gates; the existing CRF prior thresholds are
unchanged. Folders are the parent directories of source-relative paths, not
review prefixes, which may identify an exact file. Artifact counts use distinct
review fingerprints. Dispersion is the
full range divided by the median of per-source approved bounds; item scope uses
all of its approved review sizes. Any normalized rejection at or above the
smallest approved bound is a conflict and prevents that scope's proposal.
An item conflict prevents broader fallback, and a broader proposal cannot cross
an exact item's rejected lower boundary. Rejection-only evidence cannot supply a
target. There is no operator-wide target fallback or title/genre classification.

The report selects the narrowest passing scope and exposes the rule, observation
IDs, a snapshot binding the rule version, thresholds, reference and scope
partition, independent counts, dispersion, confidence, and failure
reason for every scope. Moderate/high confidence labels mean that these rule
thresholds passed; they are not statistical probabilities or visual guarantees.
When no scope passes, it reports `no_supported_default_keep_reference_target`
(or an explicit item conflict) and proposes no new target.

Reports are retrospective and always `review_only`, with
`production_authority=unverified`. Sample approvals do not establish the parent
plan's approved, in-band, promoted production-outcome authority. Applying a proposal still
requires checking the current source, intent, policy and stream budget, explicitly
confirming the new target, and testing a representative sample. The command
does not claim the reference's historical target is the current configured
default. Studio shows this evidence for exact TV, Movie, and Other items only after
matching the current calibration, source/content, intent, policy, technical
compatibility and stream ledger. Stale or missing context exposes an unavailable
reason. The disclosure preserves the current target and offers no apply or queue
action. Automatic adoption and empirical threshold calibration remain separate
work; no production setting changes from a report.

## Production outcome lineage

`mediaforce target-production-evidence <observation_id>` reports a separate,
read-only production evidence contract. It does not change target proposals,
confidence thresholds, settings, queue admission or automatic adoption. The
empirical calibration and operator acceptance work remains outstanding.

After existing queue admission gates pass, only the manifest item matching the
current approved sample may receive a versioned, hashed lineage capsule. A
folder approval cannot give its siblings this evidence. Duplicate matches,
stale source/intent/policy/stream budgets, unavailable review context and
unapproved samples produce no capsule. The capsule binds the boundary hash,
sample job and review artifact, approved CRF, technical compatibility, operator
size contract, approval time, manifest run and item index.

Linked encodes capture source identity and the actual execution host's encoder
and quality toolchain before and after production. The completed lineage also
retains the command-derived compatibility and actual selected CRF. Missing or
changed identities make the result ineligible. Advisory capture failure does
not discard an encode; the normal encoding process controls still apply.
Every re-encode replaces the staged lineage, including clearing it for unlinked
work, and clears previous validation and promotion fields.

Validation binds the staged bytes after any successful container repair.
Promotion prepares the evidence payload before moving files, then appends a
receipt within the same database transaction as the promoted item and staging
state. A receipt failure rolls back that transaction and invokes the existing
filesystem restoration. Identical receipt retries are idempotent; conflicting
payloads fail. SQLite triggers reject receipt updates and deletes. Receipt
history has no cascading library-item foreign key; removing current catalog
state revokes eligibility without erasing the historical record. Migration
adds nullable staging lineage and an empty receipt table, with no backfill.

Eligibility is re-proved at report time against the current global boundary
revision and current catalog/staging state. It requires matching source and
technical context, the same CRF as the approved sample, quality-floor success,
ordered approval/encode/validation/promotion timestamps, successful validation,
and actual bytes strictly within the approved final size band. An under-target
output accepted by production's size contract is still excluded from positive
target-learning evidence. Withdrawals, corrections, re-encodes, changed output
metadata or replaced staged evidence revoke eligibility without rewriting the
receipt.

Output continuity uses `content_version_sampled_sha1_v1`: size plus sampled
head, middle and tail bytes, together with modification time. This is a bounded,
path-independent identity compatible with promotion moves, not a full-file
cryptographic digest or proof against unsampled same-size edits. Read-only
reports consult the current catalog identity; they do not rescan media files.
Receipt and capsule hashes detect accidental mutation, not malicious rewriting
by an actor able to rewrite the database. These limits remain visible in the
JSON report. Eligible receipts are evidence candidates, not a claim that
production-derived defaults have been empirically calibrated.

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
