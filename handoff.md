# Mediaforce Workstation UX Handoff

## Context

This handoff captures the current UX diagnosis and my opinions after working on
the Mediaforce operator surfaces. It is intentionally candid. The goal is to
avoid another local patch cycle that makes one symptom better while keeping the
overall workstation model incoherent.

## Facts

- The frontend uses SvelteKit, Svelte, and Vite as the app framework.
- The frontend does not currently use a mature UI component framework or design
  system such as Carbon, PatternFly, Material UI, Bootstrap, Radix/shadcn, or a
  comparable library.
- The current UI is mostly custom Svelte components plus custom CSS:
  `OperatorShell`, `WorkstationPanel`, `StateBadge`, route-specific workstation
  views, and custom table/control styling.
- Shared styling exists through custom tokens in
  `frontend/src/lib/design/tokens.css`, but those tokens do not provide a full
  interaction model for dense operator workflows.
- The product has repeatedly drifted between these concepts:
  - Work queue
  - Folder/media browser
  - Show/season/folder scope chooser
  - Workflow launcher
  - Review/approval workstation
- `Work` and `Folders` have been combined and separated multiple times. They do
  nearly the same job, but each page tends to be missing something the other has.
- A count such as `185 whole shows` is not enough if only 32 rows feel reachable.
  Search is not a substitute for browse/navigation when the operator does not
  already know the target name.
- The current custom list/table behavior has produced silent or confusing limits,
  including a top-32 visible result pattern.
- The review flow can be made safer locally, for example by making sample
  evidence download-first, but that does not fix the broader information
  architecture problem.

## My Opinion

The main problem is not Svelte. The problem is that Mediaforce has an app
framework but not a strong enough UI/product framework for this operator
experience.

SvelteKit gives us a way to build pages. It does not, by itself, prevent bad
operator UX. It does not decide table pagination, browse semantics, toolbar
structure, empty states, row density, filter behavior, drill-in hierarchy, or
how counts map to reachable objects.

I think continuing to patch this custom UI without adopting a framework or
formal UX contract will keep recreating the same failures. The likely pattern is:

1. A specific pain appears.
2. We patch that local pain.
3. The patch introduces or reveals another inconsistency.
4. The UI shifts between queue, browser, and workflow concepts again.

That is how the app keeps falling back into `Work` vs. `Folders`, hidden show
access, misleading counts, and unclear browse paths.

## UX Contract I Think We Need

Before more UI implementation, define a simple workstation contract:

- Primary objects: shows, seasons, folders/items.
- Primary view modes: needs attention, all media.
- Primary actions: review sample, queue work, validate output, promote output,
  clean up archive.
- Navigation rule: every count shown in the UI must map to reachable rows.
- List rule: never silently cap visible results. If results are paginated, show
  the range and controls. If virtualized, make scrolling obvious and complete.
- Review safety rule: full-scope approval should come after review evidence is
  visible/downloadable, not before.
- Scope rule: show/season/folder are scopes within one workbench, not separate
  top-level products.

## Recommended Direction

Adopt an external design system or, at minimum, an external interaction pattern
spec before further workstation UI work.

My preferred option if staying with Svelte is Carbon:

- Carbon is an enterprise design system rather than only a set of primitives.
- It has established data-table, toolbar, filtering, and pagination patterns.
- It has a Svelte implementation through `carbon-components-svelte`.
- Its table and pagination conventions directly address the kind of dense
  operator UI Mediaforce needs.

PatternFly is also a useful reference for admin/operator UX, especially around
data views, toolbars, pagination, filters, and empty states. It may be better as
an interaction reference than as a direct dependency because its primary
component ecosystem is not Svelte-first.

I would not rely on a purely headless component library as the main solution.
Headless primitives can improve accessibility and mechanics, but they still
leave us inventing the information architecture, visual hierarchy, table
behavior, and workflow model ourselves. That is the trap we are already in.

## Concrete Product Shape

The main surface should be one media workbench, not separate Work and Folders
tabs:

```text
Work
  Scope: Shows | Seasons | Folders
  View: Needs attention | All media
  Sort: Recommended | Name | Status | Size | Reclaim
  Search
  Filters
  Results: all matching rows, via real pagination or complete scrolling
  Inspector: selected object details and next action
```

Drill-in surfaces should be scoped and explicit:

```text
Show Studio
  Show-level state
  Season list
  Representative sample/review evidence
  Full-show approval only after evidence is reviewed

Season Studio
  Season-level state
  Episode/item list
  Same review workflow, scoped to season

Folder/item detail
  Only when a lower-level object truly needs direct handling
```

## Options

### Option A: Adopt Carbon for Svelte

Use Carbon components and Carbon interaction patterns as the binding constraint.
This is my recommendation if Svelte remains the frontend framework.

Expected benefits:

- Fewer invented controls.
- Better table/pagination discipline.
- More consistent dense enterprise UI behavior.
- A framework-backed reason to stop oscillating between custom concepts.

Risk:

- Requires a deliberate migration and some visual redesign.
- Carbon may feel opinionated compared with the current custom look.

### Option B: Keep Custom Components, Use Carbon/PatternFly as a UX Spec

Do not add a component dependency, but write an explicit local workstation spec
based on Carbon/PatternFly patterns and enforce it in reviews.

Expected benefits:

- Less dependency churn.
- Can preserve more of the current visual language.

Risk:

- Easier to drift back into custom bad patterns because the framework is not
  mechanically constraining implementation.

### Option C: Switch to a Frontend Ecosystem with Stronger Enterprise UI Options

Move away from Svelte for operator surfaces if the team wants a broader mature
component ecosystem.

Expected benefits:

- More mature data-grid/admin UI choices.

Risk:

- Much larger migration.
- May be overkill if Carbon for Svelte is acceptable.

## What Not To Do

- Do not add another top-level tab for a slightly different media view.
- Do not keep parallel `Work` and `Folders` concepts.
- Do not silently cap lists at 32 or any other number.
- Do not make search the only way to reach counted objects.
- Do not keep fixing terminology one label at a time.
- Do not approve full-scope processing before review evidence is obvious and
  accessible.
- Do not treat custom CSS tokens as a full UX/design system.

## Current Working Opinion

The safest next step is not another UI patch. It is a design-system decision plus
a short workstation UX contract. After that, rebuild or refactor the Work surface
around the chosen system.

My bias: choose Carbon for Svelte, define the Work page around Carbon-style
toolbar + data table + pagination patterns, and make show/season/folder scope a
first-class control inside one Work surface.
