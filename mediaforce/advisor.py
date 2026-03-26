from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ADVISOR_MODEL = "gpt-5.4"
SEED_PROMPT_VERSION = "seed-v1"
TUNE_PROMPT_VERSION = "tune-v2"
TUNE_SELF_CHECK_VERSION = "tune-self-check-v1"


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
    proposed_policy: dict[str, Any] | None
    toolbelt_used: list[str]
    self_check: dict[str, Any] | None


def request_tuning_advice(*, project_root: Path, payload: dict[str, Any]) -> AdvisorResponse:
    prompt = _build_prompt(payload)
    return _run_code_prompt(project_root=project_root, prompt=prompt, max_seconds=60)


def request_seed_policy(*, project_root: Path, payload: dict[str, Any]) -> SeedPolicyResponse:
    prompt = _build_seed_prompt(payload)
    response = _run_code_prompt(project_root=project_root, prompt=prompt, max_seconds=75)
    if not response.ok:
        return SeedPolicyResponse(
            ok=False,
            summary=response.summary,
            raw=response.raw,
            prompt_version=SEED_PROMPT_VERSION,
            proposed_policy=None,
        )
    parsed = _extract_seed_payload(response.raw)
    proposed_policy = parsed.get("policy") if isinstance(parsed.get("policy"), dict) else None
    summary = str(parsed.get("summary") or response.summary)
    if proposed_policy is None:
        return SeedPolicyResponse(
            ok=False,
            summary="Seed policy response did not include a valid policy object.",
            raw=response.raw,
            prompt_version=SEED_PROMPT_VERSION,
            proposed_policy=None,
        )
    return SeedPolicyResponse(
        ok=True,
        summary=summary,
        raw=response.raw,
        prompt_version=SEED_PROMPT_VERSION,
        proposed_policy=proposed_policy,
    )


def request_note_tuning(*, project_root: Path, payload: dict[str, Any]) -> TuningPolicyResponse:
    prompt = _build_tune_prompt(payload)
    parsed = _run_structured_llm_request(
        project_root=project_root,
        developer=(
            "You are a media encode tuning worker. No interactive tools are available in this path. "
            "Use only the provided runtime context and return JSON that satisfies the schema exactly."
        ),
        message=prompt,
        schema=_tune_response_schema(),
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
            proposed_policy=None,
            toolbelt_used=sorted((payload.get("runtime_toolbelt") or {}).keys()),
            self_check=None,
        )
    proposed_policy = parsed.get("policy") if isinstance(parsed.get("policy"), dict) else None
    evidence_checked_raw = parsed.get("evidence_checked")
    evidence_checked = evidence_checked_raw if isinstance(evidence_checked_raw, list) else []
    self_check = _run_tune_self_check(
        project_root=project_root,
        tuning_context=payload,
        proposal=parsed,
    )
    self_check_status = str((self_check or {}).get("status") or "pass")
    if self_check_status == "fail":
        proposed_policy = None
        if not parsed.get("suggested_follow_up"):
            parsed["suggested_follow_up"] = str((self_check or {}).get("summary") or "Try a more specific note.")
    return TuningPolicyResponse(
        ok=proposed_policy is not None,
        summary=str(parsed.get("summary") or "No summary returned."),
        raw=json.dumps(parsed, indent=2, sort_keys=True),
        prompt_version=TUNE_PROMPT_VERSION,
        diagnosis=str(parsed.get("diagnosis") or "No diagnosis returned."),
        confidence=str(parsed.get("confidence") or "unknown"),
        evidence_checked=[str(item) for item in evidence_checked],
        suggested_follow_up=(str(parsed.get("suggested_follow_up")) if parsed.get("suggested_follow_up") else None),
        proposed_policy=proposed_policy,
        toolbelt_used=sorted((payload.get("runtime_toolbelt") or {}).keys()),
        self_check=self_check,
    )


def apply_seed_policy(base_policy: dict[str, Any], proposed_policy: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    applied_policy = json.loads(json.dumps(base_policy))
    applied_fragment: dict[str, Any] = {}

    video_updates = _clamp_video_seed(proposed_policy.get("video"), applied_policy.get("video") or {})
    if video_updates:
        applied_policy.setdefault("video", {}).update(video_updates)
        applied_fragment["video"] = video_updates

    audio_updates = _clamp_audio_seed(proposed_policy.get("audio"), applied_policy.get("audio") or {})
    if audio_updates:
        applied_policy.setdefault("audio", {}).update(audio_updates)
        applied_fragment["audio"] = audio_updates

    return applied_policy, applied_fragment


def _run_code_prompt(*, project_root: Path, prompt: str, max_seconds: int) -> AdvisorResponse:
    with tempfile.NamedTemporaryFile(prefix="mediaforce-advice-", suffix=".txt", delete=False) as handle:
        output_path = Path(handle.name)
    cmd = [
        "code",
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
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=75)
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
        "llm",
        "request",
        "--model",
        ADVISOR_MODEL,
        "--developer",
        developer,
        "--message",
        message,
        "--format-name",
        "media_harness_tuning",
        "--schema-json",
        json.dumps(schema, sort_keys=True),
        "--format-strict",
    ]
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=max_seconds + 15, cwd=project_root)
    except Exception:
        return None
    raw = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0 or not raw:
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
    return (
        "You are helping seed a first-pass media encode calibration draft. "
        "Bias slightly toward smaller files, but avoid obvious quality damage. "
        "Return valid JSON only with this exact shape: "
        '{"summary":"short sentence","policy":{"video":{},"audio":{}}}. '
        "Do not use markdown fences. Only include these policy keys when they are helpful: "
        "video.target_vmaf, video.min_target_vmaf, video.target_xpsnr, video.min_target_xpsnr, "
        "video.max_encoded_percent, video.default_grain, audio.surround_5_1_opus_bitrate. "
        "Do not invent any other keys. Keep the summary short and practical. Here is the folder context:\n\n"
        f"{serialized}"
    )


