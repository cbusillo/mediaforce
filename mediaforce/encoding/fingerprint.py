from __future__ import annotations

import copy
from functools import lru_cache
import math
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import Any, Mapping

from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.core.evidence import build_evidence_envelope, evidence_staleness, stable_policy_hash
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list

MEDIA_FINGERPRINT_SCHEMA_VERSION = 1
MEDIA_FINGERPRINT_TOOL_NAME = "mediaforce.media_fingerprint"
MEDIA_FINGERPRINT_TOOL_VERSION = "1"
DEFAULT_FINGERPRINT_MAX_FRAMES = 360
DEFAULT_FINGERPRINT_RANGE_COUNT = 3
DEFAULT_FINGERPRINT_SAMPLE_FPS = 2.0
MIN_FINGERPRINT_FRAMES = 90

_FRAME_RE = re.compile(r"(?:^|\n)frame:\s*(?P<frame>\d+)(?P<body>.*?)(?=\nframe:\s*\d+|\Z)", re.DOTALL)
_SIGNALSTAT_RE = re.compile(
    r"lavfi\.signalstats\.(?P<key>[A-Z0-9]+)=(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+))"
)
_AUDIO_NUMBER_RE = re.compile(r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))")


def media_fingerprint_policy_snapshot() -> dict[str, Any]:
    return {
        "schema_version": MEDIA_FINGERPRINT_SCHEMA_VERSION,
        "minimum_measured_frames": MIN_FINGERPRINT_FRAMES,
        "default_sample_fps": DEFAULT_FINGERPRINT_SAMPLE_FPS,
        "findings": [
            "dark_luma",
            "dark_gradient_banding_risk",
            "high_motion",
            "high_texture",
            "duplicate_cadence",
            "animation_cues",
            "likely_film_grain",
            "likely_analog_noise",
            "compression_noise_advisory",
            "uncertain_noise_mix",
            "audio_complexity",
        ],
        "destructive_cleanup_requires_operator_review": True,
    }


def unavailable_media_fingerprint_summary(reason: str) -> dict[str, Any]:
    analysis = _empty_analysis(DEFAULT_FINGERPRINT_MAX_FRAMES, reason=reason)
    return _summary_payload(analysis=analysis, decision=_unknown_decision(0.0, reason))


