# Database Tooling

Use the checked-in SQL assets under `mediaforce/core/sql/` as the source of
truth for Mediaforce's SQLite schema.

## Why

- `mediaforce/core/db.py` now loads `mediaforce/core/sql/schema.sql` at runtime
  instead of embedding the schema as a Python string.
- Backward-compatibility migrations that PyCharm previously flagged now live in
  `mediaforce/core/sql/migrations/`.
- This gives PyCharm real `.sql` files it can parse and resolve instead of
  trying to infer schema state from Python string literals.

## PyCharm setup

1. Open the `Database` tool window.
2. Add a SQLite data source for your local Mediaforce database if you want live
   data inspection.
3. Add a DDL data source that points at `mediaforce/core/sql/schema.sql`.
4. Refresh introspection after schema changes.

## Workflow expectations

- When the SQLite schema changes, update `mediaforce/core/sql/schema.sql` in the
  same commit.
- If an existing on-disk database needs compatibility work, add or update a
  targeted SQL asset under `mediaforce/core/sql/migrations/` and keep the
  corresponding Python guard in `mediaforce/core/db.py`.
- Migration assets should stay self-contained enough for PyCharm to resolve the
  target table and columns without relying on file-level suppressions.
- Prefer editing SQL in the checked-in `.sql` files rather than reintroducing
  large inline SQL blocks in Python.
