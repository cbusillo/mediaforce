import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
import uuid
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from fastapi import HTTPException
from sqlalchemy import delete, or_, select, update

from mediaforce.core.config import MediaforceConfig, load_config
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import encode_jobs, library_items, staged_artifacts
from mediaforce.core.type_defs import float_value, object_dict, object_list
from mediaforce.core.utils import filesystem_collision_key
from mediaforce.encoding.encode_queue import ACTIVE_ENCODE_JOB_STATUSES, list_child_encode_jobs, \
    load_latest_terminal_encode_job_for_prefix
from mediaforce.encoding.staging import safe_unlink
from mediaforce.library.media_scopes import MediaScope, path_matches_scope, resolve_media_scope, \
    scope_descendant_filter, scope_rel_path_filter
from mediaforce.library.movie_workflow import classify_movie_path, movie_item_included
from mediaforce.library.workflow_state import build_folder_workflow_state
from mediaforce.library.run_manifests import create_folder_manifest
from mediaforce.library.candidate_selection import encode_candidate_decisions, scope_lifecycle_payload_from_decisions, \
    workflow_eligibility
from mediaforce.tuning.quality_risk import build_quality_risk_contract
from mediaforce.tuning.quality_risk import append_quality_risk_record
from mediaforce.tuning.size_goals import operator_intent_from_policy
from mediaforce.web.runtime.folder_tuning_helpers import (
    proposal_alignment_issue,
    size_budget_sample_analysis,
    size_budget_sample_issue,
    video_quality_improvement_change,
)

ActionPayload: TypeAlias = dict[str, Any]
FolderItem: TypeAlias = dict[str, Any]
JobPayload: TypeAlias = dict[str, Any]
ManifestPayload: TypeAlias = dict[str, Any]


NowIsoFn: TypeAlias = Callable[[], str]
LoadJobStateFn: TypeAlias = Callable[[DBClient, MediaforceConfig, str], JobPayload | None]
LoadCalibrationStateFn: TypeAlias = Callable[[MediaforceConfig, str], ActionPayload | None]
ReviewGateFn: TypeAlias = Callable[[ActionPayload | None], ActionPayload]
UpsertOverrideFn: TypeAlias = Callable[[Path, str, ActionPayload], None]
LoadActiveEncodeJobFn: TypeAlias = Callable[[DBClient, str], JobPayload | None]
ClearTerminalEncodeJobsFn: TypeAlias = Callable[[DBClient, str], None]
PrepareTerminalEncodeJobForRequeueFn: TypeAlias = Callable[[DBClient, JobPayload], None]
SaveEncodeJobFn: TypeAlias = Callable[[DBClient, JobPayload], None]
CalibrationDraftHashFn: TypeAlias = Callable[[ActionPayload], str]
SaveCalibrationStateFn: TypeAlias = Callable[[MediaforceConfig, str, ActionPayload], None]
LoadAdviceStateFn: TypeAlias = Callable[[MediaforceConfig, str], ActionPayload | None]
MergeAdviceStateFn: TypeAlias = Callable[[MediaforceConfig, str, ActionPayload], ActionPayload]
LoadSampleItemFn: TypeAlias = Callable[[DBClient, MediaforceConfig, str], FolderItem | None]
QueueFolderEncodeActionFn: TypeAlias = Callable[[str, str, bool], ActionPayload]


