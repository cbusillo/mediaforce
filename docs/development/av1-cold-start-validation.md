# AV1 Cold-Start Controlled Validation

Issue #277 owns the controlled evidence that may prove or reject public AV1
cold-start cells. The checked-in preregistration freezes the experiment design
without selecting media, starting Mediaforce, or authorizing any encode.

## V1 historical protocol

`docs/validation/av1-cold-start-preregistration-v1.json` is canonical JSON with
a deterministic manifest ID and payload digest. It declares:

- two activation-eligible `balanced` cells for exact `animation` and `typical`
  trait sets
- sixteen independently mapped held-out case slots per activation candidate
- fallback-conformance coverage for darkness, motion, grain/noise,
  texture/detail, low-motion dialogue, mixed, and unknown content
- explicit no-recommendation coverage for `reference`, `transparent`, and
  `perceptual_floor` intentions
- alternating baseline-first and guided-first order for paired candidate cases
- a six-month expiration that requires re-review if execution has not started

The manifest contains no title, path, source ID, fingerprint, review-media
identity, operator note, runtime database value, or case-to-media mapping. The
case IDs are protocol slots, not hashes of media.

Validate it without loading Mediaforce config or state:

```bash
uv run python scripts/verify_av1_cold_start_preregistration.py \
  validate docs/validation/av1-cold-start-preregistration-v1.json --json
```

The validator reports `runtime_execution_authorized=false`. V1 remains immutable
historical no-go evidence; its underfilled and unreachable cells are not
reinterpreted, relabeled, or backfilled.

## V2 successor preregistration

`docs/validation/av1-cold-start-preregistration-v2.json` is the canonical issue
`#285` successor. It binds `acstp1`, `acstf1`, `av1vh1`, the unchanged production
predictor contract `acsp1`, the validation-candidate contract `acsvp1`, and the
derivation-authorization contract `acsvda1`. It registers:

- sixteen paired holdout slots each for exact `motion` and exact `darkness`
  `balanced` candidates
- three baseline-only fallback-conformance slots each for grain/noise, mixed,
  texture/detail, and the `reference`, `transparent`, and `perceptual_floor`
  directional-intent paths
- fifty total case slots, with candidate ordinals alternating baseline-first
  and guided-first and duplicate review fixed at ordinals 4, 8, 12, and 16
- explicit exclusions for animation and typical candidate cells, low-motion
  dialogue, and unknown fallback rather than silently dropping them

The selection was frozen from a fresh read-only aggregate inventory at
`2026-07-27T21:08:41Z`. The count-bearing eligibility attestation remains in
machine-local state outside the repository. The checked-in manifest carries
only its deterministic ID and SHA-256 digest; it contains no runtime database
values, media identities, paths, titles, fingerprints, or mappings.

Validate the successor without loading runtime state:

```bash
uv run python scripts/verify_av1_cold_start_preregistration.py \
  validate docs/validation/av1-cold-start-preregistration-v2.json --json
```

The v2 manifest reports both `runtime_execution_authorized=false` and
`holdout_execution_authorized=false`. Its authority is preregistration only. A
merged v2 permits #286 to prepare the private source partition while Mediaforce
remains paused; it does not authorize observations, encodes, review media, or a
candidate probe.

The separate `acsvda1` authorization may later permit ordinary sampled
calibration and unchanged measured full search on selection-lock-reserved
derivation sources only. It hard-rejects validation candidates, guided probes,
holdout case execution, and public bundle activation. A different, later
authorization must bind the reviewed candidate locks before any v2 holdout can
run. The verifier therefore refuses the v2 `report` action at this phase.

## Private v2 source partition

Issue `#286` uses the `av1vsp1` partition contract to freeze all fifty holdout
slots and two twelve-source derivation reservations while Mediaforce remains
paused. The pure contract lives in
`mediaforce/tuning/av1_validation_partition.py`; the separate read-only
inventory adapter lives in
`mediaforce/tuning/av1_validation_partition_inventory.py`. Neither module
starts runtime work, probes media, creates observations, builds a candidate
lock, or creates either execution authorization.

Create a dedicated owner-only directory outside the repository, then create its
HMAC key once:

```bash
uv run python scripts/verify_av1_cold_start_preregistration.py \
  create-partition-key /private/owner-only/av1-v2/partition.key \
  --config config/defaults.toml --json
```

The command emits an opaque `token_key_id`. Record that ID on issue `#286`
before reading the private inventory. The later build requires the same ID, so
replacing or rerolling the HMAC key after its durable commitment fails closed.
Key creation and partition construction both acquire the same exclusive runtime
lock as `mediaforce-web`; they fail closed unless Mediaforce is paused.
Key publication uses a hidden owner-only temporary file, durable file sync,
exclusive atomic rename, and parent-directory sync. Retrying the same path
validates the existing key and returns the same ID with `created=false`.

Build the immutable private partition from the checked-in manifest, pinned
machine-local eligibility attestation, current measured fingerprint inventory,
and an explicit canonical UTC timestamp:

```bash
uv run python scripts/verify_av1_cold_start_preregistration.py \
  build-partition docs/validation/av1-cold-start-preregistration-v2.json \
  /private/owner-only/eligibility-attestation-v1.json \
  --key /private/owner-only/av1-v2/partition.key \
  --expected-token-key-id av1vkey1_<committed-id> \
  --output /private/owner-only/av1-v2/source-partition-v1.json \
  --selected-at 2026-07-27T23:00:00Z --json
```

The selection timestamp must be at or after the manifest registration and
strictly before its expiration. Both the initial build and every embedded or
current-input replay reject an out-of-window timestamp.

Validate both the embedded immutable private inventory snapshot and the current
read-only database/config projection. A narrowed inventory, selected-source
change, taxonomy change, policy change, or compatibility change fails the
current-input gate:

```bash
uv run python scripts/verify_av1_cold_start_preregistration.py \
  validate-partition docs/validation/av1-cold-start-preregistration-v2.json \
  /private/owner-only/eligibility-attestation-v1.json \
  /private/owner-only/av1-v2/source-partition-v1.json \
  --key /private/owner-only/av1-v2/partition.key --json
```

