# Python Style

Use straightforward, typed, production-ready Python.

## Core rules

- Type public function signatures and important data shapes
- Prefer small functions, early returns, and descriptive names
- Use f-strings and `pathlib.Path`
- Fix root causes instead of layering workarounds
- Keep comments rare; use them for constraints or why, not for what the code does

## Repo specifics

- FastAPI backend code lives under `mediaforce/`
- Prefer updating existing helpers and modules over adding parallel abstractions
- Keep runtime/config assumptions out of code when they belong in config

## Before committing

- Remove code smells, dead branches, and avoidable complexity
- Make the code something we would be happy to maintain
