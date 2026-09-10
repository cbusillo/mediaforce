# Targeted recovery of host-setup failures

Use this operator API when a folder batch is still active but explicitly
selected child jobs failed with `host_configuration`. It requeues those same
children; it does not recreate the batch or reset its other jobs. It preserves
attempt counts, failure history, host cooldowns, original manifest indexes and
sample approval lineage. The normal scheduler still decides host availability,
schedule and reserve admission.

First restore the failed host's storage/readiness or keep it excluded from
admission. This recovery API does not implement host isolation or prove remote
process termination. Only unleased terminal shards are eligible. Do not use it
to work around a quality failure, timeout, containment failure or an unverified
completed output. Host isolation and retained-output reconciliation remain
separate work under #593.

Send a POST to `/api/encode-queue/recover-children/preview` on the running
controller, using its normal trusted operator access:

```json
{"parent_job_id":"<active folder job>","child_ids":["<exact failed child>"]}
```

Inspect the returned child IDs, manifest indexes and source items. POST the
same IDs and returned `token` to
`/api/encode-queue/recover-children/apply` only when that exact selection is
intended. Start with one child and observe its admission before expanding.
Both requests require JSON and reject cross-origin browser requests. They do not run the application's unrelated periodic artifact cleanup.

Both phases reject missing/duplicate IDs, invalid or overlapping manifest
indexes, non-host-setup failures, active ownership, changed approval or source,
current policy/cadence blockers, and any recorded or visible final/partial output.
Storage must be visible to the controller; inaccessible paths are a blocker,
not proof of absence. No media decode, content hash, deletion, promotion or
output adoption occurs. Source checks use existing fingerprints and current
size/modification time; they are not a new full-content verification.

Apply repeats all checks under one database transaction and rejects a stale
preview with HTTP 409 without changing any selected child. Ordinary progress
updates from unrelated running siblings do not invalidate a preview. The
transaction appends `targeted_child_recovery` item events and synchronizes the
parent summary. A repeated apply must use a fresh preview; a queued child is
no longer eligible.

If an output exists, preserve it and validate/reconcile it separately before
considering a re-encode. For example, the retained S01E20 output in the #593
incident is intentionally outside this recovery path. Never manually mark an
output completed or edit the live SQLite database to bypass these checks.