The partition requires owner-only directory and file permissions, rejects any
private artifact path inside the repository, and never prints the output path,
local item IDs, identity tokens, fingerprints, titles, series, or source-group
values. Its public-safe output is limited to the manifest ID, the selection-lock
and derivation-partition digests, preregistered counts, and false execution
authority flags.

Selection uses domain-separated HMAC-SHA256 ranking and identity tokens. It
enforces exact trait selectors for publication candidates, the registered
`contains_all` selectors for fallback cells, and global uniqueness across all
seventy-four reservations by content version, logical title, and series.
Publication holdout and derivation cohorts separately enforce the six-group
minimum and one-third concentration maximum, and no derivation source group may
overlap any holdout source group. Ambiguous duplicate content versions are
excluded before the immutable inventory snapshot is frozen. Within a plan,
holdout slots receive selection priority over derivation reservations so the
holdout cohort cannot be shaped by later derivation needs.

The lock also binds one coherent pre-execution compatibility signature,
plan-specific policy signatures, target-video-bitrate ranges, and the configured
numeric quality floor before any derivation observation exists. Revalidate the
partition before creating the later derivation authorization. Any config,
policy, toolchain, or selected-source drift is a stop condition, not a reason to
rewrite the frozen snapshot or mapping.

The private partition and key are machine-local audit artifacts. Only the
reviewed `selection_lock_sha256` and `derivation_partition_sha256` may leave
owner-only storage. The lock carries `runtime_execution_authorized=false`,
`derivation_execution_authorized=false`, and
`holdout_execution_authorized=false`; creating it does not create `acsvda1`.

Validate the pinned machine-local aggregate attestation without printing its
counts:

```bash
uv run python scripts/verify_av1_cold_start_preregistration.py \
  validate-eligibility /path/to/eligibility-attestation-v1.json --json
```

Successful validation emits only `eligibility_valid=true` and false execution
authority flags. It does not emit the attestation ID, cutoff timestamp, or any
aggregate counts.

## Bounded v2 derivation workflow

Issue `#287` uses `av1vdw2` to turn the reviewed partition into one immutable,
owner-only derivation plan. Plan creation revalidates the exact partition
against the current read-only inventory before it embeds the separate
merged-shape `acsvda1` authorization, exactly twenty-four derivation
assignments, and one source commitment for each assignment. Every commitment
binds the assignment ID, local item ID, content-version identity, full source
SHA-256, source size, and frozen evidence-summary SHA-256:

```bash
uv run python scripts/verify_av1_cold_start_preregistration.py \
  create-derivation-plan \
  docs/validation/av1-cold-start-preregistration-v2.json \
  /private/owner-only/eligibility-attestation-v1.json \
  /private/owner-only/av1-v2/source-partition-v1.json \
  --key /private/owner-only/av1-v2/partition.key \
  --valid-until YYYY-MM-DDTHH:MM:SSZ --json
```

The plan authorizes only its twelve motion and twelve darkness reservations.
The command writes `plan.json` only beneath the partition-global canonical
private state root derived from `web_state_dir`; there is no caller-selected
plan path. The root is keyed by the frozen private partition and immutably bound
to the first authorization and plan, so a second authorization cannot rerun the
same reservations under another plan ID.
The canonical `plan.json` is published before its root binding. A retry loads
and validates that exact plan, requires the same authorization window, reuses
its original `authorized_at`, re-hashes all twenty-four derivation sources, and
requires the rebuilt commitments to reproduce the frozen plan exactly before
it creates or verifies the binding. Retrying plan creation therefore requires
the selected media to remain present and byte-identical. The runtime clock is
sampled only for the first successful plan payload. A binding
without its plan is an explicit interrupted-state failure, not permission to
mint another authorization. The plan binds a
privacy-safe digest of the resolved database, review, and web-state locations;
every later command must use that same machine-local runtime context. Every
execution, proposal, and finalization also re-compares the plan's complete
twenty-four-assignment payload with the frozen partition rather than trusting
top-level digests alone, and every write-capable source session revalidates the
plan's complete twenty-four-source commitment set before publication.
Each assignment has one attempt. Execution cannot select a replacement, retry a
source, use a holdout, invoke the validation harness, inject a cold-start or
guided probe, consume local personalization, backfill a historical row, or
activate the public bundle. A claim file is created before media work begins so
an interrupted process cannot silently rerun the same reservation. Attempt,
terminal-intent, terminal-record, proposal, review-claim, review, and
candidate-lock artifacts live under the partition-global canonical private
state root derived from `web_state_dir`; callers cannot select alternate
artifact directories. Each
directory carries an immutable binding to one plan or proposal, so artifacts
cannot be mixed across authorization windows or review sets. The canonical
artifact-root binding also carries a one-way digest of its resolved state-root
path. Moving the private tree to a location that selects a different
runtime-lock domain therefore fails before recovery can write. Recovery derives
the lock domain from the canonical artifact tree and requires it to match the
lock path selected from current config, so aliasing only the artifact subtree
beneath a state root in another runtime-lock domain fails at the same boundary.
Candidate-lock finalization and visual-verdict publication repeat the same check
after taking the runtime lock, canonicalize all subsequent artifact access to
the bound tree, and fail before any artifact or database write when the domains
differ. Aliases that resolve to the same canonical tree and select the same lock
file remain in the same lock domain. Artifact roots created before the
path-bound digest existed are not migrated in place. Likewise, any private
derivation tree created by the pre-remediation branch must be discarded because
its `plan.json` embeds incompatible in-place extensions of `av1vsp1` and
`acsvda1`. Regenerate those derivation artifacts from the merged implementation;
the existing schema-1 partition, token key, selection-lock digest, and
derivation-partition digest remain authoritative and must not be regenerated.
Newly created
claim files and newly created parent directories are fsynced before work can
continue, so a power loss cannot silently erase assignment or review ownership.
Every private JSON artifact is completed under a random dot-prefixed
descriptor-relative temporary name with
`O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW` and
pathname mode `0400`, fsynced, then atomically moved to its final name with the
platform's no-replace rename primitive (`renameatx_np(RENAME_EXCL)` on macOS).
macOS file payloads also receive `F_FULLFSYNC`, and the parent directory is
fsynced after publication. Only an exclusive-name collision is treated as an
idempotent concurrent publish; a write, full-sync, verification, close, or
directory-sync failure propagates even when the final name is visible and must
be re-entered through the runtime-lock-held retry or recovery path before
progress. A surviving orphaned claim is terminalized by that recovery rather
than resuming the claimed action. A retry that finds an identical published
artifact re-fsyncs its parent directory before accepting it, so a prior
directory-sync error cannot be downgraded to an unsynced idempotent success. A
fresh candidate proposal rechecks the live authorization immediately before its
exclusive rename, then verifies that the renamed inode's kernel change time is
strictly earlier than the immutable authorization expiration. Loaders repeat
that publication-time check, so a process paused across expiration cannot make
a late artifact acceptable by retaining an earlier payload timestamp. A
candidate-proposal retry first loads the canonical existing proposal and reuses
its original `proposed_at`; it never samples a replacement timestamp. A review
retry accepts only a complete matching immutable lane claim and envelope,
reuses the envelope's original `reviewed_at`, re-fsyncs the review parent, and
returns without launching another agent. A claim with no complete matching
envelope remains terminal and unresolvable, and any conflicting final artifact
is rejected. A
normal pre-publication failure durably removes its temporary and surfaces any
unlink, close, or cleanup-sync failure. A hard interruption before the rename
leaves only an ignored temporary; after the rename, the canonical name contains
a complete payload, but progress still waits for the parent directory sync.
Reads require one owner-owned link, exact `0400` mode, stable
descriptor/path identity, and unchanged size, timestamps, and inode before and
after the complete read.
On the next runtime-lock-held invocation, an orphaned assignment claim is
terminalized as an unfavorable `interrupted_claim` without rerunning media, and
a persisted non-review attempt missing its terminal record is idempotently
terminalized before any later reservation can run. Recovery first confirms the
same canonical state root and its frozen plan/partition bindings, then runs
before full runtime-context, execution-environment, statistics-contract, live
inventory, or source compatibility checks. Later database, review-directory,
toolchain, policy-code, or source drift therefore cannot strand an existing
claim. Because the state root also owns the global runtime lock, a moved
`web_state_dir` must be restored before recovery rather than silently using a
different lock domain.

