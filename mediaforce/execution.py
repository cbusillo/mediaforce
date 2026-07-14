import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.encoding.commands import build_ffmpeg_command as _build_ffmpeg_command_impl
from mediaforce.encoding.helpers import _build_streaming_remote_ffmpeg_command as _build_streaming_remote_ffmpeg_command_impl, \
    _streaming_output_args_for_path as _streaming_output_args_for_path_impl, \
    build_svt_params as build_svt_params_impl, effective_video_preset as effective_video_preset_impl, \
    resolve_item_quality_source_path as resolve_item_quality_source_path_impl, \
    resolve_item_source_path as resolve_item_source_path_impl, resolve_item_staging_path as resolve_item_staging_path_impl
from mediaforce.encoding.manifest import describe_item_plan as describe_item_plan_impl, \
    encode_manifest_items as encode_manifest_items_impl, encode_one_item as encode_one_item_impl
from mediaforce.encoding.progress import _ffmpeg_command_with_progress as _ffmpeg_command_with_progress_impl, \
    _progress_float as _progress_float_impl, _progress_out_time_seconds as _progress_out_time_seconds_impl, \
    _progress_speed as _progress_speed_impl, _update_ffmpeg_progress_state as _update_ffmpeg_progress_state_impl
from mediaforce.encoding.quality_search import measure_quality_candidate as _measure_quality_candidate_impl, \
    search_quality as _search_quality_impl
from mediaforce.encoding.runner import run_encode_command as _run_encode_command_impl, \
    run_streamed_remote_encode_command as _run_streamed_remote_encode_command_impl, \
    run_tracked_process as _run_tracked_process_impl
from mediaforce.encoding.staging import finalize_output_path as _finalize_output_path_impl, \
    probe_packet_end_seconds, promote_one_item as promote_one_item_impl, remux_container_metadata, \
    validate_one_item as validate_one_item_impl
from mediaforce.encoding.streams import ProductionStreamPlan, _audio_codec as _audio_codec_impl, _check as _check_impl, \
    _format_crf as _format_crf_impl, _opus_bitrate as _opus_bitrate_impl, \
    _opus_layout_filter as _opus_layout_filter_impl, _parse_bitrate_text as _parse_bitrate_text_impl, \
    _pick_audio as _pick_audio_impl, _pick_subtitles as _pick_subtitles_impl, \
    _record_event as _record_event_impl, _select_streams as _select_streams_impl, \
    _source_has_preservable_subtitles as _source_has_preservable_subtitles_impl
from mediaforce.encoding.video_filters import most_common_crop
from mediaforce.encoding.ffmpeg import ffmpeg_hwaccel_input_args
from mediaforce.library.probe import probe_media
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError, run_command
from mediaforce.core.type_defs import float_value, int_value, object_dict
from mediaforce.encoding.quality import QualitySearchResult, SampleEncodeResult, run_crf_search, run_sample_encode, \
    select_quality_metric
from mediaforce.remote import execution_mode_for_host, host_media_access_for_host, remote_shell_path_export_line, \
    run_remote_command, ssh_client_options
from mediaforce.core.utils import file_fingerprint, timestamp
from mediaforce.tuning.stream_budget import StreamBudgetLedger, \
    resolve_stream_budget_ledger as resolve_stream_budget_ledger_impl
from mediaforce.tuning.size_goals import ResolvedSizeGoal

TEXT_SUBTITLE_CODECS = {"ass", "mov_text", "srt", "ssa", "subrip", "text", "webvtt"}
SVT_AV1_MIN_8K_PRESET = 5
SVT_AV1_8K_DIMENSION_THRESHOLD = 7680
ENCODE_PROGRESS_ARGS = ["-progress", "pipe:2", "-nostats"]
VIDEO_CROP_DETECT_TIMEOUT_SECONDS = 90


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
        connection: DBClient,
        config: MediaforceConfig,
        manifest_path: Path,
        manifest: dict[str, Any],
        indexes: list[int],
        overwrite: bool,
        process_controller: ManagedProcessController | None = None,
        host: dict[str, Any] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        encode_context: dict[str, Any] | None = None,
) -> list[EncodeResult]:
    return encode_manifest_items_impl(
        connection,
        config,
        manifest_path,
        manifest,
        indexes,
        overwrite,
        process_controller=process_controller,
        host=host,
        progress_callback=progress_callback,
        encode_context=encode_context,
        encode_one_item_fn=encode_one_item,
    )


