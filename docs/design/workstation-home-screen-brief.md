# Workstation Home Screen Brief

This brief defines the first prototype pass for the Mediaforce home screen on
the workstation pivot.

## Goal

- Replace the dashboard-like home screen with an operator console that helps a
  user scan the fleet, pick the next folder, and jump into active work without
  browsing through decorative cards.

## User job

- Understand current fleet state in a few seconds.
- See whether scan, queue, or review work needs attention.
- Identify the next folder to open.
- Move directly into folder work, ops, completed backups, or settings.

## Screen model

- Top system strip: persistent machine state, queue state, worker readiness,
  and backlog warnings.
- Main workspace: one active folder panel showing the current best candidate
  and why it matters now.
- Side rail: queue watch and catalog watch panels with live operational detail.
- Work queue table: dense list of folders with visible status, reclaim, review,
  and scope data.
- Filter bar: explicit library filtering in-place above the queue table.

## Visual direction

- Darker, tighter, and sharper than the current dashboard shell.
- Minimal radius, visible borders, and strong panel separation.
- Functional accent color only for state, selection, or warning.
- No hero gradients, oversized feature copy, or decorative empty space.

## Content rules

- Lead with state, counts, and actions instead of marketing-style framing.
- Prefer short labels and operational language.
- Show path-like and queue-like data in monospace only where that improves
  scanning.
- Keep important controls visible: folder filters, open-folder action, open-ops
  action, and backlog routes.

## Non-goals

- Do not redesign `/ops`, `/settings`, or folder studio in this pass.
- Do not introduce a new frontend stack or parallel UI shell.
- Do not bring back card-first folder browsing for the main queue view.
- Do not optimize for beauty if it reduces scan speed or state clarity.

## Acceptance cues

- The screen should read like a console, not a startup dashboard.
- A user should be able to answer “what is happening right now?” at a glance.
- The top folder should feel like an active workstation context, not one tile in
  a gallery.
- The folder list should support comparison and triage better than the old card
  grid.
