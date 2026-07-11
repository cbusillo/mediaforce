# Coding Standards

## Purpose

- Define the project-wide coding rules and guardrails for Mediaforce.

## When

- Before implementing changes or refactors
- Before cleanup passes that reshape code or repo conventions

## Core expectations

- Follow best practices, not just minimally passing code
- Prefer the simplest design that is still clean, explicit, and maintainable
- Eliminate code smells, bad practices, and unnecessary complexity before
  calling work done
- Keep naming descriptive and intent-revealing
- Update docs when behavior or workflow meaningfully changes
- Type hints are required at API boundaries: function and method signatures, and
  public data shapes that cross module or runtime boundaries
- Prefer local type inference when it is already clear; add local annotations
  when they materially improve readability or tooling
- Prefer code that reads clearly without comments; reserve comments for why,
  constraints, and decision context
- Extract repeated logic into helpers instead of duplicating code or branching
  the same behavior in parallel places
- Keep touched files warning-clean in Ruff and PyCharm when the warning is
  actionable and the fix is local to the work

## Repo guidance

- Keep `AGENTS.md` thin; put deeper guidance in `docs/`
- Treat machine-local paths and runtime values as config, not code invariants
- Preserve compatibility only when it still buys something concrete
- Use `uv run` for Python tooling and tests instead of invoking Python directly
- Use repo-native validation tools when available: PyCharm inspection for code
  cleanup, browser validation for UI work, and the repo's own npm/pytest entry
  points for automated checks
- Prefer fixing the root cause over adding broad suppressions or workaround
  layers

## Docs as code

- When behavior, workflows, or operator-facing expectations change, update the
  relevant docs in the same change
- Keep cross-references current when style or workflow guidance moves

## Dependency updates

- Keep Python and frontend dependency updates independently reviewable rather
  than combining unrelated ecosystems in one pull request.
- Group routine minor and patch updates, but leave semantic-version majors as
  separate compatibility decisions after a bounded cooldown.
- Keep security updates ungrouped and independently mergeable so routine
  version grouping cannot delay an urgent fix.
- Fix dependency constraints or application compatibility instead of enabling
  legacy peer resolution or weakening acceptance gates.
