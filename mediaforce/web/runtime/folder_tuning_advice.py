import json
import hashlib
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from mediaforce.advisor import apply_seed_policy, request_operator_note_parse, request_run_verdict, request_seed_policy
from mediaforce.advising.policy import (
    advisor_protected_policy_paths,
    has_nonpositive_video_budget,
    merge_policy_fragments,
    policy_key_paths,
)
from mediaforce.advising.routing import AdvisorRouting, advisor_routing_from_config
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.type_defs import JSONValue, float_value, int_value, object_dict, object_list
from mediaforce.execution import resolve_stream_budget_ledger
from mediaforce.library.folder_profiles import inspect_prefix
from mediaforce.reviewing.helpers import planned_audio_action, select_primary_audio_track
from mediaforce.tuning.tuning_memory import retrieve_learning_context
from mediaforce.tuning.size_goals import (
    DECIMAL_MEGABYTE_BYTES,
    DEFAULT_FINAL_OUTPUT_TOLERANCE_PERCENT,
    DEFAULT_SAMPLE_PROJECTION_TOLERANCE_PERCENT,
    ResolutionIntent,
    ResolvedSizeGoal,
    SizeGoalIntent,
    bytes_to_megabytes,
    operator_intent_from_request,
)
from mediaforce.web.runtime.folder_tuning_helpers import (
    load_json_object,
    recent_tuning_sessions,
    size_budget_sample_analysis,
)
from mediaforce.web.runtime.tool_capabilities import metric_support

MIN_RECOMMENDED_SAVINGS_BYTES = 100 * 1024 * 1024
CALIBRATION_REVIEW_FIELDS = {
    "accepted_at",
    "accepted_draft_hash",
    "accepted_policy_hash",
    "accepted_sample_job_id",
    "boundary_review_media_ready",
    "current_review_artifact_fingerprint",
    "draft_hash",
}
_SIZE_BUDGET_UNIT_BYTES = {
    "kb": 1_000,
    "mb": DECIMAL_MEGABYTE_BYTES,
    "gb": 1_000_000_000,
    "tb": 1_000_000_000_000,
}
_AUDIO_STEP_DOWN_CANDIDATES_KBPS = {
    "surround_7_1_opus_bitrate": (384.0, 320.0, 256.0, 224.0, 192.0),
    "surround_5_1_opus_bitrate": (320.0, 256.0, 224.0, 192.0, 160.0, 128.0),
    "stereo_opus_bitrate": (192.0, 160.0, 128.0, 112.0, 96.0, 80.0),
}
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_NOTE_PARSE_INTENT_TYPES = {"direct_request", "exploratory_question", "approval_feedback", "other", "unclear"}
_NOTE_PARSE_REQUEST_TYPES = {"none", "metric_target", "size_budget", "scale_target", "combined_experiment"}
_NOTE_PARSE_METRICS = {"vmaf", "xpsnr"}
_EVIDENCE_AUTHORITY_VALUES = {"none", "operator_observed", "approved_visual_result", "rejected_visual_result"}
_CROP_VALUE_RE = re.compile(r"\d+:\d+:\d+:\d+")
_SIZE_BUDGET_RE = re.compile(
    r"(?<![\w.])(?P<sign>[+-]?)\s*(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>kb|mb|gb|tb)\b",
    re.IGNORECASE,
)
_SCALE_HEIGHT_RE = re.compile(r"(?<!\d)(?P<height>240|360|480|540|720|1080|1440|2160|4320)p\b", re.IGNORECASE)
_SOURCE_RESOLUTION_RE = re.compile(
    r"\b(?:source|original|native|current)\s+resolution\b|\b(?:do\s+not|don't|dont|no)\s+(?:downsample|downscale|scale\s+down)\b|\bkeep\s+max_height\s+(?:unset|at\s+0|0)\b|\bmax_height\s+(?:unset|0)\b",
    re.IGNORECASE,
)
_HARD_SIZE_CAP_RE = re.compile(
    r"\b(?:hard\s+(?:cap|ceiling|limit|size)|strict\s+(?:cap|ceiling|limit)|size\s+ceiling|max(?:imum)?\s+size|must\s+(?:hit|be|stay|remain)\s+(?:under|below|at)|do\s+not\s+exceed|max_encoded_percent)\b",
    re.IGNORECASE,
)
_MEASURED_SIZE_FOLLOWUP_RE = re.compile(
    r"\bmeasured\s+follow[- ]?up\b|"
    r"\b(?:revise|retry|rerun)\s+(?:this\s+)?(?:sample|run|draft|test)\b.*\b(?:smaller|closer)\b|"
    r"\b(?:run\s+another|next\s+(?:sample|test)|(?:sample|test)\s+again)\b.*\b(?:smaller|closer)\b|"
    r"\b(?:last|previous|measured|sampled)\s+(?:representative\s+)?(?:sample|run|draft|test)\b.*\b(?:larger|over|miss(?:ed|es)?|target|budget|size)|"
    r"\b(?:miss(?:ed|es)?|over)\s+(?:the\s+)?(?:size\s+)?(?:target|budget)\b.*\b(?:sample|run|draft|test)\b",
    re.IGNORECASE,
)
_METRIC_TARGET_RE = re.compile(r"\b(?P<metric>vmaf|xpsnr)\s*(?:of\s*)?(?:around\s*)?(?P<target>\d+(?:\.\d+)?)\b", re.IGNORECASE)
_REVERSED_METRIC_TARGET_RE = re.compile(
    r"\b(?P<target>\d+(?:\.\d+)?)\s*(?P<metric>vmaf|xpsnr)\b",
    re.IGNORECASE,
)
_METRIC_DIRECTIVE_RE = re.compile(
    r"\b(?:use|using|with|evaluate(?:\s+with)?|measure(?:\s+with)?|run(?:\s+with)?|metric(?:\s+is)?)\s+"
    r"(?P<metric>vmaf|xpsnr)\b",
    re.IGNORECASE,
)
_OPERATOR_OBSERVED_RE = re.compile(
    r"\b(?:look(?:s|ed)?|sound(?:s|ed)?|is|was|are|were)\s+"
    r"(?:good|great|excellent|fine|clean|identical|indistinguishable|acceptable)\b|"
    r"\b(?:cannot|can't|could not|couldn't)\s+(?:see|hear|tell|notice)\b.*\b(?:difference|damage|artifact)\b",
    re.IGNORECASE,
)
_VISUAL_APPROVAL_RE = re.compile(
    r"\b(?:i\s+)?(?:approve|approved|accept|accepted)\b.*\b(?:sample|clip|encode|result|draft|quality)\b|"
    r"\b(?:sample|clip|encode|result|draft|quality)\b.*\b(?:is\s+)?(?:approved|accepted)\b",
    re.IGNORECASE,
)
_VISUAL_REJECTION_RE = re.compile(
    r"\b(?:i\s+)?(?:reject|rejected|decline|declined)\b.*\b(?:sample|clip|encode|result|draft|quality)\b|"
    r"\b(?:do\s+not|don't|dont|did\s+not|didn't|didnt)\s+(?:approve|accept)\b.*"
    r"\b(?:sample|clip|encode|result|draft|quality)\b|"
    r"\b(?:sample|clip|encode|result|draft|quality)\b.*\b"
    r"(?:look(?:s|ed)?|sound(?:s|ed)?|is|was)\s+(?:bad|worse|unacceptable|blocky|banded|damaged)\b",
    re.IGNORECASE,
)
_NEGATIVE_VISUAL_OBSERVATION_RE = re.compile(
    r"\b(?:saw|noticed|showed|shows|had|has|with)\b.{0,48}\b"
    r"(?:artifact(?:s|ing)?|banding|blocking|blockiness|smearing|damage|ringing|mosquito noise|"
    r"color banding|macroblocking|blurring|blur)\b",
    re.IGNORECASE,
)


