# Database Tooling

Use SQLAlchemy table metadata plus Alembic revisions as the source of truth for
Mediaforce's SQLite schema.

## Why

- `mediaforce/core/db.py` now boots databases through Alembic revisions instead
  of applying ad hoc runtime schema patches.
- `mediaforce/core/db_tables.py` defines the current SQLAlchemy metadata shape.
- `mediaforce/core/alembic/versions/` records the ordered schema history used
  for both fresh databases and legacy upgrades.
- `mediaforce/core/sql/schema.sql` remains the compatibility bridge for very
  old pre-Alembic databases and should stay aligned with the initial revision,
  not with later head-only changes.

## PyCharm setup

1. Open the `Database` tool window.
2. Add a SQLite data source for your local Mediaforce database if you want live
   data inspection.
3. If you want a static schema reference, inspect the current metadata in
   `mediaforce/core/db_tables.py` and the ordered revisions under
   `mediaforce/core/alembic/versions/`.
4. Refresh introspection after migration changes.

## Workflow expectations

- When the SQLite schema changes, update `mediaforce/core/db_tables.py` and add
  a new Alembic revision under `mediaforce/core/alembic/versions/` in the same
  change.
- Keep Alembic revisions hand-authored and SQLite-aware. Prefer explicit DDL or
  well-understood Alembic operations over broad autogeneration assumptions.
- Preserve `mediaforce/core/sql/schema.sql` as the initial bridge for legacy
  databases. Update it only when the initial normalized baseline truly changes.
- Add or update regression coverage in `tests/test_db_runtime.py` whenever the
  legacy bridge, head revision, or migration ordering changes.
- Validate schema work with:
  - `PYTHONPATH=. uv run --with pytest pytest tests/test_db_runtime.py`
  - `PYTHONPATH=. uv run --with pytest pytest`
    `tests/test_encode_queue_recovery.py tests/test_tuning_runtime.py`
  - `uv run mediaforce --help`

## Creating a new migration

1. Update `mediaforce/core/db_tables.py` to reflect the desired head schema.
2. Add a new Alembic revision file under `mediaforce/core/alembic/versions/`
   with explicit `upgrade()` and `downgrade()` steps.
3. If the change must also apply to pre-Alembic databases, make sure the legacy
   bridge in `mediaforce/core/db_migrations.py` still normalizes old databases
   to the initial revision before the new revision runs.
4. Add or update tests proving both fresh-database and legacy-database upgrade
   behavior.
5. Run the validation commands above before closing the change.