def _calibration_policy_hash(payload: ActionPayload) -> str:
    policy_payload = object_dict(payload.get("policy"))
    encoded = json.dumps(policy_payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _normalized_number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _high_impact_policy_change(current_policy: ActionPayload, draft_policy: ActionPayload) -> bool:
    current_video = object_dict(current_policy.get("video"))
    draft_video = object_dict(draft_policy.get("video"))
    current_guardrail = (
        _normalized_text(current_video.get("quality_metric")),
        _normalized_number(current_video.get("target_vmaf")),
        _normalized_number(current_video.get("min_target_vmaf")),
        _normalized_number(current_video.get("target_xpsnr")),
        _normalized_number(current_video.get("min_target_xpsnr")),
    )
    draft_guardrail = (
        _normalized_text(draft_video.get("quality_metric")),
        _normalized_number(draft_video.get("target_vmaf")),
        _normalized_number(draft_video.get("min_target_vmaf")),
        _normalized_number(draft_video.get("target_xpsnr")),
        _normalized_number(draft_video.get("min_target_xpsnr")),
    )
    if current_guardrail != draft_guardrail:
        return True
    if _normalized_number(current_video.get("max_encoded_percent")) != _normalized_number(
            draft_video.get("max_encoded_percent")
    ):
        return True
    if _normalized_number(current_video.get("default_grain")) != _normalized_number(draft_video.get("default_grain")):
        return True
    return False


class LoadFolderStagedItemsFn(Protocol):
    def __call__(
            self,
            connection: DBClient,
            config: MediaforceConfig,
            normalized_prefix: str,
            *,
            statuses: set[str],
    ) -> list[FolderItem]:
        ...


class ValidateManifestItemsFn(Protocol):
    def __call__(
            self,
            connection: DBClient,
            config: MediaforceConfig,
            manifest: ManifestPayload,
            indexes: list[int],
    ) -> list[ActionPayload]:
        ...


class PromoteManifestItemsFn(Protocol):
    def __call__(
            self,
            connection: DBClient,
            config: MediaforceConfig,
            manifest: ManifestPayload,
            indexes: list[int],
            *,
            force: bool,
    ) -> list[Path]:
        ...


class RecordVisualApprovalArtifactFn(Protocol):
    def __call__(
            self,
            connection: DBClient,
            config: MediaforceConfig,
            *,
            prefix: str,
            note: str,
            sample_item: ActionPayload,
            calibration: ActionPayload,
            run_verdict: ActionPayload | None,
            created_at: str,
    ) -> ActionPayload | None:
        ...


def _no_active_encode_job(_connection: DBClient, _normalized_prefix: str) -> JobPayload | None:
    return None


def production_action_blocker(
        config: MediaforceConfig,
        normalized_prefix: str,
) -> ActionPayload | None:
    root = str(normalized_prefix or "").strip().strip("/").split("/", 1)[0]
    if not root:
        if not isinstance(config.media.get("libraries"), list):
            return None
        return {
            "ok": False,
            "code": "library_not_configured",
            "message": "Choose one configured library scope before running media actions.",
        }
    if root and root in config.source_root_map:
        return None
    library = config.library_definition_map.get(root)
    if library is None:
        return {
            "ok": False,
            "code": "library_not_configured",
            "message": "This scope is not part of a configured library root. Scan the library before running media actions.",
        }
    label = str(library.get("label") or root or "This library")
    availability = str(library.get("availability") or "browse_only").replace("_", " ")
    return {
        "ok": False,
        "code": "library_not_production",
        "message": (
            f"{label} is {availability}. Set its availability to Production before running media actions."
        ),
    }


def queue_folder_encode_action(
        config: MediaforceConfig,
        normalized_prefix: str,
        notes: str,
        bypass_schedule: bool,
        override_policy_holds: bool = False,
        *,
        now_iso: NowIsoFn,
        load_job_state: LoadJobStateFn,
        load_calibration_state: LoadCalibrationStateFn,
        review_gate: ReviewGateFn,
        upsert_override: UpsertOverrideFn,
        load_active_encode_job_for_prefix_fn: LoadActiveEncodeJobFn,
        clear_terminal_encode_jobs_for_prefix_fn: ClearTerminalEncodeJobsFn,
        prepare_terminal_encode_job_for_requeue_fn: PrepareTerminalEncodeJobForRequeueFn,
        save_encode_job: SaveEncodeJobFn,
        load_advice_state: LoadAdviceStateFn | None = None,
        load_latest_failed_target_size_job_state: LoadJobStateFn | None = None,
) -> ActionPayload:
    production_blocker = production_action_blocker(config, normalized_prefix)
    if production_blocker is not None:
        return production_blocker
    with open_db(config.paths.db_path) as connection:
        scope = resolve_media_scope(
            connection,
            normalized_prefix,
            library_types=config.library_type_map,
        )
        if override_policy_holds and scope.kind != "tv_season":
            raise HTTPException(status_code=400, detail="Lifecycle holds can only be overridden for one season at a time.")
        existing_job = load_job_state(connection, config, normalized_prefix)
        if existing_job and existing_job.get("status") in {"queued", "running", "pending_review"}:
            return {"ok": False, "message": "A calibration job is already active for this folder."}
        calibration = load_calibration_state(config, normalized_prefix)
        gate = review_gate(calibration)
        if not gate["can_confirm_full"]:
            raise HTTPException(status_code=400, detail=str(gate["message"]))
        if calibration is None:
            raise HTTPException(status_code=400, detail="Run a sampled calibration first.")
        calibration_payload = object_dict(calibration)
        latest_failed_sample_job = (
            load_latest_failed_target_size_job_state(connection, config, normalized_prefix)
            if load_latest_failed_target_size_job_state is not None
            else existing_job
        )
        failed_target_reason = _failed_target_size_job_blocking_reason(
            latest_failed_sample_job,
            calibration_payload,
        )
        if failed_target_reason is not None:
            raise HTTPException(status_code=409, detail=failed_target_reason)
        if load_advice_state is not None:
            advice_state = object_dict(load_advice_state(config, normalized_prefix))
            quality_risk_contract = build_quality_risk_contract(
                prefix=normalized_prefix,
                sample_item=object_dict(calibration_payload.get("sample_item")),
                current_policy=object_dict(calibration_payload.get("policy")),
                preview_policy=object_dict(calibration_payload.get("policy")),
                operator_request=object_dict(advice_state.get("operator_request")) or None,
                calibration=calibration_payload,
                advice_state=advice_state,
                latest_failed_sample_job=latest_failed_sample_job,
            )
            blocking_reason = _quality_risk_blocking_reason(quality_risk_contract)
            if blocking_reason is not None:
                raise HTTPException(status_code=409, detail=blocking_reason)
            operator_status = str(
                object_dict(quality_risk_contract.get("operator_decision")).get("status") or ""
            ).strip().lower()
            if operator_status == "rejected":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The current review evidence was rejected after approval. "
                        "Make and approve a revised test before starting the season."
                    ),
                )
        saved_profile_path = config.paths.runtime_settings_path
        calibration_policy = object_dict(calibration_payload.get("policy"))
        calibration_video = object_dict(calibration_policy.get("video"))
        if {"target_size_mb", "target_size_bytes", "size_goal_mode"} & calibration_video.keys():
            calibration_intent = operator_intent_from_policy(
                calibration_video,
                default_video_policy=object_dict(config.raw.get("video")),
                audio_policy=object_dict(calibration_policy.get("audio")),
                subtitle_policy=object_dict(calibration_policy.get("subtitle")),
            )
            if calibration_intent.size_goal.requires_confirmation:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Confirm whether the saved legacy size is runtime-normalized or an absolute per-episode "
                        "target before queueing production."
                    ),
                )
        active_encode_job = load_active_encode_job_for_prefix_fn(connection, normalized_prefix)
        if active_encode_job is not None:
            active_prefix = str(active_encode_job.get("prefix") or normalized_prefix).strip().strip("/")
            if active_prefix == normalized_prefix:
                requeue_blocker = _movie_requeue_policy_blocker(
                    connection,
                    config,
                    scope,
                    active_encode_job,
                )
                if requeue_blocker is not None:
                    raise HTTPException(status_code=409, detail=requeue_blocker)
                recovered = _recover_active_folder_encode_job(
                    connection,
                    active_encode_job,
                    notes=notes,
                    now_iso=now_iso,
                    prepare_terminal_encode_job_for_requeue_fn=prepare_terminal_encode_job_for_requeue_fn,
                    save_encode_job=save_encode_job,
                )
                if recovered is not None:
                    return recovered
            active_status = str(active_encode_job.get("status") or "queued").replace("_", " ")
            return {
                "ok": False,
                "message": f"A folder encode is already {active_status} for {active_prefix}.",
            }
        latest_encode_job = load_latest_terminal_encode_job_for_prefix(connection, normalized_prefix)
        if latest_encode_job is not None and str(latest_encode_job.get("status") or "") in {
            "needs_attention",
            "failed",
            "stopped",
        }:
            requeue_blocker = _movie_requeue_policy_blocker(
                connection,
                config,
                scope,
                latest_encode_job,
            )
            if requeue_blocker is not None:
                raise HTTPException(status_code=409, detail=requeue_blocker)
            prepare_terminal_encode_job_for_requeue_fn(connection, latest_encode_job)
            _reset_stale_prefix_encoding_items_for_requeue(connection, config, normalized_prefix, now_iso=now_iso)
        preflight_decisions = encode_candidate_decisions(
            connection,
            config,
            prefixes=[normalized_prefix],
        )
        if scope.domain == "movie" and not any(decision.eligible for decision in preflight_decisions):
            movie_blockers = list(dict.fromkeys(
                decision.production_blocker
                for decision in preflight_decisions
                if decision.production_blocker
            ))
            if movie_blockers:
                raise HTTPException(status_code=409, detail=movie_blockers[0])
            workflow_state = build_folder_workflow_state(
                connection,
                normalized_prefix,
                candidate_eligibility=workflow_eligibility(preflight_decisions),
                library_types=config.library_type_map,
            ).to_payload()
            next_action = object_dict(workflow_state.get("next_action"))
            action_label = str(next_action.get("label") or "No action")
            raise HTTPException(
                status_code=400,
                detail=f"No encode candidates were found for this movie scope. Next action: {action_label}.",
            )
        upsert_override(saved_profile_path, normalized_prefix, calibration_policy)
        refreshed_config = load_config(config.paths.config_path)
        manifest_kwargs: dict[str, Any] = {"prefix": normalized_prefix}
        if override_policy_holds:
            manifest_kwargs["manual_override_prefix"] = normalized_prefix
        manifest, manifest_path = create_folder_manifest(connection, refreshed_config, **manifest_kwargs)
        if not manifest["items"]:
            lifecycle_decisions = encode_candidate_decisions(
                connection,
                refreshed_config,
                prefixes=[normalized_prefix],
            )
            if scope.domain == "movie":
                movie_blockers = list(dict.fromkeys(
                    decision.production_blocker
                    for decision in lifecycle_decisions
                    if decision.production_blocker
                ))
                if movie_blockers:
                    raise HTTPException(status_code=409, detail=movie_blockers[0])
            else:
                lifecycle = scope_lifecycle_payload_from_decisions(normalized_prefix, lifecycle_decisions)
                if int(lifecycle.get("held_candidate_count") or 0) > 0:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"{lifecycle['held_candidate_count']} encode candidate(s) are protected by the "
                            "library lifecycle policy. Open one season to review or override its hold."
                        ),
                    )
            workflow_state = build_folder_workflow_state(
                connection,
                normalized_prefix,
                candidate_eligibility=workflow_eligibility(lifecycle_decisions),
                library_types=refreshed_config.library_type_map,
            ).to_payload()
            next_action = object_dict(workflow_state.get("next_action"))
            action_label = str(next_action.get("label") or "No action")
            raise HTTPException(
                status_code=400,
                detail=f"No encode candidates were found for this folder. Next action: {action_label}.",
            )
        clear_terminal_encode_jobs_for_prefix_fn(connection, normalized_prefix)
        created_at = now_iso()
        parent_job_id = uuid.uuid4().hex[:12]
        queue_job: JobPayload = {
            "job_id": parent_job_id,
            "prefix": normalized_prefix,
            "job_kind": "folder",
            "parent_job_id": None,
            "status": "queued",
            "manifest_path": str(manifest_path),
            "item_count": len(manifest["items"]),
            "saved_profile_path": str(saved_profile_path),
            "manifest_indexes": None,
            "host": {},
            "last_host": {},
            "notes": notes.strip(),
            "bypass_schedule": bypass_schedule,
            "process_pid": None,
            "error": None,
            "attempt_count": 0,
            "leased_at": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "worker_id": None,
            "retry_not_before": None,
            "waiting_reason": None,
            "terminal_reason": None,
            "last_failure_kind": None,
            "last_failure_at": None,
            "host_cooldown_until": None,
            "created_at": created_at,
            "started_at": None,
            "finished_at": None,
            "updated_at": created_at,
        }
        save_encode_job(connection, queue_job)
        for shard_indexes in _build_manifest_shards(refreshed_config, manifest):
            save_encode_job(
                connection,
                {
                    **queue_job,
                    "job_id": uuid.uuid4().hex[:12],
                    "job_kind": "shard",
                    "parent_job_id": parent_job_id,
                    "manifest_indexes": shard_indexes,
                    "item_count": len(shard_indexes),
                },
            )
    return {
        "ok": True,
        "message": "Queued the eligible folder encode.",
        "job": queue_job,
        "policy_holds_overridden": override_policy_holds,
    }


