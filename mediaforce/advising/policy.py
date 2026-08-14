import json
import re
from typing import Any

from mediaforce.core.type_defs import JSONObject, JSONValue, float_value, int_value, object_dict

_SKIP_POLICY_VALUE = object()

_VIDEO_TRANSFORM_DEFAULTS: dict[str, JSONValue] = {
    "max_height": 0,
    "downsample_algorithm": "lanczos",
    "black_bar_handling": "off",
    "black_bar_detect_samples": 3,
    "black_bar_detect_seconds": 60,
    "black_bar_detect_limit": 24,
    "black_bar_detect_round": 2,
    "black_bar_detect_start_seconds": 0,
    "crop": "",
}
ADVISOR_PROTECTED_VIDEO_KEYS = frozenset(
    {
        "compression_intent_schema_version",
        "compression_intent",
        "compression_intent_source",
        "compression_intent_confirmed",
        "target_size_mb",
        "target_size_bytes",
        "target_runtime_minutes",
        "size_goal_schema_version",
        "size_goal_mode",
        "size_goal_source",
        "sample_projection_tolerance_percent",
        "final_output_tolerance_percent",
        "min_target_vmaf",
        "min_target_xpsnr",
        "max_encoded_percent",
    }
)


def tune_response_schema(current_policy: dict[str, Any], *, request_dispositions: tuple[str, ...]) -> dict[str, Any]:
    return _advice_response_schema(policy_response_schema(current_policy), request_dispositions=request_dispositions)


def seed_response_schema(base_policy: dict[str, Any], *, request_dispositions: tuple[str, ...]) -> dict[str, Any]:
    return _advice_response_schema(policy_response_schema(base_policy), request_dispositions=request_dispositions)


def _advice_response_schema(policy_schema: dict[str, Any], *, request_dispositions: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "request_response",
            "request_disposition",
            "summary",
            "diagnosis",
            "confidence",
            "evidence_checked",
            "suggested_follow_up",
            "feasibility_note",
            "policy",
        ],
        "properties": {
            "request_response": {"type": "string"},
            "request_disposition": {"type": "string", "enum": list(request_dispositions)},
            "summary": {"type": "string"},
            "diagnosis": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "evidence_checked": {"type": "array", "items": {"type": "string"}},
            "suggested_follow_up": {"type": ["string", "null"]},
            "feasibility_note": {"type": ["string", "null"]},
            "policy": policy_schema,
        },
    }


def review_artifact_critique_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "summary",
            "confidence",
            "weakest_moments",
            "preserved_strengths",
            "artifacts_to_recheck",
            "recommendation",
            "evidence_checked",
        ],
        "properties": {
            "summary": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "weakest_moments": {"type": "array", "items": {"type": "string"}},
            "preserved_strengths": {"type": "array", "items": {"type": "string"}},
            "artifacts_to_recheck": {"type": "array", "items": {"type": "string"}},
            "recommendation": {"type": "string"},
            "evidence_checked": {"type": "array", "items": {"type": "string"}},
        },
    }


def operator_note_parse_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "summary",
            "intent_type",
            "request_type",
            "operator_confirmed",
            "measured_size_followup",
            "evidence_authority",
            "metric",
            "metric_target",
            "size_budget_value",
            "size_budget_unit",
            "scale_height",
            "black_bar_handling",
            "crop",
            "reasoning_note",
        ],
        "properties": {
            "summary": {"type": "string"},
            "intent_type": {
                "type": "string",
                "enum": ["direct_request", "exploratory_question", "approval_feedback", "other", "unclear"],
            },
            "request_type": {
                "type": "string",
                "enum": ["none", "metric_target", "size_budget", "scale_target", "combined_experiment"],
            },
            "operator_confirmed": {"type": "boolean"},
            "measured_size_followup": {"type": "boolean"},
            "evidence_authority": {
                "type": "string",
                "enum": ["none"],
            },
            "metric": {"type": ["string", "null"], "enum": ["vmaf", "xpsnr", None]},
            "metric_target": {"type": ["number", "null"]},
            "size_budget_value": {"type": ["number", "null"]},
            "size_budget_unit": {"type": ["string", "null"], "enum": ["kb", "mb", "gb", "tb", None]},
            "scale_height": {"type": ["number", "null"]},
            "black_bar_handling": {"type": ["string", "null"], "enum": ["smart", "auto", "off", None]},
            "crop": {"type": ["string", "null"]},
            "reasoning_note": {"type": "string"},
        },
    }