def describe_item_plan(item: dict[str, Any]) -> dict[str, Any]:
    return describe_item_plan_impl(
        item,
        select_streams=_select_streams,
        select_quality_metric=select_quality_metric,
        audio_codec=_audio_codec,
        opus_bitrate=_opus_bitrate,
    )


def build_svt_params(video_policy: dict[str, Any]) -> list[str]:
    return build_svt_params_impl(video_policy)


def effective_video_preset(
        video_policy: dict[str, Any],
        *,
        width: int | None = None,
        height: int | None = None,
) -> int:
    return effective_video_preset_impl(
        video_policy,
        width=width,
        height=height,
        svt_av1_8k_dimension_threshold=SVT_AV1_8K_DIMENSION_THRESHOLD,
        svt_av1_min_8k_preset=SVT_AV1_MIN_8K_PRESET,
    )


def resolve_stream_budget_ledger(
        item: dict[str, Any],
        *,
        default_video_policy: dict[str, Any] | None = None,
        output_container: str | None = None,
        resolved_size_goal: ResolvedSizeGoal | None = None,
        prefer_persisted: bool = True,
) -> StreamBudgetLedger:
    return resolve_stream_budget_ledger_impl(
        item,
        default_video_policy=default_video_policy,
        output_container=output_container,
        resolved_size_goal=resolved_size_goal,
        prefer_persisted=prefer_persisted,
    )


def resolve_item_source_path(
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        host: dict[str, Any] | None = None,
) -> Path:
    return resolve_item_source_path_impl(
        config,
        item,
        host=host,
        host_media_access_for_host=host_media_access_for_host,
    )


def resolve_item_staging_path(
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        host: dict[str, Any] | None = None,
) -> Path:
    return resolve_item_staging_path_impl(
        config,
        item,
        host=host,
        host_media_access_for_host=host_media_access_for_host,
    )


def resolve_item_quality_source_path(
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        host: dict[str, Any] | None = None,
) -> Path:
    if host_media_access_for_host(host) != "stream":
        return resolve_item_source_path(config, item, host=host)
    try:
        return resolve_item_quality_source_path_impl(
            config,
            item,
            host=host,
            host_media_access_for_host=host_media_access_for_host,
        )
    except KeyError as exc:
        if exc.args != ("source_path",):
            raise
        return resolve_item_source_path(config, item, host=host)


def encode_one_item(
        connection: DBClient,
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
        encode_context: dict[str, Any] | None = None,
) -> EncodeResult:
    return encode_one_item_impl(
        connection,
        config,
        manifest_path,
        manifest,
        index,
        item,
        overwrite=overwrite,
        process_controller=process_controller,
        host=host,
        progress_callback=progress_callback,
        encode_context=encode_context,
        resolve_item_source_path=resolve_item_source_path,
        resolve_item_quality_source_path=resolve_item_quality_source_path,
        resolve_item_staging_path=resolve_item_staging_path,
        effective_video_preset=effective_video_preset,
        search_quality=_search_quality,
        measure_quality_candidate=_measure_quality_candidate,
        select_streams=_select_streams,
        build_ffmpeg_command=_build_ffmpeg_command,
        detect_video_crop=_detect_video_crop,
        timestamp=timestamp,
        record_event=_record_event,
        run_encode_command=_run_encode_command,
        finalize_output_path=_finalize_output_path,
        probe_media=probe_media,
        file_fingerprint=file_fingerprint,
        encode_result_factory=EncodeResult,
    )


def search_quality_for_source(
        source_path: Path,
        video_policy: dict[str, Any],
        *,
        source_codec: str | None = None,
        width: int | None = None,
        height: int | None = None,
        detected_crop: str | None = None,
        cadence_decision: dict[str, Any] | None = None,
        cadence_evidence: dict[str, Any] | None = None,
        cadence_source_fingerprint: str | None = None,
        process_controller: ManagedProcessController | None = None,
        host: dict[str, Any] | None = None,
        quality_temp_dir: Path | None = None,
        stream_budget_ledger: StreamBudgetLedger | None = None,
) -> QualitySearchResult:
    return _search_quality(
        source_path,
        video_policy,
        source_codec=source_codec,
        width=width,
        height=height,
        detected_crop=detected_crop,
        cadence_decision=cadence_decision,
        cadence_evidence=cadence_evidence,
        cadence_source_fingerprint=cadence_source_fingerprint,
        process_controller=process_controller,
        host=host,
        quality_temp_dir=quality_temp_dir,
        stream_budget_ledger=stream_budget_ledger,
    )


