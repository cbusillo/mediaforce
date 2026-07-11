import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.type_defs import float_value, int_value, object_dict
from mediaforce.encoding.helpers import build_svt_params
from mediaforce.encoding.video_filters import build_video_filter
from mediaforce.tuning.size_goals import bytes_to_megabytes, operator_intent_from_policy
from mediaforce.tuning.stream_budget import resolve_stream_budget_ledger


DEFAULT_BAKEOFF_ENGINES = ("ab-av1", "av1an", "xav", "auto-boost")


@dataclass(frozen=True, slots=True)
class BakeoffCandidate:
    key: str
    label: str
    category: str
    maturity: str
    required_tools: tuple[str, ...]
    metric_support: tuple[str, ...]
    command: tuple[str, ...]
    command_status: str
    sources: tuple[str, ...]
    notes: tuple[str, ...]


def build_bakeoff_plan(
        config: MediaforceConfig,
        manifest: dict[str, Any],
        *,
        indexes: list[int],
        engines: list[str] | None = None,
        output_dir: Path | None = None,
        clip_duration_seconds: float = 20.0,
) -> dict[str, Any]:
    requested_engines = _normalize_engines(engines)
    items = [object_dict(item) for item in manifest.get("items", [])]
    selected_items = []
    for index in indexes:
        if index < 0 or index >= len(items):
            raise IndexError(f"Manifest index out of range: {index}")
        selected_items.append(_build_item_plan(config, items[index], index, requested_engines, output_dir, clip_duration_seconds))

    return {
        "schema_version": 1,
        "purpose": "Compare current fast sampling against scene-aware candidate engines before production integration.",
        "decision_model": str(config.video.get("decision_model") or "size_first_review"),
        "default_targets": _default_targets(config),
        "required_result_fields": [
            "engine",
            "source_rel_path",
            "runtime_seconds",
            "output_size_bytes",
            "output_size_percent",
            "selected_crf_or_quantizer",
            "metric_name",
            "metric_score",
            "encode_wall_seconds",
            "review_artifacts",
            "operator_verdict",
        ],
        "items": selected_items,
        "recommendation_rule": (
            "Prefer engines that hit the configured size target while preserving acceptable review clips. "
            "Metric scores are guardrails, not the final decision."
        ),
    }


def write_bakeoff_plan(plan: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(plan, indent=2) + "\n")
    return output_path


def _build_item_plan(
        config: MediaforceConfig,
        item: dict[str, Any],
        index: int,
        engines: list[str],
        output_dir: Path | None,
        clip_duration_seconds: float,
) -> dict[str, Any]:
    policy = object_dict(item.get("resolved_policy"))
    video_policy = object_dict(policy.get("video")) or config.video
    width = int_value(item.get("width")) or None
    height = int_value(item.get("height")) or None
    source_path = str(item.get("source_path") or "")
    rel_path = str(item.get("rel_path") or source_path)
    item_output_dir = output_dir / f"item-{index:02d}" if output_dir is not None else None
    duration_seconds = float_value(item.get("duration_seconds"))
    operator_intent = operator_intent_from_policy(
        video_policy,
        default_video_policy=config.video,
        audio_policy=object_dict(policy.get("audio")),
        subtitle_policy=object_dict(policy.get("subtitle")),
    )
    resolved_size_goal = operator_intent.size_goal.resolve(duration_seconds or None)
    size_target_bytes = resolved_size_goal.target_size_bytes
    size_goal_issue = resolved_size_goal.rationale if size_target_bytes is None else None
    source_size_bytes = int_value(item.get("source_size_bytes")) or int_value(item.get("size_bytes"))
    stream_budget = resolve_stream_budget_ledger(
        item,
        default_video_policy=config.video,
        output_container=config.output_container,
        resolved_size_goal=resolved_size_goal,
    )
    if stream_budget.arithmetic_infeasible:
        size_goal_issue = "The production non-video plan consumes the requested total target."

    return {
        "index": index,
        "rel_path": rel_path,
        "source_path": source_path,
        "source_size_bytes": source_size_bytes,
        "duration_seconds": duration_seconds,
        "resolution": _resolution(width, height),
        "target_size_bytes": size_target_bytes,
        "target_video_size_bytes": stream_budget.remaining_video_bytes,
        "target_video_bitrate_bps": stream_budget.remaining_video_bitrate_bps,
        "size_goal_status": resolved_size_goal.status,
        "size_goal_issue": size_goal_issue,
        "resolved_operator_intent": operator_intent.to_payload(item_runtime_seconds=duration_seconds or None),
        "stream_budget_ledger": stream_budget.to_payload(),
        "target_size_percent": _target_size_percent(size_target_bytes, source_size_bytes),
        "quality_floor": _quality_floor(video_policy),
        "max_height": int_value(video_policy.get("max_height")),
        "cadence_decision": object_dict(item.get("cadence_decision")) if "cadence_decision" in item else None,
        "clip_duration_seconds": clip_duration_seconds,
        "review_artifact_dir": str(item_output_dir) if item_output_dir is not None else None,
        "engines": [
            _engine_candidate(
                engine,
                item,
                video_policy,
                item_output_dir,
                stream_budget.remaining_video_bytes,
                stream_budget.source_cap_video_percent,
                size_goal_issue,
            )
            for engine in engines
        ],
    }


