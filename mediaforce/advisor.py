import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.type_defs import JSONObject, JSONValue

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
    base_policy = dict(payload.get("base_policy") or {}) if isinstance(payload.get("base_policy"), dict) else {}
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
    proposed_policy = parsed.get("policy") if isinstance(parsed.get("policy"), dict) else None
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
    current_policy = dict(payload.get("policy") or {}) if isinstance(payload.get("policy"), dict) else {}
    review_pack = payload.get("multimodal_review_pack") if isinstance(payload.get("multimodal_review_pack"),
                                                                      dict) else None
    review_images = [
        str(path)
        for path in list((review_pack or {}).get("images") or [])
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
            toolbelt_used=sorted((payload.get("runtime_toolbelt") or {}).keys()),
            self_check=None,
        )
    proposed_policy = parsed.get("policy") if isinstance(parsed.get("policy"), dict) else None
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
    self_check_status = str((self_check or {}).get("status") or "pass")
    if self_check_status == "fail":
        proposed_policy = None
        if not parsed.get("suggested_follow_up"):
            parsed["suggested_follow_up"] = str((self_check or {}).get("summary") or "Try a more specific note.")
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
        toolbelt_used=sorted((payload.get("runtime_toolbelt") or {}).keys()),
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
    with tempfile.NamedTemporaryFile(prefix="mediaforce-advice-", suffix=".txt", delete=False) as handle:
        output_path = Path(handle.name)
    cmd = [
        "code",
        *_memory_disabled_code_args(),
        "exec",
        "--model",
        ADVISOR_MODEL,
        "--sandbox",
        "danger-full-access",
        "--skip-git-repo-check",
        "--max-seconds",
        str(max_seconds),
        "--output-last-message",
        str(output_path),
        "-C",
        str(project_root),
        prompt,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=75)
    except FileNotFoundError as exc:
        return AdvisorResponse(ok=False, summary="`code` is required but not installed.", raw=str(exc))
    except Exception as exc:
        return AdvisorResponse(ok=False, summary="`code` failed to run.", raw=str(exc))

    try:
        raw = output_path.read_text().strip()
    finally:
        output_path.unlink(missing_ok=True)

    if result.returncode != 0 and not raw:
        message = result.stderr.strip() or result.stdout.strip() or "Unknown `code` failure"
        return AdvisorResponse(ok=False, summary="GPT recommendation failed.", raw=message)

    summary = raw.splitlines()[0] if raw else "No recommendation returned."
    return AdvisorResponse(ok=bool(raw), summary=summary, raw=raw or (result.stderr.strip() or result.stdout.strip()))


def _run_structured_llm_request(
        *,
        project_root: Path,
        developer: str,
        message: str,
        schema: dict[str, Any],
        max_seconds: int,
) -> dict[str, Any] | None:
    cmd = [
        "code",
        *_memory_disabled_code_args(),
        "llm",
        "request",
        "--model",
        ADVISOR_MODEL,
        "--developer",
        developer,
        "--message",
        message,
        "--format-name",
        "mediaforce_tuning",
        "--schema-json",
        json.dumps(schema, sort_keys=True),
        "--format-strict",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=max_seconds + 15, cwd=project_root)
    except Exception:
        return None
    raw = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0 or not raw:
        return None
    parsed = _try_load_json(raw)
    if parsed is None:
        parsed = _try_load_first_json_object(raw)
    return parsed if isinstance(parsed, dict) else None


