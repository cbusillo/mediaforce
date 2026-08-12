import json
import logging
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.config import MediaforceConfig, load_config
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.encoding.helpers import resolve_item_source_path
from mediaforce.encoding.quality import quality_error_message, resolve_local_quality_temp_root
from mediaforce.encoding.video_filters import build_video_filter, planned_output_dimensions
from mediaforce.hosts.config import host_media_access_for_host
from mediaforce.reviewing.artifact_identity import reviewed_artifact_fingerprint
from mediaforce.state_cleanup import purge_transient_artifacts
from mediaforce.tuning.content_intent_observations import (
    build_content_intent_boundary_compatibility,
    content_intent_frame_rate,
    content_intent_stream_plan_id,
)
from mediaforce.tuning.av1_cold_start import unavailable_av1_cold_start_prediction
from mediaforce.tuning.target_size_search import TargetSizeSearchError

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class CalibrationRunDeps:
    now_iso: Any
    ensure_sample_host_ready: Any
    load_job_state: Any
    sample_item: Any
    save_job_state: Any
    update_job_telemetry: Any
    calibration_job_compatibility_key: Any
    calibration_heartbeat_seconds: float
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
    generate_compare_clips_from_review_pairs: Any
    resolve_stream_budget_ledger: Any
    build_svt_params: Any
    review_url: Any
    encode_manifest_items: Any
    validate_manifest_items: Any
    generate_compare_clips: Any
    staged_artifact_columns: tuple[str, ...]
    recommend_review_moments: Any | None = None
    review_moment_payload: Any | None = None
    default_review_timestamps: Any | None = None
    quality_toolchain_identity: Any | None = None
    select_quality_metric: Any | None = None
    plan_av1_cold_start: Any | None = None
    secure_review_artifacts: Any | None = None


class _CalibrationTelemetry:
    def __init__(
            self,
            config: MediaforceConfig,
            job_id: str,
            payload: dict[str, Any],
            deps: CalibrationRunDeps,
    ) -> None:
        self.config = config
        self.job_id = job_id
        self.deps = deps
        self.started_monotonic = time.monotonic()
        self.stop_event = threading.Event()
        self.write_lock = threading.Lock()
        self.thread: threading.Thread | None = None
        now = deps.now_iso()
        self.progress: dict[str, Any] = {
            "schema_version": 1,
            "stage": "preparing_source",
            "stage_started_at": now,
            "last_progress_at": now,
            "compatibility_key": deps.calibration_job_compatibility_key(payload),
        }

    def start(self) -> None:
        self._persist(progress=self.progress)
        self.thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"calibration-heartbeat-{self.job_id[:8]}",
        )
        self.thread.start()

    def update(self, stage: str, *, completed: int | None = None, total: int | None = None) -> None:
        now = self.deps.now_iso()
        if self.progress.get("stage") != stage:
            self.progress["stage"] = stage
            self.progress["stage_started_at"] = now
        self.progress["last_progress_at"] = now
        if completed is not None and total is not None and total > 0:
            self.progress["work"] = {
                "completed": max(0, min(completed, total)),
                "total": total,
            }
        else:
            self.progress.pop("work", None)
        self._persist(progress=self.progress)

    def refresh_compatibility(self, payload: dict[str, Any]) -> None:
        compatibility_key = self.deps.calibration_job_compatibility_key(payload)
        if self.progress.get("compatibility_key") == compatibility_key:
            return
        self.progress["compatibility_key"] = compatibility_key
        self.progress["last_progress_at"] = self.deps.now_iso()
        self._persist(progress=self.progress)

    def finish(self, terminal_status: str | None) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
        if terminal_status is None:
            return
        now = self.deps.now_iso()
        self.progress["terminal_status"] = terminal_status
        self.progress["last_progress_at"] = now
        self.progress["total_elapsed_seconds"] = max(0, round(time.monotonic() - self.started_monotonic))
        if terminal_status == "completed":
            self.progress["stage"] = "completed"
            self.progress["stage_started_at"] = now
            self.progress.pop("work", None)
        self._persist(progress=self.progress)

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(self.deps.calibration_heartbeat_seconds):
            self._persist()

    def _persist(self, *, progress: dict[str, Any] | None = None) -> None:
        with self.write_lock:
            heartbeat_at = self.deps.now_iso()
            try:
                with open_db(self.config.paths.db_path) as connection:
                    self.deps.update_job_telemetry(
                        connection,
                        self.job_id,
                        heartbeat_at=heartbeat_at,
                        progress=dict(progress) if progress is not None else None,
                    )
            except Exception:
                LOGGER.warning("Unable to persist calibration telemetry for %s", self.job_id, exc_info=True)


