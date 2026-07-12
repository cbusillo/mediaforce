import argparse
import json
import struct
import tempfile
import zlib
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from mediaforce.advisor import (
    request_note_tuning,
    request_operator_note_parse,
    request_review_artifact_critique,
    request_run_verdict,
    request_seed_policy,
)
from mediaforce.advising.routing import AdvisorRouting, AdvisorTask, advisor_routing_for_models
from mediaforce.advising.policy import has_nonpositive_video_budget


EVAL_SCHEMA_VERSION = 1
REQUIRED_PASS_RATE = 1.0


@dataclass(frozen=True, slots=True)
class AdvisorEvalCase:
    case_id: str
    task: AdvisorTask | str
    model: str | None
    payload: dict[str, Any]
    fallback_models: tuple[str, ...] = ()
    expected_values: tuple[tuple[str, Any], ...] = ()
    expected_one_of: tuple[tuple[str, tuple[Any, ...]], ...] = ()
    required_nonempty: tuple[str, ...] = ()
    forbidden_terms: tuple[str, ...] = ()
    preserve_surround_audio: bool = False


def recommended_eval_cases() -> tuple[AdvisorEvalCase, ...]:
    base_policy = _base_policy()
    explicit_size_fragment = {
        "video": {
            "size_goal_schema_version": 1,
            "size_goal_mode": "absolute",
            "size_goal_source": "operator_note",
            "sample_projection_tolerance_percent": 10.0,
            "final_output_tolerance_percent": 5.0,
            "target_size_bytes": 300_000_000,
            "target_size_mb": 300.0,
            "target_runtime_minutes": 45.0,
        }
    }
    requested_size = {
        "request_type": "size_budget",
        "operator_confirmed": True,
        "budget_bytes": 300_000_000,
        "applied_policy": explicit_size_fragment,
        "request_text": "Target 300 MB per episode.",
    }
    return (
        AdvisorEvalCase(
            case_id="luna-note-size-budget",
            task=AdvisorTask.OPERATOR_NOTE_PARSE,
            model="gpt-5.6-luna",
            fallback_models=("gpt-5.6-terra",),
            payload={"operator_note": "Can you target 300MB per episode?"},
            expected_values=(
                ("request_type", "size_budget"),
                ("operator_confirmed", True),
                ("size_budget_value", 300),
                ("size_budget_unit", "mb"),
            ),
            required_nonempty=("summary", "reasoning_note"),
        ),
        AdvisorEvalCase(
            case_id="luna-note-exploratory",
            task=AdvisorTask.OPERATOR_NOTE_PARSE,
            model="gpt-5.6-luna",
            fallback_models=("gpt-5.6-terra",),
            payload={"operator_note": "I want to understand if 300MB per episode is realistic."},
            expected_values=(("intent_type", "exploratory_question"), ("operator_confirmed", False)),
            required_nonempty=("summary", "reasoning_note"),
        ),
        AdvisorEvalCase(
            case_id="terra-seed-size-first",
            task=AdvisorTask.SEED_POLICY,
            model="gpt-5.6-terra",
            fallback_models=("gpt-5.6-sol",),
            payload={
                "operator_note": "Target 300 MB per episode while keeping source resolution.",
                "base_policy": base_policy,
                "requested_experiment": requested_size,
                "runtime_toolbelt": {"decision_defaults": {"decision_model": "size_first_review"}},
            },
            expected_values=(
                ("ok", True),
                ("proposed_policy.video.target_size_bytes", 300_000_000),
                ("request_disposition", "honored"),
            ),
            required_nonempty=("summary", "diagnosis", "request_response"),
        ),
        AdvisorEvalCase(
            case_id="terra-seed-content-evidence",
            task=AdvisorTask.SEED_POLICY,
            model="gpt-5.6-terra",
            fallback_models=("gpt-5.6-sol",),
            payload={
                "operator_note": (
                    "Keep source resolution, visible grain, and surround presentation while making a careful "
                    "smaller first test from the measured content evidence."
                ),
                "base_policy": base_policy,
                "runtime_toolbelt": {
                    "cadence_decision": {
                        "status": "measured",
                        "classification": "mixed",
                        "confidence": 0.72,
                        "transform": "none",
                    },
                    "media_fingerprint_decision": {
                        "status": "measured",
                        "confidence": 0.84,
                        "traits": [
                            "likely_film_grain",
                            "dark_gradient_banding_risk",
                            "high_motion",
                            "animation_cues",
                        ],
                    },
                },
            },
            expected_values=(("ok", True),),
            required_nonempty=("summary", "diagnosis", "request_response"),
            forbidden_terms=("because it is old", "title implies", "genre proves"),
            preserve_surround_audio=True,
        ),
        AdvisorEvalCase(
            case_id="terra-tune-preserve-surround",
            task=AdvisorTask.NOTE_TUNING,
            model="gpt-5.6-terra",
            fallback_models=("gpt-5.6-sol",),
            payload={
                "operator_note": "Faces look soft. Keep the surround audio and try a slightly safer video pass.",
                "policy": base_policy,
                "requested_experiment": {
                    "request_type": "metric_target",
                    "operator_confirmed": True,
                    "metric": "vmaf",
                    "target": 88.0,
                    "applied_policy": {"video": {"target_vmaf": 88.0}},
                    "request_text": "Try a VMAF target of 88 and keep surround audio.",
                },
                "runtime_toolbelt": {
                    "audio_tradeoff_hint": {
                        "policy_key": "surround_5_1_opus_bitrate",
                        "recommended_seed_action": "hold",
                        "review_confidence": "low",
                        "review_risk_summary": "Preserve surround audio unless the operator explicitly trades it.",
                    }
                },
            },
            expected_values=(
                ("ok", True),
                ("proposed_policy.video.target_vmaf", 88.0),
                ("self_check.source", "deterministic"),
            ),
            expected_one_of=(("request_disposition", ("honored", "honored_with_risk")),),
            required_nonempty=("summary", "diagnosis", "request_response"),
            preserve_surround_audio=True,
        ),
        AdvisorEvalCase(
            case_id="terra-artifact-critique",
            task=AdvisorTask.REVIEW_ARTIFACT_CRITIQUE,
            model="gpt-5.6-terra",
            fallback_models=("gpt-5.6-sol",),
            payload=_artifact_payload(),
            expected_values=(("ok", True),),
            required_nonempty=("summary", "recommendation"),
            forbidden_terms=("audio sync damage", "motion breakup was observed"),
        ),
        AdvisorEvalCase(
            case_id="terra-tune-measured-retry",
            task=AdvisorTask.NOTE_TUNING,
            model="gpt-5.6-terra",
            fallback_models=("gpt-5.6-sol",),
            payload={
                "operator_note": (
                    "The measured draft missed the approved size band. Try one smaller measured retry, keep "
                    "source resolution, and do not spend surround quality."
                ),
                "policy": base_policy,
                "requested_experiment": {
                    "request_type": "size_budget",
                    "operator_confirmed": True,
                    "measured_size_followup": True,
                    "budget_bytes": 280_000_000,
                    "applied_policy": {
                        "video": {
                            "size_goal_schema_version": 1,
                            "size_goal_mode": "absolute",
                            "size_goal_source": "operator_note",
                            "target_size_bytes": 280_000_000,
                            "target_size_mb": 280.0,
                            "target_runtime_minutes": 45.0,
                            "sample_projection_tolerance_percent": 10.0,
                            "final_output_tolerance_percent": 5.0,
                            "resolution_intent_mode": "source",
                            "resolution_intent_source": "operator",
                            "max_height": 0,
                        }
                    },
                },
                "latest_failed_sample_job": {
                    "status": "failed",
                    "failure_kind": "target_size_needs_review",
                    "target_size_verification": {"status": "over_target"},
                },
                "runtime_toolbelt": {
                    "audio_tradeoff_hint": {
                        "policy_key": "surround_5_1_opus_bitrate",
                        "leverage": "low",
                        "recommended_seed_action": "hold",
                    }
                },
            },
            expected_values=(
                ("ok", True),
                ("proposed_policy.video.target_size_bytes", 280_000_000),
                ("proposed_policy.video.max_height", 0),
                ("self_check.source", "deterministic"),
            ),
            expected_one_of=(("request_disposition", ("honored", "honored_with_risk")),),
            required_nonempty=("summary", "diagnosis", "request_response"),
            preserve_surround_audio=True,
        ),
        AdvisorEvalCase(
            case_id="sol-seed-ambiguity",
            task=AdvisorTask.SEED_POLICY,
            model="gpt-5.6-sol",
            payload={
                "operator_note": "Keep the grain and surround presentation, but make a careful smaller first test.",
                "base_policy": base_policy,
                "runtime_toolbelt": {
                    "media_fingerprint_decision": {
                        "status": "measured",
                        "traits": ["likely_film_grain", "dark_gradient_banding_risk"],
                    }
                },
            },
            expected_values=(("ok", True),),
            required_nonempty=("summary", "diagnosis", "request_response"),
            forbidden_terms=("era alone", "title implies"),
            preserve_surround_audio=True,
        ),
        AdvisorEvalCase(
            case_id="sol-artifact-critique",
            task=AdvisorTask.REVIEW_ARTIFACT_CRITIQUE,
            model="gpt-5.6-sol",
            payload=_artifact_payload(),
            expected_values=(("ok", True),),
            required_nonempty=("summary", "recommendation"),
            forbidden_terms=("audio sync damage", "motion breakup was observed"),
        ),
        AdvisorEvalCase(
            case_id="deterministic-run-verdict",
            task="run_verdict",
            model=None,
            payload={
                "operator_request": {"request_type": "size_budget", "budget_bytes": 300_000_000},
                "sample_result": {"quality_score": 88.4, "quality_target": 85.0},
                "size_target_analysis": {"status": "inside_target_band"},
            },
            expected_values=(
                ("ok", True),
                ("outcome", "strong_match"),
                ("confidence", "high"),
            ),
            required_nonempty=("summary", "next_step"),
        ),
        AdvisorEvalCase(
            case_id="deterministic-nonpositive-video-budget",
            task="nonpositive_video_budget",
            model=None,
            payload={
                "requested_experiment": {
                    "request_type": "size_budget",
                    "stream_budget_ledger": {
                        "feasibility": {"status": "arithmetically_infeasible"},
                        "totals": {"remaining_video_bytes": 0},
                    },
                }
            },
            expected_values=(("blocked", True), ("source", "deterministic")),
        ),
    )


