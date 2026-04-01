import uuid
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import HTTPException

from mediaforce.advisor import apply_seed_policy, request_note_tuning
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.library.folder_profiles import inspect_prefix
from mediaforce.tuning.tuning_memory import promote_learning_artifact, retrieve_learning_context


@dataclass(slots=True)
class FolderAiTuneDeps:
    resolve_sample_host: Any
    load_job_state: Any
    sample_item: Any
    operator_requested_experiment: Any
    load_calibration_state: Any
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
        summary = inspect_prefix(connection, config, normalized_prefix)
        sample_item = deps.sample_item(connection, config, normalized_prefix)
        if sample_item is None:
            raise HTTPException(status_code=404, detail=f"No sample item found for {normalized_prefix}")
        operator_request = deps.operator_requested_experiment(trimmed_note, sample_item)
        calibration = deps.load_calibration_state(config, normalized_prefix)
        if calibration is None:
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
            )
        return _tuned_preview_action(
            config,
            deps,
            connection=connection,
            normalized_prefix=normalized_prefix,
            trimmed_note=trimmed_note,
            host=host,
            sample_item=sample_item,
            summary=summary,
            operator_request=operator_request,
            calibration=calibration,
        )


def folder_ai_tune_confirm_action(
        config: MediaforceConfig,
        deps: FolderAiTuneDeps,
        normalized_prefix: str,
        proposal_id: str,
) -> dict[str, Any]:
    pending_proposal = deps.load_pending_proposal(config, normalized_prefix)
    if pending_proposal is None:
        return {"ok": False, "message": "Ask the bench for a draft first."}
    if str(pending_proposal.get("proposal_id") or "") != proposal_id:
        return {"ok": False, "message": "This bench draft is out of date. Refresh it before queueing a sample."}
    if not pending_proposal.get("can_queue"):
        return {
            "ok": False,
            "message": str(pending_proposal.get("message") or "The current bench draft is not ready to queue."),
        }

    host_key = str(((pending_proposal.get("host") or {}).get("key") or "")).strip()
    host = deps.resolve_sample_host(config, host_key)
    operator_note = str(pending_proposal.get("operator_note") or "").strip()
    action = str(pending_proposal.get("action") or "ai_tune")
    applied_policy = dict(pending_proposal.get("applied_policy") or {})
    advice_payload = dict(pending_proposal.get("advice_payload") or {})

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

        policy_source = calibration.get("policy") if calibration else sample_item["resolved_policy"]
        final_policy = deps.apply_policy_fragment(dict(policy_source or {}), applied_policy)
        if advice_payload:
            deps.save_advice_state(config, normalized_prefix, advice_payload)

        if action == "ai_tune":
            tuning_record = dict(pending_proposal.get("tuning_record") or {})
            session_id = str(pending_proposal.get("session_id") or "").strip()
            if not session_id:
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
                    toolbelt=dict(tuning_record.get("runtime_toolbelt") or {}),
                    created_at=deps.now_iso(),
                )
            advice_payload["session_id"] = session_id
            learning_artifact = promote_learning_artifact(
                connection,
                config,
                session_id=session_id,
                prefix=normalized_prefix,
                note=operator_note,
                sample_item=dict(tuning_record.get("sample_item") or {}),
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
            "sample_item": {
                "rel_path": sample_item["rel_path"],
                "source_path": sample_item["source_path"],
                "source_size_bytes": sample_item["source_size_bytes"],
            },
            "created_at": deps.now_iso(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "updated_at": deps.now_iso(),
        }
        if action == "baseline":
            job_payload.update(dict(pending_proposal.get("job_fields") or {}))
        deps.save_job_state(connection, config, normalized_prefix, job_payload)

    deps.clear_pending_proposal(config, normalized_prefix)
    return {
        "ok": True,
        "message": "Queued the sample run from the bench draft.",
        "job": job_payload,
        "advice": advice_payload,
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
    proposal = dict(preview.get("proposal") or {})
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
) -> dict[str, Any]:
    base_policy = dict(sample_item["resolved_policy"] or {})
    metric_support = deps.metric_support()
    seed_metadata = deps.maybe_seed_baseline_policy(
        config=config,
        prefix=normalized_prefix,
        action="baseline",
        user_note=trimmed_note,
        base_policy=base_policy,
        sample_item=sample_item,
        existing_calibration=None,
        connection=connection,
    )
    seeded_policy = seed_metadata["policy"] if seed_metadata else base_policy
    seed_fragment = dict(((seed_metadata or {}).get("job_fields") or {}).get("seed_applied_policy") or {})
    combined_fragment = seed_fragment
    advice_payload = deps.seed_advice_payload(trimmed_note, seed_metadata)
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
    alignment_issue = deps.proposal_alignment_issue(
        operator_request=operator_request,
        request_disposition=(advice_payload or {}).get("request_disposition"),
        current_policy=base_policy,
        preview_policy=seeded_policy,
    )
    can_queue = bool(combined_fragment) and alignment_issue is None
    proposal_message = alignment_issue or "Review the bench draft, then confirm when you are ready to run the first sample."
    proposal_created_at = deps.now_iso()
    session_id = deps.record_tuning_session(
        connection,
        prefix=normalized_prefix,
        note=trimmed_note,
        response={
            **dict(advice_payload or {}),
            "proposed_policy": dict((((seed_metadata or {}).get("job_fields") or {}).get("seed_proposed_policy") or {})),
        },
        applied_policy=combined_fragment,
        toolbelt={},
        created_at=proposal_created_at,
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
            (advice_payload or {}).get("request_disposition"),
        ),
        "request_disposition": advice_payload.get("request_disposition") if advice_payload else None,
        "request_response": advice_payload.get("request_response") if advice_payload else None,
        "feasibility_note": advice_payload.get("feasibility_note") if advice_payload else None,
        "summary": advice_payload.get("summary") if advice_payload else "Drafted the initial sample.",
        "diagnosis": advice_payload.get("diagnosis") if advice_payload else None,
        "confidence": advice_payload.get("confidence") if advice_payload else None,
        "suggested_follow_up": advice_payload.get("suggested_follow_up") if advice_payload else None,
        "applied_policy": combined_fragment,
        "preview_policy": seeded_policy,
        "current_policy": base_policy,
        "host": asdict(host),
        "self_check": None,
        "evidence_checked": [],
        "advice_payload": advice_payload,
        "job_fields": dict((seed_metadata or {}).get("job_fields") or {}),
        "metric_support": metric_support,
        "trace": {
            "prompt_version": (advice_payload or {}).get("prompt_version"),
            "raw_response": (advice_payload or {}).get("raw"),
            "proposed_policy": dict(((seed_metadata or {}).get("job_fields") or {}).get("seed_proposed_policy") or {}),
            "context": deps.proposal_context_snapshot(
                goal="Draft the first measured sample before anything queues.",
                current_policy=base_policy,
                sample_item=sample_item,
                summary=summary,
                metric_support=metric_support,
                requested_experiment=operator_request,
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
        connection: DBClient,
        normalized_prefix: str,
        trimmed_note: str,
        host: Any,
        sample_item: dict[str, Any],
        summary: dict[str, Any],
        operator_request: dict[str, Any] | None,
        calibration: dict[str, Any],
) -> dict[str, Any]:
    if not trimmed_note:
        raise HTTPException(status_code=400, detail="Add a note so the tuner knows what to change before running another sample.")
    current_policy = dict((calibration.get("policy") if calibration else sample_item["resolved_policy"]) or {})
    metric_support = deps.metric_support()
    learning_context = retrieve_learning_context(
        connection,
        prefix=normalized_prefix,
        sample_item=sample_item,
        note=trimmed_note,
    )
    runtime_toolbelt = deps.build_tuning_runtime_toolbelt(
        sample_item=sample_item,
        current_policy=current_policy,
        calibration=calibration,
        metric_support=metric_support,
    )
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
    }
    if multimodal_review_pack is not None:
        tuning_payload["multimodal_review_pack"] = multimodal_review_pack
    tuning = request_note_tuning(project_root=config.paths.project_root, payload=tuning_payload)
    tuned_policy, applied_fragment = apply_seed_policy(current_policy, tuning.proposed_policy or {}, mode="tune")
    combined_fragment = applied_fragment
    advice_payload = deps.tuning_advice_payload(tuning=tuning, note=trimmed_note, applied_fragment=applied_fragment)
    if public_review_pack is not None:
        advice_payload["multimodal_review_pack"] = public_review_pack
    advice_payload["retrieved_memory"] = learning_context
    if operator_request:
        advice_payload["operator_request"] = operator_request
    if combined_fragment:
        advice_payload["applied_policy"] = combined_fragment
    alignment_issue = deps.proposal_alignment_issue(
        operator_request=operator_request,
        request_disposition=tuning.request_disposition,
        current_policy=current_policy,
        preview_policy=tuned_policy,
    )
    can_queue = bool(combined_fragment) and alignment_issue is None
    proposal_message = alignment_issue or (
        "Review the bench draft, then confirm when you are ready to run the next sample."
        if can_queue
        else "The bench did not produce a queueable draft yet. Adjust the note and ask again."
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
        "operator_signal": deps.proposal_signal_copy(trimmed_note, operator_request, True, tuning.request_disposition),
        "request_disposition": tuning.request_disposition,
        "request_response": tuning.request_response,
        "feasibility_note": tuning.feasibility_note,
        "summary": tuning.summary,
        "diagnosis": tuning.diagnosis,
        "confidence": tuning.confidence,
        "suggested_follow_up": tuning.suggested_follow_up,
        "applied_policy": combined_fragment,
        "preview_policy": tuned_policy,
        "current_policy": current_policy,
        "host": asdict(host),
        "self_check": tuning.self_check,
        "evidence_checked": tuning.evidence_checked,
        "advice_payload": advice_payload,
        "trace": {
            "prompt_version": tuning.prompt_version,
            "raw_response": tuning.raw,
            "proposed_policy": dict(tuning.proposed_policy or {}),
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
