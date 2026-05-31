import hashlib
import json
import math
from collections.abc import Callable
import uuid
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from fastapi import HTTPException
from sqlalchemy import delete, or_, select, update

from mediaforce.core.config import MediaforceConfig, load_config
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import encode_jobs, library_items, staged_artifacts
from mediaforce.core.type_defs import float_value, object_dict, object_list
from mediaforce.encoding.encode_queue import ACTIVE_ENCODE_JOB_STATUSES, list_child_encode_jobs, \
    load_latest_terminal_encode_job_for_prefix
from mediaforce.encoding.staging import safe_unlink
from mediaforce.library.run_manifests import create_folder_manifest
from mediaforce.web.runtime.folder_tuning_helpers import (
    proposal_alignment_issue,
    size_budget_sample_analysis,
    size_budget_sample_issue,
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
AutoQueueApprovedFolderEncodeFn: TypeAlias = Callable[[str, str, bool], ActionPayload]
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


def _draft_raises_quality_target(current_policy: ActionPayload, draft_policy: ActionPayload) -> bool:
    current_video = object_dict(current_policy.get("video"))
    draft_video = object_dict(draft_policy.get("video"))
    for key in ("target_vmaf", "target_xpsnr"):
        current_value = _normalized_number(current_video.get(key))
        draft_value = _normalized_number(draft_video.get(key))
        if current_value is not None and draft_value is not None and draft_value > current_value + 0.01:
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


def queue_folder_encode_action(
        config: MediaforceConfig,
        normalized_prefix: str,
        notes: str,
        bypass_schedule: bool,
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
) -> ActionPayload:
    with open_db(config.paths.db_path) as connection:
        existing_job = load_job_state(connection, config, normalized_prefix)
        if existing_job and existing_job.get("status") in {"queued", "running", "pending_review"}:
            return {"ok": False, "message": "A calibration job is already active for this folder."}
        calibration = load_calibration_state(config, normalized_prefix)
        gate = review_gate(calibration)
        if not gate["can_confirm_full"]:
            raise HTTPException(status_code=400, detail=str(gate["message"]))
        if calibration is None:
            raise HTTPException(status_code=400, detail="Run a sampled calibration first.")
        saved_profile_path = config.paths.runtime_settings_path
        calibration_payload = object_dict(calibration)
        upsert_override(saved_profile_path, normalized_prefix, calibration_payload["policy"])
        active_encode_job = load_active_encode_job_for_prefix_fn(connection, normalized_prefix)
        if active_encode_job is not None:
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
                "message": f"A folder encode is already {active_status} for this folder.",
            }
        latest_encode_job = load_latest_terminal_encode_job_for_prefix(connection, normalized_prefix)
        if latest_encode_job is not None and str(latest_encode_job.get("status") or "") in {
            "needs_attention",
            "failed",
            "stopped",
        }:
            prepare_terminal_encode_job_for_requeue_fn(connection, latest_encode_job)
            _reset_stale_prefix_encoding_items_for_requeue(connection, config, normalized_prefix, now_iso=now_iso)
        refreshed_config = load_config(config.paths.config_path)
        manifest, manifest_path = create_folder_manifest(connection, refreshed_config, prefix=normalized_prefix)
        if not manifest["items"]:
            raise HTTPException(status_code=400, detail="No pending items were found to enqueue for this folder.")
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
    return {"ok": True, "message": "Queued the full folder encode.", "job": queue_job}


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
            statuses={"encoded", "validated"},
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
        manifest: ManifestPayload = {"items": items}
        promoted_paths = promote_manifest_items_fn(connection, config, manifest, list(range(len(items))), force=False)
    promoted_count = len(promoted_paths)
    file_label = "file" if promoted_count == 1 else "files"
    return {
        "ok": True,
        "message": f"Promoted {promoted_count} validated {file_label} into the library.",
        "promoted_count": promoted_count,
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
    descendant_pattern = _prefix_descendant_path_pattern(normalized_prefix)
    protected_prefixes = _active_descendant_encode_prefixes(connection, normalized_prefix)
    rows = connection.execute(
        select(library_items.c.id, library_items.c.rel_path)
        .where(
            or_(
                library_items.c.parent_dir == normalized_prefix,
                library_items.c.parent_dir.like(descendant_pattern, escape="\\"),
            )
        )
        .where(library_items.c.status == "encoding")
    ).mappings().fetchall()
    if not rows:
        return
    updated_at = now_iso()
    for row in rows:
        rel_path = str(row["rel_path"] or "").strip()
        if _rel_path_is_within_any_prefix(rel_path, protected_prefixes):
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


def _prefix_descendant_path_pattern(prefix: str) -> str:
    if not prefix:
        return "%"
    escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped_prefix}/%"