def run_recommended_evals(
        *,
        project_root: Path,
        command: str = "codex-lab",
        model_override: str | None = None,
        case_ids: set[str] | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for case in recommended_eval_cases():
        if case_ids and case.case_id not in case_ids:
            continue
        models = (
            (model_override,)
            if case.model is not None and model_override
            else (case.model, *case.fallback_models) if case.model is not None else ()
        )
        results.append(_run_eval_case(case, project_root=project_root, command=command, models=models))
    passed = sum(1 for result in results if result["passed"])
    pass_rate = passed / len(results) if results else 0.0
    fallback_case_count = sum(
        1
        for result in results
        if int(dict(result.get("telemetry") or {}).get("attempt_count") or 0) > 1
    )
    return {
        "schema_version": EVAL_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "suite": "recommended",
        "model_override": model_override,
        "summary": {
            "case_count": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": round(pass_rate, 4),
            "required_pass_rate": REQUIRED_PASS_RATE,
            "fallback_case_count": fallback_case_count,
            "all_passed": bool(results) and pass_rate >= REQUIRED_PASS_RATE,
        },
        "results": results,
    }


def _run_eval_case(
        case: AdvisorEvalCase,
        *,
        project_root: Path,
        command: str,
        models: tuple[str, ...],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mediaforce-advisor-eval-") as temp_dir:
        temp_root = Path(temp_dir)
        payload = json.loads(json.dumps(case.payload))
        if case.task == AdvisorTask.REVIEW_ARTIFACT_CRITIQUE:
            image_path = temp_root / "synthetic-review.png"
            _write_synthetic_review_png(image_path)
            payload["multimodal_review_pack"]["images"] = [str(image_path)]
        telemetry_path = temp_root / "telemetry.jsonl"
        if isinstance(case.task, AdvisorTask):
            assert models
            routing = advisor_routing_for_models(
                {case.task: models},
                command=command,
                telemetry_path=telemetry_path,
            )
            response = _run_model_case(
                case.task,
                project_root=project_root,
                payload=payload,
                routing=routing,
            )
        elif case.task == "run_verdict":
            response = request_run_verdict(project_root=project_root, payload=payload)
        elif case.task == "nonpositive_video_budget":
            response = {
                "blocked": has_nonpositive_video_budget(payload.get("requested_experiment")),
                "source": "deterministic",
            }
        else:
            raise ValueError(f"Unsupported deterministic eval task: {case.task}")
        public_response = _public_response(response)
        telemetry = _load_telemetry(telemetry_path)
    checks = _score_case(case, public_response, telemetry)
    return {
        "case_id": case.case_id,
        "task": case.task.value if isinstance(case.task, AdvisorTask) else case.task,
        "model": models[0] if models else None,
        "route_models": list(models),
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "telemetry": _telemetry_summary(telemetry),
    }


def _run_model_case(
        task: AdvisorTask,
        *,
        project_root: Path,
        payload: dict[str, Any],
        routing: AdvisorRouting,
) -> Any:
    if task == AdvisorTask.OPERATOR_NOTE_PARSE:
        return request_operator_note_parse(project_root=project_root, payload=payload, routing=routing)
    if task == AdvisorTask.SEED_POLICY:
        return request_seed_policy(project_root=project_root, payload=payload, routing=routing)
    if task == AdvisorTask.NOTE_TUNING:
        return request_note_tuning(project_root=project_root, payload=payload, routing=routing)
    if task == AdvisorTask.REVIEW_ARTIFACT_CRITIQUE:
        return request_review_artifact_critique(project_root=project_root, payload=payload, routing=routing)
    raise ValueError(f"Unsupported eval task: {task.value}")


def _public_response(response: Any) -> dict[str, Any]:
    if response is None:
        return {}
    if isinstance(response, dict):
        return response
    if is_dataclass(response) and not isinstance(response, type):
        payload = asdict(response)
        payload.pop("raw", None)
        return payload
    return {}


def _score_case(
        case: AdvisorEvalCase,
        response: dict[str, Any],
        telemetry: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for path, expected in case.expected_values:
        actual = _path_value(response, path)
        checks.append(_check(f"expected:{path}", actual == expected, f"expected {expected!r}, received {actual!r}"))
    for path, expected_values in case.expected_one_of:
        actual = _path_value(response, path)
        checks.append(
            _check(
                f"expected_one_of:{path}",
                actual in expected_values,
                f"expected one of {expected_values!r}, received {actual!r}",
            )
        )
    for path in case.required_nonempty:
        actual = _path_value(response, path)
        checks.append(_check(f"nonempty:{path}", bool(actual), f"received {actual!r}"))
    response_text = json.dumps(response, sort_keys=True).lower()
    for term in case.forbidden_terms:
        checks.append(
            _check(
                f"forbidden:{term}",
                term.lower() not in response_text,
                "forbidden unsupported claim was present",
            )
        )
    if case.preserve_surround_audio:
        bitrate = _path_value(response, "proposed_policy.audio.surround_5_1_opus_bitrate")
        checks.append(
            _check(
                "preserve:surround_5_1_opus_bitrate",
                bitrate in {None, "256k"},
                f"received {bitrate!r}",
            )
        )
    if case.model is not None:
        checks.append(_check("telemetry:recorded", bool(telemetry), "no telemetry record was produced"))
        checks.append(
            _check(
                "telemetry:valid",
                bool(telemetry) and bool(telemetry[-1].get("valid")),
                str(telemetry[-1].get("status") if telemetry else "missing"),
            )
        )
        usage = telemetry[-1].get("usage") if telemetry else {}
        checks.append(
            _check(
                "telemetry:usage",
                isinstance(usage, dict) and int(usage.get("input_tokens") or 0) > 0,
                "input token usage was unavailable",
            )
        )
    return checks


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": "" if passed else detail}


def _path_value(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for segment in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(segment)
    return value


def _load_telemetry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _telemetry_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "attempt_count": len(records),
        "models": [record.get("model") for record in records],
        "statuses": [record.get("status") for record in records],
        "latency_ms": sum(int(record.get("latency_ms") or 0) for record in records),
        "usage": {
            key: sum(int(dict(record.get("usage") or {}).get(key) or 0) for record in records)
            for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
        },
        "estimated_cost_usd": (
            round(sum(float(record.get("estimated_cost_usd") or 0) for record in records), 8)
            if records and all(record.get("estimated_cost_usd") is not None for record in records)
            else None
        ),
    }


def _base_policy() -> dict[str, Any]:
    return {
        "video": {
            "encoder": "libsvtav1",
            "pixel_format": "yuv420p10le",
            "preset": 4,
            "quality_metric": "vmaf",
            "target_vmaf": 85.0,
            "min_target_vmaf": 80.0,
            "target_xpsnr": 39.0,
            "min_target_xpsnr": 35.0,
            "sample_every": "8m",
            "sample_duration": "20s",
            "min_crf": 18,
            "max_crf": 38,
            "max_encoded_percent": 80,
            "target_size_bytes": 300_000_000,
            "target_size_mb": 300.0,
            "target_runtime_minutes": 45.0,
            "size_goal_schema_version": 1,
            "size_goal_mode": "normalized",
            "size_goal_source": "eval_fixture",
            "sample_projection_tolerance_percent": 10.0,
            "final_output_tolerance_percent": 5.0,
            "default_grain": 8,
            "grain_denoise": 0,
            "max_height": 0,
            "resolution_intent_mode": "source",
            "resolution_intent_source": "eval_fixture",
        },
        "audio": {
            "keep_languages": ["eng"],
            "copy_codecs": ["aac", "opus"],
            "convert_to_opus_codecs": ["ac3", "eac3", "dts", "truehd"],
            "stereo_opus_bitrate": "128k",
            "surround_5_1_opus_bitrate": "256k",
            "surround_7_1_opus_bitrate": "320k",
        },
        "subtitle": {
            "keep_languages": ["eng"],
            "prefer_text": True,
            "keep_forced": True,
            "default_mode": "first_english",
        },
    }


def _artifact_payload() -> dict[str, Any]:
    return {
        "operator_note": "Compare the synthetic dark gradient and edge detail without inferring motion or audio.",
        "runtime_toolbelt": {
            "review_media_context": {
                "review_media_ready": True,
                "moment_count": 1,
                "evidence_scope": "synthetic_fixture",
            }
        },
        "multimodal_review_pack": {
            "artifacts": [
                {
                    "label": "Synthetic dark gradient",
                    "detail": "Top half is the source-style reference; bottom half is the encoded-style reference.",
                }
            ],
            "images": [],
        },
    }


def _write_synthetic_review_png(path: Path) -> None:
    width = 96
    height = 64
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            smooth_gradient = int(12 + (x / max(1, width - 1)) * 80)
            reference_half = y < height // 2
            gradient = smooth_gradient if reference_half else (smooth_gradient // 8) * 8
            edge = (55 if reference_half else 34) if x in {31, 32, 63, 64} else 0
            checker_strength = 14 if reference_half else 6
            checker = checker_strength if (x // 8 + y // 8) % 2 == 0 else 0
            rows.extend((min(255, gradient + edge + checker), gradient, min(255, gradient + checker)))
    signature = b"\x89PNG\r\n\x1a\n"
    path.write_bytes(
        signature
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + _png_chunk(b"IEND", b"")
    )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the media-safe Mediaforce advisor evaluation suite.")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--command", default="codex-lab")
    parser.add_argument("--model", dest="model_override")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_recommended_evals(
        project_root=args.project_root.resolve(),
        command=args.command,
        model_override=args.model_override,
        case_ids=set(args.case_ids or []),
    )
    serialized = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{serialized}\n")
    else:
        print(serialized)
    return 0 if report["summary"]["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
