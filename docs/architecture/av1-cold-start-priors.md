# AV1 Cold-Start Recommendations

Mediaforce may recommend one better first AV1 calibration probe, then lets the
existing measured target-size search accept or reject it. A recommendation is
an optimization hint, not encode authority.

## Product Flow

The live path is:

1. Resolve explicit compression intent and the whole-item size goal.
2. Measure source and stream facts.
3. Read compatible accepted local outcomes and the shipped public bundle.
4. Return one bounded first-probe recommendation or a no-recommendation reason.
5. Run the normal measured target-size search with unchanged quality and size
   safeguards.
6. Fall back to the configured full search when the first probe is missing,
   incompatible, rejected, or unsuccessful.
7. Let the operator compare and approve the measured result before it becomes
   reusable local evidence.

The recommendation never changes target bytes, quality floors, stream budgets,
resolution, cadence, transforms, audio, subtitles, promotion, or saved policy.

## Evidence Sources

The predictor keeps public and local evidence separate:

- `mediaforce/tuning/data/av1_cold_start_priors_v1.json` is the checked-in
  aggregate bundle. It currently remains `validation_pending` with no active
  cells, so a new installation receives `no_public_evidence` rather than an
  invented recommendation.
- The runtime database contains append-only accepted and rejected operator
  outcomes. Compatible local evidence may recommend a first probe without
  modifying the checked-in bundle.

Missing, stale, low-confidence, incompatible, conflicting, future-dated, or
out-of-range evidence returns no recommendation. Full measured search remains
available in every case.

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
facts and compatible memory. It cannot invent numeric authority or bypass the
search engine. The recommendation payload stays schema-bound and records its
source, confidence, compatibility, and fallback reason.

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
