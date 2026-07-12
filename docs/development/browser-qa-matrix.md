# Browser QA Matrix

Use this matrix when signing off operator-facing UI work. The goal is to prove
the app fully loads and remains usable across route and workflow states, not just
that a single live machine state happens to render.

## Commands

- Full route and fixture smoke:
  `npm --prefix frontend run smoke:web`
- Existing live app smoke:
  `npm --prefix frontend run smoke:web -- --base-url http://127.0.0.1:8777`
- Skip narrow checks only for non-UI diagnostics:
  `npm --prefix frontend run smoke:web -- --skip-narrow`

The default managed smoke uses `config/web-smoke.toml`, seeds deterministic
fixture state, starts `mediaforce-web` on a temporary port, checks API endpoints,
loads route shells in a desktop browser, then repeats route loading at a 390px
narrow viewport. When `--base-url` is provided, the script does not seed data or
mutate the target app.

For quick manual health checks, prefer browser or `GET` requests against the app
shell and startup endpoints. A `HEAD /` probe can return `405` even when the
server is healthy and the app loads normally. If an embedded browser tab looks
stuck, open a fresh tab against the same base URL before treating the local
server as down.

After the non-empty fixture pass, the managed smoke reseeds the same runtime with
the `empty` profile and reloads the main route family. This proves the UI does
not only work when the current live library has rows.

## Seeded Fixture States

The managed smoke seeds a compact but non-empty workflow dataset:

- Queue density: two pending `tv/Example Show/Season 1` items and one lower
  priority movie folder. The representative episode is 88 minutes so the goal
  screen resolves the 300 MB / 45 minute default to approximately 587 MB.
- Folder Studio: `/folders/tv/Example%20Show/Season%201`, including enough item
  metadata to render policy comparison, sample facts, queue state, and side
  context.
- Sampling state: `/folders/tv/Sampling%20Show/Season%201`, with a queued
  sample job.
- Retryable state: `/folders/tv/Retry%20Show/Season%201`, with a failed sample
  job that exposes retry copy.
- Review-ready state: `/folders/tv/Review%20Ready/Season%201`, with retained
  review media, explicit target/band/sample-byte facts, and picture/sound risk.
- Absolute-target state: `/folders/tv/Absolute%20Goal/Season%201`, proving an
  explicit 225 MB episode goal remains 225 MB for an 88-minute episode.
- Under- and over-target states: `/folders/tv/Undershoot%20Show/Season%201` and
  `/folders/tv/Overshoot%20Show/Season%201`, with contextual measured retries.
- Infeasible and quality-conflict states:
  `/folders/tv/Infeasible%20Goal/Season%201` and
  `/folders/tv/Quality%20Conflict/Season%201`, proving arithmetic impossibility
  is explained separately from a measured quality-floor conflict.
- Approved state: `/folders/tv/Approved%20Show/Season%201`, where approval is
  complete but production remains unqueued until `Make the season` is chosen.
- Active processing state: `/folders/tv/Encoding%20Show/Season%201`, with a
  running folder processing row.
- Retryable processing state: `/folders/tv/Failed%20Encode/Season%201`, with a
  needs-attention processing row that exposes recovery copy.
- Delivery states: `/folders/tv/Validation%20Ready/Season%201`,
  `/folders/tv/Promotion%20Ready/Season%201`, and
  `/folders/tv/Finished%20Show/Season%201` cover validation, promotion, and
  completed folder states.
- Unavailable worker state: `config/web-smoke.toml` includes a smoke-only remote
  host that is unavailable, so `/ops` can expose the no-ready-host blocker while
  queued encode work exists.
- Completed cleanup: one promoted `movies/Archive Ready` item with an archived
  original under the smoke archive root, so `/completed` has cleanup-ready work.
- Completed history: the completed fixtures include earlier failed and
  operator-stopped encode events so History must distinguish the two outcomes.
- Blocked cleanup: one promoted `movies/Blocked Cleanup` item with an archived
  original outside the configured archive root, so `/completed` exposes a
  blocked cleanup state.
