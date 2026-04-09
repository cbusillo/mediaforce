import json
import re
import subprocess
import hashlib
from pathlib import Path
from typing import Any

from mediaforce.advisor import apply_seed_policy, request_run_verdict, request_seed_policy
from mediaforce.advising.policy import policy_key_paths
from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.type_defs import JSONValue, float_value, int_value, object_dict, object_list
from mediaforce.library.folder_profiles import inspect_prefix
from mediaforce.web.runtime.folder_tuning_helpers import load_json_object, recent_tuning_sessions

MIN_RECOMMENDED_SAVINGS_BYTES = 100 * 1024 * 1024
CALIBRATION_REVIEW_FIELDS = {
    "accepted_at",
    "accepted_draft_hash",
    "accepted_sample_job_id",
    "draft_hash",
}

_NOTE_VMAF_PATTERNS = (
    re.compile(r"\b(?P<target>\d{2}(?:\.\d+)?)\s*vmaf\b", re.IGNORECASE),
    re.compile(
        r"\bvmaf\b(?:\s+(?:target|around|about|roughly|approximately|approx|at|to|of)|\s*=){0,3}\s*(?P<target>\d{2}(?:\.\d+)?)\b",
        re.IGNORECASE,
    ),
)
_NOTE_XPSNR_PATTERNS = (
    re.compile(r"\b(?P<target>\d{2}(?:\.\d+)?)\s*xpsnr\b", re.IGNORECASE),
    re.compile(
        r"\bxpsnr\b(?:\s+(?:target|around|about|roughly|approximately|approx|at|to|of)|\s*=){0,3}\s*(?P<target>\d{2}(?:\.\d+)?)\b",
        re.IGNORECASE,
    ),
)
_NOTE_SIZE_BUDGET_PATTERNS = (
    re.compile(
        r"\b(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>kb|mb|gb|tb)\s*(?:per|/)\s*(?:episode|ep|file)\b",
        re.IGNORECASE,
    ),
        re.compile(
            r"\b(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>kb|mb|gb|tb)\s*(?:each|an?)\s*(?:episode|ep|file)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:want|aim|target|hit|keep|hold|try|budget|cap|under|around|about)\b[^\n.]{0,48}?\b(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>kb|mb|gb|tb)\b",
            re.IGNORECASE,
        ),
)
_SIZE_BUDGET_UNIT_BYTES = {
    "kb": 1024,
    "mb": 1024 * 1024,
    "gb": 1024 * 1024 * 1024,
    "tb": 1024 * 1024 * 1024 * 1024,
}