def _build_tune_prompt(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    return (
        "You are GPT-5.4 acting as a fast media encode tuning worker. "
        "Your job is to use the operator's note plus the current calibration context to pick the next sampled calibration draft. "
        "The runtime has already gathered the only allowed quick toolbelt evidence for this turn and will run a separate self-check after your proposal. "
        "Do not assume any other probing, frame grabs, or external tools are available. "
        "Do not do slow exhaustive analysis or long encodes. If you are not confident, say so clearly instead of making up certainty. "
        "Return valid JSON only with this exact shape: "
        '{"summary":"short sentence","diagnosis":"short diagnosis","confidence":"high|medium|low","evidence_checked":["..."],"suggested_follow_up":"optional short suggestion","policy":{"video":{},"audio":{}}}. '
        "Use null for any tunable key that should stay unchanged. Keep `video.default_grain` as a whole number, keep `video.max_encoded_percent` at or below the current budget unless the operator explicitly accepts larger files, and do not widen the draft more than the note justifies. "
        "Only include these policy keys when they are helpful: video.target_vmaf, video.min_target_vmaf, video.target_xpsnr, video.min_target_xpsnr, video.max_encoded_percent, video.default_grain, audio.surround_5_1_opus_bitrate. "
        "Do not invent other keys. Here is the tuning context:\n\n"
        f"{serialized}"
    )


def _run_tune_self_check(*, project_root: Path, tuning_context: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any] | None:
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


def _tune_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "summary",
            "diagnosis",
            "confidence",
            "evidence_checked",
            "suggested_follow_up",
            "policy",
        ],
        "properties": {
            "summary": {"type": "string"},
            "diagnosis": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "evidence_checked": {"type": "array", "items": {"type": "string"}},
            "suggested_follow_up": {"type": ["string", "null"]},
            "policy": {
                "type": "object",
                "additionalProperties": False,
                "required": ["video", "audio"],
                "properties": {
                    "video": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "target_vmaf",
                            "min_target_vmaf",
                            "target_xpsnr",
                            "min_target_xpsnr",
                            "max_encoded_percent",
                            "default_grain",
                        ],
                        "properties": {
                            "target_vmaf": {"type": ["number", "null"]},
                            "min_target_vmaf": {"type": ["number", "null"]},
                            "target_xpsnr": {"type": ["number", "null"]},
                            "min_target_xpsnr": {"type": ["number", "null"]},
                            "max_encoded_percent": {"type": ["number", "null"]},
                            "default_grain": {"type": ["number", "null"]},
                        },
                    },
                    "audio": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["surround_5_1_opus_bitrate"],
                        "properties": {
                            "surround_5_1_opus_bitrate": {"type": ["string", "null"]},
                        },
                    },
                },
            },
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


def _extract_seed_payload(raw: str) -> dict[str, Any]:
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
    parsed = _try_load_json(candidate[start : end + 1])
    return parsed if isinstance(parsed, dict) else {}


def _try_load_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _try_load_first_json_object(raw: str) -> Any:
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


def _clamp_video_seed(raw: Any, base_video: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    updates: dict[str, Any] = {}
    if raw.get("target_vmaf") is not None:
        updates["target_vmaf"] = round(_clamp_float(raw["target_vmaf"], minimum=90.0, maximum=98.0), 2)
    if raw.get("min_target_vmaf") is not None:
        target = float(updates.get("target_vmaf", base_video.get("target_vmaf", 95.0)))
        updates["min_target_vmaf"] = round(min(_clamp_float(raw["min_target_vmaf"], minimum=88.0, maximum=97.0), target), 2)
    if raw.get("target_xpsnr") is not None:
        updates["target_xpsnr"] = round(_clamp_float(raw["target_xpsnr"], minimum=32.0, maximum=41.0), 2)
    if raw.get("min_target_xpsnr") is not None:
        target = float(updates.get("target_xpsnr", base_video.get("target_xpsnr", 35.5)))
        updates["min_target_xpsnr"] = round(min(_clamp_float(raw["min_target_xpsnr"], minimum=30.0, maximum=40.0), target), 2)
    if raw.get("max_encoded_percent") is not None:
        updates["max_encoded_percent"] = _clamp_int(raw["max_encoded_percent"], minimum=35, maximum=95)
    if raw.get("default_grain") is not None:
        updates["default_grain"] = _clamp_int(raw["default_grain"], minimum=0, maximum=12)
    return updates


def _clamp_audio_seed(raw: Any, base_audio: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    updates: dict[str, Any] = {}
    if raw.get("surround_5_1_opus_bitrate") is not None:
        bitrate = _parse_bitrate_kbps(raw["surround_5_1_opus_bitrate"], fallback=base_audio.get("surround_5_1_opus_bitrate", "224k"))
        updates["surround_5_1_opus_bitrate"] = f"{_clamp_int(bitrate, minimum=160, maximum=320)}k"
    return updates


def _parse_bitrate_kbps(value: Any, *, fallback: Any) -> int:
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


def _clamp_float(value: Any, *, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return minimum
    return max(min(number, maximum), minimum)


def _clamp_int(value: Any, *, minimum: int, maximum: int) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return minimum
    return max(min(number, maximum), minimum)
