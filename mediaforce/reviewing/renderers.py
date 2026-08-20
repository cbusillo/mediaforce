import subprocess
from pathlib import Path
from typing import Any, Callable


NATIVE_COMPARE_FILTER = (
    "[0:v]setsar=1[left];"
    "[1:v]setsar=1[right];"
    "[left][right]xstack=inputs=2:layout=0_0|w0_0:fill=black[stacked];"
    "[stacked]pad=ceil(iw/2)*2:ceil(ih/2)*2[v]"
)
BROWSER_REVIEW_AUDIO_BITRATE = "256k"
BROWSER_REVIEW_AUDIO_CHANNELS = 2
BROWSER_REVIEW_AUDIO_SAMPLE_RATE = 48_000
BROWSER_REVIEW_VIDEO_CRF = "16"
BROWSER_REVIEW_VIDEO_PIXEL_FORMAT = "yuv420p"
BROWSER_REVIEW_VIDEO_PRESET = "veryfast"
REMOTE_REVIEW_CLEANUP_TIMEOUT_SECONDS = 30


def render_compare_clip(
        source_path: Path,
        staged_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        *,
        source_codec: str | None = None,
        process_controller: Any = None,
        ffmpeg_binary: Callable[[], str],
        ffmpeg_hwaccel_input_args: Callable[[str | None], list[str]],
        run_command: Callable[..., Any],
) -> None:
    cmd = [
        ffmpeg_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{clip_time:.3f}",
        "-t",
        f"{duration_seconds:.3f}",
        *ffmpeg_hwaccel_input_args(source_codec),
        "-i",
        str(source_path),
        "-ss",
        f"{clip_time:.3f}",
        "-t",
        f"{duration_seconds:.3f}",
        "-i",
        str(staged_path),
        "-filter_complex",
        NATIVE_COMPARE_FILTER,
        "-map",
        "[v]",
        "-an",
        "-c:v",
        "libx264",
        "-crf",
        "16",
        "-preset",
        "veryfast",
        str(output_path),
    ]
    result = run_command(cmd, process_controller=process_controller)
    _raise_on_failure(result, "Compare clip render failed")


def render_encoded_preview_clip(
        *,
        source_path: Path,
        source_codec: str | None = None,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        encoder: str,
        pixel_format: str,
        preset: int,
        crf: float,
        svt_params: list[str],
        audio_plan: dict[str, Any] | None = None,
        video_filter: str | None = None,
        process_controller: Any = None,
        ffmpeg_binary: Callable[[], str],
        ffmpeg_hwaccel_input_args: Callable[[str | None], list[str]],
        format_crf: Callable[[float], str],
        run_command: Callable[..., Any],
) -> None:
    production_audio_path = None
    production_preview_path = output_path.with_name(f".{output_path.stem}-production-preview.mp4")
    try:
        if _requires_production_audio_render(audio_plan):
            production_audio_path = output_path.with_name(f".{output_path.stem}-production-audio.opus")
            audio_cmd = _production_audio_render_command(
                source_path=source_path,
                output_path=production_audio_path,
                clip_time=clip_time,
                duration_seconds=duration_seconds,
                audio_plan=audio_plan or {},
                ffmpeg_binary=ffmpeg_binary,
            )
            result = run_command(audio_cmd, process_controller=process_controller)
            _raise_on_failure(result, "Preview audio render failed")

        cmd = _preview_render_command(
            source_path=source_path,
            source_codec=source_codec,
            output_path=production_preview_path,
            clip_time=clip_time,
            duration_seconds=duration_seconds,
            encoder=encoder,
            pixel_format=pixel_format,
            preset=preset,
            crf=crf,
            svt_params=svt_params,
            audio_plan=audio_plan,
            production_audio_path=production_audio_path,
            video_filter=video_filter,
            ffmpeg_binary=ffmpeg_binary,
            ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
            format_crf=format_crf,
        )
        result = run_command(cmd, process_controller=process_controller)
        _raise_on_failure(result, "Preview sample encode failed")
        browser_cmd = _browser_preview_proxy_command(
            source_path=production_preview_path,
            output_path=output_path,
            duration_seconds=duration_seconds,
            include_audio=audio_plan is not None,
            ffmpeg_binary=ffmpeg_binary,
        )
        result = run_command(browser_cmd, process_controller=process_controller)
        _raise_on_failure(result, "Browser preview render failed")
    finally:
        if production_audio_path is not None:
            production_audio_path.unlink(missing_ok=True)
        production_preview_path.unlink(missing_ok=True)


