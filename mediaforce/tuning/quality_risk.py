from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any, Mapping

from mediaforce.advisor import apply_seed_policy
from mediaforce.core.evidence import stable_json_hash, stable_policy_hash, stable_source_id
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.tuning.target_size_search import (
    TARGET_SIZE_SEARCH_SCHEMA_VERSION,
    target_size_transform_plan_valid,
)

QUALITY_RISK_CONTRACT_SCHEMA = "mediaforce.quality_risk_contract"
QUALITY_RISK_CONTRACT_VERSION = 1
MAX_QUALITY_RISK_FEEDBACK_TAGS = 7
MAX_QUALITY_RISK_FEEDBACK_DETAILS = 2000

QUALITY_RISK_VERDICTS = (
    "safe_to_sample",
    "needs_operator_review",
    "request_comparison",
    "blocked",
)

QUALITY_RISK_LEVELS = ("low", "medium", "high", "unknown")

QUALITY_RISK_TAGS = (
    "softness_detail_loss",
    "motion_breakup",
    "banding_dark_scene_damage",
    "grain_noise_treatment",
    "cadence_interlace_artifacts",
    "audio_quality_layout",
    "other",
)

QUALITY_RISK_TAG_LABELS = {
    "softness_detail_loss": "Softness / detail loss",
    "motion_breakup": "Motion breakup",
    "banding_dark_scene_damage": "Banding / dark-scene damage",
    "grain_noise_treatment": "Grain / noise treatment",
    "cadence_interlace_artifacts": "Cadence / interlace artifacts",
    "audio_quality_layout": "Audio quality / layout",
    "other": "Other",
}

QUALITY_RISK_TAG_ALIASES = {
    "softness": "softness_detail_loss",
    "detail_loss": "softness_detail_loss",
    "softness_detail_loss": "softness_detail_loss",
    "motion_breakup": "motion_breakup",
    "motion": "motion_breakup",
    "high_motion": "motion_breakup",
    "banding": "banding_dark_scene_damage",
    "dark_scene_damage": "banding_dark_scene_damage",
    "banding_dark_scene_damage": "banding_dark_scene_damage",
    "dark_luma": "banding_dark_scene_damage",
    "dark_gradient_banding_risk": "banding_dark_scene_damage",
    "grain": "grain_noise_treatment",
    "noise": "grain_noise_treatment",
    "grain_noise_treatment": "grain_noise_treatment",
    "likely_film_grain": "grain_noise_treatment",
    "likely_analog_noise": "grain_noise_treatment",
    "compression_noise_advisory": "grain_noise_treatment",
    "uncertain_noise_mix": "grain_noise_treatment",
    "texture_noise_advisory": "grain_noise_treatment",
    "cadence": "cadence_interlace_artifacts",
    "interlace": "cadence_interlace_artifacts",
    "cadence_interlace_artifacts": "cadence_interlace_artifacts",
    "duplicate_cadence": "cadence_interlace_artifacts",
    "audio": "audio_quality_layout",
    "audio_layout": "audio_quality_layout",
    "audio_quality_layout": "audio_quality_layout",
    "audio_complexity": "audio_quality_layout",
    "audio_loudness_range": "audio_quality_layout",
    "high_texture": "softness_detail_loss",
    "animation_cues": "softness_detail_loss",
    "other": "other",
}

RISK_INTERPRETATION_KEY = "quality_risk_interpretation"
ALLOW_LISTED_POLICY_SECTIONS = frozenset({"video", "audio", "subtitle"})
ALLOW_LISTED_VIDEO_TRANSFORM_KEYS = frozenset(
    {
        "encoder",
        "pixel_format",
        "crf_search",
        "max_height",
        "resolution_intent_mode",
        "resolution_intent_source",
        "downsample_algorithm",
        "black_bar_handling",
        "black_bar_detect_samples",
        "black_bar_detect_seconds",
        "black_bar_detect_limit",
        "black_bar_detect_round",
        "black_bar_detect_start_seconds",
        "crop",
        "default_grain",
        "grain_denoise",
        "min_crf",
        "max_crf",
        "max_encoded_percent",
        "quality_metric",
        "target_vmaf",
        "min_target_vmaf",
        "target_xpsnr",
        "min_target_xpsnr",
        "target_relax_step_vmaf",
        "target_relax_step_xpsnr",
        "sample_every",
        "sample_duration",
        "preset",
        "thorough",
        "target_size_mb",
        "target_size_bytes",
        "target_runtime_minutes",
        "size_goal_schema_version",
        "size_goal_mode",
        "size_goal_source",
        "sample_projection_tolerance_percent",
        "final_output_tolerance_percent",
    }
)
ALLOW_LISTED_AUDIO_KEYS = frozenset(
    {
        "keep_languages",
        "copy_codecs",
        "convert_to_opus_codecs",
        "stereo_opus_bitrate",
        "surround_5_1_opus_bitrate",
        "surround_7_1_opus_bitrate",
    }
)
ALLOW_LISTED_SUBTITLE_KEYS = frozenset({"keep_languages", "prefer_text", "keep_forced", "default_mode"})

_QUALITY_RISK_TEXT_PATTERNS = {
    "softness_detail_loss": re.compile(r"\b(?:soft|softness|blurr(?:y|ed|ing)?|detail\s+loss|faces?\s+look)\b", re.I),
    "motion_breakup": re.compile(r"\b(?:motion|breaks?\s+up|blocking|blockiness|macroblocking|smear(?:ing)?)\b", re.I),
    "banding_dark_scene_damage": re.compile(r"\b(?:banding|dark\s+scene|dark-scene|gradient|posterization)\b", re.I),
    "grain_noise_treatment": re.compile(r"\b(?:grain|noise|denoise|film\s+texture)\b", re.I),
    "cadence_interlace_artifacts": re.compile(r"\b(?:cadence|interlace|telecine|judder|combing)\b", re.I),
    "audio_quality_layout": re.compile(
        r"\b(?:audio\s+quality|surround|stereo|dialog(?:ue)?|channel\s+layout|"
        r"sound(?:s|ed)?\s+(?:bad|thin|wrong|flat|distorted|missing))\b",
        re.I,
    ),
}


@dataclass(frozen=True, slots=True)
class QualityRiskDecisionRecord:
    kind: str
    created_at: str | None
    verdict: str
    tags: tuple[str, ...]
    details: str
    evidence_ids: tuple[str, ...]
    moment_indexes: tuple[int, ...]
    sample_job_id: str | None
    policy_hash: str | None
    source_id: str | None
    prefix: str | None
    authoritative: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "created_at": self.created_at,
            "verdict": self.verdict,
            "tags": list(self.tags),
            "details": self.details,
            "evidence_id": self.evidence_ids[0] if self.evidence_ids else None,
            "evidence_ids": list(self.evidence_ids),
            "moment_indexes": list(self.moment_indexes),
            "sample_job_id": self.sample_job_id,
            "policy_hash": self.policy_hash,
            "source_id": self.source_id,
            "prefix": self.prefix,
            "authoritative": self.authoritative,
        }


