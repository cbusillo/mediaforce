import subprocess
from pathlib import Path
from typing import Any, Callable

from mediaforce.encoding.streams import opus_encode_arguments


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
) -> int | None:
    production_preview_path = output_path.with_name(f".{output_path.stem}-production-preview.mkv")
    try:
        cmd = preview_render_command(
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
            video_filter=video_filter,
            ffmpeg_binary=ffmpeg_binary,
            ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
            format_crf=format_crf,
        )
        result = run_command(cmd, process_controller=process_controller)
        _raise_on_failure(result, "Preview sample encode failed")
        production_size = _final_output_size(result.stdout)
        browser_cmd = browser_preview_proxy_command(
            source_path=production_preview_path,
            output_path=output_path,
            duration_seconds=duration_seconds,
            include_audio=audio_plan is not None,
            ffmpeg_binary=ffmpeg_binary,
        )
        result = run_command(browser_cmd, process_controller=process_controller)
        _raise_on_failure(result, "Browser preview render failed")
        return production_size
    finally:
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
        process_controller: Any = None,
) -> int | None:
    managed_kwargs = {"process_controller": process_controller} if process_controller is not None else {}
    production_preview_path = remote_output_path.with_name(
        f".{remote_output_path.stem}-production-preview.mkv"
    )
    try:
        cmd = preview_render_command(
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
            video_filter=video_filter,
            ffmpeg_binary=ffmpeg_binary,
            ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
            format_crf=format_crf,
        )
        result = run_remote_command(host, cmd, remote_preview_timeout_seconds, **managed_kwargs)
        _raise_on_failure(result, "Preview sample encode failed")
        production_size = _final_output_size(result.stdout)
        browser_cmd = browser_preview_proxy_command(
            source_path=production_preview_path,
            output_path=remote_output_path,
            duration_seconds=duration_seconds,
            include_audio=audio_plan is not None,
            ffmpeg_binary=ffmpeg_binary,
        )
        result = run_remote_command(host, browser_cmd, remote_preview_timeout_seconds, **managed_kwargs)
        _raise_on_failure(result, "Browser preview render failed")
        return production_size
    finally:
        try:
            run_remote_command(
                host,
                ["rm", "-f", str(production_preview_path)],
                min(remote_preview_timeout_seconds, REMOTE_REVIEW_CLEANUP_TIMEOUT_SECONDS),
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass


def preview_render_command(
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
        "-progress",
        "pipe:1",
        "-preset",
        str(preset),
        "-crf",
        format_crf(crf),
    ]
    if audio_plan is None:
        cmd.extend(["-an"])
    else:
        cmd.extend(["-map", f"0:{_audio_stream_index(audio_plan)}", "-shortest"])
        if audio_plan.get("production_action") == "transcode":
            channels = int(audio_plan.get("source_channels") or 2)
            cmd.extend(opus_encode_arguments(
                channels=channels,
                bitrate_text=str(audio_plan.get("production_bitrate_text") or "128k"),
                output_channels=int(audio_plan.get("production_channels") or channels),
            ))
        else:
            cmd.extend(["-ss", "0", "-c:a", "copy"])
    if encoder == "libsvtav1":
        cmd.extend(["-svtav1-params", ":".join(svt_params)])
    if video_filter:
        cmd.extend(["-vf", video_filter])
    cmd.extend(["-t", f"{duration_seconds:.3f}"])
    cmd.append(str(output_path))
    return cmd


def browser_preview_proxy_command(
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
        # Keep the first frame's offset from the seek point. Without audio, ffmpeg
        # would otherwise move it to zero and the preview would play one frame
        # ahead of its source clip (#601).
        "-copyts",
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
        cmd.extend(["-map", "0:a:0", *_playback_audio_arguments()])
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
    cmd = source_review_clip_command(
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
        process_controller: Any = None,
) -> None:
    managed_kwargs = {"process_controller": process_controller} if process_controller is not None else {}
    cmd = source_review_clip_command(
        source_path=source_path,
        source_codec=source_codec,
        output_path=remote_output_path,
        clip_time=clip_time,
        duration_seconds=duration_seconds,
        audio_plan=audio_plan,
        ffmpeg_binary=ffmpeg_binary,
        ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
    )
    result = run_remote_command(host, cmd, remote_preview_timeout_seconds, **managed_kwargs)
    _raise_on_failure(result, "Source review clip render failed")


def source_review_clip_command(
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
        cmd.extend(["-map", f"0:{_audio_stream_index(audio_plan)}", *_playback_audio_arguments()])
    cmd.extend(["-t", f"{duration_seconds:.3f}"])
    cmd.append(str(output_path))
    return cmd


def _playback_audio_arguments() -> list[str]:
    return ["-c:a", "aac", "-b:a", BROWSER_REVIEW_AUDIO_BITRATE, "-ar", str(BROWSER_REVIEW_AUDIO_SAMPLE_RATE),
            "-ac", str(BROWSER_REVIEW_AUDIO_CHANNELS), "-shortest"]


def _final_output_size(progress: str) -> int | None:
    final_report = progress.rsplit("progress=continue", 1)[-1]
    for line in final_report.splitlines():
        key, _, value = line.partition("=")
        if key == "total_size" and value.isdecimal():
            return int(value)
    return None


def _audio_stream_index(audio_plan: dict[str, Any]) -> int:
    return int(audio_plan["source_stream_index"])


def _raise_on_failure(result: Any, prefix: str) -> None:
    if result.returncode == 0:
        return
    details = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with status {result.returncode}"
    raise RuntimeError(f"{prefix}: {details}")
