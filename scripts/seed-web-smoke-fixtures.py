#!/usr/bin/env python3

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from mediaforce.core.config import load_config
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import (
    calibration_jobs,
    item_events,
    library_items,
    staged_artifacts,
)

FIXTURE_SCAN_ID = "web-smoke-fixtures"
FOLDER_PREFIX = "tv/Example Show/Season 1"
SAMPLING_PREFIX = "tv/Sampling Show/Season 1"
RETRY_PREFIX = "tv/Retry Show/Season 1"
COMPLETED_PREFIX = "movies/Archive Ready"
BLOCKED_COMPLETED_PREFIX = "movies/Blocked Cleanup"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _resolve_under_project(project_root: Path, value: Path) -> Path:
    return value if value.is_absolute() else (project_root / value).resolve()


def _library_item(
    *,
    project_root: Path,
    media_root: str,
    rel_path: str,
    size_bytes: int,
    status: str,
    video_codec: str,
    priority_score: int,
    recommendation: str,
    recommendation_reason: str,
) -> dict[str, Any]:
    timestamp = _now()
    source_path = _resolve_under_project(
        project_root, Path("scratch/web-smoke/source") / rel_path
    )
    return {
        "source_path": str(source_path),
        "rel_path": rel_path,
        "media_root": media_root,
        "parent_dir": str(Path(rel_path).parent),
        "file_name": Path(rel_path).name,
        "container": Path(rel_path).suffix.lstrip(".") or "mkv",
        "size_bytes": size_bytes,
        "mtime_ns": 1_700_000_000_000_000_000,
        "fingerprint": f"fixture:{rel_path}",
        "duration_seconds": 3_600.0,
        "video_codec": video_codec,
        "video_bitrate": 8_000_000 if video_codec == "h264" else 4_500_000,
        "width": 1920,
        "height": 1080,
        "pix_fmt": "yuv420p",
        "audio_track_count": 1,
        "subtitle_track_count": 1,
        "english_audio_count": 1,
        "english_subtitle_count": 1,
        "default_audio_language": "eng",
        "default_subtitle_language": "eng",
        "audio_summary_json": json.dumps(
            [{"index": 1, "codec_name": "dts", "channels": 6, "language": "eng"}]
        ),
        "subtitle_summary_json": json.dumps(
            [
                {
                    "index": 2,
                    "codec_name": "subrip",
                    "language": "eng",
                    "forced": False,
                    "default": False,
                }
            ]
        ),
        "status": status,
        "priority_score": priority_score,
        "recommendation": recommendation,
        "recommendation_reason": recommendation_reason,
        "last_scan_id": FIXTURE_SCAN_ID,
        "discovered_at": timestamp,
        "last_seen_at": timestamp,
        "updated_at": timestamp,
    }