def approve_measured_encode_recovery_action(
        config: MediaforceConfig,
        normalized_prefix: str,
        *,
        now_iso: NowIsoFn,
        load_calibration_state: LoadCalibrationStateFn,
        calibration_draft_hash: CalibrationDraftHashFn,
        save_calibration_state: SaveCalibrationStateFn,
        review_gate: ReviewGateFn,
        upsert_override: UpsertOverrideFn,
        queue_folder_encode_action: QueueFolderEncodeActionFn,
) -> ActionPayload:
    calibration = load_calibration_state(config, normalized_prefix)
    gate = review_gate(calibration)
    if not bool(gate.get("can_confirm_full")):
        raise HTTPException(status_code=400, detail=str(gate.get("message") or "Approve a sampled draft first."))
    calibration_payload = object_dict(calibration)
    if not calibration_payload:
        raise HTTPException(status_code=400, detail="Run and approve a sampled draft first.")

    with open_db(config.paths.db_path) as connection:
        latest_encode_job = load_latest_terminal_encode_job_for_prefix(connection, normalized_prefix)
    if latest_encode_job is None:
        raise HTTPException(status_code=400, detail="No failed folder encode was found for this folder.")
    if str(latest_encode_job.get("status") or "") not in {"needs_attention", "failed", "stopped"}:
        raise HTTPException(status_code=400, detail="The latest folder encode is not waiting for recovery.")

    failure_analysis = object_dict(object_dict(latest_encode_job.get("progress")).get("failure_analysis"))
    recovery = _measured_recovery_policy(calibration_payload, failure_analysis)
    if recovery is None:
        raise HTTPException(
            status_code=400,
            detail="This failure does not have enough measured quality data for one-click recovery.",
        )

    calibration_payload["policy"] = recovery["policy"]
    calibration_payload["accepted_at"] = now_iso()
    calibration_payload["accepted_policy_hash"] = _calibration_policy_hash(calibration_payload)
    calibration_payload["accepted_draft_hash"] = calibration_draft_hash(calibration_payload)
    calibration_payload["accepted_recovery_note"] = recovery["summary"]
    save_calibration_state(config, normalized_prefix, calibration_payload)
    upsert_override(config.paths.runtime_settings_path, normalized_prefix, object_dict(calibration_payload["policy"]))

    queue_result = queue_folder_encode_action(normalized_prefix, recovery["summary"], False)
    return {
        **queue_result,
        "action": "approved_measured_recovery",
        "message": str(queue_result.get("message") or "Measured recovery was approved and queued."),
        "recovery": recovery["public"],
    }


