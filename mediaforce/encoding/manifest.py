import json
from pathlib import Path
from typing import Any, Callable, Protocol

from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list


class SupportsCancellation(Protocol):
    def throw_if_cancelled(self) -> None:
        ...


def encode_manifest_items(
        connection: DBClient,
        config: MediaforceConfig,
        manifest_path: Path,
        manifest: dict[str, Any],
        indexes: list[int],
        overwrite: bool,
        process_controller: SupportsCancellation | None = None,
        host: dict[str, Any] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        encode_one_item_fn: Callable[..., Any] | None = None,
) -> list[Any]:
    if encode_one_item_fn is None:
        raise RuntimeError("encode_one_item dependency is required")
    results: list[Any] = []
    manifest_items = [object_dict(item) for item in object_list(manifest.get("items"))]
    total_duration_seconds = sum(float_value(manifest_items[index].get("duration_seconds")) for index in indexes)
    completed_duration_seconds = 0.0
    for index in indexes:
        controller = process_controller
        if controller is not None:
            controller.throw_if_cancelled()
        item = manifest_items[index]
        item_duration_seconds = float_value(item.get("duration_seconds"))

        def item_progress(snapshot: dict[str, Any]) -> None:
            if progress_callback is None:
                return
            current_out_time_seconds = min(float_value(snapshot.get("out_time_seconds")), item_duration_seconds)
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
                payload["percent_complete"] = min(overall_completed_duration_seconds / total_duration_seconds, 1.0) * 100.0
            speed_value = float_value(payload.get("speed"))
            if speed_value > 0.0:
                remaining_duration_seconds = float_value(payload.get("remaining_duration_seconds"))
                payload["eta_seconds"] = remaining_duration_seconds / speed_value
            progress_callback(payload)

        result = encode_one_item_fn(
            connection,
            config,
            manifest_path,
            manifest,
            index,
            item,
            overwrite=overwrite,
            process_controller=controller,
            host=host,
            progress_callback=item_progress,
        )
        results.append(result)
        completed_duration_seconds += item_duration_seconds
    return results


def describe_item_plan(
        item: dict[str, Any],
        *,
        select_streams: Callable[[dict[str, Any]], dict[str, Any]],
        select_quality_metric: Callable[[str], tuple[str, float]],
        audio_codec: Callable[[dict[str, Any], dict[str, Any]], str],
        opus_bitrate: Callable[[dict[str, Any], dict[str, Any]], str],
) -> dict[str, Any]:
    policy = item["resolved_policy"]
    selection = select_streams(item)
    quality_metric, _ = select_quality_metric(str(policy["video"].get("quality_metric", "auto")))
    selected_audio = object_dict(selection["audio_tracks"][0])
    chosen_audio_codec = audio_codec(selected_audio, policy["audio"])
    audio_plan = {
        "source_codec": str(selected_audio.get("codec_name") or "unknown"),
        "channels": int_value(selected_audio.get("channels")),
        "language": selected_audio.get("language") or "und",
        "action": "convert" if chosen_audio_codec == "libopus" else "copy",
        "output_codec": "opus" if chosen_audio_codec == "libopus" else str(selected_audio.get("codec_name") or "unknown"),
        "output_bitrate": opus_bitrate(selected_audio, policy["audio"]) if chosen_audio_codec == "libopus" else None,
        "source_track_count": len(object_list(item.get("audio_summary"))),
        "kept_track_count": len(selection["audio_tracks"]),
    }
    subtitle_tracks = selection["subtitle_tracks"]
    return {
        "video": {
            "source_codec": item.get("video_codec") or "unknown",
            "output_codec": "av1",
            "quality_metric": quality_metric,
            "target": float_value(policy["video"]["target_vmaf" if quality_metric == "vmaf" else "target_xpsnr"]),
            "min_target": float_value(policy["video"]["min_target_vmaf" if quality_metric == "vmaf" else "min_target_xpsnr"]),
            "max_encoded_percent": float_value(policy["video"].get("max_encoded_percent", 100)),
            "default_grain": int_value(policy["video"].get("default_grain", 0)),
        },
        "audio": audio_plan,
        "subtitles": {
            "source_track_count": len(object_list(item.get("subtitle_summary"))),
            "kept_track_count": len(subtitle_tracks),
            "languages": [track.get("language") or "und" for track in subtitle_tracks],
            "codecs": [track.get("codec_name") or "unknown" for track in subtitle_tracks],
        },
    }


