#!/usr/bin/env python3

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.core.config import load_config, update_runtime_settings
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import (
    background_work_state,
    calibration_jobs,
    encode_jobs,
    encode_queue_state,
    evidence_queue_state,
    item_events,
    library_items,
    scan_runs,
    series_metadata,
    staged_artifacts,
)
from mediaforce.core.evidence import stable_policy_hash, stable_source_id
from mediaforce.library.evidence_queue import start_evidence_work
from mediaforce.library.evidence_state import rebuild_library_item_evidence_states
from mediaforce.library.planner import build_manifest_item
from mediaforce.tuning.size_goals import operator_intent_from_policy
from mediaforce.web.runtime.folder_tuning_advice import (
    calibration_draft_hash,
    calibration_policy_hash,
)
from mediaforce.web.runtime.catalog_signature import save_catalog_signature

FIXTURE_SCAN_ID = "web-smoke-fixtures"
LEGACY_FIXTURE_SCAN_IDS = ("fixture-scan",)
FOLDER_PREFIX = "tv/Example Show/Season 1"
SAMPLING_PREFIX = "tv/Sampling Show/Season 1"
RETRY_PREFIX = "tv/Retry Show/Season 1"
SHARED_TEST_PREFIX = "tv/Shared Test Show/Season 1"
SHARED_TEST_SERIES_PREFIX = "tv/Shared Test Show"
COMPLETED_PREFIX = "movies/Archive Ready"
BLOCKED_COMPLETED_PREFIX = "movies/Blocked Cleanup"
REVIEW_READY_PREFIX = "tv/Review Ready/Season 1"
ABSOLUTE_TARGET_PREFIX = "tv/Absolute Goal/Season 1"
APPROVED_PREFIX = "tv/Approved Show/Season 1"
MISSED_TARGET_PREFIX = "tv/Overshoot Show/Season 1"
UNDER_TARGET_PREFIX = "tv/Undershoot Show/Season 1"
INFEASIBLE_PREFIX = "tv/Infeasible Goal/Season 1"
BOUND_EXHAUSTED_PREFIX = "tv/Search Limit/Season 1"
QUALITY_CONFLICT_PREFIX = "tv/Quality Conflict/Season 1"
ENCODE_RUNNING_PREFIX = "tv/Encoding Show/Season 1"
ENCODE_RETRY_PREFIX = "tv/Failed Encode/Season 1"
VALIDATION_PREFIX = "tv/Validation Ready/Season 1"
PROMOTION_PREFIX = "tv/Promotion Ready/Season 1"
PARTIAL_PROMOTION_PREFIX = "tv/Partial Promotion/Season 1"
FINISHED_PREFIX = "tv/Finished Show/Season 1"
ENCODE_WAITING_PREFIX = "movies/Waiting Encode"
MOVIE_LOOSE_PREFIX = "movies/Loose Feature.mkv"
MOVIE_EDITIONS_PREFIX = "movies/Editions Showcase"
MOVIE_STALE_PLAN_PREFIX = "movies/Stale Sample Plan"
MOVIE_CONFLICT_PREFIX = "movies/Promotion Conflict"
MOVIE_TARGET_BLOCKED_PREFIX = "movies/Target Too Large"
MOVIE_REVIEW_READY_PREFIX = "movies/Review Ready"
MOVIE_VALIDATION_PREFIX = "movies/Validation Ready"
MOVIE_PROMOTION_LARGE_PREFIX = "movies/Replacement Ready Large"
MOVIE_PROMOTION_SMALL_PREFIX = "movies/Replacement Ready Small"
OTHER_FOLDER_PREFIX = "other/Field Notes"
OTHER_ROOT_FILE_PREFIX = "other/Loose Capture.mkv"
OTHER_BLOCKED_PREFIX = "other/Needs Probe"
OTHER_OVERSIZED_PREFIX = "other/Oversized Intake"
OTHER_SAMPLING_PREFIX = "other/Sampling Folder"
OTHER_REVIEW_READY_PREFIX = "other/Review Ready"
OTHER_ACTIVE_PREFIX = "other/Active Batch"
OTHER_VALIDATION_PREFIX = "other/Validation Ready"
OTHER_PROMOTION_PREFIX = "other/Promotion Ready"
CURRENT_PREVIOUS_PREFIX = "tv/Current Season/Season 1"
CURRENT_SEASON_PREFIX = "tv/Current Season/Season 2"
CURRENT_SERIES_PREFIX = "tv/Current Season"
PROTECTED_READY_PREFIX = "tv/Protected Ready/Season 2"
PROTECTED_READY_PREVIOUS_PREFIX = "tv/Protected Ready/Season 1"
PROTECTED_READY_SERIES_PREFIX = "tv/Protected Ready"
FIXTURE_PREFIXES = (
    FOLDER_PREFIX,
    SAMPLING_PREFIX,
    RETRY_PREFIX,
    SHARED_TEST_PREFIX,
    COMPLETED_PREFIX,
    BLOCKED_COMPLETED_PREFIX,
    REVIEW_READY_PREFIX,
    ABSOLUTE_TARGET_PREFIX,
    APPROVED_PREFIX,
    MISSED_TARGET_PREFIX,
    UNDER_TARGET_PREFIX,
    INFEASIBLE_PREFIX,
    BOUND_EXHAUSTED_PREFIX,
    QUALITY_CONFLICT_PREFIX,
    ENCODE_RUNNING_PREFIX,
    ENCODE_RETRY_PREFIX,
    VALIDATION_PREFIX,
    PROMOTION_PREFIX,
    PARTIAL_PROMOTION_PREFIX,
    FINISHED_PREFIX,
    ENCODE_WAITING_PREFIX,
    MOVIE_LOOSE_PREFIX,
    MOVIE_EDITIONS_PREFIX,
    MOVIE_STALE_PLAN_PREFIX,
    MOVIE_CONFLICT_PREFIX,
    MOVIE_TARGET_BLOCKED_PREFIX,
    MOVIE_REVIEW_READY_PREFIX,
    MOVIE_VALIDATION_PREFIX,
    MOVIE_PROMOTION_LARGE_PREFIX,
    MOVIE_PROMOTION_SMALL_PREFIX,
    OTHER_FOLDER_PREFIX,
    OTHER_ROOT_FILE_PREFIX,
    OTHER_BLOCKED_PREFIX,
    OTHER_OVERSIZED_PREFIX,
    OTHER_SAMPLING_PREFIX,
    OTHER_REVIEW_READY_PREFIX,
    OTHER_ACTIVE_PREFIX,
    OTHER_VALIDATION_PREFIX,
    OTHER_PROMOTION_PREFIX,
    CURRENT_PREVIOUS_PREFIX,
    CURRENT_SEASON_PREFIX,
    PROTECTED_READY_PREVIOUS_PREFIX,
    PROTECTED_READY_PREFIX,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _resolve_under_project(project_root: Path, value: Path) -> Path:
    return value if value.is_absolute() else (project_root / value).resolve()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _write_checked_movie_preview(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(ffmpeg_binary()),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=24:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libsvtav1",
            "-preset",
            "11",
            "-crf",
            "45",
            "-c:a",
            "libopus",
            "-shortest",
            str(path),
        ],
        capture_output=True,
        check=True,
    )


def _library_item(
    *,
    project_root: Path,
    media_root: str,
    rel_path: str,
    size_bytes: int,
    status: str,
    video_codec: str,
    priority_score: int,
    recommendation: str,
    recommendation_reason: str,
    duration_seconds: float = 3_600.0,
    age_days: int = 730,
    width: int | None = 1920,
    height: int | None = 1080,
) -> dict[str, Any]:
    timestamp = (datetime.now(UTC) - timedelta(days=age_days)).isoformat(timespec="seconds")
    source_path = _resolve_under_project(
        project_root, Path("scratch/web-smoke/source") / rel_path
    )
    return {
        "source_path": str(source_path),
        "rel_path": rel_path,
        "media_root": media_root,
        "parent_dir": str(Path(rel_path).parent),
        "file_name": Path(rel_path).name,
        "container": Path(rel_path).suffix.lstrip(".") or "mkv",
        "size_bytes": size_bytes,
        "mtime_ns": 1_700_000_000_000_000_000,
        "fingerprint": f"fixture:{rel_path}",
        "cadence_summary_json": json.dumps(
            {
                "schema_version": 1,
                "probe": {
                    "field_order": "progressive",
                    "average_frame_rate": "24000/1001",
                },
                "analysis": {
                    "tff_frames": 0,
                    "bff_frames": 0,
                    "progressive_frames": 200,
                    "undetermined_frames": 0,
                    "repeated_neither": 200,
                    "repeated_top": 0,
                    "repeated_bottom": 0,
                    "sampled_frames": 200,
                    "coverage": 1.0,
                    "ranges": [
                        {
                            "start_seconds": 0.0,
                            "frame_limit": 200,
                            "sampled_frames": 200,
                            "status": "measured",
                        }
                    ],
                    "tool": {
                        "name": "mediaforce.ffmpeg_idet",
                        "version": "1",
                        "ffmpeg_version": "web-smoke",
                    },
                },
                "decision": {
                    "status": "resolved",
                    "classification": "progressive",
                },
            },
            sort_keys=True,
        ),
        "media_fingerprint_json": json.dumps(
            {
                "schema_version": 1,
                "analysis": {
                    "sampled_frames": 120,
                    "coverage": 1.0,
                    "aggregate": {
                        "dark_frame_fraction": 0.05,
                        "gradient_frame_fraction": 0.02,
                        "banding_risk_score": 0.02,
                        "high_motion_frame_fraction": 0.04,
                        "ydif_p90": 3.0,
                        "high_texture_frame_fraction": 0.04,
                        "edge_density_p90": 0.035,
                        "duplicate_like_frame_fraction": 0.05,
                        "temporal_noise_proxy": 0.5,
                        "chroma_instability": 0.2,
                        "smooth_temporal_noise_fraction": 0.02,
                        "audio_loudness_range_lu_max": 0.0,
                    },
                    "audio_probe": {
                        "track_count": 1,
                        "max_channels": 6,
                        "max_bitrate_bps": 768_000,
                    },
                    "ranges": [
                        {
                            "start_seconds": 0.0,
                            "frame_limit": 120,
                            "sampled_frames": 120,
                            "status": "measured",
                            "measurements": {
                                "dark_frame_fraction": 0.05,
                                "gradient_frame_fraction": 0.02,
                                "high_motion_frame_fraction": 0.04,
                                "ydif_p90": 3.0,
                                "high_texture_frame_fraction": 0.04,
                                "edge_density_p90": 0.035,
                                "duplicate_like_frame_fraction": 0.05,
                                "temporal_noise_proxy": 0.5,
                            },
                        }
                    ],
                    "tool": {
                        "name": "mediaforce.media_fingerprint",
                        "version": "1",
                        "ffmpeg_version": "web-smoke",
                    },
                },
                "decision": {"status": "measured"},
            },
            sort_keys=True,
        ),
        "duration_seconds": duration_seconds,
        "video_codec": video_codec,
        "video_bitrate": 8_000_000 if video_codec == "h264" else 4_500_000,
        "width": width,
        "height": height,
        "pix_fmt": "yuv420p",
        "audio_track_count": 1,
        "subtitle_track_count": 1,
        "english_audio_count": 1,
        "english_subtitle_count": 1,
        "default_audio_language": "eng",
        "default_subtitle_language": "eng",
        "audio_summary_json": json.dumps(
            [{"index": 1, "codec_name": "dts", "channels": 6, "language": "eng"}]
        ),
        "subtitle_summary_json": json.dumps(
            [
                {
                    "index": 2,
                    "codec_name": "subrip",
                    "language": "eng",
                    "forced": False,
                    "default": False,
                }
            ]
        ),
        "content_version_changed_at": timestamp,
        "status": status,
        "priority_score": priority_score,
        "recommendation": recommendation,
        "recommendation_reason": recommendation_reason,
        "last_scan_id": FIXTURE_SCAN_ID,
        "discovered_at": timestamp,
        "last_seen_at": timestamp,
        "updated_at": timestamp,
    }


