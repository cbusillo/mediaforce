# Frontend Style

Frontend changes should feel intentional and production-ready.

## Core rules

- Preserve the SvelteKit app/runtime unless a specific contract reason says
  otherwise; do not preserve current route/component structure merely because it
  exists
- Treat workstation-style redesigns as a product and layout reset, not a reason
  to replace SvelteKit or split the frontend into a second UI stack
- Prefer clear state flow and readable components over clever indirection
- Keep UI copy consistent with the product name `Mediaforce`
- Avoid placeholder-looking UX, stale labels, and migration leftovers
- For workbench, review, worker, completed, and settings redesigns, follow
  `docs/style/workstation-ui.md`, `handoff.md`, and
  `docs/design/workstation-reset-plan.md`

## Validation

- Validate UI changes in a real browser
- Prefer browser-visible proof over code-only reasoning
- Keep the built frontend bundle fresh when backend-served UI behavior matters
- When reviewing a primary operator surface, explicitly check for unreachable
  counts, search-only access to counted objects, hidden row caps, dashboard
  drift, and actions that do not explain why they are safe or blocked
