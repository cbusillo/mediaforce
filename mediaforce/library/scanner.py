import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import and_, case, or_
from sqlalchemy import insert
from sqlalchemy import not_
from sqlalchemy import select
from sqlalchemy import update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import scan_runs
from mediaforce.core.models import ProbeSummary
from mediaforce.encoding.cadence import (
    CADENCE_SCHEMA_VERSION,
    CADENCE_TOOL_NAME,
    CADENCE_TOOL_VERSION,
    unavailable_cadence_summary,
)
from mediaforce.encoding.fingerprint import (
    MEDIA_FINGERPRINT_SCHEMA_VERSION,
    MEDIA_FINGERPRINT_TOOL_NAME,
    MEDIA_FINGERPRINT_TOOL_VERSION,
    unavailable_media_fingerprint_summary,
)
from mediaforce.library.media_scopes import path_matches_scope
from mediaforce.library.planner import recommend_item
from mediaforce.library.probe import probe_media
from mediaforce.core.type_defs import int_value, object_list
from mediaforce.core.utils import content_version_fingerprint, file_fingerprint, timestamp

VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mp4", ".ts"}
SCAN_COMMIT_INTERVAL = 25


@dataclass(slots=True)
class ScanStats:
    scan_id: str
    discovered: int = 0
    reprobed: int = 0
    unchanged: int = 0
    missing: int = 0
    total_seen: int = 0