def _positive_size_budget_match(note: str) -> re.Match[str] | None:
    for match in _SIZE_BUDGET_RE.finditer(note):
        if match.group("sign") != "-":
            return match
    return None


def _normalize_operator_note_parse(parsed: dict[str, Any] | None) -> dict[str, Any] | None:
    parsed_object = object_dict(parsed)
    if not parsed_object:
        return None
    request_type = str(parsed_object.get("request_type") or "").strip().lower()
    if request_type not in _NOTE_PARSE_REQUEST_TYPES:
        return None
    intent_type = str(parsed_object.get("intent_type") or "").strip().lower()
    if intent_type not in _NOTE_PARSE_INTENT_TYPES:
        intent_type = "unclear"
    evidence_authority = "none"
    metric = str(parsed_object.get("metric") or "").strip().lower() or None
    metric_target = None
    size_budget_value = None
    size_budget_unit = None
    scale_height = None
    black_bar_handling = None
    crop = None
    if request_type in {"metric_target", "combined_experiment"}:
        if metric not in _NOTE_PARSE_METRICS:
            if request_type == "metric_target":
                return None
        else:
            metric_target = round(float_value(parsed_object.get("metric_target")), 2)
            if metric_target <= 0:
                metric_target = None
    if request_type in {"size_budget", "combined_experiment"}:
        size_budget_unit = str(parsed_object.get("size_budget_unit") or "").strip().lower() or None
        if size_budget_unit not in _SIZE_BUDGET_UNIT_BYTES:
            if request_type == "size_budget":
                return None
            size_budget_unit = None
        else:
            size_budget_value = round(float_value(parsed_object.get("size_budget_value")), 3)
            if size_budget_value <= 0 and request_type == "size_budget":
                return None
            if size_budget_value <= 0:
                size_budget_value = None
                size_budget_unit = None
    if request_type in {"scale_target", "combined_experiment"}:
        raw_scale_height = int_value(parsed_object.get("scale_height"))
        if raw_scale_height > 0:
            scale_height = max(240, min(raw_scale_height, 4320))
        elif parsed_object.get("scale_height") is not None and raw_scale_height == 0:
            scale_height = 0
        raw_black_bar_handling = str(parsed_object.get("black_bar_handling") or "").strip().lower()
        if raw_black_bar_handling in {"auto", "smart"}:
            black_bar_handling = raw_black_bar_handling
        raw_crop = str(parsed_object.get("crop") or "").strip()
        if _CROP_VALUE_RE.fullmatch(raw_crop):
            crop = raw_crop
        if request_type == "scale_target" and scale_height is None and black_bar_handling is None and crop is None:
            return None
    if request_type == "combined_experiment":
        explicit_parts = sum(
            part is not None for part in (metric, size_budget_value, scale_height, black_bar_handling, crop)
        )
        if explicit_parts < 2:
            return None
    operator_confirmed = bool(parsed_object.get("operator_confirmed"))
    if request_type == "none":
        operator_confirmed = False
    summary = str(parsed_object.get("summary") or "").strip() or "Parsed operator note."
    reasoning_note = str(parsed_object.get("reasoning_note") or "").strip() or "Structured operator note parse."
    hard_size_cap = bool(parsed_object.get("hard_size_cap"))
    measured_size_followup = bool(parsed_object.get("measured_size_followup"))
    return {
        "summary": summary,
        "intent_type": intent_type,
        "request_type": request_type,
        "operator_confirmed": operator_confirmed,
        "evidence_authority": evidence_authority,
        "metric": metric if metric_target is not None else None,
        "metric_target": metric_target,
        "size_budget_value": size_budget_value,
        "size_budget_unit": size_budget_unit,
        "scale_height": scale_height,
        "black_bar_handling": black_bar_handling,
        "crop": crop,
        "hard_size_cap": hard_size_cap,
        "measured_size_followup": measured_size_followup,
        "reasoning_note": reasoning_note,
    }