Run one fresh assignment at a time. The command revalidates the partition and
current inventory before touching media, uses the exact assigned library item, forces
the existing sampled calibration path to leave the cold-start planner absent,
and preserves an owner-only attempt artifact plus review media. Derivation
review clips are redirected beneath the partition-global private artifact root;
the calibration subprocess tree inherits an owner-only `0077` umask, directories
are `0700`, and generated files begin owner-only. Before the first provenance
fingerprint, the derivation callback opens and pins every preview, source, and
compare clip through the no-follow review-root descriptor. While those
descriptors remain open, post-run validation walks the complete tree, rejects
links, hard links, permission drift, ownership drift, and identity substitution,
and seals every regular file to `0400` through its held descriptor. It performs
no pathname chmod. One macOS path-chain guard per reviewed clip is then kept
active until every clip payload is hashed and every guard passes a final quiet
check. The `cira3` review fingerprint binds all three clip roles, content, a
one-way canonical-path digest, device, inode, modification/change times, mode,
and link count. Existing non-derivation `cira1` and `cira2` records retain their
original preview/source-only recomputation contracts; new compare-aware records
and every derivation attempt use `cira3`. Verdict-time identity checks use the same
all-clips-held procedure, closing the earlier-clip mutation window.
Descriptor/resource or integrity failure is an affected-cell `safety_stop`, not
`media_unavailable` or measured evidence.

Private partition creation first performs logical selection without hashing the
broader eligible library. The merged schema-1 `av1vsp1` payload intentionally
contains no full source SHA-256 or source-size field: its partition ID,
selection-lock digest, derivation-partition digest, and payload digest remain
byte-for-byte compatible with the partition published for issue `#286`.
Selection-time integrity remains bound by each assignment's immutable
evidence-summary SHA-256, the sampled content-version identity, and the complete
inventory snapshot digest. A newly built partition may replay selected
derivation bytes and fingerprint evidence before durable publication, but those
transient full hashes do not alter schema-1 payload bytes or digests.

Plan creation is the first durable full-byte freeze for the twenty-four
derivation assignments. The private `av1vdw2` plan commits each source's full
SHA-256 and size together with its assignment, local item, source identity, and
evidence-summary identity. These commitments contribute only to the plan ID and
plan payload digest; they do not alter `av1vsp1`, the selection lock, the
derivation-partition digest, or `acsvda1`. This timing is explicit: the plan
cannot retroactively prove the exact full bytes present when the schema-1
partition was selected. Full-byte commitments for the holdout cohort remain
out of scope until issue `#288`.

Candidate proposal construction, candidate-lock finalization and verification,
and visual-verdict publication each re-open and fully hash all twenty-four
derivation sources against the plan commitments. They never substitute the
frozen digest for a current byte-level check. Proposal construction is
non-authoritative and reads current observations and inventory through separate
read-only snapshots under the same runtime lease. Finalization, verification,
and verdict publication keep their inventory, observations, and source checks
inside one immediate database transaction. Publication payloads are first
written and fsynced under a hidden owner-only temporary name. Proposal and
candidate-lock publication keep the active source session through exclusive
atomic rename: the session performs another quiet and identity check immediately
before publication and performs its exit check before any database transaction
can commit. Exit validation also runs when publication raises, so post-rename
durability failures cannot skip the final source check. The human-verdict path
uses a different ordering because its database append must remain reversible on
a final source-session failure. After verdict intent, observation append, the
final contract check, and full source verification, the source session completes
its final quiet/exit boundary while the immediate transaction remains open. Only
after that boundary succeeds are terminal artifacts published. A retry accepts
only the exact canonical artifact already visible at the final path,
re-synchronizes its parent directory, and rejects any conflicting or malformed
artifact.