def _stored_sample_item_payload(sample_item: dict[str, Any]) -> dict[str, Any]:
    return {
        "library_item_id": sample_item.get("library_item_id"),
        "media_root": sample_item.get("media_root"),
        "rel_path": sample_item.get("rel_path"),
        "source_path": sample_item.get("source_path"),
        "source_fingerprint": sample_item.get("source_fingerprint"),
        "content_version_fingerprint": sample_item.get("content_version_fingerprint"),
        "source_size_bytes": sample_item.get("source_size_bytes"),
        "source_snapshot_sha256": sample_item.get("source_snapshot_sha256"),
        "source_snapshot_size_bytes": sample_item.get("source_snapshot_size_bytes"),
        "source_snapshot_content_version_fingerprint": sample_item.get(
            "source_snapshot_content_version_fingerprint"
        ),
        "video_codec": sample_item.get("video_codec"),
        "video_bitrate": sample_item.get("video_bitrate"),
        "width": sample_item.get("width"),
        "height": sample_item.get("height"),
        "cadence_class": sample_item.get("cadence_class"),
        "cadence_summary": object_dict(sample_item.get("cadence_summary")),
        "cadence_evidence": object_dict(sample_item.get("cadence_evidence")),
        "cadence_decision": object_dict(sample_item.get("cadence_decision")),
        "media_fingerprint": object_dict(sample_item.get("media_fingerprint")),
        "media_fingerprint_evidence": object_dict(sample_item.get("media_fingerprint_evidence")),
        "media_fingerprint_decision": object_dict(sample_item.get("media_fingerprint_decision")),
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
        "resolved_operator_intent": object_dict(sample_item.get("resolved_operator_intent")),
        "compression_intent": object_dict(sample_item.get("compression_intent")),
        "stream_budget_ledger": object_dict(sample_item.get("stream_budget_ledger")),
        "representative_source_id": sample_item.get("representative_source_id"),
        "representative_selection": object_dict(sample_item.get("representative_selection")),
    }


def _job_sample_item(job: dict[str, Any]) -> dict[str, Any] | None:
    sample_item = object_dict(job.get("sample_item"))
    rel_path = str(sample_item.get("rel_path") or "").strip()
    if not sample_item.get("media_root") and rel_path:
        rel_parts = Path(rel_path).parts
        if rel_parts:
            sample_item["media_root"] = rel_parts[0]
    required_keys = ("media_root", "rel_path", "source_path", "source_size_bytes", "video_codec", "duration_seconds")
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