def _active_descendant_encode_prefixes(connection: DBClient, prefix: str) -> set[str]:
    if not prefix:
        return set()
    descendant_pattern = _prefix_descendant_path_pattern(prefix)
    rows = connection.execute(
        select(encode_jobs.c.prefix)
        .where(encode_jobs.c.status.in_(("queued", "retry_backoff", "running")))
        .where(
            or_(
                encode_jobs.c.prefix == prefix,
                encode_jobs.c.prefix.like(descendant_pattern, escape="\\"),
            )
        )
    ).fetchall()
    return {
        str(row[0]).strip().strip("/")
        for row in rows
        if str(row[0] or "").strip().strip("/") and str(row[0]).strip().strip("/") != prefix
    }


def _rel_path_is_within_any_prefix(rel_path: str, prefixes: set[str]) -> bool:
    normalized_rel_path = str(rel_path).strip().strip("/")
    for prefix in prefixes:
        if normalized_rel_path == prefix or normalized_rel_path.startswith(f"{prefix}/"):
            return True
    return False


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
        auto_queue_folder_encode: AutoQueueApprovedFolderEncodeFn | None = None,
        confirm_high_impact: bool = False,
        confirm_size_tradeoff: bool = False,
        reviewed_draft_hash: str = "",
) -> ActionPayload:
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
            and _draft_raises_quality_target(baseline_policy, object_dict(calibration_payload.get("policy")))
    ):
        size_issue = None
    if size_issue is not None and not confirm_size_tradeoff:
        raise HTTPException(status_code=409, detail=size_issue)
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
                merge_advice_state(
                    config,
                    normalized_prefix,
                    {
                        "approval_artifact": approval_artifact,
                        "operator_approved_at": calibration_payload["accepted_at"],
                        "operator_approved_size_tradeoff": bool(size_issue),
                    },
                )
    upsert_override(
        config.paths.runtime_settings_path,
        normalized_prefix,
        calibration_payload["policy"],
    )
    response: ActionPayload = {
        "ok": True,
        "queued": False,
        "auto_queue_status": "not_requested",
        "message": "Approved the current draft and saved it as the folder profile.",
    }
    if auto_queue_folder_encode is None:
        return response

    try:
        queue_result = auto_queue_folder_encode(normalized_prefix, "", False)
    except HTTPException as exc:
        detail = str(exc.detail)
        if detail == "No pending items were found to enqueue for this folder.":
            response["auto_queue_status"] = "no_pending"
            response["message"] = (
                "Approved the current draft and saved it as the folder profile. "
                "There were no pending items left to queue for this folder."
            )
            return response
        response["auto_queue_status"] = "blocked"
        response["queue_message"] = detail
        response["message"] = (
            "Approved the current draft and saved it as the folder profile. "
            f"Mediaforce could not auto-queue the folder encode: {detail}"
        )
        return response

    queue_message = str(queue_result.get("message") or "").strip()
    if queue_result.get("ok"):
        response.update(
            {
                "queued": True,
                "auto_queue_status": "queued",
                "message": "Approved the current draft, saved it as the folder profile, and queued the full folder encode.",
            }
        )
        if queue_message:
            response["queue_message"] = queue_message
        if queue_result.get("job") is not None:
            response["job"] = queue_result["job"]
        if queue_result.get("action") is not None:
            response["action"] = queue_result["action"]
        return response

    if queue_message.startswith("A folder encode is already "):
        response.update(
            {
                "queued": True,
                "auto_queue_status": "already_active",
                "queue_message": queue_message,
                "message": (
                    "Approved the current draft and saved it as the folder profile. "
                    f"{queue_message}"
                ),
            }
        )
        return response

    response.update(
        {
            "auto_queue_status": "blocked",
            "queue_message": queue_message,
            "message": (
                "Approved the current draft and saved it as the folder profile. "
                f"Mediaforce could not auto-queue the folder encode: {queue_message or 'unknown queue error'}"
            ),
        }
    )
    return response
