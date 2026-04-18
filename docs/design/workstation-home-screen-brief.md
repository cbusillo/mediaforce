# Workstation Home Screen Brief

This brief defines the durable reset direction for the Mediaforce home screen
as an operator workstation surface. It replaces the earlier prototype framing
with a stricter console model based on live browser review of the current home
screen.

## Browser review snapshot

- Reviewed live at `http://127.0.0.1:8777/` on April 13, 2026.
- Screenshot artifact: `scratch/ui-checks/home-before-reset-desktop.png`.
- Current verdict: improved over the older card grid, but still too much like a
  composed dashboard. The eye still lands on multiple equal-weight panels
  instead of one dominant working surface.

## Goal

- Replace the remaining dashboard composition with a workstation console that
  helps an operator scan fleet state, confirm the active queue context, compare
  ranked folders quickly, and move directly into the next action.

## Output contract

- Visual direction: dark, restrained, technical, and editorial without looking
  sci-fi or decorative.
- Primary hierarchy decision: the ranked work queue is the main working
  surface, with one adjacent active-context panel tied directly to the current
  selection.
- Operator task flow being optimized: scan system state, confirm what matters
  now, compare queue candidates, then open or queue the next folder without
  bouncing between equal-weight cards.

## User job

- Understand current fleet state in a few seconds.
- See whether scan, queue, or review work needs attention.
- Compare the next several folders without losing context.
- Move directly into folder work, ops, completed backups, or settings.

## Current problems to correct

- The home screen still reads as a dashboard of bordered boxes rather than one
  stable workbench with supporting instrumentation.
- The active folder block is visually strong, but the queue table below it is
  the actual comparison surface; the layout still understates that priority.
- The right rail is useful, but it behaves like more dashboard panels rather
  than a tightly related monitor/control column.
- Shared surface styling is still influenced by soft panel chrome, decorative
  gradients, and rounded components from the earlier dashboard language.
- Navigation and shell framing still feel like a product dashboard header more
  than a workstation shell.

## Screen model

- Top status strip: persistent global state for queue, workers, catalog, and
  recovery, compact enough to scan in one pass.
- Main queue region: dense, sortable, table-first ranked folder queue occupying
  the dominant area of the page.
- Active context panel: selected folder summary and actions, visually coupled to
  the table selection rather than acting like a separate hero card.
- Side rail: compact operational monitors for queue activity, blockers, and
  catalog state.
- Filter controls: explicit, always-visible queue filtering adjacent to the
  queue rather than separated as a decorative subsection.

## Visual direction

- Dark, restrained, and high-contrast with sharper geometry and calmer chrome.
- Dense by default, with panel borders and dividers carrying structure instead
  of glow, wash, or oversized spacing.
- Functional color only for selection, warning, readiness, and machine state.
- Typography should support scanning first: strong labels, clear headings, and
  monospace only where it improves machine readability.
- No hero gradients, brand-led spotlighting, or decorative empty space.

## Content rules

- Lead with state, comparisons, and actions instead of dashboard framing.
- Prefer short labels and operational language.
- Show path-like and queue-like data in monospace only where that improves
  scanning.
- Keep important controls visible near the data they affect.
- Make blockers and next actions explicit when queue or catalog state is not
  nominal.

## Interaction rules

- The active folder/context must derive from the same source of truth as the
  ranked queue selection.
- Sorting, filtering, and queue actions must remain explicit and visible.
- The first operator action must win over delayed hydration or background state
  replay.
- Opening a folder, queueing a folder, and switching to ops/completed should
  feel like direct control-room actions, not navigation detours.

## Non-goals

- Do not redesign `/ops`, `/settings`, or folder studio in this pass.
- Do not introduce a new frontend stack or parallel UI shell.
- Do not bring back card-first folder browsing for the main queue view.
- Do not optimize for beauty if it reduces scan speed or state clarity.

## Acceptance cues

- The screen should read like a console, not a startup dashboard.
- A user should be able to answer “what is happening right now?” at a glance.
- The queue table should feel like the primary working surface, not a secondary
  section beneath a feature panel.
- The active folder should feel like an operator context pane, not one more
  bordered block in a mosaic.
- The shell should still feel credible if shadows and decorative styling are
  stripped away.
