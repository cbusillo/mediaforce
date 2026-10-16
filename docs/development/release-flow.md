# Releases

Agents deploy Mediaforce `main` to the controller themselves with a staged,
gated deploy script. The Director decided this on 2026-10-13
([direction#82](https://github.com/cbusillo/direction/issues/82)). Launchplane
does not manage Mediaforce. A system-owned release flow
([#730](https://github.com/cbusillo/mediaforce/issues/730),
[Launchplane#3184](https://github.com/cbusillo/launchplane/issues/3184)) is a
possible future replacement, not a gate on deploys today.

## Staged deploy

Use the maintained `scripts/deploy-controller.sh`, from a clean checkout of
reviewed `main`. Do not copy an old machine-local deploy script: those scripts
update a development worktree in place.

The controller installs into
`~/Library/Application Support/mediaforce/runtime/releases/<full-sha>-<attempt>/`.
Each release is an independent Git clone with its own environment and freshly
built frontend. A fresh attempt folder lets the same commit be retried without
overwriting a retained candidate; it has no Git alternates or worktree registration pointing back
to a development checkout. Launchd's program and working directory name that
release directly. Service commands run from a development checkout address the
installed controller rather than changing it to the caller's checkout.

The deploy script serializes deploys, checks the existing paused/idle dashboard
gate, refuses a dirty previous runtime, and prepares the candidate before
stopping the service. It rechecks the gate, stops the login item, verifies port
8777 closes, takes an online SQLite backup and runs `pragma quick_check`, then
switches the plist atomically and starts the candidate. Startup runs migrations.
After the API health check succeeds it resumes the encode queue. Preparation or
preflight failure leaves the existing service alone. A failure after stopping
leaves the service disabled for reconciliation; it does not start older code
against a possibly migrated database. Existing dashboard gates retain their
current scope; the work-count endpoint below is not an idle gate.

### Next deploy: switch from the legacy worktree

These steps belong to the next authorized deploy, after this change is merged.
They are not part of source implementation or PR review.

1. Use a clean `main` checkout containing this script. Fetch `origin/main` and
   resolve the exact reviewed commit with `git rev-parse origin/main`. Record
   that full SHA as the target; do not select a task branch.
2. Pause the queue with `POST /api/encode-queue/pause`. Wait for dashboard
   encode, sample and full-run counts to reach zero. Preserve queued jobs,
   runtime settings and all retained outputs. Check the existing runtime is
   clean; do not move, delete or unlock it.
3. Choose a fresh stage under
   `~/Library/Application Support/mediaforce/deployments/main-<sha>-<date>/`.
   Run from the clean checkout, substituting the full reviewed SHA and a fresh
   stage name:

   ```bash
   scripts/deploy-controller.sh --target <full-sha> --stage "$HOME/Library/Application Support/mediaforce/deployments/main-<sha>-<date>"
   ```

4. The stage retains `launch-item.before.plist`, `previous-commit.txt`,
   `candidate.txt` and `before.sqlite`. The new runtime retains the existing
   credential file with mode `0600` through the service install path; never
   print its contents. Database/settings remain at their existing support paths.
5. Verify `uv run mediaforce service status`, the installed plist's program and
   working directory, the dashboard and durable logs. Both launch paths must
   point into the candidate named in `candidate.txt`, and queued work must
   remain present. Keep the legacy runtime and its worktree lock as rollback;
   this deploy never removes or changes its source/environment.

### Rollback and cleanup ownership

Before changing the plist, deploy records both old and new runtime paths in
`runtime/protected-roots.json`. Registration is cumulative: active and rollback
roots remain protected across later deploys. The hourly cleanup refuses an
entire sweep when any configured cleanup tree overlaps the runtime install
root, a registered runtime/rollback path, or the executing source tree. An
unreadable registry also refuses cleanup. It never prunes `runtime/` releases.
Routine development cleanup owns development worktrees only; it must leave
installed runtimes, the registry, locked legacy runtimes and retained rollback
material alone. These guards cover Mediaforce's supported retention cleanup,
not arbitrary filesystem deletion or another repository's cleanup program.
The September disappearance's actor remains unknown.

For rollback, first stop the service with `mediaforce service disable` (or the
raw `launchctl` recovery in the login-item doc). Verify port 8777 is closed.
If startup reached migrations, restore this stage's verified `before.sqlite`
with the service stopped and remove the database's stale `-wal`/`-shm` files.
Restore `launch-item.before.plist` atomically to the launch-item path, then use
`launchctl enable` and `launchctl bootstrap` for that exact saved plist. Do not
regenerate it with new code: the old rollback may be a legacy worktree that new
service installation correctly refuses. Verify its exact saved commit, API and
queue before resuming work. No runtime directory is deleted during rollback.

The hourly cleanup keeps the newest `state.cleanup.kept_deploy_backups` stage
folders (two by default). Retain a stage outside that cleanup tree when its
backup is still needed for a longer-lived rollback. Removing old releases or
releasing legacy protection requires a separate, verified retirement after no
service, job or rollback depends on them.

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