def validate_manifest_items(
        connection: DBClient,
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


def validate_one_item(connection: DBClient, config: MediaforceConfig, item: dict[str, Any]) -> dict[str, Any]:
    return validate_one_item_impl(
        connection,
        config,
        item,
        probe_media=probe_media,
        source_has_preservable_subtitles=_source_has_preservable_subtitles,
        check=_check,
        timestamp=timestamp,
        record_event=_record_event,
        packet_end_probe=probe_packet_end_seconds,
        remux_container=remux_container_metadata,
    )


def promote_manifest_items(
        connection: DBClient,
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


def promote_one_item(connection: DBClient, config: MediaforceConfig, item: dict[str, Any], *,
                     force: bool) -> Path:
    return promote_one_item_impl(
        connection,
        config,
        item,
        force=force,
        probe_media=probe_media,
        file_fingerprint=file_fingerprint,
        timestamp=timestamp,
        record_event=_record_event,
    )


def _search_quality(
        source_path: Path,
        video_policy: dict[str, Any],
        *,
        source_codec: str | None = None,
        width: int | None = None,
        height: int | None = None,
        detected_crop: str | None = None,
        cadence_decision: dict[str, Any] | None = None,
        cadence_evidence: dict[str, Any] | None = None,
        cadence_source_fingerprint: str | None = None,
        process_controller: ManagedProcessController | None = None,
        host: dict[str, Any] | None = None,
        quality_temp_dir: Path | None = None,
        stream_budget_ledger: StreamBudgetLedger | None = None,
) -> QualitySearchResult:
    return _search_quality_impl(
        source_path,
        video_policy,
        source_codec=source_codec,
        width=width,
        height=height,
        detected_crop=detected_crop,
        cadence_decision=cadence_decision,
        cadence_evidence=cadence_evidence,
        cadence_source_fingerprint=cadence_source_fingerprint,
        process_controller=process_controller,
        host=host,
        quality_temp_dir=quality_temp_dir,
        stream_budget_ledger=stream_budget_ledger,
        host_media_access_for_host=host_media_access_for_host,
        select_quality_metric=select_quality_metric,
        build_svt_params=build_svt_params,
        effective_video_preset=effective_video_preset,
        run_crf_search=run_crf_search,
        run_sample_encode=run_sample_encode,
    )


def _measure_quality_candidate(
        source_path: Path,
        video_policy: dict[str, Any],
        *,
        crf: float,
        source_codec: str | None = None,
        width: int | None = None,
        height: int | None = None,
        detected_crop: str | None = None,
        cadence_decision: dict[str, Any] | None = None,
        cadence_evidence: dict[str, Any] | None = None,
        cadence_source_fingerprint: str | None = None,
        process_controller: ManagedProcessController | None = None,
        host: dict[str, Any] | None = None,
        quality_temp_dir: Path | None = None,
) -> SampleEncodeResult:
    return _measure_quality_candidate_impl(
        source_path,
        video_policy,
        crf=crf,
        source_codec=source_codec,
        width=width,
        height=height,
        detected_crop=detected_crop,
        cadence_decision=cadence_decision,
        cadence_evidence=cadence_evidence,
        cadence_source_fingerprint=cadence_source_fingerprint,
        process_controller=process_controller,
        host=host,
        quality_temp_dir=quality_temp_dir,
        host_media_access_for_host=host_media_access_for_host,
        select_quality_metric=select_quality_metric,
        build_svt_params=build_svt_params,
        effective_video_preset=effective_video_preset,
        run_sample_encode=run_sample_encode,
    )


def _build_ffmpeg_command(
        *,
        source_path: Path,
        staging_path: Path,
        source_codec: str | None,
        video_policy: dict[str, Any],
        preset: int,
        audio_policy: dict[str, Any],
        subtitle_policy: dict[str, Any],
        selection: ProductionStreamPlan | dict[str, Any],
        quality: QualitySearchResult,
        host: dict[str, Any] | None = None,
        width: int | None = None,
        height: int | None = None,
        detected_crop: str | None = None,
        cadence_decision: dict[str, Any] | None = None,
        cadence_evidence: dict[str, Any] | None = None,
        cadence_source_fingerprint: str | None = None,
) -> list[str]:
    return _build_ffmpeg_command_impl(
        source_path=source_path,
        staging_path=staging_path,
        source_codec=source_codec,
        video_policy=video_policy,
        preset=preset,
        audio_policy=audio_policy,
        subtitle_policy=subtitle_policy,
        selection=selection,
        quality=quality,
        host=host,
        width=width,
        height=height,
        detected_crop=detected_crop,
        cadence_decision=cadence_decision,
        cadence_evidence=cadence_evidence,
        cadence_source_fingerprint=cadence_source_fingerprint,
        text_subtitle_codecs=TEXT_SUBTITLE_CODECS,
        ffmpeg_binary=ffmpeg_binary,
        ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
        audio_codec=_audio_codec,
        opus_layout_filter=_opus_layout_filter,
        opus_bitrate=_opus_bitrate,
        format_crf=_format_crf,
    )


def _detect_video_crop(
        source_path: Path,
        video_policy: dict[str, Any],
        *,
        source_codec: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_seconds: Any = None,
        process_controller: ManagedProcessController | None = None,
        host: dict[str, Any] | None = None,
) -> str | None:
    handling = str(video_policy.get("black_bar_handling") or video_policy.get("crop_black_bars") or "off").lower()
    if handling not in {"1", "true", "yes", "auto", "smart"}:
        return None

    host_payload = object_dict(host)
    ffmpeg_name = str(host_payload.get("ffmpeg_path") or "").strip() or ffmpeg_binary()
    detect_seconds = max(1, int_value(video_policy.get("black_bar_detect_seconds")) or 12)
    limit = int_value(video_policy.get("black_bar_detect_limit")) or 24
    round_value = int_value(video_policy.get("black_bar_detect_round")) or 2
    stderr_parts: list[str] = []
    try:
        for detect_start in _cropdetect_start_seconds(duration_seconds, video_policy):
            cmd = [
                ffmpeg_name,
                "-hide_banner",
                *ffmpeg_hwaccel_input_args(
                    source_codec,
                    platform_name=str(host_payload.get("platform") or "") or None,
                    videotoolbox_available=host_payload.get("videotoolbox_available")
                    if "videotoolbox_available" in host_payload else None,
                ),
                "-ss",
                f"{detect_start:.3f}",
                "-i",
                str(source_path),
                "-t",
                str(detect_seconds),
                "-vf",
                f"cropdetect=limit={limit}:round={round_value}:reset=0",
                "-f",
                "null",
                "-",
            ]
            if execution_mode_for_host(host) == "ssh" and host_media_access_for_host(host) != "stream":
                remote_cmd = list(cmd)
                remote_cmd[0] = Path(remote_cmd[0]).name
                result = run_remote_command(host_payload, remote_cmd, VIDEO_CROP_DETECT_TIMEOUT_SECONDS)
            else:
                result = run_command(cmd, process_controller=process_controller)
            stderr_parts.append(result.stderr or "")
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    return most_common_crop("\n".join(stderr_parts), source_width=width, source_height=height)


def detect_video_crop(
        source_path: Path,
        video_policy: dict[str, Any],
        *,
        source_codec: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_seconds: Any = None,
        process_controller: ManagedProcessController | None = None,
        host: dict[str, Any] | None = None,
) -> str | None:
    return _detect_video_crop(
        source_path,
        video_policy,
        source_codec=source_codec,
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        process_controller=process_controller,
        host=host,
    )


def _cropdetect_start_seconds(duration_seconds: Any, video_policy: dict[str, Any]) -> list[float]:
    configured = float_value(video_policy.get("black_bar_detect_start_seconds"))
    if configured > 0:
        return [configured]
    duration = float_value(duration_seconds)
    if duration <= 0:
        return [60.0]
    sample_count = max(1, min(int_value(video_policy.get("black_bar_detect_samples")) or 3, 5))
    return [max(0.0, min(duration * fraction, max(duration - 30.0, 0.0))) for fraction in _sample_fractions(sample_count)]


def _sample_fractions(sample_count: int) -> list[float]:
    if sample_count == 1:
        return [0.35]
    step = 0.6 / float(sample_count - 1)
    return [0.2 + step * index for index in range(sample_count)]


def _finalize_output_path(temp_output: Path, staging_path: Path) -> None:
    return _finalize_output_path_impl(temp_output, staging_path)


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
    return _run_encode_command_impl(
        ffmpeg_cmd=ffmpeg_cmd,
        temp_output=temp_output,
        staging_path=staging_path,
        overwrite=overwrite,
        process_controller=process_controller,
        host=host,
        progress_callback=progress_callback,
        execution_mode_for_host=execution_mode_for_host,
        host_media_access_for_host=host_media_access_for_host,
        remote_shell_path_export_line=remote_shell_path_export_line,
        ssh_client_options=ssh_client_options,
        ffmpeg_command_with_progress=_ffmpeg_command_with_progress,
        run_tracked_encode_command=_run_tracked_encode_command,
        run_tracked_process_fn=_run_tracked_process,
        run_streamed_remote_encode_command_fn=_run_streamed_remote_encode_command,
    )


def _streaming_output_args_for_path(path: Path) -> list[str]:
    return _streaming_output_args_for_path_impl(path)


def _build_streaming_remote_ffmpeg_command(
        ffmpeg_cmd: list[str], *, source_path: Path, output_path: Path, executable_path: str | None = None
) -> list[str]:
    return _build_streaming_remote_ffmpeg_command_impl(
        ffmpeg_cmd,
        source_path=source_path,
        output_path=output_path,
        executable_path=executable_path,
        ffmpeg_command_with_progress=_ffmpeg_command_with_progress,
    )


def _run_streamed_remote_encode_command(
        *,
        ffmpeg_cmd: list[str],
        temp_output: Path,
        source_path: Path,
        process_controller: ManagedProcessController | None,
        host: dict[str, Any] | None,
        progress_callback: Callable[[dict[str, Any]], None] | None,
) -> subprocess.CompletedProcess[str]:
    return _run_streamed_remote_encode_command_impl(
        ffmpeg_cmd=ffmpeg_cmd,
        temp_output=temp_output,
        source_path=source_path,
        process_controller=process_controller,
        host=host,
        progress_callback=progress_callback,
        ssh_client_options=ssh_client_options,
        build_streaming_remote_ffmpeg_command=_build_streaming_remote_ffmpeg_command,
        update_ffmpeg_progress_state=_update_ffmpeg_progress_state,
        process_cancelled_error=ProcessCancelledError,
    )


def _ffmpeg_command_with_progress(command: list[str]) -> list[str]:
    return _ffmpeg_command_with_progress_impl(command, encode_progress_args=ENCODE_PROGRESS_ARGS)


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
    return _run_tracked_process_impl(
        cmd,
        process_controller=process_controller,
        progress_callback=progress_callback,
        run_command=run_command,
        update_ffmpeg_progress_state=_update_ffmpeg_progress_state,
        process_cancelled_error=ProcessCancelledError,
    )


def _update_ffmpeg_progress_state(
        progress_state: dict[str, str],
        line: str,
        *,
        elapsed_seconds: float,
) -> dict[str, Any] | None:
    return _update_ffmpeg_progress_state_impl(progress_state, line, elapsed_seconds=elapsed_seconds)


def _progress_float(value: str | None) -> float | None:
    return _progress_float_impl(value)


def _progress_speed(value: str | None) -> float | None:
    return _progress_speed_impl(value)


def _progress_out_time_seconds(progress_state: dict[str, str]) -> float | None:
    return _progress_out_time_seconds_impl(progress_state)


def _select_streams(item: dict[str, Any]) -> dict[str, Any]:
    return _select_streams_impl(item, text_subtitle_codecs=TEXT_SUBTITLE_CODECS)


def _pick_audio(audio_tracks: list[dict[str, Any]]) -> dict[str, Any]:
    return _pick_audio_impl(audio_tracks)


def _pick_subtitles(subtitle_tracks: list[dict[str, Any]], prefer_text: bool) -> list[dict[str, Any]]:
    return _pick_subtitles_impl(subtitle_tracks, prefer_text, text_subtitle_codecs=TEXT_SUBTITLE_CODECS)


def _source_has_preservable_subtitles(subtitle_tracks: list[dict[str, Any]]) -> bool:
    return _source_has_preservable_subtitles_impl(subtitle_tracks)


def _audio_codec(track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    return _audio_codec_impl(track, audio_policy)


def _opus_bitrate(track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    return _opus_bitrate_impl(track, audio_policy)


def _opus_layout_filter(track: dict[str, Any]) -> str | None:
    return _opus_layout_filter_impl(track)


def _check(validation: dict[str, Any], passed: bool, message: str) -> None:
    return _check_impl(validation, passed, message)


def _parse_bitrate_text(value: str) -> int:
    return _parse_bitrate_text_impl(value)


def _record_event(connection: DBClient, library_item_id: int, event_type: str,
                  details: dict[str, Any]) -> None:
    return _record_event_impl(connection, library_item_id, event_type, details)


def _format_crf(value: float) -> str:
    return _format_crf_impl(value)