After the immutable assignment claim and before crop, search, encode, or review
work, the runtime opens the assigned source with no-follow semantics, verifies
its inode, size, sampled identity, and frozen full SHA-256, and copies the
complete file into one assignment-scoped, owner-only snapshot under the
canonical artifact root. The snapshot is created relative to a pinned directory
descriptor with `O_CREAT | O_EXCL | O_NOFOLLOW`; an existing assignment name
fails closed and is never overwritten. Its pathname mode is `0400` from the
instant of creation, while write access exists only through the original
`O_RDWR` descriptor, so hard interruption leaves read-only partial residue. The
runtime records the snapshot's full SHA-256 and size in the immutable
calibration payload, reopens it read-only, and routes every source read through
that canonical snapshot path rather than the mutable library path. A macOS
kqueue guard watches the snapshot vnode and
every canonical pathname ancestor throughout media work; write, extend,
attribute, hardlink, delete, rename, or revoke activity is latched even if bytes
or names are later restored. The snapshot must keep one link, the source and
snapshot identities are rechecked, and final guard validation runs even when
media work raises.
The assignment claim itself is authorization-deadline-bound by both a live
pre-rename check and the final inode's kernel change time. After an in-window
claim, authorization is checked again before database preparation, before the
source is opened or each snapshot chunk is copied, and before each calibration
media stage. Expiration at any boundary creates an immutable
`authorization_expired` attempt and terminal without starting the next media
step. An orphaned in-window claim still follows the existing interrupted-claim
recovery path and never reruns media.

Source snapshots are retained as private, write-once assignment artifacts after
success, failure, cancellation, or interruption and are never reused as runtime
inputs. The bounded derivation lane performs no automatic snapshot unlink,
rename, truncation, pathname chmod, directory removal, or recursive cleanup.
Interruption recovery terminalizes the owned assignment and preserves any full
or partial snapshot residue for explicit out-of-band retirement while
Mediaforce is stopped. Creating each snapshot requires free space for the
complete source plus the existing five-gibibyte safety floor. Before execution
authorization, the private operator storage gate must account for cumulative
retained-snapshot capacity across the authorized derivation assignments; the
runtime also rechecks free capacity through the pinned snapshot-directory
descriptor before each assignment.

The repository keeps the full protocol suite active on Linux with test-only
seams for macOS capability admission, the same-filesystem mutation probe, and
kqueue event delivery. Descriptor/path identity checks and all protocol logic
remain active in those fixtures. A focused `file-integrity-macos` CI lane
separately runs the real kqueue capability, probe, transient-write, hardlink,
path-chain, partition, and derivation tests. Production capability admission
and runtime monitoring remain fail closed and are never bypassed by these test
seams.

The `av1vdw2` plan binds the resolved database, review, and state roots together
with the current machine, Python executable, every regular non-cache file under
the Mediaforce package tree, the repository-owned preregistration runner,
`pyproject.toml`, `uv.lock`, the shared CLI/runtime-lock implementation, AV1
encoder/metric toolchain, source-integrity guard contract, and merged
statistical contract. Adding, removing, or changing any bound implementation
file changes the plan's execution-environment digest. The plan also binds
SHA-256 digests of the canonical Every Code executable path and binary without
persisting or printing the private path. The embedded merged-shape `acsvda1`
authorization continues to bind only the manifest, selection lock, derivation
partition, authorization window, and derivation-only authority. Because that
authorization payload is itself embedded in the plan, the plan digest binds the
authorization, runtime context, execution environment, statistics contract,
review runner, and source commitments as one immutable unit. Proposal and
review publication require the executing verifier to be the canonical
repository file and re-check the plan-bound execution environment immediately
before each immutable write. A
real same-filesystem kqueue mutation probe must pass before the immutable
assignment claim is written. The probe creates a private owner-only file,
sets its pathname mode to `0400` from creation, mutates only its original
`O_EXCL | O_RDWR` descriptor, and retains the tiny probe directory instead of
recursively deleting a mutable pathname. Independent review execution instead
uses one private copied Every Code runner and one owner-only committed
repository snapshot per lane. Both must pass unchanged post-run identity checks
before their local cleanup can complete. Assignment execution holds the same
exclusive runtime lock as
`mediaforce-web`. Every write-capable `mediaforce` CLI command and each direct
partition-key, partition-build, and derivation artifact-publication action
acquires that lock too, so web, staging, database, cleanup, proposal, and
review-publication work cannot overlap the bounded derivation case. Config
loading itself is read-only; legacy state-path migration runs only after the
exclusive lock is held. Machine or toolchain drift before the claim or after
measurement is a safety stop rather than a newly compatible cohort.
The shared lock uses a stable parent-directory guard in addition to the metadata
file. It also holds deterministic persistent reservations under the configured
`state.runtime_reservation_dir`, with path-local anchors for the writable
database and metadata-lock namespaces. Config directories remain read-only.
Normalized namespace keys are represented only by SHA-256 directory names;
reservation directories are owner-only, their lock files are empty and
owner-only, and runtime shutdown never unlinks either one.
Each reservation locks both the key directory and its lock file, so removing and
recreating the lock-file pathname cannot split an active reservation. Existing
config inodes are also locked directly. Existing databases additionally receive
a nonblocking open-file-description byte-range lock on one Mediaforce-owned byte
that is disjoint from SQLite's lock bytes. That inode-bound reservation works
across hardlink aliases and configured reservation roots without interfering
with SQLite WAL locking. A missing database is materialized and receives the
same reservation after legacy state-path migration and before any other database
work, while the global runtime lock remains held. The stable configured
reservation root covers lock-parent replacement,
while descriptor-relative `O_NOFOLLOW` opens and device/inode rechecks protect
every reservation, direct config lock, and metadata-lock handoff. Unlinking the
metadata lock, changing configured state paths, or renaming and replacing its
parent cannot create a second lock domain for another compliant runtime. The
lock path resolves `web_state_dir` symlinks before choosing that domain, so
aliases cannot create a second one. Guarded SQLite engines verify the reserved
database identity before and after connection creation, every cursor operation,
writable transaction commit, and the raw legacy-schema bootstrap. Connection
creation pins the resolved parent directory and expected database inode, then
opens SQLite through the parent directory's stable kernel identity: `/.vol`
directory identity on macOS and a retained `/proc/self/fd` directory handle on
Linux. The original read-only or read-write URI query is preserved, so WAL and
read-only enforcement remain SQLite-native. The expected file handle stays
retained for the SQLite connection lifetime, and its device, inode, change
time, and link count must remain identical to both the pinned directory entry
and resolved path after connection creation. Unsupported pinned-path identity
inspection fails closed. A database swap-and-restore, transient ancestor
substitution, or unrelated concurrent open therefore cannot redirect or spoof
the returned connection.
Persistent or transient path replacement during connect, query, migration, or
commit fails closed before later evidence publication can proceed without
weakening read-only URI or WAL behavior.
Fresh execution installs its absolute authorization deadline before toolchain
preflight. Every managed command then starts through Mediaforce's private
isolated deadline runner. The runner refuses to execute the target after the
deadline, arms an independently scheduled process-exit watchdog first, and the
watchdog immediately kills the complete target process group at expiration even
if the parent operator process is stopped. If a group leader exits while its
descendants remain, the watchdog keeps policing the process group until every
descendant exits or the deadline kills the group. A dedicated status pipe
reports explicit clean completion, deadline expiry, or enforcement failure.
The watchdog sends the deadline kill before reporting expiry; a kill error other
than an already-absent process group reports enforcement unavailable instead.
The parent retains the target process-group identity until final status EOF is
consumed and validated. Empty or unexpected status, status-read interruption,
and watchdog unavailability all trigger parent-side descendant cleanup before
the retained group is released, while cleanup failures remain notes on the
original exception. Toolchain and quality-metric capability probes use
that same controller and deadline; the assignment's already-frozen quality
metric and target are selected without launching a second unmanaged probe.
Fresh execution samples its authorization
timestamp only after all preflight checks and immediately before the immutable
claim.
`scripts/mediaforce-dev.sh` leaves that persistent lock path in place when
stopping the backend.

