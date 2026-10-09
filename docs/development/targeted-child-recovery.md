# Targeted recovery of terminal folder children

Use this API when explicitly selected child jobs of a folder batch
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
to work around a quality failure, a timeout other than the remote quality one
below, a containment failure or an unverified completed output.

Recoverable classes include:

- `host_configuration`.
- `unreadable_output`: the encode removed its own header-only output and used
  up its automatic retries (#620).
- `storage_io`, `stale_lease`, `worker_restart`, and
  `controller_database_busy`: the share or the controller failed and the
  automatic retries ran out; nothing judged the item.
- `unknown`: an unrecognised error used up its automatic retries.
- `deterministic` failures recorded before their kind existed: storage I/O
  errors (recovered as `storage_io`), controller database busy (as
  `controller_database_busy`), and a host that could not load a VMAF model (as
  `host_configuration`).
- A child in `stopped` status: a user stop ended it without judging the
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
- `remote_quality_timeout`: a quality measurement over SSH ran past its time
  limit, was shown stopped on that computer, and the automatic retries ran out
  (#596). It measured nothing about the item. A `deterministic` child whose
  error is the raw timeout of a remote `ab-av1 crf-search` or `sample-encode`
  SSH command, recorded before this kind existed, is recovered the same way.
  Nothing showed that legacy run stopped, so check that the computer is idle
  before recovering it. A timeout whose run was not shown stopped is
  `containment_unproven` and is not recoverable here.

Every other `deterministic` failure, including a final-size miss, stays
ineligible. Host isolation and retained-output reconciliation remain
separate work under #593.

Send a POST to `/api/encode-queue/recover-children/preview` on the running
controller, using its normal trusted controller access:

```json
{"parent_job_id":"<folder job>","child_ids":["<exact failed child>"]}
```

Inspect the returned child IDs, manifest indexes, source items and `skipped`
entries. POST the returned `requested_child_ids` (the IDs sent to preview,
not only the eligible `child_ids`) and `token` to
`/api/encode-queue/recover-children/apply` only when that result is
intended. Start with one child and observe its admission before expanding.
Both requests require JSON and reject cross-origin browser requests. They do not run the application's unrelated periodic artifact cleanup.

Each selected child is judged on its own. A child is skipped, and listed in
`skipped` with a plain reason, for a failure outside the recoverable classes,
active ownership, invalid manifest indexes, an item already owned by an active
or completed sibling or claimed by another selected child, a changed source,
any recorded or visible final/partial output other than the header-only case
above, a current policy or motion-pattern blocker on one of its items, or an
approval that no longer covers its settings. The remaining children are still
recovered; apply requeues only them.

Approval is judged per child. The batch's production approval must still be
current. A newer
sample approval with the same policy and user intent covers a child; a
changed policy, or an intent that differs from the one a child's files were
resolved under, does not.

The whole request is rejected for missing, duplicate or foreign IDs, a
completed, stopped or failed parent, an unreadable manifest, an active or
completed sibling with invalid indexes, a production approval that changed or
is missing, or when no
selected child is eligible.
Storage must be visible to the controller; inaccessible paths are a blocker,
not proof of absence. No media decode, content hash, deletion, promotion or
output adoption occurs in either phase; removal of a named header-only output
happens later in the queue's retry cleanup. Source checks use existing fingerprints and current
size/modification time; they are not a new full-content verification.

Apply repeats all checks under one database transaction and rejects a stale
preview with HTTP 409 without changing any selected child. Skipped children are part of the token, so a change in their eligibility also stales the preview. Ordinary progress
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

## Remote quality runs that run too long

A quality measurement over SSH that passes its time limit raises
`RemoteQualityTimeoutError` instead of the raw timeout, whose text named the
whole SSH command. Stopping the local SSH client does not stop the run on the
computer, so Mediaforce then runs a short stop step there. From one process
listing it collects every process whose command line names the run's own
scoped temp folder (`.mediaforce-ab-av1-<id>`, passed as an argument and
matched as a whole path, so `<folder>0` is not it) plus all their descendants,
stops those processes first politely and then forcibly, and reports success
only when a fresh listing shows nothing naming the folder and none of the
collected processes still running. Listings are full width, so a long
command line keeps the folder. Each collected process is kept as its pid and
full command line, and is signalled only while that pid still shows that
command line; a pid that now shows another command was reused and counts as
gone. It never signals a whole process group, because nothing proves a group
belongs only to this run. A failed or empty process listing, or a failed
check of it, never counts as success.

ab-av1's ffmpeg children write into the folder, so their own command lines
name it. A process that neither names the folder nor descends from one that
does is not seen. Confirming that ab-av1's children behave this way is part of
the Director-watched session on a real encode computer.

- Shown stopped: the temp folder is removed as usual, and an encode records
  `remote_quality_timeout` and retries within its normal attempt limit. The
  user sees which computer, that the run was stopped, and that it will be
  tried again. If the folder cannot be removed, the user is told so in one
  plain sentence; the raw detail stays on the error as a diagnostic.
- Not shown stopped (the step failed, timed out, found survivors, or the run
  had no scoped temp folder): the temp folder is kept, and an encode records
  `containment_unproven` and waits for the user, with a plain instruction to
  check that computer is idle and then try the file again.

A sample job records the same kind, message and whether the run was stopped in
its result, without retrying on its own.

## Progress and heartbeat failures

A live heartbeat is lease evidence, not proof that encoder frames are advancing.
Compare the latest frame-progress timestamp with the running process when work
appears stalled. A database exception while publishing progress must not stop
the reader that drains encoder stderr: the runner retains the first error and
reports it from the owning thread. Local commands terminate through their
managed controller. SSH commands keep draining until the remote command exits,
then report the error, because stopping the SSH client alone does not prove the
remote encoder stopped.

A queued encode on a mounted SSH host runs inside a connection watcher, but
losing the controller does not prove that watcher fired. Startup recovery and
retry cleanup therefore inspect the remote host before removing an interrupted
output. They identify ffmpeg's own output and confirm its writable file through
`lsof`, end that encoder and its Mediaforce connection wrapper, and check again
before deleting the partial file. Process birth time and command are checked
before signals; unrelated encodes and readers of that file are preserved.

The check runs even when the file is visible through the controller's mounted
share. Recovery connects without waking sleeping computers. The retained-job
sweep runs at startup and on Stop, rather than on each queue poll, and attempts
an unavailable host only once per sweep. Automatic retry cleanup retains its
existing backoff. A schedule-close transition completes cleanup before the
computer's configured shutdown command. A late sweep result cannot overwrite
a job that was requeued or changed while SSH was running. A failed host connection, incomplete inventory, or surviving writer
keeps the unfinished file and delays automatic retry. Making a terminal file
again reports HTTP 409 with a wait message until cleanup succeeds. Retry through
the same supported action once the host is reachable; no manual file deletion
is needed. Startup and Stop also inspect retained stopped-job manifests when
an earlier cleanup already removed the staging record. They preserve output
paths currently owned by a running job. Finished and promoted outputs retain
their existing protection.

This recovery is for mounted remote outputs. Scratch-host lifetime protection
remains described in [staged encode hosts](../architecture/staged-encode-hosts.md).
A forced restart during a real approved encode is a separate runtime
qualification; source fixtures alone do not prove it on the configured Macs.

When a running job's lease has expired, the controller ends its worker only
after 10 minutes with no sign of life: no progress write, no heartbeat, and no
recent start. Each size measured during the quality search writes progress, so
a long search does not look silent. A worker ended this way retries as a stale
lease, like a reclaimed job, instead of stopping for the user. A worker whose
job another attempt now owns, or that a reclaim has already scheduled to
retry, leaves that state alone. A real outcome it reached, such as a quality
conflict, is still recorded.

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

Remote cleanup also checks writable handles by the file itself: process-list text
that escapes a non-ASCII path cannot silently authorize deletion. An unrecognised
writer leaves cleanup deferred. Signal identities travel through stdin so the old
connection watcher cannot match the cleanup command's arguments. Schedule-close
cleanup performs remote I/O before taking the database write lock, then rechecks
the saved attempt before changing queue state. Folder retries with held-back files
keep each selected child's host identity.
If an escaped path leaves a connection watcher unidentified even after its writer
exits, reuse waits for that watcher to end. This can conservatively defer a
non-ASCII output while another encoder or connection watcher remains on the same host,
including when the old file has been unlinked.

Cleanup and dispatch coordinate through a non-blocking lock beside the runtime
manifest, separate from the manifest's short policy-edit lock. Cleanup holds it
through its last remote operation; schedule closure holds it through its queue
transition. A requeue returns its existing wait response while that cleanup runs,
and dispatch leaves the queued job for a later pass. Process exit releases the
lock automatically, so a controller crash does not leave a durable claim to
manually clear. SQLite remains available while SSH is in flight.

A stale artifact recorded as remote stays remote when its computer configuration
is removed or repurposed. Cleanup waits for a matching reachable computer instead
of treating the mounted file as locally owned.
