# Workstation Reset Plan

## Goal

Replace the Mediaforce frontend from a clean operator-workstation contract. The
reset is not a cosmetic pass over existing routes. It must make the next safe
operator decision obvious while ensuring counted media objects are reachable
without relying on search.

## Non-Goals

- preserving the current `Work`/`Folders` split as product architecture
- preserving current custom panels, route layout, or card/list composition
- changing backend/media-processing behavior without a concrete frontend
  contract need
- adding new top-level surfaces before the core workbench is proven

## Durable Contract

Every primary operator surface must show:

- scope: the object level and library or prefix being acted on
- reachable rows: counts, current range, total, and movement controls
- visible filters and sort state
- selected object context
- evidence or blockers that affect approval
- the next safe action and why it is enabled or blocked

## Slice 1: Reachable Workbench Mechanics

Deliverables:

- remove silent list caps from the current workbench path
- add explicit pagination or complete scrolling with range and total copy
- keep the selected-object inspector tied to a row the operator can reach
- expose scope and filter effects in the table header
- add focused unit coverage for pagination/list mechanics
- run frontend checks and browser validation

Acceptance:

- a count such as `185 whole shows` has a visible path to all 185 rows
- the operator can move through the list without search
- the inspector never points at an invisible, unreachable object without saying
  how to reach it

## Slice 2: Workbench Contract Extraction

Deliverables:

- extract object-scope, list-state, and action-state helpers away from current
  route composition
- define view-model types for scope, row identity, row state, inspector state,
  and next action
- add tests that prove counts, filters, pagination, and selection stay coherent

Acceptance:

- the workbench can be rendered from a contract without preserving old route
  names or component hierarchy

## Slice 3: Evidence And Approval Workspace

Deliverables:

- rebuild the selected-scope workspace around sample status, review evidence,
  proposal state, blockers, and approval/revision actions
- make download/open review evidence prominent before approval
- make stale or missing evidence block broad approval

Acceptance:

- broad processing cannot be approved from a screen that hides the evidence
  status or blocker reason

## Slice 4: Workers, Validation, Promotion, Cleanup

Deliverables:

- rebuild worker/queue operations around waiting reasons, retries, schedule
  state, and worker readiness
- rebuild validation/promotion state so completed outputs are not confused with
  safe deletion
- rebuild archive cleanup around explicit scope, verification, and permanence

Acceptance:

- destructive cleanup names what will be deleted and why it is safe or blocked
- processing recovery actions are tied to the specific failed or waiting work

## Validation

Use `.github/github.json` for commands. For every UI slice, run focused frontend
checks and validate the affected surface in a real browser. Full acceptance
should include backend tests, frontend check/lint/test/build, CLI smoke, and the
repo acceptance script when practical.