```bash
uv run python scripts/verify_av1_cold_start_preregistration.py \
  run-derivation-assignment \
  docs/validation/av1-cold-start-preregistration-v2.json \
  /private/owner-only/av1-v2/source-partition-v1.json \
  '<web_state_dir>/av1-validation-derivation/<partition_id>/plan.json' \
  av1vderive1_<opaque-slot> \
  --key /private/owner-only/av1-v2/partition.key \
  --config /private/owner-only/mediaforce.toml --json
```

Successful technical attempts remain `review_pending` until a human records an
explicit visual verdict. The verdict path appends the existing current-contract
`ContentIntentBoundaryObservation`; it never infers a verdict. Derivation rows
are active evidence for the bound candidate snapshot but are explicitly marked
ineligible for local personalization, so later planning and holdout execution
cannot consume them as warm-start evidence. The complete derivation terminal is
preceded first by an immutable verdict claim. The claim exists before database
open, `BEGIN IMMEDIATE`, inventory reload, current-input validation, or review
media verification; a hard interruption before the later verdict intent is
recovered as the affected cell's immutable `safety_stop`, never as a retryable
human verdict. A first verdict whose claim publication reaches authorization
expiry publishes no claim and terminalizes the affected cell before database
open. A claim published in-window without a matching verdict intent remains an
interrupted safety stop, while an immutable in-window verdict intent remains
eligible for idempotent completion after expiry. After those checks, an
immutable verdict intent freezes the first human input and runtime timestamp.
The validated observation is appended inside
the immediate database transaction, then the execution contract is checked one
final time. The source cohort then completes full verification and its final
quiet/exit check while that transaction remains open. A final source-session
quiet, close, or exit failure after verdict intent therefore rolls back the
observation before publishing the separate immutable stopped `safety_stop`
terminal for the affected cell. Only after the source-session boundary succeeds
are the immutable observed or excluded terminal intent and terminal record
written, still before the database transaction can commit. An append conflict
or late contract drift uses the same rollback-and-stop path rather than
producing a false observed terminal.
A database-open, transaction-begin, inventory-load, current-input, contract,
review-media, or other pre-publication failure uses the same conversion; when a
transaction exists, rollback completes before the stopped terminal is
published.
A terminal-intent or terminal-record I/O failure occurs after the final source
quiet boundary, rolls back the database append, and propagates as a retryable
publication failure rather than being converted into a safety stop. The retry
reuses the frozen verdict intent and accepts only identical terminal artifacts.
If the database commit fails after the immutable record exists, an interrupted
retry likewise reuses the frozen verdict timestamp and idempotently completes
the same observation without replacing or duplicating evidence. If interruption
occurs after the terminal intent but before its terminal record, recovery copies
that exact immutable intent into the canonical terminal-record directory before
considering any later assignment. An observed terminal whose database projection
is still absent continues to block progress until the frozen verdict retry
idempotently commits it. The recovered terminal record does not prevent that
same frozen verdict from running again; it prevents only later assignments from
advancing around the missing database projection. Review-media
identity is freshly recomputed
immediately before the verdict and must still match the frozen attempt. The
complete verdict claim, media recheck, immutable verdict intent, immediate
database transaction, observation append, and terminal-intent/terminal-record
path holds the shared runtime lock.
Inside that lock it reloads the canonical plan and attempt, revalidates the
current partition inputs and execution contract, and fails closed on drift.

```bash
uv run python scripts/verify_av1_cold_start_preregistration.py \
  record-derivation-verdict \
  docs/validation/av1-cold-start-preregistration-v2.json \
  /private/owner-only/av1-v2/source-partition-v1.json \
  '<web_state_dir>/av1-validation-derivation/<partition_id>/plan.json' \
  av1vderive1_<opaque-slot> \
  --key /private/owner-only/av1-v2/partition.key \
  --verdict approved \
  --config /private/owner-only/mediaforce.toml --json
```

