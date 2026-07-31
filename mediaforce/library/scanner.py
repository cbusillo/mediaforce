import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import and_, case
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import not_
from sqlalchemy import select
from sqlalchemy import update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import scan_runs
from mediaforce.core.models import ProbeSummary
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.library.evidence_state import sync_library_item_evidence_states
from mediaforce.library.media_scopes import logical_library_rel_path, path_matches_scope
from mediaforce.library.planner import recommend_item
from mediaforce.library.probe import probe_media
from mediaforce.core.type_defs import float_value, int_value, object_list
from mediaforce.core.utils import content_version_fingerprint, file_fingerprint, timestamp

VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mp4", ".ts"}
SCAN_COMMIT_INTERVAL = 25
MASS_DISAPPEARANCE_MIN_ITEMS = 25
MASS_DISAPPEARANCE_MAX_REMAINING_PERCENT = 20


@dataclass(frozen=True, slots=True)
class ScanWarning:
    code: str
    library_key: str
    label: str
    message: str
    preserved_item_count: int


@dataclass(slots=True)
class _RootEnumeration:
    issue_code: str | None = None
    matched_count: int = 0


@dataclass(slots=True)
class ScanStats:
    scan_id: str
    discovered: int = 0
    reprobed: int = 0
    unchanged: int = 0
    missing: int = 0
    total_seen: int = 0
    warnings: list[ScanWarning] = field(default_factory=list)


