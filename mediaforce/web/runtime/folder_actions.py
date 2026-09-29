import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
import uuid
from pathlib import Path
from typing import Any, cast, Protocol, TypeAlias

from fastapi import HTTPException
from sqlalchemy import delete, or_, select, update

from mediaforce.core.config import MediaforceConfig, load_config, with_folder_policy_override
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import encode_jobs, library_items, staged_artifacts
from mediaforce.core.evidence import stable_json_hash, stable_policy_hash, stable_source_id
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.core.utils import filesystem_collision_key
from mediaforce.encoding.encode_queue import ACTIVE_ENCODE_JOB_STATUSES, list_child_encode_jobs, \
    load_active_encode_jobs_for_prefix, load_latest_terminal_encode_job_for_prefix
from mediaforce.encoding.free_space import encode_reserve_preflight
from mediaforce.encoding.staging import partial_output_path
from mediaforce.execution import HeldFile, PromotionResult
from mediaforce.library.media_scopes import MediaScope, is_tv_season_prefix, path_matches_scope, resolve_media_scope, \
    scope_descendant_filter, scope_rel_path_filter
from mediaforce.library.movie_workflow import classify_movie_path, movie_item_included
from mediaforce.library.staged_integrity import IntegrityDisposition, StagedIntegrityReport, \
    integrity_disposition_blocks_promotion, \
    staged_integrity_report_for_scope
from mediaforce.library.workflow_state import build_folder_workflow_state
from mediaforce.library.run_manifests import create_folder_manifest, write_manifest
from mediaforce.library.candidate_selection import OlderSeasonOverrideSelection, encode_candidate_decisions, \
    older_season_candidate_item_ids, older_season_override_selection, project_candidates, \
    restrict_older_season_override_selection, scope_lifecycle_payload_from_decisions, scope_target_size_partition, \
    workflow_eligibility
from mediaforce.tuning.production_lineage import attach_target_lineage
from mediaforce.tuning.quality_risk import build_quality_risk_contract
from mediaforce.tuning.quality_risk import append_quality_risk_record
from mediaforce.tuning.compression_intent import CompressionEvidenceRef, authorize_compression_change, \
    compression_intent_from_item, compression_intent_from_policy
from mediaforce.tuning.content_intent_observations import record_visual_content_intent_observation
from mediaforce.tuning.calibration_jobs import resolve_pending_review_job
from mediaforce.tuning.size_goals import operator_intent_from_policy
from mediaforce.web.runtime.decision_evidence import CadenceSafetyPartition, cadence_queue_partition, \
    cadence_safety_partition, older_season_cadence_payload
from mediaforce.web.runtime.left_out_files import LeftOutFile, cadence_left_out_files, drop_manifest_items, \
    left_out_payload, left_out_summary, manifest_rel_paths, nothing_queued_response
from mediaforce.web.runtime.encode_runtime import remove_stale_staging_path
from mediaforce.web.runtime.folder_tuning_helpers import (
    allows_measured_size_quality_tradeoff,
    proposal_alignment_issue,
    size_budget_sample_analysis,
    size_budget_sample_issue,
    video_quality_improvement_change,
)
from mediaforce.web.runtime.host_runtime import host_config_for_key

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
ClearPendingProposalFn: TypeAlias = Callable[[MediaforceConfig, str], None]
LoadSampleItemFn: TypeAlias = Callable[[DBClient, MediaforceConfig, str], FolderItem | None]
QueueFolderEncodeActionFn: TypeAlias = Callable[[str, str, bool], ActionPayload]
ValidateScopeActionFn: TypeAlias = Callable[[DBClient, str], ActionPayload | None]

_PRODUCTION_APPROVAL_CONTRACT_SCHEMA_VERSION = 1
_FINAL_SIZE_RECOVERY_BLOCKER_CODE = "final_size_recovery_contract_unchanged"
_FINAL_SIZE_RECOVERY_BLOCKER_MESSAGE = (
    "The latest production encode missed its approved final-size target under the same reviewed settings. "
    "Run and approve a fresh representative sample with a changed size, compression, quality, resolution, "
    "or retained-stream contract before retrying."
)