def has_nonpositive_video_budget(requested_experiment: dict[str, Any] | None) -> bool:
    request = object_dict(requested_experiment)
    for candidate in (request, object_dict(request.get("size_budget_request"))):
        ledger = object_dict(candidate.get("stream_budget_ledger"))
        feasibility = object_dict(ledger.get("feasibility"))
        if str(feasibility.get("status") or "") == "arithmetically_infeasible":
            return True
        remaining_video_bytes = object_dict(ledger.get("totals")).get("remaining_video_bytes")
        if (
                isinstance(remaining_video_bytes, int | float)
                and not isinstance(remaining_video_bytes, bool)
                and float(remaining_video_bytes) <= 0
        ):
            return True
        estimated_video_bitrate = candidate.get("estimated_video_bitrate_kbps")
        if (
                isinstance(estimated_video_bitrate, int | float)
                and not isinstance(estimated_video_bitrate, bool)
                and float(estimated_video_bitrate) <= 0
        ):
            return True
        budget_bytes = candidate.get("budget_bytes")
        estimated_audio_bytes = candidate.get("estimated_audio_bytes")
        if (
                isinstance(budget_bytes, int | float)
                and not isinstance(budget_bytes, bool)
                and isinstance(estimated_audio_bytes, int | float)
                and not isinstance(estimated_audio_bytes, bool)
                and float(budget_bytes) <= float(estimated_audio_bytes)
        ):
            return True
    return False


def extract_seed_payload(raw: str) -> JSONObject:
    candidate = raw.strip()
    if not candidate:
        return {}
    parsed = try_load_json(candidate)
    if isinstance(parsed, dict):
        return parsed
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        return {}
    parsed = try_load_json(candidate[start: end + 1])
    return parsed if isinstance(parsed, dict) else {}


def try_load_json(raw: str) -> JSONValue:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def policy_key_paths(policy: dict[str, Any]) -> list[str]:
    return _section_key_paths(policy)


