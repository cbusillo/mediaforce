from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import item_events, library_items, staged_artifacts
from mediaforce.encoding.staging import safe_unlink
from mediaforce.library.media_scopes import media_scope_from_prefix, path_matches_scope
FolderGroup = tuple[str, str, str, str]
ORIGINALS_REMOVED_EVENT = "originals_removed_confirmed"
HISTORY_DETAIL_MAX_CHARS = 320


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
    originals_removed_count: int


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
    domain: str
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
        "archive_cleanup": completed_archive_cleanup_summary(archive_root, folders),
        "history": [
            asdict(event)
            for event in list_completed_history_events(
                connection,
                folder_group=folder_group,
                library_types=config.library_type_map,
            )
        ],
    }


def completed_archive_cleanup_summary(archive_root: Path | None, folders: list[CompletedFolder]) -> dict[str, Any]:
    file_count = sum(folder.archived_backup_count for folder in folders)
    total_size_bytes = sum(folder.archived_backup_size_bytes for folder in folders)
    return {
        "archive_root": str(archive_root) if archive_root is not None else "",
        "file_count": file_count,
        "total_size_bytes": total_size_bytes,
        "has_cleanup": file_count > 0,
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
            staged_artifacts.c.library_item_id,
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
    confirmed_item_ids = _confirmed_originals_removed_item_ids(connection)
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
                cleanup_detail="No original backups are waiting in the Cleanup folder.",
                missing_backup_count=0,
                outside_archive_root_count=0,
                originals_removed_count=0,
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
            if int(row["library_item_id"]) in confirmed_item_ids:
                folder.originals_removed_count += 1
                continue
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
        key=lambda completed_folder: (
            completed_folder.archived_backup_count > 0,
            completed_folder.latest_promoted_at or "",
            completed_folder.total_bytes_saved,
            completed_folder.title,
        ),
        reverse=True,
    )


def confirm_originals_removed_action(
        connection: DBClient,
        *,
        folder_group: Callable[[str], FolderGroup | None],
        archive_root: Path | None,
        prefixes: list[str] | None,
        valid_prefixes: set[str] | None = None,
) -> dict[str, Any]:
    normalized_prefixes = _normalized_prefixes(prefixes)
    if not normalized_prefixes:
        raise ValueError("At least one completed folder must be selected.")
    if valid_prefixes is not None:
        unknown_prefixes = sorted(prefix for prefix in normalized_prefixes if prefix not in valid_prefixes)
        if unknown_prefixes:
            raise ValueError(
                "Unknown completed-folder prefix"
                f"{'es' if len(unknown_prefixes) != 1 else ''}: {', '.join(unknown_prefixes)}"
            )
    if archive_root is None:
        return {
            "ok": True,
            "message": "Archive settings are missing, so nothing was marked resolved.",
            "resolved_count": 0,
            "resolved_prefix_count": 0,
        }

    resolved_archive_root = archive_root.resolve()
    confirmed_item_ids = _confirmed_originals_removed_item_ids(connection)
    rows = connection.execute(
        select(
            library_items.c.id,
            library_items.c.rel_path,
            staged_artifacts.c.archived_source_path,
        )
        .select_from(
            staged_artifacts.join(
                library_items,
                staged_artifacts.c.library_item_id == library_items.c.id,
            )
        )
        .where(staged_artifacts.c.promoted_at.is_not(None))
    ).mappings().fetchall()

    now = datetime.now(UTC).isoformat(timespec="seconds")
    resolved_count = 0
    resolved_prefixes: set[str] = set()
    for row in rows:
        rel_path = str(row["rel_path"] or "")
        group = folder_group(rel_path)
        if group is None or group[0] not in normalized_prefixes:
            continue
        library_item_id = int(row["id"])
        if library_item_id in confirmed_item_ids:
            continue
        archived_source_path = _clean_text(row["archived_source_path"])
        if not archived_source_path:
            continue
        archived_path = Path(archived_source_path)
        if not _path_is_within_root(archived_path, resolved_archive_root):
            continue
        if archived_path.is_file():
            continue
        connection.execute(
            item_events.insert().values(
                library_item_id=library_item_id,
                created_at=now,
                event_type=ORIGINALS_REMOVED_EVENT,
                details_json=json.dumps(
                    {
                        "prefix": group[0],
                        "archived_source_path": archived_source_path,
                        "note": "Operator marked already-missing original backups handled after checking the finished files.",
                    }
                ),
            )
        )
        confirmed_item_ids.add(library_item_id)
        resolved_count += 1
        resolved_prefixes.add(group[0])

    if resolved_count > 0:
        connection.commit()
    message = (
        f"Marked {len(resolved_prefixes)} finished folder"
        f"{'s' if len(resolved_prefixes) != 1 else ''} as handled. Nothing was deleted."
        if resolved_count > 0
        else "No missing original backups needed to be marked handled. Nothing was deleted."
    )
    return {
        "ok": True,
        "message": message,
        "resolved_count": resolved_count,
        "resolved_prefix_count": len(resolved_prefixes),
    }