def _measured_recovery_policy(
        calibration_payload: ActionPayload,
        failure_analysis: ActionPayload,
) -> ActionPayload | None:
    analyses = [object_dict(item) for item in object_list(failure_analysis.get("item_analyses"))]
    if not analyses and failure_analysis:
        analyses = [failure_analysis]
    analyses = [analysis for analysis in analyses if object_dict(analysis.get("best_candidate"))]
    if not analyses:
        return None

    policy = object_dict(calibration_payload.get("policy"))
    video = object_dict(policy.get("video"))
    metric = str(analyses[0].get("requested_metric") or video.get("quality_metric") or "vmaf").strip().lower()
    if metric not in {"vmaf", "xpsnr"}:
        return None
    target_key = "target_vmaf" if metric == "vmaf" else "target_xpsnr"
    min_key = "min_target_vmaf" if metric == "vmaf" else "min_target_xpsnr"

    scores: list[float] = []
    crfs: list[float] = []
    percents: list[float] = []
    rel_paths: list[str] = []
    for analysis in analyses:
        candidate = object_dict(analysis.get("best_candidate"))
        score = float_value(candidate.get("score"))
        crf = float_value(candidate.get("crf"))
        percent = float_value(candidate.get("predicted_encode_percent"))
        proposed_percent = float_value(analysis.get("proposed_max_encoded_percent"))
        min_score = float_value(analysis.get("min_score") or video.get(min_key))
        if score <= 0 or crf <= 0:
            return None
        if min_score > 0 and score < min_score:
            return None
        scores.append(score)
        crfs.append(crf)
        if percent > 0:
            percents.append(percent)
        if proposed_percent > 0:
            percents.append(proposed_percent)
        rel_path = str(analysis.get("item_rel_path") or "").strip()
        if rel_path:
            rel_paths.append(rel_path)

    if not scores or not crfs:
        return None

    current_target = float_value(video.get(target_key))
    current_min = float_value(video.get(min_key))
    current_cap = float_value(video.get("max_encoded_percent"))
    current_max_crf = float_value(video.get("max_crf"))

    measured_target = math.floor(min(scores) * 2.0) / 2.0
    if current_min > 0:
        measured_target = max(measured_target, current_min)
    if current_target > 0:
        measured_target = min(current_target, measured_target)

    measured_cap = current_cap if current_cap > 0 else 0.0
    if percents:
        measured_cap = max(measured_cap, float(math.ceil(max(percents))))
    measured_max_crf = max(current_max_crf, float(math.ceil(max(crfs))))

    updated_video = dict(video)
    updated_video[target_key] = measured_target
    updated_video["max_encoded_percent"] = int(measured_cap) if measured_cap.is_integer() else measured_cap
    updated_video["max_crf"] = int(measured_max_crf) if measured_max_crf.is_integer() else measured_max_crf
    updated_video["quality_metric"] = metric
    updated_policy = dict(policy)
    updated_policy["video"] = updated_video

    file_count = len(analyses)
    metric_label = metric.upper()
    summary = (
        f"Measured recovery for {file_count} failed file{'s' if file_count != 1 else ''}: "
        f"allow {metric_label} {measured_target:.1f}, cap {updated_video['max_encoded_percent']}%, "
        f"and CRF {updated_video['max_crf']}. Preserve completed staged encodes."
    )
    return {
        "policy": updated_policy,
        "summary": summary,
        "public": {
            "file_count": file_count,
            "item_rel_paths": rel_paths,
            "quality_metric": metric_label,
            "target_score": measured_target,
            "max_encoded_percent": updated_video["max_encoded_percent"],
            "max_crf": updated_video["max_crf"],
        },
    }


def validate_folder_outputs_action(
        config: MediaforceConfig,
        normalized_prefix: str,
        *,
        load_active_encode_job_for_prefix_fn: LoadActiveEncodeJobFn | None = None,
        load_folder_staged_items_fn: LoadFolderStagedItemsFn,
        validate_manifest_items_fn: ValidateManifestItemsFn,
) -> ActionPayload:
    production_blocker = production_action_blocker(config, normalized_prefix)
    if production_blocker is not None:
        return production_blocker
    if load_active_encode_job_for_prefix_fn is None:
        load_active_encode_job_for_prefix_fn = _no_active_encode_job
    with open_db(config.paths.db_path) as connection:
        active_encode_job = load_active_encode_job_for_prefix_fn(connection, normalized_prefix)
        encode_blocked = _validate_delivery_blocked_by_active_encode(active_encode_job)
        if encode_blocked is not None:
            return encode_blocked
        items = load_folder_staged_items_fn(
            connection,
            config,
            normalized_prefix,
            statuses={"encoded"},
        )
        if not items:
            return {
                "ok": False,
                "message": "No staged encoded files are ready to validate for this folder.",
            }
        inaccessible_response = _inaccessible_staged_item_response(
            items=items,
            action="validate",
            zero_count_key="validated_count",
        )
        if inaccessible_response is not None:
            return inaccessible_response
        manifest: ManifestPayload = {"items": items}
        results: list[ActionPayload] = []
        for index in range(len(items)):
            try:
                result = validate_manifest_items_fn(connection, config, manifest, [index])[0]
            except Exception as exc:
                result = {
                    "passed": False,
                    "error": str(exc),
                }
            results.append(object_dict(result))
    passed_count = sum(1 for result in results if object_dict(result).get("passed"))
    failed_count = len(results) - passed_count
    if failed_count:
        message = f"Validated {len(results)} files: {passed_count} passed, {failed_count} failed."
    else:
        message = f"Validated {passed_count} files. All staged outputs passed."
    return {
        "ok": True,
        "message": message,
        "validated_count": passed_count,
        "failed_count": failed_count,
        "item_count": len(results),
    }