def _calibration_policy_hash(payload: ActionPayload) -> str:
    policy_payload = object_dict(payload.get("policy"))
    encoded = json.dumps(policy_payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _production_approval_contract(calibration: ActionPayload) -> ActionPayload | None:
    sample_job_id = str(calibration.get("accepted_sample_job_id") or "").strip()
    policy_hash = str(calibration.get("accepted_policy_hash") or "").strip()
    sample_item = object_dict(calibration.get("sample_item"))
    operator_intent = object_dict(sample_item.get("resolved_operator_intent"))
    request = object_dict(operator_intent.get("request"))
    if not request:
        policy = object_dict(calibration.get("policy"))
        intent = operator_intent_from_policy(
            object_dict(policy.get("video")),
            audio_policy=object_dict(policy.get("audio")),
            subtitle_policy=object_dict(policy.get("subtitle")),
        )
        if intent.requires_confirmation:
            return None
        request = intent.request_payload()
    if not sample_job_id or not policy_hash or not request:
        return None
    return {
        "schema_version": _PRODUCTION_APPROVAL_CONTRACT_SCHEMA_VERSION,
        "sample_job_id": sample_job_id,
        "policy_hash": policy_hash,
        "operator_intent_hash": f"sha256:{stable_json_hash(request)}",
        "operator_intent": request,
    }


def _valid_production_approval_contract(payload: Mapping[str, Any] | None) -> ActionPayload | None:
    contract = object_dict(payload)
    request = object_dict(contract.get("operator_intent"))
    if (
            int_value(contract.get("schema_version")) != _PRODUCTION_APPROVAL_CONTRACT_SCHEMA_VERSION
            or not str(contract.get("sample_job_id") or "").strip()
            or not str(contract.get("policy_hash") or "").strip()
            or not request
    ):
        return None
    expected_hash = f"sha256:{stable_json_hash(request)}"
    if str(contract.get("operator_intent_hash") or "").strip() != expected_hash:
        return None
    return contract


def _terminal_production_approval_contract(job: JobPayload) -> ActionPayload | None:
    manifest_value = str(job.get("manifest_path") or "").strip()
    if not manifest_value:
        return None
    manifest_path = Path(manifest_value)
    try:
        manifest = object_dict(json.loads(manifest_path.read_text()))
    except (OSError, json.JSONDecodeError):
        return None
    selection = object_dict(manifest.get("selection"))
    return _valid_production_approval_contract(selection.get("production_approval_contract"))


def _legacy_final_size_goal_changed(
        job: JobPayload,
        current_contract: ActionPayload,
) -> bool:
    """Compare a fresh size goal with a pre-contract manifest's measured target."""
    failure_analysis = object_dict(object_dict(job.get("progress")).get("failure_analysis"))
    verification = object_dict(failure_analysis.get("target_size_verification"))
    previous_target_bytes = _normalized_number(verification.get("target_size_bytes"))
    request = object_dict(current_contract.get("operator_intent"))
    size_goal = object_dict(request.get("size_goal"))
    value_mb = _normalized_number(size_goal.get("value_mb"))
    if previous_target_bytes is None or value_mb is None:
        return False
    mode = str(size_goal.get("mode") or "").strip()
    if mode == "absolute":
        current_target_bytes = value_mb * 1_000_000
    elif mode == "normalized":
        reference_minutes = _normalized_number(size_goal.get("reference_runtime_minutes"))
        manifest_value = str(job.get("manifest_path") or "").strip()
        if reference_minutes is None or reference_minutes <= 0 or not manifest_value:
            return False
        try:
            manifest = object_dict(json.loads(Path(manifest_value).read_text()))
        except (OSError, json.JSONDecodeError):
            return False
        items = object_list(manifest.get("items"))
        if len(items) != 1:
            return False
        duration_seconds = _normalized_number(object_dict(items[0]).get("duration_seconds"))
        if duration_seconds is None or duration_seconds <= 0:
            return False
        current_target_bytes = value_mb * 1_000_000 * duration_seconds / (reference_minutes * 60)
    else:
        return False
    return not math.isclose(previous_target_bytes, current_target_bytes, rel_tol=1e-6, abs_tol=1.0)


def _final_size_requeue_contract_blocker(
        job: JobPayload | None,
        current_contract: ActionPayload | None,
) -> ActionPayload | None:
    job_payload = object_dict(job)
    failure_analysis = object_dict(object_dict(job_payload.get("progress")).get("failure_analysis"))
    if str(failure_analysis.get("kind") or "") != "final_size_target_miss":
        return None
    previous_contract = _terminal_production_approval_contract(job_payload)
    current = _valid_production_approval_contract(current_contract)
    changed_sample = bool(
        previous_contract
        and current
        and str(previous_contract.get("sample_job_id")) != str(current.get("sample_job_id"))
    )
    changed_intent = bool(
        previous_contract
        and current
        and str(previous_contract.get("operator_intent_hash")) != str(current.get("operator_intent_hash"))
    )
    if changed_sample and changed_intent:
        return None
    if previous_contract is None and current and _legacy_final_size_goal_changed(job_payload, current):
        return None
    return {
        "ok": False,
        "code": _FINAL_SIZE_RECOVERY_BLOCKER_CODE,
        "message": _FINAL_SIZE_RECOVERY_BLOCKER_MESSAGE,
        "retry_strategy": "fresh_goal_required",
        "queued_count": 0,
    }


def _final_size_miss_item_ids_by_index(job: JobPayload | None) -> dict[int, int]:
    """Library items that missed final size, by manifest index; empty when any miss cannot be placed."""
    job_payload = object_dict(job)
    failure_analysis = object_dict(object_dict(job_payload.get("progress")).get("failure_analysis"))
    analyses = [object_dict(item) for item in object_list(failure_analysis.get("item_analyses"))]
    if not analyses:
        analyses = [failure_analysis]
    miss_indexes: set[int] = set()
    for analysis in analyses:
        if str(analysis.get("kind") or "") != "final_size_target_miss":
            continue
        if "manifest_index" in analysis:
            indexes = [int_value(analysis.get("manifest_index"))]
        else:
            # A shard that missed as a whole cannot say which of its files missed; leave all of them out.
            indexes = [index for index in object_list(analysis.get("manifest_indexes")) if isinstance(index, int)]
        if not indexes or any(index < 0 for index in indexes):
            return {}
        miss_indexes.update(indexes)
    items = _manifest_items(job_payload)
    item_ids: dict[int, int] = {}
    for index in sorted(miss_indexes):
        item_id = int(items[index].get("library_item_id") or 0) if index < len(items) else 0
        if item_id <= 0:
            return {}
        item_ids[index] = item_id
    return item_ids


def _normalized_number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _folder_encode_queue_message(
        older_season_selection: OlderSeasonOverrideSelection | None,
        cadence_partition: CadenceSafetyPartition | None,
        *,
        queued_item_count: int,
        left_out: list[LeftOutFile],
) -> str:
    older_season_message = _older_season_queue_message(
        older_season_selection,
        cadence_partition,
        queued_item_count=queued_item_count,
    )
    if not left_out:
        return older_season_message or "Queued the eligible folder encode."
    left_out_message = f"Left {len(left_out)} out: {left_out_summary(left_out)}."
    if older_season_message:
        return f"{older_season_message} {left_out_message}"
    return f"Queued {queued_item_count} {'file' if queued_item_count == 1 else 'files'}. {left_out_message}"


def _older_season_queue_message(
        older_season_selection: OlderSeasonOverrideSelection | None,
        cadence_partition: CadenceSafetyPartition | None,
        *,
        queued_item_count: int,
) -> str | None:
    if older_season_selection is None:
        return None
    blocked_count = len(cadence_partition.blocked_item_ids) if cadence_partition is not None else 0
    evidence_required_count = (
        len(cadence_partition.evidence_required_item_ids)
        if cadence_partition is not None
        else 0
    )
    excluded_count = blocked_count + evidence_required_count
    if excluded_count == 0:
        return "Queued the older seasons."
    reasons: list[str] = []
    if blocked_count:
        reasons.append(
            f"{blocked_count} {'has' if blocked_count == 1 else 'have'} measured motion patterns "
            "that Mediaforce cannot convert automatically"
        )
    if evidence_required_count:
        reasons.append(
            f"{evidence_required_count} still {'needs' if evidence_required_count == 1 else 'need'} "
            "motion-pattern analysis"
        )
    return (
        f"Queued {queued_item_count} cadence-cleared older-season "
        f"{'episode' if queued_item_count == 1 else 'episodes'}. "
        f"Left {excluded_count} original: {'; '.join(reasons)}."
    )


def _target_size_left_out(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
) -> tuple[list[LeftOutFile], ActionPayload | None]:
    """Files whose size goal cannot fit stay out; the folder is refused only when no file is left to try."""
    blocked, any_left = scope_target_size_partition(connection, config, prefix)
    left_out = [
        LeftOutFile(file.item_id, file.rel_path, "target_size_infeasible", file.blocker.message)
        for file in blocked
    ]
    if not blocked or any_left:
        return left_out, None
    first = blocked[0].blocker
    messages = list(dict.fromkeys(file.reason for file in left_out))
    return left_out, {
        "ok": False,
        "code": first.code,
        "message": (
            messages[0]
            if len(messages) == 1
            else f"None of the {len(left_out)} files can fit the size goal. {' '.join(messages)}"
        ),
        "target_size_blocker": first.to_payload(),
        "left_out": left_out_payload(left_out),
    }


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
    if _normalized_number(current_video.get("target_search_max_crf")) != _normalized_number(
            draft_video.get("target_search_max_crf")
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
    ) -> PromotionResult:
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
        override_older_seasons: bool = False,
        older_seasons_confirmed: bool = False,
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
        validate_scope_action: ValidateScopeActionFn | None = None,
        reserve_preflight: Callable[..., Any] = encode_reserve_preflight,
) -> ActionPayload:
    production_blocker = production_action_blocker(config, normalized_prefix)
    if production_blocker is not None:
        return production_blocker
    with open_db(config.paths.db_path) as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        if validate_scope_action is not None:
            scope_blocker = validate_scope_action(connection, normalized_prefix)
            if scope_blocker is not None:
                return scope_blocker
        scope = resolve_media_scope(
            connection,
            normalized_prefix,
            library_types=config.library_type_map,
        )
        manual_override_prefix: str | None = None
        if override_policy_holds and override_older_seasons:
            raise HTTPException(status_code=400, detail="Choose one lifecycle override mode.")
        if override_policy_holds:
            if scope.kind == "tv_season":
                manual_override_prefix = normalized_prefix
            elif (
                    scope.domain == "tv"
                    and scope.kind == "media_file"
                    and scope.parent_prefix is not None
                    and is_tv_season_prefix(scope.parent_prefix, library_types=config.library_type_map)
            ):
                manual_override_prefix = scope.parent_prefix
            else:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Lifecycle holds can only be overridden for one season at a time, "
                        "either by selecting that season or one exact episode."
                    ),
                )
        if override_older_seasons and scope.kind != "tv_series":
            raise HTTPException(
                status_code=400,
                detail="Older-season holds can only be overridden for one TV series.",
            )
        if override_older_seasons and not older_seasons_confirmed:
            raise HTTPException(
                status_code=400,
                detail="Confirm the older-season selection before starting production.",
            )
        existing_job = load_job_state(connection, config, normalized_prefix)
        if existing_job and existing_job.get("status") in {"queued", "starting", "running"}:
            return {"ok": False, "message": "A calibration job is already active for this folder."}
        calibration = load_calibration_state(config, normalized_prefix)
        gate = review_gate(calibration)
        if not gate["can_confirm_full"]:
            raise HTTPException(status_code=400, detail=str(gate["message"]))
        if calibration is None:
            raise HTTPException(status_code=400, detail="Run a sampled calibration first.")
        calibration_payload = object_dict(calibration)
        calibration_policy = object_dict(calibration_payload.get("policy"))
        production_approval_contract = _production_approval_contract(calibration_payload)
        calibration_video = object_dict(calibration_policy.get("video"))
        calibration_intent = operator_intent_from_policy(
            calibration_video,
            default_video_policy=object_dict(config.raw.get("video")),
            audio_policy=object_dict(calibration_policy.get("audio")),
            subtitle_policy=object_dict(calibration_policy.get("subtitle")),
        )
        if {"target_size_mb", "target_size_bytes", "size_goal_mode"} & calibration_video.keys():
            if calibration_intent.size_goal.requires_confirmation:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Confirm whether the saved legacy size is runtime-normalized or an absolute per-episode "
                        "target before queueing production."
                    ),
                )
        calibration_sample_item = object_dict(calibration_payload.get("sample_item"))
        frozen_compression_intent = (
            compression_intent_from_item(calibration_sample_item)
            if calibration_sample_item
            else compression_intent_from_policy(calibration_video)
        )
        if frozen_compression_intent.requires_confirmation:
            raise HTTPException(
                status_code=400,
                detail="Choose and confirm a compression goal before queueing production.",
            )
        preflight_config = with_folder_policy_override(config, normalized_prefix, calibration_policy)
        left_out, target_size_refusal = _target_size_left_out(connection, preflight_config, normalized_prefix)
        if target_size_refusal is not None:
            return target_size_refusal
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
        advice_state: ActionPayload = {}
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
        active_encode_job = load_active_encode_job_for_prefix_fn(connection, normalized_prefix)
        if active_encode_job is not None:
            active_prefix = str(active_encode_job.get("prefix") or normalized_prefix).strip().strip("/")
            if active_prefix == normalized_prefix:
                recovery_plan = _folder_recovery_plan(connection, active_encode_job)
                recovery_left_out: list[LeftOutFile] = []
                if recovery_plan is not None:
                    recovery_plan, recovery_left_out = _partition_active_recovery_plan(
                        connection,
                        preflight_config,
                        normalized_prefix,
                        scope,
                        active_encode_job,
                        recovery_plan,
                    )
                    if recovery_plan is None:
                        return nothing_queued_response(recovery_left_out)
                recovered = _recover_active_folder_encode_job(
                    connection,
                    active_encode_job,
                    notes=notes,
                    now_iso=now_iso,
                    prepare_terminal_encode_job_for_requeue_fn=prepare_terminal_encode_job_for_requeue_fn,
                    save_encode_job=save_encode_job,
                    recovery_plan=recovery_plan,
                )
                if recovered is not None:
                    if recovery_left_out:
                        recovered["left_out"] = left_out_payload(recovery_left_out)
                        recovered["message"] = (
                            f"{recovered['message']} Left {len(recovery_left_out)} out: "
                            f"{left_out_summary(recovery_left_out)}."
                        )
                    return recovered
            active_status = str(active_encode_job.get("status") or "queued").replace("_", " ")
            return {
                "ok": False,
                "code": "encode_already_active",
                "message": f"A folder encode is already {active_status} for {active_prefix}.",
            }
        latest_encode_job = load_latest_terminal_encode_job_for_prefix(connection, normalized_prefix)
        terminal_job_needs_requeue = bool(
            latest_encode_job is not None and str(latest_encode_job.get("status") or "") in {
            "needs_attention",
            "failed",
            "stopped",
            }
        )
        retry_outside_policy: dict[int, LeftOutFile] = {}
        if terminal_job_needs_requeue and latest_encode_job is not None:
            # The fresh manifest below only selects files inside the current policy; name the ones it drops.
            retry_outside_policy = _movie_requeue_policy_left_out(connection, config, scope, latest_encode_job)
            left_out.extend(retry_outside_policy.values())
        preflight_decisions = encode_candidate_decisions(
            connection,
            preflight_config,
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
                library_types=preflight_config.library_type_map,
            ).to_payload()
            next_action = object_dict(workflow_state.get("next_action"))
            action_label = str(next_action.get("label") or "No action")
            raise HTTPException(
                status_code=400,
                detail=f"No encode candidates were found for this movie scope. Next action: {action_label}.",
            )
        manifest_kwargs: dict[str, Any] = {"prefix": normalized_prefix}
        older_season_selection = None
        older_season_cadence_partition: CadenceSafetyPartition | None = None
        if manual_override_prefix is not None:
            manifest_kwargs["manual_override_prefix"] = manual_override_prefix
        elif override_older_seasons:
            older_season_decisions = project_candidates(
                connection,
                preflight_config,
                prefixes=[normalized_prefix],
            )
            older_season_selection = older_season_override_selection(
                older_season_decisions,
                normalized_prefix,
            )
            if not older_season_selection.available:
                raise HTTPException(
                    status_code=409,
                    detail="No safe older-season candidates remain for this show.",
                )
            older_season_candidate_ids = older_season_candidate_item_ids(
                older_season_decisions,
                older_season_selection,
            )
            older_season_cadence_partition = cadence_safety_partition(
                connection,
                library_item_ids=older_season_candidate_ids,
                synchronize=True,
            )
            older_season_selection = restrict_older_season_override_selection(
                older_season_decisions,
                older_season_selection,
                included_item_ids=older_season_cadence_partition.cleared_item_ids,
            )
            if not older_season_selection.available:
                blocked_count = len(older_season_cadence_partition.blocked_item_ids)
                evidence_required_count = len(older_season_cadence_partition.evidence_required_item_ids)
                exclusion_reasons: list[str] = []
                if blocked_count:
                    exclusion_reasons.append(
                        f"{blocked_count} {'has' if blocked_count == 1 else 'have'} a measured motion pattern "
                        "Mediaforce cannot convert automatically"
                    )
                if evidence_required_count:
                    exclusion_reasons.append(
                        f"{evidence_required_count} still "
                        f"{'needs' if evidence_required_count == 1 else 'need'} motion-pattern analysis"
                    )
                exclusion_detail = " and ".join(exclusion_reasons)
                return {
                    "ok": False,
                    "code": "cadence_no_cleared_older_seasons",
                    "message": (
                        "No older-season episodes are currently cleared for safe production. "
                        f"{exclusion_detail[:1].upper()}{exclusion_detail[1:]}. No files were queued."
                    ),
                    "affected_item_count": blocked_count + evidence_required_count,
                    "next_route": "/ops",
                    "next_action_label": "Open Activity",
                }
            manifest_kwargs["older_season_override"] = older_season_selection
            manifest_kwargs["include_library_item_ids"] = older_season_cadence_partition.cleared_item_ids
        preview_transaction = None
        if terminal_job_needs_requeue:
            stale_rows = _stale_prefix_encoding_rows_for_requeue(
                connection,
                config,
                normalized_prefix,
            )
            if stale_rows:
                preview_transaction = connection.begin_nested()
                connection.execute(
                    update(library_items)
                    .where(library_items.c.id.in_([int(row["id"]) for row in stale_rows]))
                    .values(status="planned")
                )
        try:
            manifest, manifest_path = create_folder_manifest(
                connection,
                preflight_config,
                prepare_only=True,
                target_provenance_config=config,
                **manifest_kwargs,
            )
        finally:
            if preview_transaction is not None:
                preview_transaction.rollback()
        provenance_blocked = [
            (item, blocker)
            for item in manifest["items"]
            if (blocker := object_dict(object_dict(item.get("target_size_provenance")).get("blocker")))
        ]
        if provenance_blocked and len(provenance_blocked) == len(manifest["items"]):
            first_blocker = provenance_blocked[0][1]
            messages = list(dict.fromkeys(str(blocker["message"]) for _item, blocker in provenance_blocked))
            return {
                "ok": False,
                "code": first_blocker["code"],
                "message": " ".join(messages),
                "target_size_provenance_blocker": first_blocker,
            }
        left_out.extend(
            LeftOutFile(
                int(item.get("library_item_id") or 0),
                str(item.get("rel_path") or ""),
                "target_size_provenance",
                str(blocker["message"]),
            )
            for item, blocker in provenance_blocked
        )
        if not manifest["items"]:
            if left_out:
                return nothing_queued_response(left_out)
            if older_season_selection is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The safe older-season selection changed before it could be queued. "
                        "Reload the show and review it again."
                    ),
                )
            lifecycle_decisions = encode_candidate_decisions(
                connection,
                preflight_config,
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
                library_types=preflight_config.library_type_map,
            ).to_payload()
            next_action = object_dict(workflow_state.get("next_action"))
            action_label = str(next_action.get("label") or "No action")
            raise HTTPException(
                status_code=400,
                detail=f"No encode candidates were found for this folder. Next action: {action_label}.",
            )
        final_size_requeue_blocker = _final_size_requeue_contract_blocker(
            latest_encode_job,
            production_approval_contract,
        )
        final_size_miss_indexes: dict[int, int] = {}
        if final_size_requeue_blocker is not None:
            final_size_miss_indexes = _final_size_miss_item_ids_by_index(latest_encode_job)
            if not final_size_miss_indexes:
                return final_size_requeue_blocker
            rel_paths = {
                int(item.get("library_item_id") or 0): str(item.get("rel_path") or "")
                for item in _manifest_items(object_dict(latest_encode_job))
            }
            left_out.extend(
                LeftOutFile(
                    item_id,
                    rel_paths.get(item_id, ""),
                    _FINAL_SIZE_RECOVERY_BLOCKER_CODE,
                    (
                        "Missed its approved final size under the same reviewed settings. "
                        "Approve a fresh test with a changed goal before retrying it."
                    ),
                )
                for item_id in sorted(set(final_size_miss_indexes.values()))
            )
        if production_approval_contract is not None:
            selection = object_dict(manifest.get("selection"))
            selection["production_approval_contract"] = production_approval_contract
            manifest["selection"] = selection
        if older_season_selection is not None and older_season_cadence_partition is not None:
            manifest_item_ids = {
                int(item.get("library_item_id") or 0)
                for item in manifest["items"]
                if int(item.get("library_item_id") or 0) > 0
            }
            if (
                    len(manifest["items"]) != older_season_selection.candidate_count
                    or manifest_item_ids != older_season_cadence_partition.cleared_item_ids
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The cadence-cleared older-season selection changed before it could be queued. "
                        "Reload the show and review it again."
                    ),
                )
            manifest["selection"]["cadence_safety"] = {
                "schema_version": 1,
                "mode": "older_season_override",
                "cleared_item_count": len(manifest["items"]),
                "blocked_item_count": len(older_season_cadence_partition.blocked_item_ids),
                "evidence_required_item_count": len(
                    older_season_cadence_partition.evidence_required_item_ids
                ),
                "excluded_item_count": len(older_season_cadence_partition.excluded_item_ids),
                "excluded_library_item_ids": sorted(older_season_cadence_partition.excluded_item_ids),
            }
        drop_manifest_items(manifest, {file.library_item_id for file in left_out})
        cadence_partition = cadence_queue_partition(
            connection,
            preflight_config,
            normalized_prefix,
            library_item_ids=[
                int(item.get("library_item_id") or 0)
                for item in manifest["items"]
            ],
            work_reason="encode_safety",
            synchronize=older_season_cadence_partition is None,
        )
        left_out.extend(cadence_left_out_files(cadence_partition, manifest_rel_paths(manifest)))
        drop_manifest_items(manifest, {file.library_item_id for file in left_out})
        if not manifest["items"]:
            return nothing_queued_response(left_out, evidence_work=cadence_partition.evidence_work)
        reserve = reserve_preflight(preflight_config, manifest["items"])
        if not reserve.allowed:
            return {
                "ok": False,
                "code": "free_space_reserve",
                "message": str(reserve.waiting_reason or "Waiting for a measurable free-space reserve."),
                "queued_count": 0,
            }
        if terminal_job_needs_requeue and latest_encode_job is not None:
            _prepare_terminal_job_except(
                connection,
                latest_encode_job,
                set(retry_outside_policy) | set(final_size_miss_indexes),
                prepare_terminal_encode_job_for_requeue_fn,
            )
            _reset_stale_prefix_encoding_items_for_requeue(connection, config, normalized_prefix, now_iso=now_iso)
        attach_target_lineage(
            connection, manifest=manifest, calibration=calibration_payload,
            advice_state=advice_state, approval_contract=production_approval_contract,
        )
        saved_profile_path = config.paths.runtime_settings_path
        upsert_override(saved_profile_path, normalized_prefix, calibration_policy)
        refreshed_config = load_config(config.paths.config_path)
        if manifest_path is None:
            manifest_path = write_manifest(connection, refreshed_config, manifest)
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
        "message": _folder_encode_queue_message(
            older_season_selection,
            older_season_cadence_partition,
            queued_item_count=int(queue_job["item_count"]),
            left_out=left_out,
        ),
        "job": queue_job,
        "left_out": left_out_payload(left_out),
        "policy_holds_overridden": bool(
            override_policy_holds
            or (
                older_season_selection is not None
                and older_season_selection.overridden_season_prefixes
            )
        ),
        "older_season_override": (
            older_season_cadence_payload(older_season_selection, older_season_cadence_partition)
            if older_season_selection is not None and older_season_cadence_partition is not None
            else None
        ),
    }


