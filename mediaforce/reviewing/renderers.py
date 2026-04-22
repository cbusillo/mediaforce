from pathlib import Path
from typing import Any, Callable


NATIVE_COMPARE_FILTER = (
    "[0:v]setsar=1[left];"
    "[1:v]setsar=1[right];"
    "[left][right]xstack=inputs=2:layout=0_0|w0_0:fill=black[stacked];"
    "[stacked]pad=ceil(iw/2)*2:ceil(ih/2)*2[v]"
)


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
        video_filter: str | None = None,
        process_controller: Any = None,
        ffmpeg_binary: Callable[[], str],
        ffmpeg_hwaccel_input_args: Callable[[str | None], list[str]],
        format_crf: Callable[[float], str],
        run_command: Callable[..., Any],
) -> None:
    cmd = _preview_render_command(
        source_path=source_path,
        source_codec=source_codec,
        output_path=output_path,
        clip_time=clip_time,
        duration_seconds=duration_seconds,
        encoder=encoder,
        pixel_format=pixel_format,
        preset=preset,
        crf=crf,
        svt_params=svt_params,
        video_filter=video_filter,
        ffmpeg_binary=ffmpeg_binary,
        ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
        format_crf=format_crf,
    )
    result = run_command(cmd, process_controller=process_controller)
    _raise_on_failure(result, "Preview sample encode failed")


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
        video_filter: str | None = None,
        remote_preview_timeout_seconds: int,
        ffmpeg_binary: Callable[[], str],
        ffmpeg_hwaccel_input_args: Callable[[str | None], list[str]],
        format_crf: Callable[[float], str],
        run_remote_command: Callable[..., Any],
) -> None:
    cmd = _preview_render_command(
        source_path=source_path,
        source_codec=source_codec,
        output_path=remote_output_path,
        clip_time=clip_time,
        duration_seconds=duration_seconds,
        encoder=encoder,
        pixel_format=pixel_format,
        preset=preset,
        crf=crf,
        svt_params=svt_params,
        video_filter=video_filter,
        ffmpeg_binary=ffmpeg_binary,
        ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
        format_crf=format_crf,
    )
    result = run_remote_command(host, cmd, remote_preview_timeout_seconds)
    _raise_on_failure(result, "Preview sample encode failed")


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
        "-an",
        "-sn",
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
    ]
    if encoder == "libsvtav1":
        cmd.extend(["-svtav1-params", ":".join(svt_params)])
    if video_filter:
        cmd.extend(["-vf", video_filter])
    cmd.append(str(output_path))
    return cmd


def render_compare_clip_from_preview(
        *,
        source_path: Path,
        source_codec: str | None = None,
        preview_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
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
        "-i",
        str(preview_path),
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
    _raise_on_failure(result, "Preview compare render failed")


def render_source_review_clip(
        *,
        source_path: Path,
        source_codec: str | None = None,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
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
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-c:v",
        "libx264",
        "-crf",
        "16",
        "-preset",
        "veryfast",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = run_command(cmd, process_controller=process_controller)
    _raise_on_failure(result, "Source review clip render failed")


def _raise_on_failure(result: Any, prefix: str) -> None:
    if result.returncode == 0:
        return
    details = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with status {result.returncode}"
    raise RuntimeError(f"{prefix}: {details}")
