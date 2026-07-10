# Basic-User Vocabulary And Workflow States

Use this reference when changing route copy, status labels, empty states,
disabled-control reasons, and destructive-action language. The goal is not to
hide Mediaforce's operational nature; it is to put user decisions before
implementation nouns.

## Copy Principles

- Name the user's next decision before naming the subsystem.
- Prefer short labels that describe work state: `Ready to review`, `Waiting for
  worker`, `Needs sample`, `Safe to delete`.
- Keep implementation terms available in advanced settings, logs, tooltips, or
  metadata rows when they help diagnosis.
- Do not use different names for the same state across routes.
- Disabled controls must either explain the missing condition or stay hidden
  when the action cannot help.
- Dangerous actions must name the scope and consequence, not the API action.

## Preferred User-Facing Terms

- Bench: use `Review assistant` in Folder Studio chat, sample requests, and
  revision requests.
- Calibration: use `Sample` in Folder Studio, Ops sample queue, and route
  summaries.
- Calibration queue: use `Sample queue` in Ops and global status strips.
- Proof: use `Sample check` or `review evidence` in Ops history and Folder
  Studio status.
- Host: use `Worker` in Queue, Ops, and Folder Studio capacity summaries.
- Remote host: use `Remote worker` in Settings and worker setup.
- SSH host: use `SSH address` in advanced worker settings only.
- Policy: use `Settings` or `proposed settings` in Folder Studio proposal rows
  and approval copy.
- Draft: use `Proposal` in user-visible review and approval copy.
- Pending proposal: use `Proposal ready for review` in Folder Studio decision
  state.
- Encode: use `Process` for basic route summaries; keep `encode` in technical
  job tables.
- Transcode root: use `Working folder` in the Settings storage section.
- Archive cleanup: use `Delete archived originals` for Completed and Settings
  destructive actions.
- Archive root: use `Cleanup folder` in Completed and Settings storage copy.
- Cleanup: use `Delete`, `clear`, or `mark handled`, matching the user's actual
  consequence.
- Missing sample: use `Needs sample` in Queue and Folder Studio blockers.
- Polling: use `Checking for updates` in loading and refresh states.
- Scheduler: use `Work schedule` in Ops and Settings.
- Schedule profile: use `Work window` in Settings basic labels.
- Staging root: use `Worker staging folder` in advanced worker settings.

Use the internal term only when the user is editing a technical setting,
debugging a worker, or comparing logs against backend output.

## Workflow State Names

### Queue And Folder Studio

- No sample exists: `Needs sample`. Mediaforce needs one representative file
  before settings can be approved.
- Sample is queued: `Sample waiting`. A worker has not started the sample yet.
- Sample is running: `Sampling`. Review evidence is being created.
- Sample failed but can retry: `Sample needs retry`. The next safe action is
  retrying the sample.
- Review media exists: `Ready to review`. Download or inspect evidence before
  approving.
- Proposal warning exists: `Review warning`. The user must inspect the warning
  before approving.
- Proposal accepted: `Approved`. The folder settings are accepted, and that
  visual approval remains authoritative evidence for future tuning.
- Encode queued or running: `Processing`. Folder-wide work is underway.
- Encode failed or stopped: `Processing needs attention`. The user must inspect
  or retry from the route that owns recovery.

### Ops

- Worker available: `Ready`. This worker can accept work now.
- Worker schedule closed: `Off schedule`. This is normal and not a failure if
  other workers are ready.
- Worker setup missing: `Needs setup`. The worker needs preparation before it
  can run work.
- Queue paused: `Paused`. Mediaforce will not start new processing work.
- Stop requested: `Stopping`. Existing work is being stopped or prevented from
  continuing.
- Retryable failures exist: `Retry available`. Failed approved work can be
  tried again.
- Historical sample failures: `Past sample issues`. Old sample/proof failures
  are history unless they block current work.

### Completed

- Archived originals exist: `Originals ready to delete`. Files can be removed
  from the cleanup folder after review.
- Selected folder is safe: `Selected originals ready`. The selected completed
  folder is eligible for deletion.
- Cleanup folder missing: `Cleanup folder missing`. Mediaforce cannot verify or
  delete archived originals.
- Folder state unknown: `Check before deleting`. The user should not remove
  originals until Mediaforce can verify scope.
- Cleanup already handled: `Nothing to delete`. No archived originals are
  waiting in the cleanup folder.

### Settings

- Unsaved settings draft: `Unsaved changes`. Edits are local until saved.
- Transcode root missing: `Working folder missing`. Mediaforce cannot store
  processing files.
- Remote host unavailable: `Worker unavailable`. A configured worker cannot
  accept work right now.
- Host trust reset supported: `Reset trust`. Advanced worker recovery is
  available.
- Archive cleanup target changed: `Save before deleting`. The cleanup target
  changed and deletion must wait for saved settings.

## Route Copy Direction

- Queue/Home should lead with `worklist`, `folder`, `sample`, `review`,
  `worker`, and `next action`. Avoid `calibration`, `proof`, and `host` in
  basic table columns.
- Folder Studio should say `Review assistant`, `sample`, `proposal`, `approved`,
  and `processing`. Keep `Bench` out of visible labels unless a product naming
  decision intentionally keeps it.
- Ops may expose more technical detail, but first-level headings should still
  use `workers`, `sample queue`, `processing queue`, and `retry available`.
- Completed should use destructive language directly: `Delete archived
  originals`, `selected originals`, `cleanup folder`, and `safe to delete`.
- Settings should split basic labels from advanced fields. `Working folder`,
  `cleanup folder`, `remote workers`, and `work windows` should be visible
  before `transcode root`, `archive root`, `SSH host`, or `staging root`.

## Dangerous Actions

Destructive or broad actions need three pieces of copy near the control:

- Scope: selected folders, all archived originals, all running work, or one
  worker.
- Consequence: delete files, stop processing, pause new work, or reset trust.
- Recovery expectation: reversible, retryable, or permanent.

Preferred labels:

- `Delete selected originals`
- `Delete all archived originals`
- `Stop processing`
- `Stop samples`
- `Reset worker trust`

Avoid vague labels such as `Clear`, `Cleanup`, `Stop encode`, or `Trust` when
they appear without adjacent scope and consequence.

## Disabled-Control Reasons

Disabled action titles and inline blockers should use this pattern:

- `Start a sample before approving this folder.`
- `Choose an available worker before sending.`
- `Save the working folder before deleting originals.`
- `Review evidence is not ready yet.`
- `No retryable processing jobs are available.`

Do not expose raw state names such as `missing_sample`, `polling`, `can_queue`,
or `archive_cleanup.has_cleanup` in visible UI.

## Reset Usage

Use this file for product language and workflow-state naming only. It does not
validate the old route structure, old component hierarchy, or previous
issue-backed cleanup plan. During the frontend reset, prefer these terms when
they clarify the operator's next safe decision, and revise them when the clean
workstation contract exposes a better product noun.
