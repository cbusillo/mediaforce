# Bounded Evidence Worker

## Purpose

Catalog refresh and evidence analysis are separate operations.

- A scan inventories files with one bounded `ffprobe` metadata read. It never
  launches cadence or media-fingerprint `ffmpeg` analysis.
- Canonical cadence and fingerprint JSON remains on `library_items` and is
  reused until the source, analyzer, schema, or policy makes it non-current.
- Expensive updates run only inside a bounded item, folder, or root batch.
  A batch a user prepares starts paused and runs when it is started; a
  batch created because production is waiting on a file starts unpaused.

The web app's evidence-autostart worker runs any unpaused batch that has
claimable work (see the just-in-time path below). Catalog scans never start
analysis. The CLI `evidence run` pass claims bounded work and exits.

Fingerprint analysis decodes each video range once, with signal and edge
measurements in two branches of that pass. Ranges run sequentially with their
own timeout and frame caps, retaining the same sample times, frame budgets and
thresholds. Supported 8-bit and 10-bit 4:2:0 formats use VideoToolbox and select
frames before copying them to CPU memory. The GPU still decodes the entire
sampled range. Each range records its decoder; unsupported or incomplete
hardware decoding uses the complete software pass. A timeout stops further
analysis for that file and leaves its result unknown for the queue to retry,
while later queue items remain free to continue. After a video decoder timeout,
the next attempt uses software; an audio timeout does not change the video
decoder. End-of-file hardware samples must contain matching signal and edge
counts meeting the expected available frames, without reducing the decode cap.
Audio and
cadence retain their measurements and safety decisions.

The worker reuses an SSH master per unchanged encode-computer configuration
within its run. Sockets live in private short temporary directories. The worker starts a
foreground master as its own resource and closes it when the run ends. Individual
managed commands use that master without spawning a background descendant,
preserving command-containment proof.
Every command retains its wake probe, wake assertion and current schedule
deadline. The master sends bounded keepalives so a sleeping or lost computer
does not leave its socket looking usable indefinitely. Host
eligibility and ffmpeg's version are refreshed for every claim. Source checks
before and after measurement are unchanged. A multiplexed status 255, including
one with empty stderr, triggers a local master-liveness check. A lost master
defers the file for its computer; a live master preserves ordinary per-file
failure handling so one bad file cannot indefinitely hold its library.

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
analysis. Revision `20260719_0014` adds durable work priority and a plain-language
reason so decision-blocking safety work can run before advisory backfill.
Analyzer or policy changes remain visible as projection state until a user
explicitly prepares and starts work.

## Batch lifecycle

`start_evidence_work()` requires a non-empty resolved scope. It selects only
non-current evidence inside that exact item or descendant scope, caps the
batch at 25 evidence updates by default, snapshots each source fingerprint,
and creates the batch paused. Policy-only reclassification is selected first.
Cadence analysis then follows the explicit user scope. Fingerprint analysis
starts with technical representatives, stays within a per-scope budget, and
adds only a small uncertainty frontier when measured hard cases justify it.
One media file can contribute two updates because cadence and media fingerprint
are independent evidence kinds.

Sample and production actions use a separate just-in-time path. Missing or stale
cadence holds back only the affected media items, adds those exact rows to the
active batch at decision priority, and lets the rest of a production selection
queue; the user sees each held item in Activity. A new batch created for a
production run starts unpaused, and the web app's evidence-autostart worker
runs any unpaused batch with claimable work, so held files are checked without
anyone pressing `Start analysis`. The global background pause, a paused batch,
and cancellation still stop it. Sample checks still create a paused batch. A retained
terminal failure is reported as a failure and requires an explicit item retry;
repeating the blocked action does not reset its attempt budget. Fingerprint
evidence remains advisory and never blocks sample or production queueing.

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

## Original source traits

Current-file evidence remains on the catalog row. Independently retained
`source_generations`, `source_versions` and append-only `source_measurements`
keep observations of an input separate from every generated output. A missing
kind is unknown; measuring an output never fills it. Legacy catalog summaries
are quarantined as unbound candidates during migration.

`library/source_evidence.py` is the source-trait reader. It returns each kind's
tier, basis, binding, measurement references, availability and reason. Original
grouping uses these records with separate proven, probable-by-basis and unknown
coverage. Direct samples and content-intent boundaries keep the actual reviewed
input's identity and measurements; original references are carried separately.
Historical advisory grouping cannot replay an approval or authorize a file change.

Recover historical traits without reading original media:

```bash
uv run mediaforce evidence restore-source
uv run mediaforce evidence restore-source --apply
```

The default opens SQLite read-only, without migration, runtime locks or cleanup.
Apply takes the CLI's exclusive runtime lock, rechecks the retained evidence and
appends records. Eligibility needs a readable non-AV1 input plan with sampled
identity and size, an intact plan/job/completion/published-output association,
consistent available input history, and evidence that the original is unavailable.
Missing unrelated plans are reported as gaps. Older links require immutable
receipts with both endpoint identities. Publication or event counts alone do
not establish ancestry or unavailability.

