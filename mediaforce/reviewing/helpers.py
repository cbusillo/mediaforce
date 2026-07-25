import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list

MAX_AUTO_SCAN_DURATION_SECONDS = 30 * 60


@dataclass(frozen=True, slots=True)
class ReviewMoment:
    timestamp_seconds: float
    duration_seconds: float
    role: str
    risk_tags: tuple[str, ...]
    rationale: str
    confidence: float
    coverage: float
    evidence_id: str | None = None


def planned_audio_action(audio_track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    codec = str(audio_track.get("codec_name") or "").lower()
    if codec in {str(name).lower() for name in object_list(audio_policy.get("copy_codecs"))}:
        return "copy"
    if codec in {str(name).lower() for name in object_list(audio_policy.get("convert_to_opus_codecs"))}:
        return "libopus"
    return "copy"


def select_primary_audio_track(audio_tracks: list[dict[str, Any]]) -> dict[str, Any]:
    english = [track for track in audio_tracks if track.get("language") == "eng"]
    candidates = english or [track for track in audio_tracks if track.get("language") in {None, "und"}] or audio_tracks
    if not candidates:
        raise ValueError("No audio tracks available")

    def sort_key(track: dict[str, Any]) -> tuple[int, int, int]:
        return (
            0 if track.get("default") else 1,
            -int_value(track.get("channels")),
            int_value(track.get("index")),
        )

    return sorted(candidates, key=sort_key)[0]


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
        complexity_timestamps_fn: Callable[..., list[float]],
        scene_change_timestamps_fn: Callable[..., list[float]],
        default_timestamps_fn: Callable[[float, float], list[float]],
) -> list[float]:
    if total_duration <= MAX_AUTO_SCAN_DURATION_SECONDS:
        complexity_points = complexity_timestamps_fn(
            source_path,
            total_duration,
            clip_duration,
            process_controller=process_controller,
        )
        if complexity_points:
            return complexity_points
    scene_points = scene_change_timestamps_fn(
        source_path,
        total_duration,
        clip_duration,
        process_controller=process_controller,
    )
    if scene_points:
        return scene_points
    return default_timestamps_fn(total_duration, clip_duration)


def recommend_review_moments(
        source_path: Path,
        total_duration: float,
        clip_duration: float,
        *,
        media_fingerprint: dict[str, Any] | None = None,
        media_fingerprint_decision: dict[str, Any] | None = None,
        analyze_source: bool = True,
        process_controller: Any = None,
        auto_timestamps_fn: Callable[..., list[float]],
        default_timestamps_fn: Callable[[float, float], list[float]],
) -> list[ReviewMoment]:
    fingerprint_moments = fingerprint_review_moments(
        total_duration,
        clip_duration,
        media_fingerprint=media_fingerprint,
        media_fingerprint_decision=media_fingerprint_decision,
    )
    if fingerprint_moments:
        return fingerprint_moments

    timestamps = (
        auto_timestamps_fn(
            source_path,
            total_duration,
            clip_duration,
            process_controller=process_controller,
        )
        if analyze_source
        else default_timestamps_fn(total_duration, clip_duration)
    )
    rationale = (
        "Selected by packet-size, scene-change, or default timestamp analysis."
        if analyze_source
        else "Selected from duration-based coverage because the controller cannot read the source directly."
    )
    return [
        ReviewMoment(
            timestamp_seconds=timestamp,
            duration_seconds=clip_duration,
            role="typical" if index == 0 else "coverage",
            risk_tags=("fallback",),
            rationale=rationale,
            confidence=0.35,
            coverage=0.0,
        )
        for index, timestamp in enumerate(timestamps)
    ]


def fingerprint_review_moments(
        total_duration: float,
        clip_duration: float,
        *,
        media_fingerprint: dict[str, Any] | None,
        media_fingerprint_decision: dict[str, Any] | None,
) -> list[ReviewMoment]:
    summary = object_dict(media_fingerprint)
    analysis = object_dict(summary.get("analysis"))
    decision = object_dict(media_fingerprint_decision or summary.get("decision"))
    if str(decision.get("status") or "") != "measured":
        return []

    usable_end = max(total_duration - clip_duration, 0.0)
    ranges = [
        object_dict(item)
        for item in object_list(analysis.get("ranges"))
        if object_dict(item).get("status") == "measured"
    ]
    if not ranges:
        return []

    evidence_id = str(decision.get("evidence_id") or "") or None
    typical_range = min(ranges, key=_typical_range_sort_key)
    selected = [
        _range_review_moment(
            typical_range,
            clip_duration=clip_duration,
            usable_end=usable_end,
            role="typical",
            evidence_id=evidence_id,
            fallback_rationale="Closest measured range to typical luma, motion, texture, and duplicate cadence.",
        )
    ]
    risk_candidates = sorted(ranges, key=_hard_range_sort_key, reverse=True)
    for candidate in risk_candidates:
        moment = _range_review_moment(
            candidate,
            clip_duration=clip_duration,
            usable_end=usable_end,
            role="hard",
            evidence_id=evidence_id,
            fallback_rationale="Measured range has elevated compression-risk signals.",
        )
        if not moment.risk_tags:
            continue
        if any(abs(moment.timestamp_seconds - existing.timestamp_seconds) < max(clip_duration * 1.5, 4.0) for existing in selected):
            continue
        selected.append(moment)
        if len(selected) >= 3:
            break
    return sorted(selected, key=lambda moment: moment.timestamp_seconds)