def _job(
    *,
    job_id: str,
    prefix: str,
    status: str,
    sample_item: dict[str, Any],
    error: str | None = None,
    result: dict[str, Any] | None = None,
    created_at: str | None = None,
    started_at: str | None = None,
    heartbeat_at: str | None = None,
    finished_at: str | None = None,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timestamp = _now()
    return {
        "job_id": job_id,
        "prefix": prefix,
        "status": status,
        "lane": "sample",
        "action": "ai_tune",
        "host_json": json.dumps({"key": "web-smoke", "label": "Smoke fixture"}),
        "notes": "Seeded browser QA fixture.",
        "policy_json": json.dumps(sample_item["resolved_policy"], sort_keys=True),
        "sample_item_json": json.dumps(sample_item, sort_keys=True),
        "result_json": json.dumps(result, sort_keys=True) if result is not None else None,
        "error": error,
        "created_at": created_at or timestamp,
        "started_at": started_at
        if started_at is not None
        else timestamp if status in {"running", "failed", "stopped"} else None,
        "heartbeat_at": heartbeat_at,
        "progress_json": json.dumps(progress, sort_keys=True) if progress is not None else None,
        "finished_at": finished_at
        if finished_at is not None
        else timestamp if status in {"failed", "stopped", "completed"} else None,
        "updated_at": timestamp,
    }


def _policy_with_target(policy: dict[str, Any], target_size_mb: float) -> dict[str, Any]:
    resolved = json.loads(json.dumps(policy))
    video = resolved.setdefault("video", {})
    video.update(
        {
            "target_size_mb": target_size_mb,
            "target_size_bytes": round(target_size_mb * 1_000_000),
            "size_goal_mode": "absolute",
            "size_goal_source": "operator",
            "resolution_intent_mode": "source",
            "target_runtime_minutes": 45,
        }
    )
    return resolved


def _target_search_failure(status: str, selection_reason: str) -> dict[str, Any]:
    return {
        "target_size_status": status,
        "target_size_trace": {
            "schema_version": 1,
            "status": status,
            "selection_reason": selection_reason,
            "target": {
                "total_target_bytes": 300_000_000,
                "sample_lower_bound_bytes": 270_000_000,
                "sample_upper_bound_bytes": 330_000_000,
            },
            "quality_floor": {"metric": "vmaf", "minimum": 93.0},
            "curve": {"shape": "monotonic", "candidate_count": 3, "max_candidates": 6},
        },
    }


def _encode_host() -> dict[str, Any]:
    return {
        "key": "web-smoke-worker",
        "label": "Smoke Worker",
        "host": "web-smoke-worker.invalid",
        "mode": "ssh",
        "capabilities": ["encode_queue", "sample_calibration"],
        "priority": 10,
        "platform": "linux",
    }


def _seed_schedule_settings(config: Any, now: datetime) -> datetime:
    start_hour = (now.hour - 1) % 24
    end_hour = (now.hour + 3) % 24
    next_start_hour = (now.hour + 2) % 24
    next_end_hour = (now.hour + 5) % 24
    days_of_week = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    scheduler = {
        "mode": "night",
        "timezone": "utc",
        "start_hour": start_hour,
        "end_hour": end_hour,
        "days_of_week": days_of_week,
    }
    open_schedule_profile = {
        **scheduler,
        "key": "web_smoke_window",
        "label": "Smoke work window",
    }
    next_schedule_profile = {
        "mode": "night",
        "timezone": "utc",
        "start_hour": next_start_hour,
        "end_hour": next_end_hour,
        "days_of_week": days_of_week,
        "key": "web_smoke_next_window",
        "label": "Smoke next window",
    }
    configured_hosts = [dict(host) for host in config.remote_hosts]
    primary_host = next(
        (host for host in configured_hosts if str(host.get("key") or "") == "web-smoke-worker"),
        _encode_host(),
    )
    primary_host["schedule_profile"] = "web_smoke_window"
    drain_host = {
        **primary_host,
        "key": "web-smoke-drain-worker",
        "label": "Smoke Drain Worker",
        "host": "web-smoke-drain-worker.invalid",
    }
    standby_host = {
        **primary_host,
        "key": "web-smoke-standby-worker",
        "label": "Smoke Standby Worker",
        "host": "web-smoke-standby-worker.invalid",
        "schedule_profile": "web_smoke_next_window",
    }
    remote_hosts = [
        host
        for host in configured_hosts
        if not str(host.get("key") or "").startswith("web-smoke-")
    ]
    remote_hosts.extend((primary_host, drain_host, standby_host))

    def _apply(runtime_settings: dict[str, Any]) -> dict[str, Any]:
        encode_queue = dict(runtime_settings.get("encode_queue") or {})
        encode_queue["scheduler"] = scheduler
        encode_queue["schedule_profiles"] = [open_schedule_profile, next_schedule_profile]
        runtime_settings["encode_queue"] = encode_queue
        runtime_settings["remote_hosts"] = remote_hosts
        return runtime_settings

    update_runtime_settings(config.paths.runtime_settings_path, _apply)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=3)


def _manifest(project_root: Path, rel_path: str, job_id: str) -> Path:
    manifest_path = project_root / "state/web-smoke/runs" / f"{job_id}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": job_id,
                "items": [
                    {
                        "rel_path": rel_path,
                        "source_size_bytes": 7 * 1024**3,
                        "duration_seconds": 3_600.0,
                    }
                ],
            },
            indent=2,
        )
        + "\n"
    )
    return manifest_path


def _encode_job(
    *,
    project_root: Path,
    job_id: str,
    prefix: str,
    rel_path: str,
    status: str,
    progress: dict[str, Any] | None = None,
    error: str | None = None,
    waiting_reason: str | None = None,
    bypass_schedule: bool = False,
    schedule_close_deadline_at: str | None = None,
) -> dict[str, Any]:
    timestamp = _now()
    started_at = timestamp if status == "running" else None
    finished_at = (
        timestamp if status in {"failed", "stopped", "needs_attention"} else None
    )
    host = _encode_host()
    return {
        "job_id": job_id,
        "prefix": prefix,
        "job_kind": "folder",
        "parent_job_id": None,
        "status": status,
        "manifest_path": str(_manifest(project_root, rel_path, job_id)),
        "manifest_indexes_json": None,
        "item_count": 1,
        "saved_profile_path": None,
        "host_json": json.dumps(host, sort_keys=True),
        "last_host_json": json.dumps(host, sort_keys=True),
        "notes": "Seeded browser QA encode fixture.",
        "process_pid": None,
        "error": error,
        "bypass_schedule": int(bypass_schedule),
        "attempt_count": 1 if started_at else 0,
        "leased_at": started_at,
        "lease_expires_at": None,
        "heartbeat_at": started_at,
        "worker_id": "web-smoke-worker" if status == "running" else None,
        "schedule_close_deadline_at": schedule_close_deadline_at,
        "retry_not_before": None,
        "waiting_reason": waiting_reason,
        "terminal_reason": "quality_threshold" if status == "needs_attention" else None,
        "last_failure_kind": "quality_threshold"
        if status == "needs_attention"
        else None,
        "last_failure_at": finished_at,
        "host_cooldown_until": None,
        "progress_json": json.dumps(progress or {}, sort_keys=True),
        "created_at": timestamp,
        "started_at": started_at,
        "finished_at": finished_at,
        "updated_at": timestamp,
    }


