from collections.abc import Callable
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import item_events, library_items, staged_artifacts
from mediaforce.encoding.staging import safe_unlink
from mediaforce.web.runtime.archive_cleanup import archive_cleanup_summary

FolderGroup = tuple[str, str, str, str]


@dataclass(slots=True)
class CompletedFolder:
    prefix: str
    title: str
    subtitle: str
    scope_label: str
    promoted_item_count: int
    archived_backup_count: int
    archived_backup_size_bytes: int
    total_bytes_saved: int
    latest_promoted_at: str | None
    cleanup_state: str
    cleanup_detail: str
    missing_backup_count: int
    outside_archive_root_count: int


@dataclass(slots=True)
class CompletedHistoryEvent:
    id: int
    event_type: str
    label: str
    tone: str
    prefix: str
    title: str
    subtitle: str
    scope_label: str
    created_at: str
    detail: str
    size_bytes: int | None = None


def completed_page_payload(
        config: MediaforceConfig,
        connection: DBClient,
        *,
        folder_group: Callable[[str], FolderGroup | None],
) -> dict[str, Any]:
    archive_root = _configured_archive_root(config)
    folders = list_completed_folders(
        connection,
        folder_group=folder_group,
        archive_root=archive_root,
    )
    folders_with_backups_count = sum(1 for folder in folders if folder.archived_backup_count > 0)
    return {
        "folders": [asdict(folder) for folder in folders],
        "completed_count": len(folders),
        "folders_with_backups_count": folders_with_backups_count,
        "archive_cleanup": archive_cleanup_summary(config),
        "history": [
            asdict(event)
            for event in list_completed_history_events(connection, folder_group=folder_group)
        ],
    }


def list_completed_folders(
        connection: DBClient,
        *,
        folder_group: Callable[[str], FolderGroup | None],
        archive_root: Path | None = None,
) -> list[CompletedFolder]:
    rows = connection.execute(
        select(
            library_items.c.rel_path,
            staged_artifacts.c.bytes_saved,
            staged_artifacts.c.promoted_at,
            staged_artifacts.c.archived_source_path,
        )
        .select_from(
            staged_artifacts.join(
                library_items,
                staged_artifacts.c.library_item_id == library_items.c.id,
            )
        )
        .where(staged_artifacts.c.promoted_at.is_not(None))
        .order_by(staged_artifacts.c.promoted_at.desc())
    ).mappings().fetchall()

    grouped: dict[str, CompletedFolder] = {}
    resolved_archive_root = archive_root.resolve() if archive_root is not None else None
    for row in rows:
        rel_path = str(row["rel_path"] or "")
        group = folder_group(rel_path)
        if group is None:
            continue
        prefix, title, subtitle, scope_label = group
        folder = grouped.get(prefix)
        if folder is None:
            folder = CompletedFolder(
                prefix=prefix,
                title=title,
                subtitle=subtitle,
                scope_label=scope_label,
                promoted_item_count=0,
                archived_backup_count=0,
                archived_backup_size_bytes=0,
                total_bytes_saved=0,
                latest_promoted_at=None,
                cleanup_state="cleaned",
                cleanup_detail="No archived originals are waiting for cleanup.",
                missing_backup_count=0,
                outside_archive_root_count=0,
            )
            grouped[prefix] = folder
        folder.promoted_item_count += 1
        folder.total_bytes_saved += int(row["bytes_saved"] or 0)
        promoted_at = _clean_text(row["promoted_at"])
        if promoted_at and (folder.latest_promoted_at is None or promoted_at > folder.latest_promoted_at):
            folder.latest_promoted_at = promoted_at
        archived_source_path = _clean_text(row["archived_source_path"])
        if not archived_source_path:
            continue
        archived_path = Path(archived_source_path)
        if resolved_archive_root is None:
            folder.missing_backup_count += 1
            continue
        if not _path_is_within_root(archived_path, resolved_archive_root):
            folder.outside_archive_root_count += 1
            continue
        if not archived_path.is_file():
            folder.missing_backup_count += 1
            continue
        folder.archived_backup_count += 1
        try:
            folder.archived_backup_size_bytes += archived_path.stat().st_size
        except FileNotFoundError:
            folder.archived_backup_count -= 1
            folder.missing_backup_count += 1

    for folder in grouped.values():
        folder.cleanup_state, folder.cleanup_detail = _folder_cleanup_state(
            folder,
            archive_root_configured=archive_root is not None,
        )

    return sorted(
        grouped.values(),
        key=lambda folder: (
            folder.archived_backup_count > 0,
            folder.latest_promoted_at or "",
            folder.total_bytes_saved,
            folder.title,
        ),
        reverse=True,
    )


