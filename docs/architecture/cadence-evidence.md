# Cadence Evidence and Safe Transforms

## Ownership

- `mediaforce/encoding/cadence.py` owns bounded cadence analysis, idet parsing,
  deterministic classification, evidence shaping, and the symbolic transform
  allow-list.
- `mediaforce/library/probe.py` records ffprobe stream metadata and runs bounded
  idet ranges only when frame order is not already known to be progressive.
- `mediaforce/library/planner.py` binds persisted cadence facts to the source
  fingerprint, cadence transform policy, and versioned evidence envelope carried by each
  manifest item.
- `mediaforce/encoding/video_filters.py` is the only place that compiles a
  resolved cadence transform into an ffmpeg filter graph.

## Evidence contract

Cadence evidence version 1 persists:

- ffprobe field order, average and nominal frame rate, and time base
- bounded idet ranges, frame limits, measured frame counts, and tool identity
- progressive, TFF, BFF, repeated-field, and undetermined counts
- classification, confidence, coverage, rationale, and transform identity

The manifest evidence ID includes the source fingerprint, cadence transform
policy, measurement ranges, tool version, and derived decision. A source,
transform-policy, or tool change therefore produces a different identity rather than
silently reusing stale cadence facts.

## Classification and gates

The deterministic classifier emits `progressive`, `tff`, `bff`, `telecine`,
`mixed`, or `unknown`. High-confidence progressive, interlaced, and telecined
results compile to one of four symbolic plans:

- `none`
- `bwdif_tff`
- `bwdif_bff`
- `fieldmatch_decimate`

Only those IDs can become filter graphs. Raw policy or model-generated filter
strings are never accepted.

Mixed, unknown, low-coverage, and low-confidence results block sample search,
bakeoff, and production before ffmpeg starts. The operator-visible error asks
for a fresh scan or more evidence; the LLM cannot choose a cadence transform.
Sampling, preview clips, bakeoff plans, and production all call the same filter
compiler, so the reviewed transform cannot drift before the final encode.

New manifests built from catalog rows without cadence evidence remain blocked
until those rows are reprobed. Already-written legacy manifests that predate the
cadence contract remain runnable; rebuilding them opts them into the evidence
gate instead of silently treating unknown material as progressive.