def _write_review_sample_state(
    config: Any,
    rows_by_prefix: dict[str, dict[str, Any]],
    *,
    prefix: str,
    job_id: str,
    review_slug: str,
    predicted_total_size_bytes: int,
    quality_score: float,
    outcome: str = "good_fit",
    accepted: bool = False,
    target_mode: str | None = None,
    target_megabytes: float | None = None,
) -> None:
    row = rows_by_prefix[prefix]
    policy = json.loads(json.dumps(config.resolve_policy(row["rel_path"])))
    if target_megabytes is not None:
        mode = target_mode or "absolute"
        target_bytes = int(round(target_megabytes * 1_000_000))
        policy.setdefault("video", {}).update(
            {
                "size_goal_schema_version": 1,
                "size_goal_mode": mode,
                "size_goal_source": "web_smoke_fixture",
                "target_size_mb": target_megabytes,
                "target_size_bytes": target_bytes,
                "target_runtime_minutes": (
                    45 if mode == "normalized" else round(float(row["duration_seconds"]) / 60.0, 3)
                ),
                "sample_projection_tolerance_percent": 10,
                "final_output_tolerance_percent": 5,
                "resolution_intent_mode": "source",
                "resolution_intent_source": "web_smoke_fixture",
                "max_height": 0,
            }
        )
    operator_intent = operator_intent_from_policy(
        policy.get("video", {}),
        default_video_policy=config.video,
        audio_policy=policy.get("audio", {}),
        subtitle_policy=policy.get("subtitle", {}),
    )
    resolved_goal = operator_intent.size_goal.resolve(float(row["duration_seconds"]))
    target_bytes = int(resolved_goal.target_size_bytes or 0)
    review_dir = config.paths.review_dir / review_slug
    review_dir.mkdir(parents=True, exist_ok=True)
    source_clip = review_dir / "source.mov"
    preview_clip = review_dir / "preview.mov"
    artifact_image = review_dir / "contact-sheet.png"
    source_clip.write_bytes(b"mediaforce smoke source clip\n")
    preview_clip.write_bytes(b"mediaforce smoke preview clip\n")
    artifact_image.write_bytes(b"mediaforce smoke contact sheet\n")

    sample_item = build_manifest_item(row, config)
    sample_item["resolved_policy"] = policy
    source_id = stable_source_id(sample_item)
    sample_item["representative_source_id"] = source_id
    evidence_ids = [
        str(sample_item["cadence_decision"]["evidence_id"]),
        str(sample_item["media_fingerprint_decision"]["evidence_id"]),
    ]
    policy_hash = stable_policy_hash(policy)
    operator_request = {
        "source": "guided_workflow",
        "request_type": "combined_experiment",
        "operator_confirmed": True,
        "evidence_authority": "none",
        "budget_bytes": target_bytes,
        "budget_label": f"{target_bytes / 1_000_000:.0f} MB per episode",
        "sample_projection_tolerance_percent": 10,
        "final_output_tolerance_percent": 5,
        "size_goal": resolved_goal.to_payload(),
    }
    run_verdict = {"outcome": outcome}
    calibration_payload = {
        "job_id": job_id,
        "mode": "sample",
        "action": "ai_tune",
        "sample_item": sample_item,
        "policy": policy,
        "sample_result": {
            "predicted_total_size_bytes": predicted_total_size_bytes,
            "sampled_clip_bytes": 12_000_000,
            "quality_metric": "vmaf",
            "quality_score": quality_score,
            "cadence_evidence_id": evidence_ids[0],
            "media_fingerprint_evidence_id": evidence_ids[1],
        },
        "advice": {"run_verdict": run_verdict},
        "review_moments": [
            {
                "moment": 1,
                "timestamp_seconds": 60,
                "role": "representative",
                "risk_tags": ["softness_detail_loss", "audio_quality_layout"],
                "evidence_id": evidence_ids[1],
                "rationale": "Fixture moment for picture and sound review.",
            }
        ],
        "review_pairs": [
            {
                "timestamp_seconds": 60,
                "duration_seconds": 12,
                "source_clip": {
                    "path": f"/review-media/{review_slug}/source.mov",
                    "timestamp_seconds": 60,
                    "duration_seconds": 12,
                    "size_bytes": 96_000_000,
                    "audio": {
                        "trustworthy": True,
                        "role": "original",
                        "review_codec": "aac",
                        "review_channels": 2,
                    },
                },
                "preview_clip": {
                    "path": f"/review-media/{review_slug}/preview.mov",
                    "timestamp_seconds": 60,
                    "duration_seconds": 12,
                    "size_bytes": 12_000_000,
                    "audio": {
                        "trustworthy": True,
                        "role": "new",
                        "review_codec": "aac",
                        "review_channels": 2,
                    },
                },
            }
        ],
        "review_media_ready": True,
        "browser_review_ready": True,
    }
    advice_payload = {
        "summary": "Fixture review pack is ready for operator inspection.",
        "confidence": "high",
        "run_verdict": run_verdict,
        "operator_request": operator_request,
        "quality_risk_interpretation": {
            "summary": "Picture detail and surround layout need an operator comparison.",
            "verdict": "request_comparison",
            "confidence": "medium",
            "risks": [
                {
                    "tag": "softness_detail_loss",
                    "label": "Picture detail",
                    "level": "medium",
                    "rationale": "Inspect faces and moving texture in the representative moment.",
                },
                {
                    "tag": "audio_quality_layout",
                    "label": "Surround layout",
                    "level": "medium",
                    "rationale": "Confirm that dialogue and channel layout remain natural.",
                },
            ],
            "suggested_actions": ["Compare the current picture and sound evidence."],
        },
        "multimodal_review_pack": {
            "artifacts": [
                {
                    "kind": "video_contact_sheet",
                    "label": "Fixture contact sheet",
                    "detail": "Representative before/after review stills.",
                    "image_url": f"/review-media/{review_slug}/contact-sheet.png",
                },
                {
                    "kind": "audio_spectrogram_compare",
                    "label": "Fixture audio compare",
                    "detail": "Primary track is ready for review.",
                },
            ]
        },
    }
    calibration_payload["advice"]["operator_request"] = operator_request
    if accepted:
        calibration_payload["accepted_at"] = _now()
        calibration_payload["accepted_sample_job_id"] = job_id
        calibration_payload["accepted_policy_hash"] = calibration_policy_hash(
            calibration_payload
        )
        calibration_payload["accepted_draft_hash"] = calibration_draft_hash(
            calibration_payload
        )
        advice_payload["quality_risk_records"] = [
            {
                "kind": "post_test",
                "created_at": calibration_payload["accepted_at"],
                "verdict": "approved",
                "tags": ["softness_detail_loss", "audio_quality_layout"],
                "details": "Fixture operator approved the current picture and sound evidence.",
                "evidence_ids": evidence_ids,
                "moment_indexes": [1],
                "sample_job_id": job_id,
                "policy_hash": policy_hash,
                "source_id": source_id,
                "prefix": prefix,
            }
        ]
    web_dir = config.paths.web_state_dir
    web_dir.mkdir(parents=True, exist_ok=True)
    slug = _slug(prefix)
    (web_dir / f"{slug}.json").write_text(
        json.dumps(calibration_payload, indent=2, sort_keys=True) + "\n"
    )
    (web_dir / f"{slug}.advice.json").write_text(
        json.dumps(advice_payload, indent=2, sort_keys=True) + "\n"
    )


def _write_review_states(config: Any, rows_by_prefix: dict[str, dict[str, Any]]) -> None:
    _write_review_sample_state(
        config,
        rows_by_prefix,
        prefix=MOVIE_REVIEW_READY_PREFIX,
        job_id="web-smoke-movie-review-ready",
        review_slug="web-smoke-movie-review-ready",
        predicted_total_size_bytes=3_900_000_000,
        quality_score=95.4,
    )
    _write_review_sample_state(
        config,
        rows_by_prefix,
        prefix=OTHER_REVIEW_READY_PREFIX,
        job_id="web-smoke-other-review-ready",
        review_slug="web-smoke-other-review-ready",
        predicted_total_size_bytes=2_100_000_000,
        quality_score=94.9,
    )
    _write_review_sample_state(
        config,
        rows_by_prefix,
        prefix=REVIEW_READY_PREFIX,
        job_id="web-smoke-review-ready",
        review_slug="web-smoke-review-ready",
        predicted_total_size_bytes=398_000_000,
        quality_score=96.2,
    )
    _write_review_sample_state(
        config,
        rows_by_prefix,
        prefix=ABSOLUTE_TARGET_PREFIX,
        job_id="web-smoke-absolute-target",
        review_slug="web-smoke-absolute-target",
        predicted_total_size_bytes=224_000_000,
        quality_score=95.1,
        target_mode="absolute",
        target_megabytes=225,
    )
    _write_review_sample_state(
        config,
        rows_by_prefix,
        prefix=APPROVED_PREFIX,
        job_id="web-smoke-approved",
        review_slug="web-smoke-approved",
        predicted_total_size_bytes=396_000_000,
        quality_score=94.8,
        accepted=True,
    )
    _write_review_sample_state(
        config,
        rows_by_prefix,
        prefix=PROMOTION_PREFIX,
        job_id="web-smoke-promotion-ready",
        review_slug="web-smoke-promotion-ready",
        predicted_total_size_bytes=396_000_000,
        quality_score=94.8,
        accepted=True,
    )
    _write_review_sample_state(
        config,
        rows_by_prefix,
        prefix=PARTIAL_PROMOTION_PREFIX,
        job_id="web-smoke-partial-promotion",
        review_slug="web-smoke-partial-promotion",
        predicted_total_size_bytes=396_000_000,
        quality_score=94.8,
        accepted=True,
    )
    _write_review_sample_state(
        config,
        rows_by_prefix,
        prefix=PROTECTED_READY_PREFIX,
        job_id="web-smoke-protected-ready",
        review_slug="web-smoke-protected-ready",
        predicted_total_size_bytes=396_000_000,
        quality_score=94.8,
        accepted=True,
    )
    _write_review_sample_state(
        config,
        rows_by_prefix,
        prefix=PROTECTED_READY_SERIES_PREFIX,
        job_id="web-smoke-protected-ready-series",
        review_slug="web-smoke-protected-ready-series",
        predicted_total_size_bytes=396_000_000,
        quality_score=94.8,
        accepted=True,
    )
    _write_review_sample_state(
        config,
        rows_by_prefix,
        prefix=MISSED_TARGET_PREFIX,
        job_id="web-smoke-overshoot",
        review_slug="web-smoke-overshoot",
        predicted_total_size_bytes=560_000_000,
        quality_score=92.0,
        outcome="poor_fit",
    )
    _write_review_sample_state(
        config,
        rows_by_prefix,
        prefix=UNDER_TARGET_PREFIX,
        job_id="web-smoke-undershoot",
        review_slug="web-smoke-undershoot",
        predicted_total_size_bytes=300_000_000,
        quality_score=90.5,
        outcome="needs_revision",
    )


