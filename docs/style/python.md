# Python Style

Read the full shared
[Python and Odoo style](https://github.com/cbusillo/codex-skills/blob/main/skills/references/python-odoo-style.md)
before writing Python. Odoo-specific rules apply only to Odoo; Mediaforce's
supported Python versions and Ruff settings remain in `pyproject.toml`.
Its gate checks annotations; it does not require Ruff formatting.

## Repo specifics

- FastAPI backend code lives under `mediaforce/`
- Type public data shapes that cross module or runtime boundaries.
- Keep runtime/config assumptions out of code when they belong in config
