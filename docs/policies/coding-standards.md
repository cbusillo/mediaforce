# Coding Standards

## Core expectations

- Follow best practices, not just minimally passing code
- Prefer the simplest design that is still clean, explicit, and maintainable
- Eliminate code smells, bad practices, and unnecessary complexity before
  calling work done
- Keep naming descriptive and intent-revealing
- Update docs when behavior or workflow meaningfully changes

## Repo guidance

- Keep `AGENTS.md` thin; put deeper guidance in `docs/`
- Treat machine-local paths and runtime values as config, not code invariants
- Preserve compatibility only when it still buys something concrete
