# Rebuildable Per-Item Evidence State

## Ownership

- `library_items.cadence_summary_json` and
  `library_items.media_fingerprint_json` remain the canonical measured evidence.
- `mediaforce/library/evidence_state.py` projects those payloads into compact,
  queryable lifecycle state without running media tools.
- `library_item_evidence_state` stores one row per library item and evidence
  kind. It is additive, disposable, and rebuildable from canonical catalog
  state.
- `mediaforce/library/scanner.py` refreshes projection rows when a new or
  content-changed item receives a new canonical payload. It also performs the
  one-time source-identity upgrade for unchanged legacy rows without rebinding
  their policy identity or running media analysis.
- Planner, representative-selection, review, and manifest code continue to
  consume canonical JSON. Projection rows never authorize an encode decision.

## State contract

Evidence kinds are independent:

- `cadence_analysis`
- `media_fingerprint`

Each row has one lifecycle state:

- `current`: the canonical payload has the supported schema and analyzer and
  does not request another analysis pass.
- `analysis_required`: media measurement is required before the evidence can be
  current.
- `classification_required`: stored measurements remain reusable, but current
  policy must reclassify them without rereading media.

Non-current reasons are compact and machine-readable:

- `missing`
- `malformed`
- `retry_required`
- `schema_changed`
- `tool_changed`
- `unknown`
- `source_changed`
- `policy_changed`

Valid blocked or mixed cadence remains current measured evidence. An explicit
unknown cadence or fingerprint result is non-current because it cannot satisfy
the next decision that needs that evidence.

## Identities

Projection rows record:

- the exact canonical JSON SHA-256 without normalizing or rewriting the payload
- the source content fingerprint, falling back to the legacy file fingerprint
  when no content fingerprint exists
- canonical schema version
- analyzer name, analyzer version, and observed ffmpeg runtime version
- the policy hash used for the current classification

A source or analyzer change requires future media analysis. A policy-only hash
change produces `classification_required`, so the stored measurements can be
reclassified cheaply. Projection never probes the currently installed ffmpeg
version; it records only the runtime identity already present in canonical JSON.

The projection source identity is intentionally content-oriented so an
mtime-only touch does not invalidate measured evidence. Run manifests continue
to use the stricter file fingerprint for final encode-time source validation.

## Retry foundation

The state row carries `attempt_count`, `retry_not_before`, `last_attempt_at`,
and `last_error` for the bounded worker. Revision `20260719_0012` also adds
operational batch, lease, heartbeat, worker, and managed-process fields on the
same per-kind row. Projection rebuilds update derived columns while preserving
existing retry and ownership metadata unless the caller explicitly resets a
changed source. See `docs/architecture/evidence-worker.md` for claim and
cancellation semantics.

## Migration and rebuild

Alembic revision `20260719_0011` creates the table and projects every existing
item inside the migration transaction. The frozen migration projector parses
SQLite text only and cannot launch `ffmpeg` or `ffprobe`.

`rebuild_library_item_evidence_states()` can rebuild the full projection or a
targeted set of library items. It never commits, never touches canonical JSON,
and never reads media. Dropping or truncating the projection therefore cannot
destroy catalog membership or measured evidence.

Rebuild preserves existing source and policy identities so it can mark source,
analyzer, or policy invalidation without discarding prior state. When a
projection row is absent, rebuild binds the canonical payload to the current
catalog identity. The bounded worker completes policy-only reclassification
and then adopts the current policy hash without rereading media.

Read APIs never call projection sync or rebuild helpers. Missing projection rows
mean the projection needs an explicit rebuild; they do not mean the canonical
media evidence is missing.
