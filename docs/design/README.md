# Mediaforce UI Design Sources

This directory now separates active reset guidance from historical artifacts.
Start with `DIRECTION.md`, which wins over every doc here, then the active
sources below. Treat archived briefs as evidence only.

## Active Reset Sources

- `docs/design/basic-user-vocabulary.md`: user-facing term and workflow-state
  reference. Use the vocabulary as product language, not as proof that old route
  layouts are valid.
- `docs/design/movies-workflow.md`: active Movies Library and movie page
  contract.
- `docs/style/workstation-ui.md`: workstation doctrine.
- `docs/style/frontend.md`: Svelte/frontend implementation and validation
  expectations.

## Quarantined Historical Briefs

Archived docs under `docs/design/archive/` describe previous design attempts and
QA passes. They are not active implementation guidance and must not override the
reset contract.

Archived files include:

- `calm-workstation-visual-system.md`
- `final-love-pass-qa.md`
- `full-frontend-reset-brief.md`
- `workstation-shell-brief.md`
- `workstation-reset-plan.md` (the June 2026 frontend reset plan)
- `workstation-home-screen-brief.md`
- `workstation-home-screen-inventory.md`

Use archived docs only to understand what failed or what product facts may need
verification against current backend/API behavior.
