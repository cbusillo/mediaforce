import uuid
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import HTTPException

from mediaforce.advisor import apply_seed_policy, request_note_tuning, request_review_artifact_critique
from mediaforce.advising.policy import has_nonpositive_video_budget, merge_policy_fragments
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.web.runtime.folder_tuning_advice import operator_preserves_source_resolution
from mediaforce.web.runtime.folder_tuning_helpers import (
    allows_measured_size_quality_tradeoff,
    measured_size_budget_policy_fragment,
)
from mediaforce.library.folder_profiles import inspect_prefix
from mediaforce.tuning.tuning_memory import promote_learning_artifact, retrieve_learning_context


QUEUEABLE_NO_CHANGE_DISPOSITIONS = {"honored", "honored_with_risk"}
NONQUEUEABLE_DISPOSITIONS = {"softened", "rejected", "unclear"}


@dataclass(slots=True)
class FolderAiTuneDeps:
    resolve_sample_host: Any
    load_job_state: Any
    load_retryable_sample_job_state: Any
    sample_item: Any
    operator_requested_experiment: Any
    load_calibration_state: Any
    recent_tuning_sessions: Any
    matching_request_history: Any
    metric_support: Any
    maybe_seed_baseline_policy: Any
    seed_advice_payload: Any
    proposal_alignment_issue: Any
    now_iso: Any
    proposal_signal_copy: Any
    proposal_context_snapshot: Any
    save_pending_proposal: Any
    pending_proposal_public_view: Any
    build_tuning_runtime_toolbelt: Any
    review_pack_dir: Any
    remove_path_if_exists: Any
    build_multimodal_review_pack: Any
    multimodal_review_pack_public_view: Any
    tuning_advice_payload: Any
    load_pending_proposal: Any
    apply_policy_fragment: Any
    save_advice_state: Any
    save_job_state: Any
    clear_pending_proposal: Any
    record_tuning_session: Any
    load_latest_failed_sample_job_state: Any | None = None


def _job_sample_item_payload(sample_item: dict[str, Any]) -> dict[str, Any]:
    return {
        "library_item_id": sample_item.get("library_item_id"),
        "rel_path": sample_item.get("rel_path"),
        "source_path": sample_item.get("source_path"),
        "source_fingerprint": sample_item.get("source_fingerprint"),
        "source_size_bytes": sample_item.get("source_size_bytes"),
        "video_codec": sample_item.get("video_codec"),
        "video_bitrate": sample_item.get("video_bitrate"),
        "width": sample_item.get("width"),
        "height": sample_item.get("height"),
        "cadence_class": sample_item.get("cadence_class"),
        "cadence_summary": object_dict(sample_item.get("cadence_summary")),
        "cadence_evidence": object_dict(sample_item.get("cadence_evidence")),
        "cadence_decision": object_dict(sample_item.get("cadence_decision")),
        "duration_seconds": sample_item.get("duration_seconds"),
        "audio_summary": object_list(sample_item.get("audio_summary")),
        "subtitle_summary": object_list(sample_item.get("subtitle_summary")),
        "resolved_policy": object_dict(sample_item.get("resolved_policy")),
        "representative_source_id": sample_item.get("representative_source_id"),
        "representative_selection": object_dict(sample_item.get("representative_selection")),
    }


