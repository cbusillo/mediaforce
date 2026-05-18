# Calm Workstation Visual System

Use this reference when making Mediaforce easier to look at without drifting
into a generic SaaS dashboard. The target is a dense, durable workstation that
is calmer because hierarchy is clearer, not because it is softer or emptier.

## Design Direction

- Keep the control-room/workstation posture from `docs/style/workstation-ui.md`.
- Reduce visual fatigue by lowering panel competition, uppercase density, and
  repeated border noise.
- Make the primary working area obvious on every route.
- Use state color for meaning before brand expression.
- Keep dark mode readable: enough contrast for text and dividers, but fewer
  bright lines fighting for attention.

## Implementation Order

1. Adjust shared tokens and shell primitives first: `tokens.css`,
   `OperatorShell.svelte`, `WorkstationPanel.svelte`, `StateBadge.svelte`, and
   common button/table patterns.
2. Apply the system in Folder Studio while redesigning the core workflow in
   GitHub issue `#55`; it is the best stress test because it contains chat,
   evidence, proposal, actions, worker state, and post-approval processing.
3. Carry the same rules through Queue, Ops, Completed, and Settings in their
   route issues.

This answers the planning question for `#54`: define shared shell/tokens first,
then ground them immediately in Folder Studio rather than doing a purely
abstract polish pass.

## Surface Hierarchy

Use fewer kinds of boxes:

- **Page shell**: persistent masthead, route bar, status strip, and footer. It
  should feel stable across routes.
- **Primary work region**: the largest route area. It should have the clearest
  heading and the most obvious action zone.
- **Supporting rail**: narrow contextual facts, filters, recent history, or
  worker readiness. It should not compete with the primary work region.
- **Panel**: only for a bounded tool, list, table, modal, or repeated item
  group. Avoid placing cards inside cards.
- **Inline state row**: preferred for blockers, warnings, and progress when a
  full panel would overstate the message.

Reduce equal-weight chrome by making only the active decision area visually
prominent. Secondary panels should use muted backgrounds and lighter dividers.

## Token Direction

Current tokens already provide a good base, but the route surfaces lean too
hard on repeated dark panels and bright borders. Future token work should:

- Keep `--mf-bg-base` and `--mf-bg-shell` dark, but widen the step between base,
  shell, panel, and raised surfaces just enough to avoid muddy stacking.
- Use `--mf-line-muted` for most internal separators; reserve `--mf-line` and
  `--mf-line-strong` for region boundaries and active tools.
- Keep radius small: `0`, `2px`, and `4px` are enough for normal workstation
  surfaces.
- Use shadows only for overlays, popovers, and modals. Do not use shadows to
  make every panel feel lifted.
- Keep monospace for paths, IDs, counters, and machine telemetry; do not use it
  for explanatory copy.

## Typography

- Use sentence case for most labels and headings.
- Reserve uppercase for compact machine labels, route/system telemetry, and
  very small table metadata where it materially improves scanning.
- Route `h1` should be direct and short; avoid product-marketing scale.
- Panel headings should stay compact. If a panel title needs a full sentence,
  the panel likely needs a clearer purpose.
- Body/helper copy should be sparse and factual. Prefer one sentence that
  changes the user's next decision over explanatory paragraphs.

## State Color

Follow the semantic state contract in
`docs/design/operator-workstation-shell-brief.md`:

- Blue: active, running, selected, or in progress.
- Green: approved, healthy, ready, or completed successfully.
- Amber: waiting, warning, degraded, or needs attention but is not failed.
- Red: failed, blocked, dangerous, destructive, or unsafe.
- Gray: inactive, unavailable, not started, or intentionally disabled.

State color should usually appear as a thin line, badge, dot, or action
emphasis. Avoid coloring entire panels unless the route is in a true warning,
failure, or destructive confirmation state.

## Primary Actions

Every route needs one obvious action area:

- Folder Studio: review pack, approve/revise, start or retry sample, queue or
  monitor processing.
- Queue: open selected folder, start sample, or continue review.
- Ops: pause/resume/stop processing, retry available work, prepare a worker.
- Completed: delete selected originals or delete all archived originals, with
  scope and consequence visible.
- Settings: save changes, then advanced worker/storage recovery actions.

Primary actions should be visually grouped near the state they affect. Avoid
floating action groups that require the user to map a button back to distant
data.

## Tables And Lists

- Use tables or dense lists for comparison surfaces: queues, workers, completed
  folders, sample history, and retryable work.
- Keep row height stable. Use `--mf-row-compact`, `--mf-row-default`, and
  `--mf-row-comfy` deliberately.
- Selected rows should have a clear active line or background, not just a faint
  text color change.
- Empty tables should show what condition would make rows appear.
- Historical rows should be visually quieter than current blockers.

## Forms And Settings

Settings should become easier by grouping by user decision:

- Libraries: what Mediaforce should scan.
- Storage: where working files and archived originals live.
- Workers: where work can run.
- Work windows: when workers can accept work.
- Advanced machine settings: SSH, staging, source roots, and trust recovery.

Do not make advanced fields disappear entirely; collapse or visually subordinate
them so basic setup is legible first.

## Anti-Patterns To Remove

- Multiple equal-weight panels above the fold.
- Uppercase labels on most headings, helper text, buttons, or tabs.
- Bright dividers on every nested surface.
- Large dark cards that contain more dark cards.
- Decorative grid backgrounds, gradients, glow effects, or single-accent visual
  branding.
- Button labels that name implementation actions without scope or consequence.

## Browser Review Checklist

For each route, capture desktop and narrow screenshots and answer:

- Where does the eye land first?
- Is the next action visible without reading helper paragraphs?
- Are historical, waiting, failed, and ready states visually distinct?
- Does the route still feel like Mediaforce, or did it become a generic
  dashboard?
- Are there any text collisions, cramped buttons, or unreadable muted labels?
- Does color communicate state before decoration?
