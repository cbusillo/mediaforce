import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.config import MediaforceConfig, load_config
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.encoding.quality import quality_error_message, resolve_local_quality_temp_root
from mediaforce.encoding.video_filters import build_video_filter
from mediaforce.state_cleanup import purge_transient_artifacts


@dataclass(slots=True)
class CalibrationRunDeps:
    now_iso: Any
    load_job_state: Any
    sample_item: Any
    save_job_state: Any
    save_calibration_state: Any
    record_run_verdict: Any
    summarize_calibration_result: Any
    calibration_mode_for_action: Any
    effective_video_preset: Any
    search_quality_for_source: Any
    run_sample_encode: Any
    detect_video_crop: Any
    recommend_review_timestamps: Any
    encode_preview_clips: Any
    render_source_review_clips: Any
    generate_compare_clips_from_previews: Any
    resolve_stream_budget_ledger: Any
    build_svt_params: Any
    review_url: Any
    encode_manifest_items: Any
    validate_manifest_items: Any
    generate_compare_clips: Any
    staged_artifact_columns: tuple[str, ...]


def _stored_sample_item_payload(sample_item: dict[str, Any]) -> dict[str, Any]:
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
        "attachment_summary": (
            object_list(sample_item.get("attachment_summary"))
            if isinstance(sample_item.get("attachment_summary"), list)
            else None
        ),
        "output_container": sample_item.get("output_container"),
        "resolved_policy": object_dict(sample_item.get("resolved_policy")),
        "stream_budget_ledger": object_dict(sample_item.get("stream_budget_ledger")),
        "representative_source_id": sample_item.get("representative_source_id"),
        "representative_selection": object_dict(sample_item.get("representative_selection")),
    }


def _job_sample_item(job: dict[str, Any]) -> dict[str, Any] | None:
    sample_item = object_dict(job.get("sample_item"))
    required_keys = ("rel_path", "source_path", "source_size_bytes", "video_codec", "duration_seconds")
    if not sample_item or any(sample_item.get(key) in {None, ""} for key in required_keys):
        return None
    return sample_item


def _output_container(config: MediaforceConfig, sample_item: dict[str, Any]) -> str:
    configured = str(object_dict(config.raw.get("media")).get("output_container") or "").strip()
    if configured:
        return configured.removeprefix(".")
    item_container = str(sample_item.get("output_container") or sample_item.get("container") or "").strip()
    if item_container:
        return item_container.removeprefix(".")
    source_suffix = Path(str(sample_item.get("source_path") or "")).suffix
    return source_suffix.removeprefix(".") or "mkv"


def snapshot_staged_artifact(
        connection: DBClient,
        library_item_id: int,
        staged_artifact_columns: tuple[str, ...],
) -> dict[str, Any] | None:
    selected_columns = [getattr(staged_artifacts.c, column) for column in staged_artifact_columns]
    row = connection.execute(
        select(*selected_columns).where(staged_artifacts.c.library_item_id == library_item_id)
    ).mappings().fetchone()
    if row is None:
        return None
    return {column: row[column] for column in staged_artifact_columns}


def restore_staged_artifact(
        connection: DBClient,
        library_item_id: int,
        snapshot: dict[str, Any] | None,
        staged_artifact_columns: tuple[str, ...],
) -> None:
    if snapshot is None:
        connection.execute(delete(staged_artifacts).where(staged_artifacts.c.library_item_id == library_item_id))
        return

    values = {column: snapshot[column] for column in staged_artifact_columns}
    connection.execute(
        sqlite_insert(staged_artifacts)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[staged_artifacts.c.library_item_id],
            set_={column: values[column] for column in staged_artifact_columns if column != "library_item_id"},
        )
    )


