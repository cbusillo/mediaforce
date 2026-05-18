# Workstation Home Screen Inventory

> Status: Historical/superseded inventory. Do not use this as the active
> Queue/Home implementation plan. Start with `docs/design/README.md`,
> `docs/design/operator-workstation-shell-brief.md`, and GitHub issue `#56`
> instead.

This inventory records what should carry forward, what should be rewritten, and
what should stop shaping the Mediaforce home screen during the workstation UI
reset.

## Browser-based findings

- Reviewed live at `http://127.0.0.1:8777/` on April 13, 2026.
- Screenshot artifact: `scratch/ui-checks/home-before-reset-desktop.png`.
- The current screen is denser and more operational than the old reset pass,
  but the composition still behaves like a dashboard of peer panels.
- The queue table is the most useful comparison surface on the page and should
  become the dominant region.
- The active folder block, right rail, and top strip all have value, but they
  need tighter hierarchy and less decorative separation.

## Carry forward

These pieces support the new workstation model and should be preserved or used
as implementation anchors.

- `frontend/src/lib/api/`
  - Keep the current data contracts, API helpers, and typed payload shapes.
- `frontend/src/lib/folder-display.ts`
  - Keep shared route/path helpers and display helpers.
  - `folderRoutePrefix(...)` and `folderRoutePath(...)` are durable contracts.
- `frontend/src/routes/+page.svelte`
  - Keep the domain state logic that loads dashboard and host data, but split it
    out of the route file into clearer helpers/components.
- `frontend/src/routes/completed/+page.svelte`
  - Keep as an adjacent route that should inherit the new operator shell later.
- `frontend/src/routes/folders/[...prefix]/+page.svelte`
  - Keep the route structure and studio entry path intact.
- Queue and toast action flows already wired into the home route.
  - Preserve working queue-resume and queue-folder actions instead of
    re-inventing them.

## Rewrite

These pieces are conceptually useful but should be redesigned or restructured
for the workstation model.

- `frontend/src/routes/+page.svelte`
  - Rewrite as a thin orchestrator with a dominant queue region, explicit active
    selection, and smaller dedicated sections.
- `frontend/src/lib/components/Masthead.svelte`
  - Rewrite the shell framing so it feels like workstation navigation rather
    than a soft product header.
- `frontend/src/lib/components/PageShell.svelte`
  - Rework page framing and width rhythm if needed to support a stronger
    operator shell.
- `frontend/src/lib/components/FolderCard.svelte`
  - Keep only if needed outside the main home queue.
  - The home route should not rely on tile/card browsing as its primary queue
    model.
- Home-specific queue, monitor, and context sections.
  - Rebuild these as purpose-fit workstation components instead of one large
    page template.

## Retired from the home route

These pieces represented the old dashboard language and were removed after the
workstation replacement stopped referencing them.

- `frontend/src/lib/components/dashboard/DashboardHero.svelte`
  - Hero framing is the wrong reference model for the operator home screen.
- `frontend/src/lib/components/dashboard/DashboardFolderGrid.svelte`
  - Card-grid folder browsing is not the right primary queue surface.
- `frontend/src/lib/components/HeroCard.svelte`
  - Retired with the dashboard hero wrapper.

## Reassess outside the home route

These old dashboard components are no longer home-screen drivers, but they are
still used by adjacent routes and should be judged in their active context.

- `frontend/src/lib/components/dashboard/DashboardHostGrid.svelte`
  - Used by the Ops route.
- `frontend/src/lib/components/dashboard/DashboardQueues.svelte`
  - Used by the Ops route.

## Shared primitives to reassess

These are not automatic deletions, but they should not be allowed to force the
new design back into the old aesthetic.

- `frontend/src/lib/components/Panel.svelte`
  - Current defaults are too soft and decorative for a workstation shell.
  - Either introduce a stricter operator variant or reduce the global panel
    chrome.
- `frontend/src/lib/components/SectionHead.svelte`
  - Keep if it can express tighter operational headings without marketing-like
    ledes.
- Existing color, radius, and shadow tokens.
  - Reassess them against `docs/style/workstation-ui.md` before broad reuse.

## Implementation guardrails

- Do not rewrite the stack.
- Do not break segment-wise folder routing.
- Do not regress guarded `localStorage` handling or hidden-library semantics.
- Do not let queue selection and active-context state drift apart.
- Prefer extracting focused components over growing `frontend/src/routes/+page.svelte`.