def _heuristic_operator_note_parse(note: str) -> dict[str, Any] | None:
    trimmed = note.strip()
    if not trimmed:
        return None
    lowered = trimmed.lower()
    visual_feedback_detected = bool(
        _VISUAL_REJECTION_RE.search(trimmed)
        or _VISUAL_APPROVAL_RE.search(trimmed)
        or _NEGATIVE_VISUAL_OBSERVATION_RE.search(trimmed)
        or _OPERATOR_OBSERVED_RE.search(trimmed)
    )

    size_budget_match = _positive_size_budget_match(trimmed)
    metric_match = _METRIC_TARGET_RE.search(trimmed) or _REVERSED_METRIC_TARGET_RE.search(trimmed)
    metric_directive_match = _METRIC_DIRECTIVE_RE.search(trimmed)
    scale_match = _SCALE_HEIGHT_RE.search(trimmed)
    crop_match = _CROP_VALUE_RE.search(trimmed)

    black_bar_handling = None
    if "black-bar" in lowered or "black bar" in lowered:
        if any(token in lowered for token in ("smart", "auto", "automatic")):
            black_bar_handling = "smart"

    source_resolution_requested = bool(_SOURCE_RESOLUTION_RE.search(trimmed))
    scale_height = int(scale_match.group("height")) if scale_match is not None else None
    if source_resolution_requested:
        scale_height = 0
    crop = crop_match.group(0) if crop_match is not None else None
    size_budget_value = float(size_budget_match.group("amount")) if size_budget_match is not None else None
    size_budget_unit = size_budget_match.group("unit").lower() if size_budget_match is not None else None
    hard_size_cap = bool(_HARD_SIZE_CAP_RE.search(trimmed))
    measured_size_followup = bool(_MEASURED_SIZE_FOLLOWUP_RE.search(trimmed))
    metric = (
        metric_match.group("metric").lower()
        if metric_match is not None
        else metric_directive_match.group("metric").lower() if metric_directive_match is not None else None
    )
    metric_target = round(float(metric_match.group("target")), 2) if metric_match is not None else None

    explicit_parts = sum(
        part is not None for part in (metric, size_budget_value, scale_height, black_bar_handling, crop)
    )
    if explicit_parts == 0 and not visual_feedback_detected:
        return None

    if explicit_parts >= 2:
        request_type = "combined_experiment"
    elif metric is not None:
        request_type = "metric_target"
    elif size_budget_value is not None:
        request_type = "size_budget"
    elif explicit_parts == 0:
        request_type = "none"
    else:
        request_type = "scale_target"

    exploratory_markers = (
        "i want to understand",
        "is it realistic",
        "would that help",
        "will that help",
        "do you think",
        "what do you think",
    )
    operator_confirmed = request_type != "none"
    intent_type = "approval_feedback" if request_type == "none" and visual_feedback_detected else "direct_request"
    if any(marker in lowered for marker in exploratory_markers):
        operator_confirmed = False
        intent_type = "exploratory_question"

    summary_parts: list[str] = []
    if scale_height is not None:
        summary_parts.append("keep source resolution" if scale_height == 0 else f"drop to {scale_height}p")
    if size_budget_value is not None and size_budget_unit is not None:
        summary_parts.append(f"target about {size_budget_value:g}{size_budget_unit.upper()}")
    if metric is not None:
        if metric_target is not None:
            summary_parts.append(f"target {metric.upper()} {metric_target:g}")
        else:
            summary_parts.append(f"use {metric.upper()}")
    if black_bar_handling is not None:
        summary_parts.append("use smart black-bar handling")
    if crop is not None:
        summary_parts.append(f"apply crop {crop}")

    return {
        "summary": (
            "Direct request: " + ", ".join(summary_parts)
            if summary_parts
            else "Operator feedback recorded for typed review."
        ),
        "intent_type": intent_type,
        "request_type": request_type,
        "operator_confirmed": operator_confirmed,
        "evidence_authority": "none",
        "metric": metric,
        "metric_target": metric_target,
        "size_budget_value": size_budget_value,
        "size_budget_unit": size_budget_unit,
        "scale_height": scale_height,
        "black_bar_handling": black_bar_handling,
        "crop": crop,
        "hard_size_cap": hard_size_cap,
        "measured_size_followup": measured_size_followup,
        "reasoning_note": "Local heuristic recovered the explicit operator request from the note text.",
    }