# noinspection PyShadowingNames
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
    manifest_items = _manifest_items(latest_encode_job)
    legacy_recovery_paths = _legacy_measured_recovery_paths(failure_analysis, manifest_items)
    if legacy_recovery_paths:
        raise HTTPException(
            status_code=400,
            detail=(
                "This failed encode predates the compression-goal contract. Choose and confirm a compression goal, "
                "then run a fresh representative test before approving a larger item-local recovery."
            ),
        )
    item_recoveries = _measured_item_recovery_policies(
        failure_analysis,
        manifest_items=manifest_items,
        job_id=str(latest_encode_job.get("job_id") or "").strip() or None,
    )
    if not item_recoveries:
        raise HTTPException(
            status_code=400,
            detail="This failure does not have enough measured quality data for one-click recovery.",
        )
    preflight_config = config
    for item_recovery in item_recoveries:
        preflight_config = with_folder_policy_override(
            preflight_config,
            str(item_recovery["rel_path"]),
            object_dict(item_recovery.get("policy")),
        )
    with open_db(config.paths.db_path) as connection:
        _target_size_files, target_size_refusal = _target_size_left_out(
            connection,
            preflight_config,
            normalized_prefix,
        )
    if target_size_refusal is not None:
        return target_size_refusal
    recovery_indexes = [
        index
        for index in object_list(failure_analysis.get("manifest_indexes"))
        if isinstance(index, int)
    ]
    recovery_item_ids = _manifest_library_item_ids(latest_encode_job, recovery_indexes)
    if recovery_indexes and len(recovery_item_ids) != len(set(recovery_indexes)):
        return {
            "ok": False,
            "message": (
                "The failed encode manifest does not identify every media item. "
                "Prepare the folder again before approving recovery."
            ),
        }
    with open_db(config.paths.db_path) as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        cadence_partition = cadence_queue_partition(
            connection,
            preflight_config,
            normalized_prefix,
            library_item_ids=recovery_item_ids,
            work_reason="encode_safety",
        )
        if recovery_item_ids and not cadence_partition.cleared_item_ids:
            return nothing_queued_response(
                cadence_left_out_files(
                    cadence_partition,
                    {
                        int(item.get("library_item_id") or 0): str(item.get("rel_path") or "")
                        for item in manifest_items
                    },
                ),
                evidence_work=cadence_partition.evidence_work,
            )

    calibration_payload["accepted_at"] = now_iso()
    calibration_payload["accepted_policy_hash"] = _calibration_policy_hash(calibration_payload)
    calibration_payload["accepted_draft_hash"] = calibration_draft_hash(calibration_payload)
    recovery_summary = (
        f"Measured item-local recovery for {len(item_recoveries)} failed "
        f"file{'s' if len(item_recoveries) != 1 else ''}. Preserve completed staged encodes."
    )
    calibration_payload["accepted_recovery_note"] = recovery_summary
    calibration_payload["accepted_item_recoveries"] = [
        {
            "rel_path": item_recovery["rel_path"],
            "summary": item_recovery["summary"],
            "policy": item_recovery["policy"],
            "compression_escalation": item_recovery["compression_escalation"],
        }
        for item_recovery in item_recoveries
    ]
    save_calibration_state(config, normalized_prefix, calibration_payload)
    for item_recovery in item_recoveries:
        upsert_override(
            config.paths.runtime_settings_path,
            str(item_recovery["rel_path"]),
            object_dict(item_recovery["policy"]),
        )

    queue_result = queue_folder_encode_action(normalized_prefix, recovery_summary, False)
    return {
        **queue_result,
        "action": "approved_measured_recovery",
        "message": str(queue_result.get("message") or "Measured recovery was approved and queued."),
        "recovery": {
            "scope": "item",
            "file_count": len(item_recoveries),
            "items": [
                {"rel_path": item_recovery["rel_path"], **object_dict(item_recovery["public"])}
                for item_recovery in item_recoveries
            ],
        },
    }


