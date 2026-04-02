import json
import shutil
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import case
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy import update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import staged_artifacts


def finalize_output_path(temp_output: Path, staging_path: Path) -> None:
    if temp_output.exists():
        temp_output.replace(staging_path)
        return
    if staging_path.exists():
        return
    raise FileNotFoundError(f"Encoded output missing after ffmpeg completed: {temp_output}")


def validate_one_item(
        connection: DBClient,
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        probe_media: Callable[[Path], Any],
        source_has_preservable_subtitles: Callable[[list[dict[str, Any]]], bool],
        check: Callable[[dict[str, Any], bool, str], None],
        timestamp: Callable[[], str],
        record_event: Callable[[DBClient, int, str, dict[str, Any]], None],
) -> dict[str, Any]:
    row = connection.execute(
        select(staged_artifacts).where(staged_artifacts.c.library_item_id == item["library_item_id"])
    ).mappings().fetchone()
    if row is None:
        raise FileNotFoundError(f"No staged artifact found for item {item['library_item_id']}")

    staging_path = Path(row["staging_path"])
    staged_probe = probe_media(staging_path)
    staged_size_bytes = staging_path.stat().st_size
    source_size_bytes = int(item["source_size_bytes"])
    validation = {
        "passed": True,
        "checks": [],
        "source_size_bytes": source_size_bytes,
        "staged_size_bytes": staged_size_bytes,
        "bytes_saved": source_size_bytes - staged_size_bytes,
        "size_ratio": round(staged_size_bytes / source_size_bytes, 4) if source_size_bytes else None,
    }

    check(validation, staged_probe.video_codec == "av1", "video codec is AV1")
    check(validation, staged_probe.audio_track_count == 1, "exactly one audio track remains")
    check(validation, staged_probe.english_audio_count == staged_probe.audio_track_count, "all audio tracks are tagged English")

    source_has_english_subs = source_has_preservable_subtitles(item.get("subtitle_summary") or [])
    if source_has_english_subs:
        check(validation, staged_probe.english_subtitle_count >= 1, "English subtitles were preserved")
        if staged_probe.english_subtitle_count:
            subtitles = json.loads(staged_probe.subtitle_summary_json)
            first_language = subtitles[0].get("language") if subtitles else None
            first_default = subtitles[0].get("default") if subtitles else 0
            first_forced = subtitles[0].get("forced") if subtitles else 0
            check(validation, first_language == "eng", "first subtitle is English")
            if any(not subtitle.get("forced") for subtitle in subtitles):
                check(validation, first_default == 1, "first subtitle is default")
            else:
                check(validation, first_forced == 1, "forced-only subtitle outputs stay flagged forced")

    require_size_reduction = bool(config.validation.get("require_size_reduction", True))
    if require_size_reduction:
        check(validation, staged_size_bytes < source_size_bytes, "staged file is smaller than source")

    now = timestamp()
    connection.execute(
        update(staged_artifacts)
        .where(staged_artifacts.c.library_item_id == item["library_item_id"])
        .values(
            validation_json=json.dumps(validation, separators=(",", ":")),
            validated_at=now,
            updated_at=now,
        )
    )
    if validation["passed"]:
        connection.execute(
            update(library_items)
            .where(library_items.c.id == item["library_item_id"])
            .values(status="validated", updated_at=now)
        )
    record_event(connection, item["library_item_id"], "validation_completed", validation)
    connection.commit()
    return validation


def promote_one_item(
        connection: DBClient,
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        force: bool,
        probe_media: Callable[[Path], Any],
        file_fingerprint: Callable[[Path, Any, float | None], str],
        timestamp: Callable[[], str],
        record_event: Callable[[DBClient, int, str, dict[str, Any]], None],
) -> Path:
    stage_row = connection.execute(
        select(staged_artifacts).where(staged_artifacts.c.library_item_id == item["library_item_id"])
    ).mappings().fetchone()
    if stage_row is None:
        raise FileNotFoundError(f"No staged artifact found for item {item['library_item_id']}")
    validation = json.loads(stage_row["validation_json"] or "{}")
    if not force and not validation.get("passed"):
        raise RuntimeError(f"Item {item['library_item_id']} must be validated before promotion")

    source_path = Path(item["source_path"])
    staging_path = Path(stage_row["staging_path"])
    destination_path = source_path.with_suffix(f".{config.output_container}")
    archive_path = config.archive_root / Path(item["rel_path"])
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    if destination_path.exists() and destination_path != source_path:
        raise FileExistsError(f"Destination already exists: {destination_path}")

    if source_path.exists():
        if archive_path.exists():
            archive_path.unlink()
        shutil.move(str(source_path), str(archive_path))
    shutil.move(str(staging_path), str(destination_path))

    promoted_stat = destination_path.stat()
    promoted_probe = probe_media(destination_path)
    promoted_fingerprint = file_fingerprint(destination_path, promoted_stat, promoted_probe.duration_seconds)
    now = timestamp()
    rel_path = str(destination_path.relative_to(config.source_root_map[item["media_root"]].parent))
    parent_dir = str(destination_path.parent.relative_to(config.source_root_map[item["media_root"]].parent))

    connection.execute(
        update(library_items)
        .where(library_items.c.id == item["library_item_id"])
        .values(
            source_path=str(destination_path),
            rel_path=rel_path,
            parent_dir=parent_dir,
            file_name=destination_path.name,
            container=destination_path.suffix.lower(),
            size_bytes=promoted_stat.st_size,
            mtime_ns=promoted_stat.st_mtime_ns,
            fingerprint=promoted_fingerprint,
            duration_seconds=promoted_probe.duration_seconds,
            video_codec=promoted_probe.video_codec,
            video_bitrate=promoted_probe.video_bitrate,
            width=promoted_probe.width,
            height=promoted_probe.height,
            pix_fmt=promoted_probe.pix_fmt,
            audio_track_count=promoted_probe.audio_track_count,
            subtitle_track_count=promoted_probe.subtitle_track_count,
            english_audio_count=promoted_probe.english_audio_count,
            english_subtitle_count=promoted_probe.english_subtitle_count,
            default_audio_language=promoted_probe.default_audio_language,
            default_subtitle_language=promoted_probe.default_subtitle_language,
            audio_summary_json=promoted_probe.audio_summary_json,
            subtitle_summary_json=promoted_probe.subtitle_summary_json,
            status="promoted",
            updated_at=now,
            last_seen_at=now,
        )
    )
    connection.execute(
        update(staged_artifacts)
        .where(staged_artifacts.c.library_item_id == item["library_item_id"])
        .values(
            promoted_at=now,
            promoted_path=str(destination_path),
            archived_source_path=str(archive_path),
            updated_at=now,
        )
    )
    record_event(
        connection,
        item["library_item_id"],
        "promotion_completed",
        {
            "promoted_path": str(destination_path),
            "archived_source_path": str(archive_path),
        },
    )
    return destination_path
