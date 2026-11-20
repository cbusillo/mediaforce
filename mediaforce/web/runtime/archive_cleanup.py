import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import library_items, staged_artifacts
from mediaforce.encoding.delivery_lock import archive_activity
from mediaforce.encoding.staging import safe_unlink
from mediaforce.ops.runtime_install import update_hold_reason
from mediaforce.encoding.source_publication import commit_archive_removal, pending_archive_paths, prepare_archive_removal, reconcile_archive_removals
from mediaforce.web.settings_runtime import settings_archive_root


def archive_cleanup_summary(config: MediaforceConfig, *, transcode_root: str | None = None) -> dict[str, Any]:
    archive_root = _archive_root_for_cleanup(config, transcode_root=transcode_root)
    if archive_root is None:
        return {
            "archive_root": "",
            "file_count": 0,
            "total_size_bytes": 0,
            "has_cleanup": False,
        }
    file_count = 0
    total_size_bytes = 0

    if archive_root.exists():
        for path in archive_root.rglob("*"):
            if not path.is_file():
                continue
            file_count += 1
            try:
                total_size_bytes += path.stat().st_size
            except FileNotFoundError:
                continue

    return {
        "archive_root": str(archive_root),
        "file_count": file_count,
        "total_size_bytes": total_size_bytes,
        "has_cleanup": file_count > 0,
    }


class BackupDeletion:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._facts: dict[str, Any] | None = None

    def facts(self) -> dict[str, Any] | None:
        with self._lock:
            if self._facts is None:
                return None
            return {**self._facts, "skipped_files": self._facts["skipped_files"].copy()}

    def begin(self) -> None:
        with self._lock:
            self._facts = {"state": "running", "total_count": None, "total_bytes": None, "deleted_count": 0,
                           "freed_bytes": 0, "kept_count": 0, "skipped_files": [], "failed_file": None, "failure_reason": None}

    def counted(self, total_count: int, total_bytes: int, kept_count: int) -> None:
        self._update(total_count=total_count, total_bytes=total_bytes, kept_count=kept_count)

    def removed(self, size_bytes: int) -> None:
        with self._lock:
            assert self._facts is not None
            self._facts["deleted_count"] += 1
            self._facts["freed_bytes"] += size_bytes

    def skipped(self, file: str, error: OSError) -> None:
        with self._lock:
            assert self._facts is not None
            self._facts["skipped_files"].append({"file": file, "reason": _failure_reason(error)})

    def failed(self, error: BaseException, *, file: str | None = None) -> None:
        with self._lock:
            if self._facts is None or self._facts["state"] != "running":
                return
            self._facts.update(state="failed", failed_file=file, failure_reason=_failure_reason(error))

    def finished(self) -> None:
        self._update(state="finished")

    def _update(self, **changes: Any) -> None:
        with self._lock:
            assert self._facts is not None
            self._facts.update(changes)


def _failure_reason(error: BaseException) -> str:
    if isinstance(error, OSError) and error.strerror:
        return error.strerror
    return str(error) or type(error).__name__


def clear_archive_cleanup_action(
        config: MediaforceConfig,
        *,
        transcode_root: str | None = None,
        on_removed: Callable[[Path], None] | None = None,
        progress: BackupDeletion | None = None,
) -> dict[str, Any]:
    with archive_activity(config.paths.db_path, cleanup=True) as available:
        if not available:
            return {"ok": False, "message": "Files are being published. Retry cleanup when they finish.",
                    "removed_count": 0, "removed_size_bytes": 0,
                    "archive_cleanup": archive_cleanup_summary(config, transcode_root=transcode_root)}
        if hold := update_hold_reason(config.paths.db_path.parent):
            return {"ok": False, "message": hold, "removed_count": 0, "removed_size_bytes": 0,
                    "archive_cleanup": archive_cleanup_summary(config, transcode_root=transcode_root)}
        progress = progress or BackupDeletion()
        progress.begin()
        try:
            result = _clear_archive_cleanup_action(
                config, transcode_root=transcode_root, on_removed=on_removed, progress=progress)
        except BaseException as error:
            progress.failed(error)
            raise
        progress.finished()
        return result