def _latest_failed_sample_job_payload(job: dict[str, Any] | None) -> dict[str, Any] | None:
    payload = object_dict(job)
    if not payload:
        return None
    status = str(payload.get("status") or "").strip()
    if status not in {"failed", "stopped"}:
        return None
    lane = str(payload.get("lane") or payload.get("mode") or "sample").strip()
    if lane != "sample":
        return None
    sample_item = object_dict(payload.get("sample_item"))
    compact_sample_item = {
        key: sample_item.get(key)
        for key in (
            "library_item_id",
            "rel_path",
            "source_size_bytes",
            "video_codec",
            "video_bitrate",
            "width",
            "height",
            "cadence_class",
            "cadence_summary",
            "cadence_evidence",
            "cadence_decision",
            "duration_seconds",
            "audio_summary",
            "subtitle_summary",
            "representative_source_id",
            "representative_selection",
        )
        if key in sample_item
    }
    return {
        "job_id": payload.get("job_id"),
        "status": status,
        "action": payload.get("action"),
        "host": object_dict(payload.get("host")),
        "notes": payload.get("notes"),
        "policy": object_dict(payload.get("policy")),
        "result": object_dict(payload.get("result")) or None,
        "error": payload.get("error"),
        "created_at": payload.get("created_at"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "updated_at": payload.get("updated_at"),
        "sample_item": compact_sample_item,
    }


def _positive_number(value: Any) -> float | None:
    if not isinstance(value, str | int | float):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _measured_budget_fragment_preserving_stricter_cap(
        existing_fragment: dict[str, Any], measured_fragment: dict[str, Any]
) -> dict[str, Any]:
    existing_cap = _positive_number(object_dict(existing_fragment.get("video")).get("max_encoded_percent"))
    measured_cap = _positive_number(object_dict(measured_fragment.get("video")).get("max_encoded_percent"))
    if existing_cap is None or measured_cap is None or existing_cap >= measured_cap:
        return measured_fragment
    return merge_policy_fragments(
        measured_fragment,
        {"video": {"max_encoded_percent": int(round(existing_cap))}},
    )


def _operator_forbids_hard_budget_cap(operator_request: dict[str, Any] | None) -> bool:
    request = object_dict(operator_request)
    text = str(request.get("request_text") or "").strip().lower()
    if not text:
        return False
    hard_cap_terms = ("hard cap", "hard size", "ceiling", "max_encoded_percent", "must hit")
    priority_terms = (
        "prioritize preserving",
        "prioritize resolution",
        "prioritize 1080",
        "preserve 1080",
        "preserve source",
        "source resolution",
        "do not downscale",
        "don't downscale",
        "dont downscale",
    )
    conditional_terms = ("only if", "if it does not require", "if it doesn't require", "if it doesnt require")
    return any(term in text for term in hard_cap_terms) and any(
        term in text for term in priority_terms
    ) and any(term in text for term in conditional_terms)


def _honor_operator_source_resolution(
        *,
        operator_request: dict[str, Any] | None,
        combined_fragment: dict[str, Any],
) -> dict[str, Any]:
    if not operator_preserves_source_resolution(operator_request):
        return combined_fragment
    return merge_policy_fragments(combined_fragment, {"video": {"max_height": 0}})


def _proposal_can_queue(
        *,
        applied_fragment: dict[str, Any],
        preview_policy: dict[str, Any],
        request_disposition: str | None,
        alignment_issue: str | None,
) -> bool:
    if alignment_issue is not None:
        return False
    disposition = str(request_disposition or "").strip().lower()
    if disposition in NONQUEUEABLE_DISPOSITIONS:
        return False
    if applied_fragment:
        return True
    return bool(preview_policy) and disposition in QUEUEABLE_NO_CHANGE_DISPOSITIONS


def _proposal_ready_message(
        *,
        can_queue: bool,
        alignment_issue: str | None,
        has_policy_change: bool,
        run_label: str,
) -> str:
    if alignment_issue:
        return alignment_issue
    if can_queue and has_policy_change:
        return f"Review the bench draft, then confirm when you are ready to run the {run_label}."
    if can_queue:
        return f"The bench kept the current policy. Confirm when you are ready to rerun the {run_label} unchanged."
    return "The bench did not produce a queueable draft yet. Adjust the note and ask again."


def _size_budget_measurement_fragment(
        operator_request: dict[str, Any] | None,
        sample_item: dict[str, Any],
) -> dict[str, Any] | None:
    request = object_dict(operator_request)
    request_type = str(request.get("request_type") or "").strip().lower()
    if request_type == "size_budget":
        size_request = request
    elif request_type == "combined_experiment":
        size_request = object_dict(request.get("size_budget_request"))
        if not size_request:
            return None
    else:
        return None
    if not request.get("operator_confirmed") or has_nonpositive_video_budget(request):
        return None
    budget_bytes = _positive_number(size_request.get("budget_bytes"))
    duration_seconds = _positive_number(sample_item.get("duration_seconds"))
    if budget_bytes is None or duration_seconds is None:
        return None
    target_fragment = {
        "video": {
            "target_size_mb": round(budget_bytes / (1024 ** 2), 3),
            "target_runtime_minutes": round(duration_seconds / 60.0, 3),
        }
    }
    return merge_policy_fragments(object_dict(request.get("applied_policy")), target_fragment)


def _can_keep_first_size_budget_sample(
        operator_request: dict[str, Any] | None,
        *,
        latest_failed_sample_job: dict[str, Any] | None = None,
) -> bool:
    request = object_dict(operator_request)
    if str(request.get("request_type") or "").strip().lower() != "size_budget":
        return False
    if str(request.get("feasibility") or "").strip().lower() != "plausible":
        return False
    if str(request.get("evidence_authority") or "none").strip().lower() == "rejected_visual_result":
        return False
    if object_dict(latest_failed_sample_job):
        return False
    if request.get("measured_size_followup") or request.get("hard_size_cap"):
        return False
    return bool(request.get("operator_confirmed")) and not object_dict(request.get("applied_policy"))


def _operator_request_with_retrieved_memory_authority(
        operator_request: dict[str, Any] | None,
        learning_context: list[dict[str, Any]],
) -> dict[str, Any] | None:
    request = object_dict(operator_request)
    if not request:
        return operator_request
    authority = str(request.get("evidence_authority") or "none").strip().lower()
    if authority != "none":
        return request
    if not any(
            str(object_dict(memory).get("evidence_authority") or "none").strip().lower()
            == "approved_visual_result"
            for memory in learning_context
    ):
        return request
    return {**request, "evidence_authority": "approved_visual_result"}


def _blocking_sample_evidence_issue(
        *,
        operator_request: dict[str, Any] | None,
        latest_failed_sample_job: dict[str, Any] | None,
        current_policy: dict[str, Any],
        preview_policy: dict[str, Any],
) -> str | None:
    if preview_policy != current_policy:
        return None
    authority = str(object_dict(operator_request).get("evidence_authority") or "none").strip().lower()
    if authority == "rejected_visual_result":
        return "The operator rejected the current sample. Change the draft before queueing another run."
    if object_dict(latest_failed_sample_job):
        return "The previous sample failed. Change the draft before queueing another run."
    return None


def _keep_first_size_budget_sample(
        *,
        advice_payload: dict[str, Any] | None,
        seed_job_fields: dict[str, Any],
        trimmed_note: str,
    operator_request: dict[str, Any],
) -> dict[str, Any]:
    request = object_dict(operator_request)
    if has_nonpositive_video_budget(request):
        summary = "The requested total size leaves no positive video budget."
        diagnosis = (
            "The estimated audio allocation consumes the requested total size, so this sample cannot queue "
            "until the total target or audio allocation changes."
        )
        request_response = (
            "That total size cannot queue as written because it leaves no positive video budget. "
            "Increase the total target or explicitly reduce the audio allocation first."
        )
        seed_job_fields["seed_source"] = "operator_request"
        seed_job_fields["seed_summary"] = summary
        seed_job_fields["seed_diagnosis"] = diagnosis
        seed_job_fields["seed_request_disposition"] = "rejected"
        seed_job_fields["seed_request_response"] = request_response
        seed_job_fields["seed_feasibility_note"] = "infeasible"
        seed_job_fields["seed_applied_policy"] = None
        payload = advice_payload if advice_payload is not None else {}
        payload.update(
            {
                "ok": False,
                "summary": summary,
                "kind": "seed_baseline",
                "operator_note": trimmed_note or None,
                "request_disposition": "rejected",
                "request_response": request_response,
                "feasibility_note": "infeasible",
                "diagnosis": diagnosis,
                "applied_policy": None,
            }
        )
        return payload
    summary = "Kept the first sample at the current policy so the size target can be measured."
    diagnosis = (
        "A direct AV1 size target is queueable for a safe first measurement, so the bench keeps the current policy "
        "and lets the real sample plus operator review decide the next change."
    )
    request_response = (
        "I kept this first sample at the current policy so we can compare the measured result to "
        f"{request.get('budget_label') or 'your size target'} before changing the policy."
    )
    seed_job_fields["seed_source"] = "operator_request"
    seed_job_fields["seed_summary"] = summary
    seed_job_fields["seed_diagnosis"] = diagnosis
    seed_job_fields["seed_request_disposition"] = "honored"
    seed_job_fields["seed_request_response"] = request_response
    seed_job_fields["seed_feasibility_note"] = request.get("feasibility")
    seed_job_fields["seed_applied_policy"] = None
    payload = advice_payload if advice_payload is not None else {}
    payload.update(
        {
            "ok": True,
            "summary": summary,
            "kind": "seed_baseline",
            "operator_note": trimmed_note or None,
            "request_disposition": "honored",
            "request_response": request_response,
            "feasibility_note": request.get("feasibility"),
            "diagnosis": diagnosis,
            "applied_policy": None,
        }
    )
    return payload


def folder_ai_tune_preview_action(
        config: MediaforceConfig,
        deps: FolderAiTuneDeps,
        normalized_prefix: str,
        note: str,
        host_key: str,
) -> dict[str, Any]:
    trimmed_note = note.strip()
    host = deps.resolve_sample_host(config, host_key)

    with open_db(config.paths.db_path) as connection:
        existing_job = deps.load_job_state(connection, config, normalized_prefix)
        if existing_job and existing_job.get("status") in {"queued", "running", "pending_review"}:
            return {"ok": False, "message": "A calibration job is already active for this folder."}
        latest_failed_sample_job_loader = deps.load_latest_failed_sample_job_state or deps.load_retryable_sample_job_state
        latest_failed_sample_job = _latest_failed_sample_job_payload(
            latest_failed_sample_job_loader(connection, config, normalized_prefix)
        )
        summary = inspect_prefix(connection, config, normalized_prefix)
        sample_item = deps.sample_item(connection, config, normalized_prefix)
        if sample_item is None:
            raise HTTPException(status_code=404, detail=f"No sample item found for {normalized_prefix}")
        calibration = deps.load_calibration_state(config, normalized_prefix)
        current_policy = object_dict(calibration.get("policy")) if calibration else object_dict(sample_item.get("resolved_policy"))
    operator_request = deps.operator_requested_experiment(
        trimmed_note,
        sample_item,
        current_policy=current_policy,
    )
    if calibration is None:
        with open_db(config.paths.db_path) as connection:
            return _seed_preview_action(
                config,
                deps,
                connection=connection,
                normalized_prefix=normalized_prefix,
                trimmed_note=trimmed_note,
                host=host,
                sample_item=sample_item,
                summary=summary,
                operator_request=operator_request,
                latest_failed_sample_job=latest_failed_sample_job,
            )
    return _tuned_preview_action(
        config,
        deps,
        normalized_prefix=normalized_prefix,
        trimmed_note=trimmed_note,
        host=host,
        sample_item=sample_item,
        summary=summary,
        operator_request=operator_request,
        calibration=calibration,
        latest_failed_sample_job=latest_failed_sample_job,
    )


def folder_ai_tune_confirm_action(
        config: MediaforceConfig,
        deps: FolderAiTuneDeps,
        normalized_prefix: str,
        proposal_id: str,
) -> dict[str, Any]:
    pending_proposal_raw = deps.load_pending_proposal(config, normalized_prefix)
    if pending_proposal_raw is None:
        if proposal_id.strip():
            return {"ok": False, "message": "This bench draft is out of date. Refresh it before queueing a sample."}
        return _retry_latest_sample_job(config, deps, normalized_prefix)
    pending_proposal = object_dict(pending_proposal_raw)
    if str(pending_proposal.get("proposal_id") or "") != proposal_id:
        return {"ok": False, "message": "This bench draft is out of date. Refresh it before queueing a sample."}
    if not pending_proposal.get("can_queue"):
        return {
            "ok": False,
            "message": str(pending_proposal.get("message") or "The current bench draft is not ready to queue."),
        }

    host_key = str(object_dict(pending_proposal.get("host")).get("key") or "").strip()
    host = deps.resolve_sample_host(config, host_key)
    operator_note = str(pending_proposal.get("operator_note") or "").strip()
    action = str(pending_proposal.get("action") or "ai_tune")
    applied_policy = object_dict(pending_proposal.get("applied_policy"))
    advice_payload = object_dict(pending_proposal.get("advice_payload"))

    with open_db(config.paths.db_path) as connection:
        existing_job = deps.load_job_state(connection, config, normalized_prefix)
        if existing_job and existing_job.get("status") in {"queued", "running", "pending_review"}:
            return {"ok": False, "message": "A calibration job is already active for this folder."}
        sample_item = deps.sample_item(connection, config, normalized_prefix)
        if sample_item is None:
            raise HTTPException(status_code=404, detail=f"No sample item found for {normalized_prefix}")
        calibration = deps.load_calibration_state(config, normalized_prefix)
        if action == "baseline" and calibration is not None:
            return {
                "ok": False,
                "message": "A measured draft already exists. Refresh the bench draft before queueing another sample.",
            }
        if action == "ai_tune" and calibration is None:
            return {"ok": False, "message": "The folder needs a first sample before the bench can tune a retry."}

        policy_source = object_dict(calibration.get("policy")) if calibration else object_dict(sample_item.get("resolved_policy"))
        final_policy = deps.apply_policy_fragment(policy_source, applied_policy)
        if advice_payload:
            deps.save_advice_state(config, normalized_prefix, advice_payload)

        if action == "ai_tune":
            tuning_record = object_dict(pending_proposal.get("tuning_record"))
            session_id = str(pending_proposal.get("session_id") or "").strip()
            if not session_id:
                session_toolbelt = object_dict(tuning_record.get("runtime_toolbelt"))
                operator_note_parse = object_dict(object_dict(pending_proposal.get("operator_request")).get("operator_note_parse"))
                if operator_note_parse:
                    session_toolbelt["operator_note_parse"] = operator_note_parse
                session_id = deps.record_tuning_session(
                    connection,
                    prefix=normalized_prefix,
                    note=operator_note,
                    response={
                        **advice_payload,
                        "prompt_version": tuning_record.get("prompt_version"),
                        "proposed_policy": tuning_record.get("proposed_policy"),
                    },
                    applied_policy=applied_policy,
                    toolbelt=session_toolbelt,
                    created_at=deps.now_iso(),
                    requested_experiment=object_dict(pending_proposal.get("operator_request")),
                )
            advice_payload["session_id"] = session_id
            learning_artifact = promote_learning_artifact(
                connection,
                config,
                session_id=session_id,
                prefix=normalized_prefix,
                note=operator_note,
                sample_item=object_dict(tuning_record.get("sample_item")),
                response=advice_payload,
                applied_policy=applied_policy,
                created_at=deps.now_iso(),
            )
            if learning_artifact is not None:
                advice_payload["learning_artifact"] = learning_artifact
            deps.save_advice_state(config, normalized_prefix, advice_payload)

        job_payload = {
            "job_id": uuid.uuid4().hex[:12],
            "status": "queued",
            "lane": "sample",
            "mode": "sample",
            "owner_pid": None,
            "prefix": normalized_prefix,
            "host": asdict(host),
            "action": action,
            "notes": operator_note,
            "policy": final_policy,
            "sample_item": _job_sample_item_payload(sample_item),
            "created_at": deps.now_iso(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "updated_at": deps.now_iso(),
        }
        if action == "baseline":
            job_payload.update(object_dict(pending_proposal.get("job_fields")))
        deps.save_job_state(connection, config, normalized_prefix, job_payload)

    deps.clear_pending_proposal(config, normalized_prefix)
    return {
        "ok": True,
        "message": "Queued the sample run from the bench draft.",
        "job": job_payload,
        "advice": advice_payload,
    }


def _retry_latest_sample_job(
        config: MediaforceConfig,
        deps: FolderAiTuneDeps,
        normalized_prefix: str,
) -> dict[str, Any]:
    with open_db(config.paths.db_path) as connection:
        existing_job = deps.load_retryable_sample_job_state(connection, config, normalized_prefix)
        if existing_job is None:
            return {"ok": False, "message": "Ask the bench for a draft first."}
        status = str(existing_job.get("status") or "").strip()
        if status in {"queued", "running", "pending_review"}:
            return {"ok": False, "message": "A calibration job is already active for this folder."}
        if status not in {"failed", "stopped"}:
            return {"ok": False, "message": "Ask the bench for a draft first."}
        if str(existing_job.get("mode") or "sample").strip() != "sample":
            return {"ok": False, "message": "Ask the bench for a draft first."}
        action = str(existing_job.get("action") or "").strip()
        if action not in {"baseline", "ai_tune"}:
            return {"ok": False, "message": "Ask the bench for a draft first."}

        stored_host = object_dict(existing_job.get("host"))
        host_key = str(stored_host.get("key") or "").strip()
        if not host_key:
            return {"ok": False, "message": "Refresh the bench draft before queueing another sample."}
        host = deps.resolve_sample_host(config, host_key)

        stored_sample_item = object_dict(existing_job.get("sample_item"))
        if not stored_sample_item:
            sample_item = deps.sample_item(connection, config, normalized_prefix)
            if sample_item is None:
                raise HTTPException(status_code=404, detail=f"No sample item found for {normalized_prefix}")
            stored_sample_item = _job_sample_item_payload(object_dict(sample_item))

        job_payload = {
            **existing_job,
            "job_id": uuid.uuid4().hex[:12],
            "status": "queued",
            "owner_pid": None,
            "host": asdict(host),
            "sample_item": stored_sample_item,
            "created_at": deps.now_iso(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "result": None,
            "updated_at": deps.now_iso(),
        }
        job_payload.pop("queue_position", None)
        job_payload.pop("queue_depth", None)
        deps.save_job_state(connection, config, normalized_prefix, job_payload)

    return {
        "ok": True,
        "message": "Queued the saved sample draft again.",
        "job": job_payload,
    }


def folder_ai_tune_action(
        config: MediaforceConfig,
        deps: FolderAiTuneDeps,
        normalized_prefix: str,
        note: str,
        host_key: str,
) -> dict[str, Any]:
    preview = folder_ai_tune_preview_action(config, deps, normalized_prefix, note, host_key)
    if not preview.get("ok"):
        return preview
    proposal = object_dict(preview.get("proposal"))
    proposal_id = str(proposal.get("proposal_id") or "")
    if not proposal_id:
        return {"ok": False, "message": "The bench draft could not be queued."}
    return folder_ai_tune_confirm_action(config, deps, normalized_prefix, proposal_id)


def _seed_preview_action(
        config: MediaforceConfig,
        deps: FolderAiTuneDeps,
        *,
        connection: DBClient,
        normalized_prefix: str,
        trimmed_note: str,
        host: Any,
        sample_item: dict[str, Any],
        summary: dict[str, Any],
        operator_request: dict[str, Any] | None,
        latest_failed_sample_job: dict[str, Any] | None,
) -> dict[str, Any]:
    base_policy = object_dict(sample_item.get("resolved_policy"))
    metric_support = deps.metric_support()
    seed_metadata_raw = deps.maybe_seed_baseline_policy(
        config=config,
        prefix=normalized_prefix,
        action="baseline",
        user_note=trimmed_note,
        base_policy=base_policy,
        sample_item=sample_item,
        existing_calibration=None,
        connection=connection,
        requested_experiment=operator_request,
        latest_failed_sample_job=latest_failed_sample_job,
    )
    seed_metadata = object_dict(seed_metadata_raw)
    seed_job_fields = object_dict(seed_metadata.get("job_fields"))
    seed_context_payload = object_dict(seed_job_fields.get("seed_context_payload"))
    learning_context = [object_dict(item) for item in object_list(seed_context_payload.get("retrieved_memory"))]
    seeded_policy = object_dict(seed_metadata.get("policy")) if seed_metadata_raw is not None else base_policy
    seed_fragment = object_dict(seed_job_fields.get("seed_applied_policy"))
    combined_fragment = seed_fragment
    measurement_fragment = _size_budget_measurement_fragment(operator_request, sample_item)
    if measurement_fragment is not None:
        combined_fragment = merge_policy_fragments(combined_fragment, measurement_fragment)
        seeded_policy = deps.apply_policy_fragment(base_policy, combined_fragment)
        seed_job_fields["seed_applied_policy"] = combined_fragment
    advice_payload_raw = deps.seed_advice_payload(trimmed_note, seed_metadata if seed_metadata_raw is not None else None)
    advice_payload = object_dict(advice_payload_raw) if advice_payload_raw is not None else None
    if advice_payload is None and operator_request:
        advice_payload = {
            "ok": True,
            "summary": "Bench noted the direct target, but kept the first sample near the base policy.",
            "raw": "",
            "kind": "seed_baseline",
            "operator_note": trimmed_note or None,
        }
    if advice_payload is not None and operator_request:
        advice_payload["operator_request"] = operator_request
    if advice_payload is not None and combined_fragment:
        advice_payload["applied_policy"] = combined_fragment
    advice_details = object_dict(advice_payload)
    if not combined_fragment and _can_keep_first_size_budget_sample(
            operator_request,
            latest_failed_sample_job=latest_failed_sample_job,
    ):
        advice_payload = _keep_first_size_budget_sample(
            advice_payload=advice_payload,
            seed_job_fields=seed_job_fields,
            trimmed_note=trimmed_note,
            operator_request=object_dict(operator_request),
        )
        advice_details = object_dict(advice_payload)
    alignment_operator_request = _operator_request_with_retrieved_memory_authority(
        operator_request,
        learning_context,
    )
    alignment_issue = deps.proposal_alignment_issue(
        operator_request=alignment_operator_request,
        request_disposition=advice_details.get("request_disposition"),
        current_policy=base_policy,
        preview_policy=seeded_policy,
    )
    blocking_evidence_issue = _blocking_sample_evidence_issue(
        operator_request=operator_request,
        latest_failed_sample_job=latest_failed_sample_job,
        current_policy=base_policy,
        preview_policy=seeded_policy,
    )
    if alignment_issue is None:
        alignment_issue = blocking_evidence_issue
    request_disposition = str(advice_details.get("request_disposition") or "").strip().lower()
    measurement_override_needed = (
        alignment_issue is not None or request_disposition in NONQUEUEABLE_DISPOSITIONS
    )
    if blocking_evidence_issue is None and measurement_override_needed and measurement_fragment is not None:
        seeded_policy = deps.apply_policy_fragment(base_policy, measurement_fragment) if measurement_fragment else base_policy
        seed_fragment = measurement_fragment
        combined_fragment = measurement_fragment
        alignment_issue = None
        seed_job_fields["seed_source"] = "operator_request" if measurement_fragment else "default"
        seed_job_fields["seed_summary"] = "Kept the first sample to the explicit request before measuring the size target."
        seed_job_fields["seed_diagnosis"] = (
            "A size budget is a planning target, so the first sample keeps only the operator's explicit "
            "request before trading quality or hard caps for size."
        )
        seed_job_fields["seed_request_disposition"] = "honored"
        seed_job_fields["seed_request_response"] = (
            "I kept this first sample to the explicit request so we can compare the measured result to your size target."
        )
        seed_job_fields["seed_feasibility_note"] = object_dict(operator_request).get("feasibility")
        seed_job_fields["seed_applied_policy"] = measurement_fragment or None
        if advice_payload is None:
            advice_payload = {}
        advice_payload.update(
            {
                "ok": True,
                "summary": seed_job_fields["seed_summary"],
                "kind": "seed_baseline",
                "operator_note": trimmed_note or None,
                "request_disposition": "honored",
                "request_response": seed_job_fields["seed_request_response"],
                "feasibility_note": seed_job_fields["seed_feasibility_note"],
                "diagnosis": seed_job_fields["seed_diagnosis"],
                "applied_policy": measurement_fragment,
            }
        )
        advice_details = object_dict(advice_payload)
    can_queue = _proposal_can_queue(
        applied_fragment=combined_fragment,
        preview_policy=seeded_policy,
        request_disposition=advice_details.get("request_disposition"),
        alignment_issue=alignment_issue,
    )
    proposal_message = _proposal_ready_message(
        can_queue=can_queue,
        alignment_issue=alignment_issue,
        has_policy_change=bool(combined_fragment),
        run_label="first sample",
    )
    proposal_created_at = deps.now_iso()
    session_toolbelt: dict[str, Any] = {}
    if latest_failed_sample_job is not None:
        session_toolbelt["latest_failed_sample_job"] = latest_failed_sample_job
    operator_note_parse = object_dict(object_dict(operator_request).get("operator_note_parse"))
    if operator_note_parse:
        session_toolbelt["operator_note_parse"] = operator_note_parse
    session_id = deps.record_tuning_session(
        connection,
        prefix=normalized_prefix,
        note=trimmed_note,
        response={
            **object_dict(advice_payload),
            "proposed_policy": object_dict(seed_job_fields.get("seed_proposed_policy")),
        },
        applied_policy=combined_fragment,
        toolbelt=session_toolbelt,
        created_at=proposal_created_at,
        requested_experiment=operator_request,
    )
    proposal_payload: dict[str, Any] = {
        "proposal_id": uuid.uuid4().hex[:12],
        "session_id": session_id,
        "status": "pending_confirmation",
        "kind": "seed_baseline",
        "action": "baseline",
        "created_at": proposal_created_at,
        "can_queue": can_queue,
        "message": proposal_message,
        "operator_note": trimmed_note or None,
        "operator_request": operator_request,
        "operator_signal": deps.proposal_signal_copy(
            trimmed_note,
            operator_request,
            False,
            advice_details.get("request_disposition"),
        ),
        "request_disposition": advice_details.get("request_disposition"),
        "request_response": advice_details.get("request_response"),
        "feasibility_note": advice_details.get("feasibility_note"),
        "summary": advice_details.get("summary") or "Drafted the initial sample.",
        "diagnosis": advice_details.get("diagnosis"),
        "confidence": advice_details.get("confidence"),
        "suggested_follow_up": advice_details.get("suggested_follow_up"),
        "applied_policy": combined_fragment,
        "preview_policy": seeded_policy,
        "current_policy": base_policy,
        "host": asdict(host),
        "self_check": None,
        "evidence_checked": [],
        "advice_payload": advice_payload,
        "job_fields": seed_job_fields,
        "metric_support": metric_support,
        "latest_failed_sample_job": latest_failed_sample_job,
        "trace": {
            "prompt_version": advice_details.get("prompt_version"),
            "raw_response": advice_details.get("raw"),
            "proposed_policy": object_dict(seed_job_fields.get("seed_proposed_policy")),
            "context": deps.proposal_context_snapshot(
                goal="Draft the first measured sample before anything queues.",
                current_policy=base_policy,
                sample_item=sample_item,
                summary=summary,
                metric_support=metric_support,
                requested_experiment=operator_request,
                latest_failed_sample_job=latest_failed_sample_job,
            ),
        },
    }
    deps.save_pending_proposal(config, normalized_prefix, proposal_payload)
    return {
        "ok": True,
        "message": "Bench draft ready. Nothing is queued until you confirm it." if can_queue else proposal_message,
        "proposal": deps.pending_proposal_public_view(proposal_payload),
    }


def _tuned_preview_action(
        config: MediaforceConfig,
        deps: FolderAiTuneDeps,
        *,
        normalized_prefix: str,
        trimmed_note: str,
        host: Any,
        sample_item: dict[str, Any],
        summary: dict[str, Any],
        operator_request: dict[str, Any] | None,
        calibration: dict[str, Any],
        latest_failed_sample_job: dict[str, Any] | None,
) -> dict[str, Any]:
    if not trimmed_note:
        raise HTTPException(status_code=400, detail="Add a note so the tuner knows what to change before running another sample.")
    current_policy = object_dict(calibration.get("policy")) if calibration else object_dict(sample_item.get("resolved_policy"))
    metric_support = deps.metric_support()
    with open_db(config.paths.db_path) as connection:
        learning_context = retrieve_learning_context(
            connection,
            prefix=normalized_prefix,
            sample_item=sample_item,
            note=trimmed_note,
        )
        recent_sessions_payload = deps.recent_tuning_sessions(connection, normalized_prefix, limit=4)
    repeat_signal = deps.matching_request_history(
        note=trimmed_note,
        sample_item=sample_item,
        recent_sessions_payload=recent_sessions_payload,
    )
    runtime_toolbelt = deps.build_tuning_runtime_toolbelt(
        sample_item=sample_item,
        current_policy=current_policy,
        calibration=calibration,
        metric_support=metric_support,
        operator_request=operator_request,
    )
    if latest_failed_sample_job is not None:
        runtime_toolbelt["latest_failed_sample_job"] = latest_failed_sample_job
    operator_note_parse = object_dict(object_dict(operator_request).get("operator_note_parse"))
    if operator_note_parse:
        runtime_toolbelt["operator_note_parse"] = operator_note_parse
    review_pack_dir = deps.review_pack_dir(config, normalized_prefix, uuid.uuid4().hex[:12])
    deps.remove_path_if_exists(review_pack_dir)
    multimodal_review_pack = deps.build_multimodal_review_pack(
        config=config,
        sample_item=sample_item,
        current_policy=current_policy,
        calibration=calibration,
        output_dir=review_pack_dir,
    )
    if multimodal_review_pack is None:
        deps.remove_path_if_exists(review_pack_dir)
    public_review_pack = deps.multimodal_review_pack_public_view(config, multimodal_review_pack)
    review_artifact_critique: dict[str, Any] | None = None
    if multimodal_review_pack is not None:
        critique = request_review_artifact_critique(
            project_root=config.paths.project_root,
            payload={
                "folder": normalized_prefix,
                "operator_note": trimmed_note,
                "requested_experiment": operator_request,
                "sample_item": {
                    "rel_path": sample_item["rel_path"],
                    "source_size_bytes": sample_item["source_size_bytes"],
                    "duration_seconds": sample_item["duration_seconds"],
                    "audio_summary": sample_item["audio_summary"],
                },
                "recent_calibration": calibration,
                "current_policy": current_policy,
                "runtime_toolbelt": runtime_toolbelt,
                "multimodal_review_pack": multimodal_review_pack,
            },
        )
        if critique.ok:
            review_artifact_critique = {
                "summary": critique.summary,
                "confidence": critique.confidence,
                "weakest_moments": critique.weakest_moments,
                "preserved_strengths": critique.preserved_strengths,
                "artifacts_to_recheck": critique.artifacts_to_recheck,
                "recommendation": critique.recommendation,
                "evidence_checked": critique.evidence_checked,
                "prompt_version": critique.prompt_version,
                "raw": critique.raw,
            }
    tuning_payload = {
        "folder": normalized_prefix,
        "operator_note": trimmed_note,
        "requested_experiment": operator_request,
        "summary": summary,
        "sample_item": {
            "rel_path": sample_item["rel_path"],
            "source_path": sample_item["source_path"],
            "source_size_bytes": sample_item["source_size_bytes"],
            "video_codec": sample_item["video_codec"],
            "duration_seconds": sample_item["duration_seconds"],
            "audio_summary": sample_item["audio_summary"],
            "subtitle_summary": sample_item["subtitle_summary"],
        },
        "policy": current_policy,
        "recent_calibration": calibration,
        "metric_support": metric_support,
        "runtime_toolbelt": runtime_toolbelt,
        "retrieved_memory": learning_context,
        "operator_repeat_signal": repeat_signal,
        "latest_failed_sample_job": latest_failed_sample_job,
    }
    if multimodal_review_pack is not None:
        tuning_payload["multimodal_review_pack"] = multimodal_review_pack
    if review_artifact_critique is not None:
        tuning_payload["review_artifact_critique"] = review_artifact_critique
    tuning = request_note_tuning(project_root=config.paths.project_root, payload=tuning_payload)
    tuned_policy, applied_fragment = apply_seed_policy(current_policy, object_dict(tuning.proposed_policy), mode="tune")
    measurement_fragment = _size_budget_measurement_fragment(operator_request, sample_item)
    combined_fragment = _honor_operator_source_resolution(
        operator_request=operator_request,
        combined_fragment=applied_fragment,
    )
    if measurement_fragment is not None:
        combined_fragment = merge_policy_fragments(combined_fragment, measurement_fragment)
    if combined_fragment != applied_fragment:
        tuned_policy = deps.apply_policy_fragment(current_policy, combined_fragment)
    advice_payload = object_dict(deps.tuning_advice_payload(tuning=tuning, note=trimmed_note, applied_fragment=combined_fragment))
    if public_review_pack is not None:
        advice_payload["multimodal_review_pack"] = public_review_pack
    if review_artifact_critique is not None:
        advice_payload["review_artifact_critique"] = review_artifact_critique
    advice_payload["retrieved_memory"] = learning_context
    if operator_request:
        advice_payload["operator_request"] = operator_request
    if combined_fragment:
        advice_payload["applied_policy"] = combined_fragment
    size_target_analysis = object_dict(runtime_toolbelt.get("size_target_analysis"))
    allow_measured_size_quality_tradeoff = allows_measured_size_quality_tradeoff(
        operator_request=operator_request,
        size_target_analysis=size_target_analysis,
        direction="smaller",
    )
    allow_measured_size_quality_increase = allows_measured_size_quality_tradeoff(
        operator_request=operator_request,
        size_target_analysis=size_target_analysis,
        direction="larger",
    )
    measured_budget_fragment = measured_size_budget_policy_fragment(
        operator_request=operator_request,
        size_target_analysis=size_target_analysis,
    )
    deterministic_measurement_fragment = object_dict(measurement_fragment)
    if measured_budget_fragment and _operator_forbids_hard_budget_cap(operator_request):
        measured_budget_fragment = {}
        advice_payload["budget_enforcement"] = {
            "status": "skipped_operator_priority",
            "size_target_analysis": size_target_analysis,
            "reason": "The operator prioritized source resolution over a conditional hard size ceiling.",
        }
    if measured_budget_fragment:
        measured_budget_fragment = _measured_budget_fragment_preserving_stricter_cap(
            combined_fragment, measured_budget_fragment
        )
        deterministic_measurement_fragment = merge_policy_fragments(
            deterministic_measurement_fragment,
            measured_budget_fragment,
        )
        combined_fragment = merge_policy_fragments(combined_fragment, measured_budget_fragment)
        combined_fragment = _honor_operator_source_resolution(
            operator_request=operator_request,
            combined_fragment=combined_fragment,
        )
        tuned_policy = deps.apply_policy_fragment(current_policy, combined_fragment)
        advice_payload["applied_policy"] = combined_fragment
        advice_payload["budget_enforcement"] = {
            "status": "enforced_after_miss",
            "size_target_analysis": size_target_analysis,
            "applied_policy": measured_budget_fragment,
        }
        if operator_request:
            size_budget_request = object_dict(operator_request.get("size_budget_request"))
            if size_budget_request:
                size_budget_request = {
                    **size_budget_request,
                    "applied_max_encoded_percent": object_dict(measured_budget_fragment.get("video")).get(
                        "max_encoded_percent"
                    ),
                    "applied_policy": merge_policy_fragments(
                        object_dict(size_budget_request.get("applied_policy")), measured_budget_fragment
                    ),
                }
            operator_request = {
                **operator_request,
                "applied_max_encoded_percent": object_dict(measured_budget_fragment.get("video")).get(
                    "max_encoded_percent"
                ),
                "applied_policy": merge_policy_fragments(
                    object_dict(operator_request.get("applied_policy")), measured_budget_fragment
                ),
            }
            if size_budget_request:
                operator_request["size_budget_request"] = size_budget_request
            advice_payload["operator_request"] = operator_request
    alignment_operator_request = _operator_request_with_retrieved_memory_authority(
        operator_request,
        learning_context,
    )
    request_disposition = tuning.request_disposition
    request_response = tuning.request_response
    proposal_summary = tuning.summary
    proposal_diagnosis = tuning.diagnosis
    proposal_confidence = tuning.confidence
    proposal_suggested_follow_up = tuning.suggested_follow_up
    alignment_issue = deps.proposal_alignment_issue(
        operator_request=alignment_operator_request,
        request_disposition=request_disposition,
        current_policy=current_policy,
        preview_policy=tuned_policy,
        allow_measured_size_quality_tradeoff=allow_measured_size_quality_tradeoff,
        allow_measured_size_quality_increase=allow_measured_size_quality_increase,
    )
    blocking_evidence_issue = _blocking_sample_evidence_issue(
        operator_request=operator_request,
        latest_failed_sample_job=latest_failed_sample_job,
        current_policy=current_policy,
        preview_policy=tuned_policy,
    )
    if alignment_issue is None:
        alignment_issue = blocking_evidence_issue
    measurement_override_needed = (
        alignment_issue is not None
        or str(request_disposition or "").strip().lower() in NONQUEUEABLE_DISPOSITIONS
    )
    if blocking_evidence_issue is None and measurement_override_needed and measurement_fragment is not None:
        combined_fragment = _honor_operator_source_resolution(
            operator_request=operator_request,
            combined_fragment=deterministic_measurement_fragment,
        )
        tuned_policy = deps.apply_policy_fragment(current_policy, combined_fragment)
        alignment_issue = None
        request_disposition = "honored"
        request_response = (
            "I kept this next sample to the explicit request so we can compare the measured result to your size target."
        )
        proposal_summary = "Kept the next sample to the explicit request before measuring the size target."
        proposal_diagnosis = (
            "The tuning worker did not produce a trustworthy draft, so the next sample uses the operator's explicit "
            "size and resolution request."
        )
        proposal_confidence = "low"
        proposal_suggested_follow_up = None
        advice_payload.update(
            {
                "ok": True,
                "summary": proposal_summary,
                "diagnosis": proposal_diagnosis,
                "confidence": proposal_confidence,
                "suggested_follow_up": proposal_suggested_follow_up,
                "request_disposition": request_disposition,
                "request_response": request_response,
                "applied_policy": combined_fragment,
            }
        )
    can_queue = _proposal_can_queue(
        applied_fragment=combined_fragment,
        preview_policy=tuned_policy,
        request_disposition=request_disposition,
        alignment_issue=alignment_issue,
    )
    proposal_message = _proposal_ready_message(
        can_queue=can_queue,
        alignment_issue=alignment_issue,
        has_policy_change=bool(combined_fragment),
        run_label="next sample",
    )
    proposal_created_at = deps.now_iso()
    with open_db(config.paths.db_path) as post_connection:
        session_id = deps.record_tuning_session(
            post_connection,
            prefix=normalized_prefix,
            note=trimmed_note,
            response={
                **advice_payload,
                "prompt_version": tuning.prompt_version,
                "proposed_policy": tuning.proposed_policy,
            },
            applied_policy=combined_fragment,
            toolbelt=runtime_toolbelt,
            created_at=proposal_created_at,
            requested_experiment=operator_request,
        )
    tune_proposal_payload: dict[str, Any] = {
        "proposal_id": uuid.uuid4().hex[:12],
        "session_id": session_id,
        "status": "pending_confirmation",
        "kind": "ai_tune",
        "action": "ai_tune",
        "created_at": proposal_created_at,
        "can_queue": can_queue,
        "message": proposal_message,
        "operator_note": trimmed_note,
        "operator_request": operator_request,
        "operator_signal": deps.proposal_signal_copy(trimmed_note, operator_request, True, request_disposition),
        "request_disposition": request_disposition,
        "request_response": request_response,
        "feasibility_note": tuning.feasibility_note,
        "summary": proposal_summary,
        "diagnosis": proposal_diagnosis,
        "confidence": proposal_confidence,
        "suggested_follow_up": proposal_suggested_follow_up,
        "applied_policy": combined_fragment,
        "preview_policy": tuned_policy,
        "current_policy": current_policy,
        "host": asdict(host),
        "self_check": tuning.self_check,
        "evidence_checked": tuning.evidence_checked,
        "advice_payload": advice_payload,
        "budget_enforcement": object_dict(advice_payload.get("budget_enforcement")) or None,
        "latest_failed_sample_job": latest_failed_sample_job,
        "trace": {
            "prompt_version": tuning.prompt_version,
            "raw_response": tuning.raw,
            "proposed_policy": object_dict(tuning.proposed_policy),
            "context": deps.proposal_context_snapshot(
                goal="Draft the next measured sample before anything queues.",
                current_policy=current_policy,
                sample_item=sample_item,
                runtime_toolbelt=runtime_toolbelt,
                learning_context=learning_context,
                recent_calibration=calibration,
                summary=summary,
                metric_support=metric_support,
                requested_experiment=operator_request,
                multimodal_review_pack=public_review_pack,
                review_artifact_critique=review_artifact_critique,
                latest_failed_sample_job=latest_failed_sample_job,
            ),
        },
        "tuning_record": {
            "prompt_version": tuning.prompt_version,
            "proposed_policy": tuning.proposed_policy,
            "runtime_toolbelt": runtime_toolbelt,
            "learning_context": learning_context,
            "sample_item": {
                "rel_path": sample_item["rel_path"],
                "source_path": sample_item["source_path"],
                "source_size_bytes": sample_item["source_size_bytes"],
                "video_codec": sample_item["video_codec"],
                "duration_seconds": sample_item["duration_seconds"],
                "audio_summary": sample_item["audio_summary"],
                "subtitle_summary": sample_item["subtitle_summary"],
            },
        },
    }
    if public_review_pack is not None:
        tune_proposal_payload["multimodal_review_pack"] = public_review_pack
    deps.save_pending_proposal(config, normalized_prefix, tune_proposal_payload)
    return {
        "ok": True,
        "message": tune_proposal_payload["message"],
        "proposal": deps.pending_proposal_public_view(tune_proposal_payload),
    }
