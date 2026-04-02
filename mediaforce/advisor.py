import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.core.type_defs import JSONObject, JSONValue, object_dict, object_list
from mediaforce.advising.policy import compact_policy_payload as _compact_policy_payload_impl, \
    extract_seed_payload as _extract_seed_payload_impl, normalize_policy_section as _normalize_policy_section_impl, \
    policy_key_paths as _policy_key_paths_impl, policy_response_schema as _policy_response_schema_impl, \
    policy_section_schema as _policy_section_schema_impl, policy_shape_example as _policy_shape_example_impl, \
    policy_value_schema as _policy_value_schema_impl, run_verdict_schema as _run_verdict_schema_impl, \
    seed_response_schema as _seed_response_schema_impl, try_load_first_json_object as _try_load_first_json_object_impl, \
    try_load_json as _try_load_json_impl, tune_response_schema as _tune_response_schema_impl, \
    tune_self_check_schema as _tune_self_check_schema_impl, normalize_policy_value as _normalize_policy_value_impl, \
    normalize_video_policy_value as _normalize_video_policy_value_impl, \
    normalize_audio_policy_value as _normalize_audio_policy_value_impl, \
    normalize_subtitle_policy_value as _normalize_subtitle_policy_value_impl, \
    normalize_like_base as _normalize_like_base_impl, finalize_video_policy_updates as _finalize_video_policy_updates_impl, \
    normalize_duration_like as _normalize_duration_like_impl, normalize_string_list as _normalize_string_list_impl, \
    coerce_bool as _coerce_bool_impl, parse_bitrate_kbps as _parse_bitrate_kbps_impl, \
    clamp_float as _clamp_float_impl, clamp_int as _clamp_int_impl
from mediaforce.advising.prompts import build_prompt as _build_prompt_impl, \
    build_run_verdict_prompt as _build_run_verdict_prompt_impl, build_seed_prompt as _build_seed_prompt_impl, \
    build_tune_prompt as _build_tune_prompt_impl
from mediaforce.advising.runtime import run_code_prompt as _run_code_prompt_impl, \
    run_multimodal_tune_request as _run_multimodal_tune_request_impl, \
    run_structured_llm_request as _run_structured_llm_request_impl

ADVISOR_MODEL = "gpt-5.4"
SEED_PROMPT_VERSION = "seed-v4"
TUNE_PROMPT_VERSION = "tune-v5"
TUNE_SELF_CHECK_VERSION = "tune-self-check-v1"
RUN_VERDICT_PROMPT_VERSION = "run-verdict-v1"
REQUEST_DISPOSITIONS = ("honored", "honored_with_risk", "softened", "rejected", "unclear")


def _memory_disabled_code_args() -> list[str]:
    return [
        "-c",
        "features.memories=false",
        "-c",
        "memories.use_memories=false",
        "-c",
        "memories.generate_memories=false",
    ]


@dataclass(slots=True)
class AdvisorResponse:
    ok: bool
    summary: str
    raw: str


@dataclass(slots=True)
class SeedPolicyResponse:
    ok: bool
    summary: str
    raw: str
    prompt_version: str
    diagnosis: str
    confidence: str
    evidence_checked: list[str]
    suggested_follow_up: str | None
    request_disposition: str
    request_response: str
    feasibility_note: str | None
    proposed_policy: dict[str, Any] | None


@dataclass(slots=True)
class TuningPolicyResponse:
    ok: bool
    summary: str
    raw: str
    prompt_version: str
    diagnosis: str
    confidence: str
    evidence_checked: list[str]
    suggested_follow_up: str | None
    request_disposition: str
    request_response: str
    feasibility_note: str | None
    proposed_policy: dict[str, Any] | None
    toolbelt_used: list[str]
    self_check: dict[str, Any] | None


@dataclass(slots=True)
class RunVerdictResponse:
    ok: bool
    summary: str
    raw: str
    prompt_version: str
    outcome: str
    confidence: str
    next_step: str | None
    evidence_checked: list[str]


def request_tuning_advice(*, project_root: Path, payload: dict[str, Any]) -> AdvisorResponse:
    prompt = _build_prompt(payload)
    return _run_code_prompt(project_root=project_root, prompt=prompt, max_seconds=60)


