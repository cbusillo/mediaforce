import errno
import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy import update

from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.core.binaries import ffprobe_binary
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.core.process_control import run_command
from mediaforce.core.type_defs import float_value
from mediaforce.core.type_defs import int_value
from mediaforce.core.type_defs import object_dict
from mediaforce.core.type_defs import object_list
from mediaforce.core.utils import content_version_fingerprint
from mediaforce.library.media_scopes import logical_library_rel_path

TRANSIENT_FILE_BUSY_ERRNOS = {errno.EBUSY}
TRANSIENT_FILE_BUSY_RETRY_ATTEMPTS = 8
TRANSIENT_FILE_BUSY_RETRY_DELAY_SECONDS = 0.25
PACKET_DURATION_STREAM_SELECTORS = ("v:0", "a:0")


def _retry_transient_file_busy(operation: Callable[[], Any]) -> Any:
    for attempt in range(TRANSIENT_FILE_BUSY_RETRY_ATTEMPTS):
        try:
            return operation()
        except OSError as exc:
            if exc.errno not in TRANSIENT_FILE_BUSY_ERRNOS or attempt == TRANSIENT_FILE_BUSY_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(TRANSIENT_FILE_BUSY_RETRY_DELAY_SECONDS)
    return None


def safe_unlink(path: Path) -> None:
    def _unlink() -> None:
        path.unlink(missing_ok=True)

    _retry_transient_file_busy(_unlink)


def finalize_output_path(temp_output: Path, staging_path: Path) -> None:
    def _finalize() -> None:
        if temp_output.exists():
            temp_output.replace(staging_path)
            return
        if staging_path.exists():
            return
        raise FileNotFoundError(f"Encoded output missing after ffmpeg completed: {temp_output}")

    _retry_transient_file_busy(_finalize)


def probe_packet_end_seconds(path: Path, stream_selector: str) -> float | None:
    result = run_command(
        [
            ffprobe_binary(),
            "-v",
            "error",
            "-select_streams",
            stream_selector,
            "-show_packets",
            "-show_entries",
            "packet=pts_time,dts_time,duration_time",
            "-of",
            "csv=p=0",
            str(path),
        ]
    )
    if result.returncode != 0:
        return None
    max_end: float | None = None
    for line in result.stdout.splitlines():
        values = [_float_or_none(part) for part in line.split(",")]
        timestamp = next((value for value in values[:2] if value is not None), None)
        if timestamp is None:
            continue
        duration = values[2] if len(values) > 2 and values[2] is not None else 0.0
        packet_end = timestamp + max(duration, 0.0)
        max_end = packet_end if max_end is None else max(max_end, packet_end)
    return max_end


def remux_container_metadata(staging_path: Path, repaired_path: Path) -> None:
    safe_unlink(repaired_path)
    result = run_command(
        [
            ffmpeg_binary(),
            "-v",
            "error",
            "-y",
            "-i",
            str(staging_path),
            "-map",
            "0",
            "-c",
            "copy",
            str(repaired_path),
        ]
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "ffmpeg remux failed").strip()
        raise RuntimeError(detail)


