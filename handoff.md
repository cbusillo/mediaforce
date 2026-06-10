# Mediaforce Frontend Reset Handoff

## Current Reset Stance

Treat the current Mediaforce frontend UX as a failed architecture, not a surface
that needs another polish pass. The reset should start from the workstation
contract below and from durable backend/product facts. Do not let existing
routes, component names, old design briefs, or previous browser QA conclusions
define the new information architecture.

This branch is `codex/mediaforce-frontend-reset`. The repo default branch is
`main`; keep implementation work on task branches and PRs.

## Durable Product Facts

Mediaforce is an operator workstation for semi-automated media processing. The
operator needs to:

- scan a media library and understand what changed
- browse reachable shows, seasons, folders, and item groups
- create representative samples
- review evidence before approving broad work
- revise settings when evidence or warnings say the proposal is not safe
- queue approved processing on workers
- monitor running or waiting work
- validate outputs before promotion
- promote outputs only after validation
- archive originals
- delete archived originals only when scope and safety are explicit

Backend/API/media-processing behavior should be preserved unless a frontend
contract cannot be expressed with the current API. Machine-local paths, runtime
state, review media, SQLite databases, and generated frontend builds are not
repo-level invariants and should not be checked in.

## Failed Frontend Assumptions

Do not preserve these just because they exist:

- top-level `Work`, `Folders`, `Completed`, and `Ops` structure as the primary
  information architecture
- parallel queue and folder-browser concepts that answer overlapping questions
- dashboard/card composition for operational queues
- hidden list caps, including the current top-32 visible-row pattern
- counts that do not map to reachable rows
- search as the only way to reach counted media objects
- custom panels/tokens/components as if they were a complete workstation design
  system
- route-by-route vocabulary cleanup as a substitute for a coherent workbench

Existing frontend code may contain useful API calls, formatting helpers, and
domain transforms. Treat visual composition, navigation, route boundaries, and
component hierarchy as suspect.

## Workstation Contract

The first replacement surface must prove one core workbench:

- explicit scope: show whether the operator is browsing shows, seasons, folders,
  or another concrete object level
- reachable objects: every count shown must map to visible rows through browsing,
  pagination, filtering, or complete scrolling
- visible mechanics: the operator can see current range, total matching rows,
  sort/filter state, loading state, and how to move through all rows
- selected-object inspector: one selected object has durable context, state,
  blockers, evidence status, and affected counts
- next safe action: the primary action is derived from workflow state and says
  why it is safe, blocked, or unavailable
- evidence before approval: full-scope approval must require visible or
  downloadable review evidence
- destructive safety: archive deletion must name scope, permanence, and
  verification state

Search can narrow a result set, but it must never be the only path to a counted
object.

## Documentation State

Active reset docs:

- `handoff.md`
- `docs/design/workstation-reset-plan.md`
- `docs/design/basic-user-vocabulary.md`
- `docs/style/workstation-ui.md`
- `docs/style/frontend.md`
- `docs/policies/acceptance-gate.md`
- `.github/github.json`

Quarantined historical docs live under `docs/design/archive/`. They may explain
how the UI drifted, but they are not implementation guidance. If an archived doc
conflicts with this handoff or the reset plan, this handoff wins.

## First Implementation Slice

Start with the core workbench mechanics before rebuilding every route:

1. remove hidden row caps from the current workbench path
2. add explicit pagination or complete scrolling with range and total copy
3. preserve selection across page movement when possible
4. make scope and filter effects visible
5. ensure the selected-object inspector always corresponds to a reachable row
6. add focused tests for list reachability and pagination math
7. validate the rendered workbench in a real browser

This slice is allowed to reuse existing API payloads and helpers. It should not
be mistaken for endorsement of the old route/component architecture.

## Next Slices

After the first slice proves list reachability:

- define the clean workbench route contract around object scope, list mechanics,
  inspector fields, and action derivation
- replace old route-level composition with the new workbench shell
- rebuild Folder Studio as the evidence/approval workspace for a selected scope
- rebuild Ops around workers, queues, blockers, retries, and schedule state
- rebuild Completed around validation, promotion, archived originals, and safe
  deletion
- only then decide whether `Work`, `Folders`, `Ops`, and `Completed` remain as
  route names or become internal views of one workstation

Keep slices small and reviewable. Browser validation is required for every
operator-surface change.