def _run_multimodal_tune_request(
        *,
        project_root: Path,
        developer: str,
        message: str,
        images: list[str],
        max_seconds: int,
) -> dict[str, Any] | None:
    with tempfile.NamedTemporaryFile(prefix="mediaforce-tune-", suffix=".txt", delete=False) as handle:
        output_path = Path(handle.name)
    cmd = [
        "code",
        *_memory_disabled_code_args(),
        "exec",
        "--model",
        ADVISOR_MODEL,
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--max-seconds",
        str(max_seconds),
        "--output-last-message",
        str(output_path),
        "--demo",
        developer,
        "-C",
        str(project_root),
    ]
    for image_path in images:
        cmd.extend(["--image", image_path])
    cmd.append(message)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=max_seconds + 30)
    except Exception:
        output_path.unlink(missing_ok=True)
        return None
    try:
        raw = output_path.read_text().strip()
    except OSError:
        raw = ""
    finally:
        output_path.unlink(missing_ok=True)
    if result.returncode != 0 and not raw:
        raw = result.stderr.strip() or result.stdout.strip()
    if not raw:
        return None
    parsed = _try_load_json(raw)
    if parsed is None:
        parsed = _try_load_first_json_object(raw)
    return parsed if isinstance(parsed, dict) else None


def _build_prompt(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    return (
        "You are advising a personal media re-encoding calibration workflow. "
        "Bias slightly toward smaller files, but do not recommend obvious quality damage. "
        "Return short markdown with these sections exactly: "
        "Recommendation, Why, Setting changes, Audio/Subtitles notes. "
        "Use plain language for a human reviewing TV encodes. Here is the calibration context:\n\n"
        f"{serialized}"
    )


def _build_seed_prompt(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    base_policy = dict(payload.get("base_policy") or {}) if isinstance(payload.get("base_policy"), dict) else {}
    valid_keys = _policy_key_paths(base_policy)
    policy_shape = _policy_shape_example(base_policy)
    return (
        "You are helping seed a first-pass media encode calibration draft. "
        "This is a cold-start guess for one folder, not a measured calibration result. "
        "Treat the operator note as the start of a conversation, not a decorative hint. "
        "Bias slightly toward smaller files, but avoid obvious quality damage. "
        "Teach media-class taste, not a single-title compression floor. "
        "Use the base policy as the safe default and make only the smallest helpful overrides. "
        "Clean, forgiving material can lean one notch smaller than default, but dark, grainy, noisy, fast-motion, or otherwise uncertain material should stay close to the base policy until measured calibration proves it can move further. "
        "Do not chase dramatic savings, do not generalize aggressively from one easy sample item, and prefer leaving a key out when the evidence is weak. "
        "If the operator asks for a smaller encode, do not silently move to a higher quality target. "
        "If the request looks unrealistic, you may still draft the operator-confirmed experiment, but say that plainly and mark it as honored_with_risk instead of fighting the request. "
        "If you soften or reject a request, say so explicitly. "
        "If the class signals clearly look like clean 1080p catalog TV, a mild lean can look like VMAF around 94-95, XPSNR around 39-40, max encoded percent in the low-to-mid 70s, grain around 4-6, and 5.1 Opus around 224k, but treat those as gentle reference points rather than mandatory targets. "
        "Use metric_support and the current base policy to decide whether VMAF or XPSNR guidance is more relevant. "
        "Return valid JSON only with this exact shape: "
        f'{{"request_response":"short conversational reply","request_disposition":"honored|honored_with_risk|softened|rejected|unclear","summary":"short sentence","diagnosis":"short diagnosis","confidence":"high|medium|low","evidence_checked":["..."],"suggested_follow_up":"optional short suggestion","feasibility_note":"optional short feasibility note","policy":{json.dumps(policy_shape, sort_keys=True)}}}. '
        "Do not use markdown fences. Use the current base policy as the draft surface. "
        "For any key that should stay unchanged, send null for that key. "
        f"Only use these policy keys: {', '.join(valid_keys) if valid_keys else 'none'}. "
        "Do not invent any other keys. Keep the reply direct and conversational, and keep the summary short and practical. Here is the folder context:\n\n"
        f"{serialized}"
    )


def _build_tune_prompt(payload: dict[str, Any]) -> str:
    prompt_payload = dict(payload)
    review_pack = prompt_payload.get("multimodal_review_pack")
    if isinstance(review_pack, dict):
        summarized_pack = {key: value for key, value in review_pack.items() if key != "images"}
        if "images" in review_pack:
            summarized_pack["image_count"] = len(list(review_pack.get("images") or []))
        prompt_payload["multimodal_review_pack"] = summarized_pack
    serialized = json.dumps(prompt_payload, indent=2, sort_keys=True)
    current_policy = dict(payload.get("policy") or {}) if isinstance(payload.get("policy"), dict) else {}
    valid_keys = _policy_key_paths(current_policy)
    policy_shape = _policy_shape_example(current_policy)
    return (
        "You are GPT-5.4 acting as a fast media encode tuning worker. "
        "Your job is to use the operator's note plus the current calibration context to draft the next sampled calibration run for operator review before anything queues. "
        "The runtime has already gathered the only allowed quick toolbelt evidence for this turn and will run a separate self-check after your proposal. "
        "Do not assume any other probing, frame grabs, or external tools are available. "
        "When review_media_context is present, treat it as part of the active review conversation: the operator may be asking about the sampled source-versus-draft clips, the compare clip, or the current audio tradeoff before approving a folder encode. "
        "When multimodal_review_pack is present, the attached images appear in the same order as multimodal_review_pack.artifacts. Use them as real review evidence, but describe them carefully and never pretend they show more than they actually show. "
        "You cannot literally watch or hear the clips from this prompt, so never pretend that you did. Instead, use the supplied review context, measured calibration result, policy, audio summary, and the operator's own observations to reason collaboratively about likely video and audio quality tradeoffs. "
        "If the operator is clearly discussing what they saw or heard, engage that review directly in request_response and suggested_follow_up instead of replying like a generic size-only tuner. "
        "Do not do slow exhaustive analysis or long encodes. If you are not confident, say so clearly instead of making up certainty. "
        "Treat the operator note as the start of a conversation, not a decorative hint. "
        "Use the operator note directly. If you intentionally soften or redirect an explicit request, say that plainly in request_response, diagnosis, and suggested_follow_up instead of hiding the tradeoff. "
        "If the operator asks for a smaller encode, do not silently move to a higher quality target. "
        "If the request looks unrealistic, you may still draft the operator-confirmed experiment, but mark it honored_with_risk and explain the risk plainly instead of fighting the request. "
        "Return JSON only with no markdown fences or extra commentary. "
        "Return valid JSON only with this exact shape: "
        f'{{"request_response":"short conversational reply","request_disposition":"honored|honored_with_risk|softened|rejected|unclear","summary":"short sentence","diagnosis":"short diagnosis","confidence":"high|medium|low","evidence_checked":["..."],"suggested_follow_up":"optional short suggestion","feasibility_note":"optional short feasibility note","policy":{json.dumps(policy_shape, sort_keys=True)}}}. '
        "Use the current policy as the full draft surface for this run. For any key that should stay unchanged, send null for that key. "
        "It is acceptable to change methodology knobs like preset, sample cadence, or CRF bounds when the note clearly justifies it, but be explicit in the diagnosis when you do that. "
        f"Only use these policy keys: {', '.join(valid_keys) if valid_keys else 'none'}. "
        "Do not invent other keys. Keep request_response conversational and direct. Here is the tuning context:\n\n"
        f"{serialized}"
    )


def _build_run_verdict_prompt(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    return (
        "You are GPT-5.4 summarizing a completed media encode calibration run for a human operator. "
        "The run is already measured by deterministic tools; do not invent metrics. "
        "If the operator requested an explicit experiment, judge the outcome against that request instead of generic conservative defaults. "
        "Keep the answer short, practical, and honest about risk. "
        "Return valid JSON only with this exact shape: "
        '{"summary":"short verdict","outcome":"strong_match|acceptable_experiment|needs_review|poor_fit","confidence":"high|medium|low","next_step":"optional short suggestion","evidence_checked":["..."]}. '
        "Here is the completed calibration context:\n\n"
        f"{serialized}"
    )


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
    policy_schema = _policy_response_schema(current_policy)
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
            "request_disposition": {"type": "string", "enum": list(REQUEST_DISPOSITIONS)},
            "summary": {"type": "string"},
            "diagnosis": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "evidence_checked": {"type": "array", "items": {"type": "string"}},
            "suggested_follow_up": {"type": ["string", "null"]},
            "feasibility_note": {"type": ["string", "null"]},
            "policy": policy_schema,
        },
    }


def _seed_response_schema(base_policy: dict[str, Any]) -> dict[str, Any]:
    policy_schema = _policy_response_schema(base_policy)
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
            "request_disposition": {"type": "string", "enum": list(REQUEST_DISPOSITIONS)},
            "summary": {"type": "string"},
            "diagnosis": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "evidence_checked": {"type": "array", "items": {"type": "string"}},
            "suggested_follow_up": {"type": ["string", "null"]},
            "feasibility_note": {"type": ["string", "null"]},
            "policy": policy_schema,
        },
    }


