# AV1 Cold-Start Recommendations

Mediaforce computes a bounded AV1 calibration recommendation as passive
telemetry. It does not currently enter the measured target-size search. The
recommendation remains an optimization hypothesis, never encode authority.

## Product Flow

The current live path is:

1. Resolve explicit compression intent and the whole-item size goal.
2. Measure source and stream facts.
3. Read compatible accepted local outcomes from the runtime database.
4. Return one bounded first-probe recommendation or a no-recommendation reason.
5. Record explicit passive execution evidence and run the normal measured
   target-size search with unchanged quality and size safeguards.
6. Let the operator compare and approve the measured result before it becomes
   reusable local evidence.

The recommendation never changes target bytes, quality floors, stream budgets,
resolution, cadence, transforms, audio, subtitles, promotion, or saved policy.

## Evidence Source

The predictor uses only append-only accepted and rejected operator outcomes
from the runtime database. The observation loader filters by the request's
content-intent compatibility identity before replay, and the planner further
restricts rows to the requested item, content profile, or selected local
cohort. No public prior, checked-in bundle, or package resource participates in
the recommendation.

The planner evaluates item, folder, content-class, and operator scopes from
narrow to broad. Each scope independently has to pass freshness and versioned
timestamps, conflict checks, confidence/actionability, CRF completeness, target
compatibility, dispersion, and configured candidate bounds. A weak or stale
narrow cohort therefore cannot suppress an eligible broader cohort. Missing,
stale, low-confidence, incompatible, conflicting, future-dated, or out-of-range
evidence returns a readable no-recommendation reason. Full measured search runs
in every case.

## Recommendation Inputs

Recommendations may use deterministic, measured inputs such as:

- compression intent
- target video bitrate and allowed CRF range
- encoder, preset, pixel format, bit depth, dimensions, cadence, and filters
- animation, darkness, motion, texture, and grain/noise observations
- stream-budget constraints
- compatible accepted local outcomes

Names, genres, eras, and folder paths may provide operator context but cannot
replace measured facts or independently choose a transform.

Animation and grain/noise are priors, not universal rules. Animation may often
support a more aggressive first probe, but measurement decides. Grain/noise may
consume substantial bitrate or obscure compression damage, but Mediaforce does
not automatically denoise or remove grain. Any cleanup treatment requires a
separate measured comparison and explicit operator approval; uncertain evidence
defaults to preservation.

## Advisor Boundary

The LLM-backed advisor may rank and explain one candidate from deterministic
facts and compatible local memory. It cannot invent numeric authority or bypass
the search engine. The recommendation payload stays schema-bound and records
its local source, confidence, compatibility, fallback reason, bounded
narrow-to-broad scope-trial diagnostics, and passive execution evidence.

Accepted and rejected outcomes remain explicit records rather than hidden
mutable model state. This keeps later recommendations explainable and allows
the operator to understand why Mediaforce started at a particular probe.

## Success Measures

Future tuning work should measure ordinary product outcomes:

- first-probe size-band and quality-floor hit rate
- search candidates and wall time saved
- automatic full-search fallback rate
- final-size misses
- quality-floor violations
- operator rejection and additional-attention rate

Narrower CRF bounds are not justified until first-probe recommendations prove
useful on compatible repeated work. Diagnostic failures must retain readable,
actionable reasons; immutable failure proof is not a substitute for operator
diagnosability.
