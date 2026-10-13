# Releases

Agents deploy Mediaforce `main` to the controller themselves with a staged,
gated deploy script. The Director decided this on 2026-10-13
([direction#82](https://github.com/cbusillo/direction/issues/82)). Launchplane
does not manage Mediaforce. A system-owned release flow
([#730](https://github.com/cbusillo/mediaforce/issues/730),
[Launchplane#3184](https://github.com/cbusillo/launchplane/issues/3184)) is a
possible future replacement, not a gate on deploys today.

## Staged deploy

Each deploy gets its own folder under
`~/Library/Application Support/mediaforce/deployments/<branch>-<sha>-<date>/`,
holding a `deploy.sh` copied from the previous one with its target and comments
changed. The script:

1. refuses to run unless the encode queue is paused and no encode, sample or
   full run is running (pause with `POST /api/encode-queue/pause`; queued work
   survives the restart);
2. refuses a dirty runtime checkout;
3. stops the login item and waits for port 8777 to close;
4. takes an online SQLite backup to `before.sqlite` and checks it with
   `pragma quick_check`;
5. checks out the target commit detached, runs
   `uv sync --active --locked --inexact --no-install-project --no-dev`, and
   replaces `frontend/build` with a prebuilt `frontend-build/` staged beside the
   script (built from a clean worktree at the same commit), keeping the old one
   as `frontend-build.before/`;
6. starts the login item (retrying once), waits for the API, and resumes the
   queue.

Database migrations run when the service starts. The script's header names the
rollback: the previous commit and, when a migration ran, restoring
`before.sqlite` with the service stopped. The hourly cleanup keeps the newest
`state.cleanup.kept_deploy_backups` deploy folders (two by default), so the
running build and its rollback stay on disk.

## Work evidence

`GET /api/release/work` reports fresh, uncached database work evidence:

```json
{
  "observed_at": "2026-10-09T20:00:00+00:00",
  "counts": {
    "pending_encode": 1,
    "active_encode": 1,
    "active_calibration": 0,
    "unfinished_scan_rows": 0,
    "active_evidence": 0
  }
}
```

`pending_encode` uses the same runnable-work counter as the dashboard's
`encode_queue.pending_work_count`: queued or retrying episodes count even when
their parent needs attention. `active_encode` includes queued, retrying and
running jobs, including folder parents and children. Counts overlap; do not sum
them to display a number of files. Samples/full runs and evidence work are also
counted. Paused queues still contain work; terminal jobs and
samples waiting only for human review do not count as database execution work.
`unfinished_scan_rows` covers the catalog traversal recorded in `scan_runs`,
not the entire scan job.

The response contains only counts and the observation-start timestamp, read
within one SQLite transaction. It deliberately has no idle or release-ready
flag, even when every count is zero. Two
ordinary kinds of running work are not represented by these counts:

- A scan can be queued before its database row exists, or continue refreshing
  external metadata after that row completes. The authoritative job state is
  loaded by `load_scan_job_state` in `mediaforce/web/runtime/job_runtime.py`;
  full and prefix-scoped scan jobs must both be considered.
- Checking and publishing an encoded file can continue after its encode job
  becomes terminal. `publish_checked_files_once` in
  `mediaforce/web/runtime/automatic_publish.py` selects the pending files, and
  publishing and held-file recovery use `mediaforce/encoding/delivery_lock.py`.
  Unpromoted staged files are a separate database-resident publishing queue;
  they are not included in these counts.
  The controller must stop new publishing admissions and verify those locks are
  released, rather than treating terminal encode jobs as finished delivery.

An unavailable database fails the request; a failed or missing response is
unknown, never idle. The response observes database work, not release readiness. It
does not stop new admissions, reserve an idle window, verify remote processes,
approve a commit, or prove a backup. The future controller must fence its
release, quiesce all work producers, recheck work and process custody before
stopping the service, then follow the authoritative release path. Candidate,
backup, runtime and post-check evidence must stay bound to that exact release;
uncertain effects require reconciliation rather than replay.
