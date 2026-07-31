# Database Tooling

Use SQLAlchemy table metadata plus Alembic revisions as the source of truth for
Mediaforce's SQLite schema.

## Why

- `mediaforce/core/db.py` now boots databases through Alembic revisions instead
  of applying ad hoc runtime schema patches.
- `mediaforce/core/db_tables.py` defines the current SQLAlchemy metadata shape.
- `mediaforce/core/db_migration_scripts/versions/` records the ordered schema
  history used
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
   `mediaforce/core/db_migration_scripts/versions/`.
4. Refresh introspection after migration changes.

## Workflow expectations

- When the SQLite schema changes, update `mediaforce/core/db_tables.py` and add
  a new Alembic revision under `mediaforce/core/db_migration_scripts/versions/`
  in the same change.
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

## Runtime transaction boundaries

- Writable `open_db()` calls automatically honor any active Mediaforce runtime
  lease. The lease pins one database inode, guards every migration and SQLite
  connection before SQL is issued, and rejects pathname replacement instead of
  adopting a new inode. A runtime that may initialize a missing database must
  call `reserve_mediaforce_database_identity(..., create_if_missing=True)`
  before its first writable open. Read-only opens remain unrestricted outside a
  runtime lease, but under an active lease they pin the same lease generation
  and recheck the database pathname before connect, before queries, and on clean
  context exit. Derivation runtime-context commitments include the database
  device and inode so a replacement between separately locked phases cannot be
  adopted as the same frozen context.
- Guarded SQLite connects bind two identities: the canonical parent directory
  device/inode and the database leaf's device, inode, change time, and link
  count. The parent and leaf are opened with no-follow descriptor-relative
  operations; the opened parent descriptor must match the expected canonical
  parent before SQLite is called. After the DBAPI connection opens, Mediaforce
  revalidates the canonical parent and leaf, the held parent and leaf
  descriptors, the descriptor-relative leaf, and the platform-pinned path
  (`/.vol` on macOS or `/proc/self/fd` on Linux). Both descriptors remain open
  until the SQLite connection closes. This prevents a substituted directory or
  a hardlink to the same database inode from redirecting SQLite's WAL/SHM
  namespace to another parent.
- The one-time legacy `state/library.sqlite3` relocation is a separate,
  runtime-locked transfer rather than a file move. It fails closed unless the
  legacy source is a stable, single-link regular file protected by the same OFD
  database-identity primitive used by active runtimes. Both SQLite connections
  open through the retained parent descriptor (`/.vol` on macOS or
  `/proc/self/fd` on Linux), and the reported main-database inode must match the
  retained source before and after backup. A retained vnode/inotify guard makes
  any transient rename, relink, replacement, or write to that source identity a
  permanent migration failure even if the original pathname is restored before
  the next stat check. Before the first SQLite open, missing WAL, SHM, and
  rollback-journal paths are created as owner-only empty files and all three
  inode identities are bound. SQLite may initialize those bound inodes while
  acquiring the no-wait source write gate, but any sidecar removal, replacement,
  relink, or later WAL write fails the transfer. The source is copied through
  that verified SQLite snapshot while the write gate excludes new commits,
  preserving committed WAL state without moving sidecar files.
  Before a staging file becomes visible, an owner-only durable `copying` intent
  binds its reserved name, the source inode and parent, and the configured
  destination and parent. After backup, quick-check, and fsync, that intent is
  atomically finalized to `ready` with the staged inode and exact backup bytes.
  The staged database is then atomically linked into an absent configured
  destination and its directory is fsynced. Cleanup removes and syncs the bound
  WAL, SHM, and rollback journal before removing and syncing the legacy main
  database, so a missing main file proves sidecar cleanup completed. Recovery
  discards only an intent-bound partial stage, or—after publication—requires a
  fresh gated SQLite backup to match the exact staged bytes before completing
  source/staging cleanup and intent removal. Parent replacement, incomplete
  cleanup, metadata-only divergence, or any other ambiguous state fails closed.
  Startup reserves the published destination only after migration or recovery
  returns.
- SQLite URLs use URI modes explicitly: `rwc` for first-use creation, `rw` for
  guarded existing databases, and `ro` for read-only opens. Path components are
  percent-quoted so spaces, `#`, `?`, `%`, and legal colons remain part of the
  filename rather than URI syntax. Alembic configuration escapes the encoded
  percent signs only at the ConfigParser boundary; SQLAlchemy and the DBAPI see
  the original URI.
- Authoritative workflows may register a final `open_db()` pre-commit guard.
  The guard runs while the transaction is still reversible; if a retained
  artifact directory no longer matches its canonical path, the guard fails and
  `open_db()` explicitly rolls the transaction back before closing the
  connection.
- End SQLite write transactions before starting ffmpeg, ffprobe, ab-av1, remote
  host waits, or other long-running media work.
- Persist the queued or running state, commit it, perform the media work, then
  use a short transaction to persist completion or failure state.
- Keep heartbeat, cancellation, polling, and recovery writes independent from
  media-process lifetimes so CLI and web workers can make progress together.
- Flush pending catalog writes before media probes, and commit promotion state
  per item before the next item starts probing.
- Recheck stale worker leases while holding a short SQLite write claim, persist
  the ownership transition, then perform retry cleanup without that claim.
- Record standalone CLI ownership with the encode event so web recovery does
  not remove artifacts while that process is still active.
- Treat failure-event persistence as secondary to the original media failure.
  Commit successful encode or promotion state first, and retain event failures
  as diagnostics without replacing the primary result or exception.

## Creating a new migration

1. Update `mediaforce/core/db_tables.py` to reflect the desired head schema.
2. Add a new Alembic revision file under
   `mediaforce/core/db_migration_scripts/versions/`
   with explicit `upgrade()` and `downgrade()` steps.
3. If the change must also apply to pre-Alembic databases, make sure the legacy
   bridge in `mediaforce/core/db_migrations.py` still normalizes old databases
   to the initial revision before the new revision runs.
4. Add or update tests proving both fresh-database and legacy-database upgrade
   behavior.
5. Run the validation commands above before closing the change.