def _engine_candidate(
        engine: str,
        item: dict[str, Any],
        video_policy: dict[str, Any],
        output_dir: Path | None,
        target_video_size_bytes: int | None,
        source_cap_video_percent: float | None,
        size_goal_issue: str | None,
) -> dict[str, Any]:
    source_path = str(item.get("source_path") or "SOURCE_PATH")
    source_codec = str(item.get("video_codec") or "")
    width = int_value(item.get("width")) or None
    height = int_value(item.get("height")) or None
    video_filter = build_video_filter(
        video_policy,
        width=width,
        height=height,
        detected_crop=None,
        cadence_decision=(
            object_dict(item.get("cadence_decision"))
            if "cadence_decision" in item
            else None
        ),
        cadence_evidence=(
            object_dict(item.get("cadence_evidence"))
            if "cadence_evidence" in item
            else None
        ),
        cadence_source_fingerprint=str(item.get("source_fingerprint") or "") or None,
    )
    target_video_size_mb = bytes_to_megabytes(target_video_size_bytes) or 0
    max_encoded_percent = (
        source_cap_video_percent
        if source_cap_video_percent is not None
        else float_value(video_policy.get("max_encoded_percent"))
    )
    target_vmaf = float_value(video_policy.get("target_vmaf"))
    min_target_vmaf = float_value(video_policy.get("min_target_vmaf"))
    preset = int_value(video_policy.get("preset"))
    pixel_format = str(video_policy.get("pixel_format") or "yuv420p10le")
    svt_params = build_svt_params(video_policy)

    if engine == "auto-boost" and (target_video_size_bytes is None or target_video_size_bytes <= 0):
        return _candidate_to_dict(
            BakeoffCandidate(
                key="auto-boost",
                label="Auto-Boost Essential",
                category="scene-aware-candidate",
                maturity="research-candidate",
                required_tools=("Auto-Boost-Essential script", "SVT-AV1-Essential or compatible SVT-AV1"),
                metric_support=("script-defined",),
                command=(),
                command_status="blocked-unresolved-size-goal",
                sources=("https://github.com/nekotrix/auto-boost-algorithm/tree/main/Auto-Boost-Essential",),
                notes=(size_goal_issue or "Resolve the size goal before running this target-size engine.",),
            )
        )

    if engine == "ab-av1":
        command = [
            "ab-av1",
            "crf-search",
            "-i",
            source_path,
            "--encoder",
            "libsvtav1",
            "--preset",
            str(preset),
            "--pix-format",
            pixel_format,
            "--min-vmaf",
            _number(target_vmaf),
            "--max-encoded-percent",
            _number(max_encoded_percent),
        ]
        command.extend(_sample_args(video_policy))
        command.extend(_filter_args(video_filter, "--vfilter"))
        for param in svt_params:
            command.extend(["--svt", param])
        candidate = BakeoffCandidate(
            key="ab-av1",
            label="ab-av1 fast sample",
            category="current",
            maturity="production-current",
            required_tools=("ab-av1", "ffmpeg with libvmaf or xpsnr", "SVT-AV1 encoder"),
            metric_support=("vmaf", "xpsnr"),
            command=tuple(command),
            command_status="production-current",
            sources=("https://github.com/alexheretic/ab-av1",),
            notes=(
                "Current Mediaforce search path; fast and already host-orchestrated.",
                "Samples across the file but does not split by scene before scoring.",
            ),
        )
    elif engine == "av1an":
        command = [
            "av1an",
            "-i",
            source_path,
            "-o",
            str((output_dir or Path("bakeoff")) / "av1an.mkv"),
            "--encoder",
            "svt-av1",
            "--target-quality",
            _number(min_target_vmaf),
            "--target-metric",
            "ssimulacra2",
            "--pix-format",
            pixel_format,
            "--video-params",
            _svt_video_params(video_policy),
        ]
        command.extend(_filter_args(video_filter, "--vfilter"))
        candidate = BakeoffCandidate(
            key="av1an",
            label="Av1an target quality",
            category="scene-aware-candidate",
            maturity="candidate",
            required_tools=("av1an", "ffmpeg", "mkvmerge", "SVT-AV1 encoder", "metric plugin/runtime"),
            metric_support=("vmaf", "ssimulacra2"),
            command=tuple(command),
            command_status="template-needs-host-validation",
            sources=(
                "https://rust-av.github.io/Av1an/Features/TargetQuality",
                "https://rust-av.github.io/Av1an/Cli/target_quality.html",
            ),
            notes=(
                "Primary candidate for scene/chunk-aware target-quality workflow.",
                "Command uses SSIMULACRA2 because Av1an target-quality supports it directly; verify installed Av1an and metric plugin names on host before production use.",
            ),
        )
    elif engine == "xav":
        command = [
            "xav",
            "--input",
            source_path,
            "--output",
            str((output_dir or Path("bakeoff")) / "xav.mkv"),
            "--encoder",
            "svt-av1",
            "--metric",
            "ssimulacra2",
            "--target",
            _number(min_target_vmaf),
        ]
        candidate = BakeoffCandidate(
            key="xav",
            label="Xav target quality",
            category="scene-aware-candidate",
            maturity="experimental-candidate",
            required_tools=("xav", "ffmpeg", "SVT-AV1 encoder", "GPU metric support recommended"),
            metric_support=("ssimulacra2", "vmaf"),
            command=tuple(command),
            command_status="research-template-needs-cli-validation",
            sources=("https://github.com/emrakyz/xav",),
            notes=(
                "Performance-oriented Av1an-style candidate.",
                "Upstream README documents chunked target-quality goals but points to a work-in-progress PDF for detailed CLI usage.",
            ),
        )
    elif engine == "auto-boost":
        command = [
            "auto-boost-essential",
            source_path,
            str((output_dir or Path("bakeoff")) / "auto-boost.mkv"),
            "--target-size-mb",
            str(target_video_size_mb),
        ]
        candidate = BakeoffCandidate(
            key="auto-boost",
            label="Auto-Boost Essential",
            category="scene-aware-candidate",
            maturity="research-candidate",
            required_tools=("Auto-Boost-Essential script", "SVT-AV1-Essential or compatible SVT-AV1"),
            metric_support=("script-defined",),
            command=tuple(command),
            command_status="research-template-needs-script-validation",
            sources=("https://github.com/nekotrix/auto-boost-algorithm/tree/main/Auto-Boost-Essential",),
            notes=(
                "Candidate from ecosystem feedback for faster consistent quality allocation.",
                f"Source codec detected as {source_codec or 'unknown'}; validate script compatibility before full encode.",
            ),
        )
    else:
        raise ValueError(f"Unsupported bakeoff engine: {engine}")
    return _candidate_to_dict(candidate)