def render_encoded_preview_clip_remote(
        *,
        host: dict[str, Any],
        source_path: Path,
        source_codec: str | None = None,
        remote_output_path: Path,
        clip_time: float,
        duration_seconds: float,
        encoder: str,
        pixel_format: str,
        preset: int,
        crf: float,
        svt_params: list[str],
        audio_plan: dict[str, Any] | None = None,
        video_filter: str | None = None,
        remote_preview_timeout_seconds: int,
        ffmpeg_binary: Callable[[], str],
        ffmpeg_hwaccel_input_args: Callable[[str | None], list[str]],
        format_crf: Callable[[float], str],
        run_remote_command: Callable[..., Any],
) -> None:
    production_audio_path = None
    production_preview_path = remote_output_path.with_name(
        f".{remote_output_path.stem}-production-preview.mp4"
    )
    try:
        if _requires_production_audio_render(audio_plan):
            production_audio_path = remote_output_path.with_name(
                f".{remote_output_path.stem}-production-audio.opus"
            )
            audio_cmd = _production_audio_render_command(
                source_path=source_path,
                output_path=production_audio_path,
                clip_time=clip_time,
                duration_seconds=duration_seconds,
                audio_plan=audio_plan or {},
                ffmpeg_binary=ffmpeg_binary,
            )
            result = run_remote_command(host, audio_cmd, remote_preview_timeout_seconds)
            _raise_on_failure(result, "Preview audio render failed")

        cmd = _preview_render_command(
            source_path=source_path,
            source_codec=source_codec,
            output_path=production_preview_path,
            clip_time=clip_time,
            duration_seconds=duration_seconds,
            encoder=encoder,
            pixel_format=pixel_format,
            preset=preset,
            crf=crf,
            svt_params=svt_params,
            audio_plan=audio_plan,
            production_audio_path=production_audio_path,
            video_filter=video_filter,
            ffmpeg_binary=ffmpeg_binary,
            ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
            format_crf=format_crf,
        )
        result = run_remote_command(host, cmd, remote_preview_timeout_seconds)
        _raise_on_failure(result, "Preview sample encode failed")
        browser_cmd = _browser_preview_proxy_command(
            source_path=production_preview_path,
            output_path=remote_output_path,
            duration_seconds=duration_seconds,
            include_audio=audio_plan is not None,
            ffmpeg_binary=ffmpeg_binary,
        )
        result = run_remote_command(host, browser_cmd, remote_preview_timeout_seconds)
        _raise_on_failure(result, "Browser preview render failed")
    finally:
        temporary_paths = [production_preview_path]
        if production_audio_path is not None:
            temporary_paths.append(production_audio_path)
        try:
            run_remote_command(
                host,
                ["rm", "-f", *(str(path) for path in temporary_paths)],
                min(remote_preview_timeout_seconds, REMOTE_REVIEW_CLEANUP_TIMEOUT_SECONDS),
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass


def _preview_render_command(
        *,
        source_path: Path,
        source_codec: str | None,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        encoder: str,
        pixel_format: str,
        preset: int,
        crf: float,
        svt_params: list[str],
        audio_plan: dict[str, Any] | None = None,
        production_audio_path: Path | None = None,
        video_filter: str | None = None,
        ffmpeg_binary: Callable[[], str],
        ffmpeg_hwaccel_input_args: Callable[[str | None], list[str]],
        format_crf: Callable[[float], str],
) -> list[str]:
    cmd = [
        ffmpeg_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{clip_time:.3f}",
        "-t",
        f"{duration_seconds:.3f}",
        *ffmpeg_hwaccel_input_args(source_codec),
        "-i",
        str(source_path),
    ]
    if production_audio_path is not None:
        cmd.extend(["-i", str(production_audio_path)])
    cmd.extend([
        "-map",
        "0:v:0",
        "-map_chapters",
        "-1",
        "-sn",
        "-dn",
        "-c:v",
        encoder,
        "-pix_fmt",
        pixel_format,
        "-movflags",
        "+faststart",
        "-preset",
        str(preset),
        "-crf",
        format_crf(crf),
    ])
    if audio_plan is None:
        cmd.extend(["-an"])
    else:
        audio_input = "1:a:0" if production_audio_path is not None else f"0:{_audio_stream_index(audio_plan)}"
        cmd.extend(
            [
                "-map",
                audio_input,
                "-c:a",
                "aac",
                "-b:a",
                BROWSER_REVIEW_AUDIO_BITRATE,
                "-ar",
                str(BROWSER_REVIEW_AUDIO_SAMPLE_RATE),
                "-ac",
                str(BROWSER_REVIEW_AUDIO_CHANNELS),
                "-shortest",
            ]
        )
    if encoder == "libsvtav1":
        cmd.extend(["-svtav1-params", ":".join(svt_params)])
    if video_filter:
        cmd.extend(["-vf", video_filter])
    cmd.extend(["-t", f"{duration_seconds:.3f}"])
    cmd.append(str(output_path))
    return cmd


def _browser_preview_proxy_command(
        *,
        source_path: Path,
        output_path: Path,
        duration_seconds: float,
        include_audio: bool,
        ffmpeg_binary: Callable[[], str],
) -> list[str]:
    cmd = [
        ffmpeg_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-map_chapters",
        "-1",
        "-sn",
        "-dn",
        "-c:v",
        "libx264",
        "-pix_fmt",
        BROWSER_REVIEW_VIDEO_PIXEL_FORMAT,
        "-preset",
        BROWSER_REVIEW_VIDEO_PRESET,
        "-crf",
        BROWSER_REVIEW_VIDEO_CRF,
        "-movflags",
        "+faststart",
        "-t",
        f"{duration_seconds:.3f}",
    ]
    if include_audio:
        cmd.extend(["-map", "0:a:0", "-c:a", "copy", "-shortest"])
    else:
        cmd.extend(["-an"])
    cmd.append(str(output_path))
    return cmd


def render_compare_clip_from_review_pair(
        *,
        source_clip_path: Path,
        preview_clip_path: Path,
        output_path: Path,
        duration_seconds: float,
        process_controller: Any = None,
        ffmpeg_binary: Callable[[], str],
        run_command: Callable[..., Any],
) -> None:
    cmd = [
        ffmpeg_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source_clip_path),
        "-i",
        str(preview_clip_path),
        "-filter_complex",
        NATIVE_COMPARE_FILTER,
        "-map",
        "[v]",
        "-an",
        "-c:v",
        "libx264",
        "-crf",
        "16",
        "-preset",
        "veryfast",
        "-t",
        f"{duration_seconds:.3f}",
        str(output_path),
    ]
    result = run_command(cmd, process_controller=process_controller)
    _raise_on_failure(result, "Review pair compare render failed")


