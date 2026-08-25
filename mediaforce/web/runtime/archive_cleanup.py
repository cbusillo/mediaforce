from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig
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


def clear_archive_cleanup_action(config: MediaforceConfig, *, transcode_root: str | None = None) -> dict[str, Any]:
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

    removed_count = 0
    removed_size_bytes = 0
    for path in archive_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            removed_size_bytes += path.stat().st_size
        except FileNotFoundError:
            pass
        safe_unlink(path)
        removed_count += 1

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
        "message": f"Deleted {removed_count} original backup{'s' if removed_count != 1 else ''}.",
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