def quality_risk_tag_label(tag: str) -> str:
    normalized = normalize_quality_risk_tag(tag)
    return QUALITY_RISK_TAG_LABELS.get(normalized, QUALITY_RISK_TAG_LABELS["other"])


def normalize_quality_risk_tag(tag: str | None) -> str:
    candidate = str(tag or "").strip().lower().replace("-", "_").replace("/", "_").replace(" ", "_")
    return QUALITY_RISK_TAG_ALIASES.get(candidate, "other")


def normalize_quality_risk_tags(tags: list[Any] | tuple[Any, ...] | None) -> list[str]:
    normalized = [normalize_quality_risk_tag(str(tag)) for tag in _sequence_values(tags)]
    unique: list[str] = []
    for tag in normalized:
        if tag not in unique:
            unique.append(tag)
    return unique or ["other"]


def quality_risk_tags_from_text(value: str | None) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return [tag for tag, pattern in _QUALITY_RISK_TEXT_PATTERNS.items() if pattern.search(text)]


def with_quality_risk_intent(
        operator_request: Mapping[str, Any] | None,
        *,
        note: str,
) -> dict[str, Any] | None:
    request = dict(object_dict(operator_request))
    explicit_tags = object_list(request.get("quality_risk_tags"))
    inferred_tags = quality_risk_tags_from_text(note)
    if not explicit_tags and not inferred_tags:
        return request or None
    tags = normalize_quality_risk_tags(explicit_tags or inferred_tags)[:MAX_QUALITY_RISK_FEEDBACK_TAGS]
    details = str(request.get("quality_risk_details") or note).strip()[:MAX_QUALITY_RISK_FEEDBACK_DETAILS]
    request.setdefault("source", "operator_note")
    request.setdefault("request_type", "quality_risk_feedback")
    request.setdefault("request_text", note.strip())
    request.setdefault("operator_confirmed", False)
    request.setdefault("evidence_authority", "none")
    request["quality_risk_tags"] = tags
    request["quality_risk_details"] = details
    return request