def _candidate_to_dict(candidate: BakeoffCandidate) -> dict[str, Any]:
    return {
        "key": candidate.key,
        "label": candidate.label,
        "category": candidate.category,
        "maturity": candidate.maturity,
        "required_tools": list(candidate.required_tools),
        "metric_support": list(candidate.metric_support),
        "command": list(candidate.command),
        "command_status": candidate.command_status,
        "sources": list(candidate.sources),
        "notes": list(candidate.notes),
    }


def _normalize_engines(engines: list[str] | None) -> list[str]:
    if not engines:
        return list(DEFAULT_BAKEOFF_ENGINES)
    normalized = []
    aliases = {"auto_boost": "auto-boost", "autoboost": "auto-boost", "abav1": "ab-av1"}
    for engine in engines:
        key = aliases.get(engine.strip().lower(), engine.strip().lower())
        if key not in DEFAULT_BAKEOFF_ENGINES:
            raise ValueError(f"Unsupported bakeoff engine: {engine}")
        if key not in normalized:
            normalized.append(key)
    return normalized


def _default_targets(config: MediaforceConfig) -> dict[str, Any]:
    video = config.video
    operator_intent = operator_intent_from_policy(
        video,
        default_video_policy=video,
        audio_policy=config.audio,
        subtitle_policy=config.subtitle,
    )
    target_size_mb = bytes_to_megabytes(operator_intent.size_goal.value_bytes)
    target_runtime_minutes = (
        operator_intent.size_goal.reference_runtime_seconds / 60.0
        if operator_intent.size_goal.reference_runtime_seconds is not None
        else None
    )
    return {
        "target_size_mb": target_size_mb,
        "target_runtime_minutes": target_runtime_minutes,
        "target_size_bytes": operator_intent.size_goal.value_bytes,
        "size_goal_mode": operator_intent.size_goal.mode,
        "sample_projection_tolerance_percent": operator_intent.size_goal.sample_projection_tolerance_percent,
        "final_output_tolerance_percent": operator_intent.size_goal.final_output_tolerance_percent,
        "max_height": int_value(video.get("max_height")),
        "quality_metric": str(video.get("quality_metric") or "auto"),
        "target_vmaf": float_value(video.get("target_vmaf")),
        "min_target_vmaf": float_value(video.get("min_target_vmaf")),
        "target_xpsnr": float_value(video.get("target_xpsnr")),
        "min_target_xpsnr": float_value(video.get("min_target_xpsnr")),
        "max_encoded_percent": int_value(video.get("max_encoded_percent")),
        "decision_model": str(video.get("decision_model") or "size_first_review"),
        "quality_engine": str(video.get("quality_engine") or "ab_av1_fast_sample"),
    }