Rejected, excluded, failed, stopped, and missing outcomes remain visible and
cannot be replaced. The next reservation cannot run until every prior technical
attempt has its human terminal. In accordance with the preregistered
`safety_stop_scope=affected_cell`, a terminal unsuccessful attempt or
unfavorable reviewed record permanently stops only that candidate cell. The
same immutable stopped terminal is written when current-input drift,
execution-contract drift, review-media identity failure, descriptor/resource
failure, or observation conflict prevents a pending human verdict from being
published; those failures cannot be retried as a more favorable review.
The
global immutable assignment order remains authoritative: later assignments in
the stopped cell are never allowed, while the next assignment in another cell
may proceed only at its original canonical position. Because each candidate has
exactly twelve reservations and requires twelve eligible observations, the
affected candidate is an exact no-go without stopping collection for the other
candidate.

Candidate derivation uses every retained terminal record. The proposal and final
candidate lock retain opaque source, logical-title, series, and source-group
token sets plus the sorted source-group token for every derivation observation.
That multiplicity-preserving projection makes the six-group minimum and
one-third concentration maximum independently verifiable after serialization,
while later holdout evidence can recheck within-holdout title uniqueness and
every derivation/holdout overlap dimension. No raw title or source identity
enters those artifacts. For an eligible cell, the CRF bounds are the full
observed minimum and maximum with the median center;
no trimming is allowed. CRF MAD must be at most `2.0`, the full span must be at
most `6`, and the range must contain an executable integer CRF. The bitrate
applicability range is the minimum and maximum frozen assignment
`target_video_bitrate_bps`, not the measured visual boundary bitrate. Measured
boundary bitrates remain the dispersion and confidence input. The quality floor
remains the numeric value frozen in the partition. Confidence reuses the
existing bitrate relative-MAD categories and records
`round(1 - relative_mad, 3)` after the hard evidence and preregistered
independent-source minima; limited confidence or a score below `0.7` is a no-go.
Proposal and lock types independently enforce exactly one series token per
observation, the preregistered source minimum, a CRF span no wider than `6`, and
freshness at proposal and lock time. The proposal records the measured CRF MAD,
bitrate relative MAD, derived conflict count, and pre-run statistical-contract
digest. A
proposal exists only when the conflict count is zero, so reviewers can verify
the exact applied dispersion and confidence contract from immutable evidence.

The full-search proof validates the native target-size trace contract, including
the first `target_seed`, any later `compression_floor`, `expanded_bound`, or
`refine` measurements, the native curve/retry policy, and exact selected CRF,
quality, and bitrate projection values. The persisted sample stream-budget
ledger must also retain the exact frozen assignment
`target_video_bitrate_bps`; a changed or missing remaining-video-bitrate value
invalidates the attempt. A one-point `target_seed` trace remains valid when the
unchanged search naturally selects its first measured candidate; the workflow
does not force exploratory probes that production search would not perform.

The resulting proposal is explicitly non-authoritative. Finalization requires
five proposal-bound approvals from distinct completed Every Code agent runs:
architecture, statistical/model-contract, privacy/security,
experimental-design, and adversarial. `record-derivation-review` launches a new
read-only `code exec --json` process itself; it accepts no caller-supplied result
path. Before launch it atomically creates one immutable proposal/lane claim that
binds the run nonce, authorization, proposal, lane, canonical runner-path
digest, and runner-binary digest. A concurrent or repeated lane cannot replace
that claim; a crash before a complete lane envelope exists leaves an unresolved
terminal claim that cannot launch another agent. A retry after the complete
matching claim and envelope are visible validates both, re-fsyncs the envelope
parent, returns the original decision and `reviewed_at`, and does not launch a
second agent. A rejected review remains terminal. The review command and its
repository/toolchain probes remain under the plan's absolute authorization
deadline. Before claim publication, the reviewer records the exact Git commit
and tree, rejects uncommitted tracked changes, and repeats that repository
identity check after the run. It never launches Code in that live source
worktree. Instead it creates an owner-only local clone without shared object
hardlinks, removes `origin`, checks out the claimed commit detached, and verifies
the exact HEAD, tree, no-remotes state, and fully clean tracked/untracked state
before and after the run. Code receives that committed clone as its working
directory, so an untracked live-worktree file cannot affect the review. Snapshot
identity drift fails closed, and the stable owner-only temporary root is removed
through the symlink-safe cleanup path. The prompt, completion marker, and
canonical run evidence all bind the same commit and tree, so later validation
cannot silently reinterpret an approval against another repository snapshot.
All five immutable claims must name exactly that same repository commit and
tree; both review-set validation and candidate finalization reject a divergent
lane, and the review-set digest includes the unanimous commit and tree
explicitly. The exact PATH-selected runner must match the authorization
before launch and again after completion. The already-verified authorized bytes
must be a native Mach-O executable, so a shebang wrapper cannot delegate to an
unbound interpreter. Its canonical path must also match the active ancestor Code
process that is conducting the operator session, so a different PATH-selected
native binary cannot establish its own trust root. Those bytes are executed from
an owner-only ephemeral copy
rather than reopening the mutable PATH-selected source. After an unchanged run,
the copy is unlinked and its empty owner-only directory is removed without a
recursive delete; an integrity failure leaves any suspicious replacement
untouched. The review process uses
a fixed system `PATH` and a strict allowlisted process environment containing no
caller secrets; agent shell commands use the same explicit minimal environment
with no caller-environment inheritance. On the macOS execution host,
a held no-follow descriptor and the canonical path-chain kqueue guard reject any
write, delete, rename, link, revoke, or parent-path substitution event on that
copy before its output can become evidence; unavailable secure monitoring fails
closed. Each attestation binds the claim plus
the SHA-256 digest of the canonical immutable owner-only completion transcript,
and the loader recomputes that digest before trusting it. The attestation and
transcript are written together as one immutable atomic lane envelope. Duplicate
run IDs or transcript digests are rejected. The completed process must emit a
matching quiescent agent message whose final non-empty line is the exact
proposal-, lane-, claim-, and run-bound `MEDIAFORCE_AV1_REVIEW_V2` JSON marker;
the JSONL parser requires one ordered config record and one ordered prompt,
rejects duplicate JSON keys, reserved-field mixing, malformed or repeated
completion, and any event after quiescence, and treats only LF/CRLF as record
boundaries so Unicode line separators remain inside their JSON strings.
The decision is extracted rather than supplied by the operator. Public summaries
expose only that the runner identity is bound, never the canonical private path.
Review, verdict, proposal, lock, and assignment-claim payload timestamps come
from the runtime clock only for their first immutable publication; recovery
reuses persisted timestamps. Fresh assignment-claim, proposal, candidate-lock,
verdict-claim, and verdict-intent publication samples the live clock immediately
before the exclusive rename and fails closed if authorization expired during
preparation. Review claims and envelopes are produced by the bounded reviewer
path and rely on the stronger final-inode publication receipt. After every
deadline-bound rename, the writer verifies the inode's kernel change time
against the same expiration; every loader repeats that check. Recovery of an
already-published authoritative artifact skips live-clock sampling but still
rejects a kernel-observable post-expiration publication. Assignment claims are
the narrow recovery exception: a claim whose rename landed after expiration is
loaded only as terminal evidence, marked late in memory, and recovered as
`failed/authorization_expired`. It can never authorize work or permanently
poison recovery. A deadline expiry raised by the final execution-contract
recheck keeps that same `failed/authorization_expired` classification; deadline
enforcement failure remains a wrapped contract safety stop rather than being
misreported as ordinary expiry.
Finalization requires exactly five resolved claims and five matching approvals
over one unanimous repository commit/tree identity; any unresolved, divergent,
or rejected claim blocks it permanently.
Finalization loads configuration once, opens an immediate database write
transaction, revalidates the frozen partition against inventory in that same
transaction, resolves every frozen source commitment, rereads current
observations, and fully verifies the source cohort. A new `locked_at` is sampled
exactly once only after those current inputs and evidence are loaded and
verified; freshness and authorization are then evaluated at that timestamp
before publication. The live authorization is checked again immediately before
the first exclusive candidate-lock rename. Recovery of an existing
candidate-lock envelope instead
reuses its persisted `locked_at` and never calls the runtime clock. The lock is
compatible with the canonical V1 chronology
`locked_at <= reviewed_at <= authorized_at`; because these artifacts use whole
UTC seconds, a review and its later authorization may legitimately share the
same serialized second. The lock is computed and written through one
runtime-owned finalization API. That API
derives the canonical root from config and the immutable plan path, loads the
attempts, terminals,
proposal, review claims, and review envelopes itself, and exposes no
write-capable caller path that accepts a precomputed evaluation. A finalized
candidate lock still does not authorize holdout execution or mutate the shipped
public bundle.

