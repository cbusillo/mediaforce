# Mediaforce UI Design Sources

This directory contains both current UI doctrine and historical reset briefs.
Read this file first before using any design brief as implementation guidance.

## Current Source Of Truth

- `docs/style/workstation-ui.md`: primary UI doctrine for operator-facing
  workstation surfaces.
- `docs/style/frontend.md`: Svelte/frontend style and validation expectations.
- `docs/design/basic-user-vocabulary.md`: user-facing term and workflow state
  reference for route copy.
- `docs/design/calm-workstation-visual-system.md`: visual-system reference for
  calmer hierarchy, tokens, surfaces, actions, and route-level browser review.
- `docs/design/operator-workstation-shell-brief.md`: current cross-route shell,
  route-role, state-color, and copy posture reference.
- GitHub issue `#62`: active execution plan for the basic-user UI/UX reset.

Current route implementation work should start from those references plus the
active GitHub sub-issue for the route or foundation track being changed.

## Historical Or Superseded Briefs

These files remain useful background, but they are not active implementation
plans:

- `docs/design/full-frontend-reset-brief.md`: historical external-design brief
  for a reset branch. It contains useful product context, but it predates the
  current issue-backed plan and should not override the current source of truth.
- `docs/design/workstation-home-screen-brief.md`: superseded home-specific
  first-pass brief. It should not be used as the current Queue/Home execution
  plan; use GitHub issue `#56` for that work.
- `docs/design/workstation-home-screen-inventory.md`: historical inventory from
  an earlier home-screen reset. Use only as background when checking old
  component decisions.

If a brief conflicts with the current source-of-truth list above, treat the
brief as stale and update the relevant GitHub plan issue before implementation.