def _clear_archive_cleanup_action(
        config: MediaforceConfig,
        *,
        transcode_root: str | None,
        on_removed: Callable[[Path], None] | None,
        progress: BackupDeletion,
) -> dict[str, Any]:
    if config.paths.db_path.is_file():
        with open_db(config.paths.db_path) as connection:
            reconcile_archive_removals(connection)
    archive_root = _archive_root_for_cleanup(config, transcode_root=transcode_root)
    summary = archive_cleanup_summary(config, transcode_root=transcode_root)
    if archive_root is None:
        progress.counted(0, 0, 0)
        return {
            "ok": True,
            "message": "No original backups are waiting in the Cleanup folder.",
            "removed_count": 0,
            "removed_size_bytes": 0,
            "archive_cleanup": summary,
        }
    if not archive_root.exists() or summary["file_count"] <= 0:
        progress.counted(0, 0, 0)
        return {
            "ok": True,
            "message": "No original backups are waiting in the Cleanup folder.",
            "archive_cleanup": summary,
        }

    protected = recovery_archive_paths(config, archive_root)
    files = [path for path in archive_root.rglob("*") if path.is_file()]
    deletable = [(path, _size_bytes(path)) for path in files if not archive_path_needs_recovery(path, protected)]
    preserved_count = len(files) - len(deletable)
    progress.counted(len(deletable), sum(size for _path, size in deletable), preserved_count)
    for path, size_bytes in deletable:
        try:
            with open_db(config.paths.db_path) as connection:
                operations = prepare_archive_removal(connection, path)
                try:
                    safe_unlink(path)
                except OSError as error:
                    progress.skipped(str(path.relative_to(archive_root)), error)
                    continue
                commit_archive_removal(connection, operations)
        except BaseException as error:
            progress.failed(error, file=str(path.relative_to(archive_root)))
            raise
        progress.removed(size_bytes)
        if on_removed is not None:
            on_removed(path)

    # Remove empty directories from the bottom up, but keep the archive root itself.
    for path in sorted(archive_root.rglob("*"), reverse=True):
        if not path.is_dir():
            continue
        try:
            path.rmdir()
        except OSError:
            continue

    facts = progress.facts()
    assert facts is not None
    removed_count = facts["deleted_count"]
    return {
        "ok": True,
        "message": f"Deleted {removed_count} original backup{'s' if removed_count != 1 else ''}."
                   + (" Kept originals needed by unpublished files. Inspect and restore those files before cleanup."
                      if preserved_count else ""),
        "preserved_count": preserved_count,
        "removed_count": removed_count,
        "removed_size_bytes": facts["freed_bytes"],
        "skipped_files": facts["skipped_files"],
        "archive_cleanup": archive_cleanup_summary(config, transcode_root=transcode_root),
    }


def _size_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _archive_root_for_cleanup(config: MediaforceConfig, *, transcode_root: str | None = None) -> Path | None:
    if transcode_root is not None and transcode_root.strip():
        return config.local_media_path(settings_archive_root(transcode_root))
    try:
        return config.archive_root
    except KeyError:
        return None


def recovery_archive_paths(config: MediaforceConfig, archive_root: Path) -> set[Path]:
    """Unpublished items may still need their archived original for recovery."""
    if not config.paths.db_path.is_file():
        return set()
    with open_db(config.paths.db_path) as connection:
        rows = connection.execute(
            select(library_items.c.rel_path, staged_artifacts.c.archived_source_path)
            .join(staged_artifacts, library_items.c.id == staged_artifacts.c.library_item_id)
            .where(staged_artifacts.c.promoted_at.is_(None))
        ).mappings().all()
        pending = pending_archive_paths(connection)
    return pending | {path for row in rows for path in (
        (archive_root / str(row["rel_path"])).resolve(),
        *((Path(str(row["archived_source_path"])).resolve(),) if row["archived_source_path"] else ()),
    )}


def archive_path_needs_recovery(path: Path, protected: set[Path]) -> bool:
    resolved = path.resolve()
    return resolved in protected or any(
        resolved.parent == original.parent
        and resolved.name.startswith(f".{original.name}.promotion-backup-")
        for original in protected
    )
