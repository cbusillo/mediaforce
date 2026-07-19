from __future__ import annotations

import copy
from functools import lru_cache
import math
from pathlib import Path
import re
import subprocess
from typing import Any, Literal, Mapping

from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.core.evidence import build_evidence_envelope, evidence_envelope_valid, stable_policy_hash
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list

CADENCE_SCHEMA_VERSION = 1
CADENCE_EVIDENCE_KIND = "cadence_analysis"
CADENCE_TOOL_NAME = "mediaforce.ffmpeg_idet"
CADENCE_TOOL_VERSION = "1"
DEFAULT_IDET_MAX_FRAMES = 600
DEFAULT_IDET_RANGE_COUNT = 3
MIN_IDET_FRAMES = 120

CadenceClass = Literal["progressive", "tff", "bff", "telecine", "mixed", "unknown"]
CadenceTransform = Literal["none", "bwdif_tff", "bwdif_bff", "fieldmatch_decimate"]

CADENCE_FILTERS: dict[CadenceTransform, str | None] = {
    "none": None,
    "bwdif_tff": "bwdif=mode=send_frame:parity=tff:deint=all",
    "bwdif_bff": "bwdif=mode=send_frame:parity=bff:deint=all",
    "fieldmatch_decimate": "fieldmatch,decimate",
}
EXPECTED_TRANSFORMS: dict[CadenceClass, CadenceTransform | None] = {
    "progressive": "none",
    "tff": "bwdif_tff",
    "bff": "bwdif_bff",
    "telecine": "fieldmatch_decimate",
    "mixed": None,
    "unknown": None,
}

_IDET_MULTI_RE = re.compile(
    r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)",
    re.IGNORECASE,
)
_IDET_REPEATED_RE = re.compile(
    r"Repeated Fields:\s*Neither:\s*(\d+)\s*Top:\s*(\d+)\s*Bottom:\s*(\d+)",
    re.IGNORECASE,
)


class CadenceResolutionError(RuntimeError):
    pass


def unavailable_cadence_summary(reason: str) -> dict[str, Any]:
    probe = {
        "field_order": "unknown",
        "average_frame_rate": None,
        "nominal_frame_rate": None,
        "time_base": None,
        "idet_required": True,
    }
    return _summary_payload(
        probe=probe,
        analysis=_empty_analysis(DEFAULT_IDET_MAX_FRAMES),
        decision=_unknown_decision(0.0, reason),
    )


def cadence_policy_snapshot() -> dict[str, Any]:
    return {
        "schema_version": CADENCE_SCHEMA_VERSION,
        "minimum_measured_frames": MIN_IDET_FRAMES,
        "automatic_transforms": dict(EXPECTED_TRANSFORMS),
    }