def _write_stale_proposal_state(config: Any) -> None:
    stale_video_policy = {
        "compression_intent_schema_version": 1,
        "compression_intent": "reference",
        "compression_intent_source": "browser_fixture",
        "compression_intent_confirmed": True,
    }
    stale_intent = operator_intent_from_policy(
        stale_video_policy,
        default_video_policy=config.video,
    ).compression_intent.to_payload()
    payload = {
        "proposal_id": "web-smoke-stale-sample-plan",
        "status": "preview",
        "kind": "sample_plan",
        "action": "baseline",
        "created_at": _now(),
        "can_queue": True,
        "message": "Sample plan ready. Nothing is queued until you confirm it.",
        "operator_note": "Balance size and detail.",
        "request_disposition": "honored",
        "applied_policy": {"video": stale_video_policy},
        "host": {"key": "studio-mini", "label": "M4 Studio"},
        "base_compression_intent": stale_intent,
        "compression_intent": stale_intent,
    }
    config.paths.web_state_dir.mkdir(parents=True, exist_ok=True)
    proposal_file = config.paths.web_state_dir / f"{_slug(MOVIE_STALE_PLAN_PREFIX)}.proposal.json"
    proposal_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _clear_fixture_files(config: Any) -> None:
    for path in config.paths.web_state_dir.glob("scan-*.job.json"):
        path.unlink(missing_ok=True)
    for prefix in FIXTURE_PREFIXES:
        slug = _slug(prefix)
        for suffix in (".json", ".advice.json", ".proposal.json", ".job.json"):
            try:
                (config.paths.web_state_dir / f"{slug}{suffix}").unlink()
            except FileNotFoundError:
                pass


