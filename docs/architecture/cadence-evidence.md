# Cadence Evidence and Safe Transforms

## Ownership

- `mediaforce/encoding/cadence.py` owns bounded cadence analysis, idet parsing,
  deterministic classification, evidence shaping, and the symbolic transform
  allow-list.
- `mediaforce/library/probe.py` records ffprobe stream metadata and runs bounded
  idet ranges only when frame order is not already known to be progressive.
- `mediaforce/library/scanner.py` preserves cadence summaries while reconciling
  unchanged catalog rows. Missing, malformed, old, or retryable cadence
  remains non-current evidence and does not make routine inventory refresh
  decode the source again.
- `mediaforce/library/evidence_state.py` projects canonical cadence JSON into
  independently queryable current, analysis-required, or
  classification-required state without changing the payload.
- `mediaforce/library/planner.py` binds persisted cadence facts to the source
  fingerprint, cadence transform policy, and versioned evidence envelope
  carried by each manifest item.
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
transform-policy, or tool change therefore produces a different identity
rather than silently reusing stale cadence facts.

## Classification and gates

The deterministic classifier emits `progressive`, `tff`, `bff`, `telecine`,
`mixed`, or `unknown`. High-confidence progressive, interlaced, and telecined
results compile to one of four symbolic plans:

- `none`
- `bwdif_tff`
- `bwdif_bff`
- `fieldmatch_decimate`

Progressive is judged against determined frames: at least 95% of the frames
idet could classify must be progressive, and at least 80% of the sample must be
determined. idet reports a frame as undetermined when it lacks field detail,
which includes the first frames of each sampled range while its multi-frame
state warms up, so undetermined frames are not evidence of interlacing.
Confidence still counts every sampled frame. Interlaced, telecine and mixed
rules are unchanged.

Only those IDs can become filter graphs. Raw policy or model-generated filter
strings are never accepted.

Mixed, unknown, low-coverage, and low-confidence results block sample search,
bakeoff, and production before ffmpeg starts. The operator-visible error asks
for refreshed cadence analysis or more evidence; the LLM cannot choose a
cadence transform.
Sampling, preview clips, bakeoff plans, and production all call the same filter
compiler, so the reviewed transform cannot drift before the final encode.

Folder, season, older-season, and recovery actions partition their candidate
set before writing a run manifest or requeueing failed children. Only items with
current, resolved cadence evidence enter production; measured blockers and items
that still need cadence evidence remain original, get their analysis queued, and
are named one by one in the action's `left_out` list with a plain reason. The
action is refused only when no selected item is cleared, so one file's cadence
problem never holds its siblings. Each held item also gets a row in
`production_holds` naming the scope, the queue mode (folder, season override,
or older seasons) and the approval it was held under. The web app's
`held-files-worker` queues held items whose evidence has since cleared as a
separate run under that same approval once no encode is active for the scope,
without clearing or retrying the scope's earlier jobs. If the approval changed,
or the queue refuses the files outright, nothing is queued and the hold keeps
that status so the files stay listed. Sample actions keep their hard blocker because
they select exactly one item.

New manifests built from catalog rows without cadence evidence remain blocked
until cadence analysis is refreshed. Already-written legacy manifests that
predate the cadence contract remain runnable; rebuilding them opts them into the
evidence gate instead of silently treating unknown material as progressive.
