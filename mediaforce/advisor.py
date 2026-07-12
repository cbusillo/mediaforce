import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.core.type_defs import JSONObject, JSONValue, int_value, object_dict, object_list
from mediaforce.advising.policy import compact_policy_payload as _compact_policy_payload_impl, \
    extract_seed_payload as _extract_seed_payload_impl, normalize_policy_section as _normalize_policy_section_impl, \
    policy_key_paths as _policy_key_paths_impl, policy_response_schema as _policy_response_schema_impl, \
    policy_section_schema as _policy_section_schema_impl, policy_shape_example as _policy_shape_example_impl, \
    policy_value_schema as _policy_value_schema_impl, \
    seed_response_schema as _seed_response_schema_impl, try_load_json as _try_load_json_impl, \
    tune_response_schema as _tune_response_schema_impl, \
    review_artifact_critique_schema as _review_artifact_critique_schema_impl, normalize_policy_value as _normalize_policy_value_impl, \
    normalize_video_policy_value as _normalize_video_policy_value_impl, \
    normalize_audio_policy_value as _normalize_audio_policy_value_impl, \
    normalize_subtitle_policy_value as _normalize_subtitle_policy_value_impl, \
    normalize_like_base as _normalize_like_base_impl, finalize_video_policy_updates as _finalize_video_policy_updates_impl, \
    normalize_duration_like as _normalize_duration_like_impl, normalize_string_list as _normalize_string_list_impl, \
    coerce_bool as _coerce_bool_impl, merge_policy_fragments as _merge_policy_fragments_impl, \
    has_nonpositive_video_budget, \
    parse_bitrate_kbps as _parse_bitrate_kbps_impl, \
    clamp_float as _clamp_float_impl, clamp_int as _clamp_int_impl, \
    operator_note_parse_schema as _operator_note_parse_schema_impl
from mediaforce.advising.prompts import \
    build_review_artifact_critique_prompt as _build_review_artifact_critique_prompt_impl, build_seed_prompt as _build_seed_prompt_impl, \
    build_tune_prompt as _build_tune_prompt_impl, build_operator_note_parse_prompt as _build_operator_note_parse_prompt_impl
from mediaforce.advising.privacy import advisor_evidence_references
from mediaforce.advising.runtime import StructuredLLMFailure, \
    run_codex_lab_process as _run_codex_lab_process_impl, \
    run_multimodal_tune_request as _run_multimodal_tune_request_impl, \
    run_structured_llm_request as _run_structured_llm_request_impl
from mediaforce.advising.routing import AdvisorRouting, AdvisorTask

SEED_PROMPT_VERSION = "seed-v9"
TUNE_PROMPT_VERSION = "tune-v10"
TUNE_SELF_CHECK_VERSION = "tune-self-check-v2-deterministic"
RUN_VERDICT_PROMPT_VERSION = "run-verdict-v3-deterministic"
REVIEW_ARTIFACT_CRITIQUE_PROMPT_VERSION = "review-artifact-critique-v1"
OPERATOR_NOTE_PARSE_PROMPT_VERSION = "operator-note-parse-v2"
REQUEST_DISPOSITIONS = ("honored", "honored_with_risk", "softened", "rejected", "unclear")
_GUARDED_AUDIO_POLICY_KEYS = ("surround_7_1_opus_bitrate", "surround_5_1_opus_bitrate", "stereo_opus_bitrate")
_AUDIO_SPECIFIC_TOKENS = (
    " audio ",
    " surround ",
    " opus ",
    " stereo ",
    " 5.1 ",
    " 7.1 ",
    " aac ",
    " ac3 ",
    " eac3 ",
    " dts ",
    " truehd ",
    " flac ",
    " pcm ",
    " mp3 ",
    " atmos ",
    " commentary ",
    " dialog ",
    " dialogue ",
    " dub ",
    " dubbing ",
    " mono ",
    " mix ",
    " voices ",
)
_NON_AUDIO_SPECIFIC_TOKENS = (
    " video ",
    " vmaf ",
    " xpsnr ",
    " crf ",
    " preset ",
    " grain ",
    " budget ",
    " cap ",
    " size ",
    " encode ",
    " target ",
)


def _merge_policy_fragments(*fragments: dict[str, Any] | None) -> dict[str, Any]:
    return _merge_policy_fragments_impl(*fragments)


def _requested_experiment_policy(payload: dict[str, Any]) -> dict[str, Any]:
    return object_dict(object_dict(payload.get("requested_experiment")).get("applied_policy"))


def _should_carry_requested_experiment(request_disposition: str) -> bool:
    return request_disposition in {"honored", "honored_with_risk"}


def _should_force_repeated_experiment(repeat_signal: dict[str, Any] | None) -> bool:
    return int_value(object_dict(repeat_signal).get("repeat_count")) >= 2