Imported traits keep the exact plan summary and hash, sampled input, codec-rule
version, `historical-plan` basis and `historical-plan-sampled` binding. Measurement
time stays unknown. Apply is idempotent; later contradictory evidence appends an
ineligibility correction against the admitted input and its retained evidence.
Later encodes and publishes of its outputs do not replace that assessment's basis,
even without production-lineage receipts. All AV1 encoders share the same codec
exclusion; a recorded non-AV1 library output challenges only assessments of that
codec, including recognized encoder-family aliases. An unrecognized library
encoder leaves the rule unresolved for all input codecs. Sample and test outputs
do not challenge the library rule. Output checks are collected once for each
planning or recovery pass. Fingerprint acquisition uses the current profile and
current measurements when there is no eligible original fingerprint summary,
including corrected admissions and probable admissions missing that measurement;
original-trait reads still require an eligible admission.
Fresh measurements, owner admissions and catalog refresh
cannot rebind or upgrade an imported measurement. Readable-source intake and
protected owner confirmation are separate work; this command cannot create
proven traits.

Publishing journals every move before changing media, including an occupied
archive's hidden promotion backup. The active version advances with the catalog
transaction. Recovery checks recorded identities and restores journaled moves;
ambiguity retains an unresolved operation and blocks that item's late commits.
Cleanup protects prepared and unresolved paths, records removed locations, and
retains lineage and measurements after media or catalog removal. The next owner
Cleanup action reconciles interrupted removal records before enumerating files:
a remaining file rolls the removal back; a missing file commits it. An unreadable
path stays pending for a later retry. Reconciliation itself never deletes media.
Cleanup continues to require the existing explicit owner action.

## Where measurement runs

When no encode computer is set up, `ffprobe` and the cadence and fingerprint
`ffmpeg` commands run on the controller as before. Once at least one remote
host reads the library from its own mount over SSH (a configured remote host
without an explicit `mode` counts as SSH), measurement runs only on an encode
computer, never on the controller:

- The worker picks the highest-priority host that is available, has `ffmpeg`,
  keeps the `encode_queue` capability, has its encode schedule open, and is
  allowed the item's library. Both the web app and `mediaforce evidence run`
  use the encode queue's schedule-aware host rows; the web app reads its
  cached host checks and the CLI checks each host fresh.
- The claim's heartbeat starts before the host is chosen, so the host check,
  a wake, and the remote `ffmpeg -version` stay inside the lease and can be
  cancelled.
- The same command builders and parsers run over `run_remote_command`,
  against the host's own path to the source. Lease loss stops the local SSH
  client through the managed process controller; the remote commands are
  bounded by frame counts and timeouts.
- When none can take the work, or the connection drops mid-run, the item moves
  to `waiting_host` with the reason "Waiting for an encode computer to
  measure this file." without consuming an attempt.
- The summary records the measuring host and that host's `ffmpeg -version`
  line. Freshness compares the analyzer name and version, schema, source, and
  policy, not the `ffmpeg` build, so a result from another computer is not
  re-queued because the controller's `ffmpeg` differs.
- The source identity check before and after the run stays on the controller:
  a stat plus, for content fingerprints, three 64 KiB samples. Before and
  after measuring, one remote `stat` must also report the controller's size
  and whole-second modification time. A mismatch fails the item like any other
  source change and no evidence is saved.

## Retry and recovery

- Unavailable roots move to `waiting_source` without consuming an attempt.
- Tool, corrupt-media, timeout, and parse failures use exponential backoff and
  stop after three attempts by default.
- Expired leases are reclaimed on the next worker pass (CLI or autostart).
  Autostart also recovers before counting ready work. An unexpired lease is
  reclaimed only when its owner names this controller and its PID is positively
  gone; active, foreign and unknown owners keep their leases.
- The automatic runner keeps one worker loop and admission prefetcher alive
  while prepared work is runnable. It discovers bounded groups of new inputs
  between completed work and when useful work runs dry, then continues without
  waiting for the autostart poll. Explicit manual and CLI runs retain their
  finite item/time budgets and never discover outside their prepared scope.
- Autostart completes its first computer-status check in its background thread
  before claiming work. Claim selection and admission prefetch share the same
  current eligible libraries. Queued measurement work in an unavailable library
  becomes `waiting_host` without taking a lease or consuming attempts; urgent
  unavailable rows therefore do not interrupt a healthy library's admission.
  Non-original current rows and policy-only reclassification can still finish.
  A current original needs a relevant observed receipt before it can qualify
  without a computer; legacy summary JSON alone does not imply that receipt.
  Prefetch includes those legacy originals when their computer is eligible.