def analyze_media_fingerprint(
        path: Path,
        *,
        video_stream: Mapping[str, Any] | None,
        audio_streams: list[Mapping[str, Any]] | None,
        duration_seconds: float | None,
        max_frames: int = DEFAULT_FINGERPRINT_MAX_FRAMES,
        range_count: int = DEFAULT_FINGERPRINT_RANGE_COUNT,
        timeout_seconds: float = 60.0,
        sample_fps: float = DEFAULT_FINGERPRINT_SAMPLE_FPS,
        process_controller: ManagedProcessController | None = None,
) -> dict[str, Any]:
    stream = object_dict(video_stream)
    audio_probe = audio_probe_summary(audio_streams or [])
    if not stream:
        return _summary_payload(
            analysis=_empty_analysis(max_frames, audio_probe=audio_probe, reason="No video stream was available."),
            decision=_unknown_decision(0.0, "No video stream was available."),
        )

    ffmpeg_name = ffmpeg_binary()
    starts = _sample_starts(duration_seconds, range_count)
    frames_per_range = max(1, max_frames // max(1, len(starts)))
    ranges: list[dict[str, Any]] = []
    for start_seconds in starts:
        if process_controller is not None:
            process_controller.throw_if_cancelled()
        signal_result = _run_signalstats_range(
            path,
            ffmpeg_name=ffmpeg_name,
            start_seconds=start_seconds,
            frame_limit=frames_per_range,
            sample_fps=sample_fps,
            timeout_seconds=timeout_seconds,
            process_controller=process_controller,
        )
        edge_result = _run_edge_range(
            path,
            ffmpeg_name=ffmpeg_name,
            start_seconds=start_seconds,
            frame_limit=frames_per_range,
            sample_fps=sample_fps,
            timeout_seconds=timeout_seconds,
            process_controller=process_controller,
        )
        signal_frames = parse_signalstats_metadata(signal_result.get("output", ""))
        edge_frames = parse_signalstats_metadata(edge_result.get("output", ""))
        audio = None
        if audio_probe["track_count"] > 0:
            audio_duration = min(max(frames_per_range / max(sample_fps, 0.1), 4.0), 12.0)
            audio_result = _run_audio_range(
                path,
                ffmpeg_name=ffmpeg_name,
                start_seconds=start_seconds,
                duration_seconds=audio_duration,
                timeout_seconds=timeout_seconds,
                process_controller=process_controller,
            )
            audio = {
                "status": audio_result["status"],
                "duration_seconds": round(audio_duration, 3),
                "measurements": parse_ebur128_summary(audio_result.get("output", "")),
            }
            if audio_result.get("failure_reason"):
                audio["failure_reason"] = audio_result["failure_reason"]

        ranges.append(
            _range_payload(
                start_seconds=start_seconds,
                frame_limit=frames_per_range,
                sample_fps=sample_fps,
                signal_status=str(signal_result["status"]),
                edge_status=str(edge_result["status"]),
                signal_frames=signal_frames,
                edge_frames=edge_frames,
                signal_failure=str(signal_result.get("failure_reason") or ""),
                edge_failure=str(edge_result.get("failure_reason") or ""),
                audio=audio,
            )
        )

    sampled_frames = sum(int_value(item.get("sampled_frames")) for item in ranges)
    measured_range_count = sum(1 for item in ranges if item.get("status") == "measured")
    frame_coverage = min(1.0, sampled_frames / max(MIN_FINGERPRINT_FRAMES, 1))
    range_coverage = measured_range_count / max(1, len(ranges))
    coverage = min(frame_coverage, range_coverage)
    analysis = {
        "sampled_frames": sampled_frames,
        "coverage": round(coverage, 4),
        "measured_range_count": measured_range_count,
        "max_frames": max_frames,
        "sample_fps": sample_fps,
        "ranges": ranges,
        "aggregate": aggregate_media_metrics(ranges),
        "audio_probe": audio_probe,
        "tool": {
            "name": MEDIA_FINGERPRINT_TOOL_NAME,
            "version": MEDIA_FINGERPRINT_TOOL_VERSION,
            "ffmpeg_version": _ffmpeg_version(ffmpeg_name),
        },
    }
    return _summary_payload(analysis=analysis, decision=classify_media_fingerprint(analysis))


def parse_signalstats_metadata(output: str) -> list[dict[str, float]]:
    frames: list[dict[str, float]] = []
    for match in _FRAME_RE.finditer(output):
        measurements: dict[str, float] = {}
        for stat_match in _SIGNALSTAT_RE.finditer(match.group("body")):
            measurements[stat_match.group("key").lower()] = float(stat_match.group("value"))
        if measurements:
            frames.append(measurements)
    if frames:
        return frames

    measurements: dict[str, float] = {}
    for stat_match in _SIGNALSTAT_RE.finditer(output):
        measurements[stat_match.group("key").lower()] = float(stat_match.group("value"))
    return [measurements] if measurements else []


def parse_ebur128_summary(output: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip().lower()
        if line.startswith("i:") and "lufs" in line:
            metrics["integrated_loudness_lufs"] = _first_number(raw_line)
        elif line.startswith("lra:") and "lu" in line:
            metrics["loudness_range_lu"] = _first_number(raw_line)
        elif line.startswith("true peak:") or line.startswith("peak:"):
            metrics["true_peak_db"] = _first_number(raw_line)
    return {key: round(value, 3) for key, value in metrics.items() if math.isfinite(value)}


def audio_probe_summary(audio_streams: list[Mapping[str, Any]]) -> dict[str, Any]:
    streams = [object_dict(stream) for stream in audio_streams]
    bitrates = [_positive_float(stream.get("bit_rate")) for stream in streams]
    known_bitrates = [value for value in bitrates if value is not None]
    channels = [int_value(stream.get("channels")) for stream in streams]
    max_channels = max(channels) if channels else 0
    primary = streams[0] if streams else {}
    return {
        "track_count": len(streams),
        "primary_codec": str(primary.get("codec_name") or "unknown") if primary else None,
        "max_channels": max_channels,
        "surround_track_count": sum(1 for value in channels if value >= 6),
        "known_bitrate_fraction": round(len(known_bitrates) / max(1, len(streams)), 4) if streams else 0.0,
        "max_bitrate_bps": int(max(known_bitrates)) if known_bitrates else None,
        "total_bitrate_bps": int(sum(known_bitrates)) if known_bitrates else None,
    }


def aggregate_media_metrics(ranges: list[Mapping[str, Any]]) -> dict[str, Any]:
    measured_ranges = [object_dict(item) for item in ranges if item.get("status") == "measured"]
    if not measured_ranges:
        return {}

    weighted = _weighted_range_metrics(measured_ranges)
    audio_ranges = [object_dict(item.get("audio")) for item in measured_ranges]
    audio_measurements = [
        object_dict(item.get("measurements"))
        for item in audio_ranges
        if object_dict(item.get("measurements"))
    ]
    if audio_measurements:
        weighted["audio_loudness_range_lu_max"] = _rounded(max(
            float_value(item.get("loudness_range_lu")) for item in audio_measurements
        ))
        weighted["audio_integrated_loudness_lufs_mean"] = _rounded(_mean(
            float_value(item.get("integrated_loudness_lufs")) for item in audio_measurements
        ))
        weighted["audio_true_peak_db_max"] = _rounded(max(
            float_value(item.get("true_peak_db")) for item in audio_measurements
        ))
    return weighted


def classify_media_fingerprint(analysis: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = object_dict(analysis)
    sampled_frames = int_value(payload.get("sampled_frames"))
    coverage = _bounded(float_value(payload.get("coverage")))
    if sampled_frames < MIN_FINGERPRINT_FRAMES:
        return _unknown_decision(
            coverage,
            f"Only {sampled_frames} frames were measured; at least {MIN_FINGERPRINT_FRAMES} are required.",
        )
    aggregate = object_dict(payload.get("aggregate"))
    if not aggregate:
        return _unknown_decision(coverage, "No measured fingerprint ranges were available.")

    findings = _visual_findings(aggregate, coverage)
    noise_finding = _noise_finding(aggregate, coverage)
    if noise_finding is not None:
        findings.append(noise_finding)
    audio_finding = _audio_finding(payload, aggregate, coverage)
    if audio_finding is not None:
        findings.append(audio_finding)

    traits = [str(finding["id"]) for finding in findings]
    confidence = _bounded(coverage * (0.70 + min(len(findings), 5) * 0.04))
    low_confidence_advisories = [
        str(finding["id"])
        for finding in findings
        if bool(finding.get("advisory")) and float_value(finding.get("confidence")) < 0.70
    ]
    return {
        "schema_version": MEDIA_FINGERPRINT_SCHEMA_VERSION,
        "status": "measured",
        "confidence": round(confidence, 4),
        "coverage": round(coverage, 4),
        "traits": traits or ["typical"],
        "findings": findings,
        "low_confidence_advisories": low_confidence_advisories,
        "policy_gates": {
            "destructive_cleanup": "operator_review_required",
            "low_confidence_findings": "advisory_only",
            "automatic_denoise": "not_allowed_from_fingerprint_alone",
        },
        "rationale": _decision_rationale(findings, aggregate),
    }


def media_fingerprint_manifest_payload(
        summary: Mapping[str, Any] | None,
        *,
        source_id: str,
        source_fingerprint: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    summary_payload = object_dict(summary)
    if not summary_payload:
        return None, _unknown_decision(0.0, "Media fingerprint evidence is missing; refresh analysis before review.")

    analysis = object_dict(summary_payload.get("analysis"))
    tool = object_dict(analysis.get("tool"))
    if (
            int_value(summary_payload.get("schema_version")) != MEDIA_FINGERPRINT_SCHEMA_VERSION
            or str(tool.get("name") or "") != MEDIA_FINGERPRINT_TOOL_NAME
            or str(tool.get("version") or "") != MEDIA_FINGERPRINT_TOOL_VERSION
    ):
        decision = _unknown_decision(
            0.0,
            "Media fingerprint evidence uses an unsupported analyzer version; refresh analysis before review.",
        )
    else:
        decision = classify_media_fingerprint(analysis)

    tool_version = str(tool.get("version") or MEDIA_FINGERPRINT_TOOL_VERSION)
    ffmpeg_version = str(tool.get("ffmpeg_version") or "").strip()
    if ffmpeg_version:
        tool_version = f"{tool_version}|{ffmpeg_version}"
    ranges = [
        {
            "source_id": source_id,
            "start_seconds": float_value(item.get("start_seconds")),
            "end_seconds": float_value(item.get("end_seconds")) or None,
            "frame_limit": int_value(item.get("frame_limit")),
            "sampled_frames": int_value(item.get("sampled_frames")),
            "status": str(item.get("status") or "unknown"),
        }
        for item in (object_dict(value) for value in object_list(analysis.get("ranges")))
    ]
    envelope = build_evidence_envelope(
        kind="media_fingerprint",
        sources=[{"source_id": source_id, "fingerprint": source_fingerprint}],
        policy_hash=stable_policy_hash(media_fingerprint_policy_snapshot()),
        tool_name=str(tool.get("name") or MEDIA_FINGERPRINT_TOOL_NAME),
        tool_version=tool_version,
        subject={"stream": "video_audio"},
        measurement={
            "unit": "frames",
            "media_ranges": ranges,
            "sampled_frames": int_value(analysis.get("sampled_frames")),
            "coverage": float_value(analysis.get("coverage")),
        },
        result={
            "aggregate": copy.deepcopy(object_dict(analysis.get("aggregate"))),
            "audio_probe": copy.deepcopy(object_dict(analysis.get("audio_probe"))),
            "decision": copy.deepcopy(decision),
        },
    )
    decision["evidence_id"] = envelope["evidence_id"]
    return envelope, decision


def media_fingerprint_staleness(
        envelope: Mapping[str, Any],
        *,
        source_id: str,
        source_fingerprint: str | None,
        tool_version: str | None = None,
) -> dict[str, Any]:
    active_tool_version = tool_version
    if active_tool_version is None:
        active_tool_version = (
            f"{MEDIA_FINGERPRINT_TOOL_VERSION}|{_ffmpeg_version(ffmpeg_binary())}"
        )
    return evidence_staleness(
        envelope,
        sources=[{"source_id": source_id, "fingerprint": source_fingerprint}],
        policy_hash=stable_policy_hash(media_fingerprint_policy_snapshot()),
        tool_name=MEDIA_FINGERPRINT_TOOL_NAME,
        tool_version=active_tool_version,
    )


def _run_signalstats_range(
        path: Path,
        *,
        ffmpeg_name: str,
        start_seconds: float,
        frame_limit: int,
        sample_fps: float,
        timeout_seconds: float,
        process_controller: ManagedProcessController | None,
) -> dict[str, str]:
    filtergraph = (
        f"fps={sample_fps:.3f},scale=320:-2:flags=bicubic,format=yuv420p,"
        "signalstats,metadata=print:file=-"
    )
    return _run_video_filter_range(
        path,
        ffmpeg_name=ffmpeg_name,
        start_seconds=start_seconds,
        frame_limit=frame_limit,
        filtergraph=filtergraph,
        timeout_seconds=timeout_seconds,
        process_controller=process_controller,
    )


def _run_edge_range(
        path: Path,
        *,
        ffmpeg_name: str,
        start_seconds: float,
        frame_limit: int,
        sample_fps: float,
        timeout_seconds: float,
        process_controller: ManagedProcessController | None,
) -> dict[str, str]:
    filtergraph = (
        f"fps={sample_fps:.3f},scale=320:-2:flags=bicubic,format=gray,"
        "edgedetect=low=0.08:high=0.22,signalstats,metadata=print:file=-"
    )
    return _run_video_filter_range(
        path,
        ffmpeg_name=ffmpeg_name,
        start_seconds=start_seconds,
        frame_limit=frame_limit,
        filtergraph=filtergraph,
        timeout_seconds=timeout_seconds,
        process_controller=process_controller,
    )


def _run_video_filter_range(
        path: Path,
        *,
        ffmpeg_name: str,
        start_seconds: float,
        frame_limit: int,
        filtergraph: str,
        timeout_seconds: float,
        process_controller: ManagedProcessController | None,
) -> dict[str, str]:
    command = [
        ffmpeg_name,
        "-hide_banner",
        "-nostats",
        "-v",
        "error",
        "-ss",
        f"{start_seconds:.3f}",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-frames:v",
        str(frame_limit),
        "-vf",
        filtergraph,
        "-an",
        "-sn",
        "-dn",
        "-f",
        "null",
        "-",
    ]
    return _run_bounded_media_command(command, timeout_seconds=timeout_seconds, process_controller=process_controller)


def _run_audio_range(
        path: Path,
        *,
        ffmpeg_name: str,
        start_seconds: float,
        duration_seconds: float,
        timeout_seconds: float,
        process_controller: ManagedProcessController | None,
) -> dict[str, str]:
    command = [
        ffmpeg_name,
        "-hide_banner",
        "-nostats",
        "-v",
        "info",
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        f"{duration_seconds:.3f}",
        "-i",
        str(path),
        "-map",
        "0:a:0?",
        "-af",
        "ebur128=peak=true:framelog=quiet",
        "-vn",
        "-sn",
        "-dn",
        "-f",
        "null",
        "-",
    ]
    return _run_bounded_media_command(command, timeout_seconds=timeout_seconds, process_controller=process_controller)


def _run_bounded_media_command(
        command: list[str],
        *,
        timeout_seconds: float,
        process_controller: ManagedProcessController | None,
) -> dict[str, str]:
    if process_controller is not None:
        process_controller.throw_if_cancelled()
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return {"status": "failed", "output": "", "failure_reason": str(exc)}

    if process_controller is not None:
        process_controller.attach(process, terminate_process_group=True)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_process(process)
            stdout, stderr = process.communicate()
            return {
                "status": "timeout",
                "output": f"{stdout or ''}\n{stderr or ''}",
                "failure_reason": f"Timed out after {timeout_seconds:g} seconds.",
            }
    finally:
        if process_controller is not None:
            process_controller.clear(process)

    if process_controller is not None and process_controller.cancelled:
        raise ProcessCancelledError("Operation was cancelled.")
    status = "measured" if process.returncode == 0 else "failed"
    failure = "" if status == "measured" else f"ffmpeg exited with code {process.returncode}."
    return {"status": status, "output": f"{stdout or ''}\n{stderr or ''}", "failure_reason": failure}


def _terminate_process(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                return


def _range_payload(
        *,
        start_seconds: float,
        frame_limit: int,
        sample_fps: float,
        signal_status: str,
        edge_status: str,
        signal_frames: list[dict[str, float]],
        edge_frames: list[dict[str, float]],
        signal_failure: str,
        edge_failure: str,
        audio: dict[str, Any] | None,
) -> dict[str, Any]:
    measurements = _range_measurements(signal_frames, edge_frames)
    sampled_frames = len(signal_frames)
    status = "measured" if signal_status == "measured" and sampled_frames > 0 else signal_status
    payload: dict[str, Any] = {
        "start_seconds": round(start_seconds, 3),
        "end_seconds": round(start_seconds + (sampled_frames / max(sample_fps, 0.1)), 3)
        if sampled_frames
        else None,
        "frame_limit": frame_limit,
        "sampled_frames": sampled_frames,
        "status": status,
        "edge_status": edge_status,
        "measurements": measurements,
    }
    reasons = [reason for reason in (signal_failure, edge_failure) if reason]
    if reasons:
        payload["failure_reasons"] = reasons
    if audio is not None:
        payload["audio"] = audio
    return payload


def _range_measurements(signal_frames: list[dict[str, float]], edge_frames: list[dict[str, float]]) -> dict[str, Any]:
    if not signal_frames:
        return {}
    luma = [_metric(frame, "yavg") for frame in signal_frames]
    ydiff = [_metric(frame, "ydif") for frame in signal_frames if "ydif" in frame]
    ylow = [_metric(frame, "ymin") for frame in signal_frames if "ymin" in frame]
    yhigh = [_metric(frame, "ymax") for frame in signal_frames if "ymax" in frame]
    contrast = [max(high - low, 0.0) for low, high in zip(ylow, yhigh, strict=False)]
    uavg = [_metric(frame, "uavg") for frame in signal_frames if "uavg" in frame]
    vavg = [_metric(frame, "vavg") for frame in signal_frames if "vavg" in frame]
    edge_density = [_metric(frame, "yavg") / 255.0 for frame in edge_frames if "yavg" in frame]
    frame_count = len(signal_frames)
    dark_flags = [value < 45.0 for value in luma]
    low_contrast_flags = [value < 36.0 for value in contrast] if contrast else [False] * frame_count
    low_edge_flags = [value < 0.035 for value in edge_density] if edge_density else [False] * frame_count
    low_motion_flags = [value < 1.2 for value in ydiff] if ydiff else [False] * frame_count
    smooth_temporal_noise_flags = [
        edge_density[index] < 0.035 and 1.2 <= ydiff[index] <= 6.0
        for index in range(min(len(edge_density), len(ydiff)))
    ]
    gradient_count = sum(
        1
        for index, dark in enumerate(dark_flags[:len(low_contrast_flags)])
        if dark and low_contrast_flags[index] and (index >= len(low_edge_flags) or low_edge_flags[index])
    )
    return {
        "luma_mean": _rounded(_mean(luma)),
        "luma_p10": _rounded(_percentile(luma, 10)),
        "luma_p90": _rounded(_percentile(luma, 90)),
        "dark_frame_fraction": _fraction(sum(dark_flags), frame_count),
        "gradient_frame_fraction": _fraction(gradient_count, frame_count),
        "ydif_mean": _rounded(_mean(ydiff)),
        "ydif_p50": _rounded(_percentile(ydiff, 50)),
        "ydif_p90": _rounded(_percentile(ydiff, 90)),
        "high_motion_frame_fraction": _fraction(sum(1 for value in ydiff if value >= 12.0), len(ydiff)),
        "duplicate_like_frame_fraction": _fraction(sum(low_motion_flags), len(low_motion_flags)),
        "edge_density_mean": _rounded(_mean(edge_density)),
        "edge_density_p90": _rounded(_percentile(edge_density, 90)),
        "high_texture_frame_fraction": _fraction(sum(1 for value in edge_density if value >= 0.080), len(edge_density)),
        "temporal_noise_proxy": _rounded(_percentile([value for value in ydiff if value >= 1.2], 50)),
        "chroma_instability": _rounded(_chroma_instability(uavg, vavg)),
        "smooth_temporal_noise_fraction": _fraction(sum(smooth_temporal_noise_flags), len(smooth_temporal_noise_flags)),
    }


def _weighted_range_metrics(measured_ranges: list[dict[str, Any]]) -> dict[str, Any]:
    weighted_keys = (
        "dark_frame_fraction",
        "gradient_frame_fraction",
        "high_motion_frame_fraction",
        "duplicate_like_frame_fraction",
        "high_texture_frame_fraction",
        "smooth_temporal_noise_fraction",
    )
    percentile_keys = (
        "luma_mean",
        "luma_p10",
        "luma_p90",
        "ydif_mean",
        "ydif_p50",
        "ydif_p90",
        "edge_density_mean",
        "edge_density_p90",
        "temporal_noise_proxy",
        "chroma_instability",
    )
    total_frames = sum(max(int_value(item.get("sampled_frames")), 0) for item in measured_ranges)
    result: dict[str, Any] = {"range_count": len(measured_ranges), "sampled_frames": total_frames}
    for key in weighted_keys:
        numerator = sum(
            float_value(object_dict(item.get("measurements")).get(key)) * max(int_value(item.get("sampled_frames")), 0)
            for item in measured_ranges
        )
        result[key] = _rounded(numerator / total_frames) if total_frames > 0 else 0.0
    for key in percentile_keys:
        values = [float_value(object_dict(item.get("measurements")).get(key)) for item in measured_ranges]
        result[key] = _rounded(_mean(values))
    result["banding_risk_score"] = _rounded(
        result["gradient_frame_fraction"] * (0.65 + min(result["dark_frame_fraction"], 1.0) * 0.35)
    )
    return result


def _visual_findings(aggregate: Mapping[str, Any], coverage: float) -> list[dict[str, Any]]:
    dark_fraction = float_value(aggregate.get("dark_frame_fraction"))
    gradient_fraction = float_value(aggregate.get("gradient_frame_fraction"))
    banding_risk = float_value(aggregate.get("banding_risk_score"))
    motion_fraction = float_value(aggregate.get("high_motion_frame_fraction"))
    ydif_p90 = float_value(aggregate.get("ydif_p90"))
    texture_fraction = float_value(aggregate.get("high_texture_frame_fraction"))
    edge_p90 = float_value(aggregate.get("edge_density_p90"))
    duplicate_fraction = float_value(aggregate.get("duplicate_like_frame_fraction"))
    findings: list[dict[str, Any]] = []
    if dark_fraction >= 0.25:
        findings.append(_finding(
            "dark_luma",
            confidence=_bounded((0.55 + dark_fraction * 0.35) * coverage),
            coverage=coverage,
            rationale=f"{dark_fraction:.0%} of measured frames have low luma.",
        ))
    if gradient_fraction >= 0.16 or banding_risk >= 0.14:
        findings.append(_finding(
            "dark_gradient_banding_risk",
            confidence=_bounded((0.50 + max(gradient_fraction, banding_risk) * 0.45) * coverage),
            coverage=coverage,
            advisory=True,
            rationale="Measured dark, low-contrast, low-edge frames are vulnerable to visible banding.",
        ))
    if motion_fraction >= 0.15 or ydif_p90 >= 12.0:
        findings.append(_finding(
            "high_motion",
            confidence=_bounded((0.55 + max(motion_fraction, min(ydif_p90 / 24.0, 1.0)) * 0.35) * coverage),
            coverage=coverage,
            rationale="Frame-difference measurements show high-motion review risk.",
        ))
    if texture_fraction >= 0.22 or edge_p90 >= 0.08:
        findings.append(_finding(
            "high_texture",
            confidence=_bounded((0.55 + max(texture_fraction, min(edge_p90 / 0.16, 1.0)) * 0.35) * coverage),
            coverage=coverage,
            rationale="Edge-density measurements show fine texture or detail that can be damaged by compression.",
        ))
    if 0.35 <= duplicate_fraction <= 0.92:
        findings.append(_finding(
            "duplicate_cadence",
            confidence=_bounded((0.50 + duplicate_fraction * 0.35) * coverage),
            coverage=coverage,
            advisory=True,
            rationale=f"{duplicate_fraction:.0%} of measured frame steps are duplicate-like.",
        ))
    if duplicate_fraction >= 0.35 and edge_p90 >= 0.035 and motion_fraction < 0.25:
        findings.append(_finding(
            "animation_cues",
            confidence=_bounded((0.45 + duplicate_fraction * 0.30 + min(edge_p90 / 0.12, 1.0) * 0.15) * coverage),
            coverage=coverage,
            advisory=True,
            rationale="Duplicate-like cadence with clean edge structure suggests animation-style review risk.",
        ))
    return findings


def _noise_finding(aggregate: Mapping[str, Any], coverage: float) -> dict[str, Any] | None:
    temporal_noise = float_value(aggregate.get("temporal_noise_proxy"))
    chroma_instability = float_value(aggregate.get("chroma_instability"))
    edge_p90 = float_value(aggregate.get("edge_density_p90"))
    smooth_temporal_noise = float_value(aggregate.get("smooth_temporal_noise_fraction"))
    if temporal_noise >= 2.2 and edge_p90 >= 0.075:
        return _finding(
            "likely_film_grain",
            confidence=min(_bounded((0.42 + min(temporal_noise / 8.0, 1.0) * 0.22 + min(edge_p90 / 0.16, 1.0) * 0.20) * coverage), 0.78),
            coverage=coverage,
            advisory=True,
            rationale="Fine edge density plus temporal luma variation is consistent with grain; preserve/detail review is required.",
        )
    if temporal_noise >= 2.2 and chroma_instability >= 1.2:
        return _finding(
            "likely_analog_noise",
            confidence=min(_bounded((0.35 + min(temporal_noise / 8.0, 1.0) * 0.20 + min(chroma_instability / 4.0, 1.0) * 0.20) * coverage), 0.65),
            coverage=coverage,
            advisory=True,
            rationale="Temporal luma variation with chroma instability is consistent with analog noise, but cleanup remains manual.",
        )
    if smooth_temporal_noise >= 0.20:
        return _finding(
            "compression_noise_advisory",
            confidence=min(_bounded((0.30 + smooth_temporal_noise * 0.40) * coverage), 0.55),
            coverage=coverage,
            advisory=True,
            rationale="Smooth low-edge areas show temporal fluctuation consistent with possible compression noise.",
        )
    if temporal_noise >= 2.0:
        return _finding(
            "uncertain_noise_mix",
            confidence=min(_bounded((0.30 + min(temporal_noise / 8.0, 1.0) * 0.25) * coverage), 0.50),
            coverage=coverage,
            advisory=True,
            rationale="Temporal variation is measurable, but the evidence is not specific enough to separate grain and noise.",
        )
    return None


def _audio_finding(analysis: Mapping[str, Any], aggregate: Mapping[str, Any], coverage: float) -> dict[str, Any] | None:
    audio_probe = object_dict(analysis.get("audio_probe"))
    track_count = int_value(audio_probe.get("track_count"))
    max_channels = int_value(audio_probe.get("max_channels"))
    max_bitrate = int_value(audio_probe.get("max_bitrate_bps"))
    loudness_range = float_value(aggregate.get("audio_loudness_range_lu_max"))
    if track_count <= 0:
        return None
    reasons: list[str] = []
    if track_count > 1:
        reasons.append(f"{track_count} audio tracks")
    if max_channels >= 6:
        reasons.append(f"{max_channels}-channel audio")
    if max_bitrate >= 768_000:
        reasons.append("high-bitrate audio")
    if loudness_range >= 12.0:
        reasons.append("wide measured loudness range")
    if not reasons:
        return None
    return _finding(
        "audio_complexity",
        confidence=_bounded((0.55 + min(len(reasons), 3) * 0.10) * max(coverage, 0.5)),
        coverage=coverage,
        advisory=True,
        rationale="Audio review cue: " + ", ".join(reasons) + ".",
    )


def _finding(
        finding_id: str,
        *,
        confidence: float,
        coverage: float,
        rationale: str,
        advisory: bool = False,
) -> dict[str, Any]:
    return {
        "id": finding_id,
        "confidence": round(_bounded(confidence), 4),
        "coverage": round(_bounded(coverage), 4),
        "advisory": advisory,
        "rationale": rationale,
    }


def _decision_rationale(findings: list[Mapping[str, Any]], aggregate: Mapping[str, Any]) -> str:
    if not findings:
        return "Measured luma, motion, texture, duplicate cadence, noise, and audio facts look typical for review."
    labels = ", ".join(str(finding.get("id")) for finding in findings)
    return (
        f"Measured fingerprint findings: {labels}. "
        f"Dark={float_value(aggregate.get('dark_frame_fraction')):.0%}, "
        f"motion={float_value(aggregate.get('ydif_p90')):.2f} p90, "
        f"texture={float_value(aggregate.get('edge_density_p90')):.3f} p90."
    )


def _unknown_decision(coverage: float, rationale: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": MEDIA_FINGERPRINT_SCHEMA_VERSION,
        "status": "unknown",
        "confidence": 0.0,
        "coverage": round(_bounded(coverage), 4),
        "traits": ["unknown"],
        "findings": [],
        "low_confidence_advisories": [],
        "policy_gates": {
            "destructive_cleanup": "not_allowed_without_measured_evidence",
            "low_confidence_findings": "advisory_only",
            "automatic_denoise": "not_allowed_from_fingerprint_alone",
        },
        "rationale": rationale or "Media fingerprint evidence is missing or too sparse to classify.",
    }


def _summary_payload(analysis: Mapping[str, Any], decision: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": MEDIA_FINGERPRINT_SCHEMA_VERSION,
        "analysis": copy.deepcopy(dict(analysis)),
        "decision": copy.deepcopy(dict(decision)),
    }


def _empty_analysis(
        max_frames: int,
        *,
        audio_probe: Mapping[str, Any] | None = None,
        reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sampled_frames": 0,
        "coverage": 0.0,
        "measured_range_count": 0,
        "max_frames": max_frames,
        "sample_fps": DEFAULT_FINGERPRINT_SAMPLE_FPS,
        "ranges": [],
        "aggregate": {},
        "audio_probe": copy.deepcopy(object_dict(audio_probe)),
        "tool": {"name": MEDIA_FINGERPRINT_TOOL_NAME, "version": MEDIA_FINGERPRINT_TOOL_VERSION, "ffmpeg_version": None},
    }
    if reason:
        payload["unknown_reason"] = reason
    return payload


@lru_cache(maxsize=None)
def _ffmpeg_version(ffmpeg_name: str) -> str:
    try:
        result = subprocess.run([ffmpeg_name, "-version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.splitlines()[0].strip() if result.stdout else "unknown"


def _sample_starts(duration_seconds: float | None, range_count: int) -> list[float]:
    if duration_seconds is None or duration_seconds <= 0:
        return [0.0]
    count = max(1, min(range_count, DEFAULT_FINGERPRINT_RANGE_COUNT))
    if count == 1 or duration_seconds < 30:
        return [0.0]
    fractions = (0.10, 0.50, 0.90)
    return [round(duration_seconds * fraction, 3) for fraction in fractions[:count]]


def _metric(frame: Mapping[str, float], key: str) -> float:
    value = frame.get(key)
    return float(value) if value is not None else 0.0


def _mean(values: Any) -> float:
    materialized = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return sum(materialized) / len(materialized) if materialized else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    materialized = sorted(value for value in values if math.isfinite(value))
    if not materialized:
        return 0.0
    if len(materialized) == 1:
        return materialized[0]
    rank = (len(materialized) - 1) * (percentile / 100.0)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return materialized[int(rank)]
    return materialized[lower] + (materialized[upper] - materialized[lower]) * (rank - lower)


def _fraction(count: int, total: int) -> float:
    return _rounded(count / total) if total > 0 else 0.0


def _rounded(value: float) -> float:
    return round(value, 4) if math.isfinite(value) else 0.0


def _bounded(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(max(value, 0.0), 1.0)


def _positive_float(value: object) -> float | None:
    number = float_value(value)
    return number if number > 0 else None


def _chroma_instability(uavg: list[float], vavg: list[float]) -> float:
    deltas: list[float] = []
    for values in (uavg, vavg):
        deltas.extend(abs(values[index] - values[index - 1]) for index in range(1, len(values)))
    return _percentile(deltas, 50)


def _first_number(line: str) -> float:
    match = _AUDIO_NUMBER_RE.search(line)
    return float(match.group(1)) if match else 0.0
