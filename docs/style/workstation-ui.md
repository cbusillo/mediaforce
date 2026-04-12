# Workstation UI Guide

Use this guide when changing Mediaforce's primary operator surfaces: the home
screen, folder workspace, queue views, review surfaces, and other screens where
an operator scans state and makes repeated workflow decisions.

## Core stance

- Mediaforce is an operator workstation, not a polished SaaS dashboard.
- This pivot stays on the existing SvelteKit frontend; redesign the workflow
  surface, not the framework stack.
- Design against references like Resolve, Avid, broadcast control rooms, and
  dense internal tools.
- Do not design against references like Notion, Linear, or generic startup
  analytics dashboards.

## What went wrong in the reset pass

- The redesign started from the wrong reference class, so the UI still read as
  a refined dashboard instead of an operational tool.
- Too much emphasis went to tasteful presentation over scan speed, persistent
  state, and operator confidence.
- Operational data was softened into cards and spacious sections that looked
  presentable but slowed down comparison and triage.
- Color and spacing were used decoratively instead of primarily to communicate
  machine state, priority, and risk.
- The layout lacked enough spatial persistence, so the screen felt composed for
  browsing rather than for repeated high-frequency use.

## The right model

- Treat the application like a control console for ongoing media operations.
- Optimize for fast scanning, confidence, explicit state, and repeatable muscle
  memory.
- Default to a dense workspace with stable regions, not a hero-first landing
  page.
- Make the active folder or queue feel like the current workstation context,
  not one card among many.

## Layout rules

- Prefer a persistent system bar or status bar for global machine state,
  throughput, warnings, and environment context.
- Use table-first or list-first layouts for operational queues, job state, and
  sortable comparisons.
- Keep the primary workspace stable across refreshes and interactions so users
  can build spatial memory.
- Put detail panels, previews, and secondary actions next to the active queue
  or selection instead of hiding them behind decorative containers.
- Favor visible filtering, sorting, and status controls over collapsed or
  inferred state.

## Visual rules

- Dense by default: avoid hero whitespace and oversized marketing spacing.
- Use sharper edges and restrained radius; do not round every surface.
- Use color for status, warning, selection, and machine meaning first, not
  brand emphasis.
- Avoid soft gradients, glow-heavy treatments, and decorative accent washes.
- Keep typography functional and calm; use monospace only for technical values,
  paths, counters, and machine-readable metadata.
- Prefer contrast and hierarchy that help scanning over softness that makes the
  product look friendly but vague.

## Interaction rules

- Keep important state visible and explicit: filters, active folder, queue
  scope, warnings, and pending work should be obvious without extra clicks.
- Distinguish hover, focus, selection, and disabled states clearly.
- Do not rely on subtle chroma changes alone for important state transitions.
- Keep actions close to the data they affect.
- Preserve user intent during hydration and migration work; the first explicit
  operator action must win over delayed storage or background state replay.

## Anti-slop rules

- No hero-first dashboard compositions for the main operator surface.
- No card carousels or gallery-style presentations for operational queues.
- No decorative gradients or glow treatments on primary workflow surfaces.
- No single-accent neutral SaaS palette where most meaning depends on one brand
  color.
- No oversized empty states when useful state, counts, or recent activity can
  be shown instead.
- No hiding critical operational metadata that supports triage.

## Review checklist

- Does this screen read like a workstation or like a startup dashboard?
- Can an operator scan the current state in a few seconds?
- Are the highest-value comparisons shown in tables or dense lists when that is
  the natural form of the data?
- Is color carrying operational meaning rather than decoration?
- Is the active workspace persistent and obvious?
- Would this still feel credible if it were shown next to Resolve or a control
  room console?

## Process expectations

- Before changing a primary operator surface, read this guide together with
  `docs/style/frontend.md`.
- For major UI resets, write or update a short design brief before coding.
- Validate in a real browser and critique the rendered result against this
  guide, not just against the component code.
- If a proposal mostly changes vibes rather than improving scan speed,
  statefulness, or workflow confidence, stop and rethink the model.
