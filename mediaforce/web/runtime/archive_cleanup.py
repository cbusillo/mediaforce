from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import library_items, staged_artifacts
from mediaforce.encoding.delivery_lock import archive_activity
from mediaforce.encoding.staging import safe_unlink
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


def clear_archive_cleanup_action(
        config: MediaforceConfig,
        *,
        transcode_root: str | None = None,
        on_removed: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    with archive_activity(config.paths.db_path, cleanup=True) as available:
        if not available:
            return {"ok": False, "message": "Files are being published. Retry cleanup when they finish.",
                    "removed_count": 0, "removed_size_bytes": 0,
                    "archive_cleanup": archive_cleanup_summary(config, transcode_root=transcode_root)}
        return _clear_archive_cleanup_action(config, transcode_root=transcode_root, on_removed=on_removed)


def _clear_archive_cleanup_action(
        config: MediaforceConfig,
        *,
        transcode_root: str | None = None,
        on_removed: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    archive_root = _archive_root_for_cleanup(config, transcode_root=transcode_root)
    summary = archive_cleanup_summary(config, transcode_root=transcode_root)
    if archive_root is None:
        return {
            "ok": True,
            "message": "No original backups are waiting in the Cleanup folder.",
            "removed_count": 0,
            "removed_size_bytes": 0,
            "archive_cleanup": summary,
        }
    if not archive_root.exists() or summary["file_count"] <= 0:
        return {
            "ok": True,
            "message": "No original backups are waiting in the Cleanup folder.",
            "archive_cleanup": summary,
        }

    protected = recovery_archive_paths(config, archive_root)
    preserved_count = 0
    removed_count = 0
    removed_size_bytes = 0
    for path in archive_root.rglob("*"):
        if not path.is_file():
            continue
        if archive_path_needs_recovery(path, protected):
            preserved_count += 1
            continue
        try:
            removed_size_bytes += path.stat().st_size
        except FileNotFoundError:
            pass
        safe_unlink(path)
        removed_count += 1
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

    return {
        "ok": True,
        "message": f"Deleted {removed_count} original backup{'s' if removed_count != 1 else ''}."
                   + (" Kept originals needed by unpublished files. Inspect and restore those files before cleanup."
                      if preserved_count else ""),
        "preserved_count": preserved_count,
        "removed_count": removed_count,
        "removed_size_bytes": removed_size_bytes,
        "archive_cleanup": archive_cleanup_summary(config, transcode_root=transcode_root),
    }


def _archive_root_for_cleanup(config: MediaforceConfig, *, transcode_root: str | None = None) -> Path | None:
    if transcode_root is not None and transcode_root.strip():
        return Path(settings_archive_root(transcode_root)).expanduser()
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
    return {path for row in rows for path in (
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