def render_source_review_clip(
        *,
        source_path: Path,
        source_codec: str | None = None,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        audio_plan: dict[str, Any] | None = None,
        process_controller: Any = None,
        ffmpeg_binary: Callable[[], str],
        ffmpeg_hwaccel_input_args: Callable[[str | None], list[str]],
        run_command: Callable[..., Any],
) -> None:
    cmd = _source_review_clip_command(
        source_path=source_path,
        source_codec=source_codec,
        output_path=output_path,
        clip_time=clip_time,
        duration_seconds=duration_seconds,
        audio_plan=audio_plan,
        ffmpeg_binary=ffmpeg_binary,
        ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
    )
    result = run_command(cmd, process_controller=process_controller)
    _raise_on_failure(result, "Source review clip render failed")


def render_source_review_clip_remote(
        *,
        host: dict[str, Any],
        source_path: Path,
        source_codec: str | None = None,
        remote_output_path: Path,
        clip_time: float,
        duration_seconds: float,
        audio_plan: dict[str, Any] | None = None,
        remote_preview_timeout_seconds: int,
        ffmpeg_binary: Callable[[], str],
        ffmpeg_hwaccel_input_args: Callable[[str | None], list[str]],
        run_remote_command: Callable[..., Any],
) -> None:
    cmd = _source_review_clip_command(
        source_path=source_path,
        source_codec=source_codec,
        output_path=remote_output_path,
        clip_time=clip_time,
        duration_seconds=duration_seconds,
        audio_plan=audio_plan,
        ffmpeg_binary=ffmpeg_binary,
        ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
    )
    result = run_remote_command(host, cmd, remote_preview_timeout_seconds)
    _raise_on_failure(result, "Source review clip render failed")


