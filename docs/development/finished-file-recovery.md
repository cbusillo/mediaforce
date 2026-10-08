# Make a finished file again

The folder workspace offers **Make again** beside each unpublished file whose
only failed check is its approved final size, and beside checked files whose
settings history is missing. It uses the existing per-file
`POST /api/folders/{prefix}/size-held-decision` action with
`library_item_id` and `keep: false`.

Select several files on the show or season page and use **Make selected again**
to queue them in one action. The same endpoint accepts `library_item_ids: [id, ...]`
with `keep: false`; bulk Keep is not supported. Each file is rechecked separately.
A file refused by the remake checks keeps its compressed copy and gets its own
reason in `left_out`. An unexpected error on one file is reported without
preventing completed recoveries or other eligible files from reaching the queue.
Each completed file is committed before checking the next,
so an interruption cannot roll back earlier recovery records and a slow host
does not hold the database write lock for the whole batch.
Eligible files sharing their saved scope and mode form one normal folder run,
with the usual host shards. Different saved scopes or modes form separate runs
rather than broadening a season override. The response names queued and removed
item IDs and includes the queue results in `runs`. Before removal, each file's
finished-file record saves its remake scope, mode and current approval. A later
queue refusal retains that request and explicitly reports that the copy was
removed. Retry **Make again** on the same file or selected IDs after the named
refusal clears; a fresh request reads the saved settings from the database,
including after a restart. This is an explicit retry, not an automatic one.
Once removal is confirmed in the saved request, retries do not repeat cleanup
on its host; an offline former host does not produce a false removal failure.
Original access, path protection, active-work and queue-admission checks still run.
The integrity details keep the request and retry guidance visible. A confirmed
removed copy counts as work awaiting queueing, including copies made on a remote
host, and points to Make again instead of missing-output repair.
An unexpected queue failure is reported for its group; the other groups are still
attempted. The workspace refreshes removed files even when none were queued,
and shows partial refusals as an attention notice. Single-file Make again uses
the same refresh behavior. Removal/queue facts in the response are preserved
even when a host refusal mentions a timeout or access failure.

A final-size failure requires a fresh approved sample and a changed goal under
the same contract check used for failed-run recovery. Older multi-file runs
need a changed size goal and compare the missed episode's own duration and target; an unnamed or unreadable
comparison stays blocked. If an earlier legacy comparison recorded the miss
under the newly approved goal, verified recovery appends a resolution event
without erasing the miss. The resolution survives removal of the terminal job
record; a later return to the old missed goal still stays blocked. Missing settings history requires a current approval
and a new encode rather than accepting the old output without that history.

The action rechecks eligibility and the queue’s run-level and saved per-file
size-miss guards before removing the staged output. These checks are read-only;
verified legacy recoveries are recognized without rewriting their history. An active
encode that has started or an active sample run blocks removal. A queued run
allows another remake request for files outside its membership, creating a
separate compatible run. Unreadable queued membership or a file already named
in that run blocks removal. Every remake needs a current approved sample. The original must be accessible, and the staged path must
not point at it; restore source access before retrying when it is unavailable. It then removes that file's staged copy and partial output,
returns that library item to planned, and queues only that item at its run's
scope and mode, including older seasons. Pre-mode manifests retain manual season
overrides through the existing legacy selection-provenance reader. If its terminal job was cleared,
the saved run scope supplies the prefix. If the manifest itself is unavailable,
the existing database run selection preserves that scope and mode when the mode
was recorded. For a pre-mode run, restore its manifest so selection provenance
can establish whether the season was manually overridden. If neither
record can be read, restore the saved run settings from a run backup before
retrying; the staged copy stays in place. A size failure also needs its run manifest
for the goal comparison; restore that manifest from a run backup when it is missing. The original and other staged outputs
stay in place. If queueing fails after removal, the file remains planned, with its
saved request retained. The ordinary folder queue is not the saved retry: it can
wait for overlapping work and uses that action's chosen mode and membership.
The saved retry never broadens a manual season selection; it queues only the
named IDs and waits if active work might include them. Disjoint queued work can
continue while the saved retry forms a separate run.

Saved retries require the same approval, checked before removal and again inside queue admission.
If it changed, nothing is queued: restore the saved sample approval before using
the saved retry. To intentionally use different approved settings, use the
normal queue action with the intended scope and mode, after existing overlapping
work finishes; this is a new production request rather than a saved retry.
All reserve, review, lifecycle and per-file guards still apply.
Any accepted queue action clears removed remake records in its transaction,
including an intentional new request under another approval. Old completion
history cannot make an interrupted new encode look finished.
Cadence-only production holds are unchanged.

If cleanup is refused before removal, the newly saved request is cleared while
the finished copy's history remains. A request left by an interruption can also
be renewed under the current approval when the copy is verifiably still present;
the original eligibility and changed-goal checks run again. When the copy is
gone or its presence cannot be confirmed, the saved approval remains required.

If removal succeeds but a later database write fails, `removed_library_item_ids`
still names the removed file and the response says its recovery was not saved.
The earlier saved request and original finished-file context remain committed.
Once the database is healthy, **Make again** retries idempotent removal and
finishes planning under those settings. **Keep this file** refuses a removed
copy. When cleanup failed and the smaller-than-predicted copy is still present,
Keep cancels the saved request and follows its existing validation contract.

**Keep this file** remains available only for the existing smaller-than-predicted
hold. It cannot waive a final-size failure, another failed check, or missing
settings history. The integrity response includes per-file `remake.reason` and
`remake.blocked_reason` so the UI explains unavailable actions; the action
rechecks these conditions rather than trusting the displayed response.

Within one integrity-page response, remake checks reuse each run manifest's parsed
record, including an unreadable result. The next page request reads it again.
The per-file decision reads the manifest and current guards afresh under its
write lock; displayed availability is never authorization to remove a copy.
