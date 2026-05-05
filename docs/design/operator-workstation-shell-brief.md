# Operator Workstation Shell Brief

This brief captures the durable shell language that now ties Mediaforce's main
operator routes together after the workstation reset and final consistency
audit. It is the carry-forward reference for future changes to home, ops,
completed, settings, and folder studio.

## Browser review snapshot

- Reviewed live at `http://127.0.0.1:8777/`, `/ops`, `/completed`, `/settings`,
  and folder workspace during the April 2026 consistency audit.
- Supporting artifacts live under `scratch/ui-checks/`, including:
  `home.png`, `ops.png`, `completed.png`, `settings.png`,
  `folder-workspace.png`, `ops-live-check.png`, and
  `settings-live-check.png`.
- Current verdict: the product now reads as one operator workstation family
  instead of a home page plus several adjacent dashboard-like tools.

## Goal

- Preserve a shared console-like shell across operator routes while allowing
  each surface to stay honest about its job: queue triage, fleet control,
  completed cleanup, configuration, or folder-level review.

## Shell contract

- Shared masthead and route navigation should make every primary route feel
  like part of the same workstation, not separate mini-products.
- The first visible region on a route should answer the route's core operator
  question quickly, without hero framing or decorative summary rows.
- System state belongs in compact strips, alert bars, tables, and tightly
  related side rails rather than spacious dashboard cards.
- Primary actions should sit beside the state they affect.
- Empty or loading states must be explicit and truthful; never render fallback
  zeros that look like real runtime data.

## Route roles

- Home: the main workbench. The ranked queue is the primary surface, with a
  compact system strip above and an adjacent operational rail.
- Ops: the fleet console. Lead with queue, workers, calibration, and cleanup
  state in one scan-friendly strip, then keep queue lanes and host readiness in
  list/table-first control surfaces rather than separate dashboard cards.
- Completed: the archive cleanup and history surface. Keep cleanup readiness,
  selected versus global destructive scope, archive-root health, and recent
  encode/cleanup events visible in one route so the operator can remove
  archived originals without treating it like another active queue.
- Settings: the runtime configuration surface. It may stay more utilitarian and
  form-heavy than the other routes, but it should still inherit the same shell,
  typography, and navigation language.
- Folder studio: the active review workspace. It should feel like an in-flight
  operator session, not a wizard with numbered steps.

## Canonical folder workflow

- Folder studio is the canonical operator route because it contains the core
  loop: run a representative sample, inspect the evidence, and approve the
  folder draft.
- The first viewport should make the folder, library, parent context, current
  sample or draft state, review readiness, main next action, proposed settings
  or important deltas, and any block or waiting reason clear without scrolling.
- Before approval, keep the request thread as a persistent left column and keep
  the main pane focused on evidence and decision support.
- Treat `Download review pack` as the primary approval-step review action. The
  inline viewer is secondary: useful for orientation and spot checks, not the
  final inspection surface.
- After the review pack has been opened, narrow the page toward the final
  `Approve` or `Revise` decision. Keep decision context compact: final size,
  duration, and resolution are enough unless a warning changes the decision.
- `Revise` should return naturally to the chat-led workflow and review posture;
  do not require a separate structured reason form.
- After approval, leave the operator on the folder in a calm approved-and-
  processing state. Collapse chat and evidence by default, and lead the main
  body with one combined processing strip.
- The approved-and-processing strip should lead with progress, ETA, and FPS,
  then carry minimal output facts: final size, duration, and resolution. Link
  to Ops for deeper operational detail instead of turning Folder Studio into a
  fleet console.
- Normal long encodes should stay visually quiet. Slow AV1 is not a warning by
  itself. Stalled, blocked, or failed states should show the reason first and
  foreground `Retry` as the recommended recovery action when retry is valid.

## Semantic state color contract

- Red: failed, blocked, dangerous, or otherwise requiring hard attention.
- Amber: warning, waiting, degraded, or needs attention but not failed.
- Green: approved, healthy, ready, or successfully completed.
- Blue: active, running, selected, or currently in progress.
- Gray: inactive, unavailable, not started, or intentionally disabled.
- Color should communicate machine or workflow state before brand emphasis. Do
  not weaken contrast or blur these meanings for visual softness.

## Operator copy posture

- Headings orient; labels name state directly.
- Helper copy should appear only when it changes operator understanding or
  prevents a bad action.
- Factual values, statuses, and action placement should carry more meaning than
  paragraphs.
- Prune decorative, redundant, or space-filling copy. The product should read
  as plain, compact, credible, and slightly blunt rather than chatty or
  promotional.

## Visual rules

- Favor dense layouts, restrained chrome, and strong dividers over decorative
  card framing.
- Use color for operational meaning first: readiness, warning, pause, failure,
  and selection.
- Prefer calm typography and stable spacing over large display treatments.
- Keep controls blunt and legible. This product should feel closer to a console
  or control-room tool than to a startup analytics dashboard.

## Non-goals

- Do not force every route into identical composition. Shared shell matters more
  than uniform page anatomy.
- Do not add decorative hero sections, soft marketing spacing, or summary rows
  that repeat information already visible in the working surface.
- Do not make settings mimic the home queue view when the route's job is direct
  configuration editing.

## Acceptance cues

- A user can tell which route they are on and what needs attention in a few
  seconds.
- Navigation, spacing, and state styling make the routes feel related.
- Loading and error states are explicit enough that operators never confuse
  transport delay with idle runtime state.
- If decorative styling were removed, the product would still read as a clear,
  credible operator workstation.
