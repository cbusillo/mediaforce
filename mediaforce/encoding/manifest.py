import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.core.process_control import ProcessCancelledError
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.encoding.cadence import CadenceResolutionError, cadence_filter
from mediaforce.encoding.quality import quality_error_message, resolve_local_quality_temp_root
from mediaforce.encoding.staging import safe_unlink
from mediaforce.encoding.streams import ProductionStreamPlan
from mediaforce.tuning.target_size_search import (
    FinalSizeMissError,
    FinalSizeVerification,
    TargetSizeSearchError,
    retry_quality_result_for_final_miss,
    target_trace_with_actual_output,
    verify_final_output_size,
)
from mediaforce.tuning.stream_budget import StreamBudgetLedger

LOGGER = logging.getLogger(__name__)


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
        encode_context: dict[str, Any] | None = None,
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
            encode_context=encode_context,
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
    cadence_decision = object_dict(item.get("cadence_decision"))
    ledger = _persisted_stream_budget(item)
    selection = ledger.stream_plan if ledger is not None else select_streams(item)
    quality_metric, _ = select_quality_metric(str(policy["video"].get("quality_metric", "auto")))
    if isinstance(selection, ProductionStreamPlan):
        if selection.audio_streams:
            selected_audio_plan = selection.audio_streams[0]
            audio_plan = {
                "source_codec": selected_audio_plan.source_codec,
                "channels": selected_audio_plan.channels,
                "language": selected_audio_plan.language or "und",
                "action": "convert" if selected_audio_plan.action == "transcode" else "copy",
                "output_codec": selected_audio_plan.output_codec,
                "output_bitrate": selected_audio_plan.output_bitrate_text,
                "source_track_count": len(object_list(item.get("audio_summary"))),
                "kept_track_count": len(selection.audio_streams),
            }
        else:
            audio_plan = {
                "source_codec": None,
                "channels": None,
                "language": None,
                "action": "none",
                "output_codec": None,
                "output_bitrate": None,
                "source_track_count": 0,
                "kept_track_count": 0,
            }
        subtitle_languages = [stream.language or "und" for stream in selection.subtitle_streams]
        subtitle_codecs = [stream.source_codec for stream in selection.subtitle_streams]
        kept_subtitle_count = len(selection.subtitle_streams)
        attachment_plan = {
            "source_track_count": len(object_list(item.get("attachment_summary"))),
            "kept_track_count": len([stream for stream in selection.attachment_streams if stream.action == "copy"]),
            "copy_unknown_source_tracks": selection.copy_unknown_attachments,
        }
    else:
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
        subtitle_languages = [track.get("language") or "und" for track in subtitle_tracks]
        subtitle_codecs = [track.get("codec_name") or "unknown" for track in subtitle_tracks]
        kept_subtitle_count = len(subtitle_tracks)
        attachment_plan = {
            "source_track_count": len(object_list(item.get("attachment_summary"))),
            "kept_track_count": 0,
            "copy_unknown_source_tracks": False,
        }
    return {
        "video": {
            "source_codec": item.get("video_codec") or "unknown",
            "output_codec": "av1",
            "quality_metric": quality_metric,
            "target": float_value(policy["video"]["target_vmaf" if quality_metric == "vmaf" else "target_xpsnr"]),
            "min_target": float_value(policy["video"]["min_target_vmaf" if quality_metric == "vmaf" else "min_target_xpsnr"]),
            "max_encoded_percent": float_value(policy["video"].get("max_encoded_percent", 100)),
            "default_grain": int_value(policy["video"].get("default_grain", 0)),
            "cadence": cadence_decision,
        },
        "audio": audio_plan,
        "subtitles": {
            "source_track_count": len(object_list(item.get("subtitle_summary"))),
            "kept_track_count": kept_subtitle_count,
            "languages": subtitle_languages,
            "codecs": subtitle_codecs,
        },
        "attachments": attachment_plan,
        "stream_plan_id": selection.plan_id if isinstance(selection, ProductionStreamPlan) else None,
        "stream_budget_ledger": ledger.to_payload() if ledger is not None else None,
    }


def _persist_event_best_effort(
        connection: DBClient,
        library_item_id: int,
        event_type: str,
        details: dict[str, Any],
        record_event: Callable[[DBClient, int, str, dict[str, Any]], None],
) -> list[str]:
    errors: list[str] = []
    try:
        record_event(connection, library_item_id, event_type, details)
        connection.commit()
    except Exception as persistence_error:
        errors.append(f"Failed to persist {event_type} event: {persistence_error}")
        try:
            connection.rollback()
        except Exception as rollback_error:
            errors.append(f"Failed to roll back event transaction: {rollback_error}")
    return errors


