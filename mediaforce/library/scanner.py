import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from sqlalchemy import case
from sqlalchemy import insert
from sqlalchemy import not_
from sqlalchemy import select
from sqlalchemy import update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import scan_runs
from mediaforce.library.planner import recommend_item
from mediaforce.library.probe import probe_media
from mediaforce.core.type_defs import int_value, object_list
from mediaforce.core.utils import file_fingerprint, timestamp

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
    roots_json = json.dumps(sorted(config.source_root_map.keys()))
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

    for root_name, root_path in config.source_root_map.items():
        for file_path in _iter_media_files(root_path, prefixes=normalized_prefixes or None, limit=limit,
                                           seen=stats.total_seen):
            source_path = str(file_path)
            seen_paths.add(source_path)
            stats.total_seen += 1

            stat_result = file_path.stat()
            row = connection.execute(
                select(library_items).where(library_items.c.source_path == source_path)
            ).mappings().fetchone()

            if row and row["size_bytes"] == stat_result.st_size and row["mtime_ns"] == stat_result.st_mtime_ns:
                connection.execute(
                    update(library_items)
                    .where(library_items.c.source_path == source_path)
                    .values(
                        last_scan_id=scan_id,
                        last_seen_at=started_at,
                        updated_at=started_at,
                        status=case((library_items.c.status == "missing", "discovered"), else_=library_items.c.status),
                    )
                )
                stats.unchanged += 1
                pending_writes = _flush_scan_progress(connection, scan_id, stats, pending_writes + 1)
                continue

            probe = probe_media(file_path)
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
                "priority_score": recommendation.score,
                "recommendation": recommendation.bucket,
                "recommendation_reason": recommendation.reason,
                "last_scan_id": scan_id,
                "discovered_at": started_at,
                "last_seen_at": started_at,
                "updated_at": started_at,
            }

            if row is None:
                connection.execute(
                    insert(library_items).values(**values)
                )
                stats.discovered += 1
            else:
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
    if full_scan and seen_paths:
        cursor = connection.execute(
            update(library_items)
            .where(
                library_items.c.media_root.in_(tuple(config.source_root_map.keys())),
                not_(library_items.c.source_path.in_(tuple(seen_paths))),
                library_items.c.status != "missing",
            )
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
            if prefixes and not any(rel_path.startswith(prefix) for prefix in prefixes):
                continue
            yield file_path
            matched += 1
            if limit is not None and seen + matched >= limit:
                return
