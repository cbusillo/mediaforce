from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from mediaforce.config import HarnessConfig
from mediaforce.planner import recommend_item
from mediaforce.probe import probe_media
from mediaforce.utils import file_fingerprint, timestamp


VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mp4", ".ts"}


@dataclass(slots=True)
class ScanStats:
    scan_id: str
    discovered: int = 0
    reprobed: int = 0
    unchanged: int = 0
    missing: int = 0
    total_seen: int = 0


def scan_library(connection: sqlite3.Connection, config: HarnessConfig, prefixes: list[str] | None = None, limit: int | None = None) -> ScanStats:
    scan_id = uuid.uuid4().hex
    started_at = timestamp()
    roots_json = json.dumps(sorted(config.source_root_map.keys()))
    normalized_prefixes = sorted({prefix.strip("/") for prefix in prefixes or [] if prefix.strip("/")})
    scope = "prefix" if normalized_prefixes else "full"
    connection.execute(
        "INSERT INTO scan_runs(scan_id, started_at, roots_json, scope, prefixes_json) VALUES (?, ?, ?, ?, ?)",
        (scan_id, started_at, roots_json, scope, json.dumps(normalized_prefixes) if normalized_prefixes else None),
    )

    stats = ScanStats(scan_id=scan_id)
    seen_paths: set[str] = set()

    for root_name, root_path in config.source_root_map.items():
        for file_path in _iter_media_files(root_path, prefixes=normalized_prefixes or None, limit=limit, seen=stats.total_seen):
            source_path = str(file_path)
            seen_paths.add(source_path)
            stats.total_seen += 1

            stat_result = file_path.stat()
            row = connection.execute(
                "SELECT * FROM library_items WHERE source_path = ?",
                (source_path,),
            ).fetchone()

            if row and row["size_bytes"] == stat_result.st_size and row["mtime_ns"] == stat_result.st_mtime_ns:
                connection.execute(
                    """
                    UPDATE library_items
                    SET last_scan_id = ?, last_seen_at = ?, updated_at = ?, status = CASE WHEN status = 'missing' THEN 'discovered' ELSE status END
                    WHERE source_path = ?
                    """,
                    (scan_id, started_at, started_at, source_path),
                )
                stats.unchanged += 1
                continue

            probe = probe_media(file_path)
            fingerprint = file_fingerprint(file_path=file_path, stat_result=stat_result, duration_seconds=probe.duration_seconds)
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

            values = (
                source_path,
                rel_path,
                root_name,
                parent_dir,
                file_path.name,
                file_path.suffix.lower(),
                stat_result.st_size,
                stat_result.st_mtime_ns,
                fingerprint,
                probe.duration_seconds,
                probe.video_codec,
                probe.video_bitrate,
                probe.width,
                probe.height,
                probe.pix_fmt,
                probe.audio_track_count,
                probe.subtitle_track_count,
                probe.english_audio_count,
                probe.english_subtitle_count,
                probe.default_audio_language,
                probe.default_subtitle_language,
                probe.audio_summary_json,
                probe.subtitle_summary_json,
                recommendation.score,
                recommendation.bucket,
                recommendation.reason,
                scan_id,
                started_at,
                started_at,
                started_at,
            )

            if row is None:
                connection.execute(
                    """
                    INSERT INTO library_items (
                        source_path, rel_path, media_root, parent_dir, file_name, container,
                        size_bytes, mtime_ns, fingerprint, duration_seconds, video_codec,
                        video_bitrate, width, height, pix_fmt, audio_track_count,
                        subtitle_track_count, english_audio_count, english_subtitle_count,
                        default_audio_language, default_subtitle_language,
                        audio_summary_json, subtitle_summary_json, priority_score,
                        recommendation, recommendation_reason, last_scan_id, discovered_at,
                        last_seen_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                stats.discovered += 1
            else:
                connection.execute(
                    """
                    UPDATE library_items
                    SET rel_path = ?, media_root = ?, parent_dir = ?, file_name = ?,
                        container = ?, size_bytes = ?, mtime_ns = ?, fingerprint = ?,
                        duration_seconds = ?, video_codec = ?, video_bitrate = ?, width = ?,
                        height = ?, pix_fmt = ?, audio_track_count = ?, subtitle_track_count = ?,
                        english_audio_count = ?, english_subtitle_count = ?,
                        default_audio_language = ?, default_subtitle_language = ?,
                        audio_summary_json = ?, subtitle_summary_json = ?, priority_score = ?,
                        recommendation = ?, recommendation_reason = ?, last_scan_id = ?,
                        last_seen_at = ?, updated_at = ?, status = CASE WHEN status = 'missing' THEN 'discovered' ELSE status END
                    WHERE source_path = ?
                    """,
                    values[1:] + (source_path,),
                )
                stats.reprobed += 1

            if limit is not None and stats.total_seen >= limit:
                break

        if limit is not None and stats.total_seen >= limit:
            break

    full_scan = not normalized_prefixes and limit is None
    if full_scan and seen_paths:
        placeholders = ",".join("?" for _ in seen_paths)
        query = f"""
            UPDATE library_items
            SET status = 'missing', updated_at = ?
            WHERE media_root IN ({','.join('?' for _ in config.source_root_map)})
              AND source_path NOT IN ({placeholders})
              AND status != 'missing'
        """
        params = (started_at, *config.source_root_map.keys(), *seen_paths)
        cursor = connection.execute(query, params)
        stats.missing = cursor.rowcount if cursor.rowcount != -1 else 0

    completed_at = timestamp()
    connection.execute(
        """
        UPDATE scan_runs
        SET completed_at = ?, file_count = ?, reprobed_count = ?, unchanged_count = ?
        WHERE scan_id = ?
        """,
        (completed_at, stats.total_seen, stats.reprobed + stats.discovered, stats.unchanged, scan_id),
    )
    return stats


def _iter_media_files(root_path: Path, prefixes: list[str] | None, limit: int | None, seen: int):
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
