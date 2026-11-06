# Python Style

Read the full shared
[Python and Odoo style](https://github.com/cbusillo/codex-skills/blob/main/skills/references/python-odoo-style.md)
before writing Python. Odoo-specific sections apply only to Odoo; Mediaforce's
supported Python versions and Ruff settings remain in `pyproject.toml`.

## Repo specifics

- FastAPI backend code lives under `mediaforce/`
- Type public data shapes that cross module or runtime boundaries.
- Keep runtime/config assumptions out of code when they belong in config
- Use Ruff for fast Python annotation/style enforcement.
- Follow [inspection authority](../policies/acceptance-gate.md#inspection-authority) for IDE validation.