def _content_intent_compatibility_payload(
        *,
        sample_item: dict[str, Any],
        video_policy: dict[str, Any],
        stream_budget: dict[str, Any],
        output_dimensions: tuple[int, int] | None,
        effective_preset: int,
        video_filter: str | None,
        encoder_parameters: list[str],
        toolchain_identity: dict[str, Any],
        quality_metric: str,
        quality_target: float,
        target_size_trace: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if output_dimensions is None:
        LOGGER.info("Content-intent compatibility unavailable: output dimensions are unresolved")
        return None
    if str(toolchain_identity.get("status") or "") != "available":
        LOGGER.info(
            "Content-intent compatibility unavailable: toolchain identity %s",
            str(toolchain_identity.get("reason") or "is unavailable"),
        )
        return None
    frame_rate = content_intent_frame_rate(sample_item)
    stream_plan_id = content_intent_stream_plan_id(stream_budget)
    if frame_rate is None:
        LOGGER.info("Content-intent compatibility unavailable: frame rate evidence is missing")
        return None
    if stream_plan_id is None:
        LOGGER.info("Content-intent compatibility unavailable: stream plan identity is missing")
        return None
    quality_floor = object_dict(object_dict(target_size_trace).get("quality_floor"))
    minimum_quality_score = float_value(quality_floor.get("minimum"))
    if minimum_quality_score <= 0:
        minimum_quality_score = float_value(
            video_policy.get(f"min_target_{str(quality_metric).casefold()}")
        ) or quality_target
    try:
        compatibility = build_content_intent_boundary_compatibility(
            encoder=str(toolchain_identity.get("encoder") or video_policy.get("encoder") or ""),
            encoder_version=str(toolchain_identity.get("encoder_version") or ""),
            encoder_runtime_version=str(toolchain_identity.get("encoder_runtime_version") or ""),
            encoder_runtime_signature_id=str(toolchain_identity.get("encoder_runtime_signature_id") or ""),
            quality_tool=str(toolchain_identity.get("quality_tool") or ""),
            quality_tool_version=str(toolchain_identity.get("quality_tool_version") or ""),
            metric_runtime_signature_id=str(toolchain_identity.get("metric_runtime_signature_id") or ""),
            preset=effective_preset,
            pixel_format=str(video_policy.get("pixel_format") or ""),
            encoder_parameters=encoder_parameters,
            output_width=output_dimensions[0],
            output_height=output_dimensions[1],
            frame_rate=frame_rate,
            cadence_transform=str(object_dict(sample_item.get("cadence_decision")).get("transform") or "none"),
            video_filter=video_filter,
            output_container=str(sample_item.get("output_container") or ""),
            stream_plan_id=stream_plan_id,
            measurement_basis="sample_projection",
            quality_metric=quality_metric,
            quality_target=quality_target,
            minimum_quality_score=minimum_quality_score,
        )
    except ValueError as exc:
        LOGGER.info("Content-intent compatibility unavailable: %s", exc)
        return None
    return compatibility.to_payload()


def _review_artifact_fingerprint(
        preview_clips: list[Any],
        source_clips: list[Any] | None = None,
        compare_clips: list[Any] | None = None,
) -> str | None:
    fingerprint = reviewed_artifact_fingerprint(
        [
            (
                role,
                Path(getattr(clip, "output_path", "")),
                float_value(getattr(clip, "timestamp_seconds", None)),
                float_value(getattr(clip, "duration_seconds", None)),
            )
            for role, clips in (
                ("preview", preview_clips),
                ("source", source_clips or []),
                ("compare", compare_clips or []),
            )
            for clip in clips
        ],
        schema_version=3 if compare_clips is not None else 2,
    )
    if fingerprint is None:
        LOGGER.info("Review artifact fingerprint unavailable: no preview clips were produced")
    return fingerprint


def _stable_quality_toolchain_identity(
        before: dict[str, Any],
        after: dict[str, Any],
) -> dict[str, Any]:
    if str(before.get("status") or "") != "available":
        return before
    if str(after.get("status") or "") != "available":
        return after
    stable_fields = (
        "schema_version",
        "encoder",
        "encoder_version",
        "encoder_runtime_version",
        "encoder_runtime_signature_id",
        "quality_tool",
        "quality_tool_version",
    )
    if any(before.get(field) != after.get(field) for field in stable_fields):
        return {
            "schema_version": 1,
            "status": "unavailable",
            "reason": "toolchain_changed_during_measurement",
        }
    return after


def _review_audio_plan(stream_budget: Any) -> dict[str, Any] | None:
    audio_streams = tuple(stream_budget.stream_plan.audio_streams)
    if not audio_streams:
        return None
    stream = audio_streams[0]
    if stream.action == "transcode" and stream.codec_argument != "libopus":
        return None
    if stream.action not in {"copy", "transcode"}:
        return None
    return {
        "source_stream_index": stream.source_index,
        "source_codec": stream.source_codec,
        "source_channels": stream.channels or 2,
        "production_action": stream.action,
        "production_codec": stream.output_codec or stream.source_codec,
        "production_bitrate_bps": stream.output_bitrate_bps,
        "production_bitrate_text": stream.output_bitrate_text,
    }


def _review_audio_payload(audio_plan: dict[str, Any] | None, role: str) -> dict[str, Any] | None:
    if audio_plan is None:
        return None
    return {
        **audio_plan,
        "trustworthy": True,
        "role": role,
        "review_codec": "aac",
        "review_channels": 2,
    }


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

    telemetry = _CalibrationTelemetry(
        config,
        job_id,
        {
            **job,
            "job_id": job_id,
            "prefix": prefix,
            "host": host_data,
            "policy": policy,
        },
        deps,
    )
    telemetry.start()

    calibration_dir: Path | None = None
    manifest_path: Path | None = None
    library_item_id: int | None = None
    staged_artifact_snapshot: dict[str, Any] | None = None
    terminal_status: str | None = None

    try:
        telemetry.update("preparing_host")
        ready_host = deps.ensure_sample_host_ready(config, host_data)
        host_data = {**host_data, **asdict(ready_host)}
        telemetry.update("preparing_source")
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
            telemetry.refresh_compatibility(
                {
                    **job,
                    "host": host_data,
                    "policy": policy,
                    "sample_item": job["sample_item"],
                }
            )
            deps.save_job_state(connection, config, prefix, job)
            current_library_item_id = int_value(sample_item.get("library_item_id"))
            library_item_id = current_library_item_id
            staged_artifact_snapshot = snapshot_staged_artifact(
                connection,
                current_library_item_id,
                deps.staged_artifact_columns,
            )
            connection.commit()

            calibration_run_id = uuid.uuid4().hex[:12]
            if deps.calibration_mode_for_action(action) == "full":
                telemetry.update("encoding_full_calibration")
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
                    progress_callback=telemetry.update,
                )

        telemetry.update("saving_results")
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
        terminal_status = "completed"
    except ProcessCancelledError:
        terminal_status = "stopped"
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
    except TargetSizeSearchError as exc:
        terminal_status = "failed"
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
                    "result": {
                        "target_size_status": exc.status,
                        "target_size_trace": exc.trace,
                    },
                },
            )
    except Exception as exc:
        terminal_status = "failed"
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
        telemetry.finish(terminal_status)
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
        source_path_override: Path | None = None,
        progress_callback: Any | None = None,
) -> tuple[dict[str, Any], Path | None]:
    _ = prefix
    quality_host = _quality_host_data(config, host_data)
    if source_path_override is None:
        controller_source_path = Path(sample_item["source_path"])
        quality_source_path = resolve_item_source_path(
            config,
            sample_item,
            host=quality_host,
            host_media_access_for_host=host_media_access_for_host,
        )
    else:
        controller_source_path = source_path_override
        quality_source_path = source_path_override
    quality_temp_dir = _quality_temp_dir_for_host(config, quality_host)
    video_policy = object_dict(policy.get("video"))
    stream_budget = deps.resolve_stream_budget_ledger(
        sample_item,
        default_video_policy=object_dict(config.raw.get("video")) or video_policy,
        output_container=_output_container(config, sample_item),
    )
    stream_budget.require_positive_target_video_budget()
    review_audio_plan = _review_audio_plan(stream_budget)
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
    if progress_callback is not None:
        progress_callback("inspecting_source")
    detected_crop = deps.detect_video_crop(
        quality_source_path,
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
    output_dimensions = planned_output_dimensions(
        video_policy,
        width=width,
        height=height,
        detected_crop=detected_crop,
    )
    encoder_parameters = deps.build_svt_params(video_policy)
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
    preferred_quality_metric = str(video_policy.get("quality_metric") or "auto").strip().casefold()
    selected_quality_metric = (
        preferred_quality_metric if preferred_quality_metric in {"vmaf", "xpsnr"} else "xpsnr"
    )
    default_quality_target = 95.0 if selected_quality_metric == "vmaf" else 41.0
    if deps.select_quality_metric is not None:
        selected_quality_metric, default_quality_target = deps.select_quality_metric(
            preferred_quality_metric
        )
    quality_target = float(
        video_policy.get(f"target_{selected_quality_metric}", default_quality_target)
    )
    toolchain_identity_before = (
        object_dict(
            deps.quality_toolchain_identity(
                quality_metric=selected_quality_metric,
                host=quality_host,
                process_controller=process_controller,
            )
        )
        if deps.quality_toolchain_identity is not None
        else {}
    )
    cold_start_prediction = unavailable_av1_cold_start_prediction("cold_start_planner_unavailable")
    if deps.plan_av1_cold_start is not None:
        preliminary_compatibility = _content_intent_compatibility_payload(
            sample_item=sample_item,
            video_policy=video_policy,
            stream_budget=stream_budget.to_payload(),
            output_dimensions=output_dimensions,
            effective_preset=preset,
            video_filter=video_filter,
            encoder_parameters=encoder_parameters,
            toolchain_identity=toolchain_identity_before,
            quality_metric=selected_quality_metric,
            quality_target=quality_target,
            target_size_trace=None,
        )
        if preliminary_compatibility is None:
            cold_start_prediction = unavailable_av1_cold_start_prediction(
                "cold_start_compatibility_unavailable"
            )
        else:
            try:
                cold_start_prediction = deps.plan_av1_cold_start(
                    config=config,
                    prefix=prefix,
                    sample_item=sample_item,
                    compatibility_payload=preliminary_compatibility,
                    configured_min_crf=int(video_policy["min_crf"]),
                    configured_max_crf=int(video_policy["max_crf"]),
                    as_of=deps.now_iso(),
                )
            except Exception as exc:
                LOGGER.warning("AV1 cold-start planning failed: %s", exc)
                cold_start_prediction = unavailable_av1_cold_start_prediction(
                    "cold_start_planner_error"
                )
    if progress_callback is not None:
        progress_callback("searching_target")
    quality_result = deps.search_quality_for_source(quality_source_path, video_policy, **quality_kwargs)
    if progress_callback is not None:
        progress_callback("measuring_quality")
    sample_result = deps.run_sample_encode(
        quality_source_path,
        source_codec=str(sample_item.get("video_codec") or ""),
        preferred_metric=str(video_policy.get("quality_metric", "auto")),
        crf=quality_result.crf,
        preset=preset,
        pixel_format=str(video_policy["pixel_format"]),
        sample_every=str(video_policy["sample_every"]),
        sample_duration=str(video_policy["sample_duration"]),
        svt_params=encoder_parameters,
        video_filter=video_filter,
        process_controller=process_controller,
        host=quality_host,
        quality_temp_dir=quality_temp_dir,
    )
    toolchain_identity_after = (
        object_dict(
            deps.quality_toolchain_identity(
                quality_metric=sample_result.metric,
                host=quality_host,
                process_controller=process_controller,
            )
        )
        if deps.quality_toolchain_identity is not None
        else {}
    )
    toolchain_identity = _stable_quality_toolchain_identity(
        toolchain_identity_before,
        toolchain_identity_after,
    )

    review_moments = []
    if progress_callback is not None:
        progress_callback("selecting_review_moments")
    if deps.recommend_review_moments is not None:
        review_moments = deps.recommend_review_moments(
            controller_source_path,
            float_value(sample_item.get("duration_seconds")),
            8.0,
            media_fingerprint=object_dict(sample_item.get("media_fingerprint")),
            media_fingerprint_decision=object_dict(sample_item.get("media_fingerprint_decision")),
            analyze_source=controller_source_path.exists(),
            process_controller=process_controller,
        )
    timestamps = [moment.timestamp_seconds for moment in review_moments]
    if not timestamps:
        if controller_source_path.exists():
            timestamps = deps.recommend_review_timestamps(
                controller_source_path,
                float_value(sample_item.get("duration_seconds")),
                8.0,
                process_controller=process_controller,
            )
        elif deps.default_review_timestamps is not None:
            timestamps = deps.default_review_timestamps(
                float_value(sample_item.get("duration_seconds")),
                8.0,
            )
        else:
            timestamps = [0.0]
        review_moments = []
    output_dir = config.paths.review_dir / calibration_run_id / "item-00"
    if progress_callback is not None:
        progress_callback("building_review", completed=0, total=3)
    preview_clips = deps.encode_preview_clips(
        source_path=quality_source_path,
        source_codec=str(sample_item.get("video_codec") or ""),
        output_dir=output_dir,
        timestamps=timestamps,
        duration_seconds=8.0,
        encoder=str(video_policy["encoder"]),
        pixel_format=str(video_policy["pixel_format"]),
        preset=preset,
        crf=quality_result.crf,
        svt_params=encoder_parameters,
        audio_plan=review_audio_plan,
        video_filter=video_filter,
        host=quality_host,
        process_controller=process_controller,
    )
    if progress_callback is not None:
        progress_callback("building_review", completed=1, total=3)
    source_clips = deps.render_source_review_clips(
        source_path=quality_source_path,
        source_codec=str(sample_item.get("video_codec") or ""),
        output_dir=output_dir,
        timestamps=timestamps,
        duration_seconds=8.0,
        audio_plan=review_audio_plan,
        host=quality_host,
        process_controller=process_controller,
    )
    if progress_callback is not None:
        progress_callback("building_review", completed=2, total=3)
    compare_clips = deps.generate_compare_clips_from_review_pairs(
        source_clips=source_clips,
        previews=preview_clips,
        output_dir=output_dir,
        process_controller=process_controller,
    )
    if deps.secure_review_artifacts is not None:
        review_artifact_fingerprint = deps.secure_review_artifacts(
            preview_clips,
            source_clips,
            compare_clips,
        )
    else:
        review_artifact_fingerprint = _review_artifact_fingerprint(
            preview_clips,
            source_clips,
            compare_clips,
        )
    review_artifact_size_bytes = sum(max(0, int_value(getattr(clip, "size_bytes", None))) for clip in preview_clips)
    if progress_callback is not None:
        progress_callback("building_review", completed=3, total=3)
    estimated_total_size_bytes = (
        sample_result.predicted_encode_size_bytes + stream_budget.non_video_bytes
        if stream_budget.non_video_bytes is not None
        else None
    )
    target_size_trace = object_dict(getattr(quality_result, "target_size_trace", None)) or None
    cold_start_payload = cold_start_prediction.to_payload()
    cold_start_payload["execution"] = _av1_cold_start_execution_evidence(target_size_trace)
    compatibility_payload = _content_intent_compatibility_payload(
        sample_item=sample_item,
        video_policy=video_policy,
        stream_budget=stream_budget.to_payload(),
        output_dimensions=output_dimensions,
        effective_preset=preset,
        video_filter=video_filter,
        encoder_parameters=encoder_parameters,
        toolchain_identity=toolchain_identity,
        quality_metric=sample_result.metric,
        quality_target=quality_result.target,
        target_size_trace=target_size_trace,
    )

    payload = {
        "mode": "sample",
        "host": host_data,
        "action": action,
        "notes": notes,
        "policy": policy,
        "policy_seed": seed_metadata,
        "sample_item": _stored_sample_item_payload(sample_item),
        "review_artifact_fingerprint": review_artifact_fingerprint,
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
            "review_artifact_size_bytes": review_artifact_size_bytes or None,
            "content_intent_compatibility": compatibility_payload,
            "av1_cold_start_prior": cold_start_payload,
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
            "media_fingerprint_evidence_id": object_dict(sample_item.get("media_fingerprint_decision")).get("evidence_id"),
        },
        "review_moments": [
            deps.review_moment_payload(moment)
            for moment in review_moments
            if deps.review_moment_payload is not None
        ],
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
                "audio": _review_audio_payload(review_audio_plan, "new"),
            }
            for clip in preview_clips
        ],
        "source_clips": [
            {
                "path": deps.review_url(config, clip.output_path),
                "timestamp_seconds": clip.timestamp_seconds,
                "duration_seconds": clip.duration_seconds,
                "size_bytes": clip.size_bytes,
                "audio": _review_audio_payload(review_audio_plan, "original"),
            }
            for clip in source_clips
        ],
    }
    return payload, None


def _av1_cold_start_execution_evidence(target_size_trace: Mapping[str, Any] | None) -> dict[str, Any]:
    trace = object_dict(target_size_trace)
    active_execution = object_dict(trace.get("warm_start"))
    if active_execution:
        return active_execution
    return {"mode": "passive", "attempted": False}


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