def _target_size_percent(target_size_bytes: int | None, source_size_bytes: int) -> float | None:
    if target_size_bytes is None or source_size_bytes <= 0:
        return None
    return round((target_size_bytes / source_size_bytes) * 100.0, 3)


def _quality_floor(video_policy: dict[str, Any]) -> dict[str, Any]:
    metric = str(video_policy.get("quality_metric") or "auto").lower()
    if metric == "xpsnr":
        return {"metric": "xpsnr", "target": float_value(video_policy.get("target_xpsnr")),
                "minimum": float_value(video_policy.get("min_target_xpsnr"))}
    return {"metric": "vmaf", "target": float_value(video_policy.get("target_vmaf")),
            "minimum": float_value(video_policy.get("min_target_vmaf"))}


def _resolution(width: int | None, height: int | None) -> str | None:
    if width is None or height is None:
        return None
    return f"{width}x{height}"


def _sample_args(video_policy: dict[str, Any]) -> list[str]:
    return [
        "--sample-every",
        str(video_policy.get("sample_every") or "8m"),
        "--sample-duration",
        str(video_policy.get("sample_duration") or "20s"),
    ]


def _filter_args(video_filter: str | None, flag: str) -> list[str]:
    if not video_filter:
        return []
    return [flag, video_filter]


def _svt_video_params(video_policy: dict[str, Any]) -> str:
    params = [
        f"--preset {int_value(video_policy.get('preset'))}",
        f"--crf {int_value(video_policy.get('min_crf'))}",
        f"--film-grain {int_value(video_policy.get('default_grain'))}",
        f"--film-grain-denoise {int_value(video_policy.get('grain_denoise'))}",
    ]
    return " ".join(params)


def _number(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")