def merge_policy_fragments(*fragments: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for fragment in fragments:
        for section, values in object_dict(fragment).items():
            normalized_values = object_dict(values)
            if not normalized_values:
                continue
            merged.setdefault(section, {}).update(normalized_values)
    return merged


def parse_audio_bitrate_kbps(value: JSONValue, fallback: float) -> float:
    stripped = str(value or "").strip().lower().removesuffix("kbps").removesuffix("k")
    try:
        parsed = float(stripped)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def sample_audio_target_kbps(sample_item: dict[str, Any] | None) -> float:
    if not isinstance(sample_item, dict):
        return 160.0
    audio_policy = object_dict(object_dict(sample_item.get("resolved_policy")).get("audio"))
    audio_tracks = object_list(sample_item.get("audio_summary"))
    channels = 0
    for track in audio_tracks:
        channels = max(channels, int_value(object_dict(track).get("channels")))
    if channels >= 8:
        return parse_audio_bitrate_kbps(audio_policy.get("surround_7_1_opus_bitrate"), 320.0)
    if channels >= 6:
        return parse_audio_bitrate_kbps(audio_policy.get("surround_5_1_opus_bitrate"), 224.0)
    return parse_audio_bitrate_kbps(audio_policy.get("stereo_opus_bitrate"), 128.0)


def size_budget_feasibility(*, source_percent: float | None, video_bitrate_kbps: float | None) -> tuple[str, bool]:
    if source_percent is None or video_bitrate_kbps is None:
        return "unknown", False
    if source_percent <= 10.0 or video_bitrate_kbps <= 500.0:
        return "unreasonable", True
    if source_percent <= 20.0 or video_bitrate_kbps <= 900.0:
        return "aggressive", False
    return "reasonable", False


def size_budget_request(trimmed: str, sample_item: dict[str, Any] | None) -> dict[str, Any] | None:
    for pattern in _NOTE_SIZE_BUDGET_PATTERNS:
        match = pattern.search(trimmed)
        if not match:
            continue
        unit = str(match.group("unit") or "").strip().lower()
        multiplier = _SIZE_BUDGET_UNIT_BYTES.get(unit)
        if multiplier is None:
            continue
        try:
            amount = float(match.group("amount"))
        except (TypeError, ValueError):
            continue
        budget_bytes = int(round(amount * multiplier))
        source_size_bytes = None
        duration_seconds = None
        if isinstance(sample_item, dict):
            source_size_candidate = float_value(sample_item.get("source_size_bytes"))
            duration_candidate = float_value(sample_item.get("duration_seconds"))
            source_size_bytes = source_size_candidate if source_size_candidate > 0 else None
            duration_seconds = duration_candidate if duration_candidate > 0 else None
        audio_kbps = sample_audio_target_kbps(sample_item)
        estimated_audio_bytes = None
        estimated_video_bitrate_kbps = None
        estimated_source_percent = None
        if duration_seconds and duration_seconds > 0:
            estimated_audio_bytes = int(round((audio_kbps * 1000.0 / 8.0) * duration_seconds))
            remaining_video_bytes = max(budget_bytes - estimated_audio_bytes, 0)
            estimated_video_bitrate_kbps = round((remaining_video_bytes * 8.0 / duration_seconds) / 1000.0, 1)
        if source_size_bytes and source_size_bytes > 0:
            estimated_source_percent = round((budget_bytes / source_size_bytes) * 100.0, 2)
        feasibility, requires_confirmation = size_budget_feasibility(
            source_percent=estimated_source_percent,
            video_bitrate_kbps=estimated_video_bitrate_kbps,
        )
        applied_policy: dict[str, Any] | None = None
        requested_max_encoded_percent = None
        applied_max_encoded_percent = None
        if estimated_source_percent is not None:
            requested_max_encoded_percent = round(estimated_source_percent, 2)
            applied_max_encoded_percent = max(10, min(100, int(round(estimated_source_percent))))
            applied_policy = {"video": {"max_encoded_percent": applied_max_encoded_percent}}
        return {
            "source": "operator_note",
            "honor_mode": "size_budget_experiment",
            "request_type": "size_budget",
            "budget_bytes": budget_bytes,
            "budget_label": f"{amount:g} {unit.upper()} per episode",
            "request_text": trimmed,
            "estimated_source_percent": estimated_source_percent,
            "estimated_audio_bytes": estimated_audio_bytes,
            "estimated_video_bitrate_kbps": estimated_video_bitrate_kbps,
            "feasibility": feasibility,
            "requires_confirmation": requires_confirmation,
            "requested_max_encoded_percent": requested_max_encoded_percent,
            "applied_max_encoded_percent": applied_max_encoded_percent,
            "applied_policy": applied_policy,
        }
    return None


def metric_target_request(trimmed: str) -> dict[str, Any] | None:
    for pattern in _NOTE_VMAF_PATTERNS:
        match = pattern.search(trimmed)
        if not match:
            continue
        try:
            target = float(match.group("target"))
        except (TypeError, ValueError):
            continue
        target = round(max(80.0, min(target, 98.0)), 2)
        min_target = round(min(target, max(75.0, target - 2.0)), 2)
        return {
            "source": "operator_note",
            "honor_mode": "literal_experiment",
            "request_type": "metric_target",
            "metric": "vmaf",
            "target": target,
            "applied_policy": {
                "video": {
                    "target_vmaf": target,
                    "min_target_vmaf": min_target,
                }
            },
            "request_text": trimmed,
        }

    for pattern in _NOTE_XPSNR_PATTERNS:
        match = pattern.search(trimmed)
        if not match:
            continue
        try:
            target = float(match.group("target"))
        except (TypeError, ValueError):
            continue
        target = round(max(30.0, min(target, 41.0)), 2)
        min_target = round(min(target, max(29.0, target - 1.0)), 2)
        return {
            "source": "operator_note",
            "honor_mode": "literal_experiment",
            "request_type": "metric_target",
            "metric": "xpsnr",
            "target": target,
            "applied_policy": {
                "video": {
                    "target_xpsnr": target,
                    "min_target_xpsnr": min_target,
                }
            },
            "request_text": trimmed,
        }
    return None


def operator_requested_experiment(note: str, sample_item: dict[str, Any] | None = None) -> dict[str, Any] | None:
    trimmed = note.strip()
    if not trimmed:
        return None

    requested_size_budget = size_budget_request(trimmed, sample_item)
    requested_metric_target = metric_target_request(trimmed)
    if requested_size_budget and requested_metric_target:
        return {
            "source": "operator_note",
            "honor_mode": "combined_experiment",
            "request_type": "combined_experiment",
            "request_text": trimmed,
            "metric": requested_metric_target.get("metric"),
            "target": requested_metric_target.get("target"),
            "budget_bytes": requested_size_budget.get("budget_bytes"),
            "budget_label": requested_size_budget.get("budget_label"),
            "estimated_source_percent": requested_size_budget.get("estimated_source_percent"),
            "estimated_audio_bytes": requested_size_budget.get("estimated_audio_bytes"),
            "estimated_video_bitrate_kbps": requested_size_budget.get("estimated_video_bitrate_kbps"),
            "feasibility": requested_size_budget.get("feasibility"),
            "requires_confirmation": requested_size_budget.get("requires_confirmation"),
            "requested_max_encoded_percent": requested_size_budget.get("requested_max_encoded_percent"),
            "applied_max_encoded_percent": requested_size_budget.get("applied_max_encoded_percent"),
            "metric_request": requested_metric_target,
            "size_budget_request": requested_size_budget,
            "applied_policy": merge_policy_fragments(
                object_dict(requested_metric_target.get("applied_policy")),
                object_dict(requested_size_budget.get("applied_policy")),
            ),
        }
    if requested_metric_target:
        return requested_metric_target
    if requested_size_budget:
        return requested_size_budget
    return None


def apply_policy_fragment(policy: dict[str, Any], fragment: dict[str, Any] | None) -> dict[str, Any]:
    updated_policy = json.loads(json.dumps(policy))
    for section, values in merge_policy_fragments(fragment).items():
        updated_policy.setdefault(section, {}).update(values)
    return updated_policy


def operator_request_signature(operator_request: dict[str, Any] | None) -> tuple[Any, ...] | None:
    request = object_dict(operator_request)
    if not request:
        return None
    request_type = str(request.get("request_type") or "").strip().lower()
    metric = str(request.get("metric") or "").strip().lower() or None
    target = float_value(request.get("target")) if request.get("target") is not None else None
    budget_bytes = int_value(request.get("budget_bytes")) if request.get("budget_bytes") is not None else None
    rounded_target = round(0.0 if target is None else target, 2)
    if request_type == "combined_experiment":
        return request_type, metric, rounded_target, budget_bytes
    if request_type == "metric_target":
        return request_type, metric, rounded_target
    if request_type == "size_budget":
        return request_type, budget_bytes
    return None


def matching_request_history(
        *,
        note: str,
        sample_item: dict[str, Any] | None,
        recent_sessions_payload: list[dict[str, Any]],
) -> dict[str, Any] | None:
    current_request = operator_requested_experiment(note, sample_item)
    current_signature = operator_request_signature(current_request)
    if current_signature is None:
        return None
    matches: list[dict[str, Any]] = []
    softened_count = 0
    for session in recent_sessions_payload:
        prior_request = operator_requested_experiment(str(session.get("note") or ""), sample_item)
        if operator_request_signature(prior_request) != current_signature:
            continue
        disposition = str(session.get("request_disposition") or "").strip().lower() or None
        if disposition == "softened":
            softened_count += 1
        matches.append(
            {
                "note": session.get("note"),
                "request_disposition": disposition,
                "summary": session.get("summary"),
                "created_at": session.get("created_at"),
            }
        )
    if not matches:
        return None
    return {
        "repeat_count": len(matches) + 1,
        "previous_softened_count": softened_count,
        "matching_sessions": matches,
        "operator_confirmed": True,
    }


def maybe_force_repeated_seed_experiment(
        *,
        base_policy: dict[str, Any],
        seed_response: Any,
        requested_experiment: dict[str, Any] | None,
        repeat_signal: dict[str, Any] | None,
) -> None:
    repeat_payload = object_dict(repeat_signal)
    if int_value(repeat_payload.get("repeat_count")) < 2:
        return
    if str(seed_response.request_disposition or "").strip().lower() not in {"softened", "rejected"}:
        return
    requested_policy = object_dict(object_dict(requested_experiment).get("applied_policy"))
    if not requested_policy:
        return
    _, applied_fragment = apply_seed_policy(base_policy, requested_policy)
    if not applied_fragment:
        return
    previous_softened = int_value(repeat_payload.get("previous_softened_count"))
    seed_response.ok = True
    seed_response.proposed_policy = applied_fragment
    seed_response.request_disposition = "honored_with_risk"
    seed_response.summary = "Kept the repeated requested experiment as a high-risk first sample draft."
    seed_response.diagnosis = (
        "The operator repeated the same explicit experiment after an earlier softening, "
        "so the seed keeps the risky target for measurement instead of overriding it again."
    )
    seed_response.request_response = (
        "You asked for the same aggressive experiment again, so I kept it as an honored high-risk draft "
        "instead of softening it again."
    )
    if previous_softened:
        seed_response.suggested_follow_up = "Measure this risky first sample and decide from the clips whether to pull it back."
    if not seed_response.feasibility_note:
        seed_response.feasibility_note = "This draft is operator-confirmed and intentionally riskier than the usual cold-start seed."


def build_run_verdict_payload(
        *,
        prefix: str,
        calibration_payload: dict[str, Any],
        advice_state: dict[str, Any] | None,
) -> dict[str, Any]:
    sample_result = object_dict(calibration_payload.get("sample_result"))
    sample_item = object_dict(calibration_payload.get("sample_item"))
    policy = object_dict(calibration_payload.get("policy"))
    advice_details = object_dict(advice_state)
    return {
        "folder": prefix,
        "action": calibration_payload.get("action"),
        "mode": calibration_payload.get("mode"),
        "operator_note": advice_details.get("operator_note") or calibration_payload.get("notes") or None,
        "operator_request": advice_details.get("operator_request"),
        "sample_item": {
            "rel_path": sample_item.get("rel_path"),
            "source_size_bytes": sample_item.get("source_size_bytes"),
        },
        "policy": policy,
        "sample_result": {
            "quality_metric": sample_result.get("quality_metric"),
            "quality_target": sample_result.get("quality_target"),
            "quality_score": sample_result.get("quality_score"),
            "chosen_crf": sample_result.get("chosen_crf"),
            "predicted_total_size_bytes": sample_result.get("predicted_total_size_bytes"),
            "predicted_encode_percent": sample_result.get("predicted_encode_percent"),
            "predicted_encode_seconds": sample_result.get("predicted_encode_seconds"),
        },
    }


def record_run_verdict(
        *,
        project_root: Path,
        prefix: str,
        calibration_payload: dict[str, Any],
        advice_state: dict[str, Any] | None,
        merge_advice_state: Any,
        now_iso: Any,
) -> None:
    if str(calibration_payload.get("mode") or "sample") != "sample":
        return
    if not calibration_payload.get("sample_result"):
        return
    verdict = request_run_verdict(
        project_root=project_root,
        payload=build_run_verdict_payload(
            prefix=prefix,
            calibration_payload=calibration_payload,
            advice_state=advice_state,
        ),
    )
    verdict_payload = {
        "summary": verdict.summary,
        "outcome": verdict.outcome,
        "confidence": verdict.confidence,
        "next_step": verdict.next_step,
        "prompt_version": verdict.prompt_version,
        "evidence_checked": verdict.evidence_checked,
        "evaluated_at": now_iso(),
    }
    if verdict.raw:
        verdict_payload["raw"] = verdict.raw
    merge_advice_state(prefix, {"run_verdict": verdict_payload})


def metric_support() -> dict[str, bool]:
    try:
        result = subprocess.run([ffmpeg_binary(), "-hide_banner", "-filters"], check=True, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return {"vmaf": False, "xpsnr": False, "ssim": False, "psnr": False}
    output = result.stdout.lower()
    return {
        "vmaf": "libvmaf" in output,
        "xpsnr": "xpsnr" in output,
        "ssim": "ssim" in output,
        "psnr": " psnr " in output or "\n ts psnr" in output,
    }


def metric_status_copy(metric_support_payload: dict[str, bool]) -> str:
    if metric_support_payload.get("vmaf"):
        return "VMAF is available on this machine, so calibrations can use the preferred perceptual metric."
    if metric_support_payload.get("xpsnr"):
        return "This ffmpeg build does not include libvmaf yet, so the app is falling back to XPSNR for calibration right now."
    return "Neither VMAF nor XPSNR is available from the current ffmpeg tooling, so calibration quality checks will fail until one is installed."


def dominant_summary_key(values: JSONValue) -> str | None:
    if not isinstance(values, dict) or not values:
        return None
    best_key: str | None = None
    best_count = -1
    for key, value in values.items():
        if not isinstance(value, str | int | float):
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > best_count or (count == best_count and best_key is not None and str(key) < best_key):
            best_key = str(key)
            best_count = count
    return best_key


def resolution_tier(width: JSONValue, height: JSONValue) -> str | None:
    if not isinstance(width, str | int | float | None.__class__) or not isinstance(height, str | int | float | None.__class__):
        return None
    width_value = int_value(width)
    height_value = int_value(height)
    largest_dimension = max(width_value, height_value)
    if largest_dimension >= 3800:
        return "2160p"
    if largest_dimension >= 1900:
        return "1080p"
    if largest_dimension >= 1200:
        return "720p"
    if width_value > 0 and height_value > 0:
        return f"{width_value}x{height_value}"
    return None


def seed_collection_shape(prefix: str) -> str:
    parts = Path(prefix).parts
    if len(parts) >= 3 and parts[0].lower() == "tv" and parts[2].lower().startswith("season"):
        return "tv_season"
    if len(parts) >= 2 and parts[0].lower() == "tv":
        return "tv_series"
    if parts and parts[0].lower() in {"movie", "movies", "films"}:
        return "movie_folder"
    return "library_prefix"


def tuning_policy_focus(policy: dict[str, Any]) -> dict[str, Any]:
    focused: dict[str, Any] = {}
    for section in ("video", "audio", "subtitle"):
        raw_section = policy.get(section)
        if not isinstance(raw_section, dict):
            continue
        cleaned: dict[str, Any] = {}
        for key, value in raw_section.items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                cleaned[key] = value
                continue
            if isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value):
                cleaned[key] = list(value)
        if cleaned:
            focused[section] = cleaned
    return focused


def tuning_policy_key_paths(policy: dict[str, Any]) -> list[str]:
    return policy_key_paths(tuning_policy_focus(policy))


def seed_policy_fragment(raw: JSONValue) -> dict[str, Any]:
    source = object_dict(raw)
    return tuning_policy_focus(
        {
            "video": object_dict(source.get("video")),
            "audio": object_dict(source.get("audio")),
            "subtitle": object_dict(source.get("subtitle")),
        }
    )


def seed_class_signals(prefix: str, sample_item: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    suggested_override = object_dict(summary.get("suggested_override"))
    collection_shape = seed_collection_shape(prefix)
    sample_resolution_tier = resolution_tier(sample_item.get("width"), sample_item.get("height"))
    dominant_video_codec = dominant_summary_key(summary.get("video_codecs"))
    video_codecs = object_dict(summary.get("video_codecs"))
    positive_signals: list[str] = []
    caution_flags: list[str] = []

    if collection_shape == "tv_season":
        positive_signals.append("Folder is a single TV season rather than a broad mixed prefix.")
    elif collection_shape == "movie_folder":
        positive_signals.append("Folder is movie-shaped rather than episodic TV.")
    else:
        caution_flags.append("Folder shape is broad enough that the seed should stay close to the base policy.")

    if sample_resolution_tier is not None:
        positive_signals.append(f"Sample item resolves to {sample_resolution_tier}.")
    else:
        caution_flags.append("Sample resolution is unknown, so avoid overfitting the first-pass guess.")

    sample_codec = str(sample_item.get("video_codec") or "").strip().lower()
    if dominant_video_codec and sample_codec and dominant_video_codec == sample_codec:
        positive_signals.append(f"Sample codec matches the folder majority codec ({dominant_video_codec}).")
    if len(video_codecs) > 1:
        caution_flags.append(
            "Folder mixes multiple video codecs, so one sample item may not represent every episode equally."
        )

    item_count = int_value(summary.get("item_count"))
    if item_count and item_count < 6:
        caution_flags.append("Small folder sample size means the seed should remain conservative.")

    for reason in object_list(suggested_override.get("reason"))[:2]:
        if reason:
            positive_signals.append(str(reason))

    caution_flags.append(
        "This first-pass seed is only a bounded starting point; measured calibration should confirm any lean move."
    )
    return {
        "collection_shape": collection_shape,
        "sample_resolution_tier": sample_resolution_tier,
        "dominant_video_codec": dominant_video_codec,
        "positive_signals": positive_signals,
        "caution_flags": caution_flags,
    }


def build_seed_policy_payload(
        *,
        prefix: str,
        user_note: str,
        base_policy: dict[str, Any],
        sample_item: dict[str, Any],
        summary: dict[str, Any],
        metric_support_payload: dict[str, bool],
        recent_sessions_payload: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    suggested_override = object_dict(summary.get("suggested_override"))
    requested_experiment = operator_requested_experiment(user_note, sample_item)
    recent_sessions = list(recent_sessions_payload) if recent_sessions_payload is not None else []
    repeat_signal = matching_request_history(
        note=user_note,
        sample_item=sample_item,
        recent_sessions_payload=recent_sessions,
    )
    return {
        "folder": prefix,
        "goal": "Prefer slightly smaller files when the visual difference is hard to spot, but keep first-pass drafts conservative when the class is uncertain.",
        "seed_principles": [
            "Teach media-class taste instead of optimizing one easy title in isolation.",
            "Prefer small, reversible moves away from the base policy.",
            "Clean, forgiving TV can lean a little smaller than default.",
            "Dark, grainy, fast-motion, or uncertain material should stay near the base policy until measured calibration says otherwise.",
        ],
        "sample_item": {
            "rel_path": sample_item["rel_path"],
            "source_size_bytes": sample_item["source_size_bytes"],
            "video_codec": sample_item["video_codec"],
            "video_bitrate": sample_item.get("video_bitrate"),
            "width": sample_item.get("width"),
            "height": sample_item.get("height"),
            "resolution_tier": resolution_tier(sample_item.get("width"), sample_item.get("height")),
            "duration_seconds": sample_item["duration_seconds"],
            "audio_summary": sample_item["audio_summary"],
            "subtitle_summary": sample_item["subtitle_summary"],
            "recommendation": sample_item.get("recommendation"),
            "recommendation_reason": sample_item.get("recommendation_reason"),
        },
        "summary": {
            "item_count": summary.get("item_count"),
            "total_size_bytes": summary.get("total_size_bytes"),
            "statuses": summary.get("statuses"),
            "video_codecs": summary.get("video_codecs"),
            "audio_codecs": summary.get("audio_codecs"),
            "seasons": summary.get("seasons"),
            "dominant_video_codec": dominant_summary_key(summary.get("video_codecs")),
            "dominant_audio_codec": dominant_summary_key(summary.get("audio_codecs")),
            "suggested_override": {
                "reason": object_list(suggested_override.get("reason")),
                "policy_focus": seed_policy_fragment(suggested_override),
            },
        },
        "class_signals": seed_class_signals(prefix, sample_item, summary),
        "base_policy": tuning_policy_focus(base_policy),
        "operator_note": user_note or None,
        "requested_experiment": requested_experiment,
        "operator_repeat_signal": repeat_signal,
        "metric_support": metric_support_payload,
        "preferred_metric": "vmaf" if metric_support_payload.get("vmaf") else (
            "xpsnr" if metric_support_payload.get("xpsnr") else None
        ),
    }


def calibration_draft_hash(payload: dict[str, Any]) -> str:
    canonical = {
        key: value
        for key, value in payload.items()
        if key not in CALIBRATION_REVIEW_FIELDS
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def review_gate(calibration: dict[str, Any] | None) -> dict[str, Any]:
    if calibration is None:
        return {
            "can_confirm_full": False,
            "message": "Run a sampled calibration first.",
            "status": "missing_sample",
            "next_action_label": "Run a sample",
        }

    if str(calibration.get("mode") or "sample") != "sample":
        return {
            "can_confirm_full": False,
            "message": "Run and save a fresh sampled draft before queueing the folder encode.",
            "status": "needs_fresh_sample",
            "next_action_label": "Run a fresh sample",
        }

    current_hash = str(calibration.get("draft_hash") or calibration_draft_hash(calibration))
    accepted_hash = str(calibration.get("accepted_draft_hash") or "")
    accepted_job_id = str(calibration.get("accepted_sample_job_id") or "")
    current_job_id = str(calibration.get("job_id") or "")
    accepted_at = calibration.get("accepted_at")
    review_media_ready = bool(calibration.get("review_media_ready"))
    can_confirm_full = bool(accepted_at and accepted_hash == current_hash and accepted_job_id == current_job_id)
    if can_confirm_full:
        return {
            "can_confirm_full": True,
            "message": f"Approved sample draft saved at {accepted_at}. Folder encode is unlocked.",
            "status": "accepted",
            "accepted_at": accepted_at,
            "next_action_label": "Queue folder encode",
        }

    if not review_media_ready:
        return {
            "can_confirm_full": False,
            "message": "Review clips are unavailable for this draft. Run a fresh sample before approving it.",
            "status": "missing_review_media",
            "next_action_label": "Run a fresh sample",
        }

    return {
        "can_confirm_full": False,
        "message": "Review the sample clips, then approve this draft to save the folder policy and unlock folder encode.",
        "status": "needs_approval",
        "next_action_label": "Review clips and approve",
    }


def maybe_seed_baseline_policy(
        *,
        config: MediaforceConfig,
        project_root: Path,
        prefix: str,
        action: str,
        user_note: str,
        base_policy: dict[str, Any],
        sample_item: dict[str, Any],
        existing_calibration: dict[str, Any] | None,
        connection: DBClient,
) -> dict[str, Any] | None:
    if action != "baseline" or existing_calibration is not None:
        return None
    summary = inspect_prefix(connection, config, prefix)
    metric_support_payload = metric_support()
    recent_sessions_payload = recent_tuning_sessions(
        connection,
        prefix,
        load_json_object_fn=load_json_object,
        limit=4,
    )
    payload = build_seed_policy_payload(
        prefix=prefix,
        user_note=user_note,
        base_policy=base_policy,
        sample_item=sample_item,
        summary=summary,
        metric_support_payload=metric_support_payload,
        recent_sessions_payload=recent_sessions_payload,
    )
    seed_response = request_seed_policy(project_root=project_root, payload=payload)
    maybe_force_repeated_seed_experiment(
        base_policy=base_policy,
        seed_response=seed_response,
        requested_experiment=object_dict(payload.get("requested_experiment")),
        repeat_signal=object_dict(payload.get("operator_repeat_signal")),
    )
    if not seed_response.ok or not seed_response.proposed_policy:
        return {
            "policy": base_policy,
            "job_fields": {
                "seed_source": "default",
                "seed_summary": seed_response.summary,
                "seed_diagnosis": seed_response.diagnosis,
                "seed_confidence": seed_response.confidence,
                "seed_evidence_checked": seed_response.evidence_checked,
                "seed_suggested_follow_up": seed_response.suggested_follow_up,
                "seed_request_disposition": seed_response.request_disposition,
                "seed_request_response": seed_response.request_response,
                "seed_feasibility_note": seed_response.feasibility_note,
                "seed_prompt_version": seed_response.prompt_version,
                "seed_raw_response": seed_response.raw,
                "seed_proposed_policy": None,
                "seed_applied_policy": None,
                "seed_context_payload": payload,
            },
        }
    seeded_policy, applied_fragment = apply_seed_policy(base_policy, seed_response.proposed_policy)
    seed_source = "ai" if applied_fragment else "default"
    return {
        "policy": seeded_policy,
        "job_fields": {
            "seed_source": seed_source,
            "seed_summary": seed_response.summary,
            "seed_diagnosis": seed_response.diagnosis,
            "seed_confidence": seed_response.confidence,
            "seed_evidence_checked": seed_response.evidence_checked,
            "seed_suggested_follow_up": seed_response.suggested_follow_up,
            "seed_request_disposition": seed_response.request_disposition,
            "seed_request_response": seed_response.request_response,
            "seed_feasibility_note": seed_response.feasibility_note,
            "seed_prompt_version": seed_response.prompt_version,
            "seed_raw_response": seed_response.raw,
            "seed_proposed_policy": seed_response.proposed_policy,
            "seed_applied_policy": applied_fragment or None,
            "seed_context_payload": payload,
        },
    }


def tuning_advice_payload(
        *,
        tuning: Any,
        note: str,
        applied_fragment: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ok": tuning.ok,
        "summary": tuning.summary,
        "raw": tuning.raw,
        "kind": "ai_tune",
        "operator_note": note,
        "prompt_version": tuning.prompt_version,
        "request_disposition": tuning.request_disposition,
        "request_response": tuning.request_response,
        "feasibility_note": tuning.feasibility_note,
        "diagnosis": tuning.diagnosis,
        "confidence": tuning.confidence,
        "evidence_checked": tuning.evidence_checked,
        "suggested_follow_up": tuning.suggested_follow_up,
        "applied_policy": applied_fragment,
        "toolbelt_used": tuning.toolbelt_used,
        "self_check": tuning.self_check,
    }


def seed_advice_payload(note: str, seed_metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if seed_metadata is None and not note:
        return None
    job_fields = object_dict(seed_metadata.get("job_fields")) if seed_metadata else {}
    return {
        "ok": True,
        "summary": job_fields.get("seed_summary") or "Queued an AI-guided first sample baseline.",
        "raw": job_fields.get("seed_raw_response") or "",
        "kind": "seed_baseline",
        "operator_note": note or None,
        "prompt_version": job_fields.get("seed_prompt_version"),
        "request_disposition": job_fields.get("seed_request_disposition"),
        "request_response": job_fields.get("seed_request_response"),
        "feasibility_note": job_fields.get("seed_feasibility_note"),
        "diagnosis": job_fields.get("seed_diagnosis"),
        "confidence": job_fields.get("seed_confidence"),
        "evidence_checked": object_list(job_fields.get("seed_evidence_checked")),
        "suggested_follow_up": job_fields.get("seed_suggested_follow_up"),
        "applied_policy": job_fields.get("seed_applied_policy"),
    }


def job_seed_metadata(job_payload: dict[str, Any]) -> dict[str, Any] | None:
    if not any(
        job_payload.get(key) is not None
        for key in ("seed_source", "seed_prompt_version", "seed_raw_response", "seed_proposed_policy", "seed_applied_policy")
    ):
        return None
    return {
        "source": job_payload.get("seed_source"),
        "summary": job_payload.get("seed_summary"),
        "diagnosis": job_payload.get("seed_diagnosis"),
        "confidence": job_payload.get("seed_confidence"),
        "request_disposition": job_payload.get("seed_request_disposition"),
        "request_response": job_payload.get("seed_request_response"),
        "feasibility_note": job_payload.get("seed_feasibility_note"),
        "prompt_version": job_payload.get("seed_prompt_version"),
        "raw_response": job_payload.get("seed_raw_response"),
        "proposed_policy": job_payload.get("seed_proposed_policy"),
        "applied_policy": job_payload.get("seed_applied_policy"),
    }


def summarize_calibration_result(calibration_payload: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "mode": calibration_payload.get("mode"),
        "action": calibration_payload.get("action"),
        "policy_seed": calibration_payload.get("policy_seed"),
    }
    if calibration_payload.get("mode") == "sample":
        sample_result = object_dict(calibration_payload.get("sample_result"))
        summary["sample_result"] = {
            "chosen_crf": sample_result.get("chosen_crf"),
            "quality_metric": sample_result.get("quality_metric"),
            "quality_score": sample_result.get("quality_score"),
            "predicted_total_size_bytes": sample_result.get("predicted_total_size_bytes"),
            "predicted_encode_percent": sample_result.get("predicted_encode_percent"),
        }
    else:
        encode_result = object_dict(calibration_payload.get("encode_result"))
        summary["encode_result"] = {
            "chosen_crf": encode_result.get("chosen_crf"),
            "quality_metric": encode_result.get("quality_metric"),
            "quality_score": encode_result.get("quality_score"),
            "staging_size_bytes": encode_result.get("staging_size_bytes"),
        }
    return summary