def _has_blocking_request_evidence(
        requested_experiment: dict[str, Any] | None,
        latest_failed_sample_job: dict[str, Any] | None = None,
) -> bool:
    request = object_dict(requested_experiment)
    return (
        bool(object_dict(latest_failed_sample_job))
        or str(request.get("evidence_authority") or "none").strip().lower() == "rejected_visual_result"
        or has_nonpositive_video_budget(request)
    )


def _should_force_operator_confirmed_experiment(
        requested_experiment: dict[str, Any] | None,
        latest_failed_sample_job: dict[str, Any] | None = None,
) -> bool:
    request = object_dict(requested_experiment)
    return (
        bool(request.get("operator_confirmed"))
        and not _has_blocking_request_evidence(request, latest_failed_sample_job)
    )


def _reject_nonpositive_video_budget(
        parsed: dict[str, Any],
        requested_experiment: dict[str, Any] | None,
) -> bool:
    if not has_nonpositive_video_budget(requested_experiment):
        return False
    parsed["request_disposition"] = "rejected"
    parsed["summary"] = "The requested total size leaves no positive video budget."
    parsed["diagnosis"] = (
        "The estimated audio allocation consumes the requested total size, so there is no positive video budget "
        "for a safe measured sample."
    )
    parsed["request_response"] = (
        "That total size cannot queue as written because it leaves no positive video budget. "
        "Increase the total target or explicitly reduce the audio allocation first."
    )
    parsed["suggested_follow_up"] = "Increase the total size target or choose an explicit lower audio bitrate."
    parsed["feasibility_note"] = "The estimated video budget is non-positive."
    return True


def _raw_failure_payload(parsed: Any) -> str:
    if isinstance(parsed, StructuredLLMFailure):
        return parsed.raw
    return json.dumps(parsed) if parsed is not None else ""


def _force_operator_confirmed_experiment(
        *,
        current_policy: dict[str, Any],
        parsed: dict[str, Any],
        requested_experiment: dict[str, Any] | None,
        latest_failed_sample_job: dict[str, Any] | None,
        mode: str,
) -> dict[str, Any] | None:
    if not _should_force_operator_confirmed_experiment(requested_experiment, latest_failed_sample_job):
        return None
    request_disposition = str(parsed.get("request_disposition") or "").strip().lower()
    if request_disposition not in {"softened", "rejected"}:
        return None
    requested_policy = object_dict(object_dict(requested_experiment).get("applied_policy"))
    draft_label = "first sample" if mode == "seed" else "next sample"
    parsed["request_disposition"] = "honored"
    parsed["summary"] = f"Kept the explicit operator request as the {draft_label} draft."
    parsed["diagnosis"] = (
        "The operator gave a direct request, so this draft preserves it for measurement instead of replacing it "
        "with generic bitrate assumptions."
    )
    parsed["request_response"] = (
        f"You asked for this directly, so I kept it as the {draft_label}. The real sample and your review will decide."
    )
    parsed["suggested_follow_up"] = f"Measure this {draft_label} and decide from the clips."
    if not parsed.get("feasibility_note"):
        parsed["feasibility_note"] = "The operator-confirmed target is queueable for a safe measured sample."
    if not requested_policy:
        return {}
    _, applied_fragment = apply_seed_policy(current_policy, requested_policy, mode=mode)
    return applied_fragment


