import uuid
from typing import Any

from fastapi import HTTPException

from mediaforce.core.config import MediaforceConfig, load_config
from mediaforce.core.db import open_db
from mediaforce.core.type_defs import object_dict
from mediaforce.library.run_manifests import create_folder_manifest


def queue_folder_encode_action(
        config: MediaforceConfig,
        normalized_prefix: str,
        notes: str,
        bypass_schedule: bool,
        *,
        now_iso: Any,
        load_job_state: Any,
        load_calibration_state: Any,
        review_gate: Any,
        upsert_override: Any,
        save_encode_job: Any,
) -> dict[str, Any]:
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
        saved_profile_path = config.paths.config_path.parent / "folder-defaults.toml"
        calibration_payload = object_dict(calibration)
        upsert_override(saved_profile_path, normalized_prefix, calibration_payload["policy"])
        refreshed_config = load_config(config.paths.config_path)
        manifest, manifest_path = create_folder_manifest(connection, refreshed_config, prefix=normalized_prefix)
        if not manifest["items"]:
            raise HTTPException(status_code=400, detail="No pending items were found to enqueue for this folder.")
        queue_job = {
            "job_id": uuid.uuid4().hex[:12],
            "prefix": normalized_prefix,
            "status": "queued",
            "manifest_path": str(manifest_path),
            "item_count": len(manifest["items"]),
            "saved_profile_path": str(saved_profile_path),
            "host": {},
            "notes": notes.strip(),
            "bypass_schedule": bypass_schedule,
            "process_pid": None,
            "error": None,
            "created_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "updated_at": now_iso(),
        }
        save_encode_job(connection, queue_job)
    return {"ok": True, "message": "Queued the full folder encode.", "job": queue_job}


def save_profile_action(
        config: MediaforceConfig,
        normalized_prefix: str,
        *,
        now_iso: Any,
        load_calibration_state: Any,
        calibration_draft_hash: Any,
        save_calibration_state: Any,
        load_advice_state: Any,
        record_visual_approval_artifact: Any,
        merge_advice_state: Any,
        upsert_override: Any,
) -> dict[str, Any]:
    calibration = load_calibration_state(config, normalized_prefix)
    if not calibration:
        raise HTTPException(status_code=400, detail="No draft calibration found for this folder")
    calibration_payload = object_dict(calibration)
    if str(calibration_payload.get("mode") or "sample") == "sample":
        if not calibration_payload.get("review_media_ready"):
            raise HTTPException(
                status_code=400,
                detail="Run a fresh sample before approving because the review clips are unavailable.",
            )
        calibration_payload["accepted_at"] = now_iso()
        calibration_payload["accepted_draft_hash"] = str(
            calibration_payload.get("draft_hash") or calibration_draft_hash(calibration_payload)
        )
        calibration_payload["accepted_sample_job_id"] = str(calibration_payload.get("job_id") or "")
        save_calibration_state(config, normalized_prefix, calibration_payload)
        advice_state = object_dict(load_advice_state(config, normalized_prefix))
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
                    },
                )
    upsert_override(
        config.paths.config_path.parent / "folder-defaults.toml",
        normalized_prefix,
        calibration_payload["policy"],
    )
    return {"ok": True, "message": "Approved the current draft and saved it as the folder profile."}