The candidate-lock file is a provenance envelope, not a standalone lock. It
commits to the plan and derivation authorization, proposal, five immutable
review claims, their unanimous repository commit/tree identity, five atomic
review envelopes, canonical artifact-root binding,
candidate lock, and terminal snapshot. #288 must load it through the verified
derivation-chain loader and must reject a raw or synthetic candidate lock. That
loader also derives the
root from config, reloads the full chain, and rechecks current database evidence
inside an immediate transaction. These owner-local hashes detect
stale, copied, mixed, and partially replaced artifacts; they do not claim to
authenticate against a malicious machine owner who changes code and rebuilds a
fully self-consistent private evidence tree. The coordination and runner checks
apply to documented Mediaforce CLI and web entrypoints. Importing private Python
helpers, replacing the interpreter or verifier, or modifying source while
deliberately bypassing those entrypoints is outside the owner-local threat
model; such activity invalidates the review and requires discarding the private
evidence tree and restarting from the merged protocol.

## Trait reachability preflight

Before a future preregistration is treated as executable, run the read-only
trait feasibility report against the proposed manifest and an explicit UTC
cutoff:

```bash
uv run mediaforce av1-trait-feasibility \
  docs/validation/av1-cold-start-preregistration-v1.json \
  --as-of 2026-07-27T18:00:00Z --json
```

The `acstp1` projection reclassifies compatible retained fingerprint analysis
without reading media. The report emits only aggregate item, parent-scope,
source, series, source-group, coherent compatibility/policy cohort, and bitrate
range counts. Paths, titles, source IDs, fingerprints, review-media identities,
compatibility tokens, and policy tokens are never emitted. The report is
deterministic for the same database snapshot, manifest, and `--as-of` value.

`ready_for_private_mapping` means only that current code can produce the
requested trait/intent path and aggregate inventory and derivation upper bounds
meet the preregistered minima. Parent-scope diversity is a conservative public
preflight; the private mapping must still prove unique titles/series, source
group concentration, derivation/holdout disjointness, and every other selection
constraint. Every feasibility report carries `execution_authorized=false` and
`runtime_integration_required=true`; it cannot start Mediaforce, alter the v1
request path, or authorize an encode. The v2 successor binds this prospective
projection without changing production routing.

The current analyzer deliberately reports low-motion dialogue as unreachable,
and the current request contract reports unknown fallback cases as unreachable.
Low-confidence animation or grain/noise advisories cannot silently create those
cell identities. The v1 preregistration remains immutable; these findings guide
the versioned successor protocol rather than relabeling v1 evidence.

## Isolated validation harness

`mediaforce/tuning/av1_validation_harness.py` defines the `av1vh1`
validation-only context and trace contract. It consumes an already reviewed
candidate lock, its bound execution authorization, and a public-safe machine
binding. It does not call the normal cold-start predictor, load private replay
evidence, mutate the packaged bundle, register a web/runtime route, or discover
media.

The harness deliberately reuses the existing target-size search rather than
simulating its control flow. A guided dry run creates one
`QualitySearchWarmStart` with source `av1_validation_harness`. The existing
search records the locked first probe and, when that probe is rejected or
fails, continues through the unchanged measured full-search path. The harness
then validates and sanitizes that target-size trace into ordered work:

- exactly one `locked_first_probe` for a recommended guided case
- zero locked probes for baseline or no-recommendation cases
- one or more `measured_full_search_probe` entries whenever fallback is invoked

The canonical public trace binds the manifest plan, candidate lock,
authorization, compatibility and policy signatures, bitrate/byte target,
quality floor, transform plan, search signature, encoder/metric toolchain, and
ordered observed work. Source paths, titles, source IDs, fingerprints, private
mapping tokens, review-media identities, and raw database rows are never
copied into the trace. Noncanonical bytes, missing warm-start evidence,
tampered CRFs, mismatched machine bindings, stale or under-supported locks,
private local personalization, and operator-authored success claims fail
closed.