def remove_path(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def run_calibration_job(
        *,
        config_path: Path,
        prefix: str,
        action: str,
        host_data: dict[str, Any],
        notes: str,
        policy: dict[str, Any],
        job_id: str,
        seed_metadata: dict[str, Any] | None,
        process_controller: ManagedProcessController,
        deps: CalibrationRunDeps,
) -> None:
    config = load_config(config_path)
    purge_transient_artifacts(config, force=True)
    process_controller.throw_if_cancelled()
    with open_db(config.paths.db_path) as connection:
        job = deps.load_job_state(connection, config, prefix) or {}
        started_at = str(job.get("started_at") or deps.now_iso())
        job.update({"status": "running", "started_at": started_at, "finished_at": None, "error": None,
                    "updated_at": deps.now_iso()})
        deps.save_job_state(connection, config, prefix, job)

    calibration_dir: Path | None = None
    manifest_path: Path | None = None
    library_item_id: int | None = None
    staged_artifact_snapshot: dict[str, Any] | None = None

    try:
        with open_db(config.paths.db_path) as connection:
            sample_item = _job_sample_item(job)
            if sample_item is None:
                loaded_sample_item = deps.sample_item(connection, config, prefix)
                if loaded_sample_item is None:
                    raise RuntimeError(f"No sample item found for {prefix}")
                sample_item = object_dict(loaded_sample_item)
            sample_item["resolved_policy"] = policy
            output_container = _output_container(config, sample_item)
            sample_item["output_container"] = output_container
            stream_budget = deps.resolve_stream_budget_ledger(
                sample_item,
                default_video_policy=object_dict(config.raw.get("video")) or object_dict(policy.get("video")),
                output_container=output_container,
                prefer_persisted=False,
            )
            sample_item["stream_budget_ledger"] = stream_budget.to_payload()
            job["sample_item"] = _stored_sample_item_payload(sample_item)
            deps.save_job_state(connection, config, prefix, job)
            current_library_item_id = int_value(sample_item.get("library_item_id"))
            library_item_id = current_library_item_id
            staged_artifact_snapshot = snapshot_staged_artifact(
                connection,
                current_library_item_id,
                deps.staged_artifact_columns,
            )

            calibration_run_id = uuid.uuid4().hex[:12]
            if deps.calibration_mode_for_action(action) == "full":
                calibration_payload, manifest_path, calibration_dir = run_full_calibration(
                    connection=connection,
                    config=config,
                    prefix=prefix,
                    action=action,
                    host_data=host_data,
                    notes=notes,
                    policy=policy,
                    seed_metadata=seed_metadata,
                    sample_item=sample_item,
                    calibration_run_id=calibration_run_id,
                    process_controller=process_controller,
                    deps=deps,
                )
            else:
                calibration_payload, calibration_dir = run_sampled_calibration(
                    config=config,
                    prefix=prefix,
                    action=action,
                    host_data=host_data,
                    notes=notes,
                    policy=policy,
                    seed_metadata=seed_metadata,
                    sample_item=sample_item,
                    calibration_run_id=calibration_run_id,
                    process_controller=process_controller,
                    deps=deps,
                )

        calibration_payload["job_id"] = job_id
        deps.save_calibration_state(config, prefix, calibration_payload)
        deps.record_run_verdict(config, prefix, calibration_payload)
        with open_db(config.paths.db_path) as connection:
            deps.save_job_state(
                connection,
                config,
                prefix,
                {
                    **job,
                    "job_id": job_id,
                    "status": "completed",
                    "finished_at": deps.now_iso(),
                    "error": None,
                    "result": deps.summarize_calibration_result(calibration_payload),
                },
            )
    except ProcessCancelledError:
        with open_db(config.paths.db_path) as connection:
            deps.save_job_state(
                connection,
                config,
                prefix,
                {
                    **job,
                    "job_id": job_id,
                    "status": "stopped",
                    "finished_at": deps.now_iso(),
                    "error": "Calibration queue job was stopped and cleaned up.",
                },
            )
    except Exception as exc:
        with open_db(config.paths.db_path) as connection:
            deps.save_job_state(
                connection,
                config,
                prefix,
                {
                    **job,
                    "job_id": job_id,
                    "status": "failed",
                    "finished_at": deps.now_iso(),
                    "error": quality_error_message(exc),
                },
            )
    finally:
        if library_item_id is not None:
            with open_db(config.paths.db_path) as connection:
                restore_staged_artifact(
                    connection,
                    library_item_id,
                    staged_artifact_snapshot,
                    deps.staged_artifact_columns,
                )
        remove_path(manifest_path)
        remove_path(calibration_dir)
        purge_transient_artifacts(config, force=True)


def run_sampled_calibration(
        *,
        config: MediaforceConfig,
        prefix: str,
        action: str,
        host_data: dict[str, Any],
        notes: str,
        policy: dict[str, Any],
        seed_metadata: dict[str, Any] | None,
        sample_item: dict[str, Any],
        calibration_run_id: str,
        process_controller: ManagedProcessController,
        deps: CalibrationRunDeps,
) -> tuple[dict[str, Any], Path | None]:
    _ = prefix
    source_path = Path(sample_item["source_path"])
    quality_host = _quality_host_data(config, host_data)
    quality_temp_dir = _quality_temp_dir_for_host(config, quality_host)
    video_policy = object_dict(policy.get("video"))
    stream_budget = deps.resolve_stream_budget_ledger(
        sample_item,
        default_video_policy=object_dict(config.raw.get("video")) or video_policy,
        output_container=_output_container(config, sample_item),
    )
    stream_budget.require_positive_target_video_budget()
    sample_item["stream_budget_ledger"] = stream_budget.to_payload()
    sample_item["output_container"] = _output_container(config, sample_item)
    width = int_value(sample_item.get("width")) or None
    height = int_value(sample_item.get("height")) or None
    cadence_decision = (
        object_dict(sample_item.get("cadence_decision"))
        if "cadence_decision" in sample_item
        else None
    )
    cadence_evidence = (
        object_dict(sample_item.get("cadence_evidence"))
        if "cadence_evidence" in sample_item
        else None
    )
    preset = deps.effective_video_preset(video_policy, width=width, height=height)
    detected_crop = deps.detect_video_crop(
        source_path,
        video_policy,
        source_codec=str(sample_item.get("video_codec") or ""),
        width=width,
        height=height,
        duration_seconds=sample_item.get("duration_seconds"),
        process_controller=process_controller,
        host=quality_host,
    )
    video_filter = build_video_filter(
        video_policy,
        width=width,
        height=height,
        detected_crop=detected_crop,
        cadence_decision=cadence_decision,
        cadence_evidence=cadence_evidence,
        cadence_source_fingerprint=str(sample_item.get("source_fingerprint") or "") or None,
    )
    quality_kwargs: dict[str, Any] = {
        "source_codec": str(sample_item.get("video_codec") or ""),
        "width": width,
        "height": height,
        "detected_crop": detected_crop,
        "process_controller": process_controller,
        "host": quality_host,
        "quality_temp_dir": quality_temp_dir,
        "stream_budget_ledger": stream_budget,
    }
    if cadence_decision is not None:
        quality_kwargs["cadence_decision"] = cadence_decision
        quality_kwargs["cadence_evidence"] = cadence_evidence
        quality_kwargs["cadence_source_fingerprint"] = (
            str(sample_item.get("source_fingerprint") or "") or None
        )
    quality_result = deps.search_quality_for_source(source_path, video_policy, **quality_kwargs)
    sample_result = deps.run_sample_encode(
        source_path,
        source_codec=str(sample_item.get("video_codec") or ""),
        preferred_metric=str(video_policy.get("quality_metric", "auto")),
        crf=quality_result.crf,
        preset=preset,
        pixel_format=str(video_policy["pixel_format"]),
        sample_every=str(video_policy["sample_every"]),
        sample_duration=str(video_policy["sample_duration"]),
        svt_params=deps.build_svt_params(video_policy),
        video_filter=video_filter,
        process_controller=process_controller,
        host=quality_host,
        quality_temp_dir=quality_temp_dir,
    )

    timestamps = deps.recommend_review_timestamps(
        source_path,
        float_value(sample_item.get("duration_seconds")),
        8.0,
        process_controller=process_controller,
    )
    output_dir = config.paths.review_dir / calibration_run_id / "item-00"
    preview_clips = deps.encode_preview_clips(
        source_path=source_path,
        source_codec=str(sample_item.get("video_codec") or ""),
        output_dir=output_dir,
        timestamps=timestamps,
        duration_seconds=8.0,
        encoder=str(video_policy["encoder"]),
        pixel_format=str(video_policy["pixel_format"]),
        preset=preset,
        crf=quality_result.crf,
        svt_params=deps.build_svt_params(video_policy),
        video_filter=video_filter,
        host=host_data,
        process_controller=process_controller,
    )
    source_clips = deps.render_source_review_clips(
        source_path=source_path,
        source_codec=str(sample_item.get("video_codec") or ""),
        output_dir=output_dir,
        timestamps=timestamps,
        duration_seconds=8.0,
        process_controller=process_controller,
    )
    compare_clips = deps.generate_compare_clips_from_previews(
        source_path=source_path,
        source_codec=str(sample_item.get("video_codec") or ""),
        previews=preview_clips,
        output_dir=output_dir,
        process_controller=process_controller,
    )
    estimated_total_size_bytes = (
        sample_result.predicted_encode_size_bytes + stream_budget.non_video_bytes
        if stream_budget.non_video_bytes is not None
        else None
    )
    target_size_trace = object_dict(getattr(quality_result, "target_size_trace", None)) or None

    payload = {
        "mode": "sample",
        "host": host_data,
        "action": action,
        "notes": notes,
        "policy": policy,
        "policy_seed": seed_metadata,
        "sample_item": _stored_sample_item_payload(sample_item),
        "sample_result": {
            "chosen_crf": quality_result.crf,
            "quality_metric": sample_result.metric,
            "quality_target": quality_result.target,
            "quality_score": sample_result.score,
            "predicted_video_size_bytes": sample_result.predicted_encode_size_bytes,
            "predicted_total_size_bytes": estimated_total_size_bytes,
            "predicted_encode_percent": (
                (estimated_total_size_bytes / int(sample_item["source_size_bytes"])) * 100
                if estimated_total_size_bytes is not None
                else None
            ),
            "predicted_encode_seconds": sample_result.predicted_encode_seconds,
            "sampled_clip_bytes": sample_result.sampled_clip_size_bytes,
            "estimated_audio_bytes": stream_budget.audio_bytes,
            "estimated_subtitle_bytes": stream_budget.subtitle_bytes,
            "estimated_attachment_bytes": stream_budget.attachment_bytes,
            "estimated_container_bytes": stream_budget.container_bytes,
            "stream_budget_ledger": stream_budget.to_payload(),
            "target_size_trace": target_size_trace,
            "sample_stdout": sample_result.stdout,
            "cadence_evidence_id": cadence_decision.get("evidence_id") if cadence_decision else None,
            "cadence_class": cadence_decision.get("classification") if cadence_decision else None,
            "cadence_transform": cadence_decision.get("transform") if cadence_decision else None,
        },
        "compare_clips": [
            {
                "path": deps.review_url(config, clip.output_path),
                "timestamp_seconds": clip.timestamp_seconds,
                "duration_seconds": clip.duration_seconds,
            }
            for clip in compare_clips
        ],
        "preview_clips": [
            {
                "path": deps.review_url(config, clip.output_path),
                "timestamp_seconds": clip.timestamp_seconds,
                "duration_seconds": clip.duration_seconds,
                "size_bytes": clip.size_bytes,
            }
            for clip in preview_clips
        ],
        "source_clips": [
            {
                "path": deps.review_url(config, clip.output_path),
                "timestamp_seconds": clip.timestamp_seconds,
                "duration_seconds": clip.duration_seconds,
                "size_bytes": clip.size_bytes,
            }
            for clip in source_clips
        ],
    }
    return payload, None


def _configured_host_record(config: MediaforceConfig, host_data: dict[str, Any]) -> dict[str, Any] | None:
    host_key = str(host_data.get("key") or host_data.get("host") or host_data.get("label") or "").strip()
    if not host_key:
        return None
    for host in config.remote_hosts:
        ssh_host = str(host.get("host") or "").strip()
        label = str(host.get("label") or ssh_host or "remote").strip()
        if host_key in {ssh_host, label}:
            return host
    return None


def _quality_host_data(config: MediaforceConfig, host_data: dict[str, Any]) -> dict[str, Any]:
    configured_host = _configured_host_record(config, host_data)
    if configured_host is None:
        return host_data

    media_access = str(host_data.get("media_access") or configured_host.get("media_access") or "").strip()
    merged = {
        **configured_host,
        **host_data,
        "media_access": media_access or configured_host.get("media_access") or "",
    }
    if str(merged.get("media_access") or "").strip().lower() == "stream":
        merged["mode"] = "local"
    return merged


def _quality_temp_dir_for_host(config: MediaforceConfig, host_data: dict[str, Any]) -> Path:
    if str(host_data.get("media_access") or "").strip().lower() == "stream":
        return resolve_local_quality_temp_root(
            config.staging_root,
            config.paths.web_state_dir / "quality-temp",
        )

    staging_root = str(host_data.get("staging_root") or "").strip()
    if staging_root:
        return Path(staging_root).expanduser()

    configured_host = _configured_host_record(config, host_data)
    if configured_host is not None:
        if str(configured_host.get("media_access") or "").strip().lower() == "stream":
            return resolve_local_quality_temp_root(
                config.staging_root,
                config.paths.web_state_dir / "quality-temp",
            )
        return config.staging_root_for_host(configured_host)

    return config.staging_root_for_host(host_data)


def run_full_calibration(
        *,
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        action: str,
        host_data: dict[str, Any],
        notes: str,
        policy: dict[str, Any],
        seed_metadata: dict[str, Any] | None,
        sample_item: dict[str, Any],
        calibration_run_id: str,
        process_controller: ManagedProcessController,
        deps: CalibrationRunDeps,
) -> tuple[dict[str, Any], Path, Path]:
    manifest_item = dict(sample_item)
    rel_output = Path(prefix) / Path(sample_item["source_path"]).name
    calibration_staging = config.staging_root / "_calibration" / calibration_run_id / rel_output.name
    calibration_dir = calibration_staging.parent
    manifest_item["staging_path"] = str(calibration_staging)
    manifest = {
        "run_id": calibration_run_id,
        "created_at": deps.now_iso(),
        "config_path": str(config.paths.config_path),
        "db_path": str(config.paths.db_path),
        "staging_root": str(config.staging_root),
        "output_container": _output_container(config, sample_item),
        "items": [manifest_item],
    }
    manifest_path = config.paths.web_state_dir / f"calibration-{calibration_run_id}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    encode_result = deps.encode_manifest_items(
        connection,
        config,
        manifest_path,
        manifest,
        [0],
        overwrite=True,
        process_controller=process_controller,
        encode_context={"origin": "calibration"},
    )[0]
    validation_result = deps.validate_manifest_items(connection, config, manifest, [0])[0]
    output_dir = config.paths.review_dir / calibration_run_id
    clips = deps.generate_compare_clips(
        connection,
        manifest,
        [0],
        output_dir=output_dir,
        duration_seconds=8.0,
        timestamps=None,
        play=False,
        process_controller=process_controller,
    )
    payload = {
        "mode": "full",
        "host": host_data,
        "action": action,
        "notes": notes,
        "policy": policy,
        "policy_seed": seed_metadata,
        "manifest_path": str(manifest_path),
        "sample_item": _stored_sample_item_payload(sample_item),
        "encode_result": {
            "staging_path": str(encode_result.staging_path),
            "source_size_bytes": encode_result.source_size_bytes,
            "staging_size_bytes": encode_result.staging_size_bytes,
            "chosen_crf": encode_result.chosen_crf,
            "quality_metric": encode_result.quality_metric,
            "quality_target": encode_result.quality_target,
            "quality_score": encode_result.quality_score,
        },
        "validation_result": validation_result,
        "compare_clips": [
            {
                "path": deps.review_url(config, clip.output_path),
                "timestamp_seconds": clip.timestamp_seconds,
                "duration_seconds": clip.duration_seconds,
            }
            for clip in clips
        ],
    }
    return payload, manifest_path, calibration_dir