def _source_review_clip_command(
        *,
        source_path: Path,
        source_codec: str | None,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        audio_plan: dict[str, Any] | None,
        ffmpeg_binary: Callable[[], str],
        ffmpeg_hwaccel_input_args: Callable[[str | None], list[str]],
) -> list[str]:
    cmd = [
        ffmpeg_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{clip_time:.3f}",
        "-t",
        f"{duration_seconds:.3f}",
        *ffmpeg_hwaccel_input_args(source_codec),
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-map_chapters",
        "-1",
        "-sn",
        "-dn",
        "-c:v",
        "libx264",
        "-crf",
        "16",
        "-preset",
        "veryfast",
        "-movflags",
        "+faststart",
    ]
    if audio_plan is None:
        cmd.extend(["-an"])
    else:
        cmd.extend(
            [
                "-map",
                f"0:{_audio_stream_index(audio_plan)}",
                "-c:a",
                "aac",
                "-b:a",
                BROWSER_REVIEW_AUDIO_BITRATE,
                "-ar",
                str(BROWSER_REVIEW_AUDIO_SAMPLE_RATE),
                "-ac",
                str(BROWSER_REVIEW_AUDIO_CHANNELS),
                "-shortest",
            ]
        )
    cmd.extend(["-t", f"{duration_seconds:.3f}"])
    cmd.append(str(output_path))
    return cmd


def _requires_production_audio_render(audio_plan: dict[str, Any] | None) -> bool:
    if audio_plan is None:
        return False
    return str(audio_plan.get("production_action") or "") == "transcode"


def _audio_stream_index(audio_plan: dict[str, Any]) -> int:
    return int(audio_plan["source_stream_index"])


def _production_audio_render_command(
        *,
        source_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        audio_plan: dict[str, Any],
        ffmpeg_binary: Callable[[], str],
) -> list[str]:
    channels = int(audio_plan.get("source_channels") or 2)
    cmd = [
        ffmpeg_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{clip_time:.3f}",
        "-t",
        f"{duration_seconds:.3f}",
        "-i",
        str(source_path),
        "-map",
        f"0:{_audio_stream_index(audio_plan)}",
        "-vn",
        "-sn",
        "-dn",
        "-c:a",
        "libopus",
        "-b:a",
        str(audio_plan.get("production_bitrate_text") or "128k"),
    ]
    if channels >= 8:
        cmd.extend(["-af", "channelmap=channel_layout=7.1", "-mapping_family", "1"])
    elif channels >= 6:
        cmd.extend(["-af", "channelmap=channel_layout=5.1", "-mapping_family", "1"])
    cmd.append(str(output_path))
    return cmd


def _raise_on_failure(result: Any, prefix: str) -> None:
    if result.returncode == 0:
        return
    details = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with status {result.returncode}"
    raise RuntimeError(f"{prefix}: {details}")