def _measured_item_recovery_policies(
        failure_analysis: ActionPayload,
        *,
        manifest_items: list[ActionPayload],
        job_id: str | None,
) -> list[ActionPayload]:
    analyses = [object_dict(item) for item in object_list(failure_analysis.get("item_analyses"))]
    if not analyses and failure_analysis:
        analyses = [failure_analysis]
    recoveries: list[ActionPayload] = []
    for analysis in analyses:
        analysis_rel_path = str(analysis.get("item_rel_path") or "").strip().strip("/")
        if not analysis_rel_path:
            return []
        if "manifest_index" not in analysis:
            return []
        manifest_index = int_value(analysis.get("manifest_index"))
        if manifest_index < 0 or manifest_index >= len(manifest_items):
            return []
        manifest_item = manifest_items[manifest_index]
        manifest_rel_path = str(manifest_item.get("rel_path") or "").strip().strip("/")
        if not manifest_rel_path or manifest_rel_path != analysis_rel_path:
            return []
        recovery = _measured_recovery_policy(
            object_dict(manifest_item.get("resolved_policy")),
            analysis,
        )
        if recovery is None:
            return []
        compression_escalation = _measured_recovery_authorization(
            manifest_item,
            analysis=analysis,
            recovery_policy=object_dict(recovery.get("policy")),
            job_id=job_id,
        )
        if compression_escalation is None:
            return []
        recoveries.append({
            "rel_path": manifest_rel_path,
            **recovery,
            "compression_escalation": compression_escalation,
        })
    return recoveries


def _legacy_measured_recovery_paths(
        failure_analysis: ActionPayload,
        manifest_items: list[ActionPayload],
) -> list[str]:
    analyses = [object_dict(item) for item in object_list(failure_analysis.get("item_analyses"))]
    if not analyses and failure_analysis:
        analyses = [failure_analysis]
    paths: list[str] = []
    for analysis in analyses:
        if "manifest_index" not in analysis:
            continue
        manifest_index = int_value(analysis.get("manifest_index"))
        if manifest_index < 0 or manifest_index >= len(manifest_items):
            continue
        if compression_intent_from_item(manifest_items[manifest_index]).requires_confirmation:
            paths.append(str(analysis.get("item_rel_path") or "").strip())
    return [path for path in paths if path]


def _measured_recovery_authorization(
        manifest_item: ActionPayload,
        *,
        analysis: ActionPayload,
        recovery_policy: ActionPayload,
        job_id: str | None,
) -> ActionPayload | None:
    intent = compression_intent_from_item(manifest_item)
    source_size_bytes = int_value(
        manifest_item.get("source_size_bytes", manifest_item.get("size_bytes"))
    )
    if intent.requires_confirmation or source_size_bytes <= 0:
        return None
    base_policy = object_dict(manifest_item.get("resolved_policy"))
    if not base_policy:
        return None
    current_cap = float_value(object_dict(base_policy.get("video")).get("max_encoded_percent"))
    proposed_cap = float_value(object_dict(recovery_policy.get("video")).get("max_encoded_percent"))
    if current_cap <= 0 or proposed_cap <= 0:
        return None
    source_id = stable_source_id(manifest_item)
    policy_hash = stable_policy_hash(base_policy)
    anchor_bytes = round(source_size_bytes * current_cap / 100.0)
    candidate_bytes = round(source_size_bytes * proposed_cap / 100.0)
    evidence_identity = {
        "kind": "operator_override",
        "source_id": source_id,
        "policy_hash": policy_hash,
        "intent_id": intent.semantic_id,
        "job_id": job_id,
        "analysis": analysis,
        "anchor_bytes": anchor_bytes,
        "candidate_bytes": candidate_bytes,
    }
    evidence = CompressionEvidenceRef(
        kind="operator_override",
        evidence_id=f"ce1_{stable_json_hash(evidence_identity)[:32]}",
        intent_id=intent.semantic_id,
        observed_bytes=candidate_bytes,
        source_id=source_id,
        policy_hash=policy_hash,
        job_id=job_id,
    )
    decision = authorize_compression_change(
        intent,
        authoritative_anchor_bytes=anchor_bytes,
        candidate_bytes=candidate_bytes,
        evidence=(evidence,),
        source_id=source_id,
        policy_hash=policy_hash,
        job_id=job_id,
    )
    if decision.outcome != "authorized":
        return None
    return {
        "schema_version": 1,
        "scope": "item",
        "source_id": source_id,
        "policy_hash": policy_hash,
        "intent_id": intent.semantic_id,
        "job_id": job_id,
        "evidence": evidence.to_payload(),
        "decision": decision.to_payload(),
    }