def _job(
    *,
    job_id: str,
    prefix: str,
    status: str,
    sample_item: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    timestamp = _now()
    return {
        "job_id": job_id,
        "prefix": prefix,
        "status": status,
        "lane": "sample",
        "action": "ai_tune",
        "host_json": json.dumps({"key": "web-smoke", "label": "Smoke fixture"}),
        "notes": "Seeded browser QA fixture.",
        "policy_json": json.dumps(sample_item["resolved_policy"], sort_keys=True),
        "sample_item_json": json.dumps(sample_item, sort_keys=True),
        "error": error,
        "created_at": timestamp,
        "started_at": timestamp if status in {"running", "failed", "stopped"} else None,
        "finished_at": timestamp
        if status in {"failed", "stopped", "completed"}
        else None,
        "updated_at": timestamp,
    }


def seed(config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    project_root = config.paths.project_root
    archive_root = _resolve_under_project(project_root, config.archive_root)
    staging_root = _resolve_under_project(project_root, config.staging_root)
    archive_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    archived_source = archive_root / "movies" / "Archive Ready" / "Feature.mkv"
    archived_source.parent.mkdir(parents=True, exist_ok=True)
    archived_source.write_bytes(b"mediaforce smoke archived original\n")

    with open_db(config.paths.db_path) as connection:
        connection.execute(
            calibration_jobs.delete().where(
                calibration_jobs.c.job_id.like("web-smoke-%")
            )
        )
        fixture_ids = [
            int(row["id"])
            for row in connection.execute(
                library_items.select()
                .with_only_columns(library_items.c.id)
                .where(library_items.c.last_scan_id == FIXTURE_SCAN_ID)
            )
            .mappings()
            .fetchall()
        ]
        if fixture_ids:
            connection.execute(
                item_events.delete().where(
                    item_events.c.library_item_id.in_(fixture_ids)
                )
            )
            connection.execute(
                staged_artifacts.delete().where(
                    staged_artifacts.c.library_item_id.in_(fixture_ids)
                )
            )
            connection.execute(
                library_items.delete().where(library_items.c.id.in_(fixture_ids))
            )

        rows = [
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Example Show/Season 1/Episode 01.mkv",
                size_bytes=8 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=92,
                recommendation="priority_encode",
                recommendation_reason="Fixture high-priority H.264 episode for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Example Show/Season 1/Episode 02.mkv",
                size_bytes=6 * 1024**3,
                status="planned",
                video_codec="h264",
                priority_score=76,
                recommendation="priority_encode",
                recommendation_reason="Fixture second episode keeps the queue non-empty and grouped.",
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Blocked Queue/Feature.mkv",
                size_bytes=5 * 1024**3,
                status="discovered",
                video_codec="hevc",
                priority_score=44,
                recommendation="review_encode",
                recommendation_reason="Fixture lower-priority movie for list density.",
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Archive Ready/Feature.mkv",
                size_bytes=7 * 1024**3,
                status="promoted",
                video_codec="h264",
                priority_score=10,
                recommendation="priority_encode",
                recommendation_reason="Fixture promoted item for completed cleanup state.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Sampling Show/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=88,
                recommendation="priority_encode",
                recommendation_reason="Fixture queued sample state for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Retry Show/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=86,
                recommendation="priority_encode",
                recommendation_reason="Fixture retryable sample state for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Blocked Cleanup/Feature.mkv",
                size_bytes=5 * 1024**3,
                status="promoted",
                video_codec="h264",
                priority_score=9,
                recommendation="priority_encode",
                recommendation_reason="Fixture blocked completed cleanup state.",
            ),
        ]
        inserted_ids: list[int] = []
        for row in rows:
            result = connection.execute(library_items.insert().values(**row))
            inserted_ids.append(int(result.inserted_primary_key[0]))

        completed_id = inserted_ids[3]
        blocked_completed_id = inserted_ids[-1]
        timestamp = _now()
        for item_id, row, archived_path in (
            (completed_id, rows[3], str(archived_source)),
            (
                blocked_completed_id,
                rows[-1],
                str(
                    Path(
                        "/tmp/mediaforce-web-smoke-outside/Blocked Cleanup/Feature.mkv"
                    )
                ),
            ),
        ):
            connection.execute(
                staged_artifacts.insert().values(
                    library_item_id=item_id,
                    manifest_run_id="web-smoke-run",
                    manifest_path=str(
                        project_root / "state/web-smoke/runs/web-smoke-run.json"
                    ),
                    item_index=0,
                    encode_origin="web-smoke",
                    source_path=row["source_path"],
                    source_rel_path=row["rel_path"],
                    source_size_bytes=row["size_bytes"],
                    source_duration_seconds=row["duration_seconds"],
                    source_video_codec=row["video_codec"],
                    source_fingerprint=row["fingerprint"],
                    staging_path=str(staging_root / row["rel_path"]),
                    staging_size_bytes=3 * 1024**3,
                    bytes_saved=4 * 1024**3,
                    size_ratio=0.43,
                    chosen_crf=28,
                    quality_metric="vmaf",
                    quality_target=95.0,
                    quality_score=96.1,
                    audio_summary_json=row["audio_summary_json"],
                    subtitle_summary_json=row["subtitle_summary_json"],
                    validation_json=json.dumps({"ok": True, "source": "web-smoke"}),
                    staged_at=timestamp,
                    validated_at=timestamp,
                    promoted_at=timestamp,
                    promoted_path=str(
                        project_root
                        / "scratch/web-smoke/source"
                        / Path(row["rel_path"]).with_suffix(".av1.mkv")
                    ),
                    archived_source_path=archived_path,
                    updated_at=timestamp,
                )
            )
            connection.execute(
                item_events.insert().values(
                    library_item_id=item_id,
                    created_at=timestamp,
                    event_type="promotion_completed",
                    details_json=json.dumps(
                        {
                            "prefix": COMPLETED_PREFIX
                            if item_id == completed_id
                            else BLOCKED_COMPLETED_PREFIX,
                            "bytes_saved": 4 * 1024**3,
                            "note": "Fixture promoted item for browser QA.",
                        }
                    ),
                )
            )

        policy = config.resolve_policy(rows[0]["rel_path"])
        for item_id, row, job in (
            (
                inserted_ids[4],
                rows[4],
                _job(
                    job_id="web-smoke-sampling",
                    prefix=SAMPLING_PREFIX,
                    status="queued",
                    sample_item={
                        "library_item_id": inserted_ids[4],
                        **rows[4],
                        "source_size_bytes": rows[4]["size_bytes"],
                        "resolved_policy": policy,
                    },
                ),
            ),
            (
                inserted_ids[5],
                rows[5],
                _job(
                    job_id="web-smoke-retry",
                    prefix=RETRY_PREFIX,
                    status="failed",
                    sample_item={
                        "library_item_id": inserted_ids[5],
                        **rows[5],
                        "source_size_bytes": rows[5]["size_bytes"],
                        "resolved_policy": policy,
                    },
                    error="Fixture sample failed so retry state is inspectable.",
                ),
            ),
        ):
            _ = item_id, row
            connection.execute(calibration_jobs.insert().values(**job))

    return {
        "folderPrefix": FOLDER_PREFIX,
        "folderRoute": "/folders/tv/Example%20Show/Season%201",
        "folderRoutes": [
            {
                "label": "Folder Studio waiting fixture",
                "route": "/folders/tv/Example%20Show/Season%201",
                "marker": "Example Show",
            },
            {
                "label": "Folder Studio sampling fixture",
                "route": "/folders/tv/Sampling%20Show/Season%201",
                "marker": "Sampling",
            },
            {
                "label": "Folder Studio retry fixture",
                "route": "/folders/tv/Retry%20Show/Season%201",
                "marker": "Retry sample",
            },
            {
                "label": "Completed cleanup fixture",
                "route": "/completed",
                "marker": "Blocked Cleanup",
            },
        ],
        "completedPrefix": COMPLETED_PREFIX,
        "libraryItems": len(rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed deterministic web smoke fixture state."
    )
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(seed(args.config), sort_keys=True))


if __name__ == "__main__":
    main()