def parse_audio_bitrate_kbps(value: JSONValue, fallback: float) -> float:
    stripped = str(value or "").strip().lower().removesuffix("kbps").removesuffix("k")
    try:
        parsed = float(stripped)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _primary_audio_track(sample_item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(sample_item, dict):
        return None
    audio_tracks = [object_dict(track) for track in object_list(sample_item.get("audio_summary")) if isinstance(track, dict)]
    if not audio_tracks:
        return None
    try:
        return select_primary_audio_track(audio_tracks)
    except ValueError:
        return None


def _audio_bitrate_key_for_channels(channels: int) -> tuple[str, float]:
    if channels >= 8:
        return "surround_7_1_opus_bitrate", 320.0
    if channels >= 6:
        return "surround_5_1_opus_bitrate", 224.0
    return "stereo_opus_bitrate", 128.0


def _next_lower_audio_bitrate_kbps(policy_key: str, current_kbps: float) -> float | None:
    for candidate in _AUDIO_STEP_DOWN_CANDIDATES_KBPS.get(policy_key, ()):
        if candidate < current_kbps:
            return candidate
    return None


def audio_tradeoff_hint(
        sample_item: dict[str, Any] | None,
        audio_policy: dict[str, Any] | None,
        *,
        budget_bytes: int | None = None,
) -> dict[str, Any] | None:
    track = _primary_audio_track(sample_item)
    if track is None:
        return None
    policy = object_dict(audio_policy)
    codec_name = str(track.get("codec_name") or "").strip().lower() or None
    if planned_audio_action(track, policy) != "libopus":
        return None
    channels = max(2, int_value(track.get("channels")) or 2)
    policy_key, fallback_kbps = _audio_bitrate_key_for_channels(channels)
    target_kbps = parse_audio_bitrate_kbps(policy.get(policy_key), fallback_kbps)
    next_lower_kbps = _next_lower_audio_bitrate_kbps(policy_key, target_kbps)
    duration_seconds = float_value(object_dict(sample_item).get("duration_seconds"))
    source_size_bytes = int_value(object_dict(sample_item).get("source_size_bytes"))
    step_down_savings_bytes = None
    step_down_source_percent = None
    step_down_budget_percent = None
    leverage = "unknown"
    if next_lower_kbps is not None and duration_seconds > 0:
        delta_kbps = max(target_kbps - next_lower_kbps, 0.0)
        step_down_savings_bytes = int(round((delta_kbps * 1000.0 / 8.0) * duration_seconds))
        if source_size_bytes > 0:
            step_down_source_percent = round((step_down_savings_bytes / source_size_bytes) * 100.0, 2)
        if budget_bytes is not None and budget_bytes > 0:
            step_down_budget_percent = round((step_down_savings_bytes / budget_bytes) * 100.0, 2)
        savings_mib = step_down_savings_bytes / (1024.0 * 1024.0)
        if savings_mib < 12.0 or (step_down_source_percent is not None and step_down_source_percent < 0.5):
            leverage = "low"
        elif savings_mib < 28.0 or (step_down_source_percent is not None and step_down_source_percent < 1.0):
            leverage = "medium"
        else:
            leverage = "high"
    summary = (
        f"A one-step drop from {target_kbps:g}k to {next_lower_kbps:g}k saves about "
        f"{(step_down_savings_bytes or 0) / (1024.0 * 1024.0):.1f} MiB per item."
        if next_lower_kbps is not None and step_down_savings_bytes is not None
        else "No lower reference bitrate step is available for the current planned audio target."
    )
    guidance = (
        "Surround audio is a low-leverage place to chase size; prefer video or methodology moves first unless the operator explicitly wants to spend audio quality."
        if leverage == "low"
        else "Surround audio offers some savings, but it should usually be a later trade after video or methodology moves."
        if leverage == "medium"
        else "Audio changes could materially help this request, so they are a plausible lever if the operator is explicitly size-first."
        if leverage == "high"
        else "Use audio changes only if they clearly support the operator's request."
    )
    review_confidence = "low" if channels > 2 else "medium"
    review_risk_summary = (
        "Multichannel audio changes are harder to validate casually than visible video tradeoffs, especially when the operator is monitoring indirectly."
        if channels > 2
        else "Stereo audio changes are still worth checking, but they are usually easier to validate directly than surround downshifts."
    )
    return {
        "primary_track": {
            "codec_name": codec_name,
            "channels": channels,
            "language": track.get("language"),
        },
        "policy_key": policy_key,
        "target_bitrate_kbps": round(target_kbps, 1),
        "next_lower_bitrate_kbps": round(next_lower_kbps, 1) if next_lower_kbps is not None else None,
        "estimated_step_down_savings_bytes": step_down_savings_bytes,
        "estimated_step_down_savings_mib": round(step_down_savings_bytes / (1024.0 * 1024.0), 1)
        if step_down_savings_bytes is not None else None,
        "estimated_step_down_source_percent": step_down_source_percent,
        "estimated_step_down_budget_percent": step_down_budget_percent,
        "leverage": leverage,
        "recommended_seed_action": "allow" if leverage == "high" else "hold",
        "review_confidence": review_confidence,
        "review_risk_summary": review_risk_summary,
        "summary": summary,
        "guidance": guidance,
    }


def _parsed_operator_note(note: str, *, routing: AdvisorRouting | None = None) -> dict[str, Any] | None:
    trimmed = note.strip()
    if not trimmed:
        return None
    heuristic_parse = _heuristic_operator_note_parse(trimmed)
    if heuristic_parse is not None:
        return heuristic_parse
    request_kwargs: dict[str, Any] = {
        "project_root": _PROJECT_ROOT,
        "payload": {
            "operator_note": trimmed,
            "goal": "Classify the operator note into a concrete tuning request, if one exists.",
        },
    }
    if routing is not None:
        request_kwargs["routing"] = routing
    structured_parse = _normalize_operator_note_parse(request_operator_note_parse(**request_kwargs))
    return structured_parse


def size_budget_request(
        trimmed: str,
        sample_item: dict[str, Any] | None,
        parsed_note: dict[str, Any],
        *,
        current_policy: dict[str, Any] | None = None,
        resolved_size_goal: ResolvedSizeGoal | None = None,
) -> dict[str, Any] | None:
    effective_sample_item = dict(sample_item) if isinstance(sample_item, dict) else None
    effective_policy = object_dict(current_policy)
    if effective_sample_item is not None and effective_policy:
        effective_sample_item["resolved_policy"] = effective_policy
    if resolved_size_goal is None:
        unit = str(parsed_note.get("size_budget_unit") or "").strip().lower()
        multiplier = _SIZE_BUDGET_UNIT_BYTES.get(unit)
        amount = float_value(parsed_note.get("size_budget_value"))
        if multiplier is None or amount <= 0:
            return None
        budget_bytes = int(
            (Decimal(str(amount)) * Decimal(multiplier)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        size_goal = SizeGoalIntent(
            mode="absolute",
            value_bytes=budget_bytes,
            reference_runtime_seconds=None,
            sample_projection_tolerance_percent=DEFAULT_SAMPLE_PROJECTION_TOLERANCE_PERCENT,
            final_output_tolerance_percent=DEFAULT_FINAL_OUTPUT_TOLERANCE_PERCENT,
            source="operator_note",
        )
    else:
        if resolved_size_goal.target_size_bytes is None:
            return None
        budget_bytes = resolved_size_goal.target_size_bytes
        unit = "mb"
        amount = bytes_to_megabytes(budget_bytes) or 0
        size_goal = resolved_size_goal.intent
    hard_size_cap = bool(parsed_note.get("hard_size_cap")) or bool(_HARD_SIZE_CAP_RE.search(trimmed))
    measured_size_followup = bool(parsed_note.get("measured_size_followup")) or bool(
        _MEASURED_SIZE_FOLLOWUP_RE.search(trimmed)
    )
    duration_seconds = None
    if isinstance(effective_sample_item, dict):
        duration_candidate = float_value(effective_sample_item.get("duration_seconds"))
        duration_seconds = duration_candidate if duration_candidate > 0 else None
    estimated_audio_bytes = None
    estimated_video_bitrate_kbps = None
    estimated_source_percent = None
    stream_budget_payload = None
    feasibility = "requires_measurement"
    requires_confirmation = False
    if effective_sample_item is not None:
        goal = resolved_size_goal or size_goal.resolve(duration_seconds)
        stream_budget = resolve_stream_budget_ledger(
            effective_sample_item,
            resolved_size_goal=goal,
            prefer_persisted=False,
        )
        stream_budget_payload = stream_budget.to_payload()
        estimated_audio_bytes = stream_budget.audio_bytes
        estimated_video_bitrate_kbps = (
            round(stream_budget.remaining_video_bitrate_bps / 1000.0, 1)
            if stream_budget.remaining_video_bitrate_bps is not None
            else None
        )
        estimated_source_percent = (
            round((budget_bytes / stream_budget.source_size_bytes) * 100.0, 2)
            if stream_budget.source_size_bytes is not None
            else None
        )
        feasibility = stream_budget.feasibility_status
        requires_confirmation = stream_budget.arithmetic_infeasible
    requested_max_encoded_percent = None
    target_encoded_percent = None
    if hard_size_cap and estimated_source_percent is not None:
        requested_max_encoded_percent = round(estimated_source_percent, 2)
        target_encoded_percent = requested_max_encoded_percent
    elif estimated_source_percent is not None:
        target_encoded_percent = round(estimated_source_percent, 2)
    tradeoff_hint = None
    if isinstance(effective_sample_item, dict):
        tradeoff_hint = audio_tradeoff_hint(
            effective_sample_item,
            object_dict(object_dict(effective_sample_item.get("resolved_policy")).get("audio")),
            budget_bytes=budget_bytes,
        )
    resolved_goal = resolved_size_goal or size_goal.resolve(duration_seconds)
    return {
        "source": "operator_note",
        "operator_note_parse": parsed_note,
        "honor_mode": "size_budget_experiment",
        "request_type": "size_budget",
        "budget_bytes": budget_bytes,
        "budget_label": f"{amount:g} {unit.upper()} per episode",
        "request_text": trimmed,
        "estimated_source_percent": estimated_source_percent,
        "estimated_audio_bytes": estimated_audio_bytes,
        "estimated_video_bitrate_kbps": estimated_video_bitrate_kbps,
        "target_video_bitrate_kbps": estimated_video_bitrate_kbps,
        "target_encoded_percent": target_encoded_percent,
        "target_tolerance_percent": size_goal.sample_projection_tolerance_percent,
        "sample_projection_tolerance_percent": size_goal.sample_projection_tolerance_percent,
        "final_output_tolerance_percent": size_goal.final_output_tolerance_percent,
        "size_goal": resolved_goal.to_payload(),
        "stream_budget_ledger": stream_budget_payload,
        "hard_size_cap": hard_size_cap,
        "measured_size_followup": measured_size_followup,
        "evidence_authority": str(parsed_note.get("evidence_authority") or "none"),
        "feasibility": feasibility,
        "requires_confirmation": requires_confirmation,
        "requested_max_encoded_percent": requested_max_encoded_percent,
        "applied_max_encoded_percent": None,
        "applied_policy": size_goal.policy_fragment(item_runtime_seconds=duration_seconds),
        "audio_tradeoff_hint": tradeoff_hint,
    }


def metric_target_request(trimmed: str, parsed_note: dict[str, Any]) -> dict[str, Any] | None:
    metric = str(parsed_note.get("metric") or "").strip().lower()
    target = float_value(parsed_note.get("metric_target"))
    if metric == "vmaf":
        applied_video: dict[str, JSONValue] = {"quality_metric": "vmaf"}
        if target > 0:
            target = round(max(80.0, min(target, 98.0)), 2)
            min_target = round(min(target, max(75.0, target - 2.0)), 2)
            applied_video.update({"target_vmaf": target, "min_target_vmaf": min_target})
        return {
            "source": "operator_note",
            "operator_note_parse": parsed_note,
            "honor_mode": "literal_experiment",
            "request_type": "metric_target",
            "metric": "vmaf",
            "target": target if target > 0 else None,
            "applied_policy": {"video": applied_video},
            "request_text": trimmed,
        }
    if metric == "xpsnr":
        applied_video: dict[str, JSONValue] = {"quality_metric": "xpsnr"}
        if target > 0:
            target = round(max(30.0, min(target, 41.0)), 2)
            min_target = round(min(target, max(29.0, target - 1.0)), 2)
            applied_video.update({"target_xpsnr": target, "min_target_xpsnr": min_target})
        return {
            "source": "operator_note",
            "operator_note_parse": parsed_note,
            "honor_mode": "literal_experiment",
            "request_type": "metric_target",
            "metric": "xpsnr",
            "target": target if target > 0 else None,
            "applied_policy": {"video": applied_video},
            "request_text": trimmed,
        }
    return None


def scale_target_request(trimmed: str, parsed_note: dict[str, Any]) -> dict[str, Any] | None:
    height = int_value(parsed_note.get("scale_height"))
    black_bar_handling = str(parsed_note.get("black_bar_handling") or "").strip().lower()
    crop = str(parsed_note.get("crop") or "").strip()
    video_policy: dict[str, Any] = {}
    labels: list[str] = []
    if height > 0:
        height = max(240, min(height, 4320))
        video_policy["max_height"] = height
        labels.append(f"{height}p max height")
    elif parsed_note.get("scale_height") is not None and height == 0:
        video_policy["max_height"] = 0
        labels.append("source resolution")
    if black_bar_handling in {"auto", "smart"}:
        video_policy["black_bar_handling"] = black_bar_handling
        video_policy["crop"] = ""
        labels.append("smart black-bar detection" if black_bar_handling == "smart" else "auto black-bar detection")
    if _CROP_VALUE_RE.fullmatch(crop):
        video_policy["crop"] = crop
        labels.append(f"crop {crop}")
    if not video_policy:
        return None
    return {
        "source": "operator_note",
        "operator_note_parse": parsed_note,
        "honor_mode": "literal_experiment",
        "request_type": "scale_target",
        "scale_height": height if height > 0 or parsed_note.get("scale_height") is not None else None,
        "scale_label": " + ".join(labels),
        "black_bar_handling": black_bar_handling if black_bar_handling in {"auto", "smart"} else None,
        "crop": crop if _CROP_VALUE_RE.fullmatch(crop) else None,
        "applied_policy": {"video": video_policy},
        "request_text": trimmed,
    }


def operator_requested_experiment(
        note: str,
        sample_item: dict[str, Any] | None = None,
        *,
        parsed_note: dict[str, Any] | None = None,
        current_policy: dict[str, Any] | None = None,
        advisor_routing: AdvisorRouting | None = None,
) -> dict[str, Any] | None:
    trimmed = note.strip()
    if not trimmed:
        return None
    note_parse = object_dict(parsed_note) or object_dict(_parsed_operator_note(trimmed, routing=advisor_routing))
    request_type = str(note_parse.get("request_type") or "").strip().lower()
    if request_type == "none":
        evidence_authority = str(note_parse.get("evidence_authority") or "none").strip().lower()
        if evidence_authority == "none":
            return None
        return {
            "source": "operator_note",
            "operator_note_parse": note_parse,
            "honor_mode": "evidence_feedback",
            "request_type": "none",
            "request_text": trimmed,
            "operator_confirmed": False,
            "evidence_authority": evidence_authority,
            "applied_policy": None,
        }
    operator_confirmed = bool(note_parse.get("operator_confirmed"))

    requested_size_budget = size_budget_request(
        trimmed,
        sample_item,
        note_parse,
        current_policy=current_policy,
    )
    requested_metric_target = metric_target_request(trimmed, note_parse)
    requested_scale_target = scale_target_request(trimmed, note_parse)
    explicit_requests = [request for request in (requested_metric_target, requested_size_budget, requested_scale_target) if request]
    if len(explicit_requests) > 1:
        return {
            "source": "operator_note",
            "operator_note_parse": note_parse,
            "honor_mode": "combined_experiment",
            "request_type": "combined_experiment",
            "request_text": trimmed,
            "metric": object_dict(requested_metric_target).get("metric"),
            "target": object_dict(requested_metric_target).get("target"),
            "budget_bytes": object_dict(requested_size_budget).get("budget_bytes"),
            "budget_label": object_dict(requested_size_budget).get("budget_label"),
            "scale_height": object_dict(requested_scale_target).get("scale_height"),
            "scale_label": object_dict(requested_scale_target).get("scale_label"),
            "black_bar_handling": object_dict(requested_scale_target).get("black_bar_handling"),
            "crop": object_dict(requested_scale_target).get("crop"),
            "estimated_source_percent": object_dict(requested_size_budget).get("estimated_source_percent"),
            "estimated_audio_bytes": object_dict(requested_size_budget).get("estimated_audio_bytes"),
            "estimated_video_bitrate_kbps": object_dict(requested_size_budget).get("estimated_video_bitrate_kbps"),
            "stream_budget_ledger": object_dict(requested_size_budget).get("stream_budget_ledger"),
            "feasibility": object_dict(requested_size_budget).get("feasibility"),
            "requires_confirmation": object_dict(requested_size_budget).get("requires_confirmation"),
            "hard_size_cap": object_dict(requested_size_budget).get("hard_size_cap"),
            "measured_size_followup": object_dict(requested_size_budget).get("measured_size_followup"),
            "requested_max_encoded_percent": object_dict(requested_size_budget).get("requested_max_encoded_percent"),
            "applied_max_encoded_percent": object_dict(requested_size_budget).get("applied_max_encoded_percent"),
            "operator_confirmed": operator_confirmed,
            "evidence_authority": str(note_parse.get("evidence_authority") or "none"),
            "metric_request": requested_metric_target,
            "size_budget_request": requested_size_budget,
            "scale_request": requested_scale_target,
            "applied_policy": merge_policy_fragments(
                object_dict(object_dict(requested_metric_target).get("applied_policy")),
                object_dict(object_dict(requested_size_budget).get("applied_policy")),
                object_dict(object_dict(requested_scale_target).get("applied_policy")),
            ),
        }
    if requested_metric_target:
        requested_metric_target["operator_confirmed"] = operator_confirmed
        requested_metric_target["evidence_authority"] = str(note_parse.get("evidence_authority") or "none")
        return requested_metric_target
    if requested_size_budget:
        requested_size_budget["operator_confirmed"] = operator_confirmed
        requested_size_budget["evidence_authority"] = str(note_parse.get("evidence_authority") or "none")
        return requested_size_budget
    if requested_scale_target:
        requested_scale_target["operator_confirmed"] = operator_confirmed
        requested_scale_target["evidence_authority"] = str(note_parse.get("evidence_authority") or "none")
        return requested_scale_target
    return None


def operator_request_from_intent(
        intent_payload: dict[str, Any],
        *,
        note: str,
        sample_item: dict[str, Any],
        current_policy: dict[str, Any],
) -> dict[str, Any]:
    intent = operator_intent_from_request(intent_payload)
    duration_seconds = _positive_float_value(sample_item.get("duration_seconds"))
    resolved_size_goal = intent.size_goal.resolve(duration_seconds)
    if intent.compression_intent.requires_confirmation:
        raise ValueError("Choose and confirm a compression goal before starting the test.")
    if resolved_size_goal.requires_confirmation or resolved_size_goal.target_size_bytes is None:
        raise ValueError(resolved_size_goal.rationale)

    target_mb = bytes_to_megabytes(resolved_size_goal.target_size_bytes) or 0
    parsed_note = {
        "summary": resolved_size_goal.rationale,
        "intent_type": "direct_request",
        "request_type": "combined_experiment",
        "operator_confirmed": True,
        "evidence_authority": "none",
        "size_budget_value": target_mb,
        "size_budget_unit": "mb",
        "scale_height": 0 if intent.resolution.mode == "source" else intent.resolution.max_height,
        "black_bar_handling": None,
        "crop": None,
        "hard_size_cap": False,
        "measured_size_followup": False,
        "reasoning_note": "The operator selected a typed size and resolution intent in the guided workflow.",
    }
    planned_policy = merge_policy_fragments(
        current_policy,
        intent.policy_fragment(item_runtime_seconds=duration_seconds),
    )
    size_request = size_budget_request(
        note,
        sample_item,
        parsed_note,
        current_policy=planned_policy,
        resolved_size_goal=resolved_size_goal,
    )
    if size_request is None:
        raise ValueError("The selected size could not be converted into a test budget.")
    size_request.update(
        {
            "budget_bytes": resolved_size_goal.target_size_bytes,
            "budget_label": _resolved_size_goal_label(intent.size_goal, resolved_size_goal.target_size_bytes),
            "size_goal": resolved_size_goal.to_payload(),
            "target_tolerance_percent": intent.size_goal.sample_projection_tolerance_percent,
            "sample_projection_tolerance_percent": intent.size_goal.sample_projection_tolerance_percent,
            "final_output_tolerance_percent": intent.size_goal.final_output_tolerance_percent,
            "applied_policy": intent.size_goal.policy_fragment(item_runtime_seconds=duration_seconds),
        }
    )
    scale_request = {
        "source": "guided_workflow",
        "operator_note_parse": parsed_note,
        "honor_mode": "literal_experiment",
        "request_type": "scale_target",
        "scale_height": 0 if intent.resolution.mode == "source" else intent.resolution.max_height,
        "scale_label": (
            "source resolution"
            if intent.resolution.mode == "source"
            else f"{intent.resolution.max_height}p max height"
        ),
        "black_bar_handling": None,
        "crop": None,
        "applied_policy": intent.resolution.policy_fragment(),
        "request_text": note,
    }
    request = {
        "source": "guided_workflow",
        "operator_note_parse": parsed_note,
        "honor_mode": "combined_experiment",
        "request_type": "combined_experiment",
        "request_text": note,
        "budget_bytes": size_request["budget_bytes"],
        "budget_label": size_request["budget_label"],
        "scale_height": scale_request["scale_height"],
        "scale_label": scale_request["scale_label"],
        "estimated_source_percent": size_request.get("estimated_source_percent"),
        "estimated_audio_bytes": size_request.get("estimated_audio_bytes"),
        "estimated_video_bitrate_kbps": size_request.get("estimated_video_bitrate_kbps"),
        "target_video_bitrate_kbps": size_request.get("target_video_bitrate_kbps"),
        "target_encoded_percent": size_request.get("target_encoded_percent"),
        "target_tolerance_percent": size_request["target_tolerance_percent"],
        "sample_projection_tolerance_percent": size_request["sample_projection_tolerance_percent"],
        "final_output_tolerance_percent": size_request["final_output_tolerance_percent"],
        "size_goal": size_request["size_goal"],
        "stream_budget_ledger": size_request.get("stream_budget_ledger"),
        "feasibility": size_request.get("feasibility"),
        "requires_confirmation": size_request.get("requires_confirmation"),
        "hard_size_cap": False,
        "measured_size_followup": False,
        "operator_confirmed": True,
        "evidence_authority": "none",
        "size_budget_request": size_request,
        "scale_request": scale_request,
        "applied_policy": intent.policy_fragment(item_runtime_seconds=duration_seconds),
    }
    feedback_tags = [
        str(tag).strip()[:64]
        for tag in object_list(intent_payload.get("quality_risk_tags"))[:7]
        if str(tag).strip()
    ]
    if feedback_tags:
        request["quality_risk_tags"] = feedback_tags
    feedback_details = str(intent_payload.get("quality_risk_details") or "").strip()
    if feedback_details:
        request["quality_risk_details"] = feedback_details[:2000]
    evidence_authority = str(intent_payload.get("evidence_authority") or "").strip().lower()
    if evidence_authority in _EVIDENCE_AUTHORITY_VALUES:
        request["evidence_authority"] = evidence_authority
    return request


def _positive_float_value(value: Any) -> float | None:
    parsed = float_value(value)
    return parsed if parsed > 0 else None


def _resolved_size_goal_label(size_goal: SizeGoalIntent, target_size_bytes: int) -> str:
    target_mb = bytes_to_megabytes(target_size_bytes) or 0
    if size_goal.mode == "normalized" and size_goal.reference_runtime_seconds is not None:
        reference_mb = bytes_to_megabytes(size_goal.value_bytes) or 0
        reference_minutes = size_goal.reference_runtime_seconds / 60.0
        return f"about {target_mb:.0f} MB for this episode ({reference_mb:g} MB / {reference_minutes:g} min)"
    return f"{target_mb:g} MB per episode"


def apply_policy_fragment(policy: dict[str, Any], fragment: dict[str, Any] | None) -> dict[str, Any]:
    updated_policy = json.loads(json.dumps(policy))
    for section, values in merge_policy_fragments(fragment).items():
        updated_policy.setdefault(section, {}).update(values)
    return updated_policy


def operator_preserves_source_resolution(operator_request: dict[str, Any] | None) -> bool:
    request = object_dict(operator_request)
    if not request:
        return False
    if int_value(request.get("scale_height")) == 0 and request.get("scale_height") is not None:
        return True
    scale_request = object_dict(request.get("scale_request"))
    if int_value(scale_request.get("scale_height")) == 0 and scale_request.get("scale_height") is not None:
        return True
    video = object_dict(object_dict(request.get("applied_policy")).get("video"))
    if int_value(video.get("max_height")) == 0 and video.get("max_height") is not None:
        return True
    text = str(request.get("request_text") or "")
    return bool(_SOURCE_RESOLUTION_RE.search(text))


def operator_request_signature(operator_request: dict[str, Any] | None) -> tuple[Any, ...] | None:
    request = object_dict(operator_request)
    if not request:
        return None
    request_type = str(request.get("request_type") or "").strip().lower()
    metric = str(request.get("metric") or "").strip().lower() or None
    target = float_value(request.get("target")) if request.get("target") is not None else None
    budget_bytes = int_value(request.get("budget_bytes")) if request.get("budget_bytes") is not None else None
    scale_height = int_value(request.get("scale_height")) if request.get("scale_height") is not None else None
    crop = str(request.get("crop") or "").strip() or None
    raw_black_bar_handling = str(request.get("black_bar_handling") or "").strip().lower()
    black_bar_handling = "smart" if raw_black_bar_handling in {"auto", "smart", "true", "yes", "1"} else (
        raw_black_bar_handling or None
    )
    if crop:
        black_bar_handling = None
    rounded_target = round(0.0 if target is None else target, 2)
    if request_type == "combined_experiment":
        return request_type, metric, rounded_target, budget_bytes, scale_height, black_bar_handling, crop
    if request_type == "metric_target":
        return request_type, metric, rounded_target
    if request_type == "size_budget":
        return request_type, budget_bytes
    if request_type == "scale_target":
        return request_type, scale_height, black_bar_handling, crop
    return None


def matching_request_history(
        *,
        note: str,
        sample_item: dict[str, Any] | None,
        recent_sessions_payload: list[dict[str, Any]],
        current_request: dict[str, Any] | None = None,
        advisor_routing: AdvisorRouting | None = None,
) -> dict[str, Any] | None:
    resolved_current_request = object_dict(current_request) or operator_requested_experiment(
        note,
        sample_item,
        advisor_routing=advisor_routing,
    )
    if not bool(object_dict(resolved_current_request).get("operator_confirmed")):
        return None
    current_signature = operator_request_signature(resolved_current_request)
    if current_signature is None:
        return None
    matches: list[dict[str, Any]] = []
    softened_count = 0
    for session in recent_sessions_payload:
        prior_request = object_dict(session.get("requested_experiment"))
        if not prior_request:
            prior_note_parse = object_dict(session.get("operator_note_parse"))
            prior_request = object_dict(
                operator_requested_experiment(
                    str(session.get("note") or ""),
                    sample_item,
                    parsed_note=prior_note_parse,
                    advisor_routing=advisor_routing,
                )
            )
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
        latest_failed_sample_job: dict[str, Any] | None = None,
) -> bool:
    repeat_payload = object_dict(repeat_signal)
    if int_value(repeat_payload.get("repeat_count")) < 2:
        return False
    request = object_dict(requested_experiment)
    if (
            has_nonpositive_video_budget(request)
            or str(request.get("evidence_authority") or "none").strip().lower() == "rejected_visual_result"
            or bool(object_dict(latest_failed_sample_job))
    ):
        return False
    if str(seed_response.request_disposition or "").strip().lower() not in {"softened", "rejected"}:
        return False
    requested_policy = object_dict(request.get("applied_policy"))
    previous_softened = int_value(repeat_payload.get("previous_softened_count"))
    seed_response.request_disposition = "honored"
    seed_response.summary = "Kept the repeated operator request as the first sample draft."
    seed_response.diagnosis = (
        "The operator repeated the same explicit experiment after an earlier softening, "
        "so the seed preserves the target for measurement instead of overriding it again."
    )
    seed_response.request_response = (
        "You repeated the same request, so I kept it as the first sample. The measured result and your review decide."
    )
    if previous_softened:
        seed_response.suggested_follow_up = "Measure this first sample and decide from the clips."
    if not seed_response.feasibility_note:
        seed_response.feasibility_note = "The repeated operator-confirmed target is queueable for a measured sample."
    if not requested_policy:
        return False
    _, applied_fragment = apply_seed_policy(base_policy, requested_policy)
    if not applied_fragment:
        return False
    seed_response.ok = True
    seed_response.proposed_policy = applied_fragment
    return True


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
    payload = {
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
    target_analysis = size_budget_sample_analysis(
        operator_request=object_dict(advice_details.get("operator_request")) or None,
        calibration_payload=calibration_payload,
    )
    if target_analysis:
        payload["size_target_analysis"] = target_analysis
    return payload


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
        caution_flags.append("Folder shape is broad; choose representative samples before applying one policy broadly.")

    if sample_resolution_tier is not None:
        positive_signals.append(f"Sample item resolves to {sample_resolution_tier}.")
    else:
        caution_flags.append("Sample resolution is unknown; measure it rather than inferring a resolution or bitrate floor.")

    sample_codec = str(sample_item.get("video_codec") or "").strip().lower()
    if dominant_video_codec and sample_codec and dominant_video_codec == sample_codec:
        positive_signals.append(f"Sample codec matches the folder majority codec ({dominant_video_codec}).")
    if len(video_codecs) > 1:
        caution_flags.append(
            "Folder mixes multiple video codecs, so one sample item may not represent every episode equally."
        )

    item_count = int_value(summary.get("item_count"))
    if item_count and item_count < 6:
        caution_flags.append("Small folder size makes representative sample selection especially important.")

    for reason in object_list(suggested_override.get("reason"))[:2]:
        if reason:
            positive_signals.append(str(reason))

    caution_flags.append(
        "This first-pass seed is only a bounded starting point; measured calibration and operator review decide."
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
        requested_experiment: dict[str, Any] | None = None,
        latest_failed_sample_job: dict[str, Any] | None = None,
        learning_context_payload: list[dict[str, Any]] | None = None,
        advisor_routing: AdvisorRouting | None = None,
) -> dict[str, Any]:
    suggested_override = object_dict(summary.get("suggested_override"))
    resolved_requested_experiment = object_dict(requested_experiment) or operator_requested_experiment(
        user_note,
        sample_item,
        advisor_routing=advisor_routing,
    )
    recent_sessions = list(recent_sessions_payload) if recent_sessions_payload is not None else []
    requested_budget_bytes = int_value(object_dict(resolved_requested_experiment).get("budget_bytes")) or None
    repeat_signal = matching_request_history(
        note=user_note,
        sample_item=sample_item,
        recent_sessions_payload=recent_sessions,
        current_request=resolved_requested_experiment,
        advisor_routing=advisor_routing,
    )
    return {
        "folder": prefix,
        "goal": (
            "Honor the resolved size goal and confirmed compression intent, then let measured samples and typed "
            "operator review decide whether a smaller result remains acceptable."
        ),
        "seed_principles": [
            "The operator has already observed strong 1080p AV1 results in this size range across conventional and dark or stylized TV.",
            "Do not infer a minimum acceptable bitrate from source size, resolution, or content-class folklore.",
            "Use representative sample moments to discover actual weaknesses instead of preemptively enlarging the encode.",
            "Preserve source resolution unless the operator explicitly asks to downsample.",
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
        "audio_tradeoff_hint": audio_tradeoff_hint(
            sample_item,
            object_dict(base_policy.get("audio")),
            budget_bytes=requested_budget_bytes,
        ),
        "operator_note": user_note or None,
        "requested_experiment": resolved_requested_experiment,
        "operator_repeat_signal": repeat_signal,
        "retrieved_memory": list(learning_context_payload or []),
        "latest_failed_sample_job": object_dict(latest_failed_sample_job) or None,
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


def calibration_policy_hash(payload: dict[str, Any]) -> str:
    policy_payload = object_dict(payload.get("policy"))
    encoded = json.dumps(policy_payload, sort_keys=True, separators=(",", ":")).encode()
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
    current_policy_hash = calibration_policy_hash(calibration)
    accepted_hash = str(calibration.get("accepted_draft_hash") or "")
    accepted_policy_hash = str(calibration.get("accepted_policy_hash") or "")
    accepted_job_id = str(calibration.get("accepted_sample_job_id") or "")
    current_job_id = str(calibration.get("job_id") or "")
    accepted_at = calibration.get("accepted_at")
    review_media_ready = bool(calibration.get("review_media_ready"))
    same_sample_job = bool(accepted_job_id and accepted_job_id == current_job_id)
    policy_still_matches = bool(accepted_policy_hash and accepted_policy_hash == current_policy_hash)
    # Legacy approvals only stored the full draft hash. Keep those approvals valid
    # for the same sampled draft even if later non-policy metadata changes drift
    # the hash after an encode failure.
    legacy_same_sample_approval = bool(same_sample_job and not accepted_policy_hash)
    can_confirm_full = bool(
        accepted_at and same_sample_job and (policy_still_matches or accepted_hash == current_hash or legacy_same_sample_approval)
    )
    if can_confirm_full:
        return {
            "can_confirm_full": True,
            "message": f"Approved sample draft saved at {accepted_at}. Production starts only when you choose Make the season.",
            "status": "accepted",
            "accepted_at": accepted_at,
            "next_action_label": "Make the season",
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
        "message": "Review the sample clips, then approve this draft to save the folder policy. Production remains separate until you choose Make the season.",
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
        requested_experiment: dict[str, Any] | None = None,
        latest_failed_sample_job: dict[str, Any] | None = None,
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
    learning_context = retrieve_learning_context(
        connection,
        prefix=prefix,
        sample_item=sample_item,
        note=user_note,
    )
    advisor_routing = advisor_routing_from_config(config)
    payload = build_seed_policy_payload(
        prefix=prefix,
        user_note=user_note,
        base_policy=base_policy,
        sample_item=sample_item,
        summary=summary,
        metric_support_payload=metric_support_payload,
        recent_sessions_payload=recent_sessions_payload,
        requested_experiment=requested_experiment,
        latest_failed_sample_job=latest_failed_sample_job,
        learning_context_payload=learning_context,
        advisor_routing=advisor_routing,
    )
    if connection.in_transaction():
        connection.commit()
    seed_response = request_seed_policy(
        project_root=project_root,
        payload=payload,
        routing=advisor_routing,
    )
    forced_operator_policy = maybe_force_repeated_seed_experiment(
        base_policy=base_policy,
        seed_response=seed_response,
        requested_experiment=object_dict(payload.get("requested_experiment")),
        repeat_signal=object_dict(payload.get("operator_repeat_signal")),
        latest_failed_sample_job=latest_failed_sample_job,
    )
    if not seed_response.ok or not seed_response.proposed_policy:
        return {
            "policy": base_policy,
            "job_fields": {
                "seed_ok": seed_response.ok,
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
                "seed_failure_kind": seed_response.failure_kind,
                "seed_failure_code": seed_response.failure_code,
                "seed_failure_attempt_count": seed_response.failure_attempt_count,
                "seed_proposed_policy": None,
                "seed_applied_policy": None,
                "seed_context_payload": payload,
            },
        }
    protected_paths = advisor_protected_policy_paths(seed_response.model_proposed_policy)
    if protected_paths and not forced_operator_policy:
        return {
            "policy": base_policy,
            "job_fields": {
                "seed_ok": seed_response.ok,
                "seed_source": "default",
                "seed_summary": seed_response.summary,
                "seed_diagnosis": (
                    "The advisor attempted to change operator-owned compression fields, so Mediaforce rejected "
                    "the proposal."
                ),
                "seed_confidence": seed_response.confidence,
                "seed_evidence_checked": seed_response.evidence_checked,
                "seed_suggested_follow_up": "Choose the compression goal or size target through the operator controls.",
                "seed_request_disposition": "rejected",
                "seed_request_response": "I kept the operator-owned compression contract unchanged.",
                "seed_feasibility_note": seed_response.feasibility_note,
                "seed_prompt_version": seed_response.prompt_version,
                "seed_raw_response": seed_response.raw,
                "seed_failure_kind": seed_response.failure_kind,
                "seed_failure_code": seed_response.failure_code,
                "seed_failure_attempt_count": seed_response.failure_attempt_count,
                "seed_proposed_policy": seed_response.proposed_policy,
                "seed_applied_policy": None,
                "seed_protected_policy_paths": protected_paths,
                "seed_context_payload": payload,
            },
        }
    seeded_policy, applied_fragment = apply_seed_policy(base_policy, seed_response.proposed_policy)
    seed_source = "ai" if applied_fragment else "default"
    return {
        "policy": seeded_policy,
        "job_fields": {
            "seed_ok": seed_response.ok,
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
            "seed_failure_kind": seed_response.failure_kind,
            "seed_failure_code": seed_response.failure_code,
            "seed_failure_attempt_count": seed_response.failure_attempt_count,
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
        "failure_kind": tuning.failure_kind,
        "failure_code": tuning.failure_code,
        "failure_attempt_count": tuning.failure_attempt_count,
    }


def seed_advice_payload(note: str, seed_metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if seed_metadata is None and not note:
        return None
    job_fields = object_dict(seed_metadata.get("job_fields")) if seed_metadata else {}
    return {
        "ok": (
            bool(job_fields.get("seed_ok"))
            if "seed_ok" in job_fields
            else bool(job_fields.get("seed_proposed_policy"))
        ),
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
        "failure_kind": job_fields.get("seed_failure_kind"),
        "failure_code": job_fields.get("seed_failure_code"),
        "failure_attempt_count": job_fields.get("seed_failure_attempt_count"),
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
        "failure_kind": job_payload.get("seed_failure_kind"),
        "failure_code": job_payload.get("seed_failure_code"),
        "failure_attempt_count": job_payload.get("seed_failure_attempt_count"),
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