def list_completed_history_events(
        connection: DBClient,
        *,
        folder_group: Callable[[str], FolderGroup | None],
        library_types: dict[str, str] | None = None,
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
                    "encoding_stopped",
                    "validation_completed",
                    "promotion_completed",
                    ORIGINALS_REMOVED_EVENT,
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
        domain = media_scope_from_prefix(
            prefix,
            match="descendants",
            library_types=library_types,
        ).domain
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
                domain=domain,
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
            "message": "No original backups are waiting in the Cleanup folder.",
            "removed_count": 0,
            "removed_size_bytes": 0,
            "removed_prefix_count": 0,
        }

    if not archive_root.exists():
        return {
            "ok": True,
            "message": "No original backups are waiting in the Cleanup folder.",
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
            "message": "No original backups matched the selected finished folders.",
            "removed_count": 0,
            "removed_size_bytes": 0,
            "removed_prefix_count": 0,
        }

    if normalized_prefixes is None:
        message = f"Deleted {removed_count} original backup{'s' if removed_count != 1 else ''}."
    else:
        message = (
            f"Deleted {removed_count} original backup{'s' if removed_count != 1 else ''} "
            f"from {len(removed_prefixes)} finished folder{'s' if len(removed_prefixes) != 1 else ''}."
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
    return any(path_matches_scope(rel_path, prefix) for prefix in prefixes)


def _configured_archive_root(config: MediaforceConfig) -> Path | None:
    try:
        return config.archive_root
    except KeyError:
        return None


def _folder_cleanup_state(folder: CompletedFolder, *, archive_root_configured: bool) -> tuple[str, str]:
    if not archive_root_configured and folder.missing_backup_count > 0:
        return "blocked", "Cleanup folder is not set, so Mediaforce cannot find the original backups."
    if folder.outside_archive_root_count > 0:
        return "blocked", "Some original backups are outside the Cleanup folder, so Mediaforce will not delete them here."
    if folder.missing_backup_count > 0:
        return "unknown", "The original backups are already gone from the Cleanup folder. Nothing will be deleted; you can mark this handled."
    if folder.archived_backup_count > 0:
        return "ready", "Original backups are in the Cleanup folder and can be deleted when you are ready."
    if folder.originals_removed_count > 0:
        return "cleaned", "Already-missing original backups were marked handled after review."
    return "cleaned", "No original backups are waiting in the Cleanup folder."


def _confirmed_originals_removed_item_ids(connection: DBClient) -> set[int]:
    return {
        int(item_id)
        for item_id in connection.execute(
            select(item_events.c.library_item_id).where(
                item_events.c.event_type == ORIGINALS_REMOVED_EVENT
            )
        ).scalars()
    }


def _event_details(raw_details: object) -> dict[str, Any]:
    if isinstance(raw_details, dict):
        return raw_details
    if not isinstance(raw_details, str) or not raw_details.strip():
        return {}
    try:
        parsed = json.loads(raw_details)
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
    if event_type == ORIGINALS_REMOVED_EVENT:
        return (
            "Marked handled",
            "ready",
            "The original backups were already gone. Nothing was deleted; this finished folder was marked handled after review.",
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
        error = _history_detail_summary(details.get("error"))
        if _is_stopped_encode_error(error):
            return "Encoding stopped", "idle", error or "Encode stopped before promotion.", None
        return (
            "Encoding failed",
            "fail",
            error or "Encode failed before promotion.",
            None,
        )
    if event_type == "encoding_stopped":
        return (
            "Encoding stopped",
            "idle",
            _history_detail_summary(details.get("error")) or "Encode stopped before promotion.",
            None,
        )
    if event_type == "validation_completed":
        return (
            "Validation completed",
            "active",
            _validation_detail(details),
            None,
        )
    return "Encoding started", "idle", "Encode worker started processing an item.", None


def _is_stopped_encode_error(value: str) -> bool:
    normalized = value.casefold()
    return any(
        phrase in normalized
        for phrase in (
            "operation was cancelled",
            "stopped and cleaned",
            "stopped by operator",
            "cancelled by operator",
        )
    )


def _path_detail(path: object, fallback: str) -> str:
    cleaned = _clean_text(path)
    return cleaned or fallback


def _history_detail_summary(value: object, max_chars: int = HISTORY_DETAIL_MAX_CHARS) -> str:
    cleaned = _clean_text(value)
    if len(cleaned) <= max_chars:
        return cleaned
    omitted = len(cleaned) - max_chars
    return f"{cleaned[:max_chars].rstrip()} ... [{omitted:,} characters omitted]"


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


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return f"{value}".strip()


def _path_is_within_root(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root)
    except OSError:
        return False