def request_seed_policy(*, project_root: Path, payload: dict[str, Any]) -> SeedPolicyResponse:
    prompt = _build_seed_prompt(payload)
    base_policy = object_dict(payload.get("base_policy"))
    parsed = _run_structured_llm_request(
        project_root=project_root,
        developer=(
            "You are a media encode seed worker. No interactive tools are available in this path. "
            "Use only the provided runtime context and return JSON that satisfies the schema exactly."
        ),
        message=prompt,
        schema=_seed_response_schema(base_policy),
        max_seconds=75,
    )
    if not isinstance(parsed, dict):
        return SeedPolicyResponse(
            ok=False,
            summary="The seed worker did not return valid structured JSON.",
            raw=json.dumps(parsed) if parsed is not None else "",
            prompt_version=SEED_PROMPT_VERSION,
            diagnosis="The seed worker did not complete cleanly.",
            confidence="low",
            evidence_checked=[],
            suggested_follow_up="Ask again with a concrete experiment or artifact concern.",
            request_disposition="unclear",
            request_response="I could not turn that note into a trustworthy first draft.",
            feasibility_note=None,
            proposed_policy=None,
        )
    proposed_policy = object_dict(parsed.get("policy")) or None
    proposed_policy = _compact_policy_payload(proposed_policy)
    if proposed_policy is not None and base_policy:
        _, proposed_policy = apply_seed_policy(base_policy, proposed_policy)
    evidence_checked_raw = parsed.get("evidence_checked")
    evidence_checked = evidence_checked_raw if isinstance(evidence_checked_raw, list) else []
    summary = str(parsed.get("summary") or "No summary returned.")
    request_disposition = str(parsed.get("request_disposition") or "unclear")
    if request_disposition not in REQUEST_DISPOSITIONS:
        request_disposition = "unclear"
    request_response = str(parsed.get("request_response") or summary)
    if proposed_policy is None:
        return SeedPolicyResponse(
            ok=False,
            summary="Seed policy response did not include a valid policy object.",
            raw=json.dumps(parsed, indent=2, sort_keys=True),
            prompt_version=SEED_PROMPT_VERSION,
            diagnosis=str(parsed.get("diagnosis") or "No diagnosis returned."),
            confidence=str(parsed.get("confidence") or "unknown"),
            evidence_checked=[str(item) for item in evidence_checked],
            suggested_follow_up=(str(parsed.get("suggested_follow_up")) if parsed.get("suggested_follow_up") else None),
            request_disposition=request_disposition,
            request_response=request_response,
            feasibility_note=(str(parsed.get("feasibility_note")) if parsed.get("feasibility_note") else None),
            proposed_policy=None,
        )
    return SeedPolicyResponse(
        ok=True,
        summary=summary,
        raw=json.dumps(parsed, indent=2, sort_keys=True),
        prompt_version=SEED_PROMPT_VERSION,
        diagnosis=str(parsed.get("diagnosis") or "No diagnosis returned."),
        confidence=str(parsed.get("confidence") or "unknown"),
        evidence_checked=[str(item) for item in evidence_checked],
        suggested_follow_up=(str(parsed.get("suggested_follow_up")) if parsed.get("suggested_follow_up") else None),
        request_disposition=request_disposition,
        request_response=request_response,
        feasibility_note=(str(parsed.get("feasibility_note")) if parsed.get("feasibility_note") else None),
        proposed_policy=proposed_policy,
    )


