import io
import json
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

from mediaforce.binaries import ffmpeg_binary
from mediaforce.config import MediaforceConfig
from mediaforce.ffmpeg import ffmpeg_hwaccel_input_args
from mediaforce.probe import probe_media
from mediaforce.process_control import ManagedProcessController, ProcessCancelledError, run_command
from mediaforce.quality import QualitySearchError, QualitySearchResult, run_crf_search, select_quality_metric
from mediaforce.remote import execution_mode_for_host, host_media_access_for_host, remote_shell_path_export_line, \
    ssh_client_options
from mediaforce.utils import file_fingerprint, timestamp

TEXT_SUBTITLE_CODECS = {"ass", "mov_text", "srt", "ssa", "subrip", "text", "webvtt"}
SVT_AV1_MIN_8K_PRESET = 5
SVT_AV1_8K_DIMENSION_THRESHOLD = 7680
ENCODE_PROGRESS_ARGS = ["-progress", "pipe:2", "-nostats"]


@dataclass(slots=True)
class EncodeResult:
    staging_path: Path
    source_size_bytes: int
    staging_size_bytes: int
    chosen_crf: float
    quality_metric: str
    quality_target: float
    quality_score: float
    encode_command: list[str]


def encode_manifest_items(
        connection: sqlite3.Connection,
        config: MediaforceConfig,
        manifest_path: Path,
        manifest: dict[str, Any],
        indexes: list[int],
        overwrite: bool,
        process_controller: ManagedProcessController | None = None,
        host: dict[str, Any] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> list[EncodeResult]:
    results: list[EncodeResult] = []
    total_duration_seconds = sum(float(manifest["items"][index].get("duration_seconds") or 0.0) for index in indexes)
    completed_duration_seconds = 0.0
    for index in indexes:
        if process_controller is not None:
            process_controller.throw_if_cancelled()
        item = manifest["items"][index]
        item_duration_seconds = float(item.get("duration_seconds") or 0.0)

        def item_progress(snapshot: dict[str, Any]) -> None:
            if progress_callback is None:
                return
            current_out_time_seconds = min(float(snapshot.get("out_time_seconds") or 0.0), item_duration_seconds)
            overall_completed_duration_seconds = completed_duration_seconds + current_out_time_seconds
            payload = {
                **snapshot,
                "current_item_index": int(index),
                "current_item_number": len(results) + 1,
                "current_item_rel_path": str(item.get("rel_path") or item.get("source_path") or ""),
                "current_item_duration_seconds": item_duration_seconds,
                "completed_item_count": len(results),
                "total_item_count": len(indexes),
                "completed_duration_seconds": completed_duration_seconds,
                "total_duration_seconds": total_duration_seconds,
                "overall_completed_duration_seconds": overall_completed_duration_seconds,
                "remaining_duration_seconds": max(total_duration_seconds - overall_completed_duration_seconds, 0.0),
            }
            if total_duration_seconds > 0:
                payload["percent_complete"] = min(overall_completed_duration_seconds / total_duration_seconds,
                                                  1.0) * 100.0
            speed_value = float(payload.get("speed") or 0.0)
            if speed_value > 0.0:
                payload["eta_seconds"] = payload["remaining_duration_seconds"] / speed_value
            progress_callback(payload)

        result = encode_one_item(
            connection,
            config,
            manifest_path,
            manifest,
            index,
            item,
            overwrite=overwrite,
            process_controller=process_controller,
            host=host,
            progress_callback=item_progress,
        )
        results.append(result)
        completed_duration_seconds += item_duration_seconds
    return results


def describe_item_plan(item: dict[str, Any]) -> dict[str, Any]:
    policy = item["resolved_policy"]
    selection = _select_streams(item)
    quality_metric, _ = select_quality_metric(str(policy["video"].get("quality_metric", "auto")))
    selected_audio = selection["audio_tracks"][0]
    audio_codec = _audio_codec(selected_audio, policy["audio"])
    audio_plan = {
        "source_codec": str(selected_audio.get("codec_name") or "unknown"),
        "channels": int(selected_audio.get("channels") or 0),
        "language": selected_audio.get("language") or "und",
        "action": "convert" if audio_codec == "libopus" else "copy",
        "output_codec": "opus" if audio_codec == "libopus" else str(selected_audio.get("codec_name") or "unknown"),
        "output_bitrate": _opus_bitrate(selected_audio, policy["audio"]) if audio_codec == "libopus" else None,
        "source_track_count": len(item.get("audio_summary") or []),
        "kept_track_count": len(selection["audio_tracks"]),
    }
    subtitle_tracks = selection["subtitle_tracks"]
    return {
        "video": {
            "source_codec": item.get("video_codec") or "unknown",
            "output_codec": "av1",
            "quality_metric": quality_metric,
            "target": float(
                policy["video"][
                    "target_vmaf" if quality_metric == "vmaf" else "target_xpsnr"
                ]
            ),
            "min_target": float(
                policy["video"][
                    "min_target_vmaf" if quality_metric == "vmaf" else "min_target_xpsnr"
                ]
            ),
            "max_encoded_percent": float(policy["video"].get("max_encoded_percent", 100)),
            "default_grain": int(policy["video"].get("default_grain", 0)),
        },
        "audio": audio_plan,
        "subtitles": {
            "source_track_count": len(item.get("subtitle_summary") or []),
            "kept_track_count": len(subtitle_tracks),
            "languages": [track.get("language") or "und" for track in subtitle_tracks],
            "codecs": [track.get("codec_name") or "unknown" for track in subtitle_tracks],
        },
    }


def build_svt_params(video_policy: dict[str, Any]) -> list[str]:
    return [
        "tune=0",
        f"film-grain={int(video_policy.get('default_grain', 0))}",
        f"film-grain-denoise={int(video_policy.get('grain_denoise', 0))}",
    ]


def effective_video_preset(
        video_policy: dict[str, Any],
        *,
        width: int | None = None,
        height: int | None = None,
) -> int:
    preset = int(video_policy["preset"])
    if str(video_policy.get("encoder") or "").lower() != "libsvtav1":
        return preset
    largest_dimension = max(int(width or 0), int(height or 0))
    if largest_dimension < SVT_AV1_8K_DIMENSION_THRESHOLD:
        return preset
    # Current SVT-AV1 builds reject 8K-plus sources on slower presets.
    return max(preset, SVT_AV1_MIN_8K_PRESET)


def estimate_output_overhead_bytes(item: dict[str, Any]) -> dict[str, int]:
    selection = _select_streams(item)
    duration_seconds = float(item.get("duration_seconds") or 0.0)
    audio_bytes = 0
    for track in selection["audio_tracks"]:
        audio_bytes += _estimate_audio_track_bytes(track, item["resolved_policy"]["audio"], duration_seconds)

    subtitle_bytes = 0
    for track in selection["subtitle_tracks"]:
        subtitle_bytes += _estimate_subtitle_track_bytes(track)

    container_bytes = 256 * 1024
    return {
        "audio_bytes": audio_bytes,
        "subtitle_bytes": subtitle_bytes,
        "container_bytes": container_bytes,
        "total_bytes": audio_bytes + subtitle_bytes + container_bytes,
    }


def resolve_item_source_path(
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        host: dict[str, Any] | None = None,
) -> Path:
    if host_media_access_for_host(host) == "stream":
        return Path(str(item["source_path"]))
    media_root = str(item.get("media_root") or "").strip()
    rel_path = str(item.get("rel_path") or "").strip()
    if media_root and rel_path:
        root = config.source_root_map_for_host(host).get(media_root)
        if root is not None:
            return root.parent / Path(rel_path)
    return Path(str(item["source_path"]))


def resolve_item_staging_path(
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        host: dict[str, Any] | None = None,
) -> Path:
    if host_media_access_for_host(host) == "stream":
        return Path(str(item["staging_path"]))
    rel_path = str(item.get("rel_path") or "").strip()
    if rel_path:
        return config.staging_root_for_host(host) / Path(rel_path).with_suffix(f".{config.output_container}")
    return Path(str(item["staging_path"]))


def encode_one_item(
        connection: sqlite3.Connection,
        config: MediaforceConfig,
        manifest_path: Path,
        manifest: dict[str, Any],
        index: int,
        item: dict[str, Any],
        *,
        overwrite: bool,
        process_controller: ManagedProcessController | None = None,
        host: dict[str, Any] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> EncodeResult:
    source_path = resolve_item_source_path(config, item, host=host)
    staging_path = resolve_item_staging_path(config, item, host=host)
    staging_path.parent.mkdir(parents=True, exist_ok=True)

    if staging_path.exists() and not overwrite:
        raise FileExistsError(f"Staging file already exists: {staging_path}")

    policy = item["resolved_policy"]
    width = int(item.get("width") or 0) or None
    height = int(item.get("height") or 0) or None
    preset = effective_video_preset(policy["video"], width=width, height=height)
    quality_result = _search_quality(
        source_path,
        policy["video"],
        source_codec=str(item.get("video_codec") or ""),
        width=width,
        height=height,
        process_controller=process_controller,
        host=host,
    )
    selection = _select_streams(item)
    ffmpeg_cmd = _build_ffmpeg_command(
        source_path=source_path,
        staging_path=staging_path,
        source_codec=str(item.get("video_codec") or ""),
        video_policy=policy["video"],
        preset=preset,
        audio_policy=policy["audio"],
        subtitle_policy=policy["subtitle"],
        selection=selection,
        quality=quality_result,
        host=host,
    )

    started_at = timestamp()
    _record_event(connection, item["library_item_id"], "encoding_started",
                  {"manifest": str(manifest_path), "item_index": index})
    connection.execute(
        "UPDATE library_items SET status = 'encoding', updated_at = ? WHERE id = ?",
        (started_at, item["library_item_id"]),
    )
    connection.commit()

    temp_output = staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}")
    if temp_output.exists():
        temp_output.unlink()
    if staging_path.exists() and overwrite:
        staging_path.unlink()

    try:
        result = _run_encode_command(
            ffmpeg_cmd=ffmpeg_cmd,
            temp_output=temp_output,
            staging_path=staging_path,
            overwrite=overwrite,
            process_controller=process_controller,
            host=host,
            progress_callback=progress_callback,
        )
        if result.returncode != 0:
            details = (result.stdout or "").strip()
            if (result.stderr or "").strip():
                details = f"{details}\n{(result.stderr or '').strip()}".strip()
            raise RuntimeError(details or "ffmpeg encode failed")
        _finalize_output_path(temp_output, staging_path)
    except Exception:
        temp_output.unlink(missing_ok=True)
        staging_path.unlink(missing_ok=True)
        raise

    staged_stat = staging_path.stat()
    staged_probe = probe_media(staging_path)
    staged_fingerprint = file_fingerprint(staging_path, staged_stat, staged_probe.duration_seconds)
    now = timestamp()

    connection.execute(
        """
        INSERT INTO staged_artifacts (library_item_id, manifest_run_id, manifest_path, item_index, source_fingerprint,
                                      staging_path, staging_size_bytes, staging_mtime_ns, staging_fingerprint,
                                      chosen_crf, quality_metric, quality_target, quality_score, encode_command_json,
                                      audio_summary_json, subtitle_summary_json, staged_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(library_item_id) DO UPDATE SET manifest_run_id       = excluded.manifest_run_id,
                                                   manifest_path         = excluded.manifest_path,
                                                   item_index            = excluded.item_index,
                                                   source_fingerprint    = excluded.source_fingerprint,
                                                   staging_path          = excluded.staging_path,
                                                   staging_size_bytes    = excluded.staging_size_bytes,
                                                   staging_mtime_ns      = excluded.staging_mtime_ns,
                                                   staging_fingerprint   = excluded.staging_fingerprint,
                                                   chosen_crf            = excluded.chosen_crf,
                                                   quality_metric        = excluded.quality_metric,
                                                   quality_target        = excluded.quality_target,
                                                   quality_score         = excluded.quality_score,
                                                   encode_command_json   = excluded.encode_command_json,
                                                   audio_summary_json    = excluded.audio_summary_json,
                                                   subtitle_summary_json = excluded.subtitle_summary_json,
                                                   staged_at             = excluded.staged_at,
                                                   updated_at            = excluded.updated_at
        """,
        (
            item["library_item_id"],
            manifest["run_id"],
            str(manifest_path),
            index,
            item["source_fingerprint"],
            str(staging_path),
            staged_stat.st_size,
            staged_stat.st_mtime_ns,
            staged_fingerprint,
            quality_result.crf,
            quality_result.metric,
            quality_result.target,
            quality_result.score,
            json.dumps(ffmpeg_cmd, separators=(",", ":")),
            staged_probe.audio_summary_json,
            staged_probe.subtitle_summary_json,
            now,
            now,
        ),
    )
    connection.execute(
        "UPDATE library_items SET status = 'encoded', updated_at = ? WHERE id = ?",
        (now, item["library_item_id"]),
    )
    _record_event(
        connection,
        item["library_item_id"],
        "encoding_completed",
        {
            "staging_path": str(staging_path),
            "chosen_crf": quality_result.crf,
            "quality_metric": quality_result.metric,
            "quality_score": quality_result.score,
        },
    )
    connection.commit()

    return EncodeResult(
        staging_path=staging_path,
        source_size_bytes=int(item["source_size_bytes"]),
        staging_size_bytes=staged_stat.st_size,
        chosen_crf=quality_result.crf,
        quality_metric=quality_result.metric,
        quality_target=quality_result.target,
        quality_score=quality_result.score,
        encode_command=ffmpeg_cmd,
    )


def search_quality_for_source(
        source_path: Path,
        video_policy: dict[str, Any],
        *,
        source_codec: str | None = None,
        width: int | None = None,
        height: int | None = None,
        process_controller: ManagedProcessController | None = None,
        host: dict[str, Any] | None = None,
) -> QualitySearchResult:
    return _search_quality(
        source_path,
        video_policy,
        source_codec=source_codec,
        width=width,
        height=height,
        process_controller=process_controller,
        host=host,
    )


def validate_manifest_items(
        connection: sqlite3.Connection,
        config: MediaforceConfig,
        manifest: dict[str, Any],
        indexes: list[int],
) -> list[dict[str, Any]]:
    results = []
    for index in indexes:
        item = manifest["items"][index]
        result = validate_one_item(connection, config, item)
        results.append(result)
    return results


def validate_one_item(connection: sqlite3.Connection, config: MediaforceConfig, item: dict[str, Any]) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM staged_artifacts WHERE library_item_id = ?",
        (item["library_item_id"],),
    ).fetchone()
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

    _check(validation, staged_probe.video_codec == "av1", "video codec is AV1")
    _check(validation, staged_probe.audio_track_count == 1, "exactly one audio track remains")
    _check(validation, staged_probe.english_audio_count == staged_probe.audio_track_count,
           "all audio tracks are tagged English")

    source_has_english_subs = _source_has_preservable_subtitles(item.get("subtitle_summary") or [])
    if source_has_english_subs:
        _check(validation, staged_probe.english_subtitle_count >= 1, "English subtitles were preserved")
        if staged_probe.english_subtitle_count:
            subtitles = json.loads(staged_probe.subtitle_summary_json)
            first_language = subtitles[0].get("language") if subtitles else None
            first_default = subtitles[0].get("default") if subtitles else 0
            first_forced = subtitles[0].get("forced") if subtitles else 0
            _check(validation, first_language == "eng", "first subtitle is English")
            if any(not subtitle.get("forced") for subtitle in subtitles):
                _check(validation, first_default == 1, "first subtitle is default")
            else:
                _check(validation, first_forced == 1, "forced-only subtitle outputs stay flagged forced")

    require_size_reduction = bool(config.validation.get("require_size_reduction", True))
    if require_size_reduction:
        _check(validation, staged_size_bytes < source_size_bytes, "staged file is smaller than source")

    now = timestamp()
    connection.execute(
        "UPDATE staged_artifacts SET validation_json = ?, validated_at = ?, updated_at = ? WHERE library_item_id = ?",
        (json.dumps(validation, separators=(",", ":")), now, now, item["library_item_id"]),
    )
    if validation["passed"]:
        connection.execute(
            "UPDATE library_items SET status = 'validated', updated_at = ? WHERE id = ?",
            (now, item["library_item_id"]),
        )
    _record_event(connection, item["library_item_id"], "validation_completed", validation)
    connection.commit()
    return validation