def _measured_recovery_policy(
        base_policy: ActionPayload,
        failure_analysis: ActionPayload,
) -> ActionPayload | None:
    analyses = [object_dict(item) for item in object_list(failure_analysis.get("item_analyses"))]
    if not analyses and failure_analysis:
        analyses = [failure_analysis]
    analyses = [analysis for analysis in analyses if object_dict(analysis.get("best_candidate"))]
    if not analyses:
        return None

    policy = object_dict(base_policy)
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

    updated_video = {
        target_key: measured_target,
        "max_encoded_percent": int(measured_cap) if measured_cap.is_integer() else measured_cap,
        "max_crf": int(measured_max_crf) if measured_max_crf.is_integer() else measured_max_crf,
        "quality_metric": metric,
    }

    file_count = len(analyses)
    metric_label = metric.upper()
    summary = (
        f"Measured recovery for {file_count} failed file{'s' if file_count != 1 else ''}: "
        f"allow {metric_label} {measured_target:.1f}, cap {updated_video['max_encoded_percent']}%, "
        f"and CRF {updated_video['max_crf']}. Preserve completed staged encodes."
    )
    return {
        "policy": {"video": updated_video},
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


def _end_scope_check_snapshot(connection: DBClient) -> None:
    """End the scope-check read before per-file work.

    Each file commits its own result. A read snapshot held across a slow file check or
    move cannot become a write once anything else has committed, and SQLite then fails
    that file at once with "database is locked" instead of waiting.
    """
    connection.commit()


def validate_folder_outputs_action(
        config: MediaforceConfig,
        normalized_prefix: str,
        *,
        load_active_encode_job_for_prefix_fn: LoadActiveEncodeJobFn | None = None,
        load_folder_staged_items_fn: LoadFolderStagedItemsFn,
        validate_manifest_items_fn: ValidateManifestItemsFn,
        validate_scope_action: ValidateScopeActionFn | None = None,
) -> ActionPayload:
    production_blocker = production_action_blocker(config, normalized_prefix)
    if production_blocker is not None:
        return production_blocker
    if load_active_encode_job_for_prefix_fn is None:
        load_active_encode_job_for_prefix_fn = _no_active_encode_job
    with open_db(config.paths.db_path) as connection:
        if validate_scope_action is not None:
            connection.exec_driver_sql("BEGIN")
            scope_blocker = validate_scope_action(connection, normalized_prefix)
            if scope_blocker is not None:
                return scope_blocker
        active_item_ids = _delivery_active_item_ids(
            connection,
            normalized_prefix,
            load_active_encode_job_for_prefix_fn,
        )
        if active_item_ids is None:
            return _delivery_unreadable_active_encode_response()
        items = load_folder_staged_items_fn(
            connection,
            config,
            normalized_prefix,
            statuses={"encoded"},
        )
        busy_count = sum(1 for item in items if int_value(item.get("library_item_id")) in active_item_ids)
        items = [item for item in items if int_value(item.get("library_item_id")) not in active_item_ids]
        if not items:
            return {
                "ok": False,
                "message": (
                    "The files ready to check are still being made. Check again when they finish."
                    if busy_count
                    else "No staged encoded files are ready to validate for this folder."
                ),
            }
        ready_items = items
        items, held = _hold_unreachable_staged_items(ready_items)
        if not items:
            return _inaccessible_staged_item_response(
                items=ready_items,
                action="validate",
                zero_count_key="validated_count",
            ) or {"ok": False, "message": "No staged encoded files are ready to validate for this folder."}
        manifest: ManifestPayload = {"items": items}
        _end_scope_check_snapshot(connection)
        results: list[ActionPayload] = []
        for index in range(len(items)):
            try:
                result = validate_manifest_items_fn(connection, config, manifest, [index])[0]
            except Exception as exc:
                # One file's failure must not keep its uncommitted writes, or the lock, for the rest.
                connection.rollback()
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
    if busy_count:
        message += f" {busy_count} still being made will be checked when they finish."
    message += _held_files_copy(held, verb="checked")
    return {
        "ok": True,
        "message": message,
        "validated_count": passed_count,
        "failed_count": failed_count,
        "item_count": len(results),
        "busy_count": busy_count,
        "held": [held_file.to_payload() for held_file in held],
    }


def promote_folder_outputs_action(
        config: MediaforceConfig,
        normalized_prefix: str,
        *,
        load_active_encode_job_for_prefix_fn: LoadActiveEncodeJobFn | None = None,
        load_calibration_state_fn: LoadCalibrationStateFn | None = None,
        load_folder_staged_items_fn: LoadFolderStagedItemsFn,
        promote_manifest_items_fn: PromoteManifestItemsFn,
        validate_scope_action: ValidateScopeActionFn | None = None,
) -> ActionPayload:
    production_blocker = production_action_blocker(config, normalized_prefix)
    if production_blocker is not None:
        return production_blocker
    if load_active_encode_job_for_prefix_fn is None:
        load_active_encode_job_for_prefix_fn = _no_active_encode_job
    with open_db(config.paths.db_path) as connection:
        if validate_scope_action is not None:
            connection.exec_driver_sql("BEGIN")
            scope_blocker = validate_scope_action(connection, normalized_prefix)
            if scope_blocker is not None:
                return scope_blocker
        scope = resolve_media_scope(connection, normalized_prefix, library_types=config.library_type_map)
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
        waiting: list[ActionPayload] = []
        if scope.kind in {"tv_season", "tv_series"}:
            report = staged_integrity_report_for_scope(connection, config, scope, discover=True)
            readiness = tv_promotion_readiness_payload(
                connection,
                config,
                scope,
                report,
                items,
                load_calibration_state_fn=load_calibration_state_fn,
            )
            if readiness["blockers"]:
                return {
                    "ok": False,
                    "code": "promotion_check_unavailable",
                    "message": "Mediaforce cannot check these files safely right now, so nothing was published.",
                    "blockers": readiness["blockers"],
                    "promotion_readiness": readiness,
                    "promoted_count": 0,
                }
            waiting = list(readiness["waiting"])
            promotable_ids = set(readiness["promotable_item_ids"])
            items = [item for item in items if int_value(item.get("library_item_id")) in promotable_ids]
        else:
            active_item_ids = _delivery_active_item_ids(
                connection,
                normalized_prefix,
                load_active_encode_job_for_prefix_fn,
            )
            if active_item_ids is None:
                return _delivery_unreadable_active_encode_response()
            busy = [item for item in items if int_value(item.get("library_item_id")) in active_item_ids]
            if busy:
                waiting.append({"code": "season_active_encode_job", "count": len(busy), "next_action": "wait_for_encode_job"})
            items = [item for item in items if item not in busy]
        if not items:
            return {
                "ok": False,
                "code": "nothing_ready_to_publish",
                "message": "No finished files are ready to publish yet.",
                "waiting": waiting,
                "promoted_count": 0,
            }
        items, held = _hold_unreachable_staged_items(items)
        items, conflict_held = _hold_conflicting_destinations(config, items)
        held.extend(conflict_held)
        promoted_paths: list[Path] = []
        if items:
            manifest: ManifestPayload = {"items": items}
            _end_scope_check_snapshot(connection)
            result = promote_manifest_items_fn(connection, config, manifest, list(range(len(items))), force=False)
            promoted_paths = result.promoted_paths
            held.extend(result.held)
    promoted_count = len(promoted_paths)
    failed_count = sum(1 for held_file in held if not held_file.waiting)
    waiting_count = sum(int_value(entry.get("count")) for entry in waiting)
    file_label = "file" if promoted_count == 1 else "files"
    unsafe_count = sum(1 for held_file in held if held_file.unsafe)
    message = f"Published {promoted_count} {file_label} into the library." if promoted_count else "Nothing was published."
    message += _held_files_copy(held, verb="published")
    if waiting_count:
        message += f" {waiting_count} other {'file is' if waiting_count == 1 else 'files are'} not ready yet."
    response: ActionPayload = {
        "ok": promoted_count > 0 and not unsafe_count,
        "message": message,
        "promoted_count": promoted_count,
        "failed_count": failed_count,
        "unsafe_count": unsafe_count,
        "waiting": waiting,
        "held": [held_file.to_payload() for held_file in held],
    }
    if promoted_count:
        response["target_prefix"] = _promotion_refresh_prefix(normalized_prefix, scope, items, promoted_paths)
    else:
        response["code"] = "nothing_published"
    return response


HELD_FILES_LISTED = 3


def _held_files_copy(held: list[HeldFile], *, verb: str) -> str:
    """One plain sentence per kind of hold, naming a few files and their reasons.

    Files whose original may be out of place are always named in full, first.
    """
    copy = ""
    for state, one, many, limit in (
            ("unsafe", "1 file needs checking now", "{count} files need checking now", None),
            ("failed", f"1 file could not be {verb}", f"{{count}} files could not be {verb}", HELD_FILES_LISTED),
            ("waiting", "1 file will be tried again later", "{count} files will be tried again later", HELD_FILES_LISTED),
    ):
        group = [held_file for held_file in held if held_file.state == state]
        if not group:
            continue
        shown = group if limit is None else group[:limit]
        listed = "; ".join(f"{Path(held_file.rel_path).name}: {held_file.reason}" for held_file in shown)
        more = f"; and {len(group) - len(shown)} more" if len(group) > len(shown) else ""
        lead = one if len(group) == 1 else many.format(count=len(group))
        copy += f" {lead} ({listed}{more})."
    return copy


def _hold_unreachable_staged_items(items: list[FolderItem]) -> tuple[list[FolderItem], list[HeldFile]]:
    """Keep files whose finished output is reachable; hold only the others."""
    reachable: list[FolderItem] = []
    held: list[HeldFile] = []
    for item in items:
        if Path(str(item.get("staging_path") or "")).exists():
            reachable.append(item)
            continue
        rel_path = str(item.get("rel_path") or item.get("source_path") or "")
        host = str(item.get("staging_host_label") or item.get("staging_host_key") or "").strip()
        if host:
            held.append(HeldFile(rel_path, f"Its finished file is on {host}, which cannot be reached now", waiting=True))
        else:
            held.append(HeldFile(rel_path, "Its finished file is missing", waiting=False))
    return reachable, held


def _hold_conflicting_destinations(
        config: MediaforceConfig,
        items: list[FolderItem],
) -> tuple[list[FolderItem], list[HeldFile]]:
    """Keep files with a clear library destination; hold only the files in a conflict."""
    conflict = _promotion_conflict_response(config, items)
    if conflict is None:
        return items, []
    reasons: dict[str, str] = {}
    for entry in object_list(conflict.get("conflicts")):
        entry = object_dict(entry)
        reason = (
            "Another file in this folder would land in the same place"
            if entry.get("kind") == "duplicate_destination"
            else "A different file is already at its place in the library"
        )
        for rel_path in object_list(entry.get("rel_paths")):
            reasons.setdefault(str(rel_path), reason)
    clear = [item for item in items if str(item.get("rel_path") or item.get("source_path")) not in reasons]
    return clear, [HeldFile(rel_path, reason, waiting=False) for rel_path, reason in reasons.items()]


def _promotion_refresh_prefix(
        normalized_prefix: str,
        scope: MediaScope,
        items: list[FolderItem],
        promoted_paths: list[Path],
) -> str:
    if scope.match != "exact_item":
        return normalized_prefix
    if scope.domain == "movie":
        membership = classify_movie_path(normalized_prefix, root=scope.root)
        if (
                membership is not None
                and membership.scope_mode == "title_folder"
                and membership.role == "feature"
                and membership.edition_label is None
        ):
            return membership.title_prefix
    if len(items) != 1 or len(promoted_paths) != 1:
        return normalized_prefix
    rel_path = str(items[0].get("rel_path") or normalized_prefix)
    return Path(rel_path).with_suffix(promoted_paths[0].suffix).as_posix()


def tv_promotion_readiness_payload(
        connection: DBClient,
        config: MediaforceConfig,
        scope: MediaScope,
        report: StagedIntegrityReport,
        items: list[FolderItem],
        *,
        load_calibration_state_fn: LoadCalibrationStateFn | None = None,
) -> ActionPayload:
    """Decide publishing file by file: each ready file publishes, every other file waits with its reason."""
    if scope.kind not in {"tv_season", "tv_series"}:
        return {"applicable": False, "can_promote": True, "blockers": [], "waiting": []}
    # Blockers stop the whole scope only when Mediaforce cannot judge any file safely.
    blockers: list[ActionPayload] = []
    if report.database_truncated:
        blockers.append({"code": "season_integrity_database_truncated", "count": 1, "next_action": "inspect_integrity_detail"})
    if report.discovery_truncated:
        blockers.append({"code": "season_integrity_discovery_truncated", "count": 1, "next_action": "inspect_integrity_detail"})
    active_item_ids = active_encode_library_item_ids(connection, scope.prefix)
    if active_item_ids is None:
        blockers.append({"code": "season_active_encode_unreadable", "count": 1, "next_action": "wait_for_encode_job"})

    waiting: dict[str, int] = {}

    def wait(code: str, amount: int = 1) -> None:
        waiting[code] = waiting.get(code, 0) + amount

    for disposition, count in sorted(report.counts.items()):
        if count and integrity_disposition_blocks_promotion(cast(IntegrityDisposition, disposition)):
            wait(f"season_staged_integrity_{disposition}", count)
    ready_ids = {
        int(record.item_id)
        for record in report.records
        if record.item_id is not None and record.disposition == "promotable"
    }
    candidates = [item for item in items if int_value(item.get("library_item_id")) in ready_ids]
    if active_item_ids:
        active = [item for item in candidates if int_value(item.get("library_item_id")) in active_item_ids]
        if active:
            wait("season_active_encode_job", len(active))
        candidates = [item for item in candidates if item not in active]
    if load_calibration_state_fn is None and candidates:
        blockers.append({"code": "season_policy_gate_unavailable", "count": 1, "next_action": "restore_policy_gate"})
    if load_calibration_state_fn is not None and candidates:
        accepted_policy_hash = _accepted_tv_scope_policy_hash(
            config,
            scope,
            load_calibration_state_fn=load_calibration_state_fn,
        )
        policy_states = _staged_policy_states(
            connection,
            {int_value(item.get("library_item_id")) for item in candidates},
            accepted_policy_hash=accepted_policy_hash,
        )
        approved: list[FolderItem] = []
        for item in candidates:
            state = policy_states.get(int_value(item.get("library_item_id")), "season_policy_provenance_missing")
            if state is None:
                approved.append(item)
            else:
                wait(state)
        candidates = approved
    conflicted_paths = _conflicted_rel_paths(config, candidates)
    if conflicted_paths:
        conflicted = [item for item in candidates if str(item.get("rel_path") or "") in conflicted_paths]
        wait("season_destination_conflict", len(conflicted))
        candidates = [item for item in candidates if item not in conflicted]
    promotable_ids = sorted(int_value(item.get("library_item_id")) for item in candidates)
    return {
        "applicable": True,
        "can_promote": bool(promotable_ids) and not blockers,
        "promotable_count": len(promotable_ids),
        "promotable_item_ids": promotable_ids,
        "blockers": blockers,
        "waiting": [
            {"code": code, "count": count, "next_action": _waiting_next_action(code)}
            for code, count in waiting.items()
        ],
    }


def active_encode_library_item_ids(connection: DBClient, prefix: str) -> set[int] | None:
    """Files an active encode may still write. None means a job's files cannot be read, so fail closed."""
    item_ids: set[int] = set()
    for job in load_active_encode_jobs_for_prefix(connection, prefix):
        manifest_items = _manifest_items(job)
        if not manifest_items:
            return None
        indexes = [
            index
            for index in object_list(job.get("manifest_indexes"))
            if isinstance(index, int)
        ] or list(range(len(manifest_items)))
        item_ids.update(_manifest_library_item_ids(job, indexes))
    return item_ids


def _waiting_next_action(code: str) -> str:
    if code.startswith("season_staged_integrity_"):
        return _season_integrity_next_action(code.removeprefix("season_staged_integrity_"))
    return {
        "season_active_encode_job": "wait_for_encode_job",
        "season_policy_provenance_missing": "recreate_output_with_policy_provenance",
        "season_policy_not_approved": "approve_matching_policy_or_recreate_outputs",
        "season_destination_conflict": "resolve_destination_conflicts",
    }.get(code, "inspect_integrity_detail")


def _accepted_tv_scope_policy_hash(
        config: MediaforceConfig,
        scope: MediaScope,
        *,
        load_calibration_state_fn: LoadCalibrationStateFn,
) -> str:
    prefixes = [scope.prefix]
    if scope.kind == "tv_season" and scope.parent_prefix:
        prefixes.append(scope.parent_prefix)
    for prefix in prefixes:
        calibration = object_dict(load_calibration_state_fn(config, prefix))
        accepted_policy_hash = str(calibration.get("accepted_policy_hash") or "").strip()
        if accepted_policy_hash:
            return accepted_policy_hash
    return ""


def _staged_policy_states(
        connection: DBClient,
        library_item_ids: set[int],
        *,
        accepted_policy_hash: str,
) -> dict[int, str | None]:
    """None when the file was made under an approved policy, otherwise the waiting code.

    A file counts as approved when its recorded policy matches the current approval, or when the run
    that made it recorded the production approval it ran under.
    """
    rows = connection.execute(
        select(
            staged_artifacts.c.library_item_id,
            staged_artifacts.c.manifest_path,
            staged_artifacts.c.item_index,
        )
        .where(staged_artifacts.c.library_item_id.in_(sorted(library_item_ids)))
    ).mappings().fetchall()
    manifest_cache: dict[Path, ActionPayload | None] = {}
    states: dict[int, str | None] = {}
    for row in rows:
        item_id = int(row["library_item_id"])
        manifest_value = str(row["manifest_path"] or "").strip()
        item_index = row["item_index"]
        if not manifest_value or not isinstance(item_index, int):
            states[item_id] = "season_policy_provenance_missing"
            continue
        manifest_path = Path(manifest_value)
        if manifest_path not in manifest_cache:
            try:
                manifest_cache[manifest_path] = object_dict(json.loads(manifest_path.read_text()))
            except (OSError, json.JSONDecodeError):
                manifest_cache[manifest_path] = None
        manifest = manifest_cache[manifest_path]
        manifest_items = object_list(object_dict(manifest).get("items"))
        if manifest is None or item_index < 0 or item_index >= len(manifest_items):
            states[item_id] = "season_policy_provenance_missing"
            continue
        policy = object_dict(object_dict(manifest_items[item_index]).get("resolved_policy"))
        if not policy:
            states[item_id] = "season_policy_provenance_missing"
            continue
        approved_at_run = bool(object_dict(object_dict(manifest.get("selection")).get("production_approval_contract")))
        matches_current = bool(accepted_policy_hash) and _calibration_policy_hash({"policy": policy}) == accepted_policy_hash
        states[item_id] = None if approved_at_run or matches_current else "season_policy_not_approved"
    return states


def _conflicted_rel_paths(config: MediaforceConfig, items: list[FolderItem]) -> set[str]:
    conflict = _promotion_conflict_response(config, items)
    if conflict is None:
        return set()
    return {
        str(rel_path)
        for entry in object_list(conflict.get("conflicts"))
        for rel_path in object_list(object_dict(entry).get("rel_paths"))
    }


def _season_integrity_next_action(disposition: str) -> str:
    return {
        "unvalidated": "validate_output",
        "validation_failed": "inspect_validation_failure",
        "missing": "recreate_staged_output",
        "drifted": "revalidate_or_recreate_output",
        "orphaned": "inspect_untracked_output",
        "partial_or_temporary": "wait_or_inspect_temporary_output",
        "remote_only_or_unreachable": "restore_staging_access",
        "not_started": "queue_encode",
    }.get(disposition, "inspect_integrity_detail")


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
    remote_items = [
        item
        for item in inaccessible_items
        if str(item.get("staging_host_label") or item.get("staging_host_key") or "").strip()
    ]
    missing_count = len(inaccessible_items) - len(remote_items)
    inaccessible_hosts = sorted(
        {
            str(item.get("staging_host_label") or item.get("staging_host_key") or "").strip()
            for item in inaccessible_items
            if str(item.get("staging_host_label") or item.get("staging_host_key") or "").strip()
        }
    )
    host_copy = f" Encoded hosts: {', '.join(inaccessible_hosts)}." if inaccessible_hosts else ""
    access_reason = (
        f"{len(remote_items)} staged file{'s are' if len(remote_items) != 1 else ' is'} remote-only or unreachable"
        if remote_items
        else f"{missing_count} staged file{'s are' if missing_count != 1 else ' is'} missing locally"
    )
    response = {
        "ok": False,
        "message": (
            f"Cannot {action} this folder from the current web host because {access_reason}.{host_copy}"
        ),
        zero_count_key: 0,
        "failed_count": len(inaccessible_items),
        "item_count": len(items),
        "remote_unreachable_count": len(remote_items),
        "missing_count": missing_count,
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
        recovery_plan: tuple[list[JobPayload], list[int]] | None = None,
) -> ActionPayload | None:
    if str(active_encode_job.get("job_kind") or "single") != "folder":
        return None
    resolved_plan = recovery_plan or _folder_recovery_plan(connection, active_encode_job)
    if resolved_plan is None:
        return None
    recoverable_children, recoverable_indexes = resolved_plan

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


def _prepare_terminal_job_except(
        connection: DBClient,
        job: JobPayload,
        excluded_indexes: set[int],
        prepare_fn: PrepareTerminalEncodeJobForRequeueFn,
) -> None:
    """Clean up a failed job for retry without touching the artifacts of files left out of it."""
    if not excluded_indexes:
        prepare_fn(connection, job)
        return
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
        selected_indexes = list(range(len(_manifest_items(job))))
    kept_indexes = sorted(set(selected_indexes) - excluded_indexes)
    if kept_indexes:
        prepare_fn(connection, {**job, "job_kind": "single", "manifest_indexes": kept_indexes})


def _partition_active_recovery_plan(
        connection: DBClient,
        config: MediaforceConfig,
        normalized_prefix: str,
        scope: MediaScope,
        active_encode_job: JobPayload,
        recovery_plan: tuple[list[JobPayload], list[int]],
) -> tuple[tuple[list[JobPayload], list[int]] | None, list[LeftOutFile]]:
    """Recover the failed children that pass; leave each other child failed with its reason."""
    children, indexes = recovery_plan
    left_out_by_index = _movie_requeue_policy_left_out(connection, config, scope, active_encode_job)
    manifest_items = _manifest_items(active_encode_job)
    item_id_by_index = {
        index: int(manifest_items[index].get("library_item_id") or 0)
        for index in indexes
        if 0 <= index < len(manifest_items)
    }
    for index in indexes:
        if item_id_by_index.get(index, 0) <= 0 and index not in left_out_by_index:
            left_out_by_index[index] = LeftOutFile(
                0,
                "",
                "manifest_item_unknown",
                "The folder's saved plan does not say which file this is. Prepare the folder again to retry it.",
            )
    cadence_ids = [
        item_id
        for index, item_id in item_id_by_index.items()
        if item_id > 0 and index not in left_out_by_index
    ]
    cadence_partition = cadence_queue_partition(
        connection,
        config,
        normalized_prefix,
        library_item_ids=cadence_ids,
        work_reason="encode_safety",
    )
    rel_paths = {
        item_id: str(manifest_items[index].get("rel_path") or "")
        for index, item_id in item_id_by_index.items()
    }
    cadence_files = {file.library_item_id: file for file in cadence_left_out_files(cadence_partition, rel_paths)}
    for index, item_id in item_id_by_index.items():
        if item_id in cadence_files and index not in left_out_by_index:
            left_out_by_index[index] = cadence_files[item_id]
    kept_children = [
        child
        for child in children
        if not any(
            isinstance(index, int) and index in left_out_by_index
            for index in object_list(child.get("manifest_indexes"))
        )
    ]
    kept_indexes = sorted({
        index
        for child in kept_children
        for index in object_list(child.get("manifest_indexes"))
        if isinstance(index, int)
    })
    left_out = [left_out_by_index[index] for index in sorted(left_out_by_index)]
    if not kept_children or not kept_indexes:
        return None, left_out
    return (kept_children, kept_indexes), left_out


def _folder_recovery_plan(
        connection: DBClient,
        active_encode_job: JobPayload,
) -> tuple[list[JobPayload], list[int]] | None:
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
    unique_indexes = sorted(set(recoverable_indexes))
    if not recoverable_children or not unique_indexes:
        return None
    return recoverable_children, unique_indexes


def _manifest_items(job: JobPayload) -> list[ActionPayload]:
    manifest_path = Path(str(job.get("manifest_path") or "").strip())
    if not str(manifest_path):
        return []
    try:
        manifest = object_dict(json.loads(manifest_path.read_text()))
    except (OSError, json.JSONDecodeError):
        return []
    return [object_dict(item) for item in object_list(manifest.get("items"))]


def _manifest_library_item_ids(job: JobPayload, manifest_indexes: list[int]) -> list[int]:
    items = _manifest_items(job)
    item_ids: list[int] = []
    for manifest_index in manifest_indexes:
        if manifest_index < 0 or manifest_index >= len(items):
            continue
        library_item_id = int(items[manifest_index].get("library_item_id") or 0)
        if library_item_id > 0:
            item_ids.append(library_item_id)
    return sorted(set(item_ids))


def _folder_recoverable_children(connection: DBClient, parent_job_id: str) -> list[JobPayload]:
    if not parent_job_id:
        return []
    return [
        child
        for child in list_child_encode_jobs(connection, parent_job_id)
        if str(child.get("status") or "") in {"needs_attention", "failed", "stopped"}
    ]


def _movie_requeue_policy_left_out(
        connection: DBClient,
        config: MediaforceConfig,
        scope: MediaScope,
        job: JobPayload,
) -> dict[int, LeftOutFile]:
    """Saved retry files now outside the movie title policy, by manifest index."""
    if scope.domain != "movie":
        return {}
    manifest_path = Path(str(job.get("manifest_path") or "").strip())
    if not str(manifest_path):
        return {}
    try:
        manifest = object_dict(json.loads(manifest_path.read_text()))
    except (OSError, json.JSONDecodeError):
        return {}
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
    left_out: dict[int, LeftOutFile] = {}
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
            left_out[index] = LeftOutFile(
                int(items[index].get("library_item_id") or 0),
                rel_path,
                "movie_title_policy",
                (
                    "Outside the current movie title policy. "
                    f"{blocker or 'Open that exact movie file to retry it deliberately.'}"
                ),
            )
    return left_out


def _delivery_active_item_ids(
        connection: DBClient,
        prefix: str,
        load_active_encode_job_for_prefix_fn: LoadActiveEncodeJobFn,
) -> set[int] | None:
    """Files an active encode may still write; delivery skips only those. None fails closed."""
    item_ids = active_encode_library_item_ids(connection, prefix)
    if item_ids is None:
        return None
    injected_job = load_active_encode_job_for_prefix_fn(connection, prefix)
    if injected_job and str(injected_job.get("status") or "queued") in ACTIVE_ENCODE_JOB_STATUSES:
        manifest_items = _manifest_items(injected_job)
        if not manifest_items:
            return None
        indexes = [
            index for index in object_list(injected_job.get("manifest_indexes")) if isinstance(index, int)
        ] or list(range(len(manifest_items)))
        item_ids.update(_manifest_library_item_ids(injected_job, indexes))
    return item_ids


def _delivery_unreadable_active_encode_response() -> ActionPayload:
    return {
        "ok": False,
        "message": (
            "Mediaforce cannot tell which files the running encode is still making, "
            "so nothing was delivered. Try again when it finishes."
        ),
    }


def _reset_stale_prefix_encoding_items_for_requeue(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        *,
        now_iso: NowIsoFn,
) -> None:
    rows = _stale_prefix_encoding_rows_for_requeue(connection, config, prefix)
    if not rows:
        return
    updated_at = now_iso()
    for row in rows:
        if row["promoted_at"] is not None or str(row["status"] or "") in {"encoded", "validated"}:
            continue
        if (
                str(row["encode_completed_at"] or "").strip()
                and str(row["staging_fingerprint"] or "").strip()
        ):
            connection.execute(
                update(library_items)
                .where(library_items.c.id == row["id"])
                .where(library_items.c.status == "encoding")
                .values(status="encoded", updated_at=updated_at)
            )
            continue
        staging_value = str(row["staging_path"] or "").strip()
        rel_path = str(row["rel_path"] or "").strip()
        staging_path: Path | None = Path(staging_value) if staging_value else None
        if staging_path is None and rel_path:
            output_suffix = str(object_dict(config.media).get("output_container") or "").strip()
            output_suffix = f".{output_suffix.lstrip('.')}" if output_suffix else Path(rel_path).suffix or ".mkv"
            staging_path = config.staging_root / Path(rel_path).with_suffix(output_suffix)
        if staging_path is not None:
            host_key = str(row["encode_host_key"] or "").strip()
            host = (
                host_config_for_key(config, host_key)
                if host_key
                else {
                    "mode": str(row["encode_host_mode"] or "").strip(),
                    "media_access": str(row["encode_media_access"] or "").strip(),
                }
            )
            cleanup_succeeded = remove_stale_staging_path(staging_path, host=host)
            cleanup_succeeded = remove_stale_staging_path(
                partial_output_path(staging_path),
                host=host,
            ) and cleanup_succeeded
            if not cleanup_succeeded:
                continue
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


def _stale_prefix_encoding_rows_for_requeue(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
) -> list[Mapping[str, Any]]:
    normalized_prefix = str(prefix).strip().strip("/")
    if not normalized_prefix:
        return []
    scope = resolve_media_scope(
        connection,
        normalized_prefix,
        library_types=config.library_type_map,
    )
    protected_prefixes = _active_descendant_encode_prefixes(connection, scope)
    rows = connection.execute(
        select(
            library_items.c.id,
            library_items.c.rel_path,
            library_items.c.status,
            staged_artifacts.c.staging_path,
            staged_artifacts.c.encode_completed_at,
            staged_artifacts.c.staging_fingerprint,
            staged_artifacts.c.promoted_at,
            staged_artifacts.c.encode_host_key,
            staged_artifacts.c.encode_host_mode,
            staged_artifacts.c.encode_media_access,
        )
        .select_from(
            library_items.outerjoin(
                staged_artifacts,
                staged_artifacts.c.library_item_id == library_items.c.id,
            )
        )
        .where(scope_rel_path_filter(library_items.c.rel_path, scope))
        .where(library_items.c.status == "encoding")
    ).mappings().fetchall()
    eligible_rows: list[Mapping[str, Any]] = []
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
        eligible_rows.append(row)
    return eligible_rows


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
        clear_pending_proposal: ClearPendingProposalFn | None = None,
        load_latest_failed_target_size_job_state: LoadJobStateFn | None = None,
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
    if bool(operator_request.get("operator_confirmed")) and request_disposition in {
        "softened",
        "rejected",
        "unclear",
        "unavailable",
    }:
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
            and allows_measured_size_quality_tradeoff(
                operator_request=operator_request or None,
                size_target_analysis=size_target_analysis,
                direction="larger",
            )
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
    latest_failed_sample_job = None
    if load_latest_failed_target_size_job_state is not None:
        with open_db(config.paths.db_path) as connection:
            latest_failed_sample_job = load_latest_failed_target_size_job_state(
                connection,
                config,
                normalized_prefix,
            )
        failed_target_reason = _failed_target_size_job_blocking_reason(
            latest_failed_sample_job,
            calibration_payload,
        )
        if failed_target_reason is not None:
            raise HTTPException(status_code=409, detail=failed_target_reason)
    quality_risk_contract = build_quality_risk_contract(
        prefix=normalized_prefix,
        sample_item=object_dict(calibration_payload.get("sample_item")),
        current_policy=object_dict(calibration_payload.get("policy")),
        preview_policy=object_dict(calibration_payload.get("policy")),
        operator_request=operator_request or None,
        calibration=calibration_payload,
        advice_state=advice_state,
        latest_failed_sample_job=latest_failed_sample_job,
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
        accepted_sample_job_id = str(calibration_payload["accepted_sample_job_id"])
        source_scope = object_dict(quality_risk_contract.get("source_scope"))
        contract_policy = object_dict(quality_risk_contract.get("policy"))
        review_tags = [
            str(object_dict(risk).get("tag") or "")
            for risk in object_list(quality_risk_contract.get("typed_risks"))
            if str(object_dict(risk).get("tag") or "").strip()
        ]
        review_evidence_ids = [str(value) for value in object_list(source_scope.get("evidence_ids"))]
        review_moment_indexes = list(range(1, len(object_list(calibration_payload.get("review_moments"))) + 1))
        existing_approval = object_dict(advice_state.get("approval_artifact"))
        approval_artifact = (
            existing_approval
            if str(existing_approval.get("sample_job_id") or "") == str(calibration_payload.get("job_id") or "")
            else None
        )
        with open_db(config.paths.db_path) as connection:
            if str(existing_approval.get("sample_job_id") or "") != str(calibration_payload.get("job_id") or ""):
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
            boundary_observation = record_visual_content_intent_observation(
                connection,
                prefix=normalized_prefix,
                sample_item=object_dict(calibration_payload.get("sample_item")),
                calibration=calibration_payload,
                verdict="approved",
                concern_tags=review_tags or ["other"],
                evidence_ids=review_evidence_ids,
                moment_indexes=review_moment_indexes,
                recorded_at=str(calibration_payload["accepted_at"]),
            )
        save_calibration_state(config, normalized_prefix, calibration_payload)
        if accepted_sample_job_id:
            with open_db(config.paths.db_path) as connection:
                resolve_pending_review_job(
                    connection,
                    accepted_sample_job_id,
                    updated_at=str(calibration_payload["accepted_at"]),
                )
        if approval_artifact is not None:
            approval_artifact["sample_job_id"] = str(calibration_payload.get("job_id") or "")
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
            evidence_ids=review_evidence_ids,
            moment_indexes=review_moment_indexes,
        )
        advice_patch: ActionPayload = {
            "operator_approved_at": calibration_payload["accepted_at"],
            "operator_approved_size_tradeoff": bool(size_issue),
            "quality_risk_records": object_list(quality_risk_state.get("quality_risk_records")),
            "content_intent_boundary_observation": asdict(boundary_observation),
        }
        if approval_artifact is not None:
            advice_patch["approval_artifact"] = approval_artifact
        merge_advice_state(config, normalized_prefix, advice_patch)
    upsert_override(
        config.paths.runtime_settings_path,
        normalized_prefix,
        calibration_payload["policy"],
    )
    if clear_pending_proposal is not None:
        clear_pending_proposal(config, normalized_prefix)
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
    if not blocking_reasons:
        return "Measured review facts still block this action."
    return " ".join(dict.fromkeys(blocking_reasons))


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
    selection_reason = str(trace.get("selection_reason") or "").strip().lower()
    if target_status == "infeasible" and selection_reason in {
        "smallest_quality_safe_candidate_over_target_band",
        "largest_quality_safe_candidate_under_target_band",
        "target_lower_bound_exceeds_source_relative_cap",
        "source_relative_cap_consumed_by_non_video_budget",
    }:
        target_status = "bound_exhausted"
    if target_status not in {"infeasible", "bound_exhausted", "quality_conflict"}:
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
    if target_status == "bound_exhausted":
        return (
            "The latest size-directed test reached a configured search or source-size limit before it found this "
            "target. "
            "Choose updated settings and approve a fresh test before starting production."
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


def child_recovery_approval(
        config: MediaforceConfig,
        parent: JobPayload,
        manifest: ActionPayload,
        *,
        load_calibration_state: LoadCalibrationStateFn,
        review_gate: ReviewGateFn,
        load_advice_state: LoadAdviceStateFn,
) -> ActionPayload:
    prefix = str(parent["prefix"])
    blocker = production_action_blocker(config, prefix)
    if blocker is not None:
        raise HTTPException(status_code=409, detail=blocker["message"])
    calibration = object_dict(load_calibration_state(config, prefix))
    gate = review_gate(calibration)
    if not gate.get("can_confirm_full"):
        raise HTTPException(status_code=409, detail=str(gate.get("message") or "Current approval is required."))
    current = _production_approval_contract(calibration)
    recorded = _valid_production_approval_contract(
        object_dict(manifest.get("selection")).get("production_approval_contract")
    )
    if current is None or recorded is None or current != recorded:
        raise HTTPException(status_code=409, detail="The manifest no longer matches the current production approval.")
    advice = object_dict(load_advice_state(config, prefix))
    risk = build_quality_risk_contract(
        prefix=prefix,
        sample_item=object_dict(calibration.get("sample_item")),
        current_policy=object_dict(calibration.get("policy")),
        preview_policy=object_dict(calibration.get("policy")),
        operator_request=object_dict(advice.get("operator_request")) or None,
        calibration=calibration,
        advice_state=advice,
    )
    reason = _quality_risk_blocking_reason(risk)
    if reason or object_dict(risk.get("operator_decision")).get("status") == "rejected":
        raise HTTPException(status_code=409, detail=reason or "The current review was rejected.")
    return current


def child_recovery_candidate_evidence(
        connection: DBClient,
        config: MediaforceConfig,
        parent: JobPayload,
        items: list[ActionPayload],
) -> ActionPayload:
    prefix = str(parent["prefix"])
    item_ids = {int(item["library_item_id"]) for item in items}
    overrides = {
        str(provenance.get("season_prefix"))
        for item in items
        if (provenance := object_dict(item.get("selection_provenance"))).get("override_applied") is True
        and provenance.get("manual_override") is True
        and str(provenance.get("season_prefix") or "").startswith(prefix.rstrip("/") + "/")
    }
    decisions = project_candidates(
        connection, config, prefixes=[prefix], manual_override_prefixes=overrides,
    )
    selected = {decision.item_id: decision for decision in decisions if decision.item_id in item_ids}
    if set(selected) != item_ids or any(not decision.eligible for decision in selected.values()):
        raise HTTPException(status_code=409, detail="Current source, lifecycle or production policy blocks recovery.")
    for item in items:
        decision = selected[int(item["library_item_id"])]
        if not decision.override_applied:
            continue
        original = object_dict(item.get("selection_provenance"))
        original_codes = {
            reason["code"]
            for reason in object_list(original.get("hold_reasons"))
            if isinstance(reason, dict) and isinstance(reason.get("code"), str)
        }
        current_codes = {reason.code for reason in decision.hold_reasons}
        if (
                current_codes != original_codes
                or decision.is_current_season != original.get("is_current_season")
        ):
            raise HTTPException(status_code=409, detail="The original lifecycle override no longer covers current holds.")
    cadence = cadence_safety_partition(connection, library_item_ids=sorted(item_ids), synchronize=False)
    if cadence.cleared_item_ids != item_ids:
        raise HTTPException(status_code=409, detail="Current cadence evidence is required for every recovery item.")
    return {
        "items": {
            str(item_id): {
                "eligible": selected[item_id].eligible,
                "override_applied": selected[item_id].override_applied,
                "hold_codes": [reason.code for reason in selected[item_id].hold_reasons],
            }
            for item_id in sorted(item_ids)
        },
        "cadence_cleared": sorted(cadence.cleared_item_ids),
    }
