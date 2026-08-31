# Workstation UI Guide

Use this guide when changing Mediaforce's primary operator surfaces: the home
screen, folder workspace, queue views, review surfaces, and other screens where
an operator scans state and makes repeated workflow decisions.

This guide is the canonical contract for Mediaforce operator surfaces. Active
plans may apply it to a specific workflow, but they do not override it with a
different visual or language model.

## Core stance

- Mediaforce is an operator workstation, not a polished SaaS dashboard.
- It is not a landing page, analytics dashboard, or conversational assistant.
- This pivot stays on the existing SvelteKit frontend; redesign the workflow
  surface, not the framework stack.
- Design against references like Resolve, Avid, broadcast control rooms, and
  dense internal tools.
- Do not design against references like Notion, Linear, or generic startup
  analytics dashboards.
- Design for repeated decisions, fast scanning, stable spatial memory, and
  confidence under operational risk.
- Preserve useful density. Simplicity means fewer competing ideas, not less
  evidence.

## First-view hierarchy

- The primary media, queue, list, comparison, or decision surface owns the
  first viewport.
- Do not put hero blocks, slogans, welcome copy, oversized context headings, or
  decorative summaries before the work.
- Show one authoritative current state and one primary action. Keep secondary
  actions visibly subordinate.
- Keep actions beside the object and evidence they affect.
- Move provenance, diagnostics, uncommon rules, and implementation detail into
  progressive disclosure rather than competing with the ordinary decision.

## Library modes

- TV, Movies, and Other are three modes of one Library workstation, not three
  independently composed landing pages.
- Keep the mode navigation, metric summary, current-work summary, notices,
  toolbar, register, and selected-detail expansion in the same order and at the
  same offsets across all three modes.
- The active mode tab supplies visible page identity. Keep the semantic `h1`
  available to assistive technology, but do not add a hero heading or subtitle
  above the library work.
- Use one compact four-metric strip and one compact current-work summary. Do not
  duplicate active rows as a card gallery or repeat one recommended row as a
  hero block.
- Let metrics and current work sit calmly on the workstation canvas. The
  register is the primary material surface; do not draw the component tree as a
  stack of bordered boxes.
- Make values lead and labels recede. Use the shared spacing and type scales for
  metrics, toolbar, rows, and selected detail instead of introducing local pixel
  rhythms.
- Keep search and equivalent filters on stable wide-screen tracks across modes.
  Majority states should use quiet text-and-dot treatment; reserve tinted pills,
  fills, and strong color for selection, recommendation, risk, blockers, and
  exceptional readiness.
- Share the shell and state-summary implementation. Keep media-specific row
  cells, policy controls, file membership, and selected-detail content
  specialized.
- Use one document scrollbar. Do not give the register or selected detail a
  fixed height or independently scrolling box.
- Keep the desktop register's column header visible during document scrolling;
  narrow rows remain self-contained instead of introducing a sticky header.
- Open at most one selected-detail expansion directly beneath its row. Preserve
  the row's scroll anchor, provide an explicit Collapse control, and route deep
  evidence to Studio rather than growing a second page inside the register.
- At constrained widths, preserve the same toolbar and row idiom. Selecting a
  row keeps the register stable; an explicit Inspect control opens its inline
  detail, and Close returns to the row without a page jump.

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
- Every count must map to reachable rows through visible browsing, pagination,
  filtering, or complete scrolling.
- Search may narrow a list, but it must never be the only route to counted
  objects.

## Layout rules

- Prefer a persistent system bar or status bar for global machine state,
  throughput, warnings, and environment context.
- Use table-first or list-first layouts for operational queues, job state, and
  sortable comparisons.
- Keep the primary workspace stable across refreshes and interactions so users
  can build spatial memory.
- Center wide workstation content on the shared shell track so global chrome,
  summaries, toolbars, registers, and selected detail keep one vertical frame.
- Put detail panels, previews, and secondary actions next to the active queue
  or selection instead of hiding them behind decorative containers.
- Favor visible filtering, sorting, and status controls over collapsed or
  inferred state.
- Show list range, total, page movement, and active filters whenever the data
  set is larger than the visible rows.

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

## Operator language

- Use ordinary media nouns and exact action verbs: movie, episode, season,
  original, sample, full encode, checked output, replacement, waiting, running,
  and stopped.
- Do not expose implementation terms such as artifact, manifest, promotion,
  prefix, lane, calibration, worker lease, scheduler, or internal job IDs as
  primary interface language.
- Button text names the actual effect and scope. A queued action says it queues;
  an immediate action says it starts now; replacement says which file changes.