def scan_library(
        connection: DBClient,
        config: MediaforceConfig,
        prefixes: list[str] | None = None,
        limit: int | None = None,
        *,
        process_controller: ManagedProcessController | None = None,
) -> ScanStats:
    _throw_if_scan_cancelled(process_controller)
    scan_id = uuid.uuid4().hex
    started_at = timestamp()
    normalized_prefixes = sorted({prefix.strip("/") for prefix in object_list(prefixes) if str(prefix).strip("/")})
    scan_source_roots = _scan_roots_for_prefixes(
        getattr(config, "scan_source_root_map", config.source_root_map),
        normalized_prefixes,
    )
    roots_json = json.dumps(sorted(scan_source_roots.keys()))
    scope = "limited" if limit is not None else "prefix" if normalized_prefixes else "full"
    connection.execute(
        insert(scan_runs).values(
            scan_id=scan_id,
            started_at=started_at,
            owner_pid=os.getpid(),
            last_progress_at=started_at,
            roots_json=roots_json,
            scope=scope,
            prefixes_json=json.dumps(normalized_prefixes) if normalized_prefixes else None,
            status="running",
            error=None,
        )
    )
    connection.commit()
    stats = ScanStats(scan_id=scan_id)
    try:
        _throw_if_scan_cancelled(process_controller)
        return _execute_scan_library(
            connection,
            config,
            scan_id=scan_id,
            started_at=started_at,
            normalized_prefixes=normalized_prefixes,
            scan_source_roots=scan_source_roots,
            limit=limit,
            stats=stats,
            process_controller=process_controller,
        )
    except BaseException as exc:
        try:
            mark_scan_run_failed(connection, scan_id, stats=stats, error=exc)
        except BaseException as cleanup_error:
            exc.add_note(
                "Scan run failure state update also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        raise


def _execute_scan_library(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        scan_id: str,
        started_at: str,
        normalized_prefixes: list[str],
        scan_source_roots: dict[str, Path],
        limit: int | None,
        stats: ScanStats,
        process_controller: ManagedProcessController | None,
) -> ScanStats:
    seen_paths_by_root: dict[str, set[str]] = {}
    reconcilable_roots: set[str] = set()
    pending_writes = 0
    full_scan = not normalized_prefixes and limit is None

    for root_name, root_path in scan_source_roots.items():
        _throw_if_scan_cancelled(process_controller)
        previous_item_count = _active_item_count(connection, root_name) if full_scan else 0
        _throw_if_scan_cancelled(process_controller)
        enumeration = _RootEnumeration()
        root_seen_paths = seen_paths_by_root.setdefault(root_name, set())
        for file_path in _iter_media_files(
                root_name,
                root_path,
                prefixes=normalized_prefixes or None,
                limit=limit,
                seen=stats.total_seen,
                enumeration=enumeration,
                process_controller=process_controller,
        ):
            _throw_if_scan_cancelled(process_controller)
            source_path = str(file_path)
            logical_path = logical_library_rel_path(root_name, root_path, file_path)
            rel_path = logical_path.as_posix()
            parent_dir = logical_path.parent.as_posix()
            try:
                stat_result = file_path.stat()
            except OSError:
                enumeration.issue_code = "source_scan_incomplete"
                continue
            _throw_if_scan_cancelled(process_controller)
            root_seen_paths.add(source_path)
            stats.total_seen += 1
            try:
                content_fingerprint = content_version_fingerprint(file_path, stat_result)
            except OSError:
                content_fingerprint = None
            row = connection.execute(
                select(library_items).where(library_items.c.source_path == source_path)
            ).mappings().fetchone()
            _throw_if_scan_cancelled(process_controller)

            if (
                    row
                    and _source_content_unchanged(
                        row,
                        size_bytes=stat_result.st_size,
                        mtime_ns=stat_result.st_mtime_ns,
                        content_fingerprint=content_fingerprint,
                    )
            ):
                connection.execute(
                    update(library_items)
                    .where(library_items.c.source_path == source_path)
                    .values(
                        last_scan_id=scan_id,
                        last_seen_at=started_at,
                        updated_at=started_at,
                        rel_path=rel_path,
                        media_root=root_name,
                        parent_dir=parent_dir,
                        size_bytes=stat_result.st_size,
                        mtime_ns=stat_result.st_mtime_ns,
                        fingerprint=file_fingerprint(
                            file_path=file_path,
                            stat_result=stat_result,
                            duration_seconds=float_value(row.get("duration_seconds")),
                        ),
                        content_version_changed_at=case(
                            (library_items.c.status == "missing", started_at),
                            else_=library_items.c.content_version_changed_at,
                        ),
                        content_version_fingerprint=(
                            content_fingerprint or row.get("content_version_fingerprint")
                        ),
                        status=case((library_items.c.status == "missing", "discovered"), else_=library_items.c.status),
                    )
                )
                if not row.get("content_version_fingerprint") and content_fingerprint:
                    sync_library_item_evidence_states(
                        connection,
                        {
                            **dict(row),
                            "fingerprint": file_fingerprint(
                                file_path=file_path,
                                stat_result=stat_result,
                                duration_seconds=float_value(row.get("duration_seconds")),
                            ),
                            "content_version_fingerprint": content_fingerprint,
                        },
                        preserve_source_identity=False,
                        updated_at=started_at,
                    )
                stats.unchanged += 1
                pending_writes = _flush_scan_progress(
                    connection,
                    scan_id,
                    stats,
                    pending_writes + 1,
                    process_controller=process_controller,
                )
                continue

            pending_writes = _commit_scan_progress_before_probe(
                connection,
                scan_id,
                stats,
                pending_writes,
                process_controller=process_controller,
            )
            try:
                probe = (
                    probe_media(file_path)
                    if process_controller is None
                    else probe_media(
                        file_path,
                        process_controller=process_controller,
                    )
                )
            except ProcessCancelledError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
                probe = _failed_probe_summary(exc)
            _throw_if_scan_cancelled(process_controller)
            fingerprint = file_fingerprint(file_path=file_path, stat_result=stat_result,
                                           duration_seconds=probe.duration_seconds)
            recommendation = recommend_item(
                {
                    "rel_path": rel_path,
                    "size_bytes": stat_result.st_size,
                    "video_codec": probe.video_codec,
                    "audio_track_count": probe.audio_track_count,
                    "english_audio_count": probe.english_audio_count,
                    "english_subtitle_count": probe.english_subtitle_count,
                },
                config,
            )

            values = {
                "source_path": source_path,
                "rel_path": rel_path,
                "media_root": root_name,
                "parent_dir": parent_dir,
                "file_name": file_path.name,
                "container": file_path.suffix.lower(),
                "size_bytes": stat_result.st_size,
                "mtime_ns": stat_result.st_mtime_ns,
                "fingerprint": fingerprint,
                "duration_seconds": probe.duration_seconds,
                "video_codec": probe.video_codec,
                "video_bitrate": probe.video_bitrate,
                "width": probe.width,
                "height": probe.height,
                "pix_fmt": probe.pix_fmt,
                "audio_track_count": probe.audio_track_count,
                "subtitle_track_count": probe.subtitle_track_count,
                "english_audio_count": probe.english_audio_count,
                "english_subtitle_count": probe.english_subtitle_count,
                "default_audio_language": probe.default_audio_language,
                "default_subtitle_language": probe.default_subtitle_language,
                "audio_summary_json": probe.audio_summary_json,
                "subtitle_summary_json": probe.subtitle_summary_json,
                "attachment_summary_json": probe.attachment_summary_json,
                "priority_score": recommendation.score,
                "recommendation": recommendation.bucket,
                "recommendation_reason": recommendation.reason,
                "last_scan_id": scan_id,
                "discovered_at": started_at,
                "content_version_changed_at": started_at,
                "content_version_fingerprint": content_fingerprint,
                "last_seen_at": started_at,
                "updated_at": started_at,
            }

            if row is None:
                insert_result = connection.execute(
                    insert(library_items).values(**values)
                )
                library_item_id = int(insert_result.inserted_primary_key[0])
                stats.discovered += 1
            else:
                library_item_id = int(row["id"])
                previous_content_fingerprint = str(row.get("content_version_fingerprint") or "") or None
                content_changed = _content_version_changed(
                    row,
                    size_bytes=stat_result.st_size,
                    content_fingerprint=content_fingerprint,
                )
                values["content_version_changed_at"] = (
                    started_at if content_changed else row.get("content_version_changed_at")
                )
                values["content_version_fingerprint"] = content_fingerprint or previous_content_fingerprint
                connection.execute(
                    update(library_items)
                    .where(library_items.c.source_path == source_path)
                    .values(
                        **{key: value for key, value in values.items() if key not in {"source_path", "discovered_at"}},
                        status=case((library_items.c.status == "missing", "discovered"), else_=library_items.c.status),
                    )
                )
                stats.reprobed += 1
            item = connection.execute(
                select(library_items).where(library_items.c.id == library_item_id)
            ).mappings().one()
            sync_library_item_evidence_states(
                connection,
                item,
                preserve_source_identity=row is not None,
                preserve_policy_identity=row is not None,
                reset_attempt_state=True,
                updated_at=started_at,
            )
            pending_writes = _flush_scan_progress(
                connection,
                scan_id,
                stats,
                pending_writes + 1,
                process_controller=process_controller,
            )

            if limit is not None and stats.total_seen >= limit:
                break

        if enumeration.issue_code:
            stats.warnings.append(
                _scan_warning(config, root_name, enumeration.issue_code, previous_item_count)
            )
        elif full_scan and previous_item_count > 0 and enumeration.matched_count == 0:
            stats.warnings.append(
                _scan_warning(config, root_name, "source_unexpectedly_empty", previous_item_count)
            )
        elif full_scan and _mass_disappearance_is_suspicious(previous_item_count, enumeration.matched_count):
            stats.warnings.append(
                _scan_warning(
                    config,
                    root_name,
                    "source_mass_disappearance",
                    max(previous_item_count - enumeration.matched_count, 0),
                )
            )
        else:
            reconcilable_roots.add(root_name)

        if limit is not None and stats.total_seen >= limit:
            break
        _throw_if_scan_cancelled(process_controller)

    if full_scan:
        _throw_if_scan_cancelled(process_controller)
        stats.missing = _reconcile_missing_items(
            connection,
            configured_roots=set(scan_source_roots),
            reconcilable_roots=reconcilable_roots,
            seen_paths_by_root=seen_paths_by_root,
            updated_at=started_at,
            process_controller=process_controller,
        )
        pending_writes = _flush_scan_progress(
            connection,
            scan_id,
            stats,
            pending_writes + 1,
            process_controller=process_controller,
        )

    _flush_scan_progress(
        connection,
        scan_id,
        stats,
        pending_writes,
        force=True,
        process_controller=process_controller,
    )

    _throw_if_scan_cancelled(process_controller)
    completed_at = timestamp()
    connection.execute(
        update(scan_runs)
        .where(scan_runs.c.scan_id == scan_id)
        .values(
            completed_at=completed_at,
            last_progress_at=completed_at,
            status="completed",
            error=None,
            file_count=stats.total_seen,
            reprobed_count=stats.reprobed + stats.discovered,
            unchanged_count=stats.unchanged,
        )
    )
    connection.commit()
    return stats


def mark_scan_run_failed(
        connection: DBClient,
        scan_id: str,
        *,
        stats: ScanStats | None,
        error: BaseException | str,
) -> None:
    rollback = getattr(connection, "rollback", None)
    in_transaction = getattr(connection, "in_transaction", None)
    if callable(rollback) and callable(in_transaction) and in_transaction():
        rollback()
    failed_at = timestamp()
    values: dict[str, Any] = {
        "completed_at": failed_at,
        "last_progress_at": failed_at,
        "status": "failed",
        "error": _scan_run_error_message(error),
    }
    if stats is not None:
        values.update(
            file_count=stats.total_seen,
            reprobed_count=stats.reprobed + stats.discovered,
            unchanged_count=stats.unchanged,
        )
    connection.execute(
        update(scan_runs)
        .where(scan_runs.c.scan_id == scan_id)
        .values(**values)
    )
    connection.commit()


def _scan_run_error_message(error: BaseException | str) -> str:
    if isinstance(error, str):
        return error
    detail = str(error).strip()
    if detail:
        return f"{type(error).__name__}: {detail}"
    return type(error).__name__


def _commit_scan_progress_before_probe(
        connection: DBClient,
        scan_id: str,
        stats: ScanStats,
        pending_writes: int,
        *,
        process_controller: ManagedProcessController | None = None,
) -> int:
    _throw_if_scan_cancelled(process_controller)
    if pending_writes:
        return _flush_scan_progress(
            connection,
            scan_id,
            stats,
            pending_writes,
            force=True,
            process_controller=process_controller,
        )
    connection.commit()
    _throw_if_scan_cancelled(process_controller)
    return 0


def _content_version_changed(
        row: Any,
        *,
        size_bytes: int,
        content_fingerprint: str | None,
) -> bool:
    if str(row["status"] or "") == "missing":
        return True
    if int(row["size_bytes"] or 0) != size_bytes:
        return True
    previous_content_fingerprint = str(row.get("content_version_fingerprint") or "") or None
    return bool(
        previous_content_fingerprint
        and content_fingerprint
        and previous_content_fingerprint != content_fingerprint
    )


def _source_content_unchanged(
        row: Any,
        *,
        size_bytes: int,
        mtime_ns: int,
        content_fingerprint: str | None,
) -> bool:
    if int(row["size_bytes"] or 0) != size_bytes:
        return False
    previous_content_fingerprint = str(row.get("content_version_fingerprint") or "") or None
    if previous_content_fingerprint and content_fingerprint:
        return previous_content_fingerprint == content_fingerprint
    return int(row["mtime_ns"] or 0) == mtime_ns


def _flush_scan_progress(
        connection: DBClient,
        scan_id: str,
        stats: ScanStats,
        pending_writes: int,
        *,
        force: bool = False,
        process_controller: ManagedProcessController | None = None,
) -> int:
    _throw_if_scan_cancelled(process_controller)
    if not force and pending_writes < SCAN_COMMIT_INTERVAL:
        return pending_writes
    connection.execute(
        update(scan_runs)
        .where(scan_runs.c.scan_id == scan_id)
        .values(
            last_progress_at=timestamp(),
            file_count=stats.total_seen,
            reprobed_count=stats.reprobed + stats.discovered,
            unchanged_count=stats.unchanged,
        )
    )
    connection.commit()
    _throw_if_scan_cancelled(process_controller)
    return 0


def _failed_probe_summary(_error: Exception) -> ProbeSummary:
    return ProbeSummary(
        duration_seconds=None,
        video_codec=None,
        video_bitrate=None,
        width=None,
        height=None,
        pix_fmt=None,
        audio_track_count=0,
        subtitle_track_count=0,
        english_audio_count=0,
        english_subtitle_count=0,
        default_audio_language=None,
        default_subtitle_language=None,
        audio_summary_json="[]",
        subtitle_summary_json="[]",
        attachment_summary_json="[]",
        cadence_summary_json=None,
        media_fingerprint_json=None,
    )


def _active_item_count(connection: DBClient, root_name: str) -> int:
    return int(
        connection.execute(
            select(func.count())
            .select_from(library_items)
            .where(
                library_items.c.media_root == root_name,
                library_items.c.status != "missing",
            )
        ).scalar_one()
    )


def _scan_roots_for_prefixes(
        scan_source_roots: dict[str, Path],
        normalized_prefixes: list[str],
) -> dict[str, Path]:
    if not normalized_prefixes:
        return scan_source_roots
    return {
        root_name: root_path
        for root_name, root_path in scan_source_roots.items()
        if any(
            prefix == root_name or prefix.startswith(f"{root_name}/")
            for prefix in normalized_prefixes
        )
    }


def _mass_disappearance_is_suspicious(previous_item_count: int, matched_count: int) -> bool:
    return (
        previous_item_count >= MASS_DISAPPEARANCE_MIN_ITEMS
        and matched_count > 0
        and matched_count * 100 <= previous_item_count * MASS_DISAPPEARANCE_MAX_REMAINING_PERCENT
    )


def _scan_warning(
        config: MediaforceConfig,
        root_name: str,
        code: str,
        preserved_item_count: int,
) -> ScanWarning:
    definitions = getattr(config, "library_definition_map", {})
    definition = definitions.get(root_name, {}) if isinstance(definitions, dict) else {}
    label = str(definition.get("label") or root_name.replace("_", " ").replace("-", " ").title())
    if code == "source_unexpectedly_empty":
        message = (
            f"{label} returned no media. Cached catalog state was preserved; disable or remove the library "
            "in Settings to accept the empty state."
        )
    elif code == "source_mass_disappearance":
        message = (
            f"{label} returned far fewer media items than expected. Cached catalog state was preserved so "
            "surviving workflow status remains unchanged."
        )
    elif code == "source_scan_incomplete":
        message = f"{label} could not be fully read. Cached catalog state was preserved."
    else:
        message = f"{label} is unavailable. Cached catalog state was preserved."
    return ScanWarning(
        code=code,
        library_key=root_name,
        label=label,
        message=message,
        preserved_item_count=preserved_item_count,
    )


def _reconcile_missing_items(
        connection: DBClient,
        *,
        configured_roots: set[str],
        reconcilable_roots: set[str],
        seen_paths_by_root: dict[str, set[str]],
        updated_at: str,
        process_controller: ManagedProcessController | None = None,
) -> int:
    _throw_if_scan_cancelled(process_controller)
    missing_count = 0
    removed_root_filter = library_items.c.status != "missing"
    if configured_roots:
        removed_root_filter = and_(
            removed_root_filter,
            not_(library_items.c.media_root.in_(tuple(sorted(configured_roots)))),
        )
    missing_count += _mark_items_missing(
        connection,
        removed_root_filter,
        updated_at,
        process_controller=process_controller,
    )

    for root_name in sorted(reconcilable_roots):
        _throw_if_scan_cancelled(process_controller)
        stale_filter = and_(
            library_items.c.status != "missing",
            library_items.c.media_root == root_name,
        )
        seen_paths = seen_paths_by_root.get(root_name, set())
        if not seen_paths:
            continue
        stale_filter = and_(
            stale_filter,
            not_(library_items.c.source_path.in_(tuple(sorted(seen_paths)))),
        )
        missing_count += _mark_items_missing(
            connection,
            stale_filter,
            updated_at,
            process_controller=process_controller,
        )
    return missing_count


def _mark_items_missing(
        connection: DBClient,
        stale_filter: Any,
        updated_at: str,
        *,
        process_controller: ManagedProcessController | None = None,
) -> int:
    _throw_if_scan_cancelled(process_controller)
    cursor = connection.execute(
        update(library_items)
        .where(stale_filter)
        .values(status="missing", updated_at=updated_at)
    )
    _throw_if_scan_cancelled(process_controller)
    return int_value(cursor.rowcount) if cursor.rowcount != -1 else 0


def _iter_media_files(
        root_name: str,
        root_path: Path,
        prefixes: list[str] | None,
        limit: int | None,
        seen: int,
        enumeration: _RootEnumeration | None = None,
        process_controller: ManagedProcessController | None = None,
) -> Iterator[Path]:
    _throw_if_scan_cancelled(process_controller)
    scan_state = enumeration or _RootEnumeration()
    if limit is not None and seen >= limit:
        return
    try:
        if not root_path.is_dir():
            scan_state.issue_code = "source_unavailable"
            return
    except OSError:
        scan_state.issue_code = "source_unavailable"
        return

    matched = 0

    def _record_error(_error: OSError) -> None:
        scan_state.issue_code = "source_scan_incomplete"

    try:
        for dirpath, _, filenames in os.walk(root_path, onerror=_record_error):
            _throw_if_scan_cancelled(process_controller)
            for name in sorted(filenames):
                _throw_if_scan_cancelled(process_controller)
                file_path = Path(dirpath, name)
                if file_path.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                rel_path = logical_library_rel_path(root_name, root_path, file_path).as_posix()
                if prefixes and not any(path_matches_scope(rel_path, prefix) for prefix in prefixes):
                    continue
                matched += 1
                scan_state.matched_count += 1
                yield file_path
                if limit is not None and seen + matched >= limit:
                    return
    except OSError:
        scan_state.issue_code = "source_scan_incomplete"


def _throw_if_scan_cancelled(
        process_controller: ManagedProcessController | None,
) -> None:
    if process_controller is not None:
        process_controller.throw_if_cancelled()