def list_completed_history_events(
        connection: DBClient,
        *,
        folder_group: Callable[[str], FolderGroup | None],
        limit: int = 80,
) -> list[CompletedHistoryEvent]:
    rows = connection.execute(
        select(
            item_events.c.id,
            item_events.c.created_at,
            item_events.c.event_type,
            item_events.c.details_json,
            library_items.c.rel_path,
        )
        .select_from(
            item_events.join(
                library_items,
                item_events.c.library_item_id == library_items.c.id,
            )
        )
        .where(
            item_events.c.event_type.in_(
                [
                    "encoding_started",
                    "encoding_completed",
                    "encoding_failed",
                    "validation_completed",
                    "promotion_completed",
                ]
            )
        )
        .order_by(item_events.c.created_at.desc(), item_events.c.id.desc())
        .limit(limit * 2)
    ).mappings().fetchall()

    history: list[CompletedHistoryEvent] = []
    for row in rows:
        group = folder_group(str(row["rel_path"] or ""))
        if group is None:
            continue
        prefix, title, subtitle, scope_label = group
        event_type = _clean_text(row["event_type"])
        details = _event_details(row["details_json"])
        label, tone, detail, size_bytes = _history_event_copy(event_type, details)
        history.append(
            CompletedHistoryEvent(
                id=int(row["id"]),
                event_type=event_type,
                label=label,
                tone=tone,
                prefix=prefix,
                title=title,
                subtitle=subtitle,
                scope_label=scope_label,
                created_at=_clean_text(row["created_at"]),
                detail=detail,
                size_bytes=size_bytes,
            )
        )
        if len(history) >= limit:
            break
    return history


def clear_completed_backups_action(
        config: MediaforceConfig,
        *,
        folder_group: Callable[[str], FolderGroup | None],
        prefixes: list[str] | None = None,
        valid_prefixes: set[str] | None = None,
) -> dict[str, Any]:
    archive_root = _configured_archive_root(config)
    normalized_prefixes = _normalized_prefixes(prefixes)
    if prefixes is not None and not normalized_prefixes:
        raise ValueError("At least one completed-folder prefix must be provided for selected cleanup.")
    if normalized_prefixes is not None and valid_prefixes is not None:
        unknown_prefixes = sorted(prefix for prefix in normalized_prefixes if prefix not in valid_prefixes)
        if unknown_prefixes:
            raise ValueError(
                "Unknown completed-folder prefix"
                f"{'es' if len(unknown_prefixes) != 1 else ''}: {', '.join(unknown_prefixes)}"
            )

    if archive_root is None:
        return {
            "ok": True,
            "message": "No archived originals are waiting for cleanup.",
            "removed_count": 0,
            "removed_size_bytes": 0,
            "removed_prefix_count": 0,
        }

    if not archive_root.exists():
        return {
            "ok": True,
            "message": "No archived originals are waiting for cleanup.",
            "removed_count": 0,
            "removed_size_bytes": 0,
            "removed_prefix_count": 0,
        }

    removed_count = 0
    removed_size_bytes = 0
    removed_prefixes: set[str] = set()
    for path in archive_root.rglob("*"):
        if not path.is_file():
            continue
        rel_path = path.relative_to(archive_root).as_posix()
        if normalized_prefixes is not None and not _path_matches_prefixes(rel_path, normalized_prefixes):
            continue
        group = folder_group(rel_path)
        if group is not None:
            removed_prefixes.add(group[0])
        try:
            removed_size_bytes += path.stat().st_size
        except FileNotFoundError:
            pass
        safe_unlink(path)
        removed_count += 1

    _prune_empty_dirs(archive_root)

    if removed_count <= 0:
        return {
            "ok": True,
            "message": "No archived originals matched the selected completed folders.",
            "removed_count": 0,
            "removed_size_bytes": 0,
            "removed_prefix_count": 0,
        }

    if normalized_prefixes is None:
        message = f"Removed {removed_count} archived original{'s' if removed_count != 1 else ''}."
    else:
        message = (
            f"Removed {removed_count} archived original{'s' if removed_count != 1 else ''} "
            f"from {len(removed_prefixes)} completed folder{'s' if len(removed_prefixes) != 1 else ''}."
        )
    return {
        "ok": True,
        "message": message,
        "removed_count": removed_count,
        "removed_size_bytes": removed_size_bytes,
        "removed_prefix_count": len(removed_prefixes),
    }