def scan_library(connection: DBClient, config: MediaforceConfig, prefixes: list[str] | None = None,
                 limit: int | None = None) -> ScanStats:
    scan_id = uuid.uuid4().hex
    started_at = timestamp()
    scan_source_roots = getattr(config, "scan_source_root_map", config.source_root_map)
    roots_json = json.dumps(sorted(scan_source_roots.keys()))
    normalized_prefixes = sorted({prefix.strip("/") for prefix in object_list(prefixes) if str(prefix).strip("/")})
    scope = "prefix" if normalized_prefixes else "full"
    connection.execute(
        insert(scan_runs).values(
            scan_id=scan_id,
            started_at=started_at,
            owner_pid=os.getpid(),
            last_progress_at=started_at,
            roots_json=roots_json,
            scope=scope,
            prefixes_json=json.dumps(normalized_prefixes) if normalized_prefixes else None,
        )
    )
    connection.commit()

    stats = ScanStats(scan_id=scan_id)
    seen_paths: set[str] = set()
    pending_writes = 0

    for root_name, root_path in scan_source_roots.items():
        for file_path in _iter_media_files(root_path, prefixes=normalized_prefixes or None, limit=limit,
                                           seen=stats.total_seen):
            source_path = str(file_path)
            seen_paths.add(source_path)
            stats.total_seen += 1

            stat_result = file_path.stat()
            try:
                content_fingerprint = content_version_fingerprint(file_path, stat_result)
            except OSError:
                content_fingerprint = None
            row = connection.execute(
                select(library_items).where(library_items.c.source_path == source_path)
            ).mappings().fetchone()

            if (
                    row
                    and row["size_bytes"] == stat_result.st_size
                    and row["mtime_ns"] == stat_result.st_mtime_ns
                    and (
                        row.get("content_version_fingerprint") is None
                        or content_fingerprint is None
                        or row["content_version_fingerprint"] == content_fingerprint
                    )
                    and _cadence_summary_present(row.get("cadence_summary_json"))
                    and _media_fingerprint_present(row.get("media_fingerprint_json"))
            ):
                connection.execute(
                    update(library_items)
                    .where(library_items.c.source_path == source_path)
                    .values(
                        last_scan_id=scan_id,
                        last_seen_at=started_at,
                        updated_at=started_at,
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
                stats.unchanged += 1
                pending_writes = _flush_scan_progress(connection, scan_id, stats, pending_writes + 1)
                continue

            try:
                probe = probe_media(file_path)
            except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
                probe = _failed_probe_summary(exc)
            fingerprint = file_fingerprint(file_path=file_path, stat_result=stat_result,
                                           duration_seconds=probe.duration_seconds)
            rel_path = str(file_path.relative_to(root_path.parent))
            parent_dir = str(file_path.parent.relative_to(root_path.parent))
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
                "cadence_summary_json": probe.cadence_summary_json,
                "media_fingerprint_json": probe.media_fingerprint_json,
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
                connection.execute(
                    insert(library_items).values(**values)
                )
                stats.discovered += 1
            else:
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
            pending_writes = _flush_scan_progress(connection, scan_id, stats, pending_writes + 1)

            if limit is not None and stats.total_seen >= limit:
                break

        if limit is not None and stats.total_seen >= limit:
            break

    full_scan = not normalized_prefixes and limit is None
    if full_scan:
        active_roots = tuple(scan_source_roots.keys())
        stale_filter = library_items.c.status != "missing"
        if active_roots and seen_paths:
            stale_filter = and_(
                stale_filter,
                or_(
                    not_(library_items.c.media_root.in_(active_roots)),
                    and_(
                        library_items.c.media_root.in_(active_roots),
                        not_(library_items.c.source_path.in_(tuple(seen_paths))),
                    ),
                ),
            )
        cursor = connection.execute(
            update(library_items)
            .where(stale_filter)
            .values(status="missing", updated_at=started_at)
        )
        stats.missing = int_value(cursor.rowcount) if cursor.rowcount != -1 else 0
        pending_writes = _flush_scan_progress(connection, scan_id, stats, pending_writes + 1)

    _flush_scan_progress(connection, scan_id, stats, pending_writes, force=True)

    completed_at = timestamp()
    connection.execute(
        update(scan_runs)
        .where(scan_runs.c.scan_id == scan_id)
        .values(
            completed_at=completed_at,
            last_progress_at=completed_at,
            file_count=stats.total_seen,
            reprobed_count=stats.reprobed + stats.discovered,
            unchanged_count=stats.unchanged,
        )
    )
    connection.commit()
    return stats


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


def _flush_scan_progress(
        connection: DBClient,
        scan_id: str,
        stats: ScanStats,
        pending_writes: int,
        *,
        force: bool = False,
) -> int:
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
    return 0


def _cadence_summary_present(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return False
    if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != CADENCE_SCHEMA_VERSION
            or payload.get("retry_required") is True
    ):
        return False
    analysis = payload.get("analysis")
    if not isinstance(analysis, dict):
        return False
    tool = analysis.get("tool")
    return (
        isinstance(payload.get("decision"), dict)
        and isinstance(tool, dict)
        and tool.get("name") == CADENCE_TOOL_NAME
        and tool.get("version") == CADENCE_TOOL_VERSION
    )


def _media_fingerprint_present(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return False
    if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != MEDIA_FINGERPRINT_SCHEMA_VERSION
            or payload.get("retry_required") is True
    ):
        return False
    analysis = payload.get("analysis")
    if not isinstance(analysis, dict):
        return False
    tool = analysis.get("tool")
    return (
        isinstance(payload.get("decision"), dict)
        and isinstance(tool, dict)
        and tool.get("name") == MEDIA_FINGERPRINT_TOOL_NAME
        and tool.get("version") == MEDIA_FINGERPRINT_TOOL_VERSION
    )


def _failed_probe_summary(error: Exception) -> ProbeSummary:
    message = str(error).strip() or error.__class__.__name__
    cadence_summary = unavailable_cadence_summary(f"Media probing failed: {message}")
    media_fingerprint = unavailable_media_fingerprint_summary(f"Media probing failed: {message}")
    cadence_summary["retry_required"] = True
    media_fingerprint["retry_required"] = True
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
        attachment_summary_json="",
        cadence_summary_json=json.dumps(cadence_summary, separators=(",", ":"), sort_keys=True),
        media_fingerprint_json=json.dumps(media_fingerprint, separators=(",", ":"), sort_keys=True),
    )


def _iter_media_files(
        root_path: Path, prefixes: list[str] | None, limit: int | None, seen: int
) -> Iterator[Path]:
    matched = 0
    for dirpath, _, filenames in os.walk(root_path):
        for name in sorted(filenames):
            file_path = Path(dirpath, name)
            if file_path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            rel_path = str(file_path.relative_to(root_path.parent))
            if prefixes and not any(path_matches_scope(rel_path, prefix) for prefix in prefixes):
                continue
            yield file_path
            matched += 1
            if limit is not None and seen + matched >= limit:
                return