def promote_manifest_items(
        connection: sqlite3.Connection,
        config: MediaforceConfig,
        manifest: dict[str, Any],
        indexes: list[int],
        force: bool,
) -> list[Path]:
    promoted_paths = []
    for index in indexes:
        item = manifest["items"][index]
        promoted_paths.append(promote_one_item(connection, config, item, force=force))
    return promoted_paths


def promote_one_item(connection: sqlite3.Connection, config: MediaforceConfig, item: dict[str, Any], *,
                     force: bool) -> Path:
    stage_row = connection.execute(
        "SELECT * FROM staged_artifacts WHERE library_item_id = ?",
        (item["library_item_id"],),
    ).fetchone()
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
        """
        UPDATE library_items
        SET source_path               = ?,
            rel_path                  = ?,
            parent_dir                = ?,
            file_name                 = ?,
            container                 = ?,
            size_bytes                = ?,
            mtime_ns                  = ?,
            fingerprint               = ?,
            duration_seconds          = ?,
            video_codec               = ?,
            video_bitrate             = ?,
            width                     = ?,
            height                    = ?,
            pix_fmt                   = ?,
            audio_track_count         = ?,
            subtitle_track_count      = ?,
            english_audio_count       = ?,
            english_subtitle_count    = ?,
            default_audio_language    = ?,
            default_subtitle_language = ?,
            audio_summary_json        = ?,
            subtitle_summary_json     = ?,
            status                    = 'promoted',
            updated_at                = ?,
            last_seen_at              = ?
        WHERE id = ?
        """,
        (
            str(destination_path),
            rel_path,
            parent_dir,
            destination_path.name,
            destination_path.suffix.lower(),
            promoted_stat.st_size,
            promoted_stat.st_mtime_ns,
            promoted_fingerprint,
            promoted_probe.duration_seconds,
            promoted_probe.video_codec,
            promoted_probe.video_bitrate,
            promoted_probe.width,
            promoted_probe.height,
            promoted_probe.pix_fmt,
            promoted_probe.audio_track_count,
            promoted_probe.subtitle_track_count,
            promoted_probe.english_audio_count,
            promoted_probe.english_subtitle_count,
            promoted_probe.default_audio_language,
            promoted_probe.default_subtitle_language,
            promoted_probe.audio_summary_json,
            promoted_probe.subtitle_summary_json,
            now,
            now,
            item["library_item_id"],
        ),
    )
    connection.execute(
        """
        UPDATE staged_artifacts
        SET promoted_at          = ?,
            promoted_path        = ?,
            archived_source_path = ?,
            updated_at           = ?
        WHERE library_item_id = ?
        """,
        (now, str(destination_path), str(archive_path), now, item["library_item_id"]),
    )
    _record_event(
        connection,
        item["library_item_id"],
        "promotion_completed",
        {
            "promoted_path": str(destination_path),
            "archived_source_path": str(archive_path),
        },
    )
    return destination_path