def _float_or_none(value: str) -> float | None:
    value = value.strip()
    if not value or value == "N/A":
        return None
    try:
        return float(value)
    except ValueError:
        return None


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
        packet_end_probe: Callable[[Path, str], float | None],
        remux_container: Callable[[Path, Path], None],
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

    source_has_english_subs = source_has_preservable_subtitles(object_list(item.get("subtitle_summary")))
    require_size_reduction = bool(config.validation.get("require_size_reduction", True))
    source_duration_seconds = float_value(item.get("duration_seconds") or row.get("source_duration_seconds"))
    staged_duration_seconds = float_value(staged_probe.duration_seconds)
    repair_candidate_path: Path | None = None
    if source_duration_seconds > 0:
        repair = _maybe_repair_unreadable_container_duration(
            item,
            staging_path,
            staged_probe,
            staged_size_bytes=staged_size_bytes,
            source_size_bytes=source_size_bytes,
            source_duration_seconds=source_duration_seconds,
            require_size_reduction=require_size_reduction,
            source_has_english_subs=source_has_english_subs,
            source_has_preservable_subtitles=source_has_preservable_subtitles,
            probe_media=probe_media,
            packet_end_probe=packet_end_probe,
            remux_container=remux_container,
        )
        if repair:
            candidate_path_value = repair.pop("_candidate_path", None)
            validation["container_metadata_repair"] = repair
            if candidate_path_value:
                repair_candidate_path = Path(str(candidate_path_value))
                staged_probe = probe_media(repair_candidate_path)
                staged_size_bytes = repair_candidate_path.stat().st_size
                validation.update(
                    {
                        "staged_size_bytes": staged_size_bytes,
                        "bytes_saved": source_size_bytes - staged_size_bytes,
                        "size_ratio": round(staged_size_bytes / source_size_bytes, 4) if source_size_bytes else None,
                    }
                )
                staged_duration_seconds = float_value(staged_probe.duration_seconds)

    check(validation, staged_probe.video_codec == "av1", "video codec is AV1")
    check(validation, staged_probe.audio_track_count == 1, "exactly one audio track remains")
    check(validation, staged_probe.english_audio_count == staged_probe.audio_track_count, "all audio tracks are tagged English")

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

    if require_size_reduction:
        check(validation, staged_size_bytes < source_size_bytes, "staged file is smaller than source")

    resolved_size_goal = object_dict(object_dict(item.get("resolved_operator_intent")).get("size_goal"))
    final_lower_bound_bytes = int_value(resolved_size_goal.get("final_lower_bound_bytes"))
    final_upper_bound_bytes = int_value(resolved_size_goal.get("final_upper_bound_bytes"))
    if final_lower_bound_bytes > 0 and final_upper_bound_bytes >= final_lower_bound_bytes:
        validation["final_size_goal"] = {
            "target_size_bytes": int_value(resolved_size_goal.get("target_size_bytes")) or None,
            "lower_bound_bytes": final_lower_bound_bytes,
            "upper_bound_bytes": final_upper_bound_bytes,
            "tolerance_percent": float_value(resolved_size_goal.get("final_output_tolerance_percent")) or None,
        }
        check(
            validation,
            final_lower_bound_bytes <= staged_size_bytes <= final_upper_bound_bytes,
            "staged file is within the final size target band",
        )

    if source_duration_seconds > 0:
        check(validation, staged_duration_seconds > 0, "staged file duration is readable")
        if staged_duration_seconds > 0:
            minimum_expected_duration = _minimum_expected_duration(source_duration_seconds)
            check(
                validation,
                staged_duration_seconds >= minimum_expected_duration,
                "staged duration closely matches the source",
            )

    if repair_candidate_path is not None:
        repair_payload = validation.get("container_metadata_repair")
        if validation["passed"]:
            try:
                repair_candidate_path.replace(staging_path)
                if isinstance(repair_payload, dict):
                    repair_payload["repaired"] = True
            except Exception as exc:  # noqa: BLE001 - leave validation failed if the repaired file cannot be installed.
                if isinstance(repair_payload, dict):
                    repair_payload["error"] = str(exc)
                check(validation, False, "container metadata repair replaced staged file")
                safe_unlink(repair_candidate_path)
        else:
            if isinstance(repair_payload, dict):
                repair_payload["skipped_reason"] = "normal validation failed after remux candidate"
            safe_unlink(repair_candidate_path)

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


