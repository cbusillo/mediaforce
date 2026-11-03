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
from mediaforce.library.media_scopes import media_scope_from_prefix
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
    file_name: str = ""
    technical_detail: str = ""


@dataclass(slots=True, frozen=True)
class _HistoryCopy:
    label: str
    tone: str
    detail: str
    size_bytes: int | None = None
    technical_detail: str = ""


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
    confirmed_item_ids = confirmed_originals_removed_item_ids(connection)
    resolved_archive_root = archive_root.resolve() if archive_root is not None else None
    counted_backup_paths: set[Path] = set()
    item_folder_prefixes: dict[int, str] = {}
    for row in rows:
        rel_path = str(row["rel_path"] or "")
        group = folder_group(rel_path)
        if group is None:
            continue
        prefix, title, subtitle, scope_label = group
        item_folder_prefixes[int(row["library_item_id"])] = prefix
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
        counted_backup_paths.add(archived_path.resolve())
        try:
            folder.archived_backup_size_bytes += archived_path.stat().st_size
        except FileNotFoundError:
            folder.archived_backup_count -= 1
            folder.missing_backup_count += 1

    # A later publish retains the previous rollback beside the latest original.
    # Include those retained copies in the same folder's owner-approved cleanup count.
    if archive_root is not None and resolved_archive_root is not None:
        retained_events = connection.execute(
            select(item_events.c.library_item_id, item_events.c.details_json).where(
                item_events.c.event_type.in_(("promotion_completed", "promotion_rollback_retained"))
            )
        ).mappings()
        for event in retained_events:
            retained_path = _clean_text(
                _event_details(event["details_json"]).get("retained_archive_backup_path")
            )
            if not retained_path:
                continue
            backup = Path(retained_path)
            if not backup.is_file() or not _path_is_within_root(
                backup, resolved_archive_root
            ):
                continue
            if backup.resolve() in counted_backup_paths:
                continue
            folder = grouped.get(item_folder_prefixes.get(int(event["library_item_id"]), ""))
            if folder is not None:
                try:
                    size = backup.stat().st_size
                except FileNotFoundError:
                    continue
                folder.archived_backup_count += 1
                folder.archived_backup_size_bytes += size
                counted_backup_paths.add(backup.resolve())

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


def record_deleted_original_backups(
        connection: DBClient,
        removed_paths: list[Path],
        *,
        folder_group: Callable[[str], FolderGroup | None],
) -> int:
    """Record originals Mediaforce itself deleted, so the operator is not asked to confirm them again."""
    removed = {str(path) for path in removed_paths}
    if not removed:
        return 0
    confirmed_item_ids = confirmed_originals_removed_item_ids(connection)
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
    recorded_count = 0
    for row in rows:
        archived_source_path = _clean_text(row["archived_source_path"])
        library_item_id = int(row["id"])
        if archived_source_path not in removed or library_item_id in confirmed_item_ids:
            continue
        group = folder_group(str(row["rel_path"] or ""))
        connection.execute(
            item_events.insert().values(
                library_item_id=library_item_id,
                created_at=now,
                event_type=ORIGINALS_REMOVED_EVENT,
                details_json=json.dumps(
                    {
                        "prefix": group[0] if group is not None else None,
                        "archived_source_path": archived_source_path,
                        "note": "Mediaforce deleted this original backup from the Cleanup folder.",
                    }
                ),
            )
        )
        recorded_count += 1
    if recorded_count > 0:
        connection.commit()
    return recorded_count


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
        copy = _history_event_copy(event_type, details, noun=_history_item_noun(domain))
        history.append(
            CompletedHistoryEvent(
                id=int(row["id"]),
                event_type=event_type,
                label=copy.label,
                tone=copy.tone,
                prefix=prefix,
                title=title,
                subtitle=subtitle,
                scope_label=scope_label,
                domain=domain,
                created_at=_clean_text(row["created_at"]),
                detail=copy.detail,
                size_bytes=copy.size_bytes,
                # A folder-wide event is not about the one file it was recorded on.
                file_name="" if event_type == ORIGINALS_REMOVED_EVENT else _file_name(row["rel_path"]),
                technical_detail=copy.technical_detail,
            )
        )
        if len(history) >= limit:
            break
    return history


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


