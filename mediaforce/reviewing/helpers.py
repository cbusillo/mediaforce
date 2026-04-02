import csv
import re
from pathlib import Path
from typing import Any, Callable

from mediaforce.core.type_defs import int_value, object_list


def planned_audio_action(audio_track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    codec = str(audio_track.get("codec_name") or "").lower()
    if codec in {str(name).lower() for name in object_list(audio_policy.get("copy_codecs"))}:
        return "copy"
    if codec in {str(name).lower() for name in object_list(audio_policy.get("convert_to_opus_codecs"))}:
        return "libopus"
    return "copy"


def planned_opus_bitrate(audio_track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    channels = int_value(audio_track.get("channels")) or 2
    if channels >= 8:
        return str(audio_policy.get("surround_7_1_opus_bitrate") or "320k")
    if channels >= 6:
        return str(audio_policy.get("surround_5_1_opus_bitrate") or "224k")
    return str(audio_policy.get("stereo_opus_bitrate") or "128k")


def default_timestamps(total_duration: float, clip_duration: float) -> list[float]:
    if total_duration <= 0:
        return [0.0]
    usable = max(total_duration - clip_duration, 0.0)
    if usable == 0:
        return [0.0]
    return [round(usable * ratio, 3) for ratio in (0.2, 0.5, 0.8)]


def auto_timestamps(
        source_path: Path,
        total_duration: float,
        clip_duration: float,
        *,
        process_controller: Any = None,
        complexity_timestamps: Callable[..., list[float]],
        scene_change_timestamps: Callable[..., list[float]],
        default_timestamps: Callable[[float, float], list[float]],
) -> list[float]:
    complexity_points = complexity_timestamps(
        source_path,
        total_duration,
        clip_duration,
        process_controller=process_controller,
    )
    if complexity_points:
        return complexity_points
    scene_points = scene_change_timestamps(
        source_path,
        total_duration,
        clip_duration,
        process_controller=process_controller,
    )
    if scene_points:
        return scene_points
    return default_timestamps(total_duration, clip_duration)


def complexity_timestamps(
        source_path: Path,
        total_duration: float,
        clip_duration: float,
        *,
        process_controller: Any = None,
        ffprobe_binary: Callable[[], str],
        run_command: Callable[..., Any],
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


def scene_change_timestamps(
        source_path: Path,
        total_duration: float,
        clip_duration: float,
        *,
        process_controller: Any = None,
        ffmpeg_binary: Callable[[], str],
        run_command: Callable[..., Any],
        pts_time_re: re.Pattern[str],
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
    for match in pts_time_re.finditer(result.stderr):
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


def slug_seconds(value: float) -> str:
    whole = int(value)
    minutes, seconds = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}-{minutes:02d}-{seconds:02d}"


def format_crf(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")