- Empty library: the second managed pass clears seeded rows and jobs, then
  reloads `/`, `/folders`, `/ops`, and `/completed`.

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
- `/folders/tv/Review%20Ready/Season%201`: review-pack-ready sample state.
- `/folders/tv/Absolute%20Goal/Season%201`: absolute 225 MB target state.
- `/folders/tv/Overshoot%20Show/Season%201`: over-target comparison state.
- `/folders/tv/Undershoot%20Show/Season%201`: under-target comparison state.
- `/folders/tv/Infeasible%20Goal/Season%201`: arithmetic infeasibility state.
- `/folders/tv/Quality%20Conflict/Season%201`: quality-floor conflict state.
- `/folders/tv/Approved%20Show/Season%201`: approved, not-yet-queued state.
- `/folders/tv/Encoding%20Show/Season%201`: running processing state.
- `/folders/tv/Failed%20Encode/Season%201`: retryable processing state.
- `/folders/tv/Validation%20Ready/Season%201`: validation-ready state.
- `/folders/tv/Promotion%20Ready/Season%201`: promotion-ready state.
- `/folders/tv/Finished%20Show/Season%201`: completed state.
- `/ops`: blockers, worker readiness, queues, and collapsed history.
- `/completed`: no-action history and cleanup-ready work.
- `/settings`: basic, advanced, and danger-zone settings sections.

## Layout Expectations

The automated narrow smoke uses a 390px viewport and fails when:

- the app shell or `<main>` does not render quickly;
- the expected route marker text is missing;
- a folder route has not published its hydrated folder marker;
- the document has horizontal page overflow;
- a visible table is wider than the viewport.

Those checks are intentionally short and mechanical. For visual redesign or
interaction work, add a manual browser review with screenshots under
`scratch/ui-checks/` and inspect the actual starting viewport, post-interaction
state, and narrow layout.

On the Work screen, discovered source items without approved review evidence
must remain in `Sample and approval` with a `Needs sample` state. Only folders
with an approved draft may enter the `Encode backlog` lane.

Review-assistant submissions may run multiple bounded inference steps. The
browser must remain pending instead of aborting before those backend limits,
and the pending state must explain that the request can take a few minutes and
nothing queues until the operator reviews the plan.

For a direct size request, the reviewed draft, queued sample, and technical
details must agree on the requested total episode target. In particular, a
request for 225 MB at source resolution must not fall back to the configured
300 MB / 45 minute default after confirmation; the queued policy should pair
225 MB with the representative episode runtime and preserve source resolution.

For the runtime-normalized default, the goal screen must use the representative
episode runtime supplied by the API. A representative 88-minute episode should
show approximately 587 MB from the 300 MB / 45 minute reference, explain that
scaling, and submit the typed normalized request rather than rebuilding the
number in the browser. The same screen must keep an explicit 225 MB target at
225 MB, show the ±10% sample and ±5% final bands in the resolved contract, and
require an explicit mode choice for ambiguous legacy values before enabling the
test action.

When a measured sample lands outside that target band, the comparison viewport
must say that the size goal was not met before presenting review media. It must
distinguish review-clip byte savings from the full-episode estimate, make another
same-target measured test the primary action, and require an explicit warning
that accepting the tradeoff saves the profile and queues the full folder encode.

While the review assistant or a representative sample is active, the first
Folder Studio viewport must show a prominent live-operation state with the
current action, worker, and elapsed/status copy. Operators must not need to
scroll past old evidence to learn whether work is still running.

On Activity, unresolved processing failures must appear newest first. A missing
controller media mount must remain queued instead of dispatching futile SSH
retries, identify storage as the blocker, and show `Selecting computer` or
`Unassigned` rather than a placeholder host name.

On Library, each show and season must expose projected space savings at the
selection point. Verify that every sort option visibly reorders shows, that the
desktop show and season panes keep one stable height with independent scrolling,
and that narrow layouts replace the long show rail with a show picker. Opening a
multi-season show must clearly state that one representative test and one size
choice apply to all seasons before any full encode can be queued.

Library structure must render before savings and workflow enrichment completes.
With the detail request delayed, verify that show and season names, episode
counts, current sizes, search, selection, and size/name sorting remain usable;
savings fields must read as pending instead of zero. When details arrive, the
selected show, scroll position, and size-sorted row order must stay stable while
the pending metrics fill in. A detail-request failure must leave the structural
library usable and explain that only savings and status details are unavailable.

On Finished, the finished-season list must fit without horizontal scrolling at
1024px and must become readable stacked rows at 390px. The light/dark control
must persist across route changes and reloads, and both themes must preserve
status contrast, media-stage darkness, focus visibility, and readable controls
on Library, Activity, Finished, Settings, and folder review routes.

## State Gaps

The current fixture covers non-empty queue, empty queue, waiting Folder Studio,
queued sample, retryable sample, review-pack-ready sample, active encode,
retryable processing, cleanup-ready Completed, blocked Completed, unavailable host
Ops, idle Ops, and narrow layout. Future UI work should extend this fixture or
add dedicated fixture modes when it introduces new workflow states.