def request_note_tuning(*, project_root: Path, payload: dict[str, Any]) -> TuningPolicyResponse:
    prompt = _build_tune_prompt(payload)
    current_policy = object_dict(payload.get("policy"))
    review_pack = object_dict(payload.get("multimodal_review_pack")) or None
    review_images = [
        str(path)
        for path in object_list(object_dict(review_pack).get("images"))
        if str(path).strip()
    ]
    if review_images:
        parsed = _run_multimodal_tune_request(
            project_root=project_root,
            developer=(
                "You are a media encode tuning worker. Return JSON only. "
                "Use the attached review images plus the provided runtime context. "
                "Do not claim to inspect audio or video beyond the attached artifacts and supplied metadata."
            ),
            message=prompt,
            images=review_images,
            max_seconds=90,
        )
    else:
        parsed = _run_structured_llm_request(
            project_root=project_root,
            developer=(
                "You are a media encode tuning worker. No interactive tools are available in this path. "
                "Use only the provided runtime context and return JSON that satisfies the schema exactly."
            ),
            message=prompt,
            schema=_tune_response_schema(current_policy),
            max_seconds=90,
        )
    if not isinstance(parsed, dict):
        return TuningPolicyResponse(
            ok=False,
            summary="The tuning worker did not return valid structured JSON.",
            raw=json.dumps(parsed) if parsed is not None else "",
            prompt_version=TUNE_PROMPT_VERSION,
            diagnosis="The tuning worker did not complete cleanly.",
            confidence="low",
            evidence_checked=[],
            suggested_follow_up="Try again with a more specific note about the artifact you see.",
            request_disposition="unclear",
            request_response="I could not turn that note into a trustworthy next draft.",
            feasibility_note=None,
            proposed_policy=None,
            toolbelt_used=sorted(object_dict(payload.get("runtime_toolbelt")).keys()),
            self_check=None,
        )
    proposed_policy = object_dict(parsed.get("policy")) or None
    proposed_policy = _compact_policy_payload(proposed_policy)
    if proposed_policy is not None and current_policy:
        _, proposed_policy = apply_seed_policy(current_policy, proposed_policy, mode="tune")
    evidence_checked_raw = parsed.get("evidence_checked")
    evidence_checked = evidence_checked_raw if isinstance(evidence_checked_raw, list) else []
    self_check = _run_tune_self_check(
        project_root=project_root,
        tuning_context=payload,
        proposal={**parsed, "policy": proposed_policy},
    )
    self_check_status = str(object_dict(self_check).get("status") or "pass")
    if self_check_status == "fail":
        proposed_policy = None
        if not parsed.get("suggested_follow_up"):
            parsed["suggested_follow_up"] = str(
                object_dict(self_check).get("summary") or "Try a more specific note."
            )
    request_disposition = str(parsed.get("request_disposition") or "unclear")
    if request_disposition not in REQUEST_DISPOSITIONS:
        request_disposition = "unclear"
    request_response = str(parsed.get("request_response") or parsed.get("summary") or "No direct response returned.")
    return TuningPolicyResponse(
        ok=proposed_policy is not None,
        summary=str(parsed.get("summary") or "No summary returned."),
        raw=json.dumps(parsed, indent=2, sort_keys=True),
        prompt_version=TUNE_PROMPT_VERSION,
        diagnosis=str(parsed.get("diagnosis") or "No diagnosis returned."),
        confidence=str(parsed.get("confidence") or "unknown"),
        evidence_checked=[str(item) for item in evidence_checked],
        suggested_follow_up=(str(parsed.get("suggested_follow_up")) if parsed.get("suggested_follow_up") else None),
        request_disposition=request_disposition,
        request_response=request_response,
        feasibility_note=(str(parsed.get("feasibility_note")) if parsed.get("feasibility_note") else None),
        proposed_policy=proposed_policy,
        toolbelt_used=sorted(object_dict(payload.get("runtime_toolbelt")).keys()),
        self_check=self_check,
    )


def request_run_verdict(*, project_root: Path, payload: dict[str, Any]) -> RunVerdictResponse:
    parsed = _run_structured_llm_request(
        project_root=project_root,
        developer=(
            "You are a media encode calibration reviewer. No tools are available. "
            "Use only the provided measured result context and return concise JSON that satisfies the schema exactly."
        ),
        message=_build_run_verdict_prompt(payload),
        schema=_run_verdict_schema(),
        max_seconds=60,
    )
    if not isinstance(parsed, dict):
        return RunVerdictResponse(
            ok=False,
            summary="Measured calibration finished, but no model verdict was returned.",
            raw=json.dumps(parsed) if parsed is not None else "",
            prompt_version=RUN_VERDICT_PROMPT_VERSION,
            outcome="unknown",
            confidence="low",
            next_step=None,
            evidence_checked=[],
        )
    evidence_checked_raw = parsed.get("evidence_checked")
    evidence_checked = evidence_checked_raw if isinstance(evidence_checked_raw, list) else []
    return RunVerdictResponse(
        ok=True,
        summary=str(parsed.get("summary") or "Measured calibration finished."),
        raw=json.dumps(parsed, indent=2, sort_keys=True),
        prompt_version=RUN_VERDICT_PROMPT_VERSION,
        outcome=str(parsed.get("outcome") or "unknown"),
        confidence=str(parsed.get("confidence") or "unknown"),
        next_step=(str(parsed.get("next_step")) if parsed.get("next_step") else None),
        evidence_checked=[str(item) for item in evidence_checked],
    )