The harness is bound by the v2 preregistration and accepts v1 or v2 manifest and
plan IDs while preserving the same isolated execution semantics. Its traces do
not grant runtime authority, select media, create candidate locks, or activate
the public bundle. Fallback reasons are allow-listed so a trace cannot carry
operator-authored titles, paths, or arbitrary private text.

## Evidence lifecycle

The v2 evidence lifecycle has six boundaries:

1. Validate the checked-in v2 manifest against its digest-bound aggregate
   eligibility attestation. This does not authorize runtime execution.
2. Map the protocol's case slots and separate derivation pools to eligible media
   in the canonical owner-only `av1vsp1` private partition. Independently review
   the exact partition, and commit only its SHA-256 selection lock and
   derivation-partition digest to later redacted evidence.
3. Create the separate derivation-only authorization, then build a candidate
   lock from current-contract observations collected through unchanged measured
   full search. Each
   candidate requires at least twelve eligible observations from six
   independent sources. The lock carries opaque derivation source, series, and
   source-group tokens so the holdout mapper can prove there is no overlap.
   Historical rows may not be relabeled or backfilled.
4. Independently review the exact traits, CRF range, compatibility and policy
   signatures, bitrate range, numeric quality floor, confidence, derivation
   freshness/conflict state, derivation snapshot, and selection lock.
5. Create one immutable holdout execution authorization that binds the manifest,
   selection-lock digest, and every reviewed candidate-lock ID. Every result
   must be later than this authorization, and evidence finalization must be
   later than every result.
6. Record one redacted result for every preregistered case, then aggregate it
   with the verifier. Missing, failed, incompatible, contaminated, or
   safety-stopped cases remain visible and block the affected cell.

Candidate locks and result evidence are local audit artifacts. They may carry
opaque random source, series, source-group, compatibility, policy, and review
environment tokens, but never paths or human-readable media identities. The
aggregate report removes those tokens, commits to the private evidence payload
digest, and is the only publication-safe output. Rebuilding a different
self-consistent manifest is insufficient: reporting requires exact equality
with the checked-in issue #277 preregistration.

No database schema migration is required. The protocol, partition contract, and
report builder are pure Python and do not import database, scheduler,
web-runtime, subprocess, or media probing code. The partition inventory adapter
is a separate read-only database/config boundary and does not open a writable
connection. Any one-time legacy state-path migration is operational startup
work and occurs only inside the shared runtime lock.

## Paired holdout contract

One activation case compares the same content-version/intention/compatibility
unit under:

- baseline: the unchanged full measured search
- guided: one candidate first probe followed by the same measured fallback

The baseline-selected CRF is the independent value used for range-hit scoring.
The guided arm may not stop early, bypass final validation, change the quality
floor, change the size target, or use private local personalization. Every
guided result must show `local_evidence_present=false` and a blinded visual
review. The locked numeric quality floor must agree with each arm's measured
score and quality-floor flag, and both paired final outputs must independently
meet the quality, size, and visual-acceptance requirements.

Holdout cases must map to unique titles and series. At least six independent
source groups are required, and no source group may provide more than one third
of a candidate cohort. The private selection lock is the audit proof that the
mapping was frozen before results were reviewed.

## Publication decision

The preregistration is intentionally stricter than the predictor's minimum
schema gates. Each candidate cell requires:

- sixteen complete paired held-out cases
- a moderate-or-higher candidate confidence score of at least `0.7`, derived
  from at least twelve eligible observations across six independent sources
- no conflicting derivation evidence and no derivation observation older than
  180 days when the candidate is locked
- at least thirteen range hits, expressed as a one-sided exact binomial result
  against the preregistered 50% null with `p <= 0.025`
- a one-sided paired sign result with `p <= 0.025` showing more candidate-count
  wins than losses, plus lower aggregate guided candidate work
- a locked CRF span no wider than six points
- zero quality-floor, final-size, blinded-visual-rejection, or
  operator-attention regressions
- duplicate blinded review on preregistered ordinals 4, 8, 12, and 16 by a
  separate review-environment token, with at least 80% agreement and no
  duplicate-review rejection where baseline was accepted
- the exact candidate lock, compatibility, policy, trait set, bitrate range,
  quality floor, and reviewed selection lock used by every case

The `0.025` per-cell threshold controls each separately preregistered claim. V1
registered animation and typical live action; v2 registers motion and darkness.
Reports remain per-cell and do not make a pooled claim that every AV1 prior
works.

Failure does not lower a threshold or widen a cell. The permitted conclusion is
`insufficient evidence to activate this cell`; the existing no-recommendation
and full-search behavior remains in place.

Fallback-conformance cells also have one exact preregistered reason. Risk-trait
cells require `content_profile_uncovered`; directional-intent cells require
`compression_intent_requires_directional_search`. A generic
`no_public_evidence` result does not prove those guards.

The aggregate report may mark one cell ready while rejecting another. It never
creates confidence, builds a public cell, mutates the checked-in prior bundle,
or decides #256/#262 automatically. Those remain explicit governance steps.

## Reporting

When redacted evidence exists:

```bash
uv run python scripts/verify_av1_cold_start_preregistration.py \
  report docs/validation/av1-cold-start-preregistration-v1.json \
  /path/to/private/redacted-evidence.json \
  --as-of YYYY-MM-DDTHH:MM:SSZ --runtime-state paused --json
```

`--as-of` is explicit so report IDs remain deterministic and future-dated
authorizations, results, or finalization timestamps fail closed. The supplied
runtime state is report context only; it is not execution authorization.

Exit codes are:

- `0`: the manifest is valid, or a report is ready for manual publication
  review
- `2`: evidence is valid but incomplete, rejected, or still blocked
- `1`: malformed, tampered, incompatible, or non-canonical input

The runtime remains paused until the private selection mapping and every
candidate lock have been independently reviewed and the bound execution
authorization has been created. This change does not start a cohort, discover
media, generate review clips, or run a real-media experiment.
