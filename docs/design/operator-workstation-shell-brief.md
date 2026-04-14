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
  state in one scan-friendly strip, followed by direct queue and host control.
- Completed: the archive cleanup surface. Keep the header light and route users
  directly into cleanup decisions instead of repeating summary pills.
- Settings: the runtime configuration surface. It may stay more utilitarian and
  form-heavy than the other routes, but it should still inherit the same shell,
  typography, and navigation language.
- Folder studio: the active review workspace. It should feel like an in-flight
  operator session, not a wizard with numbered steps.

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
