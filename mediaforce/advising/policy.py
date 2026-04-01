import json
from typing import Any

from mediaforce.core.type_defs import JSONObject, JSONValue

_SKIP_POLICY_VALUE = object()


def tune_response_schema(current_policy: dict[str, Any], *, request_dispositions: tuple[str, ...]) -> dict[str, Any]:
    policy_schema = policy_response_schema(current_policy)
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


def seed_response_schema(base_policy: dict[str, Any], *, request_dispositions: tuple[str, ...]) -> dict[str, Any]:
    policy_schema = policy_response_schema(base_policy)
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


def tune_self_check_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "summary", "issues"],
        "properties": {
            "status": {"type": "string", "enum": ["pass", "warn", "fail"]},
            "summary": {"type": "string"},
            "issues": {"type": "array", "items": {"type": "string"}},
        },
    }


def run_verdict_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "outcome", "confidence", "next_step", "evidence_checked"],
        "properties": {
            "summary": {"type": "string"},
            "outcome": {
                "type": "string",
                "enum": ["strong_match", "acceptable_experiment", "needs_review", "poor_fit"],
            },
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "next_step": {"type": ["string", "null"]},
            "evidence_checked": {"type": "array", "items": {"type": "string"}},
        },
    }


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


def try_load_first_json_object(raw: str) -> JSONValue:
    decoder = json.JSONDecoder()
    candidate = raw.lstrip()
    while candidate:
        try:
            parsed, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            next_start = candidate.find("{", 1)
            if next_start == -1:
                return None
            candidate = candidate[next_start:]
            continue
        return parsed
    return None


def policy_key_paths(policy: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for section in ("video", "audio", "subtitle"):
        raw_section = policy.get(section)
        if not isinstance(raw_section, dict):
            continue
        for key in raw_section:
            paths.append(f"{section}.{key}")
    return paths


def policy_shape_example(policy: dict[str, Any]) -> dict[str, Any]:
    shape: dict[str, Any] = {}
    for section in ("video", "audio", "subtitle"):
        raw_section = policy.get(section)
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


def policy_response_schema(policy: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for section in ("video", "audio", "subtitle"):
        raw_section = policy.get(section)
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
    for key, base_value in base_section.items():
        if key not in raw or raw.get(key) is None:
            continue
        normalized = normalize_policy_value(section, key, raw[key], base_value, mode=mode)
        if normalized is not _SKIP_POLICY_VALUE:
            updates[key] = normalized
    if section == "video":
        updates = finalize_video_policy_updates(updates, base_section)
    return updates


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
    if key in {"min_crf", "max_crf"}:
        return clamp_int(value, minimum=0, maximum=63)
    if key == "max_encoded_percent":
        return clamp_int(value, minimum=10, maximum=100)
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
        return clamp_int(value, minimum=0, maximum=max(int(base_value), 1_000_000))
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
        target = float(finalized.get("target_vmaf", base_video.get("target_vmaf", 95.0)))
        floor = float(finalized.get("min_target_vmaf", base_video.get("min_target_vmaf", target)))
        finalized["target_vmaf"] = round(clamp_float(target, minimum=70.0, maximum=98.0), 2)
        finalized["min_target_vmaf"] = round(min(clamp_float(floor, minimum=65.0, maximum=97.0), finalized["target_vmaf"]), 2)
    if "target_xpsnr" in finalized or "min_target_xpsnr" in finalized:
        target = float(finalized.get("target_xpsnr", base_video.get("target_xpsnr", 41.0)))
        floor = float(finalized.get("min_target_xpsnr", base_video.get("min_target_xpsnr", target)))
        finalized["target_xpsnr"] = round(clamp_float(target, minimum=25.0, maximum=41.0), 2)
        finalized["min_target_xpsnr"] = round(min(clamp_float(floor, minimum=20.0, maximum=40.0), finalized["target_xpsnr"]), 2)
    if "min_crf" in finalized or "max_crf" in finalized:
        min_crf = int(finalized.get("min_crf", base_video.get("min_crf", 18)))
        max_crf = int(finalized.get("max_crf", base_video.get("max_crf", 38)))
        finalized["min_crf"] = min(min_crf, max_crf)
        finalized["max_crf"] = max(min_crf, max_crf)
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
    try:
        number = float(value)
    except (TypeError, ValueError):
        return minimum
    return max(min(number, maximum), minimum)


def clamp_int(value: JSONValue, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, (str, int, float, bool)):
        return minimum
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return minimum
    return max(min(number, maximum), minimum)
