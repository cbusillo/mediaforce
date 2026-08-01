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
  the next stat check. After cleanup seals, Linux filters named unrelated
  sibling events, while macOS kqueue directory churn has no name and fails
  closed. Before the first SQLite open, missing WAL, SHM, and
  rollback-journal paths are created as owner-only empty files and all three
  inode identities are bound. SQLite may initialize those bound inodes while
  acquiring the no-wait source write gate, but any sidecar removal, replacement,
  relink, or later WAL write fails the transfer. A pre-publication failure moves
  only byte-empty, metadata-identical guard reservations into a separate
  deterministic reservation quarantine; changed or pre-existing sidecars are
  never removed. The source copy uses that verified SQLite snapshot while the
  write gate excludes new commits, preserving committed WAL state without
  moving sidecar files.
  Before a populated staging file becomes visible, an owner-only durable
  `copying` intent binds its reserved name, the source inode and parent, and the
  configured destination and parent. The observable staging pathname remains
  bound to its original empty owner-only inode. SQLite writes the backup and
  runs `quick_check` only inside a private temporary directory; Mediaforce then
  copies those verified bytes through the retained staging descriptor, fsyncs
  that inode and parent, and rejects any pathname replacement before mutation.
  New intents use schema version 5. They explicitly
  record the cleanup policy, publication policy, any parent-v4 cleanup prefix
  that was already deleted, and the exact retained cleanup-artifact set. After
  backup, quick-check, and fsync, the intent advances to `ready` with the staged
  inode and exact backup bytes.
  Intent transitions never overwrite or unlink the canonical pathname. The
  successor is first published under a digest-derived transition name, both
  files are opened and bound by descriptor, and macOS `RENAME_SWAP` or Linux
  `RENAME_EXCHANGE` atomically exchanges them. The canonical name receives the
  successor while the transition name retains the exact predecessor as a
  tombstone. Recovery validates the transition graph and resumes either the
  prepared pre-exchange or claimed post-exchange state. Prepared completion
  edges re-enter the checked finalizer instead of being replayed generically,
  including the legacy direct `cleaning`-to-`complete` edge that pre-seal builds
  could leave behind. If either pathname is replaced at the exchange boundary,
  descriptor/inode checks fail while the trusted and replacement artifacts
  remain present. Finalization first exchanges
  to a durable schema-v5 `sealing` intent. Recovery keeps requiring the exact
  published bytes, an empty destination sidecar namespace, valid SQLite content,
  and complete legacy cleanup until a second checked exchange linearizes the
  runtime handoff as `complete`. The canonical completion record is retained
  instead of being removed. Later startups revalidate the published inode, the
  legacy cleanup namespace, and any WAL/SHM/journal entries as owner-controlled,
  single-link regular files while allowing ordinary in-place database writes and
  runtime SQLite sidecar content after the migration handoff.
  Scratch preparation files and interrupted copy files are inert residue: they
  are never deleted by pathname, never become authoritative without an
  exclusive publication step, and a later attempt uses a fresh reserved name.
  The configured destination parent is retained by descriptor. Fresh schema-v5
  publication first requires an empty destination WAL/SHM/journal namespace,
  exclusively renames the staging inode to the destination, fsyncs the
  directory, and validates the exact inode and bytes plus sidecar absence through
  the platform-pinned directory path. A destination that wins the exclusive rename
  is preserved alongside the staging file and causes a fail-closed result; there
  is no validation-then-unlink rollback. Older v2-v4 hardlink publications are
  still recognized. A destination that appears after the legacy source lock but
  before intent creation is likewise preserved and fails closed instead of being
  adopted silently. A surviving legacy staging alias remains linked to the
  destination and is recorded by the publication policy rather than removed.
  Every intent transition preserves the destination-parent identity captured by
  its predecessor and verifies that identity against the retained transition
  directory descriptor. The configured parent is also rechecked before every
  source retirement, so a destination-parent swap or copying-to-ready rebind
  stops cleanup before the legacy main is retired. Before completion becomes
  authoritative, Mediaforce also publishes an immutable migration receipt in
  the machine-local runtime-reservation namespace, outside the replaceable
  destination parent. The configured reservation namespace must remain
  independently discoverable: neither its configured path nor any symlink
  expansion may pass through an entry hidden by destination replacement. A
  configuration that places that namespace at or below the destination parent,
  directly or through an alias, is rejected before migration, recovery, fresh
  database creation, or adoption of an existing destination can proceed,
  regardless of whether legacy evidence remains visible. Startup checks
  migration authority even when configuration is changed back to the legacy
  database path: an existing receipt, intent, or retired-source residue blocks
  same-path adoption or fresh creation. Startup also checks authority after the
  legacy `state/` directory has become empty: a surviving receipt or retired-source
  quarantine without the canonical destination intent fails closed instead of
  permitting a replacement database to be created or adopted. This also
  upgrades the parent-v4 all-absent cleanup case on its first verified startup.
  A live legacy WAL, SHM, or journal sidecar without its main database and
  without migration authority is likewise ambiguous residue and blocks startup;
  Mediaforce never treats that state as permission to create a replacement
  database.
  Unsafe custom layouts are intentionally not grandfathered. Before upgrading
  such an installation, stop Mediaforce, move the complete reservation
  directory atomically to a direct non-symlink path outside the destination
  parent, update `state.runtime_reservation_dir`, and preserve every migration
  receipt.
  Cleanup claims the exact legacy main first with an exclusive rename to an
  identity-derived quarantine name, syncs that namespace transition, and
  validates the claimed inode. It retains that exact quarantine as authorized
  migration residue; WAL, SHM, and the rollback journal follow in order. Live
  cleanup consumes the durable `cleaning` manifest rather than recapturing
  current sidecar metadata: every main/sidecar inode must still match all
  recorded fields and its descriptor-read SHA-256 before its exclusive claim and
  again while quarantined; only the expected rename-induced ctime change is
  relaxed afterward. A crash-surviving quarantine is durable progress. If
  another inode appears at a claim or final verification boundary, cleanup stops
  with both the trusted quarantine and replacement preserved.
  Schema-v4 `cleaning` intents from parent `8236711` are upgraded to version 5
  before any further mutation. Recovery accepts only states that implementation
  could have made: an absent cleanup prefix followed by at most one in-flight
  quarantine and then untouched live suffixes, including the durable all-absent
  window, or the later retained-quarantine prefix followed by live suffixes.
  Incomplete v4 cleanup must still have the original hardlink staging alias;
  after complete source cleanup either the alias or the parent's already-removed
  alias state is valid. Replacements, live/quarantine conflicts, out-of-order
  progress, unidentified retirement names, and mismatched digests fail closed.
  The v5 manifest records the exact legacy-absent prefix and exact quarantines
  that the new retained policy must produce, so final completion can recheck the
  namespace without guessing from absence.
  Version-2 `ready` and version-3 `cleaning` intents remain supported. When the
  live main exists, gated recovery captures a schema-v5 digest manifest before
  retirement. When the main is already retired, recovery snapshots and preserves
  the exact stat/content-bound legacy residue rather than deleting data it cannot
  authorize. Parent replacement, source recreation, malformed legacy quarantine
  state, metadata-only divergence, or any other ambiguous state fails closed.
  A later startup also fails closed if both legacy and configured databases exist
  without a resumable intent.
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