def review_moment_payload(moment: ReviewMoment) -> dict[str, Any]:
    return {
        "timestamp_seconds": moment.timestamp_seconds,
        "duration_seconds": moment.duration_seconds,
        "role": moment.role,
        "risk_tags": list(moment.risk_tags),
        "rationale": moment.rationale,
        "confidence": moment.confidence,
        "coverage": moment.coverage,
        "evidence_id": moment.evidence_id,
    }


def _range_review_moment(
        media_range: dict[str, Any],
        *,
        clip_duration: float,
        usable_end: float,
        role: str,
        evidence_id: str | None,
        fallback_rationale: str,
) -> ReviewMoment:
    measurements = object_dict(media_range.get("measurements"))
    tags = _range_risk_tags(measurements, object_dict(media_range.get("audio")))
    start_seconds = max(min(float_value(media_range.get("start_seconds")), usable_end), 0.0)
    sampled_frames = int_value(media_range.get("sampled_frames"))
    coverage = min(1.0, sampled_frames / 90.0) if sampled_frames else 0.0
    confidence = min(1.0, 0.45 + _hard_range_score(media_range) * 0.35 + coverage * 0.20)
    rationale = _moment_rationale(tags, measurements, fallback_rationale)
    return ReviewMoment(
        timestamp_seconds=round(start_seconds, 3),
        duration_seconds=clip_duration,
        role=role,
        risk_tags=tuple(tags),
        rationale=rationale,
        confidence=round(confidence, 4),
        coverage=round(coverage, 4),
        evidence_id=evidence_id,
    )


def _range_risk_tags(measurements: Mapping[str, Any], audio: Mapping[str, Any]) -> list[str]:
    tags: list[str] = []
    if float_value(measurements.get("dark_frame_fraction")) >= 0.25:
        tags.append("dark_luma")
    if float_value(measurements.get("gradient_frame_fraction")) >= 0.16:
        tags.append("dark_gradient_banding_risk")
    if float_value(measurements.get("high_motion_frame_fraction")) >= 0.15 or float_value(measurements.get("ydif_p90")) >= 12.0:
        tags.append("high_motion")
    if float_value(measurements.get("high_texture_frame_fraction")) >= 0.22 or float_value(measurements.get("edge_density_p90")) >= 0.08:
        tags.append("high_texture")
    if float_value(measurements.get("duplicate_like_frame_fraction")) >= 0.35:
        tags.append("duplicate_cadence")
    if float_value(measurements.get("temporal_noise_proxy")) >= 2.2:
        tags.append("texture_noise_advisory")
    audio_measurements = object_dict(audio.get("measurements"))
    if float_value(audio_measurements.get("loudness_range_lu")) >= 12.0:
        tags.append("audio_loudness_range")
    return tags


def _moment_rationale(tags: list[str], measurements: Mapping[str, Any], fallback: str) -> str:
    if not tags:
        return fallback
    return (
        "Selected because measured review risks include "
        + ", ".join(tags)
        + f" (dark={float_value(measurements.get('dark_frame_fraction')):.0%}, "
        + f"motion={float_value(measurements.get('ydif_p90')):.2f}, "
        + f"texture={float_value(measurements.get('edge_density_p90')):.3f})."
    )


def _typical_range_sort_key(media_range: Mapping[str, Any]) -> tuple[float, float, float]:
    measurements = object_dict(media_range.get("measurements"))
    return (
        abs(float_value(measurements.get("luma_mean")) - 100.0)
        + float_value(measurements.get("dark_frame_fraction")) * 40.0,
        abs(float_value(measurements.get("ydif_p50")) - 3.0),
        abs(float_value(measurements.get("edge_density_mean")) - 0.035),
    )


def _hard_range_sort_key(media_range: Mapping[str, Any]) -> tuple[float, float]:
    return (_hard_range_score(media_range), float_value(media_range.get("start_seconds")))


def _hard_range_score(media_range: Mapping[str, Any]) -> float:
    measurements = object_dict(media_range.get("measurements"))
    score = 0.0
    score += float_value(measurements.get("dark_frame_fraction")) * 0.16
    score += float_value(measurements.get("gradient_frame_fraction")) * 0.20
    score += min(float_value(measurements.get("ydif_p90")) / 24.0, 1.0) * 0.18
    score += float_value(measurements.get("high_motion_frame_fraction")) * 0.16
    score += min(float_value(measurements.get("edge_density_p90")) / 0.16, 1.0) * 0.16
    score += float_value(measurements.get("duplicate_like_frame_fraction")) * 0.08
    score += min(float_value(measurements.get("temporal_noise_proxy")) / 8.0, 1.0) * 0.06
    audio = object_dict(object_dict(media_range.get("audio")).get("measurements"))
    score += min(float_value(audio.get("loudness_range_lu")) / 24.0, 1.0) * 0.05
    return min(score, 1.0)


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