def _search_quality(
        source_path: Path,
        video_policy: dict[str, Any],
        *,
        source_codec: str | None = None,
        width: int | None = None,
        height: int | None = None,
        process_controller: ManagedProcessController | None = None,
        host: dict[str, Any] | None = None,
) -> QualitySearchResult:
    quality_host = host
    if host_media_access_for_host(host) == "stream":
        quality_host = {**(host or {}), "mode": "local"}
    metric_name, default_target = select_quality_metric(str(video_policy.get("quality_metric", "auto")))
    metric_target = float(video_policy.get(f"target_{metric_name.lower()}", default_target))
    min_target = float(video_policy.get(f"min_target_{metric_name.lower()}", metric_target))
    relax_step = float(
        video_policy.get(f"target_relax_step_{metric_name.lower()}", 1.0 if metric_name == "xpsnr" else 0.5))
    svt_params = build_svt_params(video_policy)
    preset = effective_video_preset(video_policy, width=width, height=height)
    attempted_target = metric_target
    last_error: Exception | None = None

    while attempted_target >= min_target:
        try:
            return run_crf_search(
                source_path,
                source_codec=source_codec,
                preferred_metric=metric_name,
                metric_target=attempted_target,
                preset=preset,
                pixel_format=str(video_policy["pixel_format"]),
                sample_every=str(video_policy["sample_every"]),
                sample_duration=str(video_policy["sample_duration"]),
                min_crf=int(video_policy["min_crf"]),
                max_crf=int(video_policy["max_crf"]),
                max_encoded_percent=int(video_policy["max_encoded_percent"]),
                svt_params=svt_params,
                thorough=bool(video_policy.get("thorough", False)),
                process_controller=process_controller,
                host=quality_host,
            )
        except QualitySearchError as exc:
            last_error = exc
            attempted_target = round(attempted_target - relax_step, 3)

    if last_error is not None:
        raise last_error
    raise RuntimeError("Quality search did not run")