def apply_seed_policy(
        base_policy: dict[str, Any],
        proposed_policy: dict[str, Any],
        *,
        mode: str = "seed",
) -> tuple[dict[str, Any], dict[str, Any]]:
    applied_policy = json.loads(json.dumps(base_policy))
    applied_fragment: dict[str, Any] = {}

    for section in ("video", "audio", "subtitle"):
        base_section = applied_policy.get(section) or {}
        updates = _normalize_policy_section(
            section,
            proposed_policy.get(section),
            base_section if isinstance(base_section, dict) else {},
            mode=mode,
        )
        if updates:
            applied_policy.setdefault(section, {}).update(updates)
            applied_fragment[section] = updates

    return applied_policy, applied_fragment


def _run_code_prompt(*, project_root: Path, prompt: str, max_seconds: int) -> AdvisorResponse:
    return _run_code_prompt_impl(
        project_root=project_root,
        prompt=prompt,
        max_seconds=max_seconds,
        advisor_model=ADVISOR_MODEL,
        memory_disabled_code_args=_memory_disabled_code_args,
        advisor_response_factory=AdvisorResponse,
        subprocess_run=subprocess.run,
    )


def _run_structured_llm_request(
        *,
        project_root: Path,
        developer: str,
        message: str,
        schema: dict[str, Any],
        max_seconds: int,
) -> dict[str, Any] | None:
    return _run_structured_llm_request_impl(
        project_root=project_root,
        developer=developer,
        message=message,
        schema=schema,
        max_seconds=max_seconds,
        advisor_model=ADVISOR_MODEL,
        memory_disabled_code_args=_memory_disabled_code_args,
        subprocess_run=subprocess.run,
        try_load_json=_try_load_json,
        try_load_first_json_object=_try_load_first_json_object,
    )


def _run_multimodal_tune_request(
        *,
        project_root: Path,
        developer: str,
        message: str,
        images: list[str],
        max_seconds: int,
) -> dict[str, Any] | None:
    return _run_multimodal_tune_request_impl(
        project_root=project_root,
        developer=developer,
        message=message,
        images=images,
        max_seconds=max_seconds,
        advisor_model=ADVISOR_MODEL,
        memory_disabled_code_args=_memory_disabled_code_args,
        subprocess_run=subprocess.run,
        try_load_json=_try_load_json,
        try_load_first_json_object=_try_load_first_json_object,
    )


def _build_prompt(payload: dict[str, Any]) -> str:
    return _build_prompt_impl(payload)


def _build_seed_prompt(payload: dict[str, Any]) -> str:
    return _build_seed_prompt_impl(
        payload,
        policy_key_paths=_policy_key_paths,
        policy_shape_example=_policy_shape_example,
    )


def _build_tune_prompt(payload: dict[str, Any]) -> str:
    return _build_tune_prompt_impl(
        payload,
        policy_key_paths=_policy_key_paths,
        policy_shape_example=_policy_shape_example,
    )


def _build_run_verdict_prompt(payload: dict[str, Any]) -> str:
    return _build_run_verdict_prompt_impl(payload)


def _run_tune_self_check(*, project_root: Path, tuning_context: dict[str, Any], proposal: dict[str, Any]) -> dict[
                                                                                                                 str, Any] | None:
    payload = {
        "context": tuning_context,
        "proposal": proposal,
    }
    message = (
        "Perform one fast self-check on the proposed next calibration draft. "
        "Use only the provided context and proposal. Return whether the draft is acceptable, needs caution, or should be rejected.\n\n"
        f"{json.dumps(payload, indent=2, sort_keys=True)}"
    )
    return _run_structured_llm_request(
        project_root=project_root,
        developer=(
            "You are validating a media encode tuning proposal. No tools are available. "
            "Return JSON that satisfies the schema exactly and keep the summary concise."
        ),
        message=message,
        schema=_tune_self_check_schema(),
        max_seconds=45,
    )