def promote_folder_outputs_action(
        config: MediaforceConfig,
        normalized_prefix: str,
        *,
        load_active_encode_job_for_prefix_fn: LoadActiveEncodeJobFn | None = None,
        load_folder_staged_items_fn: LoadFolderStagedItemsFn,
        promote_manifest_items_fn: PromoteManifestItemsFn,
) -> ActionPayload:
    production_blocker = production_action_blocker(config, normalized_prefix)
    if production_blocker is not None:
        return production_blocker
    if load_active_encode_job_for_prefix_fn is None:
        load_active_encode_job_for_prefix_fn = _no_active_encode_job
    with open_db(config.paths.db_path) as connection:
        active_encode_job = load_active_encode_job_for_prefix_fn(connection, normalized_prefix)
        encode_blocked = _validate_delivery_blocked_by_active_encode(active_encode_job)
        if encode_blocked is not None:
            return encode_blocked
        items = load_folder_staged_items_fn(
            connection,
            config,
            normalized_prefix,
            statuses={"validated"},
        )
        if not items:
            return {
                "ok": False,
                "message": "No validated staged files are ready to promote for this folder.",
            }
        inaccessible_response = _inaccessible_staged_item_response(
            items=items,
            action="promote",
            zero_count_key="promoted_count",
        )
        if inaccessible_response is not None:
            return inaccessible_response
        promotion_conflict = _promotion_conflict_response(config, items)
        if promotion_conflict is not None:
            return promotion_conflict
        manifest: ManifestPayload = {"items": items}
        promoted_paths = promote_manifest_items_fn(connection, config, manifest, list(range(len(items))), force=False)
    promoted_count = len(promoted_paths)
    file_label = "file" if promoted_count == 1 else "files"
    return {
        "ok": True,
        "message": f"Promoted {promoted_count} validated {file_label} into the library.",
        "promoted_count": promoted_count,
    }


def _promotion_conflict_response(
        config: MediaforceConfig,
        items: list[FolderItem],
) -> ActionPayload | None:
    destinations: dict[str, tuple[Path, list[str]]] = {}
    destination_sources: list[tuple[str, Path, Path, str]] = []
    for item in items:
        source_value = str(item.get("source_path") or "").strip()
        if not source_value:
            continue
        source_path = Path(source_value)
        destination_path = source_path.with_suffix(f".{config.output_container.lstrip('.')}")
        rel_path = str(item.get("rel_path") or source_path)
        destination_key = filesystem_collision_key(destination_path)
        if destination_key not in destinations:
            destinations[destination_key] = (destination_path, [])
        destinations[destination_key][1].append(rel_path)
        destination_sources.append((destination_key, destination_path, source_path, rel_path))

    conflicts: list[ActionPayload] = []
    for destination_key, (destination_path, rel_paths) in destinations.items():
        if len(rel_paths) > 1:
            conflicts.append({
                "kind": "duplicate_destination",
                "destination_path": str(destination_path),
                "rel_paths": rel_paths,
            })
        for item_key, item_destination, source_path, rel_path in destination_sources:
            if item_key != destination_key:
                continue
            if item_destination != source_path and item_destination.exists():
                conflicts.append({
                    "kind": "destination_exists",
                    "destination_path": str(item_destination),
                    "rel_paths": [rel_path],
                })
    if not conflicts:
        return None
    return {
        "ok": False,
        "message": (
            f"Promotion is blocked by {len(conflicts)} destination conflict"
            f"{'s' if len(conflicts) != 1 else ''}. Review the affected files before replacing anything."
        ),
        "promoted_count": 0,
        "conflicts": conflicts,
    }


def _inaccessible_staged_item_response(
        *,
        items: list[FolderItem],
        action: str,
        zero_count_key: str,
) -> ActionPayload | None:
    inaccessible_items = [item for item in items if not Path(str(item.get("staging_path") or "")).exists()]
    if not inaccessible_items:
        return None
    inaccessible_hosts = sorted(
        {
            str(item.get("staging_host_label") or item.get("staging_host_key") or "").strip()
            for item in inaccessible_items
            if str(item.get("staging_host_label") or item.get("staging_host_key") or "").strip()
        }
    )
    host_copy = f" Encoded hosts: {', '.join(inaccessible_hosts)}." if inaccessible_hosts else ""
    response = {
        "ok": False,
        "message": (
            f"Cannot {action} this folder from the current web host because {len(inaccessible_items)} staged "
            f"file{'s are' if len(inaccessible_items) != 1 else ' is'} not accessible locally.{host_copy}"
        ),
        zero_count_key: 0,
        "failed_count": len(inaccessible_items),
        "item_count": len(items),
    }
    if zero_count_key == "promoted_count":
        response.pop("failed_count")
        response.pop("item_count")
    return response


def _build_manifest_shards(_config: MediaforceConfig, manifest: ManifestPayload) -> list[list[int]]:
    items = [object_dict(item) for item in object_list(manifest.get("items"))]
    if not items:
        return []
    return [[manifest_index] for manifest_index in range(len(items))]


