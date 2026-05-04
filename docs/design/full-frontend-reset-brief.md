# Full Frontend Reset Brief

This branch intentionally removes the old Mediaforce visual frontend so the
next design pass starts from product workflow and preserved contracts instead of
existing panels, cards, and route layouts.

## Context Access Assumption

Assume an external design collaborator cannot read local files or inspect the
running app unless the repo branch, files, or screenshots are explicitly
provided. This brief is therefore self-contained enough to guide a first design
draft from the GitHub branch alone.

## Product Context

- Product: Mediaforce.
- Audience: one technical operator managing local media operations.
- Job: scan libraries, sample folders, inspect encode evidence, approve or
  revise settings, queue full encodes, monitor hosts, recover blocked work, and
  clean up completed backups.
- Desired feel: a blunt, dense, credible operator workstation. Not a SaaS
  dashboard, landing page, media gallery, or analytics product.

## What Was Removed

The branch deletes the old visual component tree and route implementations that
kept pulling design work back toward the failed dashboard model:

- shared visual primitives such as panels, pills, masthead, buttons, section
  heads, cards, and toast wrappers
- dashboard host and queue modules
- home queue rail/table/system strip components
- settings editor UI
- the old Folder Studio component tree, including the large monolithic
  `FolderStudioView.svelte`
- old workstation shell styling

The route files now render neutral reset placeholders that list the contracts to
rebuild against.

## What Remains

- Existing SvelteKit app and route structure.
- API client and payload types.
- Route loaders, including the folder detail loader.
- Domain helpers and tests.
- A minimal CSS reset.
- Backend behavior and data contracts.

Keep these unless a specific product decision requires changing them.

## Product Glossary

- Folder: a show season, movie folder, or media grouping that can be sampled,
  reviewed, approved, and encoded.
- Folder Studio: the detailed workspace for one folder. This is the core screen.
- Representative sample: a short encode candidate used to test settings before
  approving a whole folder.
- Review pack: downloadable media/evidence package the operator opens to judge
  whether the sample is acceptable.
- Proposal: suggested encode settings and rationale for the folder.
- Approval: the operator accepts the proposal and allows full-folder work.
- Revision: the operator asks for a new sample/proposal instead of approving.
- Encode queue: approved full-folder encoding work waiting or running on hosts.
- Calibration queue: sample/review work waiting or running before approval.
- Host: a machine that can run sampling or full encode jobs.
- Schedule window: host availability rule controlling when work may run.
- Completed backup: original media retained after successful encode, waiting for
  explicit cleanup.
- Ops: the fleet/queue control surface for hosts, workers, schedules, and stuck
  work.

## Design Goal

Create the right first draft for a new Mediaforce operator workstation UI.
Preserve workflows and data constraints, but do not recreate the old component
structure or dashboard/card composition.

The old UI should be treated as evidence of what failed. Screenshots in
`scratch/ui-checks/` may help if attached separately, but the new design should
not use them as a layout foundation.

## Primary Surface: Folder Studio

Folder Studio is the canonical operator workflow.

Primary loop:

1. Run or resume a representative sample.
2. Inspect review evidence.
3. Download/open the review pack.
4. Approve or revise the proposed settings.
5. Monitor approved processing or recover blocked work.

The first viewport must make obvious:

- active folder/season
- library and parent context
- current workflow state
- main next action
- sample/review readiness
- proposed settings or important deltas
- blocker or waiting reason, if any

Folder Studio states to design:

- not sampled
- sample queued or running
- review pack ready but not opened
- review pack opened / decision ready
- proposal stale or failed self-check
- approved and queued/running
- stalled, blocked, failed, or retryable
- completed

Content that may exist:

- folder title and path-like prefix
- library and parent context
- pending item count, size, reclaim, duration, resolution, bitrate, codec,
  audio/subtitle info, or metric warnings
- current sample status, sample host, full encode host, and host readiness
- proposal summary and changed settings
- metrics such as VMAF, XPSNR, and SSIM
- operator note/request thread and suggested follow-up
- download/open review pack action
- approve, revise, queue, retry, recover, or open Ops actions
- raw diagnostics that should be secondary or collapsible

## Route Family Roles

### Home

The main workbench. The ranked folder queue is the primary surface, with compact
global state and a selected-folder context pane.

Potential content:

- global system strip: running encodes, queued folders, worker readiness, paused
  queue, stop requested, schedule off-window, active/stalled scans, ETA, reclaim
- ranked queue rows: title, library, pending count, projected reclaim, age,
  review/readiness state, priority, selected row
- active context for selected row
- side rail for queue/fleet blockers, recent failures, metric support, and
  completed-backup cleanup signal

### Ops

The fleet console. It should lead with queue, workers, hosts, schedules,
blockers, and recovery state in dense operational surfaces, not cards.

### Completed

The archive cleanup surface. It should help the operator decide which completed
backups can be removed and which are blocked.

### Settings

The runtime configuration surface. It may be utilitarian and form-heavy, but it
should share the same shell, typography, and state language.

## State Color Contract

- Red: failed, blocked, dangerous, destructive, or requiring hard attention.
- Amber: waiting, warning, degraded, stale, needs attention but not failed.
- Green: approved, healthy, ready, successful, completed.
- Blue: active, running, selected, currently in progress.
- Gray: inactive, disabled, unavailable, not started, intentionally paused.

Color should communicate machine or workflow state before brand emphasis.

## Visual Direction

- Dark, restrained, technical, and editorial, but not sci-fi cosplay.
- Dense and stable. Favor tables, lists, strips, panes, dividers, and persistent
  regions over decorative cards.
- Sharper geometry, restrained radius, minimal shadows.
- Typography should favor scanning, labels, values, state, and paths.
- Use monospace only where it improves machine readability.

Avoid:

- hero sections
- card-grid queue browsing
- decorative gradients, glows, bokeh, or soft SaaS chrome
- one-note blue/purple/slate palettes
- long explanatory copy where state and action placement can do the job
- hidden critical controls

## Implementation Constraints

- Keep the existing SvelteKit frontend.
- Keep route structure recognizable unless there is a strong product reason.
- Preserve backend/API contracts where practical.
- Runtime media and machine-local paths live outside the repo.
- Some async values may be unknown; distinguish unknown from zero, idle, or
  healthy.
- Use ordinary Svelte/CSS implementation patterns: grid, flexbox, tables/lists,
  sticky panes, responsive breakpoints, and standard controls.
- Avoid designs that require proprietary UI kits, canvas/WebGL, or bespoke
  animation engines.

## Requested First-Draft Output

Please provide:

1. A short diagnosis of the failed UI model.
2. A new visual direction summary.
3. Folder Studio screen architecture, including first viewport and
   post-approval state.
4. Route family sketches for Home, Ops, Completed, and Settings.
5. Design tokens: color roles, typography, spacing, radius, border, shadow,
   table/list density, and state treatments.
6. Interaction notes for primary controls and workflow transitions.
7. Required-state notes for loading, empty, error, dense, mobile, warning,
   blocked, approved, and completed states.
8. Assumptions, tradeoffs, implementation risks, and any needed backend fields.
