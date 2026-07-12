import asyncio
import multiprocessing
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from fastapi.routing import APIRoute
from sqlalchemy import func
from sqlalchemy import select

from mediaforce.advisor import (
    RUN_VERDICT_PROMPT_VERSION,
    REVIEW_ARTIFACT_CRITIQUE_PROMPT_VERSION,
    SEED_PROMPT_VERSION,
    SeedPolicyResponse,
    TuningPolicyResponse,
    _build_review_artifact_critique_prompt,
    _build_tune_prompt,
    _build_operator_note_parse_prompt,
    _extract_seed_payload,
    _filter_audio_specific_guardrail_issues,
    _force_repeated_tune_experiment,
    _policy_response_schema,
    _run_tune_self_check,
    _build_seed_prompt,
    apply_seed_policy,
    request_review_artifact_critique,
    request_operator_note_parse,
    request_seed_policy,
    request_note_tuning,
    request_run_verdict,
)
from mediaforce.core.config import ConfigPaths, MediaforceConfig, load_config, save_runtime_settings
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import calibration_jobs
from mediaforce.core.db_tables import item_events
from mediaforce.core.db_tables import learning_artifacts
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.core.db_tables import tuning_sessions
from mediaforce.core.evidence import stable_policy_hash
from mediaforce.core.process_control import ProcessCancelledError
from mediaforce.core.type_defs import object_dict
from mediaforce.execution import resolve_stream_budget_ledger
from mediaforce.hosts.types import HostReadinessError, HostStatus
from mediaforce.tuning.tuning_memory import (
    promote_learning_artifact,
    record_tuning_session,
    record_visual_approval_artifact,
    retrieve_learning_context,
    sibling_approved_season_memory,
)
from mediaforce.tuning.quality_risk import build_quality_risk_contract, quality_risk_public_view, \
    with_quality_risk_intent
from mediaforce.tuning.calibration_jobs import load_latest_failed_target_size_sample_job
from mediaforce.tuning.target_size_search import TargetSizeSearchError
from mediaforce.web.app import (
    _advice_file,
    _backfill_multimodal_review_pack,
    _build_multimodal_review_pack,
    _build_review_compare_video_from_pairs,
    _build_seed_policy_payload,
    _build_review_compare_video,
    _download_review_compare_action,
    _ffmpeg_concat_file_line,
    _maybe_seed_baseline_policy,
    _build_tuning_runtime_toolbelt,
    _calibration_file,
    _clear_folder_tuning_state,
    _folder_display_policy,
    _run_periodic_cleanup,
    _review_compare_bundle_entries,
    _review_compare_pair_entries,
    _multimodal_review_pack_public_view,
    _operator_requested_experiment,
    _planned_audio_review_context,
    _proposal_file,
    _review_pack_dir,
    _upsert_override,
)
from mediaforce.web.runtime.archive_cleanup import archive_cleanup_summary, clear_archive_cleanup_action
from mediaforce.web.runtime.calibration_runtime import CalibrationRunDeps, run_calibration_job
from mediaforce.web.runtime.completed_runtime import (
    ORIGINALS_REMOVED_EVENT,
    clear_completed_backups_action,
    completed_page_payload,
    confirm_originals_removed_action,
)
from mediaforce.web.runtime.dashboard_payloads import dashboard_summary_payload
from mediaforce.web.runtime.folder_ai_tuning import (
    FolderAiTuneDeps,
    _blocking_sample_evidence_issue,
    _can_keep_first_size_budget_sample,
    _keep_first_size_budget_sample,
    _operator_request_with_retrieved_memory_authority,
    _proposal_can_queue,
    _proposal_ready_message,
    _size_budget_measurement_fragment,
    _unconfirmed_legacy_size_issue,
    folder_ai_tune_confirm_action,
    folder_ai_tune_preview_action,
)
from mediaforce.web.runtime.folder_tuning_advice import (
    audio_tradeoff_hint,
    operator_request_from_intent,
    operator_request_signature,
)
from mediaforce.web.runtime.folder_tuning_helpers import (
    measured_size_budget_policy_fragment,
    proposal_alignment_issue,
    size_budget_sample_issue,
)
from mediaforce.web.runtime.folder_state import _merge_review_pairs


def _runtime_settings_writer(path_text: str, ready_path_text: str, mode: str) -> None:
    from pathlib import Path

    from mediaforce.core.config import update_runtime_settings

    path = Path(path_text)
    ready_path = Path(ready_path_text)

    def _apply(runtime_settings: dict[str, object]) -> dict[str, object]:
        if mode == "folder":
            runtime_settings["folder_policy_overrides"] = [{"path_prefix": "tv/House/Season 2"}]
            ready_path.write_text("ready\n")
            time.sleep(0.5)
        else:
            runtime_settings["remote_hosts"] = [{"label": "m4-studio"}]
        return runtime_settings

    update_runtime_settings(path, _apply)


def _absolute_size_fragment(
        size_mb: float,
        runtime_minutes: float,
        *,
        preserve_source_resolution: bool = False,
) -> dict[str, Any]:
    video: dict[str, Any] = {
        "size_goal_schema_version": 1,
        "size_goal_mode": "absolute",
        "size_goal_source": "operator_note",
        "sample_projection_tolerance_percent": 10.0,
        "final_output_tolerance_percent": 5.0,
        "target_size_bytes": int(round(size_mb * 1_000_000)),
        "target_size_mb": size_mb,
        "target_runtime_minutes": runtime_minutes,
    }
    if preserve_source_resolution:
        video.update(
            {
                "resolution_intent_mode": "source",
                "resolution_intent_source": "operator",
                "max_height": 0,
            }
        )
    return {"video": video}


def _codex_lab_jsonl(final_text: str) -> str:
    events = [
        {"type": "thread.started", "thread_id": "test-thread"},
        {
            "type": "item.completed",
            "item": {"id": "test-message", "type": "agent_message", "text": final_text},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 128,
                "cached_input_tokens": 0,
                "output_tokens": max(1, len(final_text) // 4),
            },
        },
    ]
    return "\n".join(json.dumps(event, separators=(",", ":")) for event in events)


def _fake_operator_note_parse(
        *,
        project_root: Path,
        payload: dict[str, object],
        **_: object,
) -> dict[str, object] | None:
    project_root.as_posix()
    note_value = payload.get("operator_note")
    note = note_value.strip() if isinstance(note_value, str) else ""
    if not note:
        return None
    lower = note.lower()
    budget_match = re.search(
        r"(?<![\w.])(?P<sign>[+-]?)\s*(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>kb|mb|gb|tb)",
        lower,
    )
    if budget_match is not None and budget_match.group("sign") == "-":
        budget_match = None
    metric_match = re.search(
        r"\b(?P<metric>vmaf|xpsnr)\b(?:\s+(?:target|around|about|roughly|approximately|approx|at|to|of)|\s*=){0,3}\s*(?P<target>\d{2}(?:\.\d+)?)\b",
        lower,
    )
    if metric_match is None:
        metric_match = re.search(r"\b(?P<target>\d{2}(?:\.\d+)?)\s*(?P<metric>vmaf|xpsnr)\b", lower)
    metric = None
    metric_target = None
    if metric_match:
        metric = str(metric_match.group("metric") or "").strip() or None
        metric_target = float(metric_match.group("target") or 0)
    size_budget_value = float(budget_match.group("amount")) if budget_match else None
    size_budget_unit = str(budget_match.group("unit")) if budget_match else None
    scale_match = re.search(
        r"\b(?:downsample|downscale|scale|resize|cap|limit|convert|make)\b.{0,40}\b(?P<height>480|540|576|720|900|1080|1440|2160)p\b",
        lower,
    )
    if scale_match is None:
        scale_match = re.search(
            r"\b(?P<height>480|540|576|720|900|1080|1440|2160)p\b.{0,40}\b(?:downsample|downscale|scale|resize|cap|limit)\b",
            lower,
        )
    source_resolution_requested = bool(re.search(
        r"\b(?:source|original|native)\s+resolution\b|\b(?:do\s+not|don't|dont|no)\s+(?:downsample|downscale|scale\s+down)\b|\bkeep\s+max_height\s+(?:unset|at\s+0|0)\b|\bmax_height\s+(?:unset|0)\b",
        lower,
    ))
    scale_height = int(scale_match.group("height")) if scale_match else None
    if source_resolution_requested:
        scale_height = 0
    black_bar_handling = "smart" if re.search(r"\b(?:smart|auto(?:matic)?)\b.{0,24}\bblack[- ]?bar", lower) or re.search(
        r"\bblack[- ]?bar.{0,24}\b(?:smart|auto(?:matic)?)\b", lower
    ) else None
    crop_match = re.search(r"\b(?P<crop>\d{3,5}:\d{3,5}:\d{1,5}:\d{1,5})\b", lower)
    crop = crop_match.group("crop") if crop_match else None
    evidence_authority = "none"
    if re.search(
            r"\b(?:reject|rejected|decline|declined)\b.*\b(?:sample|clip|encode|result|draft|quality)\b|"
            r"\b(?:sample|clip|encode|result|draft|quality)\b.*\b(?:look(?:s|ed)?|sound(?:s|ed)?|is|was)\s+"
            r"(?:bad|worse|unacceptable|blocky|banded|damaged)\b|"
            r"\b(?:saw|noticed|showed|shows|had|has|with)\b.{0,48}\b"
            r"(?:artifact(?:s|ing)?|banding|blocking|blockiness|smearing|damage|ringing|macroblocking|blur)\b",
            lower,
    ):
        evidence_authority = "rejected_visual_result"
    elif re.search(r"\b(?:approve|approved|accept|accepted)\b.*\b(?:sample|clip|encode|result|draft|quality)\b", lower):
        evidence_authority = "approved_visual_result"
    elif re.search(
            r"\b(?:look(?:s|ed)?|sound(?:s|ed)?)\s+(?:good|great|excellent|fine|clean|identical|indistinguishable)\b",
            lower,
    ):
        evidence_authority = "operator_observed"
    explicit_request_count = sum(
        value is not None for value in (size_budget_value, metric_target, scale_height, black_bar_handling, crop)
    )
    if explicit_request_count > 1:
        request_type = "combined_experiment"
    elif size_budget_value is not None:
        request_type = "size_budget"
    elif metric_target is not None:
        request_type = "metric_target"
    elif scale_height is not None or black_bar_handling is not None or crop is not None:
        request_type = "scale_target"
    else:
        request_type = "none"
    exploratory = any(
        phrase in lower
        for phrase in (
            "can we try",
            "will that help",
            "want to understand",
            "is realistic",
        )
    )
    operator_confirmed = request_type != "none" and not exploratory
    intent_type = "exploratory_question" if exploratory else (
        "direct_request" if request_type != "none" else "approval_feedback" if evidence_authority != "none" else "other"
    )
    return {
        "summary": "parsed note",
        "intent_type": intent_type,
        "request_type": request_type,
        "operator_confirmed": operator_confirmed,
        "measured_size_followup": "revise" in lower and "sample" in lower and "target" in lower,
        "evidence_authority": evidence_authority,
        "metric": metric,
        "metric_target": metric_target,
        "size_budget_value": size_budget_value,
        "size_budget_unit": size_budget_unit,
        "scale_height": scale_height,
        "black_bar_handling": black_bar_handling,
        "crop": crop,
        "reasoning_note": "test parser",
    }


def _ready_calibration_host(host_data: dict[str, Any]) -> HostStatus:
    return HostStatus(
        key=str(host_data.get("key") or host_data.get("host") or "local"),
        label=str(host_data.get("label") or "Test host"),
        mode="ssh",
        priority=0,
        capabilities=["sample_calibration"],
        available=True,
        message="Mounted and ready",
        missing_paths=[],
    )


class TuningRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = MediaforceConfig(
            raw={
                "state": {},
                "media": {
                    "source_roots": {"tv": str(self.root / "source" / "tv")},
                    "staging_root": str(self.root / "staging"),
                    "archive_root": str(self.root / "archive"),
                },
                "remote_hosts": [],
            },
            paths=ConfigPaths(
                project_root=self.root,
                config_path=self.root / "config.toml",
                db_path=self.root / "library.sqlite3",
                run_manifest_dir=self.root / "runs",
                web_state_dir=self.root / "web",
                review_dir=self.root / "review",
                runtime_settings_path=self.root / "runtime.json",
            ),
        )
        self.note_parse_patcher = patch(
            "mediaforce.web.runtime.folder_tuning_advice.request_operator_note_parse",
            side_effect=_fake_operator_note_parse,
        )
        self.note_parse_patcher.start()

    def tearDown(self) -> None:
        self.note_parse_patcher.stop()
        self.tempdir.cleanup()

    def _capture_subprocess_commands(self, response_body: str) -> tuple[list[list[str]], object]:
        commands: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(kwargs, dict)
            commands.append(cmd)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(response_body), "stderr": ""})()

        return commands, fake_run

    def _assert_structured_subprocess_call(self, commands: list[list[str]]) -> None:
        self.assertTrue(commands)
        command = commands[0]
        self.assertEqual(command[:3], ["codex-lab", "exec", "--ephemeral"])
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--output-schema", command)
        self.assertIn("--json", command)
        self.assertEqual(command[-1], "-")

    def _insert_promoted_artifact(
            self,
            *,
            rel_path: str,
            promoted_at: str,
            archived_size_bytes: int,
            bytes_saved: int,
            archive_exists: bool = True,
    ) -> None:
        source_path = self.root / "source" / rel_path
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("source")
        archived_path = self.config.archive_root / Path(rel_path)
        if archive_exists:
            archived_path.parent.mkdir(parents=True, exist_ok=True)
            archived_path.write_bytes(b"a" * archived_size_bytes)
        with open_db(self.config.paths.db_path) as connection:
            result = connection.execute(
                library_items.insert().values(
                    source_path=str(source_path),
                    rel_path=rel_path,
                    media_root="tv",
                    parent_dir=str(Path(rel_path).parent),
                    file_name=Path(rel_path).name,
                    container=Path(rel_path).suffix or ".mkv",
                    size_bytes=archived_size_bytes,
                    mtime_ns=1,
                    fingerprint=f"fp-{rel_path}",
                    duration_seconds=1800.0,
                    video_codec="av1",
                    audio_summary_json="[]",
                    subtitle_summary_json="[]",
                    status="promoted",
                    last_scan_id="scan-1",
                    discovered_at=promoted_at,
                    last_seen_at=promoted_at,
                    updated_at=promoted_at,
                )
            )
            library_item_id = int(result.inserted_primary_key[0])
            connection.execute(
                staged_artifacts.insert().values(
                    library_item_id=library_item_id,
                    staging_path=str(self.root / "staging" / Path(rel_path).name),
                    bytes_saved=bytes_saved,
                    promoted_at=promoted_at,
                    archived_source_path=str(archived_path),
                    updated_at=promoted_at,
                )
            )
            connection.commit()

    def _insert_library_item(self, *, rel_path: str, status: str = "discovered") -> None:
        source_path = self.root / "source" / rel_path
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("source")
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                library_items.insert().values(
                    source_path=str(source_path),
                    rel_path=rel_path,
                    media_root="tv",
                    parent_dir=str(Path(rel_path).parent),
                    file_name=Path(rel_path).name,
                    container=Path(rel_path).suffix or ".mkv",
                    size_bytes=1024,
                    mtime_ns=1,
                    fingerprint=f"fp-{rel_path}",
                    duration_seconds=1800.0,
                    video_codec="h264",
                    audio_summary_json="[]",
                    subtitle_summary_json="[]",
                    status=status,
                    last_scan_id="scan-1",
                    discovered_at="2025-01-01T00:00:00Z",
                    last_seen_at="2025-01-01T00:00:00Z",
                    updated_at="2025-01-01T00:00:00Z",
                )
            )
            connection.commit()

    def test_request_operator_note_parse_uses_structured_runtime_path(self) -> None:
        commands, fake_run = self._capture_subprocess_commands(
            json.dumps(
                {
                    "summary": "Direct size-budget request.",
                    "intent_type": "direct_request",
                    "request_type": "size_budget",
                    "operator_confirmed": True,
                    "measured_size_followup": False,
                    "evidence_authority": "none",
                    "metric": None,
                    "metric_target": None,
                    "size_budget_value": 300,
                    "size_budget_unit": "mb",
                    "scale_height": None,
                    "black_bar_handling": None,
                    "crop": None,
                    "reasoning_note": "Directive question asking for action.",
                }
            )
        )

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            parsed = request_operator_note_parse(
                project_root=self.root,
                payload={"operator_note": "Can you target 300MB per episode?"},
            )

        assert parsed is not None
        self.assertTrue(parsed["operator_confirmed"])
        self.assertEqual(parsed["evidence_authority"], "none")
        self._assert_structured_subprocess_call(commands)

    def test_folder_display_policy_prefers_pending_preview_before_first_sample(self) -> None:
        sample_item = {
            "resolved_policy": {
                "video": {
                    "quality_metric": "auto",
                    "target_vmaf": 95.0,
                    "min_target_vmaf": 93.0,
                    "target_xpsnr": 35.5,
                    "min_target_xpsnr": 34.5,
                    "max_encoded_percent": 55,
                }
            }
        }

        pending_proposal = {
            "preview_policy": {
                "video": {
                    "quality_metric": "vmaf",
                    "target_vmaf": 94.5,
                    "min_target_vmaf": 93.0,
                    "max_encoded_percent": 50,
                }
            },
            "current_policy": sample_item["resolved_policy"],
        }

        policy = _folder_display_policy(
            sample_item=sample_item,
            calibration=None,
            pending_proposal=pending_proposal,
        )

        self.assertEqual(policy["video"]["quality_metric"], "vmaf")
        self.assertEqual(policy["video"]["target_vmaf"], 94.5)
        self.assertEqual(policy["video"]["max_encoded_percent"], 50)

    def test_folder_display_policy_prefers_saved_calibration_over_pending_preview(self) -> None:
        sample_item = {"resolved_policy": {"video": {"quality_metric": "auto", "target_vmaf": 95.0}}}
        calibration = {"policy": {"video": {"quality_metric": "xpsnr", "target_xpsnr": 35.5}}}
        pending_proposal = {
            "preview_policy": {"video": {"quality_metric": "vmaf", "target_vmaf": 94.5}},
        }

        policy = _folder_display_policy(
            sample_item=sample_item,
            calibration=calibration,
            pending_proposal=pending_proposal,
        )

        self.assertEqual(policy["video"]["quality_metric"], "xpsnr")
        self.assertEqual(policy["video"]["target_xpsnr"], 35.5)

    def test_folder_display_policy_prefers_live_summary_over_sample_snapshot(self) -> None:
        sample_item = {
            "resolved_policy": {
                "video": {
                    "quality_metric": "vmaf",
                    "target_vmaf": 95.0,
                    "max_height": 720,
                }
            }
        }
        summary = {
            "resolved_policy": {
                "video": {
                    "quality_metric": "vmaf",
                    "target_vmaf": 85.0,
                    "max_height": 1080,
                }
            }
        }

        policy = _folder_display_policy(
            sample_item=sample_item,
            calibration=None,
            pending_proposal=None,
            summary=summary,
        )

        self.assertEqual(policy["video"]["target_vmaf"], 85.0)
        self.assertEqual(policy["video"]["max_height"], 1080)

    def test_folder_ai_tune_confirm_retries_latest_stopped_sample_without_pending_proposal(self) -> None:
        saved_jobs: list[dict[str, object]] = []
        host = HostStatus(
            key="cbusillo@localhost",
            label="M4 Studio",
            mode="ssh",
            priority=10,
            capabilities=["encode_queue", "sample_calibration"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )
        retryable_job = {
            "job_id": "job-old",
            "status": "stopped",
            "lane": "sample",
            "mode": "sample",
            "action": "ai_tune",
            "prefix": "tv/show",
            "host": {"key": host.key},
            "notes": "Aim for 8%",
            "policy": {"video": {"target_vmaf": 93.5, "default_grain": 0}},
            "sample_item": {
                "library_item_id": 7,
                "rel_path": "tv/show/episode.mkv",
                "source_path": str(self.root / "source" / "tv" / "show" / "episode.mkv"),
                "source_size_bytes": 1234,
                "video_codec": "h264",
                "duration_seconds": 120.0,
                "width": 1920,
                "height": 1080,
                "audio_summary": [],
                "subtitle_summary": [],
            },
            "result": {"quality_score": 91.2},
            "created_at": "2026-04-11T15:18:09+00:00",
            "started_at": "2026-04-11T15:18:10+00:00",
            "finished_at": "2026-04-11T15:18:21+00:00",
            "updated_at": "2026-04-11T15:18:21+00:00",
            "error": "Calibration queue job was stopped and cleaned up.",
        }

        deps = FolderAiTuneDeps(
            resolve_sample_host=lambda _config, host_key: host if host_key == host.key else None,
            load_job_state=lambda _connection, _config, prefix: dict(retryable_job) if prefix == "tv/show" else None,
            load_retryable_sample_job_state=lambda _connection, _config, prefix: dict(retryable_job)
            if prefix == "tv/show"
            else None,
            sample_item=lambda *_args, **_kwargs: None,
            operator_requested_experiment=lambda *_args, **_kwargs: None,
            load_calibration_state=lambda *_args, **_kwargs: None,
            recent_tuning_sessions=lambda *_args, **_kwargs: [],
            matching_request_history=lambda *_args, **_kwargs: None,
            metric_support=lambda: {},
            maybe_seed_baseline_policy=lambda *_args, **_kwargs: None,
            seed_advice_payload=lambda *_args, **_kwargs: None,
            proposal_alignment_issue=lambda *_args, **_kwargs: None,
            now_iso=lambda: "2026-04-11T15:30:00+00:00",
            proposal_signal_copy=lambda *_args, **_kwargs: "",
            proposal_context_snapshot=lambda *_args, **_kwargs: {},
            save_pending_proposal=lambda *_args, **_kwargs: None,
            pending_proposal_public_view=lambda payload: payload,
            build_tuning_runtime_toolbelt=lambda *_args, **_kwargs: {},
            review_pack_dir=lambda *_args, **_kwargs: self.root / "review-pack",
            remove_path_if_exists=lambda *_args, **_kwargs: None,
            build_multimodal_review_pack=lambda *_args, **_kwargs: None,
            multimodal_review_pack_public_view=lambda *_args, **_kwargs: None,
            tuning_advice_payload=lambda *_args, **_kwargs: {},
            load_pending_proposal=lambda *_args, **_kwargs: None,
            apply_policy_fragment=lambda current, fragment: {
                **current,
                "video": {
                    **object_dict(current.get("video")),
                    **object_dict(fragment.get("video")),
                },
            },
            save_advice_state=lambda *_args, **_kwargs: None,
            save_job_state=lambda _connection, _config, _prefix, payload: saved_jobs.append(dict(payload)),
            clear_pending_proposal=lambda *_args, **_kwargs: None,
            record_tuning_session=lambda *_args, **_kwargs: "session-1",
        )

        result = folder_ai_tune_confirm_action(self.config, deps, "tv/show", "")

        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "Queued the saved sample draft again.")
        self.assertEqual(len(saved_jobs), 1)
        self.assertEqual(saved_jobs[0]["status"], "queued")
        self.assertEqual(saved_jobs[0]["action"], "ai_tune")
        self.assertIsNone(saved_jobs[0]["started_at"])
        self.assertIsNone(saved_jobs[0]["finished_at"])
        self.assertIsNone(saved_jobs[0]["error"])
        self.assertIsNone(saved_jobs[0]["result"])
        self.assertNotEqual(saved_jobs[0]["job_id"], retryable_job["job_id"])

    def test_saved_sample_work_blocks_unconfirmed_legacy_size_goal(self) -> None:
        issue = _unconfirmed_legacy_size_issue(
            self.config,
            {"video": {"target_size_mb": 225, "target_runtime_minutes": 45}},
        )

        self.assertIsNotNone(issue)
        self.assertIn("runtime-normalized or an absolute", str(issue))
        self.assertIsNone(
            _unconfirmed_legacy_size_issue(
                self.config,
                {
                    "video": {
                        "size_goal_mode": "absolute",
                        "target_size_bytes": 225_000_000,
                        "target_size_mb": 225,
                        "target_runtime_minutes": 88,
                    }
                },
            )
        )
    def test_folder_ai_tune_confirm_fails_closed_when_stale_proposal_id_has_no_pending_proposal(self) -> None:
        host = HostStatus(
            key="cbusillo@localhost",
            label="M4 Studio",
            mode="ssh",
            priority=10,
            capabilities=["encode_queue", "sample_calibration"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )
        retryable_job = {
            "job_id": "job-old",
            "status": "stopped",
            "lane": "sample",
            "mode": "sample",
            "action": "ai_tune",
            "prefix": "tv/show",
            "host": {"key": host.key},
            "notes": "Aim for 8%",
            "policy": {"video": {"target_vmaf": 93.5, "default_grain": 0}},
            "sample_item": {
                "rel_path": "tv/show/episode.mkv",
                "source_path": str(self.root / "source" / "tv" / "show" / "episode.mkv"),
                "source_size_bytes": 1234,
            },
            "created_at": "2026-04-11T15:18:09+00:00",
            "started_at": "2026-04-11T15:18:10+00:00",
            "finished_at": "2026-04-11T15:18:21+00:00",
            "updated_at": "2026-04-11T15:18:21+00:00",
            "error": "Calibration queue job was stopped and cleaned up.",
        }

        deps = FolderAiTuneDeps(
            resolve_sample_host=lambda _config, host_key: host if host_key == host.key else None,
            load_job_state=lambda _connection, _config, prefix: dict(retryable_job) if prefix == "tv/show" else None,
            load_retryable_sample_job_state=lambda _connection, _config, prefix: dict(retryable_job)
            if prefix == "tv/show"
            else None,
            sample_item=lambda *_args, **_kwargs: None,
            operator_requested_experiment=lambda *_args, **_kwargs: None,
            load_calibration_state=lambda *_args, **_kwargs: None,
            recent_tuning_sessions=lambda *_args, **_kwargs: [],
            matching_request_history=lambda *_args, **_kwargs: None,
            metric_support=lambda: {},
            maybe_seed_baseline_policy=lambda *_args, **_kwargs: None,
            seed_advice_payload=lambda *_args, **_kwargs: None,
            proposal_alignment_issue=lambda *_args, **_kwargs: None,
            now_iso=lambda: "2026-04-11T15:30:00+00:00",
            proposal_signal_copy=lambda *_args, **_kwargs: "",
            proposal_context_snapshot=lambda *_args, **_kwargs: {},
            save_pending_proposal=lambda *_args, **_kwargs: None,
            pending_proposal_public_view=lambda payload: payload,
            build_tuning_runtime_toolbelt=lambda *_args, **_kwargs: {},
            review_pack_dir=lambda *_args, **_kwargs: self.root / "review-pack",
            remove_path_if_exists=lambda *_args, **_kwargs: None,
            build_multimodal_review_pack=lambda *_args, **_kwargs: None,
            multimodal_review_pack_public_view=lambda *_args, **_kwargs: None,
            tuning_advice_payload=lambda *_args, **_kwargs: {},
            load_pending_proposal=lambda *_args, **_kwargs: None,
            apply_policy_fragment=lambda current, fragment: {
                **current,
                "video": {
                    **object_dict(current.get("video")),
                    **object_dict(fragment.get("video")),
                },
            },
            save_advice_state=lambda *_args, **_kwargs: None,
            save_job_state=lambda *_args, **_kwargs: None,
            clear_pending_proposal=lambda *_args, **_kwargs: None,
            record_tuning_session=lambda *_args, **_kwargs: "session-1",
        )

        result = folder_ai_tune_confirm_action(self.config, deps, "tv/show", "stale-proposal-id")

        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "This bench draft is out of date. Refresh it before queueing a sample.")

    def test_folder_ai_tune_confirm_retries_saved_sample_even_if_latest_job_is_full(self) -> None:
        host = HostStatus(
            key="cbusillo@localhost",
            label="M4 Studio",
            mode="ssh",
            priority=10,
            capabilities=["encode_queue", "sample_calibration"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )
        retryable_job = {
            "job_id": "sample-job-old",
            "status": "stopped",
            "lane": "sample",
            "mode": "sample",
            "action": "ai_tune",
            "prefix": "tv/show",
            "host": {"key": host.key},
            "notes": "Aim for 8%",
            "policy": {"video": {"target_vmaf": 93.5, "default_grain": 0}},
            "sample_item": {
                "rel_path": "tv/show/episode.mkv",
                "source_path": str(self.root / "source" / "tv" / "show" / "episode.mkv"),
                "source_size_bytes": 1234,
            },
            "created_at": "2026-04-11T15:18:09+00:00",
            "started_at": "2026-04-11T15:18:10+00:00",
            "finished_at": "2026-04-11T15:18:21+00:00",
            "updated_at": "2026-04-11T15:18:21+00:00",
            "error": "Calibration queue job was stopped and cleaned up.",
        }
        latest_full_job = {
            "job_id": "full-job-newer",
            "status": "stopped",
            "lane": "full",
            "mode": "full",
            "action": "full",
            "prefix": "tv/show",
            "host": {"key": host.key},
        }
        saved_jobs: list[dict[str, object]] = []

        deps = FolderAiTuneDeps(
            resolve_sample_host=lambda _config, host_key: host if host_key == host.key else None,
            load_job_state=lambda _connection, _config, prefix: dict(latest_full_job) if prefix == "tv/show" else None,
            load_retryable_sample_job_state=lambda _connection, _config, prefix: dict(retryable_job)
            if prefix == "tv/show"
            else None,
            sample_item=lambda *_args, **_kwargs: None,
            operator_requested_experiment=lambda *_args, **_kwargs: None,
            load_calibration_state=lambda *_args, **_kwargs: None,
            recent_tuning_sessions=lambda *_args, **_kwargs: [],
            matching_request_history=lambda *_args, **_kwargs: None,
            metric_support=lambda: {},
            maybe_seed_baseline_policy=lambda *_args, **_kwargs: None,
            seed_advice_payload=lambda *_args, **_kwargs: None,
            proposal_alignment_issue=lambda *_args, **_kwargs: None,
            now_iso=lambda: "2026-04-11T15:30:00+00:00",
            proposal_signal_copy=lambda *_args, **_kwargs: "",
            proposal_context_snapshot=lambda *_args, **_kwargs: {},
            save_pending_proposal=lambda *_args, **_kwargs: None,
            pending_proposal_public_view=lambda payload: payload,
            build_tuning_runtime_toolbelt=lambda *_args, **_kwargs: {},
            review_pack_dir=lambda *_args, **_kwargs: self.root / "review-pack",
            remove_path_if_exists=lambda *_args, **_kwargs: None,
            build_multimodal_review_pack=lambda *_args, **_kwargs: None,
            multimodal_review_pack_public_view=lambda *_args, **_kwargs: None,
            tuning_advice_payload=lambda *_args, **_kwargs: {},
            load_pending_proposal=lambda *_args, **_kwargs: None,
            apply_policy_fragment=lambda current, fragment: {
                **current,
                "video": {
                    **object_dict(current.get("video")),
                    **object_dict(fragment.get("video")),
                },
            },
            save_advice_state=lambda *_args, **_kwargs: None,
            save_job_state=lambda _connection, _config, _prefix, payload: saved_jobs.append(dict(payload)),
            clear_pending_proposal=lambda *_args, **_kwargs: None,
            record_tuning_session=lambda *_args, **_kwargs: "session-1",
        )

        result = folder_ai_tune_confirm_action(self.config, deps, "tv/show", "")

        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "Queued the saved sample draft again.")
        self.assertEqual(len(saved_jobs), 1)
        self.assertEqual(saved_jobs[0]["action"], "ai_tune")

    def test_build_review_compare_video_concatenates_all_compare_clips(self) -> None:
        review_run_dir = self.config.paths.review_dir / "sample-run"
        review_run_dir.mkdir(parents=True, exist_ok=True)
        compare_clip_1 = review_run_dir / "compare-01.mkv"
        compare_clip_2 = review_run_dir / "compare-02.mkv"
        compare_clip_3 = review_run_dir / "compare-03.mkv"
        compare_clip_1.write_bytes(b"compare1")
        compare_clip_2.write_bytes(b"compare2")
        compare_clip_3.write_bytes(b"compare3")

        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> object:
            commands.append(command)
            output_path = Path(command[-1])
            output_path.write_bytes(b"mov")
            return type("Result", (), {"returncode": 0, "stderr": ""})()

        with patch("mediaforce.web.app.subprocess.run", side_effect=fake_run), patch(
                "mediaforce.web.app.ffmpeg_binary", return_value="ffmpeg"
        ):
            bundle_path, download_name = _build_review_compare_video(
                config=self.config,
                prefix="tv/Suits/Season 5",
                compare_clips=[compare_clip_1, compare_clip_2, compare_clip_3],
            )
        self.addCleanup(bundle_path.unlink, missing_ok=True)

        self.assertEqual(download_name, "tv-suits-season-5-full-review-compare.mov")
        self.assertTrue(commands)
        self.assertEqual(
            commands[0][:18],
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(bundle_path.with_suffix('.txt')),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
            ],
        )
        concat_list = bundle_path.with_suffix('.txt')
        self.assertFalse(concat_list.exists())
        self.assertEqual(bundle_path.suffix, ".mov")
        self.assertEqual(bundle_path.read_bytes(), b"mov")

    def test_build_review_compare_video_from_pairs_renders_native_compare_clips(self) -> None:
        review_run_dir = self.config.paths.review_dir / "sample-run"
        review_run_dir.mkdir(parents=True, exist_ok=True)
        source_clip = review_run_dir / "source-01.mp4"
        preview_clip = review_run_dir / "encoded-01.mp4"
        wide_source_clip = review_run_dir / "source-02.mp4"
        wide_preview_clip = review_run_dir / "encoded-02.mp4"
        source_clip.write_bytes(b"source")
        preview_clip.write_bytes(b"preview")
        wide_source_clip.write_bytes(b"wide-source")
        wide_preview_clip.write_bytes(b"wide-preview")
        commands: list[list[str]] = []
        geometry_by_path = {
            source_clip: {"width": 1280, "height": 720},
            preview_clip: {"width": 1280, "height": 720},
            wide_source_clip: {"width": 3840, "height": 1606},
            wide_preview_clip: {"width": 3840, "height": 1606},
        }

        def fake_run(command: list[str], **_kwargs: object) -> object:
            commands.append(command)
            if command[0] == "ffprobe":
                geometry = geometry_by_path[Path(command[-1])]
                return type(
                    "Result",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"streams": [geometry]}), "stderr": ""},
                )()
            output_path = Path(command[-1])
            output_path.write_bytes(b"video")
            return type("Result", (), {"returncode": 0, "stderr": ""})()

        with patch("mediaforce.web.app.subprocess.run", side_effect=fake_run), patch(
                "mediaforce.web.app.ffmpeg_binary", return_value="ffmpeg"
        ):
            bundle_path, download_name, cleanup_path = _build_review_compare_video_from_pairs(
                config=self.config,
                prefix="tv/Suits/Season 5",
                retained_pairs=[(source_clip, preview_clip), (wide_source_clip, wide_preview_clip)],
            )
        self.addCleanup(lambda: shutil.rmtree(cleanup_path, ignore_errors=True))

        self.assertEqual(download_name, "tv-suits-season-5-full-review-compare.mov")
        self.assertEqual(bundle_path.parent, cleanup_path)
        self.assertEqual(len([command for command in commands if command[0] == "ffprobe"]), 4)
        render_commands = [command for command in commands if "-filter_complex" in command]
        self.assertEqual(len(render_commands), 2)
        render_command = render_commands[0]
        self.assertEqual(render_command[render_command.index("-i") + 1], str(source_clip))
        self.assertEqual(render_command[render_command.index("-i", render_command.index("-i") + 1) + 1], str(preview_clip))
        for command in render_commands:
            filter_complex = command[command.index("-filter_complex") + 1]
            self.assertIn("xstack=inputs=2", filter_complex)
            self.assertIn("pad=7680:1606", filter_complex)
            self.assertNotIn("scale=", filter_complex)
            self.assertNotIn("-ss", command)
        self.assertTrue(bundle_path.exists())

    def test_merge_review_pairs_preserves_chronological_order(self) -> None:
        merged_pairs = _merge_review_pairs(
            [
                {
                    "timestamp_seconds": 20.0,
                    "duration_seconds": 8.0,
                    "source_clip": {"path": "/review-media/run/source-20.mp4"},
                    "preview_clip": {"path": "/review-media/run/encoded-20.mp4"},
                    "compare_clip": None,
                }
            ],
            [
                {
                    "timestamp_seconds": 10.0,
                    "duration_seconds": 8.0,
                    "source_clip": {"path": "/review-media/run/source-10.mp4"},
                    "preview_clip": {"path": "/review-media/run/encoded-10.mp4"},
                    "compare_clip": {"path": "/review-media/run/compare-10.mkv"},
                },
                {
                    "timestamp_seconds": 20.0,
                    "duration_seconds": 8.0,
                    "source_clip": {"path": "/review-media/run/source-20.mp4"},
                    "preview_clip": {"path": "/review-media/run/encoded-20.mp4"},
                    "compare_clip": {"path": "/review-media/run/compare-20.mkv"},
                },
            ],
        )

        self.assertEqual([pair["timestamp_seconds"] for pair in merged_pairs], [10.0, 20.0])
        self.assertEqual(merged_pairs[1]["compare_clip"]["path"], "/review-media/run/compare-20.mkv")

    def test_review_compare_pair_entries_reads_retained_source_and_preview_clips(self) -> None:
        review_run_dir = self.config.paths.review_dir / "sample-run"
        review_run_dir.mkdir(parents=True, exist_ok=True)
        source_clip = review_run_dir / "source-01.mp4"
        preview_clip = review_run_dir / "encoded-01.mp4"
        source_clip.write_bytes(b"source")
        preview_clip.write_bytes(b"preview")
        stale_compare_clip = review_run_dir / "compare-01.mkv"
        stale_compare_clip.write_bytes(b"stale-low-resolution-compare")

        calibration = {
            "compare_clips": [{"path": "/review-media/sample-run/compare-01.mkv"}],
            "review_pairs": [
                {
                    "source_clip": {"path": "/review-media/sample-run/source-01.mp4"},
                    "preview_clip": {"path": "/review-media/sample-run/encoded-01.mp4"},
                    "compare_clip": {"path": "/review-media/sample-run/compare-01.mkv"},
                }
            ],
        }

        entries = _review_compare_pair_entries(self.config, calibration)

        self.assertEqual(entries, [(source_clip.resolve(), preview_clip.resolve())])

    def test_download_review_compare_prefers_full_size_retained_pairs(self) -> None:
        review_run_dir = self.config.paths.review_dir / "sample-run"
        review_run_dir.mkdir(parents=True, exist_ok=True)
        source_clip = review_run_dir / "source-01.mp4"
        preview_clip = review_run_dir / "encoded-01.mp4"
        stale_compare_clip = review_run_dir / "compare-01.mkv"
        source_clip.write_bytes(b"source")
        preview_clip.write_bytes(b"preview")
        stale_compare_clip.write_bytes(b"stale-low-resolution-compare")
        bundle_path = self.root / "full-size-review.mov"
        bundle_path.write_bytes(b"bundle")
        cleanup_path = self.root / "download-cleanup"

        calibration_path = _calibration_file(self.config, "tv/Suits/Season 5")
        calibration_path.parent.mkdir(parents=True, exist_ok=True)
        calibration_path.write_text(json.dumps({
            "review_pairs": [
                {
                    "source_clip": {"path": "/review-media/sample-run/source-01.mp4"},
                    "preview_clip": {"path": "/review-media/sample-run/encoded-01.mp4"},
                    "compare_clip": {"path": "/review-media/sample-run/compare-01.mkv"},
                }
            ],
            "compare_clips": [{"path": "/review-media/sample-run/compare-01.mkv"}],
        }))

        with patch(
                "mediaforce.web.app._build_review_compare_video_from_pairs",
                return_value=(bundle_path, "full-size.mov", cleanup_path),
        ) as pair_builder, patch(
            "mediaforce.web.app._build_review_compare_video",
            side_effect=AssertionError("download used stale resized compare clips"),
        ):
            response = _download_review_compare_action(self.config, "tv/Suits/Season 5")

        pair_builder.assert_called_once_with(
            config=self.config,
            prefix="tv/Suits/Season 5",
            retained_pairs=[(source_clip.resolve(), preview_clip.resolve())],
        )
        self.assertIn('filename="full-size.mov"', response.headers["content-disposition"])

    def test_download_review_compare_uses_existing_compare_set_when_retained_pairs_are_partial(self) -> None:
        review_run_dir = self.config.paths.review_dir / "sample-run"
        review_run_dir.mkdir(parents=True, exist_ok=True)
        source_clip = review_run_dir / "source-01.mp4"
        preview_clip = review_run_dir / "encoded-01.mp4"
        compare_clip_1 = review_run_dir / "compare-01.mkv"
        compare_clip_2 = review_run_dir / "compare-02.mkv"
        source_clip.write_bytes(b"source")
        preview_clip.write_bytes(b"preview")
        compare_clip_1.write_bytes(b"compare1")
        compare_clip_2.write_bytes(b"compare2")
        bundle_path = self.root / "full-existing-review.mov"
        bundle_path.write_bytes(b"bundle")

        calibration_path = _calibration_file(self.config, "tv/Suits/Season 5")
        calibration_path.parent.mkdir(parents=True, exist_ok=True)
        calibration_path.write_text(json.dumps({
            "review_pairs": [
                {
                    "source_clip": {"path": "/review-media/sample-run/source-01.mp4"},
                    "preview_clip": {"path": "/review-media/sample-run/encoded-01.mp4"},
                    "compare_clip": {"path": "/review-media/sample-run/compare-01.mkv"},
                }
            ],
            "compare_clips": [
                {"path": "/review-media/sample-run/compare-01.mkv"},
                {"path": "/review-media/sample-run/compare-02.mkv"},
            ],
        }))

        with patch(
                "mediaforce.web.app._build_review_compare_video_from_pairs",
                side_effect=AssertionError("partial retained pairs dropped compare moments"),
        ), patch(
            "mediaforce.web.app._build_review_compare_video",
            return_value=(bundle_path, "existing-full.mov"),
        ) as compare_builder:
            response = _download_review_compare_action(self.config, "tv/Suits/Season 5")

        compare_builder.assert_called_once_with(
            config=self.config,
            prefix="tv/Suits/Season 5",
            compare_clips=[compare_clip_1.resolve(), compare_clip_2.resolve()],
        )
        self.assertIn('filename="existing-full.mov"', response.headers["content-disposition"])

    def test_review_compare_bundle_entries_prefers_compare_clips_over_review_pairs(self) -> None:
        review_run_dir = self.config.paths.review_dir / "sample-run"
        review_run_dir.mkdir(parents=True, exist_ok=True)
        compare_clip_1 = review_run_dir / "compare-01.mkv"
        compare_clip_2 = review_run_dir / "compare-02.mkv"
        compare_clip_1.write_bytes(b"compare1")
        compare_clip_2.write_bytes(b"compare2")

        calibration = {
            "compare_clips": [
                {"path": "/review-media/sample-run/compare-01.mkv"},
                {"path": "/review-media/sample-run/compare-02.mkv"},
            ],
            "review_pairs": [
                {
                    "compare_clip": {"path": "/review-media/sample-run/compare-01.mkv"},
                }
            ],
        }

        entries = _review_compare_bundle_entries(self.config, calibration)

        self.assertEqual(entries, [compare_clip_1.resolve(), compare_clip_2.resolve()])

    def test_ffmpeg_concat_file_line_escapes_single_quotes(self) -> None:
        path = Path("/tmp/it's fine/compare clip.mkv")
        line = _ffmpeg_concat_file_line(path)

        self.assertEqual(line, "file '/tmp/it'\\''s fine/compare clip.mkv'\n")

    def test_request_note_tuning_uses_structured_runtime_path_and_self_check(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can make it smaller without obvious blockiness, so I trimmed the quality target a bit.",
                    "request_disposition": "honored",
                    "summary": "Lower XPSNR slightly to save more space.",
                    "diagnosis": "Current draft is conservative for clean 1080p TV.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.recent_sample_result", "retrieved_memory[0]"],
                    "suggested_follow_up": None,
                    "feasibility_note": None,
                    "policy": {"video": {"target_xpsnr": 34.5, "max_encoded_percent": 50}},
                }
            ),
            json.dumps(
                {
                    "status": "pass",
                    "summary": "The proposal is consistent with the operator request and bounded evidence.",
                    "issues": [],
                }
            ),
        ]
        commands: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(kwargs, dict)
            commands.append(cmd)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-5",
                    "operator_note": "Try slightly smaller without obvious blockiness.",
                    "runtime_toolbelt": {"recent_sample_result": {"quality_score": 95.0}},
                    "retrieved_memory": [{"title": "Prior Suits note"}],
                },
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.toolbelt_used, ["recent_sample_result"])
        assert response.self_check is not None
        assert response.proposed_policy is not None
        self.assertEqual(response.self_check["status"], "pass")
        self.assertEqual(response.proposed_policy["video"]["target_xpsnr"], 34.5)
        self.assertEqual(response.request_disposition, "honored")
        self.assertIn("smaller", response.request_response)
        self._assert_structured_subprocess_call(commands)

    def test_archive_cleanup_summary_counts_files_and_size(self) -> None:
        archive_root = self.config.archive_root
        first = archive_root / "tv/show/episode-1.mkv"
        second = archive_root / "movies/demo.mp4"
        first.parent.mkdir(parents=True, exist_ok=True)
        second.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"abc")
        second.write_bytes(b"12345")

        summary = archive_cleanup_summary(self.config)

        self.assertTrue(summary["has_cleanup"])
        self.assertEqual(summary["file_count"], 2)
        self.assertEqual(summary["total_size_bytes"], 8)

    def test_dashboard_summary_payload_does_not_scan_archive_cleanup(self) -> None:
        payload = dashboard_summary_payload(
            self.config,
            folder_card_cache_key=lambda _config: ("empty", 0, 0),
            preview_folder_cards=lambda _config, _connection: [],
            maybe_schedule_scan=lambda _connection, _config, prefix=None: None,
            decorate_encode_queue_for_scheduler=lambda _config, summary: summary,
            library_color_map_for_config=lambda _config: {},
        )

        self.assertNotIn("archive_cleanup", payload)

    def test_dashboard_summary_payload_preview_limit_zero_skips_folder_cards(self) -> None:
        self._insert_library_item(rel_path="tv/show/episode-1.mkv")

        def fail_preview_cards(_config: MediaforceConfig, _connection: Any) -> list[Any]:
            raise AssertionError("folder cards should not be built for preview_limit=0")

        payload = dashboard_summary_payload(
            self.config,
            folder_card_cache_key=lambda _config: ("one-item", 0, 0),
            preview_folder_cards=fail_preview_cards,
            maybe_schedule_scan=lambda _connection, _config, prefix=None: None,
            decorate_encode_queue_for_scheduler=lambda _config, summary: summary,
            library_color_map_for_config=lambda _config: {},
            preview_limit=0,
        )

        self.assertEqual(payload["folders_preview"], [])
        self.assertFalse(payload["catalog_empty"])

    def test_dashboard_summary_payload_rejects_negative_preview_limit(self) -> None:
        with self.assertRaises(ValueError):
            dashboard_summary_payload(
                self.config,
                folder_card_cache_key=lambda _config: ("empty", 0, 0),
                preview_folder_cards=lambda _config, _connection: [],
                maybe_schedule_scan=lambda _connection, _config, prefix=None: None,
                decorate_encode_queue_for_scheduler=lambda _config, summary: summary,
                library_color_map_for_config=lambda _config: {},
                preview_limit=-1,
            )

    def test_clear_archive_cleanup_action_removes_files_and_prunes_directories(self) -> None:
        archive_root = self.config.archive_root
        archived = archive_root / "tv/show/episode-1.mkv"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_text("backup")

        result = clear_archive_cleanup_action(self.config)

        self.assertTrue(result["ok"])
        self.assertEqual(result["removed_count"], 1)
        self.assertFalse(archived.exists())
        self.assertFalse((archive_root / "tv/show").exists())
        self.assertTrue(archive_root.exists())
        self.assertEqual(result["archive_cleanup"]["file_count"], 0)

    def test_clear_archive_cleanup_action_tolerates_missing_archive_root(self) -> None:
        config = MediaforceConfig(
            raw={
                "state": {},
                "media": {
                    "source_roots": {"tv": str(self.root / "source" / "tv")},
                    "staging_root": str(self.root / "staging"),
                },
                "remote_hosts": [],
            },
            paths=self.config.paths,
        )

        result = clear_archive_cleanup_action(config)

        self.assertTrue(result["ok"])
        self.assertEqual(result["removed_count"], 0)
        self.assertEqual(result["removed_size_bytes"], 0)
        self.assertEqual(result["archive_cleanup"]["archive_root"], "")
        self.assertFalse(result["archive_cleanup"]["has_cleanup"])

    def test_completed_page_payload_groups_promoted_folders_and_active_backups(self) -> None:
        from mediaforce.web import app as web_app

        self._insert_promoted_artifact(
            rel_path="tv/Example Show/Season 1/Episode 01.mkv",
            promoted_at="2026-04-10T10:00:00+00:00",
            archived_size_bytes=3,
            bytes_saved=12,
        )
        self._insert_promoted_artifact(
            rel_path="tv/Example Show/Season 1/Episode 02.mkv",
            promoted_at="2026-04-11T10:00:00+00:00",
            archived_size_bytes=5,
            bytes_saved=18,
        )
        self._insert_promoted_artifact(
            rel_path="tv/Example Show/Season 2/Episode 01.mkv",
            promoted_at="2026-04-12T10:00:00+00:00",
            archived_size_bytes=7,
            bytes_saved=20,
            archive_exists=False,
        )

        with open_db(self.config.paths.db_path) as connection:
            payload = completed_page_payload(self.config, connection, folder_group=web_app._folder_group)

        self.assertEqual(payload["completed_count"], 2)
        self.assertEqual(payload["folders_with_backups_count"], 1)
        self.assertEqual(payload["archive_cleanup"]["file_count"], 2)
        first_folder = payload["folders"][0]
        self.assertEqual(first_folder["prefix"], "tv/Example Show/Season 1")
        self.assertEqual(first_folder["promoted_item_count"], 2)
        self.assertEqual(first_folder["archived_backup_count"], 2)
        self.assertEqual(first_folder["archived_backup_size_bytes"], 8)
        self.assertEqual(first_folder["total_bytes_saved"], 30)
        self.assertEqual(first_folder["cleanup_state"], "ready")
        second_folder = payload["folders"][1]
        self.assertEqual(second_folder["prefix"], "tv/Example Show/Season 2")
        self.assertEqual(second_folder["archived_backup_count"], 0)
        self.assertEqual(second_folder["cleanup_state"], "unknown")
        self.assertEqual(second_folder["missing_backup_count"], 1)

    def test_completed_page_payload_uses_folder_backup_counts_for_archive_summary(self) -> None:
        from mediaforce.web import app as web_app

        self._insert_promoted_artifact(
            rel_path="tv/Example Show/Season 1/Episode 01.mkv",
            promoted_at="2026-04-10T10:00:00+00:00",
            archived_size_bytes=3,
            bytes_saved=12,
        )
        unrelated_archive = self.config.archive_root / "unrelated" / "orphan.mkv"
        unrelated_archive.parent.mkdir(parents=True, exist_ok=True)
        unrelated_archive.write_text("not part of promoted state")

        with open_db(self.config.paths.db_path) as connection:
            payload = completed_page_payload(self.config, connection, folder_group=web_app._folder_group)

        self.assertEqual(payload["archive_cleanup"]["file_count"], 1)
        self.assertEqual(payload["archive_cleanup"]["total_size_bytes"], 3)
        self.assertTrue(payload["archive_cleanup"]["has_cleanup"])

    def test_completed_page_payload_does_not_scan_archive_cleanup(self) -> None:
        from mediaforce.web import app as web_app

        self._insert_promoted_artifact(
            rel_path="tv/Example Show/Season 1/Episode 01.mkv",
            promoted_at="2026-04-10T10:00:00+00:00",
            archived_size_bytes=3,
            bytes_saved=12,
        )
        with patch("mediaforce.web.runtime.archive_cleanup.Path.rglob") as rglob:
            with open_db(self.config.paths.db_path) as connection:
                payload = completed_page_payload(self.config, connection, folder_group=web_app._folder_group)

        rglob.assert_not_called()
        self.assertEqual(payload["archive_cleanup"]["file_count"], 1)

    def test_completed_page_payload_ignores_archived_paths_outside_active_archive_root(self) -> None:
        from mediaforce.web import app as web_app

        self._insert_promoted_artifact(
            rel_path="tv/Example Show/Season 1/Episode 01.mkv",
            promoted_at="2026-04-10T10:00:00+00:00",
            archived_size_bytes=3,
            bytes_saved=12,
            archive_exists=False,
        )
        outside_archive = self.root / "outside-archive" / "tv" / "Example Show" / "Season 1" / "Episode 02.mkv"
        outside_archive.parent.mkdir(parents=True, exist_ok=True)
        outside_archive.write_text("outside")
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                staged_artifacts.update()
                .values(archived_source_path=str(outside_archive))
                .where(staged_artifacts.c.library_item_id == 1)
            )
            connection.commit()
            payload = completed_page_payload(self.config, connection, folder_group=web_app._folder_group)

        self.assertEqual(payload["archive_cleanup"]["file_count"], 0)
        self.assertEqual(payload["folders"][0]["archived_backup_count"], 0)
        self.assertEqual(payload["folders"][0]["cleanup_state"], "blocked")
        self.assertEqual(payload["folders"][0]["outside_archive_root_count"], 1)

    def test_completed_page_payload_includes_recent_history_events(self) -> None:
        from mediaforce.web import app as web_app

        self._insert_promoted_artifact(
            rel_path="tv/Example Show/Season 1/Episode 01.mkv",
            promoted_at="2026-04-10T10:00:00+00:00",
            archived_size_bytes=3,
            bytes_saved=12,
        )
        with open_db(self.config.paths.db_path) as connection:
            library_item_id = connection.execute(select(library_items.c.id)).scalar_one()
            connection.execute(
                item_events.insert().values(
                    library_item_id=library_item_id,
                    created_at="2026-04-10T10:01:00+00:00",
                    event_type="promotion_completed",
                    details_json=json.dumps({"promoted_path": "/media/tv/Example Show/Episode 01.mkv"}),
                )
            )
            connection.commit()
            payload = completed_page_payload(self.config, connection, folder_group=web_app._folder_group)

        self.assertEqual(payload["history"][0]["event_type"], "promotion_completed")
        self.assertEqual(payload["history"][0]["prefix"], "tv/Example Show/Season 1")
        self.assertEqual(payload["history"][0]["tone"], "ready")

    def test_completed_page_payload_truncates_large_failed_history_details(self) -> None:
        from mediaforce.web import app as web_app

        self._insert_promoted_artifact(
            rel_path="tv/Example Show/Season 1/Episode 01.mkv",
            promoted_at="2026-04-10T10:00:00+00:00",
            archived_size_bytes=3,
            bytes_saved=12,
        )
        with open_db(self.config.paths.db_path) as connection:
            library_item_id = connection.execute(select(library_items.c.id)).scalar_one()
            connection.execute(
                item_events.insert().values(
                    library_item_id=library_item_id,
                    created_at="2026-04-10T10:01:00+00:00",
                    event_type="encoding_failed",
                    details_json=json.dumps({"error": "x" * 1200}),
                )
            )
            connection.commit()
            payload = completed_page_payload(self.config, connection, folder_group=web_app._folder_group)

        self.assertLessEqual(len(payload["history"][0]["detail"]), 380)
        self.assertIn("characters omitted", payload["history"][0]["detail"])

    def test_completed_page_payload_tolerates_missing_archive_root(self) -> None:
        from mediaforce.web import app as web_app

        config = MediaforceConfig(
            raw={
                "state": {},
                "media": {
                    "source_roots": {"tv": str(self.root / "source" / "tv")},
                    "staging_root": str(self.root / "staging"),
                },
                "remote_hosts": [],
            },
            paths=self.config.paths,
        )

        with open_db(config.paths.db_path) as connection:
            payload = completed_page_payload(config, connection, folder_group=web_app._folder_group)

        self.assertEqual(payload["completed_count"], 0)
        self.assertEqual(payload["folders_with_backups_count"], 0)
        self.assertEqual(payload["archive_cleanup"]["archive_root"], "")
        self.assertFalse(payload["archive_cleanup"]["has_cleanup"])

    def test_completed_page_payload_keeps_total_bytes_saved_after_backup_is_deleted(self) -> None:
        from mediaforce.web import app as web_app

        self._insert_promoted_artifact(
            rel_path="tv/Example Show/Season 1/Episode 01.mkv",
            promoted_at="2026-04-10T10:00:00+00:00",
            archived_size_bytes=3,
            bytes_saved=12,
        )
        archived_path = self.config.archive_root / "tv/Example Show/Season 1/Episode 01.mkv"
        archived_path.unlink()

        with open_db(self.config.paths.db_path) as connection:
            payload = completed_page_payload(self.config, connection, folder_group=web_app._folder_group)

        self.assertEqual(payload["folders"][0]["archived_backup_count"], 0)
        self.assertEqual(payload["folders"][0]["archived_backup_size_bytes"], 0)
        self.assertEqual(payload["folders"][0]["total_bytes_saved"], 12)

    def test_confirm_originals_removed_marks_missing_backups_cleaned(self) -> None:
        from mediaforce.web import app as web_app

        self._insert_promoted_artifact(
            rel_path="tv/Example Show/Season 1/Episode 01.mkv",
            promoted_at="2026-04-10T10:00:00+00:00",
            archived_size_bytes=3,
            bytes_saved=12,
        )
        archived_path = self.config.archive_root / "tv/Example Show/Season 1/Episode 01.mkv"
        archived_path.unlink()

        with open_db(self.config.paths.db_path) as connection:
            before = completed_page_payload(self.config, connection, folder_group=web_app._folder_group)
            self.assertEqual(before["folders"][0]["cleanup_state"], "unknown")

            result = confirm_originals_removed_action(
                connection,
                folder_group=web_app._folder_group,
                archive_root=self.config.archive_root,
                prefixes=["tv/Example Show/Season 1"],
                valid_prefixes={"tv/Example Show/Season 1"},
            )
            after = completed_page_payload(self.config, connection, folder_group=web_app._folder_group)
            event_count = connection.execute(
                select(func.count())
                .select_from(item_events)
                .where(item_events.c.event_type == ORIGINALS_REMOVED_EVENT)
            ).scalar_one()

        self.assertTrue(result["ok"])
        self.assertEqual(result["resolved_count"], 1)
        self.assertIn("No files were deleted", result["message"])
        self.assertEqual(after["folders"][0]["cleanup_state"], "cleaned")
        self.assertEqual(after["folders"][0]["missing_backup_count"], 0)
        self.assertEqual(after["folders"][0]["originals_removed_count"], 1)
        self.assertEqual(event_count, 1)

    def test_confirm_originals_removed_leaves_existing_backups_ready(self) -> None:
        from mediaforce.web import app as web_app

        self._insert_promoted_artifact(
            rel_path="tv/Example Show/Season 1/Episode 01.mkv",
            promoted_at="2026-04-10T10:00:00+00:00",
            archived_size_bytes=3,
            bytes_saved=12,
        )

        with open_db(self.config.paths.db_path) as connection:
            result = confirm_originals_removed_action(
                connection,
                folder_group=web_app._folder_group,
                archive_root=self.config.archive_root,
                prefixes=["tv/Example Show/Season 1"],
                valid_prefixes={"tv/Example Show/Season 1"},
            )
            payload = completed_page_payload(self.config, connection, folder_group=web_app._folder_group)
            event_count = connection.execute(
                select(func.count())
                .select_from(item_events)
                .where(item_events.c.event_type == ORIGINALS_REMOVED_EVENT)
            ).scalar_one()

        self.assertEqual(result["resolved_count"], 0)
        self.assertEqual(payload["folders"][0]["cleanup_state"], "ready")
        self.assertEqual(payload["folders"][0]["archived_backup_count"], 1)
        self.assertEqual(event_count, 0)

    def test_clear_completed_backups_action_removes_selected_prefixes(self) -> None:
        from mediaforce.web import app as web_app

        season_one_backup = self.config.archive_root / "tv/Example Show/Season 1/Episode 01.mkv"
        season_two_backup = self.config.archive_root / "tv/Example Show/Season 2/Episode 01.mkv"
        self._insert_promoted_artifact(
            rel_path="tv/Example Show/Season 1/Episode 01.mkv",
            promoted_at="2026-04-10T10:00:00+00:00",
            archived_size_bytes=3,
            bytes_saved=12,
        )
        self._insert_promoted_artifact(
            rel_path="tv/Example Show/Season 2/Episode 01.mkv",
            promoted_at="2026-04-11T10:00:00+00:00",
            archived_size_bytes=5,
            bytes_saved=18,
        )

        result = clear_completed_backups_action(
            self.config,
            folder_group=web_app._folder_group,
            prefixes=["tv/Example Show/Season 1"],
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(result["removed_prefix_count"], 1)
        self.assertFalse(season_one_backup.exists())
        self.assertTrue(season_two_backup.exists())

    def test_clear_completed_backups_action_rejects_unknown_prefixes(self) -> None:
        from mediaforce.web import app as web_app

        self._insert_promoted_artifact(
            rel_path="tv/Example Show/Season 1/Episode 01.mkv",
            promoted_at="2026-04-10T10:00:00+00:00",
            archived_size_bytes=3,
            bytes_saved=12,
        )

        with self.assertRaisesRegex(ValueError, "Unknown completed-folder prefix"):
            clear_completed_backups_action(
                self.config,
                folder_group=web_app._folder_group,
                prefixes=["tv"],
                valid_prefixes={"tv/Example Show/Season 1"},
            )

    def test_completed_route_returns_payload(self) -> None:
        from mediaforce.web import app as web_app

        expected_payload = {"folders": [], "completed_count": 0, "folders_with_backups_count": 0, "archive_cleanup": {}}

        with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ), patch("mediaforce.web.app._refresh_host_status_cache", return_value=[]), patch(
            "mediaforce.web.app.completed_page_payload",
            return_value=expected_payload,
        ):
            app = web_app.create_app(self.root / "config.toml")
            route = next(
                route
                for route in app.routes
                if isinstance(route, APIRoute) and route.path == "/api/completed"
            )
            assert isinstance(route, APIRoute)
            response = route.endpoint()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), expected_payload)

    def test_app_proposal_alignment_wrapper_accepts_quality_increase_flag(self) -> None:
        from mediaforce.web import app as web_app

        captured: list[dict[str, object]] = []

        def fake_proposal_alignment_issue(**kwargs: object) -> None:
            captured.append(dict(kwargs))
            return None

        def fake_preview_action(_config: MediaforceConfig, deps: FolderAiTuneDeps, *_args: object) -> dict[str, object]:
            deps.proposal_alignment_issue(
                operator_request={"request_type": "size_budget"},
                request_disposition="honored",
                current_policy={"video": {"target_vmaf": 92.0}},
                preview_policy={"video": {"target_vmaf": 95.0}},
                allow_measured_size_quality_increase=True,
            )
            return {"ok": True}

        with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ), patch("mediaforce.web.app._refresh_host_status_cache", return_value=[]), patch(
            "mediaforce.web.app.proposal_alignment_issue",
            side_effect=fake_proposal_alignment_issue,
        ), patch(
            "mediaforce.web.app.folder_ai_tune_preview_action",
            side_effect=fake_preview_action,
        ):
            app = web_app.create_app(self.root / "config.toml")
            preview_route = next(
                route
                for route in app.routes
                if isinstance(route, APIRoute) and route.path == "/api/folders/{prefix:path}/ai-tune/preview"
            )

            class _FakeRequest:
                async def json(self) -> dict[str, str]:
                    return {"note": "raise quality", "host_key": "host-1"}

            response = asyncio.run(preview_route.endpoint("tv/show", _FakeRequest()))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(captured[0]["allow_measured_size_quality_increase"])

    def test_completed_cleanup_route_passes_selected_prefixes(self) -> None:
        from mediaforce.web import app as web_app

        captured: list[list[str] | None] = []

        def fake_clear_completed_backups_action(
                _config: MediaforceConfig,
                *,
                folder_group: object,
                prefixes: list[str] | None = None,
                valid_prefixes: set[str] | None = None,
        ) -> dict[str, object]:
            _ = folder_group, valid_prefixes
            captured.append(prefixes)
            return {
                "ok": True,
                "message": "done",
                "removed_count": 1,
                "removed_size_bytes": 3,
                "removed_prefix_count": 1,
            }

        expected_payload = {"folders": [], "completed_count": 0, "folders_with_backups_count": 0, "archive_cleanup": {}}

        with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ), patch("mediaforce.web.app._refresh_host_status_cache", return_value=[]), patch(
            "mediaforce.web.app.clear_completed_backups_action",
            side_effect=fake_clear_completed_backups_action,
        ), patch("mediaforce.web.app.completed_page_payload", return_value=expected_payload):
            app = web_app.create_app(self.root / "config.toml")
            route = next(
                route
                for route in app.routes
                if isinstance(route, APIRoute) and route.path == "/api/completed/backups/clear"
            )
            assert isinstance(route, APIRoute)

            class _FakeRequest:
                def __init__(self, payload: dict[str, object]) -> None:
                    self._payload = payload

                async def json(self) -> dict[str, object]:
                    return self._payload

            response = asyncio.run(route.endpoint(_FakeRequest({"prefixes": ["tv/Example Show/Season 1"]})))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured, [["tv/Example Show/Season 1"]])
        self.assertEqual(json.loads(response.body)["completed"], expected_payload)

    def test_completed_cleanup_route_tolerates_missing_archive_root(self) -> None:
        from mediaforce.web import app as web_app

        config = MediaforceConfig(
            raw={
                "state": {},
                "media": {
                    "source_roots": {"tv": str(self.root / "source" / "tv")},
                    "staging_root": str(self.root / "staging"),
                },
                "remote_hosts": [],
            },
            paths=self.config.paths,
        )

        expected_payload = {
            "folders": [],
            "completed_count": 0,
            "folders_with_backups_count": 0,
            "archive_cleanup": {"archive_root": "", "file_count": 0, "total_size_bytes": 0, "has_cleanup": False},
        }

        with patch("mediaforce.web.app.load_config", return_value=config), patch(
            "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ), patch("mediaforce.web.app._refresh_host_status_cache", return_value=[]), patch(
            "mediaforce.web.app.completed_page_payload", return_value=expected_payload
        ), patch("mediaforce.web.app.list_completed_folders", return_value=[]), patch(
            "mediaforce.web.app.clear_completed_backups_action",
            return_value={
                "ok": True,
                "message": "No archived originals are waiting for cleanup.",
                "removed_count": 0,
                "removed_size_bytes": 0,
                "removed_prefix_count": 0,
            },
        ) as clear_mock:
            app = web_app.create_app(self.root / "config.toml")
            route = next(
                route
                for route in app.routes
                if isinstance(route, APIRoute) and route.path == "/api/completed/backups/clear"
            )
            assert isinstance(route, APIRoute)

            class _FakeRequest:
                def __init__(self, payload: dict[str, object]) -> None:
                    self._payload = payload

                async def json(self) -> dict[str, object]:
                    return self._payload

            response = asyncio.run(route.endpoint(_FakeRequest({})))

        self.assertEqual(response.status_code, 200)
        clear_mock.assert_called_once()
        self.assertEqual(json.loads(response.body)["completed"], expected_payload)

    def test_completed_cleanup_route_rejects_malformed_prefix_payload(self) -> None:
        from mediaforce.web import app as web_app

        with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ), patch("mediaforce.web.app._refresh_host_status_cache", return_value=[]):
            app = web_app.create_app(self.root / "config.toml")
            route = next(
                route
                for route in app.routes
                if isinstance(route, APIRoute) and route.path == "/api/completed/backups/clear"
            )
            assert isinstance(route, APIRoute)

            class _FakeRequest:
                def __init__(self, payload: object) -> None:
                    self._payload = payload

                async def json(self) -> object:
                    return self._payload

            response = asyncio.run(route.endpoint(_FakeRequest({"prefixes": "tv/Example Show/Season 1"})))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            json.loads(response.body),
            {"ok": False, "message": "Completed-folder prefixes must be provided as a list."},
        )

    def test_completed_cleanup_route_rejects_unknown_selected_prefix(self) -> None:
        from mediaforce.web import app as web_app

        with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ), patch("mediaforce.web.app._refresh_host_status_cache", return_value=[]), patch(
            "mediaforce.web.app.list_completed_folders",
            return_value=[],
        ):
            app = web_app.create_app(self.root / "config.toml")
            route = next(
                route
                for route in app.routes
                if isinstance(route, APIRoute) and route.path == "/api/completed/backups/clear"
            )
            assert isinstance(route, APIRoute)

            class _FakeRequest:
                def __init__(self, payload: dict[str, object]) -> None:
                    self._payload = payload

                async def json(self) -> dict[str, object]:
                    return self._payload

            response = asyncio.run(route.endpoint(_FakeRequest({"prefixes": ["tv"]})))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            json.loads(response.body),
            {
                "ok": False,
                "message": "Completed cleanup request could not be processed. Review the selected folders and try again.",
            },
        )

    def test_completed_cleanup_route_rejects_malformed_json(self) -> None:
        from mediaforce.web import app as web_app

        with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ), patch("mediaforce.web.app._refresh_host_status_cache", return_value=[]):
            app = web_app.create_app(self.root / "config.toml")
            route = next(
                route
                for route in app.routes
                if isinstance(route, APIRoute) and route.path == "/api/completed/backups/clear"
            )
            assert isinstance(route, APIRoute)

            class _FakeRequest:
                async def json(self) -> object:
                    raise json.JSONDecodeError("bad", "{", 0)

            response = asyncio.run(route.endpoint(_FakeRequest()))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            json.loads(response.body),
            {"ok": False, "message": "Completed cleanup requests must be valid JSON."},
        )

    def test_archive_cleanup_route_passes_transcode_root_to_action(self) -> None:
        from mediaforce.web import app as web_app

        captured: list[str | None] = []

        def fake_clear_archive_cleanup_action(
                _config: MediaforceConfig,
                *,
                transcode_root: str | None = None,
        ) -> dict[str, object]:
            captured.append(transcode_root)
            return {"ok": True, "transcode_root": transcode_root}

        with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ), patch("mediaforce.web.app._refresh_host_status_cache", return_value=[]), patch(
            "mediaforce.web.app.clear_archive_cleanup_action",
            side_effect=fake_clear_archive_cleanup_action,
        ):
            app = web_app.create_app(self.root / "config.toml")
            route = next(
                route
                for route in app.routes
                if isinstance(route, APIRoute) and route.path == "/api/archive-cleanup/clear"
            )
            assert isinstance(route, APIRoute)

            class _FakeRequest:
                def __init__(self, payload: dict[str, str]) -> None:
                    self._payload = payload

                async def json(self) -> dict[str, str]:
                    return self._payload

            response = asyncio.run(route.endpoint(_FakeRequest({"transcode_root": "/Volumes/media/transcode-alt"})))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.body),
            {"ok": True, "transcode_root": "/Volumes/media/transcode-alt"},
        )
        self.assertEqual(captured, ["/Volumes/media/transcode-alt"])

    def test_archive_cleanup_summary_route_passes_transcode_root(self) -> None:
        from mediaforce.web import app as web_app

        captured: list[str | None] = []

        def fake_archive_cleanup_summary(
                _config: MediaforceConfig,
                *,
                transcode_root: str | None = None,
        ) -> dict[str, object]:
            captured.append(transcode_root)
            return {
                "archive_root": "/Volumes/media/transcode-alt/_replaced",
                "file_count": 3,
                "total_size_bytes": 42,
                "has_cleanup": True,
            }

        with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ), patch("mediaforce.web.app._refresh_host_status_cache", return_value=[]), patch(
            "mediaforce.web.app.archive_cleanup_summary",
            side_effect=fake_archive_cleanup_summary,
        ):
            app = web_app.create_app(self.root / "config.toml")
            route = next(
                route
                for route in app.routes
                if isinstance(route, APIRoute) and route.path == "/api/archive-cleanup"
            )
            assert isinstance(route, APIRoute)

            response = route.endpoint("/Volumes/media/transcode-alt")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.body),
            {
                "archive_root": "/Volumes/media/transcode-alt/_replaced",
                "file_count": 3,
                "total_size_bytes": 42,
                "has_cleanup": True,
            },
        )
        self.assertEqual(captured, ["/Volumes/media/transcode-alt"])

    def test_settings_route_can_skip_archive_cleanup_summary(self) -> None:
        from mediaforce.web import app as web_app

        archive_cleanup_calls = 0

        def fake_archive_cleanup_summary(
                _config: MediaforceConfig,
                *,
                transcode_root: str | None = None,
        ) -> dict[str, object]:
            nonlocal archive_cleanup_calls
            archive_cleanup_calls += 1
            return {
                "archive_root": str((Path(transcode_root) / "_replaced") if transcode_root else self.config.archive_root),
                "file_count": 0,
                "total_size_bytes": 0,
                "has_cleanup": False,
            }

        with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ), patch("mediaforce.web.app._refresh_host_status_cache", return_value=[]), patch(
            "mediaforce.web.app.archive_cleanup_summary",
            side_effect=fake_archive_cleanup_summary,
        ):
            app = web_app.create_app(self.root / "config.toml")
            route = next(
                route for route in app.routes if isinstance(route, APIRoute) and route.path == "/api/settings"
            )
            assert isinstance(route, APIRoute)

            response = route.endpoint(include_archive_cleanup=0)

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertIsNone(body["archive_cleanup"])
        self.assertEqual(archive_cleanup_calls, 0)

    def test_run_periodic_cleanup_starts_background_thread_without_blocking(self) -> None:
        started = threading.Event()
        release = threading.Event()
        cleanup_lock = threading.Lock()

        def fake_purge(_config: MediaforceConfig) -> None:
            started.set()
            self.assertTrue(release.wait(timeout=1.0))

        with patch("mediaforce.web.app.purge_transient_artifacts", side_effect=fake_purge):
            started_at = time.monotonic()
            _run_periodic_cleanup(self.config, cleanup_lock)
            elapsed = time.monotonic() - started_at

            self.assertLess(elapsed, 0.2)
            self.assertTrue(started.wait(timeout=1.0))
            self.assertTrue(cleanup_lock.locked())

            release.set()
            deadline = time.monotonic() + 1.0
            while cleanup_lock.locked() and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(cleanup_lock.locked())

    def test_request_note_tuning_uses_multimodal_exec_when_review_pack_present(self) -> None:
        image_path = self.root / "review-pack.png"
        image_path.write_bytes(b"png")
        commands: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(kwargs, dict)
            commands.append(cmd)
            if "exec" in cmd:
                response_body = json.dumps(
                    {
                        "request_response": "The attached review pack still looks clean enough to push a bit smaller.",
                        "request_disposition": "honored_with_risk",
                        "summary": "Use the review pack to justify one smaller pass.",
                        "diagnosis": "The attached source-versus-draft contact sheets do not show obvious new damage on the sampled moments.",
                        "confidence": "medium",
                        "evidence_checked": ["multimodal_review_pack.artifacts[0]",
                                             "runtime_toolbelt.review_media_context"],
                        "suggested_follow_up": "If the next draft softens faces or dark scenes, stop there.",
                        "feasibility_note": None,
                        "policy": {"video": {"target_vmaf": 88.5, "max_crf": 41}},
                    }
                )
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(response_body), "stderr": ""})()
            return type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "status": "pass",
                            "summary": "The proposal is consistent with the multimodal evidence.",
                            "issues": [],
                        }
                    ),
                    "stderr": "",
                },
            )()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/house/season-2",
                    "operator_note": "It still looks good in the review pack. Can we push a little more?",
                    "runtime_toolbelt": {
                        "review_media_context": {"review_media_ready": True, "moment_count": 2}
                    },
                    "multimodal_review_pack": {
                        "artifacts": [{"label": "Moment 1", "detail": "Top source, bottom draft"}],
                        "images": [str(image_path)],
                    },
                },
            )

        self.assertTrue(response.ok)
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["video"]["target_vmaf"], 88.5)
        self.assertTrue(commands)
        self.assertIn("exec", commands[0])
        self.assertIn("--image", commands[0])
        self._assert_structured_subprocess_call(commands)

    def test_request_review_artifact_critique_uses_multimodal_exec(self) -> None:
        image_path = self.root / "review-pack.png"
        image_path.write_bytes(b"png")
        commands: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(kwargs, dict)
            commands.append(cmd)
            response_body = json.dumps(
                {
                    "summary": "The hallway moment looks like the most fragile visual tradeoff.",
                    "confidence": "medium",
                    "weakest_moments": ["Moment 2 looks softer in dark material."],
                    "preserved_strengths": ["Dialogue close-ups still look stable."],
                    "artifacts_to_recheck": ["Compare timeline for review moment 2"],
                    "recommendation": "Treat dark material as the veto moment for the next draft.",
                    "evidence_checked": ["multimodal_review_pack.artifacts[0]"],
                }
            )
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(response_body), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_review_artifact_critique(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "operator_note": "Does the hallway still hold up?",
                    "multimodal_review_pack": {
                        "artifacts": [{"label": "Moment 2", "detail": "Source on left, draft on right"}],
                        "images": [str(image_path)],
                    },
                },
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.prompt_version, REVIEW_ARTIFACT_CRITIQUE_PROMPT_VERSION)
        self.assertEqual(response.weakest_moments, ["Moment 2 looks softer in dark material."])
        self.assertTrue(commands)
        self.assertIn("exec", commands[0])
        self.assertIn("--image", commands[0])

    def test_request_note_tuning_blocks_failed_self_check(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I do not have enough evidence to justify that much extra grain.",
                    "request_disposition": "rejected",
                    "summary": "Crank grain up.",
                    "diagnosis": "Trying to protect quality.",
                    "confidence": "low",
                    "evidence_checked": ["runtime_toolbelt.current_policy_focus"],
                    "suggested_follow_up": None,
                    "feasibility_note": None,
                    "policy": {"video": {"default_grain": 20}},
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run), patch(
            "mediaforce.advisor._run_tune_self_check",
            return_value={
                "status": "fail",
                "summary": "The proposal is too aggressive for the stated goal.",
                "issues": ["Default grain increase is not supported by the supplied evidence."],
                "source": "deterministic",
            },
        ):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-5",
                    "operator_note": "Go smaller, but keep it clean.",
                    "runtime_toolbelt": {"current_policy_focus": {"video": {"default_grain": 0}}},
                    "retrieved_memory": [],
                },
            )

        self.assertFalse(response.ok)
        self.assertIsNone(response.proposed_policy)
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "fail")

    def test_request_note_tuning_preserves_low_leverage_surround_audio_when_self_check_flags_it(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and surround audio.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try the lower surround tier again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"surround_5_1_opus_bitrate": "192k"},
                    },
                }
            ),
            json.dumps(
                {
                    "status": "warn",
                    "summary": "The draft is mostly usable, but the surround audio cut is not the best lever here.",
                    "issues": [],
                    "surround_audio_guardrail": {
                        "status": "prefer_preserve_current",
                        "reason": "Dropping 5.1 Opus from 224k to 192k is low-leverage here, so preserve the current surround bitrate and spend the size budget on video instead.",
                    },
                }
            ),
            json.dumps(
                {
                    "status": "pass",
                    "summary": "The adjusted video-only draft is safe to try.",
                    "issues": [],
                    "surround_audio_guardrail": {"status": "ok", "reason": ""},
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "operator_note": "Keep pushing toward 300MB.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"surround_5_1_opus_bitrate": "224k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "leverage": "low",
                            "target_bitrate_kbps": 224.0,
                            "next_lower_bitrate_kbps": 192.0,
                            "estimated_step_down_savings_mib": 10.4,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["video"]["target_vmaf"], 89.5)
        self.assertEqual(response.proposed_policy["video"]["max_encoded_percent"], 18)
        self.assertNotIn("audio", response.proposed_policy)
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "pass")
        self.assertIsNone(response.suggested_follow_up)
        self.assertIn("held the current surround bitrate", response.request_response)
        self.assertNotIn("leaning on both video and surround audio", response.request_response)
        self.assertEqual(response.self_check["issues"], [])

    def test_request_note_tuning_reruns_self_check_after_surround_guardrail_rollback(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and surround audio.",
                    "request_disposition": "honored",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try the lower surround tier again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"surround_5_1_opus_bitrate": "192k"},
                    },
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run), patch(
            "mediaforce.advisor._run_tune_self_check",
            side_effect=[
                {
                    "status": "fail",
                    "summary": "The surround audio cut is not a safe trade for this draft.",
                    "issues": ["The surround bitrate cut is the main problem in this proposal."],
                    "surround_audio_guardrail": {
                        "status": "prefer_preserve_current",
                        "reason": "Dropping 5.1 Opus from 224k to 192k is low-leverage here, so preserve the current surround bitrate and spend the size budget on video instead.",
                    },
                },
                {
                    "status": "warn",
                    "summary": "The remaining video-only draft is usable, but verify it against the review clips.",
                    "issues": ["The cap may still land a bit under the requested budget."],
                    "surround_audio_guardrail": {"status": "ok", "reason": ""},
                },
            ],
        ):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "operator_note": "Keep pushing toward 300MB.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"surround_5_1_opus_bitrate": "224k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "leverage": "low",
                            "target_bitrate_kbps": 224.0,
                            "next_lower_bitrate_kbps": 192.0,
                            "estimated_step_down_savings_mib": 10.4,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["video"]["target_vmaf"], 89.5)
        self.assertEqual(response.proposed_policy["video"]["max_encoded_percent"], 18)
        self.assertNotIn("audio", response.proposed_policy)
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "warn")
        self.assertEqual(
            response.self_check["summary"],
            "The remaining video-only draft is usable, but verify it against the review clips.",
        )
        self.assertIsNone(response.suggested_follow_up)
        self.assertIn("held the current surround bitrate", response.request_response)

    def test_request_note_tuning_preserves_failed_self_check_when_guardrail_rerun_returns_none(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and surround audio.",
                    "request_disposition": "honored",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try the lower surround tier again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"surround_5_1_opus_bitrate": "192k"},
                    },
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        initial_fail_self_check = {
            "status": "fail",
            "summary": "The surround audio cut is not a safe trade for this draft.",
            "issues": ["The surround bitrate cut is the main problem in this proposal."],
            "surround_audio_guardrail": {
                "status": "prefer_preserve_current",
                "reason": "Dropping 5.1 Opus from 224k to 192k is low-leverage here, so preserve the current surround bitrate and spend the size budget on video instead.",
            },
        }

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run), patch(
            "mediaforce.advisor._run_tune_self_check",
            side_effect=[initial_fail_self_check, None],
        ):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "operator_note": "Keep pushing toward 300MB.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"surround_5_1_opus_bitrate": "224k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "policy_key": "surround_5_1_opus_bitrate",
                            "leverage": "low",
                            "target_bitrate_kbps": 224.0,
                            "next_lower_bitrate_kbps": 192.0,
                            "estimated_step_down_savings_mib": 10.4,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertFalse(response.ok)
        self.assertIsNone(response.proposed_policy)
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "fail")
        self.assertEqual(
            response.self_check["summary"],
            "Dropping 5.1 Opus from 224k to 192k is low-leverage here, so preserve the current surround bitrate and spend the size budget on video instead.",
        )

    def test_request_note_tuning_preserves_non_audio_failures_when_guardrail_rerun_passes(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and surround audio.",
                    "request_disposition": "honored",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try the lower surround tier again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"surround_5_1_opus_bitrate": "192k"},
                    },
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run), patch(
            "mediaforce.advisor._run_tune_self_check",
            side_effect=[
                {
                    "status": "fail",
                    "summary": "The surround audio cut is not a safe trade for this draft.",
                    "issues": [
                        "The surround bitrate cut is the main problem in this proposal.",
                        "The cap may still land a bit under the requested budget.",
                    ],
                    "surround_audio_guardrail": {
                        "status": "prefer_preserve_current",
                        "reason": "Dropping 5.1 Opus from 224k to 192k is low-leverage here, so preserve the current surround bitrate and spend the size budget on video instead.",
                    },
                },
                {
                    "status": "pass",
                    "summary": "The adjusted draft is safe to try.",
                    "issues": [],
                    "surround_audio_guardrail": {"status": "ok", "reason": ""},
                },
            ],
        ):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "operator_note": "Keep pushing toward 300MB.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"surround_5_1_opus_bitrate": "224k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "policy_key": "surround_5_1_opus_bitrate",
                            "leverage": "low",
                            "target_bitrate_kbps": 224.0,
                            "next_lower_bitrate_kbps": 192.0,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertFalse(response.ok)
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "fail")
        self.assertEqual(response.self_check["issues"], ["The cap may still land a bit under the requested budget."])

    def test_filter_audio_specific_guardrail_issues_keeps_mixed_issue_text(self) -> None:
        issues = [
            "The surround audio cut is not the best lever here.",
            "The surround cut may still leave the size budget slightly under target.",
        ]

        filtered = _filter_audio_specific_guardrail_issues(issues)

        self.assertEqual(filtered, ["The surround cut may still leave the size budget slightly under target."])

    def test_request_note_tuning_clears_mixed_audio_sample_follow_up_when_surround_guardrail_fires(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and surround audio.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Measure the surround audio in the next sample clip before deciding whether to keep that cut.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"surround_5_1_opus_bitrate": "192k"},
                    },
                }
            ),
            json.dumps(
                {
                    "status": "warn",
                    "summary": "The draft is mostly usable, but the surround audio cut is not the best lever here.",
                    "issues": [],
                    "surround_audio_guardrail": {
                        "status": "prefer_preserve_current",
                        "reason": "Dropping 5.1 Opus from 224k to 192k is low-leverage here, so preserve the current surround bitrate and spend the size budget on video instead.",
                    },
                }
            ),
            json.dumps(
                {
                    "status": "pass",
                    "summary": "The adjusted video-only draft is safe to try.",
                    "issues": [],
                    "surround_audio_guardrail": {"status": "ok", "reason": ""},
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "operator_note": "Keep pushing toward 300MB.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"surround_5_1_opus_bitrate": "224k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "leverage": "low",
                            "target_bitrate_kbps": 224.0,
                            "next_lower_bitrate_kbps": 192.0,
                            "estimated_step_down_savings_mib": 10.4,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        self.assertIsNone(response.suggested_follow_up)
        self.assertIn("held the current surround bitrate", response.request_response)

    def test_request_note_tuning_clears_punctuated_audio_follow_up_when_surround_guardrail_fires(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and surround audio.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Measure the surround audio, in the next sample clip, before deciding whether to keep that cut.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"surround_5_1_opus_bitrate": "192k"},
                    },
                }
            ),
            json.dumps(
                {
                    "status": "warn",
                    "summary": "The draft is mostly usable, but the surround audio cut is not the best lever here.",
                    "issues": [],
                    "surround_audio_guardrail": {
                        "status": "prefer_preserve_current",
                        "reason": "Dropping 5.1 Opus from 224k to 192k is low-leverage here, so preserve the current surround bitrate and spend the size budget on video instead.",
                    },
                }
            ),
            json.dumps(
                {
                    "status": "pass",
                    "summary": "The adjusted video-only draft is safe to try.",
                    "issues": [],
                    "surround_audio_guardrail": {"status": "ok", "reason": ""},
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "operator_note": "Keep pushing toward 300MB.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"surround_5_1_opus_bitrate": "224k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "leverage": "low",
                            "target_bitrate_kbps": 224.0,
                            "next_lower_bitrate_kbps": 192.0,
                            "estimated_step_down_savings_mib": 10.4,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        self.assertIsNone(response.suggested_follow_up)
        self.assertIn("held the current surround bitrate", response.request_response)

    def test_request_note_tuning_preserves_non_audio_follow_up_when_surround_guardrail_fires(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can keep the surround bitrates steady and tighten the video target instead.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try lowering target_vmaf on the next pass if you still need savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"surround_5_1_opus_bitrate": "192k"},
                    },
                }
            ),
            json.dumps(
                {
                    "status": "warn",
                    "summary": "The draft is mostly usable, but the surround audio cut is not the best lever here.",
                    "issues": [],
                    "surround_audio_guardrail": {
                        "status": "prefer_preserve_current",
                        "reason": "Dropping 5.1 Opus from 224k to 192k is low-leverage here, so preserve the current surround bitrate and spend the size budget on video instead.",
                    },
                }
            ),
            json.dumps(
                {
                    "status": "pass",
                    "summary": "The adjusted video-only draft is safe to try.",
                    "issues": [],
                    "surround_audio_guardrail": {"status": "ok", "reason": ""},
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "operator_note": "Keep pushing toward 300MB.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"surround_5_1_opus_bitrate": "224k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "leverage": "low",
                            "target_bitrate_kbps": 224.0,
                            "next_lower_bitrate_kbps": 192.0,
                            "estimated_step_down_savings_mib": 10.4,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["video"]["target_vmaf"], 89.5)
        self.assertEqual(response.proposed_policy["video"]["max_encoded_percent"], 18)
        self.assertNotIn("audio", response.proposed_policy)
        self.assertEqual(response.suggested_follow_up, "Try lowering target_vmaf on the next pass if you still need savings.")
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "pass")

    def test_request_note_tuning_rolls_back_stereo_audio_cut_when_guardrail_fires(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and stereo audio.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try the lower stereo bitrate again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"stereo_opus_bitrate": "112k"},
                    },
                }
            ),
            json.dumps(
                {
                    "status": "warn",
                    "summary": "The draft is mostly usable, but the stereo audio cut is not the best lever here.",
                    "issues": [],
                    "surround_audio_guardrail": {
                        "status": "prefer_preserve_current",
                        "reason": "Dropping stereo Opus from 128k to 112k is low-leverage here, so preserve the current stereo bitrate and spend the size budget on video instead.",
                    },
                }
            ),
            json.dumps(
                {
                    "status": "pass",
                    "summary": "The adjusted video-only draft is safe to try.",
                    "issues": [],
                    "surround_audio_guardrail": {"status": "ok", "reason": ""},
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/demo/season-1",
                    "operator_note": "Keep pushing toward 300MB.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"stereo_opus_bitrate": "128k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "policy_key": "stereo_opus_bitrate",
                            "leverage": "low",
                            "target_bitrate_kbps": 128.0,
                            "next_lower_bitrate_kbps": 112.0,
                            "estimated_step_down_savings_mib": 6.2,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["video"]["target_vmaf"], 89.5)
        self.assertEqual(response.proposed_policy["video"]["max_encoded_percent"], 18)
        self.assertNotIn("audio", response.proposed_policy)
        self.assertIsNone(response.suggested_follow_up)
        self.assertIn("held the current stereo bitrate", response.request_response)

    def test_request_note_tuning_rejects_audio_only_draft_after_guardrail_removes_last_edit(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by lowering stereo audio.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Lower stereo only.",
                    "diagnosis": "The remaining opportunity is in audio.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint"],
                    "suggested_follow_up": "Try the lower stereo bitrate again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "audio": {"stereo_opus_bitrate": "112k"},
                    },
                }
            ),
            json.dumps(
                {
                    "status": "warn",
                    "summary": "The stereo audio cut is not the best lever here.",
                    "issues": [],
                    "surround_audio_guardrail": {
                        "status": "prefer_preserve_current",
                        "reason": "Dropping stereo Opus from 128k to 112k is low-leverage here.",
                    },
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/demo/season-1",
                    "operator_note": "Go a little smaller.",
                    "policy": {
                        "audio": {"stereo_opus_bitrate": "128k"},
                    },
                    "runtime_toolbelt": {
                        "audio_tradeoff_hint": {
                            "policy_key": "stereo_opus_bitrate",
                            "leverage": "low",
                            "target_bitrate_kbps": 128.0,
                            "next_lower_bitrate_kbps": 112.0,
                        }
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertFalse(response.ok)
        self.assertIsNone(response.proposed_policy)
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "warn")
        self.assertIn("Dropping current stereo bitrate from 128k to 112k is low-leverage here", response.self_check["summary"])
        self.assertIn("Dropping current stereo bitrate from 128k to 112k is low-leverage here", response.suggested_follow_up)

    def test_request_note_tuning_applies_audio_guardrail_when_self_check_is_missing(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and stereo audio.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try the lower stereo bitrate again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"stereo_opus_bitrate": "112k"},
                    },
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run), patch(
            "mediaforce.advisor._run_tune_self_check",
            return_value=None,
        ):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/demo/season-1",
                    "operator_note": "Keep pushing toward 300MB.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"stereo_opus_bitrate": "128k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "policy_key": "stereo_opus_bitrate",
                            "leverage": "low",
                            "target_bitrate_kbps": 128.0,
                            "next_lower_bitrate_kbps": 112.0,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["video"]["target_vmaf"], 89.5)
        self.assertEqual(response.proposed_policy["video"]["max_encoded_percent"], 18)
        self.assertNotIn("audio", response.proposed_policy)
        self.assertIsNone(response.suggested_follow_up)
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "warn")
        self.assertEqual(
            response.self_check["summary"],
            "Dropping current stereo bitrate from 128k to 112k is low-leverage here, so preserve the current stereo bitrate and spend the size budget on video instead.",
        )

    def test_request_note_tuning_backstops_audio_guardrail_when_self_check_misses_it(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and stereo audio.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try the lower stereo bitrate again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"stereo_opus_bitrate": "112k"},
                    },
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run), patch(
            "mediaforce.advisor._run_tune_self_check",
            side_effect=[
                {
                    "status": "pass",
                    "summary": "The draft is safe to try.",
                    "issues": [],
                    "surround_audio_guardrail": {"status": "ok", "reason": ""},
                },
                {
                    "status": "pass",
                    "summary": "The adjusted draft is safe to try.",
                    "issues": [],
                    "surround_audio_guardrail": {"status": "ok", "reason": ""},
                },
            ],
        ):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/demo/season-1",
                    "operator_note": "Keep pushing toward 300MB.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"stereo_opus_bitrate": "128k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "policy_key": "stereo_opus_bitrate",
                            "leverage": "low",
                            "target_bitrate_kbps": 128.0,
                            "next_lower_bitrate_kbps": 112.0,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["video"]["target_vmaf"], 89.5)
        self.assertEqual(response.proposed_policy["video"]["max_encoded_percent"], 18)
        self.assertNotIn("audio", response.proposed_policy)
        self.assertIsNone(response.suggested_follow_up)
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "pass")
        self.assertEqual(response.self_check["summary"], "The adjusted draft is safe to try.")

    def test_request_note_tuning_does_not_treat_generic_bitrate_note_as_audio_specific(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and stereo audio.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try the lower stereo bitrate again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"stereo_opus_bitrate": "112k"},
                    },
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run), patch(
            "mediaforce.advisor._run_tune_self_check",
            return_value={
                "status": "pass",
                "summary": "The draft is safe to try.",
                "issues": [],
                "surround_audio_guardrail": {"status": "ok", "reason": ""},
            },
        ):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/demo/season-1",
                    "operator_note": "Trim the bitrate a little more, but keep the motion clean.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"stereo_opus_bitrate": "128k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "policy_key": "stereo_opus_bitrate",
                            "leverage": "low",
                            "target_bitrate_kbps": 128.0,
                            "next_lower_bitrate_kbps": 112.0,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["video"]["target_vmaf"], 89.5)
        self.assertEqual(response.proposed_policy["video"]["max_encoded_percent"], 18)
        self.assertNotIn("audio", response.proposed_policy)
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "pass")

    def test_request_note_tuning_recognizes_codec_names_as_audio_specific(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and stereo audio.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try the lower stereo bitrate again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"stereo_opus_bitrate": "112k"},
                    },
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run), patch(
            "mediaforce.advisor._run_tune_self_check",
            return_value={
                "status": "pass",
                "summary": "The draft is safe to try.",
                "issues": [],
                "surround_audio_guardrail": {"status": "ok", "reason": ""},
            },
        ):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/demo/season-1",
                    "operator_note": "Drop the AAC track harder if that buys us the space.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"stereo_opus_bitrate": "128k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "policy_key": "stereo_opus_bitrate",
                            "leverage": "low",
                            "target_bitrate_kbps": 128.0,
                            "next_lower_bitrate_kbps": 112.0,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["audio"]["stereo_opus_bitrate"], "112k")
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "pass")

    def test_request_note_tuning_recognizes_audio_request_even_when_video_terms_appear(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and stereo audio.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try the lower stereo bitrate again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"stereo_opus_bitrate": "112k"},
                    },
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run), patch(
            "mediaforce.advisor._run_tune_self_check",
            return_value={
                "status": "pass",
                "summary": "The draft is safe to try.",
                "issues": [],
                "surround_audio_guardrail": {"status": "ok", "reason": ""},
            },
        ):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/demo/season-1",
                    "operator_note": "Keep the video clean, but drop the AAC track harder if that buys us the space.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"stereo_opus_bitrate": "128k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "policy_key": "stereo_opus_bitrate",
                            "leverage": "low",
                            "target_bitrate_kbps": 128.0,
                            "next_lower_bitrate_kbps": 112.0,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["audio"]["stereo_opus_bitrate"], "112k")
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "pass")

    def test_request_note_tuning_recognizes_audio_mix_language(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and stereo audio.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try the lower stereo bitrate again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"stereo_opus_bitrate": "112k"},
                    },
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run), patch(
            "mediaforce.advisor._run_tune_self_check",
            return_value={
                "status": "pass",
                "summary": "The draft is safe to try.",
                "issues": [],
                "surround_audio_guardrail": {"status": "ok", "reason": ""},
            },
        ):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/demo/season-1",
                    "operator_note": "Keep the video close, but spend the English mix if that helps size.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"stereo_opus_bitrate": "128k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "policy_key": "stereo_opus_bitrate",
                            "leverage": "low",
                            "target_bitrate_kbps": 128.0,
                            "next_lower_bitrate_kbps": 112.0,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["audio"]["stereo_opus_bitrate"], "112k")
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "pass")

    def test_request_note_tuning_reruns_self_check_after_guardrail_warn_rewrite(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and stereo audio.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try the lower stereo bitrate again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"stereo_opus_bitrate": "112k"},
                    },
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run), patch(
            "mediaforce.advisor._run_tune_self_check",
            side_effect=[
                {
                    "status": "pass",
                    "summary": "The original draft is safe to try.",
                    "issues": [],
                    "surround_audio_guardrail": {
                        "status": "prefer_preserve_current",
                        "reason": "Dropping current stereo bitrate from 128k to 112k is low-leverage here, so preserve the current stereo bitrate and spend the size budget on video instead.",
                    },
                },
                {
                    "status": "pass",
                    "summary": "The adjusted draft is safe to try.",
                    "issues": [],
                    "surround_audio_guardrail": {"status": "ok", "reason": ""},
                },
            ],
        ):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/demo/season-1",
                    "operator_note": "Keep pushing toward 300MB.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"stereo_opus_bitrate": "128k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "policy_key": "stereo_opus_bitrate",
                            "leverage": "low",
                            "target_bitrate_kbps": 128.0,
                            "next_lower_bitrate_kbps": 112.0,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        assert response.proposed_policy is not None
        self.assertNotIn("audio", response.proposed_policy)
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "pass")
        self.assertEqual(response.self_check["summary"], "The adjusted draft is safe to try.")

    def test_request_note_tuning_updates_summary_and_diagnosis_after_guardrail(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and surround audio.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try the lower surround tier again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"surround_5_1_opus_bitrate": "192k"},
                    },
                }
            ),
            json.dumps(
                {
                    "status": "warn",
                    "summary": "The draft is mostly usable, but the surround audio cut is not the best lever here.",
                    "issues": [],
                    "surround_audio_guardrail": {
                        "status": "prefer_preserve_current",
                        "reason": "Dropping 5.1 Opus from 224k to 192k is low-leverage here, so preserve the current surround bitrate and spend the size budget on video instead.",
                    },
                }
            ),
            json.dumps(
                {
                    "status": "warn",
                    "summary": "The remaining video-only draft is usable, but the cap may still land a bit under the requested budget.",
                    "issues": ["The cap may still land a bit under the requested budget."],
                    "surround_audio_guardrail": {"status": "ok", "reason": ""},
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "operator_note": "Keep pushing toward 300MB.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"surround_5_1_opus_bitrate": "224k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "policy_key": "surround_5_1_opus_bitrate",
                            "leverage": "low",
                            "target_bitrate_kbps": 224.0,
                            "next_lower_bitrate_kbps": 192.0,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.summary, "Kept the non-audio tightening, but preserved the current surround bitrate.")
        self.assertIn("lowering the current surround bitrate was not the right trade here", response.diagnosis)
        self.assertIn("Dropping current surround bitrate from 224k to 192k is low-leverage here", response.diagnosis)

    def test_request_note_tuning_filters_audio_only_issues_after_guardrail_warn(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can shave a little more size by leaning on both video and surround audio.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Try the lower surround tier again if you still need size savings.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"surround_5_1_opus_bitrate": "192k"},
                    },
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run), patch(
            "mediaforce.advisor._run_tune_self_check",
            side_effect=[
                {
                    "status": "warn",
                    "summary": "The draft is mostly usable, but the surround audio cut is not the best lever here.",
                    "issues": [
                        "The surround audio cut is not the best lever here.",
                        "The cap may still land a bit under the requested budget.",
                    ],
                    "surround_audio_guardrail": {
                        "status": "prefer_preserve_current",
                        "reason": "Dropping 5.1 Opus from 224k to 192k is low-leverage here, so preserve the current surround bitrate and spend the size budget on video instead.",
                    },
                },
                {
                    "status": "warn",
                    "summary": "The remaining video-only draft is usable, but the cap may still land a bit under the requested budget.",
                    "issues": ["The cap may still land a bit under the requested budget."],
                    "surround_audio_guardrail": {"status": "ok", "reason": ""},
                },
            ],
        ):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "operator_note": "Keep pushing toward 300MB.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"surround_5_1_opus_bitrate": "224k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "policy_key": "surround_5_1_opus_bitrate",
                            "leverage": "low",
                            "target_bitrate_kbps": 224.0,
                            "next_lower_bitrate_kbps": 192.0,
                            "estimated_step_down_savings_mib": 10.4,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        assert response.self_check is not None
        self.assertEqual(
            response.self_check["issues"],
            ["The cap may still land a bit under the requested budget."],
        )

    def test_request_note_tuning_preserves_follow_up_that_only_uses_track_as_verb(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can keep the surround bitrates steady and tighten the video target instead.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Trim the next sample slightly further.",
                    "diagnosis": "The current draft still has room for a modest size-first push.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.audio_tradeoff_hint", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "Track whether the dark scenes still look stable on the next pass.",
                    "feasibility_note": None,
                    "policy": {
                        "video": {"target_vmaf": 89.5, "max_encoded_percent": 18},
                        "audio": {"surround_5_1_opus_bitrate": "192k"},
                    },
                }
            ),
            json.dumps(
                {
                    "status": "warn",
                    "summary": "The draft is mostly usable, but the surround audio cut is not the best lever here.",
                    "issues": [],
                    "surround_audio_guardrail": {
                        "status": "prefer_preserve_current",
                        "reason": "Dropping 5.1 Opus from 224k to 192k is low-leverage here, so preserve the current surround bitrate and spend the size budget on video instead.",
                    },
                }
            ),
            json.dumps(
                {
                    "status": "pass",
                    "summary": "The adjusted video-only draft is safe to try.",
                    "issues": [],
                    "surround_audio_guardrail": {"status": "ok", "reason": ""},
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "operator_note": "Keep pushing toward 300MB.",
                    "policy": {
                        "video": {"target_vmaf": 91.0, "max_encoded_percent": 22},
                        "audio": {"surround_5_1_opus_bitrate": "224k"},
                    },
                    "runtime_toolbelt": {
                        "recent_sample_result": {"predicted_encode_percent": 20.4},
                        "audio_tradeoff_hint": {
                            "policy_key": "surround_5_1_opus_bitrate",
                            "leverage": "low",
                            "target_bitrate_kbps": 224.0,
                            "next_lower_bitrate_kbps": 192.0,
                            "estimated_step_down_savings_mib": 10.4,
                        },
                    },
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        self.assertEqual(
            response.suggested_follow_up,
            "Track whether the dark scenes still look stable on the next pass.",
        )

    def test_request_note_tuning_keeps_repeat_confirmed_experiment(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I softened that request to stay safer.",
                    "request_disposition": "softened",
                    "summary": "Safer follow-up draft.",
                    "diagnosis": "The request still looks too aggressive.",
                    "confidence": "medium",
                    "evidence_checked": ["requested_experiment"],
                    "suggested_follow_up": None,
                    "feasibility_note": None,
                    "policy": {"video": {"target_vmaf": 91.0}},
                }
            ),
            json.dumps(
                {
                    "status": "pass",
                    "summary": "The repeated experiment is coherent enough to measure.",
                    "issues": [],
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "operator_note": "Keep pushing. I still want under 300MB.",
                    "requested_experiment": {
                        "request_type": "combined_experiment",
                        "applied_policy": {"video": {"target_vmaf": 88.5, "min_target_vmaf": 87.0, "max_encoded_percent": 8}},
                    },
                    "operator_repeat_signal": {
                        "repeat_count": 2,
                        "previous_softened_count": 1,
                        "operator_confirmed": True,
                    },
                    "policy": {
                        "video": {
                            "target_vmaf": 94.5,
                            "min_target_vmaf": 93.0,
                            "max_encoded_percent": 55,
                        }
                    },
                    "runtime_toolbelt": {"recent_sample_result": {"predicted_encode_percent": 20.28}},
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.request_disposition, "honored")
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["video"]["target_vmaf"], 88.5)
        self.assertEqual(response.proposed_policy["video"]["min_target_vmaf"], 87.0)
        self.assertEqual(response.proposed_policy["video"]["max_encoded_percent"], 8)

    def test_request_run_verdict_uses_deterministic_evidence(self) -> None:
        with patch("mediaforce.advisor._run_codex_lab_process_impl") as subprocess_run:
            response = request_run_verdict(
                project_root=self.root,
                payload={
                    "folder": "tv/house/season-2",
                    "sample_result": {"quality_metric": "VMAF", "quality_score": 91.4},
                    "operator_request": {"metric": "vmaf", "target": 90.0},
                },
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.prompt_version, RUN_VERDICT_PROMPT_VERSION)
        self.assertEqual(response.outcome, "acceptable_experiment")
        self.assertEqual(response.confidence, "medium")
        self.assertIn("met the requested quality floor", response.summary)
        subprocess_run.assert_not_called()

    def test_request_seed_policy_uses_structured_runner(self) -> None:
        response_body = json.dumps(
            {
                "request_response": "I would keep the first pass close to base and only lean slightly smaller.",
                "request_disposition": "softened",
                "summary": "Keep the base policy close and only lean a touch smaller.",
                "diagnosis": "The note asks for a tighter encode than a cold-start draft can justify.",
                "confidence": "medium",
                "evidence_checked": ["base_policy", "class_signals"],
                "suggested_follow_up": "Run one sample first, then ask for a tighter size target if it still looks roomy.",
                "feasibility_note": "Cold-start size budgets are rough until a measured sample exists.",
                "policy": {"video": {}}
            }
        )
        commands, fake_run = self._capture_subprocess_commands(response_body)

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_seed_policy(
                project_root=self.root,
                payload={
                    "folder": "tv/house/season-2",
                    "base_policy": {"video": {"target_vmaf": 94.5}},
                },
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.request_disposition, "softened")
        self.assertIn("first pass", response.request_response)
        self._assert_structured_subprocess_call(commands)

    def test_request_seed_policy_keeps_operator_confirmed_budget_experiment(self) -> None:
        response_body = json.dumps(
            {
                "request_response": "300 MB is too aggressive for a safe cold start.",
                "request_disposition": "softened",
                "summary": "Safer first-pass draft.",
                "diagnosis": "The request is too aggressive for a cold start.",
                "confidence": "medium",
                "evidence_checked": ["requested_experiment"],
                "suggested_follow_up": None,
                "feasibility_note": None,
                "policy": {"video": {"target_vmaf": 94.5}},
            }
        )
        commands, fake_run = self._capture_subprocess_commands(response_body)

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_seed_policy(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "base_policy": {
                        "video": {
                            "target_vmaf": 95.0,
                            "min_target_vmaf": 93.0,
                            "max_encoded_percent": 55,
                        }
                    },
                    "requested_experiment": {
                        "request_type": "size_budget",
                        "operator_confirmed": True,
                        "applied_policy": {"video": {"max_encoded_percent": 8}},
                    },
                },
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.request_disposition, "honored")
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["video"]["max_encoded_percent"], 8)
        self.assertIn("real sample and your review will decide", response.request_response)
        self._assert_structured_subprocess_call(commands)

    def test_request_seed_policy_rejects_nonpositive_video_budget(self) -> None:
        response_body = json.dumps(
            {
                "request_response": "I kept the requested target.",
                "request_disposition": "honored",
                "summary": "Queue the requested sample.",
                "diagnosis": "The request was explicit.",
                "confidence": "medium",
                "evidence_checked": ["requested_experiment"],
                "suggested_follow_up": None,
                "feasibility_note": None,
                "policy": {"video": {"target_vmaf": 85.0}},
            }
        )
        commands, fake_run = self._capture_subprocess_commands(response_body)

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_seed_policy(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "base_policy": {"video": {"target_vmaf": 95.0}},
                    "requested_experiment": {
                        "request_type": "size_budget",
                        "operator_confirmed": True,
                        "budget_bytes": 20 * 1024 * 1024,
                        "estimated_audio_bytes": 24 * 1024 * 1024,
                        "estimated_video_bitrate_kbps": 0.0,
                        "applied_policy": None,
                    },
                },
            )

        self.assertFalse(response.ok)
        self.assertEqual(response.request_disposition, "rejected")
        self.assertIsNone(response.proposed_policy)
        self.assertIn("no positive video budget", response.request_response)
        self._assert_structured_subprocess_call(commands)

    def test_request_seed_policy_does_not_override_failed_sample_refusal(self) -> None:
        response_body = json.dumps(
            {
                "request_response": "The failed sample needs a different plan.",
                "request_disposition": "rejected",
                "summary": "Do not repeat the failed draft.",
                "diagnosis": "The previous sample failed with the same constraints.",
                "confidence": "high",
                "evidence_checked": ["latest_failed_sample_job.error"],
                "suggested_follow_up": "Change the draft before retrying.",
                "feasibility_note": None,
                "policy": {"video": {"target_vmaf": 94.0}},
            }
        )
        commands, fake_run = self._capture_subprocess_commands(response_body)

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_seed_policy(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "base_policy": {"video": {"target_vmaf": 95.0}},
                    "requested_experiment": {
                        "request_type": "size_budget",
                        "operator_confirmed": True,
                        "feasibility": "plausible",
                        "applied_policy": None,
                    },
                    "latest_failed_sample_job": {
                        "status": "failed",
                        "error": "Failed to find a suitable CRF",
                    },
                },
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.request_disposition, "rejected")
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["video"]["target_vmaf"], 94.0)
        self._assert_structured_subprocess_call(commands)

    def test_repeated_tune_backstop_preserves_visual_rejection(self) -> None:
        parsed = {
            "request_disposition": "rejected",
            "summary": "The current sample was rejected.",
        }

        forced = _force_repeated_tune_experiment(
            current_policy={"video": {"target_vmaf": 95.0}},
            parsed=parsed,
            requested_experiment={
                "operator_confirmed": True,
                "evidence_authority": "rejected_visual_result",
                "applied_policy": {"video": {"target_vmaf": 85.0}},
            },
            repeat_signal={"repeat_count": 2, "previous_softened_count": 1},
            latest_failed_sample_job=None,
        )

        self.assertIsNone(forced)
        self.assertEqual(parsed["request_disposition"], "rejected")

    def test_request_seed_policy_keeps_structured_worker_failure_diagnostics(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> object:
            timeout = kwargs.get("timeout")
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(timeout or 0), stderr="model timed out")

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run):
            response = request_seed_policy(
                project_root=self.root,
                payload={
                    "folder": "tv/lucifer/season-2",
                    "base_policy": {"video": {"target_size_mb": 300.0}},
                },
        )

        self.assertFalse(response.ok)
        self.assertIn("attempt 1: timeout: Codex Lab timed out", response.raw)
        self.assertNotIn("model timed out", response.raw)

    def test_operator_requested_experiment_detects_literal_vmaf_target(self) -> None:
        request = _operator_requested_experiment("I want to try 85 VMAF on this show.")

        assert request is not None
        self.assertEqual(request["metric"], "vmaf")
        self.assertEqual(request["target"], 85.0)
        self.assertEqual(request["operator_note_parse"]["request_type"], "metric_target")
        self.assertEqual(request["applied_policy"]["video"]["quality_metric"], "vmaf")
        self.assertEqual(request["applied_policy"]["video"]["target_vmaf"], 85.0)
        self.assertEqual(request["applied_policy"]["video"]["min_target_vmaf"], 83.0)

    def test_operator_requested_experiment_accepts_vmaf_of_phrasing(self) -> None:
        request = _operator_requested_experiment("Try a VMAF of 85 for this sample.")

        assert request is not None
        self.assertEqual(request["metric"], "vmaf")
        self.assertEqual(request["target"], 85.0)

    def test_operator_requested_experiment_accepts_metric_only_directive(self) -> None:
        request = _operator_requested_experiment("Use VMAF for this sample.")

        assert request is not None
        self.assertEqual(request["metric"], "vmaf")
        self.assertIsNone(request["target"])
        self.assertEqual(request["applied_policy"]["video"], {"quality_metric": "vmaf"})

    def test_operator_requested_experiment_ignores_incidental_metric_mentions(self) -> None:
        request = _operator_requested_experiment("The dashboard says VMAF is available on this host.")

        self.assertIsNone(request)

    def test_operator_requested_experiment_detects_size_budget_request(self) -> None:
        request = _operator_requested_experiment(
            "I want to aim for 200MB per episode while losing as little fidelity as possible.",
            {
                "source_size_bytes": 4_815_446_620,
                "duration_seconds": 2660.352,
                "audio_summary": [{"codec_name": "eac3", "channels": 6}],
                "resolved_policy": {
                    "video": {"encoder": "libsvtav1"},
                    "audio": {
                        "convert_to_opus_codecs": ["eac3"],
                        "surround_5_1_opus_bitrate": "224k",
                    }
                },
            },
        )

        assert request is not None
        self.assertEqual(request["request_type"], "size_budget")
        self.assertTrue(request["operator_confirmed"])
        self.assertEqual(request["budget_label"], "200 MB per episode")
        self.assertEqual(request["feasibility"], "aggressive_but_measurable")
        self.assertEqual(
            request["stream_budget_ledger"]["feasibility"]["status"],
            "aggressive_but_measurable",
        )
        self.assertFalse(request["requires_confirmation"])
        self.assertFalse(request["hard_size_cap"])
        self.assertFalse(request["measured_size_followup"])
        self.assertAlmostEqual(request["estimated_source_percent"], 4.15, places=2)
        self.assertAlmostEqual(request["target_encoded_percent"], 4.15, places=2)
        self.assertIsNone(request["requested_max_encoded_percent"])
        self.assertIsNone(request["applied_max_encoded_percent"])
        self.assertEqual(request["applied_policy"]["video"]["size_goal_mode"], "absolute")
        self.assertEqual(request["applied_policy"]["video"]["target_size_bytes"], 200_000_000)

    def test_operator_requested_experiment_uses_model_for_ambiguous_worded_budget(self) -> None:
        parsed_note = {
            "summary": "Target roughly three hundred megabytes per episode.",
            "intent_type": "direct_request",
            "request_type": "size_budget",
            "operator_confirmed": True,
            "measured_size_followup": False,
            "evidence_authority": "none",
            "metric": None,
            "metric_target": None,
            "size_budget_value": 300,
            "size_budget_unit": "mb",
            "scale_height": None,
            "black_bar_handling": None,
            "crop": None,
            "reasoning_note": "The amount was written as words.",
        }
        with patch(
            "mediaforce.web.runtime.folder_tuning_advice.request_operator_note_parse",
            return_value=parsed_note,
        ) as note_parser:
            request = _operator_requested_experiment(
                "Please aim for roughly three hundred megabytes per episode.",
                {
                    "source_size_bytes": 3_519_516_042,
                    "duration_seconds": 2561.35,
                    "audio_summary": [{"channels": 6}],
                    "resolved_policy": {"audio": {"surround_5_1_opus_bitrate": "224k"}},
                },
            )

        assert request is not None
        self.assertEqual(request["budget_bytes"], 300_000_000)
        note_parser.assert_called_once()

    def test_operator_requested_experiment_detects_generated_measured_followup(self) -> None:
        note = (
            "Measured follow-up: keep the 225 MB per episode goal. The last representative test was 4.6 times over "
            "that goal. Keep the current resolution and make the next representative test materially smaller toward "
            "the goal while preserving as much picture and sound quality as possible."
        )
        with patch("mediaforce.web.runtime.folder_tuning_advice.request_operator_note_parse", return_value=None):
            request = _operator_requested_experiment(
                note,
                {
                    "source_size_bytes": 9_090_180_637,
                    "duration_seconds": 5279.774,
                    "audio_summary": [{"codec_name": "eac3", "channels": 2}],
                    "resolved_policy": {
                        "video": {"encoder": "libsvtav1"},
                        "audio": {
                            "convert_to_opus_codecs": ["eac3"],
                            "stereo_opus_bitrate": "128k",
                        },
                    },
                },
            )

        assert request is not None
        self.assertEqual(request["request_type"], "combined_experiment")
        self.assertTrue(request["operator_confirmed"])
        self.assertTrue(request["measured_size_followup"])
        self.assertEqual(request["budget_bytes"], 225_000_000)
        self.assertEqual(request["scale_height"], 0)
        self.assertEqual(request["applied_policy"]["video"]["max_height"], 0)

    def test_operator_requested_experiment_detects_generated_under_target_followup(self) -> None:
        note = (
            "Measured follow-up: keep the 225 MB per episode goal. The last representative test landed below that "
            "goal. Keep the current resolution and make the next representative test spend more of the available "
            "size on picture and sound quality."
        )
        with patch("mediaforce.web.runtime.folder_tuning_advice.request_operator_note_parse", return_value=None):
            request = _operator_requested_experiment(note)

        assert request is not None
        self.assertEqual(request["request_type"], "combined_experiment")
        self.assertTrue(request["operator_confirmed"])
        self.assertTrue(request["measured_size_followup"])
        self.assertEqual(request["budget_bytes"], 225_000_000)
        self.assertEqual(request["scale_height"], 0)
        self.assertEqual(request["applied_policy"]["video"]["max_height"], 0)

    def test_operator_requested_experiment_ignores_negative_size_budget(self) -> None:
        with patch("mediaforce.web.runtime.folder_tuning_advice.request_operator_note_parse", return_value=None):
            request = _operator_requested_experiment("Target -10MB per episode.")

        self.assertIsNone(request)

    def test_operator_requested_experiment_preserves_observed_av1_quality_authority(self) -> None:
        request = _operator_requested_experiment(
            "The 1080p AV1 sample looked excellent. Keep source resolution and target 250MB per episode.",
            {
                "source_size_bytes": 4_815_446_620,
                "duration_seconds": 2660.352,
                "audio_summary": [{"codec_name": "eac3", "channels": 6}],
                "resolved_policy": {
                    "video": {"encoder": "libsvtav1"},
                    "audio": {
                        "convert_to_opus_codecs": ["eac3"],
                        "surround_5_1_opus_bitrate": "256k",
                    },
                },
            },
        )

        assert request is not None
        self.assertEqual(request["request_type"], "combined_experiment")
        self.assertEqual(request["evidence_authority"], "operator_observed")
        self.assertEqual(request["feasibility"], "aggressive_but_measurable")
        self.assertEqual(request["scale_height"], 0)

    def test_operator_requested_experiment_preserves_visual_rejection(self) -> None:
        request = _operator_requested_experiment(
            "I reject this sample because it looked blocky. Keep source resolution and target 250MB per episode.",
            {
                "source_size_bytes": 4_815_446_620,
                "duration_seconds": 2660.352,
                "audio_summary": [{"codec_name": "eac3", "channels": 6}],
                "resolved_policy": {
                    "video": {"encoder": "libsvtav1"},
                    "audio": {
                        "convert_to_opus_codecs": ["eac3"],
                        "surround_5_1_opus_bitrate": "256k",
                    },
                },
            },
        )

        assert request is not None
        self.assertEqual(request["evidence_authority"], "rejected_visual_result")
        issue = proposal_alignment_issue(
            operator_request=request,
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 95.0, "max_height": 0}},
            preview_policy={"video": {"target_vmaf": 85.0, "max_height": 0}},
        )
        self.assertIsNotNone(issue)

    def test_operator_requested_experiment_preserves_standalone_visual_rejection(self) -> None:
        request = _operator_requested_experiment("I reject this sample because it looked blocky.")

        assert request is not None
        self.assertEqual(request["request_type"], "none")
        self.assertEqual(request["evidence_authority"], "rejected_visual_result")
        self.assertIsNone(request["applied_policy"])

    def test_operator_requested_experiment_mixed_review_is_not_positive_authority(self) -> None:
        with patch(
                "mediaforce.web.runtime.folder_tuning_advice.request_operator_note_parse",
                return_value={
                    "summary": "Approved-looking structured parse.",
                    "intent_type": "direct_request",
                    "request_type": "size_budget",
                    "operator_confirmed": True,
                    "measured_size_followup": False,
                    "evidence_authority": "approved_visual_result",
                    "metric": None,
                    "metric_target": None,
                    "size_budget_value": 250,
                    "size_budget_unit": "mb",
                    "scale_height": None,
                    "black_bar_handling": None,
                    "crop": None,
                    "reasoning_note": "Incorrectly treated the mixed review as approval.",
                },
        ):
            request = _operator_requested_experiment(
                "The sample looked good overall, but dark scenes showed banding. Target 250MB per episode.",
                {
                    "source_size_bytes": 4_815_446_620,
                    "duration_seconds": 2660.352,
                    "audio_summary": [{"codec_name": "eac3", "channels": 6}],
                    "resolved_policy": {
                        "video": {"encoder": "libsvtav1"},
                        "audio": {
                            "convert_to_opus_codecs": ["eac3"],
                            "surround_5_1_opus_bitrate": "256k",
                        },
                    },
                },
            )

        assert request is not None
        self.assertEqual(request["evidence_authority"], "rejected_visual_result")

    def test_operator_requested_experiment_detects_explicit_hard_size_cap(self) -> None:
        request = _operator_requested_experiment(
            "Hard cap this at 300MB per episode.",
            {
                "source_size_bytes": 4_349_049_136,
                "duration_seconds": 3161.376,
                "audio_summary": [{"codec_name": "ac3", "channels": 6}],
                "resolved_policy": {
                    "video": {"encoder": "libsvtav1"},
                    "audio": {
                        "convert_to_opus_codecs": ["ac3"],
                        "surround_5_1_opus_bitrate": "256k",
                    },
                },
            },
        )

        assert request is not None
        self.assertEqual(request["request_type"], "size_budget")
        self.assertTrue(request["hard_size_cap"])
        self.assertFalse(request["measured_size_followup"])
        self.assertEqual(request["budget_label"], "300 MB per episode")
        self.assertAlmostEqual(request["requested_max_encoded_percent"], 6.9, places=2)
        self.assertEqual(request["applied_policy"]["video"]["size_goal_mode"], "absolute")
        self.assertEqual(request["applied_policy"]["video"]["target_size_bytes"], 300_000_000)

    def test_operator_requested_experiment_marks_measured_size_followup(self) -> None:
        request = _operator_requested_experiment(
            "Revise this sample smaller toward 300 MB per episode. The last sample was 766 MiB, 2.6x target.",
            {
                "source_size_bytes": 4_349_049_136,
                "duration_seconds": 3161.376,
                "audio_summary": [{"codec_name": "ac3", "channels": 6}],
                "resolved_policy": {
                    "video": {"encoder": "libsvtav1"},
                    "audio": {
                        "convert_to_opus_codecs": ["ac3"],
                        "surround_5_1_opus_bitrate": "256k",
                    },
                },
            },
        )

        assert request is not None
        self.assertEqual(request["request_type"], "size_budget")
        self.assertTrue(request["operator_confirmed"])
        self.assertTrue(request["measured_size_followup"])
        self.assertFalse(request["hard_size_cap"])

    def test_operator_requested_experiment_does_not_mark_plain_size_sample_as_followup(self) -> None:
        request = _operator_requested_experiment(
            "Run another sample targeting about 300 MB per episode.",
            {
                "source_size_bytes": 4_349_049_136,
                "duration_seconds": 3161.376,
                "audio_summary": [{"codec_name": "ac3", "channels": 6}],
                "resolved_policy": {
                    "video": {"encoder": "libsvtav1"},
                    "audio": {
                        "convert_to_opus_codecs": ["ac3"],
                        "surround_5_1_opus_bitrate": "256k",
                    },
                },
            },
        )

        assert request is not None
        self.assertEqual(request["request_type"], "size_budget")
        self.assertTrue(request["operator_confirmed"])
        self.assertFalse(request["measured_size_followup"])
        self.assertFalse(request["hard_size_cap"])

    def test_operator_requested_experiment_uses_deterministic_followup_without_model(self) -> None:
        structured_parse = {
            "summary": "Size follow-up request.",
            "intent_type": "direct_request",
            "request_type": "size_budget",
            "operator_confirmed": True,
            "measured_size_followup": False,
            "metric": None,
            "metric_target": None,
            "size_budget_value": 300,
            "size_budget_unit": "mb",
            "scale_height": None,
            "black_bar_handling": None,
            "crop": None,
            "hard_size_cap": False,
            "reasoning_note": "Structured parse missed the measured follow-up wording.",
        }

        with patch(
            "mediaforce.web.runtime.folder_tuning_advice.request_operator_note_parse",
            return_value=structured_parse,
        ) as note_parser:
            request = _operator_requested_experiment(
                "Revise this sample smaller toward 300 MB per episode. The last sample was 766 MiB, 2.6x target.",
                {
                    "source_size_bytes": 4_349_049_136,
                    "duration_seconds": 3161.376,
                    "audio_summary": [{"codec_name": "ac3", "channels": 6}],
                    "resolved_policy": {
                        "video": {"encoder": "libsvtav1"},
                        "audio": {
                            "convert_to_opus_codecs": ["ac3"],
                            "surround_5_1_opus_bitrate": "256k",
                        },
                    },
                },
            )

        assert request is not None
        self.assertTrue(request["measured_size_followup"])
        note_parser.assert_not_called()

    def test_operator_requested_experiment_detects_scale_target_request(self) -> None:
        request = _operator_requested_experiment("Downsample the 4K files to 1080p for this folder.")

        assert request is not None
        self.assertEqual(request["request_type"], "scale_target")
        self.assertTrue(request["operator_confirmed"])
        self.assertEqual(request["scale_height"], 1080)
        self.assertEqual(request["applied_policy"]["video"]["max_height"], 1080)

    def test_operator_requested_experiment_detects_source_resolution_request(self) -> None:
        request = _operator_requested_experiment(
            "Keep source resolution at 1080p. Do not downscale and keep max_height unset or 0."
        )

        assert request is not None
        self.assertEqual(request["request_type"], "scale_target")
        self.assertTrue(request["operator_confirmed"])
        self.assertEqual(request["scale_height"], 0)
        self.assertEqual(request["scale_label"], "source resolution")
        self.assertEqual(request["applied_policy"]["video"]["max_height"], 0)

    def test_operator_requested_experiment_detects_smart_black_bar_request(self) -> None:
        request = _operator_requested_experiment("Use smart black-bar detection for this letterboxed season.")

        assert request is not None
        self.assertEqual(request["request_type"], "scale_target")
        self.assertTrue(request["operator_confirmed"])
        self.assertEqual(request["black_bar_handling"], "smart")
        self.assertEqual(request["applied_policy"]["video"]["black_bar_handling"], "smart")
        self.assertEqual(request["applied_policy"]["video"]["crop"], "")

    def test_operator_requested_experiment_detects_manual_crop_request(self) -> None:
        request = _operator_requested_experiment("Use manual crop 1920:800:0:140 for this folder.")

        assert request is not None
        self.assertEqual(request["request_type"], "scale_target")
        self.assertTrue(request["operator_confirmed"])
        self.assertEqual(request["crop"], "1920:800:0:140")
        self.assertEqual(request["applied_policy"]["video"]["crop"], "1920:800:0:140")

    def test_operator_requested_experiment_combines_scale_with_metric_request(self) -> None:
        request = _operator_requested_experiment("Downsample to 1080p and target 88 VMAF for the next sample.")

        assert request is not None
        self.assertEqual(request["request_type"], "combined_experiment")
        self.assertEqual(request["metric"], "vmaf")
        self.assertEqual(request["target"], 88.0)
        self.assertEqual(request["scale_height"], 1080)
        self.assertEqual(request["applied_policy"]["video"]["target_vmaf"], 88.0)
        self.assertEqual(request["applied_policy"]["video"]["max_height"], 1080)

    def test_operator_requested_experiment_combines_source_resolution_with_budget(self) -> None:
        request = _operator_requested_experiment(
            "Keep source resolution, do not downscale, and target 300MB per episode if possible.",
            {
                "source_size_bytes": 4_349_049_136,
                "duration_seconds": 3161.376,
                "audio_summary": [{"codec_name": "ac3", "channels": 6}],
                "resolved_policy": {
                    "video": {"encoder": "libsvtav1"},
                    "audio": {
                        "convert_to_opus_codecs": ["ac3"],
                        "surround_5_1_opus_bitrate": "256k",
                    },
                },
            },
        )

        assert request is not None
        self.assertEqual(request["request_type"], "combined_experiment")
        self.assertEqual(request["budget_label"], "300 MB per episode")
        self.assertEqual(request["scale_height"], 0)
        self.assertEqual(request["applied_policy"]["video"]["max_height"], 0)
        self.assertAlmostEqual(request["estimated_video_bitrate_kbps"], 493.0, places=1)

    def test_operator_requested_experiment_uses_current_policy_for_av1_budget_feasibility(self) -> None:
        request = _operator_requested_experiment(
            "I want to aim for 200MB per episode while losing as little fidelity as possible.",
            {
                "source_size_bytes": 4_815_446_620,
                "duration_seconds": 2660.352,
                "audio_summary": [{"codec_name": "eac3", "channels": 6}],
                "resolved_policy": {
                    "video": {"encoder": "libx264"},
                    "audio": {
                        "convert_to_opus_codecs": ["eac3"],
                        "surround_5_1_opus_bitrate": "224k",
                    },
                },
            },
            current_policy={
                "video": {"encoder": "libsvtav1"},
                "audio": {
                    "convert_to_opus_codecs": ["eac3"],
                    "surround_5_1_opus_bitrate": "224k",
                },
            },
        )

        assert request is not None
        self.assertEqual(request["feasibility"], "aggressive_but_measurable")
        self.assertFalse(request["requires_confirmation"])

    def test_folder_ai_tune_preview_uses_calibration_policy_for_operator_budget_request(self) -> None:
        captured_current_policy: list[dict[str, object]] = []

        class _StopPreview(Exception):
            pass

        def _capture_operator_request(_note: str, _sample_item: dict[str, object], **kwargs: object) -> None:
            current_policy = kwargs.get("current_policy")
            captured_current_policy.append(dict(current_policy) if isinstance(current_policy, dict) else {})
            raise _StopPreview

        deps = FolderAiTuneDeps(
            resolve_sample_host=lambda _config, _host_key: {"key": "host-1"},
            load_job_state=lambda *_args, **_kwargs: None,
            load_retryable_sample_job_state=lambda *_args, **_kwargs: None,
            sample_item=lambda *_args, **_kwargs: {
                "rel_path": "tv/show/episode.mkv",
                "resolved_policy": {"video": {"encoder": "libx264"}},
            },
            operator_requested_experiment=_capture_operator_request,
            load_calibration_state=lambda *_args, **_kwargs: {
                "policy": {"video": {"encoder": "libsvtav1"}},
            },
            recent_tuning_sessions=lambda *_args, **_kwargs: [],
            matching_request_history=lambda *_args, **_kwargs: None,
            metric_support=lambda: {},
            maybe_seed_baseline_policy=lambda *_args, **_kwargs: None,
            seed_advice_payload=lambda *_args, **_kwargs: None,
            proposal_alignment_issue=lambda *_args, **_kwargs: None,
            now_iso=lambda: "2026-04-11T15:30:00+00:00",
            proposal_signal_copy=lambda *_args, **_kwargs: "",
            proposal_context_snapshot=lambda *_args, **_kwargs: {},
            save_pending_proposal=lambda *_args, **_kwargs: None,
            pending_proposal_public_view=lambda payload: payload,
            build_tuning_runtime_toolbelt=lambda *_args, **_kwargs: {},
            review_pack_dir=lambda *_args, **_kwargs: self.root / "review-pack",
            remove_path_if_exists=lambda *_args, **_kwargs: None,
            build_multimodal_review_pack=lambda *_args, **_kwargs: None,
            multimodal_review_pack_public_view=lambda *_args, **_kwargs: None,
            tuning_advice_payload=lambda *_args, **_kwargs: {},
            load_pending_proposal=lambda *_args, **_kwargs: None,
            apply_policy_fragment=lambda current, fragment: {**current, **fragment},
            save_advice_state=lambda *_args, **_kwargs: None,
            save_job_state=lambda *_args, **_kwargs: None,
            clear_pending_proposal=lambda *_args, **_kwargs: None,
            record_tuning_session=lambda *_args, **_kwargs: "session-1",
        )

        with self.assertRaises(_StopPreview):
            folder_ai_tune_preview_action(
                self.config,
                deps,
                "tv/show",
                "target 200MB per episode",
                "host-1",
            )

        self.assertEqual(captured_current_policy, [{"video": {"encoder": "libsvtav1"}}])

    def test_folder_ai_tune_preview_and_confirm_preserve_combined_225_mb_request(self) -> None:
        saved_proposals: list[dict[str, Any]] = []
        saved_jobs: list[dict[str, Any]] = []
        base_policy = {
            "video": {
                "target_size_mb": 300.0,
                "target_runtime_minutes": 45.0,
                "target_vmaf": 95.0,
                "max_encoded_percent": 80,
                "max_height": 1080,
            }
        }
        host = HostStatus(
            key="host-1",
            label="M4 Studio",
            mode="ssh",
            priority=10,
            capabilities=["sample_calibration"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )
        operator_request = {
            "request_type": "combined_experiment",
            "operator_confirmed": True,
            "budget_label": "225 MB per episode",
            "budget_bytes": 225_000_000,
            "feasibility": "plausible",
            "applied_policy": {"video": {"max_height": 0}},
            "size_budget_request": {
                "request_type": "size_budget",
                "budget_bytes": 225_000_000,
                "feasibility": "plausible",
                "applied_policy": None,
            },
        }
        bad_seed = {
            "policy": {"video": {"target_vmaf": 94.0, "max_encoded_percent": 8}},
            "job_fields": {
                "seed_source": "ai",
                "seed_summary": "Lowered target for size.",
                "seed_diagnosis": "Bad first pass.",
                "seed_request_disposition": "honored",
                "seed_request_response": "I lowered the target.",
                "seed_feasibility_note": None,
                "seed_prompt_version": "test",
                "seed_raw_response": "{}",
                "seed_proposed_policy": {"video": {"target_vmaf": 94.0, "max_encoded_percent": 8}},
                "seed_applied_policy": {"video": {"target_vmaf": 94.0, "max_encoded_percent": 8}},
            },
        }

        deps = FolderAiTuneDeps(
            resolve_sample_host=lambda _config, _host_key: host,
            load_job_state=lambda *_args, **_kwargs: None,
            load_retryable_sample_job_state=lambda *_args, **_kwargs: None,
            sample_item=lambda *_args, **_kwargs: {
                "rel_path": "tv/show/episode.mkv",
                "source_path": str(self.root / "source" / "tv" / "show" / "episode.mkv"),
                "source_size_bytes": 4_000_000_000,
                "video_codec": "h264",
                "duration_seconds": 5279.774,
                "audio_summary": [],
                "subtitle_summary": [],
                "resolved_policy": dict(base_policy),
            },
            operator_requested_experiment=lambda *_args, **_kwargs: dict(operator_request),
            load_calibration_state=lambda *_args, **_kwargs: None,
            recent_tuning_sessions=lambda *_args, **_kwargs: [],
            matching_request_history=lambda *_args, **_kwargs: None,
            metric_support=lambda: {"vmaf": True},
            maybe_seed_baseline_policy=lambda *_args, **_kwargs: dict(bad_seed),
            seed_advice_payload=lambda *_args, **_kwargs: {
                "ok": True,
                "kind": "seed_baseline",
                "request_disposition": "honored",
                "request_response": "I lowered the target.",
                "summary": "Lowered target for size.",
                "applied_policy": {"video": {"target_vmaf": 94.0, "max_encoded_percent": 8}},
            },
            proposal_alignment_issue=proposal_alignment_issue,
            now_iso=lambda: "2026-04-25T17:30:00+00:00",
            proposal_signal_copy=lambda *_args, **_kwargs: "signal",
            proposal_context_snapshot=lambda **kwargs: dict(kwargs),
            save_pending_proposal=lambda _config, _prefix, payload: saved_proposals.append(dict(payload)),
            pending_proposal_public_view=lambda payload: payload,
            build_tuning_runtime_toolbelt=lambda *_args, **_kwargs: {},
            review_pack_dir=lambda *_args, **_kwargs: self.root / "review-pack",
            remove_path_if_exists=lambda *_args, **_kwargs: None,
            build_multimodal_review_pack=lambda *_args, **_kwargs: None,
            multimodal_review_pack_public_view=lambda *_args, **_kwargs: None,
            tuning_advice_payload=lambda *_args, **_kwargs: {},
            load_pending_proposal=lambda *_args, **_kwargs: None,
            apply_policy_fragment=lambda current, fragment: {
                **current,
                "video": {
                    **dict(current.get("video", {})),
                    **dict(fragment.get("video", {})),
                },
            },
            save_advice_state=lambda *_args, **_kwargs: None,
            save_job_state=lambda _connection, _config, _prefix, payload: saved_jobs.append(dict(payload)),
            clear_pending_proposal=lambda *_args, **_kwargs: None,
            record_tuning_session=lambda *_args, **_kwargs: "session-1",
        )

        with patch("mediaforce.web.runtime.folder_ai_tuning.inspect_prefix", return_value={"item_count": 1}):
            result = folder_ai_tune_preview_action(
                self.config,
                deps,
                "tv/show",
                "Aim for about 225 MB per episode. Keep the current resolution and make a representative test so I can judge the picture and sound.",
                "host-1",
            )

        self.assertTrue(result["ok"])
        proposal = saved_proposals[0]
        self.assertTrue(proposal["can_queue"], proposal["quality_risk_contract"])
        expected_fragment = _absolute_size_fragment(225.0, 87.996, preserve_source_resolution=True)
        self.assertEqual(
            proposal["preview_policy"],
            {"video": {**base_policy["video"], **expected_fragment["video"]}},
        )
        self.assertEqual(proposal["applied_policy"], expected_fragment)
        self.assertEqual(proposal["job_fields"]["seed_applied_policy"], expected_fragment)
        self.assertEqual(proposal["request_disposition"], "honored")

        deps.load_pending_proposal = lambda *_args, **_kwargs: proposal
        confirm_result = folder_ai_tune_confirm_action(
            self.config,
            deps,
            "tv/show",
            str(proposal["proposal_id"]),
        )

        self.assertTrue(confirm_result["ok"])
        self.assertEqual(saved_jobs[0]["policy"], proposal["preview_policy"])
        queued_ledger = saved_jobs[0]["sample_item"]["stream_budget_ledger"]
        self.assertEqual(queued_ledger["totals"]["total_target_bytes"], 225_000_000)
        self.assertEqual(queued_ledger["totals"]["remaining_video_bytes"], 217_000_000)
        self.assertEqual(queued_ledger["feasibility"]["status"], "requires_measurement")
        self.assertEqual(
            saved_jobs[0]["sample_item"]["resolved_policy"],
            saved_jobs[0]["policy"],
        )

    def test_size_budget_measurement_fragment_preserves_combined_source_resolution_request(self) -> None:
        fragment = _size_budget_measurement_fragment(
            {
                "request_type": "combined_experiment",
                "operator_confirmed": True,
                "applied_policy": {"video": {"max_height": 0}},
                "size_budget_request": {
                    "request_type": "size_budget",
                    "budget_bytes": 225_000_000,
                },
            },
            {"duration_seconds": 5279.774},
        )

        self.assertEqual(
            fragment,
            {
                "video": {
                    "max_height": 0,
                    "size_goal_schema_version": 1,
                    "size_goal_mode": "absolute",
                    "size_goal_source": "operator_note",
                    "sample_projection_tolerance_percent": 10.0,
                    "final_output_tolerance_percent": 5.0,
                    "target_size_bytes": 225_000_000,
                    "target_size_mb": 225.0,
                    "target_runtime_minutes": 87.996,
                }
            },
        )

    def test_folder_ai_retune_and_confirm_preserve_combined_225_mb_request_when_worker_is_unclear(self) -> None:
        saved_proposals: list[dict[str, Any]] = []
        saved_jobs: list[dict[str, Any]] = []
        preview_connections: list[Any] = []
        base_policy = {
            "video": {
                "target_size_mb": 300.0,
                "target_runtime_minutes": 45.0,
                "target_vmaf": 85.0,
                "max_encoded_percent": 80,
                "max_height": 1080,
            }
        }
        sample_item = {
            "rel_path": "tv/show/episode.mkv",
            "source_path": str(self.root / "source" / "tv" / "show" / "episode.mkv"),
            "source_size_bytes": 4_000_000_000,
            "video_codec": "h264",
            "duration_seconds": 5279.774,
            "audio_summary": [],
            "subtitle_summary": [],
            "resolved_policy": dict(base_policy),
        }
        operator_request = {
            "request_type": "combined_experiment",
            "operator_confirmed": True,
            "budget_label": "225 MB per episode",
            "budget_bytes": 225_000_000,
            "feasibility": "plausible",
            "applied_policy": {"video": {"max_height": 0}},
            "size_budget_request": {
                "request_type": "size_budget",
                "budget_bytes": 225_000_000,
                "feasibility": "plausible",
                "applied_policy": None,
            },
        }
        host = HostStatus(
            key="host-1",
            label="M4 Studio",
            mode="ssh",
            priority=10,
            capabilities=["sample_calibration"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )

        def load_sample_item(connection: Any, *_args: object, **_kwargs: object) -> dict[str, Any]:
            preview_connections.append(connection)
            return dict(sample_item)

        def load_recent_sessions(connection: Any, *_args: object, **_kwargs: object) -> list[dict[str, Any]]:
            preview_connections.append(connection)
            return []

        def apply_fragment(current: dict[str, Any], fragment: dict[str, Any]) -> dict[str, Any]:
            merged = dict(current)
            for section, values in fragment.items():
                if isinstance(values, dict) and isinstance(merged.get(section), dict):
                    merged[section] = {**merged[section], **values}
                else:
                    merged[section] = values
            return merged

        def unclear_tuning(*, project_root: Path, payload: dict[str, object]) -> TuningPolicyResponse:
            self.assertEqual(project_root, self.config.paths.project_root)
            self.assertEqual(payload["requested_experiment"], operator_request)
            self.assertTrue(preview_connections)
            self.assertTrue(all(connection.closed for connection in preview_connections))
            return TuningPolicyResponse(
                ok=False,
                summary="The tuning worker did not return valid structured JSON.",
                raw="attempt 1: timed out",
                prompt_version="tune-v10",
                diagnosis="The tuning worker did not complete cleanly.",
                confidence="low",
                evidence_checked=[],
                suggested_follow_up="Ask again with a concrete experiment.",
                request_disposition="unclear",
                request_response="I could not turn that note into a trustworthy next draft.",
                feasibility_note=None,
                proposed_policy={},
                toolbelt_used=[],
                self_check=None,
            )

        deps = FolderAiTuneDeps(
            resolve_sample_host=lambda _config, _host_key: host,
            load_job_state=lambda *_args, **_kwargs: None,
            load_retryable_sample_job_state=lambda *_args, **_kwargs: None,
            sample_item=load_sample_item,
            operator_requested_experiment=lambda *_args, **_kwargs: dict(operator_request),
            load_calibration_state=lambda *_args, **_kwargs: {
                "policy": dict(base_policy),
                "sample_result": {"predicted_total_size_bytes": 1_000_000_000},
            },
            recent_tuning_sessions=load_recent_sessions,
            matching_request_history=lambda *_args, **_kwargs: None,
            metric_support=lambda: {"vmaf": True},
            maybe_seed_baseline_policy=lambda *_args, **_kwargs: None,
            seed_advice_payload=lambda *_args, **_kwargs: None,
            proposal_alignment_issue=proposal_alignment_issue,
            now_iso=lambda: "2026-07-10T21:46:56+00:00",
            proposal_signal_copy=lambda *_args, **_kwargs: "signal",
            proposal_context_snapshot=lambda **kwargs: dict(kwargs),
            save_pending_proposal=lambda _config, _prefix, payload: saved_proposals.append(dict(payload)),
            pending_proposal_public_view=lambda payload: payload,
            build_tuning_runtime_toolbelt=lambda *_args, **_kwargs: {},
            review_pack_dir=lambda *_args, **_kwargs: self.root / "review-pack",
            remove_path_if_exists=lambda *_args, **_kwargs: None,
            build_multimodal_review_pack=lambda *_args, **_kwargs: None,
            multimodal_review_pack_public_view=lambda *_args, **_kwargs: None,
            tuning_advice_payload=lambda **kwargs: {
                "summary": kwargs["tuning"].summary,
                "diagnosis": kwargs["tuning"].diagnosis,
                "confidence": kwargs["tuning"].confidence,
                "request_disposition": kwargs["tuning"].request_disposition,
                "request_response": kwargs["tuning"].request_response,
            },
            load_pending_proposal=lambda *_args, **_kwargs: None,
            apply_policy_fragment=apply_fragment,
            save_advice_state=lambda *_args, **_kwargs: None,
            save_job_state=lambda _connection, _config, _prefix, payload: saved_jobs.append(dict(payload)),
            clear_pending_proposal=lambda *_args, **_kwargs: None,
            record_tuning_session=lambda *_args, **_kwargs: "session-1",
        )

        with patch("mediaforce.web.runtime.folder_ai_tuning.inspect_prefix", return_value={"item_count": 1}), patch(
                "mediaforce.web.runtime.folder_ai_tuning.retrieve_learning_context",
                return_value=[],
        ), patch(
            "mediaforce.web.runtime.folder_ai_tuning.request_note_tuning",
            side_effect=unclear_tuning,
        ):
            result = folder_ai_tune_preview_action(
                self.config,
                deps,
                "tv/show",
                "Aim for about 225 MB per episode. Keep the current resolution and make a representative test so I can judge the picture and sound.",
                "host-1",
            )

        self.assertTrue(result["ok"])
        proposal = saved_proposals[0]
        expected_fragment = _absolute_size_fragment(225.0, 87.996, preserve_source_resolution=True)
        self.assertTrue(proposal["can_queue"], proposal["quality_risk_contract"])
        self.assertEqual(proposal["request_disposition"], "honored")
        self.assertTrue(proposal["advice_payload"]["ok"])
        self.assertEqual(proposal["confidence"], "low")
        self.assertEqual(proposal["applied_policy"], expected_fragment)
        self.assertEqual(
            proposal["preview_policy"],
            {"video": {**base_policy["video"], **expected_fragment["video"]}},
        )

        deps.load_pending_proposal = lambda *_args, **_kwargs: proposal
        with patch("mediaforce.web.runtime.folder_ai_tuning.promote_learning_artifact", return_value=None):
            confirm_result = folder_ai_tune_confirm_action(
                self.config,
                deps,
                "tv/show",
                str(proposal["proposal_id"]),
            )

        self.assertTrue(confirm_result["ok"])
        self.assertEqual(saved_jobs[0]["policy"], proposal["preview_policy"])

    def test_folder_ai_tune_preview_applies_size_target_when_seed_worker_fails(self) -> None:
        saved_proposals: list[dict[str, Any]] = []
        base_policy = {"video": {"target_size_mb": 300.0, "target_vmaf": 85.0, "max_encoded_percent": 80}}
        host = HostStatus(
            key="host-1",
            label="M4 Studio",
            mode="ssh",
            priority=10,
            capabilities=["sample_calibration"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )
        operator_request = {
            "request_type": "size_budget",
            "operator_confirmed": True,
            "budget_label": "300 MB per episode",
            "budget_bytes": 300_000_000,
            "feasibility": "plausible",
            "applied_policy": None,
        }
        failed_seed = {
            "policy": dict(base_policy),
            "job_fields": {
                "seed_source": "default",
                "seed_summary": "The seed worker did not return valid structured JSON.",
                "seed_diagnosis": "The seed worker did not complete cleanly.",
                "seed_confidence": "low",
                "seed_evidence_checked": [],
                "seed_suggested_follow_up": "Ask again with a concrete experiment or artifact concern.",
                "seed_request_disposition": "unclear",
                "seed_request_response": "I could not turn that note into a trustworthy first draft.",
                "seed_feasibility_note": None,
                "seed_prompt_version": "seed-v9",
                "seed_raw_response": "attempt 1: timed out after 90s",
                "seed_proposed_policy": None,
                "seed_applied_policy": None,
            },
        }

        deps = FolderAiTuneDeps(
            resolve_sample_host=lambda _config, _host_key: host,
            load_job_state=lambda *_args, **_kwargs: None,
            load_retryable_sample_job_state=lambda *_args, **_kwargs: None,
            sample_item=lambda *_args, **_kwargs: {
                "rel_path": "tv/Lucifer/Season 2/Lucifer.S02E10.mkv",
                "source_path": str(self.root / "source" / "tv" / "Lucifer" / "Season 2" / "Lucifer.S02E10.mkv"),
                "source_size_bytes": 3_913_541_003,
                "video_codec": "hevc",
                "duration_seconds": 2686.464,
                "audio_summary": [{"channels": 6, "codec_name": "aac"}],
                "subtitle_summary": [],
                "resolved_policy": dict(base_policy),
            },
            operator_requested_experiment=lambda *_args, **_kwargs: dict(operator_request),
            load_calibration_state=lambda *_args, **_kwargs: None,
            recent_tuning_sessions=lambda *_args, **_kwargs: [],
            matching_request_history=lambda *_args, **_kwargs: None,
            metric_support=lambda: {"vmaf": True, "xpsnr": True},
            maybe_seed_baseline_policy=lambda *_args, **_kwargs: dict(failed_seed),
            seed_advice_payload=lambda _note, seed_metadata: {
                "ok": True,
                "summary": seed_metadata["job_fields"]["seed_summary"],
                "raw": seed_metadata["job_fields"]["seed_raw_response"],
                "kind": "seed_baseline",
                "operator_note": "Target 300MB per episode",
                "prompt_version": seed_metadata["job_fields"]["seed_prompt_version"],
                "request_disposition": seed_metadata["job_fields"]["seed_request_disposition"],
                "request_response": seed_metadata["job_fields"]["seed_request_response"],
                "feasibility_note": seed_metadata["job_fields"]["seed_feasibility_note"],
                "diagnosis": seed_metadata["job_fields"]["seed_diagnosis"],
                "confidence": seed_metadata["job_fields"]["seed_confidence"],
                "evidence_checked": [],
                "suggested_follow_up": seed_metadata["job_fields"]["seed_suggested_follow_up"],
                "applied_policy": seed_metadata["job_fields"]["seed_applied_policy"],
            },
            proposal_alignment_issue=proposal_alignment_issue,
            now_iso=lambda: "2026-06-04T23:54:18+00:00",
            proposal_signal_copy=lambda *_args, **_kwargs: "signal",
            proposal_context_snapshot=lambda **kwargs: dict(kwargs),
            save_pending_proposal=lambda _config, _prefix, payload: saved_proposals.append(dict(payload)),
            pending_proposal_public_view=lambda payload: payload,
            build_tuning_runtime_toolbelt=lambda *_args, **_kwargs: {},
            review_pack_dir=lambda *_args, **_kwargs: self.root / "review-pack",
            remove_path_if_exists=lambda *_args, **_kwargs: None,
            build_multimodal_review_pack=lambda *_args, **_kwargs: None,
            multimodal_review_pack_public_view=lambda *_args, **_kwargs: None,
            tuning_advice_payload=lambda *_args, **_kwargs: {},
            load_pending_proposal=lambda *_args, **_kwargs: None,
            apply_policy_fragment=lambda current, fragment: {
                **current,
                "video": {
                    **dict(current.get("video", {})),
                    **dict(fragment.get("video", {})),
                },
            },
            save_advice_state=lambda *_args, **_kwargs: None,
            save_job_state=lambda *_args, **_kwargs: None,
            clear_pending_proposal=lambda *_args, **_kwargs: None,
            record_tuning_session=lambda *_args, **_kwargs: "session-1",
        )

        with patch("mediaforce.web.runtime.folder_ai_tuning.inspect_prefix", return_value={"item_count": 18}):
            result = folder_ai_tune_preview_action(
                self.config,
                deps,
                "tv/Lucifer/Season 2",
                "Target 300MB per episode",
                "host-1",
            )

        self.assertTrue(result["ok"])
        proposal = saved_proposals[0]
        self.assertTrue(proposal["can_queue"])
        expected_fragment = _absolute_size_fragment(300.0, 44.774)
        self.assertEqual(
            proposal["preview_policy"],
            {"video": {**base_policy["video"], **expected_fragment["video"]}},
        )
        self.assertEqual(proposal["applied_policy"], expected_fragment)
        self.assertEqual(proposal["request_disposition"], "honored")
        self.assertIn("explicit request", proposal["request_response"])
        self.assertEqual(proposal["trace"]["raw_response"], "attempt 1: timed out after 90s")

    def test_folder_ai_tune_preview_overrides_seed_refusal_for_first_av1_size_budget(self) -> None:
        saved_proposals: list[dict[str, Any]] = []
        base_policy = {"video": {"target_size_mb": 300.0, "target_vmaf": 85.0, "max_encoded_percent": 80}}
        host = HostStatus(
            key="host-1",
            label="M4 Studio",
            mode="ssh",
            priority=10,
            capabilities=["sample_calibration"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )
        operator_request = {
            "request_type": "size_budget",
            "operator_confirmed": True,
            "budget_label": "300 MB per episode",
            "budget_bytes": 300 * 1024 * 1024,
            "feasibility": "plausible",
            "applied_policy": None,
        }
        refused_seed = {
            "policy": dict(base_policy),
            "job_fields": {
                "seed_source": "default",
                "seed_summary": "The requested size budget is not feasible as written.",
                "seed_diagnosis": "The worker refused the draft because the budget cannot be met cleanly.",
                "seed_confidence": "high",
                "seed_evidence_checked": [],
                "seed_suggested_follow_up": "Relax the budget or ask for a measured follow-up.",
                "seed_request_disposition": "rejected",
                "seed_request_response": "I cannot make this first sample queueable as written.",
                "seed_feasibility_note": "infeasible",
                "seed_prompt_version": "seed-v9",
                "seed_raw_response": "{\"request_disposition\":\"rejected\"}",
                "seed_proposed_policy": None,
                "seed_applied_policy": None,
            },
        }

        deps = FolderAiTuneDeps(
            resolve_sample_host=lambda _config, _host_key: host,
            load_job_state=lambda *_args, **_kwargs: None,
            load_retryable_sample_job_state=lambda *_args, **_kwargs: None,
            sample_item=lambda *_args, **_kwargs: {
                "rel_path": "tv/Lucifer/Season 2/Lucifer.S02E10.mkv",
                "source_path": str(self.root / "source" / "tv" / "Lucifer" / "Season 2" / "Lucifer.S02E10.mkv"),
                "source_size_bytes": 3_913_541_003,
                "video_codec": "hevc",
                "duration_seconds": 2686.464,
                "audio_summary": [{"channels": 6, "codec_name": "aac"}],
                "subtitle_summary": [],
                "resolved_policy": dict(base_policy),
            },
            operator_requested_experiment=lambda *_args, **_kwargs: dict(operator_request),
            load_calibration_state=lambda *_args, **_kwargs: None,
            recent_tuning_sessions=lambda *_args, **_kwargs: [],
            matching_request_history=lambda *_args, **_kwargs: None,
            metric_support=lambda: {"vmaf": True, "xpsnr": True},
            maybe_seed_baseline_policy=lambda *_args, **_kwargs: dict(refused_seed),
            seed_advice_payload=lambda _note, seed_metadata: {
                "ok": True,
                "summary": seed_metadata["job_fields"]["seed_summary"],
                "raw": seed_metadata["job_fields"]["seed_raw_response"],
                "kind": "seed_baseline",
                "operator_note": "Target 300MB per episode",
                "prompt_version": seed_metadata["job_fields"]["seed_prompt_version"],
                "request_disposition": seed_metadata["job_fields"]["seed_request_disposition"],
                "request_response": seed_metadata["job_fields"]["seed_request_response"],
                "feasibility_note": seed_metadata["job_fields"]["seed_feasibility_note"],
                "diagnosis": seed_metadata["job_fields"]["seed_diagnosis"],
                "confidence": seed_metadata["job_fields"]["seed_confidence"],
                "evidence_checked": [],
                "suggested_follow_up": seed_metadata["job_fields"]["seed_suggested_follow_up"],
                "applied_policy": seed_metadata["job_fields"]["seed_applied_policy"],
            },
            proposal_alignment_issue=proposal_alignment_issue,
            now_iso=lambda: "2026-06-04T23:54:18+00:00",
            proposal_signal_copy=lambda *_args, **_kwargs: "signal",
            proposal_context_snapshot=lambda **kwargs: dict(kwargs),
            save_pending_proposal=lambda _config, _prefix, payload: saved_proposals.append(dict(payload)),
            pending_proposal_public_view=lambda payload: payload,
            build_tuning_runtime_toolbelt=lambda *_args, **_kwargs: {},
            review_pack_dir=lambda *_args, **_kwargs: self.root / "review-pack",
            remove_path_if_exists=lambda *_args, **_kwargs: None,
            build_multimodal_review_pack=lambda *_args, **_kwargs: None,
            multimodal_review_pack_public_view=lambda *_args, **_kwargs: None,
            tuning_advice_payload=lambda *_args, **_kwargs: {},
            load_pending_proposal=lambda *_args, **_kwargs: None,
            apply_policy_fragment=lambda current, fragment: {
                **current,
                "video": {
                    **object_dict(current.get("video")),
                    **object_dict(fragment.get("video")),
                },
            },
            save_advice_state=lambda *_args, **_kwargs: None,
            save_job_state=lambda *_args, **_kwargs: None,
            clear_pending_proposal=lambda *_args, **_kwargs: None,
            record_tuning_session=lambda *_args, **_kwargs: "session-1",
        )

        with patch("mediaforce.web.runtime.folder_ai_tuning.inspect_prefix", return_value={"item_count": 18}):
            result = folder_ai_tune_preview_action(
                self.config,
                deps,
                "tv/Lucifer/Season 2",
                "Target 300MB per episode",
                "host-1",
            )

        self.assertTrue(result["ok"])
        proposal = saved_proposals[0]
        self.assertTrue(proposal["can_queue"])
        self.assertEqual(proposal["request_disposition"], "honored")
        self.assertIn("explicit request", proposal["request_response"])

    def test_first_size_budget_sample_rejects_nonpositive_video_budget(self) -> None:
        seed_job_fields = {
            "seed_request_disposition": "honored",
            "seed_applied_policy": None,
        }
        advice = _keep_first_size_budget_sample(
            advice_payload={"ok": True, "request_disposition": "honored"},
            seed_job_fields=seed_job_fields,
            trimmed_note="Target 20MB per episode",
            operator_request={
                "request_type": "size_budget",
                "operator_confirmed": True,
                "budget_bytes": 20 * 1024 * 1024,
                "estimated_audio_bytes": 24 * 1024 * 1024,
                "estimated_video_bitrate_kbps": 0.0,
                "applied_policy": None,
            },
        )

        self.assertFalse(advice["ok"])
        self.assertEqual(advice["request_disposition"], "rejected")
        self.assertEqual(seed_job_fields["seed_request_disposition"], "rejected")
        self.assertFalse(
            _proposal_can_queue(
                applied_fragment={},
                preview_policy={"video": {"target_vmaf": 85.0}},
                request_disposition=advice["request_disposition"],
                alignment_issue=None,
            )
        )

    def test_first_size_budget_fallback_requires_unblocked_plausible_request(self) -> None:
        plausible_request = {
            "request_type": "size_budget",
            "operator_confirmed": True,
            "feasibility": "plausible",
            "applied_policy": None,
        }

        self.assertTrue(_can_keep_first_size_budget_sample(plausible_request))
        self.assertFalse(
            _can_keep_first_size_budget_sample(
                {**plausible_request, "evidence_authority": "rejected_visual_result"}
            )
        )
        self.assertFalse(
            _can_keep_first_size_budget_sample(
                plausible_request,
                latest_failed_sample_job={"status": "failed", "error": "encode failed"},
            )
        )

    def test_blocking_sample_evidence_uses_semantic_policy_change(self) -> None:
        current_policy = {"video": {"target_vmaf": 95.0, "min_target_vmaf": 93.0}}
        rejected_request = {"evidence_authority": "rejected_visual_result"}

        self.assertIsNotNone(
            _blocking_sample_evidence_issue(
                operator_request=rejected_request,
                latest_failed_sample_job=None,
                current_policy=current_policy,
                preview_policy=dict(current_policy),
            )
        )
        self.assertIsNone(
            _blocking_sample_evidence_issue(
                operator_request=rejected_request,
                latest_failed_sample_job=None,
                current_policy=current_policy,
                preview_policy={"video": {"target_vmaf": 94.0, "min_target_vmaf": 92.0}},
            )
        )
    def test_folder_ai_tune_preview_enforces_followup_size_target_after_measured_miss(self) -> None:
        saved_proposals: list[dict[str, Any]] = []
        base_policy = {"video": {"target_vmaf": 95.0, "max_encoded_percent": 80}}
        host = HostStatus(
            key="host-1",
            label="M4 Studio",
            mode="ssh",
            priority=10,
            capabilities=["sample_calibration"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )
        operator_request = {
            "request_type": "size_budget",
            "operator_confirmed": True,
            "measured_size_followup": True,
            "budget_label": "300 MB per episode",
            "budget_bytes": 300_000_000,
            "target_encoded_percent": 6.9,
            "hard_size_cap": False,
            "applied_policy": None,
        }
        size_target_analysis = {
            "status": "over_target",
            "predicted_total_size_bytes": 803_322_876,
            "budget_bytes": 300_000_000,
            "predicted_to_budget_ratio": 2.6777,
        }

        def apply_fragment(current: dict[str, Any], fragment: dict[str, Any]) -> dict[str, Any]:
            merged = dict(current)
            for section, values in fragment.items():
                if isinstance(values, dict) and isinstance(merged.get(section), dict):
                    merged[section] = {**merged[section], **values}
                else:
                    merged[section] = values
            return merged

        def fake_request_note_tuning(
                *,
                project_root: Path,
                payload: dict[str, object],
                **_: object,
        ) -> TuningPolicyResponse:
            self.assertEqual(project_root, self.config.paths.project_root)
            self.assertEqual(payload["requested_experiment"], operator_request)
            return TuningPolicyResponse(
                ok=True,
                summary="Lower quality slightly for the next measured sample.",
                raw="{}",
                prompt_version="test",
                diagnosis="The last sample missed the requested size target.",
                confidence="high",
                evidence_checked=["runtime_toolbelt.size_target_analysis"],
                suggested_follow_up=None,
                request_disposition="honored",
                request_response="I tightened the next sample against the measured miss.",
                feasibility_note=None,
                proposed_policy={"video": {"target_vmaf": 88.0}},
                toolbelt_used=["size_target_analysis"],
                self_check={"status": "pass", "summary": "ok", "issues": []},
            )

        deps = FolderAiTuneDeps(
            resolve_sample_host=lambda _config, _host_key: host,
            load_job_state=lambda *_args, **_kwargs: None,
            load_retryable_sample_job_state=lambda *_args, **_kwargs: None,
            sample_item=lambda *_args, **_kwargs: {
                "rel_path": "tv/show/episode.mkv",
                "source_path": str(self.root / "source" / "tv" / "show" / "episode.mkv"),
                "source_size_bytes": 4_000_000_000,
                "video_codec": "h264",
                "duration_seconds": 2500.0,
                "audio_summary": [],
                "subtitle_summary": [],
                "resolved_policy": dict(base_policy),
            },
            operator_requested_experiment=lambda *_args, **_kwargs: dict(operator_request),
            load_calibration_state=lambda *_args, **_kwargs: {
                "policy": dict(base_policy),
                "sample_result": {
                    "predicted_total_size_bytes": 803_322_876,
                    "predicted_encode_percent": 18.47,
                    "quality_metric": "VMAF",
                    "quality_score": 95.04,
                },
            },
            recent_tuning_sessions=lambda *_args, **_kwargs: [],
            matching_request_history=lambda *_args, **_kwargs: None,
            metric_support=lambda: {"vmaf": True},
            maybe_seed_baseline_policy=lambda *_args, **_kwargs: None,
            seed_advice_payload=lambda *_args, **_kwargs: None,
            proposal_alignment_issue=proposal_alignment_issue,
            now_iso=lambda: "2026-04-25T18:30:00+00:00",
            proposal_signal_copy=lambda *_args, **_kwargs: "signal",
            proposal_context_snapshot=lambda **kwargs: dict(kwargs),
            save_pending_proposal=lambda _config, _prefix, payload: saved_proposals.append(dict(payload)),
            pending_proposal_public_view=lambda payload: payload,
            build_tuning_runtime_toolbelt=lambda *_args, **_kwargs: {
                "size_target_analysis": dict(size_target_analysis),
                "recent_sample_result": {"quality_score": 95.04},
            },
            review_pack_dir=lambda *_args, **_kwargs: self.root / "review-pack",
            remove_path_if_exists=lambda *_args, **_kwargs: None,
            build_multimodal_review_pack=lambda *_args, **_kwargs: None,
            multimodal_review_pack_public_view=lambda *_args, **_kwargs: None,
            tuning_advice_payload=lambda **kwargs: {"summary": kwargs["tuning"].summary},
            load_pending_proposal=lambda *_args, **_kwargs: None,
            apply_policy_fragment=apply_fragment,
            save_advice_state=lambda *_args, **_kwargs: None,
            save_job_state=lambda *_args, **_kwargs: None,
            clear_pending_proposal=lambda *_args, **_kwargs: None,
            record_tuning_session=lambda *_args, **_kwargs: "session-1",
        )

        with patch("mediaforce.web.runtime.folder_ai_tuning.inspect_prefix", return_value={"item_count": 1}), patch(
                "mediaforce.web.runtime.folder_ai_tuning.request_note_tuning",
                side_effect=fake_request_note_tuning,
        ):
            result = folder_ai_tune_preview_action(
                self.config,
                deps,
                "tv/show",
                "Aim for 200-300 MB per episode.",
                "host-1",
            )

        self.assertTrue(result["ok"])
        proposal = saved_proposals[0]
        self.assertTrue(proposal["can_queue"])
        self.assertEqual(proposal["preview_policy"]["video"]["max_encoded_percent"], 7)
        self.assertEqual(proposal["preview_policy"]["video"]["target_vmaf"], 88.0)
        self.assertEqual(
            proposal["applied_policy"],
            {
                "video": {
                    "target_vmaf": 88.0,
                    "min_target_vmaf": 88.0,
                    **_absolute_size_fragment(300.0, 41.667)["video"],
                    "max_encoded_percent": 7,
                }
            },
        )
        self.assertEqual(proposal["operator_request"]["applied_max_encoded_percent"], 7)
        self.assertEqual(proposal["operator_request"]["applied_policy"]["video"]["max_encoded_percent"], 7)
        self.assertEqual(
            proposal["budget_enforcement"],
            {
                "status": "enforced_after_miss",
                "size_target_analysis": size_target_analysis,
                "applied_policy": {"video": {"max_encoded_percent": 7}},
            },
        )
        self.assertEqual(
            proposal["advice_payload"]["budget_enforcement"]["applied_policy"],
            {"video": {"max_encoded_percent": 7}},
        )

    def test_folder_ai_tune_preview_keeps_quality_policy_for_first_soft_size_target_after_measured_miss(self) -> None:
        saved_proposals: list[dict[str, Any]] = []
        base_policy = {"video": {"target_vmaf": 95.0, "max_encoded_percent": 80}}
        host = HostStatus(
            key="host-1",
            label="M4 Studio",
            mode="ssh",
            priority=10,
            capabilities=["sample_calibration"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )
        operator_request = {
            "request_type": "size_budget",
            "operator_confirmed": True,
            "budget_label": "300 MB per episode",
            "budget_bytes": 300_000_000,
            "target_encoded_percent": 6.9,
            "hard_size_cap": False,
            "applied_policy": None,
        }
        size_target_analysis = {
            "status": "over_target",
            "predicted_total_size_bytes": 803_322_876,
            "budget_bytes": 300_000_000,
            "predicted_to_budget_ratio": 2.6777,
        }

        def apply_fragment(current: dict[str, Any], fragment: dict[str, Any]) -> dict[str, Any]:
            merged = dict(current)
            for section, values in fragment.items():
                if isinstance(values, dict) and isinstance(merged.get(section), dict):
                    merged[section] = {**merged[section], **values}
                else:
                    merged[section] = values
            return merged

        deps = FolderAiTuneDeps(
            resolve_sample_host=lambda _config, _host_key: host,
            load_job_state=lambda *_args, **_kwargs: None,
            load_retryable_sample_job_state=lambda *_args, **_kwargs: None,
            sample_item=lambda *_args, **_kwargs: {
                "rel_path": "tv/show/episode.mkv",
                "source_path": str(self.root / "source" / "tv" / "show" / "episode.mkv"),
                "source_size_bytes": 4_000_000_000,
                "video_codec": "h264",
                "duration_seconds": 2500.0,
                "audio_summary": [],
                "subtitle_summary": [],
                "resolved_policy": dict(base_policy),
            },
            operator_requested_experiment=lambda *_args, **_kwargs: dict(operator_request),
            load_calibration_state=lambda *_args, **_kwargs: {
                "policy": dict(base_policy),
                "sample_result": {"predicted_total_size_bytes": 803_322_876},
            },
            recent_tuning_sessions=lambda *_args, **_kwargs: [],
            matching_request_history=lambda *_args, **_kwargs: None,
            metric_support=lambda: {"vmaf": True},
            maybe_seed_baseline_policy=lambda *_args, **_kwargs: None,
            seed_advice_payload=lambda *_args, **_kwargs: None,
            proposal_alignment_issue=proposal_alignment_issue,
            now_iso=lambda: "2026-04-25T18:30:00+00:00",
            proposal_signal_copy=lambda *_args, **_kwargs: "signal",
            proposal_context_snapshot=lambda **kwargs: dict(kwargs),
            save_pending_proposal=lambda _config, _prefix, payload: saved_proposals.append(dict(payload)),
            pending_proposal_public_view=lambda payload: payload,
            build_tuning_runtime_toolbelt=lambda *_args, **_kwargs: {"size_target_analysis": dict(size_target_analysis)},
            review_pack_dir=lambda *_args, **_kwargs: self.root / "review-pack",
            remove_path_if_exists=lambda *_args, **_kwargs: None,
            build_multimodal_review_pack=lambda *_args, **_kwargs: None,
            multimodal_review_pack_public_view=lambda *_args, **_kwargs: None,
            tuning_advice_payload=lambda **kwargs: {"summary": kwargs["tuning"].summary},
            load_pending_proposal=lambda *_args, **_kwargs: None,
            apply_policy_fragment=apply_fragment,
            save_advice_state=lambda *_args, **_kwargs: None,
            save_job_state=lambda *_args, **_kwargs: None,
            clear_pending_proposal=lambda *_args, **_kwargs: None,
            record_tuning_session=lambda *_args, **_kwargs: "session-1",
        )

        with patch("mediaforce.web.runtime.folder_ai_tuning.inspect_prefix", return_value={"item_count": 1}), patch(
                "mediaforce.web.runtime.folder_ai_tuning.request_note_tuning",
                return_value=TuningPolicyResponse(
                    ok=True,
                    summary="Lower quality slightly for the next measured sample.",
                    raw="{}",
                    prompt_version="test",
                    diagnosis="The last sample missed the requested size target.",
                    confidence="high",
                    evidence_checked=["runtime_toolbelt.size_target_analysis"],
                    suggested_follow_up=None,
                    request_disposition="honored",
                    request_response="I tightened the next sample against the measured miss.",
                    feasibility_note=None,
                    proposed_policy={"video": {"target_vmaf": 88.0}},
                    toolbelt_used=["size_target_analysis"],
                    self_check={"status": "pass", "summary": "ok", "issues": []},
                ),
        ):
            result = folder_ai_tune_preview_action(
                self.config,
                deps,
                "tv/show",
                "Aim for 200-300 MB per episode.",
                "host-1",
            )

        self.assertTrue(result["ok"])
        proposal = saved_proposals[0]
        self.assertTrue(proposal["can_queue"])
        self.assertNotIn("max_encoded_percent", proposal["applied_policy"].get("video", {}))
        self.assertEqual(
            proposal["applied_policy"],
            _absolute_size_fragment(300.0, 41.667),
        )
        self.assertEqual(proposal["preview_policy"]["video"]["target_vmaf"], 95.0)
        self.assertEqual(proposal["request_disposition"], "honored")

    def test_folder_ai_tune_preview_keeps_stricter_ai_size_cap_for_hard_cap_after_measured_miss(self) -> None:
        saved_proposals: list[dict[str, Any]] = []
        base_policy = {"video": {"target_vmaf": 95.0, "max_encoded_percent": 80}}
        host = HostStatus(
            key="host-1",
            label="M4 Studio",
            mode="ssh",
            priority=10,
            capabilities=["sample_calibration"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )
        operator_request = {
            "request_type": "size_budget",
            "operator_confirmed": True,
            "budget_label": "300 MB per episode",
            "budget_bytes": 314_572_800,
            "requested_max_encoded_percent": 7.23,
            "target_encoded_percent": 7.23,
            "hard_size_cap": True,
            "applied_policy": None,
        }
        size_target_analysis = {
            "status": "over_target",
            "predicted_total_size_bytes": 803_322_876,
            "budget_bytes": 314_572_800,
            "predicted_to_budget_ratio": 2.55,
        }

        def apply_fragment(current: dict[str, Any], fragment: dict[str, Any]) -> dict[str, Any]:
            merged = dict(current)
            for section, values in fragment.items():
                if isinstance(values, dict) and isinstance(merged.get(section), dict):
                    merged[section] = {**merged[section], **values}
                else:
                    merged[section] = values
            return merged

        def fake_request_note_tuning(
                *,
                project_root: Path,
                payload: dict[str, object],
                **_: object,
        ) -> TuningPolicyResponse:
            self.assertEqual(project_root, self.config.paths.project_root)
            self.assertEqual(payload["requested_experiment"], operator_request)
            return TuningPolicyResponse(
                ok=True,
                summary="Use a tighter cap for the next measured sample.",
                raw="{}",
                prompt_version="test",
                diagnosis="The last sample missed the requested size target.",
                confidence="high",
                evidence_checked=["runtime_toolbelt.size_target_analysis"],
                suggested_follow_up=None,
                request_disposition="honored",
                request_response="I tightened the next sample against the measured miss.",
                feasibility_note=None,
                proposed_policy={"video": {"target_vmaf": 88.0, "max_encoded_percent": 5}},
                toolbelt_used=["size_target_analysis"],
                self_check={"status": "pass", "summary": "ok", "issues": []},
            )

        deps = FolderAiTuneDeps(
            resolve_sample_host=lambda _config, _host_key: host,
            load_job_state=lambda *_args, **_kwargs: None,
            load_retryable_sample_job_state=lambda *_args, **_kwargs: None,
            sample_item=lambda *_args, **_kwargs: {
                "rel_path": "tv/show/episode.mkv",
                "source_path": str(self.root / "source" / "tv" / "show" / "episode.mkv"),
                "source_size_bytes": 4_000_000_000,
                "video_codec": "h264",
                "duration_seconds": 2500.0,
                "audio_summary": [],
                "subtitle_summary": [],
                "resolved_policy": dict(base_policy),
            },
            operator_requested_experiment=lambda *_args, **_kwargs: dict(operator_request),
            load_calibration_state=lambda *_args, **_kwargs: {
                "policy": dict(base_policy),
                "sample_result": {
                    "predicted_total_size_bytes": 803_322_876,
                    "predicted_encode_percent": 18.47,
                    "quality_metric": "VMAF",
                    "quality_score": 95.04,
                },
            },
            recent_tuning_sessions=lambda *_args, **_kwargs: [],
            matching_request_history=lambda *_args, **_kwargs: None,
            metric_support=lambda: {"vmaf": True},
            maybe_seed_baseline_policy=lambda *_args, **_kwargs: None,
            seed_advice_payload=lambda *_args, **_kwargs: None,
            proposal_alignment_issue=proposal_alignment_issue,
            now_iso=lambda: "2026-04-25T18:30:00+00:00",
            proposal_signal_copy=lambda *_args, **_kwargs: "signal",
            proposal_context_snapshot=lambda **kwargs: dict(kwargs),
            save_pending_proposal=lambda _config, _prefix, payload: saved_proposals.append(dict(payload)),
            pending_proposal_public_view=lambda payload: payload,
            build_tuning_runtime_toolbelt=lambda *_args, **_kwargs: {
                "size_target_analysis": dict(size_target_analysis),
                "recent_sample_result": {"quality_score": 95.04},
            },
            review_pack_dir=lambda *_args, **_kwargs: self.root / "review-pack",
            remove_path_if_exists=lambda *_args, **_kwargs: None,
            build_multimodal_review_pack=lambda *_args, **_kwargs: None,
            multimodal_review_pack_public_view=lambda *_args, **_kwargs: None,
            tuning_advice_payload=lambda **kwargs: {"summary": kwargs["tuning"].summary},
            load_pending_proposal=lambda *_args, **_kwargs: None,
            apply_policy_fragment=apply_fragment,
            save_advice_state=lambda *_args, **_kwargs: None,
            save_job_state=lambda *_args, **_kwargs: None,
            clear_pending_proposal=lambda *_args, **_kwargs: None,
            record_tuning_session=lambda *_args, **_kwargs: "session-1",
        )

        with patch("mediaforce.web.runtime.folder_ai_tuning.inspect_prefix", return_value={"item_count": 1}), patch(
                "mediaforce.web.runtime.folder_ai_tuning.request_note_tuning",
                side_effect=fake_request_note_tuning,
        ):
            result = folder_ai_tune_preview_action(
                self.config,
                deps,
                "tv/show",
                "Hard cap at 200-300 MB per episode.",
                "host-1",
            )

        self.assertTrue(result["ok"])
        proposal = saved_proposals[0]
        self.assertEqual(proposal["preview_policy"]["video"]["max_encoded_percent"], 5)
        self.assertEqual(proposal["applied_policy"]["video"]["max_encoded_percent"], 5)
        self.assertEqual(proposal["operator_request"]["applied_max_encoded_percent"], 5)
        self.assertEqual(proposal["operator_request"]["applied_policy"]["video"]["max_encoded_percent"], 5)
        self.assertEqual(
            proposal["advice_payload"]["budget_enforcement"]["applied_policy"],
            {"video": {"max_encoded_percent": 5}},
        )

    def test_folder_ai_tune_preview_exposes_latest_failed_sample_job_to_bench(self) -> None:
        captured_payloads: list[dict[str, Any]] = []
        saved_proposals: list[dict[str, Any]] = []
        failed_job = {
            "job_id": "failed-sample-1",
            "status": "failed",
            "lane": "sample",
            "mode": "sample",
            "action": "ai_tune",
            "host": {"key": "host-1", "label": "M4 Studio"},
            "notes": "Target 14% while preserving quality.",
            "policy": {"video": {"target_vmaf": 97.0, "max_encoded_percent": 14}},
            "sample_item": {
                "library_item_id": 7,
                "rel_path": "tv/show/episode.mkv",
                "source_path": str(self.root / "source" / "tv" / "show" / "episode.mkv"),
                "source_size_bytes": 3_000_000_000,
                "video_codec": "h264",
                "duration_seconds": 2400.0,
                "audio_summary": [{"codec_name": "eac3", "channels": 6}],
                "subtitle_summary": [],
            },
            "result": None,
            "error": "Error: Failed to find a suitable crf",
            "created_at": "2026-04-22T14:00:00+00:00",
            "started_at": "2026-04-22T14:00:05+00:00",
            "finished_at": "2026-04-22T14:03:00+00:00",
            "updated_at": "2026-04-22T14:03:00+00:00",
        }
        sample_item = {
            "library_item_id": 7,
            "rel_path": "tv/show/episode.mkv",
            "source_path": str(self.root / "source" / "tv" / "show" / "episode.mkv"),
            "source_size_bytes": 3_000_000_000,
            "video_codec": "h264",
            "duration_seconds": 2400.0,
            "audio_summary": [{"codec_name": "eac3", "channels": 6}],
            "subtitle_summary": [],
            "resolved_policy": {"video": {"target_vmaf": 95.0, "max_encoded_percent": 30}},
        }
        host = HostStatus(
            key="host-1",
            label="M4 Studio",
            mode="ssh",
            priority=10,
            capabilities=["sample_calibration"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )

        def fake_request_note_tuning(
                *,
                project_root: Path,
                payload: dict[str, object],
                **_: object,
        ) -> TuningPolicyResponse:
            self.assertEqual(project_root, self.config.paths.project_root)
            captured_payloads.append(payload)
            return TuningPolicyResponse(
                ok=True,
                summary="Relax the target enough to avoid the same CRF dead end.",
                raw="{}",
                prompt_version="test",
                diagnosis="The last sample could not satisfy quality and size together.",
                confidence="medium",
                evidence_checked=["latest_failed_sample_job.error"],
                suggested_follow_up=None,
                request_disposition="honored",
                request_response="I adjusted the next draft based on the failed CRF search.",
                feasibility_note=None,
                proposed_policy={"video": {"target_vmaf": 93.0, "max_encoded_percent": 18}},
                toolbelt_used=["latest_failed_sample_job"],
                self_check={"status": "pass", "summary": "ok", "issues": []},
            )

        deps = FolderAiTuneDeps(
            resolve_sample_host=lambda _config, _host_key: host,
            load_job_state=lambda *_args, **_kwargs: None,
            load_retryable_sample_job_state=lambda *_args, **_kwargs: None,
            sample_item=lambda *_args, **_kwargs: dict(sample_item),
            operator_requested_experiment=lambda *_args, **_kwargs: None,
            load_calibration_state=lambda *_args, **_kwargs: {
                "policy": {"video": {"target_vmaf": 95.0, "max_encoded_percent": 30}},
                "sample_result": {"quality_score": 95.1, "predicted_encode_percent": 25.0},
            },
            recent_tuning_sessions=lambda *_args, **_kwargs: [],
            matching_request_history=lambda *_args, **_kwargs: None,
            metric_support=lambda: {"vmaf": True},
            maybe_seed_baseline_policy=lambda *_args, **_kwargs: None,
            seed_advice_payload=lambda *_args, **_kwargs: None,
            proposal_alignment_issue=lambda *_args, **_kwargs: None,
            now_iso=lambda: "2026-04-22T14:10:00+00:00",
            proposal_signal_copy=lambda *_args, **_kwargs: "The bench translated your note into a draft.",
            proposal_context_snapshot=lambda **kwargs: dict(kwargs),
            save_pending_proposal=lambda _config, _prefix, payload: saved_proposals.append(dict(payload)),
            pending_proposal_public_view=lambda payload: payload,
            build_tuning_runtime_toolbelt=lambda *_args, **_kwargs: {"recent_sample_result": {"quality_score": 95.1}},
            review_pack_dir=lambda *_args, **_kwargs: self.root / "review-pack",
            remove_path_if_exists=lambda *_args, **_kwargs: None,
            build_multimodal_review_pack=lambda *_args, **_kwargs: None,
            multimodal_review_pack_public_view=lambda *_args, **_kwargs: None,
            tuning_advice_payload=lambda **kwargs: {"summary": kwargs["tuning"].summary},
            load_pending_proposal=lambda *_args, **_kwargs: None,
            apply_policy_fragment=lambda current, fragment: {**current, **fragment},
            save_advice_state=lambda *_args, **_kwargs: None,
            save_job_state=lambda *_args, **_kwargs: None,
            clear_pending_proposal=lambda *_args, **_kwargs: None,
            record_tuning_session=lambda *_args, **_kwargs: "session-1",
            load_latest_failed_sample_job_state=lambda *_args, **_kwargs: dict(failed_job),
        )

        with patch("mediaforce.web.runtime.folder_ai_tuning.inspect_prefix", return_value={"item_count": 1}), patch(
            "mediaforce.web.runtime.folder_ai_tuning.request_note_tuning",
            side_effect=fake_request_note_tuning,
        ):
            result = folder_ai_tune_preview_action(
                self.config,
                deps,
                "tv/show",
                "Fix the failed sample without going much larger.",
                "host-1",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(captured_payloads), 1)
        latest_failed = captured_payloads[0]["latest_failed_sample_job"]
        self.assertIsInstance(latest_failed, dict)
        assert isinstance(latest_failed, dict)
        self.assertEqual(latest_failed["job_id"], "failed-sample-1")
        self.assertEqual(latest_failed["error"], "Error: Failed to find a suitable crf")
        self.assertNotIn("source_path", latest_failed["sample_item"])
        toolbelt = captured_payloads[0]["runtime_toolbelt"]
        self.assertIsInstance(toolbelt, dict)
        assert isinstance(toolbelt, dict)
        self.assertEqual(toolbelt["latest_failed_sample_job"], latest_failed)
        self.assertEqual(saved_proposals[0]["latest_failed_sample_job"], latest_failed)
        trace = saved_proposals[0]["trace"]
        self.assertIsInstance(trace, dict)
        assert isinstance(trace, dict)
        context = trace["context"]
        self.assertIsInstance(context, dict)
        assert isinstance(context, dict)
        self.assertEqual(
            context["latest_failed_sample_job"],
            latest_failed,
        )

    def test_operator_requested_experiment_leaves_exploratory_note_unconfirmed(self) -> None:
        request = _operator_requested_experiment("Can we try to target 85 VMAF instead? Will that help?")

        assert request is not None
        self.assertFalse(request["operator_confirmed"])

    def test_operator_requested_experiment_marks_directive_question_budget_confirmed(self) -> None:
        request = _operator_requested_experiment(
            "Target 300MB per episode?",
            {
                "source_size_bytes": 3_519_516_042,
                "duration_seconds": 2561.35,
                "audio_summary": [{"channels": 6}],
                "resolved_policy": {"audio": {"surround_5_1_opus_bitrate": "224k"}},
            },
        )

        assert request is not None
        self.assertEqual(request["request_type"], "size_budget")
        self.assertTrue(request["operator_confirmed"])

    def test_operator_requested_experiment_marks_can_you_directive_confirmed(self) -> None:
        request = _operator_requested_experiment(
            "Can you target 300MB per episode?",
            {
                "source_size_bytes": 3_519_516_042,
                "duration_seconds": 2561.35,
                "audio_summary": [{"channels": 6}],
                "resolved_policy": {"audio": {"surround_5_1_opus_bitrate": "224k"}},
            },
        )

        assert request is not None
        self.assertEqual(request["request_type"], "size_budget")
        self.assertTrue(request["operator_confirmed"])

    def test_operator_requested_experiment_leaves_want_to_understand_unconfirmed(self) -> None:
        request = _operator_requested_experiment(
            "I want to understand if 300MB per episode is realistic.",
            {
                "source_size_bytes": 3_519_516_042,
                "duration_seconds": 2561.35,
                "audio_summary": [{"channels": 6}],
                "resolved_policy": {"audio": {"surround_5_1_opus_bitrate": "224k"}},
            },
        )

        assert request is not None
        self.assertEqual(request["request_type"], "size_budget")
        self.assertFalse(request["operator_confirmed"])

    def test_operator_requested_experiment_detects_combined_budget_and_vmaf_request(self) -> None:
        request = _operator_requested_experiment(
            "I really want 200MB and VMAF of around 85.",
            {
                "source_size_bytes": 4_480_523_243,
                "duration_seconds": 2645.248,
                "audio_summary": [{"channels": 6}],
                "resolved_policy": {"audio": {"surround_5_1_opus_bitrate": "224k"}},
            },
        )

        assert request is not None
        self.assertEqual(request["request_type"], "combined_experiment")
        self.assertEqual(request["metric"], "vmaf")
        self.assertEqual(request["target"], 85.0)
        self.assertEqual(request["budget_label"], "200 MB per episode")
        self.assertEqual(request["applied_policy"]["video"]["target_vmaf"], 85.0)
        self.assertNotIn("max_encoded_percent", request["applied_policy"]["video"])

    def test_measured_size_budget_policy_fragment_waits_for_confirmed_soft_target_after_miss(self) -> None:
        fragment = measured_size_budget_policy_fragment(
            operator_request={
                "request_type": "size_budget",
                "target_encoded_percent": 7.23,
                "hard_size_cap": False,
            },
            size_target_analysis={"status": "over_target"},
        )

        self.assertEqual(fragment, {})

    def test_measured_size_budget_policy_fragment_enforces_followup_soft_target_after_miss(self) -> None:
        fragment = measured_size_budget_policy_fragment(
            operator_request={
                "request_type": "size_budget",
                "target_encoded_percent": 7.23,
                "operator_confirmed": True,
                "measured_size_followup": True,
                "hard_size_cap": False,
            },
            size_target_analysis={"status": "over_target"},
        )

        self.assertEqual(fragment, {"video": {"max_encoded_percent": 7}})

    def test_measured_size_budget_policy_fragment_does_not_enforce_new_soft_target_after_miss(self) -> None:
        fragment = measured_size_budget_policy_fragment(
            operator_request={
                "request_type": "size_budget",
                "target_encoded_percent": 7.23,
                "operator_confirmed": True,
                "hard_size_cap": False,
            },
            size_target_analysis={"status": "over_target"},
        )

        self.assertEqual(fragment, {})

    def test_measured_size_budget_policy_fragment_ignores_unconfirmed_followup_after_miss(self) -> None:
        fragment = measured_size_budget_policy_fragment(
            operator_request={
                "request_type": "size_budget",
                "target_encoded_percent": 7.23,
                "operator_confirmed": False,
                "measured_size_followup": True,
                "hard_size_cap": False,
            },
            size_target_analysis={"status": "over_target"},
        )

        self.assertEqual(fragment, {})

    def test_measured_size_budget_policy_fragment_ignores_unconfirmed_hard_cap_after_miss(self) -> None:
        fragment = measured_size_budget_policy_fragment(
            operator_request={
                "request_type": "size_budget",
                "requested_max_encoded_percent": 7.23,
                "operator_confirmed": False,
                "hard_size_cap": True,
            },
            size_target_analysis={"status": "over_target"},
        )

        self.assertEqual(fragment, {})

    def test_measured_size_budget_policy_fragment_enforces_explicit_hard_cap_after_miss(self) -> None:
        fragment = measured_size_budget_policy_fragment(
            operator_request={
                "request_type": "size_budget",
                "requested_max_encoded_percent": 7.23,
                "operator_confirmed": True,
                "hard_size_cap": True,
            },
            size_target_analysis={"status": "over_target"},
        )

        self.assertEqual(fragment, {"video": {"max_encoded_percent": 7}})

    def test_measured_size_budget_policy_fragment_waits_for_measured_miss(self) -> None:
        fragment = measured_size_budget_policy_fragment(
            operator_request={
                "request_type": "size_budget",
                "requested_max_encoded_percent": 7.23,
            },
            size_target_analysis={"status": "under_target"},
        )

        self.assertEqual(fragment, {})

    def test_operator_requested_experiment_uses_deterministic_budget_when_model_unavailable(self) -> None:
        with patch(
            "mediaforce.web.runtime.folder_tuning_advice.request_operator_note_parse",
            return_value=None,
        ) as note_parser:
            request = _operator_requested_experiment(
                "Can you target 300MB per episode?",
                {
                    "source_size_bytes": 3_519_516_042,
                    "duration_seconds": 2561.35,
                    "audio_summary": [{"channels": 6}],
                    "resolved_policy": {"audio": {"surround_5_1_opus_bitrate": "224k"}},
                },
            )

        assert request is not None
        self.assertEqual(request["request_type"], "size_budget")
        self.assertTrue(request["operator_confirmed"])
        note_parser.assert_not_called()

    def test_operator_requested_experiment_uses_deterministic_combined_request_without_model(self) -> None:
        with patch(
                "mediaforce.web.runtime.folder_tuning_advice.request_operator_note_parse",
                return_value={
                    "summary": "Direct request to target around 300MB while dropping to 1080P",
                    "intent_type": "direct_request",
                    "request_type": "size_budget",
                    "operator_confirmed": True,
                    "metric": None,
                    "metric_target": None,
                    "size_budget_value": 300,
                    "size_budget_unit": "mb",
                    "scale_height": None,
                    "black_bar_handling": None,
                    "crop": None,
                    "reasoning_note": "Structured parse dropped the explicit scale target.",
                },
        ) as note_parser:
            request = _operator_requested_experiment(
                "Can we drop this to 1080P and try to hit around 300MB?",
                {
                    "source_size_bytes": 11_046_780_928,
                    "duration_seconds": 3602.0,
                    "audio_summary": [{"channels": 6}],
                    "resolved_policy": {"audio": {"surround_5_1_opus_bitrate": "224k"}},
                },
            )

        assert request is not None
        self.assertEqual(request["request_type"], "combined_experiment")
        self.assertEqual(request["scale_height"], 1080)
        self.assertEqual(request["applied_policy"]["video"]["max_height"], 1080)
        self.assertEqual(request["budget_label"], "300 MB per episode")
        note_parser.assert_not_called()

    def test_build_seed_policy_payload_carries_requested_experiment(self) -> None:
        payload = _build_seed_policy_payload(
            prefix="tv/House/Season 2",
            user_note="Can we try to target 85 VMAF instead? Will that help?",
            base_policy={"video": {"target_vmaf": 90.0, "min_target_vmaf": 88.0}},
            sample_item={
                "rel_path": "tv/House/Season 2/House.S02E06.mkv",
                "source_size_bytes": 4_815_446_620,
                "video_codec": "h264",
                "video_bitrate": None,
                "width": 1920,
                "height": 1080,
                "duration_seconds": 2660.352,
                "audio_summary": [],
                "subtitle_summary": [],
                "recommendation": None,
                "recommendation_reason": None,
            },
            summary={
                "item_count": 24,
                "total_size_bytes": 100_570_881_417,
                "statuses": {"discovered": 24},
                "video_codecs": {"h264": 24},
                "audio_codecs": {"eac3:6": 24},
                "seasons": ["Season 2"],
            },
            metric_support={"vmaf": True, "xpsnr": True, "ssim": True, "psnr": True},
        )

        request = payload["requested_experiment"]
        assert request is not None
        self.assertEqual(request["metric"], "vmaf")
        self.assertEqual(request["target"], 85.0)
        self.assertEqual(request["applied_policy"]["video"]["target_vmaf"], 85.0)

    def test_build_seed_policy_payload_carries_latest_failed_sample_job(self) -> None:
        failed_job = {
            "job_id": "failed-sample-1",
            "status": "failed",
            "action": "baseline",
            "error": "Error: Failed to find a suitable crf",
            "policy": {"video": {"target_vmaf": 97.0, "max_encoded_percent": 14}},
        }

        payload = _build_seed_policy_payload(
            prefix="tv/Sanctuary",
            user_note="What can we do about the failure?",
            base_policy={"video": {"target_vmaf": 95.0, "max_encoded_percent": 30}},
            sample_item={
                "rel_path": "tv/Sanctuary/S01E01.mkv",
                "source_size_bytes": 3_000_000_000,
                "video_codec": "h264",
                "video_bitrate": None,
                "width": 1920,
                "height": 1080,
                "duration_seconds": 2600.0,
                "audio_summary": [],
                "subtitle_summary": [],
                "recommendation": None,
                "recommendation_reason": None,
            },
            summary={
                "item_count": 13,
                "total_size_bytes": 39_000_000_000,
                "statuses": {"discovered": 13},
                "video_codecs": {"h264": 13},
                "audio_codecs": {"aac:2": 13},
                "seasons": ["Season 1"],
            },
            metric_support={"vmaf": True, "xpsnr": True, "ssim": True, "psnr": True},
            latest_failed_sample_job=failed_job,
        )

        self.assertEqual(payload["latest_failed_sample_job"], failed_job)

    def test_build_seed_policy_payload_marks_repeated_operator_confirmation(self) -> None:
        payload = _build_seed_policy_payload(
            prefix="tv/House/Season 5",
            user_note="I really want 200MB and VMAF of around 85.",
            base_policy={"video": {"target_vmaf": 94.5, "min_target_vmaf": 93.0, "max_encoded_percent": 75}},
            sample_item={
                "rel_path": "tv/House/Season 5/House.S05E22.mkv",
                "source_size_bytes": 4_480_523_243,
                "video_codec": "h264",
                "video_bitrate": None,
                "width": 1920,
                "height": 1080,
                "duration_seconds": 2645.248,
                "audio_summary": [{"channels": 6}],
                "subtitle_summary": [],
                "recommendation": None,
                "recommendation_reason": None,
                "resolved_policy": {"audio": {"surround_5_1_opus_bitrate": "224k"}},
            },
            summary={
                "item_count": 24,
                "total_size_bytes": 76_892_875_827,
                "statuses": {"discovered": 24},
                "video_codecs": {"h264": 24},
                "audio_codecs": {"eac3:6": 24},
                "seasons": ["Season 5"],
            },
            metric_support={"vmaf": True, "xpsnr": True, "ssim": True, "psnr": True},
            recent_sessions_payload=[
                {
                    "note": "Try to hit around 200MB an episode. VMAF of around 85.",
                    "request_disposition": "softened",
                    "summary": "Too aggressive for a safe cold start.",
                    "created_at": "2026-04-04T01:29:02+00:00",
                }
            ],
            learning_context_payload=[
                {
                    "evidence_authority": "approved_visual_result",
                    "summary": "A prior 1080p AV1 result was visually approved near 250 MiB.",
                }
            ],
        )

        repeat_signal = payload["operator_repeat_signal"]
        assert repeat_signal is not None
        self.assertEqual(repeat_signal["repeat_count"], 2)
        self.assertEqual(repeat_signal["previous_softened_count"], 1)
        self.assertEqual(payload["retrieved_memory"][0]["evidence_authority"], "approved_visual_result")

    def test_build_seed_policy_payload_reuses_stored_operator_note_parse(self) -> None:
        sample_item = {
            "rel_path": "tv/House/Season 5/House.S05E22.mkv",
            "source_size_bytes": 4_480_523_243,
            "video_codec": "h264",
            "video_bitrate": None,
            "width": 1920,
            "height": 1080,
            "duration_seconds": 2645.248,
            "audio_summary": [{"channels": 6}],
            "subtitle_summary": [],
            "recommendation": None,
            "recommendation_reason": None,
            "resolved_policy": {"audio": {"surround_5_1_opus_bitrate": "224k"}},
        }
        current_request = _operator_requested_experiment(
            "I really want 200MB and VMAF of around 85.",
            sample_item,
        )
        assert current_request is not None

        with patch("mediaforce.web.runtime.folder_tuning_advice.request_operator_note_parse", return_value=None):
            payload = _build_seed_policy_payload(
                prefix="tv/House/Season 5",
                user_note="I really want 200MB and VMAF of around 85.",
                base_policy={"video": {"target_vmaf": 94.5, "min_target_vmaf": 93.0, "max_encoded_percent": 75}},
                sample_item=sample_item,
                summary={
                    "item_count": 24,
                    "total_size_bytes": 76_892_875_827,
                    "statuses": {"discovered": 24},
                    "video_codecs": {"h264": 24},
                    "audio_codecs": {"eac3:6": 24},
                    "seasons": ["Season 5"],
                },
                metric_support={"vmaf": True, "xpsnr": True, "ssim": True, "psnr": True},
                recent_sessions_payload=[
                    {
                        "note": "Try to hit around 200MB an episode. VMAF of around 85.",
                        "operator_note_parse": current_request["operator_note_parse"],
                        "request_disposition": "softened",
                        "summary": "Too aggressive for a safe cold start.",
                        "created_at": "2026-04-04T01:29:02+00:00",
                    }
                ],
                requested_experiment=current_request,
            )

        repeat_signal = payload["operator_repeat_signal"]
        assert repeat_signal is not None
        self.assertEqual(repeat_signal["repeat_count"], 2)
        self.assertEqual(repeat_signal["previous_softened_count"], 1)

    def test_maybe_seed_baseline_policy_honors_repeated_explicit_request(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            record_tuning_session(
                connection,
                prefix="tv/House/Season 5",
                note="Try to hit around 200MB an episode. VMAF of around 85.",
                response={
                    "summary": "Too aggressive for a safe cold start.",
                    "diagnosis": "Prior request was softened.",
                    "confidence": "medium",
                    "raw": json.dumps({"request_disposition": "softened"}),
                },
                applied_policy={},
                toolbelt={},
                created_at="2026-04-04T01:29:02+00:00",
            )

            mocked_response = SeedPolicyResponse(
                ok=True,
                summary="Mildly smaller first-pass draft.",
                raw=json.dumps({"request_disposition": "softened"}),
                prompt_version=SEED_PROMPT_VERSION,
                diagnosis="The request is too aggressive for a cold start.",
                confidence="medium",
                evidence_checked=["base_policy"],
                suggested_follow_up="Measure one sample first.",
                request_disposition="softened",
                request_response="I softened that request.",
                feasibility_note="Cold-start budgets are rough.",
                proposed_policy={"video": {"target_vmaf": 94.0}},
            )

            with patch(
                    "mediaforce.web.runtime.folder_tuning_advice.inspect_prefix",
                    return_value={
                        "item_count": 24,
                        "total_size_bytes": 76_892_875_827,
                        "statuses": {"discovered": 24},
                        "video_codecs": {"h264": 24},
                        "audio_codecs": {"eac3:6": 24},
                        "seasons": ["Season 5"],
                    },
            ), patch(
                "mediaforce.web.runtime.folder_tuning_advice.request_seed_policy",
                return_value=mocked_response,
            ):
                metadata = _maybe_seed_baseline_policy(
                    config=self.config,
                    prefix="tv/House/Season 5",
                    action="baseline",
                    user_note="I really want 200MB and VMAF of around 85.",
                    base_policy={"video": {"target_vmaf": 94.5, "min_target_vmaf": 93.0, "max_encoded_percent": 75}},
                    sample_item={
                        "rel_path": "tv/House/Season 5/House.S05E22.mkv",
                        "source_size_bytes": 4_480_523_243,
                        "video_codec": "h264",
                        "video_bitrate": None,
                        "width": 1920,
                        "height": 1080,
                        "duration_seconds": 2645.248,
                        "audio_summary": [{"codec_name": "eac3", "channels": 6}],
                        "subtitle_summary": [],
                        "resolved_policy": {
                            "audio": {
                                "convert_to_opus_codecs": ["eac3"],
                                "surround_5_1_opus_bitrate": "224k",
                            }
                        },
                    },
                    existing_calibration=None,
                    connection=connection,
                )

        assert metadata is not None
        job_fields = metadata["job_fields"]
        self.assertEqual(job_fields["seed_request_disposition"], "honored")
        self.assertEqual(job_fields["seed_applied_policy"]["video"]["target_vmaf"], 85.0)
        self.assertNotIn("max_encoded_percent", job_fields["seed_applied_policy"]["video"])

    def test_maybe_seed_baseline_policy_sends_latest_failed_sample_job(self) -> None:
        captured_payloads: list[dict[str, Any]] = []
        failed_job = {
            "job_id": "failed-sample-1",
            "status": "failed",
            "action": "baseline",
            "error": "Error: Failed to find a suitable crf",
            "policy": {"video": {"target_vmaf": 97.0, "max_encoded_percent": 14}},
        }

        def fake_request_seed_policy(
                *,
                project_root: Path,
                payload: dict[str, object],
                **_: object,
        ) -> SeedPolicyResponse:
            self.assertEqual(project_root, self.config.paths.project_root)
            self.assertFalse(connection.in_transaction())
            captured_payloads.append(payload)
            return SeedPolicyResponse(
                ok=True,
                summary="Use the failed sample details to avoid the same CRF dead end.",
                raw="{}",
                prompt_version=SEED_PROMPT_VERSION,
                diagnosis="The prior sample missed the size and quality constraints together.",
                confidence="medium",
                evidence_checked=["latest_failed_sample_job.error"],
                suggested_follow_up=None,
                request_disposition="honored",
                request_response="I adjusted this from the failed sample evidence.",
                feasibility_note=None,
                proposed_policy={"video": {"target_vmaf": 93.0, "max_encoded_percent": 18}},
            )

        with open_db(self.config.paths.db_path) as connection, patch(
                "mediaforce.web.runtime.folder_tuning_advice.inspect_prefix",
                return_value={
                    "item_count": 13,
                    "total_size_bytes": 39_000_000_000,
                    "statuses": {"discovered": 13},
                    "video_codecs": {"h264": 13},
                    "audio_codecs": {"aac:2": 13},
                    "seasons": ["Season 1"],
                },
        ), patch(
            "mediaforce.web.runtime.folder_tuning_advice.request_seed_policy",
            side_effect=fake_request_seed_policy,
        ):
            metadata = _maybe_seed_baseline_policy(
                config=self.config,
                prefix="tv/Sanctuary",
                action="baseline",
                user_note="What can we do about the failure?",
                base_policy={"video": {"target_vmaf": 95.0, "max_encoded_percent": 30}},
                sample_item={
                    "rel_path": "tv/Sanctuary/S01E01.mkv",
                    "source_size_bytes": 3_000_000_000,
                    "video_codec": "h264",
                    "video_bitrate": None,
                    "width": 1920,
                    "height": 1080,
                    "duration_seconds": 2600.0,
                    "audio_summary": [],
                    "subtitle_summary": [],
                },
                existing_calibration=None,
                connection=connection,
                latest_failed_sample_job=failed_job,
            )

        assert metadata is not None
        self.assertEqual(len(captured_payloads), 1)
        self.assertEqual(captured_payloads[0]["latest_failed_sample_job"], failed_job)
        self.assertEqual(metadata["job_fields"]["seed_evidence_checked"], ["latest_failed_sample_job.error"])

    def test_request_note_tuning_carries_explicit_budget_into_honored_risky_draft(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "You asked for the aggressive budget explicitly, so I kept that experiment and lowered the quality target to match.",
                    "request_disposition": "honored_with_risk",
                    "summary": "Risky size-budget draft.",
                    "diagnosis": "This needs a hard size cap plus much softer video targets.",
                    "confidence": "medium",
                    "evidence_checked": ["requested_experiment", "runtime_toolbelt.recent_sample_result"],
                    "suggested_follow_up": "If this still misses the budget, stop and reassess.",
                    "feasibility_note": "This is operator-confirmed and high risk.",
                    "policy": {"video": {"target_vmaf": 89.0, "min_target_vmaf": 87.5}},
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": _codex_lab_jsonl(stdout), "stderr": ""})()

        with patch("mediaforce.advisor._run_codex_lab_process_impl", side_effect=fake_run), patch(
            "mediaforce.advisor._run_tune_self_check",
            return_value={
                "status": "fail",
                "summary": "The bench explanation has caveats, but the explicit experiment is still coherent enough to review.",
                "issues": ["The fallback wording is weaker than the draft policy itself."],
                "source": "deterministic",
            },
        ):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-3",
                    "operator_note": "Keep pushing. I want to see under 300MB.",
                    "requested_experiment": {
                        "request_type": "size_budget",
                        "applied_policy": {"video": {"max_encoded_percent": 8}},
                    },
                    "policy": {
                        "video": {
                            "target_vmaf": 94.5,
                            "min_target_vmaf": 93.0,
                            "max_encoded_percent": 55,
                        }
                    },
                    "runtime_toolbelt": {"recent_sample_result": {"predicted_encode_percent": 20.28}},
                    "retrieved_memory": [],
                },
            )

        self.assertTrue(response.ok)
        assert response.proposed_policy is not None
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "warn")
        self.assertEqual(response.proposed_policy["video"]["target_vmaf"], 89.0)
        self.assertEqual(response.proposed_policy["video"]["min_target_vmaf"], 87.5)
        self.assertEqual(response.proposed_policy["video"]["max_encoded_percent"], 8)

    def test_clear_folder_tuning_state_removes_thread_and_artifacts(self) -> None:
        prefix = "tv/House/Season 2"
        review_root = self.config.paths.review_dir / "run-123"
        preview_path = review_root / "item-00" / "encoded-01.mp4"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_text("preview")
        review_pack_dir = _review_pack_dir(self.config, prefix, "request-1")
        review_pack_image = review_pack_dir / "review-video-moment-01.png"
        review_pack_image.parent.mkdir(parents=True, exist_ok=True)
        review_pack_image.write_text("pack")
        calibration_path = _calibration_file(self.config, prefix)
        calibration_path.parent.mkdir(parents=True, exist_ok=True)
        calibration_path.write_text(
            json.dumps(
                {
                    "mode": "sample",
                    "job_id": "job-1",
                    "preview_clips": [{"path": "/review-media/run-123/item-00/encoded-01.mp4"}],
                    "source_clips": [],
                    "compare_clips": [],
                }
            )
        )
        _advice_file(self.config, prefix).write_text(json.dumps({"summary": "Bench summary"}))
        _proposal_file(self.config, prefix).write_text(json.dumps({"proposal_id": "prop-1"}))

        with open_db(self.config.paths.db_path) as connection:
            session_id = record_tuning_session(
                connection,
                prefix=prefix,
                note="try smaller",
                response={
                    "summary": "Draft smaller",
                    "diagnosis": "Need a leaner sample",
                    "confidence": "medium",
                    "evidence_checked": [],
                    "prompt_version": "tune-v2",
                    "raw": "{}",
                },
                applied_policy={"video": {"target_vmaf": 90.0}},
                toolbelt={},
                created_at="2026-03-30T00:00:00+00:00",
            )
            artifact_path = self.root / "learned-memory" / "artifact.md"
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text("artifact")
            connection.execute(
                learning_artifacts.insert().values(
                    artifact_id="artifact-1",
                    session_id=session_id,
                    prefix=prefix,
                    title="artifact",
                    artifact_path=str(artifact_path),
                    summary="summary",
                    tags_json="[]",
                    created_at="2026-03-30T00:00:00+00:00",
                    updated_at="2026-03-30T00:00:00+00:00",
                )
            )
            connection.execute(
                calibration_jobs.insert().values(
                    job_id="job-1",
                    prefix=prefix,
                    status="failed",
                    lane="sample",
                    action="ai_tune",
                    host_json="{}",
                    notes="note",
                    policy_json="{}",
                    sample_item_json="{}",
                    created_at="2026-03-30T00:00:00+00:00",
                    updated_at="2026-03-30T00:00:00+00:00",
                )
            )

            result = _clear_folder_tuning_state(connection, config=self.config, prefix=prefix)

            self.assertTrue(result["ok"])
            self.assertEqual(
                connection.execute(
                    select(func.count()).select_from(tuning_sessions).where(tuning_sessions.c.prefix == prefix)
                ).scalar_one(),
                0,
            )
            self.assertEqual(
                connection.execute(
                    select(func.count()).select_from(calibration_jobs).where(calibration_jobs.c.prefix == prefix)
                ).scalar_one(),
                0,
            )

        self.assertFalse(calibration_path.exists())
        self.assertFalse(_advice_file(self.config, prefix).exists())
        self.assertFalse(_proposal_file(self.config, prefix).exists())
        self.assertFalse(preview_path.exists())
        self.assertFalse(review_root.exists())
        self.assertFalse(review_pack_image.exists())
        self.assertFalse(review_pack_dir.exists())
        self.assertFalse(_review_pack_dir(self.config, prefix).exists())
        self.assertFalse((self.root / "learned-memory" / "artifact.md").exists())

    def test_multimodal_review_pack_public_view_uses_review_media_urls(self) -> None:
        pack_dir = _review_pack_dir(self.config, "tv/House/Season 2", "request-1")
        image_path = pack_dir / "review-video-moment-01.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_text("png")

        public_view = _multimodal_review_pack_public_view(
            self.config,
            {
                "artifacts": [
                    {
                        "kind": "video_contact_sheet",
                        "label": "Review moment 1",
                        "detail": "Top row source, bottom row preview.",
                    }
                ],
                "images": [str(image_path)],
                "audio_plan": {"summary": "Primary track eac3 is planned for Opus at 224k."},
            },
        )

        assert public_view is not None
        self.assertEqual(public_view["artifact_count"], 1)
        self.assertEqual(public_view["artifacts"][0]["image_url"],
                         f"/review-media/{image_path.relative_to(self.config.paths.review_dir).as_posix()}")
        self.assertEqual(
            public_view["audio_plan"]["summary"],
            "Primary track eac3 is planned for Opus at 224k.",
        )

    def test_backfill_multimodal_review_pack_restores_legacy_advice_with_retained_clips(self) -> None:
        prefix = "movies/The Super Mario Galaxy Movie (2026)"
        advice_path = _advice_file(self.config, prefix)
        advice_path.parent.mkdir(parents=True, exist_ok=True)
        advice_path.write_text(json.dumps({"summary": "Legacy draft without review pack."}) + "\n")

        sample_item = {
            "rel_path": f"{prefix}/The.Super.Mario.Galaxy.Movie.2026.mkv",
            "source_path": str(self.root / "galaxy-source.mkv"),
            "source_size_bytes": 2_519_516_042,
            "video_codec": "h264",
            "duration_seconds": 2561.35,
            "audio_summary": [{"codec_name": "eac3", "channels": 6, "language": "eng", "default": 1, "index": 1}],
            "subtitle_summary": [],
            "resolved_policy": {
                "video": {"quality_metric": "vmaf", "target_vmaf": 85.0},
                "audio": {"convert_to_opus_codecs": ["eac3"], "surround_5_1_opus_bitrate": "224k"},
            },
        }

        calibration = {
            "draft_hash": "728b984563319b8f",
            "review_media_ready": True,
            "policy": sample_item["resolved_policy"],
            "source_clips": [{"path": "/review-media/legacy-run/item-00/source-01.mp4", "timestamp_seconds": 345.0}],
            "preview_clips": [{"path": "/review-media/legacy-run/item-00/encoded-01.mp4", "timestamp_seconds": 345.0}],
            "compare_clips": [{"path": "/review-media/legacy-run/item-00/compare-01.mkv", "timestamp_seconds": 345.0}],
        }

        with patch(
            "mediaforce.web.app._build_multimodal_review_pack",
            return_value={"artifacts": [{"kind": "audio_spectrogram_compare"}]},
        ), patch(
            "mediaforce.web.app._multimodal_review_pack_public_view",
            return_value={
                "artifact_count": 3,
                "artifacts": [
                    {"kind": "video_compare_timeline", "image_url": "/review-media/review-compare.png"},
                    {"kind": "video_contact_sheet", "image_url": "/review-media/review-video.png"},
                    {"kind": "audio_spectrogram_compare", "image_url": "/review-media/review-audio.png"},
                ],
            },
        ):
            merged = _backfill_multimodal_review_pack(
                self.config,
                prefix,
                sample_item=sample_item,
                calibration=calibration,
                advice_state={"summary": "Legacy draft without review pack."},
            )

        assert merged is not None
        self.assertEqual(merged["summary"], "Legacy draft without review pack.")
        pack = merged.get("multimodal_review_pack")
        assert isinstance(pack, dict)
        artifact_kinds = [artifact["kind"] for artifact in pack["artifacts"]]
        self.assertIn("video_contact_sheet", artifact_kinds)
        self.assertIn("video_compare_timeline", artifact_kinds)
        self.assertIn("audio_spectrogram_compare", artifact_kinds)
        calibration_advice = calibration.get("advice")
        assert isinstance(calibration_advice, dict)
        self.assertEqual(calibration_advice.get("multimodal_review_pack"), pack)
        persisted_advice = json.loads(advice_path.read_text())
        self.assertEqual(persisted_advice["summary"], "Legacy draft without review pack.")
        self.assertEqual(persisted_advice["multimodal_review_pack"], pack)

    def test_build_multimodal_review_pack_includes_compare_timelines_and_moment_signals(self) -> None:
        sample_item = {
            "rel_path": "tv/House/Season 2/House.S02E06.mkv",
            "source_path": str(self.root / "source.mkv"),
            "source_size_bytes": 4_815_446_620,
            "duration_seconds": 2660.352,
            "audio_summary": [
                {"codec_name": "commentary", "channels": 2, "language": "spa", "default": 1, "index": 1},
                {"codec_name": "eac3", "channels": 6, "language": "eng", "default": 1, "index": 2},
            ],
        }
        Path(sample_item["source_path"]).write_text("source")
        source_clip = self.config.paths.review_dir / "run-123" / "item-00" / "source-01.mp4"
        preview_clip = self.config.paths.review_dir / "run-123" / "item-00" / "encoded-01.mp4"
        compare_clip = self.config.paths.review_dir / "run-123" / "item-00" / "compare-01.mkv"
        for path in (source_clip, preview_clip, compare_clip):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("clip")

        def create_artifact(*_args: object, **kwargs: object) -> None:
            output_path = kwargs["output_path"]
            self.assertIsInstance(output_path, (Path, str))
            if isinstance(output_path, Path):
                output_path.write_text("artifact")
            else:
                Path(output_path).write_text("artifact")

        with patch("mediaforce.web.runtime.folder_tuning_runtime.render_review_timeline_strip", side_effect=create_artifact), patch(
            "mediaforce.web.runtime.folder_tuning_runtime.render_review_contact_sheet", side_effect=create_artifact
        ), patch(
            "mediaforce.web.runtime.folder_tuning_runtime.render_audio_spectrogram_compare",
            return_value={"action": "libopus", "bitrate": "224k", "channels": 6, "codec_name": "eac3"},
        ) as audio_compare_mock:
            pack = _build_multimodal_review_pack(
                config=self.config,
                sample_item=sample_item,
                current_policy={
                    "audio": {
                        "copy_codecs": ["aac", "opus"],
                        "convert_to_opus_codecs": ["eac3"],
                        "surround_5_1_opus_bitrate": "224k",
                    }
                },
                calibration={
                    "review_media_ready": True,
                    "source_clips": [
                        {
                            "path": "/review-media/run-123/item-00/source-01.mp4",
                            "timestamp_seconds": 89.0,
                            "duration_seconds": 8.0,
                            "size_bytes": 27_525_266,
                        }
                    ],
                    "preview_clips": [
                        {
                            "path": "/review-media/run-123/item-00/encoded-01.mp4",
                            "timestamp_seconds": 89.0,
                            "duration_seconds": 8.0,
                            "size_bytes": 2_461_753,
                        }
                    ],
                    "compare_clips": [
                        {
                            "path": "/review-media/run-123/item-00/compare-01.mkv",
                            "timestamp_seconds": 89.0,
                            "duration_seconds": 8.0,
                        }
                    ],
                },
                output_dir=self.root / "review-pack",
            )

        assert pack is not None
        artifact_kinds = [artifact["kind"] for artifact in pack["artifacts"]]
        self.assertIn("video_compare_timeline", artifact_kinds)
        self.assertIn("video_contact_sheet", artifact_kinds)
        self.assertEqual(pack["moment_signals"][0]["moment"], 1)
        self.assertTrue(pack["moment_signals"][0]["has_compare_clip"])
        self.assertEqual(audio_compare_mock.call_args.kwargs["audio_track"]["index"], 2)

    def test_review_pack_dir_is_unique_per_request(self) -> None:
        first = _review_pack_dir(self.config, "tv/House/Season 2", "request-1")
        second = _review_pack_dir(self.config, "tv/House/Season 2", "request-2")

        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, second.parent)
        self.assertEqual(first.parent, _review_pack_dir(self.config, "tv/House/Season 2"))

    def test_clear_folder_tuning_state_refuses_active_job(self) -> None:
        prefix = "tv/House/Season 2"
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                calibration_jobs.insert().values(
                    job_id="job-active",
                    prefix=prefix,
                    status="queued",
                    lane="sample",
                    action="ai_tune",
                    host_json="{}",
                    notes="note",
                    policy_json="{}",
                    sample_item_json="{}",
                    created_at="2026-03-30T00:00:00+00:00",
                    updated_at="2026-03-30T00:00:00+00:00",
                )
            )

            result = _clear_folder_tuning_state(connection, config=self.config, prefix=prefix)

            self.assertFalse(result["ok"])
            self.assertIn("still active", result["message"])

    def test_learning_artifact_round_trip(self) -> None:
        sample_item = {
            "rel_path": "tv/Suits/Season 5/Episode.mkv",
            "video_codec": "h264",
            "recommendation": "priority_encode",
            "resolved_policy": {"video": {"quality_metric": "xpsnr"}},
        }
        applied_policy = {"video": {"target_xpsnr": 34.5}}
        response: dict[str, object] = {
            "summary": "Lower XPSNR slightly for clean TV episodes.",
            "diagnosis": "The season calibrates cleanly and can save more space.",
            "confidence": "medium",
            "evidence_checked": ["runtime_toolbelt.recent_sample_result"],
            "prompt_version": "tune-v2",
            "proposed_policy": {"video": {"target_xpsnr": 34.5}},
            "applied_policy": applied_policy,
            "self_check": {"status": "pass", "summary": "Looks safe.", "issues": []},
            "raw": "{}",
        }
        with open_db(self.config.paths.db_path) as connection:
            session_id = record_tuning_session(
                connection,
                prefix="tv/suits/season-5",
                note="Try slightly smaller.",
                response=response,
                applied_policy=applied_policy,
                toolbelt={"recent_sample_result": {"quality_score": 95.0}},
                created_at="2026-03-25T00:00:00+00:00",
            )
            artifact = promote_learning_artifact(
                connection,
                self.config,
                session_id=session_id,
                prefix="tv/suits/season-5",
                note="Try slightly smaller.",
                sample_item=sample_item,
                response=response,
                applied_policy=applied_policy,
                created_at="2026-03-25T00:00:00+00:00",
            )
            self.assertIsNotNone(artifact)
            context = retrieve_learning_context(
                connection,
                prefix="tv/suits/season-8",
                sample_item=sample_item,
                note="Need slightly smaller output.",
            )

        assert artifact is not None
        self.assertTrue(Path(artifact["artifact_path"]).exists())
        self.assertTrue(context)
        self.assertIn("Lower XPSNR slightly", context[0]["summary"])
        self.assertEqual(context[0]["evidence_authority"], "none")

    def test_record_visual_approval_artifact_creates_retrievable_memory(self) -> None:
        sample_item = {
            "rel_path": "tv/House/Season 2/Episode.mkv",
            "video_codec": "h264",
            "recommendation": "priority_encode",
            "resolved_policy": {"video": {"quality_metric": "vmaf"}},
        }
        calibration = {
            "job_id": "job-123",
            "policy": {"video": {"target_vmaf": 90.0, "min_target_vmaf": 88.0}},
            "sample_result": {"quality_metric": "VMAF", "quality_score": 91.4, "chosen_crf": 38.0},
        }

        with open_db(self.config.paths.db_path) as connection:
            artifact = record_visual_approval_artifact(
                connection,
                self.config,
                prefix="tv/House/Season 2",
                note="Looks good after review.",
                sample_item=sample_item,
                calibration=calibration,
                run_verdict={"summary": "Aggressive but acceptable for this show."},
                created_at="2026-03-29T23:45:00+00:00",
            )
            context = retrieve_learning_context(
                connection,
                prefix="tv/House/Season 2",
                sample_item=sample_item,
                note="Need a smaller approved draft.",
            )

        assert artifact is not None
        self.assertEqual(artifact["sample_job_id"], "job-123")
        self.assertTrue(Path(artifact["artifact_path"]).exists())
        self.assertTrue(context)
        self.assertIn("Aggressive but acceptable", context[0]["summary"])
        self.assertEqual(context[0]["evidence_authority"], "none")
        self.assertEqual(context[0]["approved_policy"], {})
        self.assertEqual(context[0]["sample_result"], {})
        self.assertEqual(context[0]["run_verdict"], {})

    def test_retrieve_learning_context_keeps_sibling_approval_non_authoritative(self) -> None:
        approved_sample_item = {
            "rel_path": "tv/House/Season 2/Episode.mkv",
            "video_codec": "h264",
            "recommendation": "priority_encode",
            "resolved_policy": {"video": {"quality_metric": "vmaf"}},
        }
        with open_db(self.config.paths.db_path) as connection:
            artifact = record_visual_approval_artifact(
                connection,
                self.config,
                prefix="tv/House/Season 2",
                note="Looks good after review.",
                sample_item=approved_sample_item,
                calibration={
                    "job_id": "job-house-s2",
                    "policy": {"video": {"target_vmaf": 88.0}},
                    "sample_result": {"quality_metric": "VMAF", "quality_score": 89.0},
                },
                run_verdict={"summary": "Approved for House season 2."},
                created_at="2026-03-29T23:45:00+00:00",
            )
            context = retrieve_learning_context(
                connection,
                prefix="tv/House/Season 3",
                sample_item={
                    "rel_path": "tv/House/Season 3/Episode.mkv",
                    "video_codec": "h264",
                    "recommendation": "priority_encode",
                    "resolved_policy": {"video": {"quality_metric": "vmaf"}},
                },
                note="Need a smaller approved draft.",
            )

        self.assertIsNotNone(artifact)
        self.assertTrue(context)
        self.assertEqual(context[0]["evidence_authority"], "none")
        self.assertEqual(context[0]["approved_policy"], {})

    def test_quality_risk_contract_public_view_keeps_current_rejection_authoritative(self) -> None:
        current_policy = {"video": {"target_vmaf": 90.0}}
        contract = build_quality_risk_contract(
            prefix="tv/House/Season 2",
            sample_item={
                "rel_path": "tv/House/Season 2/Episode.mkv",
                "representative_source_id": "src-house-s2",
                "source_fingerprint": "fp-1",
                "stream_budget_ledger": {"feasibility": {"status": "feasible"}},
                "cadence_decision": {
                    "classification": "progressive",
                    "confidence": 0.98,
                    "transform": "none",
                    "evidence_id": "ev1_cadence",
                },
                "media_fingerprint_decision": {
                    "status": "measured",
                    "confidence": 0.88,
                    "evidence_id": "ev1_fingerprint",
                    "findings": [
                        {
                            "id": "high_motion",
                            "confidence": 0.82,
                            "rationale": "Motion stays elevated in the measured ranges.",
                        }
                    ],
                },
            },
            current_policy=current_policy,
            preview_policy=current_policy,
            calibration={
                "job_id": "job-123",
                "sample_result": {"predicted_encode_percent": 12.0},
                "review_moments": [
                    {
                        "timestamp_seconds": 89.0,
                        "duration_seconds": 8.0,
                        "role": "hard",
                        "risk_tags": ["high_motion"],
                        "rationale": "Compare the motion-heavy range.",
                        "confidence": 0.88,
                        "coverage": 0.66,
                        "evidence_id": "ev1_fingerprint",
                    }
                ],
            },
            advice_state={
                "quality_risk_records": [
                    {
                        "kind": "post_test",
                        "verdict": "approved",
                        "tags": ["motion_breakup"],
                        "details": "Older approval.",
                        "prefix": "tv/House/Season 2",
                        "source_id": "src-house-s2",
                        "policy_hash": "sha256:wrong",
                        "sample_job_id": "job-old",
                        "created_at": "2026-03-28T00:00:00+00:00",
                    },
                    {
                        "kind": "post_test",
                        "verdict": "rejected",
                        "tags": ["motion_breakup"],
                        "details": "Current rejection.",
                        "prefix": "tv/House/Season 2",
                        "source_id": "src-house-s2",
                        "policy_hash": stable_policy_hash(current_policy),
                        "sample_job_id": "job-123",
                        "evidence_ids": ["ev1_cadence", "ev1_fingerprint"],
                        "moment_indexes": [1],
                        "created_at": "2026-03-29T00:00:00+00:00",
                    },
                ]
            },
        )

        public_view = quality_risk_public_view(contract)
        assert public_view is not None
        self.assertEqual(public_view["verdict"], "blocked")
        self.assertEqual(public_view["operator_decision"]["status"], "rejected")
        self.assertEqual(public_view["operator_decision"]["effective_record"]["details"], "Current rejection.")
        self.assertEqual(public_view["pre_test_instruction"]["moments"][0]["evidence_id"], "ev1_fingerprint")

    def test_retrieve_learning_context_ignores_unrelated_visual_approval(self) -> None:
        approved_sample_item = {
            "rel_path": "tv/House/Season 2/Episode.mkv",
            "video_codec": "h264",
            "recommendation": "priority_encode",
            "resolved_policy": {"video": {"quality_metric": "vmaf"}},
        }
        with open_db(self.config.paths.db_path) as connection:
            artifact = record_visual_approval_artifact(
                connection,
                self.config,
                prefix="tv/House/Season 2",
                note="Looks good after review.",
                sample_item=approved_sample_item,
                calibration={
                    "job_id": "job-house",
                    "policy": {"video": {"target_vmaf": 88.0}},
                    "sample_result": {"quality_metric": "VMAF", "quality_score": 89.0},
                },
                run_verdict={"summary": "Approved for House."},
                created_at="2026-03-29T23:45:00+00:00",
            )
            context = retrieve_learning_context(
                connection,
                prefix="tv/Suits/Season 5",
                sample_item={
                    "rel_path": "tv/Suits/Season 5/Episode.mkv",
                    "video_codec": "h264",
                    "recommendation": "priority_encode",
                    "resolved_policy": {"video": {"quality_metric": "vmaf"}},
                },
                note="Need a smaller approved draft.",
            )

        self.assertIsNotNone(artifact)
        self.assertEqual(context, [])

    def test_sibling_approved_season_memory_lists_other_approved_seasons(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            session_s1 = record_tuning_session(
                connection,
                prefix="tv/Suits/Season 1",
                note="Season 1 note",
                response={"summary": "Season 1", "confidence": "high", "raw": "{}"},
                applied_policy={"video": {"target_xpsnr": 35.0}},
                toolbelt={},
                created_at="2026-03-20T00:00:00+00:00",
            )
            session_s3 = record_tuning_session(
                connection,
                prefix="tv/Suits/Season 3",
                note="Season 3 note",
                response={"summary": "Season 3", "confidence": "high", "raw": "{}"},
                applied_policy={"video": {"target_xpsnr": 35.0}},
                toolbelt={},
                created_at="2026-03-22T00:00:00+00:00",
            )
            session_s2 = record_tuning_session(
                connection,
                prefix="tv/Suits/Season 2",
                note="Season 2 note",
                response={"summary": "Season 2", "confidence": "high", "raw": "{}"},
                applied_policy={"video": {"target_xpsnr": 35.0}},
                toolbelt={},
                created_at="2026-03-24T00:00:00+00:00",
            )
            session_s4 = record_tuning_session(
                connection,
                prefix="tv/Suits/Season 4",
                note="Season 4 note",
                response={"summary": "Season 4", "confidence": "high", "raw": "{}"},
                applied_policy={"video": {"target_xpsnr": 35.0}},
                toolbelt={},
                created_at="2026-03-26T00:00:00+00:00",
            )
            session_house = record_tuning_session(
                connection,
                prefix="tv/House/Season 1",
                note="House note",
                response={"summary": "House", "confidence": "high", "raw": "{}"},
                applied_policy={"video": {"target_xpsnr": 35.0}},
                toolbelt={},
                created_at="2026-03-28T00:00:00+00:00",
            )
            connection.execute(
                learning_artifacts.insert().values(
                    artifact_id="artifact-s1",
                    session_id=session_s1,
                    prefix="tv/Suits/Season 1",
                    title="Season 1 approval",
                    artifact_path=str(self.root / "learned-memory" / "season-1.md"),
                    summary="Season 1 accepted",
                    tags_json=json.dumps(["approval:visual", "decision:accepted"], sort_keys=True),
                    created_at="2026-03-20T00:00:00+00:00",
                    updated_at="2026-03-20T00:00:00+00:00",
                )
            )
            connection.execute(
                learning_artifacts.insert().values(
                    artifact_id="artifact-s3",
                    session_id=session_s3,
                    prefix="tv/Suits/Season 3",
                    title="Season 3 approval",
                    artifact_path=str(self.root / "learned-memory" / "season-3.md"),
                    summary="Season 3 accepted",
                    tags_json=json.dumps(["approval:visual", "decision:accepted"], sort_keys=True),
                    created_at="2026-03-22T00:00:00+00:00",
                    updated_at="2026-03-22T00:00:00+00:00",
                )
            )
            connection.execute(
                learning_artifacts.insert().values(
                    artifact_id="artifact-s2",
                    session_id=session_s2,
                    prefix="tv/Suits/Season 2",
                    title="Season 2 approval",
                    artifact_path=str(self.root / "learned-memory" / "season-2.md"),
                    summary="Current season accepted",
                    tags_json=json.dumps(["approval:visual", "decision:accepted"], sort_keys=True),
                    created_at="2026-03-24T00:00:00+00:00",
                    updated_at="2026-03-24T00:00:00+00:00",
                )
            )
            connection.execute(
                learning_artifacts.insert().values(
                    artifact_id="artifact-s4-unapproved",
                    session_id=session_s4,
                    prefix="tv/Suits/Season 4",
                    title="Season 4 draft",
                    artifact_path=str(self.root / "learned-memory" / "season-4.md"),
                    summary="Not approved",
                    tags_json=json.dumps(["approval:visual"], sort_keys=True),
                    created_at="2026-03-26T00:00:00+00:00",
                    updated_at="2026-03-26T00:00:00+00:00",
                )
            )
            connection.execute(
                learning_artifacts.insert().values(
                    artifact_id="artifact-house",
                    session_id=session_house,
                    prefix="tv/House/Season 1",
                    title="House approval",
                    artifact_path=str(self.root / "learned-memory" / "house-season-1.md"),
                    summary="Different show",
                    tags_json=json.dumps(["approval:visual", "decision:accepted"], sort_keys=True),
                    created_at="2026-03-28T00:00:00+00:00",
                    updated_at="2026-03-28T00:00:00+00:00",
                )
            )

            shortcut = sibling_approved_season_memory(connection, prefix="tv/Suits/Season 2")

        assert shortcut is not None
        self.assertEqual(shortcut["root_prefix"], "tv/Suits")
        self.assertEqual(shortcut["count"], 2)
        self.assertEqual(shortcut["season_labels"], ["Season 1", "Season 3"])
        self.assertEqual(shortcut["season_prefixes"], ["tv/Suits/Season 1", "tv/Suits/Season 3"])
        self.assertIn("approved Suits seasons", shortcut["suggested_note"])

    def test_sibling_approved_season_memory_escapes_sql_wildcards(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            matching_session = record_tuning_session(
                connection,
                prefix="tv/100%_Real/Season 1",
                note="matching note",
                response={"summary": "matching", "confidence": "high", "raw": "{}"},
                applied_policy={"video": {"target_xpsnr": 35.0}},
                toolbelt={},
                created_at="2026-03-20T00:00:00+00:00",
            )
            unrelated_session = record_tuning_session(
                connection,
                prefix="tv/100abcXReal/Season 9",
                note="unrelated note",
                response={"summary": "unrelated", "confidence": "high", "raw": "{}"},
                applied_policy={"video": {"target_xpsnr": 35.0}},
                toolbelt={},
                created_at="2026-03-21T00:00:00+00:00",
            )
            connection.execute(
                learning_artifacts.insert().values(
                    artifact_id="artifact-match",
                    session_id=matching_session,
                    prefix="tv/100%_Real/Season 1",
                    title="Matching season",
                    artifact_path=str(self.root / "learned-memory" / "matching.md"),
                    summary="matching season",
                    tags_json=json.dumps(["approval:visual", "decision:accepted"], sort_keys=True),
                    created_at="2026-03-20T00:00:00+00:00",
                    updated_at="2026-03-20T00:00:00+00:00",
                )
            )
            connection.execute(
                learning_artifacts.insert().values(
                    artifact_id="artifact-unrelated",
                    session_id=unrelated_session,
                    prefix="tv/100abcXReal/Season 9",
                    title="Unrelated season",
                    artifact_path=str(self.root / "learned-memory" / "unrelated.md"),
                    summary="unrelated season",
                    tags_json=json.dumps(["approval:visual", "decision:accepted"], sort_keys=True),
                    created_at="2026-03-21T00:00:00+00:00",
                    updated_at="2026-03-21T00:00:00+00:00",
                )
            )

            shortcut = sibling_approved_season_memory(connection, prefix="tv/100%_Real/Season 2")

        assert shortcut is not None
        self.assertEqual(shortcut["season_prefixes"], ["tv/100%_Real/Season 1"])

    def test_sibling_approved_season_memory_supports_show_root_prefix(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            season_session = record_tuning_session(
                connection,
                prefix="tv/Suits/Season 2",
                note="season note",
                response={"summary": "season", "confidence": "high", "raw": "{}"},
                applied_policy={"video": {"target_xpsnr": 35.0}},
                toolbelt={},
                created_at="2026-03-20T00:00:00+00:00",
            )
            connection.execute(
                learning_artifacts.insert().values(
                    artifact_id="artifact-show-root",
                    session_id=season_session,
                    prefix="tv/Suits/Season 2",
                    title="Season 2 approval",
                    artifact_path=str(self.root / "learned-memory" / "season-2.md"),
                    summary="season root",
                    tags_json=json.dumps(["approval:visual", "decision:accepted"], sort_keys=True),
                    created_at="2026-03-20T00:00:00+00:00",
                    updated_at="2026-03-20T00:00:00+00:00",
                )
            )

            shortcut = sibling_approved_season_memory(connection, prefix="tv/Suits")

        assert shortcut is not None
        self.assertEqual(shortcut["root_prefix"], "tv/Suits")
        self.assertEqual(shortcut["season_prefixes"], ["tv/Suits/Season 2"])
        self.assertIn("Season 2", shortcut["suggested_note"])
        self.assertIn("remaining episodes", shortcut["suggested_note"])

    def test_sibling_approved_season_memory_ignores_episode_level_prefixes(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            season_session = record_tuning_session(
                connection,
                prefix="tv/Suits/Season 1",
                note="season note",
                response={"summary": "season", "confidence": "high", "raw": "{}"},
                applied_policy={"video": {"target_xpsnr": 35.0}},
                toolbelt={},
                created_at="2026-03-20T00:00:00+00:00",
            )
            episode_session = record_tuning_session(
                connection,
                prefix="tv/Suits/Season 4/Episode 01",
                note="episode note",
                response={"summary": "episode", "confidence": "high", "raw": "{}"},
                applied_policy={"video": {"target_xpsnr": 35.0}},
                toolbelt={},
                created_at="2026-03-21T00:00:00+00:00",
            )
            connection.execute(
                learning_artifacts.insert().values(
                    artifact_id="artifact-season-root",
                    session_id=season_session,
                    prefix="tv/Suits/Season 1",
                    title="Season 1 approval",
                    artifact_path=str(self.root / "learned-memory" / "season-root.md"),
                    summary="season root",
                    tags_json=json.dumps(["approval:visual", "decision:accepted"], sort_keys=True),
                    created_at="2026-03-20T00:00:00+00:00",
                    updated_at="2026-03-20T00:00:00+00:00",
                )
            )
            connection.execute(
                learning_artifacts.insert().values(
                    artifact_id="artifact-episode-level",
                    session_id=episode_session,
                    prefix="tv/Suits/Season 4/Episode 01",
                    title="Episode approval",
                    artifact_path=str(self.root / "learned-memory" / "episode-level.md"),
                    summary="episode level",
                    tags_json=json.dumps(["approval:visual", "decision:accepted"], sort_keys=True),
                    created_at="2026-03-21T00:00:00+00:00",
                    updated_at="2026-03-21T00:00:00+00:00",
                )
            )

            shortcut = sibling_approved_season_memory(connection, prefix="tv/Suits/Season 2")

        assert shortcut is not None
        self.assertEqual(shortcut["season_prefixes"], ["tv/Suits/Season 1"])

    def test_proposal_can_queue_when_bench_keeps_current_policy(self) -> None:
        self.assertTrue(
            _proposal_can_queue(
                applied_fragment={},
                preview_policy={"video": {"target_vmaf": 88.0}},
                request_disposition="honored",
                alignment_issue=None,
            )
        )
        self.assertIn(
            "kept the current policy",
            _proposal_ready_message(
                can_queue=True,
                alignment_issue=None,
                has_policy_change=False,
                run_label="next sample",
            ),
        )

    def test_proposal_cannot_queue_without_change_when_request_was_rejected(self) -> None:
        self.assertFalse(
            _proposal_can_queue(
                applied_fragment={},
                preview_policy={"video": {"target_vmaf": 88.0}},
                request_disposition="rejected",
                alignment_issue=None,
            )
        )
        self.assertFalse(
            _proposal_can_queue(
                applied_fragment={"video": {"target_vmaf": 87.0}},
                preview_policy={"video": {"target_vmaf": 87.0}},
                request_disposition="rejected",
                alignment_issue=None,
            )
        )

    def test_proposal_alignment_issue_still_blocks_queue(self) -> None:
        self.assertFalse(
            _proposal_can_queue(
                applied_fragment={"video": {"target_vmaf": 87.0}},
                preview_policy={"video": {"target_vmaf": 87.0}},
                request_disposition="honored",
                alignment_issue="The draft raises the VMAF target even though your note asked for a smaller encode.",
            )
        )

    def test_quality_risk_blocker_prevents_sample_queue(self) -> None:
        self.assertFalse(
            _proposal_can_queue(
                applied_fragment={"video": {"target_vmaf": 87.0}},
                preview_policy={"video": {"target_vmaf": 87.0}},
                request_disposition="honored",
                alignment_issue=None,
                quality_risk_contract={
                    "verdict": "blocked",
                    "deterministic_gates": {"blocking_reasons": ["Mixed cadence needs review."]},
                },
            )
        )

    def test_proposal_alignment_issue_allows_explicit_combined_metric_target(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "combined_experiment",
                "metric": "vmaf",
                "target": 95.0,
                "budget_label": "300 MB per episode",
                "size_budget_request": {"budget_bytes": 300 * 1024 * 1024},
                "applied_policy": {
                    "video": {
                        "target_vmaf": 95.0,
                        "min_target_vmaf": 93.0,
                    }
                },
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 88.0, "min_target_vmaf": 86.0}},
            preview_policy={
                "video": {
                    "target_vmaf": 95.0,
                    "min_target_vmaf": 93.0,
                }
            },
        )

        self.assertIsNone(issue)

    def test_proposal_alignment_issue_validates_combined_metric_target(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "combined_experiment",
                "metric": "vmaf",
                "target": 95.0,
                "budget_label": "300 MB per episode",
                "size_budget_request": {"budget_bytes": 300 * 1024 * 1024},
                "applied_policy": {
                    "video": {
                        "target_vmaf": 95.0,
                        "min_target_vmaf": 93.0,
                    }
                },
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 88.0, "min_target_vmaf": 86.0}},
            preview_policy={
                "video": {
                    "target_vmaf": 90.0,
                    "min_target_vmaf": 88.0,
                    "max_encoded_percent": 8,
                }
            },
        )

        self.assertEqual(issue, "The draft uses 90.00 VMAF instead of the requested 95.00 target.")

    def test_proposal_alignment_issue_rejects_unrequested_combined_size_budget_cap(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "combined_experiment",
                "metric": "vmaf",
                "target": 95.0,
                "budget_label": "300 MB per episode",
                "size_budget_request": {"budget_bytes": 300 * 1024 * 1024},
                "applied_policy": {
                    "video": {
                        "target_vmaf": 95.0,
                        "min_target_vmaf": 93.0,
                    }
                },
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 88.0, "min_target_vmaf": 86.0}},
            preview_policy={
                "video": {
                    "target_vmaf": 95.0,
                    "min_target_vmaf": 93.0,
                    "max_encoded_percent": 12,
                }
            },
        )

        self.assertEqual(
            issue,
            "The draft turns the size budget into a max_encoded_percent change. Size budgets are planning "
            "targets for measured comparison unless the operator explicitly asks for a hard cap.",
        )

    def test_proposal_alignment_issue_rejects_size_budget_as_cap_change(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_policy": None,
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 95.0, "max_encoded_percent": 80}},
            preview_policy={"video": {"target_vmaf": 95.0, "max_encoded_percent": 8}},
        )

        self.assertEqual(
            issue,
            "The draft turns the size budget into a max_encoded_percent change. Size budgets are planning "
            "targets for measured comparison unless the operator explicitly asks for a hard cap.",
        )

    def test_proposal_alignment_issue_rejects_size_budget_new_cap(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_policy": None,
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 95.0}},
            preview_policy={"video": {"target_vmaf": 95.0, "max_encoded_percent": 8}},
        )

        self.assertEqual(
            issue,
            "The draft turns the size budget into a max_encoded_percent change. Size budgets are planning "
            "targets for measured comparison unless the operator explicitly asks for a hard cap.",
        )

    def test_proposal_alignment_issue_rejects_size_budget_relaxed_cap(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_policy": None,
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 95.0, "max_encoded_percent": 55}},
            preview_policy={"video": {"target_vmaf": 95.0, "max_encoded_percent": 80}},
        )

        self.assertEqual(
            issue,
            "The draft turns the size budget into a max_encoded_percent change. Size budgets are planning "
            "targets for measured comparison unless the operator explicitly asks for a hard cap.",
        )

    def test_proposal_alignment_issue_rejects_size_budget_quality_drop(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "combined_experiment",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "scale_height": 1080,
                "size_budget_request": {"budget_bytes": 300 * 1024 * 1024},
                "applied_policy": {"video": {"max_height": 1080}},
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 95.0, "max_height": 0, "max_encoded_percent": 80}},
            preview_policy={"video": {"target_vmaf": 89.0, "max_height": 1080, "max_encoded_percent": 80}},
        )

        self.assertEqual(
            issue,
            "The draft lowers the VMAF target based only on a size budget. Run a measured sample or ask for "
            "a specific quality tradeoff before changing quality targets.",
        )

    def test_proposal_alignment_issue_allows_evidence_backed_av1_video_tradeoff(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "250 MB per episode",
                "budget_bytes": 250 * 1024 * 1024,
                "evidence_authority": "operator_observed",
                "applied_policy": None,
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 95.0, "max_crf": 38, "max_height": 0}},
            preview_policy={"video": {"target_vmaf": 85.0, "max_crf": 46, "max_height": 0}},
        )

        self.assertIsNone(issue)

    def test_retrieved_visual_approval_remains_context_without_overriding_current_authority(self) -> None:
        request = {
            "request_type": "size_budget",
            "budget_label": "250 MB per episode",
            "budget_bytes": 250 * 1024 * 1024,
            "evidence_authority": "none",
            "applied_policy": None,
        }
        effective_request = _operator_request_with_retrieved_memory_authority(
            request,
            [{"evidence_authority": "approved_visual_result", "prefix": "tv/House/Season 2"}],
        )

        assert effective_request is not None
        self.assertEqual(effective_request["evidence_authority"], "none")
        self.assertIsNotNone(
            proposal_alignment_issue(
                operator_request=effective_request,
                request_disposition="honored",
                current_policy={"video": {"target_vmaf": 95.0, "max_crf": 38}},
                preview_policy={"video": {"target_vmaf": 85.0, "max_crf": 46}},
            )
        )
        rejected_request = _operator_request_with_retrieved_memory_authority(
            {**request, "evidence_authority": "rejected_visual_result"},
            [{"evidence_authority": "approved_visual_result", "prefix": "tv/House/Season 2"}],
        )
        assert rejected_request is not None
        self.assertEqual(rejected_request["evidence_authority"], "rejected_visual_result")

    def test_proposal_alignment_issue_rejects_size_budget_resolution_drop(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_policy": None,
            },
            request_disposition="honored",
            current_policy={"video": {"max_height": 0, "max_encoded_percent": 80}},
            preview_policy={"video": {"max_height": 720, "max_encoded_percent": 80}},
        )

        self.assertEqual(
            issue,
            "The draft changes resolution based only on a size budget. Ask for that resolution "
            "explicitly before changing the height cap.",
        )

    def test_proposal_alignment_issue_rejects_size_budget_new_resolution_cap(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_policy": None,
            },
            request_disposition="honored",
            current_policy={"video": {"max_encoded_percent": 80}},
            preview_policy={"video": {"max_height": 720, "max_encoded_percent": 80}},
        )

        self.assertEqual(
            issue,
            "The draft changes resolution based only on a size budget. Ask for that resolution "
            "explicitly before changing the height cap.",
        )

    def test_proposal_alignment_issue_rejects_measured_size_budget_resolution_drop(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_policy": None,
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 85.0, "max_height": 1080, "max_encoded_percent": 80}},
            preview_policy={"video": {"target_vmaf": 82.0, "max_height": 720, "max_encoded_percent": 80}},
            allow_measured_size_quality_tradeoff=True,
        )

        self.assertEqual(
            issue,
            "The draft changes resolution based only on a size budget. Ask for that resolution "
            "explicitly before changing the height cap.",
        )

    def test_proposal_alignment_issue_rejects_measured_size_budget_audio_drop(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_policy": None,
            },
            request_disposition="honored",
            current_policy={
                "video": {"target_vmaf": 85.0, "max_encoded_percent": 80},
                "audio": {"surround_5_1_opus_bitrate": "256k"},
            },
            preview_policy={
                "video": {"target_vmaf": 82.0, "max_encoded_percent": 80},
                "audio": {"surround_5_1_opus_bitrate": "160k"},
            },
            allow_measured_size_quality_tradeoff=True,
        )

        self.assertEqual(
            issue,
            "The draft changes audio.surround_5_1_opus_bitrate based only on a size budget. Ask for that audio "
            "tradeoff or measure it explicitly before changing audio quality.",
        )

    def test_proposal_alignment_issue_rejects_size_budget_audio_drop(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_policy": None,
            },
            request_disposition="honored",
            current_policy={
                "video": {"max_encoded_percent": 80},
                "audio": {"surround_5_1_opus_bitrate": "224k"},
            },
            preview_policy={
                "video": {"max_encoded_percent": 80},
                "audio": {"surround_5_1_opus_bitrate": "160k"},
            },
        )

        self.assertEqual(
            issue,
            "The draft changes audio.surround_5_1_opus_bitrate based only on a size budget. Ask for that audio "
            "tradeoff or measure it explicitly before changing audio quality.",
        )

    def test_proposal_alignment_issue_rejects_size_budget_new_audio_cap(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_policy": None,
            },
            request_disposition="honored",
            current_policy={"video": {"max_encoded_percent": 80}},
            preview_policy={
                "video": {"max_encoded_percent": 80},
                "audio": {"surround_5_1_opus_bitrate": "160k"},
            },
        )

        self.assertEqual(
            issue,
            "The draft changes audio.surround_5_1_opus_bitrate based only on a size budget. Ask for that audio "
            "tradeoff or measure it explicitly before changing audio quality.",
        )

    def test_proposal_alignment_issue_allows_measured_size_budget_quality_tradeoff(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_policy": None,
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 95.0, "max_encoded_percent": 80}},
            preview_policy={"video": {"target_vmaf": 92.0, "max_encoded_percent": 80}},
            allow_measured_size_quality_tradeoff=True,
        )

        self.assertIsNone(issue)

    def test_proposal_alignment_issue_allows_under_target_size_budget_quality_increase(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_policy": None,
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 92.0, "max_encoded_percent": 80}},
            preview_policy={"video": {"target_vmaf": 95.0, "max_encoded_percent": 80}},
            allow_measured_size_quality_increase=True,
        )

        self.assertIsNone(issue)

    def test_proposal_alignment_issue_allows_under_target_size_budget_quality_knob_increase(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_policy": None,
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 92.0, "default_grain": 0, "max_encoded_percent": 80}},
            preview_policy={"video": {"target_vmaf": 92.0, "default_grain": 8, "max_encoded_percent": 80}},
            allow_measured_size_quality_increase=True,
        )

        self.assertIsNone(issue)

    def test_proposal_alignment_issue_rejects_under_target_size_budget_quality_drop(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_policy": None,
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 95.0, "max_encoded_percent": 80}},
            preview_policy={"video": {"target_vmaf": 92.0, "max_encoded_percent": 80}},
            allow_measured_size_quality_tradeoff=False,
        )

        self.assertEqual(
            issue,
            "The draft lowers the VMAF target based only on a size budget. Run a measured sample or ask for "
            "a specific quality tradeoff before changing quality targets.",
        )

    def test_proposal_alignment_issue_allows_enforced_measured_size_budget_cap(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_max_encoded_percent": 8,
                "applied_policy": {"video": {"max_encoded_percent": 8}},
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 95.0, "max_encoded_percent": 80}},
            preview_policy={"video": {"target_vmaf": 92.0, "max_encoded_percent": 8}},
            allow_measured_size_quality_tradeoff=True,
        )

        self.assertIsNone(issue)

    def test_proposal_alignment_issue_rejects_measured_size_budget_weaker_cap(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "size_budget",
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "applied_max_encoded_percent": 8,
                "applied_policy": {"video": {"max_encoded_percent": 8}},
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 95.0, "max_encoded_percent": 80}},
            preview_policy={"video": {"target_vmaf": 92.0, "max_encoded_percent": 12}},
            allow_measured_size_quality_tradeoff=True,
        )

        self.assertEqual(
            issue,
            "The draft uses a 12% size ceiling instead of the requested 8% ceiling.",
        )

    def test_proposal_alignment_issue_rejects_combined_metric_size_budget_cap_change(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "combined_experiment",
                "metric": "vmaf",
                "target": 95.0,
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "size_budget_request": {"budget_bytes": 300 * 1024 * 1024},
                "applied_policy": {"video": {"target_vmaf": 95.0, "min_target_vmaf": 93.0}},
            },
            request_disposition="honored",
            current_policy={"video": {"target_vmaf": 95.0, "min_target_vmaf": 93.0, "max_encoded_percent": 80}},
            preview_policy={"video": {"target_vmaf": 95.0, "min_target_vmaf": 93.0, "max_encoded_percent": 1}},
            allow_measured_size_quality_tradeoff=True,
        )

        self.assertEqual(
            issue,
            "The draft turns the size budget into a max_encoded_percent change. Size budgets are planning "
            "targets for measured comparison unless the operator explicitly asks for a hard cap.",
        )

    def test_proposal_alignment_issue_rejects_unmeasured_size_budget_grain_change(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "combined_experiment",
                "metric": "vmaf",
                "target": 95.0,
                "budget_label": "300 MB per episode",
                "budget_bytes": 300 * 1024 * 1024,
                "size_budget_request": {"budget_bytes": 300 * 1024 * 1024},
                "applied_policy": {
                    "video": {
                        "quality_metric": "vmaf",
                        "target_vmaf": 95.0,
                        "min_target_vmaf": 93.0,
                        "max_height": 1080,
                    }
                },
            },
            request_disposition="honored",
            current_policy={"video": {"quality_metric": "auto", "target_vmaf": 95.0, "default_grain": 8}},
            preview_policy={
                "video": {
                    "quality_metric": "vmaf",
                    "target_vmaf": 95.0,
                    "min_target_vmaf": 93.0,
                    "max_height": 1080,
                    "default_grain": 0,
                }
            },
        )

        self.assertEqual(
            issue,
            "The draft changes video.default_grain based only on a size budget. Run a measured sample or ask for "
            "that specific tradeoff before changing video tuning knobs.",
        )

    def test_size_budget_sample_issue_rejects_undersized_prediction(self) -> None:
        issue = size_budget_sample_issue(
            operator_request={
                "operator_confirmed": True,
                "request_type": "size_budget",
                "budget_bytes": 300 * 1024 * 1024,
            },
            calibration_payload={"sample_result": {"predicted_total_size_bytes": 180 * 1024 * 1024}},
        )

        assert issue is not None
        self.assertIn("below the requested", issue)

    def test_size_budget_sample_issue_rejects_oversized_prediction(self) -> None:
        issue = size_budget_sample_issue(
            operator_request={
                "operator_confirmed": True,
                "request_type": "size_budget",
                "budget_bytes": 300 * 1024 * 1024,
            },
            calibration_payload={"sample_result": {"predicted_total_size_bytes": 360 * 1024 * 1024}},
        )

        assert issue is not None
        self.assertIn("above the requested", issue)

    def test_size_budget_sample_issue_accepts_prediction_inside_band(self) -> None:
        issue = size_budget_sample_issue(
            operator_request={
                "operator_confirmed": True,
                "request_type": "size_budget",
                "budget_bytes": 300 * 1024 * 1024,
            },
            calibration_payload={"sample_result": {"predicted_total_size_bytes": 320 * 1024 * 1024}},
        )

        self.assertIsNone(issue)

    def test_proposal_alignment_issue_validates_scale_target(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "scale_target",
                "applied_policy": {"video": {"max_height": 1080}},
            },
            request_disposition="honored",
            current_policy={"video": {"max_height": 2160}},
            preview_policy={"video": {"max_height": 720}},
        )

        self.assertEqual(issue, "The draft uses 720p instead of the requested 1080p height cap.")

    def test_proposal_alignment_issue_validates_source_resolution_target(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "scale_target",
                "scale_height": 0,
                "applied_policy": {"video": {"max_height": 0}},
            },
            request_disposition="honored",
            current_policy={"video": {"max_height": 0}},
            preview_policy={"video": {"max_height": 1080}},
        )

        self.assertEqual(issue, "The draft sets a height cap even though the operator requested source resolution.")

    def test_proposal_alignment_issue_allows_source_resolution_target(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "scale_target",
                "scale_height": 0,
                "applied_policy": {"video": {"max_height": 0}},
            },
            request_disposition="honored",
            current_policy={"video": {"max_height": 720}},
            preview_policy={"video": {"max_height": 0}},
        )

        self.assertIsNone(issue)

    def test_proposal_alignment_issue_validates_smart_black_bar_target(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "scale_target",
                "applied_policy": {"video": {"black_bar_handling": "smart", "crop": ""}},
            },
            request_disposition="honored",
            current_policy={"video": {"black_bar_handling": "off", "crop": "1920:800:0:140"}},
            preview_policy={"video": {"black_bar_handling": "smart", "crop": "1920:800:0:140"}},
        )

        self.assertEqual(issue, "The draft keeps a manual crop even though smart black-bar detection was requested.")

    def test_proposal_alignment_issue_treats_auto_and_smart_black_bar_as_equivalent(self) -> None:
        issue = proposal_alignment_issue(
            operator_request={
                "request_type": "scale_target",
                "applied_policy": {"video": {"black_bar_handling": "smart", "crop": ""}},
            },
            request_disposition="honored",
            current_policy={"video": {"black_bar_handling": "off"}},
            preview_policy={"video": {"black_bar_handling": "auto", "crop": ""}},
        )

        self.assertIsNone(issue)

    def test_operator_request_signature_ignores_black_bar_mode_when_crop_is_explicit(self) -> None:
        manual_crop_signature = operator_request_signature(
            {
                "request_type": "scale_target",
                "black_bar_handling": "smart",
                "crop": "1920:800:0:140",
            }
        )
        crop_only_signature = operator_request_signature(
            {
                "request_type": "scale_target",
                "black_bar_handling": "off",
                "crop": "1920:800:0:140",
            }
        )

        self.assertEqual(manual_crop_signature, crop_only_signature)

    def test_operator_request_signature_treats_auto_and_smart_black_bar_as_equivalent(self) -> None:
        smart_signature = operator_request_signature(
            {"request_type": "scale_target", "black_bar_handling": "smart"}
        )
        auto_signature = operator_request_signature(
            {"request_type": "scale_target", "black_bar_handling": "auto"}
        )

        self.assertEqual(smart_signature, auto_signature)

    def test_guided_review_feedback_preserves_target_and_current_rejection(self) -> None:
        request = operator_request_from_intent(
            {
                "schema_version": 1,
                "size_goal": {
                    "mode": "normalized",
                    "value_mb": 300,
                    "reference_runtime_minutes": 45,
                    "sample_projection_tolerance_percent": 10,
                    "final_output_tolerance_percent": 5,
                },
                "resolution": {"mode": "source", "max_height": None},
                "quality_risk_tags": ["motion_breakup", "audio_quality_layout"],
                "quality_risk_details": "Moment 2 loses texture and the center channel sounds thin.",
                "evidence_authority": "rejected_visual_result",
            },
            note="Keep the target and revise the picture and sound.",
            sample_item={
                "rel_path": "tv/show/season-1/episode.mkv",
                "source_size_bytes": 8_000_000_000,
                "duration_seconds": 88 * 60,
                "video_bitrate": 8_000_000,
                "audio_summary": [],
                "subtitle_summary": [],
            },
            current_policy={
                "video": {"max_height": 0},
                "audio": {},
                "subtitle": {},
            },
        )

        self.assertEqual(request["request_type"], "combined_experiment")
        self.assertEqual(request["budget_bytes"], 586_666_667)
        self.assertEqual(
            request["quality_risk_tags"],
            ["motion_breakup", "audio_quality_layout"],
        )
        self.assertEqual(request["evidence_authority"], "rejected_visual_result")
        self.assertEqual(
            request["quality_risk_details"],
            "Moment 2 loses texture and the center channel sounds thin.",
        )

        normalized_request = with_quality_risk_intent(
            request,
            note="Keep the target because motion breakup and thin sound need another test. " + ("x" * 3000),
        )

        self.assertIsNotNone(normalized_request)
        self.assertEqual(
            normalized_request["quality_risk_tags"],
            ["motion_breakup", "audio_quality_layout"],
        )
        self.assertEqual(
            normalized_request["quality_risk_details"],
            "Moment 2 loses texture and the center channel sounds thin.",
        )
        inferred_request = with_quality_risk_intent(
            None,
            note="Motion breakup needs another test. " + ("x" * 3000),
        )
        self.assertIsNotNone(inferred_request)
        self.assertEqual(inferred_request["quality_risk_tags"], ["motion_breakup"])
        self.assertEqual(len(inferred_request["quality_risk_details"]), 2000)
        details_only_request = with_quality_risk_intent(
            {"quality_risk_details": "x" * 3000},
            note="Make another test.",
        )
        self.assertIsNotNone(details_only_request)
        self.assertEqual(details_only_request["quality_risk_tags"], ["other"])
        self.assertEqual(len(details_only_request["quality_risk_details"]), 2000)

    def test_latest_failed_target_size_job_ignores_newer_unrelated_failure(self) -> None:
        prefix = "tv/show/Season 1"
        with open_db(self.config.paths.db_path) as connection:
            for job_id, created_at, result in (
                (
                    "target-failed",
                    "2026-07-12T00:00:00+00:00",
                    {"target_size_status": "quality_conflict"},
                ),
                (
                    "unrelated-failed",
                    "2026-07-12T00:01:00+00:00",
                    {"failure_kind": "host_unavailable"},
                ),
            ):
                connection.execute(
                    calibration_jobs.insert().values(
                        job_id=job_id,
                        prefix=prefix,
                        status="failed",
                        lane="sample",
                        action="ai_tune",
                        host_json="{}",
                        notes="note",
                        policy_json="{}",
                        sample_item_json="{}",
                        result_json=json.dumps(result),
                        created_at=created_at,
                        updated_at=created_at,
                    )
                )

            job = load_latest_failed_target_size_sample_job(connection, prefix)

        self.assertIsNotNone(job)
        self.assertEqual(job["job_id"], "target-failed")

    def test_run_calibration_job_records_storage_recovery_failure_before_sampling(self) -> None:
        saved_payloads: list[dict[str, object]] = []
        sample_item = Mock()

        deps = CalibrationRunDeps(
            now_iso=lambda: "2026-04-11T00:00:00+00:00",
            ensure_sample_host_ready=Mock(
                side_effect=HostReadinessError(
                    "M2 MBP could not connect the media share with Finder.",
                    failure_kind="host_configuration",
                )
            ),
            load_job_state=lambda *_args, **_kwargs: {"job_id": "job-1", "status": "queued"},
            sample_item=sample_item,
            save_job_state=lambda _connection, _config, _prefix, payload: saved_payloads.append(dict(payload)),
            save_calibration_state=lambda *_args, **_kwargs: None,
            record_run_verdict=lambda *_args, **_kwargs: None,
            summarize_calibration_result=lambda payload: payload,
            calibration_mode_for_action=lambda _action: "sample",
            effective_video_preset=lambda *_args, **_kwargs: 4,
            search_quality_for_source=lambda *_args, **_kwargs: None,
            run_sample_encode=lambda *_args, **_kwargs: None,
            detect_video_crop=lambda *_args, **_kwargs: None,
            recommend_review_timestamps=lambda *_args, **_kwargs: [],
            encode_preview_clips=lambda *_args, **_kwargs: [],
            render_source_review_clips=lambda *_args, **_kwargs: [],
            generate_compare_clips_from_previews=lambda *_args, **_kwargs: [],
            resolve_stream_budget_ledger=resolve_stream_budget_ledger,
            build_svt_params=lambda *_args, **_kwargs: {},
            review_url=lambda *_args, **_kwargs: "",
            encode_manifest_items=lambda *_args, **_kwargs: None,
            validate_manifest_items=lambda *_args, **_kwargs: None,
            generate_compare_clips=lambda *_args, **_kwargs: [],
            staged_artifact_columns=("library_item_id",),
        )

        with patch("mediaforce.web.runtime.calibration_runtime.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.calibration_runtime.purge_transient_artifacts"
        ):
            run_calibration_job(
                config_path=self.config.paths.config_path,
                prefix="tv/show/season-1",
                action="ai_tune",
                host_data={"key": "cbusillo@chris-mbp", "label": "M2 MBP"},
                notes="retry later",
                policy={"video": {}},
                job_id="job-1",
                seed_metadata=None,
                process_controller=type("Controller", (), {"throw_if_cancelled": lambda _self: None})(),
                deps=deps,
            )

        failed = next(payload for payload in saved_payloads if payload.get("status") == "failed")
        self.assertIn("could not connect the media share with Finder", str(failed.get("error") or ""))
        sample_item.assert_not_called()

    def test_run_calibration_job_marks_cancelled_run_stopped(self) -> None:
        saved_statuses: list[str] = []
        readiness = Mock(side_effect=lambda _config, host_data: _ready_calibration_host(host_data))

        def _save_job_state(_connection: object, _config: object, _prefix: str, payload: dict[str, object]) -> None:
            status = payload.get("status")
            saved_statuses.append(status if isinstance(status, str) else "")

        deps = CalibrationRunDeps(
            now_iso=lambda: "2026-04-11T00:00:00+00:00",
            ensure_sample_host_ready=readiness,
            load_job_state=lambda *_args, **_kwargs: {"job_id": "job-1", "status": "queued"},
            sample_item=lambda *_args, **_kwargs: {
                "rel_path": "tv/show/season-1/episode.mkv",
                "source_path": str(self.root / "episode.mkv"),
                "source_size_bytes": 1024,
                "video_codec": "h264",
                "duration_seconds": 120.0,
            },
            save_job_state=_save_job_state,
            save_calibration_state=lambda *_args, **_kwargs: None,
            record_run_verdict=lambda *_args, **_kwargs: None,
            summarize_calibration_result=lambda payload: payload,
            calibration_mode_for_action=lambda _action: "sample",
            effective_video_preset=lambda *_args, **_kwargs: 4,
            search_quality_for_source=lambda *_args, **_kwargs: (_ for _ in ()).throw(ProcessCancelledError()),
            run_sample_encode=lambda *_args, **_kwargs: None,
            detect_video_crop=lambda *_args, **_kwargs: None,
            recommend_review_timestamps=lambda *_args, **_kwargs: [],
            encode_preview_clips=lambda *_args, **_kwargs: [],
            render_source_review_clips=lambda *_args, **_kwargs: [],
            generate_compare_clips_from_previews=lambda *_args, **_kwargs: [],
            resolve_stream_budget_ledger=resolve_stream_budget_ledger,
            build_svt_params=lambda *_args, **_kwargs: {},
            review_url=lambda *_args, **_kwargs: "",
            encode_manifest_items=lambda *_args, **_kwargs: None,
            validate_manifest_items=lambda *_args, **_kwargs: None,
            generate_compare_clips=lambda *_args, **_kwargs: [],
            staged_artifact_columns=("library_item_id",),
        )

        episode_path = self.root / "episode.mkv"
        episode_path.write_text("episode")

        with patch("mediaforce.web.runtime.calibration_runtime.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.calibration_runtime.purge_transient_artifacts"
        ):
            run_calibration_job(
                config_path=self.config.paths.config_path,
                prefix="tv/show/season-1",
                action="ai_tune",
                host_data={"key": "cbusillo@localhost", "label": "M4 Studio"},
                notes="retry later",
                policy={"video": {"pixel_format": "yuv420p10le", "sample_every": "8m", "sample_duration": "20s", "encoder": "libsvtav1"}},
                job_id="job-1",
                seed_metadata=None,
                process_controller=type("Controller", (), {"throw_if_cancelled": lambda _self: None})(),
                deps=deps,
            )

        self.assertIn("running", saved_statuses)
        self.assertIn("stopped", saved_statuses)
        self.assertNotIn("failed", saved_statuses)
        readiness.assert_called_once_with(
            self.config,
            {"key": "cbusillo@localhost", "label": "M4 Studio"},
        )

    def test_run_calibration_job_preserves_failed_target_search_evidence(self) -> None:
        saved_payloads: list[dict[str, object]] = []
        trace = {
            "schema_version": 1,
            "status": "quality_conflict",
            "selection_reason": "all_candidates_violate_quality_floor",
            "target": {
                "total_target_bytes": 300_000_000,
                "sample_lower_bound_bytes": 270_000_000,
                "sample_upper_bound_bytes": 330_000_000,
            },
            "quality_floor": {"metric": "vmaf", "minimum": 93.0},
            "curve": {"shape": "monotonic", "candidate_count": 3, "max_candidates": 6},
        }

        deps = CalibrationRunDeps(
            now_iso=lambda: "2026-04-11T00:00:00+00:00",
            ensure_sample_host_ready=lambda _config, host_data: _ready_calibration_host(host_data),
            load_job_state=lambda *_args, **_kwargs: {"job_id": "job-1", "status": "queued"},
            sample_item=lambda *_args, **_kwargs: {
                "rel_path": "tv/show/season-1/episode.mkv",
                "source_path": str(self.root / "target-search-episode.mkv"),
                "source_size_bytes": 8_000_000_000,
                "video_codec": "h264",
                "video_bitrate": 8_000_000,
                "duration_seconds": 5280.0,
                "audio_summary": [],
                "subtitle_summary": [],
            },
            save_job_state=lambda _connection, _config, _prefix, payload: saved_payloads.append(dict(payload)),
            save_calibration_state=lambda *_args, **_kwargs: None,
            record_run_verdict=lambda *_args, **_kwargs: None,
            summarize_calibration_result=lambda payload: payload,
            calibration_mode_for_action=lambda _action: "sample",
            effective_video_preset=lambda *_args, **_kwargs: 4,
            search_quality_for_source=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TargetSizeSearchError(
                    "No candidate met both the target band and quality floor.",
                    status="quality_conflict",
                    trace=trace,
                )
            ),
            run_sample_encode=lambda *_args, **_kwargs: None,
            detect_video_crop=lambda *_args, **_kwargs: None,
            recommend_review_timestamps=lambda *_args, **_kwargs: [],
            encode_preview_clips=lambda *_args, **_kwargs: [],
            render_source_review_clips=lambda *_args, **_kwargs: [],
            generate_compare_clips_from_previews=lambda *_args, **_kwargs: [],
            resolve_stream_budget_ledger=resolve_stream_budget_ledger,
            build_svt_params=lambda *_args, **_kwargs: {},
            review_url=lambda *_args, **_kwargs: "",
            encode_manifest_items=lambda *_args, **_kwargs: None,
            validate_manifest_items=lambda *_args, **_kwargs: None,
            generate_compare_clips=lambda *_args, **_kwargs: [],
            staged_artifact_columns=("library_item_id",),
        )

        (self.root / "target-search-episode.mkv").write_text("episode")
        with patch("mediaforce.web.runtime.calibration_runtime.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.calibration_runtime.purge_transient_artifacts"
        ):
            run_calibration_job(
                config_path=self.config.paths.config_path,
                prefix="tv/show/season-1",
                action="ai_tune",
                host_data={"key": "localhost", "label": "Local worker"},
                notes="Keep the target.",
                policy={
                    "video": {
                        "pixel_format": "yuv420p10le",
                        "sample_every": "8m",
                        "sample_duration": "20s",
                        "encoder": "libsvtav1",
                    }
                },
                job_id="job-1",
                seed_metadata=None,
                process_controller=type("Controller", (), {"throw_if_cancelled": lambda _self: None})(),
                deps=deps,
            )

        failed = next(payload for payload in saved_payloads if payload.get("status") == "failed")
        self.assertEqual(
            failed["result"],
            {"target_size_status": "quality_conflict", "target_size_trace": trace},
        )

    def test_run_calibration_job_uses_saved_job_sample_item_before_reselecting(self) -> None:
        saved_statuses: list[str] = []

        def _save_job_state(_connection: object, _config: object, _prefix: str, payload: dict[str, object]) -> None:
            status = payload.get("status")
            saved_statuses.append(status if isinstance(status, str) else "")

        saved_sample_item = {
            "library_item_id": 9,
            "rel_path": "tv/show/season-1/saved-episode.mkv",
            "source_path": str(self.root / "saved-episode.mkv"),
            "source_size_bytes": 1024,
            "video_codec": "h264",
            "duration_seconds": 120.0,
            "width": 1920,
            "height": 1080,
            "audio_summary": [],
            "subtitle_summary": [],
        }

        deps = CalibrationRunDeps(
            now_iso=lambda: "2026-04-11T00:00:00+00:00",
            ensure_sample_host_ready=lambda _config, host_data: _ready_calibration_host(host_data),
            load_job_state=lambda *_args, **_kwargs: {"job_id": "job-1", "status": "queued", "sample_item": dict(saved_sample_item)},
            sample_item=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not reselect sample item")),
            save_job_state=_save_job_state,
            save_calibration_state=lambda *_args, **_kwargs: None,
            record_run_verdict=lambda *_args, **_kwargs: None,
            summarize_calibration_result=lambda payload: payload,
            calibration_mode_for_action=lambda _action: "sample",
            effective_video_preset=lambda *_args, **_kwargs: 4,
            search_quality_for_source=lambda *_args, **_kwargs: (_ for _ in ()).throw(ProcessCancelledError()),
            run_sample_encode=lambda *_args, **_kwargs: None,
            detect_video_crop=lambda *_args, **_kwargs: None,
            recommend_review_timestamps=lambda *_args, **_kwargs: [],
            encode_preview_clips=lambda *_args, **_kwargs: [],
            render_source_review_clips=lambda *_args, **_kwargs: [],
            generate_compare_clips_from_previews=lambda *_args, **_kwargs: [],
            resolve_stream_budget_ledger=resolve_stream_budget_ledger,
            build_svt_params=lambda *_args, **_kwargs: {},
            review_url=lambda *_args, **_kwargs: "",
            encode_manifest_items=lambda *_args, **_kwargs: None,
            validate_manifest_items=lambda *_args, **_kwargs: None,
            generate_compare_clips=lambda *_args, **_kwargs: [],
            staged_artifact_columns=("library_item_id",),
        )

        saved_episode_path = self.root / "saved-episode.mkv"
        saved_episode_path.write_text("episode")

        with patch("mediaforce.web.runtime.calibration_runtime.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.calibration_runtime.purge_transient_artifacts"
        ):
            run_calibration_job(
                config_path=self.config.paths.config_path,
                prefix="tv/show/season-1",
                action="ai_tune",
                host_data={"key": "cbusillo@localhost", "label": "M4 Studio"},
                notes="retry later",
                policy={"video": {"pixel_format": "yuv420p10le", "sample_every": "8m", "sample_duration": "20s", "encoder": "libsvtav1"}},
                job_id="job-1",
                seed_metadata=None,
                process_controller=type("Controller", (), {"throw_if_cancelled": lambda _self: None})(),
                deps=deps,
            )

        self.assertIn("running", saved_statuses)
        self.assertIn("stopped", saved_statuses)

    def test_apply_seed_policy_ignores_null_tunable_fields(self) -> None:
        base_policy = {
            "video": {
                "preset": 4,
                "target_vmaf": 95.0,
                "min_target_vmaf": 93.0,
                "target_xpsnr": 35.5,
                "min_target_xpsnr": 34.5,
                "max_encoded_percent": 55,
                "default_grain": 0,
            },
            "audio": {"surround_5_1_opus_bitrate": "224k", "keep_languages": ["eng"]},
            "subtitle": {"prefer_text": True},
        }
        proposed_policy = {
            "video": {
                "default_grain": 1,
                "preset": None,
                "target_vmaf": None,
                "min_target_vmaf": None,
                "target_xpsnr": None,
                "min_target_xpsnr": None,
                "max_encoded_percent": None,
            },
            "audio": {"surround_5_1_opus_bitrate": None, "keep_languages": None},
            "subtitle": {"prefer_text": None},
        }
        updated_policy, applied = apply_seed_policy(base_policy, proposed_policy)
        self.assertEqual(applied, {"video": {"default_grain": 1}})
        self.assertEqual(updated_policy["video"]["default_grain"], 1)
        self.assertEqual(updated_policy["video"]["target_vmaf"], 95.0)
        self.assertEqual(updated_policy["video"]["preset"], 4)
        self.assertEqual(updated_policy["video"]["max_encoded_percent"], 55)
        self.assertEqual(updated_policy["audio"]["surround_5_1_opus_bitrate"], "224k")
        self.assertEqual(updated_policy["audio"]["keep_languages"], ["eng"])
        self.assertTrue(updated_policy["subtitle"]["prefer_text"])

    def test_apply_seed_policy_supports_full_encode_policy_surface(self) -> None:
        base_policy = {
            "video": {
                "encoder": "libsvtav1",
                "pixel_format": "yuv420p10le",
                "preset": 4,
                "crf_search": True,
                "quality_metric": "auto",
                "target_vmaf": 95.0,
                "min_target_vmaf": 93.0,
                "target_xpsnr": 41.0,
                "min_target_xpsnr": 35.0,
                "sample_every": "8m",
                "sample_duration": "20s",
                "min_crf": 18,
                "max_crf": 38,
                "max_encoded_percent": 80,
                "default_grain": 8,
                "grain_denoise": 0,
                "thorough": True,
            },
            "audio": {
                "keep_languages": ["eng"],
                "copy_codecs": ["aac", "opus"],
                "convert_to_opus_codecs": ["ac3", "eac3"],
                "stereo_opus_bitrate": "128k",
                "surround_5_1_opus_bitrate": "256k",
                "surround_7_1_opus_bitrate": "320k",
            },
            "subtitle": {
                "keep_languages": ["eng"],
                "prefer_text": True,
                "keep_forced": True,
                "default_mode": "first_english",
            },
        }
        proposed_policy = {
            "video": {
                "preset": 6,
                "sample_every": "5m",
                "min_crf": 20,
                "max_crf": 36,
                "default_grain": 5,
                "thorough": False,
            },
            "audio": {
                "keep_languages": ["eng", "jpn"],
                "surround_5_1_opus_bitrate": "224k",
            },
            "subtitle": {
                "prefer_text": False,
                "default_mode": "none",
            },
        }

        updated_policy, applied = apply_seed_policy(base_policy, proposed_policy, mode="tune")

        self.assertEqual(updated_policy["video"]["preset"], 6)
        self.assertEqual(updated_policy["video"]["sample_every"], "5m")
        self.assertEqual(updated_policy["video"]["min_crf"], 20)
        self.assertEqual(updated_policy["video"]["max_crf"], 36)
        self.assertEqual(updated_policy["video"]["default_grain"], 5)
        self.assertFalse(updated_policy["video"]["thorough"])
        self.assertEqual(updated_policy["audio"]["keep_languages"], ["eng", "jpn"])
        self.assertEqual(updated_policy["audio"]["surround_5_1_opus_bitrate"], "224k")
        self.assertFalse(updated_policy["subtitle"]["prefer_text"])
        self.assertEqual(updated_policy["subtitle"]["default_mode"], "none")
        self.assertIn("subtitle", applied)

    def test_seed_prompt_adds_class_guardrails(self) -> None:
        prompt = _build_seed_prompt({"folder": "tv/House/Season 5"})

        self.assertEqual(SEED_PROMPT_VERSION, "seed-v9")
        self.assertIn("cold-start guess", prompt)
        self.assertIn("best first-pass attempt", prompt)
        self.assertIn("instruction to satisfy", prompt)
        self.assertIn("audio_tradeoff_hint", prompt)
        self.assertIn("recommended_seed_action", prompt)
        self.assertIn("review_confidence", prompt)
        self.assertIn("hold", prompt)
        self.assertIn("Use softened only when the operator note is exploratory", prompt)
        self.assertIn("closest faithful experiment", prompt)
        self.assertIn("operator_repeat_signal", prompt)
        self.assertIn("established, visually approved product baseline", prompt)
        self.assertIn("Evidence precedence is strict", prompt)
        self.assertIn("legacy H.264 or HEVC bitrate intuition", prompt)
        self.assertIn("High-80s VMAF", prompt)
        self.assertIn("non-positive video budget", prompt)
        self.assertNotIn("risky draft", prompt)
        self.assertNotIn("request looks unrealistic", prompt)
        self.assertIn("video.black_bar_handling", prompt)
        self.assertIn("Do not infer 1080p or 720p scaling from a size budget alone", prompt)
        self.assertIn("request_response", prompt)
        self.assertIn("honored_with_risk", prompt)

    def test_extract_seed_payload_recovers_json_object_from_wrapped_text(self) -> None:
        raw = "Here you go:\n```json\n{\"summary\":\"safe\",\"policy\":{\"video\":{}}}\n```"

        payload = _extract_seed_payload(raw)

        self.assertEqual(payload["summary"], "safe")
        self.assertEqual(payload["policy"], {"video": {}})

    def test_policy_response_schema_tracks_policy_shape(self) -> None:
        schema = _policy_response_schema(
            {
                "video": {"target_vmaf": 95.0, "thorough": True},
                "audio": {"keep_languages": ["eng"], "stereo_opus_bitrate": "128k"},
                "subtitle": {"prefer_text": True},
            }
        )

        self.assertEqual(schema["required"], ["video", "audio", "subtitle"])
        self.assertEqual(schema["properties"]["video"]["properties"]["target_vmaf"]["type"], ["number", "null"])
        self.assertEqual(schema["properties"]["video"]["properties"]["thorough"]["type"], ["boolean", "null"])
        self.assertEqual(schema["properties"]["video"]["properties"]["max_height"]["type"], ["number", "null"])
        self.assertEqual(
            schema["properties"]["video"]["properties"]["black_bar_handling"]["type"],
            ["string", "null"],
        )
        self.assertEqual(schema["properties"]["audio"]["properties"]["keep_languages"]["type"], ["array", "null"])

    def test_apply_seed_policy_allows_transform_keys_when_base_policy_omits_them(self) -> None:
        updated_policy, applied = apply_seed_policy(
            {"video": {"target_vmaf": 94.0}},
            {"video": {"max_height": 1080, "black_bar_handling": "smart", "crop": ""}},
        )

        self.assertEqual(applied["video"]["max_height"], 1080)
        self.assertEqual(applied["video"]["black_bar_handling"], "smart")
        self.assertEqual(applied["video"]["crop"], "")
        self.assertEqual(updated_policy["video"]["max_height"], 1080)
        self.assertEqual(updated_policy["video"]["black_bar_handling"], "smart")

    def test_apply_seed_policy_normalizes_transform_policy_values(self) -> None:
        _updated_policy, applied = apply_seed_policy(
            {
                "video": {
                    "max_height": 0,
                    "downsample_algorithm": "lanczos",
                    "black_bar_handling": "off",
                    "black_bar_detect_samples": 3,
                    "crop": "",
                }
            },
            {
                "video": {
                    "max_height": 1080,
                    "downsample_algorithm": "bicubic",
                    "black_bar_handling": "SMART",
                    "black_bar_detect_samples": 8,
                    "crop": "1920:800:0:140",
                }
            },
        )

        self.assertEqual(applied["video"]["max_height"], 1080)
        self.assertEqual(applied["video"]["downsample_algorithm"], "bicubic")
        self.assertEqual(applied["video"]["black_bar_handling"], "smart")
        self.assertEqual(applied["video"]["black_bar_detect_samples"], 5)
        self.assertEqual(applied["video"]["crop"], "1920:800:0:140")

    def test_tune_self_check_is_deterministic_and_requires_policy(self) -> None:
        failed = _run_tune_self_check(project_root=self.root, tuning_context={}, proposal={})
        passed = _run_tune_self_check(
            project_root=self.root,
            tuning_context={},
            proposal={"policy": {"video": {"target_vmaf": 88.0}}},
        )

        self.assertEqual(failed["status"], "fail")
        self.assertEqual(failed["source"], "deterministic")
        self.assertEqual(passed["status"], "pass")
        self.assertEqual(passed["source"], "deterministic")

    def test_tune_prompt_mentions_review_media_conversation(self) -> None:
        prompt = _build_tune_prompt(
            {
                "folder": "tv/House/Season 2",
                "operator_note": "What do you think of the current review clips?",
                "runtime_toolbelt": {
                    "review_media_context": {
                        "review_media_ready": True,
                        "moment_count": 3,
                        "moments": [{"moment": 1, "timestamp_seconds": 89.0}],
                    }
                },
            }
        )

        self.assertIn("review_media_context", prompt)
        self.assertIn("current audio tradeoff", prompt)
        self.assertIn("never pretend", prompt)
        self.assertIn("instruction to satisfy", prompt)
        self.assertIn("runtime_toolbelt.audio_tradeoff_hint", prompt)
        self.assertIn("closest faithful experiment", prompt)
        self.assertIn("video.black_bar_handling", prompt)
        self.assertIn("operator_note_parse.scale_height", prompt)
        self.assertIn("Evidence precedence is strict", prompt)

    def test_tune_prompt_summarizes_multimodal_review_pack_without_paths(self) -> None:
        prompt = _build_tune_prompt(
            {
                "folder": "tv/House/Season 2",
                "operator_note": "What do you think of these artifacts?",
                "multimodal_review_pack": {
                    "artifacts": [{"label": "Moment 1", "detail": "Source on top, draft on bottom"}],
                    "images": ["/tmp/private-artifact.png"],
                },
            }
        )

        self.assertIn("multimodal_review_pack", prompt)
        self.assertIn("image_count", prompt)
        self.assertNotIn("/tmp/private-artifact.png", prompt)

    def test_tune_prompt_mentions_review_artifact_critique(self) -> None:
        prompt = _build_tune_prompt(
            {
                "folder": "tv/House/Season 2",
                "operator_note": "Push a little more if the hallway still looks clean.",
                "review_artifact_critique": {
                    "summary": "Moment 2 is the fragile scene.",
                    "weakest_moments": ["Moment 2"],
                },
            }
        )

        self.assertIn("review_artifact_critique", prompt)
        self.assertIn("first-pass reading over artifacts extracted from the actual review clips", prompt)

    def test_review_artifact_critique_prompt_summarizes_multimodal_pack_without_paths(self) -> None:
        prompt = _build_review_artifact_critique_prompt(
            {
                "folder": "tv/House/Season 2",
                "multimodal_review_pack": {
                    "artifacts": [{"label": "Moment 1", "detail": "Compare timeline"}],
                    "images": ["/tmp/private-artifact.png"],
                },
            }
        )

        self.assertIn("multimodal_review_pack", prompt)
        self.assertIn("image_count", prompt)
        self.assertNotIn("/tmp/private-artifact.png", prompt)

    def test_operator_note_parse_prompt_extracts_scale_height_only_from_request(self) -> None:
        prompt = _build_operator_note_parse_prompt({"operator_note": "Downsample the 4K files to 1080p."})

        self.assertIn("scale_height", prompt)
        self.assertIn("black_bar_handling", prompt)
        self.assertIn("crop", prompt)
        self.assertIn("scale_target", prompt)
        self.assertIn("do not infer a scale target", prompt)
        self.assertIn("evidence_authority", prompt)

    def test_build_tuning_runtime_toolbelt_summarizes_review_media(self) -> None:
        toolbelt = _build_tuning_runtime_toolbelt(
            sample_item={
                "rel_path": "tv/House/Season 2/House.S02E06.mkv",
                "source_size_bytes": 4_815_446_620,
                "duration_seconds": 2660.352,
                "audio_summary": [
                    {"codec_name": "commentary", "channels": 2, "language": "spa", "default": 1, "index": 1},
                    {"codec_name": "eac3", "channels": 6, "language": "eng", "default": 1, "index": 2},
                ],
                "resolved_policy": {},
            },
            current_policy={
                "video": {"target_vmaf": 89.0},
                "audio": {
                    "convert_to_opus_codecs": ["eac3"],
                    "surround_5_1_opus_bitrate": "224k",
                },
            },
            calibration={
                "sample_result": {"quality_score": 90.7, "quality_target": 89.0},
                "review_media_ready": True,
                "source_clips": [
                    {
                        "path": "/review-media/run-123/item-00/source-01.mp4",
                        "timestamp_seconds": 89.0,
                        "duration_seconds": 8.0,
                        "size_bytes": 27_525_266,
                    }
                ],
                "preview_clips": [
                    {
                        "path": "/review-media/run-123/item-00/encoded-01.mp4",
                        "timestamp_seconds": 89.0,
                        "duration_seconds": 8.0,
                        "size_bytes": 2_461_753,
                    }
                ],
                "compare_clips": [
                    {
                        "path": "/review-media/run-123/item-00/compare-01.mkv",
                        "timestamp_seconds": 89.0,
                        "duration_seconds": 8.0,
                    }
                ],
            },
            metric_support={"vmaf": True, "xpsnr": True},
        )

        review_media_context = toolbelt.get("review_media_context")
        assert isinstance(review_media_context, dict)
        self.assertTrue(review_media_context["review_media_ready"])
        self.assertEqual(review_media_context["moment_count"], 1)
        self.assertEqual(review_media_context["moments"][0]["preview_clip_size_bytes"], 2_461_753)
        self.assertEqual(review_media_context["moments"][0]["compare_clip_path"],
                         "/review-media/run-123/item-00/compare-01.mkv")
        toolbelt_audio_hint = toolbelt.get("audio_tradeoff_hint")
        assert isinstance(toolbelt_audio_hint, dict)
        self.assertEqual(toolbelt_audio_hint["leverage"], "low")
        self.assertEqual(toolbelt_audio_hint["recommended_seed_action"], "hold")
        self.assertEqual(toolbelt_audio_hint["review_confidence"], "low")
        self.assertEqual(toolbelt_audio_hint["target_bitrate_kbps"], 224.0)
        self.assertEqual(toolbelt_audio_hint["next_lower_bitrate_kbps"], 192.0)

    def test_build_tuning_runtime_toolbelt_includes_size_target_analysis(self) -> None:
        toolbelt = _build_tuning_runtime_toolbelt(
            sample_item={
                "rel_path": "tv/Suits/Season 5/Suits.S05E01.mkv",
                "source_size_bytes": 4_815_446_620,
                "duration_seconds": 2660.352,
                "audio_summary": [],
                "resolved_policy": {},
            },
            current_policy={"video": {"target_vmaf": 95.0}},
            calibration={"sample_result": {"predicted_total_size_bytes": 360 * 1024 * 1024}},
            metric_support={"vmaf": True, "xpsnr": True},
            operator_request={
                "operator_confirmed": True,
                "request_type": "size_budget",
                "budget_bytes": 300 * 1024 * 1024,
            },
        )

        size_target_analysis = toolbelt.get("size_target_analysis")
        assert isinstance(size_target_analysis, dict)
        self.assertEqual(size_target_analysis["status"], "over_target")
        self.assertEqual(size_target_analysis["budget_bytes"], 300 * 1024 * 1024)

    def test_build_tuning_runtime_toolbelt_includes_decision_defaults(self) -> None:
        toolbelt = _build_tuning_runtime_toolbelt(
            sample_item={
                "rel_path": "tv/Suits/Season 5/Suits.S05E01.mkv",
                "source_size_bytes": 4_815_446_620,
                "duration_seconds": 2700.0,
                "audio_summary": [],
                "resolved_policy": {},
            },
            current_policy={
                "video": {
                    "decision_model": "size_first_review",
                    "quality_engine": "ab_av1_fast_sample",
                    "target_size_mb": 300,
                    "target_runtime_minutes": 45,
                    "max_height": 1080,
                    "quality_metric": "vmaf",
                    "target_vmaf": 85.0,
                    "min_target_vmaf": 80.0,
                }
            },
            calibration=None,
            metric_support={"vmaf": True, "xpsnr": True},
        )

        decision_defaults = toolbelt.get("decision_defaults")
        assert isinstance(decision_defaults, dict)
        self.assertEqual(decision_defaults["decision_model"], "size_first_review")
        self.assertEqual(decision_defaults["quality_engine"], "ab_av1_fast_sample")
        self.assertEqual(decision_defaults["target_size_bytes"], 300_000_000)
        self.assertEqual(decision_defaults["sample_target_size_bytes"], 300_000_000)
        self.assertEqual(decision_defaults["max_height"], 1080)

    def test_planned_audio_review_context_uses_planner_primary_audio_track(self) -> None:
        context = _planned_audio_review_context(
            sample_item={
                "audio_summary": [
                    {"codec_name": "commentary", "channels": 2, "language": "spa", "default": 1, "index": 1},
                    {"codec_name": "eac3", "channels": 6, "language": "eng", "default": 1, "index": 2},
                ]
            },
            current_policy={
                "audio": {
                    "copy_codecs": ["aac", "opus"],
                    "convert_to_opus_codecs": ["eac3"],
                    "surround_5_1_opus_bitrate": "224k",
                }
            },
        )

        self.assertEqual(context["action"], "libopus")
        self.assertEqual(context["primary_track"]["index"], 2)
        self.assertEqual(context["primary_track"]["codec_name"], "eac3")
        self.assertEqual(context["primary_track"]["channels"], 6)
        self.assertEqual(context["target_bitrate"], "224k")

    def test_planned_audio_review_context_marks_copy_vs_transcode(self) -> None:
        transcode_context = _planned_audio_review_context(
            sample_item={"audio_summary": [{"codec_name": "eac3", "channels": 6, "language": "eng"}]},
            current_policy={
                "audio": {
                    "copy_codecs": ["aac", "opus"],
                    "convert_to_opus_codecs": ["eac3"],
                    "surround_5_1_opus_bitrate": "224k",
                }
            },
        )
        copy_context = _planned_audio_review_context(
            sample_item={"audio_summary": [{"codec_name": "aac", "channels": 2, "language": "eng"}]},
            current_policy={
                "audio": {
                    "copy_codecs": ["aac", "opus"],
                    "convert_to_opus_codecs": ["eac3"],
                    "stereo_opus_bitrate": "128k",
                }
            },
        )

        self.assertEqual(transcode_context["action"], "libopus")
        self.assertEqual(transcode_context["target_bitrate"], "224k")
        self.assertEqual(copy_context["action"], "copy")
        self.assertIsNone(copy_context["target_bitrate"])

    def test_operator_requested_experiment_uses_selected_audio_track_for_copy_budget_estimate(self) -> None:
        request = _operator_requested_experiment(
            "I want to aim for 200MB per episode while keeping the current audio.",
            {
                "source_size_bytes": 1_000_000_000,
                "duration_seconds": 100.0,
                "audio_summary": [
                    {"codec_name": "commentary", "channels": 2, "language": "spa", "default": 1, "index": 1},
                    {"codec_name": "aac", "channels": 2, "language": "eng", "default": 0, "index": 2, "bit_rate": 320_000},
                ],
                "resolved_policy": {
                    "audio": {
                        "copy_codecs": ["aac"],
                        "convert_to_opus_codecs": ["eac3"],
                        "stereo_opus_bitrate": "128k",
                    }
                },
            },
        )

        assert request is not None
        self.assertEqual(request["request_type"], "size_budget")
        self.assertEqual(request["estimated_audio_bytes"], 4_000_000)
        self.assertIsNone(request.get("audio_tradeoff_hint"))

    def test_build_seed_policy_payload_surfaces_class_signals(self) -> None:
        payload = _build_seed_policy_payload(
            prefix="tv/House/Season 5",
            user_note="",
            base_policy={
                "video": {
                    "target_vmaf": 95.0,
                    "min_target_vmaf": 93.0,
                    "target_xpsnr": 41.0,
                    "min_target_xpsnr": 35.0,
                    "max_encoded_percent": 80,
                    "default_grain": 8,
                    "encoder": "libsvtav1",
                },
                "audio": {
                    "surround_5_1_opus_bitrate": "256k",
                    "copy_codecs": ["aac", "opus"],
                    "convert_to_opus_codecs": ["eac3"],
                },
            },
            sample_item={
                "rel_path": "tv/House/Season 5/House.S05E22.mkv",
                "source_size_bytes": 4_700_000_000,
                "video_codec": "h264",
                "video_bitrate": 11_000_000,
                "width": 1920,
                "height": 1080,
                "duration_seconds": 2645.248,
                "audio_summary": [{"codec_name": "eac3", "channels": 6}],
                "subtitle_summary": [{"codec_name": "subrip", "language": "eng"}],
                "recommendation": "priority_encode",
                "recommendation_reason": "Large H.264 season.",
            },
            summary={
                "item_count": 24,
                "total_size_bytes": 112_800_000_000,
                "statuses": {"discovered": 24},
                "video_codecs": {"h264": 24},
                "audio_codecs": {"eac3:6": 24},
                "seasons": {"Season 5": 24},
                "suggested_override": {
                    "video": {
                        "default_grain": 0,
                        "target_xpsnr": 35.5,
                        "min_target_xpsnr": 34.5,
                        "max_encoded_percent": 55,
                    },
                    "audio": {"surround_5_1_opus_bitrate": "224k"},
                    "planning": {"extra_score": 18},
                    "reason": [
                        "Mostly H.264, so this folder is a strong AV1 candidate.",
                        "Clean catalog TV usually does not need synthetic grain and can tolerate a lower XPSNR floor.",
                    ],
                },
            },
            metric_support={"vmaf": True, "xpsnr": True, "ssim": True, "psnr": True},
        )

        self.assertEqual(payload["sample_item"]["resolution_tier"], "1080p")
        self.assertEqual(payload["class_signals"]["collection_shape"], "tv_season")
        self.assertEqual(payload["preferred_metric"], "vmaf")
        self.assertEqual(payload["base_policy"]["audio"]["surround_5_1_opus_bitrate"], "256k")
        self.assertEqual(payload["base_policy"]["video"]["encoder"], "libsvtav1")
        payload_audio_hint = payload.get("audio_tradeoff_hint")
        assert isinstance(payload_audio_hint, dict)
        self.assertEqual(payload_audio_hint["leverage"], "low")
        self.assertEqual(payload_audio_hint["recommended_seed_action"], "hold")
        self.assertEqual(payload_audio_hint["review_confidence"], "low")
        self.assertEqual(payload_audio_hint["target_bitrate_kbps"], 256.0)
        self.assertEqual(payload_audio_hint["next_lower_bitrate_kbps"], 224.0)
        self.assertEqual(payload["summary"]["suggested_override"]["policy_focus"]["audio"]["surround_5_1_opus_bitrate"],
                         "224k")
        self.assertIn(
            "Sample codec matches the folder majority codec (h264).",
            payload["class_signals"]["positive_signals"],
        )
        self.assertIn(
            "This first-pass seed is only a bounded starting point; measured calibration and operator review decide.",
            payload["class_signals"]["caution_flags"],
        )

    def test_audio_tradeoff_hint_holds_when_low_leverage(self) -> None:
        # 44-min episode: 224k→192k saves ~10 MiB — below the 12 MiB low threshold
        hint = audio_tradeoff_hint(
            {
                "source_size_bytes": 2_000_000_000,
                "duration_seconds": 2640.0,
                "audio_summary": [{"codec_name": "eac3", "channels": 6, "language": "eng"}],
            },
            {
                "convert_to_opus_codecs": ["eac3"],
                "surround_5_1_opus_bitrate": "224k",
            },
        )

        assert hint is not None
        self.assertEqual(hint["leverage"], "low")
        self.assertEqual(hint["recommended_seed_action"], "hold")
        self.assertEqual(hint["review_confidence"], "low")

    def test_audio_tradeoff_hint_holds_when_medium_leverage(self) -> None:
        # 224k→192k (32 kbps delta) over 5000s = ~19 MiB, 3 GB source → 0.67% → medium leverage
        hint = audio_tradeoff_hint(
            {
                "source_size_bytes": 3_000_000_000,
                "duration_seconds": 5000.0,
                "audio_summary": [{"codec_name": "eac3", "channels": 6, "language": "eng"}],
            },
            {
                "convert_to_opus_codecs": ["eac3"],
                "surround_5_1_opus_bitrate": "224k",
            },
        )

        assert hint is not None
        self.assertEqual(hint["leverage"], "medium")
        self.assertEqual(hint["recommended_seed_action"], "hold")
        self.assertEqual(hint["review_confidence"], "low")

    def test_audio_tradeoff_hint_allows_when_high_leverage(self) -> None:
        # 3-hour item, 320k→256k saves ~82 MiB, source is only 5 GB so that's >1% → high leverage
        hint = audio_tradeoff_hint(
            {
                "source_size_bytes": 5_000_000_000,
                "duration_seconds": 10_800.0,
                "audio_summary": [{"codec_name": "eac3", "channels": 6, "language": "eng"}],
            },
            {
                "convert_to_opus_codecs": ["eac3"],
                "surround_5_1_opus_bitrate": "320k",
            },
        )

        assert hint is not None
        self.assertEqual(hint["leverage"], "high")
        self.assertEqual(hint["recommended_seed_action"], "allow")
        self.assertEqual(hint["review_confidence"], "low")

    def test_seed_prompt_references_recommended_seed_action(self) -> None:
        prompt = _build_seed_prompt({"folder": "tv/Suits/Season 3"})

        self.assertIn("recommended_seed_action", prompt)
        self.assertIn("review_confidence", prompt)
        self.assertIn("hold", prompt)
        self.assertNotIn("requested budget clearly needs that savings", prompt)
        self.assertIn("materially necessary and worth the review risk", prompt)

    def test_seed_prompt_references_hinted_audio_key_for_stereo(self) -> None:
        prompt = _build_seed_prompt(
            {
                "folder": "movies/Demo",
                "audio_tradeoff_hint": {"policy_key": "stereo_opus_bitrate", "review_confidence": "medium"},
            }
        )

        self.assertIn("stereo_opus_bitrate", prompt)
        self.assertNotIn("surround Opus bitrate", prompt)

    def test_tune_prompt_does_not_allow_budget_to_override_low_leverage_audio(self) -> None:
        prompt = _build_tune_prompt({"folder": "tv/Suits/Season 3"})

        self.assertIn("runtime_toolbelt.audio_tradeoff_hint", prompt)
        self.assertIn("review_confidence", prompt)
        self.assertNotIn("requested budget clearly needs that savings", prompt)
        self.assertIn("materially necessary and worth that review risk", prompt)
        self.assertIn("legacy H.264 or HEVC bitrate intuition", prompt)
        self.assertIn("high-80s VMAF", prompt)

    def test_tune_self_check_bypasses_model_for_audio_hint(self) -> None:
        with patch("mediaforce.advisor._run_structured_llm_request") as structured_request:
            result = _run_tune_self_check(
                project_root=self.root,
                tuning_context={
                    "runtime_toolbelt": {
                        "audio_tradeoff_hint": {
                            "policy_key": "stereo_opus_bitrate",
                            "review_risk_summary": "Stereo changes are easier to validate directly.",
                            "review_confidence": "medium",
                        }
                    }
                },
                proposal={"policy": {"audio": {"stereo_opus_bitrate": "112k"}}},
            )

        assert result is not None
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["source"], "deterministic")
        structured_request.assert_not_called()

    def test_shutdown_cleanup_cancels_managed_processes(self) -> None:
        from mediaforce.web import app as web_app

        calibration_controller = web_app.ManagedProcessController()
        encode_controller = web_app.ManagedProcessController()

        original_map = dict(web_app.CALIBRATION_QUEUE_PROCESSES)
        original_encode = web_app.ENCODE_QUEUE_PROCESS
        try:
            web_app.CALIBRATION_QUEUE_PROCESSES.clear()
            web_app.CALIBRATION_QUEUE_PROCESSES["job-1"] = calibration_controller
            web_app.ENCODE_QUEUE_PROCESS = encode_controller

            with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                    "mediaforce.web.app._start_calibration_queue_worker"
            ), patch("mediaforce.web.app._start_encode_queue_worker"), patch(
                "mediaforce.web.app._refresh_host_status_cache", return_value=[]
            ):
                app = web_app.create_app(self.root / "config.toml")
            import asyncio

            async def _exercise_lifespan() -> None:
                async with app.router.lifespan_context(app):
                    return None

            asyncio.run(_exercise_lifespan())

            self.assertTrue(calibration_controller.cancelled)
            self.assertTrue(encode_controller.cancelled)
        finally:
            web_app.CALIBRATION_QUEUE_PROCESSES.clear()
            web_app.CALIBRATION_QUEUE_PROCESSES.update(original_map)
            web_app.ENCODE_QUEUE_PROCESS = original_encode

    def test_upsert_override_replaces_existing_runtime_override(self) -> None:
        override_file = self.root / "runtime-settings.json"
        override_file.write_text(
            json.dumps(
                {
                    "remote_hosts": [{"key": "m4-studio"}],
                    "folder_policy_overrides": [
                        {
                            "path_prefix": "tv/House/Season 2",
                            "note": "Old note",
                            "video": {"target_xpsnr": 33.0},
                        },
                        {
                            "path_prefix": "tv/Other",
                            "note": "Keep me",
                            "planning": {"default_limit": 8},
                        },
                    ],
                }
            )
        )

        _upsert_override(
            override_file,
            "tv/House/Season 2",
            {"video": {"target_xpsnr": 34.5}, "audio": {"bitrate_kbps": 224}},
        )

        updated = json.loads(override_file.read_text())
        self.assertEqual(updated["remote_hosts"], [{"key": "m4-studio"}])
        overrides = updated["folder_policy_overrides"]
        self.assertEqual(len(overrides), 2)
        self.assertEqual([item["path_prefix"] for item in overrides], ["tv/House/Season 2", "tv/Other"])
        self.assertEqual(overrides[0]["video"]["target_xpsnr"], 34.5)
        self.assertEqual(overrides[0]["audio"]["bitrate_kbps"], 224)
        self.assertEqual(overrides[0]["note"], "Saved from the calibration bench.")
        self.assertEqual(overrides[1]["planning"]["default_limit"], 8)

    def test_upsert_override_preserves_nested_planning_tables_in_runtime_settings(self) -> None:
        override_file = self.root / "runtime-settings.json"

        _upsert_override(
            override_file,
            "tv/House/Season 2",
            {
                "planning": {
                    "bucket_thresholds": {"priority_encode": 70, "review_encode": 35},
                    "codec_bonus": {"av1": -45, "default": 18, "h264": 40},
                }
            },
        )

        updated = json.loads(override_file.read_text())
        planning = updated["folder_policy_overrides"][0]["planning"]
        self.assertEqual(planning["bucket_thresholds"], {"priority_encode": 70, "review_encode": 35})
        self.assertEqual(planning["codec_bonus"], {"av1": -45, "default": 18, "h264": 40})

    def test_runtime_settings_updates_are_serialized_across_processes(self) -> None:
        runtime_file = self.root / "runtime-settings.json"
        runtime_file.write_text(
            json.dumps(
                {
                    "remote_hosts": [{"label": "original"}],
                    "wake_mac": "aa:bb:cc:dd:ee:ff",
                }
            )
        )
        ready_file = self.root / "runtime-writer-ready.txt"

        ctx = multiprocessing.get_context("spawn")
        first_writer = ctx.Process(
            target=_runtime_settings_writer,
            args=(str(runtime_file), str(ready_file), "folder"),
        )
        first_writer.start()

        deadline = time.time() + 10
        while not ready_file.exists():
            if time.time() > deadline:
                first_writer.terminate()
                self.fail("Timed out waiting for the first runtime-settings writer to acquire the lock")
            time.sleep(0.05)

        second_writer = ctx.Process(
            target=_runtime_settings_writer,
            args=(str(runtime_file), str(self.root / "runtime-writer-second.txt"), "remote"),
        )
        second_writer.start()

        first_writer.join(10)
        second_writer.join(10)
        self.assertEqual(first_writer.exitcode, 0)
        self.assertEqual(second_writer.exitcode, 0)

        updated = json.loads(runtime_file.read_text())
        self.assertEqual(updated["remote_hosts"], [{"label": "m4-studio"}])
        self.assertEqual(updated["wake_mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(updated["folder_policy_overrides"], [{"path_prefix": "tv/House/Season 2"}])

    def test_save_runtime_settings_overwrites_malformed_runtime_file(self) -> None:
        runtime_file = self.root / "runtime-settings.json"
        runtime_file.write_text("{broken json")

        save_runtime_settings(runtime_file, {"remote_hosts": [{"label": "recovered"}]})

        updated = json.loads(runtime_file.read_text())
        self.assertEqual(updated, {"remote_hosts": [{"label": "recovered"}]})

    def test_update_runtime_settings_recovers_malformed_runtime_file(self) -> None:
        runtime_file = self.root / "runtime-settings.json"
        runtime_file.write_text("{broken json")

        from mediaforce.core.config import update_runtime_settings

        updated = update_runtime_settings(runtime_file, lambda current: {**current, "remote_hosts": [{"label": "recovered"}]})

        self.assertEqual(updated, {"remote_hosts": [{"label": "recovered"}]})
        self.assertEqual(json.loads(runtime_file.read_text()), updated)

    def test_load_config_appends_local_folder_policy_overrides_after_tracked_defaults(self) -> None:
        config_dir = self.root / "config"
        config_dir.mkdir()
        defaults_path = config_dir / "defaults.toml"
        defaults_path.write_text(
            """[config]
include_files = ["folder-defaults.toml"]

[state]
db_path = "library.sqlite3"
run_manifest_dir = "runs"
web_state_dir = "web"
review_dir = "review"
runtime_settings_path = "runtime-settings.json"

[media]
staging_root = "staging"
archive_root = "archive"
output_container = "mkv"

[media.source_roots]
tv = "source/tv"

[video]
encoder = "libsvtav1"

[audio]
keep_languages = ["eng"]

[subtitle]
keep_languages = ["eng"]

[planning]
default_limit = 20

[validation]
require_size_reduction = true
"""
        )
        (config_dir / "folder-defaults.toml").write_text(
            """[[overrides]]
path_prefix = "tv/House"
note = "Tracked default"

[overrides.video]
target_xpsnr = 33.0
"""
        )
        (self.root / "runtime-settings.json").write_text(
            json.dumps(
                {
                    "folder_policy_overrides": [
                        {
                            "path_prefix": "tv/House/Season 2",
                            "note": "Saved from the calibration bench.",
                            "video": {"target_xpsnr": 35.5},
                            "planning": {"default_limit": 12},
                        }
                    ]
                }
            )
        )

        loaded = load_config(defaults_path)

        self.assertEqual([override["path_prefix"] for override in loaded.overrides], ["tv/House", "tv/House/Season 2"])
        resolved = loaded.resolve_policy("tv/House/Season 2/Episode 1.mkv")
        self.assertEqual(resolved["video"]["target_xpsnr"], 35.5)
        self.assertEqual(resolved["planning"]["default_limit"], 12)

    def test_resolve_policy_prefers_more_specific_shipped_defaults_over_broad_local_override(self) -> None:
        config_dir = self.root / "config"
        config_dir.mkdir()
        defaults_path = config_dir / "defaults.toml"
        defaults_path.write_text(
            """[config]
include_files = ["folder-defaults.toml"]

[state]
db_path = "library.sqlite3"
run_manifest_dir = "runs"
web_state_dir = "web"
review_dir = "review"
runtime_settings_path = "runtime-settings.json"

[media]
staging_root = "staging"
archive_root = "archive"
output_container = "mkv"

[media.source_roots]
tv = "source/tv"

[video]
encoder = "libsvtav1"

[audio]
keep_languages = ["eng"]

[subtitle]
keep_languages = ["eng"]

[planning]
default_limit = 20

[validation]
require_size_reduction = true
"""
        )
        (config_dir / "folder-defaults.toml").write_text(
            """[[overrides]]
path_prefix = "tv/House"
note = "Tracked broad default"

[overrides.video]
target_xpsnr = 33.0

[[overrides]]
path_prefix = "tv/House/Season 2"
note = "Tracked specific default"

[overrides.video]
target_xpsnr = 36.0
"""
        )
        (self.root / "runtime-settings.json").write_text(
            json.dumps(
                {
                    "folder_policy_overrides": [
                        {
                            "path_prefix": "tv/House",
                            "note": "Saved broad local override",
                            "video": {"target_xpsnr": 40.0},
                        }
                    ]
                }
            )
        )

        loaded = load_config(defaults_path)

        season_two = loaded.resolve_policy("tv/House/Season 2/Episode 1.mkv")
        self.assertEqual(season_two["video"]["target_xpsnr"], 36.0)

        season_six = loaded.resolve_policy("tv/House/Season 6/Episode 1.mkv")
        self.assertEqual(season_six["video"]["target_xpsnr"], 40.0)


if __name__ == "__main__":
    unittest.main()
