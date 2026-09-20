# Targeted recovery of terminal folder children

Use this operator API when explicitly selected child jobs of a folder batch
failed for a reason that did not judge the source, policy or encode result. The
parent may still be active, or may have aggregated to `needs_attention` after
its last active child ended. It requeues those same
children; it does not recreate the batch or reset its other jobs. It preserves
attempt counts, failure history, host cooldowns, original manifest indexes and
sample approval lineage. The normal scheduler still decides host availability,
schedule and reserve admission.

First restore the failed host's storage/readiness or keep it excluded from
admission. This recovery API does not implement host isolation or prove remote
process termination. Only unleased terminal shards are eligible. Do not use it
to work around a quality failure, timeout, containment failure or an unverified
completed output.

Five classes are recoverable:

- `host_configuration`.
- `unreadable_output`: the encode removed its own header-only output and used
  up its automatic retries (#620).
- A child in `stopped` status: an operator stop ended it without judging the
  source, policy or result, and the stop already cleaned up its output.
- `deterministic` with exactly the error `Mediaforce database identity changed
  during connection`: the controller lost its database connection while the
  encode ran (#604, #608).
- `deterministic` where the post-encode `ffprobe` of the child's own staged path
  exited non-zero (#620). A header-only output of at most 64 KiB with no
  `staged_artifacts` row does not block this class: the preview names it as
  `header_only_output`, and apply requeues the child as `retry_backoff` so the
  queue's ordinary retry cleanup removes it before the encode starts. A larger
  unreadable output still blocks and follows the retained-output rules below.

Every other `deterministic` failure, including a final-size miss, stays
ineligible. Host isolation and retained-output reconciliation remain
separate work under #593.

Send a POST to `/api/encode-queue/recover-children/preview` on the running
controller, using its normal trusted operator access:

```json
{"parent_job_id":"<folder job>","child_ids":["<exact failed child>"]}
```

Inspect the returned child IDs, manifest indexes and source items. POST the
same IDs and returned `token` to
`/api/encode-queue/recover-children/apply` only when that exact selection is
intended. Start with one child and observe its admission before expanding.
Both requests require JSON and reject cross-origin browser requests. They do not run the application's unrelated periodic artifact cleanup.

Both phases reject missing/duplicate IDs, invalid or overlapping manifest
indexes, failures outside the recoverable classes, completed, stopped or failed
parents, active ownership, changed approval or source,
current policy/cadence blockers, and any recorded or visible final/partial output
other than the header-only case above.
Storage must be visible to the controller; inaccessible paths are a blocker,
not proof of absence. No media decode, content hash, deletion, promotion or
output adoption occurs in either phase; removal of a named header-only output
happens later in the queue's retry cleanup. Source checks use existing fingerprints and current
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

## Header-only encoder output

New encodes no longer reach this state. When the encoder exits cleanly but the
staged output is at most 64 KiB and cannot be probed, the encode removes that
output and fails with the retryable kind `unreadable_output`, so the queue
retries within its normal attempt limit. A larger unreadable output is kept and
ends `needs_attention`, because it may be repairable.

## Progress and heartbeat failures

A live heartbeat is lease evidence, not proof that encoder frames are advancing.
Compare the latest frame-progress timestamp with the running process when work
appears stalled. A database exception while publishing progress must not stop
the reader that drains encoder stderr: the runner retains the first error and
reports it from the owning thread. Local commands terminate through their
managed controller. SSH commands keep draining until the remote command exits,
then report the error, because stopping the SSH client alone does not prove the
remote encoder stopped.

A queued encode on a mounted SSH host runs inside a connection watcher. The
controller holds the connection's input open; when the controller stops the
job, restarts, or loses the link, the watcher on the host ends the processes
writing that job's partial output and removes the partial file. Before this, a
stopped encode kept running on the host, competed with the host's next job and
could leave a complete but unrecorded output (observed on 2026-09-19 and
2026-09-20). A plain command-line encode is not watched. This can leave an output requiring operator review;
existing output and failure checks still apply.

Heartbeat database/path exceptions are logged and retried at the normal
heartbeat interval; status and worker-ownership checks remain mandatory. This
does not establish remote termination or make an expired lease safe to reclaim
while a writer may still exist. Those containment and reconciliation cases remain
under #593.

During database connection setup, volatile metadata changes before SQLite opens
the file receive up to three fresh pinning attempts; the last attempt pins on the
stable device, inode and link-count identity, because sustained concurrent SQLite
writes move `ctime` on the same inode. The lease-owned namespace
witness treats leaf replacement or relinking and parent detachment as terminal
custody failures on supported platforms. Linux also treats leaf attribute events
as terminal. On macOS, attribute-only metadata changes before SQLite opens remain
tolerated this way; the witness tracks replacement, relinking, and
parent detachment. Ordinary in-place writes and WAL checkpoints remain valid.
The retained custody borrow and descriptor-relative identity checks continue
through the SQLite connection lifetime and fail closed if the database or its
parent identity changes.