def analyze_cadence(
        path: Path,
        *,
        video_stream: Mapping[str, Any] | None,
        duration_seconds: float | None,
        max_frames: int = DEFAULT_IDET_MAX_FRAMES,
        range_count: int = DEFAULT_IDET_RANGE_COUNT,
        timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    stream = object_dict(video_stream)
    probe = {
        "field_order": _normalized_field_order(stream.get("field_order")),
        "average_frame_rate": _optional_text(stream.get("avg_frame_rate")),
        "nominal_frame_rate": _optional_text(stream.get("r_frame_rate")),
        "time_base": _optional_text(stream.get("time_base")),
    }
    probe["idet_required"] = _probe_requires_idet(probe) if stream else False
    if not stream:
        return _summary_payload(probe=probe, analysis=_empty_analysis(max_frames), decision=_unknown_decision(0.0))
    if probe["field_order"] == "progressive" and not probe["idet_required"]:
        decision = _decision(
            classification="progressive",
            confidence=0.98,
            coverage=1.0,
            transform="none",
            rationale="ffprobe reports progressive frame order.",
        )
        return _summary_payload(probe=probe, analysis=_empty_analysis(max_frames), decision=decision)

    ffmpeg_name = ffmpeg_binary()
    starts = _sample_starts(duration_seconds, range_count)
    frames_per_range = max(1, max_frames // max(1, len(starts)))
    frame_rate = _rational_value(probe["average_frame_rate"] or probe["nominal_frame_rate"])
    aggregate = _empty_counts()
    ranges: list[dict[str, Any]] = []
    for start_seconds in starts:
        result = _run_idet_range(
            path,
            ffmpeg_name=ffmpeg_name,
            start_seconds=start_seconds,
            frame_limit=frames_per_range,
            timeout_seconds=timeout_seconds,
        )
        counts = parse_idet_output(result["stderr"]) if result["status"] == "measured" else _empty_counts()
        _merge_counts(aggregate, counts)
        sampled_frames = _sampled_frames(counts)
        ranges.append(
            {
                "start_seconds": round(start_seconds, 3),
                "end_seconds": round(start_seconds + (sampled_frames / frame_rate), 3)
                if frame_rate and sampled_frames > 0
                else None,
                "frame_limit": frames_per_range,
                "sampled_frames": sampled_frames,
                "status": result["status"],
            }
        )

    sampled_frames = _sampled_frames(aggregate)
    measured_range_count = sum(1 for item in ranges if item["status"] == "measured")
    frame_coverage = min(1.0, sampled_frames / max(MIN_IDET_FRAMES, 1))
    range_coverage = measured_range_count / max(1, len(ranges))
    coverage = min(frame_coverage, range_coverage)
    evidence = {
        **aggregate,
        "sampled_frames": sampled_frames,
        "coverage": round(coverage, 4),
        "measured_range_count": measured_range_count,
        "max_frames": max_frames,
        "ranges": ranges,
        "tool": {
            "name": CADENCE_TOOL_NAME,
            "version": CADENCE_TOOL_VERSION,
            "ffmpeg_version": _ffmpeg_version(ffmpeg_name),
        },
    }
    decision = classify_cadence(probe=probe, analysis=evidence, coverage=coverage)
    return _summary_payload(probe=probe, analysis=evidence, decision=decision)


def parse_idet_output(stderr: str) -> dict[str, int]:
    multi_matches = _IDET_MULTI_RE.findall(stderr)
    repeated_matches = _IDET_REPEATED_RE.findall(stderr)
    tff, bff, progressive, undetermined = (
        (int(value) for value in multi_matches[-1])
        if multi_matches
        else (0, 0, 0, 0)
    )
    repeated_neither, repeated_top, repeated_bottom = (
        (int(value) for value in repeated_matches[-1])
        if repeated_matches
        else (0, 0, 0)
    )
    return {
        "tff_frames": tff,
        "bff_frames": bff,
        "progressive_frames": progressive,
        "undetermined_frames": undetermined,
        "repeated_neither": repeated_neither,
        "repeated_top": repeated_top,
        "repeated_bottom": repeated_bottom,
    }


def classify_cadence(
        *,
        probe: Mapping[str, Any] | None,
        analysis: Mapping[str, Any] | None,
        coverage: float | None = None,
) -> dict[str, Any]:
    probe_payload = object_dict(probe)
    analysis_payload = object_dict(analysis)
    sampled_frames = int_value(analysis_payload.get("sampled_frames"))
    if sampled_frames <= 0:
        field_order = _normalized_field_order(probe_payload.get("field_order"))
        if field_order == "progressive" and not bool(probe_payload.get("idet_required")):
            return _decision(
                classification="progressive",
                confidence=0.98,
                coverage=1.0,
                transform="none",
                rationale="ffprobe reports progressive frame order.",
            )
        return _unknown_decision(0.0)

    coverage_value = _bounded(coverage if coverage is not None else sampled_frames / MIN_IDET_FRAMES)
    if sampled_frames < MIN_IDET_FRAMES:
        return _unknown_decision(
            coverage_value,
            f"Only {sampled_frames} frames were measured; at least {MIN_IDET_FRAMES} are required.",
        )

    tff = int_value(analysis_payload.get("tff_frames"))
    bff = int_value(analysis_payload.get("bff_frames"))
    progressive = int_value(analysis_payload.get("progressive_frames"))
    undetermined = int_value(analysis_payload.get("undetermined_frames"))
    total = max(1, tff + bff + progressive + undetermined)
    tff_ratio = tff / total
    bff_ratio = bff / total
    progressive_ratio = progressive / total
    interlaced_ratio = tff_ratio + bff_ratio
    repeated_top = int_value(analysis_payload.get("repeated_top"))
    repeated_bottom = int_value(analysis_payload.get("repeated_bottom"))
    repeated_neither = int_value(analysis_payload.get("repeated_neither"))
    repeated_total = repeated_top + repeated_bottom + repeated_neither
    repeated_ratio = (repeated_top + repeated_bottom) / repeated_total if repeated_total > 0 else 0.0
    repeated_top_ratio = repeated_top / repeated_total if repeated_total > 0 else 0.0
    repeated_bottom_ratio = repeated_bottom / repeated_total if repeated_total > 0 else 0.0
    frame_rate = _rational_value(
        probe_payload.get("average_frame_rate") or probe_payload.get("nominal_frame_rate")
    )
    telecine_rate = frame_rate is not None and (
        29.0 <= frame_rate <= 30.1 or 59.0 <= frame_rate <= 60.1
    )

    if (
            telecine_rate
            and repeated_ratio >= 0.12
            and repeated_top_ratio >= 0.03
            and repeated_bottom_ratio >= 0.03
            and interlaced_ratio >= 0.35
    ):
        confidence = _bounded((0.65 + min(repeated_ratio, 0.30)) * coverage_value)
        return _decision(
            classification="telecine",
            confidence=confidence,
            coverage=coverage_value,
            transform="fieldmatch_decimate" if confidence >= 0.80 else None,
            rationale="Repeated fields and mixed progressive/interlaced measurements match a 3:2 telecine pattern.",
        )
    if progressive_ratio >= 0.95:
        confidence = _bounded(progressive_ratio * coverage_value)
        return _decision(
            classification="progressive",
            confidence=confidence,
            coverage=coverage_value,
            transform="none" if confidence >= 0.80 else None,
            rationale="Measured frames are consistently progressive.",
        )
    if (progressive_ratio >= 0.10 and interlaced_ratio >= 0.10) or (
            tff_ratio >= 0.10 and bff_ratio >= 0.10
    ):
        return _decision(
            classification="mixed",
            confidence=_bounded(max(progressive_ratio, tff_ratio, bff_ratio) * coverage_value),
            coverage=coverage_value,
            transform=None,
            rationale="Measured ranges contain incompatible cadence classes; automatic production is blocked.",
        )
    if tff_ratio >= 0.85:
        confidence = _bounded(tff_ratio * coverage_value)
        return _decision(
            classification="tff",
            confidence=confidence,
            coverage=coverage_value,
            transform="bwdif_tff" if confidence >= 0.80 else None,
            rationale="Measured frames are consistently top-field-first interlaced.",
        )
    if bff_ratio >= 0.85:
        confidence = _bounded(bff_ratio * coverage_value)
        return _decision(
            classification="bff",
            confidence=confidence,
            coverage=coverage_value,
            transform="bwdif_bff" if confidence >= 0.80 else None,
            rationale="Measured frames are consistently bottom-field-first interlaced.",
        )
    return _unknown_decision(coverage_value)


def cadence_manifest_payload(
        summary: Mapping[str, Any] | None,
        *,
        source_id: str,
        source_fingerprint: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    summary_payload = object_dict(summary)
    if not summary_payload:
        return None, _unknown_decision(0.0)

    probe = object_dict(summary_payload.get("probe"))
    analysis = object_dict(summary_payload.get("analysis"))
    sampled_frames = int_value(analysis.get("sampled_frames"))
    tool = object_dict(analysis.get("tool"))
    if (
            int_value(summary_payload.get("schema_version")) != CADENCE_SCHEMA_VERSION
            or str(tool.get("name") or "") != CADENCE_TOOL_NAME
            or str(tool.get("version") or "") != CADENCE_TOOL_VERSION
    ):
        decision = _unknown_decision(
            0.0,
            "Cadence evidence uses an unsupported analyzer version; refresh cadence analysis before encoding.",
        )
    else:
        decision = classify_cadence(
            probe=probe,
            analysis=analysis,
            coverage=_analysis_coverage(analysis),
        )
    tool_version = str(tool.get("version") or CADENCE_TOOL_VERSION)
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
        }
        for item in (object_dict(value) for value in object_list(analysis.get("ranges")))
    ]
    envelope = build_evidence_envelope(
        kind=CADENCE_EVIDENCE_KIND,
        sources=[{"source_id": source_id, "fingerprint": source_fingerprint}],
        policy_hash=stable_policy_hash(cadence_policy_snapshot()),
        tool_name=str(tool.get("name") or CADENCE_TOOL_NAME),
        tool_version=tool_version,
        subject={"stream": "video"},
        measurement={
            "unit": "frames",
            "media_ranges": ranges,
            "sampled_frames": int_value(analysis.get("sampled_frames")),
        },
        result={
            "probe": copy.deepcopy(probe),
            "counts": {
                key: int_value(analysis.get(key))
                for key in _empty_counts()
            },
            "decision": decision,
        },
    )
    decision["evidence_id"] = envelope["evidence_id"]
    return envelope, decision


def cadence_filter(
        decision: Mapping[str, Any] | None,
        evidence: Mapping[str, Any] | None,
        *,
        source_fingerprint: str | None,
) -> str | None:
    payload = object_dict(decision)
    evidence_payload = object_dict(evidence)
    evidence_id = str(payload.get("evidence_id") or "")
    if (
            int_value(payload.get("schema_version")) != CADENCE_SCHEMA_VERSION
            or evidence_id != str(evidence_payload.get("evidence_id") or "")
            or not evidence_envelope_valid(evidence_payload)
    ):
        raise CadenceResolutionError("Cadence transform is not backed by versioned media evidence.")
    inputs = object_dict(evidence_payload.get("inputs"))
    if str(inputs.get("policy_hash") or "") != stable_policy_hash(cadence_policy_snapshot()):
        raise CadenceResolutionError("Cadence evidence is stale for the current transform policy.")
    current_fingerprint = str(source_fingerprint or "").strip()
    recorded_fingerprints = {
        str(source.get("fingerprint") or "").strip()
        for source in (object_dict(item) for item in object_list(inputs.get("sources")))
    }
    if not current_fingerprint or current_fingerprint not in recorded_fingerprints:
        raise CadenceResolutionError("Cadence evidence is stale for the current source fingerprint.")
    recorded_decision = object_dict(object_dict(evidence_payload.get("result")).get("decision"))
    for key in (
            "schema_version",
            "classification",
            "status",
            "confidence",
            "coverage",
            "high_risk",
            "transform",
            "rationale",
    ):
        if recorded_decision.get(key) != payload.get(key):
            raise CadenceResolutionError("Cadence decision does not match its versioned media evidence.")
    classification = str(payload.get("classification") or "unknown")
    transform = str(payload.get("transform") or "")
    status = str(payload.get("status") or "blocked")
    expected = EXPECTED_TRANSFORMS.get(classification)  # type: ignore[arg-type]
    if status != "resolved" or expected is None:
        rationale = str(payload.get("rationale") or "Cadence evidence is missing or inconclusive.")
        raise CadenceResolutionError(f"Cadence is unresolved: {rationale}")
    if transform != expected or transform not in CADENCE_FILTERS:
        raise CadenceResolutionError(
            f"Cadence transform '{transform or 'missing'}' does not match {classification} evidence."
        )
    return CADENCE_FILTERS[transform]  # type: ignore[index]


def _decision(
        *,
        classification: CadenceClass,
        confidence: float,
        coverage: float,
        transform: CadenceTransform | None,
        rationale: str,
) -> dict[str, Any]:
    resolved = transform is not None and confidence >= 0.80
    return {
        "schema_version": CADENCE_SCHEMA_VERSION,
        "classification": classification,
        "status": "resolved" if resolved else "blocked",
        "confidence": round(_bounded(confidence), 4),
        "coverage": round(_bounded(coverage), 4),
        "high_risk": classification != "progressive",
        "transform": transform if resolved else None,
        "rationale": rationale,
    }


def _unknown_decision(coverage: float, rationale: str | None = None) -> dict[str, Any]:
    return _decision(
        classification="unknown",
        confidence=0.0,
        coverage=coverage,
        transform=None,
        rationale=rationale or "Cadence evidence is missing or inconclusive; refresh cadence analysis before encoding.",
    )


def _summary_payload(
        *,
        probe: Mapping[str, Any],
        analysis: Mapping[str, Any],
        decision: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": CADENCE_SCHEMA_VERSION,
        "probe": copy.deepcopy(dict(probe)),
        "analysis": copy.deepcopy(dict(analysis)),
        "decision": copy.deepcopy(dict(decision)),
    }


def _run_idet_range(
        path: Path,
        *,
        ffmpeg_name: str,
        start_seconds: float,
        frame_limit: int,
        timeout_seconds: float,
) -> dict[str, str]:
    command = [
        ffmpeg_name,
        "-hide_banner",
        "-v",
        "info",
        "-ss",
        f"{start_seconds:.3f}",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-frames:v",
        str(frame_limit),
        "-vf",
        "idet",
        "-an",
        "-sn",
        "-dn",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {"status": "timeout", "stderr": str(exc.stderr or "")}
    except OSError as exc:
        return {"status": "failed", "stderr": str(exc)}
    return {
        "status": "measured" if result.returncode == 0 else "failed",
        "stderr": result.stderr,
    }


@lru_cache(maxsize=None)
def _ffmpeg_version(ffmpeg_name: str) -> str:
    try:
        result = subprocess.run(
            [ffmpeg_name, "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    return first_line.strip() or "unknown"


def _sample_starts(duration_seconds: float | None, range_count: int) -> list[float]:
    if duration_seconds is None or not math.isfinite(duration_seconds) or duration_seconds <= 0:
        return [0.0]
    count = max(1, range_count)
    if count == 1 or duration_seconds < 30:
        return [0.0]
    return [duration_seconds * fraction for fraction in (0.10, 0.50, 0.90)[:count]]


def _normalized_field_order(value: object) -> str:
    normalized = str(value or "unknown").strip().lower()
    aliases = {
        "progressive": "progressive",
        "tt": "tff",
        "tb": "tff",
        "tff": "tff",
        "bb": "bff",
        "bt": "bff",
        "bff": "bff",
    }
    return aliases.get(normalized, "unknown")


def _probe_requires_idet(probe: Mapping[str, Any]) -> bool:
    if str(probe.get("field_order") or "unknown") != "progressive":
        return True
    frame_rate = _rational_value(probe.get("average_frame_rate"))
    if frame_rate is None:
        return True
    return 29.0 <= frame_rate <= 30.1 or 59.0 <= frame_rate <= 60.1


def _empty_counts() -> dict[str, int]:
    return {
        "tff_frames": 0,
        "bff_frames": 0,
        "progressive_frames": 0,
        "undetermined_frames": 0,
        "repeated_neither": 0,
        "repeated_top": 0,
        "repeated_bottom": 0,
    }


def _empty_analysis(max_frames: int) -> dict[str, Any]:
    return {
        **_empty_counts(),
        "sampled_frames": 0,
        "coverage": 0.0,
        "measured_range_count": 0,
        "max_frames": max_frames,
        "ranges": [],
        "tool": {
            "name": CADENCE_TOOL_NAME,
            "version": CADENCE_TOOL_VERSION,
            "ffmpeg_version": None,
        },
    }


def _merge_counts(target: dict[str, int], counts: Mapping[str, Any]) -> None:
    for key in _empty_counts():
        target[key] += int_value(counts.get(key))


def _sampled_frames(counts: Mapping[str, Any]) -> int:
    return sum(
        int_value(counts.get(key))
        for key in ("tff_frames", "bff_frames", "progressive_frames", "undetermined_frames")
    )


def _analysis_coverage(analysis: Mapping[str, Any]) -> float:
    sampled_frames = int_value(analysis.get("sampled_frames"))
    ranges = [object_dict(item) for item in object_list(analysis.get("ranges"))]
    if sampled_frames <= 0 or not ranges:
        return 0.0
    measured_range_count = sum(1 for item in ranges if item.get("status") == "measured")
    frame_coverage = min(1.0, sampled_frames / max(MIN_IDET_FRAMES, 1))
    range_coverage = measured_range_count / len(ranges)
    return min(frame_coverage, range_coverage)


def _bounded(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _rational_value(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            parsed = float(numerator) / float(denominator)
        else:
            parsed = float(text)
    except (ValueError, ZeroDivisionError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None