def seed(config_path: Path, *, profile: str = "default") -> dict[str, Any]:
    config = load_config(config_path)
    fixture_now = datetime.now(UTC)
    schedule_closes_at = _seed_schedule_settings(config, fixture_now)
    project_root = config.paths.project_root
    archive_root = _resolve_under_project(project_root, config.archive_root)
    staging_root = _resolve_under_project(project_root, config.staging_root)
    archive_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(staging_root / PARTIAL_PROMOTION_PREFIX, ignore_errors=True)
    config.paths.web_state_dir.mkdir(parents=True, exist_ok=True)
    _clear_fixture_files(config)
    archived_source = archive_root / "movies" / "Archive Ready" / "Feature.mkv"
    archived_source.parent.mkdir(parents=True, exist_ok=True)
    archived_source.write_bytes(b"mediaforce smoke archived original\n")

    with open_db(config.paths.db_path) as connection:
        connection.execute(
            encode_jobs.delete().where(encode_jobs.c.job_id.like("web-smoke-%"))
        )
        connection.execute(
            encode_queue_state.delete().where(
                encode_queue_state.c.queue_name == "heavy"
            )
        )
        connection.execute(evidence_queue_state.delete())
        connection.execute(background_work_state.delete())
        connection.execute(
            calibration_jobs.delete().where(
                calibration_jobs.c.job_id.like("web-smoke-%")
            )
        )
        connection.execute(
            scan_runs.delete().where(
                scan_runs.c.scan_id.in_((FIXTURE_SCAN_ID, *LEGACY_FIXTURE_SCAN_IDS))
            )
        )
        fixture_ids = [
            int(row["id"])
            for row in connection.execute(
                library_items.select()
                .with_only_columns(library_items.c.id)
                .where(
                    library_items.c.last_scan_id.in_(
                        (FIXTURE_SCAN_ID, *LEGACY_FIXTURE_SCAN_IDS)
                    )
                )
            )
            .mappings()
            .fetchall()
        ]
        if fixture_ids:
            connection.execute(
                item_events.delete().where(
                    item_events.c.library_item_id.in_(fixture_ids)
                )
            )
            connection.execute(
                staged_artifacts.delete().where(
                    staged_artifacts.c.library_item_id.in_(fixture_ids)
                )
            )
            connection.execute(
                library_items.delete().where(library_items.c.id.in_(fixture_ids))
            )
        connection.execute(
            series_metadata.delete().where(
                series_metadata.c.series_prefix.in_((CURRENT_SERIES_PREFIX, PROTECTED_READY_SERIES_PREFIX))
            )
        )

        if profile == "empty":
            save_catalog_signature(config)
            return {
                "profile": profile,
                "folderRoutes": [],
                "libraryItems": 0,
                "encodeJobs": 0,
            }

        rows = [
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Example Show/Season 1/Episode 01.mkv",
                size_bytes=8 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=92,
                recommendation="priority_encode",
                recommendation_reason="Fixture high-priority H.264 episode for browser QA.",
                duration_seconds=88 * 60,
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Example Show/Season 1/Episode 02.mkv",
                size_bytes=6 * 1024**3,
                status="planned",
                video_codec="h264",
                priority_score=76,
                recommendation="priority_encode",
                recommendation_reason="Fixture second episode keeps the queue non-empty and grouped.",
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Blocked Queue/Feature.mkv",
                size_bytes=5 * 1024**3,
                status="discovered",
                video_codec="hevc",
                priority_score=44,
                recommendation="review_encode",
                recommendation_reason="Fixture lower-priority movie for list density.",
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Archive Ready/Feature.mkv",
                size_bytes=7 * 1024**3,
                status="promoted",
                video_codec="h264",
                priority_score=10,
                recommendation="priority_encode",
                recommendation_reason="Fixture promoted item for completed cleanup state.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Sampling Show/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=88,
                recommendation="priority_encode",
                recommendation_reason="Fixture queued sample state for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Retry Show/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=86,
                recommendation="priority_encode",
                recommendation_reason="Fixture retryable sample state for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Blocked Cleanup/Feature.mkv",
                size_bytes=5 * 1024**3,
                status="promoted",
                video_codec="h264",
                priority_score=9,
                recommendation="priority_encode",
                recommendation_reason="Fixture blocked completed cleanup state.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Review Ready/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=84,
                recommendation="priority_encode",
                recommendation_reason="Fixture review-pack-ready state for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Absolute Goal/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=84,
                recommendation="priority_encode",
                recommendation_reason="Fixture absolute 225 MB goal state for browser QA.",
                duration_seconds=88 * 60,
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Approved Show/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=83,
                recommendation="priority_encode",
                recommendation_reason="Fixture approved sample state for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Overshoot Show/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=82,
                recommendation="priority_encode",
                recommendation_reason="Fixture target-missed review state for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Undershoot Show/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=81,
                recommendation="priority_encode",
                recommendation_reason="Fixture under-target review state for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Infeasible Goal/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=81,
                recommendation="priority_encode",
                recommendation_reason="Fixture arithmetic infeasibility state for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Quality Conflict/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=81,
                recommendation="priority_encode",
                recommendation_reason="Fixture quality-floor conflict state for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Search Limit/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=81,
                recommendation="priority_encode",
                recommendation_reason="Fixture target-search bound state for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Encoding Show/Season 1/Episode 01.mkv",
                size_bytes=9 * 1024**3,
                status="approved",
                video_codec="h264",
                priority_score=80,
                recommendation="priority_encode",
                recommendation_reason="Fixture active encode state for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Failed Encode/Season 1/Episode 01.mkv",
                size_bytes=9 * 1024**3,
                status="approved",
                video_codec="h264",
                priority_score=79,
                recommendation="priority_encode",
                recommendation_reason="Fixture retryable encode state for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Waiting Encode/Feature.mkv",
                size_bytes=6 * 1024**3,
                status="approved",
                video_codec="h264",
                priority_score=73,
                recommendation="priority_encode",
                recommendation_reason="Fixture queued encode with unavailable host state.",
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Review Ready/Feature.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=72,
                recommendation="review_encode",
                recommendation_reason="Fixture movie comparison clips ready for review.",
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Validation Ready/Feature.mkv",
                size_bytes=6 * 1024**3,
                status="encoded",
                video_codec="av1",
                priority_score=17,
                recommendation="already_optimized",
                recommendation_reason="Fixture movie output waiting for validation.",
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Replacement Ready Large/Feature.mkv",
                size_bytes=12 * 1024**3,
                status="validated",
                video_codec="av1",
                priority_score=16,
                recommendation="already_optimized",
                recommendation_reason="Fixture higher-savings movie waiting for replacement.",
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Replacement Ready Small/Feature.mkv",
                size_bytes=4 * 1024**3,
                status="validated",
                video_codec="av1",
                priority_score=15,
                recommendation="already_optimized",
                recommendation_reason="Fixture lower-savings movie waiting for replacement.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Validation Ready/Season 1/Episode 01.mkv",
                size_bytes=5 * 1024**3,
                status="encoded",
                video_codec="av1",
                priority_score=20,
                recommendation="already_optimized",
                recommendation_reason="Fixture output waiting for validation.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Promotion Ready/Season 1/Episode 01.mkv",
                size_bytes=5 * 1024**3,
                status="validated",
                video_codec="av1",
                priority_score=19,
                recommendation="already_optimized",
                recommendation_reason="Fixture validated output waiting for promotion.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Finished Show/Season 1/Episode 01.mkv",
                size_bytes=5 * 1024**3,
                status="promoted",
                video_codec="av1",
                priority_score=18,
                recommendation="already_optimized",
                recommendation_reason="Fixture completed season state for browser QA.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Current Season/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=82,
                recommendation="priority_encode",
                recommendation_reason="Fixture aged season remains eligible while the current season is protected.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Current Season/Season 2/Episode 01.mkv",
                size_bytes=8 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=84,
                recommendation="priority_encode",
                recommendation_reason="Fixture active current season is protected from automatic encoding.",
                age_days=5,
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Protected Ready/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=83,
                recommendation="priority_encode",
                recommendation_reason="Fixture older season requires an explicit lifecycle override.",
                age_days=5,
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Protected Ready/Season 2/Episode 01.mkv",
                size_bytes=8 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=85,
                recommendation="priority_encode",
                recommendation_reason="Fixture approved current season requires an explicit lifecycle override.",
                age_days=5,
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Loose Feature.mkv",
                size_bytes=11 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=72,
                recommendation="priority_encode",
                recommendation_reason="Fixture root-level exact movie file for browser QA.",
                age_days=1_200,
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Target Too Large/Feature.mkv",
                size_bytes=360_000_000,
                status="discovered",
                video_codec="hevc",
                priority_score=71,
                recommendation="review_encode",
                recommendation_reason="Fixture movie target exceeds the source-relative cap.",
                duration_seconds=5_520.0,
                age_days=1_100,
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Stale Sample Plan/Feature.mkv",
                size_bytes=8 * 1024**3,
                status="planned",
                video_codec="h264",
                priority_score=73,
                recommendation="priority_encode",
                recommendation_reason="Fixture outdated sample plan for browser QA.",
                age_days=1_050,
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Editions Showcase/Editions Showcase - Theatrical.mkv",
                size_bytes=12 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=74,
                recommendation="priority_encode",
                recommendation_reason="Fixture theatrical movie edition for browser QA.",
                age_days=980,
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Editions Showcase/Editions Showcase - Director's Cut.mkv",
                size_bytes=14 * 1024**3,
                status="planned",
                video_codec="h264",
                priority_score=73,
                recommendation="priority_encode",
                recommendation_reason="Fixture independently reachable movie edition for browser QA.",
                age_days=960,
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Editions Showcase/Featurettes/Making Of.mkv",
                size_bytes=650 * 1024**2,
                status="discovered",
                video_codec="h264",
                priority_score=20,
                recommendation="review_encode",
                recommendation_reason="Fixture excluded movie extra for browser QA.",
                age_days=950,
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Editions Showcase/Disc 2/Alternate.mkv",
                size_bytes=4 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=18,
                recommendation="review_encode",
                recommendation_reason="Fixture uncertain nested movie file for browser QA.",
                age_days=940,
            ),
            _library_item(
                project_root=project_root,
                media_root="movies",
                rel_path="movies/Promotion Conflict/Feature.mp4",
                size_bytes=9 * 1024**3,
                status="validated",
                video_codec="h264",
                priority_score=70,
                recommendation="priority_encode",
                recommendation_reason="Fixture movie promotion collision for browser QA.",
                age_days=900,
            ),
            _library_item(
                project_root=project_root,
                media_root="other",
                rel_path="other/Field Notes/Camera A.mkv",
                size_bytes=4 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=69,
                recommendation="review_encode",
                recommendation_reason="Fixture bounded Other folder member for browser QA.",
                age_days=820,
            ),
            _library_item(
                project_root=project_root,
                media_root="other",
                rel_path="other/Field Notes/Nested/Camera B.mkv",
                size_bytes=3 * 1024**3,
                status="planned",
                video_codec="hevc",
                priority_score=61,
                recommendation="review_encode",
                recommendation_reason="Fixture nested Other folder member for membership review.",
                age_days=810,
            ),
            _library_item(
                project_root=project_root,
                media_root="other",
                rel_path=OTHER_ROOT_FILE_PREFIX,
                size_bytes=5 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=67,
                recommendation="review_encode",
                recommendation_reason="Fixture root-level exact Other file for browser QA.",
                age_days=800,
            ),
            _library_item(
                project_root=project_root,
                media_root="other",
                rel_path="other/Needs Probe/Unknown Stream.mkv",
                size_bytes=2 * 1024**3,
                status="discovered",
                video_codec="",
                priority_score=30,
                recommendation="review_encode",
                recommendation_reason="Fixture unsupported Other profile state for browser QA.",
                age_days=790,
                width=None,
                height=None,
            ),
            _library_item(
                project_root=project_root,
                media_root="other",
                rel_path="other/Sampling Folder/Camera C.mkv",
                size_bytes=4 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=66,
                recommendation="review_encode",
                recommendation_reason="Fixture Other sample-waiting state for browser QA.",
                age_days=785,
            ),
            _library_item(
                project_root=project_root,
                media_root="other",
                rel_path="other/Review Ready/Camera D.mkv",
                size_bytes=4 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=65,
                recommendation="review_encode",
                recommendation_reason="Fixture Other comparison clips ready for review.",
                age_days=782,
            ),
            _library_item(
                project_root=project_root,
                media_root="other",
                rel_path="other/Active Batch/Camera C.mkv",
                size_bytes=6 * 1024**3,
                status="encoding",
                video_codec="h264",
                priority_score=65,
                recommendation="review_encode",
                recommendation_reason="Fixture active Other processing state for browser QA.",
                age_days=780,
            ),
            _library_item(
                project_root=project_root,
                media_root="other",
                rel_path="other/Validation Ready/Camera D.mkv",
                size_bytes=5 * 1024**3,
                status="encoded",
                video_codec="h264",
                priority_score=64,
                recommendation="review_encode",
                recommendation_reason="Fixture Other validation state for browser QA.",
                age_days=770,
            ),
            _library_item(
                project_root=project_root,
                media_root="other",
                rel_path="other/Promotion Ready/Camera E.mkv",
                size_bytes=5 * 1024**3,
                status="validated",
                video_codec="hevc",
                priority_score=63,
                recommendation="review_encode",
                recommendation_reason="Fixture Other promotion state for browser QA.",
                age_days=760,
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Shared Test Show/Season 1/Episode 01.mkv",
                size_bytes=7 * 1024**3,
                status="discovered",
                video_codec="h264",
                priority_score=84,
                recommendation="priority_encode",
                recommendation_reason="Fixture show-level sample shown from a season route.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Partial Promotion/Season 1/Episode 01 Ready.mkv",
                size_bytes=5 * 1024**3,
                status="validated",
                video_codec="h264",
                priority_score=17,
                recommendation="already_optimized",
                recommendation_reason="Fixture validated episode in an incomplete promotion scope.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Partial Promotion/Season 1/Episode 02 Not Started.mkv",
                size_bytes=5 * 1024**3,
                status="planned",
                video_codec="h264",
                priority_score=17,
                recommendation="priority_encode",
                recommendation_reason="Fixture unprocessed episode blocks whole-season promotion.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Partial Promotion/Season 1/Episode 03 Missing.mkv",
                size_bytes=5 * 1024**3,
                status="validated",
                video_codec="h264",
                priority_score=17,
                recommendation="already_optimized",
                recommendation_reason="Fixture missing staged output blocks whole-season promotion.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Partial Promotion/Season 1/Episode 04 Drifted.mkv",
                size_bytes=5 * 1024**3,
                status="validated",
                video_codec="h264",
                priority_score=17,
                recommendation="already_optimized",
                recommendation_reason="Fixture changed staged output blocks whole-season promotion.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Partial Promotion/Season 1/Episode 05 Failed Check.mkv",
                size_bytes=5 * 1024**3,
                status="validated",
                video_codec="h264",
                priority_score=17,
                recommendation="already_optimized",
                recommendation_reason="Fixture failed validation blocks whole-season promotion.",
            ),
            _library_item(
                project_root=project_root,
                media_root="tv",
                rel_path="tv/Partial Promotion/Season 1/Episode 06 Remote.mkv",
                size_bytes=5 * 1024**3,
                status="validated",
                video_codec="h264",
                priority_score=17,
                recommendation="already_optimized",
                recommendation_reason="Fixture unreachable worker output blocks whole-season promotion.",
            ),
        ]
        rows.extend(
            _library_item(
                project_root=project_root,
                media_root="other",
                rel_path=f"{OTHER_OVERSIZED_PREFIX}/Clip-{index:03d}.mkv",
                size_bytes=32 * 1024**2,
                status="discovered",
                video_codec="h264",
                priority_score=25,
                recommendation="review_encode",
                recommendation_reason="Fixture oversized Other scope for browser safety coverage.",
                age_days=700,
            )
            for index in range(251)
        )
        for row in rows:
            if row["rel_path"] == "tv/Example Show/Season 1/Episode 01.mkv":
                row["cadence_summary_json"] = None
            elif row["rel_path"] == "tv/Example Show/Season 1/Episode 02.mkv":
                row["media_fingerprint_json"] = None
            elif row["rel_path"] == "movies/Editions Showcase/Feature - Director Cut.mkv":
                row["media_fingerprint_json"] = None
        inserted_ids: list[int] = []
        for row in rows:
            result = connection.execute(library_items.insert().values(**row))
            inserted_ids.append(int(result.inserted_primary_key[0]))
        for row, item_id in zip(rows, inserted_ids, strict=True):
            row["id"] = item_id
        rebuild_library_item_evidence_states(connection, library_item_ids=inserted_ids)
        scan_timestamp = _now()
        connection.execute(
            scan_runs.insert().values(
                scan_id=FIXTURE_SCAN_ID,
                started_at=scan_timestamp,
                completed_at=scan_timestamp,
                owner_pid=None,
                last_progress_at=scan_timestamp,
                roots_json=json.dumps(
                    {key: str(path) for key, path in config.scan_source_root_map.items()},
                    sort_keys=True,
                ),
                scope="full",
                prefixes_json=None,
                file_count=len(rows),
                reprobed_count=len(rows),
                unchanged_count=0,
            )
        )
        rows_by_prefix = {str(row["parent_dir"]): row for row in rows}
        rows_by_prefix[PROTECTED_READY_SERIES_PREFIX] = rows_by_prefix[PROTECTED_READY_PREFIX]
        ids_by_rel_path = {
            str(row["rel_path"]): item_id
            for row, item_id in zip(rows, inserted_ids, strict=True)
        }
        timestamp = _now()
        for series_prefix, tmdb_series_id in (
            (CURRENT_SERIES_PREFIX, 4242),
            (PROTECTED_READY_SERIES_PREFIX, 4343),
        ):
            connection.execute(
                series_metadata.insert().values(
                    series_prefix=series_prefix,
                    plex_guids_json=json.dumps([f"tmdb://{tmdb_series_id}"]),
                    tmdb_series_id=tmdb_series_id,
                    tmdb_status="Returning Series",
                    tmdb_in_production=1,
                    tmdb_observed_at=timestamp,
                    updated_at=timestamp,
                )
            )

        completed_id = inserted_ids[3]
        blocked_completed_id = inserted_ids[6]
        for item_id, row, archived_path in (
            (completed_id, rows[3], str(archived_source)),
            (
                blocked_completed_id,
                rows[6],
                str(
                    Path(
                        "/tmp/mediaforce-web-smoke-outside/Blocked Cleanup/Feature.mkv"
                    )
                ),
            ),
        ):
            if item_id == completed_id:
                connection.execute(
                    item_events.insert().values(
                        library_item_id=item_id,
                        created_at=timestamp,
                        event_type="encoding_failed",
                        details_json=json.dumps(
                            {
                                "prefix": COMPLETED_PREFIX,
                                "error": "Fixture encode failed before a successful retry.",
                            }
                        ),
                    )
                )
            else:
                connection.execute(
                    item_events.insert().values(
                        library_item_id=item_id,
                        created_at=timestamp,
                        event_type="encoding_stopped",
                        details_json=json.dumps(
                            {
                                "prefix": BLOCKED_COMPLETED_PREFIX,
                                "error": "Fixture encode was stopped by operator.",
                            }
                        ),
                    )
                )
            connection.execute(
                staged_artifacts.insert().values(
                    library_item_id=item_id,
                    manifest_run_id="web-smoke-run",
                    manifest_path=str(
                        project_root / "state/web-smoke/runs/web-smoke-run.json"
                    ),
                    item_index=0,
                    encode_origin="web-smoke",
                    source_path=row["source_path"],
                    source_rel_path=row["rel_path"],
                    source_size_bytes=row["size_bytes"],
                    source_duration_seconds=row["duration_seconds"],
                    source_video_codec=row["video_codec"],
                    source_fingerprint=row["fingerprint"],
                    staging_path=str(staging_root / row["rel_path"]),
                    staging_size_bytes=3 * 1024**3,
                    bytes_saved=4 * 1024**3,
                    size_ratio=0.43,
                    chosen_crf=28,
                    quality_metric="vmaf",
                    quality_target=95.0,
                    quality_score=96.1,
                    audio_summary_json=row["audio_summary_json"],
                    subtitle_summary_json=row["subtitle_summary_json"],
                    validation_json=json.dumps({"ok": True, "source": "web-smoke"}),
                    staged_at=timestamp,
                    validated_at=timestamp,
                    promoted_at=timestamp,
                    promoted_path=str(
                        project_root
                        / "scratch/web-smoke/source"
                        / Path(row["rel_path"]).with_suffix(".av1.mkv")
                    ),
                    archived_source_path=archived_path,
                    updated_at=timestamp,
                )
            )
            connection.execute(
                item_events.insert().values(
                    library_item_id=item_id,
                    created_at=timestamp,
                    event_type="promotion_completed",
                    details_json=json.dumps(
                        {
                            "prefix": COMPLETED_PREFIX
                            if item_id == completed_id
                            else BLOCKED_COMPLETED_PREFIX,
                            "bytes_saved": 4 * 1024**3,
                            "note": "Fixture promoted item for browser QA.",
                        }
                    ),
                )
            )

        for prefix, validated_at in (
            (VALIDATION_PREFIX, None),
            (PROMOTION_PREFIX, timestamp),
            (MOVIE_VALIDATION_PREFIX, None),
            (MOVIE_PROMOTION_LARGE_PREFIX, timestamp),
            (MOVIE_PROMOTION_SMALL_PREFIX, timestamp),
            (OTHER_VALIDATION_PREFIX, None),
            (OTHER_PROMOTION_PREFIX, timestamp),
        ):
            row = rows_by_prefix[prefix]
            item_id = ids_by_rel_path[str(row["rel_path"])]
            staging_path = (
                staging_root / Path(str(row["rel_path"])).with_suffix(".av1.mkv")
            )
            staging_path.parent.mkdir(parents=True, exist_ok=True)
            if prefix in (MOVIE_PROMOTION_LARGE_PREFIX, MOVIE_PROMOTION_SMALL_PREFIX):
                _write_checked_movie_preview(staging_path)
            else:
                staging_path.write_bytes(b"mediaforce smoke staged output\n")
            manifest_path = None
            item_index = None
            validation_json = None
            staging_mtime_ns = None
            staging_size_bytes = max(1, int(row["size_bytes"]) // 2)
            if prefix in (
                PROMOTION_PREFIX,
                MOVIE_PROMOTION_LARGE_PREFIX,
                MOVIE_PROMOTION_SMALL_PREFIX,
            ):
                policy = config.resolve_policy(str(row["rel_path"]))
                manifest_run_id = f"web-smoke-{_slug(prefix)}"
                manifest_path = config.paths.run_manifest_dir / f"{manifest_run_id}.json"
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                manifest_path.write_text(json.dumps({
                    "items": [{"resolved_policy": policy}],
                }))
                item_index = 0
                validation_json = json.dumps({"passed": True, "source": "web-smoke"})
                staging_size_bytes = staging_path.stat().st_size
                staging_mtime_ns = staging_path.stat().st_mtime_ns
            connection.execute(
                staged_artifacts.insert().values(
                    library_item_id=item_id,
                    manifest_run_id=manifest_run_id if manifest_path else None,
                    manifest_path=str(manifest_path) if manifest_path else None,
                    item_index=item_index,
                    source_rel_path=row["rel_path"],
                    source_size_bytes=row["size_bytes"],
                    staging_path=str(staging_path),
                    staging_size_bytes=staging_size_bytes,
                    staging_mtime_ns=staging_mtime_ns,
                    bytes_saved=max(1, int(row["size_bytes"]) // 2),
                    size_ratio=0.5,
                    validation_json=validation_json,
                    staged_at=timestamp,
                    validated_at=validated_at,
                    updated_at=timestamp,
                )
            )

        partial_rows = [
            row for row in rows if str(row["parent_dir"]) == PARTIAL_PROMOTION_PREFIX
        ]
        partial_policy = config.resolve_policy(str(partial_rows[0]["rel_path"]))
        partial_manifest_path = config.paths.run_manifest_dir / "web-smoke-partial-promotion.json"
        partial_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        partial_manifest_path.write_text(json.dumps({
            "items": [{"resolved_policy": partial_policy} for _row in partial_rows],
        }))
        for item_index, row in enumerate(partial_rows):
            rel_path = str(row["rel_path"])
            item_id = ids_by_rel_path[rel_path]
            if "Not Started" in rel_path:
                continue
            if "Remote" in rel_path:
                staging_path = project_root / "scratch/web-smoke-remote" / Path(rel_path)
                encode_host_key = "web-smoke-remote"
            else:
                staging_path = staging_root / Path(rel_path)
                encode_host_key = None
            if "Missing" not in rel_path and "Remote" not in rel_path:
                staging_path.parent.mkdir(parents=True, exist_ok=True)
                staging_path.write_bytes(f"mediaforce smoke {rel_path}\n".encode())
            validation_passed = "Failed Check" not in rel_path
            expected_size = staging_path.stat().st_size if staging_path.exists() else 1
            expected_mtime = staging_path.stat().st_mtime_ns if staging_path.exists() else 1
            if "Drifted" in rel_path:
                expected_size = 1
                expected_mtime = 1
            connection.execute(
                staged_artifacts.insert().values(
                    library_item_id=item_id,
                    manifest_run_id="web-smoke-partial-promotion",
                    manifest_path=str(partial_manifest_path),
                    item_index=item_index,
                    encode_host_key=encode_host_key,
                    source_rel_path=rel_path,
                    source_size_bytes=row["size_bytes"],
                    staging_path=str(staging_path),
                    staging_size_bytes=expected_size,
                    staging_mtime_ns=expected_mtime,
                    bytes_saved=max(1, int(row["size_bytes"]) // 2),
                    size_ratio=0.5,
                    validation_json=json.dumps({"passed": validation_passed, "source": "web-smoke"}),
                    staged_at=timestamp,
                    validated_at=timestamp,
                    updated_at=timestamp,
                )
            )
        orphan_path = staging_root / PARTIAL_PROMOTION_PREFIX / "Episode 07 Orphan.mkv"
        orphan_path.parent.mkdir(parents=True, exist_ok=True)
        orphan_path.write_bytes(b"mediaforce smoke orphaned staged output\n")
        partial_path = staging_root / PARTIAL_PROMOTION_PREFIX / "Episode 08.partial.mkv"
        partial_path.write_bytes(b"mediaforce smoke partial staged output\n")

        movie_conflict_row = rows_by_prefix[MOVIE_CONFLICT_PREFIX]
        movie_conflict_id = ids_by_rel_path[str(movie_conflict_row["rel_path"])]
        movie_conflict_destination = Path(str(movie_conflict_row["source_path"])).with_suffix(".mkv")
        movie_conflict_destination.parent.mkdir(parents=True, exist_ok=True)
        movie_conflict_destination.write_bytes(b"mediaforce smoke conflicting movie destination\n")
        movie_conflict_staging = staging_root / Path(str(movie_conflict_row["rel_path"])).with_suffix(".mkv")
        movie_conflict_staging.parent.mkdir(parents=True, exist_ok=True)
        movie_conflict_staging.write_bytes(b"mediaforce smoke staged movie output\n")
        connection.execute(
            staged_artifacts.insert().values(
                library_item_id=movie_conflict_id,
                source_rel_path=movie_conflict_row["rel_path"],
                source_size_bytes=movie_conflict_row["size_bytes"],
                staging_path=str(movie_conflict_staging),
                staging_size_bytes=max(1, int(movie_conflict_row["size_bytes"]) // 2),
                bytes_saved=max(1, int(movie_conflict_row["size_bytes"]) // 2),
                size_ratio=0.5,
                staged_at=timestamp,
                validated_at=timestamp,
                updated_at=timestamp,
            )
        )

        policy = config.resolve_policy(rows[0]["rel_path"])
        sampling_policy = _policy_with_target(policy, 225)
        retry_policy = _policy_with_target(policy, 225)
        movie_loose_row = next(row for row in rows if row["rel_path"] == MOVIE_LOOSE_PREFIX)
        other_sampling_row = rows_by_prefix[OTHER_SAMPLING_PREFIX]
        active_started_at = (fixture_now - timedelta(minutes=15)).isoformat(timespec="seconds")
        heartbeat_at = fixture_now.isoformat(timespec="seconds")
        for item_id, row, job in (
            (
                inserted_ids[4],
                rows[4],
                _job(
                    job_id="web-smoke-sampling",
                    prefix=SAMPLING_PREFIX,
                    status="starting",
                    sample_item={
                        "library_item_id": inserted_ids[4],
                        **rows[4],
                        "source_size_bytes": rows[4]["size_bytes"],
                        "resolved_policy": sampling_policy,
                    },
                    started_at=active_started_at,
                    heartbeat_at=heartbeat_at,
                    progress={
                        "schema_version": 1,
                        "stage": "building_review",
                        "stage_started_at": (
                            fixture_now - timedelta(minutes=3)
                        ).isoformat(timespec="seconds"),
                        "last_progress_at": (
                            fixture_now - timedelta(seconds=20)
                        ).isoformat(timespec="seconds"),
                        "work": {"completed": 2, "total": 3},
                    },
                ),
            ),
            (
                ids_by_rel_path[MOVIE_LOOSE_PREFIX],
                movie_loose_row,
                _job(
                    job_id="web-smoke-movie-sampling",
                    prefix=MOVIE_LOOSE_PREFIX,
                    status="starting",
                    sample_item={
                        "library_item_id": ids_by_rel_path[MOVIE_LOOSE_PREFIX],
                        **movie_loose_row,
                        "source_size_bytes": movie_loose_row["size_bytes"],
                        "resolved_policy": sampling_policy,
                    },
                    started_at=active_started_at,
                    heartbeat_at=heartbeat_at,
                    progress={
                        "schema_version": 1,
                        "stage": "building_review",
                        "stage_started_at": (
                            fixture_now - timedelta(minutes=3)
                        ).isoformat(timespec="seconds"),
                        "last_progress_at": (
                            fixture_now - timedelta(seconds=20)
                        ).isoformat(timespec="seconds"),
                        "work": {"completed": 2, "total": 3},
                    },
                ),
            ),
            (
                ids_by_rel_path[str(other_sampling_row["rel_path"])],
                other_sampling_row,
                _job(
                    job_id="web-smoke-other-sampling",
                    prefix=OTHER_SAMPLING_PREFIX,
                    status="queued",
                    sample_item={
                        "library_item_id": ids_by_rel_path[str(other_sampling_row["rel_path"])],
                        **other_sampling_row,
                        "source_size_bytes": other_sampling_row["size_bytes"],
                        "resolved_policy": sampling_policy,
                    },
                ),
            ),
            (
                inserted_ids[5],
                rows[5],
                _job(
                    job_id="web-smoke-retry",
                    prefix=RETRY_PREFIX,
                    status="failed",
                    sample_item={
                        "library_item_id": inserted_ids[5],
                        **rows[5],
                        "source_size_bytes": rows[5]["size_bytes"],
                        "resolved_policy": retry_policy,
                    },
                    error="Fixture sample failed so retry state is inspectable.",
                ),
            ),
            (
                ids_by_rel_path["tv/Infeasible Goal/Season 1/Episode 01.mkv"],
                rows_by_prefix[INFEASIBLE_PREFIX],
                _job(
                    job_id="web-smoke-infeasible",
                    prefix=INFEASIBLE_PREFIX,
                    status="failed",
                    sample_item={
                        "library_item_id": ids_by_rel_path[
                            "tv/Infeasible Goal/Season 1/Episode 01.mkv"
                        ],
                        **rows_by_prefix[INFEASIBLE_PREFIX],
                        "source_size_bytes": rows_by_prefix[INFEASIBLE_PREFIX]["size_bytes"],
                        "resolved_policy": policy,
                    },
                    error="The resolved stream budget leaves no positive video budget.",
                    result=_target_search_failure(
                        "infeasible",
                        "arithmetically_infeasible_stream_budget",
                    ),
                ),
            ),
            (
                ids_by_rel_path["tv/Quality Conflict/Season 1/Episode 01.mkv"],
                rows_by_prefix[QUALITY_CONFLICT_PREFIX],
                _job(
                    job_id="web-smoke-quality-conflict",
                    prefix=QUALITY_CONFLICT_PREFIX,
                    status="failed",
                    sample_item={
                        "library_item_id": ids_by_rel_path[
                            "tv/Quality Conflict/Season 1/Episode 01.mkv"
                        ],
                        **rows_by_prefix[QUALITY_CONFLICT_PREFIX],
                        "source_size_bytes": rows_by_prefix[QUALITY_CONFLICT_PREFIX]["size_bytes"],
                        "resolved_policy": policy,
                    },
                    error="No measured candidate met both the size target and quality floor.",
                    result=_target_search_failure(
                        "quality_conflict",
                        "all_candidates_violate_quality_floor",
                    ),
                ),
            ),
            (
                ids_by_rel_path["tv/Search Limit/Season 1/Episode 01.mkv"],
                rows_by_prefix[BOUND_EXHAUSTED_PREFIX],
                _job(
                    job_id="web-smoke-bound-exhausted",
                    prefix=BOUND_EXHAUSTED_PREFIX,
                    status="failed",
                    sample_item={
                        "library_item_id": ids_by_rel_path[
                            "tv/Search Limit/Season 1/Episode 01.mkv"
                        ],
                        **rows_by_prefix[BOUND_EXHAUSTED_PREFIX],
                        "source_size_bytes": rows_by_prefix[BOUND_EXHAUSTED_PREFIX]["size_bytes"],
                        "resolved_policy": policy,
                    },
                    error="The configured target-size search bound was exhausted.",
                    result=_target_search_failure(
                        "bound_exhausted",
                        "smallest_quality_safe_candidate_over_target_band",
                    ),
                ),
            ),
        ):
            _ = item_id, row
            connection.execute(calibration_jobs.insert().values(**job))

        sampling_sample_item = {
            "library_item_id": inserted_ids[4],
            **rows[4],
            "source_size_bytes": rows[4]["size_bytes"],
            "resolved_policy": sampling_policy,
        }
        for index, duration_minutes in enumerate((20, 30, 40), start=1):
            history_started = fixture_now - timedelta(days=index, minutes=duration_minutes)
            history_finished = history_started + timedelta(minutes=duration_minutes)
            connection.execute(
                calibration_jobs.insert().values(
                    **_job(
                        job_id=f"web-smoke-sampling-history-{index}",
                        prefix=f"tv/Sampling History {index}/Season 1",
                        status="completed",
                        sample_item=sampling_sample_item,
                        created_at=history_started.isoformat(timespec="seconds"),
                        started_at=history_started.isoformat(timespec="seconds"),
                        finished_at=history_finished.isoformat(timespec="seconds"),
                    )
                )
            )

        shared_row = rows_by_prefix[SHARED_TEST_PREFIX]
        shared_id = ids_by_rel_path[str(shared_row["rel_path"])]
        shared_sample_item = {
            "library_item_id": shared_id,
            **shared_row,
            "source_size_bytes": shared_row["size_bytes"],
            "resolved_policy": _policy_with_target(policy, 314.6),
        }
        stale_started = fixture_now - timedelta(minutes=45)
        stale_finished = fixture_now - timedelta(minutes=30)
        connection.execute(
            calibration_jobs.insert().values(
                **_job(
                    job_id="web-smoke-shared-stale-season",
                    prefix=SHARED_TEST_PREFIX,
                    status="completed",
                    sample_item=shared_sample_item,
                    created_at=stale_started.isoformat(timespec="seconds"),
                    started_at=stale_started.isoformat(timespec="seconds"),
                    finished_at=stale_finished.isoformat(timespec="seconds"),
                )
            )
        )
        shared_sample_item["resolved_policy"] = sampling_policy
        connection.execute(
            calibration_jobs.insert().values(
                **_job(
                    job_id="web-smoke-shared-show-active",
                    prefix=SHARED_TEST_SERIES_PREFIX,
                    status="starting",
                    sample_item=shared_sample_item,
                    created_at=active_started_at,
                    started_at=active_started_at,
                    heartbeat_at=heartbeat_at,
                    progress={
                        "schema_version": 1,
                        "stage": "searching_target",
                        "stage_started_at": (
                            fixture_now - timedelta(minutes=8)
                        ).isoformat(timespec="seconds"),
                        "last_progress_at": (
                            fixture_now - timedelta(seconds=10)
                        ).isoformat(timespec="seconds"),
                    },
                )
            )
        )

        encode_rows = [
            _encode_job(
                project_root=project_root,
                job_id="web-smoke-encode-running",
                prefix=ENCODE_RUNNING_PREFIX,
                rel_path="tv/Encoding Show/Season 1/Episode 01.mkv",
                status="running",
                schedule_close_deadline_at=schedule_closes_at.isoformat(timespec="seconds"),
                progress={
                    "total_duration_seconds": 3600.0,
                    "overall_completed_duration_seconds": 1512.0,
                    "remaining_duration_seconds": 2088.0,
                    "percent_complete": 42,
                    "fps": 82.4,
                    "speed": 1.72,
                    "current_item_number": 1,
                    "total_item_count": 1,
                    "current_item_rel_path": "tv/Encoding Show/Season 1/Episode 01.mkv",
                    "progress_state": "encoding",
                },
            ),
            _encode_job(
                project_root=project_root,
                job_id="web-smoke-encode-retry",
                prefix=ENCODE_RETRY_PREFIX,
                rel_path="tv/Failed Encode/Season 1/Episode 01.mkv",
                status="needs_attention",
                error="Fixture encode missed the requested quality target; retry is available.",
                waiting_reason="Review quality target and retry the folder.",
                progress={
                    "failure_analysis": {
                        "kind": "quality_threshold",
                        "summary": "Fixture encode missed the requested quality target.",
                    }
                },
            ),
            _encode_job(
                project_root=project_root,
                job_id="web-smoke-encode-waiting",
                prefix=ENCODE_WAITING_PREFIX,
                rel_path="movies/Waiting Encode/Feature.mkv",
                status="queued",
                waiting_reason="Waiting for a host schedule window.",
                progress={"progress_state": "schedule_waiting"},
            ),
            _encode_job(
                project_root=project_root,
                job_id="web-smoke-encode-window-too-short",
                prefix="movies/Window Too Short",
                rel_path="movies/Window Too Short/Feature.mkv",
                status="queued",
                waiting_reason=(
                    "Estimated runtime about 4h is longer than every configured host schedule window "
                    "(longest about 3h on Smoke Worker). Widen a host window or use Bypass scheduler."
                ),
            ),
            _encode_job(
                project_root=project_root,
                job_id="web-smoke-encode-waiting-for-full-window",
                prefix="tv/Waiting for Full Window/Season 1",
                rel_path="tv/Waiting for Full Window/Season 1/Episode 01.mkv",
                status="queued",
                waiting_reason=(
                    "Estimated runtime about 2h; waiting for a host window with enough time remaining."
                ),
            ),
            _encode_job(
                project_root=project_root,
                job_id="web-smoke-other-running",
                prefix=OTHER_ACTIVE_PREFIX,
                rel_path="other/Active Batch/Camera C.mkv",
                status="running",
                bypass_schedule=True,
                progress={
                    "total_duration_seconds": 3600.0,
                    "overall_completed_duration_seconds": 900.0,
                    "remaining_duration_seconds": 2700.0,
                    "percent_complete": 25,
                    "fps": 76.0,
                    "speed": 1.55,
                    "current_item_number": 1,
                    "total_item_count": 1,
                    "current_item_rel_path": "other/Active Batch/Camera C.mkv",
                    "progress_state": "encoding",
                },
            ),
        ]
        for row in encode_rows:
            connection.execute(encode_jobs.insert().values(**row))
        connection.execute(
            encode_queue_state.insert().values(
                queue_name="heavy",
                is_paused=0,
                stop_requested=0,
                active_job_id="web-smoke-encode-running",
                updated_at=_now(),
            )
        )
        start_evidence_work(
            connection,
            config,
            FOLDER_PREFIX,
            limit=2,
        )

    _write_review_states(config, rows_by_prefix)
    _write_stale_proposal_state(config)
    save_catalog_signature(config)

    return {
        "profile": profile,
        "folderPrefix": FOLDER_PREFIX,
        "folderRoute": "/folders/tv/Example%20Show/Season%201",
        "folderRoutes": [
            {
                "label": "Other Library fixture",
                "route": "/other",
                "marker": "Field Notes",
            },
            {
                "label": "Other Studio bounded-folder fixture",
                "route": "/folders/other/Field%20Notes",
                "marker": "Field Notes",
                "stageMarker": "Set up sample",
            },
            {
                "label": "Other Studio exact-file fixture",
                "route": "/folders/other/Loose%20Capture.mkv",
                "marker": "Loose Capture",
                "stageMarker": "Only this file",
            },
            {
                "label": "Other Studio profile-blocked fixture",
                "route": "/folders/other/Needs%20Probe",
                "marker": "Needs Probe",
                "stageMarker": "Profile blocked",
            },
            {
                "label": "Other Studio oversized-scope fixture",
                "route": "/folders/other/Oversized%20Intake",
                "marker": "Oversized Intake",
                "stageMarker": "More than 250 files",
            },
            {
                "label": "Other Studio sample-waiting fixture",
                "route": "/folders/other/Sampling%20Folder",
                "marker": "Sampling Folder",
                "stageMarker": "Sample waiting",
            },
            {
                "label": "Other Studio review-ready fixture",
                "route": "/folders/other/Review%20Ready",
                "marker": "Review Ready",
                "stageMarker": "Compare clips",
            },
            {
                "label": "Other Studio active-processing fixture",
                "route": "/folders/other/Active%20Batch",
                "marker": "Active Batch",
                "stageMarker": "Compressing now.",
            },
            {
                "label": "Other Studio validation fixture",
                "route": "/folders/other/Validation%20Ready",
                "marker": "Validation Ready",
                "stageMarker": "Check compressed file",
            },
            {
                "label": "Other Studio promotion fixture",
                "route": "/folders/other/Promotion%20Ready",
                "marker": "Promotion Ready",
                "stageMarker": "Replace original file",
            },
            {
                "label": "Folder Studio waiting fixture",
                "route": "/folders/tv/Example%20Show/Season%201",
                "marker": "Example Show",
                "stageMarker": "Choose a size for Season 1",
            },
            {
                "label": "Movie Studio editions fixture",
                "route": "/folders/movies/Editions%20Showcase",
                "marker": "Editions Showcase",
                "stageMarker": "Files and editions",
            },
            {
                "label": "Movie Studio review-sample requirement fixture",
                "route": "/folders/movies/Editions%20Showcase",
                "marker": "Editions Showcase",
                "stageMarker": "need a sample before compressing",
            },
            {
                "label": "Movie Studio stale sample-plan fixture",
                "route": "/folders/movies/Stale%20Sample%20Plan",
                "marker": "Stale Sample Plan",
                "stageMarker": "Sample plan is out of date",
            },
            {
                "label": "Movie Studio exact-file fixture",
                "route": "/folders/movies/Loose%20Feature.mkv",
                "marker": "Loose Feature",
                "stageMarker": "Only this file",
            },
            {
                "label": "Movie Studio sample-monitoring fixture",
                "route": "/folders/movies/Loose%20Feature.mkv",
                "marker": "Loose Feature",
                "stageMarker": "Mediaforce is creating the sample now.",
            },
            {
                "label": "Movie Studio review-ready fixture",
                "route": "/folders/movies/Review%20Ready",
                "marker": "Review Ready",
                "stageMarker": "Download comparison clips",
            },
            {
                "label": "Movie Studio promotion-conflict fixture",
                "route": "/folders/movies/Promotion%20Conflict",
                "marker": "Promotion Conflict",
                "stageMarker": "Mediaforce cannot replace this movie yet.",
            },
            {
                "label": "Movie Studio validation fixture",
                "route": "/folders/movies/Validation%20Ready",
                "marker": "Validation Ready",
                "stageMarker": "Check compressed file",
            },
            {
                "label": "Movie Studio checked-output preview fixture",
                "route": "/folders/movies/Replacement%20Ready%20Large",
                "marker": "Replacement Ready Large",
                "stageMarker": "Preview checked output",
            },
            {
                "label": "Movie Studio active-processing fixture",
                "route": "/folders/movies/Waiting%20Encode",
                "marker": "Waiting Encode",
                "stageMarker": "Queued, but not able to start",
            },
            {
                "label": "Movie Studio infeasible-target fixture",
                "route": "/folders/movies/Target%20Too%20Large",
                "marker": "Target Too Large",
                "stageMarker": "The requested size is larger than the allowed 80% of the original file.",
            },
            {
                "label": "Movie Studio infeasible-target remedy fixture",
                "route": "/folders/movies/Target%20Too%20Large",
                "marker": "Target Too Large",
                "stageMarker": "Choose a smaller target in library settings.",
            },
            {
                "label": "Movie Studio completed fixture",
                "route": "/folders/movies/Archive%20Ready",
                "marker": "Archive Ready",
                "stageMarker": "This movie is finished.",
            },
            {
                "label": "Folder Studio sampling fixture",
                "route": "/folders/tv/Sampling%20Show/Season%201",
                "marker": "Sampling Show",
                "stageMarker": "ESTIMATED REMAINING",
            },
            {
                "label": "Folder Studio shared-scope sample fixture",
                "route": "/folders/tv/Shared%20Test%20Show/Season%201",
                "marker": "Shared Test Show",
                "stageMarker": "A show-level sample is running",
            },
            {
                "label": "Folder Studio retry fixture",
                "route": "/folders/tv/Retry%20Show/Season%201",
                "marker": "Retry Show",
                "stageMarker": "Retry sample",
            },
            {
                "label": "Completed cleanup fixture",
                "route": "/completed",
                "marker": "Blocked Cleanup",
            },
            {
                "label": "Ops unavailable host fixture",
                "route": "/ops",
                "marker": "Smoke Worker",
            },
            {
                "label": "Folder Studio review-ready fixture",
                "route": "/folders/tv/Review%20Ready/Season%201",
                "marker": "Review Ready",
                "stageMarker": "Ready to review",
            },
            {
                "label": "Folder Studio absolute-target fixture",
                "route": "/folders/tv/Absolute%20Goal/Season%201",
                "marker": "Absolute Goal",
                "stageMarker": "225 MB",
            },
            {
                "label": "Folder Studio approved fixture",
                "route": "/folders/tv/Approved%20Show/Season%201",
                "marker": "Approved Show",
                "stageMarker": "Ready to compress the season",
            },
            {
                "label": "Folder Studio protected approved fixture",
                "route": "/folders/tv/Protected%20Ready/Season%202",
                "marker": "Protected Ready",
                "stageMarker": "This season is ready, but protected",
            },
            {
                "label": "Folder Studio older-season override fixture",
                "route": "/folders/tv/Protected%20Ready",
                "marker": "Protected Ready",
                "stageMarker": "Ready to compress the older seasons",
            },
            {
                "label": "Folder Studio missed-target fixture",
                "route": "/folders/tv/Overshoot%20Show/Season%201",
                "marker": "Overshoot Show",
                "stageMarker": "Size goal not met",
            },
            {
                "label": "Folder Studio under-target fixture",
                "route": "/folders/tv/Undershoot%20Show/Season%201",
                "marker": "Undershoot Show",
                "stageMarker": "Mediaforce will not spend the unused size automatically.",
            },
            {
                "label": "Folder Studio infeasible fixture",
                "route": "/folders/tv/Infeasible%20Goal/Season%201",
                "marker": "Infeasible Goal",
                "stageMarker": "This size cannot fit the required streams",
            },
            {
                "label": "Folder Studio quality-conflict fixture",
                "route": "/folders/tv/Quality%20Conflict/Season%201",
                "marker": "Quality Conflict",
                "stageMarker": "This size conflicts with the quality floor",
            },
            {
                "label": "Folder Studio target-search-bound fixture",
                "route": "/folders/tv/Search%20Limit/Season%201",
                "marker": "Search Limit",
                "stageMarker": "The sample reached a configured limit",
            },
            {
                "label": "Folder Studio active processing fixture",
                "route": "/folders/tv/Encoding%20Show/Season%201",
                "marker": "Encoding Show",
                "stageMarker": "Compressing Season 1",
            },
            {
                "label": "Folder Studio retryable processing fixture",
                "route": "/folders/tv/Failed%20Encode/Season%201",
                "marker": "Failed Encode",
                "stageMarker": "The season stopped",
            },
            {
                "label": "Folder Studio validation fixture",
                "route": "/folders/tv/Validation%20Ready/Season%201",
                "marker": "Validation Ready",
                "stageMarker": "Let’s check every new file",
            },
            {
                "label": "Folder Studio promotion fixture",
                "route": "/folders/tv/Promotion%20Ready/Season%201",
                "marker": "Promotion Ready",
                "stageMarker": "Ready to replace the original episodes",
            },
            {
                "label": "Folder Studio partial-promotion fixture",
                "route": "/folders/tv/Partial%20Promotion/Season%201",
                "marker": "Partial Promotion",
                "stageMarker": "Whole season required",
            },
            {
                "label": "Folder Studio finished fixture",
                "route": "/folders/tv/Finished%20Show/Season%201",
                "marker": "Finished Show",
                "stageMarker": "Season 1 is ready",
            },
        ],
        "completedPrefix": COMPLETED_PREFIX,
        "libraryItems": len(rows),
        "encodeJobs": 6,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed deterministic web smoke fixture state."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--profile",
        choices=("default", "empty"),
        default="default",
        help="Fixture profile to seed.",
    )
    args = parser.parse_args()
    print(json.dumps(seed(args.config, profile=args.profile), sort_keys=True))


if __name__ == "__main__":
    main()
