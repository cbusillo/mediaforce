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
- Update docs when behavior or workflow meaningfully changes
- Follow [Python style](../style/python.md) for the full shared guide and
  Mediaforce's Python-specific guidance.
- Keep touched files warning-clean in Ruff when the warning is actionable
  and the fix is local to the work

## Repo guidance

- Keep `AGENTS.md` thin; put deeper guidance in `docs/`
- Treat machine-local paths and runtime values as config, not code invariants
- Preserve compatibility only when it still buys something concrete
- Follow the shared [Python uv workflow](https://github.com/cbusillo/codex-skills/blob/main/skills/python-uv-workflow/SKILL.md).
- Follow [inspection authority](acceptance-gate.md#inspection-authority) for IDE validation.
- Use the repo's own pytest and Mac app entry points for automated checks
- Prefer fixing the root cause over adding broad suppressions or workaround
  layers

## Docs as code

- When behavior, workflows, or user-facing expectations change, update the
  relevant docs in the same change
- Keep cross-references current when style or workflow guidance moves

## Dependency updates

- Keep Python and Mac app dependency updates independently reviewable rather
  than combining unrelated ecosystems in one pull request.
- Group routine minor and patch updates, but leave semantic-version majors as
  separate compatibility decisions after a bounded cooldown.
- Keep security updates ungrouped and independently mergeable so routine
  version grouping cannot delay an urgent fix.
- Fix dependency constraints or application compatibility instead of enabling
  legacy peer resolution or weakening acceptance gates.
