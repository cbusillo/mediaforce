import csv
import re
import shutil
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.binaries import ffmpeg_binary, ffprobe_binary
from mediaforce.ffmpeg import ffmpeg_hwaccel_input_args
from mediaforce.process_control import ManagedProcessController, run_command
from mediaforce.remote import copy_remote_file_to_local, execution_mode_for_host, run_remote_command

PTS_TIME_RE = re.compile(r"pts_time:(?P<pts>[0-9]+(?:\.[0-9]+)?)")
REMOTE_PREVIEW_TIMEOUT_SECONDS = 2 * 60 * 60


@dataclass(slots=True)
class CompareClip:
    output_path: Path
    timestamp_seconds: float
    duration_seconds: float


@dataclass(slots=True)
class EncodedPreviewClip:
    output_path: Path
    timestamp_seconds: float
    duration_seconds: float
    size_bytes: int


@dataclass(slots=True)
class BrowserReviewClip:
    output_path: Path
    timestamp_seconds: float
    duration_seconds: float
    size_bytes: int


def render_review_contact_sheet(
        *,
        source_clip_path: Path,
        preview_clip_path: Path,
        output_path: Path,
        process_controller: ManagedProcessController | None = None,
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
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with status {result.returncode}"
        raise RuntimeError(f"Review contact sheet render failed: {details}")


def render_audio_spectrogram_compare(
        *,
        source_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        audio_track: dict[str, Any],
        audio_policy: dict[str, Any],
        process_controller: ManagedProcessController | None = None,
) -> dict[str, Any] | None:
    action = _planned_audio_action(audio_track, audio_policy)
    if action != "libopus":
        return None

    bitrate = _planned_opus_bitrate(audio_track, audio_policy)
    temp_root = output_path.parent / f".{output_path.stem}-artifacts"
    temp_root.mkdir(parents=True, exist_ok=True)
    try:
        source_png = temp_root / "source-audio.png"
        encoded_audio = temp_root / "encoded-audio.opus"
        encoded_png = temp_root / "encoded-audio.png"

        _render_audio_spectrogram(
            source_path=source_path,
            output_path=source_png,
            clip_time=clip_time,
            duration_seconds=duration_seconds,
            process_controller=process_controller,
        )
        _render_encoded_audio_clip(
            source_path=source_path,
            output_path=encoded_audio,
            clip_time=clip_time,
            duration_seconds=duration_seconds,
            bitrate=bitrate,
            process_controller=process_controller,
        )
        _render_audio_spectrogram(
            source_path=encoded_audio,
            output_path=encoded_png,
            clip_time=0.0,
            duration_seconds=duration_seconds,
            process_controller=process_controller,
        )
        _stack_review_images(top_path=source_png, bottom_path=encoded_png, output_path=output_path,
                             process_controller=process_controller)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    return {
        "action": action,
        "bitrate": bitrate,
        "channels": int(audio_track.get("channels") or 2),
        "codec_name": str(audio_track.get("codec_name") or "").lower() or None,
    }


def generate_compare_clips(
        connection: sqlite3.Connection,
        manifest: dict[str, Any],
        indexes: list[int],
        *,
        output_dir: Path,
        duration_seconds: float,
        timestamps: list[float] | None,
        play: bool,
        process_controller: ManagedProcessController | None = None,
) -> list[CompareClip]:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated: list[CompareClip] = []
    for index in indexes:
        item = manifest["items"][index]
        stage_row = connection.execute(
            "SELECT staging_path FROM staged_artifacts WHERE library_item_id = ?",
            (item["library_item_id"],),
        ).fetchone()
        if stage_row is None:
            raise FileNotFoundError(f"No staged artifact found for item {item['library_item_id']}")

        source_path = Path(item["source_path"])
        staged_path = Path(stage_row["staging_path"])
        clip_times = timestamps or _auto_timestamps(
            source_path=source_path,
            total_duration=float(item.get("duration_seconds") or 0),
            clip_duration=duration_seconds,
            process_controller=process_controller,
        )
        item_dir = output_dir / f"item-{index:02d}"
        item_dir.mkdir(parents=True, exist_ok=True)

        generated.extend(
            generate_compare_clips_for_pair(
                source_path=source_path,
                staged_path=staged_path,
                source_codec=str(item.get("video_codec") or ""),
                output_dir=item_dir,
                duration_seconds=duration_seconds,
                timestamps=clip_times,
                process_controller=process_controller,
            )
        )

    if play and generated:
        subprocess.run(["ffplay", "-autoexit", str(generated[0].output_path)])

    return generated


def recommend_review_timestamps(
        source_path: Path,
        total_duration: float,
        clip_duration: float,
        *,
        process_controller: ManagedProcessController | None = None,
) -> list[float]:
    return _auto_timestamps(source_path, total_duration, clip_duration, process_controller=process_controller)


def encode_preview_clips(
        *,
        source_path: Path,
        source_codec: str | None = None,
        output_dir: Path,
        timestamps: list[float],
        duration_seconds: float,
        encoder: str,
        pixel_format: str,
        preset: int,
        crf: float,
        svt_params: list[str],
        host: dict[str, Any] | None = None,
        process_controller: ManagedProcessController | None = None,
) -> list[EncodedPreviewClip]:
    output_dir.mkdir(parents=True, exist_ok=True)
    host_mode = execution_mode_for_host(host)
    if host_mode == "ssh":
        return _encode_preview_clips_remote(
            host=host or {},
            source_path=source_path,
            source_codec=source_codec,
            output_dir=output_dir,
            timestamps=timestamps,
            duration_seconds=duration_seconds,
            encoder=encoder,
            pixel_format=pixel_format,
            preset=preset,
            crf=crf,
            svt_params=svt_params,
        )

    encoded: list[EncodedPreviewClip] = []
    for clip_number, clip_time in enumerate(timestamps, start=1):
        output_path = output_dir / f"encoded-{clip_number:02d}-{_slug_seconds(clip_time)}.mp4"
        _render_encoded_preview_clip(
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
            process_controller=process_controller,
        )
        encoded.append(
            EncodedPreviewClip(
                output_path=output_path,
                timestamp_seconds=clip_time,
                duration_seconds=duration_seconds,
                size_bytes=output_path.stat().st_size,
            )
        )
    return encoded


def _encode_preview_clips_remote(
        *,
        host: dict[str, Any],
        source_path: Path,
        source_codec: str | None,
        output_dir: Path,
        timestamps: list[float],
        duration_seconds: float,
        encoder: str,
        pixel_format: str,
        preset: int,
        crf: float,
        svt_params: list[str],
) -> list[EncodedPreviewClip]:
    remote_root = Path("/tmp") / f"mediaforce-preview-{uuid.uuid4().hex[:12]}"
    run_remote_command(host, ["mkdir", "-p", str(remote_root)], timeout=30)
    encoded: list[EncodedPreviewClip] = []
    try:
        for clip_number, clip_time in enumerate(timestamps, start=1):
            file_name = f"encoded-{clip_number:02d}-{_slug_seconds(clip_time)}.mp4"
            output_path = output_dir / file_name
            remote_output_path = remote_root / file_name
            _render_encoded_preview_clip_remote(
                host=host,
                source_path=source_path,
                source_codec=source_codec,
                remote_output_path=remote_output_path,
                clip_time=clip_time,
                duration_seconds=duration_seconds,
                encoder=encoder,
                pixel_format=pixel_format,
                preset=preset,
                crf=crf,
                svt_params=svt_params,
            )
            copy_remote_file_to_local(host, remote_output_path, output_path, timeout=REMOTE_PREVIEW_TIMEOUT_SECONDS)
            encoded.append(
                EncodedPreviewClip(
                    output_path=output_path,
                    timestamp_seconds=clip_time,
                    duration_seconds=duration_seconds,
                    size_bytes=output_path.stat().st_size,
                )
            )
    finally:
        try:
            run_remote_command(host, ["rm", "-rf", str(remote_root)], timeout=30)
        except Exception:
            pass
    return encoded


def generate_compare_clips_for_pair(
        *,
        source_path: Path,
        staged_path: Path,
        source_codec: str | None = None,
        output_dir: Path,
        duration_seconds: float,
        timestamps: list[float],
        process_controller: ManagedProcessController | None = None,
) -> list[CompareClip]:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated: list[CompareClip] = []
    for clip_number, clip_time in enumerate(timestamps, start=1):
        output_path = output_dir / f"compare-{clip_number:02d}-{_slug_seconds(clip_time)}.mkv"
        _render_compare_clip(
            source_path,
            staged_path,
            output_path,
            clip_time,
            duration_seconds,
            source_codec=source_codec,
            process_controller=process_controller,
        )
        generated.append(
            CompareClip(output_path=output_path, timestamp_seconds=clip_time, duration_seconds=duration_seconds))
    return generated


def generate_compare_clips_from_previews(
        *,
        source_path: Path,
        source_codec: str | None = None,
        previews: list[EncodedPreviewClip],
        output_dir: Path,
        process_controller: ManagedProcessController | None = None,
) -> list[CompareClip]:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated: list[CompareClip] = []
    for clip_number, preview in enumerate(previews, start=1):
        output_path = output_dir / f"compare-{clip_number:02d}-{_slug_seconds(preview.timestamp_seconds)}.mkv"
        _render_compare_clip_from_preview(
            source_path=source_path,
            source_codec=source_codec,
            preview_path=preview.output_path,
            output_path=output_path,
            clip_time=preview.timestamp_seconds,
            duration_seconds=preview.duration_seconds,
            process_controller=process_controller,
        )
        generated.append(
            CompareClip(
                output_path=output_path,
                timestamp_seconds=preview.timestamp_seconds,
                duration_seconds=preview.duration_seconds,
            )
        )
    return generated


def render_source_review_clips(
        *,
        source_path: Path,
        source_codec: str | None = None,
        output_dir: Path,
        timestamps: list[float],
        duration_seconds: float,
        process_controller: ManagedProcessController | None = None,
) -> list[BrowserReviewClip]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[BrowserReviewClip] = []
    for clip_number, clip_time in enumerate(timestamps, start=1):
        output_path = output_dir / f"source-{clip_number:02d}-{_slug_seconds(clip_time)}.mp4"
        _render_source_review_clip(
            source_path=source_path,
            source_codec=source_codec,
            output_path=output_path,
            clip_time=clip_time,
            duration_seconds=duration_seconds,
            process_controller=process_controller,
        )
        rendered.append(
            BrowserReviewClip(
                output_path=output_path,
                timestamp_seconds=clip_time,
                duration_seconds=duration_seconds,
                size_bytes=output_path.stat().st_size,
            )
        )
    return rendered


def _render_compare_clip(
        source_path: Path,
        staged_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        *,
        source_codec: str | None = None,
        process_controller: ManagedProcessController | None = None,
) -> None:
    filter_complex = "[0:v]scale=-2:540:flags=lanczos,setsar=1[left];[1:v]scale=-2:540:flags=lanczos,setsar=1[right];[left][right]hstack=inputs=2[v]"
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
        filter_complex,
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
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with status {result.returncode}"
        raise RuntimeError(f"Compare clip render failed: {details}")


def _render_encoded_preview_clip(
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
        process_controller: ManagedProcessController | None = None,
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
        encoder,
        "-pix_fmt",
        pixel_format,
        "-movflags",
        "+faststart",
        "-preset",
        str(preset),
        "-crf",
        _format_crf(crf),
    ]
    if encoder == "libsvtav1":
        cmd.extend(["-svtav1-params", ":".join(svt_params)])
    cmd.append(str(output_path))
    result = run_command(cmd, process_controller=process_controller)
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with status {result.returncode}"
        raise RuntimeError(f"Preview sample encode failed: {details}")


def _render_encoded_preview_clip_remote(
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
        encoder,
        "-pix_fmt",
        pixel_format,
        "-movflags",
        "+faststart",
        "-preset",
        str(preset),
        "-crf",
        _format_crf(crf),
    ]
    if encoder == "libsvtav1":
        cmd.extend(["-svtav1-params", ":".join(svt_params)])
    cmd.append(str(remote_output_path))
    result = run_remote_command(host, cmd, timeout=REMOTE_PREVIEW_TIMEOUT_SECONDS)
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with status {result.returncode}"
        raise RuntimeError(f"Preview sample encode failed: {details}")


def _render_compare_clip_from_preview(
        *,
        source_path: Path,
        source_codec: str | None = None,
        preview_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        process_controller: ManagedProcessController | None = None,
) -> None:
    filter_complex = "[0:v]scale=-2:540:flags=lanczos,setsar=1[left];[1:v]scale=-2:540:flags=lanczos,setsar=1[right];[left][right]hstack=inputs=2[v]"
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
        filter_complex,
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
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with status {result.returncode}"
        raise RuntimeError(f"Preview compare render failed: {details}")


def _render_source_review_clip(
        *,
        source_path: Path,
        source_codec: str | None = None,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        process_controller: ManagedProcessController | None = None,
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
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with status {result.returncode}"
        raise RuntimeError(f"Source review clip render failed: {details}")


def _render_audio_spectrogram(
        *,
        source_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        process_controller: ManagedProcessController | None = None,
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
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with status {result.returncode}"
        raise RuntimeError(f"Audio spectrogram render failed: {details}")


def _render_encoded_audio_clip(
        *,
        source_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        bitrate: str,
        process_controller: ManagedProcessController | None = None,
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
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with status {result.returncode}"
        raise RuntimeError(f"Encoded audio review clip failed: {details}")


def _stack_review_images(
        *,
        top_path: Path,
        bottom_path: Path,
        output_path: Path,
        process_controller: ManagedProcessController | None = None,
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
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or f"ffmpeg exited with status {result.returncode}"
        raise RuntimeError(f"Review image stack render failed: {details}")


def _planned_audio_action(audio_track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    codec = str(audio_track.get("codec_name") or "").lower()
    if codec in {str(name).lower() for name in audio_policy.get("copy_codecs", [])}:
        return "copy"
    if codec in {str(name).lower() for name in audio_policy.get("convert_to_opus_codecs", [])}:
        return "libopus"
    return "copy"


def _planned_opus_bitrate(audio_track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    channels = int(audio_track.get("channels") or 2)
    if channels >= 8:
        return str(audio_policy.get("surround_7_1_opus_bitrate") or "320k")
    if channels >= 6:
        return str(audio_policy.get("surround_5_1_opus_bitrate") or "224k")
    return str(audio_policy.get("stereo_opus_bitrate") or "128k")


def _default_timestamps(total_duration: float, clip_duration: float) -> list[float]:
    if total_duration <= 0:
        return [0.0]
    usable = max(total_duration - clip_duration, 0.0)
    if usable == 0:
        return [0.0]
    return [round(usable * ratio, 3) for ratio in (0.2, 0.5, 0.8)]


def _auto_timestamps(
        source_path: Path,
        total_duration: float,
        clip_duration: float,
        *,
        process_controller: ManagedProcessController | None = None,
) -> list[float]:
    complexity_points = _complexity_timestamps(
        source_path,
        total_duration,
        clip_duration,
        process_controller=process_controller,
    )
    if complexity_points:
        return complexity_points
    scene_points = _scene_change_timestamps(
        source_path,
        total_duration,
        clip_duration,
        process_controller=process_controller,
    )
    if scene_points:
        return scene_points
    return _default_timestamps(total_duration, clip_duration)


def _complexity_timestamps(
        source_path: Path,
        total_duration: float,
        clip_duration: float,
        *,
        process_controller: ManagedProcessController | None = None,
) -> list[float]:
    if total_duration <= 0:
        return []
    usable_end = max(total_duration - clip_duration, 0.0)
    if usable_end <= 0:
        return []

    cmd = [
        ffprobe_binary(),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "frame=best_effort_timestamp_time,pkt_size",
        "-of",
        "csv=p=0",
        str(source_path),
    ]
    result = run_command(cmd, process_controller=process_controller)
    if result.returncode != 0:
        return []

    second_buckets: dict[int, float] = {}
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) < 2:
            continue
        try:
            pts_time = float(row[0])
            pkt_size = float(row[1])
        except ValueError:
            continue
        second = int(max(pts_time, 0.0))
        second_buckets[second] = second_buckets.get(second, 0.0) + pkt_size

    if not second_buckets:
        return []

    window_seconds = max(int(round(clip_duration)), 4)
    scored_windows: list[tuple[float, float]] = []
    for start_second in range(int(usable_end) + 1):
        total_bytes = 0.0
        for second in range(start_second, start_second + window_seconds):
            total_bytes += second_buckets.get(second, 0.0)
        if total_bytes <= 0:
            continue
        scored_windows.append((total_bytes, float(start_second)))

    if not scored_windows:
        return []

    scored_windows.sort(key=lambda item: item[0], reverse=True)
    min_spacing = max(clip_duration * 4, total_duration * 0.12)
    selected: list[float] = []
    for _, clip_time in scored_windows:
        clip_time = round(min(clip_time, usable_end), 3)
        if clip_time < clip_duration:
            continue
        if any(abs(clip_time - existing) < min_spacing for existing in selected):
            continue
        selected.append(clip_time)
        if len(selected) == 3:
            break

    return sorted(selected)


def _scene_change_timestamps(
        source_path: Path,
        total_duration: float,
        clip_duration: float,
        *,
        process_controller: ManagedProcessController | None = None,
) -> list[float]:
    if total_duration <= 0:
        return []
    usable_end = max(total_duration - clip_duration, 0.0)
    if usable_end <= 0:
        return []

    cmd = [
        ffmpeg_binary(),
        "-hide_banner",
        "-nostats",
        "-i",
        str(source_path),
        "-vf",
        "fps=2,scale=320:-2,select='gt(scene,0.28)',showinfo",
        "-an",
        "-f",
        "null",
        "-",
    ]
    result = run_command(cmd, process_controller=process_controller)
    if result.returncode != 0:
        return []

    candidates: list[float] = []
    min_spacing = max(clip_duration * 4, total_duration * 0.12)
    for match in PTS_TIME_RE.finditer(result.stderr):
        pts_time = float(match.group("pts"))
        clip_time = max(min(pts_time - (clip_duration / 2), usable_end), 0.0)
        if clip_time < clip_duration:
            continue
        if candidates and abs(clip_time - candidates[-1]) < min_spacing:
            continue
        candidates.append(round(clip_time, 3))

    if not candidates:
        return []

    target_count = 3
    if len(candidates) <= target_count:
        return candidates

    selected: list[float] = []
    last_index = len(candidates) - 1
    for ratio in (0.15, 0.5, 0.85):
        desired_index = round(last_index * ratio)
        selected.append(candidates[desired_index])

    deduped = []
    for clip_time in selected:
        if clip_time not in deduped:
            deduped.append(clip_time)
    return deduped or candidates[:target_count]


def _slug_seconds(value: float) -> str:
    whole = int(value)
    minutes, seconds = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}-{minutes:02d}-{seconds:02d}"


def _format_crf(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")