def _section_key_paths(policy: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for section in ("video", "audio", "subtitle"):
        raw_section = _policy_section_with_implicit_keys(section, policy.get(section))
        if not isinstance(raw_section, dict):
            continue
        for key in raw_section:
            paths.append(f"{section}.{key}")
    return paths


def policy_shape_example(policy: dict[str, Any]) -> dict[str, Any]:
    shape: dict[str, Any] = {}
    for section in ("video", "audio", "subtitle"):
        raw_section = _policy_section_with_implicit_keys(section, policy.get(section))
        if not isinstance(raw_section, dict):
            continue
        shape[section] = {key: None for key in raw_section}
    if not shape:
        shape = {"video": {}, "audio": {}, "subtitle": {}}
    return shape


def compact_policy_payload(policy: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(policy, dict):
        return None
    compacted: dict[str, Any] = {}
    for section in ("video", "audio", "subtitle"):
        raw_section = policy.get(section)
        if not isinstance(raw_section, dict):
            continue
        cleaned = {key: value for key, value in raw_section.items() if value is not None}
        if cleaned:
            compacted[section] = cleaned
    return compacted


def advisor_protected_policy_paths(policy: dict[str, Any] | None) -> list[str]:
    video = object_dict(object_dict(policy).get("video"))
    return sorted(
        f"video.{key}"
        for key in ADVISOR_PROTECTED_VIDEO_KEYS
        if key in video and video.get(key) is not None
    )


def merge_policy_fragments(*fragments: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for fragment in fragments:
        for section, values in object_dict(fragment).items():
            normalized_values = object_dict(values)
            if not normalized_values:
                continue
            merged.setdefault(section, {}).update(normalized_values)
    return merged


def policy_response_schema(policy: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for section in ("video", "audio", "subtitle"):
        raw_section = _policy_section_with_implicit_keys(section, policy.get(section))
        if not isinstance(raw_section, dict):
            continue
        properties[section] = policy_section_schema(raw_section)
        required.append(section)
    if not properties:
        properties = {
            "video": {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
            "audio": {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
            "subtitle": {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
        }
        required = ["video", "audio", "subtitle"]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def policy_section_schema(section_policy: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for key, value in section_policy.items():
        properties[key] = policy_value_schema(value)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def policy_value_schema(value: JSONValue) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"type": ["boolean", "null"]}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"type": ["number", "null"]}
    if isinstance(value, list):
        item_schema: dict[str, Any] = {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]}
        if value:
            if all(isinstance(item, bool) for item in value):
                item_schema = {"type": "boolean"}
            elif all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
                item_schema = {"type": "number"}
            elif all(isinstance(item, str) for item in value):
                item_schema = {"type": "string"}
        return {"type": ["array", "null"], "items": item_schema}
    return {"type": ["string", "null"]}


def normalize_policy_section(section: str, raw: JSONValue, base_section: JSONObject, *, mode: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    updates: dict[str, Any] = {}
    normalized_base_section = _policy_section_with_implicit_keys(section, base_section, include_protected=True)
    if not isinstance(normalized_base_section, dict):
        return {}
    for key, base_value in normalized_base_section.items():
        if key not in raw or raw.get(key) is None:
            continue
        normalized = normalize_policy_value(section, key, raw[key], base_value, mode=mode)
        if normalized is not _SKIP_POLICY_VALUE:
            updates[key] = normalized
    if section == "video":
        updates = finalize_video_policy_updates(updates, base_section)
    return updates


def _policy_section_with_implicit_keys(
        section: str,
        raw_section: JSONValue,
        *,
        include_protected: bool = False,
) -> dict[str, JSONValue] | JSONValue:
    if not isinstance(raw_section, dict):
        return raw_section
    if section != "video":
        return raw_section
    merged = {**_VIDEO_TRANSFORM_DEFAULTS, **raw_section}
    if include_protected:
        return merged
    return {key: value for key, value in merged.items() if key not in ADVISOR_PROTECTED_VIDEO_KEYS}


def normalize_policy_value(section: str, key: str, value: JSONValue, base_value: JSONValue, *, mode: str) -> JSONValue | object:
    if section == "video":
        return normalize_video_policy_value(key, value, base_value, mode=mode)
    if section == "audio":
        return normalize_audio_policy_value(key, value, base_value)
    if section == "subtitle":
        return normalize_subtitle_policy_value(key, value, base_value)
    return _SKIP_POLICY_VALUE


def normalize_video_policy_value(key: str, value: JSONValue, base_value: JSONValue, *, mode: str) -> JSONValue | object:
    _ = mode
    if key == "quality_metric":
        metric = str(value).strip().lower()
        if metric in {"auto", "vmaf", "xpsnr", "ssim", "psnr"}:
            return metric
        return _SKIP_POLICY_VALUE
    if key in {"encoder", "pixel_format"}:
        trimmed = str(value).strip()
        return trimmed or _SKIP_POLICY_VALUE
    if key == "preset":
        return clamp_int(value, minimum=0, maximum=13)
    if key == "crf_search":
        return coerce_bool(value, fallback=bool(base_value))
    if key == "target_vmaf":
        return round(clamp_float(value, minimum=70.0, maximum=98.0), 2)
    if key == "min_target_vmaf":
        return round(clamp_float(value, minimum=65.0, maximum=97.0), 2)
    if key == "target_xpsnr":
        return round(clamp_float(value, minimum=25.0, maximum=41.0), 2)
    if key == "min_target_xpsnr":
        return round(clamp_float(value, minimum=20.0, maximum=40.0), 2)
    if key in {"target_relax_step_vmaf", "target_relax_step_xpsnr"}:
        return round(clamp_float(value, minimum=0.1, maximum=5.0), 2)
    if key in {"sample_every", "sample_duration"}:
        return normalize_duration_like(value, fallback=str(base_value or ""))
    if key in {"min_crf", "max_crf", "target_search_max_crf"}:
        return clamp_int(value, minimum=0, maximum=63)
    if key == "max_encoded_percent":
        return clamp_int(value, minimum=1, maximum=100)
    if key == "target_size_bytes":
        return max(1, int_value(value))
    if key in {"target_size_mb", "target_runtime_minutes"}:
        return round(max(float_value(value), 0.001), 3)
    if key == "max_height":
        return clamp_int(value, minimum=0, maximum=4320)
    if key in {"black_bar_detect_samples"}:
        return clamp_int(value, minimum=1, maximum=5)
    if key in {"black_bar_detect_seconds", "black_bar_detect_limit"}:
        return clamp_int(value, minimum=1, maximum=120)
    if key == "black_bar_detect_round":
        return clamp_int(value, minimum=2, maximum=64)
    if key == "black_bar_detect_start_seconds":
        return max(0, clamp_int(value, minimum=0, maximum=24 * 60 * 60))
    if key == "black_bar_handling":
        handling = str(value).strip().lower()
        if handling in {"off", "auto", "smart"}:
            return handling
        return _SKIP_POLICY_VALUE
    if key == "downsample_algorithm":
        algorithm = str(value).strip().lower()
        if algorithm in {"fast_bilinear", "bilinear", "bicubic", "area", "lanczos", "spline", "neighbor"}:
            return algorithm
        return _SKIP_POLICY_VALUE
    if key == "crop":
        crop = str(value).strip()
        if not crop:
            return ""
        if re.fullmatch(r"\d+:\d+:\d+:\d+", crop):
            return crop
        return _SKIP_POLICY_VALUE
    if key == "default_grain":
        return clamp_int(value, minimum=0, maximum=50)
    if key == "grain_denoise":
        return clamp_int(value, minimum=0, maximum=1)
    if key == "thorough":
        return coerce_bool(value, fallback=bool(base_value))
    return normalize_like_base(value, base_value)


def normalize_audio_policy_value(key: str, value: JSONValue, base_value: JSONValue) -> JSONValue | object:
    if key in {"keep_languages", "copy_codecs", "convert_to_opus_codecs"}:
        return normalize_string_list(value)
    if key == "stereo_opus_bitrate":
        bitrate = parse_bitrate_kbps(value, fallback=base_value)
        return f"{clamp_int(bitrate, minimum=48, maximum=384)}k"
    if key == "surround_5_1_opus_bitrate":
        bitrate = parse_bitrate_kbps(value, fallback=base_value)
        return f"{clamp_int(bitrate, minimum=96, maximum=512)}k"
    if key == "surround_7_1_opus_bitrate":
        bitrate = parse_bitrate_kbps(value, fallback=base_value)
        return f"{clamp_int(bitrate, minimum=128, maximum=768)}k"
    return normalize_like_base(value, base_value)


def normalize_subtitle_policy_value(key: str, value: JSONValue, base_value: JSONValue) -> JSONValue | object:
    if key == "keep_languages":
        return normalize_string_list(value)
    if key in {"prefer_text", "keep_forced"}:
        return coerce_bool(value, fallback=bool(base_value))
    if key == "default_mode":
        trimmed = str(value).strip()
        return trimmed or _SKIP_POLICY_VALUE
    return normalize_like_base(value, base_value)


def normalize_like_base(value: JSONValue, base_value: JSONValue) -> JSONValue | object:
    if isinstance(base_value, bool):
        return coerce_bool(value, fallback=base_value)
    if isinstance(base_value, int) and not isinstance(base_value, bool):
        return clamp_int(value, minimum=0, maximum=max(int_value(base_value), 1_000_000))
    if isinstance(base_value, float):
        return round(clamp_float(value, minimum=float("-inf"), maximum=float("inf")), 2)
    if isinstance(base_value, list):
        if all(isinstance(item, str) for item in base_value):
            return normalize_string_list(value)
        if all(isinstance(item, bool) for item in base_value):
            if not isinstance(value, list):
                return []
            return [coerce_bool(item, fallback=False) for item in value]
        if all(isinstance(item, (int, float)) for item in base_value):
            if not isinstance(value, list):
                return []
            return [clamp_float(item, minimum=float("-inf"), maximum=float("inf")) for item in value]
    trimmed = str(value).strip()
    return trimmed or _SKIP_POLICY_VALUE


def finalize_video_policy_updates(updates: dict[str, Any], base_video: dict[str, Any]) -> dict[str, Any]:
    finalized = dict(updates)
    if "target_vmaf" in finalized or "min_target_vmaf" in finalized:
        target = float_value(finalized.get("target_vmaf", base_video.get("target_vmaf", 95.0)))
        floor = float_value(finalized.get("min_target_vmaf", base_video.get("min_target_vmaf", target)))
        finalized["target_vmaf"] = round(clamp_float(target, minimum=70.0, maximum=98.0), 2)
        finalized["min_target_vmaf"] = round(min(clamp_float(floor, minimum=65.0, maximum=97.0), finalized["target_vmaf"]), 2)
    if "target_xpsnr" in finalized or "min_target_xpsnr" in finalized:
        target = float_value(finalized.get("target_xpsnr", base_video.get("target_xpsnr", 41.0)))
        floor = float_value(finalized.get("min_target_xpsnr", base_video.get("min_target_xpsnr", target)))
        finalized["target_xpsnr"] = round(clamp_float(target, minimum=25.0, maximum=41.0), 2)
        finalized["min_target_xpsnr"] = round(min(clamp_float(floor, minimum=20.0, maximum=40.0), finalized["target_xpsnr"]), 2)
    if "min_crf" in finalized or "max_crf" in finalized or "target_search_max_crf" in finalized:
        min_crf = int_value(finalized.get("min_crf", base_video.get("min_crf", 18)))
        max_crf = int_value(finalized.get("max_crf", base_video.get("max_crf", 38)))
        target_search_max_crf = int_value(
            finalized.get("target_search_max_crf", base_video.get("target_search_max_crf", max_crf))
        )
        finalized["min_crf"] = min(min_crf, max_crf)
        finalized["max_crf"] = max(min_crf, max_crf)
        finalized["target_search_max_crf"] = max(finalized["max_crf"], target_search_max_crf)
    return finalized


def normalize_duration_like(value: JSONValue, *, fallback: str) -> str | object:
    trimmed = str(value).strip().lower()
    if not trimmed:
        return fallback or _SKIP_POLICY_VALUE
    if any(unit in trimmed for unit in ("ms", "s", "m", "h")) and any(char.isdigit() for char in trimmed):
        return trimmed
    return fallback or _SKIP_POLICY_VALUE


def normalize_string_list(value: JSONValue) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        trimmed = str(item).strip().lower()
        if not trimmed or trimmed in seen:
            continue
        seen.add(trimmed)
        normalized.append(trimmed)
    return normalized


def coerce_bool(value: JSONValue, *, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    trimmed = str(value).strip().lower()
    if trimmed in {"1", "true", "yes", "on"}:
        return True
    if trimmed in {"0", "false", "no", "off"}:
        return False
    return fallback


def parse_bitrate_kbps(value: JSONValue, *, fallback: JSONValue) -> int:
    if value is None:
        value = fallback
    stripped = str(value).strip().lower().removesuffix("kbps").removesuffix("k")
    try:
        return int(float(stripped))
    except ValueError:
        fallback_str = str(fallback).strip().lower().removesuffix("kbps").removesuffix("k")
        try:
            return int(float(fallback_str))
        except ValueError:
            return 224


def clamp_float(value: JSONValue, *, minimum: float, maximum: float) -> float:
    if not isinstance(value, (str, int, float, bool)):
        return minimum
    number = float_value(value)
    return max(min(number, maximum), minimum)


def clamp_int(value: JSONValue, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, (str, int, float, bool)):
        return minimum
    number = int(round(float_value(value)))
    return max(min(number, maximum), minimum)
