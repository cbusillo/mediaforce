# Quality-Risk Contract and Review Authority

## Ownership

- `mediaforce/tuning/quality_risk.py` owns the versioned quality-risk
  contract, deterministic gate evaluation, typed risk normalization, safe public
  views, and evidence-bound review-record precedence.
- `mediaforce/web/runtime/folder_ai_tuning.py` attaches the contract to pending
  bench proposals before anything queues.
- `mediaforce/web/runtime/folder_actions.py` records the current authoritative
  post-test approval at sample-approval time.
- `mediaforce/web/app.py` publishes the safe typed route payload for the
  workstation without exposing raw internal trace JSON.

## Contract shape

Quality-risk contract version 1 keeps four layers separate:

- `facts`: immutable measured or operator-supplied inputs such as cadence,
  fingerprint findings, review moments, sample result, stream budget, and the
  current operator request.
- `deterministic_gates`: cadence, allow-list, and budget checks that block or
  redirect work without model interpretation.
- `interpretation`: optional model or artifact-reading output that may add
  explainable typed risks but never replaces measured facts.
- `operator_decision`: the current evidence-bound human review record for this
  source, reviewed policy hash, sample job, evidence IDs, and referenced review
  moments.

The public route view exposes only safe typed data such as `verdict`, typed
risks, blocking reasons, comparison reasons, safe provenance identifiers, and
current operator authority. Raw prompt traces and internal contract JSON stay on
the proposal trace path only.

Current and preview policy hashes remain distinct. A review of the current
sample cannot authorize a pending preview whose policy hash differs.

## Deterministic rules

- Only existing allow-listed video transform and encode keys may be compiled.
  Unknown keys are rejected before sampling.
- Allowed values are range- and type-checked before normalization; invalid
  values are rejected instead of being silently clamped into executable policy.
- Cadence `mixed`, `unknown`, or transform-less interlaced/telecine outcomes
  block automatic reuse and remain explicit operator-visible gates.
- Arithmetic infeasibility remains deterministic and is never delegated to a
  model.
- Target-size search traces are schema-checked, source-scoped, and bound to a
  validated transform-plan identity. The operator route receives a compact
  typed summary instead of raw candidate arrays.
- A blocked contract clears proposal queueability, and confirmation recomputes
  the contract against current source, policy, calibration, and failed-job state
  before a sample job is written.
- Names, era, genre, or category may be carried as non-authoritative context,
  but they do not trigger denoise, deinterlace, grain handling, resizing, or
  audio changes.
- Model interpretation may request comparison or add typed rationale, but it
  cannot create an approval or turn a pending human decision into
  `safe_to_sample`.

## Typed risks

Version 1 risk tags are:

- `softness_detail_loss`
- `motion_breakup`
- `banding_dark_scene_damage`
- `grain_noise_treatment`
- `cadence_interlace_artifacts`
- `audio_quality_layout`
- `other`

Each risk carries a label, level, rationale, evidence IDs when available, and
review-moment indexes when the risk is tied to measured review moments.

## Review authority and precedence

- Current rejection outranks older approval.
- A later explicit approval can resolve an older rejection for the exact same
  binding.
- A review record becomes authoritative only when its prefix, source ID,
  reviewed policy hash, sample job ID, and complete evidence-ID set match the
  current source-scoped contract. Referenced moment indexes must still exist.
- Sibling seasons and unrelated folders may remain visible as contextual memory
  or operator shortcuts, but they never gain authority for the current item.
- Historical same-folder approvals are also contextual only. Current authority
  comes from the exact source/policy/sample/evidence review record, not from a
  retrieved learning artifact.
- Stale policy hashes, stale sample jobs, or mismatched source IDs remain in the
  history list but do not control the current verdict.

## Pre-test and post-test records

- Pre-test instructions are derived from current typed risks and measured review
  moments, then bound to the current source ID, policy hash, sample job, and
  moment evidence IDs.
- Post-test approval or rejection records are stored with the same identifiers
  so retries can distinguish current authoritative feedback from stale history.
- Free-form feedback is deterministically mapped onto the first-class concern
  tags. Rejected or concern-bearing post-test notes are persisted immediately,
  even when the resulting next-sample proposal is not queueable.
- Review records supplement facts; they do not overwrite the measured evidence
  or rewrite unknown coverage into prose certainty.
