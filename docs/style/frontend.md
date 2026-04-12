# Frontend Style

Frontend changes should feel intentional and production-ready.

## Core rules

- Preserve the current SvelteKit structure instead of inventing alternate UI paths
- Treat workstation-style redesigns as a product and layout reset, not a reason
  to replace SvelteKit or split the frontend into a second UI stack
- Prefer clear state flow and readable components over clever indirection
- Keep UI copy consistent with the product name `Mediaforce`
- Avoid placeholder-looking UX, stale labels, and migration leftovers
- For home, queue, review, and folder-workspace redesigns, follow
  `docs/style/workstation-ui.md` and optimize for operator workflows rather than
  dashboard polish

## Validation

- Validate UI changes in a real browser
- Prefer browser-visible proof over code-only reasoning
- Keep the built frontend bundle fresh when backend-served UI behavior matters
- When reviewing a primary operator surface, explicitly check for dashboard
  drift: hero whitespace, card-first queue layouts, decorative gradients, and
  color used as branding instead of state