def confirmed_originals_removed_item_ids(connection: DBClient) -> set[int]:
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


def _history_item_noun(domain: str) -> str:
    if domain == "tv":
        return "episode"
    if domain == "movie":
        return "movie"
    return "file"


def _history_event_copy(event_type: str, details: dict[str, Any], *, noun: str = "file") -> _HistoryCopy:
    """Plain words for one history row; raw paths and computer output stay in technical_detail."""
    if event_type == "promotion_completed":
        promoted_path = _clean_text(details.get("promoted_path"))
        promoted_name = _file_name(promoted_path)
        return _HistoryCopy(
            "Replaced",
            "ready",
            f"Replaced the original with {promoted_name}." if promoted_name else f"Replaced the original {noun}.",
            technical_detail=promoted_path,
        )
    if event_type == ORIGINALS_REMOVED_EVENT:
        return _HistoryCopy(
            "Marked handled",
            "ready",
            "The original backups were already gone. Nothing was deleted; this finished folder was marked handled after review.",
        )
    if event_type == "encoding_completed":
        bytes_saved = _int_or_none(details.get("bytes_saved"))
        return _HistoryCopy(
            "Compressed",
            "ready",
            f"Saved {_size_text(bytes_saved)}." if bytes_saved else "Compressed.",
            bytes_saved,
            f"{bytes_saved:,} bytes saved" if bytes_saved is not None else "",
        )
    if event_type in ("encoding_failed", "encoding_stopped"):
        error = _history_detail_summary(details.get("error"))
        if event_type == "encoding_stopped" or _is_stopped_encode_error(error):
            return _HistoryCopy(
                "Stopped",
                "idle",
                f"Compressing this {noun} stopped before it finished. Nothing was replaced.",
                technical_detail=error,
            )
        return _HistoryCopy(
            "Failed",
            "fail",
            error if _is_plain_sentence(error) else f"Compressing this {noun} did not finish.",
            technical_detail="" if _is_plain_sentence(error) else error,
        )
    if event_type == "validation_completed":
        return _HistoryCopy("Checked", "active", "Checked.", technical_detail=_validation_detail(details))
    return _HistoryCopy("Started", "idle", f"A computer started compressing this {noun}.")


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


def _is_plain_sentence(value: str, max_chars: int = 160) -> bool:
    """Mediaforce's own short failure sentences show as-is; computer output does not."""
    return (
        0 < len(value) <= max_chars
        and "\n" not in value
        and value.endswith(".")
        and not any(marker in value for marker in ("/", "\\", "ffmpeg", "Traceback", "Error:", "[", "{"))
    )


def _file_name(path: object) -> str:
    cleaned = _clean_text(path).rstrip("/")
    return cleaned.rsplit("/", 1)[-1] if cleaned else ""


def _history_detail_summary(value: object, max_chars: int = HISTORY_DETAIL_MAX_CHARS) -> str:
    cleaned = _clean_text(value)
    if len(cleaned) <= max_chars:
        return cleaned
    omitted = len(cleaned) - max_chars
    return f"{cleaned[:max_chars].rstrip()} ... [{omitted:,} characters omitted]"


def _size_text(size_bytes: int | None) -> str:
    """Decimal sizes, rounded the way people read file sizes."""
    value = float(size_bytes or 0)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    index = 0
    while value >= 1000 and index < len(units) - 1:
        value /= 1000
        index += 1
    digits = 0 if value.is_integer() or value >= 100 else 1 if value >= 10 else 2
    number = f"{value:,.{digits}f}"
    if digits:
        number = number.rstrip("0").rstrip(".")
    return f"{number} {units[index]}"


def _validation_detail(details: dict[str, Any]) -> str:
    metric = _clean_text(details.get("metric") or details.get("quality_metric"))
    score = _clean_text(details.get("score") or details.get("quality_score"))
    if metric and score:
        return f"{metric.upper()} {score}"
    return ""


def _int_or_none(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


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
