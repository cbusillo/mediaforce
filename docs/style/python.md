# Python Style

Use straightforward, typed, production-ready Python.

## Purpose

- Define the house style for Python used in the FastAPI backend, CLI, and
  utilities.

## Core rules

- Type hints are required at API boundaries: all function and method
  signatures, plus public data shapes that act as external contracts
- Prefer local type inference when the type is obvious
- Add local annotations when they materially improve clarity or tooling,
  especially for ambiguous empty containers and values crossing dynamic
  boundaries
- Prefer small functions, early returns, and descriptive names
- Use f-strings, PEP 604 unions, and `pathlib.Path`
- Fix root causes instead of layering workarounds
- Keep comments rare; use them for constraints or why, not for what the code does
- Avoid broad `except Exception` blocks unless they re-raise, classify, or log
  a real boundary failure

## Repo specifics

- FastAPI backend code lives under `mediaforce/`
- Prefer updating existing helpers and modules over adding parallel abstractions
- Keep runtime/config assumptions out of code when they belong in config
- Use Ruff for fast Python annotation/style enforcement and PyCharm inspection
  for cleanup of actionable Python warnings
- Prefer narrow, justified suppressions only when a warning is a confirmed tool
  false positive that cannot be fixed in code

## Before committing

- Remove code smells, dead branches, and avoidable complexity
- Make the code something we would be happy to maintain