def _build_ffmpeg_command(
        *,
        source_path: Path,
        staging_path: Path,
        source_codec: str | None,
        video_policy: dict[str, Any],
        preset: int,
        audio_policy: dict[str, Any],
        subtitle_policy: dict[str, Any],
        selection: dict[str, Any],
        quality: QualitySearchResult,
        host: dict[str, Any] | None = None,
) -> list[str]:
    _ = subtitle_policy
    mediaforce_tags = {
        "mediaforce_encoded_by": "mediaforce",
        "mediaforce_quality_metric": quality.metric,
        "mediaforce_quality_target": _format_metadata_number(quality.target),
        "mediaforce_quality_score": _format_metadata_number(quality.score),
        "mediaforce_chosen_crf": _format_metadata_number(quality.crf),
    }
    cmd = [
        ffmpeg_binary(),
        "-y",
        *ffmpeg_hwaccel_input_args(
            source_codec,
            platform_name=str((host or {}).get("platform") or "") or None,
            videotoolbox_available=bool((host or {}).get("videotoolbox_available"))
            if "videotoolbox_available" in (host or {})
            else None,
        ),
        "-i",
        str(source_path),
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-map",
        "0:v:0",
    ]
    for key, value in mediaforce_tags.items():
        cmd.extend(["-metadata", f"{key}={value}"])

    for audio in selection["audio_tracks"]:
        cmd.extend(["-map", f"0:{audio['index']}"])
    for subtitle in selection["subtitle_tracks"]:
        cmd.extend(["-map", f"0:{subtitle['index']}"])

    cmd.extend(
        [
            "-c:v",
            str(video_policy["encoder"]),
            "-pix_fmt",
            str(video_policy["pixel_format"]),
            "-preset",
            str(preset),
            "-crf",
            _format_crf(quality.crf),
            "-svtav1-params",
            f"tune=0:film-grain={int(video_policy.get('default_grain', 0))}:film-grain-denoise={int(video_policy.get('grain_denoise', 0))}",
        ]
    )

    for output_index, audio in enumerate(selection["audio_tracks"]):
        codec = _audio_codec(audio, audio_policy)
        cmd.extend([f"-c:a:{output_index}", codec])
        cmd.extend([f"-metadata:s:a:{output_index}", "language=eng"])
        cmd.extend([f"-disposition:a:{output_index}", "default"])
        if codec == "libopus":
            layout_filter = _opus_layout_filter(audio)
            if layout_filter:
                cmd.extend([f"-af:a:{output_index}", layout_filter])
                cmd.extend([f"-mapping_family:a:{output_index}", "1"])
            cmd.extend([f"-b:a:{output_index}", _opus_bitrate(audio, audio_policy)])

    for output_index, subtitle in enumerate(selection["subtitle_tracks"]):
        codec = "srt" if subtitle["codec_name"] in TEXT_SUBTITLE_CODECS else "copy"
        cmd.extend([f"-c:s:{output_index}", codec])
        cmd.extend([f"-metadata:s:s:{output_index}", "language=eng"])
        disposition = "default" if output_index == 0 and not subtitle.get("forced") else "0"
        if subtitle.get("forced"):
            disposition = "forced"
        cmd.extend([f"-disposition:s:{output_index}", disposition])

    if not selection["subtitle_tracks"]:
        cmd.extend(["-sn"])

    return cmd + [str(staging_path)]