def _tune_self_check_schema() -> dict[str, Any]:
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


def _run_verdict_schema() -> dict[str, Any]:
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


def _extract_seed_payload(raw: str) -> JSONObject:
    candidate = raw.strip()
    if not candidate:
        return {}
    parsed = _try_load_json(candidate)
    if isinstance(parsed, dict):
        return parsed
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        return {}
    parsed = _try_load_json(candidate[start: end + 1])
    return parsed if isinstance(parsed, dict) else {}


def _try_load_json(raw: str) -> JSONValue:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _try_load_first_json_object(raw: str) -> JSONValue:
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


def _policy_key_paths(policy: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for section in ("video", "audio", "subtitle"):
        raw_section = policy.get(section)
        if not isinstance(raw_section, dict):
            continue
        for key in raw_section:
            paths.append(f"{section}.{key}")
    return paths


def _policy_shape_example(policy: dict[str, Any]) -> dict[str, Any]:
    shape: dict[str, Any] = {}
    for section in ("video", "audio", "subtitle"):
        raw_section = policy.get(section)
        if not isinstance(raw_section, dict):
            continue
        shape[section] = {key: None for key in raw_section}
    if not shape:
        shape = {"video": {}, "audio": {}, "subtitle": {}}
    return shape


def _compact_policy_payload(policy: dict[str, Any] | None) -> dict[str, Any] | None:
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


def _policy_response_schema(policy: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for section in ("video", "audio", "subtitle"):
        raw_section = policy.get(section)
        if not isinstance(raw_section, dict):
            continue
        properties[section] = _policy_section_schema(raw_section)
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


def _policy_section_schema(section_policy: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for key, value in section_policy.items():
        properties[key] = _policy_value_schema(value)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def _policy_value_schema(value: JSONValue) -> dict[str, Any]:
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
        return {
            "type": ["array", "null"],
            "items": item_schema,
        }
    return {"type": ["string", "null"]}


def _normalize_policy_section(
        section: str, raw: JSONValue, base_section: JSONObject, *, mode: str
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    updates: dict[str, Any] = {}
    for key, base_value in base_section.items():
        if key not in raw or raw.get(key) is None:
            continue
        normalized = _normalize_policy_value(section, key, raw[key], base_value, mode=mode)
        if normalized is not _SKIP_POLICY_VALUE:
            updates[key] = normalized
    if section == "video":
        updates = _finalize_video_policy_updates(updates, base_section)
    return updates


_SKIP_POLICY_VALUE = object()


def _normalize_policy_value(
        section: str, key: str, value: JSONValue, base_value: JSONValue, *, mode: str
) -> JSONValue | object:
    if section == "video":
        return _normalize_video_policy_value(key, value, base_value, mode=mode)
    if section == "audio":
        return _normalize_audio_policy_value(key, value, base_value)
    if section == "subtitle":
        return _normalize_subtitle_policy_value(key, value, base_value)
    return _SKIP_POLICY_VALUE


def _normalize_video_policy_value(
        key: str, value: JSONValue, base_value: JSONValue, *, mode: str
) -> JSONValue | object:
    _ = mode
    if key == "quality_metric":
        metric = str(value).strip().lower()
        if metric in {"auto", "vmaf", "xpsnr", "ssim", "psnr"}:
            return metric
        return _SKIP_POLICY_VALUE
    if key == "encoder":
        trimmed = str(value).strip()
        return trimmed or _SKIP_POLICY_VALUE
    if key == "pixel_format":
        trimmed = str(value).strip()
        return trimmed or _SKIP_POLICY_VALUE
    if key == "preset":
        return _clamp_int(value, minimum=0, maximum=13)
    if key == "crf_search":
        return _coerce_bool(value, fallback=bool(base_value))
    if key == "target_vmaf":
        return round(_clamp_float(value, minimum=70.0, maximum=98.0), 2)
    if key == "min_target_vmaf":
        return round(_clamp_float(value, minimum=65.0, maximum=97.0), 2)
    if key == "target_xpsnr":
        return round(_clamp_float(value, minimum=25.0, maximum=41.0), 2)
    if key == "min_target_xpsnr":
        return round(_clamp_float(value, minimum=20.0, maximum=40.0), 2)
    if key in {"target_relax_step_vmaf", "target_relax_step_xpsnr"}:
        return round(_clamp_float(value, minimum=0.1, maximum=5.0), 2)
    if key in {"sample_every", "sample_duration"}:
        return _normalize_duration_like(value, fallback=str(base_value or ""))
    if key in {"min_crf", "max_crf"}:
        return _clamp_int(value, minimum=0, maximum=63)
    if key == "max_encoded_percent":
        return _clamp_int(value, minimum=10, maximum=100)
    if key == "default_grain":
        return _clamp_int(value, minimum=0, maximum=50)
    if key == "grain_denoise":
        return _clamp_int(value, minimum=0, maximum=1)
    if key == "thorough":
        return _coerce_bool(value, fallback=bool(base_value))
    return _normalize_like_base(value, base_value)


def _normalize_audio_policy_value(key: str, value: JSONValue, base_value: JSONValue) -> JSONValue | object:
    if key in {"keep_languages", "copy_codecs", "convert_to_opus_codecs"}:
        return _normalize_string_list(value)
    if key == "stereo_opus_bitrate":
        bitrate = _parse_bitrate_kbps(value, fallback=base_value)
        return f"{_clamp_int(bitrate, minimum=48, maximum=384)}k"
    if key == "surround_5_1_opus_bitrate":
        bitrate = _parse_bitrate_kbps(value, fallback=base_value)
        return f"{_clamp_int(bitrate, minimum=96, maximum=512)}k"
    if key == "surround_7_1_opus_bitrate":
        bitrate = _parse_bitrate_kbps(value, fallback=base_value)
        return f"{_clamp_int(bitrate, minimum=128, maximum=768)}k"
    return _normalize_like_base(value, base_value)


def _normalize_subtitle_policy_value(
        key: str, value: JSONValue, base_value: JSONValue
) -> JSONValue | object:
    if key == "keep_languages":
        return _normalize_string_list(value)
    if key in {"prefer_text", "keep_forced"}:
        return _coerce_bool(value, fallback=bool(base_value))
    if key == "default_mode":
        trimmed = str(value).strip()
        return trimmed or _SKIP_POLICY_VALUE
    return _normalize_like_base(value, base_value)


def _normalize_like_base(value: JSONValue, base_value: JSONValue) -> JSONValue | object:
    if isinstance(base_value, bool):
        return _coerce_bool(value, fallback=base_value)
    if isinstance(base_value, int) and not isinstance(base_value, bool):
        return _clamp_int(value, minimum=0, maximum=max(int(base_value), 1_000_000))
    if isinstance(base_value, float):
        return round(_clamp_float(value, minimum=float("-inf"), maximum=float("inf")), 2)
    if isinstance(base_value, list):
        if all(isinstance(item, str) for item in base_value):
            return _normalize_string_list(value)
        if all(isinstance(item, bool) for item in base_value):
            if not isinstance(value, list):
                return []
            return [_coerce_bool(item, fallback=False) for item in value]
        if all(isinstance(item, (int, float)) for item in base_value):
            if not isinstance(value, list):
                return []
            return [_clamp_float(item, minimum=float("-inf"), maximum=float("inf")) for item in value]
    trimmed = str(value).strip()
    return trimmed or _SKIP_POLICY_VALUE


def _finalize_video_policy_updates(updates: dict[str, Any], base_video: dict[str, Any]) -> dict[str, Any]:
    finalized = dict(updates)
    if "target_vmaf" in finalized or "min_target_vmaf" in finalized:
        target = float(finalized.get("target_vmaf", base_video.get("target_vmaf", 95.0)))
        floor = float(finalized.get("min_target_vmaf", base_video.get("min_target_vmaf", target)))
        finalized["target_vmaf"] = round(_clamp_float(target, minimum=70.0, maximum=98.0), 2)
        finalized["min_target_vmaf"] = round(
            min(_clamp_float(floor, minimum=65.0, maximum=97.0), finalized["target_vmaf"]), 2)
    if "target_xpsnr" in finalized or "min_target_xpsnr" in finalized:
        target = float(finalized.get("target_xpsnr", base_video.get("target_xpsnr", 41.0)))
        floor = float(finalized.get("min_target_xpsnr", base_video.get("min_target_xpsnr", target)))
        finalized["target_xpsnr"] = round(_clamp_float(target, minimum=25.0, maximum=41.0), 2)
        finalized["min_target_xpsnr"] = round(
            min(_clamp_float(floor, minimum=20.0, maximum=40.0), finalized["target_xpsnr"]), 2)
    if "min_crf" in finalized or "max_crf" in finalized:
        min_crf = int(finalized.get("min_crf", base_video.get("min_crf", 18)))
        max_crf = int(finalized.get("max_crf", base_video.get("max_crf", 38)))
        finalized["min_crf"] = min(min_crf, max_crf)
        finalized["max_crf"] = max(min_crf, max_crf)
    return finalized


def _normalize_duration_like(value: JSONValue, *, fallback: str) -> str | object:
    trimmed = str(value).strip().lower()
    if not trimmed:
        return fallback or _SKIP_POLICY_VALUE
    if any(unit in trimmed for unit in ("ms", "s", "m", "h")) and any(char.isdigit() for char in trimmed):
        return trimmed
    return fallback or _SKIP_POLICY_VALUE


def _normalize_string_list(value: JSONValue) -> list[str]:
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


def _coerce_bool(value: JSONValue, *, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    trimmed = str(value).strip().lower()
    if trimmed in {"1", "true", "yes", "on"}:
        return True
    if trimmed in {"0", "false", "no", "off"}:
        return False
    return fallback


def _parse_bitrate_kbps(value: JSONValue, *, fallback: JSONValue) -> int:
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


def _clamp_float(value: JSONValue, *, minimum: float, maximum: float) -> float:
    if not isinstance(value, (str, int, float, bool)):
        return minimum
    try:
        number = float(value)
    except (TypeError, ValueError):
        return minimum
    return max(min(number, maximum), minimum)


def _clamp_int(value: JSONValue, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, (str, int, float, bool)):
        return minimum
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return minimum
    return max(min(number, maximum), minimum)