def _normalized_prefixes(prefixes: list[str] | None) -> set[str] | None:
    if prefixes is None:
        return None
    normalized = set()
    for prefix in prefixes:
        if not isinstance(prefix, str):
            raise ValueError("Completed-folder prefixes must be strings.")
        cleaned = prefix.strip().strip("/")
        if not cleaned:
            raise ValueError("Completed-folder prefixes may not be blank.")
        normalized.add(cleaned)
    return normalized


def _path_matches_prefixes(rel_path: str, prefixes: set[str]) -> bool:
    for prefix in prefixes:
        if rel_path == prefix or rel_path.startswith(f"{prefix}/"):
            return True
    return False


def _configured_archive_root(config: MediaforceConfig) -> Path | None:
    try:
        return config.archive_root
    except KeyError:
        return None


def _folder_cleanup_state(folder: CompletedFolder, *, archive_root_configured: bool) -> tuple[str, str]:
    if not archive_root_configured and folder.missing_backup_count > 0:
        return "blocked", "Archive root is not configured; cleanup cannot verify archived originals."
    if folder.outside_archive_root_count > 0:
        return "blocked", "One or more archived originals are outside the configured archive root."
    if folder.missing_backup_count > 0:
        return "unknown", "One or more archived originals are missing from the archive root."
    if folder.archived_backup_count > 0:
        return "ready", "Archived originals are present and eligible for selected cleanup."
    return "cleaned", "No archived originals are waiting for cleanup."


def _event_details(raw_details: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw_details or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _history_event_copy(event_type: str, details: dict[str, Any]) -> tuple[str, str, str, int | None]:
    if event_type == "promotion_completed":
        return (
            "Promotion completed",
            "ready",
            _path_detail(details.get("promoted_path"), "Encoded file promoted into the source library."),
            None,
        )
    if event_type == "encoding_completed":
        return (
            "Encoding completed",
            "ready",
            _bytes_detail(details.get("bytes_saved"), "Encoded item completed."),
            _int_or_none(details.get("bytes_saved")),
        )
    if event_type == "encoding_failed":
        return (
            "Encoding failed",
            "fail",
            _clean_text(details.get("error")) or "Encode failed before promotion.",
            None,
        )
    if event_type == "validation_completed":
        return (
            "Validation completed",
            "active",
            _validation_detail(details),
            None,
        )
    return ("Encoding started", "idle", "Encode worker started processing an item.", None)


def _path_detail(path: object, fallback: str) -> str:
    cleaned = _clean_text(path)
    return cleaned or fallback


def _bytes_detail(value: object, fallback: str) -> str:
    parsed = _int_or_none(value)
    if parsed is None:
        return fallback
    return f"{parsed} bytes saved by the encoded item."


def _validation_detail(details: dict[str, Any]) -> str:
    metric = _clean_text(details.get("metric") or details.get("quality_metric"))
    score = _clean_text(details.get("score") or details.get("quality_score"))
    if metric and score:
        return f"{metric.upper()} {score}"
    return "Validation completed for encoded output."


def _int_or_none(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _prune_empty_dirs(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if not path.is_dir():
            continue
        try:
            path.rmdir()
        except OSError:
            continue


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _path_is_within_root(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root)
    except OSError:
        return False
