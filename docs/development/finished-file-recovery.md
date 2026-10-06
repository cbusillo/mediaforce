# Make a finished file again

The folder workspace offers **Make again** beside each unpublished file whose
only failed check is its approved final size, and beside checked files whose
settings history is missing. It uses the existing per-file
`POST /api/folders/{prefix}/size-held-decision` action with
`library_item_id` and `keep: false`.

A final-size failure requires a fresh approved sample and a changed goal under
the same contract check used for failed-run recovery. Older multi-file runs
compare the missed episode's own duration and target; an unnamed or unreadable
comparison stays blocked. If an earlier legacy comparison recorded the miss
under the newly approved goal, verified recovery appends a resolution event
without erasing the miss. The resolution survives removal of the terminal job
record; a later return to the old missed goal still stays blocked. Missing settings history requires a current approval
and a new encode rather than accepting the old output without that history.

The action rechecks eligibility before removing the staged output. An active
run blocks removal. The original must be accessible, and the staged path must
not point at it; restore source access before retrying when it is unavailable. It then removes that file's staged copy and partial output,
returns that library item to planned, and queues only that item at its run's
scope and mode, including older seasons. If its terminal job was cleared,
the saved run scope supplies the prefix. The original and other staged outputs
stay in place. If queuing fails after removal, the response says so and the item
remains planned for the supported queue action.

**Keep this file** remains available only for the existing smaller-than-predicted
hold. It cannot waive a final-size failure, another failed check, or missing
settings history. The integrity response includes per-file `remake.reason` and
`remake.blocked_reason` so the UI explains unavailable actions; the action
rechecks these conditions rather than trusting the displayed response.