def quality_risk_interpretation_payload(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    payload = object_dict(value)
    if not payload:
        return None
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in QUALITY_RISK_VERDICTS:
        verdict = "request_comparison"
    confidence = str(payload.get("confidence") or "").strip().lower()
    if confidence not in QUALITY_RISK_LEVELS:
        confidence = "unknown"
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        return None
    risks: list[dict[str, Any]] = []
    for raw_risk in object_list(payload.get("risks")):
        risk = object_dict(raw_risk)
        tag = normalize_quality_risk_tag(risk.get("tag"))
        level = str(risk.get("level") or "").strip().lower()
        if level not in QUALITY_RISK_LEVELS:
            level = "unknown"
        rationale = str(risk.get("rationale") or "").strip()
        if not rationale:
            continue
        risks.append(
            {
                "tag": tag,
                "label": quality_risk_tag_label(tag),
                "level": level,
                "rationale": rationale,
                "evidence_ids": _normalized_evidence_ids(risk.get("evidence_ids")),
                "moment_indexes": _normalized_moment_indexes(risk.get("moment_indexes")),
            }
        )
    suggested_actions = []
    for raw_action in object_list(payload.get("suggested_actions")):
        action = str(raw_action).strip()
        if action and action not in suggested_actions:
            suggested_actions.append(action)
    return {
        "summary": summary,
        "verdict": verdict,
        "confidence": confidence,
        "risks": risks,
        "suggested_actions": suggested_actions,
    }


def compile_allow_listed_transform_fragment(
        base_policy: Mapping[str, Any],
        transform_fragment: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    fragment = object_dict(transform_fragment)
    if not fragment:
        policy_copy = json.loads(json.dumps(object_dict(base_policy)))
        return policy_copy, {}, []
    base_payload = json.loads(json.dumps(object_dict(base_policy)))
    normalized_fragment: dict[str, Any] = {}
    rejected_keys = [str(key) for key in fragment if key not in ALLOW_LISTED_POLICY_SECTIONS]
    for section in ALLOW_LISTED_POLICY_SECTIONS:
        if section not in fragment:
            continue
        raw_section = fragment.get(section)
        if not isinstance(raw_section, Mapping):
            rejected_keys.append(section)
            continue
        allowed_keys = set(object_dict(base_payload.get(section)))
        if section == "video":
            allowed_keys.update(ALLOW_LISTED_VIDEO_TRANSFORM_KEYS)
        elif section == "audio":
            allowed_keys.update(ALLOW_LISTED_AUDIO_KEYS)
        elif section == "subtitle":
            allowed_keys.update(ALLOW_LISTED_SUBTITLE_KEYS)
        filtered_section: dict[str, Any] = {}
        for key, value in raw_section.items():
            if key not in allowed_keys:
                rejected_keys.append(f"{section}.{key}")
                continue
            if value is not None and not _policy_value_valid(
                    section,
                    str(key),
                    value,
                    object_dict(base_payload.get(section)).get(key),
            ):
                rejected_keys.append(f"{section}.{key}")
                continue
            filtered_section[str(key)] = value
        if filtered_section:
            normalized_fragment[section] = filtered_section
            base_section = base_payload.setdefault(section, {})
            for key, value in filtered_section.items():
                base_section.setdefault(key, value)
    compiled_policy, applied_fragment = apply_seed_policy(
        base_payload,
        normalized_fragment,
        mode="tune",
    )
    for section, requested_section in normalized_fragment.items():
        applied_section = object_dict(applied_fragment.get(section))
        for key, value in object_dict(requested_section).items():
            if value is not None and key not in applied_section:
                rejected_keys.append(f"{section}.{key}")
    return compiled_policy, applied_fragment, sorted(set(rejected_keys))


def _policy_value_valid(section: str, key: str, value: Any, base_value: Any) -> bool:
    if section == "video":
        enum_values = {
            "quality_metric": {"auto", "vmaf", "xpsnr", "ssim", "psnr"},
            "black_bar_handling": {"off", "auto", "smart"},
            "downsample_algorithm": {
                "fast_bilinear",
                "bilinear",
                "bicubic",
                "area",
                "lanczos",
                "spline",
                "neighbor",
            },
            "size_goal_mode": {"normalized", "absolute"},
            "resolution_intent_mode": {"source", "max_height"},
        }
        if key in enum_values:
            return isinstance(value, str) and value.strip().lower() in enum_values[key]
        if key in {"encoder", "pixel_format", "size_goal_source", "resolution_intent_source"}:
            return isinstance(value, str) and bool(value.strip())
        if key in {"crf_search", "thorough"}:
            return isinstance(value, bool)
        numeric_ranges = {
            "preset": (0, 13),
            "target_vmaf": (70, 98),
            "min_target_vmaf": (65, 97),
            "target_xpsnr": (25, 41),
            "min_target_xpsnr": (20, 40),
            "target_relax_step_vmaf": (0.1, 5),
            "target_relax_step_xpsnr": (0.1, 5),
            "min_crf": (0, 63),
            "max_crf": (0, 63),
            "max_encoded_percent": (1, 100),
            "max_height": (0, 4320),
            "black_bar_detect_samples": (1, 5),
            "black_bar_detect_seconds": (1, 120),
            "black_bar_detect_limit": (1, 120),
            "black_bar_detect_round": (2, 64),
            "black_bar_detect_start_seconds": (0, 24 * 60 * 60),
            "default_grain": (0, 50),
            "grain_denoise": (0, 1),
        }
        if key in numeric_ranges:
            minimum, maximum = numeric_ranges[key]
            number = _strict_number(value)
            return number is not None and minimum <= number <= maximum
        if key in {"target_size_mb", "target_size_bytes", "target_runtime_minutes"}:
            number = _strict_number(value)
            return number is not None and number > 0
        if key in {"sample_projection_tolerance_percent", "final_output_tolerance_percent"}:
            number = _strict_number(value)
            return number is not None and 0 < number <= 100
        if key == "size_goal_schema_version":
            return _strict_number(value) == 1
        if key in {"sample_every", "sample_duration"}:
            return isinstance(value, str) and bool(re.fullmatch(r"\s*\d+(?:\.\d+)?\s*(?:ms|s|m|h)\s*", value, re.I))
        if key == "crop":
            return isinstance(value, str) and (not value.strip() or bool(re.fullmatch(r"\d+:\d+:\d+:\d+", value.strip())))
    if section == "audio":
        if key in {"keep_languages", "copy_codecs", "convert_to_opus_codecs"}:
            return _string_list_valid(value)
        bitrate_ranges = {
            "stereo_opus_bitrate": (48, 384),
            "surround_5_1_opus_bitrate": (96, 512),
            "surround_7_1_opus_bitrate": (128, 768),
        }
        if key in bitrate_ranges:
            bitrate = _bitrate_kbps(value)
            minimum, maximum = bitrate_ranges[key]
            return bitrate is not None and minimum <= bitrate <= maximum
    if section == "subtitle":
        if key == "keep_languages":
            return _string_list_valid(value)
        if key in {"prefer_text", "keep_forced"}:
            return isinstance(value, bool)
        if key == "default_mode":
            return isinstance(value, str) and bool(value.strip())
    return _value_matches_base_type(value, base_value)


def _strict_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _bitrate_kbps(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:k|kbps)?\s*", value, re.I)
    return int(float(match.group(1))) if match else None


def _string_list_valid(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and bool(item.strip()) for item in value)


def _value_matches_base_type(value: Any, base_value: Any) -> bool:
    if isinstance(base_value, bool):
        return isinstance(value, bool)
    if isinstance(base_value, int) and not isinstance(base_value, bool):
        return _strict_number(value) is not None and float(value).is_integer()
    if isinstance(base_value, float):
        return _strict_number(value) is not None
    if isinstance(base_value, str):
        return isinstance(value, str) and bool(value.strip())
    if isinstance(base_value, list):
        if all(isinstance(item, str) for item in base_value):
            return _string_list_valid(value)
        return isinstance(value, list)
    return base_value is not None and type(value) is type(base_value)


def build_quality_risk_contract(
        *,
        prefix: str,
        sample_item: Mapping[str, Any],
        current_policy: Mapping[str, Any],
        preview_policy: Mapping[str, Any] | None = None,
        operator_request: Mapping[str, Any] | None = None,
        calibration: Mapping[str, Any] | None = None,
        advice_state: Mapping[str, Any] | None = None,
        latest_failed_sample_job: Mapping[str, Any] | None = None,
        interpretation: Mapping[str, Any] | None = None,
        proposed_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    sample = object_dict(sample_item)
    current_policy_payload = object_dict(current_policy)
    preview_policy_payload = object_dict(preview_policy) or current_policy_payload
    operator_request_payload = object_dict(operator_request)
    calibration_payload = object_dict(calibration)
    advice_payload = object_dict(advice_state)
    failed_payload = object_dict(latest_failed_sample_job)
    source_id = str(sample.get("representative_source_id") or sample.get("source_id") or stable_source_id(sample))
    source_fingerprint = str(sample.get("source_fingerprint") or "").strip() or None
    cadence_decision = object_dict(sample.get("cadence_decision"))
    fingerprint_decision = object_dict(sample.get("media_fingerprint_decision"))
    stream_budget = object_dict(sample.get("stream_budget_ledger"))
    sample_result = object_dict(calibration_payload.get("sample_result"))
    review_moments = _normalized_review_moments(calibration_payload.get("review_moments"))
    sample_job_id = str(calibration_payload.get("job_id") or failed_payload.get("job_id") or "") or None
    target_size_trace = _target_size_trace(
        sample_result,
        failed_payload,
        current_sample_job_id=str(calibration_payload.get("job_id") or "") or None,
    )
    evidence_ids = _current_evidence_ids(
        cadence_decision=cadence_decision,
        fingerprint_decision=fingerprint_decision,
        review_moments=review_moments,
        sample_result=sample_result,
    )
    proposed_fragment = object_dict(proposed_policy) or _policy_difference(
        current_policy_payload,
        preview_policy_payload,
    )
    compiled_policy, applied_fragment, rejected_keys = compile_allow_listed_transform_fragment(
        current_policy_payload,
        proposed_fragment,
    )
    compiled_policy_matches_preview = _policy_values_equivalent(compiled_policy, preview_policy_payload)
    gates = _deterministic_gates(
        source_id=source_id,
        cadence_decision=cadence_decision,
        fingerprint_decision=fingerprint_decision,
        stream_budget=stream_budget,
        sample_result=sample_result,
        target_size_trace=target_size_trace,
        rejected_transform_keys=rejected_keys,
        current_policy=current_policy_payload,
        preview_policy=preview_policy_payload,
        compiled_policy_matches_preview=compiled_policy_matches_preview,
    )
    interpretation_payload = quality_risk_interpretation_payload(interpretation)
    operator_decision = resolve_quality_risk_operator_decision(
        prefix=prefix,
        source_id=source_id,
        reviewed_policy=preview_policy_payload,
        current_sample_job_id=sample_job_id,
        current_evidence_ids=evidence_ids,
        current_moment_count=len(review_moments),
        advice_state=advice_payload,
    )
    risks = _typed_quality_risks(
        cadence_decision=cadence_decision,
        fingerprint_decision=fingerprint_decision,
        operator_request=operator_request_payload,
        stream_budget=stream_budget,
        sample_result=sample_result,
        target_size_trace=target_size_trace,
        review_moments=review_moments,
        interpretation=interpretation_payload,
    )
    verdict = _contract_verdict(gates=gates, operator_decision=operator_decision, interpretation=interpretation_payload)
    return {
        "schema": QUALITY_RISK_CONTRACT_SCHEMA,
        "version": QUALITY_RISK_CONTRACT_VERSION,
        "source_scope": {
            "prefix": prefix,
            "source_id": source_id,
            "source_fingerprint": source_fingerprint,
            "sample_job_id": sample_job_id,
            "evidence_ids": evidence_ids,
        },
        "policy": {
            "current_policy_hash": stable_policy_hash(current_policy_payload),
            "preview_policy_hash": stable_policy_hash(preview_policy_payload),
            "compiled_fragment": applied_fragment,
            "rejected_transform_keys": rejected_keys,
            "compiled_policy_matches_preview": compiled_policy_matches_preview,
        },
        "facts": {
            "current_policy": current_policy_payload,
            "preview_policy": preview_policy_payload,
            "operator_request": operator_request_payload,
            "sample_result": sample_result,
            "review_moments": review_moments,
            "cadence_decision": cadence_decision,
            "media_fingerprint_decision": fingerprint_decision,
            "stream_budget": stream_budget,
            "target_size_trace": target_size_trace,
        },
        "deterministic_gates": gates,
        "interpretation": interpretation_payload,
        "operator_decision": operator_decision,
        "typed_risks": risks,
        "verdict": verdict,
        "pre_test_instruction": pre_test_instruction_record(
            prefix=prefix,
            source_id=source_id,
            policy_hash=stable_policy_hash(preview_policy_payload),
            sample_job_id=sample_job_id,
            review_moments=review_moments,
            typed_risks=risks,
        ),
    }


def target_size_search_public_view(trace: Mapping[str, Any] | None) -> dict[str, Any] | None:
    payload = object_dict(trace)
    if not payload:
        return None
    target = object_dict(payload.get("target"))
    curve = object_dict(payload.get("curve"))
    quality_floor = object_dict(payload.get("quality_floor"))
    selected = object_dict(payload.get("selected_candidate"))
    transform_plan = object_dict(payload.get("transform_plan"))
    return {
        "trace_id": f"tss1_{stable_json_hash(payload)[:24]}",
        "schema_version": int_value(payload.get("schema_version")),
        "status": str(payload.get("status") or "needs_review"),
        "selection_reason": str(payload.get("selection_reason") or "") or None,
        "curve_shape": str(curve.get("shape") or "") or None,
        "candidate_count": int_value(curve.get("candidate_count")),
        "max_candidates": int_value(curve.get("max_candidates")),
        "target_bytes": int_value(target.get("total_target_bytes")) or None,
        "lower_bound_bytes": int_value(target.get("sample_lower_bound_bytes")) or None,
        "upper_bound_bytes": int_value(target.get("sample_upper_bound_bytes")) or None,
        "selected_crf": float_value(selected.get("crf")) if selected else None,
        "selected_metric": str(selected.get("metric") or quality_floor.get("metric") or "") or None,
        "selected_metric_score": float_value(selected.get("metric_score")) if selected else None,
        "minimum_metric_score": float_value(quality_floor.get("minimum")) or None,
        "quality_floor_met": bool(selected.get("quality_floor_met")) if selected else None,
        "predicted_whole_episode_bytes": int_value(selected.get("predicted_whole_episode_bytes")) or None,
        "within_sample_band": bool(selected.get("within_sample_band")) if selected else None,
        "transform_plan_id": str(transform_plan.get("transform_plan_id") or "") or None,
    }


def quality_risk_public_view(contract: Mapping[str, Any] | None) -> dict[str, Any] | None:
    payload = object_dict(contract)
    if not payload:
        return None
    source_scope = object_dict(payload.get("source_scope"))
    gates = object_dict(payload.get("deterministic_gates"))
    decision = object_dict(payload.get("operator_decision"))
    interpretation = object_dict(payload.get("interpretation"))
    typed_risks = [object_dict(item) for item in object_list(payload.get("typed_risks")) if object_dict(item)]
    facts = object_dict(payload.get("facts"))
    review_moments = [object_dict(item) for item in object_list(facts.get("review_moments"))]
    policy = object_dict(payload.get("policy"))
    return {
        "schema": str(payload.get("schema") or QUALITY_RISK_CONTRACT_SCHEMA),
        "version": int_value(payload.get("version")) or QUALITY_RISK_CONTRACT_VERSION,
        "verdict": str(payload.get("verdict") or "request_comparison"),
        "source_id": str(source_scope.get("source_id") or "") or None,
        "source_fingerprint": str(source_scope.get("source_fingerprint") or "") or None,
        "sample_job_id": str(source_scope.get("sample_job_id") or "") or None,
        "evidence_ids": _normalized_evidence_ids(source_scope.get("evidence_ids")),
        "policy_hash": str(policy.get("preview_policy_hash") or "") or None,
        "current_policy_hash": str(policy.get("current_policy_hash") or "") or None,
        "preview_policy_hash": str(policy.get("preview_policy_hash") or "") or None,
        "blocked": bool(gates.get("blocked")),
        "blocking_reasons": [str(reason) for reason in object_list(gates.get("blocking_reasons")) if str(reason).strip()],
        "request_comparison": bool(gates.get("request_comparison")),
        "comparison_reason": str(gates.get("comparison_reason") or "") or None,
        "typed_risks": typed_risks,
        "operator_decision": decision,
        "interpretation": interpretation,
        "moment_count": len(review_moments),
        "review_moment_ids": _normalized_evidence_ids(
            [moment.get("evidence_id") for moment in review_moments]
        ),
        "target_size_search": target_size_search_public_view(object_dict(facts.get("target_size_trace"))),
        "pre_test_instruction": object_dict(payload.get("pre_test_instruction")) or None,
    }


def resolve_quality_risk_operator_decision(
        *,
        prefix: str,
        source_id: str,
        reviewed_policy: Mapping[str, Any],
        current_sample_job_id: str | None,
        current_evidence_ids: list[str],
        current_moment_count: int,
        advice_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    advice_payload = object_dict(advice_state)
    current_policy_hash = stable_policy_hash(reviewed_policy)
    records = quality_risk_decision_records(
        prefix=prefix,
        source_id=source_id,
        current_policy_hash=current_policy_hash,
        current_sample_job_id=current_sample_job_id,
        current_evidence_ids=current_evidence_ids,
        current_moment_count=current_moment_count,
        advice_state=advice_payload,
    )
    effective = next(
        (record for record in records if record.authoritative and record.kind == "post_test"),
        None,
    )
    status = str(effective.verdict) if effective is not None else "pending"
    return {
        "status": status,
        "current_rejection_outranks_approval": True,
        "reviewed_policy_hash": current_policy_hash,
        "records": [record.to_payload() for record in records],
        "effective_record": effective.to_payload() if effective is not None else None,
    }


def quality_risk_decision_records(
        *,
        prefix: str,
        source_id: str,
        current_policy_hash: str,
        current_sample_job_id: str | None,
        current_evidence_ids: list[str],
        current_moment_count: int,
        advice_state: Mapping[str, Any] | None,
) -> list[QualityRiskDecisionRecord]:
    advice_payload = object_dict(advice_state)
    raw_records = object_list(advice_payload.get("quality_risk_records"))
    records: list[QualityRiskDecisionRecord] = []
    expected_evidence_ids = tuple(sorted(_normalized_evidence_ids(current_evidence_ids)))
    for raw_record in raw_records:
        record = object_dict(raw_record)
        record_evidence_ids = tuple(sorted(_record_evidence_ids(record)))
        moment_indexes = tuple(_normalized_moment_indexes(record.get("moment_indexes")))
        moments_current = all(index <= current_moment_count for index in moment_indexes)
        authoritative = (
            current_sample_job_id is not None
            and bool(expected_evidence_ids)
            and str(record.get("prefix") or "") == prefix
            and str(record.get("source_id") or "") == source_id
            and str(record.get("policy_hash") or "") == current_policy_hash
            and str(record.get("sample_job_id") or "") == current_sample_job_id
            and record_evidence_ids == expected_evidence_ids
            and moments_current
        )
        records.append(
            QualityRiskDecisionRecord(
                kind=str(record.get("kind") or "post_test"),
                created_at=str(record.get("created_at") or "") or None,
                verdict=str(record.get("verdict") or "pending"),
                tags=tuple(normalize_quality_risk_tags(record.get("tags"))),
                details=str(record.get("details") or "").strip(),
                evidence_ids=record_evidence_ids,
                moment_indexes=moment_indexes,
                sample_job_id=str(record.get("sample_job_id") or "") or None,
                policy_hash=str(record.get("policy_hash") or "") or None,
                source_id=str(record.get("source_id") or "") or None,
                prefix=str(record.get("prefix") or "") or None,
                authoritative=authoritative,
            )
        )
    records.sort(
        key=lambda item: (
            item.created_at or "",
            int(item.verdict == "rejected"),
            item.kind,
        ),
        reverse=True,
    )
    return records


def append_quality_risk_record(
        existing_state: Mapping[str, Any] | None,
        *,
        prefix: str,
        source_id: str,
        policy_hash: str,
        sample_job_id: str | None,
        kind: str,
        verdict: str,
        tags: list[str] | tuple[str, ...] | None,
        details: str,
        created_at: str,
        evidence_id: str | None = None,
        evidence_ids: list[str] | tuple[str, ...] | None = None,
        moment_indexes: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    payload = dict(object_dict(existing_state))
    records = [object_dict(record) for record in object_list(payload.get("quality_risk_records"))]
    normalized_evidence_ids = _normalized_evidence_ids(list(evidence_ids or []))
    if evidence_id and evidence_id not in normalized_evidence_ids:
        normalized_evidence_ids.append(evidence_id)
    record_payload = {
        "kind": kind,
        "verdict": verdict,
        "tags": normalize_quality_risk_tags(list(tags or [])),
        "details": details.strip(),
        "evidence_id": normalized_evidence_ids[0] if normalized_evidence_ids else None,
        "evidence_ids": normalized_evidence_ids,
        "moment_indexes": _normalized_moment_indexes(list(moment_indexes or [])),
        "sample_job_id": sample_job_id,
        "policy_hash": policy_hash,
        "source_id": source_id,
        "prefix": prefix,
        "created_at": created_at,
    }
    binding_keys = ("kind", "verdict", "sample_job_id", "policy_hash", "source_id", "prefix")
    records = [
        record
        for record in records
        if not all(record.get(key) == record_payload.get(key) for key in binding_keys)
    ]
    records.append(record_payload)
    payload["quality_risk_records"] = records[-100:]
    return payload


def pre_test_instruction_record(
        *,
        prefix: str,
        source_id: str,
        policy_hash: str,
        sample_job_id: str | None,
        review_moments: list[Mapping[str, Any]],
        typed_risks: list[Mapping[str, Any]],
) -> dict[str, Any]:
    focus_tags = [
        str(risk.get("tag") or "other")
        for risk in typed_risks
        if str(risk.get("level") or "unknown") in {"medium", "high"}
    ]
    unique_focus_tags: list[str] = []
    for tag in focus_tags:
        normalized = normalize_quality_risk_tag(tag)
        if normalized not in unique_focus_tags:
            unique_focus_tags.append(normalized)
    moment_refs = []
    for index, moment in enumerate(review_moments, start=1):
        moment_payload = object_dict(moment)
        if not moment_payload:
            continue
        moment_refs.append(
            {
                "moment": index,
                "timestamp_seconds": float_value(moment_payload.get("timestamp_seconds")),
                "role": str(moment_payload.get("role") or "typical"),
                "risk_tags": normalize_quality_risk_tags(moment_payload.get("risk_tags")),
                "evidence_id": str(moment_payload.get("evidence_id") or "") or None,
                "rationale": str(moment_payload.get("rationale") or "").strip() or None,
            }
        )
    return {
        "prefix": prefix,
        "source_id": source_id,
        "policy_hash": policy_hash,
        "sample_job_id": sample_job_id,
        "focus_tags": unique_focus_tags,
        "focus_labels": [quality_risk_tag_label(tag) for tag in unique_focus_tags],
        "moments": moment_refs,
    }


def _normalized_review_moments(raw_moments: Any) -> list[dict[str, Any]]:
    moments: list[dict[str, Any]] = []
    for raw_moment in object_list(raw_moments):
        moment = object_dict(raw_moment)
        if not moment:
            continue
        moments.append(
            {
                "timestamp_seconds": float_value(moment.get("timestamp_seconds")),
                "duration_seconds": float_value(moment.get("duration_seconds")),
                "role": str(moment.get("role") or "typical"),
                "risk_tags": normalize_quality_risk_tags(moment.get("risk_tags")),
                "rationale": str(moment.get("rationale") or "").strip(),
                "confidence": float_value(moment.get("confidence")),
                "coverage": float_value(moment.get("coverage")),
                "evidence_id": str(moment.get("evidence_id") or "") or None,
            }
        )
    return moments


def _normalized_evidence_ids(raw_ids: Any) -> list[str]:
    evidence_ids: list[str] = []
    for raw_id in _sequence_values(raw_ids):
        if raw_id is None:
            continue
        value = str(raw_id).strip()
        if value and value.lower() != 'none' and value not in evidence_ids:
            evidence_ids.append(value)
    return evidence_ids


def _normalized_moment_indexes(raw_indexes: Any) -> list[int]:
    indexes: list[int] = []
    for raw_index in _sequence_values(raw_indexes):
        value = int_value(raw_index)
        if value > 0 and value not in indexes:
            indexes.append(value)
    return indexes


def _sequence_values(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def quality_risk_feedback_moment_indexes(
        tags: list[str] | tuple[str, ...] | None,
        review_moments: Any,
) -> list[int]:
    normalized_tags = {tag for tag in normalize_quality_risk_tags(tags) if tag != "other"}
    if not normalized_tags:
        return []
    indexes: list[int] = []
    for index, raw_moment in enumerate(object_list(review_moments), start=1):
        moment_tags = {
            tag
            for tag in normalize_quality_risk_tags(object_dict(raw_moment).get("risk_tags"))
            if tag != "other"
        }
        if normalized_tags & moment_tags:
            indexes.append(index)
    return indexes


def _policy_difference(
        current_policy: Mapping[str, Any],
        preview_policy: Mapping[str, Any],
) -> dict[str, Any]:
    current = object_dict(current_policy)
    preview = object_dict(preview_policy)
    fragment: dict[str, Any] = {}
    for section in sorted(set(current) | set(preview)):
        current_section = current.get(section)
        preview_section = preview.get(section)
        if isinstance(current_section, Mapping) or isinstance(preview_section, Mapping):
            current_values = object_dict(current_section)
            preview_values = object_dict(preview_section)
            changed = {
                key: preview_values.get(key)
                for key in sorted(set(current_values) | set(preview_values))
                if current_values.get(key) != preview_values.get(key)
            }
            if changed:
                fragment[section] = changed
        elif current_section != preview_section:
            fragment[section] = preview_section
    return fragment


def _policy_values_equivalent(left: Any, right: Any) -> bool:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        left_payload = object_dict(left)
        right_payload = object_dict(right)
        return set(left_payload) == set(right_payload) and all(
            _policy_values_equivalent(left_payload[key], right_payload[key])
            for key in left_payload
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _policy_values_equivalent(left_value, right_value)
            for left_value, right_value in zip(left, right, strict=True)
        )
    if (
            isinstance(left, int | float)
            and not isinstance(left, bool)
            and isinstance(right, int | float)
            and not isinstance(right, bool)
    ):
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=0.01)
    return left == right


def _target_size_trace(
        sample_result: Mapping[str, Any],
        failed_sample_job: Mapping[str, Any],
        *,
        current_sample_job_id: str | None,
) -> dict[str, Any]:
    direct = object_dict(sample_result.get("target_size_trace"))
    if direct:
        return direct
    failed_job_id = str(failed_sample_job.get("job_id") or "") or None
    if current_sample_job_id is not None and failed_job_id != current_sample_job_id:
        return {}
    failed_result = object_dict(failed_sample_job.get("result"))
    return (
        object_dict(failed_result.get("target_size_trace"))
        or object_dict(object_dict(failed_result.get("sample_result")).get("target_size_trace"))
    )


def _current_evidence_ids(
        *,
        cadence_decision: Mapping[str, Any],
        fingerprint_decision: Mapping[str, Any],
        review_moments: list[Mapping[str, Any]],
        sample_result: Mapping[str, Any],
) -> list[str]:
    return _normalized_evidence_ids(
        [
            cadence_decision.get("evidence_id"),
            fingerprint_decision.get("evidence_id"),
            sample_result.get("cadence_evidence_id"),
            sample_result.get("media_fingerprint_evidence_id"),
            *[object_dict(moment).get("evidence_id") for moment in review_moments],
        ]
    )


def _record_evidence_ids(record: Mapping[str, Any]) -> list[str]:
    evidence_ids = _normalized_evidence_ids(record.get("evidence_ids"))
    legacy_evidence_id = str(record.get("evidence_id") or "").strip()
    if legacy_evidence_id and legacy_evidence_id not in evidence_ids:
        evidence_ids.append(legacy_evidence_id)
    return evidence_ids


def _deterministic_gates(
        *,
        source_id: str,
        cadence_decision: Mapping[str, Any],
        fingerprint_decision: Mapping[str, Any],
        stream_budget: Mapping[str, Any],
        sample_result: Mapping[str, Any],
        target_size_trace: Mapping[str, Any],
        rejected_transform_keys: list[str],
        current_policy: Mapping[str, Any],
        preview_policy: Mapping[str, Any],
        compiled_policy_matches_preview: bool,
) -> dict[str, Any]:
    blocking_reasons: list[str] = []
    comparison_reason = ""
    has_cadence_decision = bool(cadence_decision)
    cadence_status = str(cadence_decision.get("classification") or "unknown")
    cadence_transform = str(cadence_decision.get("transform") or "")
    cadence_confidence = float_value(cadence_decision.get("confidence"))
    if has_cadence_decision and (
            cadence_status in {"mixed", "unknown"}
            or (cadence_status in {"tff", "bff", "telecine"} and not cadence_transform)
    ):
        blocking_reasons.append(str(cadence_decision.get("rationale") or "Cadence needs more evidence before sampling."))
    feasibility_status = str(object_dict(stream_budget.get("feasibility")).get("status") or "")
    if feasibility_status == "arithmetically_infeasible":
        blocking_reasons.append("The resolved stream budget leaves no positive video budget.")
    if rejected_transform_keys:
        blocking_reasons.append(
            "The requested transform includes non-allow-listed keys: " + ", ".join(sorted(rejected_transform_keys))
        )
    if not compiled_policy_matches_preview:
        blocking_reasons.append("The proposed policy does not match the deterministically compiled allow-listed policy.")
    target_size_status = str(target_size_trace.get("status") or "")
    if target_size_trace:
        if int_value(target_size_trace.get("schema_version")) != TARGET_SIZE_SEARCH_SCHEMA_VERSION:
            blocking_reasons.append("The target-size search trace uses an unsupported schema version.")
        target_ledger = object_dict(target_size_trace.get("ledger"))
        trace_source_id = str(target_ledger.get("source_id") or "")
        if trace_source_id and trace_source_id != source_id:
            blocking_reasons.append("The target-size search trace belongs to a different source item.")
        current_ledger_id = str(stream_budget.get("ledger_id") or "")
        trace_ledger_id = str(target_ledger.get("ledger_id") or "")
        if current_ledger_id and trace_ledger_id and trace_ledger_id != current_ledger_id:
            blocking_reasons.append("The target-size search trace belongs to a different stream-budget ledger.")
        current_stream_plan_id = str(object_dict(stream_budget.get("stream_plan")).get("plan_id") or "")
        trace_stream_plan_id = str(target_ledger.get("stream_plan_id") or "")
        if current_stream_plan_id and trace_stream_plan_id and trace_stream_plan_id != current_stream_plan_id:
            blocking_reasons.append("The target-size search trace belongs to a different production stream plan.")
        transform_plan = object_dict(target_size_trace.get("transform_plan"))
        if not target_size_transform_plan_valid(transform_plan):
            blocking_reasons.append("The target-size search transform identity is missing or invalid.")
        else:
            transform_cadence_evidence_id = str(transform_plan.get("cadence_evidence_id") or "")
            current_cadence_evidence_id = str(cadence_decision.get("evidence_id") or "")
            if (
                    current_cadence_evidence_id
                    and transform_cadence_evidence_id != current_cadence_evidence_id
            ):
                blocking_reasons.append("The target-size search trace uses stale cadence evidence.")
            if str(transform_plan.get("cadence_class") or "") != cadence_status:
                blocking_reasons.append("The target-size search trace uses a different cadence classification.")
            if str(transform_plan.get("cadence_transform") or "") != cadence_transform:
                blocking_reasons.append("The target-size search trace uses a different cadence transform.")
        if target_size_status == "infeasible":
            blocking_reasons.append(
                str(target_size_trace.get("selection_reason") or "The requested target size is arithmetically infeasible.")
            )
        elif target_size_status in {"quality_conflict", "needs_review"}:
            comparison_reason = str(
                target_size_trace.get("selection_reason")
                or "The target-size search needs operator review before this policy can be reused."
            )
        elif target_size_status not in {"", "selected"}:
            blocking_reasons.append("The target-size search trace has an unsupported status.")
        if str(object_dict(target_size_trace.get("curve")).get("shape") or "") == "non_monotonic":
            comparison_reason = comparison_reason or "The measured target-size curve is non-monotonic and needs comparison."
    if not comparison_reason:
        fingerprint_status = str(fingerprint_decision.get("status") or "unknown")
        fingerprint_confidence = float_value(fingerprint_decision.get("confidence"))
        low_confidence = object_list(fingerprint_decision.get("low_confidence_advisories"))
        if not has_cadence_decision:
            comparison_reason = "Cadence evidence is unavailable for this sample, so review or a fresh scan is safer than silent reuse."
        elif fingerprint_status != "measured":
            comparison_reason = str(fingerprint_decision.get("rationale") or "Fingerprint evidence is unknown.")
        elif low_confidence:
            comparison_reason = "Low-confidence fingerprint findings require comparison before destructive cleanup."
        elif fingerprint_confidence < 0.55:
            comparison_reason = "Measured fingerprint confidence is low, so comparison is safer than silent cleanup."
        elif cadence_confidence < 0.8 and cadence_status not in {"progressive", "none"}:
            comparison_reason = "Cadence confidence is too low for destructive treatment without comparison."
    return {
        "blocked": bool(blocking_reasons),
        "blocking_reasons": blocking_reasons,
        "request_comparison": bool(comparison_reason) and not blocking_reasons,
        "comparison_reason": comparison_reason or None,
        "cadence_gate": cadence_status,
        "cadence_transform": cadence_transform or None,
        "non_video_feasibility": feasibility_status or None,
        "allow_list_rejections": rejected_transform_keys,
        "policy_hash_matches_preview": stable_policy_hash(current_policy) == stable_policy_hash(preview_policy),
        "compiled_policy_matches_preview": compiled_policy_matches_preview,
        "sample_result_present": bool(sample_result),
        "target_size_status": target_size_status or None,
        "target_size_trace_present": bool(target_size_trace),
    }


def _typed_quality_risks(
        *,
        cadence_decision: Mapping[str, Any],
        fingerprint_decision: Mapping[str, Any],
        operator_request: Mapping[str, Any],
        stream_budget: Mapping[str, Any],
        sample_result: Mapping[str, Any],
        target_size_trace: Mapping[str, Any],
        review_moments: list[Mapping[str, Any]],
        interpretation: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    risks_by_tag: dict[str, dict[str, Any]] = {}
    findings = [object_dict(item) for item in object_list(fingerprint_decision.get("findings"))]
    for finding in findings:
        finding_id = str(finding.get("id") or "")
        confidence = float_value(finding.get("confidence"))
        rationale = str(finding.get("rationale") or "").strip()
        if finding_id in {"likely_film_grain", "likely_analog_noise", "compression_noise_advisory", "uncertain_noise_mix"}:
            _add_risk(
                risks_by_tag,
                tag="grain_noise_treatment",
                level=_risk_level(confidence, high_threshold=0.78, medium_threshold=0.58),
                rationale=rationale,
                evidence_ids=_normalized_evidence_ids([fingerprint_decision.get("evidence_id")]),
            )
        elif finding_id in {"dark_luma", "dark_gradient_banding_risk"}:
            _add_risk(
                risks_by_tag,
                tag="banding_dark_scene_damage",
                level=_risk_level(confidence, high_threshold=0.72, medium_threshold=0.52),
                rationale=rationale,
                evidence_ids=_normalized_evidence_ids([fingerprint_decision.get("evidence_id")]),
            )
        elif finding_id in {"high_motion", "duplicate_cadence"}:
            _add_risk(
                risks_by_tag,
                tag="motion_breakup",
                level=_risk_level(confidence, high_threshold=0.74, medium_threshold=0.54),
                rationale=rationale,
                evidence_ids=_normalized_evidence_ids([fingerprint_decision.get("evidence_id")]),
            )
        elif finding_id in {"high_texture", "animation_cues"}:
            _add_risk(
                risks_by_tag,
                tag="softness_detail_loss",
                level=_risk_level(confidence, high_threshold=0.76, medium_threshold=0.56),
                rationale=rationale,
                evidence_ids=_normalized_evidence_ids([fingerprint_decision.get("evidence_id")]),
            )
        elif finding_id == "audio_complexity":
            _add_risk(
                risks_by_tag,
                tag="audio_quality_layout",
                level=_risk_level(confidence, high_threshold=0.72, medium_threshold=0.50),
                rationale=rationale,
                evidence_ids=_normalized_evidence_ids([fingerprint_decision.get("evidence_id")]),
            )
    cadence_class = str(cadence_decision.get("classification") or "unknown")
    if cadence_class in {"mixed", "telecine", "tff", "bff", "unknown"}:
        _add_risk(
            risks_by_tag,
            tag="cadence_interlace_artifacts",
            level=_risk_level(float_value(cadence_decision.get("confidence")), high_threshold=0.80, medium_threshold=0.55),
            rationale=str(cadence_decision.get("rationale") or "Measured cadence needs explicit review."),
            evidence_ids=_normalized_evidence_ids([cadence_decision.get("evidence_id")]),
        )
    feasibility = object_dict(stream_budget.get("feasibility"))
    feasibility_status = str(feasibility.get("status") or "")
    if feasibility_status == "aggressive_but_measurable":
        _add_risk(
            risks_by_tag,
            tag="softness_detail_loss",
            level="medium",
            rationale="The remaining video budget is aggressive enough that detail loss is more likely at the requested size.",
            evidence_ids=[],
        )
    if feasibility_status == "requires_measurement":
        _add_risk(
            risks_by_tag,
            tag="other",
            level="unknown",
            rationale="Non-video cost uncertainty still requires measurement before trusting the size tradeoff.",
            evidence_ids=[],
        )
    if sample_result:
        predicted_percent = float_value(sample_result.get("predicted_encode_percent"))
        if predicted_percent > 0 and predicted_percent <= 8.0:
            _add_risk(
                risks_by_tag,
                tag="softness_detail_loss",
                level="medium",
                rationale="The sampled result projects to a very small fraction of the source size, so preserved detail should be checked directly.",
                evidence_ids=[],
            )
    target_size_status = str(target_size_trace.get("status") or "")
    target_size_reason = str(target_size_trace.get("selection_reason") or "").strip()
    if target_size_status == "quality_conflict":
        _add_risk(
            risks_by_tag,
            tag="softness_detail_loss",
            level="high",
            rationale=target_size_reason or "The measured target cannot be reached without crossing the quality floor.",
            evidence_ids=[],
        )
    elif target_size_status in {"needs_review", "infeasible"}:
        _add_risk(
            risks_by_tag,
            tag="other",
            level="high" if target_size_status == "infeasible" else "medium",
            rationale=target_size_reason or "The measured target-size search needs operator review.",
            evidence_ids=[],
        )
    if str(object_dict(target_size_trace.get("curve")).get("shape") or "") == "non_monotonic":
        _add_risk(
            risks_by_tag,
            tag="other",
            level="medium",
            rationale="The measured target-size curve is non-monotonic, so another comparison is safer than extrapolation.",
            evidence_ids=[],
        )
    if operator_request:
        evidence_authority = str(operator_request.get("evidence_authority") or "none")
        operator_tags = [
            tag for tag in normalize_quality_risk_tags(operator_request.get("quality_risk_tags"))
            if tag != "other"
        ]
        operator_rationale = str(
            operator_request.get("quality_risk_details")
            or operator_request.get("request_text")
            or ""
        ).strip()
        for tag in operator_tags:
            _add_risk(
                risks_by_tag,
                tag=tag,
                level="high" if evidence_authority == "rejected_visual_result" else "medium",
                rationale=operator_rationale or "The operator asked to preserve or review this quality dimension.",
                evidence_ids=[],
            )
        if evidence_authority == "rejected_visual_result" and not operator_tags:
            _add_risk(
                risks_by_tag,
                tag="other",
                level="high",
                rationale="The current operator note contains a visual or audible rejection of the current sample.",
                evidence_ids=[],
            )
    for index, moment in enumerate(review_moments, start=1):
        moment_payload = object_dict(moment)
        for tag in normalize_quality_risk_tags(moment_payload.get("risk_tags")):
            if tag == "other":
                continue
            _add_risk(
                risks_by_tag,
                tag=_moment_tag_to_contract_tag(tag),
                level="medium",
                rationale=str(moment_payload.get("rationale") or "Measured review moment highlights this risk."),
                evidence_ids=_normalized_evidence_ids([moment_payload.get("evidence_id")]),
                moment_indexes=[index],
            )
    interpretation_payload = object_dict(interpretation)
    for risk in [object_dict(item) for item in object_list(interpretation_payload.get("risks"))]:
        _add_risk(
            risks_by_tag,
            tag=str(risk.get("tag") or "other"),
            level=str(risk.get("level") or "unknown"),
            rationale=str(risk.get("rationale") or "").strip(),
            evidence_ids=_normalized_evidence_ids(risk.get("evidence_ids")),
            moment_indexes=_normalized_moment_indexes(risk.get("moment_indexes")),
        )
    ordered: list[dict[str, Any]] = []
    for tag in QUALITY_RISK_TAGS:
        risk = risks_by_tag.get(tag)
        if risk is not None:
            ordered.append(risk)
    return ordered


def _moment_tag_to_contract_tag(tag: str) -> str:
    return normalize_quality_risk_tag(tag)


def _add_risk(
        risks_by_tag: dict[str, dict[str, Any]],
        *,
        tag: str,
        level: str,
        rationale: str,
        evidence_ids: list[str],
        moment_indexes: list[int] | None = None,
) -> None:
    normalized_tag = normalize_quality_risk_tag(tag)
    normalized_level = level if level in QUALITY_RISK_LEVELS else "unknown"
    existing = risks_by_tag.get(normalized_tag)
    if existing is None:
        risks_by_tag[normalized_tag] = {
            "tag": normalized_tag,
            "label": quality_risk_tag_label(normalized_tag),
            "level": normalized_level,
            "rationale": rationale,
            "evidence_ids": list(evidence_ids),
            "moment_indexes": list(moment_indexes or []),
        }
        return
    existing["level"] = _max_risk_level(str(existing.get("level") or "unknown"), normalized_level)
    if rationale and rationale not in str(existing.get("rationale") or ""):
        joined = str(existing.get("rationale") or "").strip()
        existing["rationale"] = f"{joined} {rationale}".strip() if joined else rationale
    for evidence_id in evidence_ids:
        if evidence_id and evidence_id not in object_list(existing.get("evidence_ids")):
            object_list(existing.get("evidence_ids")).append(evidence_id)
    for moment_index in moment_indexes or []:
        if moment_index and moment_index not in object_list(existing.get("moment_indexes")):
            object_list(existing.get("moment_indexes")).append(moment_index)


def _max_risk_level(left: str, right: str) -> str:
    order = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
    return left if order.get(left, 0) >= order.get(right, 0) else right


def _risk_level(confidence: float, *, high_threshold: float, medium_threshold: float) -> str:
    if confidence >= high_threshold:
        return "high"
    if confidence >= medium_threshold:
        return "medium"
    if confidence > 0:
        return "low"
    return "unknown"


def _contract_verdict(
        *,
        gates: Mapping[str, Any],
        operator_decision: Mapping[str, Any],
        interpretation: Mapping[str, Any] | None,
) -> str:
    if bool(gates.get("blocked")):
        return "blocked"
    if str(operator_decision.get("status") or "") == "rejected":
        return "blocked"
    if bool(gates.get("request_comparison")):
        return "request_comparison"
    if str(operator_decision.get("status") or "") == "approved":
        return "safe_to_sample"
    if str(operator_decision.get("status") or "") == "needs_operator_review":
        return "needs_operator_review"
    interpretation_payload = object_dict(interpretation)
    interpretation_verdict = str(interpretation_payload.get("verdict") or "")
    if interpretation_verdict in {"blocked", "request_comparison"}:
        return "request_comparison"
    return "needs_operator_review"
