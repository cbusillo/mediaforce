# System-owned releases

Mediaforce release work is tracked in [#730](https://github.com/cbusillo/mediaforce/issues/730).
Release acceptance and controller behavior belong to
[Launchplane's release review](https://github.com/cbusillo/launchplane/blob/main/docs/release-review.md),
under the Director's [overall direction](https://github.com/cbusillo/direction/blob/main/DIRECTION.md).
Mediaforce does not keep a second approval record or offer an agent deploy command.

## Supported path and remaining integration

Launchplane's release worker separates the candidate and recorded acceptance
from the system's backup, promotion, post-checks and recovery. Its generic-web
driver targets managed web deployments; it cannot replace a macOS login item's
checkout. The Odoo release panel is likewise not a Mediaforce request path.
Neither an issue comment nor a successful dry run is release acceptance.

[Launchplane#3184](https://github.com/cbusillo/launchplane/issues/3184) owns the
missing macOS adapter and release-request integration. Until that exists and is
qualified, an agent cannot request a working Mediaforce release through the
supported controller. This source preparation does not activate a worker,
install a login item, enable releases, take a live backup, or deploy a runtime.

## Work evidence

`GET /api/release/work` reports fresh, uncached database work evidence:

```json
{
  "observed_at": "2026-10-09T20:00:00+00:00",
  "idle": false,
  "counts": {
    "pending_encode": 1,
    "active_encode": 1,
    "active_calibration": 0,
    "active_scan": 0,
    "active_evidence": 0
  }
}
```

`pending_encode` uses the same runnable-work counter as the dashboard's
`encode_queue.pending_work_count`: queued or retrying episodes count even when
their parent needs attention. `active_encode` includes queued, retrying and
running jobs, including folder parents and children. Counts overlap; do not sum
them to display a number of files. Samples/full runs, scans, and evidence work
also keep `idle` false. Paused queues still contain work; terminal jobs and
samples waiting only for human review do not hold an idle window.

An unavailable database fails the request; a failed or missing response is
unknown, never idle. The response observes work, not release readiness. It
does not stop new admissions, reserve an idle window, verify remote processes,
approve a commit, or prove a backup. The future controller must fence its
release, quiesce all work producers, recheck work and process custody before
stopping the service, then follow the authoritative release path. Candidate,
backup, runtime and post-check evidence must stay bound to that exact release;
uncertain effects require reconciliation rather than replay.
