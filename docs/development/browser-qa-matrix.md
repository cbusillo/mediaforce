# Browser QA Matrix

Use this matrix when signing off operator-facing UI work. The goal is to prove
the app fully loads and remains usable across route and workflow states, not just
that a single live machine state happens to render.

## Commands

- Full route and fixture smoke: `npm --prefix frontend run smoke:web`
- Existing live app smoke: `npm --prefix frontend run smoke:web -- --base-url http://127.0.0.1:5555`
- Skip narrow checks only for non-UI diagnostics: `npm --prefix frontend run smoke:web -- --skip-narrow`

The default managed smoke uses `config/web-smoke.toml`, seeds deterministic
fixture state, starts `mediaforce-web` on a temporary port, checks API endpoints,
loads route shells in a desktop browser, then repeats route loading at a 390px
narrow viewport. When `--base-url` is provided, the script does not seed data or
mutate the target app.

## Seeded Fixture States

The managed smoke seeds a compact but non-empty workflow dataset:

- Queue density: two pending `tv/Example Show/Season 1` items and one lower
  priority movie folder.
- Folder Studio: `/folders/tv/Example%20Show/Season%201`, including enough item
  metadata to render policy comparison, sample facts, queue state, and side
  context.
- Sampling state: `/folders/tv/Sampling%20Show/Season%201`, with a queued
  sample job.
- Retryable state: `/folders/tv/Retry%20Show/Season%201`, with a failed sample
  job that exposes retry copy.
- Completed cleanup: one promoted `movies/Archive Ready` item with an archived
  original under the smoke archive root, so `/completed` has cleanup-ready work.
- Blocked cleanup: one promoted `movies/Blocked Cleanup` item with an archived
  original outside the configured archive root, so `/completed` exposes a
  blocked cleanup state.

Seeded rows use `last_scan_id = web-smoke-fixtures` and are replaced on each
managed smoke run. Runtime state remains under `state/web-smoke/` and
`scratch/web-smoke/`.

## Required Route Coverage

Every browser QA pass should cover these routes:

- `/`: Queue worklist and selected folder context.
- `/folders`: Folder queue entry point.
- `/folders/tv/Example%20Show/Season%201`: representative Folder Studio state.
- `/folders/tv/Sampling%20Show/Season%201`: active sample queue state.
- `/folders/tv/Retry%20Show/Season%201`: retryable sample state.
- `/ops`: blockers, worker readiness, queues, and collapsed history.
- `/completed`: no-action history and cleanup-ready work.
- `/settings`: basic, advanced, and danger-zone settings sections.

## Layout Expectations

The automated narrow smoke uses a 390px viewport and fails when:

- the app shell or `<main>` does not render quickly;
- the expected route marker text is missing;
- the document has horizontal page overflow;
- a visible table is wider than the viewport.

Those checks are intentionally short and mechanical. For visual redesign or
interaction work, add a manual browser review with screenshots under
`scratch/ui-checks/` and inspect the actual starting viewport, post-interaction
state, and narrow layout.

## State Gaps

The current fixture covers non-empty queue, waiting Folder Studio, queued sample,
retryable sample, cleanup-ready Completed, blocked Completed, idle Ops, and
narrow layout. Future UI work should extend this fixture or add dedicated
fixture modes for:

- approved review pack;
- active encode running;
- failed or retryable encode;
- empty library;
- unreachable or unavailable worker hosts.
