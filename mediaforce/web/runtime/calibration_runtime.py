import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.config import MediaforceConfig, load_config
from mediaforce.db import open_db
from mediaforce.process_control import ManagedProcessController, ProcessCancelledError
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
    recommend_review_timestamps: Any
    encode_preview_clips: Any
    render_source_review_clips: Any
    generate_compare_clips_from_previews: Any
    estimate_output_overhead_bytes: Any
    build_svt_params: Any
    review_url: Any
    encode_manifest_items: Any
    validate_manifest_items: Any
    generate_compare_clips: Any
    staged_artifact_columns: tuple[str, ...]


def snapshot_staged_artifact(
        connection: sqlite3.Connection,
        library_item_id: int,
        staged_artifact_columns: tuple[str, ...],
) -> dict[str, Any] | None:
    columns = ", ".join(staged_artifact_columns)
    row = connection.execute(
        f"SELECT {columns} FROM staged_artifacts WHERE library_item_id = ?",
        (library_item_id,),
    ).fetchone()
    if row is None:
        return None
    return {column: row[column] for column in staged_artifact_columns}


def restore_staged_artifact(
        connection: sqlite3.Connection,
        library_item_id: int,
        snapshot: dict[str, Any] | None,
        staged_artifact_columns: tuple[str, ...],
) -> None:
    if snapshot is None:
        connection.execute("DELETE FROM staged_artifacts WHERE library_item_id = ?", (library_item_id,))
        return

    columns = ", ".join(staged_artifact_columns)
    placeholders = ", ".join("?" for _ in staged_artifact_columns)
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in staged_artifact_columns
        if column != "library_item_id"
    )
    values = tuple(snapshot[column] for column in staged_artifact_columns)
    connection.execute(
        f"""
        INSERT INTO staged_artifacts ({columns})
        VALUES ({placeholders})
        ON CONFLICT(library_item_id) DO UPDATE SET {updates}
        """,
        values,
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
            sample_item = deps.sample_item(connection, config, prefix)
            if sample_item is None:
                raise RuntimeError(f"No sample item found for {prefix}")
            sample_item = dict(sample_item)
            sample_item["resolved_policy"] = policy
            library_item_id = int(sample_item["library_item_id"])
            staged_artifact_snapshot = snapshot_staged_artifact(
                connection,
                library_item_id,
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
                    "status": "failed",
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
                    "error": str(exc),
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
    video_policy = dict(policy["video"])
    width = int(sample_item.get("width") or 0) or None
    height = int(sample_item.get("height") or 0) or None
    preset = deps.effective_video_preset(video_policy, width=width, height=height)
    quality_result = deps.search_quality_for_source(
        source_path,
        video_policy,
        source_codec=str(sample_item.get("video_codec") or ""),
        width=width,
        height=height,
        process_controller=process_controller,
        host=host_data,
    )
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
        process_controller=process_controller,
        host=host_data,
    )

    timestamps = deps.recommend_review_timestamps(
        source_path,
        float(sample_item.get("duration_seconds") or 0.0),
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
    overhead = deps.estimate_output_overhead_bytes(sample_item)
    estimated_total_size_bytes = sample_result.predicted_encode_size_bytes + overhead["total_bytes"]

    payload = {
        "mode": "sample",
        "host": host_data,
        "action": action,
        "notes": notes,
        "policy": policy,
        "policy_seed": seed_metadata,
        "sample_item": {
            "rel_path": sample_item["rel_path"],
            "source_path": sample_item["source_path"],
            "source_size_bytes": sample_item["source_size_bytes"],
        },
        "sample_result": {
            "chosen_crf": quality_result.crf,
            "quality_metric": sample_result.metric,
            "quality_target": quality_result.target,
            "quality_score": sample_result.score,
            "predicted_video_size_bytes": sample_result.predicted_encode_size_bytes,
            "predicted_total_size_bytes": estimated_total_size_bytes,
            "predicted_encode_percent": (estimated_total_size_bytes / int(sample_item["source_size_bytes"])) * 100,
            "predicted_encode_seconds": sample_result.predicted_encode_seconds,
            "estimated_audio_bytes": overhead["audio_bytes"],
            "estimated_subtitle_bytes": overhead["subtitle_bytes"],
            "estimated_container_bytes": overhead["container_bytes"],
            "sample_stdout": sample_result.stdout,
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


def run_full_calibration(
        *,
        connection: sqlite3.Connection,
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
        "output_container": config.output_container,
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
        "sample_item": {
            "rel_path": sample_item["rel_path"],
            "source_path": sample_item["source_path"],
            "source_size_bytes": sample_item["source_size_bytes"],
        },
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