def encode_one_item(
        connection: DBClient,
        config: MediaforceConfig,
        manifest_path: Path,
        manifest: dict[str, Any],
        index: int,
        item: dict[str, Any],
        *,
        overwrite: bool,
        process_controller: SupportsCancellation | None = None,
        host: dict[str, Any] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        resolve_item_source_path: Callable[..., Path],
        resolve_item_staging_path: Callable[..., Path],
        effective_video_preset: Callable[..., int],
        search_quality: Callable[..., Any],
        select_streams: Callable[[dict[str, Any]], dict[str, Any]],
        build_ffmpeg_command: Callable[..., list[str]],
        timestamp: Callable[[], str],
        record_event: Callable[[DBClient, int, str, dict[str, Any]], None],
        run_encode_command: Callable[..., Any],
        finalize_output_path: Callable[[Path, Path], None],
        probe_media: Callable[[Path], Any],
        file_fingerprint: Callable[[Path, Any, float | None], str],
        encode_result_factory: Callable[..., Any],
) -> Any:
    source_path = resolve_item_source_path(config, item, host=host)
    staging_path = resolve_item_staging_path(config, item, host=host)
    staging_path.parent.mkdir(parents=True, exist_ok=True)

    if staging_path.exists() and not overwrite:
        raise FileExistsError(f"Staging file already exists: {staging_path}")

    policy = item["resolved_policy"]
    width = int_value(item.get("width")) or None
    height = int_value(item.get("height")) or None
    preset = effective_video_preset(policy["video"], width=width, height=height)
    quality_result = search_quality(
        source_path,
        policy["video"],
        source_codec=str(item.get("video_codec") or ""),
        width=width,
        height=height,
        process_controller=process_controller,
        host=host,
    )
    selection = select_streams(item)
    ffmpeg_cmd = build_ffmpeg_command(
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
    record_event(connection, item["library_item_id"], "encoding_started", {"manifest": str(manifest_path), "item_index": index})
    connection.execute(
        update(library_items)
        .where(library_items.c.id == item["library_item_id"])
        .values(status="encoding", updated_at=started_at)
    )
    connection.commit()

    temp_output = staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}")
    if temp_output.exists():
        temp_output.unlink()
    if staging_path.exists() and overwrite:
        staging_path.unlink()

    try:
        result = run_encode_command(
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
        finalize_output_path(temp_output, staging_path)
    except Exception:
        temp_output.unlink(missing_ok=True)
        staging_path.unlink(missing_ok=True)
        raise

    staged_stat = staging_path.stat()
    staged_probe = probe_media(staging_path)
    staged_fingerprint = file_fingerprint(staging_path, staged_stat, staged_probe.duration_seconds)
    now = timestamp()

    staged_values = {
        "library_item_id": item["library_item_id"],
        "manifest_run_id": manifest["run_id"],
        "manifest_path": str(manifest_path),
        "item_index": index,
        "source_fingerprint": item["source_fingerprint"],
        "staging_path": str(staging_path),
        "staging_size_bytes": staged_stat.st_size,
        "staging_mtime_ns": staged_stat.st_mtime_ns,
        "staging_fingerprint": staged_fingerprint,
        "chosen_crf": quality_result.crf,
        "quality_metric": quality_result.metric,
        "quality_target": quality_result.target,
        "quality_score": quality_result.score,
        "encode_command_json": json.dumps(ffmpeg_cmd, separators=(",", ":")),
        "audio_summary_json": staged_probe.audio_summary_json,
        "subtitle_summary_json": staged_probe.subtitle_summary_json,
        "staged_at": now,
        "updated_at": now,
    }
    connection.execute(
        sqlite_insert(staged_artifacts)
        .values(**staged_values)
        .on_conflict_do_update(
            index_elements=[staged_artifacts.c.library_item_id],
            set_={key: value for key, value in staged_values.items() if key != "library_item_id"},
        )
    )
    connection.execute(
        update(library_items)
        .where(library_items.c.id == item["library_item_id"])
        .values(status="encoded", updated_at=now)
    )
    record_event(
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

    return encode_result_factory(
        staging_path=staging_path,
        source_size_bytes=int(item["source_size_bytes"]),
        staging_size_bytes=staged_stat.st_size,
        chosen_crf=quality_result.crf,
        quality_metric=quality_result.metric,
        quality_target=quality_result.target,
        quality_score=quality_result.score,
        encode_command=ffmpeg_cmd,
    )