def _tune_response_schema(current_policy: dict[str, Any]) -> dict[str, Any]:
    return _tune_response_schema_impl(current_policy, request_dispositions=REQUEST_DISPOSITIONS)


def _seed_response_schema(base_policy: dict[str, Any]) -> dict[str, Any]:
    return _seed_response_schema_impl(base_policy, request_dispositions=REQUEST_DISPOSITIONS)


def _tune_self_check_schema() -> dict[str, Any]:
    return _tune_self_check_schema_impl()


def _run_verdict_schema() -> dict[str, Any]:
    return _run_verdict_schema_impl()


def _extract_seed_payload(raw: str) -> JSONObject:
    return _extract_seed_payload_impl(raw)


def _try_load_json(raw: str) -> JSONValue:
    return _try_load_json_impl(raw)


def _try_load_first_json_object(raw: str) -> JSONValue:
    return _try_load_first_json_object_impl(raw)


def _policy_key_paths(policy: dict[str, Any]) -> list[str]:
    return _policy_key_paths_impl(policy)


def _policy_shape_example(policy: dict[str, Any]) -> dict[str, Any]:
    return _policy_shape_example_impl(policy)


def _compact_policy_payload(policy: dict[str, Any] | None) -> dict[str, Any] | None:
    return _compact_policy_payload_impl(policy)


def _policy_response_schema(policy: dict[str, Any]) -> dict[str, Any]:
    return _policy_response_schema_impl(policy)


def _policy_section_schema(section_policy: dict[str, Any]) -> dict[str, Any]:
    return _policy_section_schema_impl(section_policy)


def _policy_value_schema(value: JSONValue) -> dict[str, Any]:
    return _policy_value_schema_impl(value)


def _normalize_policy_section(
        section: str, raw: JSONValue, base_section: JSONObject, *, mode: str
) -> dict[str, Any]:
    return _normalize_policy_section_impl(section, raw, base_section, mode=mode)


def _normalize_policy_value(
        section: str, key: str, value: JSONValue, base_value: JSONValue, *, mode: str
) -> JSONValue | object:
    return _normalize_policy_value_impl(section, key, value, base_value, mode=mode)


def _normalize_video_policy_value(
        key: str, value: JSONValue, base_value: JSONValue, *, mode: str
) -> JSONValue | object:
    return _normalize_video_policy_value_impl(key, value, base_value, mode=mode)


def _normalize_audio_policy_value(key: str, value: JSONValue, base_value: JSONValue) -> JSONValue | object:
    return _normalize_audio_policy_value_impl(key, value, base_value)


def _normalize_subtitle_policy_value(
        key: str, value: JSONValue, base_value: JSONValue
) -> JSONValue | object:
    return _normalize_subtitle_policy_value_impl(key, value, base_value)


def _normalize_like_base(value: JSONValue, base_value: JSONValue) -> JSONValue | object:
    return _normalize_like_base_impl(value, base_value)


def _finalize_video_policy_updates(updates: dict[str, Any], base_video: dict[str, Any]) -> dict[str, Any]:
    return _finalize_video_policy_updates_impl(updates, base_video)


def _normalize_duration_like(value: JSONValue, *, fallback: str) -> str | object:
    return _normalize_duration_like_impl(value, fallback=fallback)


def _normalize_string_list(value: JSONValue) -> list[str]:
    return _normalize_string_list_impl(value)


def _coerce_bool(value: JSONValue, *, fallback: bool) -> bool:
    return _coerce_bool_impl(value, fallback=fallback)


def _parse_bitrate_kbps(value: JSONValue, *, fallback: JSONValue) -> int:
    return _parse_bitrate_kbps_impl(value, fallback=fallback)


def _clamp_float(value: JSONValue, *, minimum: float, maximum: float) -> float:
    return _clamp_float_impl(value, minimum=minimum, maximum=maximum)


def _clamp_int(value: JSONValue, *, minimum: int, maximum: int) -> int:
    return _clamp_int_impl(value, minimum=minimum, maximum=maximum)