def _recover_active_folder_encode_job(
        connection: DBClient,
        active_encode_job: JobPayload,
        *,
        notes: str,
        now_iso: NowIsoFn,
        prepare_terminal_encode_job_for_requeue_fn: PrepareTerminalEncodeJobForRequeueFn,
        save_encode_job: SaveEncodeJobFn,
) -> ActionPayload | None:
    if str(active_encode_job.get("job_kind") or "single") != "folder":
        return None
    recoverable_children: list[JobPayload] = []
    recoverable_indexes: list[int] = []
    for child in _folder_recoverable_children(connection, str(active_encode_job.get("job_id") or "")):
        child_indexes = [index for index in object_list(child.get("manifest_indexes")) if isinstance(index, int)]
        if not child_indexes:
            continue
        recoverable_children.append(child)
        recoverable_indexes.extend(child_indexes)
    recoverable_indexes = sorted(set(recoverable_indexes))
    if not recoverable_children or not recoverable_indexes:
        return None

    created_at = now_iso()
    for child in recoverable_children:
        prepare_terminal_encode_job_for_requeue_fn(connection, child)
        connection.execute(delete(encode_jobs).where(encode_jobs.c.job_id == str(child.get("job_id") or "")))

    recovery_notes = notes.strip() or str(active_encode_job.get("notes") or "").strip()
    for manifest_index in recoverable_indexes:
        save_encode_job(
            connection,
            {
                "job_id": uuid.uuid4().hex[:12],
                "prefix": str(active_encode_job.get("prefix") or ""),
                "job_kind": "shard",
                "parent_job_id": str(active_encode_job.get("job_id") or ""),
                "status": "queued",
                "manifest_path": str(active_encode_job.get("manifest_path") or ""),
                "manifest_indexes": [manifest_index],
                "item_count": 1,
                "saved_profile_path": active_encode_job.get("saved_profile_path"),
                "host": {},
                "last_host": {},
                "notes": recovery_notes,
                "bypass_schedule": bool(active_encode_job.get("bypass_schedule")),
                "process_pid": None,
                "error": None,
                "attempt_count": 0,
                "leased_at": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
                "worker_id": None,
                "retry_not_before": None,
                "waiting_reason": None,
                "terminal_reason": None,
                "last_failure_kind": None,
                "last_failure_at": None,
                "host_cooldown_until": None,
                "created_at": created_at,
                "started_at": None,
                "finished_at": None,
                "updated_at": created_at,
            },
        )

    file_label = "file" if len(recoverable_indexes) == 1 else "files"
    return {
        "ok": True,
        "action": "recovered",
        "message": f"Recovered {len(recoverable_indexes)} failed {file_label} back into the active folder encode.",
        "job": active_encode_job,
        "recovered_item_count": len(recoverable_indexes),
    }


def _folder_recoverable_children(connection: DBClient, parent_job_id: str) -> list[JobPayload]:
    if not parent_job_id:
        return []
    return [
        child
        for child in list_child_encode_jobs(connection, parent_job_id)
        if str(child.get("status") or "") in {"needs_attention", "failed", "stopped"}
    ]


def _movie_requeue_policy_blocker(
        connection: DBClient,
        config: MediaforceConfig,
        scope: MediaScope,
        job: JobPayload,
) -> str | None:
    if scope.domain != "movie":
        return None
    manifest_path = Path(str(job.get("manifest_path") or "").strip())
    if not str(manifest_path):
        return None
    try:
        manifest = object_dict(json.loads(manifest_path.read_text()))
    except (OSError, json.JSONDecodeError):
        return None
    items = [object_dict(item) for item in object_list(manifest.get("items"))]
    selected_indexes = [index for index in object_list(job.get("manifest_indexes")) if isinstance(index, int)]
    if str(job.get("job_kind") or "") == "folder":
        child_indexes = [
            index
            for child in list_child_encode_jobs(connection, str(job.get("job_id") or ""))
            if str(child.get("status") or "") != "completed"
            for index in object_list(child.get("manifest_indexes"))
            if isinstance(index, int)
        ]
        if child_indexes:
            selected_indexes = child_indexes
    if not selected_indexes:
        selected_indexes = list(range(len(items)))

    library = config.library_definition_map.get(scope.root, {})
    policy = object_dict(library.get("policy"))
    for index in sorted(set(selected_indexes)):
        if index < 0 or index >= len(items):
            continue
        rel_path = str(items[index].get("rel_path") or "").strip()
        membership = classify_movie_path(rel_path, root=scope.root)
        if membership is None:
            continue
        included, blocker = movie_item_included(
            membership,
            policy,
            explicit_exact=scope.match == "exact_item",
        )
        if not included:
            return (
                f"The saved retry includes {rel_path}, which is outside the current movie title policy. "
                f"{blocker or 'Open that exact movie file to retry it deliberately.'}"
            )
    return None


def _validate_delivery_blocked_by_active_encode(active_encode_job: JobPayload | None) -> ActionPayload | None:
    if not active_encode_job:
        return None
    status = str(active_encode_job.get("status") or "queued")
    if status not in ACTIVE_ENCODE_JOB_STATUSES:
        return None
    return {
        "ok": False,
        "message": f"Cannot deliver outputs while folder encode is {status.replace('_', ' ')}.",
    }


