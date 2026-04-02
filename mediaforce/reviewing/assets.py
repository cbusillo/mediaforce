import shutil
from pathlib import Path
from typing import Any, Callable

from mediaforce.core.type_defs import int_value


def render_review_contact_sheet(
        *,
        source_clip_path: Path,
        preview_clip_path: Path,
        output_path: Path,
        process_controller: Any = None,
        ffmpeg_binary: Callable[[], str],
        run_command: Callable[..., Any],
) -> None:
    filter_complex = (
        "[0:v]fps=3/8,scale=320:-1:flags=lanczos,tile=3x1,setsar=1[src];"
        "[1:v]fps=3/8,scale=320:-1:flags=lanczos,tile=3x1,setsar=1[draft];"
        "[src][draft]vstack=inputs=2[v]"
    )
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
        filter_complex,
        "-map",
        "[v]",
        "-frames:v",
        "1",
        str(output_path),
    ]
    result = run_command(cmd, process_controller=process_controller)
    _raise_on_failure(result, "Review contact sheet render failed")


def render_audio_spectrogram_compare(
        *,
        source_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        audio_track: dict[str, Any],
        audio_policy: dict[str, Any],
        process_controller: Any = None,
        planned_audio_action: Callable[[dict[str, Any], dict[str, Any]], str],
        planned_opus_bitrate: Callable[[dict[str, Any], dict[str, Any]], str],
        render_audio_spectrogram: Callable[..., None],
        render_encoded_audio_clip: Callable[..., None],
        stack_review_images: Callable[..., None],
) -> dict[str, Any] | None:
    action = planned_audio_action(audio_track, audio_policy)
    if action != "libopus":
        return None

    bitrate = planned_opus_bitrate(audio_track, audio_policy)
    temp_root = output_path.parent / f".{output_path.stem}-artifacts"
    temp_root.mkdir(parents=True, exist_ok=True)
    try:
        source_png = temp_root / "source-audio.png"
        encoded_audio = temp_root / "encoded-audio.opus"
        encoded_png = temp_root / "encoded-audio.png"

        render_audio_spectrogram(
            source_path=source_path,
            output_path=source_png,
            clip_time=clip_time,
            duration_seconds=duration_seconds,
            process_controller=process_controller,
        )
        render_encoded_audio_clip(
            source_path=source_path,
            output_path=encoded_audio,
            clip_time=clip_time,
            duration_seconds=duration_seconds,
            bitrate=bitrate,
            process_controller=process_controller,
        )
        render_audio_spectrogram(
            source_path=encoded_audio,
            output_path=encoded_png,
            clip_time=0.0,
            duration_seconds=duration_seconds,
            process_controller=process_controller,
        )
        stack_review_images(
            top_path=source_png,
            bottom_path=encoded_png,
            output_path=output_path,
            process_controller=process_controller,
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    return {
        "action": action,
        "bitrate": bitrate,
        "channels": int_value(audio_track.get("channels")) or 2,
        "codec_name": str(audio_track.get("codec_name") or "").lower() or None,
    }


def render_audio_spectrogram(
        *,
        source_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        process_controller: Any = None,
        ffmpeg_binary: Callable[[], str],
        run_command: Callable[..., Any],
) -> None:
    filter_complex = (
        "[0:a]aformat=channel_layouts=stereo,"
        "showspectrumpic=s=960x240:legend=disabled:mode=combined:color=rainbow[v]"
    )
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
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-frames:v",
        "1",
        str(output_path),
    ]
    result = run_command(cmd, process_controller=process_controller)
    _raise_on_failure(result, "Audio spectrogram render failed")


def render_encoded_audio_clip(
        *,
        source_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        bitrate: str,
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
        "-ss",
        f"{clip_time:.3f}",
        "-t",
        f"{duration_seconds:.3f}",
        "-i",
        str(source_path),
        "-map",
        "0:a:0",
        "-c:a",
        "libopus",
        "-b:a",
        bitrate,
        "-ac",
        "2",
        str(output_path),
    ]
    result = run_command(cmd, process_controller=process_controller)
    _raise_on_failure(result, "Encoded audio review clip failed")


def stack_review_images(
        *,
        top_path: Path,
        bottom_path: Path,
        output_path: Path,
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
        str(top_path),
        "-i",
        str(bottom_path),
        "-filter_complex",
        "[0:v][1:v]vstack=inputs=2[v]",
        "-map",
        "[v]",
        "-frames:v",
        "1",
        str(output_path),
    ]
    result = run_command(cmd, process_controller=process_controller)
    _raise_on_failure(result, "Review image stack render failed")


def _raise_on_failure(result: Any, prefix: str) -> None:
    if result.returncode == 0:
        return
    details = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with status {result.returncode}"
    raise RuntimeError(f"{prefix}: {details}")