- Approval records an operator judgment. Compression admission is a separate
  commitment and action.
- Put scope, consequence, and recovery or backup expectation beside each
  consequential action once. Do not repeat the same safety sentence throughout
  the screen.

## Copy budget

- Every visible sentence must communicate state, consequence, risk, blocker,
  recovery, or a genuinely non-obvious interaction.
- Remove text that restates a heading, label, button, or visible relationship.
- Prefer compact labels and values over explanatory paragraphs.
- Keep empty states compact and operational rather than turning them into
  onboarding essays.

## Interaction rules

- Keep important state visible and explicit: filters, active folder, queue
  scope, warnings, and pending work should be obvious without extra clicks.
- Distinguish hover, focus, selection, and disabled states clearly.
- Do not rely on subtle chroma changes alone for important state transitions.
- Keep actions close to the data they affect.
- Preserve user intent during hydration and migration work; the first explicit
  operator action must win over delayed storage or background state replay.
- Keep familiar controls discoverable: fullscreen, playback, comparison layout,
  filtering, sorting, pagination, stop, retry, check, and replace.
- Fullscreen review is for viewing only. It may contain playback, moments,
  sound source, picture arrangement, scale, and exit controls, but not approval,
  queue, stop, retry, checking, replacement, or details actions.
- Keep every non-queueable plan directly above its request composer. It states
  `Nothing was queued.` and offers one recovery action: retry the exact saved
  request for assistant failure, edit an unclear request, or change a blocked
  request. Never show a start action for these states.

## Review decisions

- Let the original and sample evidence dominate the review viewport.
- Place compact facts and the current decision after the evidence rather than
  before it.
- Keep ordinary review outcomes explicit: `Keep this version`, `Use less
  space`, and `Improve picture or sound`.
- Show `Allow a larger file` only inside the improve-quality path when the
  operator intentionally changes that constraint.
- On narrow screens, keep both pictures reachable, preserve the One/Both
  control, and provide a non-mutating jump to the review decision.
- After a successful replacement, collapse the comparison into known completed
  facts: current file size, actual space saved, original-backup state, and the
  next safe destination.

## Design evidence

- Major operator-surface changes begin with at least two independent visual
  directions based on one shared brief.
- Proposals use real copy and include default, dense, loading, error, blocked,
  running, completed, and narrow states rather than only a polished happy path.
- Evaluate proposals for workflow clarity, visual quality, language, state
  coverage, accessibility, implementation feasibility, and workstation
  credibility.
- Compare proposals directly. Do not average incompatible ideas into a
  compromised layout.
- Begin implementation only after the operator accepts a browser-viewable
  direction.

## Anti-slop rules

- No hero-first dashboard compositions for the main operator surface.
- No card carousels or gallery-style presentations for operational queues.
- No decorative gradients or glow treatments on primary workflow surfaces.
- No single-accent neutral SaaS palette where most meaning depends on one brand
  color.
- No oversized empty states when useful state, counts, or recent activity can
  be shown instead.
- No hiding critical operational metadata that supports triage.
- No hidden row caps.
- No inspector state that points at an object the operator cannot reach from the
  visible list mechanics.

## Review checklist

- Does this screen read like a workstation or like a startup dashboard?
- Can an operator scan the current state in a few seconds?
- Are the highest-value comparisons shown in tables or dense lists when that is
  the natural form of the data?
- Is color carrying operational meaning rather than decoration?
- Is the active workspace persistent and obvious?
- Do all displayed counts map to reachable rows?
- Can the operator move through all counted media without using search?
- Does the next action explain why it is safe, blocked, or waiting?
- Would this still feel credible if it were shown next to Resolve or a control
  room console?
- Does every visible sentence earn its place under the copy budget?
- Does fullscreen contain viewing controls only?
- Are approval, compression, checking, and replacement visibly separate
  commitments?

## Process expectations

- Before changing a primary operator surface, read this guide together with
  `docs/style/frontend.md`.
- For major UI resets, write or update a short design brief before coding.
- Validate in a real browser and critique the rendered result against this
  guide, not just against the component code.
- Reach the surface through ordinary navigation, and test functional behavior
  separately from visual quality.
- Test desktop and 390px, keyboard focus, dense data, blockers, failure
  recovery, important transitions, and destructive-action safety.
- Reject implementations that are technically correct but still ugly,
  exhausting, narratively verbose, or dependent on developer knowledge.
- If a proposal mostly changes vibes rather than improving scan speed,
  statefulness, or workflow confidence, stop and rethink the model.
