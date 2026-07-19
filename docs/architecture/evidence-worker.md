# Bounded Evidence Worker

## Purpose

Catalog refresh and evidence analysis are separate operations.

- A scan inventories files with one bounded `ffprobe` metadata read. It never
  launches cadence or media-fingerprint `ffmpeg` analysis.
- Canonical cadence and fingerprint JSON remains on `library_items` and is
  reused until the source, analyzer, schema, or policy makes it non-current.
- Expensive updates run only after an operator creates an explicit item,
  folder, or root batch and then resumes it.

There is no evidence worker attached to web-server startup and no permanent
idle polling loop. A foreground worker pass claims bounded work and exits.

## Durable state

`library_item_evidence_state` owns one independent work unit for each
`(library_item_id, evidence_kind)` pair. Cadence and fingerprint can therefore
complete, retry, or fail independently. The row stores:

- projected evidence freshness and source/policy/tool identity
- bounded retry state
- batch and work status
- worker ownership, lease, heartbeat, and managed child-process PID
- the source fingerprint captured when the batch was created

`evidence_queue_state` is a singleton control record for the active explicit
batch. It stores the scope, selected evidence kinds, pause/cancel state, and
aggregate counts. Queue data is operational and rebuildable; canonical
evidence is not.

Alembic revision `20260719_0012` adds this operational state without creating a
batch or queueing existing evidence. Revision `20260719_0013` adds the durable
`background_work_state` switch shared by catalog inventory and evidence
analysis. Analyzer or policy changes remain visible as projection state until
an operator explicitly prepares and starts work.

## Batch lifecycle

`start_evidence_work()` requires a non-empty resolved scope. It selects only
non-current evidence inside that exact item or descendant scope, caps the
batch at 25 evidence updates by default, snapshots each source fingerprint,
and creates the batch paused. One media file can contribute two updates because
cadence and media fingerprint are independent evidence kinds.

Queue states are:

- `paused`: no new claim is allowed
- `queued`: claimable work exists
- `running`: exactly one work unit owns the media-analysis lane
- `cancel_requested`: queued work is cancelled and the active managed process
  is being terminated
- `completed`, `completed_with_errors`, or `cancelled`: terminal batch state

The default foreground run budget is one work unit. `--max-items` can raise
that bound, and `--max-seconds` prevents another claim after the requested
foreground time budget. Each analyzer command retains its own subprocess
timeout and cancellation boundary.

## Claim and commit protocol

Claims use a short SQLite `BEGIN IMMEDIATE` transaction:

1. Read the singleton pause/cancel state.
2. Refuse a claim while another work unit is `running`.
3. Atomically move one ready work unit to `running` with a worker ID and lease.
4. Commit and close the transaction before opening media or launching a child
   process.

The analyzer runs with a heartbeat thread. A lost claim, failed heartbeat, or
cancel request terminates the managed process group. Completion opens a new
short transaction and writes canonical JSON only when all of these still
match:

- batch ID
- worker ownership
- evidence kind and item ID
- catalog source fingerprint captured at claim time
- fresh filesystem source fingerprint after analysis

The canonical result, refreshed projection, and terminal work transition
commit together. A stale result is discarded rather than rebound to newer
media.

## Retry and recovery

- Unavailable roots move to `waiting_source` without consuming an attempt.
- Tool, corrupt-media, timeout, and parse failures use exponential backoff and
  stop after three attempts by default.
- Expired leases are reclaimed on the next explicit worker pass. A live lease
  is never stolen merely because another scheduler starts.
- Pause prevents new claims but does not interrupt the active safe unit.
- The global background-work pause prevents new catalog scans, batch creation,
  queue resumes, and evidence claims. A scan or evidence update already running
  is allowed to finish safely. A queued scan rechecks the switch before it opens
  a source.
- Cancel marks pending units cancelled and asks the heartbeat/controller path
  to terminate the active subprocess group. The attempt is restored when that
  cancellation is observed.
- Policy-only `classification_required` work is claimed before media analysis
  and reuses stored measurements without `ffmpeg` or `ffprobe`.

## Operator flow

The Activity workstation exposes the same bounded state machine:

1. `Refresh catalog` performs inventory only. It updates changed file facts and
   never starts cadence or fingerprint analysis.
2. `Prepare analysis` chooses one explicit item, folder, or root scope, evidence
   kinds, and a maximum number of updates. The new batch is paused.
3. `Start analysis` launches one process-local bounded runner. It exits when the
   prepared batch completes, reaches its update budget, or becomes blocked.
4. `Pause after this item`, `Resume analysis`, and `Cancel batch` write durable
   queue controls near the visible scope and progress.
5. `Pause new background work` is separate from encode approval and processing
   controls. It governs catalog inventory and evidence analysis only.

The workstation polls while a catalog scan or evidence runner is active and
returns to manual refresh when idle. Backlog totals are paired with filters and
pagination so every counted evidence update remains reachable. Source-root
warnings use the scanner's preserved-cache messages rather than discarding the
last known catalog.

The CLI remains available for advanced or scripted operation.

Create a paused folder pilot:

```bash
uv run mediaforce evidence start "tv/Show/Season 1" --limit 10
```

Inspect, permit claims, and process one unit:

```bash
uv run mediaforce evidence status
uv run mediaforce evidence resume
uv run mediaforce evidence run --max-items 1
```

Repeat `evidence run` to resume durable progress. Use `pause` before the next
claim or `cancel` to stop the active managed process and cancel the remaining
batch. A root pilot uses the same commands with an explicit root such as `tv`;
keep the limit small before considering a broader backfill.