- An actual connection failure keeps the existing retry interval. Remote
  workers and prefetch read that retained wait, so a stale available host check
  cannot repeatedly admit the next files. Local runs with no encode computers
  retain independent per-file retry behavior.
- A missing source root similarly places source-dependent siblings in
  `waiting_source` for the observed retry interval, including current originals
  that still need admission. Metadata-only non-original work stays claimable.
  Bulk marking never extends a deadline without another failed observation.
- The measuring computer is selected again after admission, so a schedule that
  closes during hashing prevents measurement. CLI host probes use the same raw
  status cache as the web, while schedules are recomputed for every selection.
  Prefetch commit guards do no host checks or network work under write locks.
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
- If fingerprint reclassification reveals that new media measurements are
  required, the row completes its policy-only pass and returns to the bounded
  representative planner instead of fanning out into immediate analysis.

## User flow

The Activity workstation exposes the same bounded state machine:

1. `Refresh catalog` performs inventory only. It updates changed file facts and
   never starts cadence or fingerprint analysis. The same inventory also runs
   by itself: a leader-owned worker starts one when the last finished refresh
   is older than `media.catalog_refresh_hours` (default 6, `0` turns it off).
   Settings exposes this interval under Metadata. The worker re-runs a refresh
   that a restart interrupted, and waits 30 minutes after a
   failed one. It never runs from a read path and it honours
   `Pause new background work`. The catalog is shown as stale after two missed
   intervals, or after a day when automatic refresh is off.
2. `Prepare analysis` chooses one explicit item, folder, or root scope, evidence
   kinds, and a maximum number of updates. The new batch is paused.
3. `Start analysis` launches one process-local bounded runner. It exits when the
   prepared batch completes, reaches its update budget, or becomes blocked.
4. `Pause after this item`, `Resume analysis`, and `Cancel batch` write durable
   queue controls near the visible scope and progress.
5. `Pause new background work` is separate from encode approval and processing
   controls. It governs catalog inventory and evidence analysis only.

The active item and backlog show both why evidence needs an update and why that
work is scheduled now. Decision-blocking cadence rows are labelled as blocking a
sample or production action; policy-only rows explicitly say that no media read
is required.

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

Compare representative dimensions from stored evidence without opening media:

```bash
uv run mediaforce evidence replay "tv/Show/Season 1"
```


### Automatic original intake

Bounded discovery adds new unpublished inputs to the existing evidence queue.
It does no media I/O. Each job checks the raw container and stream stamps,
streams a cancellable full digest against retained outputs, and applies the
historical codec and encoder exclusions. Hashing reads at most the observed
file length and allocates no media copies. The queue's existing lease,
heartbeat, retry, pause, cancellation and shutdown handling own the whole job.
Higher priority analysis can defer a hash without consuming a failure attempt.

While a file is measured, the bounded runner prepares admission for at most
one next queued original on a separate controller thread. It uses the same
digest, probe, before/after observations and admission commit checks. It takes
no measurement lease and changes no work status. Pause, cancellation, shutdown
or higher-priority work interrupt preparation; failure leaves the queued claim
to retry normally. Preparation finishes before the runner claims another file,
so the next claim can reuse the verified admission without racing its commit.

An assessment is final for its unchanged input and rule versions. Stable
negative checks leave the original unknown; transient failures stay in the
queue's retry state, not in admission history. Unknown or missing checks never
become passes. Completed cancellation leaves future downloads available.
Prepare can add requested analysis to a background-only batch without resetting
its leases or retries; it pauses new starts until the Director presses Start.

A probable assessment freezes the IDs of qualifying earlier observed receipts,
including their before/after identity and summary hash. It cannot reuse legacy
catalog summaries or unbound measurements. Subsequent queue receipts bind to
the admitted input version; their stat checks bracket the measurement rather
than requiring an earlier mount's device or inode. The newest successful
receipt supplies each trait while older evidence remains retained. Missing
traits use the same normal analyzer and independent per-kind retries.

All measurements remain observations, including local files without strict
custody, SMB and remote/staged-host paths. None becomes proven. Remote workers
use Python 3 to execute the controller's descriptor-sampling code, comparing
samples and size across hosts and stat signatures within each host. Stat-only
hosts cannot qualify. Existing staged scratch, capacity and cleanup rules are
unchanged. Probable traits support advisory grouping and grant no deletion,
cleanup or approval-transfer authority.

Failed automatic original checks remain visible in the maintenance backlog after the existing attempt budget, until Prepare or a changed source retries them. Cancelled automatic checks become eligible again after the queue's cooldown. Discovery can refill either missing trait. A completed full-file hash survives a later probe failure as an observed version, without admitting an original. Prepare can extend a background-only active batch without replacing its leases; completed operator rows do not prevent later preparation.

Prepare preserves background ownership and retry state, but a partial hash restarts after a pause or higher-priority interruption. Unknown analyzer results use the same bounded retry budget as other failures.