def _force_repeated_tune_experiment(
        *,
        current_policy: dict[str, Any],
        parsed: dict[str, Any],
        requested_experiment: dict[str, Any] | None,
        repeat_signal: dict[str, Any] | None,
        latest_failed_sample_job: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not _should_force_repeated_experiment(repeat_signal):
        return None
    if _has_blocking_request_evidence(requested_experiment, latest_failed_sample_job):
        return None
    request_disposition = str(parsed.get("request_disposition") or "").strip().lower()
    if request_disposition not in {"softened", "rejected"}:
        return None
    requested_policy = object_dict(object_dict(requested_experiment).get("applied_policy"))
    previous_softened = int_value(object_dict(repeat_signal).get("previous_softened_count"))
    parsed["request_disposition"] = "honored"
    parsed["summary"] = "Kept the repeated operator request as the next sample draft."
    parsed["diagnosis"] = (
        "The operator repeated the same explicit experiment after earlier softening, "
        "so this draft preserves the target for measurement instead of overriding it again."
    )
    parsed["request_response"] = (
        "You repeated the same request, so I kept it as the next sample. The measured result and your review decide."
    )
    if previous_softened:
        parsed["suggested_follow_up"] = "Measure this retry and decide from the clips."
    if not parsed.get("feasibility_note"):
        parsed["feasibility_note"] = "The repeated operator-confirmed target is queueable for a measured sample."
    if not requested_policy:
        return {}
    _, applied_fragment = apply_seed_policy(current_policy, requested_policy, mode="tune")
    return applied_fragment


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


@dataclass(slots=True)
class ReviewArtifactCritiqueResponse:
    ok: bool
    summary: str
    raw: str
    prompt_version: str
    confidence: str
    weakest_moments: list[str]
    preserved_strengths: list[str]
    artifacts_to_recheck: list[str]
    recommendation: str
    evidence_checked: list[str]


def request_seed_policy(
        *,
        project_root: Path,
        payload: dict[str, Any],
        routing: AdvisorRouting | None = None,
) -> SeedPolicyResponse:
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
        task=AdvisorTask.SEED_POLICY,
        prompt_version=SEED_PROMPT_VERSION,
        routing=routing,
        evidence_references=advisor_evidence_references(payload),
    )
    if not isinstance(parsed, dict):
        return SeedPolicyResponse(
            ok=False,
            summary="The seed worker did not return valid structured JSON.",
            raw=_raw_failure_payload(parsed),
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
    evidence_checked_raw = parsed.get("evidence_checked")
    evidence_checked = evidence_checked_raw if isinstance(evidence_checked_raw, list) else []
    request_disposition = str(parsed.get("request_disposition") or "unclear")
    if request_disposition not in REQUEST_DISPOSITIONS:
        request_disposition = "unclear"
    requested_experiment = object_dict(payload.get("requested_experiment"))
    latest_failed_sample_job = object_dict(payload.get("latest_failed_sample_job"))
    if _reject_nonpositive_video_budget(parsed, requested_experiment):
        proposed_policy = None
        request_disposition = "rejected"
    else:
        forced_confirmed_policy = _force_operator_confirmed_experiment(
            current_policy=base_policy,
            parsed=parsed,
            requested_experiment=requested_experiment,
            latest_failed_sample_job=latest_failed_sample_job,
            mode="seed",
        )
        if forced_confirmed_policy is not None:
            if forced_confirmed_policy:
                proposed_policy = _merge_policy_fragments(proposed_policy, forced_confirmed_policy) or proposed_policy
            request_disposition = "honored"
    if _should_carry_requested_experiment(request_disposition):
        requested_policy = _requested_experiment_policy(payload)
        if requested_policy:
            proposed_policy = _merge_policy_fragments(proposed_policy, requested_policy) or proposed_policy
    if proposed_policy is not None and base_policy:
        _, proposed_policy = apply_seed_policy(base_policy, proposed_policy)
    summary = str(parsed.get("summary") or "No summary returned.")
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


def request_note_tuning(
        *,
        project_root: Path,
        payload: dict[str, Any],
        routing: AdvisorRouting | None = None,
) -> TuningPolicyResponse:
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
            schema=_tune_response_schema(current_policy),
            max_seconds=90,
            task=AdvisorTask.NOTE_TUNING,
            prompt_version=TUNE_PROMPT_VERSION,
            routing=routing,
            evidence_references=advisor_evidence_references(payload),
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
            task=AdvisorTask.NOTE_TUNING,
            prompt_version=TUNE_PROMPT_VERSION,
            routing=routing,
            evidence_references=advisor_evidence_references(payload),
        )
    if not isinstance(parsed, dict):
        return TuningPolicyResponse(
            ok=False,
            summary="The tuning worker did not return valid structured JSON.",
            raw=_raw_failure_payload(parsed),
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
    evidence_checked_raw = parsed.get("evidence_checked")
    evidence_checked = evidence_checked_raw if isinstance(evidence_checked_raw, list) else []
    request_disposition = str(parsed.get("request_disposition") or "unclear")
    if request_disposition not in REQUEST_DISPOSITIONS:
        request_disposition = "unclear"
    requested_experiment = object_dict(payload.get("requested_experiment"))
    latest_failed_sample_job = object_dict(payload.get("latest_failed_sample_job"))
    if _reject_nonpositive_video_budget(parsed, requested_experiment):
        proposed_policy = None
        request_disposition = "rejected"
    else:
        forced_repeated_policy = _force_repeated_tune_experiment(
            current_policy=current_policy,
            parsed=parsed,
            requested_experiment=requested_experiment,
            repeat_signal=object_dict(payload.get("operator_repeat_signal")),
            latest_failed_sample_job=latest_failed_sample_job,
        )
        if forced_repeated_policy is not None:
            if forced_repeated_policy:
                proposed_policy = _merge_policy_fragments(proposed_policy, forced_repeated_policy) or proposed_policy
            request_disposition = "honored"
        forced_confirmed_policy = _force_operator_confirmed_experiment(
            current_policy=current_policy,
            parsed=parsed,
            requested_experiment=requested_experiment,
            latest_failed_sample_job=latest_failed_sample_job,
            mode="tune",
        )
        if forced_confirmed_policy is not None:
            if forced_confirmed_policy:
                proposed_policy = _merge_policy_fragments(proposed_policy, forced_confirmed_policy) or proposed_policy
            request_disposition = "honored"
    requested_policy = _requested_experiment_policy(payload)
    if _should_carry_requested_experiment(request_disposition) and requested_policy:
        proposed_policy = _merge_policy_fragments(proposed_policy, requested_policy) or proposed_policy
    if proposed_policy is not None and current_policy:
        _, proposed_policy = apply_seed_policy(current_policy, proposed_policy, mode="tune")
    self_check = _run_tune_self_check(
        project_root=project_root,
        tuning_context=payload,
        proposal={**parsed, "policy": proposed_policy},
    )
    self_check = _backstop_audio_guardrail_self_check(
        current_policy=current_policy,
        proposed_policy=proposed_policy,
        tuning_context=payload,
        self_check=self_check,
    )
    guardrail_rerun_status = str(object_dict(self_check).get("status") or "pass") if self_check is not None else None
    proposed_policy, self_check, surround_guardrail_message = _apply_surround_audio_guardrail(
        current_policy=current_policy,
        proposed_policy=proposed_policy,
        self_check=self_check,
    )
    if surround_guardrail_message:
        hinted_audio_key = _hinted_audio_policy_key(payload)
        parsed["summary"] = _surround_guardrail_summary(
            proposed_policy=proposed_policy,
            hinted_audio_key=hinted_audio_key,
        )
        parsed["diagnosis"] = _surround_guardrail_diagnosis(
            proposed_policy=proposed_policy,
            hinted_audio_key=hinted_audio_key,
            reason=surround_guardrail_message,
        )
        parsed["request_response"] = _surround_guardrail_request_response(
            proposed_policy=proposed_policy,
            hinted_audio_key=hinted_audio_key,
            reason=surround_guardrail_message,
        )
        follow_up = str(parsed.get("suggested_follow_up") or "").strip()
        if _suggested_follow_up_is_audio_specific(follow_up):
            parsed["suggested_follow_up"] = None
        self_check = _rerun_tune_self_check_after_surround_guardrail(
            project_root=project_root,
            tuning_context=payload,
            parsed=parsed,
            proposed_policy=proposed_policy,
            self_check=self_check,
            original_status=guardrail_rerun_status,
        )
    if proposed_policy is not None and not proposed_policy:
        proposed_policy = None
        if not parsed.get("suggested_follow_up"):
            guardrail_reason = str(
                object_dict(object_dict(self_check).get("surround_audio_guardrail")).get("reason") or ""
            ).strip()
            parsed["suggested_follow_up"] = guardrail_reason or "Try a non-audio change if you still want a smaller next sample."
    self_check_status = str(object_dict(self_check).get("status") or "pass")
    if self_check_status == "fail":
        if _should_carry_requested_experiment(request_disposition) and requested_policy and proposed_policy is not None:
            self_check = {
                **object_dict(self_check),
                "status": "warn",
                "summary": str(
                    object_dict(self_check).get("summary")
                    or "The explicit experiment is queueable, but the bench flagged caveats to review first."
                ),
            }
        else:
            proposed_policy = None
            if not parsed.get("suggested_follow_up"):
                parsed["suggested_follow_up"] = str(
                    object_dict(self_check).get("summary") or "Try a more specific note."
                )
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


def request_review_artifact_critique(
        *,
        project_root: Path,
        payload: dict[str, Any],
        routing: AdvisorRouting | None = None,
) -> ReviewArtifactCritiqueResponse:
    review_pack = object_dict(payload.get("multimodal_review_pack"))
    review_images = [
        str(path)
        for path in object_list(object_dict(review_pack).get("images"))
        if str(path).strip()
    ]
    if not review_images:
        return ReviewArtifactCritiqueResponse(
            ok=False,
            summary="No review images were available for artifact critique.",
            raw="",
            prompt_version=REVIEW_ARTIFACT_CRITIQUE_PROMPT_VERSION,
            confidence="low",
            weakest_moments=[],
            preserved_strengths=[],
            artifacts_to_recheck=[],
            recommendation="",
            evidence_checked=[],
        )
    parsed = _run_multimodal_tune_request(
        project_root=project_root,
        developer=(
            "You are a media review artifact critic. Return JSON only. "
            "Use the attached review images plus the provided runtime context. "
            "Do not claim to inspect motion or audio beyond what the supplied artifacts can support."
        ),
        message=_build_review_artifact_critique_prompt(payload),
        images=review_images,
        schema=_review_artifact_critique_schema(),
        max_seconds=75,
        task=AdvisorTask.REVIEW_ARTIFACT_CRITIQUE,
        prompt_version=REVIEW_ARTIFACT_CRITIQUE_PROMPT_VERSION,
        routing=routing,
        evidence_references=advisor_evidence_references(payload),
    )
    if not isinstance(parsed, dict):
        return ReviewArtifactCritiqueResponse(
            ok=False,
            summary="The artifact critic did not return valid structured JSON.",
            raw=_raw_failure_payload(parsed),
            prompt_version=REVIEW_ARTIFACT_CRITIQUE_PROMPT_VERSION,
            confidence="low",
            weakest_moments=[],
            preserved_strengths=[],
            artifacts_to_recheck=[],
            recommendation="",
            evidence_checked=[],
        )
    weakest_moments_raw = parsed.get("weakest_moments")
    preserved_strengths_raw = parsed.get("preserved_strengths")
    artifacts_to_recheck_raw = parsed.get("artifacts_to_recheck")
    evidence_checked_raw = parsed.get("evidence_checked")
    return ReviewArtifactCritiqueResponse(
        ok=True,
        summary=str(parsed.get("summary") or "No artifact summary returned."),
        raw=json.dumps(parsed, indent=2, sort_keys=True),
        prompt_version=REVIEW_ARTIFACT_CRITIQUE_PROMPT_VERSION,
        confidence=str(parsed.get("confidence") or "unknown"),
        weakest_moments=[str(item) for item in weakest_moments_raw] if isinstance(weakest_moments_raw, list) else [],
        preserved_strengths=[str(item) for item in preserved_strengths_raw] if isinstance(preserved_strengths_raw, list) else [],
        artifacts_to_recheck=[str(item) for item in artifacts_to_recheck_raw] if isinstance(artifacts_to_recheck_raw, list) else [],
        recommendation=str(parsed.get("recommendation") or ""),
        evidence_checked=[str(item) for item in evidence_checked_raw] if isinstance(evidence_checked_raw, list) else [],
    )


def request_run_verdict(
        *,
        project_root: Path,
        payload: dict[str, Any],
        routing: AdvisorRouting | None = None,
) -> RunVerdictResponse:
    del project_root, routing
    sample_result = object_dict(payload.get("sample_result"))
    size_target = object_dict(payload.get("size_target_analysis"))
    operator_request = object_dict(payload.get("operator_request"))
    size_status = str(size_target.get("status") or "").strip().lower()
    quality_score = _optional_number(sample_result.get("quality_score"))
    quality_target = _optional_number(sample_result.get("quality_target"))
    if quality_target is None:
        quality_target = _optional_number(operator_request.get("target"))
    quality_met = None if quality_score is None or quality_target is None else quality_score >= quality_target

    if size_status in {"infeasible", "quality_conflict", "over_target"} or quality_met is False:
        outcome = "needs_review"
    elif size_status in {"inside_target_band", "under_target"}:
        outcome = "strong_match"
    else:
        outcome = "acceptable_experiment"
    confidence = "high" if size_status and quality_met is not None else "medium" if sample_result else "low"
    summary = _deterministic_run_verdict_summary(
        size_status=size_status,
        quality_score=quality_score,
        quality_target=quality_target,
        quality_met=quality_met,
    )
    next_step = (
        "Review the comparison and choose a measured retry before approval."
        if outcome == "needs_review"
        else "Review the sampled moments, then approve the current evidence if picture and sound hold up."
    )
    evidence_checked = ["sample_result"]
    if size_target:
        evidence_checked.append("size_target_analysis")
    if operator_request:
        evidence_checked.append("operator_request")
    verdict_payload = {
        "summary": summary,
        "outcome": outcome,
        "confidence": confidence,
        "next_step": next_step,
        "evidence_checked": evidence_checked,
        "source": "deterministic",
    }
    return RunVerdictResponse(
        ok=bool(sample_result),
        summary=summary,
        raw=json.dumps(verdict_payload, indent=2, sort_keys=True),
        prompt_version=RUN_VERDICT_PROMPT_VERSION,
        outcome=outcome if sample_result else "unknown",
        confidence=confidence,
        next_step=next_step if sample_result else None,
        evidence_checked=evidence_checked if sample_result else [],
    )


def _deterministic_run_verdict_summary(
        *,
        size_status: str,
        quality_score: float | None,
        quality_target: float | None,
        quality_met: bool | None,
) -> str:
    if size_status == "infeasible":
        return "The approved stream plan cannot fit inside the requested size budget."
    if size_status == "quality_conflict":
        return "The measured sample reached a quality-floor conflict before it could satisfy the requested size."
    if size_status == "over_target":
        return "The measured sample is above the requested whole-episode size band."
    if quality_met is False and quality_score is not None and quality_target is not None:
        return f"The measured quality score {quality_score:g} is below the requested floor {quality_target:g}."
    if size_status == "inside_target_band":
        return "The measured sample is inside the requested whole-episode size band."
    if size_status == "under_target":
        return "The measured sample is smaller than the requested whole-episode size without a measured quality-floor miss."
    if quality_met is True:
        return "The measured sample met the requested quality floor; review the current picture and sound before approval."
    return "The measured calibration completed; review the current picture, sound, and size evidence before approval."


def _optional_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def request_operator_note_parse(
        *,
        project_root: Path,
        payload: dict[str, Any],
        routing: AdvisorRouting | None = None,
) -> dict[str, Any] | None:
    parsed = _run_structured_llm_request(
        project_root=project_root,
        developer=(
            "You are classifying a media encode operator note. No interactive tools are available. "
            "Use only the provided note context and return JSON that satisfies the schema exactly."
        ),
        message=_build_operator_note_parse_prompt(payload),
        schema=_operator_note_parse_schema(),
        max_seconds=45,
        task=AdvisorTask.OPERATOR_NOTE_PARSE,
        prompt_version=OPERATOR_NOTE_PARSE_PROMPT_VERSION,
        routing=routing,
        evidence_references=advisor_evidence_references(payload),
    )
    return parsed if isinstance(parsed, dict) else None


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


def _run_structured_llm_request(
        *,
        project_root: Path,
        developer: str,
        message: str,
        schema: dict[str, Any],
        max_seconds: int,
        task: AdvisorTask,
        prompt_version: str,
        routing: AdvisorRouting | None = None,
        evidence_references: list[str] | None = None,
) -> dict[str, Any] | StructuredLLMFailure | None:
    return _run_structured_llm_request_impl(
        project_root=project_root,
        developer=developer,
        message=message,
        schema=schema,
        max_seconds=max_seconds,
        task=task,
        prompt_version=prompt_version,
        routing=routing,
        evidence_references=evidence_references or [],
        subprocess_run=_run_codex_lab_process_impl,
        try_load_json=_try_load_json,
    )


def _run_multimodal_tune_request(
        *,
        project_root: Path,
        developer: str,
        message: str,
        images: list[str],
        schema: dict[str, Any],
        max_seconds: int,
        task: AdvisorTask,
        prompt_version: str,
        routing: AdvisorRouting | None = None,
        evidence_references: list[str] | None = None,
) -> dict[str, Any] | StructuredLLMFailure | None:
    return _run_multimodal_tune_request_impl(
        project_root=project_root,
        developer=developer,
        message=message,
        images=images,
        schema=schema,
        max_seconds=max_seconds,
        task=task,
        prompt_version=prompt_version,
        routing=routing,
        evidence_references=evidence_references or [],
        subprocess_run=_run_codex_lab_process_impl,
        try_load_json=_try_load_json,
    )


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


def _build_review_artifact_critique_prompt(payload: dict[str, Any]) -> str:
    return _build_review_artifact_critique_prompt_impl(payload)


def _build_operator_note_parse_prompt(payload: dict[str, Any]) -> str:
    return _build_operator_note_parse_prompt_impl(payload)


def _run_tune_self_check(
        *,
        project_root: Path,
        tuning_context: dict[str, Any],
        proposal: dict[str, Any],
) -> dict[str, Any]:
    del project_root, tuning_context
    proposed_policy = object_dict(proposal.get("policy"))
    if not proposed_policy:
        return {
            "status": "fail",
            "summary": "The proposed draft did not contain an executable allow-listed policy fragment.",
            "issues": ["No executable policy fragment was returned."],
            "source": "deterministic",
            "version": TUNE_SELF_CHECK_VERSION,
        }
    return {
        "status": "pass",
        "summary": "The normalized draft can proceed to deterministic quality-risk and operator-review gates.",
        "issues": [],
        "source": "deterministic",
        "version": TUNE_SELF_CHECK_VERSION,
    }


def _tune_response_schema(current_policy: dict[str, Any]) -> dict[str, Any]:
    return _tune_response_schema_impl(current_policy, request_dispositions=REQUEST_DISPOSITIONS)


def _seed_response_schema(base_policy: dict[str, Any]) -> dict[str, Any]:
    return _seed_response_schema_impl(base_policy, request_dispositions=REQUEST_DISPOSITIONS)


def _apply_surround_audio_guardrail(
        *,
        current_policy: dict[str, Any],
        proposed_policy: dict[str, Any] | None,
        self_check: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    if proposed_policy is None or self_check is None:
        return proposed_policy, self_check, None
    self_check_dict = object_dict(self_check)
    guardrail = object_dict(self_check_dict.get("surround_audio_guardrail"))
    if str(guardrail.get("status") or "ok") != "prefer_preserve_current":
        return proposed_policy, self_check, None
    proposed_audio = object_dict(proposed_policy.get("audio"))
    if not proposed_audio:
        return proposed_policy, self_check, None
    current_audio = object_dict(current_policy.get("audio"))
    lowered_keys = [
        key
        for key in _GUARDED_AUDIO_POLICY_KEYS
        if key in proposed_audio and current_audio.get(key) is not None and _surround_audio_bitrate_was_lowered(
            current_value=current_audio.get(key),
            proposed_value=proposed_audio.get(key),
        )
    ]
    if not lowered_keys:
        return proposed_policy, self_check, None
    adjusted_policy = json.loads(json.dumps(proposed_policy))
    adjusted_audio = object_dict(adjusted_policy.get("audio"))
    for key in lowered_keys:
        adjusted_audio.pop(key, None)
    if adjusted_audio:
        adjusted_policy["audio"] = adjusted_audio
    else:
        adjusted_policy.pop("audio", None)
    reason = str(guardrail.get("reason") or "").strip()
    issues = _filter_audio_specific_guardrail_issues(object_list(self_check_dict.get("issues")))
    summary = reason or "Rolled back the proposed surround-audio cut to preserve the current surround bitrate."
    status = str(self_check_dict.get("status") or "pass")
    if status != "fail":
        status = "warn"
    self_check = {
        **self_check_dict,
        "status": status,
        "summary": summary,
        "issues": issues,
    }
    return _compact_policy_payload(adjusted_policy), self_check, summary


def _surround_audio_bitrate_was_lowered(*, current_value: Any, proposed_value: Any) -> bool:
    if current_value is None or proposed_value is None:
        return False
    current_kbps = _parse_bitrate_kbps(current_value, fallback=current_value)
    proposed_kbps = _parse_bitrate_kbps(proposed_value, fallback=current_value)
    return proposed_kbps < current_kbps


def _normalize_guardrail_text(text: str) -> str:
    return f" {re.sub(r'[^a-z0-9.]+', ' ', text.casefold()).strip()} "


def _text_is_audio_specific(text: str) -> bool:
    if not text:
        return False
    lowered = _normalize_guardrail_text(text)
    if not any(token in lowered for token in _AUDIO_SPECIFIC_TOKENS):
        return False
    return not any(token in lowered for token in _NON_AUDIO_SPECIFIC_TOKENS)


def _text_mentions_audio(text: str) -> bool:
    if not text:
        return False
    lowered = _normalize_guardrail_text(text)
    return any(token in lowered for token in _AUDIO_SPECIFIC_TOKENS)


def _filter_audio_specific_guardrail_issues(raw_issues: list[Any]) -> list[str]:
    filtered: list[str] = []
    for item in raw_issues:
        issue_text = str(item).strip()
        if not issue_text or _text_is_audio_specific(issue_text):
            continue
        filtered.append(issue_text)
    return filtered


def _merge_issue_lists(*issue_groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in issue_groups:
        for issue in group:
            if issue and issue not in merged:
                merged.append(issue)
    return merged


def _suggested_follow_up_is_audio_specific(follow_up: str) -> bool:
    return _text_mentions_audio(follow_up)


def _operator_note_mentions_audio(note: str) -> bool:
    return _text_mentions_audio(note)


def _hinted_audio_policy_key(payload: dict[str, Any]) -> str | None:
    hint = object_dict(object_dict(payload.get("runtime_toolbelt")).get("audio_tradeoff_hint"))
    policy_key = str(hint.get("policy_key") or "").strip()
    return policy_key or None


def _audio_policy_key_copy(policy_key: str | None) -> str:
    if policy_key == "stereo_opus_bitrate":
        return "current stereo bitrate"
    if policy_key == "surround_5_1_opus_bitrate":
        return "current surround bitrate"
    if policy_key == "surround_7_1_opus_bitrate":
        return "current surround bitrate"
    return "current audio bitrate"


def _infer_audio_policy_key_from_reason(reason: str) -> str | None:
    lowered = reason.casefold()
    if "stereo" in lowered:
        return "stereo_opus_bitrate"
    if "7.1" in lowered:
        return "surround_7_1_opus_bitrate"
    if "5.1" in lowered or "surround" in lowered:
        return "surround_5_1_opus_bitrate"
    return None


def _surround_guardrail_request_response(*, proposed_policy: dict[str, Any] | None, hinted_audio_key: str | None, reason: str) -> str:
    effective_policy_key = hinted_audio_key or _infer_audio_policy_key_from_reason(reason)
    bitrate_copy = _audio_policy_key_copy(effective_policy_key)
    if proposed_policy:
        return (
            f"I kept the non-audio tightening, but held the {bitrate_copy} because that audio cut was not a "
            f"good trade here. {reason}"
        )
    return (
        f"I did not keep the cut to the {bitrate_copy} because it was not a good trade here. "
        f"{reason}"
    )


def _surround_guardrail_summary(*, proposed_policy: dict[str, Any] | None, hinted_audio_key: str | None) -> str:
    bitrate_copy = _audio_policy_key_copy(hinted_audio_key)
    if proposed_policy:
        return f"Kept the non-audio tightening, but preserved the {bitrate_copy}."
    return f"Preserved the {bitrate_copy} instead of taking the low-value audio cut."


def _surround_guardrail_diagnosis(*, proposed_policy: dict[str, Any] | None, hinted_audio_key: str | None, reason: str) -> str:
    bitrate_copy = _audio_policy_key_copy(hinted_audio_key)
    if proposed_policy:
        return (
            f"The draft still has useful non-audio tightening, but lowering the {bitrate_copy} was not the right trade here. "
            f"{reason}"
        )
    return f"The only proposed change was the {bitrate_copy} cut, and it was not the right trade here. {reason}"


def _audio_guardrail_backstop_reason(
        *,
        current_policy: dict[str, Any],
        proposed_policy: dict[str, Any] | None,
        tuning_context: dict[str, Any],
) -> str | None:
    if proposed_policy is None:
        return None
    hint = object_dict(object_dict(tuning_context.get("runtime_toolbelt")).get("audio_tradeoff_hint"))
    policy_key = str(hint.get("policy_key") or "").strip()
    leverage = str(hint.get("leverage") or "").strip().lower()
    if leverage not in {"low", "medium"}:
        return None
    if _operator_note_mentions_audio(str(tuning_context.get("operator_note") or "")):
        return None
    current_audio = object_dict(current_policy.get("audio"))
    proposed_audio = object_dict(proposed_policy.get("audio"))
    if policy_key not in _GUARDED_AUDIO_POLICY_KEYS:
        lowered_keys = [
            key
            for key in _GUARDED_AUDIO_POLICY_KEYS
            if current_audio.get(key) is not None
            and proposed_audio.get(key) is not None
            and _surround_audio_bitrate_was_lowered(
                current_value=current_audio.get(key),
                proposed_value=proposed_audio.get(key),
            )
        ]
        if len(lowered_keys) != 1:
            return None
        policy_key = lowered_keys[0]
    current_value = current_audio.get(policy_key)
    proposed_value = proposed_audio.get(policy_key)
    if current_value is None or proposed_value is None:
        return None
    if not _surround_audio_bitrate_was_lowered(current_value=current_value, proposed_value=proposed_value):
        return None
    current_kbps = _parse_bitrate_kbps(current_value, fallback=current_value)
    proposed_kbps = _parse_bitrate_kbps(proposed_value, fallback=current_value)
    bitrate_copy = _audio_policy_key_copy(policy_key)
    return (
        f"Dropping {bitrate_copy} from {current_kbps:g}k to {proposed_kbps:g}k is {leverage}-leverage here, "
        f"so preserve the {bitrate_copy} and spend the size budget on video instead."
    )


def _backstop_audio_guardrail_self_check(
        *,
        current_policy: dict[str, Any],
        proposed_policy: dict[str, Any] | None,
        tuning_context: dict[str, Any],
        self_check: dict[str, Any] | None,
) -> dict[str, Any] | None:
    reason = _audio_guardrail_backstop_reason(
        current_policy=current_policy,
        proposed_policy=proposed_policy,
        tuning_context=tuning_context,
    )
    if reason is None:
        return self_check
    self_check_dict = object_dict(self_check)
    guardrail = object_dict(self_check_dict.get("surround_audio_guardrail"))
    if str(guardrail.get("status") or "").strip() == "prefer_preserve_current":
        return self_check
    if self_check is None:
        return {
            "status": "warn",
            "summary": reason,
            "issues": [reason],
            "surround_audio_guardrail": {
                "status": "prefer_preserve_current",
                "reason": reason,
            },
        }
    return {
        **self_check_dict,
        "surround_audio_guardrail": {
            "status": "prefer_preserve_current",
            "reason": reason,
        },
    }


def _rerun_tune_self_check_after_surround_guardrail(
        *,
        project_root: Path,
        tuning_context: dict[str, Any],
        parsed: dict[str, Any],
        proposed_policy: dict[str, Any] | None,
        self_check: dict[str, Any] | None,
        original_status: str | None,
) -> dict[str, Any] | None:
    if proposed_policy is None or not proposed_policy or self_check is None:
        return self_check
    if str(original_status or "pass") not in {"pass", "fail", "warn"}:
        return self_check
    rerun_self_check = _run_tune_self_check(
        project_root=project_root,
        tuning_context=tuning_context,
        proposal={**parsed, "policy": proposed_policy},
    )
    if rerun_self_check is None:
        return self_check
    if str(original_status or "") != "fail":
        return rerun_self_check
    existing_self_check = object_dict(self_check)
    existing_issues = _filter_audio_specific_guardrail_issues(object_list(existing_self_check.get("issues")))
    if not existing_issues:
        return rerun_self_check
    rerun_dict = object_dict(rerun_self_check)
    rerun_issues = _filter_audio_specific_guardrail_issues(object_list(rerun_dict.get("issues")))
    merged_issues = _merge_issue_lists(existing_issues, rerun_issues)
    return {
        **rerun_dict,
        "status": "fail",
        "summary": str(existing_self_check.get("summary") or rerun_dict.get("summary") or ""),
        "issues": merged_issues,
    }


def _review_artifact_critique_schema() -> dict[str, Any]:
    return _review_artifact_critique_schema_impl()


def _operator_note_parse_schema() -> dict[str, Any]:
    return _operator_note_parse_schema_impl()


def _extract_seed_payload(raw: str) -> JSONObject:
    return _extract_seed_payload_impl(raw)


def _try_load_json(raw: str) -> JSONValue:
    return _try_load_json_impl(raw)


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