def _format_metadata_number(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _finalize_output_path(temp_output: Path, staging_path: Path) -> None:
    if temp_output.exists():
        temp_output.replace(staging_path)
        return
    if staging_path.exists():
        return
    raise FileNotFoundError(f"Encoded output missing after ffmpeg completed: {temp_output}")


def _run_encode_command(
        *,
        ffmpeg_cmd: list[str],
        temp_output: Path,
        staging_path: Path,
        overwrite: bool,
        process_controller: ManagedProcessController | None,
        host: dict[str, Any] | None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    host_mode = execution_mode_for_host(host)
    if host_mode != "ssh":
        return _run_tracked_encode_command(
            ffmpeg_cmd[:-1] + [str(temp_output)],
            process_controller=process_controller,
            progress_callback=progress_callback,
        )

    if host_media_access_for_host(host) == "stream":
        return _run_streamed_remote_encode_command(
            ffmpeg_cmd=ffmpeg_cmd,
            temp_output=temp_output,
            source_path=Path(ffmpeg_cmd[ffmpeg_cmd.index("-i") + 1]),
            process_controller=process_controller,
            host=host,
            progress_callback=progress_callback,
        )

    ssh_host = str((host or {}).get("key") or (host or {}).get("host") or "").strip()
    if not ssh_host:
        raise RuntimeError("Remote encode host is missing an SSH target.")

    remote_ffmpeg_cmd = list(ffmpeg_cmd[:-1]) + [str(temp_output)]
    remote_ffmpeg_cmd[0] = Path(remote_ffmpeg_cmd[0]).name
    remote_ffmpeg_cmd = _ffmpeg_command_with_progress(remote_ffmpeg_cmd)
    remote_script_parts = [
        remote_shell_path_export_line(),
        f"mkdir -p {shlex.quote(str(staging_path.parent))}",
        f"rm -f {shlex.quote(str(temp_output))}",
    ]
    if overwrite:
        remote_script_parts.append(f"rm -f {shlex.quote(str(staging_path))}")
    remote_script_parts.extend(
        [
            shlex.join(remote_ffmpeg_cmd),
            f"mv -f {shlex.quote(str(temp_output))} {shlex.quote(str(staging_path))}",
        ]
    )
    ssh_cmd = [
        "ssh",
        *ssh_client_options(),
        ssh_host,
        "sh",
        "-lc",
        " && ".join(remote_script_parts),
    ]
    return _run_tracked_process(
        ssh_cmd,
        process_controller=process_controller,
        progress_callback=progress_callback,
    )


def _streaming_output_args_for_path(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".mkv":
        return ["-f", "matroska"]
    if suffix in {".mp4", ".m4v", ".mov"}:
        # Fragmented MP4/MOV can be written to a non-seekable pipe safely.
        return ["-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4"]
    if suffix == ".ts":
        return ["-f", "mpegts"]
    raise RuntimeError(f"Streaming encode output is not supported for {suffix or 'this file type'}.")


def _build_streaming_remote_ffmpeg_command(
        ffmpeg_cmd: list[str], *, source_path: Path, output_path: Path, executable_path: str | None = None
) -> list[str]:
    remote_ffmpeg_cmd = list(ffmpeg_cmd[:-1])
    remote_ffmpeg_cmd[0] = str(executable_path or Path(remote_ffmpeg_cmd[0]).name)
    try:
        source_index = remote_ffmpeg_cmd.index(str(source_path))
    except ValueError as exc:
        raise RuntimeError("Streaming encode source path was missing from the ffmpeg command.") from exc
    remote_ffmpeg_cmd[source_index] = "pipe:0"
    remote_ffmpeg_cmd = _ffmpeg_command_with_progress(remote_ffmpeg_cmd)
    return [*remote_ffmpeg_cmd, *_streaming_output_args_for_path(output_path), "pipe:1"]


def _run_streamed_remote_encode_command(
        *,
        ffmpeg_cmd: list[str],
        temp_output: Path,
        source_path: Path,
        process_controller: ManagedProcessController | None,
        host: dict[str, Any] | None,
        progress_callback: Callable[[dict[str, Any]], None] | None,
) -> subprocess.CompletedProcess[str]:
    ssh_host = str((host or {}).get("key") or (host or {}).get("host") or "").strip()
    if not ssh_host:
        raise RuntimeError("Remote encode host is missing an SSH target.")

    remote_ffmpeg_cmd = _build_streaming_remote_ffmpeg_command(
        ffmpeg_cmd,
        source_path=source_path,
        output_path=temp_output,
        executable_path=str((host or {}).get("ffmpeg_path") or "") or None,
    )
    ssh_cmd = [
        "ssh",
        *ssh_client_options(),
        ssh_host,
        *remote_ffmpeg_cmd,
    ]

    process_controller.throw_if_cancelled() if process_controller is not None else None
    process = subprocess.Popen(ssh_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               bufsize=0)
    process_handle = cast(subprocess.Popen[str], process)
    if process_controller is not None:
        process_controller.attach(process_handle)

    stderr_lines: list[str] = []
    progress_state: dict[str, str] = {}
    start_time = time.monotonic()

    def pump_source() -> None:
        if process.stdin is None:
            return
        try:
            with source_path.open("rb") as source_file:
                shutil.copyfileobj(source_file, process.stdin, length=1024 * 1024)
        except BrokenPipeError:
            return
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    def collect_output() -> None:
        if process.stdout is None:
            return
        with temp_output.open("wb") as output_file:
            shutil.copyfileobj(process.stdout, output_file, length=1024 * 1024)
        process.stdout.close()

    def consume_stderr() -> None:
        if process.stderr is None:
            return
        stream = io.TextIOWrapper(process.stderr, encoding="utf-8", errors="replace")
        for line in iter(stream.readline, ""):
            stderr_lines.append(line)
            if progress_callback is None:
                continue
            snapshot = _update_ffmpeg_progress_state(progress_state, line,
                                                     elapsed_seconds=time.monotonic() - start_time)
            if snapshot is not None:
                progress_callback(snapshot)
        stream.close()

    source_thread = threading.Thread(target=pump_source, daemon=True)
    output_thread = threading.Thread(target=collect_output, daemon=True)
    stderr_thread = threading.Thread(target=consume_stderr, daemon=True)
    source_thread.start()
    output_thread.start()
    stderr_thread.start()

    try:
        return_code = process.wait()
    finally:
        source_thread.join()
        output_thread.join()
        stderr_thread.join()
        if process_controller is not None:
            process_controller.clear(process_handle)

    if process_controller is not None and process_controller.cancelled:
        raise ProcessCancelledError("Operation was cancelled.")
    return subprocess.CompletedProcess(ssh_cmd, return_code, "", "".join(stderr_lines))


def _ffmpeg_command_with_progress(command: list[str]) -> list[str]:
    if command[:3] == [command[0], *ENCODE_PROGRESS_ARGS[:2]]:
        return command
    return [command[0], *ENCODE_PROGRESS_ARGS, *command[1:]]


def _run_tracked_encode_command(
        ffmpeg_cmd: list[str],
        *,
        process_controller: ManagedProcessController | None,
        progress_callback: Callable[[dict[str, Any]], None] | None,
) -> subprocess.CompletedProcess[str]:
    return _run_tracked_process(
        _ffmpeg_command_with_progress(ffmpeg_cmd),
        process_controller=process_controller,
        progress_callback=progress_callback,
    )


def _run_tracked_process(
        cmd: list[str],
        *,
        process_controller: ManagedProcessController | None,
        progress_callback: Callable[[dict[str, Any]], None] | None,
) -> subprocess.CompletedProcess[str]:
    if process_controller is None and progress_callback is None:
        return run_command(cmd)

    process_controller.throw_if_cancelled() if process_controller is not None else None
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    if process_controller is not None:
        process_controller.attach(process)

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    progress_state: dict[str, str] = {}
    start_time = time.monotonic()

    def consume_stdout() -> None:
        if process.stdout is None:
            return
        for line in iter(process.stdout.readline, ""):
            stdout_lines.append(line)
        process.stdout.close()

    def consume_stderr() -> None:
        if process.stderr is None:
            return
        for line in iter(process.stderr.readline, ""):
            stderr_lines.append(line)
            if progress_callback is None:
                continue
            snapshot = _update_ffmpeg_progress_state(progress_state, line,
                                                     elapsed_seconds=time.monotonic() - start_time)
            if snapshot is not None:
                progress_callback(snapshot)
        process.stderr.close()

    stdout_thread = threading.Thread(target=consume_stdout, daemon=True)
    stderr_thread = threading.Thread(target=consume_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    try:
        return_code = process.wait()
    finally:
        stdout_thread.join()
        stderr_thread.join()
        if process_controller is not None:
            process_controller.clear(process)

    if process_controller is not None and process_controller.cancelled:
        raise ProcessCancelledError("Operation was cancelled.")
    return subprocess.CompletedProcess(cmd, return_code, "".join(stdout_lines), "".join(stderr_lines))


def _update_ffmpeg_progress_state(
        progress_state: dict[str, str],
        line: str,
        *,
        elapsed_seconds: float,
) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    progress_state[key] = value
    if key != "progress":
        return None
    return {
        "fps": _progress_float(progress_state.get("fps")),
        "speed": _progress_speed(progress_state.get("speed")),
        "out_time_seconds": _progress_out_time_seconds(progress_state),
        "elapsed_seconds": max(elapsed_seconds, 0.0),
        "progress_state": value,
    }


def _progress_float(value: str | None) -> float | None:
    try:
        return float(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def _progress_speed(value: str | None) -> float | None:
    text = str(value or "").strip().lower().rstrip("x")
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _progress_out_time_seconds(progress_state: dict[str, str]) -> float | None:
    raw_microseconds = progress_state.get("out_time_us") or progress_state.get("out_time_ms")
    if raw_microseconds:
        try:
            return float(raw_microseconds) / 1_000_000.0
        except (TypeError, ValueError):
            return None
    text = str(progress_state.get("out_time") or "").strip()
    if not text:
        return None
    try:
        hours, minutes, seconds = text.split(":", 2)
        return (int(hours) * 3600) + (int(minutes) * 60) + float(seconds)
    except (TypeError, ValueError):
        return None


def _select_streams(item: dict[str, Any]) -> dict[str, Any]:
    audio_tracks = item["audio_summary"]
    subtitle_tracks = item["subtitle_summary"]

    selected_audio = _pick_audio(audio_tracks)
    selected_subtitles = _pick_subtitles(subtitle_tracks,
                                         bool(item["resolved_policy"]["subtitle"].get("prefer_text", True)))

    return {
        "audio_tracks": [selected_audio],
        "subtitle_tracks": selected_subtitles,
    }


def _pick_audio(audio_tracks: list[dict[str, Any]]) -> dict[str, Any]:
    english = [track for track in audio_tracks if track.get("language") == "eng"]
    candidates = english or [track for track in audio_tracks if track.get("language") in {None, "und"}] or audio_tracks
    if not candidates:
        raise ValueError("No audio tracks available")
    return sorted(candidates, key=lambda track: (-int(track.get("default") or 0), -(int(track.get("channels") or 0)),
                                                 int(track["index"])))[0]


def _pick_subtitles(subtitle_tracks: list[dict[str, Any]], prefer_text: bool) -> list[dict[str, Any]]:
    english = [track for track in subtitle_tracks if track.get("language") == "eng"]
    if not english:
        fallback = [track for track in subtitle_tracks if track.get("language") in {None, "und"}]
        if fallback and not any(track.get("language") not in {None, "und", "eng"} for track in subtitle_tracks):
            english = fallback
        else:
            return []

    def sort_key(track: dict[str, Any]) -> tuple[int, int, int, int]:
        codec = str(track.get("codec_name") or "")
        is_text = codec in TEXT_SUBTITLE_CODECS
        return (
            0 if (prefer_text and is_text and not track.get("forced")) else 1,
            0 if (not track.get("forced")) else 1,
            0 if track.get("default") else 1,
            int(track["index"]),
        )

    ordered = sorted(english, key=sort_key)
    forced = [track for track in ordered if track.get("forced")]
    full = [track for track in ordered if not track.get("forced")]
    return full[:1] + forced + full[1:]


def _source_has_preservable_subtitles(subtitle_tracks: list[dict[str, Any]]) -> bool:
    if any(track.get("language") == "eng" for track in subtitle_tracks):
        return True
    untagged = [track for track in subtitle_tracks if track.get("language") in {None, "und"}]
    return bool(untagged) and not any(track.get("language") not in {None, "und", "eng"} for track in subtitle_tracks)


def _audio_codec(track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    codec = str(track.get("codec_name") or "").lower()
    if codec in {str(name).lower() for name in audio_policy.get("copy_codecs", [])}:
        return "copy"
    if codec in {str(name).lower() for name in audio_policy.get("convert_to_opus_codecs", [])}:
        return "libopus"
    return "copy"


def _opus_bitrate(track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    channels = int(track.get("channels") or 2)
    if channels >= 8:
        return str(audio_policy["surround_7_1_opus_bitrate"])
    if channels >= 6:
        return str(audio_policy["surround_5_1_opus_bitrate"])
    return str(audio_policy["stereo_opus_bitrate"])


def _opus_layout_filter(track: dict[str, Any]) -> str | None:
    channels = int(track.get("channels") or 2)
    if channels >= 8:
        return "channelmap=channel_layout=7.1"
    if channels >= 6:
        return "channelmap=channel_layout=5.1"
    return None


def _check(validation: dict[str, Any], passed: bool, message: str) -> None:
    validation["checks"].append({"passed": passed, "message": message})
    validation["passed"] = validation["passed"] and passed


def _estimate_audio_track_bytes(track: dict[str, Any], audio_policy: dict[str, Any], duration_seconds: float) -> int:
    codec = _audio_codec(track, audio_policy)
    if codec == "libopus":
        bitrate_text = _opus_bitrate(track, audio_policy)
        bitrate_bps = _parse_bitrate_text(bitrate_text)
    else:
        bitrate_bps = int(track.get("bit_rate") or 0)
        if bitrate_bps <= 0:
            channels = int(track.get("channels") or 2)
            bitrate_bps = 640_000 if channels >= 6 else 192_000
    return int((bitrate_bps / 8) * duration_seconds)


def _estimate_subtitle_track_bytes(track: dict[str, Any]) -> int:
    bit_rate = int(track.get("bit_rate") or 0)
    if bit_rate > 0:
        duration_seconds = float(track.get("duration_seconds") or 0.0)
        if duration_seconds > 0:
            return int((bit_rate / 8) * duration_seconds)
    codec = str(track.get("codec_name") or "").lower()
    if codec in TEXT_SUBTITLE_CODECS:
        return 128 * 1024
    return 4 * 1024 * 1024


def _parse_bitrate_text(value: str) -> int:
    stripped = value.strip().lower()
    if stripped.endswith("k"):
        return int(float(stripped[:-1]) * 1000)
    if stripped.endswith("m"):
        return int(float(stripped[:-1]) * 1_000_000)
    return int(float(stripped))


def _record_event(connection: sqlite3.Connection, library_item_id: int, event_type: str,
                  details: dict[str, Any]) -> None:
    connection.execute(
        "INSERT INTO item_events(library_item_id, created_at, event_type, details_json) VALUES (?, ?, ?, ?)",
        (library_item_id, timestamp(), event_type, json.dumps(details, separators=(",", ":"))),
    )


def _format_crf(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}"
