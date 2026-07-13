# Stream Budget and Feasibility Ledger

## Ownership

- `mediaforce/encoding/streams.py` owns the immutable production stream plan:
  selected audio, subtitles, and attachments plus copy, transcode, or drop
  decisions and output codec/bitrate details.
- `mediaforce/tuning/stream_budget.py` prices that plan against the canonical
  per-item size goal. It is the only authority for non-video overhead,
  remaining video bytes/bitrate, uncertainty, and arithmetic feasibility.
- `mediaforce/library/planner.py` persists the plan and ledger on manifest
  items. Queued sample and production jobs carry the same identity-bound
  payload rather than recomputing stream choices later.
- `mediaforce/encoding/quality_search.py` hands ledger-backed work to
  `mediaforce/tuning/target_size_search.py`, which samples CRF candidates
  against the remaining video budget without changing transforms, caps, stream
  choices, or quality floors.
- `mediaforce/encoding/commands.py` compiles the exact persisted stream plan
  into ffmpeg mappings and codec arguments.
- `mediaforce/encoding/manifest.py` verifies the actual final output against
  the resolved final-output band and records any bounded retry or needs-review
  outcome.

## Contract

The versioned ledger is bound to the source ID and fingerprint, resolved policy
hash, canonical size goal, output container, and production stream-plan ID. A
consumer must reject a ledger whose source, policy, target, container, or stream
plan no longer matches the current item.

Each audio, subtitle, attachment, and container entry records:

- selected source stream and production action
- output codec and bitrate when applicable
- estimated bytes plus lower and upper bounds
- provenance, confidence, and whether measurement is still required
- a plain-language rationale for exact values or fallbacks

User-facing MB remains decimal. Canonical arithmetic uses integer bytes.

## Feasibility

The ledger distinguishes four deterministic states:

- `feasible`: a positive video budget remains with sufficiently bounded
  non-video costs
- `aggressive_but_measurable`: the remaining video budget is positive but low
  enough that measured search is required
- `arithmetically_infeasible`: even the minimum production stream plan leaves
  no positive video budget
- `requires_measurement`: an unknown stream cost or missing runtime prevents a
  trustworthy total

Arithmetic infeasibility is never delegated to an LLM. Quality risk remains a
separate measured outcome for target-size search and operator review.

`stream_budget_projection_blocker()` exposes the same deterministic arithmetic
for planning surfaces. Movie candidate projection and CLI/web start actions use
that result before creating sample jobs, manifests, or encode jobs. Sample
confirmation checks the proposed policy, while production queueing checks the
accepted calibration policy through an in-memory override before persisting it.
A target whose lower sample-band bound is above the configured source-relative
cap is a hard workflow blocker; `requires_measurement` remains available for
measured review instead of being treated as impossible.

## Target-size search

The first representative sample is seeded from the approved whole-episode size
goal after the ledger subtracts production audio, subtitle, attachment, and
container bytes. The search records a typed trace containing:

- sampled clip bytes when the sample engine reports them
- predicted whole-episode video bytes
- predicted whole-episode total bytes after non-video ledger bytes are added
- the selected CRF, measured quality score, quality floor, sample target band,
  source cap, ledger identity, stream-plan identity, and transform-plan identity

The search may only choose among CRFs inside the approved policy range. It does
not relax max size caps, lower quality floors, pick cadence transforms, change
stream selection, or rewrite an operator's size goal. Monotonic curves select a
candidate inside the sample band when one exists. Arithmetic impossibility,
quality-floor conflict, and exhausted or noisy non-monotonic searches surface as
structured infeasibility, quality-conflict, or needs-review outcomes.

Production encodes verify actual output bytes against the resolved final band
from the same size goal, currently ±5% by default. A final miss can retry only
once. The completed output calibrates the sample curve's video-byte projection;
an already measured candidate may be reused when that corrected projection is
inside the final band. Otherwise Mediaforce may interpolate one integer CRF
between measured quality-safe candidates and run one targeted sample at that
CRF. The replacement full encode starts only when the new sample supplies a real
quality score that meets the configured floor, remains under the
source-relative cap, and its calibrated total projection is inside the final
band. If no bounded measured retry is available, or if the retry budget is
exhausted, the item enters a needs-review failure state rather than falling back
to quality-first encoding or silently relaxing the approved constraint.

## Fallbacks and migration

When container overhead is not measured, reserve the greater of 4,000,000
bytes or one percent of the total target. Unknown copied stream sizes use
explicit bounded fallbacks and keep `requires_measurement` visible.

Legacy manifests without a ledger remain runnable through the compatibility
stream selector. Newly planned or retried work receives a ledger and must use
its production stream plan consistently across sample search, API payloads,
queued jobs, command construction, and final validation.