def _maybe_repair_unreadable_container_duration(
        item: dict[str, Any],
        staging_path: Path,
        staged_probe: Any,
        *,
        staged_size_bytes: int,
        source_size_bytes: int,
        source_duration_seconds: float,
        require_size_reduction: bool,
        source_has_english_subs: bool,
        source_has_preservable_subtitles: Callable[[list[dict[str, Any]]], bool],
        probe_media: Callable[[Path], Any],
        packet_end_probe: Callable[[Path, str], float | None],
        remux_container: Callable[[Path, Path], None],
) -> dict[str, Any] | None:
    if float_value(staged_probe.duration_seconds) > 0:
        return None

    repair = {
        "attempted": False,
        "repaired": False,
        "reason": "staged duration was unreadable",
    }
    if not _non_duration_checks_allow_container_repair(
            item,
            staged_probe,
            staged_size_bytes=staged_size_bytes,
            source_size_bytes=source_size_bytes,
            require_size_reduction=require_size_reduction,
            source_has_english_subs=source_has_english_subs,
            source_has_preservable_subtitles=source_has_preservable_subtitles,
    ):
        repair["skipped_reason"] = "another validation check failed"
        return repair

    source_path_value = item.get("source_path")
    if not source_path_value:
        repair["skipped_reason"] = "source path missing"
        return repair
    try:
        source_probe = probe_media(Path(str(source_path_value)))
    except Exception as exc:  # noqa: BLE001 - validation should fail closed on probe issues.
        repair["skipped_reason"] = f"source duration probe failed: {exc}"
        return repair
    probed_source_duration = float_value(source_probe.duration_seconds)
    if probed_source_duration <= 0:
        repair["skipped_reason"] = "source duration unreadable"
        return repair

    repair["source_duration_seconds"] = round(probed_source_duration, 3)
    effective_source_duration = probed_source_duration or source_duration_seconds
    try:
        packet_endpoints = _packet_endpoints(staging_path, packet_end_probe)
    except Exception as exc:  # noqa: BLE001 - optional repair must not mask the validation failure.
        repair["skipped_reason"] = f"packet timestamp probe failed: {exc}"
        return repair
    repair["packet_end_seconds"] = {
        selector: round(endpoint, 3) if endpoint is not None else None
        for selector, endpoint in packet_endpoints.items()
    }
    if not _packet_endpoints_reach_duration(packet_endpoints, effective_source_duration):
        repair["skipped_reason"] = "packet timestamps did not prove the staged file is complete"
        return repair

    repaired_path = staging_path.with_name(f"{staging_path.stem}.remuxing{staging_path.suffix}")
    repair["attempted"] = True
    try:
        remux_container(staging_path, repaired_path)
        repaired_probe = probe_media(repaired_path)
        repaired_duration_seconds = float_value(repaired_probe.duration_seconds)
        repair["repaired_duration_seconds"] = round(repaired_duration_seconds, 3)
        if repaired_duration_seconds < _minimum_expected_duration(effective_source_duration):
            repair["skipped_reason"] = "remuxed duration still did not match source"
            return repair
        repair["_candidate_path"] = str(repaired_path)
    except Exception as exc:  # noqa: BLE001 - validation should retain original staged file on repair issues.
        repair["error"] = str(exc)
        return repair
    finally:
        if "_candidate_path" not in repair:
            safe_unlink(repaired_path)

    return repair


def _non_duration_checks_allow_container_repair(
        item: dict[str, Any],
        staged_probe: Any,
        *,
        staged_size_bytes: int,
        source_size_bytes: int,
        require_size_reduction: bool,
        source_has_english_subs: bool,
        source_has_preservable_subtitles: Callable[[list[dict[str, Any]]], bool],
) -> bool:
    if staged_probe.video_codec != "av1":
        return False
    if staged_probe.audio_track_count != 1:
        return False
    if staged_probe.english_audio_count != staged_probe.audio_track_count:
        return False
    if require_size_reduction and staged_size_bytes >= source_size_bytes:
        return False
    if source_has_english_subs:
        if staged_probe.english_subtitle_count < 1:
            return False
        subtitles = json.loads(staged_probe.subtitle_summary_json)
        first_language = subtitles[0].get("language") if subtitles else None
        first_default = subtitles[0].get("default") if subtitles else 0
        first_forced = subtitles[0].get("forced") if subtitles else 0
        if first_language != "eng":
            return False
        if any(not subtitle.get("forced") for subtitle in subtitles):
            return first_default == 1
        return first_forced == 1
    return not source_has_preservable_subtitles(object_list(item.get("subtitle_summary")))


def _packet_endpoints(staging_path: Path, packet_end_probe: Callable[[Path, str], float | None]) -> dict[str, float | None]:
    return {
        selector: packet_end_probe(staging_path, selector)
        for selector in PACKET_DURATION_STREAM_SELECTORS
    }


def _packet_endpoints_reach_duration(packet_endpoints: dict[str, float | None], source_duration_seconds: float) -> bool:
    minimum_expected_duration = _minimum_expected_duration(source_duration_seconds)
    video_end = packet_endpoints.get("v:0")
    audio_end = packet_endpoints.get("a:0")
    if video_end is None or video_end < minimum_expected_duration:
        return False
    return audio_end is not None and audio_end >= minimum_expected_duration


def _minimum_expected_duration(source_duration_seconds: float) -> float:
    return max(source_duration_seconds * 0.98, source_duration_seconds - 2.0)


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
    promoted_content_fingerprint = content_version_fingerprint(destination_path, promoted_stat)
    now = timestamp()
    logical_path = logical_library_rel_path(
        str(item["media_root"]),
        config.source_root_map[str(item["media_root"])],
        destination_path,
    )
    rel_path = logical_path.as_posix()
    parent_dir = logical_path.parent.as_posix()

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
            attachment_summary_json=promoted_probe.attachment_summary_json,
            content_version_changed_at=now,
            content_version_fingerprint=promoted_content_fingerprint,
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
