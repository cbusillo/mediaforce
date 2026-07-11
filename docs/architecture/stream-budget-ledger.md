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
- `mediaforce/encoding/quality_search.py` consumes the ledger's source-relative
  video cap after non-video overhead has been removed.
- `mediaforce/encoding/commands.py` compiles the exact persisted stream plan
  into ffmpeg mappings and codec arguments.

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

## Fallbacks and migration

When container overhead is not measured, reserve the greater of 4,000,000
bytes or one percent of the total target. Unknown copied stream sizes use
explicit bounded fallbacks and keep `requires_measurement` visible.

Legacy manifests without a ledger remain runnable through the compatibility
stream selector. Newly planned or retried work receives a ledger and must use
its production stream plan consistently across sample search, API payloads,
queued jobs, command construction, and final validation.