def _reset_stale_prefix_encoding_items_for_requeue(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        *,
        now_iso: NowIsoFn,
) -> None:
    normalized_prefix = str(prefix).strip().strip("/")
    if not normalized_prefix:
        return
    scope = resolve_media_scope(
        connection,
        normalized_prefix,
        library_types=config.library_type_map,
    )
    protected_prefixes = _active_descendant_encode_prefixes(connection, scope)
    rows = connection.execute(
        select(library_items.c.id, library_items.c.rel_path)
        .where(scope_rel_path_filter(library_items.c.rel_path, scope))
        .where(library_items.c.status == "encoding")
    ).mappings().fetchall()
    if not rows:
        return
    updated_at = now_iso()
    for row in rows:
        rel_path = str(row["rel_path"] or "").strip()
        if _rel_path_is_within_any_prefix(rel_path, protected_prefixes):
            continue
        if scope.domain == "movie":
            membership = classify_movie_path(rel_path, root=scope.root)
            library = config.library_definition_map.get(scope.root, {})
            if membership is None or not movie_item_included(
                    membership,
                    object_dict(library.get("policy")),
                    explicit_exact=scope.match == "exact_item",
            )[0]:
                continue
        if rel_path:
            output_suffix = str(object_dict(config.media).get("output_container") or "").strip()
            if output_suffix:
                output_suffix = f".{output_suffix.lstrip('.')}"
            else:
                output_suffix = Path(rel_path).suffix or ".mkv"
            staging_path = config.staging_root / Path(rel_path).with_suffix(output_suffix)
            safe_unlink(staging_path)
            safe_unlink(staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}"))
        connection.execute(
            delete(staged_artifacts)
            .where(staged_artifacts.c.library_item_id == row["id"])
            .where(staged_artifacts.c.promoted_at.is_(None))
        )
        connection.execute(
            update(library_items)
            .where(library_items.c.id == row["id"])
            .where(library_items.c.status == "encoding")
            .values(status="planned", updated_at=updated_at)
        )


def _active_descendant_encode_prefixes(connection: DBClient, scope: MediaScope) -> set[str]:
    if not scope.prefix or scope.match == "exact_item":
        return set()
    rows = connection.execute(
        select(encode_jobs.c.prefix)
        .where(encode_jobs.c.status.in_(("queued", "retry_backoff", "running")))
        .where(
            or_(
                encode_jobs.c.prefix == scope.prefix,
                scope_descendant_filter(encode_jobs.c.prefix, scope.prefix),
            )
        )
    ).fetchall()
    return {
        str(row[0]).strip().strip("/")
        for row in rows
        if str(row[0] or "").strip().strip("/") and str(row[0]).strip().strip("/") != scope.prefix
    }


def _rel_path_is_within_any_prefix(rel_path: str, prefixes: set[str]) -> bool:
    return any(path_matches_scope(rel_path, prefix) for prefix in prefixes)


