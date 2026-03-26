# Mediaforce TODO

## Current priorities

1. Fix UI regressions introduced during the move from the older refreshing page
   flow to the SvelteKit frontend.
2. Bring the Svelte UI back to parity on the operator-critical paths:
   dashboard, folder calibration, queue visibility, and settings.
3. Make browser-driven validation a normal part of frontend work so UI changes
   are checked against actual rendered behavior, not just code review.

## Immediate tasks

- Audit the Svelte dashboard at `frontend/src/routes/+page.svelte` against the
  expected operator workflow and identify missing data, broken actions, or
  degraded affordances.
- Audit the folder workstation at
  `frontend/src/routes/folders/[...prefix]/+page.svelte`, especially anything
  related to calibration state, action gating, and host status visibility.
- Audit the settings page at `frontend/src/routes/settings/+page.svelte` for
  missing controls, persistence issues, or placeholders that still reflect
  migration-era assumptions.
- Verify that FastAPI endpoints consumed by the Svelte pages still expose the
  data the UI needs without relying on the old server-rendered page behavior.

## Secondary tasks

- Decide which legacy compatibility names should remain user-facing and which
  should stay internal only.
- Add more targeted tests around the API contracts that the Svelte frontend now
  depends on.
- Tighten docs so operator workflows describe the standalone repo and current
  frontend/backend split clearly.

## Acceptance mindset

- Prefer browser-visible evidence for UI work.
- Prefer the targeted backend and frontend checks from `AGENTS.md`.
- Do not call the migration done just because pages render; confirm that the
  operator workflow is still good.