def _persist_failure_event_without_masking(
        connection: DBClient,
        library_item_id: int,
        event_type: str,
        details: dict[str, Any],
        primary_error: Exception,
        record_event: Callable[[DBClient, int, str, dict[str, Any]], None],
) -> None:
    for note in _persist_event_best_effort(
            connection,
            library_item_id,
            event_type,
            details,
            record_event,
    ):
        primary_error.add_note(note)


def _cleanup_paths_without_masking(primary_error: Exception, *paths: Path) -> None:
    for path in paths:
        try:
            safe_unlink(path)
        except Exception as cleanup_error:
            primary_error.add_note(f"Failed to remove {path}: {cleanup_error}")


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
        encode_context: dict[str, Any] | None = None,
        resolve_item_source_path: Callable[..., Path],
        resolve_item_quality_source_path: Callable[..., Path],
        resolve_item_staging_path: Callable[..., Path],
        effective_video_preset: Callable[..., int],
        search_quality: Callable[..., Any],
        measure_quality_candidate: Callable[..., Any],
        select_streams: Callable[[dict[str, Any]], dict[str, Any]],
        build_ffmpeg_command: Callable[..., list[str]],
        detect_video_crop: Callable[..., str | None] | None,
        timestamp: Callable[[], str],
        record_event: Callable[[DBClient, int, str, dict[str, Any]], None],
        run_encode_command: Callable[..., Any],
        finalize_output_path: Callable[[Path, Path], None],
        probe_media: Callable[[Path], Any],
        file_fingerprint: Callable[[Path, Any, float | None], str],
        encode_result_factory: Callable[..., Any],
) -> Any:
    connection.commit()
    source_path = resolve_item_source_path(config, item, host=host)
    quality_source_path = resolve_item_quality_source_path(config, item, host=host)
    staging_path = resolve_item_staging_path(config, item, host=host)
    staging_path.parent.mkdir(parents=True, exist_ok=True)

    if staging_path.exists() and not overwrite:
        raise FileExistsError(f"Staging file already exists: {staging_path}")

    policy = item["resolved_policy"]
    cadence_decision = (
        object_dict(item.get("cadence_decision"))
        if "cadence_decision" in item
        else None
    )
    cadence_evidence = (
        object_dict(item.get("cadence_evidence"))
        if "cadence_evidence" in item
        else None
    )
    current_source_fingerprint = str(item.get("source_fingerprint") or "") or None
    try:
        current_source_fingerprint = file_fingerprint(
            source_path,
            source_path.stat(),
            float_value(item.get("duration_seconds")) or None,
        )
    except OSError:
        pass
    if cadence_decision is not None:
        try:
            cadence_filter(
                cadence_decision,
                cadence_evidence,
                source_fingerprint=current_source_fingerprint,
            )
        except CadenceResolutionError as exc:
            blocked_at = timestamp()
            _persist_failure_event_without_masking(
                connection,
                item["library_item_id"],
                "encoding_failed",
                {
                    **_encode_event_details(
                        manifest_path=manifest_path,
                        index=index,
                        item=item,
                        source_path=source_path,
                        host=host,
                        encode_context=encode_context,
                    ),
                    "encode_completed_at": blocked_at,
                    "failure_kind": "cadence_unresolved",
                    "error": str(exc),
                },
                exc,
                record_event,
            )
            raise
    width = int_value(item.get("width")) or None
    height = int_value(item.get("height")) or None
    detected_crop = None
    if detect_video_crop is not None:
        detected_crop = detect_video_crop(
            source_path,
            policy["video"],
            source_codec=str(item.get("video_codec") or ""),
            width=width,
            height=height,
            duration_seconds=item.get("duration_seconds"),
            process_controller=process_controller,
            host=host,
        )
    preset = effective_video_preset(policy["video"], width=width, height=height)
    if progress_callback is not None:
        progress_callback(
            {
                "progress_state": "quality_search",
                "phase_label": "Searching quality",
                "fps": None,
                "speed": None,
                "eta_seconds": None,
                "elapsed_seconds": 0.0,
                "out_time_seconds": 0.0,
            }
        )
    quality_search_host = (
        {**host, "media_access": "mounted"}
        if quality_source_path != source_path and isinstance(host, dict)
        else host
    )
    stream_budget = _persisted_stream_budget(item)
    try:
        quality_result = search_quality(
            quality_source_path,
            policy["video"],
            source_codec=str(item.get("video_codec") or ""),
            width=width,
            height=height,
            detected_crop=detected_crop,
            cadence_decision=cadence_decision,
            cadence_evidence=cadence_evidence,
            cadence_source_fingerprint=current_source_fingerprint,
            process_controller=process_controller,
            host=quality_search_host,
            quality_temp_dir=_quality_temp_dir_for_encode_host(config, quality_search_host),
            stream_budget_ledger=stream_budget,
        )
    except TargetSizeSearchError as exc:
        blocked_at = timestamp()
        _persist_failure_event_without_masking(
            connection,
            item["library_item_id"],
            "encoding_needs_review",
            {
                **_encode_event_details(
                    manifest_path=manifest_path,
                    index=index,
                    item=item,
                    source_path=source_path,
                    host=host,
                    encode_context=encode_context,
                ),
                "encode_completed_at": blocked_at,
                "failure_kind": f"target_size_{exc.status}",
                "error": quality_error_message(exc),
                "target_size_trace": exc.trace,
            },
            exc,
            record_event,
        )
        raise
    selection = stream_budget.stream_plan if stream_budget is not None else select_streams(item)
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
        width=width,
        height=height,
        detected_crop=detected_crop,
        cadence_decision=cadence_decision,
        cadence_evidence=cadence_evidence,
        cadence_source_fingerprint=current_source_fingerprint,
    )

    started_at = timestamp()
    start_monotonic = time.monotonic()
    encode_event_details = _encode_event_details(
        manifest_path=manifest_path,
        index=index,
        item=item,
        source_path=source_path,
        host=host,
        encode_context=encode_context,
    )
    record_event(connection, item["library_item_id"], "encoding_started", {**encode_event_details, "encode_started_at": started_at})
    connection.execute(
        update(library_items)
        .where(library_items.c.id == item["library_item_id"])
        .values(status="encoding", updated_at=started_at)
    )
    connection.commit()

    temp_output = staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}")
    if temp_output.exists():
        safe_unlink(temp_output)
    if staging_path.exists() and overwrite:
        safe_unlink(staging_path)

    final_verification: FinalSizeVerification | None = None
    final_trace = object_dict(getattr(quality_result, "target_size_trace", None)) or None
    retry_count = 0
    try:
        while True:
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
            if stream_budget is None or stream_budget.total_target_bytes is None:
                final_verification = None
                break
            final_verification = verify_final_output_size(stream_budget, staging_path.stat().st_size, retry_count=retry_count)
            final_trace = target_trace_with_actual_output(
                object_dict(getattr(quality_result, "target_size_trace", None)) or None,
                final_verification,
                retry_count=retry_count,
            ) or final_trace
            if final_verification.passed:
                break
            safe_unlink(staging_path)
            quality_result.target_size_trace = final_trace

            def measure_retry_candidate(crf: float) -> Any:
                if progress_callback is not None:
                    progress_callback(
                        {
                            "progress_state": "quality_search",
                            "phase_label": "Measuring retry",
                            "fps": None,
                            "speed": None,
                            "eta_seconds": None,
                            "elapsed_seconds": 0.0,
                            "out_time_seconds": 0.0,
                        }
                    )
                return measure_quality_candidate(
                    quality_source_path,
                    policy["video"],
                    crf=crf,
                    source_codec=str(item.get("video_codec") or ""),
                    width=width,
                    height=height,
                    detected_crop=detected_crop,
                    cadence_decision=cadence_decision,
                    cadence_evidence=cadence_evidence,
                    cadence_source_fingerprint=current_source_fingerprint,
                    process_controller=process_controller,
                    host=quality_search_host,
                    quality_temp_dir=_quality_temp_dir_for_encode_host(config, quality_search_host),
                )

            retry_quality = retry_quality_result_for_final_miss(
                quality_result,
                final_verification,
                measure_candidate=measure_retry_candidate,
            )
            final_trace = object_dict(quality_result.target_size_trace) or final_trace
            retry_payload = final_verification.to_payload()
            retry_payload.update(
                {
                    **encode_event_details,
                    "encode_started_at": started_at,
                    "retry_count": retry_count,
                    "target_size_trace": final_trace,
                }
            )
            if retry_quality is None:
                raise FinalSizeMissError(_final_size_miss_message(final_verification))
            final_trace = object_dict(retry_quality.target_size_trace) or final_trace
            retry_payload["target_size_trace"] = final_trace
            retry_count += 1
            retry_payload["retry_count"] = retry_count
            retry_payload["next_crf"] = retry_quality.crf
            persistence_errors = _persist_event_best_effort(
                connection,
                item["library_item_id"],
                "encoding_target_size_retry",
                retry_payload,
                record_event,
            )
            if persistence_errors:
                final_trace = {
                    **(object_dict(final_trace) or {}),
                    "event_persistence_errors": [
                        *object_list(object_dict(final_trace).get("event_persistence_errors")),
                        *persistence_errors,
                    ],
                }
                retry_quality.target_size_trace = final_trace
            safe_unlink(temp_output)
            quality_result = retry_quality
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
                width=width,
                height=height,
                detected_crop=detected_crop,
                cadence_decision=cadence_decision,
                cadence_evidence=cadence_evidence,
                cadence_source_fingerprint=current_source_fingerprint,
            )
    except Exception as exc:
        failed_at = timestamp()
        if isinstance(exc, FinalSizeMissError):
            event_type = "encoding_needs_review"
        else:
            event_type = "encoding_stopped" if isinstance(exc, ProcessCancelledError) else "encoding_failed"
        _persist_failure_event_without_masking(
            connection,
            item["library_item_id"],
            event_type,
            {
                **encode_event_details,
                "encode_started_at": started_at,
                "encode_completed_at": failed_at,
                "encode_duration_seconds": round(max(time.monotonic() - start_monotonic, 0.0), 3),
                "failure_kind": _failure_kind_for_exception(exc, final_verification),
                "error": quality_error_message(exc),
                "target_size_verification": final_verification.to_payload() if final_verification is not None else None,
                "target_size_trace": final_trace,
            },
            exc,
            record_event,
        )
        _cleanup_paths_without_masking(exc, temp_output, staging_path)
        raise

    staged_stat = staging_path.stat()
    staged_probe = probe_media(staging_path)
    staged_fingerprint = file_fingerprint(staging_path, staged_stat, staged_probe.duration_seconds)
    now = timestamp()
    encode_duration_seconds = round(max(time.monotonic() - start_monotonic, 0.0), 3)
    source_size_bytes = int(item["source_size_bytes"])
    bytes_saved = source_size_bytes - staged_stat.st_size
    size_ratio = (staged_stat.st_size / source_size_bytes) if source_size_bytes else None

    staged_values = {
        "library_item_id": item["library_item_id"],
        "manifest_run_id": manifest["run_id"],
        "manifest_path": str(manifest_path),
        "item_index": index,
        "encode_origin": _encode_context_text(encode_context, "origin"),
        "encode_job_id": _encode_context_text(encode_context, "encode_job_id"),
        "encode_worker_id": _encode_context_text(encode_context, "encode_worker_id"),
        "encode_host_key": _host_text(host, "key", fallback_key="host"),
        "encode_host_label": _host_text(host, "label", fallback_key="key"),
        "encode_host_mode": _host_text(host, "mode"),
        "encode_media_access": _host_text(host, "media_access"),
        "source_path": str(source_path),
        "source_rel_path": _item_text(item, "rel_path"),
        "source_size_bytes": source_size_bytes,
        "source_duration_seconds": _item_float(item, "duration_seconds"),
        "source_video_codec": _item_text(item, "video_codec"),
        "source_fingerprint": item["source_fingerprint"],
        "encode_started_at": started_at,
        "encode_completed_at": now,
        "encode_duration_seconds": encode_duration_seconds,
        "staging_path": str(staging_path),
        "staging_size_bytes": staged_stat.st_size,
        "staging_mtime_ns": staged_stat.st_mtime_ns,
        "staging_fingerprint": staged_fingerprint,
        "bytes_saved": bytes_saved,
        "size_ratio": round(size_ratio, 6) if size_ratio is not None else None,
        "chosen_crf": quality_result.crf,
        "quality_metric": quality_result.metric,
        "quality_target": quality_result.target,
        "quality_score": quality_result.score,
        "validation_json": json.dumps(_target_validation_payload(final_verification, final_trace), separators=(",", ":")),
        "encode_command_json": json.dumps(ffmpeg_cmd, separators=(",", ":")),
        "audio_summary_json": staged_probe.audio_summary_json,
        "subtitle_summary_json": staged_probe.subtitle_summary_json,
        "attachment_summary_json": staged_probe.attachment_summary_json,
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
    connection.commit()
    completion_event_errors = _persist_event_best_effort(
        connection,
        item["library_item_id"],
        "encoding_completed",
        {
            **encode_event_details,
            "encode_started_at": started_at,
            "encode_completed_at": now,
            "encode_duration_seconds": encode_duration_seconds,
            "staging_path": str(staging_path),
            "staging_size_bytes": staged_stat.st_size,
            "source_size_bytes": source_size_bytes,
            "bytes_saved": bytes_saved,
            "size_ratio": round(size_ratio, 6) if size_ratio is not None else None,
            "chosen_crf": quality_result.crf,
            "quality_metric": quality_result.metric,
            "quality_target": quality_result.target,
            "quality_score": quality_result.score,
            "target_size_verification": final_verification.to_payload() if final_verification is not None else None,
            "target_size_trace": final_trace,
        },
        record_event,
    )
    for event_error in completion_event_errors:
        LOGGER.warning("%s", event_error)

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


def _persisted_stream_budget(item: Mapping[str, Any]) -> StreamBudgetLedger | None:
    payload = object_dict(item.get("stream_budget_ledger"))
    if not payload:
        return None
    ledger = StreamBudgetLedger.from_payload(payload)
    ledger.validate_item(item)
    return ledger


def _encode_event_details(
        *,
        manifest_path: Path,
        index: int,
        item: dict[str, Any],
        source_path: Path,
        host: dict[str, Any] | None,
        encode_context: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "manifest": str(manifest_path),
        "item_index": index,
        "source_path": str(source_path),
        "source_rel_path": _item_text(item, "rel_path"),
        "source_size_bytes": int(item["source_size_bytes"]),
        "source_duration_seconds": _item_float(item, "duration_seconds"),
        "source_video_codec": _item_text(item, "video_codec"),
        "encode_origin": _encode_context_text(encode_context, "origin"),
        "encode_owner_pid": _encode_context_int(encode_context, "owner_pid"),
        "encode_job_id": _encode_context_text(encode_context, "encode_job_id"),
        "encode_worker_id": _encode_context_text(encode_context, "encode_worker_id"),
        "encode_host_key": _host_text(host, "key", fallback_key="host"),
        "encode_host_label": _host_text(host, "label", fallback_key="key"),
        "encode_host_mode": _host_text(host, "mode"),
        "encode_media_access": _host_text(host, "media_access"),
    }


def _target_validation_payload(
        verification: FinalSizeVerification | None,
        trace: dict[str, Any] | None,
) -> dict[str, Any]:
    if verification is None:
        return {"passed": True, "target_size_verification": None, "target_size_trace": trace}
    return {
        "passed": verification.passed,
        "target_size_verification": verification.to_payload(),
        "target_size_trace": trace,
    }


def _final_size_miss_message(verification: FinalSizeVerification) -> str:
    return (
        "Final output size missed the approved target band: "
        f"status={verification.status}, actual={verification.actual_output_bytes}, "
        f"target={verification.target_size_bytes}, lower={verification.lower_bound_bytes}, "
        f"upper={verification.upper_bound_bytes}."
    )


def _failure_kind_for_exception(exc: BaseException, verification: FinalSizeVerification | None) -> str:
    if isinstance(exc, ProcessCancelledError):
        return "cancelled"
    if verification is not None and not verification.passed:
        return "target_size_needs_review"
    return "deterministic"


def _quality_temp_dir_for_encode_host(config: MediaforceConfig, host: dict[str, Any] | None) -> Path:
    if isinstance(host, dict) and str(host.get("media_access") or "").strip().lower() == "stream":
        return resolve_local_quality_temp_root(
            config.staging_root,
            config.paths.web_state_dir / "quality-temp",
        )
    return config.staging_root_for_host(host)


def _encode_context_text(encode_context: dict[str, Any] | None, key: str) -> str | None:
    payload = object_dict(encode_context)
    value = str(payload.get(key) or "").strip()
    return value or None


def _encode_context_int(encode_context: dict[str, Any] | None, key: str) -> int | None:
    value = int_value(object_dict(encode_context).get(key))
    return value or None


def _host_text(host: dict[str, Any] | None, key: str, *, fallback_key: str | None = None) -> str | None:
    payload = object_dict(host)
    value = str(payload.get(key) or "").strip()
    if not value and fallback_key is not None:
        value = str(payload.get(fallback_key) or "").strip()
    return value or None


def _item_text(item: dict[str, Any], key: str) -> str | None:
    value = str(item.get(key) or "").strip()
    return value or None


def _item_float(item: dict[str, Any], key: str) -> float | None:
    value = item.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