def save_profile_action(
        config: MediaforceConfig,
        normalized_prefix: str,
        *,
        now_iso: NowIsoFn,
        load_sample_item: LoadSampleItemFn,
        load_calibration_state: LoadCalibrationStateFn,
        calibration_draft_hash: CalibrationDraftHashFn,
        save_calibration_state: SaveCalibrationStateFn,
        load_advice_state: LoadAdviceStateFn,
        record_visual_approval_artifact: RecordVisualApprovalArtifactFn,
        merge_advice_state: MergeAdviceStateFn,
        upsert_override: UpsertOverrideFn,
        confirm_high_impact: bool = False,
        confirm_size_tradeoff: bool = False,
        reviewed_draft_hash: str = "",
) -> ActionPayload:
    production_blocker = production_action_blocker(config, normalized_prefix)
    if production_blocker is not None:
        return production_blocker
    calibration = load_calibration_state(config, normalized_prefix)
    if not calibration:
        raise HTTPException(status_code=400, detail="No draft calibration found for this folder")
    calibration_payload = object_dict(calibration)
    current_draft_hash = str(calibration_payload.get("draft_hash") or calibration_draft_hash(calibration_payload)).strip()
    baseline_policy = object_dict(object_dict(calibration_payload.get("sample_item")).get("resolved_policy"))
    if not baseline_policy:
        with open_db(config.paths.db_path) as connection:
            sample_item = load_sample_item(connection, config, normalized_prefix)
        baseline_policy = object_dict(object_dict(sample_item).get("resolved_policy"))
    if baseline_policy and _high_impact_policy_change(
            baseline_policy,
            object_dict(calibration_payload.get("policy")),
    ):
        if not confirm_high_impact:
            raise HTTPException(
                status_code=409,
                detail="This draft includes high-impact policy changes. Review the diff, then confirm approval again.",
            )
        if reviewed_draft_hash.strip() != current_draft_hash:
            raise HTTPException(
                status_code=409,
                detail="This draft changed after the high-impact review. Review the diff and confirm approval again.",
            )
    advice_state = object_dict(load_advice_state(config, normalized_prefix))
    operator_request = object_dict(advice_state.get("operator_request"))
    request_disposition = str(advice_state.get("request_disposition") or "").strip().lower()
    if bool(operator_request.get("operator_confirmed")) and request_disposition in {"softened", "rejected", "unclear"}:
        raise HTTPException(
            status_code=409,
            detail=(
                "This draft does not carry the approved operator request forward. "
                "Run a fresh sample that follows the requested experiment before approving it."
            ),
        )
    size_target_analysis = size_budget_sample_analysis(
        operator_request=operator_request or None,
        calibration_payload=calibration_payload,
    )
    allow_measured_size_quality_tradeoff = (
            str(calibration_payload.get("action") or "").strip() == "ai_tune"
            and str(size_target_analysis.get("status") or "").strip() == "inside_target_band"
    )
    allow_measured_size_quality_increase = (
            str(calibration_payload.get("action") or "").strip() == "ai_tune"
            and str(size_target_analysis.get("status") or "").strip() == "under_target"
    )
    alignment_issue = proposal_alignment_issue(
        operator_request=operator_request or None,
        request_disposition=request_disposition or None,
        current_policy=baseline_policy,
        preview_policy=object_dict(calibration_payload.get("policy")),
        allow_measured_size_quality_tradeoff=allow_measured_size_quality_tradeoff,
        allow_measured_size_quality_increase=allow_measured_size_quality_increase,
    )
    if alignment_issue is not None:
        raise HTTPException(status_code=409, detail=alignment_issue)
    size_issue = size_budget_sample_issue(
        operator_request=operator_request or None,
        calibration_payload=calibration_payload,
    )
    if (
            allow_measured_size_quality_increase
            and str(size_target_analysis.get("status") or "").strip() == "under_target"
            and video_quality_improvement_change(
                object_dict(baseline_policy.get("video")),
                object_dict(object_dict(calibration_payload.get("policy")).get("video")),
            )
    ):
        size_issue = None
    if size_issue is not None:
        size_status = str(size_target_analysis.get("status") or "").strip()
        if size_status == "missing_prediction":
            raise HTTPException(status_code=409, detail=size_issue)
        if not confirm_size_tradeoff:
            raise HTTPException(status_code=409, detail=size_issue)
        if reviewed_draft_hash.strip() != current_draft_hash:
            raise HTTPException(
                status_code=409,
                detail="This draft changed after the size tradeoff review. Review the result and confirm approval again.",
            )
    quality_risk_contract = build_quality_risk_contract(
        prefix=normalized_prefix,
        sample_item=object_dict(calibration_payload.get("sample_item")),
        current_policy=object_dict(calibration_payload.get("policy")),
        preview_policy=object_dict(calibration_payload.get("policy")),
        operator_request=operator_request or None,
        calibration=calibration_payload,
        advice_state=advice_state,
    )
    blocking_reason = _quality_risk_blocking_reason(quality_risk_contract)
    if blocking_reason is not None:
        raise HTTPException(
            status_code=409,
            detail=blocking_reason,
        )
    if str(calibration_payload.get("mode") or "sample") == "sample":
        if not calibration_payload.get("review_media_ready"):
            raise HTTPException(
                status_code=400,
                detail="Run a fresh sample before approving because the review clips are unavailable.",
            )
        calibration_payload["accepted_at"] = now_iso()
        calibration_payload["accepted_draft_hash"] = current_draft_hash
        calibration_payload["accepted_policy_hash"] = _calibration_policy_hash(calibration_payload)
        calibration_payload["accepted_sample_job_id"] = str(calibration_payload.get("job_id") or "")
        save_calibration_state(config, normalized_prefix, calibration_payload)
        existing_approval = object_dict(advice_state.get("approval_artifact"))
        approval_artifact = (
            existing_approval
            if str(existing_approval.get("sample_job_id") or "") == str(calibration_payload.get("job_id") or "")
            else None
        )
        if str(existing_approval.get("sample_job_id") or "") != str(calibration_payload.get("job_id") or ""):
            with open_db(config.paths.db_path) as connection:
                approval_artifact = record_visual_approval_artifact(
                    connection,
                    config,
                    prefix=normalized_prefix,
                    note=str(advice_state.get("operator_note") or ""),
                    sample_item=object_dict(calibration_payload.get("sample_item")),
                    calibration=calibration_payload,
                    run_verdict=object_dict(advice_state.get("run_verdict")),
                    created_at=str(calibration_payload["accepted_at"]),
                )
        if approval_artifact is not None:
            approval_artifact["sample_job_id"] = str(calibration_payload.get("job_id") or "")
        source_scope = object_dict(quality_risk_contract.get("source_scope"))
        contract_policy = object_dict(quality_risk_contract.get("policy"))
        review_tags = [
            str(object_dict(risk).get("tag") or "")
            for risk in object_list(quality_risk_contract.get("typed_risks"))
            if str(object_dict(risk).get("tag") or "").strip()
        ]
        quality_risk_state = append_quality_risk_record(
            advice_state,
            prefix=normalized_prefix,
            source_id=str(source_scope.get("source_id") or ""),
            policy_hash=str(contract_policy.get("preview_policy_hash") or ""),
            sample_job_id=str(source_scope.get("sample_job_id") or "") or None,
            kind="post_test",
            verdict="approved",
            tags=review_tags or ["other"],
            details=str(
                object_dict(advice_state).get("operator_note")
                or "Operator approved this sample after reviewing the current evidence."
            ),
            created_at=str(calibration_payload["accepted_at"]),
            evidence_ids=[str(value) for value in object_list(source_scope.get("evidence_ids"))],
            moment_indexes=list(range(1, len(object_list(calibration_payload.get("review_moments"))) + 1)),
        )
        advice_patch: ActionPayload = {
            "operator_approved_at": calibration_payload["accepted_at"],
            "operator_approved_size_tradeoff": bool(size_issue),
            "quality_risk_records": object_list(quality_risk_state.get("quality_risk_records")),
        }
        if approval_artifact is not None:
            advice_patch["approval_artifact"] = approval_artifact
        merge_advice_state(config, normalized_prefix, advice_patch)
    upsert_override(
        config.paths.runtime_settings_path,
        normalized_prefix,
        calibration_payload["policy"],
    )
    return {
        "ok": True,
        "queued": False,
        "auto_queue_status": "approval_only",
        "message": (
            "Approved the current test and saved it as the folder profile. "
            "Choose Make the season when you are ready to start production."
        ),
    }


def _quality_risk_blocking_reason(contract: ActionPayload) -> str | None:
    gates = object_dict(contract.get("deterministic_gates"))
    if not bool(gates.get("blocked")):
        return None
    blocking_reasons = [
        str(reason)
        for reason in object_list(gates.get("blocking_reasons"))
        if str(reason).strip()
    ]
    return blocking_reasons[0] if blocking_reasons else "Measured review facts still block this action."


def _failed_target_size_job_blocking_reason(
        job: JobPayload | None,
        calibration: ActionPayload,
) -> str | None:
    job_payload = object_dict(job)
    if str(job_payload.get("status") or "") not in {"failed", "stopped"}:
        return None
    result = object_dict(job_payload.get("result"))
    trace = object_dict(result.get("target_size_trace"))
    target_status = str(result.get("target_size_status") or trace.get("status") or "").strip().lower()
    if target_status not in {"infeasible", "quality_conflict"}:
        return None
    accepted_at = _parse_state_timestamp(calibration.get("accepted_at"))
    failed_at = _parse_state_timestamp(
        job_payload.get("finished_at")
        or job_payload.get("updated_at")
        or job_payload.get("created_at")
    )
    if accepted_at and failed_at and failed_at < accepted_at:
        return None
    if target_status == "infeasible":
        return (
            "The latest size-directed test found that this target cannot fit the required streams. "
            "Choose a different size and approve a fresh test before starting production."
        )
    return (
        "The latest size-directed test could not reach this target without crossing the quality floor. "
        "Choose a different size or revise the quality decision, then approve a fresh test before starting production."
    )


def _parse_state_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
