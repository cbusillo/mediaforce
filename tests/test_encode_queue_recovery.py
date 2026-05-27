import errno
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, cast
from unittest.mock import Mock, patch

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update

from mediaforce import execution, quality, remote, review, state_cleanup
from mediaforce.core import binaries
from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import calibration_jobs
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.db_tables import item_events
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import scan_runs
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.core.models import ProbeSummary
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.encoding import manifest as manifest
from mediaforce.encoding import quality_search
from mediaforce.encoding import staging as staging_runtime
from mediaforce.encoding import video_filters
from mediaforce.encoding.encode_queue import clear_terminal_encode_jobs_for_prefix, list_child_encode_jobs, \
    load_active_encode_job_for_prefix, load_encode_job, \
    load_latest_terminal_encode_job_for_prefix, load_queue_state, repair_persisted_encode_job_hosts, \
    save_encode_job, save_queue_state
from mediaforce.encoding.quality import QualitySearchResult, SampleEncodeResult
from mediaforce.hosts import status_runtime as host_status_runtime
from mediaforce.remote import HostStatus
from mediaforce.tuning.calibration_jobs import list_queue_summary
from mediaforce.review import BrowserReviewClip, CompareClip, EncodedPreviewClip
from mediaforce.web import app as web_app
from mediaforce.web import settings_runtime
from mediaforce.web.runtime import dashboard_payloads, encode_runtime, folder_actions as folder_actions_runtime, \
    host_runtime as host_runtime_module, job_runtime, queue_actions as queue_actions_runtime, \
    calibration_runtime
from mediaforce.web.runtime import folder_cards as folder_cards_runtime
from mediaforce.web.runtime.worker_leadership import WorkerLeadershipLease


class EncodeQueueRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self._build_config()
        web_app._reset_background_worker_leadership_for_tests()

    def tearDown(self) -> None:
        web_app._reset_background_worker_leadership_for_tests()
        binaries.media_binary.cache_clear()
        self.tempdir.cleanup()

    def test_media_binary_falls_back_when_preferred_homebrew_binary_is_broken(self) -> None:
        binaries.media_binary.cache_clear()
        preferred = self.root / "ffmpeg-full" / "ffprobe"
        discovered = self.root / "bin" / "ffprobe"
        preferred.parent.mkdir(parents=True)
        discovered.parent.mkdir(parents=True)
        preferred.write_text("broken", encoding="utf-8")
        discovered.write_text("ok", encoding="utf-8")

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 1 if cmd[0] == str(preferred) else 0)

        with patch.dict(binaries._HOMEBREW_BINARIES, {"ffprobe": preferred}), patch(
                "mediaforce.core.binaries.shutil.which", return_value=str(discovered)
        ), patch("mediaforce.core.binaries.subprocess.run", side_effect=fake_run):
            self.assertEqual(binaries.ffprobe_binary(), str(discovered))

    def test_media_binary_uses_preferred_homebrew_binary_when_it_runs(self) -> None:
        binaries.media_binary.cache_clear()
        preferred = self.root / "ffmpeg-full" / "ffprobe"
        preferred.parent.mkdir(parents=True)
        preferred.write_text("ok", encoding="utf-8")

        with patch.dict(binaries._HOMEBREW_BINARIES, {"ffprobe": preferred}), patch(
                "mediaforce.core.binaries.shutil.which", return_value="/usr/bin/ffprobe"
        ), patch(
                "mediaforce.core.binaries.subprocess.run",
                return_value=subprocess.CompletedProcess([str(preferred), "-version"], 0),
        ):
            self.assertEqual(binaries.ffprobe_binary(), str(preferred))

    @staticmethod
    def _noop_load_job_state(
            _connection: DBClient,
            _config: MediaforceConfig,
            _prefix: str,
    ) -> folder_actions_runtime.JobPayload | None:
        return None

    @staticmethod
    def _accepted_calibration_state(
            _config: MediaforceConfig,
            _prefix: str,
    ) -> folder_actions_runtime.ActionPayload | None:
        return {"policy": {}, "accepted_at": web_app._now_iso()}

    @staticmethod
    def _accepted_review_gate(
            _calibration: folder_actions_runtime.ActionPayload | None,
    ) -> folder_actions_runtime.ActionPayload:
        return {
            "can_confirm_full": True,
            "message": None,
            "status": "accepted",
            "next_action_label": "Auto-queued after approval",
        }

    @staticmethod
    def _folder_card_for_delivery_badge(
            *,
            item_count: int,
            pending_count: int,
            statuses: dict[str, int],
    ) -> folder_cards_runtime.FolderCard:
        return folder_cards_runtime.FolderCard(
            prefix="tv/show/Season 10",
            title="Season 10",
            subtitle="TV",
            scope_label="Season",
            item_count=item_count,
            pending_count=pending_count,
            total_size_bytes=0,
            estimated_savings_bytes=0,
            known_saved_bytes=0,
            projected_reclaim_bytes=0,
            average_age_days=0.0,
            sort_score=0.0,
            statuses=statuses,
            video_codecs={},
        )

    @staticmethod
    def _noop_upsert_override(
            _file_path: Path,
            _prefix: str,
            _policy: folder_actions_runtime.ActionPayload,
    ) -> None:
        return None

    @staticmethod
    def _prepare_terminal_encode_job_for_requeue(
            connection: DBClient,
            job: folder_actions_runtime.JobPayload,
    ) -> None:
        encode_runtime.prepare_terminal_encode_job_for_requeue(
            connection,
            job,
            deps=web_app._encode_queue_runtime_deps(),
        )

    def test_restart_recovery_requeues_running_job_and_cleans_transients(self) -> None:
        source_path = self._create_source_file("episode-a.mkv")
        staging_path = self._staging_path("episode-a.mkv")
        partial_path = staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}")
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("staged")
        partial_path.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoded")
            self._write_manifest(
                "manifest-a.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._insert_staged_artifact(connection, item_id, staging_path)
            self._save_job(
                connection,
                job_id="job-restart",
                manifest_name="manifest-a.json",
                host={"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                status="running",
                attempt_count=1,
                lease_expires_at="2026-03-25T00:00:00+00:00",
            )
            state = load_queue_state(connection)
            state.update({"active_job_id": "job-restart", "stop_requested": True, "updated_at": web_app._now_iso()})
            save_queue_state(connection, state)

            web_app._recover_encode_queue(connection, self.config)

            job = load_encode_job(connection, "job-restart")
            self.assertIsNotNone(job)
            assert job is not None
            self.assertEqual(job["status"], "retry_backoff")
            self.assertEqual(job["last_failure_kind"], "worker_restart")
            self.assertEqual(job["last_host"]["key"], "remote-a")
            self.assertFalse(staging_path.exists())
            self.assertFalse(partial_path.exists())
            stage_row = self._staged_artifact_value(connection, item_id, staged_artifacts.c.promoted_at)
            self.assertIsNone(stage_row)
            item_status_row = self._library_item_value(connection, item_id, library_items.c.status)
            assert item_status_row is not None
            item_status = item_status_row["status"]
            self.assertEqual(item_status, "planned")
            queue_state = load_queue_state(connection)
            self.assertIsNone(queue_state["active_job_id"])
            self.assertFalse(queue_state["stop_requested"])

    def test_stale_lease_reconciler_requeues_running_job(self) -> None:
        source_path = self._create_source_file("episode-b.mkv")
        staging_path = self._staging_path("episode-b.mkv")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._write_manifest(
                "manifest-b.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._save_job(
                connection,
                job_id="job-stale",
                manifest_name="manifest-b.json",
                host={"key": "local", "label": "Local", "mode": "local"},
                status="running",
                attempt_count=1,
                lease_expires_at="2000-01-01T00:00:00+00:00",
            )

            web_app._reconcile_encode_jobs(connection, self.config)

            job = load_encode_job(connection, "job-stale")
            self.assertIsNotNone(job)
            assert job is not None
            self.assertEqual(job["status"], "retry_backoff")
            self.assertEqual(job["last_failure_kind"], "stale_lease")
            self.assertIn("stale worker lease", str(job["waiting_reason"]))

    def test_retry_backoff_promotes_job_to_queued_when_ready(self) -> None:
        source_path = self._create_source_file("episode-c.mkv")
        staging_path = self._staging_path("episode-c.mkv")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path)
            self._write_manifest(
                "manifest-c.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._save_job(
                connection,
                job_id="job-backoff",
                manifest_name="manifest-c.json",
                host={"key": "local", "label": "Local", "mode": "local"},
                status="retry_backoff",
                attempt_count=1,
                retry_not_before="2000-01-01T00:00:00+00:00",
                waiting_reason="retrying",
            )

            web_app._reconcile_encode_jobs(connection, self.config)

            job = load_encode_job(connection, "job-backoff")
            self.assertIsNotNone(job)
            assert job is not None
            self.assertEqual(job["status"], "queued")
            self.assertIsNone(job["retry_not_before"])
            self.assertIsNone(job["waiting_reason"])

    def test_retry_backoff_recleans_partial_before_queueing(self) -> None:
        source_path = self._create_source_file("episode-backoff-cleanup.mkv")
        staging_path = self._staging_path("episode-backoff-cleanup.mkv")
        partial_path = staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}")
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("staged")
        partial_path.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._insert_staged_artifact(connection, item_id, staging_path)
            self._write_manifest(
                "manifest-backoff-cleanup.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._save_job(
                connection,
                job_id="job-backoff-cleanup",
                manifest_name="manifest-backoff-cleanup.json",
                host={"key": "local", "label": "Local", "mode": "local"},
                status="retry_backoff",
                attempt_count=1,
                retry_not_before="2000-01-01T00:00:00+00:00",
                waiting_reason="retrying",
            )

            web_app._reconcile_encode_jobs(connection, self.config)

            job = load_encode_job(connection, "job-backoff-cleanup")
            item_status_row = self._library_item_value(connection, item_id, library_items.c.status)

        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["status"], "queued")
        self.assertFalse(staging_path.exists())
        self.assertFalse(partial_path.exists())
        assert item_status_row is not None
        self.assertEqual(item_status_row["status"], "planned")

    def test_recover_encode_queue_sweeps_processes_for_interrupted_prefixes(self) -> None:
        source_path = self._create_source_file("episode-restart-sweep.mkv")
        staging_path = self._staging_path("episode-restart-sweep.mkv")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._write_manifest(
                "manifest-restart-sweep.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._save_job(
                connection,
                job_id="job-restart-sweep",
                manifest_name="manifest-restart-sweep.json",
                host={"key": "remote", "label": "Remote", "mode": "ssh"},
                status="running",
                attempt_count=1,
                lease_expires_at="2999-01-01T00:00:00+00:00",
            )

            with patch("mediaforce.web.app._sweep_orphaned_encode_processes") as sweep_mock:
                web_app._recover_encode_queue(connection, self.config)

            job = load_encode_job(connection, "job-restart-sweep")

        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["status"], "retry_backoff")
        sweep_mock.assert_called_once_with(self.config, prefixes=["tv/show"])

    def test_reconcile_encode_jobs_clears_stale_encoding_items_when_idle(self) -> None:
        source_path = self._create_source_file("episode-idle-clear.mkv")
        staging_path = self._staging_path("episode-idle-clear.mkv")
        partial_path = staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}")
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("staged")
        partial_path.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._insert_staged_artifact(connection, item_id, staging_path)

            web_app._reconcile_encode_jobs(connection, self.config)

            item_status_row = self._library_item_value(connection, item_id, library_items.c.status)
            assert item_status_row is not None
            self.assertEqual(item_status_row["status"], "planned")
            self.assertIsNone(self._staged_artifact_value(connection, item_id, staged_artifacts.c.staging_path))
        self.assertFalse(staging_path.exists())
        self.assertFalse(partial_path.exists())

    def test_reconcile_encode_jobs_skips_promoted_stale_encoding_rows(self) -> None:
        source_path = self._create_source_file("episode-idle-promoted.mkv")
        staging_path = self._staging_path("episode-idle-promoted.mkv")
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("staged")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._insert_staged_artifact(connection, item_id, staging_path)
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == item_id)
                .values(promoted_at=web_app._now_iso())
            )

            cleared_count = encode_runtime.clear_stale_encoding_items_when_idle(
                connection,
                self.config,
                web_app._encode_queue_runtime_deps(),
            )

            item_status_row = self._library_item_value(connection, item_id, library_items.c.status)
            assert item_status_row is not None
            self.assertEqual(cleared_count, 0)
            self.assertEqual(item_status_row["status"], "encoding")
            self.assertIsNotNone(self._staged_artifact_value(connection, item_id, staged_artifacts.c.staging_path))
        self.assertTrue(staging_path.exists())

    def test_reconcile_encode_jobs_ignores_unlink_errors_during_stale_cleanup(self) -> None:
        first_source = self._create_source_file("episode-stale-first.mkv")
        second_source = self._create_source_file("episode-stale-second.mkv")
        first_staging = self._staging_path("episode-stale-first.mkv")
        second_staging = self._staging_path("episode-stale-second.mkv")
        for path in (first_staging, second_staging):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("staged")

        with open_db(self.config.paths.db_path) as connection:
            first_id = self._insert_library_item(connection, first_source, status="encoding")
            second_id = self._insert_library_item(connection, second_source, status="encoding")
            self._insert_staged_artifact(connection, first_id, first_staging)
            self._insert_staged_artifact(connection, second_id, second_staging)

            def _unlink_with_one_failure(target_path: Path) -> None:
                if target_path == first_staging:
                    raise OSError("blocked")
                target_path.unlink(missing_ok=True)

            with patch("mediaforce.web.runtime.encode_runtime.safe_unlink", side_effect=_unlink_with_one_failure):
                web_app._reconcile_encode_jobs(connection, self.config)

            for item_id in (first_id, second_id):
                item_status_row = self._library_item_value(connection, item_id, library_items.c.status)
                assert item_status_row is not None
                self.assertEqual(item_status_row["status"], "planned")
                self.assertIsNone(self._staged_artifact_value(connection, item_id, staged_artifacts.c.staging_path))

        self.assertTrue(first_staging.exists())
        self.assertFalse(second_staging.exists())

    def test_reconcile_encode_jobs_prunes_empty_quality_temp_dirs(self) -> None:
        source_path = self._create_source_file("episode-temp-dir.mkv")
        temp_dir = self.root / "staging" / ".mediaforce-ab-av1-stale"
        staging_path = temp_dir / "episode-temp-dir.mkv"
        partial_path = temp_dir / "episode-temp-dir.partial.mkv"
        temp_dir.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("staged")
        partial_path.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._insert_staged_artifact(connection, item_id, staging_path)

            web_app._reconcile_encode_jobs(connection, self.config)

            item_status_row = self._library_item_value(connection, item_id, library_items.c.status)
            assert item_status_row is not None
            self.assertEqual(item_status_row["status"], "planned")
            self.assertIsNone(self._staged_artifact_value(connection, item_id, staged_artifacts.c.staging_path))

        self.assertFalse(staging_path.exists())
        self.assertFalse(partial_path.exists())
        self.assertFalse(temp_dir.exists())

    def test_reconcile_encode_jobs_prunes_empty_quality_temp_dirs_when_files_are_already_missing(self) -> None:
        source_path = self._create_source_file("episode-temp-dir-missing.mkv")
        temp_dir = self.root / "staging" / ".mediaforce-ab-av1-missing"
        staging_path = temp_dir / "episode-temp-dir-missing.mkv"
        temp_dir.mkdir(parents=True, exist_ok=True)

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._insert_staged_artifact(connection, item_id, staging_path)

            web_app._reconcile_encode_jobs(connection, self.config)

            item_status_row = self._library_item_value(connection, item_id, library_items.c.status)
            assert item_status_row is not None
            self.assertEqual(item_status_row["status"], "planned")
            self.assertIsNone(self._staged_artifact_value(connection, item_id, staged_artifacts.c.staging_path))

        self.assertFalse(temp_dir.exists())

    def test_reconcile_encode_jobs_cleans_host_staging_output_without_staged_artifact_row(self) -> None:
        source_path = self._create_source_file("episode-host-orphan.mkv")
        self.config.raw["remote_hosts"] = [
            {
                "mode": "ssh",
                "host": "encode-remote",
                "label": "Encode Remote",
                "staging_root": str(self.root / "remote-staging"),
                "capabilities": ["encode_queue"],
            }
        ]
        host_staging_path = self.root / "remote-staging" / "tv" / "show" / "episode-host-orphan.mkv"
        host_partial_path = self.root / "remote-staging" / "tv" / "show" / "episode-host-orphan.partial.mkv"
        host_staging_path.parent.mkdir(parents=True, exist_ok=True)
        host_staging_path.write_text("staged")
        host_partial_path.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")

            with patch("mediaforce.web.runtime.encode_runtime.run_remote_command") as run_remote_command_mock:
                web_app._reconcile_encode_jobs(connection, self.config)

            item_status_row = self._library_item_value(connection, item_id, library_items.c.status)
            assert item_status_row is not None
            self.assertEqual(item_status_row["status"], "planned")
            self.assertEqual(run_remote_command_mock.call_count, 2)
            scripted_paths = "\n".join(str(call.args[1][2]) for call in run_remote_command_mock.call_args_list)
            self.assertIn(str(host_staging_path), scripted_paths)
            self.assertIn(str(host_partial_path), scripted_paths)

        self.assertTrue(host_staging_path.exists())
        self.assertTrue(host_partial_path.exists())

    def test_reconcile_encode_jobs_keeps_stream_host_cleanup_local(self) -> None:
        source_path = self._create_source_file("episode-stream-host.mkv")
        self.config.raw["remote_hosts"] = [
            {
                "mode": "ssh",
                "host": "stream-remote",
                "label": "Stream Remote",
                "media_access": "stream",
                "staging_root": str(self.root / "stream-staging"),
                "capabilities": ["encode_queue"],
            }
        ]
        staging_path = self.root / "stream-staging" / ".mediaforce-ab-av1-stream" / "episode-stream-host.mkv"
        partial_path = self.root / "stream-staging" / ".mediaforce-ab-av1-stream" / "episode-stream-host.partial.mkv"
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("staged")
        partial_path.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._insert_staged_artifact(connection, item_id, staging_path)

            with patch("mediaforce.web.runtime.encode_runtime.run_remote_command") as run_remote_command_mock:
                web_app._reconcile_encode_jobs(connection, self.config)

            item_status_row = self._library_item_value(connection, item_id, library_items.c.status)
            assert item_status_row is not None
            self.assertEqual(item_status_row["status"], "planned")
            self.assertIsNone(self._staged_artifact_value(connection, item_id, staged_artifacts.c.staging_path))
            run_remote_command_mock.assert_not_called()

        self.assertFalse(staging_path.exists())
        self.assertFalse(partial_path.exists())
        self.assertFalse(staging_path.parent.exists())

    def test_remove_remote_stale_staging_path_only_prunes_temp_parents(self) -> None:
        host = {"mode": "ssh", "host": "encode-remote"}
        ordinary_path = Path("/srv/media/transcode/top-level-file.mkv")
        temp_path = Path("/srv/media/transcode/.mediaforce-ab-av1-stale/clip.mkv")

        with patch("mediaforce.web.runtime.encode_runtime.run_remote_command") as run_remote_command_mock:
            encode_runtime._remove_remote_stale_staging_path(ordinary_path, host)
            encode_runtime._remove_remote_stale_staging_path(temp_path, host)

        self.assertEqual(run_remote_command_mock.call_count, 2)
        ordinary_script = str(run_remote_command_mock.call_args_list[0].args[1][2])
        temp_script = str(run_remote_command_mock.call_args_list[1].args[1][2])
        self.assertIn("rm -f /srv/media/transcode/top-level-file.mkv", ordinary_script)
        self.assertNotIn("rmdir", ordinary_script)
        self.assertIn("rm -f /srv/media/transcode/.mediaforce-ab-av1-stale/clip.mkv", temp_script)
        self.assertIn("rmdir /srv/media/transcode/.mediaforce-ab-av1-stale", temp_script)

    def test_host_selection_prefers_other_encode_capable_host_during_cooldown(self) -> None:
        job = {
            "last_host": {"key": "remote-a", "label": "Remote A", "host": "remote-a"},
            "host_cooldown_until": "2999-01-01T00:00:00+00:00",
        }
        statuses = [
            {
                "key": "remote-a",
                "label": "Remote A",
                "priority": 90,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
            {
                "key": "remote-b",
                "label": "Remote B",
                "priority": 70,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
            {
                "key": "calibration-only",
                "label": "Calibration Only",
                "priority": 100,
                "capabilities": ["sample_calibration"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": False,
            },
        ]
        with open_db(self.config.paths.db_path) as connection:
            with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
                host_payload, waiting_reason = web_app._select_encode_host(connection, self.config, job)
        self.assertIsNotNone(host_payload)
        assert host_payload is not None
        self.assertEqual(host_payload["key"], "remote-b")
        self.assertIsNone(waiting_reason)

    def test_host_selection_uses_backup_host_after_repeated_transient_failures(self) -> None:
        job = {}
        statuses = [
            {
                "key": "remote-a",
                "label": "Remote A",
                "priority": 90,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
            {
                "key": "remote-b",
                "label": "Remote B",
                "priority": 70,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
        ]
        manifest_path = self._write_manifest("manifest-global-host-backoff.json", [{"library_item_id": 1}])
        second_manifest_path = self._write_manifest("manifest-global-host-backoff-b.json", [{"library_item_id": 2}])
        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "cooling-remote-a",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": None,
                    "status": "queued",
                    "manifest_path": str(manifest_path),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": [0],
                    "host": {},
                    "last_host": {"key": "remote-a", "label": "Remote A", "host": "remote-a"},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 2,
                    "process_pid": None,
                    "error": "ssh: connect to host remote-a port 22: Operation timed out",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": "ssh_transport",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": "2999-01-01T00:00:00+00:00",
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": None,
                    "updated_at": web_app._now_iso(),
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "cooling-remote-a-b",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": None,
                    "status": "queued",
                    "manifest_path": str(second_manifest_path),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": [0],
                    "host": {},
                    "last_host": {"key": "remote-a", "label": "Remote A", "host": "remote-a"},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": "Read from remote host remote-a: Operation timed out",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": "ssh_transport",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": "2999-01-01T00:00:00+00:00",
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": None,
                    "updated_at": web_app._now_iso(),
                },
            )
            with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
                host_payload, waiting_reason = web_app._select_encode_host(connection, self.config, job)
        self.assertIsNotNone(host_payload)
        assert host_payload is not None
        self.assertEqual(host_payload["key"], "remote-b")
        self.assertIsNone(waiting_reason)

    def test_host_selection_does_not_globally_demote_host_after_single_transient_failure(self) -> None:
        job = {}
        statuses = [
            {
                "key": "remote-a",
                "label": "Remote A",
                "priority": 90,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
            {
                "key": "remote-b",
                "label": "Remote B",
                "priority": 70,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
        ]
        manifest_path = self._write_manifest("manifest-single-host-blip.json", [{"library_item_id": 1}])
        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "single-blip-remote-a",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": None,
                    "status": "queued",
                    "manifest_path": str(manifest_path),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": [0],
                    "host": {},
                    "last_host": {"key": "remote-a", "label": "Remote A", "host": "remote-a"},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": "ssh: connect to host remote-a port 22: Operation timed out",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": "ssh_transport",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": "2999-01-01T00:00:00+00:00",
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": None,
                    "updated_at": web_app._now_iso(),
                },
            )
            with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
                host_payload, waiting_reason = web_app._select_encode_host(connection, self.config, job)
        self.assertIsNotNone(host_payload)
        assert host_payload is not None
        self.assertEqual(host_payload["key"], "remote-a")
        self.assertIsNone(waiting_reason)

    def test_host_selection_waits_when_only_repeatedly_failing_host_is_available(self) -> None:
        job = {}
        statuses = [
            {
                "key": "remote-a",
                "label": "Remote A",
                "priority": 90,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
        ]
        manifest_path = self._write_manifest("manifest-only-bad-host.json", [{"library_item_id": 1}])
        second_manifest_path = self._write_manifest("manifest-only-bad-host-b.json", [{"library_item_id": 2}])
        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "only-host-cooling",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": None,
                    "status": "queued",
                    "manifest_path": str(manifest_path),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": [0],
                    "host": {},
                    "last_host": {"key": "remote-a", "label": "Remote A", "host": "remote-a"},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 2,
                    "process_pid": None,
                    "error": "Read from remote host remote-a: Operation timed out",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": "ssh_transport",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": "2999-01-01T00:00:00+00:00",
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": None,
                    "updated_at": web_app._now_iso(),
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "only-host-cooling-b",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": None,
                    "status": "queued",
                    "manifest_path": str(second_manifest_path),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": [0],
                    "host": {},
                    "last_host": {"key": "remote-a", "label": "Remote A", "host": "remote-a"},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": "ssh: connect to host remote-a port 22: Connection refused",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": "ssh_transport",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": "2999-01-01T00:00:00+00:00",
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": None,
                    "updated_at": web_app._now_iso(),
                },
            )
            with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
                host_payload, waiting_reason = web_app._select_encode_host(connection, self.config, job)
        self.assertIsNone(host_payload)
        self.assertEqual(waiting_reason, "waiting for host cooldown to expire on Remote A")

    def test_host_selection_uses_startable_backup_when_active_host_is_globally_blocked(self) -> None:
        job = {}
        statuses = [
            {
                "key": "remote-a",
                "label": "Remote A",
                "priority": 90,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
            {
                "key": "ct103",
                "label": "CT103",
                "priority": 70,
                "capabilities": ["encode_queue"],
                "available": False,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "start_command": "ssh prox-main.shiny pct start 103",
                "schedule_profile": "always",
            },
        ]
        manifest_path = self._write_manifest("manifest-active-blocked-host.json", [{"library_item_id": 1}])
        second_manifest_path = self._write_manifest("manifest-active-blocked-host-b.json", [{"library_item_id": 2}])
        with open_db(self.config.paths.db_path) as connection:
            for job_id, path in (("blocked-remote-a", manifest_path), ("blocked-remote-a-b", second_manifest_path)):
                save_encode_job(
                    connection,
                    {
                        "job_id": job_id,
                        "prefix": "tv/show",
                        "job_kind": "shard",
                        "parent_job_id": None,
                        "status": "queued",
                        "manifest_path": str(path),
                        "item_count": 1,
                        "saved_profile_path": None,
                        "manifest_indexes": [0],
                        "host": {},
                        "last_host": {"key": "remote-a", "label": "Remote A", "host": "remote-a"},
                        "notes": "",
                        "bypass_schedule": False,
                        "attempt_count": 1,
                        "process_pid": None,
                        "error": "ssh: connect to host remote-a port 22: Operation timed out",
                        "leased_at": None,
                        "lease_expires_at": None,
                        "heartbeat_at": None,
                        "worker_id": None,
                        "retry_not_before": None,
                        "waiting_reason": None,
                        "terminal_reason": None,
                        "last_failure_kind": "ssh_transport",
                        "last_failure_at": web_app._now_iso(),
                        "host_cooldown_until": "2999-01-01T00:00:00+00:00",
                        "created_at": web_app._now_iso(),
                        "started_at": web_app._now_iso(),
                        "finished_at": None,
                        "updated_at": web_app._now_iso(),
                    },
                )
            with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
                host_payload, waiting_reason = web_app._select_encode_host(connection, self.config, job)
        self.assertIsNotNone(host_payload)
        assert host_payload is not None
        self.assertEqual(host_payload["key"], "ct103")
        self.assertIsNone(waiting_reason)

    def test_host_selection_blocks_globally_quarantined_host_by_host_alias(self) -> None:
        job = {}
        statuses = [
            {
                "key": "",
                "host": "remote-a.internal",
                "label": "Primary Encoder",
                "priority": 90,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
            {
                "key": "remote-b",
                "host": "remote-b.internal",
                "label": "Backup Encoder",
                "priority": 70,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
        ]
        manifest_path = self._write_manifest("manifest-host-alias-quarantine.json", [{"library_item_id": 1}])
        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "host-alias-cooling-a",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": None,
                    "status": "queued",
                    "manifest_path": str(manifest_path),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": [0],
                    "host": {},
                    "last_host": {"host": "remote-a.internal", "failure_streak": 2},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 2,
                    "process_pid": None,
                    "error": "ssh: connect to host remote-a.internal port 22: Operation timed out",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": "ssh_transport",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": "2999-01-01T00:00:00+00:00",
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": None,
                    "updated_at": web_app._now_iso(),
                },
            )
            with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
                host_payload, waiting_reason = web_app._select_encode_host(connection, self.config, job)
        self.assertIsNotNone(host_payload)
        assert host_payload is not None
        self.assertEqual(host_payload["key"], "remote-b")
        self.assertIsNone(waiting_reason)

    def test_host_selection_merges_global_quarantine_rows_across_alias_drift(self) -> None:
        job = {}
        statuses = [
            {
                "key": "cbusillo@10.0.0.5",
                "host": "10.0.0.5",
                "label": "Primary Encoder",
                "priority": 90,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
            {
                "key": "remote-b",
                "host": "10.0.0.6",
                "label": "Backup Encoder",
                "priority": 70,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
        ]
        manifest_a = self._write_manifest("manifest-merged-quarantine-a.json", [{"library_item_id": 1}])
        manifest_b = self._write_manifest("manifest-merged-quarantine-b.json", [{"library_item_id": 2}])
        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "merged-quarantine-a",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": None,
                    "status": "queued",
                    "manifest_path": str(manifest_a),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": [0],
                    "host": {},
                    "last_host": {"key": "cbusillo@10.0.0.5", "host": "10.0.0.5", "failure_streak": 1},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": "ssh: connect to host 10.0.0.5 port 22: Operation timed out",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": "ssh_transport",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": "2999-01-01T00:00:00+00:00",
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": None,
                    "updated_at": web_app._now_iso(),
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "merged-quarantine-b",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": None,
                    "status": "queued",
                    "manifest_path": str(manifest_b),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": [0],
                    "host": {},
                    "last_host": {"label": "Primary Encoder", "host": "10.0.0.5", "failure_streak": 1},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": "Read from remote host 10.0.0.5: Operation timed out",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": "ssh_transport",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": "2999-01-01T00:00:00+00:00",
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": None,
                    "updated_at": web_app._now_iso(),
                },
            )
            with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
                host_payload, waiting_reason = web_app._select_encode_host(connection, self.config, job)
        self.assertIsNotNone(host_payload)
        assert host_payload is not None
        self.assertEqual(host_payload["key"], "remote-b")
        self.assertIsNone(waiting_reason)

    def test_host_selection_ignores_malformed_last_host_json_in_global_quarantine_scan(self) -> None:
        job = {}
        statuses = [
            {
                "key": "remote-a",
                "label": "Remote A",
                "priority": 90,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            }
        ]
        manifest_path = self._write_manifest("manifest-malformed-host-json.json", [{"library_item_id": 1}])
        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "malformed-host-json",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": None,
                    "status": "queued",
                    "manifest_path": str(manifest_path),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": [0],
                    "host": {},
                    "last_host": {"key": "remote-a", "label": "Remote A"},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": "ssh: connect to host remote-a port 22: Operation timed out",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": "ssh_transport",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": "2999-01-01T00:00:00+00:00",
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": None,
                    "updated_at": web_app._now_iso(),
                },
            )
            connection.execute(
                update(encode_jobs)
                .where(encode_jobs.c.job_id == "malformed-host-json")
                .values(last_host_json="{")
            )
            with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
                host_payload, waiting_reason = web_app._select_encode_host(connection, self.config, job)
        self.assertIsNotNone(host_payload)
        assert host_payload is not None
        self.assertEqual(host_payload["key"], "remote-a")
        self.assertIsNone(waiting_reason)

    def test_global_quarantine_does_not_match_hosts_by_shared_label_only(self) -> None:
        job = {}
        statuses = [
            {
                "key": "remote-a",
                "host": "10.0.0.10",
                "label": "Shared Encoder",
                "priority": 90,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
            {
                "key": "remote-b",
                "host": "10.0.0.20",
                "label": "Shared Encoder",
                "priority": 70,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
        ]
        manifest_a = self._write_manifest("manifest-shared-label-a.json", [{"library_item_id": 1}])
        manifest_b = self._write_manifest("manifest-shared-label-b.json", [{"library_item_id": 2}])
        with open_db(self.config.paths.db_path) as connection:
            for job_id, manifest_path in (("shared-label-cool-a", manifest_a), ("shared-label-cool-b", manifest_b)):
                save_encode_job(
                    connection,
                    {
                        "job_id": job_id,
                        "prefix": "tv/show",
                        "job_kind": "shard",
                        "parent_job_id": None,
                        "status": "queued",
                        "manifest_path": str(manifest_path),
                        "item_count": 1,
                        "saved_profile_path": None,
                        "manifest_indexes": [0],
                        "host": {},
                        "last_host": {"key": "remote-b", "host": "10.0.0.20", "label": "Shared Encoder", "failure_streak": 1},
                        "notes": "",
                        "bypass_schedule": False,
                        "attempt_count": 1,
                        "process_pid": None,
                        "error": "ssh: connect to host 10.0.0.20 port 22: Operation timed out",
                        "leased_at": None,
                        "lease_expires_at": None,
                        "heartbeat_at": None,
                        "worker_id": None,
                        "retry_not_before": None,
                        "waiting_reason": None,
                        "terminal_reason": None,
                        "last_failure_kind": "ssh_transport",
                        "last_failure_at": web_app._now_iso(),
                        "host_cooldown_until": "2999-01-01T00:00:00+00:00",
                        "created_at": web_app._now_iso(),
                        "started_at": web_app._now_iso(),
                        "finished_at": None,
                        "updated_at": web_app._now_iso(),
                    },
                )
            with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
                host_payload, waiting_reason = web_app._select_encode_host(connection, self.config, job)
        self.assertIsNotNone(host_payload)
        assert host_payload is not None
        self.assertEqual(host_payload["key"], "remote-a")
        self.assertIsNone(waiting_reason)

    def test_global_quarantine_wait_message_is_deterministic(self) -> None:
        job = {}
        statuses = [
            {
                "key": "remote-b",
                "host": "10.0.0.20",
                "label": "Backup Encoder",
                "priority": 90,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
            {
                "key": "remote-a",
                "host": "10.0.0.10",
                "label": "Primary Encoder",
                "priority": 80,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
        ]
        manifests = [
            self._write_manifest("manifest-deterministic-wait-a.json", [{"library_item_id": 1}]),
            self._write_manifest("manifest-deterministic-wait-b.json", [{"library_item_id": 2}]),
            self._write_manifest("manifest-deterministic-wait-c.json", [{"library_item_id": 3}]),
            self._write_manifest("manifest-deterministic-wait-d.json", [{"library_item_id": 4}]),
        ]
        rows = [
            ("det-wait-b1", manifests[0], {"key": "remote-b", "host": "10.0.0.20", "label": "Backup Encoder", "failure_streak": 1}, "ssh: connect to host 10.0.0.20 port 22: Operation timed out", "2999-01-02T00:00:00+00:00"),
            ("det-wait-b2", manifests[1], {"key": "remote-b", "host": "10.0.0.20", "label": "Backup Encoder", "failure_streak": 1}, "Read from remote host 10.0.0.20: Operation timed out", "2999-01-02T00:00:00+00:00"),
            ("det-wait-a1", manifests[2], {"key": "remote-a", "host": "10.0.0.10", "label": "Primary Encoder", "failure_streak": 1}, "ssh: connect to host 10.0.0.10 port 22: Operation timed out", "2999-01-01T00:00:00+00:00"),
            ("det-wait-a2", manifests[3], {"key": "remote-a", "host": "10.0.0.10", "label": "Primary Encoder", "failure_streak": 1}, "Read from remote host 10.0.0.10: Operation timed out", "2999-01-01T00:00:00+00:00"),
        ]
        with open_db(self.config.paths.db_path) as connection:
            for job_id, manifest_path, last_host, error_message, cooldown_until in rows:
                save_encode_job(
                    connection,
                    {
                        "job_id": job_id,
                        "prefix": "tv/show",
                        "job_kind": "shard",
                        "parent_job_id": None,
                        "status": "queued",
                        "manifest_path": str(manifest_path),
                        "item_count": 1,
                        "saved_profile_path": None,
                        "manifest_indexes": [0],
                        "host": {},
                        "last_host": last_host,
                        "notes": "",
                        "bypass_schedule": False,
                        "attempt_count": 1,
                        "process_pid": None,
                        "error": error_message,
                        "leased_at": None,
                        "lease_expires_at": None,
                        "heartbeat_at": None,
                        "worker_id": None,
                        "retry_not_before": None,
                        "waiting_reason": None,
                        "terminal_reason": None,
                        "last_failure_kind": "ssh_transport",
                        "last_failure_at": web_app._now_iso(),
                        "host_cooldown_until": cooldown_until,
                        "created_at": web_app._now_iso(),
                        "started_at": web_app._now_iso(),
                        "finished_at": None,
                        "updated_at": web_app._now_iso(),
                    },
                )
            with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
                host_payload, waiting_reason = web_app._select_encode_host(connection, self.config, job)
        self.assertIsNone(host_payload)
        self.assertEqual(waiting_reason, "waiting for host cooldown to expire on Primary Encoder")

    def test_deterministic_failure_moves_job_to_needs_attention(self) -> None:
        source_path = self._create_source_file("episode-d.mkv")
        staging_path = self._staging_path("episode-d.mkv")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._write_manifest(
                "manifest-d.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._save_job(
                connection,
                job_id="job-deterministic",
                manifest_name="manifest-d.json",
                host={"key": "local", "label": "Local", "mode": "local"},
                status="running",
                attempt_count=1,
            )

            job = load_encode_job(connection, "job-deterministic")
            assert job is not None
            web_app._transition_encode_job_failure(
                connection,
                self.config,
                job,
                failure_kind="deterministic",
                error_message="ffmpeg encode failed: invalid stream mapping",
            )

            updated = load_encode_job(connection, "job-deterministic")
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated["status"], "needs_attention")
            self.assertEqual(updated["terminal_reason"], "deterministic")

    def test_quality_policy_near_miss_raises_size_cap_and_retries(self) -> None:
        source_path = self._create_source_file("episode-near-miss.mkv")
        staging_path = self._staging_path("episode-near-miss.mkv")
        error_message = "\n".join(
            [
                "[INFO ab_av1::command::sample_encode] crf 48 VMAF 89.20 predicted video stream size 130.00 MiB (18%) taking 40 minutes",
                "[INFO ab_av1::command::sample_encode] crf 51 VMAF 86.93 predicted video stream size 108.70 MiB (16%) taking 25 minutes",
                "[INFO ab_av1::command::crf_search] crf 51 VMAF 86.93 (16%)",
                "Error: Failed to find a suitable crf",
            ]
        )

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            manifest_path = self._write_manifest(
                "manifest-quality-near-miss.json",
                [
                    {
                        "library_item_id": item_id,
                        "rel_path": "tv/show/episode-near-miss.mkv",
                        "staging_path": str(staging_path),
                        "resolved_policy": {
                            "video": {
                                "quality_metric": "vmaf",
                                "target_vmaf": 88.0,
                                "min_target_vmaf": 86.5,
                                "max_encoded_percent": 15,
                            }
                        },
                    }
                ],
            )
            self._save_job(
                connection,
                job_id="job-quality-near-miss",
                manifest_name="manifest-quality-near-miss.json",
                host={"key": "local", "label": "Local", "mode": "local"},
                status="running",
                attempt_count=1,
            )

            job = load_encode_job(connection, "job-quality-near-miss")
            assert job is not None
            web_app._transition_encode_job_failure(
                connection,
                self.config,
                job,
                failure_kind="deterministic",
                error_message=error_message,
            )

            updated = load_encode_job(connection, "job-quality-near-miss")
            manifest = json.loads(manifest_path.read_text())

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["status"], "retry_backoff")
        self.assertIsNone(updated["terminal_reason"])
        self.assertIn("measured policy adjustment", str(updated["waiting_reason"]))
        failure_analysis = object_dict(object_dict(updated.get("progress")).get("failure_analysis"))
        self.assertEqual(failure_analysis["kind"], "size_cap_too_strict")
        self.assertTrue(failure_analysis["auto_retry_allowed"])
        self.assertEqual(failure_analysis["proposed_max_encoded_percent"], 17)
        self.assertEqual(
            manifest["items"][0]["resolved_policy"]["video"]["max_encoded_percent"],
            17,
        )

    def test_quality_policy_large_size_miss_stays_attention_with_analysis(self) -> None:
        source_path = self._create_source_file("episode-large-miss.mkv")
        staging_path = self._staging_path("episode-large-miss.mkv")
        error_message = "\n".join(
            [
                "[INFO ab_av1::command::sample_encode] crf 44 VMAF 87.30 predicted video stream size 190.00 MiB (24%) taking 40 minutes",
                "Error: Failed to find a suitable crf",
            ]
        )

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._write_manifest(
                "manifest-quality-large-miss.json",
                [
                    {
                        "library_item_id": item_id,
                        "rel_path": "tv/show/episode-large-miss.mkv",
                        "staging_path": str(staging_path),
                        "resolved_policy": {
                            "video": {
                                "quality_metric": "vmaf",
                                "target_vmaf": 88.0,
                                "min_target_vmaf": 86.5,
                                "max_encoded_percent": 15,
                            }
                        },
                    }
                ],
            )
            self._save_job(
                connection,
                job_id="job-quality-large-miss",
                manifest_name="manifest-quality-large-miss.json",
                host={"key": "local", "label": "Local", "mode": "local"},
                status="running",
                attempt_count=1,
            )

            job = load_encode_job(connection, "job-quality-large-miss")
            assert job is not None
            web_app._transition_encode_job_failure(
                connection,
                self.config,
                job,
                failure_kind="deterministic",
                error_message=error_message,
            )

            updated = load_encode_job(connection, "job-quality-large-miss")

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["status"], "needs_attention")
        failure_analysis = object_dict(object_dict(updated.get("progress")).get("failure_analysis"))
        self.assertEqual(failure_analysis["kind"], "size_cap_too_strict")
        self.assertFalse(failure_analysis["auto_retry_allowed"])

    def test_quality_policy_ignores_missing_configured_metric_measurements(self) -> None:
        error_message = "\n".join(
            [
                "[INFO ab_av1::command::sample_encode] crf 42 XPSNR 10.20 predicted video stream size 120.00 MiB (16%) taking 20 minutes",
                "Error: Failed to find a suitable crf",
            ]
        )

        analysis = quality.analyze_quality_policy_failure(
            error_message,
            {
                "quality_metric": "vmaf",
                "target_vmaf": 88.0,
                "min_target_vmaf": 86.5,
                "max_encoded_percent": 15,
            },
        )

        self.assertIsNone(analysis)

    def test_quality_policy_floor_miss_uses_best_score_candidate(self) -> None:
        error_message = "\n".join(
            [
                "[INFO ab_av1::command::sample_encode] crf 48 VMAF 80.00 predicted video stream size 80.00 MiB (8%) taking 12 minutes",
                "[INFO ab_av1::command::sample_encode] crf 40 VMAF 85.90 predicted video stream size 160.00 MiB (18%) taking 25 minutes",
                "Error: Failed to find a suitable crf",
            ]
        )

        analysis = object_dict(
            quality.analyze_quality_policy_failure(
                error_message,
                {
                    "quality_metric": "vmaf",
                    "target_vmaf": 88.0,
                    "min_target_vmaf": 86.5,
                    "max_encoded_percent": 20,
                },
            )
        )

        self.assertEqual(analysis["kind"], "quality_floor_too_strict")
        self.assertEqual(object_dict(analysis["best_candidate"])["score"], 85.9)

    def test_quality_policy_multi_item_retry_updates_each_selected_item_cap(self) -> None:
        first_source_path = self._create_source_file("episode-multi-a.mkv")
        second_source_path = self._create_source_file("episode-multi-b.mkv")
        first_staging_path = self._staging_path("episode-multi-a.mkv")
        second_staging_path = self._staging_path("episode-multi-b.mkv")
        error_message = "\n".join(
            [
                "[INFO ab_av1::command::sample_encode] crf 42 VMAF 87.30 predicted video stream size 126.00 MiB (16.2%) taking 25 minutes",
                "Error: Failed to find a suitable crf",
            ]
        )

        with open_db(self.config.paths.db_path) as connection:
            first_item_id = self._insert_library_item(connection, first_source_path, status="encoding")
            second_item_id = self._insert_library_item(connection, second_source_path, status="encoding")
            manifest_path = self._write_manifest(
                "manifest-quality-multi-miss.json",
                [
                    {
                        "library_item_id": first_item_id,
                        "rel_path": "tv/show/episode-multi-a.mkv",
                        "staging_path": str(first_staging_path),
                        "resolved_policy": {
                            "video": {
                                "quality_metric": "vmaf",
                                "target_vmaf": 88.0,
                                "min_target_vmaf": 86.5,
                                "max_encoded_percent": 15,
                            }
                        },
                    },
                    {
                        "library_item_id": second_item_id,
                        "rel_path": "tv/show/episode-multi-b.mkv",
                        "staging_path": str(second_staging_path),
                        "resolved_policy": {
                            "video": {
                                "quality_metric": "vmaf",
                                "target_vmaf": 88.0,
                                "min_target_vmaf": 86.5,
                                "max_encoded_percent": 16,
                            }
                        },
                    },
                ],
            )
            self._save_job(
                connection,
                job_id="job-quality-multi-miss",
                manifest_name="manifest-quality-multi-miss.json",
                host={"key": "local", "label": "Local", "mode": "local"},
                status="running",
                attempt_count=1,
            )

            job = load_encode_job(connection, "job-quality-multi-miss")
            assert job is not None
            web_app._transition_encode_job_failure(
                connection,
                self.config,
                job,
                failure_kind="deterministic",
                error_message=error_message,
            )

            updated = load_encode_job(connection, "job-quality-multi-miss")
            manifest = json.loads(manifest_path.read_text())

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["status"], "retry_backoff")
        failure_analysis = object_dict(object_dict(updated.get("progress")).get("failure_analysis"))
        self.assertTrue(failure_analysis["auto_retry_allowed"])
        self.assertEqual(len(object_list(failure_analysis["item_analyses"])), 2)
        self.assertEqual(
            manifest["items"][0]["resolved_policy"]["video"]["max_encoded_percent"],
            17,
        )
        self.assertEqual(
            manifest["items"][1]["resolved_policy"]["video"]["max_encoded_percent"],
            17,
        )

    def test_quality_policy_multi_item_partial_analysis_requires_attention(self) -> None:
        first_source_path = self._create_source_file("episode-partial-a.mkv")
        second_source_path = self._create_source_file("episode-partial-b.mkv")
        first_staging_path = self._staging_path("episode-partial-a.mkv")
        second_staging_path = self._staging_path("episode-partial-b.mkv")
        error_message = "\n".join(
            [
                "[INFO ab_av1::command::sample_encode] crf 42 VMAF 87.30 predicted video stream size 126.00 MiB (16.2%) taking 25 minutes",
                "Error: Failed to find a suitable crf",
            ]
        )

        with open_db(self.config.paths.db_path) as connection:
            first_item_id = self._insert_library_item(connection, first_source_path, status="encoding")
            second_item_id = self._insert_library_item(connection, second_source_path, status="encoding")
            manifest_path = self._write_manifest(
                "manifest-quality-partial-miss.json",
                [
                    {
                        "library_item_id": first_item_id,
                        "rel_path": "tv/show/episode-partial-a.mkv",
                        "staging_path": str(first_staging_path),
                        "resolved_policy": {
                            "video": {
                                "quality_metric": "vmaf",
                                "target_vmaf": 88.0,
                                "min_target_vmaf": 86.5,
                                "max_encoded_percent": 15,
                            }
                        },
                    },
                    {
                        "library_item_id": second_item_id,
                        "rel_path": "tv/show/episode-partial-b.mkv",
                        "staging_path": str(second_staging_path),
                        "resolved_policy": {
                            "video": {
                                "quality_metric": "xpsnr",
                                "target_xpsnr": 12.0,
                                "min_target_xpsnr": 11.0,
                                "max_encoded_percent": 15,
                            }
                        },
                    },
                ],
            )
            self._save_job(
                connection,
                job_id="job-quality-partial-miss",
                manifest_name="manifest-quality-partial-miss.json",
                host={"key": "local", "label": "Local", "mode": "local"},
                status="running",
                attempt_count=1,
            )

            job = load_encode_job(connection, "job-quality-partial-miss")
            assert job is not None
            web_app._transition_encode_job_failure(
                connection,
                self.config,
                job,
                failure_kind="deterministic",
                error_message=error_message,
            )

            updated = load_encode_job(connection, "job-quality-partial-miss")
            manifest = json.loads(manifest_path.read_text())

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["status"], "needs_attention")
        failure_analysis = object_dict(object_dict(updated.get("progress")).get("failure_analysis"))
        self.assertFalse(failure_analysis["auto_retry_allowed"])
        self.assertEqual(failure_analysis["retry_strategy"], "needs_operator_approval")
        self.assertIn("1 of 2 selected items", str(failure_analysis["summary"]))
        self.assertEqual(
            manifest["items"][0]["resolved_policy"]["video"]["max_encoded_percent"],
            15,
        )
        self.assertEqual(
            manifest["items"][1]["resolved_policy"]["video"]["max_encoded_percent"],
            15,
        )

    def test_quality_search_failure_is_deterministic_even_for_ssh_host(self) -> None:
        job = {
            "host": {"key": "remote-a", "label": "Remote A", "mode": "ssh"},
        }
        exc = quality.QualitySearchError(
            "ssh remote command exited with status 1\nError: Failed to find a suitable crf"
        )

        self.assertEqual(encode_runtime._classify_encode_failure(exc, job), "deterministic")

    def test_quality_temp_setup_timeout_is_ssh_transport_for_remote_host(self) -> None:
        job = {
            "host": {"key": "remote-a", "label": "Remote A", "mode": "ssh"},
        }
        exc = quality.QualityTempSetupError("mkdir: /Volumes/media/transcode: Operation timed out")

        self.assertEqual(encode_runtime._classify_encode_failure(exc, job), "ssh_transport")

    def test_quality_temp_setup_plain_failure_stays_deterministic(self) -> None:
        job = {
            "host": {"key": "remote-a", "label": "Remote A", "mode": "ssh"},
        }
        exc = quality.QualityTempSetupError("No writable quality temp root available")

        self.assertEqual(encode_runtime._classify_encode_failure(exc, job), "deterministic")

    def test_quality_search_failure_message_is_deterministic_before_ssh_retry(self) -> None:
        job = {
            "host": {"key": "remote-a", "label": "Remote A", "mode": "ssh"},
        }
        exc = RuntimeError("ssh remote command exited with status 1\nError: Failed to find a suitable crf")

        self.assertEqual(encode_runtime._classify_encode_failure(exc, job), "deterministic")

    def test_transient_ssh_failure_stays_in_retry_backoff_after_attempt_cap(self) -> None:
        source_path = self._create_source_file("episode-host-retry.mkv")
        staging_path = self._staging_path("episode-host-retry.mkv")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._write_manifest(
                "manifest-host-retry.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._save_job(
                connection,
                job_id="job-host-retry",
                manifest_name="manifest-host-retry.json",
                host={"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                status="running",
                attempt_count=3,
            )

            job = load_encode_job(connection, "job-host-retry")
            assert job is not None
            web_app._transition_encode_job_failure(
                connection,
                self.config,
                job,
                failure_kind="ssh_transport",
                error_message="ssh: connect to host remote-a port 22: Operation timed out",
            )

            updated = load_encode_job(connection, "job-host-retry")
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated["status"], "retry_backoff")
            self.assertIsNone(updated["terminal_reason"])
            self.assertEqual(updated["last_failure_kind"], "ssh_transport")
            self.assertIn("SSH transport failure", str(updated["waiting_reason"]))
            self.assertIsNone(updated["finished_at"])
            self.assertIsNotNone(updated["host_cooldown_until"])
            self.assertEqual(updated["last_host"]["failure_streak"], 1)

    def test_same_job_repeated_host_failures_increment_last_host_failure_streak(self) -> None:
        source_path = self._create_source_file("episode-host-repeat.mkv")
        staging_path = self._staging_path("episode-host-repeat.mkv")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._write_manifest(
                "manifest-host-repeat.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._save_job(
                connection,
                job_id="job-host-repeat",
                manifest_name="manifest-host-repeat.json",
                host={"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                status="running",
                attempt_count=2,
            )
            job = load_encode_job(connection, "job-host-repeat")
            assert job is not None
            job["last_host"] = {"key": "remote-a", "label": "Remote A", "mode": "ssh", "failure_streak": 1}
            save_encode_job(connection, job)

            web_app._transition_encode_job_failure(
                connection,
                self.config,
                job,
                failure_kind="ssh_transport",
                error_message="ssh: connect to host remote-a port 22: Operation timed out",
            )

            updated = load_encode_job(connection, "job-host-repeat")
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated["status"], "retry_backoff")
            self.assertEqual(updated["last_host"]["failure_streak"], 2)

    def test_same_job_repeated_host_failures_preserve_streak_across_alias_change(self) -> None:
        source_path = self._create_source_file("episode-host-repeat-alias.mkv")
        staging_path = self._staging_path("episode-host-repeat-alias.mkv")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._write_manifest(
                "manifest-host-repeat-alias.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._save_job(
                connection,
                job_id="job-host-repeat-alias",
                manifest_name="manifest-host-repeat-alias.json",
                host={"key": "cbusillo@10.0.0.5", "host": "10.0.0.5", "label": "Primary Encoder", "mode": "ssh"},
                status="running",
                attempt_count=2,
            )
            job = load_encode_job(connection, "job-host-repeat-alias")
            assert job is not None
            job["last_host"] = {"host": "10.0.0.5", "label": "Primary Encoder", "failure_streak": 1}
            save_encode_job(connection, job)

            web_app._transition_encode_job_failure(
                connection,
                self.config,
                job,
                failure_kind="ssh_transport",
                error_message="ssh: connect to host 10.0.0.5 port 22: Operation timed out",
            )

            updated = load_encode_job(connection, "job-host-repeat-alias")
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated["status"], "retry_backoff")
            self.assertEqual(updated["last_host"]["failure_streak"], 2)
            self.assertEqual(updated["last_host"]["host"], "10.0.0.5")
            self.assertEqual(updated["last_host"]["key"], "cbusillo@10.0.0.5")

    def test_host_selection_uses_backup_host_after_single_job_repeated_failures(self) -> None:
        job = {}
        statuses = [
            {
                "key": "remote-a",
                "label": "Remote A",
                "priority": 90,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
            {
                "key": "remote-b",
                "label": "Remote B",
                "priority": 70,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
        ]
        manifest_path = self._write_manifest("manifest-single-job-repeated-host.json", [{"library_item_id": 1}])
        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "single-job-remote-a-repeat",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": None,
                    "status": "queued",
                    "manifest_path": str(manifest_path),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": [0],
                    "host": {},
                    "last_host": {"key": "remote-a", "label": "Remote A", "host": "remote-a", "failure_streak": 2},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 2,
                    "process_pid": None,
                    "error": "Read from remote host remote-a: Operation timed out",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": "ssh_transport",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": "2999-01-01T00:00:00+00:00",
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": None,
                    "updated_at": web_app._now_iso(),
                },
            )
            with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
                host_payload, waiting_reason = web_app._select_encode_host(connection, self.config, job)
        self.assertIsNotNone(host_payload)
        assert host_payload is not None
        self.assertEqual(host_payload["key"], "remote-b")
        self.assertIsNone(waiting_reason)

    def test_permission_denied_ssh_failure_still_needs_attention_after_attempt_cap(self) -> None:
        source_path = self._create_source_file("episode-host-permission.mkv")
        staging_path = self._staging_path("episode-host-permission.mkv")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._write_manifest(
                "manifest-host-permission.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._save_job(
                connection,
                job_id="job-host-permission",
                manifest_name="manifest-host-permission.json",
                host={"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                status="running",
                attempt_count=3,
            )

            job = load_encode_job(connection, "job-host-permission")
            assert job is not None
            web_app._transition_encode_job_failure(
                connection,
                self.config,
                job,
                failure_kind="ssh_transport",
                error_message="Permission denied (publickey).",
            )

            updated = load_encode_job(connection, "job-host-permission")
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated["status"], "needs_attention")
            self.assertEqual(updated["terminal_reason"], "max_attempts_exhausted")

    def test_runtime_settings_payload_normalizes_capabilities(self) -> None:
        payload = web_app._build_runtime_settings_payload(
            libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
            remote_hosts=[
                {
                    "label": "Remote A",
                    "host": "remote-a",
                    "repo_path": "/srv/mediaforce",
                    "wake_mac": "aa:bb:cc:dd:ee:ff",
                    "start_command": "ssh prox-main.shiny pct start 103",
                    "stop_command": "ssh prox-main.shiny pct shutdown 103",
                    "start_timeout_seconds": "240",
                    "priority": "20",
                    "max_parallel_encodes": "3",
                    "schedule_profile": "late_night",
                    "capabilities": "encode_queue, sample_calibration, encode_queue",
                }
            ],
            transcode_root=str(self.root / "staging"),
            encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
            schedule_profiles=[
                {
                    "key": "late_night",
                    "label": "Late Night",
                    "mode": "night",
                    "timezone": "local",
                    "start_hour": "23",
                    "end_hour": "6",
                }
            ],
        )
        self.assertEqual(
            payload["remote_hosts"][0]["capabilities"],
            ["encode_queue", "sample_calibration"],
        )
        self.assertEqual(payload["remote_hosts"][0]["start_command"], "ssh prox-main.shiny pct start 103")
        self.assertEqual(payload["remote_hosts"][0]["stop_command"], "ssh prox-main.shiny pct shutdown 103")
        self.assertEqual(payload["remote_hosts"][0]["start_timeout_seconds"], 240)
        self.assertEqual(payload["remote_hosts"][0]["max_parallel_encodes"], 3)
        self.assertEqual(payload["remote_hosts"][0]["schedule_profile"], "late_night")
        self.assertEqual(payload["encode_queue"]["schedule_profiles"][0]["key"], "late_night")
        self.assertEqual(payload["encode_queue"]["schedule_profiles"][0]["timezone"], "host_local")

    def test_runtime_settings_payload_defaults_host_schedule_to_always(self) -> None:
        payload = web_app._build_runtime_settings_payload(
            libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
            remote_hosts=[
                {
                    "label": "Remote A",
                    "host": "remote-a",
                    "priority": "20",
                    "max_parallel_encodes": "2",
                    "capabilities": ["encode_queue"],
                }
            ],
            transcode_root=str(self.root / "staging"),
            encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
            schedule_profiles=[],
        )
        self.assertEqual(payload["remote_hosts"][0]["schedule_profile"], "always")

    def test_runtime_settings_payload_persists_video_defaults(self) -> None:
        payload = web_app._build_runtime_settings_payload(
            libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
            remote_hosts=[],
            transcode_root=str(self.root / "staging"),
            video_defaults={
                "quality_metric": "VMAF",
                "target_vmaf": "85",
                "min_target_vmaf": "80",
                "target_xpsnr": "41",
                "min_target_xpsnr": "35",
                "target_size_mb": "300",
                "target_runtime_minutes": "45",
                "decision_model": "size_first_review",
                "quality_engine": "ab_av1_fast_sample",
                "max_height": "1080",
                "default_grain": "8",
                "max_encoded_percent": "80",
            },
            encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
            schedule_profiles=[],
        )

        self.assertEqual(
            payload["video"],
            {
                "quality_metric": "vmaf",
                "target_vmaf": 85.0,
                "min_target_vmaf": 80.0,
                "target_xpsnr": 41.0,
                "min_target_xpsnr": 35.0,
                "target_size_mb": 300.0,
                "target_runtime_minutes": 45.0,
                "decision_model": "size_first_review",
                "quality_engine": "ab_av1_fast_sample",
                "max_height": 1080,
                "default_grain": 8,
                "max_encoded_percent": 80.0,
            },
        )

    def test_runtime_settings_payload_persists_xpsnr_video_defaults(self) -> None:
        payload = web_app._build_runtime_settings_payload(
            libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
            remote_hosts=[],
            transcode_root=str(self.root / "staging"),
            video_defaults={
                "quality_metric": "XPSNR",
                "target_vmaf": "85",
                "min_target_vmaf": "80",
                "target_xpsnr": "39.5",
                "min_target_xpsnr": "34.5",
                "target_size_mb": "300",
                "target_runtime_minutes": "45",
                "decision_model": "size_first_review",
                "quality_engine": "ab_av1_fast_sample",
                "max_height": "1080",
                "default_grain": "8",
                "max_encoded_percent": "80",
            },
            encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
            schedule_profiles=[],
        )

        self.assertEqual(payload["video"]["quality_metric"], "xpsnr")
        self.assertEqual(payload["video"]["target_xpsnr"], 39.5)
        self.assertEqual(payload["video"]["min_target_xpsnr"], 34.5)

    def test_runtime_settings_payload_rejects_unsupported_video_metric(self) -> None:
        payload = web_app._build_runtime_settings_payload(
            libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
            remote_hosts=[],
            transcode_root=str(self.root / "staging"),
            video_defaults={"quality_metric": "ssim"},
            encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
            schedule_profiles=[],
        )

        self.assertEqual(payload["video"]["quality_metric"], "auto")

    def test_runtime_settings_payload_defaults_video_metric_to_auto(self) -> None:
        payload = web_app._build_runtime_settings_payload(
            libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
            remote_hosts=[],
            transcode_root=str(self.root / "staging"),
            encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
            schedule_profiles=[],
        )

        self.assertEqual(payload["video"]["quality_metric"], "auto")

    def test_runtime_settings_payload_defaults_to_size_first_review_model(self) -> None:
        payload = web_app._build_runtime_settings_payload(
            libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
            remote_hosts=[],
            transcode_root=str(self.root / "staging"),
            video_defaults={
                "target_size_mb": "300",
                "target_runtime_minutes": "45",
                "decision_model": "size_first_review",
                "quality_engine": "ab_av1_fast_sample",
            },
            encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
            schedule_profiles=[],
        )

        self.assertEqual(payload["video"]["target_size_mb"], 300.0)
        self.assertEqual(payload["video"]["target_runtime_minutes"], 45.0)
        self.assertEqual(payload["video"]["decision_model"], "size_first_review")
        self.assertEqual(payload["video"]["quality_engine"], "ab_av1_fast_sample")

    def test_runtime_settings_payload_normalizes_media_access(self) -> None:
        payload = web_app._build_runtime_settings_payload(
            libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
            remote_hosts=[
                {
                    "label": "Remote A",
                    "host": "remote-a",
                    "priority": "20",
                    "max_parallel_encodes": "2",
                    "capabilities": ["encode_queue"],
                    "media_access": "stream",
                }
            ],
            transcode_root=str(self.root / "staging"),
            encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
            schedule_profiles=[],
        )
        self.assertEqual(payload["remote_hosts"][0]["media_access"], "stream")

    def test_runtime_settings_payload_keeps_host_path_overrides_optional(self) -> None:
        payload = web_app._build_runtime_settings_payload(
            libraries=[
                {"key": "movies", "path": str(self.root / "source" / "movies")},
                {"key": "tv", "path": str(self.root / "source" / "tv")},
            ],
            remote_hosts=[
                {
                    "label": "Remote A",
                    "host": "remote-a",
                    "priority": "20",
                    "max_parallel_encodes": "2",
                    "capabilities": ["encode_queue"],
                    "staging_root": str(self.root / "remote-staging"),
                    "source_roots_json": json.dumps(
                        {
                            "movies": str(self.root / "remote-movies"),
                            "tv": str(self.root / "remote-tv"),
                        }
                    ),
                }
            ],
            transcode_root=str(self.root / "staging"),
            encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
            schedule_profiles=[],
        )
        self.assertEqual(
            payload["remote_hosts"][0]["source_roots"],
            {
                "movies": str((self.root / "remote-movies").expanduser()),
                "tv": str((self.root / "remote-tv").expanduser()),
            },
        )
        self.assertEqual(payload["remote_hosts"][0]["staging_root"], str(self.root / "remote-staging"))

    def test_runtime_settings_payload_rejects_unknown_library_override(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown library override"):
            web_app._build_runtime_settings_payload(
                libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
                remote_hosts=[
                    {
                        "label": "Remote A",
                        "host": "remote-a",
                        "priority": "20",
                        "max_parallel_encodes": "2",
                        "capabilities": ["encode_queue"],
                        "source_roots_json": json.dumps({"movies": "/srv/media/movies"}),
                    }
                ],
                transcode_root=str(self.root / "staging"),
                encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
                schedule_profiles=[],
            )

    def test_runtime_settings_payload_rejects_unknown_host_schedule_profile(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown schedule profile"):
            web_app._build_runtime_settings_payload(
                libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
                remote_hosts=[
                    {
                        "label": "Remote A",
                        "host": "remote-a",
                        "priority": "20",
                        "max_parallel_encodes": "2",
                        "schedule_profile": "missing_profile",
                        "capabilities": "encode_queue",
                    }
                ],
                transcode_root=str(self.root / "staging"),
                encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
                schedule_profiles=[],
            )

    def test_runtime_settings_payload_rejects_reserved_schedule_profile_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "reserved"):
            web_app._build_runtime_settings_payload(
                libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
                remote_hosts=[],
                transcode_root=str(self.root / "staging"),
                encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
                schedule_profiles=[
                    {
                        "key": "never",
                        "label": "Should Not Override",
                        "start_hour": "0",
                        "end_hour": "0",
                    }
                ],
            )

    def test_runtime_settings_payload_preserves_allowed_libraries_for_host(self) -> None:
        payload = web_app._build_runtime_settings_payload(
            libraries=[
                {"key": "tv", "path": str(self.root / "source" / "tv")},
                {"key": "movies", "path": str(self.root / "source" / "movies")},
            ],
            remote_hosts=[
                {
                    "label": "Remote A",
                    "host": "remote-a",
                    "priority": "20",
                    "max_parallel_encodes": "2",
                    "schedule_profile": "never",
                    "capabilities": ["encode_queue"],
                    "allowed_libraries": ["tv"],
                }
            ],
            transcode_root=str(self.root / "staging"),
            encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
            schedule_profiles=[],
        )

        self.assertEqual(payload["remote_hosts"][0]["schedule_profile"], "never")
        self.assertEqual(payload["remote_hosts"][0]["allowed_libraries"], ["tv"])

    def test_settings_helpers_tolerate_malformed_runtime_values(self) -> None:
        self.config.raw["media"]["source_roots"] = ["not-a-mapping"]
        self.config.raw["media"]["staging_root"] = {"bad": "shape"}
        self.config.raw["remote_hosts"] = ["not-a-host-row"]

        library_rows = web_app._settings_library_rows_for_config(self.config)
        remote_rows = web_app._settings_remote_rows_for_config(self.config)
        transcode_root = web_app._settings_transcode_root_value(self.config)
        archive_root = web_app._settings_archive_root(transcode_root)
        host_statuses = web_app._safe_collect_host_statuses(self.config)

        self.assertEqual(library_rows[0]["key"], "")
        self.assertEqual(library_rows[0]["path"], "")
        self.assertEqual(remote_rows[0]["host"], "")
        self.assertEqual(transcode_root, "")
        self.assertEqual(archive_root, "")
        self.assertEqual(len(host_statuses), 1)
        self.assertEqual(host_statuses[0].key, "host-status-error")
        self.assertFalse(host_statuses[0].available)

    def test_settings_rows_map_legacy_default_schedule_to_always(self) -> None:
        self.config.raw["remote_hosts"] = [
            {
                "host": "remote-a",
                "label": "Remote A",
                "schedule_profile": "default",
                "capabilities": ["encode_queue", "sample_calibration"],
            }
        ]

        remote_rows = web_app._settings_remote_rows_for_config(self.config)

        self.assertEqual(remote_rows[0]["schedule_profile"], "always")
        self.assertEqual(remote_rows[0]["capabilities"], ["encode_queue", "sample_calibration"])

    def test_schedule_profile_options_include_built_in_never(self) -> None:
        options = settings_runtime.schedule_profile_options(schedule_profiles=[])

        self.assertEqual(options[0]["key"], "always")
        self.assertEqual(options[1]["key"], "never")

    def test_settings_schedule_rows_skip_reserved_never_profile(self) -> None:
        self.config.raw["encode_queue"]["schedule_profiles"] = [
            {
                "key": "never",
                "label": "Should Not Surface",
                "mode": "night",
                "timezone": "host_local",
                "start_hour": 1,
                "end_hour": 2,
            }
        ]

        rows = settings_runtime.settings_schedule_profile_rows_for_config(self.config)

        self.assertEqual(rows[0]["key"], "")

    def test_settings_schedule_rows_default_legacy_profile_to_every_day(self) -> None:
        self.config.raw["encode_queue"]["schedule_profiles"] = [
            {
                "key": "sunday_hours",
                "label": "Sunday Hours",
                "mode": "night",
                "timezone": "host_local",
                "start_hour": 0,
                "end_hour": 5,
            }
        ]

        rows = settings_runtime.settings_schedule_profile_rows_for_config(self.config)

        self.assertEqual(rows[0]["days_of_week"], ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
        self.assertEqual(rows[0]["all_day_days_of_week"], [])

    def test_settings_schedule_rows_preserve_legacy_default_window_when_all_day_days_exist(self) -> None:
        self.config.raw["encode_queue"]["schedule_profiles"] = [
            {
                "key": "after_hours_plus_sunday",
                "label": "After Hours + Sunday",
                "mode": "night",
                "timezone": "host_local",
                "start_hour": 20,
                "end_hour": 6,
                "all_day_days_of_week": ["sun"],
            }
        ]

        rows = settings_runtime.settings_schedule_profile_rows_for_config(self.config)

        self.assertEqual(rows[0]["days_of_week"], ["mon", "tue", "wed", "thu", "fri", "sat"])
        self.assertEqual(rows[0]["all_day_days_of_week"], ["sun"])

    def test_build_runtime_settings_payload_persists_schedule_days_of_week(self) -> None:
        payload = settings_runtime.build_runtime_settings_payload(
            libraries=[{"key": "movies", "path": str(self.root / "source" / "movies"), "color": "#123456"}],
            remote_hosts=[],
            transcode_root=str(self.root / "transcode"),
            encode_queue_scheduler={"mode": "anytime", "timezone": "host_local"},
            schedule_profiles=[
                {
                    "key": "sunday_only",
                    "label": "Sunday Only",
                    "days_of_week": ["sun"],
                    "all_day_days_of_week": [],
                    "start_hour": "0",
                    "end_hour": "0",
                }
            ],
        )

        self.assertEqual(payload["encode_queue"]["schedule_profiles"][0]["days_of_week"], ["sun"])
        self.assertEqual(payload["encode_queue"]["schedule_profiles"][0]["all_day_days_of_week"], [])

    def test_build_runtime_settings_payload_persists_all_day_schedule_days(self) -> None:
        payload = settings_runtime.build_runtime_settings_payload(
            libraries=[{"key": "movies", "path": str(self.root / "source" / "movies"), "color": "#123456"}],
            remote_hosts=[],
            transcode_root=str(self.root / "transcode"),
            encode_queue_scheduler={"mode": "anytime", "timezone": "host_local"},
            schedule_profiles=[
                {
                    "key": "after_hours_plus_sunday",
                    "label": "After Hours + Sunday",
                    "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
                    "all_day_days_of_week": ["sun"],
                    "start_hour": "20",
                    "end_hour": "6",
                }
            ],
        )

        self.assertEqual(payload["encode_queue"]["schedule_profiles"][0]["days_of_week"],
                         ["mon", "tue", "wed", "thu", "fri"])
        self.assertEqual(payload["encode_queue"]["schedule_profiles"][0]["all_day_days_of_week"], ["sun"])

    def test_build_runtime_settings_payload_preserves_all_day_only_schedule_days(self) -> None:
        payload = settings_runtime.build_runtime_settings_payload(
            libraries=[{"key": "movies", "path": str(self.root / "source" / "movies"), "color": "#123456"}],
            remote_hosts=[],
            transcode_root=str(self.root / "transcode"),
            encode_queue_scheduler={"mode": "anytime", "timezone": "host_local"},
            schedule_profiles=[
                {
                    "key": "sunday_all_day",
                    "label": "Sunday All Day",
                    "days_of_week": [],
                    "all_day_days_of_week": ["sun"],
                    "start_hour": "20",
                    "end_hour": "6",
                }
            ],
        )

        self.assertEqual(payload["encode_queue"]["schedule_profiles"][0]["days_of_week"], [])
        self.assertEqual(payload["encode_queue"]["schedule_profiles"][0]["all_day_days_of_week"], ["sun"])

    def test_build_runtime_settings_payload_rejects_schedule_profile_without_days(self) -> None:
        with self.assertRaisesRegex(ValueError, "Select at least one day"):
            settings_runtime.build_runtime_settings_payload(
                libraries=[{"key": "movies", "path": str(self.root / "source" / "movies"), "color": "#123456"}],
                remote_hosts=[],
                transcode_root=str(self.root / "transcode"),
                encode_queue_scheduler={"mode": "anytime", "timezone": "host_local"},
                schedule_profiles=[
                    {
                        "key": "empty_days",
                        "label": "Empty Days",
                        "days_of_week": [],
                        "all_day_days_of_week": [],
                        "start_hour": "20",
                        "end_hour": "6",
                    }
                ],
            )

    def test_host_runtime_rows_mark_never_schedule_as_disabled(self) -> None:
        self.config.raw["remote_hosts"] = [
            {
                "host": "remote-a",
                "label": "Remote A",
                "schedule_profile": "never",
                "capabilities": ["encode_queue"],
            }
        ]
        status = HostStatus(
            key="remote-a",
            label="Remote A",
            mode="ssh",
            priority=20,
            capabilities=["encode_queue"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )

        with open_db(self.config.paths.db_path) as connection, patch(
                "mediaforce.web.app._safe_collect_host_statuses", return_value=[status]
        ):
            rows = web_app._host_runtime_rows(connection, self.config)

        self.assertEqual(rows[0]["schedule_profile"], "never")
        self.assertEqual(rows[0]["schedule_profile_label"], "Never")
        self.assertFalse(rows[0]["schedule_open"])
        self.assertFalse(rows[0]["queue_active"])
        self.assertEqual(rows[0]["active_reason"], "encode queue disabled by schedule")

    def test_select_encode_host_can_choose_startable_unavailable_host(self) -> None:
        host = {
            "key": "ct103",
            "host": "ct103",
            "label": "CT103",
            "available": False,
            "priority": 50,
            "capabilities": ["encode_queue"],
            "active_encode_count": 0,
            "max_parallel_encodes": 1,
            "start_command": "ssh prox-main.shiny pct start 103",
            "schedule_profile": "always",
        }
        job = {"job_id": "job-1", "bypass_schedule": False}
        with patch("mediaforce.web.app._host_runtime_rows", return_value=[host]):
            selected_host, waiting_reason = web_app._select_encode_host(cast(Any, None), self.config, job)
        self.assertIsNotNone(selected_host)
        assert selected_host is not None
        self.assertEqual(selected_host["key"], "ct103")
        self.assertIsNone(waiting_reason)

    def test_select_encode_host_does_not_choose_startable_host_with_capability_issues(self) -> None:
        host = {
            "key": "tdarr",
            "host": "tdarr",
            "label": "Tdarr",
            "available": False,
            "priority": 50,
            "capabilities": ["encode_queue"],
            "active_encode_count": 0,
            "max_parallel_encodes": 1,
            "start_command": "ssh prox-main.shiny pct start 103",
            "schedule_profile": "always",
            "issues": ["ffmpeg is missing both libvmaf and xpsnr support required for sampled calibration."],
        }
        job = {"job_id": "job-1", "bypass_schedule": False}
        with patch("mediaforce.web.app._host_runtime_rows", return_value=[host]):
            selected_host, waiting_reason = web_app._select_encode_host(cast(Any, None), self.config, job)
        self.assertIsNone(selected_host)
        self.assertEqual(waiting_reason, "waiting for an available encode host")

    def test_select_encode_host_respects_allowed_libraries(self) -> None:
        job = {"job_id": "job-1", "prefix": "tv/show", "bypass_schedule": False}
        statuses = [
            {
                "key": "movies-only",
                "label": "Movies Only",
                "priority": 90,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
                "allowed_libraries": ["movies"],
            },
            {
                "key": "tv-only",
                "label": "TV Only",
                "priority": 70,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
                "allowed_libraries": ["tv"],
            },
        ]

        with open_db(self.config.paths.db_path) as connection, patch(
                "mediaforce.web.app._host_runtime_rows", return_value=statuses
        ):
            selected_host, waiting_reason = web_app._select_encode_host(connection, self.config, job)

        self.assertIsNotNone(selected_host)
        assert selected_host is not None
        self.assertEqual(selected_host["key"], "tv-only")
        self.assertIsNone(waiting_reason)

    def test_select_encode_host_does_not_bypass_never_schedule(self) -> None:
        job = {"job_id": "job-1", "prefix": "tv/show", "bypass_schedule": True}
        statuses = [
            {
                "key": "disabled-host",
                "label": "Disabled Host",
                "priority": 90,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": False,
                "schedule_profile": "never",
            }
        ]

        with open_db(self.config.paths.db_path) as connection, patch(
                "mediaforce.web.app._host_runtime_rows", return_value=statuses
        ):
            selected_host, waiting_reason = web_app._select_encode_host(connection, self.config, job)

        self.assertIsNone(selected_host)
        self.assertEqual(waiting_reason, "waiting for a host schedule window")

    def test_host_runtime_rows_include_host_lifecycle_commands(self) -> None:
        self.config.raw["remote_hosts"] = [
            {
                "host": "ct103",
                "label": "CT103",
                "start_command": "ssh prox-main.shiny pct start 103",
                "stop_command": "ssh prox-main.shiny pct shutdown 103",
                "start_timeout_seconds": 420,
            }
        ]
        status = HostStatus(
            key="ct103",
            label="CT103",
            mode="ssh",
            priority=50,
            capabilities=["encode_queue"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )
        with open_db(self.config.paths.db_path) as connection, patch(
                "mediaforce.web.app._safe_collect_host_statuses", return_value=[status]
        ):
            rows = web_app._host_runtime_rows(connection, self.config)

        self.assertEqual(rows[0]["start_command"], "ssh prox-main.shiny pct start 103")
        self.assertEqual(rows[0]["stop_command"], "ssh prox-main.shiny pct shutdown 103")
        self.assertEqual(rows[0]["start_timeout_seconds"], 420)

    def test_host_runtime_rows_include_merged_source_roots_for_mounted_hosts(self) -> None:
        merged_tv_root = self.root / "source" / "tv"
        self.config.raw["media"]["source_roots"] = {"tv": str(merged_tv_root)}
        self.config.raw["remote_hosts"] = [
            {
                "host": "ct103",
                "label": "CT103",
                "media_access": "mounted",
                "capabilities": ["encode_queue"],
            }
        ]
        status = HostStatus(
            key="ct103",
            label="CT103",
            mode="ssh",
            priority=50,
            capabilities=["encode_queue"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )
        with open_db(self.config.paths.db_path) as connection, patch(
                "mediaforce.web.app._safe_collect_host_statuses", return_value=[status]
        ):
            rows = web_app._host_runtime_rows(connection, self.config)

        self.assertEqual(rows[0]["source_roots"], {"tv": str(merged_tv_root)})

    def test_host_runtime_rows_do_not_inherit_source_roots_for_stream_hosts(self) -> None:
        merged_tv_root = self.root / "source" / "tv"
        self.config.raw["media"]["source_roots"] = {"tv": str(merged_tv_root)}
        self.config.raw["remote_hosts"] = [
            {
                "host": "ct103",
                "label": "CT103",
                "media_access": "stream",
                "capabilities": ["encode_queue"],
            }
        ]
        status = HostStatus(
            key="ct103",
            label="CT103",
            mode="ssh",
            priority=50,
            capabilities=["encode_queue"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )
        with open_db(self.config.paths.db_path) as connection, patch(
                "mediaforce.web.app._safe_collect_host_statuses", return_value=[status]
        ):
            rows = web_app._host_runtime_rows(connection, self.config)

        self.assertEqual(rows[0]["source_roots"], {})

    def test_select_encode_host_waits_for_cooldown_when_only_startable_host_is_blocked(self) -> None:
        job = {
            "last_host": {"key": "ct103", "label": "CT103", "host": "ct103"},
            "host_cooldown_until": "2999-01-01T00:00:00+00:00",
        }
        statuses = [
            {
                "key": "ct103",
                "label": "CT103",
                "priority": 50,
                "capabilities": ["encode_queue"],
                "available": False,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "start_command": "ssh prox-main.shiny pct start 103",
                "schedule_profile": "always",
            }
        ]
        with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
            host_payload, waiting_reason = web_app._select_encode_host(cast(Any, None), self.config, job)
        self.assertIsNone(host_payload)
        self.assertEqual(waiting_reason, "waiting for host cooldown to expire on CT103")

    def test_select_encode_host_does_not_choose_startable_host_when_capacity_full(self) -> None:
        statuses = [
            {
                "key": "ct103",
                "label": "CT103",
                "priority": 50,
                "capabilities": ["encode_queue"],
                "available": False,
                "active_encode_count": 1,
                "max_parallel_encodes": 1,
                "start_command": "ssh prox-main.shiny pct start 103",
                "schedule_profile": "always",
            }
        ]
        with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
            host_payload, waiting_reason = web_app._select_encode_host(cast(Any, None), self.config,
                                                                       {"bypass_schedule": False})
        self.assertIsNone(host_payload)
        self.assertEqual(waiting_reason, "waiting for host capacity to free up")

    def test_ensure_encode_host_ready_runs_start_command_and_waits_for_status(self) -> None:
        host = {
            "key": "ct103",
            "host": "ct103",
            "label": "CT103",
            "start_command": "ssh prox-main.shiny pct start 103",
            "start_timeout_seconds": 5,
        }
        unavailable = HostStatus(
            key="ct103",
            label="CT103",
            mode="ssh",
            priority=50,
            capabilities=["encode_queue"],
            available=False,
            message="SSH unavailable",
            missing_paths=[],
        )
        available = HostStatus(
            key="ct103",
            label="CT103",
            mode="ssh",
            priority=50,
            capabilities=["encode_queue"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )
        with patch(
                "mediaforce.web.app.collect_host_statuses",
                side_effect=[[unavailable], [available]],
        ), patch(
            "mediaforce.web.app.run_host_lifecycle_command",
            return_value=subprocess.CompletedProcess(args=["sh"], returncode=0, stdout="", stderr=""),
        ) as lifecycle_mock, patch("mediaforce.web.app.time.sleep") as sleep_mock:
            started = web_app._ensure_encode_host_ready(self.config, host)
        lifecycle_mock.assert_called_once_with(host, "ssh prox-main.shiny pct start 103",
                                               timeout=web_app.HOST_LIFECYCLE_COMMAND_TIMEOUT_SECONDS)
        self.assertTrue(started)
        sleep_mock.assert_not_called()

    def test_ensure_encode_host_ready_returns_false_when_host_already_available(self) -> None:
        host = {
            "key": "ct103",
            "host": "ct103",
            "label": "CT103",
            "start_command": "ssh prox-main.shiny pct start 103",
        }
        available = HostStatus(
            key="ct103",
            label="CT103",
            mode="ssh",
            priority=50,
            capabilities=["encode_queue"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
        )
        with patch("mediaforce.web.app.collect_host_statuses", return_value=[available]), patch(
                "mediaforce.web.app.run_host_lifecycle_command"
        ) as lifecycle_mock:
            started = web_app._ensure_encode_host_ready(self.config, host)
        self.assertFalse(started)
        lifecycle_mock.assert_not_called()

    def test_stop_encode_host_if_configured_runs_stop_command(self) -> None:
        host = {
            "key": "ct103",
            "host": "ct103",
            "label": "CT103",
            "stop_command": "ssh prox-main.shiny pct shutdown 103",
        }
        with patch(
                "mediaforce.web.app.run_host_lifecycle_command",
                return_value=subprocess.CompletedProcess(args=["sh"], returncode=0, stdout="", stderr=""),
        ) as lifecycle_mock:
            web_app._stop_encode_host_if_configured(self.config, host)
        lifecycle_mock.assert_called_once_with(host, "ssh prox-main.shiny pct shutdown 103",
                                               timeout=web_app.HOST_LIFECYCLE_COMMAND_TIMEOUT_SECONDS)

    def test_run_encode_job_skips_stop_when_host_was_already_running(self) -> None:
        manifest_path = self._write_manifest("manifest-running.json", [{"library_item_id": 1}])
        with open_db(self.config.paths.db_path) as connection:
            self._save_job(
                connection,
                job_id="job-running-host",
                manifest_name=manifest_path.name,
                host={"key": "ct103", "host": "ct103", "stop_command": "ssh prox-main.shiny pct shutdown 103"},
                status="running",
                attempt_count=1,
                lease_expires_at=web_app._now_iso(),
            )
        with patch("mediaforce.web.app._ensure_encode_host_ready", return_value=False), patch(
                "mediaforce.web.app._stop_encode_host_if_configured"
        ) as stop_mock, patch(
            "mediaforce.web.app.encode_manifest_items", return_value=[]
        ), patch("mediaforce.web.app.load_config", return_value=self.config):
            web_app._run_encode_job(config_path=self.config.paths.config_path, job_id="job-running-host")
        stop_mock.assert_not_called()

    def test_run_encode_job_keeps_completed_job_clean_when_stop_command_fails(self) -> None:
        manifest_path = self._write_manifest("manifest-stop-failure.json", [{"library_item_id": 1}])
        with open_db(self.config.paths.db_path) as connection:
            self._save_job(
                connection,
                job_id="job-stop-failure",
                manifest_name=manifest_path.name,
                host={"key": "ct103", "host": "ct103", "stop_command": "ssh prox-main.shiny pct shutdown 103"},
                status="running",
                attempt_count=1,
                lease_expires_at=web_app._now_iso(),
            )
        with patch("mediaforce.web.app._ensure_encode_host_ready", return_value=True), patch(
                "mediaforce.web.app._stop_encode_host_if_configured", side_effect=RuntimeError("stop failed")
        ), patch("mediaforce.web.app.encode_manifest_items", return_value=[]), patch(
            "mediaforce.web.app.load_config", return_value=self.config
        ):
            web_app._run_encode_job(config_path=self.config.paths.config_path, job_id="job-stop-failure")
        with open_db(self.config.paths.db_path) as connection:
            job = load_encode_job(connection, "job-stop-failure")
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["status"], "completed")
        self.assertIsNone(job["error"])

    def test_run_encode_job_resets_host_failure_streak_after_success(self) -> None:
        manifest_path = self._write_manifest("manifest-reset-streak.json", [{"library_item_id": 1}])
        with open_db(self.config.paths.db_path) as connection:
            self._save_job(
                connection,
                job_id="job-reset-streak",
                manifest_name=manifest_path.name,
                host={"key": "ct103", "host": "ct103", "label": "CT103", "stop_command": "ssh prox-main.shiny pct shutdown 103"},
                status="running",
                attempt_count=2,
                lease_expires_at=web_app._now_iso(),
            )
            job = load_encode_job(connection, "job-reset-streak")
            assert job is not None
            job["last_host"] = {"key": "ct103", "host": "ct103", "label": "CT103", "failure_streak": 2}
            save_encode_job(connection, job)
        with patch("mediaforce.web.app._ensure_encode_host_ready", return_value=False), patch(
                "mediaforce.web.app._stop_encode_host_if_configured"
        ) as stop_mock, patch(
            "mediaforce.web.app.encode_manifest_items", return_value=[]
        ), patch("mediaforce.web.app.load_config", return_value=self.config):
            web_app._run_encode_job(config_path=self.config.paths.config_path, job_id="job-reset-streak")
        stop_mock.assert_not_called()
        with open_db(self.config.paths.db_path) as connection:
            job = load_encode_job(connection, "job-reset-streak")
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["status"], "completed")
        self.assertNotIn("failure_streak", job["last_host"])

    def test_full_scan_becomes_stale_when_library_roots_change(self) -> None:
        source_path = self._create_source_file("episode-a.mkv")

        with open_db(self.config.paths.db_path) as connection:
            self._insert_library_item(connection, source_path)
            now = web_app._now_iso()
            self._insert_scan_run(
                connection,
                scan_id="scan-1",
                started_at=now,
                completed_at=now,
                roots_json=json.dumps(self.config.raw["media"]["source_roots"]),
                scope="full",
                prefixes_json=None,
                file_count=1,
                reprobed_count=0,
                unchanged_count=0,
            )

            web_app._save_catalog_signature(self.config)
            self.assertFalse(web_app._scan_is_stale(connection, self.config, prefix=None))

            self.config.raw["media"]["source_roots"]["movies"] = str(self.root / "source" / "movies")

            self.assertTrue(web_app._scan_is_stale(connection, self.config, prefix=None))

    def test_full_scan_becomes_stale_after_fifteen_minutes(self) -> None:
        source_path = self._create_source_file("episode-b.mkv")

        with open_db(self.config.paths.db_path) as connection:
            self._insert_library_item(connection, source_path)
            completed_at = (datetime.now(UTC) - timedelta(minutes=16)).isoformat(timespec="seconds")
            self._insert_scan_run(
                connection,
                scan_id="scan-older-than-threshold",
                started_at=completed_at,
                completed_at=completed_at,
                roots_json=json.dumps(self.config.raw["media"]["source_roots"]),
                scope="full",
                prefixes_json=None,
                file_count=1,
                reprobed_count=0,
                unchanged_count=0,
            )

            web_app._save_catalog_signature(self.config)

            self.assertTrue(web_app._scan_is_stale(connection, self.config, prefix=None))

    def test_orphaned_scan_run_is_expired_before_rescheduling(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            now = web_app._now_iso()
            self._insert_scan_run(
                connection,
                scan_id="scan-stale",
                started_at=now,
                completed_at=None,
                owner_pid=None,
                roots_json=json.dumps(self.config.raw["media"]["source_roots"]),
                scope="full",
                prefixes_json=None,
                file_count=0,
                reprobed_count=0,
                unchanged_count=0,
            )
            web_app._save_scan_job_state(
                self.config,
                None,
                {
                    "job_id": "scan-stale-job",
                    "status": "running",
                    "scope": "full",
                    "prefix": None,
                    "owner_pid": 999999,
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "error": None,
                    "stats": None,
                },
            )

            fake_thread = Mock()
            with patch.object(web_app, "_scan_process_is_alive", return_value=False):
                with patch.object(web_app.threading, "Thread", return_value=fake_thread):
                    job = web_app._maybe_schedule_scan(connection, self.config, prefix=None)

            self.assertIsNotNone(job)
            assert job is not None
            self.assertEqual(job["status"], "queued")
            fake_thread.start.assert_called_once()

            stale_row = connection.execute(
                select(scan_runs.c.completed_at).where(scan_runs.c.scan_id == "scan-stale")
            ).mappings().fetchone()
            self.assertIsNotNone(stale_row)
            assert stale_row is not None
            self.assertIsNotNone(stale_row["completed_at"])

            refreshed_job = web_app._load_scan_job_state(self.config, None)
            self.assertIsNotNone(refreshed_job)
            assert refreshed_job is not None
            self.assertEqual(refreshed_job["status"], "queued")
            self.assertNotEqual(refreshed_job["job_id"], "scan-stale-job")

    def test_scheduler_uses_host_local_time_for_windows(self) -> None:
        policy = web_app._normalize_encode_queue_scheduler(
            {"mode": "night", "start_hour": 22, "end_hour": 6, "timezone": "host_local"}
        )
        now = web_app._parse_iso("2026-03-25T07:30:00+00:00")

        self.assertTrue(
            web_app._scheduler_allows_encode_run(
                policy,
                now=now,
                host_payload={"utc_offset_minutes": -420},
            )
        )
        self.assertFalse(
            web_app._scheduler_allows_encode_run(
                policy,
                now=now,
                host_payload={"utc_offset_minutes": 120},
            )
        )

    def test_scheduler_host_local_time_falls_back_to_runner_timezone_without_remote_offset(self) -> None:
        policy = web_app._normalize_encode_queue_scheduler(
            {"mode": "night", "start_hour": 22, "end_hour": 6, "timezone": "host_local"}
        )
        now = web_app._parse_iso("2026-03-25T07:30:00+00:00")
        assert now is not None
        local_hour = now.astimezone().hour
        expected = local_hour >= 22 or local_hour < 6

        self.assertEqual(
            web_app._scheduler_allows_encode_run(policy, now=now, host_payload={}),
            expected,
        )

    def test_scheduler_preserves_midnight_start_hour_for_host_local_windows(self) -> None:
        policy = web_app._normalize_encode_queue_scheduler(
            {"mode": "night", "start_hour": 0, "end_hour": 5, "timezone": "host_local"}
        )

        self.assertFalse(
            web_app._scheduler_allows_encode_run(
                policy,
                now=web_app._parse_iso("2026-03-25T03:30:00+00:00"),
                host_payload={"utc_offset_minutes": -240},
            )
        )
        self.assertTrue(
            web_app._scheduler_allows_encode_run(
                policy,
                now=web_app._parse_iso("2026-03-25T05:30:00+00:00"),
                host_payload={"utc_offset_minutes": -240},
            )
        )

    def test_scheduler_preserves_all_day_only_profiles(self) -> None:
        policy = web_app._normalize_encode_queue_scheduler(
            {
                "mode": "night",
                "start_hour": 20,
                "end_hour": 6,
                "timezone": "host_local",
                "days_of_week": [],
                "all_day_days_of_week": ["sun"],
            }
        )

        self.assertEqual(policy["days_of_week"], [])
        self.assertEqual(policy["all_day_days_of_week"], ["sun"])

    def test_scheduler_never_mode_cannot_be_bypassed(self) -> None:
        policy = web_app._normalize_encode_queue_scheduler(
            {"mode": "never", "start_hour": 22, "end_hour": 8, "timezone": "host_local"}
        )

        self.assertFalse(
            web_app._scheduler_allows_encode_run(
                policy,
                now=web_app._parse_iso("2026-03-25T05:30:00+00:00"),
                host_payload={"utc_offset_minutes": -240},
            )
        )
        self.assertFalse(
            web_app._scheduler_allows_encode_run(
                policy,
                bypass_schedule=True,
                now=web_app._parse_iso("2026-03-25T05:30:00+00:00"),
                host_payload={"utc_offset_minutes": -240},
            )
        )

    def test_scheduler_respects_host_local_days_of_week(self) -> None:
        policy = web_app._normalize_encode_queue_scheduler(
            {
                "mode": "night",
                "start_hour": 0,
                "end_hour": 0,
                "timezone": "host_local",
                "days_of_week": ["sun"],
            }
        )
        now = web_app._parse_iso("2026-04-12T01:30:00+00:00")

        self.assertFalse(
            web_app._scheduler_allows_encode_run(
                policy,
                now=now,
                host_payload={"utc_offset_minutes": -420},
            )
        )
        self.assertTrue(
            web_app._scheduler_allows_encode_run(
                policy,
                now=now,
                host_payload={"utc_offset_minutes": 600},
            )
        )

    def test_scheduler_supports_all_day_days_alongside_window_days(self) -> None:
        policy = web_app._normalize_encode_queue_scheduler(
            {
                "mode": "night",
                "start_hour": 20,
                "end_hour": 6,
                "timezone": "host_local",
                "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
                "all_day_days_of_week": ["sun"],
            }
        )

        self.assertTrue(
            web_app._scheduler_allows_encode_run(
                policy,
                now=web_app._parse_iso("2026-04-12T10:30:00+00:00"),
                host_payload={"utc_offset_minutes": 60},
            )
        )
        self.assertFalse(
            web_app._scheduler_allows_encode_run(
                policy,
                now=web_app._parse_iso("2026-04-14T16:00:00+00:00"),
                host_payload={"utc_offset_minutes": 0},
            )
        )
        self.assertTrue(
            web_app._scheduler_allows_encode_run(
                policy,
                now=web_app._parse_iso("2026-04-14T22:30:00+00:00"),
                host_payload={"utc_offset_minutes": 0},
            )
        )

    def test_scheduler_keeps_cross_midnight_window_open_after_midnight_for_previous_allowed_day(self) -> None:
        policy = web_app._normalize_encode_queue_scheduler(
            {
                "mode": "night",
                "start_hour": 20,
                "end_hour": 6,
                "timezone": "host_local",
                "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
            }
        )

        self.assertTrue(
            web_app._scheduler_allows_encode_run(
                policy,
                now=web_app._parse_iso("2026-04-18T07:30:00+00:00"),
                host_payload={"utc_offset_minutes": -240},
            )
        )
        self.assertFalse(
            web_app._scheduler_allows_encode_run(
                policy,
                now=web_app._parse_iso("2026-04-19T07:30:00+00:00"),
                host_payload={"utc_offset_minutes": -240},
            )
        )

    def test_scheduler_summary_keeps_day_and_timezone_context(self) -> None:
        policy = web_app._normalize_encode_queue_scheduler(
            {
                "mode": "night",
                "start_hour": 20,
                "end_hour": 6,
                "timezone": "host_local",
                "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
                "all_day_days_of_week": ["sun"],
            }
        )

        self.assertEqual(
            policy["summary"],
            "runs Sun all day and weekdays between 20:00 and 06:00 (host local)",
        )

    def test_folder_cards_count_validated_items_as_pending_until_promoted(self) -> None:
        first = self._create_source_file("episode-pending.mkv")
        second = self._create_source_file("episode-validated.mkv")
        third = self._create_source_file("episode-promoted.mkv")

        with open_db(self.config.paths.db_path) as connection:
            first_id = self._insert_library_item(connection, first)
            second_id = self._insert_library_item(connection, second, status="validated")
            third_id = self._insert_library_item(connection, third, status="promoted")
            for item_id in (first_id, second_id, third_id):
                connection.execute(
                    update(library_items)
                    .where(library_items.c.id == item_id)
                    .values(
                        size_bytes=2 * 1024 * 1024 * 1024,
                        rel_path=f"tv/show/Season 1/item-{item_id}.mkv",
                        parent_dir="tv/show/Season 1",
                    )
                )

            cards = web_app._list_folder_cards(self.config, connection)

        matching_cards = [card for card in cards if card.prefix == "tv/show/Season 1"]
        self.assertEqual(len(matching_cards), 1)
        card = matching_cards[0]
        self.assertEqual(card.prefix, "tv/show/Season 1")
        self.assertEqual(card.item_count, 3)
        self.assertEqual(card.pending_count, 2)
        self.assertEqual(card.statuses["planned"], 1)
        self.assertEqual(card.statuses["validated"], 1)
        self.assertEqual(card.statuses["promoted"], 1)

    def test_folder_cards_projected_reclaim_uses_known_validated_savings(self) -> None:
        validated = self._create_source_file("episode-validated.mkv")

        with open_db(self.config.paths.db_path) as connection:
            validated_id = self._insert_library_item(connection, validated, status="validated")
            connection.execute(
                update(library_items)
                .where(library_items.c.id == validated_id)
                .values(
                    size_bytes=2 * 1024 * 1024 * 1024,
                    rel_path=f"tv/show/Season 5/{validated.name}",
                    parent_dir="tv/show/Season 5",
                    video_codec="h264",
                )
            )
            self._insert_staged_artifact(connection, validated_id, self._staging_path(validated.name))
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == validated_id)
                .values(
                    source_rel_path=f"tv/show/Season 5/{validated.name}",
                    source_video_codec="h264",
                    source_size_bytes=2 * 1024 * 1024 * 1024,
                    bytes_saved=int(1.5 * 1024 * 1024 * 1024),
                )
            )

            cards = web_app._list_folder_cards(self.config, connection)

        matching_cards = [card for card in cards if card.prefix == "tv/show/Season 5"]
        self.assertEqual(len(matching_cards), 1)
        self.assertEqual(matching_cards[0].estimated_savings_bytes, 0)
        self.assertEqual(matching_cards[0].known_saved_bytes, int(1.5 * 1024 * 1024 * 1024))
        self.assertEqual(matching_cards[0].projected_reclaim_bytes, int(1.5 * 1024 * 1024 * 1024))

    def test_preview_folder_cards_rank_by_pending_opportunity(self) -> None:
        season_a_pending = self._create_source_file("season-a-pending.mkv")
        season_a_promoted = self._create_source_file("season-a-promoted.mkv")
        season_b_pending = self._create_source_file("season-b-pending.mkv")
        season_b_validated = self._create_source_file("season-b-validated.mkv")

        with open_db(self.config.paths.db_path) as connection:
            season_a_pending_id = self._insert_library_item(connection, season_a_pending)
            season_a_promoted_id = self._insert_library_item(connection, season_a_promoted, status="promoted")
            season_b_pending_id = self._insert_library_item(connection, season_b_pending)
            season_b_validated_id = self._insert_library_item(connection, season_b_validated, status="validated")

            for item_id, rel_path, status in (
                (season_a_pending_id, "tv/show/Season A/season-a-pending.mkv", "planned"),
                (season_a_promoted_id, "tv/show/Season A/season-a-promoted.mkv", "promoted"),
                (season_b_pending_id, "tv/show/Season B/season-b-pending.mkv", "planned"),
                (season_b_validated_id, "tv/show/Season B/season-b-validated.mkv", "validated"),
            ):
                connection.execute(
                    update(library_items)
                    .where(library_items.c.id == item_id)
                    .values(
                        size_bytes=1 * 1024 * 1024 * 1024,
                        rel_path=rel_path,
                        parent_dir=rel_path.rsplit("/", 1)[0],
                        status=status,
                    )
                )

            self._insert_staged_artifact(connection, season_b_validated_id, self._staging_path(season_b_validated.name))
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == season_b_validated_id)
                .values(
                    source_rel_path="tv/show/Season B/season-b-validated.mkv",
                    source_video_codec="h264",
                    source_size_bytes=2 * 1024 * 1024 * 1024,
                    bytes_saved=int(5 * 1024 * 1024 * 1024),
                )
            )

            def fake_estimate_savings(*args: Any, **kwargs: Any) -> int:
                return 300 * 1024 * 1024

            with patch.object(web_app, "_estimate_savings_bytes", autospec=True, side_effect=fake_estimate_savings):
                cards = web_app._preview_folder_cards(self.config, connection)

        self.assertEqual([card.prefix for card in cards], ["tv/show/Season B", "tv/show/Season A"])

    def test_preview_folder_cards_use_projected_reclaim_when_pending_estimate_is_zero(self) -> None:
        small_total = self._create_source_file("season-c-validated-small.mkv")
        large_total = self._create_source_file("season-d-validated-large.mkv")

        with open_db(self.config.paths.db_path) as connection:
            small_total_id = self._insert_library_item(connection, small_total, status="validated")
            large_total_id = self._insert_library_item(connection, large_total, status="validated")

            connection.execute(
                update(library_items)
                .where(library_items.c.id == small_total_id)
                .values(
                    size_bytes=1 * 1024 * 1024 * 1024,
                    rel_path="tv/show/Season C/season-c-validated-small.mkv",
                    parent_dir="tv/show/Season C",
                )
            )
            connection.execute(
                update(library_items)
                .where(library_items.c.id == large_total_id)
                .values(
                    size_bytes=4 * 1024 * 1024 * 1024,
                    rel_path="tv/show/Season D/season-d-validated-large.mkv",
                    parent_dir="tv/show/Season D",
                )
            )

            for item_id, name, bytes_saved in (
                (small_total_id, small_total.name, int(5 * 1024 * 1024 * 1024)),
                (large_total_id, large_total.name, int(3 * 1024 * 1024 * 1024)),
            ):
                self._insert_staged_artifact(connection, item_id, self._staging_path(name))
                connection.execute(
                    update(staged_artifacts)
                    .where(staged_artifacts.c.library_item_id == item_id)
                    .values(
                        source_rel_path=f"tv/show/{'Season C' if item_id == small_total_id else 'Season D'}/{name}",
                        source_video_codec="h264",
                        source_size_bytes=2 * 1024 * 1024 * 1024,
                        bytes_saved=bytes_saved,
                    )
                )

            with patch.object(web_app, "_estimate_savings_bytes", autospec=True, return_value=0):
                cards = web_app._preview_folder_cards(self.config, connection)

        self.assertEqual([card.prefix for card in cards], ["tv/show/Season C", "tv/show/Season D"])

    def test_preview_folder_cards_rank_by_projected_reclaim_when_confirmed_savings_dominate(self) -> None:
        season_a_pending = self._create_source_file("season-a-pending.mkv")
        season_a_validated = self._create_source_file("season-a-validated.mkv")
        season_b_pending = self._create_source_file("season-b-pending.mkv")

        with open_db(self.config.paths.db_path) as connection:
            season_a_pending_id = self._insert_library_item(connection, season_a_pending)
            season_a_validated_id = self._insert_library_item(connection, season_a_validated, status="validated")
            season_b_pending_id = self._insert_library_item(connection, season_b_pending)

            for item_id, rel_path, status in (
                (season_a_pending_id, "tv/show/Season A/season-a-pending.mkv", "planned"),
                (season_a_validated_id, "tv/show/Season A/season-a-validated.mkv", "validated"),
                (season_b_pending_id, "tv/show/Season B/season-b-pending.mkv", "planned"),
            ):
                connection.execute(
                    update(library_items)
                    .where(library_items.c.id == item_id)
                    .values(
                        size_bytes=1 * 1024 * 1024 * 1024,
                        rel_path=rel_path,
                        parent_dir=rel_path.rsplit("/", 1)[0],
                        status=status,
                    )
                )

            self._insert_staged_artifact(connection, season_a_validated_id, self._staging_path(season_a_validated.name))
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == season_a_validated_id)
                .values(
                    source_rel_path="tv/show/Season A/season-a-validated.mkv",
                    source_video_codec="h264",
                    source_size_bytes=2 * 1024 * 1024 * 1024,
                    bytes_saved=int(5 * 1024 * 1024 * 1024),
                )
            )

            with patch.object(
                    web_app,
                    "_estimate_savings_bytes",
                    autospec=True,
                    side_effect=[50 * 1024 * 1024, 2 * 1024 * 1024 * 1024],
            ):
                cards = web_app._preview_folder_cards(self.config, connection)

        self.assertEqual([card.prefix for card in cards], ["tv/show/Season A", "tv/show/Season B"])

    def test_list_folder_cards_rank_by_pending_opportunity(self) -> None:
        season_a_pending = self._create_source_file("season-a-pending.mkv")
        season_a_promoted = self._create_source_file("season-a-promoted.mkv")
        season_b_pending = self._create_source_file("season-b-pending.mkv")
        season_b_validated = self._create_source_file("season-b-validated.mkv")

        with open_db(self.config.paths.db_path) as connection:
            season_a_pending_id = self._insert_library_item(connection, season_a_pending)
            season_a_promoted_id = self._insert_library_item(connection, season_a_promoted, status="promoted")
            season_b_pending_id = self._insert_library_item(connection, season_b_pending)
            season_b_validated_id = self._insert_library_item(connection, season_b_validated, status="validated")

            for item_id, rel_path, status in (
                (season_a_pending_id, "tv/show/Season A/season-a-pending.mkv", "planned"),
                (season_a_promoted_id, "tv/show/Season A/season-a-promoted.mkv", "promoted"),
                (season_b_pending_id, "tv/show/Season B/season-b-pending.mkv", "planned"),
                (season_b_validated_id, "tv/show/Season B/season-b-validated.mkv", "validated"),
            ):
                connection.execute(
                    update(library_items)
                    .where(library_items.c.id == item_id)
                    .values(
                        size_bytes=1 * 1024 * 1024 * 1024,
                        rel_path=rel_path,
                        parent_dir=rel_path.rsplit("/", 1)[0],
                        status=status,
                    )
                )

            self._insert_staged_artifact(connection, season_b_validated_id, self._staging_path(season_b_validated.name))
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == season_b_validated_id)
                .values(
                    source_rel_path="tv/show/Season B/season-b-validated.mkv",
                    source_video_codec="h264",
                    source_size_bytes=2 * 1024 * 1024 * 1024,
                    bytes_saved=int(5 * 1024 * 1024 * 1024),
                )
            )

            def fake_estimate_savings(*args: Any, **kwargs: Any) -> int:
                return 300 * 1024 * 1024

            with (
                    patch.object(web_app, "_age_days", autospec=True, return_value=42.0),
                    patch.object(web_app, "_estimate_savings_bytes", autospec=True, side_effect=fake_estimate_savings),
            ):
                cards = web_app._list_folder_cards(self.config, connection)

        self.assertEqual([card.prefix for card in cards], ["tv/show/Season B", "tv/show/Season A"])

    def test_list_folder_cards_use_projected_reclaim_when_sort_score_is_zero(self) -> None:
        small_total = self._create_source_file("season-c-validated-small.mkv")
        large_total = self._create_source_file("season-d-validated-large.mkv")

        with open_db(self.config.paths.db_path) as connection:
            small_total_id = self._insert_library_item(connection, small_total, status="validated")
            large_total_id = self._insert_library_item(connection, large_total, status="validated")

            connection.execute(
                update(library_items)
                .where(library_items.c.id == small_total_id)
                .values(
                    size_bytes=1 * 1024 * 1024 * 1024,
                    rel_path="tv/show/Season C/season-c-validated-small.mkv",
                    parent_dir="tv/show/Season C",
                )
            )
            connection.execute(
                update(library_items)
                .where(library_items.c.id == large_total_id)
                .values(
                    size_bytes=4 * 1024 * 1024 * 1024,
                    rel_path="tv/show/Season D/season-d-validated-large.mkv",
                    parent_dir="tv/show/Season D",
                )
            )

            for item_id, name, bytes_saved in (
                (small_total_id, small_total.name, int(5 * 1024 * 1024 * 1024)),
                (large_total_id, large_total.name, int(3 * 1024 * 1024 * 1024)),
            ):
                self._insert_staged_artifact(connection, item_id, self._staging_path(name))
                connection.execute(
                    update(staged_artifacts)
                    .where(staged_artifacts.c.library_item_id == item_id)
                    .values(
                        source_rel_path=f"tv/show/{'Season C' if item_id == small_total_id else 'Season D'}/{name}",
                        source_video_codec="h264",
                        source_size_bytes=2 * 1024 * 1024 * 1024,
                        bytes_saved=bytes_saved,
                    )
                )

            with (
                    patch.object(web_app, "_age_days", autospec=True, return_value=42.0),
                    patch.object(web_app, "_estimate_savings_bytes", autospec=True, return_value=0),
            ):
                cards = web_app._list_folder_cards(self.config, connection)

        self.assertEqual([card.prefix for card in cards], ["tv/show/Season C", "tv/show/Season D"])

    def test_list_folder_cards_rank_by_projected_reclaim_when_confirmed_savings_dominate(self) -> None:
        season_a_pending = self._create_source_file("season-a-pending.mkv")
        season_a_validated = self._create_source_file("season-a-validated.mkv")
        season_b_pending = self._create_source_file("season-b-pending.mkv")

        with open_db(self.config.paths.db_path) as connection:
            season_a_pending_id = self._insert_library_item(connection, season_a_pending)
            season_a_validated_id = self._insert_library_item(connection, season_a_validated, status="validated")
            season_b_pending_id = self._insert_library_item(connection, season_b_pending)

            for item_id, rel_path, status in (
                (season_a_pending_id, "tv/show/Season A/season-a-pending.mkv", "planned"),
                (season_a_validated_id, "tv/show/Season A/season-a-validated.mkv", "validated"),
                (season_b_pending_id, "tv/show/Season B/season-b-pending.mkv", "planned"),
            ):
                connection.execute(
                    update(library_items)
                    .where(library_items.c.id == item_id)
                    .values(
                        size_bytes=1 * 1024 * 1024 * 1024,
                        rel_path=rel_path,
                        parent_dir=rel_path.rsplit("/", 1)[0],
                        status=status,
                    )
                )

            self._insert_staged_artifact(connection, season_a_validated_id, self._staging_path(season_a_validated.name))
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == season_a_validated_id)
                .values(
                    source_rel_path="tv/show/Season A/season-a-validated.mkv",
                    source_video_codec="h264",
                    source_size_bytes=2 * 1024 * 1024 * 1024,
                    bytes_saved=int(5 * 1024 * 1024 * 1024),
                )
            )

            with (
                    patch.object(web_app, "_age_days", autospec=True, return_value=42.0),
                    patch.object(
                        web_app,
                        "_estimate_savings_bytes",
                        autospec=True,
                        side_effect=[50 * 1024 * 1024, 2 * 1024 * 1024 * 1024],
                    ),
            ):
                cards = web_app._list_folder_cards(self.config, connection)

        self.assertEqual([card.prefix for card in cards], ["tv/show/Season A", "tv/show/Season B"])

    def test_folder_cards_use_validated_samples_in_history_for_pending_items(self) -> None:
        pending = self._create_source_file("episode-pending.mkv")
        validated_one = self._create_source_file("episode-validated-one.mkv")
        validated_two = self._create_source_file("episode-validated-two.mkv")

        with open_db(self.config.paths.db_path) as connection:
            pending_id = self._insert_library_item(connection, pending)
            validated_one_id = self._insert_library_item(connection, validated_one, status="validated")
            validated_two_id = self._insert_library_item(connection, validated_two, status="encoded")
            for item_id, name, status in (
                (pending_id, pending.name, "planned"),
                (validated_one_id, validated_one.name, "validated"),
                (validated_two_id, validated_two.name, "encoded"),
            ):
                connection.execute(
                    update(library_items)
                    .where(library_items.c.id == item_id)
                    .values(
                        size_bytes=2 * 1024 * 1024 * 1024,
                        rel_path=f"tv/show/Season 6/{name}",
                        parent_dir="tv/show/Season 6",
                        video_codec="h264",
                        status=status,
                    )
                )

            for item_id, name, bytes_saved in (
                (validated_one_id, validated_one.name, int(1.5 * 1024 * 1024 * 1024)),
                (validated_two_id, validated_two.name, int(1.0 * 1024 * 1024 * 1024)),
            ):
                self._insert_staged_artifact(connection, item_id, self._staging_path(name))
                connection.execute(
                    update(staged_artifacts)
                    .where(staged_artifacts.c.library_item_id == item_id)
                    .values(
                        source_rel_path=f"tv/show/Season 6/{name}",
                        source_video_codec="h264",
                        source_size_bytes=2 * 1024 * 1024 * 1024,
                        bytes_saved=bytes_saved,
                    )
                )

            with patch.object(web_app, "_estimate_savings_bytes", autospec=True, return_value=0):
                cards = web_app._list_folder_cards(self.config, connection)

        matching_cards = [card for card in cards if card.prefix == "tv/show/Season 6"]
        self.assertEqual(len(matching_cards), 1)
        self.assertEqual(matching_cards[0].known_saved_bytes, int(2.5 * 1024 * 1024 * 1024))
        self.assertGreater(matching_cards[0].estimated_savings_bytes, int(1.0 * 1024 * 1024 * 1024))
        self.assertEqual(
            matching_cards[0].projected_reclaim_bytes,
            matching_cards[0].known_saved_bytes + matching_cards[0].estimated_savings_bytes,
        )

    def test_folder_cards_use_folder_history_to_adjust_estimated_reclaim(self) -> None:
        pending = self._create_source_file("episode-pending.mkv")
        promoted_one = self._create_source_file("episode-promoted-one.mkv")
        promoted_two = self._create_source_file("episode-promoted-two.mkv")

        with open_db(self.config.paths.db_path) as connection:
            pending_id = self._insert_library_item(connection, pending)
            promoted_one_id = self._insert_library_item(connection, promoted_one, status="promoted")
            promoted_two_id = self._insert_library_item(connection, promoted_two, status="promoted")
            for item_id, name in (
                (pending_id, pending.name),
                (promoted_one_id, promoted_one.name),
                (promoted_two_id, promoted_two.name),
            ):
                connection.execute(
                    update(library_items)
                    .where(library_items.c.id == item_id)
                    .values(
                        size_bytes=2 * 1024 * 1024 * 1024,
                        rel_path=f"tv/show/Season 1/{name}",
                        parent_dir="tv/show/Season 1",
                        video_codec="h264",
                    )
                )

            for item_id, name in ((promoted_one_id, promoted_one.name), (promoted_two_id, promoted_two.name)):
                self._insert_staged_artifact(connection, item_id, self._staging_path(name))
                connection.execute(
                    update(staged_artifacts)
                    .where(staged_artifacts.c.library_item_id == item_id)
                    .values(
                        promoted_at=web_app._now_iso(),
                        source_rel_path=f"tv/show/Season 1/{name}",
                        source_video_codec="h264",
                        source_size_bytes=2 * 1024 * 1024 * 1024,
                        bytes_saved=int(1.5 * 1024 * 1024 * 1024),
                    )
                )

            cards = web_app._list_folder_cards(self.config, connection)

        matching_cards = [card for card in cards if card.prefix == "tv/show/Season 1"]
        self.assertEqual(len(matching_cards), 1)
        self.assertEqual(matching_cards[0].estimated_savings_bytes, int(1.5 * 1024 * 1024 * 1024))
        self.assertEqual(matching_cards[0].known_saved_bytes, 0)
        self.assertEqual(matching_cards[0].projected_reclaim_bytes, int(1.5 * 1024 * 1024 * 1024))

    def test_folder_cards_do_not_count_promoted_savings_toward_projected_reclaim_threshold(self) -> None:
        pending = self._create_source_file("episode-pending.mkv")
        promoted = self._create_source_file("episode-promoted.mkv")

        with open_db(self.config.paths.db_path) as connection:
            pending_id = self._insert_library_item(connection, pending)
            promoted_id = self._insert_library_item(connection, promoted, status="promoted")
            connection.execute(
                update(library_items)
                .where(library_items.c.id == pending_id)
                .values(
                    size_bytes=2 * 1024 * 1024 * 1024,
                    rel_path=f"tv/show/Season 7/{pending.name}",
                    parent_dir="tv/show/Season 7",
                    video_codec=None,
                )
            )
            connection.execute(
                update(library_items)
                .where(library_items.c.id == promoted_id)
                .values(
                    size_bytes=2 * 1024 * 1024 * 1024,
                    rel_path=f"tv/show/Season 7/{promoted.name}",
                    parent_dir="tv/show/Season 7",
                    video_codec=None,
                )
            )

            self._insert_staged_artifact(connection, promoted_id, self._staging_path(promoted.name))
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == promoted_id)
                .values(
                    promoted_at=web_app._now_iso(),
                    source_rel_path=f"tv/show/Season 7/{promoted.name}",
                    source_size_bytes=2 * 1024 * 1024 * 1024,
                    bytes_saved=int(1.5 * 1024 * 1024 * 1024),
                )
            )

            with patch.object(
                    web_app,
                    "_estimate_savings_bytes",
                    autospec=True,
                    return_value=50 * 1024 * 1024,
            ):
                cards = web_app._list_folder_cards(self.config, connection)

        self.assertEqual([card for card in cards if card.prefix == "tv/show/Season 7"], [])

    def test_list_folder_cards_recomputes_review_badges_on_cache_hit(self) -> None:
        folder_cards_runtime.reset_folder_card_cache()
        pending = self._create_source_file("episode-cache-hit.mkv")

        with open_db(self.config.paths.db_path) as connection:
            pending_id = self._insert_library_item(connection, pending)
            connection.execute(
                update(library_items)
                .where(library_items.c.id == pending_id)
                .values(
                    size_bytes=2 * 1024 * 1024 * 1024,
                    rel_path=f"tv/show/Season 8/{pending.name}",
                    parent_dir="tv/show/Season 8",
                    video_codec="h264",
                )
            )

            with patch.object(folder_cards_runtime, "folder_card_cache_key", return_value=("cache", 1, 1)):
                with patch.object(web_app, "_estimate_savings_bytes", autospec=True, return_value=2 * 1024 * 1024 * 1024):
                    with patch.object(
                            web_app,
                            "_folder_needs_attention_badges",
                            autospec=True,
                            side_effect=[{}, {"tv/show/Season 8": {"label": "Encode failed", "tone": "danger"}}],
                    ), patch.object(web_app, "_folder_calibration_job_badges", autospec=True, return_value={}):
                        first_cards = web_app._list_folder_cards(self.config, connection)
                        second_cards = web_app._list_folder_cards(self.config, connection)

        self.assertEqual([card.prefix for card in first_cards], ["tv/show/Season 8"])
        self.assertEqual([card.prefix for card in second_cards], ["tv/show/Season 8"])
        self.assertIsNone(first_cards[0].review_badge_label)
        self.assertEqual(second_cards[0].review_badge_label, "Encode failed")
        self.assertEqual(second_cards[0].review_badge_tone, "danger")
        folder_cards_runtime.reset_folder_card_cache()

    def test_folder_cards_show_delivery_badge_after_approved_draft_validates(self) -> None:
        validated = self._create_source_file("episode-validated-badge.mkv")

        with open_db(self.config.paths.db_path) as connection:
            validated_id = self._insert_library_item(connection, validated, status="validated")
            connection.execute(
                update(library_items)
                .where(library_items.c.id == validated_id)
                .values(
                    size_bytes=2 * 1024 * 1024 * 1024,
                    rel_path=f"tv/show/Season 9/{validated.name}",
                    parent_dir="tv/show/Season 9",
                    video_codec="h264",
                )
            )
            self._insert_staged_artifact(connection, validated_id, self._staging_path(validated.name))
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == validated_id)
                .values(bytes_saved=2 * 1024 * 1024 * 1024)
            )

            cards = folder_cards_runtime.list_folder_cards(
                connection,
                minimum_recommended_savings_bytes=100 * 1024 * 1024,
                folder_group=web_app._folder_group,
                age_days=lambda _path: 1.0,
                estimate_savings_bytes=Mock(return_value=2 * 1024 * 1024 * 1024),
                review_badge_for_prefix=Mock(return_value={"label": "Approved draft", "tone": "ok"}),
            )

        card = next(card for card in cards if card.prefix == "tv/show/Season 9")
        self.assertEqual(card.review_badge_label, "Ready to promote")
        self.assertEqual(card.review_badge_tone, "ok")
        self.assertEqual(card.review_badge_detail, "All staged outputs passed validation.")

    def test_folder_cards_use_catalog_mtime_without_statting_source_paths(self) -> None:
        source = self._create_source_file("episode-catalog-age.mkv")
        age_days = Mock(side_effect=AssertionError("source path age lookup should not run"))

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source)
            connection.execute(
                update(library_items)
                .where(library_items.c.id == item_id)
                .values(
                    size_bytes=2 * 1024 * 1024 * 1024,
                    rel_path=f"tv/show/Season 10/{source.name}",
                    parent_dir="tv/show/Season 10",
                    mtime_ns=1_700_000_000_000_000_000,
                )
            )

            cards = folder_cards_runtime.list_folder_cards(
                connection,
                minimum_recommended_savings_bytes=100 * 1024 * 1024,
                folder_group=web_app._folder_group,
                age_days=age_days,
                estimate_savings_bytes=Mock(return_value=200 * 1024 * 1024),
                review_badge_for_prefix=Mock(return_value={"label": None, "tone": None}),
            )

        self.assertEqual([card.prefix for card in cards], ["tv/show/Season 10"])
        age_days.assert_not_called()

    def test_folder_delivery_badge_ignores_mixed_validated_and_encoded_items(self) -> None:
        card = self._folder_card_for_delivery_badge(
            item_count=4,
            pending_count=4,
            statuses={"validated": 2, "encoded": 2},
        )

        self.assertIsNone(folder_cards_runtime._folder_delivery_badge(card))

    def test_folder_delivery_badge_still_marks_promoted_and_encoded_items_ready_to_validate(self) -> None:
        card = self._folder_card_for_delivery_badge(
            item_count=4,
            pending_count=2,
            statuses={"promoted": 2, "encoded": 2},
        )

        self.assertEqual(
            folder_cards_runtime._folder_delivery_badge(card),
            {"label": "Ready to validate", "tone": "attention", "detail": "All pending outputs are encoded."},
        )

    def test_folder_delivery_badge_ignores_fully_promoted_folders(self) -> None:
        card = self._folder_card_for_delivery_badge(
            item_count=4,
            pending_count=0,
            statuses={"promoted": 4},
        )

        self.assertIsNone(folder_cards_runtime._folder_delivery_badge(card))

    def test_cached_folder_cards_recomputes_after_reset_during_cache_miss(self) -> None:
        folder_cards_runtime.reset_folder_card_cache()

        stale_card = folder_cards_runtime.FolderCard(
            prefix="tv/show/Season 8",
            title="Season 8",
            subtitle="TV",
            scope_label="Season",
            item_count=1,
            pending_count=1,
            total_size_bytes=2 * 1024 * 1024 * 1024,
            estimated_savings_bytes=200 * 1024 * 1024,
            known_saved_bytes=0,
            projected_reclaim_bytes=200 * 1024 * 1024,
            average_age_days=10.0,
            sort_score=0.2,
            statuses={"planned": 1},
            video_codecs={"h264": 1},
        )
        fresh_card = folder_cards_runtime.FolderCard(
            prefix="tv/show/Season 9",
            title="Season 9",
            subtitle="TV",
            scope_label="Season",
            item_count=1,
            pending_count=1,
            total_size_bytes=3 * 1024 * 1024 * 1024,
            estimated_savings_bytes=300 * 1024 * 1024,
            known_saved_bytes=0,
            projected_reclaim_bytes=300 * 1024 * 1024,
            average_age_days=20.0,
            sort_score=0.3,
            statuses={"planned": 1},
            video_codecs={"h264": 1},
        )
        calls = {"count": 0}

        def fake_list_folder_cards(*args: Any, **kwargs: Any) -> list[folder_cards_runtime.FolderCard]:
            calls["count"] += 1
            if calls["count"] == 1:
                folder_cards_runtime.reset_folder_card_cache()
                return [stale_card]
            return [fresh_card]

        with patch.object(folder_cards_runtime, "folder_card_cache_key", return_value=("cache", 1, 1)):
            with patch.object(folder_cards_runtime, "list_folder_cards", side_effect=fake_list_folder_cards):
                cards = folder_cards_runtime.cached_folder_cards(
                    self.config,
                    Mock(),
                    minimum_recommended_savings_bytes=100 * 1024 * 1024,
                    folder_group=Mock(),
                    age_days=Mock(),
                    estimate_savings_bytes=Mock(),
                    review_badge_for_prefix=Mock(return_value={"label": None, "tone": None}),
                )

        self.assertEqual(calls["count"], 2)
        self.assertEqual([card.prefix for card in cards], [fresh_card.prefix])
        folder_cards_runtime.reset_folder_card_cache()

    def test_cached_folder_cards_reapplies_review_badges_without_shared_mutation(self) -> None:
        folder_cards_runtime.reset_folder_card_cache()

        cached_source_card = folder_cards_runtime.FolderCard(
            prefix="tv/show/Season 10",
            title="Season 10",
            subtitle="TV",
            scope_label="Season",
            item_count=1,
            pending_count=1,
            total_size_bytes=2 * 1024 * 1024 * 1024,
            estimated_savings_bytes=200 * 1024 * 1024,
            known_saved_bytes=0,
            projected_reclaim_bytes=200 * 1024 * 1024,
            average_age_days=10.0,
            sort_score=0.2,
            statuses={"planned": 1},
            video_codecs={"h264": 1},
            review_badge_label="First badge",
            review_badge_tone="attention",
            review_badge_detail="First detail",
        )
        list_folder_cards = Mock(return_value=[cached_source_card])

        with patch.object(folder_cards_runtime, "folder_card_cache_key", return_value=("cache", 1, 1)):
            with patch.object(folder_cards_runtime, "list_folder_cards", list_folder_cards):
                first_cards = folder_cards_runtime.cached_folder_cards(
                    self.config,
                    Mock(),
                    minimum_recommended_savings_bytes=100 * 1024 * 1024,
                    folder_group=Mock(),
                    age_days=Mock(),
                    estimate_savings_bytes=Mock(),
                    review_badge_for_prefix=Mock(return_value={"label": "First badge", "tone": "attention"}),
                )
                second_cards = folder_cards_runtime.cached_folder_cards(
                    self.config,
                    Mock(),
                    minimum_recommended_savings_bytes=100 * 1024 * 1024,
                    folder_group=Mock(),
                    age_days=Mock(),
                    estimate_savings_bytes=Mock(),
                    review_badge_for_prefix=Mock(return_value={"label": "Second badge", "tone": "ready", "detail": "Second detail"}),
                )

        self.assertEqual(list_folder_cards.call_count, 1)
        self.assertIsNot(first_cards[0], second_cards[0])
        self.assertEqual(first_cards[0].review_badge_label, "First badge")
        self.assertEqual(first_cards[0].review_badge_tone, "attention")
        self.assertEqual(first_cards[0].review_badge_detail, "First detail")
        self.assertEqual(second_cards[0].review_badge_label, "Second badge")
        self.assertEqual(second_cards[0].review_badge_tone, "ready")
        self.assertEqual(second_cards[0].review_badge_detail, "Second detail")

        with patch.object(folder_cards_runtime, "folder_card_cache_key", return_value=("cache", 1, 1)):
            with patch.object(folder_cards_runtime, "list_folder_cards", list_folder_cards):
                second_cards[0].review_badge_label = "Mutated response copy"
                third_cards = folder_cards_runtime.cached_folder_cards(
                    self.config,
                    Mock(),
                    minimum_recommended_savings_bytes=100 * 1024 * 1024,
                    folder_group=Mock(),
                    age_days=Mock(),
                    estimate_savings_bytes=Mock(),
                    review_badge_for_prefix=Mock(return_value={"label": "Third badge", "tone": "attention"}),
                )

        self.assertEqual(third_cards[0].review_badge_label, "Third badge")
        self.assertEqual(third_cards[0].review_badge_tone, "attention")
        self.assertIsNone(third_cards[0].review_badge_detail)
        folder_cards_runtime.reset_folder_card_cache()

    def test_folder_cards_require_multiple_labeled_samples_before_using_folder_history(self) -> None:
        pending = self._create_source_file("episode-pending.mkv")
        folder_promoted = self._create_source_file("episode-folder-promoted.mkv")
        codec_promoted = self._create_source_file("episode-codec-promoted.mkv")
        legacy_promoted = self._create_source_file("episode-legacy-promoted.mkv")

        with open_db(self.config.paths.db_path) as connection:
            pending_id = self._insert_library_item(connection, pending)
            folder_promoted_id = self._insert_library_item(connection, folder_promoted, status="promoted")
            codec_promoted_id = self._insert_library_item(connection, codec_promoted, status="promoted")
            legacy_promoted_id = self._insert_library_item(connection, legacy_promoted, status="promoted")

            connection.execute(
                update(library_items)
                .where(library_items.c.id == pending_id)
                .values(
                    size_bytes=2 * 1024 * 1024 * 1024,
                    rel_path=f"tv/show/Season 2/{pending.name}",
                    parent_dir="tv/show/Season 2",
                    video_codec="h264",
                )
            )

            for item_id, name, rel_path, source_codec, bytes_saved in (
                (
                    folder_promoted_id,
                    folder_promoted.name,
                    f"tv/show/Season 2/{folder_promoted.name}",
                    "h264",
                    int(1.5 * 1024 * 1024 * 1024),
                ),
                (
                    codec_promoted_id,
                    codec_promoted.name,
                    f"movies/library/{codec_promoted.name}",
                    "h264",
                    1024 * 1024 * 1024,
                ),
                (
                    legacy_promoted_id,
                    legacy_promoted.name,
                    f"tv/show/Season 2/{legacy_promoted.name}",
                    None,
                    0,
                ),
            ):
                connection.execute(
                    update(library_items)
                    .where(library_items.c.id == item_id)
                    .values(
                        size_bytes=2 * 1024 * 1024 * 1024,
                        rel_path=rel_path,
                        parent_dir="/".join(rel_path.split("/")[:-1]),
                        video_codec="h264",
                    )
                )
                self._insert_staged_artifact(connection, item_id, self._staging_path(name))
                update_values = {
                    "promoted_at": web_app._now_iso(),
                    "source_rel_path": rel_path,
                    "source_size_bytes": 2 * 1024 * 1024 * 1024,
                    "bytes_saved": bytes_saved,
                }
                if source_codec is not None:
                    update_values["source_video_codec"] = source_codec
                connection.execute(
                    update(staged_artifacts)
                    .where(staged_artifacts.c.library_item_id == item_id)
                    .values(**update_values)
                )

            cards = web_app._list_folder_cards(self.config, connection)

        matching_cards = [card for card in cards if card.prefix == "tv/show/Season 2"]
        self.assertEqual(len(matching_cards), 1)
        self.assertEqual(matching_cards[0].estimated_savings_bytes, int(1.25 * 1024 * 1024 * 1024))

    def test_folder_cards_ignore_unlabeled_promotions_for_unknown_codec_history(self) -> None:
        pending = self._create_source_file("episode-pending.mkv")
        legacy_promoted = self._create_source_file("episode-legacy-promoted.mkv")

        with open_db(self.config.paths.db_path) as connection:
            pending_id = self._insert_library_item(connection, pending)
            legacy_promoted_id = self._insert_library_item(connection, legacy_promoted, status="promoted")

            connection.execute(
                update(library_items)
                .where(library_items.c.id == pending_id)
                .values(
                    size_bytes=2 * 1024 * 1024 * 1024,
                    rel_path=f"tv/show/Season 3/{pending.name}",
                    parent_dir="tv/show/Season 3",
                    video_codec=None,
                )
            )
            connection.execute(
                update(library_items)
                .where(library_items.c.id == legacy_promoted_id)
                .values(
                    size_bytes=2 * 1024 * 1024 * 1024,
                    rel_path=f"movies/library/{legacy_promoted.name}",
                    parent_dir="movies/library",
                    video_codec=None,
                )
            )

            self._insert_staged_artifact(connection, legacy_promoted_id, self._staging_path(legacy_promoted.name))
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == legacy_promoted_id)
                .values(
                    promoted_at=web_app._now_iso(),
                    source_rel_path=f"movies/library/{legacy_promoted.name}",
                    source_size_bytes=2 * 1024 * 1024 * 1024,
                    bytes_saved=int(1.5 * 1024 * 1024 * 1024),
                )
            )

            cards = web_app._list_folder_cards(self.config, connection)

        matching_cards = [card for card in cards if card.prefix == "tv/show/Season 3"]
        self.assertEqual(len(matching_cards), 1)
        self.assertEqual(matching_cards[0].estimated_savings_bytes, int(0.4 * 1024 * 1024 * 1024))

    def test_folder_cards_ignore_unlabeled_promotions_in_folder_history_ratio(self) -> None:
        pending = self._create_source_file("episode-pending.mkv")
        promoted_one = self._create_source_file("episode-promoted-one.mkv")
        promoted_two = self._create_source_file("episode-promoted-two.mkv")
        legacy_promoted = self._create_source_file("episode-legacy-promoted.mkv")

        with open_db(self.config.paths.db_path) as connection:
            pending_id = self._insert_library_item(connection, pending)
            promoted_one_id = self._insert_library_item(connection, promoted_one, status="promoted")
            promoted_two_id = self._insert_library_item(connection, promoted_two, status="promoted")
            legacy_promoted_id = self._insert_library_item(connection, legacy_promoted, status="promoted")

            for item_id, name in (
                (pending_id, pending.name),
                (promoted_one_id, promoted_one.name),
                (promoted_two_id, promoted_two.name),
                (legacy_promoted_id, legacy_promoted.name),
            ):
                connection.execute(
                    update(library_items)
                    .where(library_items.c.id == item_id)
                    .values(
                        size_bytes=2 * 1024 * 1024 * 1024,
                        rel_path=f"tv/show/Season 4/{name}",
                        parent_dir="tv/show/Season 4",
                        video_codec="h264",
                    )
                )

            for item_id, name, source_codec, bytes_saved in (
                (promoted_one_id, promoted_one.name, "h264", int(1.5 * 1024 * 1024 * 1024)),
                (promoted_two_id, promoted_two.name, "h264", int(1.5 * 1024 * 1024 * 1024)),
                (legacy_promoted_id, legacy_promoted.name, None, 0),
            ):
                self._insert_staged_artifact(connection, item_id, self._staging_path(name))
                update_values = {
                    "promoted_at": web_app._now_iso(),
                    "source_rel_path": f"tv/show/Season 4/{name}",
                    "source_size_bytes": 2 * 1024 * 1024 * 1024,
                    "bytes_saved": bytes_saved,
                }
                if source_codec is not None:
                    update_values["source_video_codec"] = source_codec
                connection.execute(
                    update(staged_artifacts)
                    .where(staged_artifacts.c.library_item_id == item_id)
                    .values(**update_values)
                )

            cards = web_app._list_folder_cards(self.config, connection)

        matching_cards = [card for card in cards if card.prefix == "tv/show/Season 4"]
        self.assertEqual(len(matching_cards), 1)
        self.assertEqual(matching_cards[0].estimated_savings_bytes, int(1.5 * 1024 * 1024 * 1024))

    def test_folder_cards_fall_back_to_codec_history_when_folder_history_missing(self) -> None:
        pending = self._create_source_file("episode-pending.mkv")
        promoted = self._create_source_file("episode-promoted.mkv")

        with open_db(self.config.paths.db_path) as connection:
            pending_id = self._insert_library_item(connection, pending)
            promoted_id = self._insert_library_item(connection, promoted, status="promoted")
            connection.execute(
                update(library_items)
                .where(library_items.c.id == pending_id)
                .values(
                    size_bytes=2 * 1024 * 1024 * 1024,
                    rel_path=f"tv/show/Season 2/{pending.name}",
                    parent_dir="tv/show/Season 2",
                    video_codec="h264",
                )
            )
            connection.execute(
                update(library_items)
                .where(library_items.c.id == promoted_id)
                .values(
                    size_bytes=2 * 1024 * 1024 * 1024,
                    rel_path=f"movies/library/{promoted.name}",
                    parent_dir="movies/library",
                    video_codec="h264",
                )
            )

            self._insert_staged_artifact(connection, promoted_id, self._staging_path(promoted.name))
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == promoted_id)
                .values(
                    promoted_at=web_app._now_iso(),
                    source_rel_path=f"movies/library/{promoted.name}",
                    source_video_codec="h264",
                    source_size_bytes=2 * 1024 * 1024 * 1024,
                    bytes_saved=1024 * 1024 * 1024,
                )
            )

            cards = web_app._list_folder_cards(self.config, connection)

        matching_cards = [card for card in cards if card.prefix == "tv/show/Season 2"]
        self.assertEqual(len(matching_cards), 1)
        self.assertEqual(matching_cards[0].estimated_savings_bytes, 1024 * 1024 * 1024)

    def test_project_env_loader_sets_defaults_without_overriding_shell_env(self) -> None:
        env_path = Path.home() / "Developer" / "claude-local-machine" / "projects" / "media-encoding" / ".env"
        with patch.object(web_app, "DEFAULT_CONFIG_PATH", env_path.parent / "config" / "defaults.toml"):
            with patch.object(Path, "exists", autospec=True) as exists_mock:
                exists_mock.side_effect = lambda path: path == env_path
                with patch.object(Path, "read_text", autospec=True, return_value=(
                        "MEDIAFORCE_WEB_HOST=0.0.0.0\n"
                        "MEDIAFORCE_WEB_PORT=8777\n"
                        "MEDIAFORCE_WEB_RELOAD=true\n"
                )):
                    with patch.dict(os.environ, {"MEDIAFORCE_WEB_PORT": "9999"}, clear=True):
                        web_app._load_project_env_file()
                        self.assertEqual(os.environ["MEDIAFORCE_WEB_HOST"], "0.0.0.0")
                        self.assertEqual(os.environ["MEDIAFORCE_WEB_PORT"], "9999")
                        self.assertEqual(os.environ["MEDIAFORCE_WEB_RELOAD"], "true")

    def test_default_web_host_and_reload_use_neutral_fallbacks(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(web_app._default_web_host(), "127.0.0.1")
            self.assertFalse(web_app._default_web_reload_enabled())

    def test_mediaforce_env_controls_host_and_reload(self) -> None:
        with patch.dict(os.environ, {"MEDIAFORCE_WEB_HOST": "0.0.0.0", "MEDIAFORCE_WEB_RELOAD": "true"}, clear=True):
            self.assertEqual(web_app._default_web_host(), "0.0.0.0")
            self.assertTrue(web_app._default_web_reload_enabled())

    def test_web_startup_cli_args_override_environment_defaults(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "MEDIAFORCE_WEB_HOST": "0.0.0.0",
                    "MEDIAFORCE_WEB_PORT": "8777",
                    "MEDIAFORCE_WEB_RELOAD": "true",
                },
                clear=True,
        ):
            args = web_app._parse_web_startup_args([
                "--host",
                "127.0.0.9",
                "--port",
                "8777",
                "--no-reload",
            ])
            settings = web_app._web_startup_settings(args)

        self.assertEqual(settings.host, "127.0.0.9")
        self.assertEqual(settings.port, 8777)
        self.assertFalse(settings.reload_enabled)

    def test_web_startup_shell_env_overrides_project_env_file(self) -> None:
        env_path = self.root / ".env"
        env_path.write_text(
            "MEDIAFORCE_WEB_HOST=0.0.0.0\n"
            "MEDIAFORCE_WEB_PORT=8777\n"
            "MEDIAFORCE_WEB_RELOAD=true\n"
        )
        with patch.object(web_app, "DEFAULT_CONFIG_PATH", self.root / "config" / "defaults.toml"):
            with patch.dict(os.environ, {"MEDIAFORCE_WEB_PORT": "9999"}, clear=True):
                web_app._load_project_env_file()
                args = web_app._parse_web_startup_args([])
                settings = web_app._web_startup_settings(args)

        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 9999)
        self.assertTrue(settings.reload_enabled)

    def test_main_uses_cli_values_for_uvicorn(self) -> None:
        config = self.config
        with patch.dict(
                os.environ,
                {
                    "MEDIAFORCE_WEB_HOST": "0.0.0.0",
                    "MEDIAFORCE_WEB_PORT": "8777",
                    "MEDIAFORCE_WEB_RELOAD": "true",
                },
                clear=True,
        ), patch("mediaforce.web.app.load_config", return_value=config), patch(
                "mediaforce.web.app.create_app", return_value=object()
        ), patch("mediaforce.web.app.uvicorn.run") as uvicorn_run_mock:
            web_app.main(["--host", "127.0.0.9", "--port", "8777", "--no-reload"])

        uvicorn_run_mock.assert_called_once()
        self.assertEqual(uvicorn_run_mock.call_args.kwargs["host"], "127.0.0.9")
        self.assertEqual(uvicorn_run_mock.call_args.kwargs["port"], 8777)
        self.assertNotIn("reload", uvicorn_run_mock.call_args.kwargs)

    def test_main_uses_cli_config_path_for_reload_app(self) -> None:
        config = self.config
        config_path = self.root / "custom.toml"
        with patch.dict(os.environ, {"MEDIAFORCE_WEB_RELOAD": "false"}, clear=True), patch(
                "mediaforce.web.app.load_config", return_value=config
        ) as load_config_mock, patch("mediaforce.web.app.uvicorn.run") as uvicorn_run_mock:
            web_app.main(["--config", str(config_path), "--reload"])
            self.assertEqual(os.environ["MEDIAFORCE_CONFIG_PATH"], str(config.paths.config_path))

        load_config_mock.assert_called_once_with(config_path)
        uvicorn_run_mock.assert_called_once()
        self.assertTrue(uvicorn_run_mock.call_args.kwargs["reload"])

    def test_main_uses_default_port_when_mediaforce_web_port_is_blank(self) -> None:
        config = self.config
        with patch.dict(os.environ, {"MEDIAFORCE_WEB_PORT": "", "MEDIAFORCE_WEB_RELOAD": "false"}, clear=True), patch(
                "mediaforce.web.app.load_config", return_value=config
        ), patch(
                "mediaforce.web.app.create_app", return_value=object()
        ), patch("mediaforce.web.app.uvicorn.run") as uvicorn_run_mock:
            web_app.main([])

        uvicorn_run_mock.assert_called_once()
        self.assertEqual(uvicorn_run_mock.call_args.kwargs["port"], 8777)

    def test_web_server_lock_rejects_second_backend_instance(self) -> None:
        settings = web_app.WebStartupSettings(
            config_path=self.config.paths.config_path,
            host="127.0.0.1",
            port=8777,
            reload_enabled=False,
        )
        lock_path = web_app._web_server_lock_path(self.config)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({"pid": 123, "host": "127.0.0.1", "port": 8777}))

        with patch("mediaforce.web.app.fcntl.flock", side_effect=BlockingIOError):
            with self.assertRaises(SystemExit) as raised:
                with web_app._exclusive_web_server_lock(self.config, settings):
                    pass

        self.assertIn("pid 123 on 127.0.0.1:8777", str(raised.exception))

    @staticmethod
    def test_create_reloadable_app_uses_default_config_when_env_path_is_blank() -> None:
        with patch.dict(os.environ, {"MEDIAFORCE_CONFIG_PATH": ""}, clear=True), patch(
                "mediaforce.web.app.create_app", return_value=object()
        ) as create_app_mock:
            web_app.create_reloadable_app()

        create_app_mock.assert_called_once_with(web_app.DEFAULT_CONFIG_PATH.expanduser())

    def test_parse_project_env_value_strips_matching_quotes(self) -> None:
        self.assertEqual(web_app._parse_project_env_value('"0.0.0.0"'), "0.0.0.0")
        self.assertEqual(web_app._parse_project_env_value("'8777'"), "8777")
        self.assertEqual(web_app._parse_project_env_value("true"), "true")

    def test_review_gate_requires_review_media_before_approval(self) -> None:
        missing_media_gate = web_app._review_gate(
            {
                "mode": "sample",
                "job_id": "sample-1",
                "preview_clips": [],
                "compare_clips": [],
                "review_media_ready": False,
            }
        )

        self.assertEqual(missing_media_gate["status"], "missing_review_media")
        self.assertFalse(missing_media_gate["can_confirm_full"])
        self.assertEqual(missing_media_gate["next_action_label"], "Run a fresh sample")

        payload = {
            "mode": "sample",
            "job_id": "sample-2",
            "preview_clips": [{"path": "/review-media/run/item-00/encoded-01.mp4"}],
            "compare_clips": [],
            "review_media_ready": True,
            "accepted_at": "2026-03-28T19:10:00+00:00",
        }
        payload = {
            **payload,
            "accepted_draft_hash": web_app._calibration_draft_hash(payload),
            "accepted_sample_job_id": "sample-2",
        }

        accepted_gate = web_app._review_gate(payload)

        self.assertEqual(accepted_gate["status"], "accepted")
        self.assertTrue(accepted_gate["can_confirm_full"])
        self.assertEqual(accepted_gate["next_action_label"], "Auto-queued after approval")

    def test_review_gate_preserves_legacy_approval_for_same_sample_when_hash_drifts(self) -> None:
        payload = {
            "mode": "sample",
            "job_id": "sample-2",
            "policy": {"video": {"target_vmaf": 95.0}},
            "review_media_ready": True,
            "accepted_at": "2026-03-28T19:10:00+00:00",
            "accepted_draft_hash": "older-hash",
            "accepted_sample_job_id": "sample-2",
            "advice": {"summary": "Non-policy metadata changed after approval."},
        }

        accepted_gate = web_app._review_gate(payload)

        self.assertEqual(accepted_gate["status"], "accepted")
        self.assertTrue(accepted_gate["can_confirm_full"])

    def test_review_gate_requires_reapproval_when_same_sample_policy_changes_after_approval(self) -> None:
        payload = {
            "mode": "sample",
            "job_id": "sample-2",
            "policy": {"video": {"target_vmaf": 95.0}},
            "review_media_ready": True,
            "accepted_at": "2026-03-28T19:10:00+00:00",
            "accepted_draft_hash": "older-hash",
            "accepted_policy_hash": folder_actions_runtime._calibration_policy_hash(
                {"policy": {"video": {"target_vmaf": 90.0}}}
            ),
            "accepted_sample_job_id": "sample-2",
        }

        review_gate = web_app._review_gate(payload)

        self.assertEqual(review_gate["status"], "needs_approval")
        self.assertFalse(review_gate["can_confirm_full"])

    def test_save_profile_action_auto_queues_folder_encode(self) -> None:
        manifest_path = self._write_manifest("manifest-approved-queue.json", [{"library_item_id": 1}])
        calibration_state: dict[str, folder_actions_runtime.ActionPayload] = {
            "tv/show": {
                "mode": "sample",
                "job_id": "sample-1",
                "review_media_ready": True,
                "policy": {"video": {"target_vmaf": 95.0}},
                "sample_item": {"library_item_id": 1},
            }
        }

        def load_calibration_state(
                _config: MediaforceConfig,
                prefix: str,
        ) -> folder_actions_runtime.ActionPayload | None:
            payload = calibration_state.get(prefix)
            return dict(payload) if payload is not None else None

        def save_calibration_state(
                _config: MediaforceConfig,
                prefix: str,
                payload: folder_actions_runtime.ActionPayload,
        ) -> None:
            calibration_state[prefix] = dict(payload)

        with patch("mediaforce.web.runtime.folder_actions.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.folder_actions.create_folder_manifest",
                return_value=({"items": [{"library_item_id": 1}]}, manifest_path),
        ):
            result = folder_actions_runtime.save_profile_action(
                self.config,
                "tv/show",
                now_iso=web_app._now_iso,
                load_sample_item=lambda *_args, **_kwargs: {"resolved_policy": {"video": {"target_vmaf": 95.0}}},
                load_calibration_state=load_calibration_state,
                calibration_draft_hash=web_app._calibration_draft_hash,
                save_calibration_state=save_calibration_state,
                load_advice_state=lambda *_args, **_kwargs: None,
                record_visual_approval_artifact=lambda *_args, **_kwargs: None,
                merge_advice_state=lambda *_args, **_kwargs: {},
                upsert_override=web_app._upsert_override,
                auto_queue_folder_encode=lambda prefix, notes,
                                                bypass_schedule: folder_actions_runtime.queue_folder_encode_action(
                    self.config,
                    prefix,
                    notes,
                    bypass_schedule,
                    now_iso=web_app._now_iso,
                    load_job_state=self._noop_load_job_state,
                    load_calibration_state=load_calibration_state,
                    review_gate=web_app._review_gate,
                    upsert_override=web_app._upsert_override,
                    load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
                    clear_terminal_encode_jobs_for_prefix_fn=clear_terminal_encode_jobs_for_prefix,
                    prepare_terminal_encode_job_for_requeue_fn=self._prepare_terminal_encode_job_for_requeue,
                    save_encode_job=save_encode_job,
                ),
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["queued"])
        self.assertEqual(result["auto_queue_status"], "queued")
        self.assertIn("queued the full folder encode", result["message"])
        calibration = load_calibration_state(self.config, "tv/show")
        assert calibration is not None
        self.assertTrue(calibration.get("accepted_at"))
        self.assertTrue(calibration.get("accepted_policy_hash"))
        with open_db(self.config.paths.db_path) as connection:
            rows = connection.execute(
                select(encode_jobs.c.job_kind, encode_jobs.c.status)
                .where(encode_jobs.c.prefix == "tv/show")
                .order_by(encode_jobs.c.created_at.asc(), encode_jobs.c.job_id.asc())
            ).mappings().fetchall()
        self.assertCountEqual(
            [(str(row["job_kind"]), str(row["status"])) for row in rows],
            [("folder", "queued"), ("shard", "queued")],
        )

    def test_save_profile_action_reports_when_nothing_is_left_to_queue(self) -> None:
        manifest_path = self._write_manifest("manifest-approved-empty.json", [])
        calibration_state: dict[str, folder_actions_runtime.ActionPayload] = {
            "tv/show": {
                "mode": "sample",
                "job_id": "sample-1",
                "review_media_ready": True,
                "policy": {"video": {"target_vmaf": 95.0}},
                "sample_item": {"library_item_id": 1},
            }
        }

        def load_calibration_state(
                _config: MediaforceConfig,
                prefix: str,
        ) -> folder_actions_runtime.ActionPayload | None:
            payload = calibration_state.get(prefix)
            return dict(payload) if payload is not None else None

        def save_calibration_state(
                _config: MediaforceConfig,
                prefix: str,
                payload: folder_actions_runtime.ActionPayload,
        ) -> None:
            calibration_state[prefix] = dict(payload)

        with patch("mediaforce.web.runtime.folder_actions.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.folder_actions.create_folder_manifest",
                return_value=({"items": []}, manifest_path),
        ):
            result = folder_actions_runtime.save_profile_action(
                self.config,
                "tv/show",
                now_iso=web_app._now_iso,
                load_sample_item=lambda *_args, **_kwargs: {"resolved_policy": {"video": {"target_vmaf": 95.0}}},
                load_calibration_state=load_calibration_state,
                calibration_draft_hash=web_app._calibration_draft_hash,
                save_calibration_state=save_calibration_state,
                load_advice_state=lambda *_args, **_kwargs: None,
                record_visual_approval_artifact=lambda *_args, **_kwargs: None,
                merge_advice_state=lambda *_args, **_kwargs: {},
                upsert_override=web_app._upsert_override,
                auto_queue_folder_encode=lambda prefix, notes,
                                                bypass_schedule: folder_actions_runtime.queue_folder_encode_action(
                    self.config,
                    prefix,
                    notes,
                    bypass_schedule,
                    now_iso=web_app._now_iso,
                    load_job_state=self._noop_load_job_state,
                    load_calibration_state=load_calibration_state,
                    review_gate=web_app._review_gate,
                    upsert_override=web_app._upsert_override,
                    load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
                    clear_terminal_encode_jobs_for_prefix_fn=clear_terminal_encode_jobs_for_prefix,
                    prepare_terminal_encode_job_for_requeue_fn=self._prepare_terminal_encode_job_for_requeue,
                    save_encode_job=save_encode_job,
                ),
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["queued"])
        self.assertEqual(result["auto_queue_status"], "no_pending")
        self.assertIn("no pending items left to queue", result["message"])

    def test_retry_failed_encode_queue_action_retries_latest_approved_failures_only(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            now = web_app._now_iso()
            for prefix, status in (
                    ("tv/approved-a", "needs_attention"),
                    ("tv/approved-b", "failed"),
                    ("tv/review-blocked", "stopped"),
                    ("tv/already-running", "running"),
            ):
                save_encode_job(
                    connection,
                    {
                        "job_id": f"job-{prefix.rsplit('/', 1)[-1]}",
                        "prefix": prefix,
                        "job_kind": "folder",
                        "parent_job_id": None,
                        "status": status,
                        "manifest_path": str(self.root / "runs" / f"{prefix.rsplit('/', 1)[-1]}.json"),
                        "item_count": 1,
                        "saved_profile_path": None,
                        "host": {},
                        "last_host": {},
                        "notes": "",
                        "bypass_schedule": False,
                        "attempt_count": 3,
                        "process_pid": None,
                        "error": None,
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
                        "created_at": now,
                        "started_at": None,
                        "finished_at": now if status in {"needs_attention", "failed", "stopped"} else None,
                        "updated_at": now,
                    },
                )

        queued_prefixes: list[str] = []

        def load_calibration_state(
                _config: MediaforceConfig,
                prefix: str,
        ) -> folder_actions_runtime.ActionPayload | None:
            if prefix == "tv/review-blocked":
                return {"policy": {}, "review_media_ready": True}
            return {"policy": {}, "accepted_at": web_app._now_iso(), "accepted_sample_job_id": "sample-1",
                    "job_id": "sample-1"}

        def review_gate(
                calibration: folder_actions_runtime.ActionPayload | None) -> folder_actions_runtime.ActionPayload:
            if calibration and calibration.get("accepted_at"):
                return self._accepted_review_gate(calibration)
            return {
                "can_confirm_full": False,
                "message": "Review clips and approve",
                "status": "needs_approval",
                "next_action_label": "Review clips and approve",
            }

        def queue_folder_encode_action(prefix: str, _notes: str,
                                       _bypass_schedule: bool) -> folder_actions_runtime.ActionPayload:
            queued_prefixes.append(prefix)
            return {"ok": True, "message": "Queued the full folder encode."}

        result = queue_actions_runtime.retry_failed_encode_queue_action(
            connection_factory=lambda: open_db(self.config.paths.db_path),
            config=self.config,
            load_calibration_state=load_calibration_state,
            review_gate=review_gate,
            queue_folder_encode_action=queue_folder_encode_action,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(set(queued_prefixes), {"tv/approved-a", "tv/approved-b"})
        self.assertEqual(result["queued_count"], 2)
        self.assertEqual(result["review_blocked_count"], 1)
        self.assertEqual(result["review_blocked_prefixes"], ["tv/review-blocked"])

    def test_retry_failed_encode_prefix_action_retries_only_requested_prefix(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            now = web_app._now_iso()
            for prefix in ("tv/approved-a", "tv/approved-b"):
                save_encode_job(
                    connection,
                    {
                        "job_id": f"job-{prefix.rsplit('/', 1)[-1]}",
                        "prefix": prefix,
                        "job_kind": "folder",
                        "parent_job_id": None,
                        "status": "needs_attention",
                        "manifest_path": str(self.root / "runs" / f"{prefix.rsplit('/', 1)[-1]}.json"),
                        "item_count": 1,
                        "saved_profile_path": None,
                        "host": {},
                        "last_host": {},
                        "notes": "",
                        "bypass_schedule": False,
                        "attempt_count": 3,
                        "process_pid": None,
                        "error": None,
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
                        "created_at": now,
                        "started_at": None,
                        "finished_at": now,
                        "updated_at": now,
                    },
                )

        queued_prefixes: list[str] = []

        def queue_folder_encode_action(prefix: str, _notes: str,
                                       _bypass_schedule: bool) -> folder_actions_runtime.ActionPayload:
            queued_prefixes.append(prefix)
            return {"ok": True, "message": "Queued the full folder encode."}

        result = queue_actions_runtime.retry_failed_encode_prefix_action(
            connection_factory=lambda: open_db(self.config.paths.db_path),
            config=self.config,
            prefix="tv/approved-b",
            load_calibration_state=lambda _config, _prefix: {
                "policy": {},
                "accepted_at": web_app._now_iso(),
                "accepted_sample_job_id": "sample-1",
                "job_id": "sample-1",
            },
            review_gate=self._accepted_review_gate,
            queue_folder_encode_action=queue_folder_encode_action,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(queued_prefixes, ["tv/approved-b"])
        self.assertEqual(result["queued_prefixes"], ["tv/approved-b"])

    def test_retry_failed_encode_prefix_action_reports_non_retryable_prefix(self) -> None:
        result = queue_actions_runtime.retry_failed_encode_prefix_action(
            connection_factory=lambda: open_db(self.config.paths.db_path),
            config=self.config,
            prefix="tv/not-failed",
            load_calibration_state=lambda _config, _prefix: None,
            review_gate=self._accepted_review_gate,
            queue_folder_encode_action=lambda _prefix, _notes, _bypass_schedule: {"ok": True},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["queued_count"], 0)
        self.assertEqual(result["queued_prefixes"], [])
        self.assertIn("No failed folder encode was ready", result["message"])

    def test_save_profile_action_requires_high_impact_confirmation(self) -> None:
        calibration_payload: folder_actions_runtime.ActionPayload = {
            "mode": "sample",
            "job_id": "sample-1",
            "review_media_ready": True,
            "policy": {"video": {"target_vmaf": 88.0, "max_encoded_percent": 8, "default_grain": 4}},
            "sample_item": {
                "library_item_id": 1,
                "resolved_policy": {
                    "video": {
                        "quality_metric": "vmaf",
                        "target_vmaf": 95.0,
                        "max_encoded_percent": 55,
                        "default_grain": 0,
                    }
                },
            },
        }

        with self.assertRaises(HTTPException) as exc:
            folder_actions_runtime.save_profile_action(self.config, "tv/show", now_iso=web_app._now_iso,
                                                       load_sample_item=lambda *_args, **_kwargs: None,
                                                       load_calibration_state=lambda *_args, **_kwargs: dict(
                                                           calibration_payload),
                                                       calibration_draft_hash=web_app._calibration_draft_hash,
                                                       save_calibration_state=lambda *_args, **_kwargs: None,
                                                       load_advice_state=lambda *_args, **_kwargs: None,
                                                       record_visual_approval_artifact=lambda *_args, **_kwargs: None,
                                                       merge_advice_state=lambda *_args, **_kwargs: {},
                                                       upsert_override=web_app._upsert_override)

        self.assertEqual(exc.exception.status_code, 409)
        self.assertIn("high-impact policy changes", str(exc.exception.detail))

    def test_save_profile_action_rejects_stale_high_impact_confirmation_hash(self) -> None:
        calibration_payload: folder_actions_runtime.ActionPayload = {
            "mode": "sample",
            "job_id": "sample-1",
            "review_media_ready": True,
            "policy": {"video": {"target_vmaf": 88.0, "max_encoded_percent": 8, "default_grain": 4}},
            "sample_item": {
                "library_item_id": 1,
                "resolved_policy": {
                    "video": {
                        "quality_metric": "vmaf",
                        "target_vmaf": 95.0,
                        "max_encoded_percent": 55,
                        "default_grain": 0,
                    }
                },
            },
            "draft_hash": "draft-current",
        }

        with self.assertRaises(HTTPException) as exc:
            folder_actions_runtime.save_profile_action(self.config, "tv/show", now_iso=web_app._now_iso,
                                                       load_sample_item=lambda *_args, **_kwargs: None,
                                                       load_calibration_state=lambda *_args, **_kwargs: dict(
                                                           calibration_payload),
                                                       calibration_draft_hash=web_app._calibration_draft_hash,
                                                       save_calibration_state=lambda *_args, **_kwargs: None,
                                                       load_advice_state=lambda *_args, **_kwargs: None,
                                                       record_visual_approval_artifact=lambda *_args, **_kwargs: None,
                                                       merge_advice_state=lambda *_args, **_kwargs: {},
                                                       upsert_override=web_app._upsert_override,
                                                       confirm_high_impact=True, reviewed_draft_hash="draft-old")

        self.assertEqual(exc.exception.status_code, 409)
        self.assertIn("changed after the high-impact review", str(exc.exception.detail))

    def test_save_profile_action_prefers_persisted_draft_baseline_for_high_impact_guard(self) -> None:
        calibration_payload: folder_actions_runtime.ActionPayload = {
            "mode": "sample",
            "job_id": "sample-1",
            "review_media_ready": True,
            "policy": {"video": {"target_vmaf": 88.0, "max_encoded_percent": 8, "default_grain": 4}},
            "sample_item": {
                "library_item_id": 1,
                "resolved_policy": {
                    "video": {
                        "quality_metric": "vmaf",
                        "target_vmaf": 95.0,
                        "max_encoded_percent": 55,
                        "default_grain": 0,
                    }
                },
            },
        }

        with self.assertRaises(HTTPException) as exc:
            folder_actions_runtime.save_profile_action(self.config, "tv/show", now_iso=web_app._now_iso,
                                                       load_sample_item=lambda *_args, **_kwargs: {
                                                           "resolved_policy": {
                                                               "video": {
                                                                   "quality_metric": "vmaf",
                                                                   "target_vmaf": 88.0,
                                                                   "max_encoded_percent": 8,
                                                                   "default_grain": 4,
                                                               }
                                                           }
                                                       }, load_calibration_state=lambda *_args, **_kwargs: dict(
                    calibration_payload), calibration_draft_hash=web_app._calibration_draft_hash,
                                                       save_calibration_state=lambda *_args, **_kwargs: None,
                                                       load_advice_state=lambda *_args, **_kwargs: None,
                                                       record_visual_approval_artifact=lambda *_args, **_kwargs: None,
                                                       merge_advice_state=lambda *_args, **_kwargs: {},
                                                       upsert_override=web_app._upsert_override)

        self.assertEqual(exc.exception.status_code, 409)
        self.assertIn("high-impact policy changes", str(exc.exception.detail))

    def test_save_profile_action_rejects_softened_confirmed_operator_request(self) -> None:
        calibration_payload: folder_actions_runtime.ActionPayload = {
            "mode": "sample",
            "job_id": "sample-1",
            "review_media_ready": True,
            "policy": {"video": {"target_vmaf": 95.0}},
            "sample_item": {
                "library_item_id": 1,
                "resolved_policy": {"video": {"target_vmaf": 95.0}},
            },
        }

        with self.assertRaises(HTTPException) as exc:
            folder_actions_runtime.save_profile_action(
                self.config,
                "tv/show",
                now_iso=web_app._now_iso,
                load_sample_item=lambda *_args, **_kwargs: None,
                load_calibration_state=lambda *_args, **_kwargs: dict(calibration_payload),
                calibration_draft_hash=web_app._calibration_draft_hash,
                save_calibration_state=lambda *_args, **_kwargs: None,
                load_advice_state=lambda *_args, **_kwargs: {
                    "request_disposition": "softened",
                    "operator_request": {
                        "operator_confirmed": True,
                        "request_type": "size_budget",
                        "applied_policy": {"video": {"max_encoded_percent": 8}},
                    },
                },
                record_visual_approval_artifact=lambda *_args, **_kwargs: None,
                merge_advice_state=lambda *_args, **_kwargs: {},
                upsert_override=web_app._upsert_override,
                confirm_high_impact=True,
                reviewed_draft_hash=web_app._calibration_draft_hash(calibration_payload),
            )

        self.assertEqual(exc.exception.status_code, 409)
        self.assertIn("does not carry the approved operator request", str(exc.exception.detail))

    def test_save_profile_action_rejects_alignment_mismatch_before_approval(self) -> None:
        calibration_payload: folder_actions_runtime.ActionPayload = {
            "mode": "sample",
            "job_id": "sample-1",
            "review_media_ready": True,
            "policy": {"video": {"target_vmaf": 89.0, "max_encoded_percent": 2}},
            "sample_item": {
                "library_item_id": 1,
                "resolved_policy": {"video": {"target_vmaf": 95.0, "max_encoded_percent": 55}},
            },
        }

        with self.assertRaises(HTTPException) as exc:
            folder_actions_runtime.save_profile_action(
                self.config,
                "tv/show",
                now_iso=web_app._now_iso,
                load_sample_item=lambda *_args, **_kwargs: None,
                load_calibration_state=lambda *_args, **_kwargs: dict(calibration_payload),
                calibration_draft_hash=web_app._calibration_draft_hash,
                save_calibration_state=lambda *_args, **_kwargs: None,
                load_advice_state=lambda *_args, **_kwargs: {
                    "request_disposition": "honored_with_risk",
                    "operator_request": {
                        "operator_confirmed": True,
                        "request_type": "combined_experiment",
                        "scale_height": 1080,
                        "budget_bytes": 314572800,
                        "applied_max_encoded_percent": 2,
                        "applied_policy": {"video": {"max_height": 1080, "max_encoded_percent": 2}},
                        "size_budget_request": {"applied_max_encoded_percent": 2},
                    },
                },
                record_visual_approval_artifact=lambda *_args, **_kwargs: None,
                merge_advice_state=lambda *_args, **_kwargs: {},
                upsert_override=web_app._upsert_override,
                confirm_high_impact=True,
                reviewed_draft_hash=web_app._calibration_draft_hash(calibration_payload),
            )

        self.assertEqual(exc.exception.status_code, 409)
        self.assertIn("does not apply the requested 1080p height cap", str(exc.exception.detail))

    def test_save_profile_action_allows_explicit_size_tradeoff_approval(self) -> None:
        saved_payloads: list[folder_actions_runtime.ActionPayload] = []
        merged_advice: list[folder_actions_runtime.ActionPayload] = []
        calibration_payload: folder_actions_runtime.ActionPayload = {
            "mode": "sample",
            "job_id": "sample-1",
            "review_media_ready": True,
            "policy": {"video": {"target_vmaf": 85.0, "max_encoded_percent": 80}},
            "sample_item": {
                "library_item_id": 1,
                "resolved_policy": {"video": {"target_vmaf": 85.0, "max_encoded_percent": 80}},
            },
            "sample_result": {"predicted_total_size_bytes": 376 * 1024 * 1024},
        }

        result = folder_actions_runtime.save_profile_action(
            self.config,
            "tv/show",
            now_iso=lambda: "2026-05-24T04:40:00+00:00",
            load_sample_item=lambda *_args, **_kwargs: None,
            load_calibration_state=lambda *_args, **_kwargs: dict(calibration_payload),
            calibration_draft_hash=web_app._calibration_draft_hash,
            save_calibration_state=lambda _config, _prefix, payload: saved_payloads.append(dict(payload)),
            load_advice_state=lambda *_args, **_kwargs: {
                "request_disposition": "honored",
                "operator_request": {
                    "operator_confirmed": True,
                    "request_type": "size_budget",
                    "budget_bytes": 300 * 1024 * 1024,
                    "budget_label": "300 MB per episode",
                    "applied_policy": None,
                },
            },
            record_visual_approval_artifact=lambda *_args, **_kwargs: {"artifact_id": "approval-1"},
            merge_advice_state=lambda _config, _prefix, payload: merged_advice.append(dict(payload)),
            upsert_override=lambda *_args, **_kwargs: None,
            auto_queue_folder_encode=lambda *_args, **_kwargs: {"ok": True, "message": "Queued folder encode."},
            confirm_size_tradeoff=True,
            reviewed_draft_hash=web_app._calibration_draft_hash(calibration_payload),
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["queued"])
        self.assertEqual(saved_payloads[0]["accepted_at"], "2026-05-24T04:40:00+00:00")
        self.assertTrue(merged_advice[0]["operator_approved_size_tradeoff"])

    def test_run_sampled_calibration_keeps_review_directory_for_approval(self) -> None:
        source_path = self._create_source_file("episode-review.mkv")
        preview_dir = self.config.paths.review_dir / "run-123" / "item-00"
        preview_clip = EncodedPreviewClip(
            output_path=preview_dir / "encoded-01-00-10.mp4",
            timestamp_seconds=10.0,
            duration_seconds=8.0,
            size_bytes=1024,
        )
        source_clip = BrowserReviewClip(
            output_path=preview_dir / "source-01-00-10.mp4",
            timestamp_seconds=10.0,
            duration_seconds=8.0,
            size_bytes=2048,
        )
        compare_clip = CompareClip(
            output_path=preview_dir / "compare-01-00-10.mkv",
            timestamp_seconds=10.0,
            duration_seconds=8.0,
        )

        with patch.object(web_app, "search_quality_for_source",
                          return_value=QualitySearchResult(27.0, "VMAF", 94.5, 94.6, "ok")):
            with patch.object(
                    web_app,
                    "run_sample_encode",
                    return_value=SampleEncodeResult("VMAF", 94.6, 18.0, 1200.0, 100_000_000, "ok"),
            ):
                with patch.object(web_app, "recommend_review_timestamps", return_value=[10.0]):
                    with patch.object(web_app, "encode_preview_clips", return_value=[preview_clip]):
                        with patch.object(web_app, "render_source_review_clips", return_value=[source_clip]):
                            with patch.object(web_app, "generate_compare_clips_from_previews",
                                              return_value=[compare_clip]):
                                with patch.object(
                                        web_app,
                                        "estimate_output_overhead_bytes",
                                        return_value={"audio_bytes": 1, "subtitle_bytes": 2, "container_bytes": 3,
                                                      "total_bytes": 6},
                                ):
                                    payload, cleanup_path = web_app._run_sampled_calibration(
                                        config=self.config,
                                        prefix="tv/show",
                                        action="baseline",
                                        host_data={"key": "localhost"},
                                        notes="",
                                        policy={
                                            "video": {
                                                "encoder": "libsvtav1",
                                                "pixel_format": "yuv420p10le",
                                                "quality_metric": "auto",
                                                "sample_every": "8m",
                                                "sample_duration": "20s",
                                                "preset": 4,
                                            },
                                            "audio": {},
                                            "subtitle": {},
                                        },
                                        seed_metadata=None,
                                        sample_item={
                                            "source_path": str(source_path),
                                            "source_size_bytes": 200_000_000,
                                            "duration_seconds": 2600.0,
                                            "rel_path": "tv/show/episode-review.mkv",
                                        },
                                        calibration_run_id="run-123",
                                        process_controller=Mock(),
                                    )

        self.assertIsNone(cleanup_path)
        self.assertTrue(payload["preview_clips"][0]["path"].startswith("/review-media/run-123/"))
        self.assertTrue(payload["source_clips"][0]["path"].startswith("/review-media/run-123/"))
        self.assertTrue(payload["compare_clips"][0]["path"].startswith("/review-media/run-123/"))

    def test_load_calibration_state_builds_review_pairs_for_browser_player(self) -> None:
        review_dir = self.config.paths.review_dir / "run-pairs" / "item-00"
        review_dir.mkdir(parents=True, exist_ok=True)
        for name in ("source-01-00-10.mp4", "encoded-01-00-10.mp4", "compare-01-00-10.mkv"):
            (review_dir / name).write_text("clip")
        calibration_path = web_app._calibration_file(self.config, "tv/show")
        calibration_path.parent.mkdir(parents=True, exist_ok=True)
        calibration_path.write_text(
            json.dumps(
                {
                    "mode": "sample",
                    "job_id": "sample-9",
                    "source_clips": [
                        {
                            "path": "/review-media/run-pairs/item-00/source-01-00-10.mp4",
                            "timestamp_seconds": 10.0,
                            "duration_seconds": 8.0,
                        }
                    ],
                    "preview_clips": [
                        {
                            "path": "/review-media/run-pairs/item-00/encoded-01-00-10.mp4",
                            "timestamp_seconds": 10.0,
                            "duration_seconds": 8.0,
                        }
                    ],
                    "compare_clips": [
                        {
                            "path": "/review-media/run-pairs/item-00/compare-01-00-10.mkv",
                            "timestamp_seconds": 10.0,
                            "duration_seconds": 8.0,
                        }
                    ],
                }
            )
        )

        payload = web_app._load_calibration_state(self.config, "tv/show")

        assert payload is not None
        self.assertTrue(payload["browser_review_ready"])
        self.assertEqual(len(payload["review_pairs"]), 1)
        self.assertEqual(payload["review_pairs"][0]["source_clip"]["path"],
                         "/review-media/run-pairs/item-00/source-01-00-10.mp4")
        self.assertEqual(payload["review_pairs"][0]["preview_clip"]["path"],
                         "/review-media/run-pairs/item-00/encoded-01-00-10.mp4")

    def test_load_calibration_state_merges_retained_compare_clip_into_existing_pair(self) -> None:
        review_dir = self.config.paths.review_dir / "run-pairs" / "item-00"
        review_dir.mkdir(parents=True, exist_ok=True)
        for name in ("source-01-00-10.mp4", "encoded-01-00-10.mp4", "compare-01-00-10.mkv"):
            (review_dir / name).write_text("clip")
        calibration_path = web_app._calibration_file(self.config, "tv/show")
        calibration_path.parent.mkdir(parents=True, exist_ok=True)
        calibration_path.write_text(
            json.dumps(
                {
                    "mode": "sample",
                    "job_id": "sample-9",
                    "source_clips": [
                        {
                            "path": "/review-media/run-pairs/item-00/source-01-00-10.mp4",
                            "timestamp_seconds": 10.0,
                            "duration_seconds": 8.0,
                        }
                    ],
                    "preview_clips": [
                        {
                            "path": "/review-media/run-pairs/item-00/encoded-01-00-10.mp4",
                            "timestamp_seconds": 10.0,
                            "duration_seconds": 8.0,
                        }
                    ],
                    "review_pairs": [
                        {
                            "timestamp_seconds": 10.0,
                            "duration_seconds": 8.0,
                            "source_clip": {"path": "/review-media/run-pairs/item-00/source-01-00-10.mp4"},
                            "preview_clip": {"path": "/review-media/run-pairs/item-00/encoded-01-00-10.mp4"},
                            "compare_clip": {"path": "/review-media/run-pairs/item-00/compare-01-00-10.mkv"},
                        }
                    ],
                }
            )
        )

        payload = web_app._load_calibration_state(self.config, "tv/show")

        assert payload is not None
        self.assertEqual(len(payload["review_pairs"]), 1)
        self.assertEqual(
            payload["review_pairs"][0]["compare_clip"]["path"],
            "/review-media/run-pairs/item-00/compare-01-00-10.mkv",
        )

    def test_folder_review_badge_marks_ready_and_refresh_states(self) -> None:
        ready_dir = self.config.paths.review_dir / "run-ready" / "item-00"
        ready_dir.mkdir(parents=True, exist_ok=True)
        for name in ("source-01-00-10.mp4", "encoded-01-00-10.mp4"):
            (ready_dir / name).write_text("clip")
        ready_calibration = web_app._calibration_file(self.config, "tv/show-ready")
        ready_calibration.parent.mkdir(parents=True, exist_ok=True)
        ready_calibration.write_text(
            json.dumps(
                {
                    "mode": "sample",
                    "job_id": "sample-ready",
                    "source_clips": [
                        {
                            "path": "/review-media/run-ready/item-00/source-01-00-10.mp4",
                            "timestamp_seconds": 10.0,
                            "duration_seconds": 8.0,
                        }
                    ],
                    "preview_clips": [
                        {
                            "path": "/review-media/run-ready/item-00/encoded-01-00-10.mp4",
                            "timestamp_seconds": 10.0,
                            "duration_seconds": 8.0,
                        }
                    ],
                }
            )
        )

        refresh_dir = self.config.paths.review_dir / "run-refresh" / "item-00"
        refresh_dir.mkdir(parents=True, exist_ok=True)
        (refresh_dir / "compare-01-00-10.mkv").write_text("clip")
        refresh_calibration = web_app._calibration_file(self.config, "tv/show-refresh")
        refresh_calibration.write_text(
            json.dumps(
                {
                    "mode": "sample",
                    "job_id": "sample-refresh",
                    "compare_clips": [
                        {
                            "path": "/review-media/run-refresh/item-00/compare-01-00-10.mkv",
                            "timestamp_seconds": 10.0,
                            "duration_seconds": 8.0,
                        }
                    ],
                }
            )
        )

        self.assertEqual(
            web_app._folder_review_badge(self.config, "tv/show-ready"),
            {"label": "Ready to review", "tone": "attention"},
        )
        self.assertEqual(
            web_app._folder_review_badge(self.config, "tv/show-refresh"),
            {"label": "Refresh review", "tone": "warning"},
        )

    def test_folder_review_badge_prioritizes_active_sample_job(self) -> None:
        ready_dir = self.config.paths.review_dir / "run-active-sample" / "item-00"
        ready_dir.mkdir(parents=True, exist_ok=True)
        for name in ("source-01-00-10.mp4", "encoded-01-00-10.mp4"):
            (ready_dir / name).write_text("clip")
        ready_calibration = web_app._calibration_file(self.config, "tv/show")
        ready_calibration.parent.mkdir(parents=True, exist_ok=True)
        ready_calibration.write_text(
            json.dumps(
                {
                    "mode": "sample",
                    "job_id": "sample-ready",
                    "source_clips": [
                        {
                            "path": "/review-media/run-active-sample/item-00/source-01-00-10.mp4",
                            "timestamp_seconds": 10.0,
                            "duration_seconds": 8.0,
                        }
                    ],
                    "preview_clips": [
                        {
                            "path": "/review-media/run-active-sample/item-00/encoded-01-00-10.mp4",
                            "timestamp_seconds": 10.0,
                            "duration_seconds": 8.0,
                        }
                    ],
                }
            )
        )
        now = web_app._now_iso()

        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                calibration_jobs.insert().values(
                    job_id="sample-running",
                    prefix="tv/show",
                    status="running",
                    lane="sample",
                    action="ai_tune",
                    host_json=json.dumps({"key": "local", "label": "Local"}),
                    notes="",
                    policy_json="{}",
                    sample_item_json="{}",
                    owner_pid=os.getpid(),
                    created_at=now,
                    started_at=now,
                    updated_at=now,
                )
            )

            self.assertEqual(
                web_app._folder_review_badge(self.config, "tv/show", connection),
                {
                    "label": "Sample running",
                    "tone": "attention",
                    "detail": "Sample calibration is active.",
                },
            )

    def test_folder_review_badge_exposes_failed_sample_job(self) -> None:
        now = web_app._now_iso()

        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                calibration_jobs.insert().values(
                    job_id="sample-failed",
                    prefix="tv/show",
                    status="failed",
                    lane="sample",
                    action="ai_tune",
                    host_json=json.dumps({"key": "local", "label": "Local"}),
                    notes="",
                    policy_json="{}",
                    sample_item_json="{}",
                    error="ffmpeg output\nError: source disappeared after reset",
                    created_at=now,
                    started_at=now,
                    finished_at=now,
                    updated_at=now,
                )
            )

            self.assertEqual(
                web_app._folder_review_badge(self.config, "tv/show", connection),
                {
                    "label": "Sample failed",
                    "tone": "warning",
                    "detail": "Error: source disappeared after reset",
                },
            )

    def test_folder_review_badge_prioritizes_encode_needs_attention(self) -> None:
        accepted_calibration = web_app._calibration_file(self.config, "tv/show")
        accepted_calibration.parent.mkdir(parents=True, exist_ok=True)
        accepted_calibration.write_text(json.dumps({"accepted_at": web_app._now_iso()}))
        manifest_path = self._write_manifest("manifest-needs-attention.json", [])
        now = web_app._now_iso()

        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "encode-needs-attention",
                    "prefix": "tv/show",
                    "status": "needs_attention",
                    "manifest_path": str(manifest_path),
                    "item_count": 2,
                    "saved_profile_path": None,
                    "host": {"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": "ffmpeg output\nError: Failed to find a suitable crf",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": "needs_attention",
                    "last_failure_kind": "deterministic",
                    "last_failure_at": now,
                    "host_cooldown_until": None,
                    "created_at": now,
                    "started_at": now,
                    "finished_at": now,
                    "updated_at": now,
                },
            )

            self.assertEqual(
                web_app._folder_review_badge(self.config, "tv/show", connection),
                {
                    "label": "Needs attention",
                    "tone": "warning",
                    "detail": "Error: Failed to find a suitable crf",
                },
            )

    def test_folder_needs_attention_badges_use_latest_jobs_in_one_pass(self) -> None:
        manifest_path = self._write_manifest("manifest-needs-attention-badges.json", [])
        base_time = datetime.now(UTC)

        def job_payload(job_id: str, prefix: str, status: str, created_at: str) -> dict[str, Any]:
            return {
                "job_id": job_id,
                "prefix": prefix,
                "status": status,
                "manifest_path": str(manifest_path),
                "item_count": 1,
                "saved_profile_path": None,
                "host": {"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                "last_host": {},
                "notes": "",
                "bypass_schedule": False,
                "attempt_count": 1,
                "process_pid": None,
                "error": "Error: bad source",
                "leased_at": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
                "worker_id": None,
                "retry_not_before": None,
                "waiting_reason": None,
                "terminal_reason": status,
                "last_failure_kind": "deterministic",
                "last_failure_at": created_at,
                "host_cooldown_until": None,
                "created_at": created_at,
                "started_at": created_at,
                "finished_at": created_at,
                "updated_at": created_at,
            }

        stale_attention_at = (base_time - timedelta(minutes=2)).isoformat()
        completed_at = (base_time - timedelta(minutes=1)).isoformat()
        latest_attention_at = base_time.isoformat()
        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                job_payload("stale-attention", "tv/stale", "needs_attention", stale_attention_at),
            )
            save_encode_job(
                connection,
                job_payload("stale-completed", "tv/stale", "completed", completed_at),
            )
            save_encode_job(
                connection,
                job_payload("latest-attention", "tv/latest", "needs_attention", latest_attention_at),
            )

            badges = web_app._folder_needs_attention_badges(connection)

        self.assertNotIn("tv/stale", badges)
        self.assertEqual(
            badges["tv/latest"],
            {"label": "Needs attention", "tone": "warning", "detail": "Error: bad source"},
        )

    def test_resolve_sample_host_maps_legacy_local_key_to_self_host(self) -> None:
        statuses = [
            HostStatus(
                key="cbusillo@localhost",
                label="Chris-Studio",
                mode="ssh",
                priority=100,
                capabilities=["encode_queue", "sample_calibration"],
                available=True,
                message="Mounted and ready",
                missing_paths=[],
                repo_path=str(self.root),
            )
        ]
        with patch("mediaforce.web.app._safe_collect_host_statuses", return_value=statuses):
            host = web_app._resolve_sample_host(self.config, "local")
        self.assertEqual(host.key, "cbusillo@localhost")

    def test_resolve_sample_host_accepts_remote_sample_host(self) -> None:
        statuses = [
            HostStatus(
                key="cbusillo@m1-mini",
                label="M1 mini",
                mode="ssh",
                priority=80,
                capabilities=["sample_calibration"],
                available=True,
                message="Mounted and ready",
                missing_paths=[],
                repo_path=str(self.root),
            )
        ]
        with patch("mediaforce.web.app._safe_collect_host_statuses", return_value=statuses):
            host = web_app._resolve_sample_host(self.config, "cbusillo@m1-mini")
        self.assertEqual(host.key, "cbusillo@m1-mini")

    def test_resolve_sample_host_allows_closed_encode_schedule_for_manual_samples(self) -> None:
        config = replace(
            self.config,
            raw={
                **self.config.raw,
                "remote_hosts": [
                    {
                        "label": "M4 Studio",
                        "host": "cbusillo@localhost",
                        "capabilities": ["encode_queue", "sample_calibration"],
                        "schedule_profile": "tou_vb",
                    }
                ],
                "encode_queue": {
                    "schedule_profiles": [
                        {
                            "key": "tou_vb",
                            "label": "Time of Use VB Super off Peak",
                            "mode": "night",
                            "timezone": "host_local",
                            "start_hour": 0,
                            "end_hour": 5,
                            "days_of_week": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                        }
                    ]
                },
            },
        )
        statuses = [
            HostStatus(
                key="cbusillo@localhost",
                label="M4 Studio",
                mode="ssh",
                priority=100,
                capabilities=["encode_queue", "sample_calibration"],
                available=True,
                message="Mounted and ready",
                missing_paths=[],
                repo_path=str(self.root),
                utc_offset_minutes=-240,
            )
        ]

        with patch("mediaforce.web.app._safe_collect_host_statuses", return_value=statuses):
            with patch("mediaforce.web.app.datetime") as fake_datetime:
                fake_datetime.now.return_value = web_app._parse_iso("2026-05-24T16:30:00+00:00")
                host = web_app._resolve_sample_host(config, "cbusillo@localhost")

        self.assertEqual(host.key, "cbusillo@localhost")

    def test_resolve_sample_host_rejects_non_sample_host(self) -> None:
        statuses = [
            HostStatus(
                key="remote-a",
                label="Remote A",
                mode="ssh",
                priority=80,
                capabilities=["encode_queue"],
                available=True,
                message="Mounted and ready",
                missing_paths=[],
                repo_path=str(self.root),
            )
        ]
        with patch("mediaforce.web.app._safe_collect_host_statuses", return_value=statuses):
            with self.assertRaises(HTTPException) as exc_info:
                web_app._resolve_sample_host(self.config, "remote-a")
        self.assertEqual(exc_info.exception.status_code, 400)
        self.assertEqual(exc_info.exception.detail, "Unknown sampled calibration host")

    @patch("mediaforce.web.app.generate_compare_clips_from_previews")
    @patch("mediaforce.web.app.render_source_review_clips")
    @patch("mediaforce.web.app.encode_preview_clips")
    @patch("mediaforce.web.app.recommend_review_timestamps")
    @patch("mediaforce.web.app.run_sample_encode")
    @patch("mediaforce.web.app.search_quality_for_source")
    def test_run_sampled_calibration_passes_remote_host_to_remote_work(
            self,
            search_quality_mock: Mock,
            sample_encode_mock: Mock,
            recommend_timestamps_mock: Mock,
            encode_preview_mock: Mock,
            source_review_mock: Mock,
            compare_preview_mock: Mock,
    ) -> None:
        source_path = self._create_source_file("episode-remote.mkv")
        preview_path = self.root / "review" / "remote-run" / "item-00" / "encoded-01-12m-00s.mp4"
        source_review_path = self.root / "review" / "remote-run" / "item-00" / "source-01-12m-00s.mp4"
        compare_path = self.root / "review" / "remote-run" / "item-00" / "compare-01-12m-00s.mkv"
        host = {"key": "cbusillo@m1-mini", "label": "M1 mini", "mode": "ssh"}
        policy = {
            "video": {
                "quality_metric": "auto",
                "preset": 6,
                "pixel_format": "yuv420p10le",
                "sample_every": "10m",
                "sample_duration": "30s",
                "encoder": "libsvtav1",
                "default_grain": 0,
                "grain_denoise": 0,
            },
            "audio": {
                "copy_codecs": ["aac"],
                "convert_to_opus_codecs": ["ac3", "dca"],
                "stereo_opus_bitrate": "160k",
                "surround_5_1_opus_bitrate": "320k",
                "surround_7_1_opus_bitrate": "448k",
            },
            "subtitle": {"prefer_text": True},
        }
        sample_item = {
            "source_path": str(source_path),
            "rel_path": "tv/show/episode-remote.mkv",
            "source_size_bytes": 1_000_000,
            "duration_seconds": 3600.0,
            "video_codec": "h264",
            "resolved_policy": policy,
            "audio_summary": [
                {
                    "index": 1,
                    "codec_name": "aac",
                    "channels": 2,
                    "language": "eng",
                    "default": 1,
                    "bit_rate": 192_000,
                }
            ],
            "subtitle_summary": [],
        }

        search_quality_mock.return_value = QualitySearchResult(
            crf=28.0,
            metric="VMAF",
            target=95.0,
            score=95.2,
            stdout="quality ok",
        )
        sample_encode_mock.return_value = SampleEncodeResult(
            metric="VMAF",
            score=95.2,
            predicted_encode_percent=55.0,
            predicted_encode_seconds=120.0,
            predicted_encode_size_bytes=550_000,
            stdout="sample ok",
        )
        recommend_timestamps_mock.return_value = [720.0]
        encode_preview_mock.return_value = [
            EncodedPreviewClip(
                output_path=preview_path,
                timestamp_seconds=720.0,
                duration_seconds=8.0,
                size_bytes=123_456,
            )
        ]
        source_review_mock.return_value = [
            BrowserReviewClip(
                output_path=source_review_path,
                timestamp_seconds=720.0,
                duration_seconds=8.0,
                size_bytes=123_456,
            )
        ]
        compare_preview_mock.return_value = [
            CompareClip(
                output_path=compare_path,
                timestamp_seconds=720.0,
                duration_seconds=8.0,
            )
        ]

        payload, _ = web_app._run_sampled_calibration(
            config=self.config,
            prefix="tv/show",
            action="baseline",
            host_data=host,
            notes="Prefer a smaller file if it still looks clean.",
            policy=policy,
            seed_metadata=None,
            sample_item=sample_item,
            calibration_run_id="remote-run",
            process_controller=web_app.ManagedProcessController(),
        )

        search_quality_mock.assert_called_once_with(
            source_path,
            policy["video"],
            source_codec="h264",
            width=None,
            height=None,
            detected_crop=None,
            process_controller=unittest.mock.ANY,
            host=host,
            quality_temp_dir=self.config.staging_root,
        )
        sample_encode_mock.assert_called_once()
        self.assertEqual(sample_encode_mock.call_args.kwargs["host"], host)
        self.assertEqual(sample_encode_mock.call_args.kwargs["source_codec"], "h264")
        self.assertIsNone(sample_encode_mock.call_args.kwargs["video_filter"])
        self.assertEqual(sample_encode_mock.call_args.kwargs["quality_temp_dir"], self.config.staging_root)
        encode_preview_mock.assert_called_once()
        self.assertEqual(encode_preview_mock.call_args.kwargs["host"], host)
        self.assertEqual(encode_preview_mock.call_args.kwargs["source_codec"], "h264")
        self.assertIsNone(encode_preview_mock.call_args.kwargs["video_filter"])
        self.assertEqual(payload["host"], host)
        self.assertEqual(payload["compare_clips"][0]["path"], "/review-media/remote-run/item-00/compare-01-12m-00s.mkv")

    @patch("mediaforce.web.app.generate_compare_clips_from_previews")
    @patch("mediaforce.web.app.render_source_review_clips")
    @patch("mediaforce.web.app.encode_preview_clips")
    @patch("mediaforce.web.app.recommend_review_timestamps")
    @patch("mediaforce.web.app.run_sample_encode")
    @patch("mediaforce.web.app.search_quality_for_source")
    def test_run_sampled_calibration_uses_configured_host_staging_root_for_quality_temp(
            self,
            search_quality_mock: Mock,
            sample_encode_mock: Mock,
            recommend_timestamps_mock: Mock,
            encode_preview_mock: Mock,
            source_review_mock: Mock,
            compare_preview_mock: Mock,
    ) -> None:
        self.config.raw["remote_hosts"] = [
            {
                "host": "cbusillo@localhost",
                "label": "M4 Studio",
                "capabilities": ["sample_calibration", "encode_queue"],
                "staging_root": str(self.root / "custom-local-staging"),
            }
        ]
        source_path = self._create_source_file("episode-local.mkv")
        host = {"key": "cbusillo@localhost", "label": "M4 Studio", "mode": "ssh"}
        policy = {
            "video": {
                "quality_metric": "auto",
                "preset": 6,
                "pixel_format": "yuv420p10le",
                "sample_every": "10m",
                "sample_duration": "30s",
                "encoder": "libsvtav1",
                "default_grain": 0,
                "grain_denoise": 0,
            },
            "audio": {},
            "subtitle": {"prefer_text": True},
        }
        sample_item = {
            "source_path": str(source_path),
            "rel_path": "tv/show/episode-local.mkv",
            "source_size_bytes": 1_000_000,
            "duration_seconds": 3600.0,
            "video_codec": "h264",
            "resolved_policy": policy,
            "audio_summary": [
                {
                    "index": 1,
                    "codec_name": "aac",
                    "channels": 2,
                    "language": "eng",
                    "default": 1,
                    "bit_rate": 192_000,
                }
            ],
            "subtitle_summary": [],
        }
        search_quality_mock.return_value = QualitySearchResult(
            crf=28.0,
            metric="VMAF",
            target=95.0,
            score=95.2,
            stdout="quality ok",
        )
        sample_encode_mock.return_value = SampleEncodeResult(
            metric="VMAF",
            score=95.2,
            predicted_encode_percent=55.0,
            predicted_encode_seconds=120.0,
            predicted_encode_size_bytes=550_000,
            stdout="sample ok",
        )
        recommend_timestamps_mock.return_value = []
        encode_preview_mock.return_value = []
        source_review_mock.return_value = []
        compare_preview_mock.return_value = []

        web_app._run_sampled_calibration(
            config=self.config,
            prefix="tv/show",
            action="baseline",
            host_data=host,
            notes="Prefer a smaller file if it still looks clean.",
            policy=policy,
            seed_metadata=None,
            sample_item=sample_item,
            calibration_run_id="local-run",
            process_controller=web_app.ManagedProcessController(),
        )

        expected_temp_dir = self.root / "custom-local-staging"
        self.assertEqual(search_quality_mock.call_args.kwargs["quality_temp_dir"], expected_temp_dir)
        self.assertEqual(sample_encode_mock.call_args.kwargs["quality_temp_dir"], expected_temp_dir)

    @patch("mediaforce.web.app.generate_compare_clips_from_previews")
    @patch("mediaforce.web.app.render_source_review_clips")
    @patch("mediaforce.web.app.encode_preview_clips")
    @patch("mediaforce.web.app.recommend_review_timestamps")
    @patch("mediaforce.web.app.run_sample_encode")
    @patch("mediaforce.web.app.search_quality_for_source")
    def test_run_sampled_calibration_uses_local_temp_dir_for_stream_hosts(
            self,
            search_quality_mock: Mock,
            sample_encode_mock: Mock,
            recommend_timestamps_mock: Mock,
            encode_preview_mock: Mock,
            source_review_mock: Mock,
            compare_preview_mock: Mock,
    ) -> None:
        self.config.raw["remote_hosts"] = [
            {
                "host": "cbusillo@stream-box",
                "label": "Stream Box",
                "capabilities": ["sample_calibration", "encode_queue"],
                "media_access": "stream",
                "staging_root": "/srv/media/transcode",
            }
        ]
        source_path = self._create_source_file("episode-stream.mkv")
        host = {"key": "cbusillo@stream-box", "label": "Stream Box", "mode": "ssh", "media_access": "stream"}
        policy = {
            "video": {
                "quality_metric": "auto",
                "preset": 6,
                "pixel_format": "yuv420p10le",
                "sample_every": "10m",
                "sample_duration": "30s",
                "encoder": "libsvtav1",
                "default_grain": 0,
                "grain_denoise": 0,
            },
            "audio": {},
            "subtitle": {"prefer_text": True},
        }
        sample_item = {
            "source_path": str(source_path),
            "rel_path": "tv/show/episode-stream.mkv",
            "source_size_bytes": 1_000_000,
            "duration_seconds": 3600.0,
            "video_codec": "h264",
            "resolved_policy": policy,
            "audio_summary": [
                {
                    "index": 1,
                    "codec_name": "aac",
                    "channels": 2,
                    "language": "eng",
                    "default": 1,
                    "bit_rate": 192_000,
                }
            ],
            "subtitle_summary": [],
        }
        search_quality_mock.return_value = QualitySearchResult(
            crf=28.0,
            metric="VMAF",
            target=95.0,
            score=95.2,
            stdout="quality ok",
        )
        sample_encode_mock.return_value = SampleEncodeResult(
            metric="VMAF",
            score=95.2,
            predicted_encode_percent=55.0,
            predicted_encode_seconds=120.0,
            predicted_encode_size_bytes=550_000,
            stdout="sample ok",
        )
        recommend_timestamps_mock.return_value = []
        encode_preview_mock.return_value = []
        source_review_mock.return_value = []
        compare_preview_mock.return_value = []

        web_app._run_sampled_calibration(
            config=self.config,
            prefix="tv/show",
            action="baseline",
            host_data=host,
            notes="Prefer a smaller file if it still looks clean.",
            policy=policy,
            seed_metadata=None,
            sample_item=sample_item,
            calibration_run_id="stream-run",
            process_controller=web_app.ManagedProcessController(),
        )

        self.assertEqual(search_quality_mock.call_args.kwargs["quality_temp_dir"], self.config.staging_root)
        self.assertEqual(sample_encode_mock.call_args.kwargs["quality_temp_dir"], self.config.staging_root)
        self.assertEqual(search_quality_mock.call_args.kwargs["host"]["mode"], "local")
        self.assertEqual(sample_encode_mock.call_args.kwargs["host"]["mode"], "local")
        self.assertEqual(search_quality_mock.call_args.kwargs["host"]["media_access"], "stream")
        self.assertEqual(sample_encode_mock.call_args.kwargs["host"]["media_access"], "stream")

    def test_purge_transient_artifacts_prunes_ab_av1_dirs_in_host_specific_staging_roots(self) -> None:
        host_staging_root = self.root / "custom-local-staging"
        self.config.raw["remote_hosts"] = [
            {
                "host": "cbusillo@localhost",
                "label": "M4 Studio",
                "capabilities": ["sample_calibration", "encode_queue"],
                "staging_root": str(host_staging_root),
            }
        ]
        temp_dir = host_staging_root / ".ab-av1-test123"
        temp_dir.mkdir(parents=True, exist_ok=True)
        old_time = datetime.now().timestamp() - (16 * 86400)
        os.utime(temp_dir, (old_time, old_time))

        web_app.purge_transient_artifacts(self.config, force=True)

        self.assertFalse(temp_dir.exists())

    def test_cleanup_process_snapshot_probes_remote_hosts_without_explicit_mode(self) -> None:
        self.config.raw["remote_hosts"] = [
            {
                "host": "cbusillo@example-host",
                "label": "Remote",
                "capabilities": ["encode_queue"],
            }
        ]

        with patch(
                "mediaforce.state_cleanup._local_process_commands",
                return_value=[],
        ), patch(
                "mediaforce.state_cleanup.run_remote_command",
                return_value=subprocess.CompletedProcess(
                    args=["ssh"],
                    returncode=0,
                    stdout="remote-proc\n",
                    stderr="",
                ),
        ) as run_remote_mock:
            snapshot = state_cleanup._cleanup_process_snapshot(self.config)

        run_remote_mock.assert_called_once()
        self.assertEqual(snapshot.commands, ("remote-proc",))

    def test_cleanup_process_snapshot_probes_remote_hosts_with_blank_mode(self) -> None:
        self.config.raw["remote_hosts"] = [
            {
                "host": "cbusillo@example-host",
                "label": "Remote",
                "mode": "   ",
                "capabilities": ["encode_queue"],
            }
        ]

        with patch(
                "mediaforce.state_cleanup._local_process_commands",
                return_value=[],
        ), patch(
                "mediaforce.state_cleanup.run_remote_command",
                return_value=subprocess.CompletedProcess(
                    args=["ssh"],
                    returncode=0,
                    stdout="remote-proc\n",
                    stderr="",
                ),
        ) as run_remote_mock:
            snapshot = state_cleanup._cleanup_process_snapshot(self.config)

        run_remote_mock.assert_called_once()
        self.assertEqual(snapshot.commands, ("remote-proc",))

    def test_purge_transient_artifacts_prunes_orphaned_quality_temp_dirs_before_general_retention(self) -> None:
        temp_dir = self.config.paths.web_state_dir / "quality-temp" / ".mediaforce-ab-av1-orphan"
        temp_dir.mkdir(parents=True, exist_ok=True)
        (temp_dir / "sample.mkv").write_text("sample")
        old_time = datetime.now().timestamp() - (2 * 3600)
        os.utime(temp_dir, (old_time, old_time))
        self.config.raw["state"]["cleanup"]["orphan_temp_retention_minutes"] = 60

        with patch(
                "mediaforce.state_cleanup._cleanup_process_snapshot",
                return_value=state_cleanup.CleanupProcessSnapshot((), ()),
        ):
            web_app.purge_transient_artifacts(self.config, force=True)

        self.assertFalse(temp_dir.exists())

    def test_purge_transient_artifacts_keeps_quality_temp_dirs_used_by_active_processes(self) -> None:
        temp_dir = self.config.staging_root / ".mediaforce-ab-av1-active"
        temp_dir.mkdir(parents=True, exist_ok=True)
        (temp_dir / "sample.mkv").write_text("sample")
        old_time = datetime.now().timestamp() - (2 * 3600)
        os.utime(temp_dir, (old_time, old_time))
        self.config.raw["state"]["cleanup"]["orphan_temp_retention_minutes"] = 60
        snapshot = state_cleanup.CleanupProcessSnapshot((f"ab-av1 crf-search --temp-dir {temp_dir}",), ())

        with patch("mediaforce.state_cleanup._cleanup_process_snapshot", return_value=snapshot):
            web_app.purge_transient_artifacts(self.config, force=True)

        self.assertTrue(temp_dir.exists())

    def test_purge_transient_artifacts_keeps_temp_dirs_when_host_processes_are_unverified(self) -> None:
        temp_dir = self.config.staging_root / ".mediaforce-ab-av1-unverified"
        temp_dir.mkdir(parents=True, exist_ok=True)
        (temp_dir / "sample.mkv").write_text("sample")
        old_time = datetime.now().timestamp() - (2 * 3600)
        os.utime(temp_dir, (old_time, old_time))
        self.config.raw["state"]["cleanup"]["orphan_temp_retention_minutes"] = 60
        snapshot = state_cleanup.CleanupProcessSnapshot((), (self.config.staging_root,))

        with patch("mediaforce.state_cleanup._cleanup_process_snapshot", return_value=snapshot):
            web_app.purge_transient_artifacts(self.config, force=True)

        self.assertTrue(temp_dir.exists())

    def test_purge_transient_artifacts_prunes_old_unowned_run_manifests_only(self) -> None:
        old_manifest = self._write_manifest("manifest-old-unowned.json", [{"library_item_id": 1}])
        active_manifest = self._write_manifest("manifest-needs-attention.json", [{"library_item_id": 2}])
        for path in (old_manifest, active_manifest):
            old_time = datetime.now().timestamp() - (16 * 86400)
            os.utime(path, (old_time, old_time))

        with open_db(self.config.paths.db_path) as connection:
            now = web_app._now_iso()
            save_encode_job(
                connection,
                {
                    "job_id": "manifest-needs-attention-job",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "needs_attention",
                    "manifest_path": str(active_manifest),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 0,
                    "process_pid": None,
                    "error": "needs operator decision",
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
                    "created_at": now,
                    "started_at": now,
                    "finished_at": now,
                    "updated_at": now,
                },
            )

        with patch(
                "mediaforce.state_cleanup._cleanup_process_snapshot",
                return_value=state_cleanup.CleanupProcessSnapshot((), ()),
        ):
            web_app.purge_transient_artifacts(self.config, force=True)

        self.assertFalse(old_manifest.exists())
        self.assertTrue(active_manifest.exists())

    def test_purge_transient_artifacts_prunes_legacy_local_quality_temp_root(self) -> None:
        legacy_root = quality.legacy_local_quality_temp_root()
        temp_dir = legacy_root / ".mediaforce-ab-av1-legacy"
        temp_dir.mkdir(parents=True, exist_ok=True)
        old_time = datetime.now().timestamp() - (16 * 86400)
        os.utime(temp_dir, (old_time, old_time))

        try:
            web_app.purge_transient_artifacts(self.config, force=True)
            self.assertFalse(temp_dir.exists())
        finally:
            shutil.rmtree(legacy_root, ignore_errors=True)

    def test_purge_transient_artifacts_prunes_previous_local_quality_temp_root(self) -> None:
        previous_root = quality.previous_local_quality_temp_root()
        temp_dir = previous_root / ".mediaforce-ab-av1-previous"
        temp_dir.mkdir(parents=True, exist_ok=True)
        old_time = datetime.now().timestamp() - (16 * 86400)
        os.utime(temp_dir, (old_time, old_time))

        try:
            web_app.purge_transient_artifacts(self.config, force=True)
            self.assertFalse(temp_dir.exists())
        finally:
            shutil.rmtree(previous_root, ignore_errors=True)

    def test_collect_host_statuses_uses_configured_hosts_only(self) -> None:
        configured_hosts = [
            {"host": "cbusillo@localhost", "label": "Chris-Studio"},
            {"host": "cbusillo@example-host", "label": "Remote"},
        ]
        self.config.raw["remote_hosts"] = configured_hosts
        with patch("mediaforce.remote._remote_host_status", side_effect=lambda config, host: HostStatus(
                key=str(host["host"]),
                label=str(host["label"]),
                mode="ssh",
                priority=0,
                capabilities=["encode_queue"],
                available=True,
                message="Mounted and ready",
                missing_paths=[],
                repo_path=None,
        )):
            statuses = remote.collect_host_statuses(self.config)
        self.assertEqual([status.key for status in statuses], ["cbusillo@localhost", "cbusillo@example-host"])

    def test_collect_host_statuses_checks_multiple_hosts_in_parallel(self) -> None:
        configured_hosts = [
            {"host": "first-host", "label": "First"},
            {"host": "second-host", "label": "Second"},
        ]
        self.config.raw["remote_hosts"] = configured_hosts
        second_started = threading.Event()

        def _status_for(host: dict[str, object]) -> HostStatus:
            return HostStatus(
                key=str(host["host"]),
                label=str(host["label"]),
                mode="ssh",
                priority=0,
                capabilities=["encode_queue"],
                available=True,
                message="Mounted and ready",
                missing_paths=[],
                repo_path=None,
            )

        def _fake_remote_host_status(config: MediaforceConfig, host: dict[str, object]) -> HostStatus:
            _ = config
            if str(host.get("host")) == "first-host":
                self.assertTrue(second_started.wait(timeout=0.5))
                return _status_for(host)
            second_started.set()
            return _status_for(host)

        with patch("mediaforce.remote._remote_host_status", side_effect=_fake_remote_host_status):
            statuses = remote.collect_host_statuses(self.config)

        self.assertEqual([status.key for status in statuses], ["first-host", "second-host"])

    def test_remote_host_status_targets_current_machine_without_ssh_probe(self) -> None:
        source_root = Path(next(iter(self.config.source_root_map.values())))
        source_root.mkdir(parents=True, exist_ok=True)
        self.config.staging_root.mkdir(parents=True, exist_ok=True)
        host: dict[str, object] = {
            "host": "cbusillo@localhost",
            "label": "Chris-Studio",
            "capabilities": ["encode_queue", "sample_calibration"],
            "repo_path": str(self.root),
        }
        with patch("mediaforce.remote._run_remote_status_probe") as status_probe_mock, patch(
                "mediaforce.remote._local_tool_status_snapshot",
                return_value={
                    "xcode_clt": True,
                    "brew": True,
                    "ffmpeg": True,
                    "ffmpeg_videotoolbox": True,
                    "ffmpeg_libvmaf": True,
                    "ffmpeg_xpsnr": True,
                    "ffmpeg_libsvtav1": True,
                    "ab_av1": True,
                },
        ):
            status = remote._remote_host_status(self.config, host)

        status_probe_mock.assert_not_called()
        self.assertTrue(status.available)
        self.assertEqual(status.message, "Mounted and ready")
        self.assertEqual(status.missing_paths, [])

    def test_remote_host_status_requires_ab_av1_for_sample_hosts(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@sample-host",
            "label": "Sample Host",
            "capabilities": ["sample_calibration"],
        }
        stdout = "\n".join(
            [
                f"path|{self.config.staging_root}|1",
                f"path|{self.config.archive_root}|1",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|1",
                "tool|ffmpeg_libvmaf|1",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|0",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertFalse(status.available)
        self.assertIn(remote.AB_AV1_MISSING_ISSUE, status.issues)
        self.assertEqual(status.message, "Install ab-av1 first")

    def test_remote_host_status_uses_host_path_overrides(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@encode-host",
            "label": "Encode Host",
            "capabilities": ["encode_queue"],
            "source_roots": {"tv": "/srv/media/tv"},
            "staging_root": "/srv/media/transcode",
        }
        stdout = "\n".join(
            [
                "path|/srv/media/tv|1",
                "path|/srv/media/transcode|1",
                "tool|xcode_clt|0",
                "tool|brew|0",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|0",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|linux",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ) as run_remote_ssh_mock, patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertTrue(status.available)
        self.assertIn("/srv/media/tv", run_remote_ssh_mock.call_args.kwargs["input_text"])
        self.assertIn("/srv/media/transcode", run_remote_ssh_mock.call_args.kwargs["input_text"])
        script = run_remote_ssh_mock.call_args.kwargs["input_text"]
        self.assertIn("for write_path in /srv/media/transcode", script)
        self.assertNotIn("for write_path in /srv/media/tv /srv/media/transcode", script)
        self.assertIn('mktemp "$path/.mediaforce-write-test.XXXXXX"', script)

    def test_remote_host_status_keeps_missing_mounts_as_missing_paths(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@encode-host",
            "label": "Encode Host",
            "capabilities": ["encode_queue"],
            "source_roots": {"tv": "/srv/media/tv"},
            "staging_root": "/srv/media/transcode",
        }
        stdout = "\n".join(
            [
                "path|/srv/media/tv|0",
                "pathexists|/srv/media/tv|0",
                "pathread|/srv/media/tv|0",
                "pathwrite|/srv/media/tv|0",
                "path|/srv/media/transcode|1",
                "pathexists|/srv/media/transcode|1",
                "pathread|/srv/media/transcode|1",
                "pathwrite|/srv/media/transcode|1",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|1",
                "tool|ffmpeg_libvmaf|1",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|macos",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)

        self.assertFalse(status.available)
        self.assertEqual(status.message, "Missing required paths")
        self.assertEqual(status.missing_paths, ["/srv/media/tv"])
        self.assertEqual(status.issues, [])

    def test_remote_host_status_rejects_unwritable_staging_root(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@encode-host",
            "label": "Encode Host",
            "capabilities": ["encode_queue"],
            "source_roots": {"tv": "/srv/media/tv"},
            "staging_root": "/srv/media/transcode",
        }
        stdout = "\n".join(
            [
                "path|/srv/media/tv|1",
                "pathexists|/srv/media/tv|1",
                "pathread|/srv/media/tv|1",
                "pathwrite|/srv/media/tv|0",
                "path|/srv/media/transcode|1",
                "pathexists|/srv/media/transcode|1",
                "pathread|/srv/media/transcode|1",
                "pathwrite|/srv/media/transcode|0",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|1",
                "tool|ffmpeg_libvmaf|1",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|macos",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)

        self.assertFalse(status.available)
        self.assertEqual(status.message, "Staging root not writable")
        self.assertIn("Staging root is not writable on this host. /srv/media/transcode", status.issues)

    def test_remote_host_status_rejects_unreadable_source_root(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@encode-host",
            "label": "Encode Host",
            "capabilities": ["encode_queue"],
            "source_roots": {"tv": "/srv/media/tv"},
            "staging_root": "/srv/media/transcode",
        }
        stdout = "\n".join(
            [
                "path|/srv/media/tv|1",
                "pathexists|/srv/media/tv|1",
                "pathread|/srv/media/tv|0",
                "pathwrite|/srv/media/tv|0",
                "path|/srv/media/transcode|1",
                "pathexists|/srv/media/transcode|1",
                "pathread|/srv/media/transcode|1",
                "pathwrite|/srv/media/transcode|1",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|1",
                "tool|ffmpeg_libvmaf|1",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|macos",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)

        self.assertFalse(status.available)
        self.assertEqual(status.message, "Source root not readable")
        self.assertIn("Source root is not readable on this host. /srv/media/tv", status.issues)

    def test_remote_host_status_reuses_cached_expensive_tool_probe(self) -> None:
        host_status_runtime._REMOTE_TOOL_CACHE.clear()
        host: dict[str, object] = {
            "host": "cbusillo@encode-host",
            "label": "Encode Host",
            "capabilities": ["encode_queue"],
            "source_roots": {"tv": "/srv/media/tv"},
            "staging_root": "/srv/media/transcode",
        }
        full_stdout = "\n".join(
            [
                "path|/srv/media/tv|1",
                "path|/srv/media/transcode|1",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "toolpath|ffmpeg|/opt/homebrew/bin/ffmpeg",
                "toolmeta|ffmpeg_signature|/opt/homebrew/Cellar/ffmpeg-full/8.1/bin/ffmpeg:123:456",
                "tool|ffmpeg_videotoolbox|1",
                "tool|ffmpeg_libvmaf|1",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|macos",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        cheap_stdout = "\n".join(
            [
                "path|/srv/media/tv|1",
                "path|/srv/media/transcode|1",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "toolpath|ffmpeg|/opt/homebrew/bin/ffmpeg",
                "toolmeta|ffmpeg_signature|/opt/homebrew/Cellar/ffmpeg-full/8.1/bin/ffmpeg:123:456",
                "tool|ab_av1|1",
                "meta|platform|macos",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                side_effect=[
                    subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=full_stdout, stderr=""),
                    subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=cheap_stdout, stderr=""),
                ],
        ) as run_remote_ssh_mock, patch("mediaforce.remote._learn_remote_wake_mac"):
            first = remote._remote_host_status(self.config, host)
            second = remote._remote_host_status(self.config, host)

        self.assertTrue(first.available)
        self.assertTrue(second.available)
        first_script = run_remote_ssh_mock.call_args_list[0].kwargs["input_text"]
        second_script = run_remote_ssh_mock.call_args_list[1].kwargs["input_text"]
        self.assertIn("-filters", first_script)
        self.assertIn("ffmpeg_libvmaf", first_script)
        self.assertNotIn("-filters", second_script)
        self.assertNotIn("ffmpeg_libvmaf", second_script)

    def test_remote_host_status_revalidates_cached_tools_when_ffmpeg_changes(self) -> None:
        host_status_runtime._REMOTE_TOOL_CACHE.clear()
        host: dict[str, object] = {
            "host": "cbusillo@encode-host",
            "label": "Encode Host",
            "capabilities": ["encode_queue"],
            "source_roots": {"tv": "/srv/media/tv"},
            "staging_root": "/srv/media/transcode",
        }
        full_stdout = "\n".join(
            [
                "path|/srv/media/tv|1",
                "path|/srv/media/transcode|1",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "toolpath|ffmpeg|/opt/homebrew/bin/ffmpeg",
                "toolmeta|ffmpeg_signature|/opt/homebrew/Cellar/ffmpeg-full/8.1/bin/ffmpeg:123:456",
                "tool|ffmpeg_videotoolbox|1",
                "tool|ffmpeg_libvmaf|1",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|macos",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        changed_cheap_stdout = "\n".join(
            [
                "path|/srv/media/tv|1",
                "path|/srv/media/transcode|1",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "toolpath|ffmpeg|/opt/homebrew/bin/ffmpeg",
                "toolmeta|ffmpeg_signature|/opt/homebrew/Cellar/ffmpeg-full/8.2/bin/ffmpeg:222:999",
                "tool|ab_av1|1",
                "meta|platform|macos",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        refreshed_full_stdout = "\n".join(
            [
                "path|/srv/media/tv|1",
                "path|/srv/media/transcode|1",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "toolpath|ffmpeg|/opt/homebrew/bin/ffmpeg",
                "toolmeta|ffmpeg_signature|/opt/homebrew/Cellar/ffmpeg-full/8.2/bin/ffmpeg:222:999",
                "tool|ffmpeg_videotoolbox|1",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|0",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|macos",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        try:
            with patch(
                    "mediaforce.remote._run_remote_ssh",
                    side_effect=[
                        subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=full_stdout, stderr=""),
                        subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=changed_cheap_stdout, stderr=""),
                        subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=refreshed_full_stdout, stderr=""),
                    ],
            ) as run_remote_ssh_mock, patch("mediaforce.remote._learn_remote_wake_mac"):
                first = remote._remote_host_status(self.config, host)
                second = remote._remote_host_status(self.config, host)
        finally:
            host_status_runtime._REMOTE_TOOL_CACHE.clear()

        self.assertTrue(first.available)
        self.assertFalse(second.available)
        self.assertIn("ffmpeg is missing both libvmaf and xpsnr", second.issues[0])
        scripts = [call.kwargs["input_text"] for call in run_remote_ssh_mock.call_args_list]
        self.assertIn("-filters", scripts[0])
        self.assertNotIn("-filters", scripts[1])
        self.assertIn("-filters", scripts[2])

    def test_remote_host_status_requires_metric_for_mounted_encode_hosts(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@linux-encode-host",
            "label": "Linux Encode Host",
            "capabilities": ["encode_queue"],
        }
        stdout = "\n".join(
            [
                f"path|{self.config.staging_root}|1",
                f"path|{self.config.archive_root}|1",
                "tool|xcode_clt|0",
                "tool|brew|0",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|0",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|0",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|linux",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertFalse(status.available)
        self.assertIn(remote.SAMPLE_METRIC_MISSING_ISSUE, status.issues)

    def test_remote_host_status_stream_mode_ignores_inherited_library_paths(self) -> None:
        self.config.raw["media"]["source_roots"] = {"tv": "/Volumes/media/tv"}
        host: dict[str, object] = {
            "host": "cbusillo@stream-host",
            "label": "Stream Host",
            "capabilities": ["encode_queue"],
            "media_access": "stream",
        }
        stdout = "\n".join(
            [
                "tool|xcode_clt|0",
                "tool|brew|0",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|0",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|0",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|0",
                "meta|platform|linux",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertTrue(status.available)
        self.assertEqual(status.missing_paths, [])

    def test_remote_host_status_stream_mode_ignores_unwritable_staging_root(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@stream-host",
            "label": "Stream Host",
            "capabilities": ["encode_queue"],
            "media_access": "stream",
            "source_roots": {"tv": "/srv/media/tv"},
            "staging_root": "/srv/media/transcode",
        }
        stdout = "\n".join(
            [
                "path|/srv/media/tv|1",
                "pathexists|/srv/media/tv|1",
                "pathread|/srv/media/tv|1",
                "pathwrite|/srv/media/tv|0",
                "path|/srv/media/transcode|1",
                "pathexists|/srv/media/transcode|1",
                "pathread|/srv/media/transcode|1",
                "pathwrite|/srv/media/transcode|0",
                "tool|xcode_clt|0",
                "tool|brew|0",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|0",
                "tool|ffmpeg_libvmaf|1",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|linux",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)

        self.assertTrue(status.available)
        self.assertEqual(status.issues, [])

    def test_remote_host_status_stream_mode_probes_explicit_source_roots(self) -> None:
        self.config.raw["media"]["source_roots"] = {"tv": "/Volumes/media/tv"}
        host: dict[str, object] = {
            "host": "cbusillo@stream-host",
            "label": "Stream Host",
            "capabilities": ["encode_queue"],
            "media_access": "stream",
            "source_roots": {"tv": "/srv/media/tv"},
        }
        stdout = "\n".join(
            [
                "path|/srv/media/tv|1",
                "pathexists|/srv/media/tv|1",
                "pathread|/srv/media/tv|1",
                "pathwrite|/srv/media/tv|0",
                "tool|xcode_clt|0",
                "tool|brew|0",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|0",
                "tool|ffmpeg_libvmaf|1",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|linux",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ) as run_remote_ssh_mock, patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)

        self.assertTrue(status.available)
        script = run_remote_ssh_mock.call_args.kwargs["input_text"]
        self.assertIn("/srv/media/tv", script)
        self.assertNotIn("/Volumes/media/tv", script)
        self.assertIn("for write_path in ; do", script)

    def test_remote_host_status_rejects_sample_host_unwritable_staging_root(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@sample-host",
            "label": "Sample Host",
            "capabilities": ["sample_calibration"],
            "source_roots": {"tv": "/srv/media/tv"},
            "staging_root": "/srv/media/transcode",
        }
        stdout = "\n".join(
            [
                "path|/srv/media/tv|1",
                "pathexists|/srv/media/tv|1",
                "pathread|/srv/media/tv|1",
                "pathwrite|/srv/media/tv|0",
                "path|/srv/media/transcode|1",
                "pathexists|/srv/media/transcode|1",
                "pathread|/srv/media/transcode|1",
                "pathwrite|/srv/media/transcode|0",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|1",
                "tool|ffmpeg_libvmaf|1",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|macos",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)

        self.assertFalse(status.available)
        self.assertEqual(status.message, "Staging root not writable")
        self.assertIn("Staging root is not writable on this host. /srv/media/transcode", status.issues)

    def test_remote_host_status_requires_ab_av1_for_stream_encode_host_with_remote_roots(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@stream-host",
            "label": "Stream Host",
            "capabilities": ["encode_queue"],
            "media_access": "stream",
            "source_roots": {"tv": "/srv/media/tv"},
        }
        stdout = "\n".join(
            [
                "path|/srv/media/tv|1",
                f"path|{self.config.staging_root}|1",
                "tool|xcode_clt|0",
                "tool|brew|0",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|0",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|0",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|0",
                "meta|platform|linux",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertFalse(status.available)
        self.assertEqual(status.message, "Install ab-av1 first")
        self.assertIn(remote.AB_AV1_MISSING_ISSUE, status.issues)

    def test_remote_host_status_stream_hosts_ignore_missing_staging_root(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@stream-host",
            "label": "Stream Host",
            "capabilities": ["encode_queue"],
            "media_access": "stream",
            "source_roots": {"tv": "/srv/media/tv"},
            "staging_root": "/srv/media/transcode",
        }
        stdout = "\n".join(
            [
                "path|/srv/media/tv|1",
                "tool|xcode_clt|0",
                "tool|brew|0",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|0",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|linux",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ) as run_remote_ssh_mock, patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertTrue(status.available)
        self.assertEqual(status.missing_paths, [])
        self.assertIn("/srv/media/tv", run_remote_ssh_mock.call_args.kwargs["input_text"])
        self.assertNotIn("/srv/media/transcode", run_remote_ssh_mock.call_args.kwargs["input_text"])

    def test_remote_host_status_marks_stream_host_unavailable_when_mapped_root_is_missing(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@stream-host",
            "label": "Stream Host",
            "capabilities": ["encode_queue"],
            "media_access": "stream",
            "source_roots": {"tv": "/srv/media/tv"},
        }
        stdout = "\n".join(
            [
                "path|/srv/media/tv|0",
                "tool|xcode_clt|0",
                "tool|brew|0",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|0",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|linux",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertFalse(status.available)
        self.assertEqual(status.message, "Missing required paths")
        self.assertEqual(status.missing_paths, ["/srv/media/tv"])

    def test_remote_host_status_ignores_inherited_stream_source_roots_without_host_overrides(self) -> None:
        self.config.raw["media"]["source_roots"] = {"tv": "/srv/media/tv"}
        host: dict[str, object] = {
            "host": "cbusillo@stream-host",
            "label": "Stream Host",
            "capabilities": ["encode_queue"],
            "media_access": "stream",
        }
        stdout = "\n".join(
            [
                "tool|xcode_clt|0",
                "tool|brew|0",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|0",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|linux",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ) as run_remote_ssh_mock, patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertTrue(status.available)
        self.assertEqual(status.missing_paths, [])
        self.assertNotIn("/srv/media/tv", run_remote_ssh_mock.call_args.kwargs["input_text"])

    def test_current_machine_status_ignores_inherited_stream_source_roots_without_host_overrides(self) -> None:
        missing_root = self.root / "missing-tv-root"
        self.config.raw["media"]["source_roots"] = {"tv": str(missing_root)}
        host: dict[str, object] = {
            "host": "localhost",
            "label": "Local Stream",
            "capabilities": ["encode_queue"],
            "media_access": "stream",
        }
        with patch(
                "mediaforce.remote._local_tool_status_snapshot",
                return_value={
                    "xcode_clt": True,
                    "brew": True,
                    "ffmpeg": True,
                    "ffmpeg_videotoolbox": True,
                    "ffmpeg_libvmaf": True,
                    "ffmpeg_xpsnr": True,
                    "ffmpeg_libsvtav1": True,
                    "ab_av1": True,
                },
        ):
            status = remote._current_machine_host_status(
                self.config,
                host,
                ssh_host="localhost",
                label="Local Stream",
                repo_path=None,
            )
        self.assertTrue(status.available)
        self.assertEqual(status.missing_paths, [])

    def test_remote_host_status_requires_metric_and_av1_support_for_sample_hosts(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@sample-host",
            "label": "Sample Host",
            "capabilities": ["sample_calibration"],
        }
        stdout = "\n".join(
            [
                f"path|{self.config.staging_root}|1",
                f"path|{self.config.archive_root}|1",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|1",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|0",
                "tool|ffmpeg_libsvtav1|0",
                "tool|ab_av1|1",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertFalse(status.available)
        self.assertIn(remote.SAMPLE_METRIC_MISSING_ISSUE, status.issues)
        self.assertIn(remote.SAMPLE_AV1_ENCODER_MISSING_ISSUE, status.issues)

    def test_remote_host_status_requires_ab_av1_for_encode_hosts(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@encode-host",
            "label": "Encode Host",
            "capabilities": ["encode_queue"],
        }
        stdout = "\n".join(
            [
                f"path|{self.config.staging_root}|1",
                f"path|{self.config.archive_root}|1",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|1",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|0",
                "tool|ffmpeg_libsvtav1|0",
                "tool|ab_av1|0",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertFalse(status.available)
        self.assertEqual(status.message, "Install ab-av1 first")
        self.assertIn(remote.AB_AV1_MISSING_ISSUE, status.issues)

    def test_remote_host_status_requires_videotoolbox_for_encode_hosts(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@encode-host",
            "label": "Encode Host",
            "capabilities": ["encode_queue"],
        }
        stdout = "\n".join(
            [
                f"path|{self.config.staging_root}|1",
                f"path|{self.config.archive_root}|1",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|0",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|0",
                "tool|ffmpeg_libsvtav1|0",
                "tool|ab_av1|0",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertFalse(status.available)
        self.assertIn(remote.VIDEOTOOLBOX_REQUIRED_ISSUE, status.issues)

    def test_remote_host_status_allows_linux_encode_hosts_without_videotoolbox(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@linux-encode-host",
            "label": "Linux Encode Host",
            "capabilities": ["encode_queue"],
        }
        stdout = "\n".join(
            [
                f"path|{self.config.staging_root}|1",
                f"path|{self.config.archive_root}|1",
                "tool|xcode_clt|0",
                "tool|brew|0",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|0",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|linux",
                "time|utc_offset|+0000",
                "repo|exists|0",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertTrue(status.available)
        self.assertEqual(status.platform, "linux")
        self.assertFalse(status.setup_supported)
        self.assertNotIn(remote.VIDEOTOOLBOX_REQUIRED_ISSUE, status.issues)

    def test_remote_host_status_requires_libsvtav1_for_linux_encode_hosts(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@linux-encode-host",
            "label": "Linux Encode Host",
            "capabilities": ["encode_queue"],
        }
        stdout = "\n".join(
            [
                f"path|{self.config.staging_root}|1",
                f"path|{self.config.archive_root}|1",
                "tool|xcode_clt|0",
                "tool|brew|0",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|0",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|0",
                "tool|ffmpeg_libsvtav1|0",
                "tool|ab_av1|1",
                "meta|platform|linux",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertFalse(status.available)
        self.assertFalse(status.setup_supported)
        self.assertEqual(status.message, "Install AV1 encoder support")
        self.assertIn(remote.SVT_AV1_REQUIRED_ISSUE, status.issues)

    def test_remote_host_status_marks_linux_sample_hosts_unsupported(self) -> None:
        host: dict[str, object] = {
            "host": "cbusillo@linux-sample-host",
            "label": "Linux Sample Host",
            "capabilities": ["encode_queue", "sample_calibration"],
            "repo_path": "/srv/mediaforce",
        }
        stdout = "\n".join(
            [
                f"path|{self.config.staging_root}|1",
                f"path|{self.config.archive_root}|1",
                "tool|xcode_clt|0",
                "tool|brew|0",
                "tool|ffmpeg|1",
                "tool|ffmpeg_videotoolbox|0",
                "tool|ffmpeg_libvmaf|1",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|1",
                "meta|platform|linux",
                "time|utc_offset|+0000",
                "repo|exists|0",
            ]
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertFalse(status.available)
        self.assertEqual(status.message, "Linux sample unsupported")
        self.assertIn(remote.LINUX_SAMPLE_CALIBRATION_UNSUPPORTED_ISSUE, status.issues)
        self.assertIn("Repo path is missing: /srv/mediaforce", status.issues)

    def test_finish_remote_host_prepare_installs_ab_av1_for_sample_hosts(self) -> None:
        host = {
            "host": "cbusillo@sample-host",
            "label": "Sample Host",
            "capabilities": ["encode_queue", "sample_calibration"],
        }
        ready_status = HostStatus(
            key="cbusillo@sample-host",
            label="Sample Host",
            mode="ssh",
            priority=0,
            capabilities=["encode_queue", "sample_calibration"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
            repo_path=None,
        )
        with patch(
                "mediaforce.remote._run_remote_ssh",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
        ) as run_remote_ssh_mock, patch("mediaforce.remote._remote_host_status", return_value=ready_status):
            result = remote._finish_remote_host_prepare(self.config, host, prep_steps=[])
        self.assertTrue(result.ok)
        self.assertIn("Installed ffmpeg-full with Homebrew for sampled calibration hosts when required.",
                      result.performed_steps)
        self.assertIn("Installed ab-av1 with Homebrew for encode and sample hosts if it was missing.",
                      result.performed_steps)
        prep_script = run_remote_ssh_mock.call_args.args[3]
        self.assertIn("install ffmpeg-full", prep_script)
        self.assertIn('install ab-av1', prep_script)

    def test_prepare_remote_host_with_password_requires_password_for_initial_ssh_setup(self) -> None:
        host = {"host": "cbusillo@sample-host", "label": "Sample Host"}
        self.config.raw["remote_hosts"] = [host]
        setup_required = HostStatus(
            key="cbusillo@sample-host",
            label="Sample Host",
            mode="ssh",
            priority=0,
            capabilities=["encode_queue"],
            available=False,
            message="SSH access setup required",
            missing_paths=[],
            repo_path=None,
            setup_supported=True,
        )
        with patch("mediaforce.remote._remote_host_status", return_value=setup_required):
            result = remote.prepare_remote_host_with_password(self.config, "Sample Host", password=None)
        self.assertFalse(result.ok)
        self.assertTrue(result.requires_password)
        self.assertIn("SSH key", result.message)

    def test_prepare_remote_host_with_password_runs_key_install_then_rechecks_status(self) -> None:
        host = {"host": "cbusillo@sample-host", "label": "Sample Host"}
        self.config.raw["remote_hosts"] = [host]
        setup_required = HostStatus(
            key="cbusillo@sample-host",
            label="Sample Host",
            mode="ssh",
            priority=0,
            capabilities=["encode_queue"],
            available=False,
            message="SSH access setup required",
            missing_paths=[],
            repo_path=None,
            setup_supported=True,
        )
        ready = HostStatus(
            key="cbusillo@sample-host",
            label="Sample Host",
            mode="ssh",
            priority=0,
            capabilities=["encode_queue"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
            repo_path=None,
        )
        key_install = remote.HostSetupResult(
            ok=True,
            message="Installed this Mac's SSH key on the remote host.",
            performed_steps=["Installed id_ed25519.pub for passwordless SSH access."],
        )
        with patch("mediaforce.remote._remote_host_status", side_effect=[setup_required, ready]) as status_mock, patch(
                "mediaforce.remote._install_local_ssh_key", return_value=key_install) as key_install_mock:
            result = remote.prepare_remote_host_with_password(self.config, "Sample Host", password="secret")
        self.assertTrue(result.ok)
        self.assertIn("mounted and ready", result.message.lower())
        self.assertEqual(result.performed_steps, ["Installed id_ed25519.pub for passwordless SSH access."])
        key_install_mock.assert_called_once_with(host, "secret")
        self.assertEqual(status_mock.call_count, 2)

    def test_prepare_remote_host_with_password_routes_missing_paths_to_finish_prepare(self) -> None:
        host = {"host": "cbusillo@sample-host", "label": "Sample Host"}
        self.config.raw["remote_hosts"] = [host]
        needs_paths = HostStatus(
            key="cbusillo@sample-host",
            label="Sample Host",
            mode="ssh",
            priority=0,
            capabilities=["encode_queue"],
            available=False,
            message="Missing required paths",
            missing_paths=["/srv/media/transcode"],
            repo_path=None,
            setup_supported=True,
        )
        finished = remote.HostSetupResult(ok=True, message="Sample Host is mounted and ready.")
        with patch("mediaforce.remote._remote_host_status", return_value=needs_paths), patch(
                "mediaforce.remote._finish_remote_host_prepare", return_value=finished) as finish_mock:
            result = remote.prepare_remote_host_with_password(self.config, "Sample Host", password=None)
        self.assertTrue(result.ok)
        finish_mock.assert_called_once_with(self.config, host, [])

    def test_request_remote_xcode_install_returns_missing_key_message_without_public_key(self) -> None:
        host = {"host": "cbusillo@sample-host", "label": "Sample Host"}
        with patch("mediaforce.remote._default_public_key_path", return_value=None):
            result = remote._request_remote_xcode_install(host)
        self.assertFalse(result.ok)
        self.assertIn("No local SSH public key", result.message)

    def test_request_remote_xcode_install_waits_when_install_already_requested(self) -> None:
        host = {"host": "cbusillo@sample-host", "label": "Sample Host"}
        public_key = self.root / "keys" / "id_ed25519.pub"
        private_key = self.root / "keys" / "id_ed25519"
        public_key.parent.mkdir(parents=True, exist_ok=True)
        public_key.write_text("ssh-ed25519 AAAATEST user@test")
        private_key.write_text("private")
        pending = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=1,
            stdout="",
            stderr="Install requested",
        )
        waited = remote.HostSetupResult(ok=True,
                                        message="Xcode Command Line Tools finished installing on the remote Mac.")
        with patch("mediaforce.remote._default_public_key_path", return_value=public_key), patch(
                "mediaforce.remote._private_key_path_for_public_key", return_value=private_key), patch(
            "mediaforce.remote._run_remote_ssh", return_value=pending) as run_remote_ssh_mock, patch(
            "mediaforce.remote._wait_for_remote_xcode_install", return_value=waited) as wait_mock:
            result = remote._request_remote_xcode_install(host)
        self.assertTrue(result.ok)
        run_remote_ssh_mock.assert_called_once()
        wait_mock.assert_called_once_with(
            host,
            private_key,
            requested_step="The macOS Command Line Tools installer was already pending.",
        )

    def test_wait_for_remote_xcode_install_polls_until_success(self) -> None:
        host = {"host": "cbusillo@sample-host", "label": "Sample Host"}
        private_key = self.root / "keys" / "id_ed25519"
        private_key.parent.mkdir(parents=True, exist_ok=True)
        private_key.write_text("private")
        pending = subprocess.CompletedProcess(args=["ssh"], returncode=1, stdout="", stderr="pending")
        ready = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="/Library/Developer/CommandLineTools",
                                            stderr="")
        with patch("mediaforce.remote._run_remote_ssh", side_effect=[pending, ready]) as run_remote_ssh_mock, patch(
                "mediaforce.remote.time.monotonic", side_effect=[0, 5, 10]), patch(
            "mediaforce.remote.time.sleep") as sleep_mock:
            result = remote._wait_for_remote_xcode_install(
                host,
                private_key,
                requested_step="Requested installer.",
                wait_seconds=30,
                poll_interval_seconds=1,
            )
        self.assertTrue(result.ok)
        self.assertIn("finished installing", result.message)
        self.assertEqual(run_remote_ssh_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1)

    def test_install_local_ssh_key_returns_missing_key_message_without_public_key(self) -> None:
        host = {"host": "cbusillo@sample-host", "label": "Sample Host"}
        with patch("mediaforce.remote._default_public_key_path", return_value=None):
            result = remote._install_local_ssh_key(host, "secret")
        self.assertFalse(result.ok)
        self.assertIn("No local SSH public key", result.message)

    def test_install_local_ssh_key_succeeds_after_expect_and_verify(self) -> None:
        host = {"host": "cbusillo@sample-host", "label": "Sample Host"}
        public_key = self.root / "keys" / "id_ed25519.pub"
        private_key = self.root / "keys" / "id_ed25519"
        public_key.parent.mkdir(parents=True, exist_ok=True)
        public_key.write_text("ssh-ed25519 AAAATEST user@test")
        private_key.write_text("private")
        expect_ok = subprocess.CompletedProcess(args=["expect"], returncode=0, stdout="", stderr="")
        verify_ok = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr="")
        with patch("mediaforce.remote._ensure_remote_awake_for_ssh") as awake_mock, patch(
                "mediaforce.remote._default_public_key_path", return_value=public_key), patch(
            "mediaforce.remote._private_key_path_for_public_key", return_value=private_key), patch(
            "mediaforce.remote.subprocess.run", return_value=expect_ok) as subprocess_run_mock, patch(
            "mediaforce.remote._run_remote_ssh", return_value=verify_ok) as run_remote_ssh_mock:
            result = remote._install_local_ssh_key(host, "secret")
        self.assertTrue(result.ok)
        self.assertIn("Installed this Mac's SSH key", result.message)
        awake_mock.assert_called_once_with(host)
        subprocess_run_mock.assert_called_once()
        run_remote_ssh_mock.assert_called_once_with(host, "true", identity_file=private_key, timeout=15)

    def test_bootstrap_remote_macos_returns_noop_when_no_bootstrap_issues(self) -> None:
        host = {"host": "cbusillo@sample-host", "label": "Sample Host"}
        result = remote._bootstrap_remote_macos(host, "secret", issues=[])
        self.assertTrue(result.ok)
        self.assertEqual(result.message, "No elevated bootstrap steps were needed.")

    def test_bootstrap_remote_macos_routes_xcode_issue_to_request_helper(self) -> None:
        host = {"host": "cbusillo@sample-host", "label": "Sample Host"}
        expected = remote.HostSetupResult(ok=True,
                                          message="Xcode Command Line Tools are already installed on the remote Mac.")
        with patch("mediaforce.remote._request_remote_xcode_install", return_value=expected) as request_mock:
            result = remote._bootstrap_remote_macos(
                host,
                "secret",
                issues=["Xcode Command Line Tools are not installed on the remote Mac."],
            )
        self.assertTrue(result.ok)
        request_mock.assert_called_once_with(host)

    def test_run_encode_command_remote_prefers_ffmpeg_full_path(self) -> None:
        ffmpeg_cmd = ["/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "-hide_banner", "-i", "/tmp/in.mkv", "/tmp/out.mkv"]
        temp_output = self.root / "staging" / "episode.partial.mkv"
        staging_path = self.root / "staging" / "episode.mkv"
        with patch(
                "mediaforce.execution.run_command",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
        ) as run_command_mock:
            execution._run_encode_command(
                ffmpeg_cmd=ffmpeg_cmd,
                temp_output=temp_output,
                staging_path=staging_path,
                overwrite=False,
                process_controller=None,
                host={"key": "cbusillo@sample-host", "mode": "ssh"},
            )
        ssh_cmd = run_command_mock.call_args.args[0]
        self.assertEqual(ssh_cmd[0], "ssh")
        self.assertIn("StrictHostKeyChecking=accept-new", ssh_cmd)
        self.assertIn("UpdateHostKeys=yes", ssh_cmd)
        self.assertIn("CheckHostIP=no", ssh_cmd)
        self.assertNotIn("-progress", ssh_cmd[1:6])
        self.assertIn("/opt/homebrew/opt/ffmpeg-full/bin", ssh_cmd[-1])

    def test_run_encode_command_remote_uses_tracked_process(self) -> None:
        ffmpeg_cmd = ["/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "-hide_banner", "-i", "/tmp/in.mkv", "/tmp/out.mkv"]
        temp_output = self.root / "staging" / "episode.partial.mkv"
        staging_path = self.root / "staging" / "episode.mkv"
        with patch(
                "mediaforce.execution._run_tracked_process",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
        ) as tracked_process_mock, patch(
            "mediaforce.execution._run_tracked_encode_command",
            side_effect=RuntimeError("_run_tracked_encode_command should not be used for SSH shell dispatch"),
        ):
            execution._run_encode_command(
                ffmpeg_cmd=ffmpeg_cmd,
                temp_output=temp_output,
                staging_path=staging_path,
                overwrite=False,
                process_controller=None,
                host={"key": "cbusillo@sample-host", "mode": "ssh"},
            )
        self.assertEqual(tracked_process_mock.call_count, 1)

    def test_effective_video_preset_bumps_8k_svt_av1_jobs_to_supported_preset(self) -> None:
        preset = execution.effective_video_preset(
            {"encoder": "libsvtav1", "preset": 4},
            width=8000,
            height=4000,
        )
        self.assertEqual(preset, 5)

    def test_effective_video_preset_keeps_non_8k_jobs_unchanged(self) -> None:
        preset = execution.effective_video_preset(
            {"encoder": "libsvtav1", "preset": 4},
            width=3840,
            height=2160,
        )
        self.assertEqual(preset, 4)

    def test_run_encode_command_localhost_ssh_executes_locally(self) -> None:
        ffmpeg_cmd = ["/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "-hide_banner", "-i", "/tmp/in.mkv", "/tmp/out.mkv"]
        temp_output = self.root / "staging" / "episode.partial.mkv"
        staging_path = self.root / "staging" / "episode.mkv"
        with patch(
                "mediaforce.execution.run_command",
                return_value=subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr=""),
        ) as run_command_mock:
            execution._run_encode_command(
                ffmpeg_cmd=ffmpeg_cmd,
                temp_output=temp_output,
                staging_path=staging_path,
                overwrite=False,
                process_controller=None,
                host={"key": "cbusillo@localhost", "mode": "ssh"},
            )
        local_cmd = run_command_mock.call_args.args[0]
        self.assertEqual(local_cmd[0], "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
        self.assertNotEqual(local_cmd[0], "ssh")

    def test_run_encode_command_stream_host_uses_streamed_remote_runner(self) -> None:
        ffmpeg_cmd = ["/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "-hide_banner", "-i", "/tmp/in.mkv", "/tmp/out.mkv"]
        temp_output = self.root / "staging" / "episode.partial.mkv"
        staging_path = self.root / "staging" / "episode.mkv"
        with patch(
                "mediaforce.execution._run_streamed_remote_encode_command",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
        ) as streamed_mock:
            execution._run_encode_command(
                ffmpeg_cmd=ffmpeg_cmd,
                temp_output=temp_output,
                staging_path=staging_path,
                overwrite=False,
                process_controller=None,
                host={"key": "cbusillo@sample-host", "mode": "ssh", "media_access": "stream"},
            )
        streamed_mock.assert_called_once()
        self.assertEqual(streamed_mock.call_args.kwargs["source_path"], Path("/tmp/in.mkv"))

    def test_validate_one_item_accepts_forced_only_english_subtitles(self) -> None:
        source_path = self._create_source_file("episode-validate.mkv")
        staging_path = self._staging_path("episode-validate.mkv")
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("encoded")
        self.config.raw["validation"] = {"require_size_reduction": True}
        subtitle_summary = [{"index": 2, "codec_name": "subrip", "language": "eng", "default": 0, "forced": 1}]
        staged_probe = ProbeSummary(
            duration_seconds=60.0,
            video_codec="av1",
            video_bitrate=900000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=1,
            subtitle_track_count=1,
            english_audio_count=1,
            english_subtitle_count=1,
            default_audio_language="eng",
            default_subtitle_language="eng",
            audio_summary_json="[]",
            subtitle_summary_json=json.dumps(subtitle_summary),
        )

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoded")
            self._insert_staged_artifact(connection, item_id, staging_path)
            item = {
                "library_item_id": item_id,
                "source_size_bytes": 1024,
                "subtitle_summary": subtitle_summary,
            }
            with patch("mediaforce.execution.probe_media", return_value=staged_probe):
                validation = execution.validate_one_item(connection, self.config, item)

            self.assertTrue(validation["passed"])
            self.assertIn(
                {"passed": True, "message": "forced-only subtitle outputs stay flagged forced"},
                validation["checks"],
            )
            stored_status_row = self._library_item_value(connection, item_id, library_items.c.status)
            stored_validation_row = self._staged_artifact_value(connection, item_id, staged_artifacts.c.validation_json)
            assert stored_status_row is not None
            assert stored_validation_row is not None
            stored_status = stored_status_row["status"]
            stored_validation = cast(str, stored_validation_row["validation_json"])
            self.assertEqual(stored_status, "validated")
            self.assertTrue(json.loads(stored_validation)["passed"])

    def test_validate_one_item_rejects_truncated_output_duration(self) -> None:
        source_path = self._create_source_file("episode-truncated.mkv")
        staging_path = self._staging_path("episode-truncated.mkv")
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("encoded")
        self.config.raw["validation"] = {"require_size_reduction": True}
        staged_probe = ProbeSummary(
            duration_seconds=60.0,
            video_codec="av1",
            video_bitrate=900000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=1,
            subtitle_track_count=0,
            english_audio_count=1,
            english_subtitle_count=0,
            default_audio_language="eng",
            default_subtitle_language=None,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
        )

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoded")
            self._insert_staged_artifact(connection, item_id, staging_path)
            item = {
                "library_item_id": item_id,
                "source_size_bytes": 1024,
                "duration_seconds": 120.0,
                "subtitle_summary": [],
            }
            with patch("mediaforce.execution.probe_media", return_value=staged_probe):
                validation = execution.validate_one_item(connection, self.config, item)

            self.assertFalse(validation["passed"])
            self.assertIn(
                {"passed": False, "message": "staged duration closely matches the source"},
                validation["checks"],
            )
            stored_status_row = self._library_item_value(connection, item_id, library_items.c.status)
            assert stored_status_row is not None
            self.assertEqual(stored_status_row["status"], "encoded")

    def test_validate_one_item_repairs_unreadable_container_duration(self) -> None:
        source_path = self._create_source_file("episode-remux-repair.mkv")
        staging_path = self._staging_path("episode-remux-repair.mkv")
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("encoded")
        self.config.raw["validation"] = {"require_size_reduction": True}
        unreadable_staged_probe = ProbeSummary(
            duration_seconds=None,
            video_codec="av1",
            video_bitrate=900000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=1,
            subtitle_track_count=0,
            english_audio_count=1,
            english_subtitle_count=0,
            default_audio_language="eng",
            default_subtitle_language=None,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
        )
        repaired_probe = ProbeSummary(
            duration_seconds=120.0,
            video_codec="av1",
            video_bitrate=900000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=1,
            subtitle_track_count=0,
            english_audio_count=1,
            english_subtitle_count=0,
            default_audio_language="eng",
            default_subtitle_language=None,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
        )
        staging_probe_calls = 0

        def fake_probe(path: Path) -> ProbeSummary:
            nonlocal staging_probe_calls
            if path == source_path:
                return repaired_probe
            if path == staging_path:
                staging_probe_calls += 1
                return unreadable_staged_probe if staging_probe_calls == 1 else repaired_probe
            return repaired_probe

        def fake_remux(_staged_path: Path, repaired_path: Path) -> None:
            repaired_path.write_text("remuxed")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoded")
            self._insert_staged_artifact(connection, item_id, staging_path)
            item = {
                "library_item_id": item_id,
                "source_path": str(source_path),
                "source_size_bytes": 1024,
                "duration_seconds": 120.0,
                "subtitle_summary": [],
            }
            with patch("mediaforce.execution.probe_media", side_effect=fake_probe), patch(
                    "mediaforce.execution.probe_packet_end_seconds", return_value=120.2
            ) as packet_probe_mock, patch(
                    "mediaforce.execution.remux_container_metadata", side_effect=fake_remux
            ) as remux_mock:
                validation = execution.validate_one_item(connection, self.config, item)

            self.assertTrue(validation["passed"])
            self.assertEqual(validation["container_metadata_repair"]["repaired"], True)
            self.assertIn(
                {"passed": True, "message": "staged file duration is readable"},
                validation["checks"],
            )
            self.assertEqual(packet_probe_mock.call_count, 2)
            remux_mock.assert_called_once()
            stored_status_row = self._library_item_value(connection, item_id, library_items.c.status)
            stored_validation_row = self._staged_artifact_value(connection, item_id, staged_artifacts.c.validation_json)
            assert stored_status_row is not None
            assert stored_validation_row is not None
            self.assertEqual(stored_status_row["status"], "validated")
            self.assertTrue(json.loads(cast(str, stored_validation_row["validation_json"]))["passed"])

    def test_validate_one_item_keeps_original_when_remux_candidate_fails_validation(self) -> None:
        source_path = self._create_source_file("episode-remux-candidate-fails.mkv")
        staging_path = self._staging_path("episode-remux-candidate-fails.mkv")
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("encoded")
        candidate_path = staging_path.with_name(f"{staging_path.stem}.remuxing{staging_path.suffix}")
        self.config.raw["validation"] = {"require_size_reduction": True}
        unreadable_staged_probe = ProbeSummary(
            duration_seconds=None,
            video_codec="av1",
            video_bitrate=900000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=1,
            subtitle_track_count=0,
            english_audio_count=1,
            english_subtitle_count=0,
            default_audio_language="eng",
            default_subtitle_language=None,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
        )
        source_probe = ProbeSummary(
            duration_seconds=120.0,
            video_codec="h264",
            video_bitrate=1800000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=1,
            subtitle_track_count=0,
            english_audio_count=1,
            english_subtitle_count=0,
            default_audio_language="eng",
            default_subtitle_language=None,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
        )
        bad_candidate_probe = ProbeSummary(
            duration_seconds=120.0,
            video_codec="av1",
            video_bitrate=900000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=2,
            subtitle_track_count=0,
            english_audio_count=2,
            english_subtitle_count=0,
            default_audio_language="eng",
            default_subtitle_language=None,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
        )

        def fake_probe(path: Path) -> ProbeSummary:
            if path == source_path:
                return source_probe
            if path == candidate_path:
                return bad_candidate_probe
            return unreadable_staged_probe

        def fake_remux(_staged_path: Path, repaired_path: Path) -> None:
            repaired_path.write_text("remuxed")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoded")
            self._insert_staged_artifact(connection, item_id, staging_path)
            item = {
                "library_item_id": item_id,
                "source_path": str(source_path),
                "source_size_bytes": 1024,
                "duration_seconds": 120.0,
                "subtitle_summary": [],
            }
            with patch("mediaforce.execution.probe_media", side_effect=fake_probe), patch(
                    "mediaforce.execution.probe_packet_end_seconds", return_value=120.2
            ), patch("mediaforce.execution.remux_container_metadata", side_effect=fake_remux):
                validation = execution.validate_one_item(connection, self.config, item)

            self.assertFalse(validation["passed"])
            self.assertEqual(validation["container_metadata_repair"]["repaired"], False)
            self.assertEqual(
                validation["container_metadata_repair"]["skipped_reason"],
                "normal validation failed after remux candidate",
            )
            self.assertIn(
                {"passed": False, "message": "exactly one audio track remains"},
                validation["checks"],
            )
            self.assertEqual(staging_path.read_text(), "encoded")
            self.assertFalse(candidate_path.exists())

    def test_validate_one_item_does_not_repair_when_packet_duration_is_short(self) -> None:
        source_path = self._create_source_file("episode-remux-short.mkv")
        staging_path = self._staging_path("episode-remux-short.mkv")
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("encoded")
        self.config.raw["validation"] = {"require_size_reduction": True}
        unreadable_staged_probe = ProbeSummary(
            duration_seconds=None,
            video_codec="av1",
            video_bitrate=900000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=1,
            subtitle_track_count=0,
            english_audio_count=1,
            english_subtitle_count=0,
            default_audio_language="eng",
            default_subtitle_language=None,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
        )
        source_probe = ProbeSummary(
            duration_seconds=120.0,
            video_codec="h264",
            video_bitrate=1800000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=1,
            subtitle_track_count=0,
            english_audio_count=1,
            english_subtitle_count=0,
            default_audio_language="eng",
            default_subtitle_language=None,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
        )

        def fake_probe(path: Path) -> ProbeSummary:
            return source_probe if path == source_path else unreadable_staged_probe

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoded")
            self._insert_staged_artifact(connection, item_id, staging_path)
            item = {
                "library_item_id": item_id,
                "source_path": str(source_path),
                "source_size_bytes": 1024,
                "duration_seconds": 120.0,
                "subtitle_summary": [],
            }
            with patch("mediaforce.execution.probe_media", side_effect=fake_probe), patch(
                    "mediaforce.execution.probe_packet_end_seconds", return_value=60.0
            ), patch("mediaforce.execution.remux_container_metadata") as remux_mock:
                validation = execution.validate_one_item(connection, self.config, item)

            self.assertFalse(validation["passed"])
            self.assertEqual(validation["container_metadata_repair"]["repaired"], False)
            self.assertEqual(
                validation["container_metadata_repair"]["skipped_reason"],
                "packet timestamps did not prove the staged file is complete",
            )
            self.assertIn(
                {"passed": False, "message": "staged file duration is readable"},
                validation["checks"],
            )
            remux_mock.assert_not_called()
            stored_status_row = self._library_item_value(connection, item_id, library_items.c.status)
            assert stored_status_row is not None
            self.assertEqual(stored_status_row["status"], "encoded")

    def test_promote_one_item_moves_source_and_updates_metadata(self) -> None:
        source_path = self._create_source_file("episode-promote.mkv")
        staging_path = self._staging_path("episode-promote.mp4")
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("encoded")
        self.config.raw["media"]["output_container"] = "mp4"
        promoted_probe = ProbeSummary(
            duration_seconds=60.0,
            video_codec="av1",
            video_bitrate=900000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=1,
            subtitle_track_count=0,
            english_audio_count=1,
            english_subtitle_count=0,
            default_audio_language="eng",
            default_subtitle_language=None,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
        )

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="validated")
            connection.execute(
                staged_artifacts.insert().values(
                    library_item_id=item_id,
                    staging_path=str(staging_path),
                    validation_json=json.dumps({"passed": True}),
                    updated_at=web_app._now_iso(),
                )
            )
            item = {
                "library_item_id": item_id,
                "source_path": str(source_path),
                "rel_path": "tv/show/episode-promote.mkv",
                "media_root": "tv",
            }

            with patch("mediaforce.execution.probe_media", return_value=promoted_probe), patch(
                    "mediaforce.execution.file_fingerprint", return_value="promoted-fingerprint"
            ):
                destination_path = execution.promote_one_item(connection, self.config, item, force=False)

            self.assertEqual(destination_path, source_path.with_suffix(".mp4"))
            self.assertTrue(destination_path.exists())
            archived_source = self.config.archive_root / Path("tv/show/episode-promote.mkv")
            self.assertTrue(archived_source.exists())
            self.assertFalse(staging_path.exists())

            library_row = self._library_item_value(
                connection,
                item_id,
                library_items.c.source_path,
                library_items.c.rel_path,
                library_items.c.container,
                library_items.c.status,
                library_items.c.fingerprint,
            )
            staged_row = self._staged_artifact_value(
                connection,
                item_id,
                staged_artifacts.c.promoted_path,
                staged_artifacts.c.archived_source_path,
            )
            self.assertEqual(library_row["source_path"], str(destination_path))
            self.assertEqual(library_row["rel_path"], "tv/show/episode-promote.mp4")
            self.assertEqual(library_row["container"], ".mp4")
            self.assertEqual(library_row["status"], "promoted")
            self.assertEqual(library_row["fingerprint"], "promoted-fingerprint")
            self.assertEqual(staged_row["promoted_path"], str(destination_path))
            self.assertEqual(staged_row["archived_source_path"], str(archived_source))

    def test_run_tracked_process_reports_progress_snapshots(self) -> None:
        class FakeTextProcess:
            def __init__(self) -> None:
                self.stdout = io.StringIO("stdout line\n")
                self.stderr = io.StringIO("out_time_ms=45000000\nprogress=continue\n")

            @staticmethod
            def wait() -> int:
                return 0

        snapshots: list[dict[str, object]] = []
        with patch("mediaforce.execution.subprocess.Popen", return_value=FakeTextProcess()):
            result = execution._run_tracked_process(
                ["ffmpeg", "-i", "input.mkv", "output.mkv"],
                process_controller=None,
                progress_callback=lambda snapshot: snapshots.append(snapshot),
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "stdout line\n")
        self.assertEqual(result.stderr, "out_time_ms=45000000\nprogress=continue\n")
        self.assertEqual(snapshots[-1]["out_time_seconds"], 45.0)
        self.assertEqual(snapshots[-1]["progress_state"], "continue")

    def test_run_streamed_remote_encode_command_streams_source_output_and_progress(self) -> None:
        class FakeBinaryProcess:
            def __init__(self) -> None:
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(b"encoded-output")
                self.stderr = io.BytesIO(b"out_time_ms=45000000\nprogress=continue\n")

            @staticmethod
            def wait() -> int:
                return 0

        source_path = self._create_source_file("episode-stream.mkv")
        temp_output = self.root / "staging" / "tv" / "show" / "episode-stream.partial.mkv"
        temp_output.parent.mkdir(parents=True, exist_ok=True)
        snapshots: list[dict[str, object]] = []

        with patch("mediaforce.execution.subprocess.Popen", return_value=FakeBinaryProcess()), patch(
                "mediaforce.execution.ssh_client_options", return_value=[]
        ):
            result = execution._run_streamed_remote_encode_command(
                ffmpeg_cmd=["/opt/homebrew/bin/ffmpeg", "-y", "-i", str(source_path), "/tmp/output.mkv"],
                temp_output=temp_output,
                source_path=source_path,
                process_controller=None,
                host={"key": "cbusillo@sample-host", "mode": "ssh"},
                progress_callback=lambda snapshot: snapshots.append(snapshot),
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(temp_output.read_bytes(), b"encoded-output")
        self.assertEqual(snapshots[-1]["out_time_seconds"], 45.0)
        self.assertEqual(snapshots[-1]["progress_state"], "continue")

    def test_finalize_output_path_retries_transient_resource_busy(self) -> None:
        temp_output = self.root / "staging" / "episode.partial.mkv"
        staging_path = self.root / "staging" / "episode.mkv"
        temp_output.parent.mkdir(parents=True, exist_ok=True)
        temp_output.write_text("encoded")

        original_replace = Path.replace
        replace_calls = 0

        def flaky_replace(path: Path, target: Path) -> Path:
            nonlocal replace_calls
            if path == temp_output and replace_calls == 0:
                replace_calls += 1
                raise OSError(errno.EBUSY, "Resource busy", str(path))
            replace = cast(Callable[[Path, Path], Path], original_replace)
            return replace(path, target)

        with patch("pathlib.Path.replace", autospec=True, side_effect=flaky_replace), patch(
                "mediaforce.encoding.staging.time.sleep"
        ) as sleep_mock:
            execution._finalize_output_path(temp_output, staging_path)

        self.assertFalse(temp_output.exists())
        self.assertTrue(staging_path.exists())
        sleep_mock.assert_called_once_with(staging_runtime.TRANSIENT_FILE_BUSY_RETRY_DELAY_SECONDS)

    def test_safe_unlink_retries_transient_resource_busy(self) -> None:
        target = self.root / "staging" / "episode.partial.mkv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("partial")

        original_unlink = Path.unlink
        unlink_calls = 0

        def flaky_unlink(path: Path, *, missing_ok: bool = False) -> None:
            nonlocal unlink_calls
            if path == target and unlink_calls == 0:
                unlink_calls += 1
                raise OSError(errno.EBUSY, "Resource busy", str(path))
            original_unlink.__get__(path, Path)(missing_ok=missing_ok)

        with patch("pathlib.Path.unlink", autospec=True, side_effect=flaky_unlink), patch(
                "mediaforce.encoding.staging.time.sleep"
        ) as sleep_mock:
            staging_runtime.safe_unlink(target)

        self.assertFalse(target.exists())
        sleep_mock.assert_called_once_with(staging_runtime.TRANSIENT_FILE_BUSY_RETRY_DELAY_SECONDS)

    def test_encode_one_item_records_staged_artifact_on_success(self) -> None:
        source_path = self._create_source_file("episode-encode.mkv")
        staging_path = self._staging_path("episode-encode.mkv")
        quality_result = QualitySearchResult(crf=28.0, metric="XPSNR", target=41.0, score=41.5, stdout="ok")
        staged_probe = ProbeSummary(
            duration_seconds=60.0,
            video_codec="av1",
            video_bitrate=900000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=1,
            subtitle_track_count=0,
            english_audio_count=1,
            english_subtitle_count=0,
            default_audio_language="eng",
            default_subtitle_language=None,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
        )
        manifest = {"run_id": "run-encode", "items": []}

        def run_encode_side_effect(*, temp_output: Path, **_: object) -> subprocess.CompletedProcess[str]:
            temp_output.parent.mkdir(parents=True, exist_ok=True)
            temp_output.write_text("encoded")
            return subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr="")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path)
            item = {
                "library_item_id": item_id,
                "resolved_policy": {
                    "video": {"preset": 4, "encoder": "libsvtav1"},
                    "audio": {},
                    "subtitle": {},
                },
                "rel_path": "tv/show/episode-encode.mkv",
                "duration_seconds": 1500.0,
                "video_codec": "h264",
                "width": 1920,
                "height": 1080,
                "source_path": str(source_path),
                "source_fingerprint": "source-fingerprint",
                "source_size_bytes": 1024,
            }
            with patch("mediaforce.execution.resolve_item_source_path", return_value=source_path), patch(
                    "mediaforce.execution.resolve_item_staging_path", return_value=staging_path
            ), patch("mediaforce.execution._search_quality", return_value=quality_result), patch(
                "mediaforce.execution._select_streams", return_value={"audio_tracks": [], "subtitle_tracks": []}
            ), patch("mediaforce.execution._build_ffmpeg_command",
                     return_value=["ffmpeg", "-i", str(source_path), str(staging_path)]), patch(
                "mediaforce.execution._run_encode_command", side_effect=run_encode_side_effect
            ), patch("mediaforce.execution.probe_media", return_value=staged_probe), patch(
                "mediaforce.execution.file_fingerprint", return_value="staged-fingerprint"
            ):
                result = execution.encode_one_item(
                    connection,
                    self.config,
                    self.root / "runs" / "manifest-encode.json",
                    manifest,
                    0,
                    item,
                    overwrite=False,
                    host={"key": "remote-a", "label": "Remote A", "mode": "ssh", "media_access": "mounted"},
                    encode_context={"origin": "queue", "encode_job_id": "job-123", "encode_worker_id": "worker-1"},
                )

            self.assertEqual(result.staging_path, staging_path)
            self.assertTrue(staging_path.exists())
            artifact_row = self._staged_artifact_value(
                connection,
                item_id,
                staged_artifacts.c.staging_path,
                staged_artifacts.c.encode_origin,
                staged_artifacts.c.encode_job_id,
                staged_artifacts.c.encode_worker_id,
                staged_artifacts.c.encode_host_key,
                staged_artifacts.c.encode_host_label,
                staged_artifacts.c.encode_host_mode,
                staged_artifacts.c.encode_media_access,
                staged_artifacts.c.source_path,
                staged_artifacts.c.source_rel_path,
                staged_artifacts.c.source_size_bytes,
                staged_artifacts.c.source_duration_seconds,
                staged_artifacts.c.source_video_codec,
                staged_artifacts.c.encode_started_at,
                staged_artifacts.c.encode_completed_at,
                staged_artifacts.c.encode_duration_seconds,
                staged_artifacts.c.bytes_saved,
                staged_artifacts.c.size_ratio,
                staged_artifacts.c.quality_metric,
                staged_artifacts.c.quality_score,
                staged_artifacts.c.staging_fingerprint,
            )
            item_row = self._library_item_value(connection, item_id, library_items.c.status)
            self.assertEqual(artifact_row["staging_path"], str(staging_path))
            self.assertEqual(artifact_row["encode_origin"], "queue")
            self.assertEqual(artifact_row["encode_job_id"], "job-123")
            self.assertEqual(artifact_row["encode_worker_id"], "worker-1")
            self.assertEqual(artifact_row["encode_host_key"], "remote-a")
            self.assertEqual(artifact_row["encode_host_label"], "Remote A")
            self.assertEqual(artifact_row["encode_host_mode"], "ssh")
            self.assertEqual(artifact_row["encode_media_access"], "mounted")
            self.assertEqual(artifact_row["source_path"], str(source_path))
            self.assertEqual(artifact_row["source_rel_path"], "tv/show/episode-encode.mkv")
            self.assertEqual(artifact_row["source_size_bytes"], 1024)
            self.assertEqual(artifact_row["source_duration_seconds"], 1500.0)
            self.assertEqual(artifact_row["source_video_codec"], "h264")
            self.assertIsNotNone(artifact_row["encode_started_at"])
            self.assertIsNotNone(artifact_row["encode_completed_at"])
            self.assertGreaterEqual(float(cast(float | int | str, artifact_row["encode_duration_seconds"])), 0.0)
            self.assertEqual(artifact_row["bytes_saved"], 1017)
            self.assertAlmostEqual(float(cast(float | int | str, artifact_row["size_ratio"])), 7 / 1024, places=6)
            self.assertEqual(artifact_row["quality_metric"], "XPSNR")
            self.assertEqual(artifact_row["quality_score"], 41.5)
            self.assertEqual(artifact_row["staging_fingerprint"], "staged-fingerprint")
            self.assertEqual(item_row["status"], "encoded")
            event_rows = self._item_event_rows(connection, item_id)
            self.assertEqual([row["event_type"] for row in event_rows[-2:]], ["encoding_started", "encoding_completed"])
            started_details = json.loads(cast(str, event_rows[-2]["details_json"]))
            completed_details = json.loads(cast(str, event_rows[-1]["details_json"]))
            self.assertEqual(started_details["encode_origin"], "queue")
            self.assertEqual(started_details["encode_job_id"], "job-123")
            self.assertEqual(started_details["encode_host_label"], "Remote A")
            self.assertEqual(completed_details["bytes_saved"], 1017)
            self.assertEqual(completed_details["staging_size_bytes"], 7)

    def test_encode_one_item_cleans_partial_output_on_failure(self) -> None:
        source_path = self._create_source_file("episode-encode-fail.mkv")
        staging_path = self._staging_path("episode-encode-fail.mkv")
        quality_result = QualitySearchResult(crf=28.0, metric="XPSNR", target=41.0, score=41.5, stdout="ok")
        manifest = {"run_id": "run-encode-fail", "items": []}

        def run_encode_side_effect(*, temp_output: Path, **_: object) -> subprocess.CompletedProcess[str]:
            temp_output.parent.mkdir(parents=True, exist_ok=True)
            temp_output.write_text("partial")
            return subprocess.CompletedProcess(args=["ffmpeg"], returncode=1, stdout="bad stdout", stderr="bad stderr")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path)
            item = {
                "library_item_id": item_id,
                "resolved_policy": {
                    "video": {"preset": 4, "encoder": "libsvtav1"},
                    "audio": {},
                    "subtitle": {},
                },
                "rel_path": "tv/show/episode-encode-fail.mkv",
                "duration_seconds": 1200.0,
                "video_codec": "h264",
                "width": 1920,
                "height": 1080,
                "source_path": str(source_path),
                "source_fingerprint": "source-fingerprint",
                "source_size_bytes": 1024,
            }
            with patch("mediaforce.execution.resolve_item_source_path", return_value=source_path), patch(
                    "mediaforce.execution.resolve_item_staging_path", return_value=staging_path
            ), patch("mediaforce.execution._search_quality", return_value=quality_result), patch(
                "mediaforce.execution._select_streams", return_value={"audio_tracks": [], "subtitle_tracks": []}
            ), patch("mediaforce.execution._build_ffmpeg_command",
                     return_value=["ffmpeg", "-i", str(source_path), str(staging_path)]), patch(
                "mediaforce.execution._run_encode_command", side_effect=run_encode_side_effect
            ):
                with self.assertRaisesRegex(RuntimeError, "bad stdout"):
                    execution.encode_one_item(
                        connection,
                        self.config,
                        self.root / "runs" / "manifest-encode-fail.json",
                        manifest,
                        0,
                        item,
                        overwrite=False,
                        host={"key": "remote-b", "label": "Remote B", "mode": "ssh", "media_access": "stream"},
                        encode_context={"origin": "queue", "encode_job_id": "job-fail", "encode_worker_id": "worker-2"},
                    )

            self.assertFalse(staging_path.exists())
            self.assertFalse(staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}").exists())
            event_rows = self._item_event_rows(connection, item_id)
            self.assertEqual([row["event_type"] for row in event_rows[-2:]], ["encoding_started", "encoding_failed"])
            failed_details = json.loads(cast(str, event_rows[-1]["details_json"]))
            self.assertEqual(failed_details["encode_job_id"], "job-fail")
            self.assertEqual(failed_details["encode_host_key"], "remote-b")
            self.assertIn("bad stdout", failed_details["error"])

    def test_encode_one_item_records_cleanup_warning_in_failure_event(self) -> None:
        source_path = self._create_source_file("episode-cleanup-warning.mkv")
        staging_path = self._staging_path("episode-cleanup-warning.mkv")
        quality_result = QualitySearchResult(crf=28.0, metric="XPSNR", target=41.0, score=41.5, stdout="ok")
        encode_error = quality.QualitySearchError("quality failed")
        quality._attach_quality_cleanup_detail(encode_error, "cleanup failed")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path)
            item = {
                "library_item_id": item_id,
                "resolved_policy": {
                    "video": {"preset": 4, "encoder": "libsvtav1"},
                    "audio": {},
                    "subtitle": {},
                },
                "rel_path": "tv/show/episode-cleanup-warning.mkv",
                "duration_seconds": 1200.0,
                "video_codec": "h264",
                "width": 1920,
                "height": 1080,
                "source_path": str(source_path),
                "source_fingerprint": "source-fingerprint",
                "source_size_bytes": 1024,
            }
            manifest = {"items": [item]}

            with patch(
                    "mediaforce.execution.resolve_item_staging_path", return_value=staging_path
            ), patch("mediaforce.execution._search_quality", return_value=quality_result), patch(
                "mediaforce.execution._select_streams", return_value={"audio_tracks": [], "subtitle_tracks": []}
            ), patch("mediaforce.execution._build_ffmpeg_command",
                     return_value=["ffmpeg", "-i", str(source_path), str(staging_path)]), patch(
                "mediaforce.execution._run_encode_command",
                side_effect=encode_error,
            ):
                with self.assertRaises(quality.QualitySearchError):
                    execution.encode_one_item(
                        connection,
                        self.config,
                        self.root / "runs" / "manifest-cleanup-warning.json",
                        manifest,
                        0,
                        item,
                        overwrite=False,
                        host={"key": "remote-b", "label": "Remote B", "mode": "ssh", "media_access": "stream"},
                        encode_context={"origin": "queue", "encode_job_id": "job-cleanup", "encode_worker_id": "worker-3"},
                    )

            event_rows = self._item_event_rows(connection, item_id)
            failed_details = json.loads(cast(str, event_rows[-1]["details_json"]))
            self.assertIn("quality failed", failed_details["error"])
            self.assertIn("Cleanup warning: cleanup failed", failed_details["error"])

    def test_encode_one_item_uses_remote_quality_search_for_stream_host_with_remote_roots(self) -> None:
        source_path = self._create_source_file("episode-remote-quality.mkv")
        staging_path = self._staging_path("episode-remote-quality.mkv")
        quality_result = QualitySearchResult(crf=28.0, metric="XPSNR", target=41.0, score=41.5, stdout="ok")
        staged_probe = ProbeSummary(
            duration_seconds=60.0,
            video_codec="av1",
            video_bitrate=900000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=1,
            subtitle_track_count=0,
            english_audio_count=1,
            english_subtitle_count=0,
            default_audio_language="eng",
            default_subtitle_language=None,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
        )
        manifest = {"run_id": "run-remote-quality", "items": []}

        def run_encode_side_effect(*, temp_output: Path, **_: object) -> subprocess.CompletedProcess[str]:
            temp_output.parent.mkdir(parents=True, exist_ok=True)
            temp_output.write_text("encoded")
            return subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr="")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path)
            item = {
                "library_item_id": item_id,
                "media_root": "tv",
                "resolved_policy": {
                    "video": {"preset": 4, "encoder": "libsvtav1"},
                    "audio": {},
                    "subtitle": {},
                },
                "rel_path": "tv/show/episode-remote-quality.mkv",
                "duration_seconds": 1200.0,
                "video_codec": "h264",
                "width": 1920,
                "height": 1080,
                "source_path": str(source_path),
                "source_fingerprint": "source-fingerprint",
                "source_size_bytes": 1024,
            }
            host = {
                "key": "remote-q",
                "label": "Remote Q",
                "mode": "ssh",
                "media_access": "stream",
                "staging_root": "/srv/media/transcode",
                "source_roots": {"tv": "/srv/media/tv"},
            }
            with patch("mediaforce.execution.resolve_item_source_path", return_value=source_path), patch(
                    "mediaforce.execution.resolve_item_staging_path", return_value=staging_path
            ), patch("mediaforce.execution._search_quality", return_value=quality_result) as search_mock, patch(
                "mediaforce.execution._select_streams", return_value={"audio_tracks": [], "subtitle_tracks": []}
            ), patch(
                "mediaforce.execution._build_ffmpeg_command",
                return_value=["ffmpeg", "-i", str(source_path), str(staging_path)],
            ), patch(
                "mediaforce.execution._run_encode_command",
                side_effect=run_encode_side_effect,
            ), patch("mediaforce.execution.probe_media", return_value=staged_probe), patch(
                "mediaforce.execution.file_fingerprint", return_value="staged-fingerprint"
            ):
                execution.encode_one_item(
                    connection,
                    self.config,
                    self.root / "runs" / "manifest-remote-quality.json",
                    manifest,
                    0,
                    item,
                    overwrite=False,
                    host=host,
                )

            self.assertEqual(search_mock.call_args.args[0], Path("/srv/media/tv/show/episode-remote-quality.mkv"))
            self.assertEqual(search_mock.call_args.kwargs["host"]["mode"], "ssh")
            self.assertEqual(search_mock.call_args.kwargs["host"]["media_access"], "mounted")
            self.assertEqual(search_mock.call_args.kwargs["quality_temp_dir"], Path("/srv/media/transcode"))

    def test_encode_one_item_uses_local_quality_temp_dir_for_stream_host(self) -> None:
        source_path = self._create_source_file("episode-stream-tempdir.mkv")
        staging_path = self._staging_path("episode-stream-tempdir.mkv")
        quality_result = QualitySearchResult(crf=28.0, metric="XPSNR", target=41.0, score=41.5, stdout="ok")
        staged_probe = ProbeSummary(
            duration_seconds=60.0,
            video_codec="av1",
            video_bitrate=900000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=1,
            subtitle_track_count=0,
            english_audio_count=1,
            english_subtitle_count=0,
            default_audio_language="eng",
            default_subtitle_language=None,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
        )
        manifest = {"run_id": "run-stream-tempdir", "items": []}

        def run_encode_side_effect(*, temp_output: Path, **_: object) -> subprocess.CompletedProcess[str]:
            temp_output.parent.mkdir(parents=True, exist_ok=True)
            temp_output.write_text("encoded")
            return subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr="")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path)
            item = {
                "library_item_id": item_id,
                "media_root": "tv",
                "resolved_policy": {
                    "video": {"preset": 4, "encoder": "libsvtav1"},
                    "audio": {},
                    "subtitle": {},
                },
                "rel_path": "tv/show/episode-stream-tempdir.mkv",
                "duration_seconds": 1200.0,
                "video_codec": "h264",
                "width": 1920,
                "height": 1080,
                "source_path": str(source_path),
                "source_fingerprint": "source-fingerprint",
                "source_size_bytes": 1024,
            }
            host = {
                "key": "remote-stream",
                "label": "Remote Stream",
                "mode": "ssh",
                "media_access": "stream",
                "staging_root": "/srv/media/transcode",
            }
            with patch("mediaforce.execution.resolve_item_source_path", return_value=source_path), patch(
                    "mediaforce.execution.resolve_item_quality_source_path", return_value=source_path
            ), patch(
                "mediaforce.execution.resolve_item_staging_path", return_value=staging_path
            ), patch("mediaforce.execution._search_quality", return_value=quality_result) as search_mock, patch(
                "mediaforce.execution._select_streams", return_value={"audio_tracks": [], "subtitle_tracks": []}
            ), patch(
                "mediaforce.execution._build_ffmpeg_command",
                return_value=["ffmpeg", "-i", str(source_path), str(staging_path)],
            ), patch(
                "mediaforce.execution._run_encode_command",
                side_effect=run_encode_side_effect,
            ), patch("mediaforce.execution.probe_media", return_value=staged_probe), patch(
                "mediaforce.execution.file_fingerprint", return_value="staged-fingerprint"
            ):
                execution.encode_one_item(
                    connection,
                    self.config,
                    self.root / "runs" / "manifest-stream-tempdir.json",
                    manifest,
                    0,
                    item,
                    overwrite=False,
                    host=host,
                )

            self.assertEqual(search_mock.call_args.args[0], source_path)
            self.assertEqual(search_mock.call_args.kwargs["host"]["media_access"], "stream")
            self.assertEqual(search_mock.call_args.kwargs["quality_temp_dir"], self.config.staging_root)

    def test_stream_quality_temp_dir_falls_back_when_staging_root_is_not_writable(self) -> None:
        fallback_root = self.config.paths.web_state_dir / "quality-temp"
        with patch(
                "mediaforce.quality._quality_temp_root_is_writable",
                side_effect=[False, True],
        ):
            resolved = manifest._quality_temp_dir_for_encode_host(
                self.config,
                {"mode": "ssh", "media_access": "stream"},
            )

        self.assertEqual(resolved, fallback_root)

    def test_calibration_stream_quality_temp_dir_falls_back_when_staging_root_is_not_writable(self) -> None:
        fallback_root = self.config.paths.web_state_dir / "quality-temp"
        with patch(
                "mediaforce.quality._quality_temp_root_is_writable",
                side_effect=[False, True],
        ):
            resolved = calibration_runtime._quality_temp_dir_for_host(
                self.config,
                {"mode": "ssh", "media_access": "stream"},
            )

        self.assertEqual(resolved, fallback_root)

    def test_stream_quality_temp_dir_uses_system_temp_when_state_fallback_is_not_writable(self) -> None:
        fallback_root = quality.default_local_quality_temp_root()
        with patch(
                "mediaforce.quality._quality_temp_root_is_writable",
                side_effect=[False, False, True],
        ):
            resolved = manifest._quality_temp_dir_for_encode_host(
                self.config,
                {"mode": "ssh", "media_access": "stream"},
            )

        self.assertEqual(resolved, fallback_root)

    def test_calibration_stream_quality_temp_dir_uses_system_temp_when_state_fallback_is_not_writable(self) -> None:
        fallback_root = quality.default_local_quality_temp_root()
        with patch(
                "mediaforce.quality._quality_temp_root_is_writable",
                side_effect=[False, False, True],
        ):
            resolved = calibration_runtime._quality_temp_dir_for_host(
                self.config,
                {"mode": "ssh", "media_access": "stream"},
            )

        self.assertEqual(resolved, fallback_root)

    def test_run_crf_search_surfaces_temp_dir_setup_failure_without_quality_retry(self) -> None:
        with patch(
            "mediaforce.quality.tempfile.mkdtemp",
            side_effect=OSError(errno.EROFS, "Read-only file system"),
        ):
            with self.assertRaises(quality.QualityTempSetupError) as context:
                quality.run_crf_search(
                    Path("/tmp/input.mkv"),
                    source_codec="hevc",
                    preferred_metric="xpsnr",
                    metric_target=41.0,
                    preset=4,
                    pixel_format="yuv420p10le",
                    sample_every="12m",
                    sample_duration="20s",
                    min_crf=20,
                    max_crf=35,
                    max_encoded_percent=70,
                    svt_params=[],
                    thorough=False,
                    quality_temp_dir=Path("/tmp/mediaforce-transcode"),
                )

        self.assertIn("Failed to prepare local quality temp dir", str(context.exception))

    def test_run_sample_encode_surfaces_temp_dir_setup_failure(self) -> None:
        with patch(
            "mediaforce.quality.tempfile.mkdtemp",
            side_effect=OSError(errno.EROFS, "Read-only file system"),
        ):
            with self.assertRaises(quality.QualityTempSetupError) as context:
                quality.run_sample_encode(
                    Path("/tmp/input.mkv"),
                    source_codec="hevc",
                    preferred_metric="xpsnr",
                    crf=28.0,
                    preset=4,
                    pixel_format="yuv420p10le",
                    sample_every="12m",
                    sample_duration="20s",
                    svt_params=[],
                    quality_temp_dir=Path("/tmp/mediaforce-transcode"),
                )

        self.assertIn("Failed to prepare local quality temp dir", str(context.exception))

    def test_search_quality_does_not_retry_on_temp_setup_failure(self) -> None:
        run_crf_search_mock = Mock(side_effect=quality.QualityTempSetupError("temp root unavailable"))

        with self.assertRaises(quality.QualityTempSetupError):
            quality_search.search_quality(
                Path("/tmp/input.mkv"),
                {
                    "quality_metric": "xpsnr",
                    "target_xpsnr": 41.0,
                    "min_target_xpsnr": 39.0,
                    "target_relax_step_xpsnr": 1.0,
                    "pixel_format": "yuv420p10le",
                    "sample_every": "12m",
                    "sample_duration": "20s",
                    "min_crf": 20,
                    "max_crf": 35,
                    "max_encoded_percent": 70,
                },
                host_media_access_for_host=Mock(return_value="mounted"),
                select_quality_metric=quality.select_quality_metric,
                build_svt_params=Mock(return_value=[]),
                effective_video_preset=Mock(return_value=4),
                run_crf_search=run_crf_search_mock,
            )

        run_crf_search_mock.assert_called_once()

    def test_search_quality_extends_crf_ceiling_when_size_cap_needs_more_range(self) -> None:
        run_crf_search_mock = Mock(
            side_effect=[
                quality.QualitySearchError("Error: Failed to find a suitable crf"),
                QualitySearchResult(crf=48.0, metric="VMAF", target=88.0, score=88.7, stdout="ok"),
            ]
        )

        result = quality_search.search_quality(
            Path("/tmp/input.mkv"),
            {
                "quality_metric": "vmaf",
                "target_vmaf": 88.0,
                "min_target_vmaf": 86.5,
                "target_relax_step_vmaf": 1.0,
                "pixel_format": "yuv420p10le",
                "sample_every": "6m",
                "sample_duration": "30s",
                "min_crf": 18,
                "max_crf": 46,
                "max_encoded_percent": 15,
            },
            host_media_access_for_host=Mock(return_value="stream"),
            select_quality_metric=Mock(return_value=("vmaf", 95.0)),
            build_svt_params=Mock(return_value=[]),
            effective_video_preset=Mock(return_value=4),
            run_crf_search=run_crf_search_mock,
        )

        self.assertEqual(result.crf, 48.0)
        self.assertEqual(
            [call.kwargs["max_crf"] for call in run_crf_search_mock.call_args_list],
            [46, 63],
        )
        self.assertEqual(
            [call.kwargs["metric_target"] for call in run_crf_search_mock.call_args_list],
            [88.0, 88.0],
        )

    def test_search_quality_clamps_configured_crf_ceiling_above_encoder_range(self) -> None:
        run_crf_search_mock = Mock(
            return_value=QualitySearchResult(crf=61.0, metric="VMAF", target=88.0, score=88.1, stdout="ok")
        )

        quality_search.search_quality(
            Path("/tmp/input.mkv"),
            {
                "quality_metric": "vmaf",
                "target_vmaf": 88.0,
                "min_target_vmaf": 86.5,
                "target_relax_step_vmaf": 1.0,
                "pixel_format": "yuv420p10le",
                "sample_every": "6m",
                "sample_duration": "30s",
                "min_crf": 18,
                "max_crf": 70,
                "max_encoded_percent": 15,
            },
            host_media_access_for_host=Mock(return_value="stream"),
            select_quality_metric=Mock(return_value=("vmaf", 95.0)),
            build_svt_params=Mock(return_value=[]),
            effective_video_preset=Mock(return_value=4),
            run_crf_search=run_crf_search_mock,
        )

        run_crf_search_mock.assert_called_once()
        self.assertEqual(run_crf_search_mock.call_args.kwargs["max_crf"], 63)

    def test_run_crf_search_passes_temp_dir_to_ab_av1(self) -> None:
        with patch(
                "mediaforce.quality._run_quality_command",
                return_value=subprocess.CompletedProcess(args=["ab-av1"], returncode=0, stdout="crf 28 xpsnr 41.5",
                                                         stderr=""),
        ) as run_mock:
            quality.run_crf_search(
                Path("/tmp/input.mkv"),
                source_codec="hevc",
                preferred_metric="xpsnr",
                metric_target=41.0,
                preset=4,
                pixel_format="yuv420p10le",
                sample_every="12m",
                sample_duration="20s",
                min_crf=20,
                max_crf=35,
                max_encoded_percent=70,
                svt_params=[],
                thorough=False,
                quality_temp_dir=Path("/tmp/mediaforce-transcode"),
            )
        cmd = run_mock.call_args.args[0]
        self.assertIn("--temp-dir", cmd)
        temp_dir = Path(cmd[cmd.index("--temp-dir") + 1])
        self.assertEqual(temp_dir.parent, Path("/tmp/mediaforce-transcode"))
        self.assertTrue(temp_dir.name.startswith(".mediaforce-ab-av1-"))

    def test_search_quality_passes_video_filter_to_ab_av1(self) -> None:
        run_crf_search_mock = Mock(
            return_value=QualitySearchResult(crf=28.0, metric="XPSNR", target=41.0, score=41.5, stdout="ok")
        )

        quality_search.search_quality(
            Path("/tmp/input.mkv"),
            {
                "quality_metric": "xpsnr",
                "target_xpsnr": 41.0,
                "min_target_xpsnr": 39.0,
                "target_relax_step_xpsnr": 1.0,
                "pixel_format": "yuv420p10le",
                "sample_every": "12m",
                "sample_duration": "20s",
                "min_crf": 20,
                "max_crf": 35,
                "max_encoded_percent": 70,
                "black_bar_handling": "auto",
                "max_height": 720,
            },
            width=1920,
            height=1080,
            detected_crop="1920:800:0:140",
            host_media_access_for_host=Mock(return_value="mounted"),
            select_quality_metric=quality.select_quality_metric,
            build_svt_params=Mock(return_value=[]),
            effective_video_preset=Mock(return_value=4),
            run_crf_search=run_crf_search_mock,
        )

        self.assertEqual(
            run_crf_search_mock.call_args.kwargs["video_filter"],
            "crop=1920:800:0:140,scale=-2:720:flags=lanczos",
        )

    def test_run_sample_encode_passes_temp_dir_to_ab_av1(self) -> None:
        with patch(
                "mediaforce.quality._run_quality_command",
                return_value=subprocess.CompletedProcess(
                    args=["ab-av1"],
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "xpsnr": 41.5,
                            "predicted_encode_percent": 62.0,
                            "predicted_encode_seconds": 120.0,
                            "predicted_encode_size": 123456,
                        }
                    ),
                    stderr="",
                ),
        ) as run_mock:
            quality.run_sample_encode(
                Path("/tmp/input.mkv"),
                source_codec="hevc",
                preferred_metric="xpsnr",
                crf=28.0,
                preset=4,
                pixel_format="yuv420p10le",
                sample_every="12m",
                sample_duration="20s",
                svt_params=[],
                quality_temp_dir=Path("/tmp/mediaforce-transcode"),
            )
        cmd = run_mock.call_args.args[0]
        self.assertIn("--temp-dir", cmd)
        temp_dir = Path(cmd[cmd.index("--temp-dir") + 1])
        self.assertEqual(temp_dir.parent, Path("/tmp/mediaforce-transcode"))
        self.assertTrue(temp_dir.name.startswith(".mediaforce-ab-av1-"))

    def test_run_crf_search_prepares_remote_temp_dir_before_remote_quality_run(self) -> None:
        host = {"mode": "ssh", "host": "cbusillo@stream-host"}
        remote_results = [
            subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="crf 28 xpsnr 41.5", stderr=""),
            subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
        ]
        with patch("mediaforce.quality.run_remote_command", side_effect=remote_results) as remote_command_mock:
            quality.run_crf_search(
                Path("/tmp/input.mkv"),
                source_codec="hevc",
                preferred_metric="xpsnr",
                metric_target=41.0,
                preset=4,
                pixel_format="yuv420p10le",
                sample_every="12m",
                sample_duration="20s",
                min_crf=20,
                max_crf=35,
                max_encoded_percent=70,
                svt_params=[],
                thorough=False,
                host=host,
                quality_temp_dir=Path("/tmp/mediaforce-transcode"),
            )

        self.assertEqual(remote_command_mock.call_count, 3)
        self.assertEqual(remote_command_mock.call_args_list[0].args[2], quality.REMOTE_QUALITY_TIMEOUT_SECONDS)
        mkdir_cmd = remote_command_mock.call_args_list[0].args[1]
        self.assertEqual(mkdir_cmd[:2], ["mkdir", "-p"])
        scoped_temp_dir = mkdir_cmd[2]
        self.assertTrue(scoped_temp_dir.startswith("/tmp/mediaforce-transcode/.mediaforce-ab-av1-"))
        self.assertIn("--temp-dir", remote_command_mock.call_args_list[1].args[1])
        self.assertIn(scoped_temp_dir, remote_command_mock.call_args_list[1].args[1])
        self.assertEqual(remote_command_mock.call_args_list[2].args[2], quality.REMOTE_QUALITY_CLEANUP_TIMEOUT_SECONDS)
        self.assertEqual(remote_command_mock.call_args_list[2].args[1], ["rm", "-rf", scoped_temp_dir])

    def test_run_crf_search_removes_scoped_local_temp_dir_after_completion(self) -> None:
        temp_root = Path(tempfile.mkdtemp(prefix="mediaforce-quality-root-"))
        try:
            def fake_run_quality_command(cmd: list[str], *, process_controller: object, host: object) -> \
            subprocess.CompletedProcess[str]:
                _ = process_controller, host
                scoped_temp_dir = Path(cmd[cmd.index("--temp-dir") + 1])
                (scoped_temp_dir / ".ab-av1-child").mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(args=["ab-av1"], returncode=0, stdout="crf 28 xpsnr 41.5", stderr="")

            with patch("mediaforce.quality._run_quality_command", side_effect=fake_run_quality_command):
                quality.run_crf_search(
                    Path("/tmp/input.mkv"),
                    source_codec="hevc",
                    preferred_metric="xpsnr",
                    metric_target=41.0,
                    preset=4,
                    pixel_format="yuv420p10le",
                    sample_every="12m",
                    sample_duration="20s",
                    min_crf=20,
                    max_crf=35,
                    max_encoded_percent=70,
                    svt_params=[],
                    thorough=False,
                    quality_temp_dir=temp_root,
                )

            self.assertEqual(list(temp_root.iterdir()), [])
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def test_run_crf_search_removes_scoped_remote_temp_dir_after_completion(self) -> None:
        host = {"mode": "ssh", "host": "cbusillo@stream-host"}
        remote_results = [
            subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="crf 28 xpsnr 41.5", stderr=""),
            subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
        ]

        with patch("mediaforce.quality.run_remote_command", side_effect=remote_results) as remote_command_mock:
            quality.run_crf_search(
                Path("/tmp/input.mkv"),
                source_codec="hevc",
                preferred_metric="xpsnr",
                metric_target=41.0,
                preset=4,
                pixel_format="yuv420p10le",
                sample_every="12m",
                sample_duration="20s",
                min_crf=20,
                max_crf=35,
                max_encoded_percent=70,
                svt_params=[],
                thorough=False,
                host=host,
                quality_temp_dir=Path("/tmp/mediaforce-transcode"),
            )

        scoped_temp_dir = remote_command_mock.call_args_list[0].args[1][2]
        self.assertEqual(remote_command_mock.call_args_list[2].args[1], ["rm", "-rf", scoped_temp_dir])

    def test_run_crf_search_surfaces_remote_temp_cleanup_failure(self) -> None:
        host = {"mode": "ssh", "host": "cbusillo@stream-host"}
        remote_results = [
            subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="crf 28 xpsnr 41.5", stderr=""),
            subprocess.CompletedProcess(args=["ssh"], returncode=1, stdout="", stderr="permission denied"),
        ]

        with patch("mediaforce.quality.run_remote_command", side_effect=remote_results):
            with self.assertRaises(quality.QualityTempCleanupError) as context:
                quality.run_crf_search(
                    Path("/tmp/input.mkv"),
                    source_codec="hevc",
                    preferred_metric="xpsnr",
                    metric_target=41.0,
                    preset=4,
                    pixel_format="yuv420p10le",
                    sample_every="12m",
                    sample_duration="20s",
                    min_crf=20,
                    max_crf=35,
                    max_encoded_percent=70,
                    svt_params=[],
                    thorough=False,
                    host=host,
                    quality_temp_dir=Path("/tmp/mediaforce-transcode"),
                )

        self.assertIn("permission denied", str(context.exception))

    def test_run_crf_search_ignores_remote_temp_cleanup_timeout_after_success(self) -> None:
        host = {"mode": "ssh", "host": "cbusillo@stream-host"}
        remote_results = [
            subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="crf 28 xpsnr 41.5", stderr=""),
            subprocess.TimeoutExpired(cmd=["ssh"], timeout=quality.REMOTE_QUALITY_CLEANUP_TIMEOUT_SECONDS),
        ]

        with patch("mediaforce.quality.run_remote_command", side_effect=remote_results):
            result = quality.run_crf_search(
                Path("/tmp/input.mkv"),
                source_codec="hevc",
                preferred_metric="xpsnr",
                metric_target=41.0,
                preset=4,
                pixel_format="yuv420p10le",
                sample_every="12m",
                sample_duration="20s",
                min_crf=20,
                max_crf=35,
                max_encoded_percent=70,
                svt_params=[],
                thorough=False,
                host=host,
                quality_temp_dir=Path("/tmp/mediaforce-transcode"),
            )

        self.assertEqual(result.crf, 28.0)

    def test_run_crf_search_preserves_quality_error_when_cleanup_also_fails(self) -> None:
        host = {"mode": "ssh", "host": "cbusillo@stream-host"}
        scoped_temp_dir = "/tmp/mediaforce-transcode/.mediaforce-ab-av1-test"
        remote_results = [
            subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["ssh"], returncode=1, stdout="", stderr="quality failed"),
            subprocess.CompletedProcess(args=["ssh"], returncode=1, stdout="", stderr="cleanup failed"),
        ]

        with patch("mediaforce.quality.run_remote_command", side_effect=remote_results), patch(
                "mediaforce.quality.uuid.uuid4",
                return_value=type("UUID", (), {"hex": "test"})(),
        ):
            with self.assertRaises(quality.QualitySearchError) as context:
                quality.run_crf_search(
                    Path("/tmp/input.mkv"),
                    source_codec="hevc",
                    preferred_metric="xpsnr",
                    metric_target=41.0,
                    preset=4,
                    pixel_format="yuv420p10le",
                    sample_every="12m",
                    sample_duration="20s",
                    min_crf=20,
                    max_crf=35,
                    max_encoded_percent=70,
                    svt_params=[],
                    thorough=False,
                    host=host,
                    quality_temp_dir=Path("/tmp/mediaforce-transcode"),
                )

        self.assertNotIsInstance(context.exception, quality.QualityTempCleanupError)
        self.assertIn("quality failed", str(context.exception))
        self.assertNotIn("cleanup failed", str(context.exception))
        self.assertEqual(quality.quality_cleanup_error(context.exception), "cleanup failed")
        self.assertIn("Cleanup warning: cleanup failed", quality.quality_error_message(context.exception))

    def test_run_crf_search_surfaces_local_temp_cleanup_failure(self) -> None:
        with patch(
                "mediaforce.quality._run_quality_command",
                return_value=subprocess.CompletedProcess(args=["ab-av1"], returncode=0, stdout="crf 28 xpsnr 41.5",
                                                         stderr=""),
        ), patch("mediaforce.quality.shutil.rmtree", side_effect=OSError("disk busy")):
            with self.assertRaises(quality.QualityTempCleanupError) as context:
                quality.run_crf_search(
                    Path("/tmp/input.mkv"),
                    source_codec="hevc",
                    preferred_metric="xpsnr",
                    metric_target=41.0,
                    preset=4,
                    pixel_format="yuv420p10le",
                    sample_every="12m",
                    sample_duration="20s",
                    min_crf=20,
                    max_crf=35,
                    max_encoded_percent=70,
                    svt_params=[],
                    thorough=False,
                    quality_temp_dir=Path("/tmp/mediaforce-transcode"),
                )

        self.assertIn("Failed to remove local quality temp dir", str(context.exception))

    def test_search_quality_does_not_retry_on_temp_cleanup_failure(self) -> None:
        run_crf_search_mock = Mock(side_effect=quality.QualityTempCleanupError("cleanup failed"))

        with self.assertRaises(quality.QualityTempCleanupError):
            quality_search.search_quality(
                Path("/tmp/input.mkv"),
                {
                    "quality_metric": "xpsnr",
                    "target_xpsnr": 41.0,
                    "min_target_xpsnr": 39.0,
                    "target_relax_step_xpsnr": 1.0,
                    "pixel_format": "yuv420p10le",
                    "sample_every": "12m",
                    "sample_duration": "20s",
                    "min_crf": 20,
                    "max_crf": 35,
                    "max_encoded_percent": 70,
                    "preset": 4,
                },
                host_media_access_for_host=lambda _host: "mounted",
                select_quality_metric=quality.select_quality_metric,
                build_svt_params=lambda _policy: [],
                effective_video_preset=lambda *_args, **_kwargs: 4,
                run_crf_search=run_crf_search_mock,
            )

        self.assertEqual(run_crf_search_mock.call_count, 1)

    def test_encode_manifest_items_reports_aggregate_progress(self) -> None:
        manifest = {
            "items": [
                {"library_item_id": 1, "duration_seconds": 100.0, "rel_path": "tv/show/episode-a.mkv"},
                {"library_item_id": 2, "duration_seconds": 50.0, "rel_path": "tv/show/episode-b.mkv"},
            ]
        }
        results = [
            execution.EncodeResult(
                staging_path=Path("/tmp/a.mkv"),
                source_size_bytes=1000,
                staging_size_bytes=800,
                chosen_crf=28.0,
                quality_metric="XPSNR",
                quality_target=41.0,
                quality_score=41.5,
                encode_command=["ffmpeg"],
            ),
            execution.EncodeResult(
                staging_path=Path("/tmp/b.mkv"),
                source_size_bytes=900,
                staging_size_bytes=700,
                chosen_crf=29.0,
                quality_metric="XPSNR",
                quality_target=41.0,
                quality_score=41.2,
                encode_command=["ffmpeg"],
            ),
        ]
        progress_snapshots: list[dict[str, object]] = []

        def encode_side_effect(*args: object, **kwargs: object) -> execution.EncodeResult:
            index = cast(int, args[4])
            progress_callback = cast(Callable[[dict[str, object]], None] | None, kwargs["progress_callback"])
            if callable(progress_callback):
                snapshot = {0: {"out_time_seconds": 50.0, "speed": 2.0}, 1: {"out_time_seconds": 25.0, "speed": 2.5}}[
                    index]
                progress_callback(snapshot)
            return results[index]

        with patch("mediaforce.execution.encode_one_item", side_effect=encode_side_effect):
            encode_results = execution.encode_manifest_items(
                connection=sqlite3.connect(":memory:"),
                config=self.config,
                manifest_path=self.root / "runs" / "manifest-progress.json",
                manifest=manifest,
                indexes=[0, 1],
                overwrite=False,
                progress_callback=lambda snapshot: progress_snapshots.append(snapshot),
            )

        self.assertEqual(encode_results, results)
        self.assertEqual(progress_snapshots[0]["current_item_number"], 1)
        self.assertEqual(progress_snapshots[0]["completed_item_count"], 0)
        self.assertEqual(progress_snapshots[0]["overall_completed_duration_seconds"], 50.0)
        self.assertAlmostEqual(float(cast(float, progress_snapshots[0]["percent_complete"])), 33.3333, places=3)
        self.assertEqual(progress_snapshots[1]["current_item_number"], 2)
        self.assertEqual(progress_snapshots[1]["completed_item_count"], 1)
        self.assertEqual(progress_snapshots[1]["overall_completed_duration_seconds"], 125.0)
        self.assertAlmostEqual(float(cast(float, progress_snapshots[1]["percent_complete"])), 83.3333, places=3)
        self.assertEqual(progress_snapshots[1]["eta_seconds"], 10.0)

    def test_describe_item_plan_reports_audio_and_subtitle_decisions(self) -> None:
        item = {
            "video_codec": "h264",
            "audio_summary": [{"index": 1, "codec_name": "aac", "channels": 2, "language": "eng", "default": 1}],
            "subtitle_summary": [{"index": 2, "codec_name": "subrip", "language": "eng", "default": 1, "forced": 0}],
            "resolved_policy": {
                "video": {
                    "quality_metric": "xpsnr",
                    "target_xpsnr": 41.0,
                    "min_target_xpsnr": 40.0,
                    "max_encoded_percent": 90,
                    "default_grain": 0,
                },
                "audio": {
                    "copy_codecs": [],
                    "convert_to_opus_codecs": ["aac"],
                    "stereo_opus_bitrate": "128k",
                    "surround_5_1_opus_bitrate": "256k",
                    "surround_7_1_opus_bitrate": "320k",
                },
                "subtitle": {"prefer_text": True},
            },
        }

        plan = execution.describe_item_plan(item)

        self.assertEqual(plan["video"]["quality_metric"], "xpsnr")
        self.assertEqual(plan["audio"]["action"], "convert")
        self.assertEqual(plan["audio"]["output_codec"], "opus")
        self.assertEqual(plan["audio"]["output_bitrate"], "128k")
        self.assertEqual(plan["subtitles"]["kept_track_count"], 1)
        self.assertEqual(plan["subtitles"]["languages"], ["eng"])

    def test_review_helper_defaults_and_formatters(self) -> None:
        self.assertEqual(review._planned_audio_action({"codec_name": "aac"}, {"convert_to_opus_codecs": ["aac"]}),
                         "libopus")
        self.assertEqual(review._planned_audio_action({"codec_name": "ac3"}, {"copy_codecs": ["ac3"]}), "copy")
        self.assertEqual(review._planned_opus_bitrate({"channels": 6}, {"surround_5_1_opus_bitrate": "256k"}), "256k")
        self.assertEqual(review._default_timestamps(100.0, 10.0), [18.0, 45.0, 72.0])
        self.assertEqual(review._slug_seconds(3661.2), "01-01-01")
        self.assertEqual(review._format_crf(28.0), "28")
        self.assertEqual(review._format_crf(28.25), "28.25")

    def test_review_auto_timestamps_prefers_complexity_then_scene_then_default(self) -> None:
        with patch("mediaforce.review._complexity_timestamps", return_value=[30.0, 60.0, 90.0]), patch(
                "mediaforce.review._scene_change_timestamps", return_value=[10.0, 20.0, 30.0]
        ):
            complexity = review._auto_timestamps(Path("/tmp/input.mkv"), 120.0, 10.0)
        self.assertEqual(complexity, [30.0, 60.0, 90.0])

        with patch("mediaforce.review._complexity_timestamps", return_value=[]), patch(
                "mediaforce.review._scene_change_timestamps", return_value=[12.0, 48.0, 84.0]
        ):
            scene = review._auto_timestamps(Path("/tmp/input.mkv"), 120.0, 10.0)
        self.assertEqual(scene, [12.0, 48.0, 84.0])

        with patch("mediaforce.review._complexity_timestamps", return_value=[]), patch(
                "mediaforce.review._scene_change_timestamps", return_value=[]
        ):
            default = review._auto_timestamps(Path("/tmp/input.mkv"), 100.0, 10.0)
        self.assertEqual(default, [18.0, 45.0, 72.0])

    def test_review_auto_timestamps_skips_expensive_scans_for_long_media(self) -> None:
        with patch("mediaforce.review._complexity_timestamps", return_value=[30.0, 60.0, 90.0]) as complexity, patch(
                "mediaforce.review._scene_change_timestamps", return_value=[12.0, 48.0, 84.0]
        ) as scene:
            timestamps = review._auto_timestamps(Path("/tmp/input.mkv"), 3600.0, 10.0)

        self.assertEqual(timestamps, [12.0, 48.0, 84.0])
        complexity.assert_not_called()
        scene.assert_called_once()

    def test_review_auto_timestamps_uses_defaults_for_long_media_without_scenes(self) -> None:
        with patch("mediaforce.review._complexity_timestamps", return_value=[30.0, 60.0, 90.0]) as complexity, patch(
                "mediaforce.review._scene_change_timestamps", return_value=[]
        ) as scene:
            timestamps = review._auto_timestamps(Path("/tmp/input.mkv"), 3600.0, 10.0)

        self.assertEqual(timestamps, [718.0, 1795.0, 2872.0])
        complexity.assert_not_called()
        scene.assert_called_once()

    def test_render_encoded_preview_clip_adds_svt_params_and_formatted_crf(self) -> None:
        with patch("mediaforce.review.ffmpeg_binary", return_value="/tmp/ffmpeg"), patch(
                "mediaforce.review.ffmpeg_hwaccel_input_args", return_value=["-hwaccel", "videotoolbox"]
        ), patch(
            "mediaforce.review.run_command",
            return_value=subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr=""),
        ) as run_mock:
            review._render_encoded_preview_clip(
                source_path=Path("/tmp/input.mkv"),
                source_codec="h264",
                output_path=Path("/tmp/preview.mp4"),
                clip_time=12.0,
                duration_seconds=8.0,
                encoder="libsvtav1",
                pixel_format="yuv420p10le",
                preset=4,
                crf=28.25,
                svt_params=["tune=0", "film-grain=0"],
            )
        cmd = run_mock.call_args.args[0]
        self.assertIn("-hwaccel", cmd)
        self.assertIn("videotoolbox", cmd)
        self.assertIn("-svtav1-params", cmd)
        self.assertIn("tune=0:film-grain=0", cmd)
        self.assertIn("28.25", cmd)

    def test_resolve_item_paths_use_host_overrides(self) -> None:
        item = {
            "source_path": "/Volumes/media/tv/show/episode.mkv",
            "staging_path": "/Volumes/media/transcode/tv/show/episode.mkv",
            "media_root": "tv",
            "rel_path": "tv/show/episode.mkv",
        }
        host = {
            "source_roots": {"tv": "/srv/media/tv"},
            "staging_root": "/srv/media/transcode",
        }
        self.config.raw["media"]["output_container"] = "mkv"

        self.assertEqual(
            execution.resolve_item_source_path(self.config, item, host=host),
            Path("/srv/media") / "tv/show/episode.mkv",
        )
        self.assertEqual(
            execution.resolve_item_staging_path(self.config, item, host=host),
            Path("/srv/media/transcode") / "tv/show/episode.mkv",
        )

    def test_resolve_item_paths_keep_controller_paths_for_stream_hosts(self) -> None:
        item = {
            "source_path": "/Volumes/media/tv/show/episode.mkv",
            "staging_path": "/Volumes/media/transcode/tv/show/episode.mkv",
            "media_root": "tv",
            "rel_path": "tv/show/episode.mkv",
        }
        host = {
            "source_roots": {"tv": "/srv/media/tv"},
            "staging_root": "/srv/media/transcode",
            "media_access": "stream",
        }
        self.assertEqual(
            execution.resolve_item_source_path(self.config, item, host=host),
            Path("/Volumes/media/tv/show/episode.mkv"),
        )
        self.assertEqual(
            execution.resolve_item_staging_path(self.config, item, host=host),
            Path("/Volumes/media/transcode/tv/show/episode.mkv"),
        )

    def test_build_streaming_remote_ffmpeg_command_uses_pipe_endpoints(self) -> None:
        cmd = execution._build_streaming_remote_ffmpeg_command(
            ["/opt/homebrew/bin/ffmpeg", "-y", "-i", "/tmp/input.mkv", "/tmp/output.mkv"],
            source_path=Path("/tmp/input.mkv"),
            output_path=Path("/tmp/output.mkv"),
        )
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertIn("pipe:0", cmd)
        self.assertEqual(cmd[-3:], ["-f", "matroska", "pipe:1"])

    def test_build_streaming_remote_ffmpeg_command_supports_mp4_pipe_output(self) -> None:
        cmd = execution._build_streaming_remote_ffmpeg_command(
            ["/opt/homebrew/bin/ffmpeg", "-y", "-i", "/tmp/input.mkv", "/tmp/output.mp4"],
            source_path=Path("/tmp/input.mkv"),
            output_path=Path("/tmp/output.mp4"),
        )
        self.assertIn("pipe:0", cmd)
        self.assertIn("-movflags", cmd)
        self.assertIn("+frag_keyframe+empty_moov+default_base_moof", cmd)
        self.assertEqual(cmd[-3:], ["-f", "mp4", "pipe:1"])

    def test_search_quality_runs_locally_for_stream_hosts(self) -> None:
        with patch(
                "mediaforce.execution.run_crf_search",
                return_value=QualitySearchResult(crf=28.0, metric="XPSNR", target=41.0, score=41.5, stdout="ok"),
        ) as run_mock:
            execution._search_quality(
                Path("/tmp/input.mkv"),
                {
                    "quality_metric": "xpsnr",
                    "target_xpsnr": 41.0,
                    "min_target_xpsnr": 40.0,
                    "target_relax_step_xpsnr": 1.0,
                    "pixel_format": "yuv420p10le",
                    "sample_every": "7m",
                    "sample_duration": "30",
                    "min_crf": 18,
                    "max_crf": 34,
                    "max_encoded_percent": 90,
                    "preset": 4,
                    "encoder": "libsvtav1",
                    "default_grain": 0,
                    "grain_denoise": 0,
                },
                source_codec="h264",
                host={"mode": "ssh", "platform": "linux", "media_access": "stream"},
            )
        self.assertEqual(run_mock.call_args.kwargs["host"]["mode"], "local")

    def test_resolve_item_quality_source_path_uses_remote_source_root_for_stream_host(self) -> None:
        item = {
            "media_root": "tv",
            "rel_path": "tv/show/episode.mkv",
            "source_path": "/Volumes/media/tv/show/episode.mkv",
        }
        resolved = execution.resolve_item_quality_source_path(
            self.config,
            item,
            host={
                "mode": "ssh",
                "media_access": "stream",
                "source_roots": {"tv": "/srv/media/tv"},
            },
        )
        self.assertEqual(resolved, Path("/srv/media/tv/show/episode.mkv"))

    def test_resolve_item_quality_source_path_falls_back_local_for_stream_host_without_remote_roots(self) -> None:
        self.config.raw["media"]["source_roots"] = {}
        item = {
            "media_root": "tv",
            "rel_path": "tv/show/episode.mkv",
            "source_path": "/Volumes/media/tv/show/episode.mkv",
        }
        resolved = execution.resolve_item_quality_source_path(
            self.config,
            item,
            host={
                "mode": "ssh",
                "media_access": "stream",
            },
        )
        self.assertEqual(resolved, Path("/Volumes/media/tv/show/episode.mkv"))

    def test_resolve_item_quality_source_path_ignores_inherited_source_root_for_stream_host(self) -> None:
        self.config.raw["media"]["source_roots"] = {"tv": "/srv/media/tv"}
        item = {
            "media_root": "tv",
            "rel_path": "tv/show/episode.mkv",
            "source_path": "/Volumes/media/tv/show/episode.mkv",
        }
        resolved = execution.resolve_item_quality_source_path(
            self.config,
            item,
            host={
                "mode": "ssh",
                "media_access": "stream",
            },
        )
        self.assertEqual(resolved, Path("/Volumes/media/tv/show/episode.mkv"))

    def test_run_quality_command_localhost_ssh_executes_locally(self) -> None:
        with patch(
                "mediaforce.quality.run_command",
                return_value=subprocess.CompletedProcess(args=["ab-av1"], returncode=0, stdout="{}", stderr=""),
        ) as run_command_mock, patch("mediaforce.quality.run_remote_command") as run_remote_command_mock:
            quality._run_quality_command(
                ["ab-av1", "sample-encode", "-i", "/tmp/input.mkv"],
                process_controller=None,
                host={"key": "cbusillo@localhost", "mode": "ssh"},
            )
        self.assertEqual(run_command_mock.call_args.args[0][0], "ab-av1")
        self.assertTrue(
            run_command_mock.call_args.kwargs["env"]["PATH"].startswith(
                "/opt/homebrew/opt/ffmpeg-full/bin:/usr/local/opt/ffmpeg-full/bin:"
            )
        )
        run_remote_command_mock.assert_not_called()

    def test_local_quality_environment_prefers_mediaforce_binary_overrides(self) -> None:
        with patch.dict(
                "mediaforce.quality.os.environ",
                {
                    "MEDIAFORCE_FFMPEG": "/tmp/mediaforce-tools/ffmpeg",
                    "MEDIAFORCE_FFPROBE": "/tmp/mediaforce-tools/ffprobe",
                    "PATH": "/usr/bin:/bin",
                },
                clear=True,
        ):
            env = quality._local_quality_environment()

        self.assertTrue(env["PATH"].startswith("/tmp/mediaforce-tools:/opt/homebrew/opt/ffmpeg-full/bin:"))

    def test_local_quality_environment_ignores_bare_override_names(self) -> None:
        with patch.dict(
                "mediaforce.quality.os.environ",
                {
                    "MEDIAFORCE_FFMPEG": "ffmpeg",
                    "MEDIAFORCE_FFPROBE": "ffprobe",
                    "PATH": "/usr/bin:/bin",
                },
                clear=True,
        ):
            env = quality._local_quality_environment()

        self.assertEqual(
            env["PATH"].split(":", 2)[:2],
            ["/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/opt/ffmpeg-full/bin"],
        )
        self.assertNotIn(".", env["PATH"].split(":"))

    def test_local_quality_environment_ignores_relative_override_paths(self) -> None:
        with patch.dict(
                "mediaforce.quality.os.environ",
                {
                    "MEDIAFORCE_FFMPEG": "tools/ffmpeg",
                    "MEDIAFORCE_FFPROBE": "tools/ffprobe",
                    "PATH": "/usr/bin:/bin",
                },
                clear=True,
        ):
            env = quality._local_quality_environment()

        self.assertNotIn("tools", env["PATH"].split(":"))
        self.assertTrue(env["PATH"].startswith("/opt/homebrew/opt/ffmpeg-full/bin:/usr/local/opt/ffmpeg-full/bin:"))

    def test_local_quality_environment_deduplicates_override_dirs(self) -> None:
        with patch.dict(
                "mediaforce.quality.os.environ",
                {
                    "MEDIAFORCE_FFMPEG": "/tmp/mediaforce-tools/ffmpeg",
                    "MEDIAFORCE_FFPROBE": "/tmp/mediaforce-tools/ffprobe",
                },
                clear=True,
        ):
            env = quality._local_quality_environment()

        self.assertEqual(env["PATH"].count("/tmp/mediaforce-tools"), 1)

    def test_build_ffmpeg_command_enables_videotoolbox_decode_for_h264_sources(self) -> None:
        with patch("mediaforce.execution.ffmpeg_binary", return_value="/tmp/ffmpeg"):
            cmd = execution._build_ffmpeg_command(
                source_path=Path("/tmp/input.mkv"),
                staging_path=Path("/tmp/output.mkv"),
                source_codec="h264",
                video_policy={
                    "encoder": "libsvtav1",
                    "pixel_format": "yuv420p10le",
                    "default_grain": 0,
                    "grain_denoise": 0,
                },
                preset=4,
                audio_policy={},
                subtitle_policy={},
                selection={
                    "audio_tracks": [{"index": 1, "codec_name": "aac", "channels": 2}],
                    "subtitle_tracks": [],
                },
                quality=QualitySearchResult(crf=28.0, metric="XPSNR", target=41.0, score=41.5, stdout="ok"),
                host={"platform": "macos", "videotoolbox_available": True},
            )
        self.assertEqual(cmd[:5], ["/tmp/ffmpeg", "-y", "-hwaccel", "videotoolbox", "-i"])
        self.assertIn("mediaforce_encoded_by=mediaforce", cmd)
        self.assertIn("mediaforce_quality_metric=XPSNR", cmd)
        self.assertIn("mediaforce_quality_target=41", cmd)
        self.assertIn("mediaforce_quality_score=41.5", cmd)
        self.assertIn("mediaforce_chosen_crf=28", cmd)

    def test_build_ffmpeg_command_omits_videotoolbox_decode_for_linux_hosts(self) -> None:
        with patch("mediaforce.execution.ffmpeg_binary", return_value="/tmp/ffmpeg"):
            cmd = execution._build_ffmpeg_command(
                source_path=Path("/tmp/input.mkv"),
                staging_path=Path("/tmp/output.mkv"),
                source_codec="h264",
                video_policy={
                    "encoder": "libsvtav1",
                    "pixel_format": "yuv420p10le",
                    "default_grain": 0,
                    "grain_denoise": 0,
                },
                preset=4,
                audio_policy={},
                subtitle_policy={},
                selection={
                    "audio_tracks": [{"index": 1, "codec_name": "aac", "channels": 2}],
                    "subtitle_tracks": [],
                },
                quality=QualitySearchResult(crf=28.0, metric="XPSNR", target=41.0, score=41.5, stdout="ok"),
                host={"platform": "linux", "videotoolbox_available": False},
            )
        self.assertEqual(cmd[:3], ["/tmp/ffmpeg", "-y", "-i"])

    def test_build_ffmpeg_command_applies_crop_then_downsample_filter(self) -> None:
        with patch("mediaforce.execution.ffmpeg_binary", return_value="/tmp/ffmpeg"):
            cmd = execution._build_ffmpeg_command(
                source_path=Path("/tmp/input.mkv"),
                staging_path=Path("/tmp/output.mkv"),
                source_codec="h264",
                video_policy={
                    "encoder": "libsvtav1",
                    "pixel_format": "yuv420p10le",
                    "default_grain": 0,
                    "grain_denoise": 0,
                    "black_bar_handling": "auto",
                    "max_height": 720,
                    "downsample_algorithm": "lanczos",
                },
                preset=4,
                audio_policy={},
                subtitle_policy={},
                selection={
                    "audio_tracks": [{"index": 1, "codec_name": "aac", "channels": 2}],
                    "subtitle_tracks": [],
                },
                quality=QualitySearchResult(crf=28.0, metric="XPSNR", target=41.0, score=41.5, stdout="ok"),
                host={"platform": "macos", "videotoolbox_available": True},
                width=1920,
                height=1080,
                detected_crop="1920:800:0:140",
            )
        self.assertIn("-vf", cmd)
        self.assertEqual(cmd[cmd.index("-vf") + 1], "crop=1920:800:0:140,scale=-2:720:flags=lanczos")

    def test_video_filter_skips_downsample_when_source_is_already_within_max_height(self) -> None:
        self.assertIsNone(video_filters.build_video_filter({"max_height": 1080}, width=1920, height=800))

    def test_video_filter_picks_most_common_meaningful_crop(self) -> None:
        stderr = "\n".join(
            [
                "[Parsed_cropdetect_0] x crop=1920:1080:0:0",
                "[Parsed_cropdetect_0] x crop=1920:800:0:140",
                "[Parsed_cropdetect_0] x crop=1920:800:0:140",
            ]
        )
        self.assertEqual(
            video_filters.most_common_crop(stderr, source_width=1920, source_height=1080),
            "1920:800:0:140",
        )

    def test_detect_video_crop_runs_only_for_smart_black_bar_policy(self) -> None:
        with patch("mediaforce.execution.run_command") as run_mock:
            self.assertIsNone(
                execution.detect_video_crop(
                    Path("/tmp/input.mkv"),
                    {"black_bar_handling": "off"},
                    width=1920,
                    height=1080,
                )
            )
        run_mock.assert_not_called()

    def test_detect_video_crop_parses_ffmpeg_cropdetect_output(self) -> None:
        with patch("mediaforce.execution.ffmpeg_binary", return_value="/tmp/ffmpeg"), patch(
            "mediaforce.execution.run_command",
            return_value=subprocess.CompletedProcess(
                args=["ffmpeg"],
                returncode=0,
                stdout="",
                stderr="\n".join(
                    [
                        "[Parsed_cropdetect_0] crop=1920:1080:0:0",
                        "[Parsed_cropdetect_0] crop=1920:800:0:140",
                        "[Parsed_cropdetect_0] crop=1920:800:0:140",
                    ]
                ),
            ),
        ) as run_mock:
            crop = execution.detect_video_crop(
                Path("/tmp/input.mkv"),
                {"black_bar_handling": "smart", "black_bar_detect_seconds": 8},
                source_codec="h264",
                width=1920,
                height=1080,
                duration_seconds=3600.0,
            )

        self.assertEqual(crop, "1920:800:0:140")
        cmd = run_mock.call_args.args[0]
        self.assertIn("cropdetect=limit=24:round=2:reset=0", cmd)

    def test_run_crf_search_enables_videotoolbox_for_h265_sources(self) -> None:
        with patch(
                "mediaforce.quality._run_quality_command",
                return_value=subprocess.CompletedProcess(args=["ab-av1"], returncode=0, stdout="crf 28 xpsnr 41.5",
                                                         stderr=""),
        ) as run_mock:
            quality.run_crf_search(
                Path("/tmp/input.mkv"),
                source_codec="hevc",
                preferred_metric="xpsnr",
                metric_target=41.0,
                preset=4,
                pixel_format="yuv420p10le",
                sample_every="12m",
                sample_duration="20s",
                min_crf=20,
                max_crf=35,
                max_encoded_percent=70,
                svt_params=[],
                thorough=False,
                host={"platform": "macos", "videotoolbox_available": True},
            )
        cmd = run_mock.call_args.args[0]
        self.assertIn("--enc-input", cmd)
        self.assertIn("hwaccel=videotoolbox", cmd)

    def test_run_crf_search_omits_videotoolbox_for_linux_hosts(self) -> None:
        with patch(
                "mediaforce.quality._run_quality_command",
                return_value=subprocess.CompletedProcess(args=["ab-av1"], returncode=0, stdout="crf 28 xpsnr 41.5",
                                                         stderr=""),
        ) as run_mock:
            quality.run_crf_search(
                Path("/tmp/input.mkv"),
                source_codec="hevc",
                preferred_metric="xpsnr",
                metric_target=41.0,
                preset=4,
                pixel_format="yuv420p10le",
                sample_every="12m",
                sample_duration="20s",
                min_crf=20,
                max_crf=35,
                max_encoded_percent=70,
                svt_params=[],
                thorough=False,
                host={"platform": "linux", "videotoolbox_available": False},
            )
        cmd = run_mock.call_args.args[0]
        self.assertNotIn("--enc-input", cmd)
        self.assertNotIn("hwaccel=videotoolbox", cmd)

    def test_ffmpeg_progress_state_parses_key_metrics(self) -> None:
        progress_state: dict[str, str] = {}
        self.assertIsNone(execution._update_ffmpeg_progress_state(progress_state, "fps=23.4\n", elapsed_seconds=5.0))
        self.assertIsNone(execution._update_ffmpeg_progress_state(progress_state, "speed=2.50x\n", elapsed_seconds=5.0))
        self.assertIsNone(
            execution._update_ffmpeg_progress_state(progress_state, "out_time_ms=45000000\n", elapsed_seconds=5.0))
        snapshot = execution._update_ffmpeg_progress_state(progress_state, "progress=continue\n", elapsed_seconds=5.0)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["fps"], 23.4)
        self.assertEqual(snapshot["speed"], 2.5)
        self.assertEqual(snapshot["out_time_seconds"], 45.0)
        self.assertEqual(snapshot["elapsed_seconds"], 5.0)

    def test_decorate_encode_queue_for_scheduler_adds_queue_eta(self) -> None:
        manifest_path = self.root / "telemetry-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "items": [
                        {"duration_seconds": 60.0, "source_size_bytes": 1000},
                    ]
                }
            )
        )
        encode_queue = {
            "state": {"is_paused": False, "stop_requested": False},
            "running": [
                {
                    "job_id": "running-job",
                    "prefix": "tv/House/Season 2",
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "item_count": 1,
                    "host": {"key": "cbusillo@studio", "schedule_profile": "always"},
                    "attempt_count": 1,
                    "progress": {
                        "remaining_duration_seconds": 120.0,
                        "speed": 2.0,
                        "percent_complete": 40.0,
                    },
                }
            ],
            "queued": [
                {
                    "job_id": "queued-job",
                    "prefix": "tv/House/Season 3",
                    "status": "queued",
                    "manifest_path": str(manifest_path),
                    "item_count": 1,
                    "host": {},
                    "attempt_count": 0,
                    "progress": None,
                }
            ],
            "recent": [],
            "queued_count": 1,
            "running_count": 1,
            "retry_backoff_count": 0,
            "needs_attention_count": 0,
        }
        decorated = web_app._decorate_encode_queue_for_scheduler(self.config, encode_queue)
        telemetry = decorated.get("telemetry")
        assert isinstance(telemetry, Mapping)
        eta_seconds = telemetry.get("eta_seconds")
        assert eta_seconds is None or isinstance(eta_seconds, (int, float))
        eta_copy = telemetry.get("eta_copy")
        assert eta_copy is None or isinstance(eta_copy, str)
        self.assertAlmostEqual(0.0 if eta_seconds is None else float(eta_seconds), 90.0)
        self.assertEqual(eta_copy, "1m 30s")
        self.assertEqual(decorated["running"][0]["telemetry_summary"], "40% · 2.00x · Est. ETA 1m 0s")

    def test_decorate_encode_queue_for_scheduler_labels_quality_search_phase(self) -> None:
        manifest_path = self.root / "quality-search-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "items": [
                        {"duration_seconds": 60.0, "source_size_bytes": 1000},
                    ]
                }
            )
        )
        encode_queue = {
            "state": {"is_paused": False, "stop_requested": False},
            "running": [
                {
                    "job_id": "quality-search-job",
                    "prefix": "tv/House/Season 2",
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "item_count": 1,
                    "host": {"key": "cbusillo@studio", "schedule_profile": "always"},
                    "attempt_count": 1,
                    "progress": {
                        "progress_state": "quality_search",
                        "phase_label": "Searching quality",
                        "percent_complete": 0.0,
                    },
                }
            ],
            "queued": [],
            "recent": [],
            "queued_count": 0,
            "running_count": 1,
            "retry_backoff_count": 0,
            "needs_attention_count": 0,
        }

        decorated = web_app._decorate_encode_queue_for_scheduler(self.config, encode_queue)

        self.assertEqual(decorated["running"][0]["telemetry_summary"], "Searching quality")

    def test_decorate_encode_queue_for_scheduler_uses_projected_eta_speed(self) -> None:
        manifest_path = self.root / "queue-eta-speed-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "items": [
                        {"duration_seconds": 60.0, "source_size_bytes": 1000},
                    ]
                }
            )
        )
        encode_queue = {
            "state": {"is_paused": False, "stop_requested": False},
            "running": [
                {
                    "job_id": "running-job",
                    "prefix": "tv/House/Season 2",
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "item_count": 1,
                    "host": {"key": "cbusillo@studio", "schedule_profile": "always"},
                    "attempt_count": 1,
                    "progress": {
                        "remaining_duration_seconds": 120.0,
                        "speed": 2.0,
                        "eta_speed": 4.0,
                        "percent_complete": 40.0,
                    },
                }
            ],
            "queued": [
                {
                    "job_id": "queued-job",
                    "prefix": "tv/House/Season 3",
                    "status": "queued",
                    "manifest_path": str(manifest_path),
                    "item_count": 1,
                    "host": {},
                    "attempt_count": 0,
                    "progress": None,
                }
            ],
            "recent": [],
            "queued_count": 1,
            "running_count": 1,
            "retry_backoff_count": 0,
            "needs_attention_count": 0,
        }

        decorated = web_app._decorate_encode_queue_for_scheduler(self.config, encode_queue)

        telemetry = decorated.get("telemetry")
        assert isinstance(telemetry, Mapping)
        eta_seconds = telemetry.get("eta_seconds")
        assert eta_seconds is None or isinstance(eta_seconds, (int, float))
        eta_copy = telemetry.get("eta_copy")
        assert eta_copy is None or isinstance(eta_copy, str)
        self.assertAlmostEqual(0.0 if eta_seconds is None else float(eta_seconds), 45.0)
        self.assertEqual(eta_copy, "45s")

    def test_decorate_encode_queue_for_scheduler_attempt_summary_respects_started_at(self) -> None:
        manifest_path = self.root / "started-at-manifest.json"
        manifest_path.write_text(json.dumps({"items": [{"duration_seconds": 60.0, "source_size_bytes": 1000}]}))
        encode_queue = {
            "state": {"is_paused": False, "stop_requested": False},
            "running": [],
            "queued": [],
            "recent": [
                {
                    "job_id": "started-job",
                    "prefix": "tv/House/Season 2",
                    "status": "needs_attention",
                    "manifest_path": str(manifest_path),
                    "item_count": 1,
                    "host": {},
                    "attempt_count": 0,
                    "started_at": "2026-04-03T21:14:17+00:00",
                    "progress": {
                        "progress_state": "needs_attention",
                        "overall_completed_duration_seconds": 12.0,
                    },
                }
            ],
            "queued_count": 0,
            "running_count": 0,
            "retry_backoff_count": 0,
            "needs_attention_count": 1,
        }

        decorated = web_app._decorate_encode_queue_for_scheduler(self.config, encode_queue)

        self.assertEqual(decorated["recent"][0]["attempt_summary"], "attempt 1 of 3")

    def test_decorate_encode_queue_for_scheduler_attempt_summary_clamps_to_max(self) -> None:
        manifest_path = self.root / "clamped-attempt-manifest.json"
        manifest_path.write_text(json.dumps({"items": [{"duration_seconds": 60.0, "source_size_bytes": 1000}]}))
        encode_queue = {
            "state": {"is_paused": False, "stop_requested": False},
            "running": [],
            "queued": [],
            "recent": [
                {
                    "job_id": "retrying-host-job",
                    "prefix": "tv/House/Season 2",
                    "status": "retry_backoff",
                    "manifest_path": str(manifest_path),
                    "item_count": 1,
                    "host": {},
                    "attempt_count": 7,
                    "started_at": "2026-04-03T21:14:17+00:00",
                    "progress": {
                        "progress_state": "retry_backoff",
                        "overall_completed_duration_seconds": 12.0,
                    },
                }
            ],
            "queued_count": 0,
            "running_count": 0,
            "retry_backoff_count": 1,
            "needs_attention_count": 0,
        }

        decorated = web_app._decorate_encode_queue_for_scheduler(self.config, encode_queue)

        self.assertEqual(decorated["recent"][0]["attempt_summary"], "attempt 3 of 3")

    def test_encode_queue_summary_copy_labels_eta_as_estimated(self) -> None:
        encode_queue = {
            "running_count": 1,
            "queued_count": 2,
            "queued_waiting_count": 1,
            "needs_attention_count": 0,
            "telemetry": {"eta_copy": "12m 0s"},
        }
        encode_job = {"status": "queued", "queue_position": 2, "queue_depth": 3}

        summary = web_app._encode_queue_summary_copy(
            encode_queue,
            {"is_paused": False},
            encode_job,
        )

        self.assertIn("estimated queue finish in 12m 0s", summary)

    def test_host_runtime_rows_include_running_job_telemetry(self) -> None:
        status = HostStatus(
            key="cbusillo@studio",
            label="Studio",
            mode="ssh",
            priority=100,
            capabilities=["encode_queue"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
            repo_path=str(self.root),
            platform="macos",
            videotoolbox_available=True,
        )
        with open_db(self.config.paths.db_path) as connection, patch(
                "mediaforce.web.app._safe_collect_host_statuses", return_value=[status]
        ):
            self._save_job(
                connection,
                job_id="running-job",
                manifest_name="manifest-running.json",
                host={"key": "cbusillo@studio", "label": "Studio", "mode": "ssh", "schedule_profile": "always"},
                status="running",
                attempt_count=1,
            )
            job = load_encode_job(connection, "running-job")
            assert job is not None
            job["progress"] = {
                "remaining_duration_seconds": 180.0,
                "speed": 3.0,
                "percent_complete": 55.0,
                "eta_seconds": 60.0,
            }
            save_encode_job(connection, job)
            rows = web_app._host_runtime_rows(connection, self.config)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["running_jobs"][0]["telemetry_summary"], "55% · 3.00x · Est. ETA 1m 0s")
        self.assertEqual(rows[0]["telemetry"]["eta_copy"], "1m 0s")

    def test_host_runtime_rows_preserve_zero_progress_values(self) -> None:
        status = HostStatus(
            key="cbusillo@studio",
            label="Studio",
            mode="ssh",
            priority=100,
            capabilities=["encode_queue"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
            repo_path=str(self.root),
            platform="macos",
            videotoolbox_available=True,
        )
        with open_db(self.config.paths.db_path) as connection, patch(
                "mediaforce.web.app._safe_collect_host_statuses", return_value=[status]
        ):
            self._save_job(
                connection,
                job_id="zero-progress-job",
                manifest_name="manifest-zero-progress.json",
                host={"key": "cbusillo@studio", "label": "Studio", "mode": "ssh", "schedule_profile": "always"},
                status="running",
                attempt_count=1,
            )
            job = load_encode_job(connection, "zero-progress-job")
            assert job is not None
            job["progress"] = {
                "remaining_duration_seconds": 0.0,
                "speed": 0.0,
                "percent_complete": 0.0,
                "eta_seconds": 0.0,
            }
            save_encode_job(connection, job)
            rows = web_app._host_runtime_rows(connection, self.config)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["running_jobs"][0]["telemetry_summary"], "0% · Est. ETA 0s")
        self.assertIsNone(rows[0]["telemetry"]["eta_copy"])

    def test_host_runtime_rows_ignore_folder_aggregate_running_rows(self) -> None:
        status = HostStatus(
            key="ct103",
            label="CT103",
            mode="ssh",
            priority=50,
            capabilities=["encode_queue"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
            repo_path=str(self.root),
            platform="linux",
            videotoolbox_available=False,
        )
        manifest_path = self._write_manifest("manifest-runtime-folder.json", [{"library_item_id": 1}])
        now = web_app._now_iso()

        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "folder-runtime",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": [0],
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {"key": "ct103", "label": "CT103", "mode": "ssh"},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": 123,
                    "error": None,
                    "leased_at": now,
                    "lease_expires_at": now,
                    "heartbeat_at": now,
                    "worker_id": "test-worker",
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "updated_at": now,
                    "progress": {
                        "total_item_count": 1,
                        "completed_item_count": 0,
                        "total_duration_seconds": 120.0,
                        "overall_completed_duration_seconds": 0.0,
                        "remaining_duration_seconds": 120.0,
                        "percent_complete": 0.0,
                        "speed": 1.0,
                    },
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "shard-runtime",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "folder-runtime",
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": [0],
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {"key": "ct103", "label": "CT103", "mode": "ssh"},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": 124,
                    "error": None,
                    "leased_at": now,
                    "lease_expires_at": now,
                    "heartbeat_at": now,
                    "worker_id": "test-worker",
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "updated_at": now,
                    "progress": {
                        "total_item_count": 1,
                        "completed_item_count": 0,
                        "total_duration_seconds": 120.0,
                        "overall_completed_duration_seconds": 0.0,
                        "remaining_duration_seconds": 120.0,
                        "percent_complete": 0.0,
                        "speed": 1.0,
                    },
                },
            )
        with open_db(self.config.paths.db_path) as connection, patch(
                "mediaforce.web.app._safe_collect_host_statuses", return_value=[status]
        ):
            rows = web_app._host_runtime_rows(connection, self.config)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["active_encode_count"], 1)
        self.assertEqual(rows[0]["running_jobs"][0]["job_id"], "shard-runtime")

    def test_host_runtime_rows_keep_active_host_visible_when_probe_fails(self) -> None:
        status = HostStatus(
            key="busy-host",
            label="Busy Host",
            mode="ssh",
            priority=50,
            capabilities=["encode_queue"],
            available=False,
            message="SSH unavailable",
            missing_paths=[],
            issues=["Mediaforce could not reach this host over SSH."],
            detail="operation timed out",
        )

        with open_db(self.config.paths.db_path) as connection, patch(
                "mediaforce.web.app._safe_collect_host_statuses", return_value=[status]
        ):
            self._save_job(
                connection,
                job_id="running-on-busy-host",
                manifest_name="manifest-running-on-busy-host.json",
                host={"key": "busy-host", "label": "Busy Host", "mode": "ssh"},
                status="running",
                attempt_count=1,
            )

            rows = web_app._host_runtime_rows(connection, self.config)

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["available"])
        self.assertFalse(rows[0]["probe_available"])
        self.assertEqual(rows[0]["probe_message"], "SSH unavailable")
        self.assertEqual(rows[0]["message"], "Encoding in progress; latest host check was deferred")
        self.assertEqual(rows[0]["issues"], [])
        self.assertFalse(rows[0]["queue_active"])
        self.assertEqual(rows[0]["active_reason"], "status check deferred while encode is running")

    def test_select_encode_host_skips_probe_degraded_active_hosts(self) -> None:
        statuses = [
            {
                "key": "busy-host",
                "label": "Busy Host",
                "priority": 50,
                "capabilities": ["encode_queue"],
                "available": True,
                "probe_available": False,
                "active_encode_count": 1,
                "max_parallel_encodes": 2,
                "schedule_profile": "always",
            }
        ]
        with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
            host_payload, waiting_reason = web_app._select_encode_host(cast(Any, None), self.config,
                                                                       {"bypass_schedule": False})

        self.assertIsNone(host_payload)
        self.assertEqual(waiting_reason, "waiting for an available encode host")

    def test_host_runtime_rows_prefer_current_host_over_last_host(self) -> None:
        statuses = [
            HostStatus(
                key="old-host",
                label="Old Host",
                mode="ssh",
                priority=20,
                capabilities=["encode_queue"],
                available=True,
                message="Mounted and ready",
                missing_paths=[],
                repo_path=str(self.root),
                platform="linux",
                videotoolbox_available=False,
            ),
            HostStatus(
                key="new-host",
                label="New Host",
                mode="ssh",
                priority=30,
                capabilities=["encode_queue"],
                available=True,
                message="Mounted and ready",
                missing_paths=[],
                repo_path=str(self.root),
                platform="linux",
                videotoolbox_available=False,
            ),
        ]

        with open_db(self.config.paths.db_path) as connection, patch(
                "mediaforce.web.app._safe_collect_host_statuses", return_value=statuses
        ):
            self._save_job(
                connection,
                job_id="retried-job",
                manifest_name="manifest-retried.json",
                host={"key": "new-host", "label": "New Host", "mode": "ssh"},
                status="running",
                attempt_count=2,
            )
            job = load_encode_job(connection, "retried-job")
            assert job is not None
            job["last_host"] = {"key": "old-host", "label": "Old Host", "mode": "ssh"}
            save_encode_job(connection, job)

            rows = web_app._host_runtime_rows(connection, self.config)

        old_row = next(row for row in rows if row["key"] == "old-host")
        new_row = next(row for row in rows if row["key"] == "new-host")
        self.assertEqual(old_row["active_encode_count"], 0)
        self.assertEqual(old_row["running_jobs"], [])
        self.assertEqual(new_row["active_encode_count"], 1)
        self.assertEqual(new_row["running_jobs"][0]["job_id"], "retried-job")

    def test_host_runtime_rows_count_all_running_jobs_even_above_display_cap(self) -> None:
        status = HostStatus(
            key="busy-host",
            label="Busy Host",
            mode="ssh",
            priority=50,
            capabilities=["encode_queue"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
            repo_path=str(self.root),
            platform="linux",
            videotoolbox_available=False,
        )

        with open_db(self.config.paths.db_path) as connection, patch(
                "mediaforce.web.app._safe_collect_host_statuses", return_value=[status]
        ):
            for index in range(40):
                self._save_job(
                    connection,
                    job_id=f"running-{index}",
                    manifest_name=f"manifest-running-{index}.json",
                    host={"key": "busy-host", "label": "Busy Host", "mode": "ssh"},
                    status="running",
                    attempt_count=1,
                )

            rows = web_app._host_runtime_rows(connection, self.config)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["active_encode_count"], 40)
        self.assertEqual(len(rows[0]["running_jobs"]), 32)

    def test_host_runtime_rows_fall_back_to_last_host_when_current_host_keys_are_blank(self) -> None:
        status = HostStatus(
            key="fallback-host",
            label="Fallback Host",
            mode="ssh",
            priority=25,
            capabilities=["encode_queue"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
            repo_path=str(self.root),
            platform="linux",
            videotoolbox_available=False,
        )

        with open_db(self.config.paths.db_path) as connection, patch(
                "mediaforce.web.app._safe_collect_host_statuses", return_value=[status]
        ):
            self._save_job(
                connection,
                job_id="blank-current-host",
                manifest_name="manifest-blank-current-host.json",
                host={"key": "fallback-host", "label": "Fallback Host", "mode": "ssh"},
                status="running",
                attempt_count=1,
            )
            connection.execute(
                update(encode_jobs)
                .where(encode_jobs.c.job_id == "blank-current-host")
                .values(
                    host_json=json.dumps({"key": "", "host": "", "label": ""}, sort_keys=True),
                    last_host_json=json.dumps(
                        {"key": "fallback-host", "label": "Fallback Host", "mode": "ssh"},
                        sort_keys=True,
                    ),
                )
            )

            rows = web_app._host_runtime_rows(connection, self.config)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["active_encode_count"], 1)
        self.assertEqual(rows[0]["running_jobs"][0]["job_id"], "blank-current-host")

    def test_running_encode_counts_by_host_trims_sql_host_keys(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._save_job(
                connection,
                job_id="trimmed-host-key",
                manifest_name="manifest-trimmed-host-key.json",
                host={"key": "busy-host", "label": "Busy Host", "mode": "ssh"},
                status="running",
                attempt_count=1,
            )
            connection.execute(
                update(encode_jobs)
                .where(encode_jobs.c.job_id == "trimmed-host-key")
                .values(host_json=json.dumps({"key": "  busy-host  "}, sort_keys=True))
            )

            counts = host_runtime_module.running_encode_counts_by_host(connection)

        self.assertEqual(counts, {"busy-host": 1})

    def test_save_encode_job_persists_minimal_host_snapshot(self) -> None:
        bloated_host = {
            "key": "ct103",
            "label": "CT103",
            "host": "ct103",
            "mode": "ssh",
            "media_access": "stream",
            "ffmpeg_path": "/usr/local/bin/ffmpeg",
            "source_roots": {"tv": "/srv/tv"},
            "staging_root": "/srv/staging",
            "schedule_profile": "always",
            "queue_active": True,
            "telemetry": {"eta_copy": "2m"},
            "running_jobs": [
                {
                    "job_id": "nested-job",
                    "host": {"key": "ct103", "running_jobs": [{"job_id": "deeply-nested"}]},
                }
            ],
        }

        with open_db(self.config.paths.db_path) as connection:
            self._save_job(
                connection,
                job_id="persisted-host",
                manifest_name="manifest-persisted-host.json",
                host=bloated_host,
                status="queued",
                attempt_count=0,
            )
            job = load_encode_job(connection, "persisted-host")
            host_row = connection.execute(
                select(encode_jobs.c.host_json).where(encode_jobs.c.job_id == "persisted-host")
            ).mappings().fetchone()

        assert job is not None
        assert host_row is not None
        self.assertEqual(
            job["host"],
            {
                "ffmpeg_path": "/usr/local/bin/ffmpeg",
                "host": "ct103",
                "key": "ct103",
                "label": "CT103",
                "media_access": "stream",
                "mode": "ssh",
                "schedule_profile": "always",
                "source_roots": {"tv": "/srv/tv"},
                "staging_root": "/srv/staging",
            },
        )
        self.assertNotIn("running_jobs", job["host"])
        self.assertNotIn("telemetry", job["host"])
        self.assertNotIn("queue_active", job["host"])
        self.assertNotIn('"running_jobs"', str(host_row["host_json"]))

    def test_repair_persisted_encode_job_hosts_prunes_existing_recursive_rows(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._save_job(
                connection,
                job_id="repair-host",
                manifest_name="manifest-repair-host.json",
                host={"key": "ct103", "label": "CT103", "mode": "ssh"},
                status="running",
                attempt_count=1,
            )
            bloated_host = {
                "key": "ct103",
                "label": "CT103",
                "host": "ct103",
                "mode": "ssh",
                "media_access": "stream",
                "running_jobs": [
                    {
                        "job_id": "repair-host",
                        "host": {"key": "ct103", "running_jobs": [{"job_id": "nested"}]},
                    }
                ],
            }
            connection.execute(
                update(encode_jobs)
                .where(encode_jobs.c.job_id == "repair-host")
                .values(
                    host_json=json.dumps(bloated_host, sort_keys=True),
                    last_host_json=json.dumps(bloated_host, sort_keys=True),
                )
            )
            before = connection.execute(
                select(func.length(encode_jobs.c.host_json).label("host_length"))
                .where(encode_jobs.c.job_id == "repair-host")
            ).mappings().fetchone()
            repaired = repair_persisted_encode_job_hosts(connection)
            after = connection.execute(
                select(func.length(encode_jobs.c.host_json).label("host_length"))
                .where(encode_jobs.c.job_id == "repair-host")
            ).mappings().fetchone()
            job = load_encode_job(connection, "repair-host")

        assert before is not None
        assert after is not None
        assert job is not None
        self.assertEqual(repaired, 1)
        self.assertGreater(int(before["host_length"]), int(after["host_length"]))
        self.assertEqual(
            job["host"],
            {
                "host": "ct103",
                "key": "ct103",
                "label": "CT103",
                "media_access": "stream",
                "mode": "ssh",
            },
        )
        self.assertEqual(job["last_host"], job["host"])

    def test_repair_persisted_encode_job_hosts_prunes_short_malformed_rows(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._save_job(
                connection,
                job_id="repair-short-host",
                manifest_name="manifest-repair-short-host.json",
                host={"key": "ct103", "label": "CT103", "mode": "ssh"},
                status="queued",
                attempt_count=0,
            )
            malformed_host = {
                "key": "ct103",
                "label": "CT103",
                "mode": "ssh",
                "telemetry": {"eta_copy": "2m"},
            }
            connection.execute(
                update(encode_jobs)
                .where(encode_jobs.c.job_id == "repair-short-host")
                .values(host_json=json.dumps(malformed_host, sort_keys=True))
            )

            repaired = repair_persisted_encode_job_hosts(connection)
            job = load_encode_job(connection, "repair-short-host")

        assert job is not None
        self.assertEqual(repaired, 1)
        self.assertEqual(job["host"], {"key": "ct103", "label": "CT103", "mode": "ssh"})

    def test_repair_persisted_encode_job_hosts_rewrites_invalid_json_rows(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._save_job(
                connection,
                job_id="repair-invalid-json-host",
                manifest_name="manifest-repair-invalid-json-host.json",
                host={"key": "ct103", "label": "CT103", "mode": "ssh"},
                status="running",
                attempt_count=1,
            )
            connection.execute(
                update(encode_jobs)
                .where(encode_jobs.c.job_id == "repair-invalid-json-host")
                .values(
                    host_json="{",
                    last_host_json=json.dumps(
                        {"key": "ct103", "label": "CT103", "mode": "ssh"}, sort_keys=True
                    ),
                )
            )

            repaired = repair_persisted_encode_job_hosts(connection)
            job = load_encode_job(connection, "repair-invalid-json-host")

        assert job is not None
        self.assertEqual(repaired, 1)
        self.assertEqual(job["host"], {"key": "ct103", "label": "CT103", "mode": "ssh"})

    def test_repair_persisted_encode_job_hosts_rewrites_empty_string_rows(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._save_job(
                connection,
                job_id="repair-empty-string-host",
                manifest_name="manifest-repair-empty-string-host.json",
                host={"key": "ct103", "label": "CT103", "mode": "ssh"},
                status="running",
                attempt_count=1,
            )
            connection.execute(
                update(encode_jobs)
                .where(encode_jobs.c.job_id == "repair-empty-string-host")
                .values(
                    host_json="",
                    last_host_json=json.dumps(
                        {"key": "ct103", "label": "CT103", "mode": "ssh"}, sort_keys=True
                    ),
                )
            )

            repaired = repair_persisted_encode_job_hosts(connection)
            job = load_encode_job(connection, "repair-empty-string-host")

        assert job is not None
        self.assertEqual(repaired, 1)
        self.assertEqual(job["host"], {"key": "ct103", "label": "CT103", "mode": "ssh"})

    def test_repair_persisted_encode_job_hosts_backfills_host_from_last_host(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._save_job(
                connection,
                job_id="repair-backfill-host",
                manifest_name="manifest-repair-backfill-host.json",
                host={"key": "ct103", "label": "CT103", "mode": "ssh"},
                status="running",
                attempt_count=1,
            )
            connection.execute(
                update(encode_jobs)
                .where(encode_jobs.c.job_id == "repair-backfill-host")
                .values(
                    host_json=json.dumps({"key": "", "label": "", "mode": "ssh"}, sort_keys=True),
                    last_host_json=json.dumps(
                        {"key": "ct103", "label": "CT103", "mode": "ssh"}, sort_keys=True
                    ),
                )
            )

            repaired = repair_persisted_encode_job_hosts(connection)
            job = load_encode_job(connection, "repair-backfill-host")

        assert job is not None
        self.assertEqual(repaired, 1)
        self.assertEqual(job["host"], {"key": "ct103", "label": "CT103", "mode": "ssh"})
        self.assertEqual(job["last_host"], {"key": "ct103", "label": "CT103", "mode": "ssh"})

    def test_repair_persisted_encode_job_hosts_backfills_key_when_host_is_label_only(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._save_job(
                connection,
                job_id="repair-label-only-host",
                manifest_name="manifest-repair-label-only-host.json",
                host={"key": "ct103", "label": "CT103", "mode": "ssh"},
                status="running",
                attempt_count=1,
            )
            connection.execute(
                update(encode_jobs)
                .where(encode_jobs.c.job_id == "repair-label-only-host")
                .values(
                    host_json=json.dumps({"label": "CT103", "mode": "ssh"}, sort_keys=True),
                    last_host_json=json.dumps(
                        {"key": "ct103", "label": "CT103", "mode": "ssh"}, sort_keys=True
                    ),
                )
            )

            repaired = repair_persisted_encode_job_hosts(connection)
            job = load_encode_job(connection, "repair-label-only-host")

        assert job is not None
        self.assertEqual(repaired, 1)
        self.assertEqual(job["host"], {"key": "ct103", "label": "CT103", "mode": "ssh"})

    def test_render_source_review_clip_enables_videotoolbox_decode_for_h264_sources(self) -> None:
        with patch("mediaforce.review.ffmpeg_binary", return_value="/tmp/ffmpeg"), patch(
                "mediaforce.review.ffmpeg_hwaccel_input_args", return_value=["-hwaccel", "videotoolbox"]
        ), patch(
            "mediaforce.review.run_command",
            return_value=subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr=""),
        ) as run_mock:
            review._render_source_review_clip(
                source_path=Path("/tmp/input.mkv"),
                source_codec="h264",
                output_path=Path("/tmp/review.mp4"),
                clip_time=12.0,
                duration_seconds=8.0,
            )
        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd[:9], [
            "/tmp/ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-ss",
            "12.000",
            "-t",
        ])
        self.assertIn("-hwaccel", cmd)
        self.assertIn("videotoolbox", cmd)

    def test_render_compare_clip_preserves_native_resolution(self) -> None:
        with patch("mediaforce.review.ffmpeg_binary", return_value="/tmp/ffmpeg"), patch(
                "mediaforce.review.ffmpeg_hwaccel_input_args", return_value=[]
        ), patch(
            "mediaforce.review.run_command",
            return_value=subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr=""),
        ) as run_mock:
            review._render_compare_clip(
                Path("/tmp/source.mkv"),
                Path("/tmp/staged.mkv"),
                Path("/tmp/compare.mkv"),
                12.0,
                8.0,
            )

        cmd = run_mock.call_args.args[0]
        filter_complex = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("xstack=inputs=2", filter_complex)
        self.assertIn("pad=ceil(iw/2)*2:ceil(ih/2)*2", filter_complex)
        self.assertNotIn("scale=", filter_complex)
        self.assertNotIn("hstack", filter_complex)

    def test_render_compare_clip_from_preview_preserves_native_resolution(self) -> None:
        with patch("mediaforce.review.ffmpeg_binary", return_value="/tmp/ffmpeg"), patch(
                "mediaforce.review.ffmpeg_hwaccel_input_args", return_value=[]
        ), patch(
            "mediaforce.review.run_command",
            return_value=subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr=""),
        ) as run_mock:
            review._render_compare_clip_from_preview(
                source_path=Path("/tmp/source.mkv"),
                preview_path=Path("/tmp/preview.mp4"),
                output_path=Path("/tmp/compare.mkv"),
                clip_time=12.0,
                duration_seconds=8.0,
            )

        cmd = run_mock.call_args.args[0]
        filter_complex = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("xstack=inputs=2", filter_complex)
        self.assertIn("pad=ceil(iw/2)*2:ceil(ih/2)*2", filter_complex)
        self.assertNotIn("scale=", filter_complex)
        self.assertNotIn("hstack", filter_complex)

    def test_encode_preview_clips_localhost_ssh_executes_locally(self) -> None:
        output_dir = self.root / "review"
        output_dir.mkdir(parents=True, exist_ok=True)

        def create_preview_file(*_args: object, **kwargs: object) -> None:
            output_path = Path(str(kwargs["output_path"]))
            output_path.write_bytes(b"preview")

        with patch("mediaforce.review._render_encoded_preview_clip") as render_mock, patch(
                "mediaforce.review._encode_preview_clips_remote"
        ) as remote_mock:
            render_mock.side_effect = create_preview_file
            clips = review.encode_preview_clips(
                source_path=Path("/tmp/input.mkv"),
                output_dir=output_dir,
                timestamps=[12.0],
                duration_seconds=8.0,
                encoder="libsvtav1",
                pixel_format="yuv420p10le",
                preset=4,
                crf=28.0,
                svt_params=[],
                host={"key": "cbusillo@localhost", "mode": "ssh"},
            )
        render_mock.assert_called_once()
        remote_mock.assert_not_called()
        self.assertEqual(len(clips), 1)

    def test_encode_preview_clips_remote_copies_results_and_cleans_up(self) -> None:
        output_dir = self.root / "review-remote"
        output_dir.mkdir(parents=True, exist_ok=True)
        remote_calls: list[list[str]] = []

        def remote_command_side_effect(
                _host: dict[str, object],
                cmd: list[str],
                timeout: int,
        ) -> subprocess.CompletedProcess[str]:
            self.assertIn(timeout, (30, review.REMOTE_PREVIEW_TIMEOUT_SECONDS))
            remote_calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        def copy_side_effect(
                _host: dict[str, object],
                _remote_path: Path,
                local_path: Path,
                timeout: int,
        ) -> None:
            self.assertEqual(timeout, review.REMOTE_PREVIEW_TIMEOUT_SECONDS)
            local_path.write_bytes(b"preview")

        with patch("mediaforce.review.run_remote_command", side_effect=remote_command_side_effect), patch(
                "mediaforce.review._render_encoded_preview_clip_remote"
        ) as render_remote_mock, patch(
            "mediaforce.review.copy_remote_file_to_local", side_effect=copy_side_effect
        ), patch("mediaforce.review.uuid.uuid4") as uuid_mock:
            uuid_mock.return_value.hex = "abcdef1234567890"
            clips = review.encode_preview_clips(
                source_path=Path("/tmp/input.mkv"),
                output_dir=output_dir,
                timestamps=[12.0],
                duration_seconds=8.0,
                encoder="libsvtav1",
                pixel_format="yuv420p10le",
                preset=4,
                crf=28.0,
                svt_params=[],
                host={"key": "cbusillo@studio", "mode": "ssh"},
            )

        render_remote_mock.assert_called_once()
        self.assertEqual(remote_calls[0], ["mkdir", "-p", "/tmp/mediaforce-preview-abcdef123456"])
        self.assertEqual(remote_calls[-1], ["rm", "-rf", "/tmp/mediaforce-preview-abcdef123456"])
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].size_bytes, len(b"preview"))

    def test_render_review_contact_sheet_builds_expected_stack_command(self) -> None:
        with patch("mediaforce.review.ffmpeg_binary", return_value="/tmp/ffmpeg"), patch(
                "mediaforce.review.run_command",
                return_value=subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr=""),
        ) as run_mock:
            review.render_review_contact_sheet(
                source_clip_path=Path("/tmp/source.mp4"),
                preview_clip_path=Path("/tmp/preview.mp4"),
                output_path=Path("/tmp/contact-sheet.png"),
            )
        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd[:6], ["/tmp/ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"])
        self.assertIn("[src][draft]vstack=inputs=2[v]", cmd[cmd.index("-filter_complex") + 1])
        self.assertEqual(cmd[-2:], ["1", "/tmp/contact-sheet.png"])

    def test_render_review_timeline_strip_builds_expected_command(self) -> None:
        with patch("mediaforce.review.ffmpeg_binary", return_value="/tmp/ffmpeg"), patch(
                "mediaforce.review.run_command",
                return_value=subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr=""),
        ) as run_mock:
            review.render_review_timeline_strip(clip_path=Path("/tmp/compare.mkv"),
                                                output_path=Path("/tmp/timeline-strip.png"), duration_seconds=8.0)
        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd[:6], ["/tmp/ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"])
        self.assertIn("tile=6x1", cmd[cmd.index("-filter_complex") + 1])
        self.assertEqual(cmd[-2:], ["1", "/tmp/timeline-strip.png"])

    def test_render_audio_spectrogram_compare_renders_assets_and_cleans_temp_dir(self) -> None:
        output_path = self.root / "review-assets" / "spectrogram.png"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        def create_file(*_args: object, **kwargs: object) -> None:
            Path(str(kwargs["output_path"])).write_bytes(b"artifact")

        with patch("mediaforce.review._render_audio_spectrogram", side_effect=create_file) as spectrogram_mock, patch(
                "mediaforce.review._render_encoded_audio_clip", side_effect=create_file
        ) as encoded_audio_mock, patch("mediaforce.review._stack_review_images", side_effect=create_file) as stack_mock:
            result = review.render_audio_spectrogram_compare(
                source_path=Path("/tmp/input.mkv"),
                output_path=output_path,
                clip_time=12.0,
                duration_seconds=8.0,
                audio_track={"codec_name": "aac", "channels": 6, "index": 2},
                audio_policy={"convert_to_opus_codecs": ["aac"], "surround_5_1_opus_bitrate": "256k"},
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "libopus")
        self.assertEqual(result["bitrate"], "256k")
        self.assertEqual(result["channels"], 6)
        self.assertEqual(result["codec_name"], "aac")
        self.assertTrue(output_path.exists())
        self.assertEqual(spectrogram_mock.call_count, 2)
        encoded_audio_mock.assert_called_once()
        stack_mock.assert_called_once()
        self.assertFalse((output_path.parent / ".spectrogram-artifacts").exists())
        self.assertEqual(spectrogram_mock.call_args_list[0].kwargs["audio_track"]["index"], 2)
        self.assertEqual(encoded_audio_mock.call_args.kwargs["audio_track"]["index"], 2)

    def test_render_audio_spectrogram_uses_selected_stream_index(self) -> None:
        with patch("mediaforce.review.ffmpeg_binary", return_value="/tmp/ffmpeg"), patch(
                "mediaforce.review.run_command",
                return_value=subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr=""),
        ) as run_mock:
            review._render_audio_spectrogram(
                source_path=Path("/tmp/input.mkv"),
                output_path=Path("/tmp/audio.png"),
                clip_time=12.0,
                duration_seconds=8.0,
                audio_track={"index": 3},
            )

        cmd = run_mock.call_args.args[0]
        self.assertIn("[0:3]aformat=channel_layouts=stereo", cmd[cmd.index("-filter_complex") + 1])

    def test_render_encoded_audio_clip_uses_selected_stream_index(self) -> None:
        with patch("mediaforce.review.ffmpeg_binary", return_value="/tmp/ffmpeg"), patch(
                "mediaforce.review.run_command",
                return_value=subprocess.CompletedProcess(args=["ffmpeg"], returncode=0, stdout="", stderr=""),
        ) as run_mock:
            review._render_encoded_audio_clip(
                source_path=Path("/tmp/input.mkv"),
                output_path=Path("/tmp/audio.opus"),
                clip_time=12.0,
                duration_seconds=8.0,
                audio_track={"index": 3},
                bitrate="224k",
            )

        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd[cmd.index("-map") + 1], "0:3")

    def test_render_encoded_preview_clip_remote_builds_remote_ffmpeg_command(self) -> None:
        with patch("mediaforce.review.ffmpeg_binary", return_value="/tmp/ffmpeg"), patch(
                "mediaforce.review.ffmpeg_hwaccel_input_args", return_value=["-hwaccel", "videotoolbox"]
        ), patch(
            "mediaforce.review.run_remote_command",
            return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
        ) as remote_mock:
            review._render_encoded_preview_clip_remote(
                host={"key": "cbusillo@studio", "mode": "ssh"},
                source_path=Path("/tmp/input.mkv"),
                source_codec="h264",
                remote_output_path=Path("/tmp/output.mp4"),
                clip_time=12.0,
                duration_seconds=8.0,
                encoder="libsvtav1",
                pixel_format="yuv420p10le",
                preset=4,
                crf=28.25,
                svt_params=["tune=0", "film-grain=0"],
            )
        cmd = remote_mock.call_args.args[1]
        self.assertIn("-hwaccel", cmd)
        self.assertIn("videotoolbox", cmd)
        self.assertIn("-svtav1-params", cmd)
        self.assertIn("tune=0:film-grain=0", cmd)
        self.assertIn("28.25", cmd)
        self.assertEqual(cmd[-1], "/tmp/output.mp4")

    def test_generate_compare_clips_uses_staged_artifacts_and_auto_timestamps(self) -> None:
        source_path = self._create_source_file("episode-compare.mkv")
        staged_path = self._staging_path("episode-compare.mkv")
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_text("encoded")
        manifest = {
            "items": [
                {
                    "library_item_id": 1,
                    "source_path": str(source_path),
                    "duration_seconds": 120.0,
                    "video_codec": "h264",
                }
            ]
        }
        compare_clip = review.CompareClip(output_path=self.root / "compare.mkv", timestamp_seconds=30.0,
                                          duration_seconds=8.0)
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="validated")
            self._insert_staged_artifact(connection, item_id, staged_path)
            manifest["items"][0]["library_item_id"] = item_id
            with patch("mediaforce.review._auto_timestamps", return_value=[30.0]) as auto_mock, patch(
                    "mediaforce.review.generate_compare_clips_for_pair", return_value=[compare_clip]
            ) as pair_mock:
                result = review.generate_compare_clips(
                    connection,
                    manifest,
                    [0],
                    output_dir=self.root / "review-compare",
                    duration_seconds=8.0,
                    timestamps=None,
                    play=False,
                )

        auto_mock.assert_called_once()
        self.assertEqual(pair_mock.call_args.kwargs["source_path"], source_path)
        self.assertEqual(pair_mock.call_args.kwargs["staged_path"], staged_path)
        self.assertEqual(result, [compare_clip])

    def test_run_remote_ssh_uses_alias_friendly_ssh_options(self) -> None:
        host: dict[str, object] = {"host": "cbusillo@chris-mini.local"}
        with patch(
                "mediaforce.remote.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
        ) as run_mock:
            remote._run_remote_ssh(host, "true", timeout=5, wake_before_connect=False)
        ssh_cmd = run_mock.call_args.args[0]
        self.assertEqual(ssh_cmd[0], "ssh")
        self.assertIn("StrictHostKeyChecking=accept-new", ssh_cmd)
        self.assertIn("UpdateHostKeys=yes", ssh_cmd)
        self.assertIn("CheckHostIP=no", ssh_cmd)
        self.assertIn("BatchMode=yes", ssh_cmd)
        self.assertIn("ConnectTimeout=5", ssh_cmd)

    def test_run_remote_command_quotes_shell_script_for_ssh(self) -> None:
        host: dict[str, object] = {"host": "cbusillo@extra-mbp.shiny", "mode": "ssh"}
        with patch(
                "mediaforce.remote.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
        ) as run_mock:
            remote.run_remote_command(host, ["ab-av1", "--version"], timeout=5)
        ssh_cmd = run_mock.call_args.args[0]
        self.assertEqual(ssh_cmd[0], "ssh")
        self.assertEqual(ssh_cmd[-2], "cbusillo@extra-mbp.shiny")
        self.assertTrue(ssh_cmd[-1].startswith("sh -lc "))
        self.assertIn("ab-av1 --version", ssh_cmd[-1])

    def test_worker_leadership_lease_blocks_second_acquire_until_release(self) -> None:
        lock_path = self.root / "state" / "web" / "background-workers.lock"
        first = WorkerLeadershipLease(lock_path, worker_name="background-workers")
        second = WorkerLeadershipLease(lock_path, worker_name="background-workers")

        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())

        owner = second.owner_metadata()
        self.assertIsNotNone(owner)
        assert owner is not None
        self.assertEqual(owner["worker_name"], "background-workers")

        first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_start_background_workers_skips_startup_without_leadership(self) -> None:
        with patch("mediaforce.web.app._acquire_background_worker_leadership", return_value=False), patch(
                "mediaforce.web.app._start_calibration_queue_worker"
        ) as start_calibration, patch("mediaforce.web.app._start_encode_queue_worker") as start_encode:
            started = web_app._start_background_workers(self.config)

        self.assertFalse(started)
        start_calibration.assert_not_called()
        start_encode.assert_not_called()

    def test_start_background_workers_starts_both_workers_when_leader(self) -> None:
        with patch("mediaforce.web.app._acquire_background_worker_leadership", return_value=True), patch(
                "mediaforce.web.app._start_calibration_queue_worker"
        ) as start_calibration, patch("mediaforce.web.app._start_encode_queue_worker") as start_encode:
            started = web_app._start_background_workers(self.config)

        self.assertTrue(started)
        start_calibration.assert_called_once_with(self.config)
        start_encode.assert_called_once_with(self.config)

    def test_stop_encode_queue_action_sweeps_orphaned_processes_after_cancel(self) -> None:
        cancel_queue_process = Mock()
        sweep_orphaned_encode_processes = Mock()
        clear_stale_encoding_items = Mock(return_value=4)

        result = queue_actions_runtime.stop_encode_queue_action(
            connection_factory=lambda: open_db(self.config.paths.db_path),
            config=self.config,
            now_iso=web_app._now_iso,
            cancel_queue_process=cancel_queue_process,
            sweep_orphaned_encode_processes=sweep_orphaned_encode_processes,
            clear_stale_encoding_items=clear_stale_encoding_items,
        )

        self.assertTrue(result["ok"])
        cancel_queue_process.assert_called_once_with()
        sweep_orphaned_encode_processes.assert_called_once_with()
        clear_stale_encoding_items.assert_called_once_with()
        self.assertEqual(result["cleared_stale_item_count"], 4)

        with open_db(self.config.paths.db_path) as connection:
            state = load_queue_state(connection)
        self.assertTrue(state["is_paused"])
        self.assertTrue(state["stop_requested"])

    def test_sweep_orphaned_encode_processes_only_targets_ssh_encode_hosts(self) -> None:
        self.config.raw["remote_hosts"] = [
            {"host": "encode-a", "label": "Encode A", "mode": "ssh", "capabilities": ["encode_queue"]},
            {"host": "encode-b", "label": "Encode B", "capabilities": ["encode_queue"]},
            {"host": "encode-c", "label": "Encode C", "mode": "   ", "capabilities": ["encode_queue"]},
            {"host": "sample-only", "label": "Sample Only", "mode": "ssh", "capabilities": ["sample_calibration"]},
            {"host": "local-ish", "label": "Local-ish", "mode": "local", "capabilities": ["encode_queue"]},
        ]

        with patch("mediaforce.web.app.run_remote_command") as run_remote_command_mock:
            web_app._sweep_orphaned_encode_processes(self.config)

        self.assertEqual(run_remote_command_mock.call_count, 3)
        self.assertEqual(
            [call.args[0]["host"] for call in run_remote_command_mock.call_args_list],
            ["encode-a", "encode-b", "encode-c"],
        )
        command_payload = run_remote_command_mock.call_args.args[1]
        self.assertEqual(command_payload[:2], ["sh", "-lc"])
        self.assertIn("self_pgid=$(ps -o pgid= -p \"$self_pid\"", command_payload[2])
        self.assertIn("kill_tree() ( signal=$1; target=$2;", command_payload[2])
        self.assertIn("mediaforce_encoded_by=mediaforce", command_payload[2])
        self.assertIn("ab-av1 .*--temp-dir .*\\.mediaforce-ab-av1-", command_payload[2])
        self.assertIn("$0 !~ /(^|[[:space:]/])awk([[:space:]]|$)/", command_payload[2])
        self.assertIn("[ \"$pgid\" != \"$self_pgid\" ]", command_payload[2])
        self.assertIn("kill -TERM -\"$pgid\"", command_payload[2])
        self.assertIn("kill_tree TERM \"$pid\"", command_payload[2])
        self.assertIn("kill -KILL -\"$pgid\"", command_payload[2])
        self.assertIn("kill_tree KILL \"$pid\"", command_payload[2])

    def test_sweep_orphaned_encode_processes_can_scope_to_recovered_prefix(self) -> None:
        host_staging_root = self.root / "remote-transcode"
        self.config.raw["remote_hosts"] = [
            {
                "host": "encode-a",
                "label": "Encode A",
                "mode": "ssh",
                "capabilities": ["encode_queue"],
                "staging_root": str(host_staging_root),
            }
        ]

        with patch("mediaforce.web.app.run_remote_command") as run_remote_command_mock:
            web_app._sweep_orphaned_encode_processes(self.config, prefixes=["tv/show"])

        run_remote_command_mock.assert_called_once()
        command_payload = run_remote_command_mock.call_args.args[1]
        self.assertEqual(command_payload[:3], ["sh", "-lc", command_payload[2]])
        self.assertEqual(command_payload[3], "mediaforce-sweep")
        self.assertIn(
            f"{re.escape(str(host_staging_root / 'tv' / 'show'))}(/|[[:space:]]|$)",
            command_payload[4],
        )
        self.assertIn(
            f"{re.escape(str(self.root / 'source' / 'tv' / 'show'))}(/|[[:space:]]|$)",
            command_payload[4],
        )
        self.assertNotIn(re.escape(str(host_staging_root / "tv" / "showing")), command_payload[4])
        self.assertNotIn(re.escape(str(self.root / "source" / "tv" / "showing")), command_payload[4])

    def test_create_app_registers_folder_status_route_before_catch_all_folder_route(self) -> None:
        with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ):
            app = web_app.create_app(self.config.paths.config_path)

        folder_route_paths = [
            str(getattr(route, "path", ""))
            for route in app.router.routes
            if str(getattr(route, "path", "")).startswith("/api/folders/")
        ]
        self.assertLess(
            folder_route_paths.index("/api/folders/{prefix:path}/status"),
            folder_route_paths.index("/api/folders/{prefix:path}"),
        )

    def test_folder_api_route_returns_payload_for_seeded_prefix(self) -> None:
        source_path = self._create_source_file("episode-folder.mkv")
        self.config.raw.update(
            {
                "video": {
                    "quality_metric": "auto",
                    "target_vmaf": 94,
                    "min_target_vmaf": 92,
                    "target_xpsnr": 36,
                    "min_target_xpsnr": 34,
                    "max_encoded_percent": 100,
                    "default_grain": 0,
                },
                "audio": {},
                "subtitle": {},
                "planning": {},
            }
        )
        self.config.raw["media"]["output_container"] = "mkv"

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path)
            connection.execute(
                update(library_items)
                .where(library_items.c.id == item_id)
                .values(audio_summary_json=json.dumps([{"index": 0, "codec": "aac", "channels": 2, "language": "eng"}]))
            )
            connection.commit()

        with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ):
            app = web_app.create_app(self.config.paths.config_path)

        folder_endpoint = next(
            route.endpoint
            for route in app.router.routes
            if getattr(route, "path", "") == "/api/folders/{prefix:path}"
        )
        with patch("mediaforce.web.app._maybe_schedule_scan", return_value=None), patch(
                "mediaforce.web.app._sample_calibration_host_statuses", return_value=[]
        ):
            response = folder_endpoint("tv/show")
        payload = json.loads(response.body)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["pending"])
        self.assertEqual(payload["prefix"], "tv/show")
        self.assertEqual(payload["sample_item"]["rel_path"], "tv/show/episode-folder.mkv")
        self.assertIn("summary", payload)

    def test_folder_endpoint_returns_payload_for_fully_promoted_prefix(self) -> None:
        from mediaforce.web import app as folder_web_app

        source_path = self._create_source_file("episode-promoted-folder.mkv")
        self.config.raw["media"]["output_container"] = "mp4"
        self.config.raw.update(
            {
                "video": {
                    "quality_metric": "auto",
                    "target_vmaf": 94,
                    "min_target_vmaf": 92,
                    "target_xpsnr": 36,
                    "min_target_xpsnr": 34,
                    "max_encoded_percent": 100,
                    "default_grain": 0,
                },
                "audio": {},
                "subtitle": {},
                "planning": {},
            }
        )

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="promoted")
            connection.execute(
                update(library_items)
                .where(library_items.c.id == item_id)
                .values(
                    rel_path="tv/House/Season 5/episode-promoted-folder.mp4",
                    parent_dir="tv/House/Season 5",
                    source_path=str(
                        self.root / "source" / "tv" / "House" / "Season 5" / "episode-promoted-folder.mp4"
                    ),
                    container="mp4",
                    audio_summary_json=json.dumps([{"index": 0, "codec": "opus", "channels": 6, "language": "eng"}]),
                    subtitle_summary_json=json.dumps([]),
                )
            )
            connection.commit()

        with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ):
            app = folder_web_app.create_app(self.config.paths.config_path)

        folder_endpoint = next(
            route.endpoint
            for route in app.router.routes
            if getattr(route, "path", "") == "/api/folders/{prefix:path}"
        )
        with patch("mediaforce.web.app._maybe_schedule_scan", return_value=None), patch(
                "mediaforce.web.app._sample_calibration_host_statuses", return_value=[]
        ):
            response = folder_endpoint("tv/House/Season 5")
        payload = json.loads(response.body)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["pending"])
        self.assertEqual(payload["prefix"], "tv/House/Season 5")
        self.assertEqual(payload["sample_item"]["rel_path"], "tv/House/Season 5/episode-promoted-folder.mp4")
        self.assertEqual(payload["summary"]["statuses"]["promoted"], 1)

    def test_process_calibration_queue_once_handles_plain_connection_queries(self) -> None:
        deps = web_app._calibration_queue_runtime_deps()
        deps.run_calibration_job = Mock()

        with patch("mediaforce.web.runtime.job_runtime.load_config", return_value=self.config):
            job_runtime.process_calibration_queue_once(config_path=self.config.paths.config_path, deps=deps)

        deps.run_calibration_job.assert_not_called()

    def test_encode_queue_worker_loop_logs_pass_failures_and_keeps_running(self) -> None:
        deps = Mock()
        deps.logger = Mock()
        deps.encode_queue_poll_seconds = 1.0

        wait_gate = Mock()
        wait_gate.wait.side_effect = KeyboardInterrupt()

        with patch(
                "mediaforce.web.runtime.encode_runtime.process_encode_queue_once",
                side_effect=RuntimeError("boom"),
        ), patch("mediaforce.web.runtime.worker_supervision.threading.Event", return_value=wait_gate):
            with self.assertRaises(KeyboardInterrupt):
                encode_runtime.encode_queue_worker_loop(config_path=self.config.paths.config_path, deps=deps)

        deps.logger.exception.assert_called_once_with("Encode queue worker pass failed")

    def test_calibration_queue_worker_loop_logs_pass_failures_and_keeps_running(self) -> None:
        deps = Mock()
        deps.calibration_queue_poll_seconds = 1.0
        logger = Mock()

        wait_gate = Mock()
        wait_gate.wait.side_effect = KeyboardInterrupt()

        with patch(
                "mediaforce.web.runtime.job_runtime.process_calibration_queue_once",
                side_effect=RuntimeError("boom"),
        ), patch("mediaforce.web.runtime.worker_supervision.threading.Event", return_value=wait_gate):
            with self.assertRaises(KeyboardInterrupt):
                job_runtime.calibration_queue_worker_loop(
                    config_path=self.config.paths.config_path,
                    deps=deps,
                    logger=logger,
                )

        logger.exception.assert_called_once_with("Calibration queue worker pass failed")

    def test_load_next_runnable_encode_job_supports_plain_sqlalchemy_connection(self) -> None:
        source_path = self._create_source_file("episode-queued.mkv")
        staging_path = self._staging_path("episode-queued.mkv")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path)
            self._write_manifest(
                "manifest-queued.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._save_job(
                connection,
                job_id="job-queued-runtime",
                manifest_name="manifest-queued.json",
                host={},
                status="queued",
                attempt_count=0,
            )
            connection.commit()

            deps = Mock()
            deps.now_iso.return_value = web_app._now_iso()

            with patch(
                    "mediaforce.web.runtime.encode_runtime.select_encode_host",
                    return_value=({"key": "local", "label": "Local", "mode": "local"}, None),
            ):
                job = encode_runtime.load_next_runnable_encode_job(connection, self.config, deps)

            self.assertIsNotNone(job)
            assert job is not None
            self.assertEqual(job["job_id"], "job-queued-runtime")
            self.assertEqual(job["host"]["key"], "local")

    def test_process_encode_queue_once_dispatches_multiple_jobs(self) -> None:
        manifest_path = self._write_manifest(
            "manifest-fanout.json",
            [
                {"library_item_id": 1, "staging_path": str(self._staging_path("fanout-1.mkv")),
                 "duration_seconds": 90.0},
                {"library_item_id": 2, "staging_path": str(self._staging_path("fanout-2.mkv")),
                 "duration_seconds": 120.0},
            ],
        )
        deps = web_app._encode_queue_runtime_deps()
        deps.load_config = Mock(return_value=self.config)
        deps.dispatch_encode_job = Mock()
        deps.active_encode_process_controllers = Mock(return_value=[])
        host_rows = [
            {
                "key": "remote-a",
                "label": "Remote A",
                "available": True,
                "capabilities": ["encode_queue"],
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "priority": 30,
            },
            {
                "key": "remote-b",
                "label": "Remote B",
                "available": True,
                "capabilities": ["encode_queue"],
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "priority": 20,
            },
        ]
        deps.host_runtime_rows = Mock(return_value=host_rows)

        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "folder-parent",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "queued",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": None,
                    "item_count": 2,
                    "saved_profile_path": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 0,
                    "process_pid": None,
                    "error": None,
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
                    "created_at": web_app._now_iso(),
                    "started_at": None,
                    "finished_at": None,
                    "updated_at": web_app._now_iso(),
                },
            )
            for shard_id, manifest_indexes in (("shard-a", [0]), ("shard-b", [1])):
                save_encode_job(
                    connection,
                    {
                        "job_id": shard_id,
                        "prefix": "tv/show",
                        "job_kind": "shard",
                        "parent_job_id": "folder-parent",
                        "status": "queued",
                        "manifest_path": str(manifest_path),
                        "manifest_indexes": manifest_indexes,
                        "item_count": 1,
                        "saved_profile_path": None,
                        "host": {},
                        "last_host": {},
                        "notes": "",
                        "bypass_schedule": False,
                        "attempt_count": 0,
                        "process_pid": None,
                        "error": None,
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
                        "created_at": web_app._now_iso(),
                        "started_at": None,
                        "finished_at": None,
                        "updated_at": web_app._now_iso(),
                    },
                )

        with patch(
                "mediaforce.web.runtime.encode_runtime.select_encode_host",
                side_effect=[
                    ({"key": "remote-a", "label": "Remote A", "mode": "ssh"}, None),
                    ({"key": "remote-b", "label": "Remote B", "mode": "ssh"}, None),
                    (None, "waiting for host capacity to free up"),
                ],
        ):
            encode_runtime.process_encode_queue_once(config_path=self.config.paths.config_path, deps=deps)

        dispatched_ids = [call.kwargs["job_id"] for call in deps.dispatch_encode_job.mock_calls]
        self.assertEqual(dispatched_ids, ["shard-a", "shard-b"])
        with open_db(self.config.paths.db_path) as connection:
            shard_a = load_encode_job(connection, "shard-a")
            shard_b = load_encode_job(connection, "shard-b")
            parent = load_encode_job(connection, "folder-parent")
        assert shard_a is not None and shard_b is not None and parent is not None
        self.assertEqual(shard_a["status"], "running")
        self.assertEqual(shard_b["status"], "running")
        self.assertEqual(parent["status"], "running")

    def test_resolve_encode_job_for_display_aggregates_shards(self) -> None:
        manifest_path = self._write_manifest(
            "manifest-display.json",
            [
                {"library_item_id": 1, "staging_path": str(self._staging_path("display-1.mkv")),
                 "duration_seconds": 100.0},
                {"library_item_id": 2, "staging_path": str(self._staging_path("display-2.mkv")),
                 "duration_seconds": 200.0},
            ],
        )
        now = web_app._now_iso()
        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "folder-display",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "queued",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": None,
                    "item_count": 2,
                    "saved_profile_path": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 0,
                    "process_pid": None,
                    "error": None,
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
                    "created_at": now,
                    "started_at": None,
                    "finished_at": None,
                    "updated_at": now,
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "display-shard-a",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "folder-display",
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": [0],
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": 123,
                    "error": None,
                    "leased_at": now,
                    "lease_expires_at": now,
                    "heartbeat_at": now,
                    "worker_id": "worker-a",
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "progress": {
                        "total_item_count": 1,
                        "completed_item_count": 0,
                        "total_duration_seconds": 100.0,
                        "overall_completed_duration_seconds": 40.0,
                        "remaining_duration_seconds": 60.0,
                        "percent_complete": 40.0,
                        "fps": 12.0,
                        "speed": 2.0,
                        "current_item_rel_path": "tv/show/display-1.mkv",
                    },
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "updated_at": now,
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "display-shard-b",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "folder-display",
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": [1],
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {"key": "remote-b", "label": "Remote B", "mode": "ssh"},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": 456,
                    "error": None,
                    "leased_at": now,
                    "lease_expires_at": now,
                    "heartbeat_at": now,
                    "worker_id": "worker-b",
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "progress": {
                        "total_item_count": 1,
                        "completed_item_count": 1,
                        "total_duration_seconds": 200.0,
                        "overall_completed_duration_seconds": 100.0,
                        "remaining_duration_seconds": 100.0,
                        "percent_complete": 50.0,
                        "fps": 18.0,
                        "speed": 1.5,
                        "current_item_rel_path": "tv/show/display-2.mkv",
                    },
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "updated_at": now,
                },
            )
            parent = load_encode_job(connection, "folder-display")
            assert parent is not None
            resolved = encode_runtime.resolve_encode_job_for_display(connection, parent,
                                                                     web_app._encode_queue_runtime_deps())

        assert resolved is not None
        self.assertEqual(resolved["status"], "running")
        self.assertEqual(resolved["running_shard_count"], 2)
        self.assertEqual(resolved["shard_count"], 2)
        self.assertEqual(len(resolved["active_hosts"]), 2)
        self.assertAlmostEqual(float(resolved["progress"]["percent_complete"]), (140.0 / 300.0) * 100.0, places=2)
        self.assertAlmostEqual(float(resolved["progress"]["speed"]), 3.5, places=2)
        self.assertAlmostEqual(float(resolved["progress"]["fps"]), 30.0, places=2)
        self.assertAlmostEqual(float(resolved["progress"]["eta_seconds"]), 160.0 / 3.5, places=2)

    def test_resolve_encode_job_for_display_projects_eta_for_running_shards_without_speed(self) -> None:
        manifest_path = self._write_manifest(
            "manifest-display-mixed-speed.json",
            [
                {"library_item_id": 1, "staging_path": str(self._staging_path("display-mixed-1.mkv")),
                 "duration_seconds": 100.0},
                {"library_item_id": 2, "staging_path": str(self._staging_path("display-mixed-2.mkv")),
                 "duration_seconds": 200.0},
            ],
        )
        now = web_app._now_iso()
        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "folder-display-mixed-speed",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "queued",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": None,
                    "item_count": 2,
                    "saved_profile_path": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 0,
                    "process_pid": None,
                    "error": None,
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
                    "created_at": now,
                    "started_at": None,
                    "finished_at": None,
                    "updated_at": now,
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "display-mixed-shard-a",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "folder-display-mixed-speed",
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": [0],
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": 123,
                    "error": None,
                    "leased_at": now,
                    "lease_expires_at": now,
                    "heartbeat_at": now,
                    "worker_id": "worker-a",
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "progress": {
                        "total_item_count": 1,
                        "completed_item_count": 0,
                        "total_duration_seconds": 100.0,
                        "overall_completed_duration_seconds": 40.0,
                        "remaining_duration_seconds": 60.0,
                        "percent_complete": 40.0,
                        "fps": 12.0,
                        "speed": 2.0,
                        "current_item_rel_path": "tv/show/display-mixed-1.mkv",
                    },
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "updated_at": now,
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "display-mixed-shard-b",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "folder-display-mixed-speed",
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": [1],
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {"key": "remote-b", "label": "Remote B", "mode": "ssh"},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": 456,
                    "error": None,
                    "leased_at": now,
                    "lease_expires_at": now,
                    "heartbeat_at": now,
                    "worker_id": "worker-b",
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "progress": {
                        "total_item_count": 1,
                        "completed_item_count": 0,
                        "total_duration_seconds": 200.0,
                        "overall_completed_duration_seconds": 0.0,
                        "remaining_duration_seconds": 200.0,
                        "percent_complete": 0.0,
                        "fps": None,
                        "speed": None,
                        "progress_state": "quality_search",
                        "current_item_rel_path": "tv/show/display-mixed-2.mkv",
                    },
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "updated_at": now,
                },
            )
            parent = load_encode_job(connection, "folder-display-mixed-speed")
            assert parent is not None
            resolved = encode_runtime.resolve_encode_job_for_display(connection, parent,
                                                                     web_app._encode_queue_runtime_deps())

        assert resolved is not None
        self.assertAlmostEqual(float(resolved["progress"]["speed"]), 2.0, places=2)
        self.assertAlmostEqual(float(resolved["progress"]["eta_speed"]), 4.0, places=2)
        self.assertAlmostEqual(float(resolved["progress"]["eta_seconds"]), 260.0 / 4.0, places=2)

    def test_resolve_encode_job_for_display_keeps_host_payload_empty(self) -> None:
        manifest_path = self._write_manifest("manifest-display-single-shard.json", [{"library_item_id": 1}])
        now = web_app._now_iso()

        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "folder-single-shard",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "queued",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": None,
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 0,
                    "process_pid": None,
                    "error": None,
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
                    "created_at": now,
                    "started_at": None,
                    "finished_at": None,
                    "updated_at": now,
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "shard-single",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "folder-single-shard",
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": [0],
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {"key": "ct103", "label": "CT103", "mode": "ssh"},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": 321,
                    "error": None,
                    "leased_at": now,
                    "lease_expires_at": now,
                    "heartbeat_at": now,
                    "worker_id": "test-worker",
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "updated_at": now,
                    "progress": {
                        "total_item_count": 1,
                        "completed_item_count": 0,
                        "total_duration_seconds": 120.0,
                        "overall_completed_duration_seconds": 0.0,
                        "remaining_duration_seconds": 120.0,
                        "percent_complete": 0.0,
                        "speed": 1.0,
                    },
                },
            )

            parent = load_encode_job(connection, "folder-single-shard")
            assert parent is not None
            resolved = encode_runtime.resolve_encode_job_for_display(connection, parent,
                                                                     web_app._encode_queue_runtime_deps())

        assert resolved is not None
        self.assertEqual(resolved["status"], "running")
        self.assertEqual(resolved["running_shard_count"], 1)
        self.assertEqual(resolved["host"], {})

    def test_aggregate_encode_parent_job_marks_quality_search_phase(self) -> None:
        manifest_path = self._write_manifest(
            "manifest-quality-parent.json",
            [{"library_item_id": 1, "duration_seconds": 120.0, "source_size_bytes": 1000}],
        )
        now = web_app._now_iso()

        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "folder-quality-parent",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": None,
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 0,
                    "process_pid": None,
                    "error": None,
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
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "updated_at": now,
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "quality-shard",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "folder-quality-parent",
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": [0],
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": 123,
                    "error": None,
                    "leased_at": now,
                    "lease_expires_at": now,
                    "heartbeat_at": now,
                    "worker_id": "worker-a",
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "progress": {
                        "total_item_count": 1,
                        "completed_item_count": 0,
                        "total_duration_seconds": 120.0,
                        "overall_completed_duration_seconds": 0.0,
                        "remaining_duration_seconds": 120.0,
                        "percent_complete": 0.0,
                        "progress_state": "quality_search",
                        "phase_label": "Searching quality",
                        "current_item_rel_path": "tv/show/episode-1.mkv",
                    },
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "updated_at": now,
                },
            )
            parent = load_encode_job(connection, "folder-quality-parent")
            assert parent is not None
            aggregated = encode_runtime.aggregate_encode_parent_job(
                connection,
                parent,
                web_app._encode_queue_runtime_deps(),
            )

        self.assertEqual(aggregated["progress"]["progress_state"], "quality_search")
        self.assertEqual(aggregated["progress"]["phase_label"], "Searching quality")

    def test_aggregate_encode_parent_job_stays_running_while_other_shards_need_attention(self) -> None:
        manifest_path = self._write_manifest(
            "manifest-parent-mixed-shards.json",
            [
                {"library_item_id": 1, "duration_seconds": 120.0, "source_size_bytes": 1000},
                {"library_item_id": 2, "duration_seconds": 180.0, "source_size_bytes": 2000},
            ],
        )
        now = web_app._now_iso()

        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "folder-mixed-parent",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": None,
                    "item_count": 2,
                    "saved_profile_path": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 0,
                    "process_pid": None,
                    "error": None,
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
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "updated_at": now,
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "mixed-running-shard",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "folder-mixed-parent",
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": [0],
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": 321,
                    "error": None,
                    "leased_at": now,
                    "lease_expires_at": now,
                    "heartbeat_at": now,
                    "worker_id": "worker-a",
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "updated_at": now,
                    "progress": {
                        "total_item_count": 1,
                        "completed_item_count": 0,
                        "total_duration_seconds": 120.0,
                        "overall_completed_duration_seconds": 0.0,
                        "remaining_duration_seconds": 120.0,
                        "percent_complete": 0.0,
                        "progress_state": "continue",
                        "phase_label": "Encoding",
                        "fps": 12.0,
                        "speed": 0.5,
                    },
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "mixed-attention-shard",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "folder-mixed-parent",
                    "status": "needs_attention",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": [1],
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {"key": "remote-b", "label": "Remote B", "mode": "ssh"},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": "stale partial",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": "needs_attention",
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "created_at": now,
                    "started_at": now,
                    "finished_at": now,
                    "updated_at": now,
                    "progress": {
                        "total_item_count": 1,
                        "completed_item_count": 0,
                        "total_duration_seconds": 180.0,
                        "overall_completed_duration_seconds": 0.0,
                        "remaining_duration_seconds": 180.0,
                        "percent_complete": 0.0,
                        "progress_state": "needs_attention",
                        "phase_label": "Needs attention",
                    },
                },
            )
            parent = load_encode_job(connection, "folder-mixed-parent")
            assert parent is not None
            aggregated = encode_runtime.aggregate_encode_parent_job(
                connection,
                parent,
                web_app._encode_queue_runtime_deps(),
            )

        self.assertEqual(aggregated["status"], "running")
        self.assertEqual(aggregated["running_shard_count"], 1)
        self.assertEqual(aggregated["progress"]["progress_state"], "running")

    def test_aggregate_encode_parent_job_includes_all_quality_failures(self) -> None:
        manifest_path = self._write_manifest(
            "manifest-parent-quality-failures.json",
            [
                {"library_item_id": 1, "duration_seconds": 120.0, "source_size_bytes": 1000},
                {"library_item_id": 2, "duration_seconds": 180.0, "source_size_bytes": 2000},
            ],
        )
        now = web_app._now_iso()

        def job_payload(**overrides: object) -> dict[str, object]:
            payload: dict[str, object] = {
                "job_id": "folder-quality-failures",
                "prefix": "tv/show",
                "job_kind": "folder",
                "parent_job_id": None,
                "status": "running",
                "manifest_path": str(manifest_path),
                "manifest_indexes": None,
                "item_count": 2,
                "saved_profile_path": None,
                "host": {},
                "last_host": {},
                "notes": "",
                "bypass_schedule": False,
                "attempt_count": 0,
                "process_pid": None,
                "error": None,
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
                "created_at": now,
                "started_at": now,
                "finished_at": None,
                "updated_at": now,
            }
            payload.update(overrides)
            return payload

        first_analysis = {
            "kind": "quality_floor_too_strict",
            "retry_strategy": "needs_operator_approval",
            "auto_retry_allowed": False,
            "manifest_index": 0,
            "manifest_indexes": [0],
            "item_rel_path": "tv/show/e01.mkv",
            "requested_metric": "VMAF",
            "target_score": 88.0,
            "best_candidate": {"crf": 50.0, "metric": "VMAF", "score": 87.15, "predicted_encode_percent": 15.0},
            "summary": "First failed policy search.",
        }
        second_analysis = {
            "kind": "size_cap_too_strict",
            "retry_strategy": "needs_operator_approval",
            "auto_retry_allowed": False,
            "manifest_index": 1,
            "manifest_indexes": [1],
            "item_rel_path": "tv/show/e02.mkv",
            "requested_metric": "VMAF",
            "target_score": 88.0,
            "proposed_max_encoded_percent": 18,
            "best_candidate": {"crf": 48.0, "metric": "VMAF", "score": 87.12, "predicted_encode_percent": 17.0},
            "summary": "Second failed policy search.",
        }

        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(connection, job_payload())
            save_encode_job(
                connection,
                job_payload(
                    job_id="quality-failure-a",
                    job_kind="shard",
                    parent_job_id="folder-quality-failures",
                    status="needs_attention",
                    manifest_indexes=[0],
                    item_count=1,
                    error="Failed to find a suitable crf",
                    terminal_reason="deterministic",
                    last_failure_kind="deterministic",
                    progress={
                        "total_item_count": 1,
                        "completed_item_count": 0,
                        "total_duration_seconds": 120.0,
                        "overall_completed_duration_seconds": 0.0,
                        "failure_analysis": first_analysis,
                    },
                ),
            )
            save_encode_job(
                connection,
                job_payload(
                    job_id="quality-failure-b",
                    job_kind="shard",
                    parent_job_id="folder-quality-failures",
                    status="needs_attention",
                    manifest_indexes=[1],
                    item_count=1,
                    error="Failed to find a suitable crf",
                    terminal_reason="deterministic",
                    last_failure_kind="deterministic",
                    progress={
                        "total_item_count": 1,
                        "completed_item_count": 0,
                        "total_duration_seconds": 180.0,
                        "overall_completed_duration_seconds": 0.0,
                        "failure_analysis": second_analysis,
                    },
                ),
            )
            parent = load_encode_job(connection, "folder-quality-failures")
            assert parent is not None
            aggregated = encode_runtime.aggregate_encode_parent_job(
                connection,
                parent,
                web_app._encode_queue_runtime_deps(),
            )

        failure_analysis = object_dict(object_dict(aggregated["progress"]).get("failure_analysis"))
        self.assertEqual(failure_analysis["manifest_indexes"], [0, 1])
        self.assertEqual(len(object_list(failure_analysis["item_analyses"])), 2)
        self.assertIn("2 of 2 selected items", str(failure_analysis["summary"]))

    def test_approve_measured_encode_recovery_updates_policy_and_queues_failed_items(self) -> None:
        manifest_path = self._write_manifest(
            "manifest-recovery-approval.json",
            [
                {"library_item_id": 1, "source_path": "tv/show/e09.mkv"},
                {"library_item_id": 2, "source_path": "tv/show/e10.mkv"},
            ],
        )
        now = web_app._now_iso()
        failure_analysis = {
            "kind": "quality_floor_too_strict",
            "retry_strategy": "needs_operator_approval",
            "auto_retry_allowed": False,
            "manifest_indexes": [0, 1],
            "summary": "Measured policy analysis covers 2 of 2 selected items; operator approval is required.",
            "item_analyses": [
                {
                    "kind": "quality_floor_too_strict",
                    "manifest_index": 0,
                    "manifest_indexes": [0],
                    "item_rel_path": "tv/show/e09.mkv",
                    "requested_metric": "VMAF",
                    "target_score": 88.0,
                    "min_score": 86.5,
                    "max_encoded_percent": 15.0,
                    "best_candidate": {
                        "crf": 50.0,
                        "metric": "VMAF",
                        "score": 87.15,
                        "predicted_encode_percent": 15.0,
                    },
                },
                {
                    "kind": "size_cap_too_strict",
                    "manifest_index": 1,
                    "manifest_indexes": [1],
                    "item_rel_path": "tv/show/e10.mkv",
                    "requested_metric": "VMAF",
                    "target_score": 88.0,
                    "min_score": 86.5,
                    "max_encoded_percent": 15.0,
                    "proposed_max_encoded_percent": 18,
                    "best_candidate": {
                        "crf": 48.0,
                        "metric": "VMAF",
                        "score": 87.12,
                        "predicted_encode_percent": 17.0,
                    },
                },
            ],
        }
        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "terminal-recovery-parent",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "needs_attention",
                    "manifest_path": str(manifest_path),
                    "item_count": 2,
                    "saved_profile_path": None,
                    "manifest_indexes": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": "quality policy needs approval",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": "deterministic",
                    "last_failure_kind": "deterministic",
                    "last_failure_at": now,
                    "host_cooldown_until": None,
                    "created_at": now,
                    "started_at": now,
                    "finished_at": now,
                    "updated_at": now,
                    "progress": {"failure_analysis": failure_analysis},
                },
            )

        calibration_state = {
            "accepted_at": now,
            "policy": {
                "video": {
                    "quality_metric": "vmaf",
                    "target_vmaf": 88.0,
                    "min_target_vmaf": 86.5,
                    "max_encoded_percent": 15,
                    "max_crf": 46,
                }
            },
        }
        saved_calibrations: list[folder_actions_runtime.ActionPayload] = []
        saved_overrides: list[folder_actions_runtime.ActionPayload] = []
        queue_calls: list[tuple[str, str, bool]] = []

        def save_calibration(
                _config: MediaforceConfig,
                _prefix: str,
                payload: folder_actions_runtime.ActionPayload,
        ) -> None:
            saved_calibrations.append(payload)

        def upsert_override(
                _file_path: Path,
                _prefix: str,
                policy: folder_actions_runtime.ActionPayload,
        ) -> None:
            saved_overrides.append(policy)

        def queue_action(prefix: str, notes: str, bypass_schedule: bool) -> folder_actions_runtime.ActionPayload:
            queue_calls.append((prefix, notes, bypass_schedule))
            return {"ok": True, "message": "Recovered 2 failed files.", "recovered_item_count": 2}

        result = folder_actions_runtime.approve_measured_encode_recovery_action(
            self.config,
            "tv/show",
            now_iso=web_app._now_iso,
            load_calibration_state=lambda _config, _prefix: calibration_state,
            calibration_draft_hash=lambda _payload: "draft-hash",
            save_calibration_state=save_calibration,
            review_gate=self._accepted_review_gate,
            upsert_override=upsert_override,
            queue_folder_encode_action=queue_action,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "approved_measured_recovery")
        self.assertEqual(queue_calls[0][0], "tv/show")
        self.assertFalse(queue_calls[0][2])
        self.assertIn("Preserve completed staged encodes", queue_calls[0][1])
        saved_video = object_dict(object_dict(saved_calibrations[0]["policy"]).get("video"))
        self.assertEqual(saved_video["target_vmaf"], 87.0)
        self.assertEqual(saved_video["min_target_vmaf"], 86.5)
        self.assertEqual(saved_video["max_encoded_percent"], 18)
        self.assertEqual(saved_video["max_crf"], 50)
        self.assertEqual(object_dict(saved_overrides[0]).get("video"), saved_video)

    def test_queue_folder_encode_rejects_existing_active_encode_for_prefix(self) -> None:
        manifest_path = self._write_manifest("manifest-existing-active.json", [{"library_item_id": 1}])
        with open_db(self.config.paths.db_path) as connection:
            self._save_job(
                connection,
                job_id="active-existing",
                manifest_name=manifest_path.name,
                host={"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                status="queued",
                attempt_count=1,
            )

        with patch("mediaforce.web.runtime.folder_actions.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.folder_actions.create_folder_manifest",
                return_value=({"items": [{"library_item_id": 1}]}, manifest_path),
        ):
            result = folder_actions_runtime.queue_folder_encode_action(
                self.config,
                "tv/show",
                "",
                False,
                now_iso=web_app._now_iso,
                load_job_state=self._noop_load_job_state,
                load_calibration_state=self._accepted_calibration_state,
                review_gate=self._accepted_review_gate,
                upsert_override=self._noop_upsert_override,
                load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
                clear_terminal_encode_jobs_for_prefix_fn=clear_terminal_encode_jobs_for_prefix,
                prepare_terminal_encode_job_for_requeue_fn=self._prepare_terminal_encode_job_for_requeue,
                save_encode_job=save_encode_job,
            )

        self.assertFalse(result["ok"])
        self.assertIn("already queued", result["message"])
        with open_db(self.config.paths.db_path) as connection:
            row_count = int(
                connection.execute(
                    select(func.count()).select_from(encode_jobs).where(encode_jobs.c.prefix == "tv/show")
                ).scalar_one()
            )
        self.assertEqual(row_count, 1)

    def test_build_manifest_shards_creates_one_file_per_shard(self) -> None:
        manifest: folder_actions_runtime.ManifestPayload = {
            "items": [
                {"library_item_id": 1, "duration_seconds": 600.0, "source_size_bytes": 1000},
                {"library_item_id": 2, "duration_seconds": 400.0, "source_size_bytes": 900},
                {"library_item_id": 3, "duration_seconds": 200.0, "source_size_bytes": 800},
            ]
        }

        shards = folder_actions_runtime._build_manifest_shards(self.config, manifest)

        self.assertEqual(shards, [[0], [1], [2]])

    def test_validate_folder_outputs_action_summarizes_pass_and_fail_counts(self) -> None:
        class _LoadStagedItems(folder_actions_runtime.LoadFolderStagedItemsFn):
            def __call__(
                    self,
                    _connection: DBClient,
                    _config: MediaforceConfig,
                    _normalized_prefix: str,
                    *,
                    statuses: set[str],
            ) -> list[folder_actions_runtime.FolderItem]:
                self_test.assertEqual(statuses, {"encoded", "validated"})
                return [{"library_item_id": 1}, {"library_item_id": 2}]

        class _ValidateManifestItems(folder_actions_runtime.ValidateManifestItemsFn):
            def __call__(
                    self,
                    _connection: DBClient,
                    _config: MediaforceConfig,
                    _manifest: folder_actions_runtime.ManifestPayload,
                    indexes: list[int],
            ) -> list[folder_actions_runtime.ActionPayload]:
                return [{"passed": indexes[0] == 0}]

        self_test = self
        load_staged_items_fn = _LoadStagedItems()
        validate_manifest_items_fn = _ValidateManifestItems()

        result = folder_actions_runtime.validate_folder_outputs_action(
            self.config,
            "tv/show",
            load_folder_staged_items_fn=load_staged_items_fn,
            validate_manifest_items_fn=validate_manifest_items_fn,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["validated_count"], 1)
        self.assertEqual(result["failed_count"], 1)
        self.assertIn("1 passed, 1 failed", result["message"])

    def test_validate_folder_outputs_action_rejects_while_folder_encode_active(self) -> None:
        class _SingleStagedItem(folder_actions_runtime.LoadFolderStagedItemsFn):
            def __call__(
                    self,
                    _connection: DBClient,
                    _config: MediaforceConfig,
                    _normalized_prefix: str,
                    *,
                    statuses: set[str],
            ) -> list[folder_actions_runtime.FolderItem]:
                self_test.assertEqual(statuses, {"encoded", "validated"})
                return [{"library_item_id": 1}]

        class _PassedValidation(folder_actions_runtime.ValidateManifestItemsFn):
            def __call__(
                    self,
                    _connection: DBClient,
                    _config: MediaforceConfig,
                    _manifest: folder_actions_runtime.ManifestPayload,
                    indexes: list[int],
            ) -> list[folder_actions_runtime.ActionPayload]:
                self_test.assertEqual(indexes, [0])
                return [{"passed": True}]

        self_test = self
        load_folder_staged_items_fn = _SingleStagedItem()
        validate_manifest_items_fn = _PassedValidation()

        for status in ("queued", "running", "retry_backoff"):
            prefix = f"tv/show-{status}"
            with open_db(self.config.paths.db_path) as connection:
                save_encode_job(
                    connection,
                    {
                        "job_id": f"active-validate-{status}",
                        "prefix": prefix,
                        "job_kind": "folder",
                        "parent_job_id": None,
                        "status": status,
                        "manifest_path": str(self._write_manifest("active-validate.json", [])),
                        "manifest_indexes": None,
                        "item_count": 1,
                        "saved_profile_path": None,
                        "host": {},
                        "last_host": {},
                        "notes": "",
                        "bypass_schedule": False,
                        "attempt_count": 1,
                        "process_pid": None,
                        "error": None,
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
                        "created_at": web_app._now_iso(),
                        "started_at": None,
                        "finished_at": None,
                        "updated_at": web_app._now_iso(),
                    },
                )

            result = folder_actions_runtime.validate_folder_outputs_action(
                self.config,
                prefix,
                load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
                load_folder_staged_items_fn=load_folder_staged_items_fn,
                validate_manifest_items_fn=validate_manifest_items_fn,
            )

            self.assertFalse(result["ok"])
            self.assertIn(f"folder encode is {status.replace('_', ' ')}", result["message"])

    def test_promote_folder_outputs_action_requires_validated_items(self) -> None:
        class _NoStagedItems(folder_actions_runtime.LoadFolderStagedItemsFn):
            def __call__(
                    self,
                    _connection: DBClient,
                    _config: MediaforceConfig,
                    _normalized_prefix: str,
                    *,
                    statuses: set[str],
            ) -> list[folder_actions_runtime.FolderItem]:
                self_test.assertEqual(statuses, {"validated"})
                return []

        class _PromoteManifestItems(folder_actions_runtime.PromoteManifestItemsFn):
            def __call__(
                    self,
                    _connection: DBClient,
                    _config: MediaforceConfig,
                    _manifest: folder_actions_runtime.ManifestPayload,
                    indexes: list[int],
                    *,
                    force: bool,
            ) -> list[Path]:
                self_test.assertEqual(indexes, [])
                self_test.assertFalse(force)
                return []

        self_test = self
        load_folder_staged_items_fn = _NoStagedItems()
        promote_manifest_items_fn = _PromoteManifestItems()

        result = folder_actions_runtime.promote_folder_outputs_action(
            self.config,
            "tv/show",
            load_folder_staged_items_fn=load_folder_staged_items_fn,
            promote_manifest_items_fn=promote_manifest_items_fn,
        )

        self.assertFalse(result["ok"])
        self.assertIn("No validated staged files", result["message"])

    def test_promote_folder_outputs_action_rejects_while_folder_encode_active(self) -> None:
        class _SingleValidatedItem(folder_actions_runtime.LoadFolderStagedItemsFn):
            def __call__(
                    self,
                    _connection: DBClient,
                    _config: MediaforceConfig,
                    _normalized_prefix: str,
                    *,
                    statuses: set[str],
            ) -> list[folder_actions_runtime.FolderItem]:
                self_test.assertEqual(statuses, {"validated"})
                return [{"library_item_id": 1}]

        class _PromoteManifestItems(folder_actions_runtime.PromoteManifestItemsFn):
            def __call__(
                    self,
                    _connection: DBClient,
                    _config: MediaforceConfig,
                    _manifest: folder_actions_runtime.ManifestPayload,
                    indexes: list[int],
                    *,
                    force: bool,
            ) -> list[Path]:
                self_test.assertEqual(indexes, [0])
                self_test.assertFalse(force)
                return []

        self_test = self
        load_folder_staged_items_fn = _SingleValidatedItem()
        promote_manifest_items_fn = _PromoteManifestItems()

        for status in ("queued", "running", "retry_backoff"):
            prefix = f"tv/show-{status}-promote"
            with open_db(self.config.paths.db_path) as connection:
                save_encode_job(
                    connection,
                    {
                        "job_id": f"active-promote-{status}",
                        "prefix": prefix,
                        "job_kind": "folder",
                        "parent_job_id": None,
                        "status": status,
                        "manifest_path": str(self._write_manifest("active-promote.json", [])),
                        "manifest_indexes": None,
                        "item_count": 1,
                        "saved_profile_path": None,
                        "host": {},
                        "last_host": {},
                        "notes": "",
                        "bypass_schedule": False,
                        "attempt_count": 1,
                        "process_pid": None,
                        "error": None,
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
                        "created_at": web_app._now_iso(),
                        "started_at": None,
                        "finished_at": None,
                        "updated_at": web_app._now_iso(),
                    },
                )

            result = folder_actions_runtime.promote_folder_outputs_action(
                self.config,
                prefix,
                load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
                load_folder_staged_items_fn=load_folder_staged_items_fn,
                promote_manifest_items_fn=promote_manifest_items_fn,
            )

            self.assertFalse(result["ok"])
            self.assertIn(f"folder encode is {status.replace('_', ' ')}", result["message"])

    def test_load_folder_staged_items_ignores_promoted_rows_and_filters_statuses(self) -> None:
        encoded_source = self._create_source_file("episode-encoded.mkv")
        validated_source = self._create_source_file("episode-validated.mkv")
        promoted_source = self._create_source_file("episode-promoted.mkv")

        encoded_stage = self._staging_path("episode-encoded.mp4")
        validated_stage = self._staging_path("episode-validated.mp4")
        promoted_stage = self._staging_path("episode-promoted.mp4")
        encoded_stage.parent.mkdir(parents=True, exist_ok=True)
        encoded_stage.write_text("encoded")
        validated_stage.write_text("validated")
        promoted_stage.write_text("promoted")

        with open_db(self.config.paths.db_path) as connection:
            encoded_id = self._insert_library_item(connection, encoded_source, status="encoded")
            validated_id = self._insert_library_item(connection, validated_source, status="validated")
            promoted_id = self._insert_library_item(connection, promoted_source, status="promoted")
            self._insert_staged_artifact(connection, encoded_id, encoded_stage)
            self._insert_staged_artifact(connection, validated_id, validated_stage)
            self._insert_staged_artifact(connection, promoted_id, promoted_stage)
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == promoted_id)
                .values(promoted_at=web_app._now_iso())
            )

            with patch(
                    "mediaforce.web.app.build_manifest_item",
                    side_effect=lambda row, _config: {"library_item_id": row["id"]},
            ):
                items = web_app._load_folder_staged_items(
                    connection,
                    self.config,
                    "tv/show",
                    statuses={"encoded", "validated"},
                )

        self.assertEqual([item["library_item_id"] for item in items], [encoded_id, validated_id])

    def test_folder_status_payload_polls_while_encode_is_active(self) -> None:
        manifest_path = self._write_manifest("manifest-folder-status-active.json", [{"library_item_id": 1}])
        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "folder-status-active",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": None,
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": None,
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
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": None,
                    "updated_at": web_app._now_iso(),
                },
            )

        payload = dashboard_payloads.folder_status_payload(
            self.config,
            "tv/show",
            load_job_state=web_app._load_job_state,
            load_retryable_sample_job_state=web_app._load_retryable_sample_job_state,
            load_scan_job_state=web_app._load_scan_job_state,
            load_active_encode_job_for_prefix=load_active_encode_job_for_prefix,
        )

        self.assertTrue(payload["polling_active"])

    def test_queue_folder_encode_recovers_failed_files_into_active_parent(self) -> None:
        source_a = self._create_source_file("recover-active-a.mkv")
        source_b = self._create_source_file("recover-active-b.mkv")
        source_c = self._create_source_file("recover-active-c.mkv")
        staging_a = self._staging_path("recover-active-a.mkv")
        staging_b = self._staging_path("recover-active-b.mkv")
        partial_a = staging_a.with_name(f"{staging_a.stem}.partial{staging_a.suffix}")
        partial_b = staging_b.with_name(f"{staging_b.stem}.partial{staging_b.suffix}")
        staging_a.parent.mkdir(parents=True, exist_ok=True)
        staging_a.write_text("stale")
        staging_b.write_text("stale")
        partial_a.write_text("partial")
        partial_b.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            item_a = self._insert_library_item(connection, source_a, status="encoding")
            item_b = self._insert_library_item(connection, source_b, status="encoding")
            item_c = self._insert_library_item(connection, source_c, status="encoding")
            manifest_path = self._write_manifest(
                "manifest-active-recovery.json",
                [
                    {"library_item_id": item_a, "staging_path": str(staging_a)},
                    {"library_item_id": item_b, "staging_path": str(staging_b)},
                    {"library_item_id": item_c, "staging_path": str(self._staging_path("recover-active-c.mkv"))},
                ],
            )
            now = web_app._now_iso()
            save_encode_job(
                connection,
                {
                    "job_id": "active-parent-folder",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": None,
                    "item_count": 3,
                    "saved_profile_path": None,
                    "host": {},
                    "last_host": {},
                    "notes": "existing note",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": None,
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
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "updated_at": now,
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "failed-active-shard",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "active-parent-folder",
                    "status": "needs_attention",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": [0, 1],
                    "item_count": 2,
                    "saved_profile_path": None,
                    "host": {"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 3,
                    "process_pid": None,
                    "error": "resource busy",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": "max_attempts_exhausted",
                    "last_failure_kind": "ssh_transport",
                    "last_failure_at": now,
                    "host_cooldown_until": None,
                    "created_at": now,
                    "started_at": now,
                    "finished_at": now,
                    "updated_at": now,
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "running-active-shard",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "active-parent-folder",
                    "status": "running",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": [2],
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {"key": "remote-b", "label": "Remote B", "mode": "ssh"},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 2,
                    "process_pid": 123,
                    "error": None,
                    "leased_at": now,
                    "lease_expires_at": now,
                    "heartbeat_at": now,
                    "worker_id": "worker-1",
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "updated_at": now,
                    "progress": {
                        "total_item_count": 1,
                        "completed_item_count": 0,
                        "total_duration_seconds": 120.0,
                        "overall_completed_duration_seconds": 0.0,
                        "remaining_duration_seconds": 120.0,
                        "percent_complete": 0.0,
                        "progress_state": "continue",
                        "speed": 0.5,
                        "fps": 12.0,
                    },
                },
            )

        result = folder_actions_runtime.queue_folder_encode_action(
            self.config,
            "tv/show",
            "recover the active folder",
            False,
            now_iso=web_app._now_iso,
            load_job_state=self._noop_load_job_state,
            load_calibration_state=self._accepted_calibration_state,
            review_gate=self._accepted_review_gate,
            upsert_override=self._noop_upsert_override,
            load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
            clear_terminal_encode_jobs_for_prefix_fn=clear_terminal_encode_jobs_for_prefix,
            prepare_terminal_encode_job_for_requeue_fn=self._prepare_terminal_encode_job_for_requeue,
            save_encode_job=save_encode_job,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "recovered")
        self.assertEqual(result["recovered_item_count"], 2)
        self.assertFalse(staging_a.exists())
        self.assertFalse(staging_b.exists())
        self.assertFalse(partial_a.exists())
        self.assertFalse(partial_b.exists())
        with open_db(self.config.paths.db_path) as connection:
            failed_child = load_encode_job(connection, "failed-active-shard")
            children = list_child_encode_jobs(connection, "active-parent-folder")
            item_a_row = self._library_item_value(connection, item_a, library_items.c.status)
            item_b_row = self._library_item_value(connection, item_b, library_items.c.status)
            item_c_row = self._library_item_value(connection, item_c, library_items.c.status)

        self.assertIsNone(failed_child)
        queued_indexes = sorted(
            tuple(cast(list[int], child["manifest_indexes"]))
            for child in children
            if child["job_id"] != "running-active-shard"
        )
        self.assertEqual(queued_indexes, [(0,), (1,)])
        self.assertEqual(item_a_row["status"], "planned")
        self.assertEqual(item_b_row["status"], "planned")
        self.assertEqual(item_c_row["status"], "encoding")

    def test_queue_folder_encode_replaces_previous_terminal_history_for_prefix(self) -> None:
        manifest_path = self._write_manifest("manifest-requeue.json", [{"library_item_id": 1}])
        with open_db(self.config.paths.db_path) as connection:
            self._save_job(
                connection,
                job_id="stale-terminal",
                manifest_name=manifest_path.name,
                host={"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                status="needs_attention",
                attempt_count=3,
            )

        with patch("mediaforce.web.runtime.folder_actions.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.folder_actions.create_folder_manifest",
                return_value=({"items": [{"library_item_id": 1}]}, manifest_path),
        ):
            result = folder_actions_runtime.queue_folder_encode_action(
                self.config,
                "tv/show",
                "retry after blocker",
                False,
                now_iso=web_app._now_iso,
                load_job_state=self._noop_load_job_state,
                load_calibration_state=self._accepted_calibration_state,
                review_gate=self._accepted_review_gate,
                upsert_override=self._noop_upsert_override,
                load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
                clear_terminal_encode_jobs_for_prefix_fn=clear_terminal_encode_jobs_for_prefix,
                prepare_terminal_encode_job_for_requeue_fn=self._prepare_terminal_encode_job_for_requeue,
                save_encode_job=save_encode_job,
            )

        self.assertTrue(result["ok"])
        new_job_id = str(result["job"]["job_id"])
        with open_db(self.config.paths.db_path) as connection:
            stale_job = load_encode_job(connection, "stale-terminal")
            new_job = load_encode_job(connection, new_job_id)
            prefix_rows = connection.execute(
                select(encode_jobs.c.job_id, encode_jobs.c.job_kind, encode_jobs.c.status)
                .where(encode_jobs.c.prefix == "tv/show")
                .order_by(encode_jobs.c.created_at.asc(), encode_jobs.c.job_id.asc())
            ).mappings().fetchall()
        self.assertIsNone(stale_job)
        self.assertIsNotNone(new_job)
        self.assertEqual({str(row["job_kind"]) for row in prefix_rows}, {"folder", "shard"})
        self.assertEqual({str(row["status"]) for row in prefix_rows}, {"queued"})

    def test_load_latest_terminal_encode_job_for_prefix_prefers_latest_row_when_timestamps_tie(self) -> None:
        shared_created_at = "2026-04-09T12:00:00+00:00"
        manifest_path = self._write_manifest("manifest-terminal-tie.json", [{"library_item_id": 1}])

        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "older-terminal",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "failed",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": None,
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": None,
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
                    "created_at": shared_created_at,
                    "started_at": None,
                    "finished_at": shared_created_at,
                    "updated_at": shared_created_at,
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "newer-terminal",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "needs_attention",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": None,
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": None,
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
                    "created_at": shared_created_at,
                    "started_at": None,
                    "finished_at": shared_created_at,
                    "updated_at": shared_created_at,
                },
            )

            latest_job = load_latest_terminal_encode_job_for_prefix(connection, "tv/show")

        self.assertIsNotNone(latest_job)
        assert latest_job is not None
        self.assertEqual(latest_job["job_id"], "newer-terminal")

    def test_clear_terminal_encode_jobs_for_prefix_preserves_completed_shards(self) -> None:
        shared_created_at = "2026-04-09T12:00:00+00:00"
        manifest_path = self._write_manifest("manifest-terminal-shards.json", [{"library_item_id": 1}])

        with open_db(self.config.paths.db_path) as connection:
            save_encode_job(
                connection,
                {
                    "job_id": "terminal-folder",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "failed",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": None,
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": None,
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
                    "created_at": shared_created_at,
                    "started_at": None,
                    "finished_at": shared_created_at,
                    "updated_at": shared_created_at,
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "completed-shard",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "terminal-folder",
                    "status": "completed",
                    "manifest_path": str(manifest_path),
                    "manifest_indexes": [0],
                    "item_count": 1,
                    "saved_profile_path": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": None,
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
                    "created_at": shared_created_at,
                    "started_at": None,
                    "finished_at": shared_created_at,
                    "updated_at": shared_created_at,
                },
            )
            for job_id, status in (("queued-stale-shard", "queued"), ("running-stale-shard", "running")):
                save_encode_job(
                    connection,
                    {
                        "job_id": job_id,
                        "prefix": "tv/show",
                        "job_kind": "shard",
                        "parent_job_id": "terminal-folder",
                        "status": status,
                        "manifest_path": str(manifest_path),
                        "manifest_indexes": [0],
                        "item_count": 1,
                        "saved_profile_path": None,
                        "host": {},
                        "last_host": {},
                        "notes": "",
                        "bypass_schedule": False,
                        "attempt_count": 1,
                        "process_pid": None,
                        "error": None,
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
                        "created_at": shared_created_at,
                        "started_at": None,
                        "finished_at": None,
                        "updated_at": shared_created_at,
                    },
                )

            clear_terminal_encode_jobs_for_prefix(connection, "tv/show")

            remaining_job_ids = {
                str(row["job_id"])
                for row in connection.execute(
                    select(encode_jobs.c.job_id).where(encode_jobs.c.prefix == "tv/show")
                ).mappings().fetchall()
            }

        self.assertEqual(remaining_job_ids, {"completed-shard"})

    def test_queue_folder_encode_retry_resets_stale_encoding_items_before_manifest(self) -> None:
        source_a = self._create_source_file("retry-a.mkv")
        source_b = self._create_source_file("retry-b.mkv")
        staging_a = self._staging_path("retry-a.mkv")
        partial_a = staging_a.with_name(f"{staging_a.stem}.partial{staging_a.suffix}")
        staging_a.parent.mkdir(parents=True, exist_ok=True)
        partial_a.write_text("partial")
        terminal_manifest = self._write_manifest(
            "manifest-terminal-retry.json",
            [
                {"library_item_id": 1, "staging_path": str(staging_a)},
                {"library_item_id": 2, "staging_path": str(self._staging_path("retry-b.mkv"))},
            ],
        )

        with open_db(self.config.paths.db_path) as connection:
            item_a = self._insert_library_item(connection, source_a, status="encoding")
            item_b = self._insert_library_item(connection, source_b)
            terminal_manifest.write_text(
                json.dumps(
                    {
                        "items": [
                            {"library_item_id": item_a, "staging_path": str(staging_a)},
                            {"library_item_id": item_b, "staging_path": str(self._staging_path("retry-b.mkv"))},
                        ]
                    }
                )
            )
            self._save_job(
                connection,
                job_id="stale-retry-terminal",
                manifest_name=terminal_manifest.name,
                host={"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                status="needs_attention",
                attempt_count=3,
            )

        next_manifest = self._write_manifest(
            "manifest-new-retry.json",
            [{"library_item_id": item_a}, {"library_item_id": item_b}],
        )

        def create_manifest_stub(
                db_connection: DBClient,
                _config: MediaforceConfig,
                *,
                prefix: str,
        ) -> tuple[folder_actions_runtime.ManifestPayload, Path]:
            self.assertEqual(prefix, "tv/show")
            pending_ids = {
                int(row["id"])
                for row in db_connection.execute(
                    select(library_items.c.id)
                    .where(library_items.c.parent_dir == "tv/show")
                    .where(library_items.c.status.in_(("discovered", "planned", "validated")))
                ).mappings().fetchall()
            }
            self.assertEqual(pending_ids, {item_a, item_b})
            manifest_payload: folder_actions_runtime.ManifestPayload = {
                "items": [{"library_item_id": item_a}, {"library_item_id": item_b}],
            }
            return manifest_payload, next_manifest

        with patch("mediaforce.web.runtime.folder_actions.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.folder_actions.create_folder_manifest",
                side_effect=create_manifest_stub,
        ):
            result = folder_actions_runtime.queue_folder_encode_action(
                self.config,
                "tv/show",
                "retry interrupted folder",
                False,
                now_iso=web_app._now_iso,
                load_job_state=self._noop_load_job_state,
                load_calibration_state=self._accepted_calibration_state,
                review_gate=self._accepted_review_gate,
                upsert_override=self._noop_upsert_override,
                load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
                clear_terminal_encode_jobs_for_prefix_fn=clear_terminal_encode_jobs_for_prefix,
                prepare_terminal_encode_job_for_requeue_fn=self._prepare_terminal_encode_job_for_requeue,
                save_encode_job=save_encode_job,
            )

        self.assertTrue(result["ok"])
        self.assertFalse(partial_a.exists())
        manifest_path = Path(str(result["job"]["manifest_path"]))
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(len(manifest["items"]), 2)
        with open_db(self.config.paths.db_path) as connection:
            item_a_row = self._library_item_value(connection, item_a, library_items.c.status)
            item_b_row = self._library_item_value(connection, item_b, library_items.c.status)
        self.assertEqual(item_a_row["status"], "planned")
        self.assertEqual(item_b_row["status"], "planned")

    def test_queue_folder_encode_retry_resets_stale_prefix_items_missing_from_terminal_manifest(self) -> None:
        source_a = self._create_source_file("retry-missing-a.mkv")
        source_b = self._create_source_file("retry-missing-b.mkv")
        staging_a = self._staging_path("retry-missing-a.mkv")
        partial_a = staging_a.with_name(f"{staging_a.stem}.partial{staging_a.suffix}")
        staging_a.parent.mkdir(parents=True, exist_ok=True)
        partial_a.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            item_a = self._insert_library_item(connection, source_a, status="encoding")
            item_b = self._insert_library_item(connection, source_b)
            terminal_manifest = self._write_manifest(
                "manifest-terminal-missing-retry.json",
                [{"library_item_id": item_b, "staging_path": str(self._staging_path("retry-missing-b.mkv"))}],
            )
            save_encode_job(
                connection,
                {
                    "job_id": "stale-missing-folder",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "needs_attention",
                    "manifest_path": str(terminal_manifest),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 3,
                    "process_pid": None,
                    "error": "worker restart",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": "max_attempts_exhausted",
                    "last_failure_kind": "worker_restart",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": None,
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": web_app._now_iso(),
                    "updated_at": web_app._now_iso(),
                },
            )

        next_manifest = self._write_manifest(
            "manifest-next-missing-retry.json",
            [{"library_item_id": item_a}, {"library_item_id": item_b}],
        )

        def create_manifest_stub(
                db_connection: DBClient,
                _config: MediaforceConfig,
                *,
                prefix: str,
        ) -> tuple[folder_actions_runtime.ManifestPayload, Path]:
            self.assertEqual(prefix, "tv/show")
            pending_ids = {
                int(row["id"])
                for row in db_connection.execute(
                    select(library_items.c.id)
                    .where(library_items.c.parent_dir == "tv/show")
                    .where(library_items.c.status.in_(("discovered", "planned", "validated")))
                ).mappings().fetchall()
            }
            self.assertEqual(pending_ids, {item_a, item_b})
            manifest_payload: folder_actions_runtime.ManifestPayload = {
                "items": [{"library_item_id": item_a}, {"library_item_id": item_b}],
            }
            return manifest_payload, next_manifest

        with patch("mediaforce.web.runtime.folder_actions.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.folder_actions.create_folder_manifest",
                side_effect=create_manifest_stub,
        ):
            result = folder_actions_runtime.queue_folder_encode_action(
                self.config,
                "tv/show",
                "retry interrupted folder",
                False,
                now_iso=web_app._now_iso,
                load_job_state=self._noop_load_job_state,
                load_calibration_state=self._accepted_calibration_state,
                review_gate=self._accepted_review_gate,
                upsert_override=self._noop_upsert_override,
                load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
                clear_terminal_encode_jobs_for_prefix_fn=clear_terminal_encode_jobs_for_prefix,
                prepare_terminal_encode_job_for_requeue_fn=self._prepare_terminal_encode_job_for_requeue,
                save_encode_job=save_encode_job,
            )

        self.assertTrue(result["ok"])
        self.assertFalse(partial_a.exists())
        manifest_path = Path(str(result["job"]["manifest_path"]))
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(len(manifest["items"]), 2)
        with open_db(self.config.paths.db_path) as connection:
            item_a_row = self._library_item_value(connection, item_a, library_items.c.status)
            item_b_row = self._library_item_value(connection, item_b, library_items.c.status)
        self.assertEqual(item_a_row["status"], "planned")
        self.assertEqual(item_b_row["status"], "planned")

    def test_queue_folder_encode_retry_resets_stale_nested_prefix_items(self) -> None:
        source = self._create_source_file("retry-nested.mkv")
        other_source = self._create_source_file("retry-nested-other.mkv")
        staging_path = self.root / "staging" / "tv" / "show" / "season-1" / "retry-nested.mkv"
        partial_path = staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}")
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("stale")
        partial_path.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source, status="encoding")
            other_item_id = self._insert_library_item(connection, other_source)
            connection.execute(
                update(library_items)
                .where(library_items.c.id == item_id)
                .values(
                    parent_dir="tv/show/season-1",
                    rel_path="tv/show/season-1/retry-nested.mkv",
                )
            )
            terminal_manifest = self._write_manifest(
                "manifest-terminal-nested-retry.json",
                [{"library_item_id": other_item_id}],
            )
            save_encode_job(
                connection,
                {
                    "job_id": "stale-nested-folder",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "needs_attention",
                    "manifest_path": str(terminal_manifest),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 3,
                    "process_pid": None,
                    "error": "worker restart",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": "max_attempts_exhausted",
                    "last_failure_kind": "worker_restart",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": None,
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": web_app._now_iso(),
                    "updated_at": web_app._now_iso(),
                },
            )

        next_manifest = self._write_manifest(
            "manifest-next-nested-retry.json",
            [{"library_item_id": item_id}, {"library_item_id": other_item_id}],
        )

        with patch("mediaforce.web.runtime.folder_actions.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.folder_actions.create_folder_manifest",
                return_value=({"items": [{"library_item_id": item_id}, {"library_item_id": other_item_id}]},
                              next_manifest),
        ):
            result = folder_actions_runtime.queue_folder_encode_action(
                self.config,
                "tv/show",
                "retry nested folder",
                False,
                now_iso=web_app._now_iso,
                load_job_state=self._noop_load_job_state,
                load_calibration_state=self._accepted_calibration_state,
                review_gate=self._accepted_review_gate,
                upsert_override=self._noop_upsert_override,
                load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
                clear_terminal_encode_jobs_for_prefix_fn=clear_terminal_encode_jobs_for_prefix,
                prepare_terminal_encode_job_for_requeue_fn=self._prepare_terminal_encode_job_for_requeue,
                save_encode_job=save_encode_job,
            )

        self.assertTrue(result["ok"])
        self.assertFalse(staging_path.exists())
        self.assertFalse(partial_path.exists())
        with open_db(self.config.paths.db_path) as connection:
            item_row = self._library_item_value(connection, item_id, library_items.c.status)
        self.assertEqual(item_row["status"], "planned")

    def test_queue_folder_encode_retry_does_not_reset_active_descendant_prefix_items(self) -> None:
        source = self._create_source_file("retry-active-nested.mkv")
        other_source = self._create_source_file("retry-active-parent.mkv")
        staging_path = self.root / "staging" / "tv" / "show" / "season-1" / "retry-active-nested.mkv"
        partial_path = staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}")
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("active")
        partial_path.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            nested_item_id = self._insert_library_item(connection, source, status="encoding")
            parent_item_id = self._insert_library_item(connection, other_source)
            connection.execute(
                update(library_items)
                .where(library_items.c.id == nested_item_id)
                .values(
                    parent_dir="tv/show/season-1",
                    rel_path="tv/show/season-1/retry-active-nested.mkv",
                )
            )
            terminal_manifest = self._write_manifest(
                "manifest-terminal-active-descendant-retry.json",
                [{"library_item_id": parent_item_id}],
            )
            save_encode_job(
                connection,
                {
                    "job_id": "stale-parent-folder",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "needs_attention",
                    "manifest_path": str(terminal_manifest),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 3,
                    "process_pid": None,
                    "error": "worker restart",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": "max_attempts_exhausted",
                    "last_failure_kind": "worker_restart",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": None,
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": web_app._now_iso(),
                    "updated_at": web_app._now_iso(),
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "active-descendant-folder",
                    "prefix": "tv/show/season-1",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "running",
                    "manifest_path": str(
                        self._write_manifest("manifest-active-descendant.json", [{"library_item_id": nested_item_id}])),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": 123,
                    "error": None,
                    "leased_at": web_app._now_iso(),
                    "lease_expires_at": None,
                    "heartbeat_at": web_app._now_iso(),
                    "worker_id": "worker-1",
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": None,
                    "updated_at": web_app._now_iso(),
                },
            )

        next_manifest = self._write_manifest("manifest-next-parent-retry.json", [{"library_item_id": parent_item_id}])

        with patch("mediaforce.web.runtime.folder_actions.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.folder_actions.create_folder_manifest",
                return_value=({"items": [{"library_item_id": parent_item_id}]}, next_manifest),
        ):
            result = folder_actions_runtime.queue_folder_encode_action(
                self.config,
                "tv/show",
                "retry parent folder",
                False,
                now_iso=web_app._now_iso,
                load_job_state=self._noop_load_job_state,
                load_calibration_state=self._accepted_calibration_state,
                review_gate=self._accepted_review_gate,
                upsert_override=self._noop_upsert_override,
                load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
                clear_terminal_encode_jobs_for_prefix_fn=clear_terminal_encode_jobs_for_prefix,
                prepare_terminal_encode_job_for_requeue_fn=self._prepare_terminal_encode_job_for_requeue,
                save_encode_job=save_encode_job,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(staging_path.exists())
        self.assertTrue(partial_path.exists())
        with open_db(self.config.paths.db_path) as connection:
            nested_item_row = self._library_item_value(connection, nested_item_id, library_items.c.status)
            parent_item_row = self._library_item_value(connection, parent_item_id, library_items.c.status)
        self.assertEqual(nested_item_row["status"], "encoding")
        self.assertEqual(parent_item_row["status"], "planned")

    def test_queue_folder_encode_retry_root_prefix_does_not_reset_unrelated_encoding_items(self) -> None:
        source = self._create_source_file("retry-root-scope.mkv")
        other_source = self._create_source_file("retry-root-other.mkv")
        staging_path = self._staging_path("retry-root-scope.mkv")
        partial_path = staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}")
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("stale")
        partial_path.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source, status="encoding")
            other_item_id = self._insert_library_item(connection, other_source)
            connection.execute(
                update(library_items)
                .where(library_items.c.id == item_id)
                .values(parent_dir="tv/show", rel_path="tv/show/retry-root-scope.mkv")
            )
            terminal_manifest = self._write_manifest(
                "manifest-terminal-root-retry.json",
                [{"library_item_id": other_item_id}],
            )
            save_encode_job(
                connection,
                {
                    "job_id": "stale-root-folder",
                    "prefix": "",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "needs_attention",
                    "manifest_path": str(terminal_manifest),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 3,
                    "process_pid": None,
                    "error": "worker restart",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": "max_attempts_exhausted",
                    "last_failure_kind": "worker_restart",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": None,
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": web_app._now_iso(),
                    "updated_at": web_app._now_iso(),
                },
            )

        next_manifest = self._write_manifest(
            "manifest-next-root-retry.json",
            [{"library_item_id": item_id}, {"library_item_id": other_item_id}],
        )

        with patch("mediaforce.web.runtime.folder_actions.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.folder_actions.create_folder_manifest",
                return_value=({"items": [{"library_item_id": item_id}, {"library_item_id": other_item_id}]},
                              next_manifest),
        ):
            result = folder_actions_runtime.queue_folder_encode_action(
                self.config,
                "",
                "retry root folder",
                False,
                now_iso=web_app._now_iso,
                load_job_state=self._noop_load_job_state,
                load_calibration_state=self._accepted_calibration_state,
                review_gate=self._accepted_review_gate,
                upsert_override=self._noop_upsert_override,
                load_active_encode_job_for_prefix_fn=load_active_encode_job_for_prefix,
                clear_terminal_encode_jobs_for_prefix_fn=clear_terminal_encode_jobs_for_prefix,
                prepare_terminal_encode_job_for_requeue_fn=self._prepare_terminal_encode_job_for_requeue,
                save_encode_job=save_encode_job,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(staging_path.exists())
        self.assertTrue(partial_path.exists())
        with open_db(self.config.paths.db_path) as connection:
            item_row = self._library_item_value(connection, item_id, library_items.c.status)
        self.assertEqual(item_row["status"], "encoding")

    def test_prepare_terminal_encode_job_for_requeue_preserves_completed_shard_items(self) -> None:
        source_a = self._create_source_file("completed-a.mkv")
        source_b = self._create_source_file("failed-b.mkv")
        staging_a = self._staging_path("completed-a.mkv")
        staging_b = self._staging_path("failed-b.mkv")
        partial_b = staging_b.with_name(f"{staging_b.stem}.partial{staging_b.suffix}")
        staging_a.parent.mkdir(parents=True, exist_ok=True)
        staging_a.write_text("encoded")
        partial_b.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            item_a = self._insert_library_item(connection, source_a, status="encoded")
            item_b = self._insert_library_item(connection, source_b, status="encoding")
            self._insert_staged_artifact(connection, item_a, staging_a)
            manifest = self._write_manifest(
                "manifest-mixed-retry.json",
                [
                    {"library_item_id": item_a, "staging_path": str(staging_a)},
                    {"library_item_id": item_b, "staging_path": str(staging_b)},
                ],
            )
            save_encode_job(
                connection,
                {
                    "job_id": "parent-folder",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "needs_attention",
                    "manifest_path": str(manifest),
                    "item_count": 2,
                    "saved_profile_path": None,
                    "manifest_indexes": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 0,
                    "process_pid": None,
                    "error": "mixed shard result",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": "max_attempts_exhausted",
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": web_app._now_iso(),
                    "updated_at": web_app._now_iso(),
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "child-complete",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "parent-folder",
                    "status": "completed",
                    "manifest_path": str(manifest),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": [0],
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": None,
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
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": web_app._now_iso(),
                    "updated_at": web_app._now_iso(),
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "child-failed",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "parent-folder",
                    "status": "needs_attention",
                    "manifest_path": str(manifest),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": [1],
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 3,
                    "process_pid": None,
                    "error": "worker restart",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": "max_attempts_exhausted",
                    "last_failure_kind": "worker_restart",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": None,
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": web_app._now_iso(),
                    "updated_at": web_app._now_iso(),
                },
            )

            parent_job = load_encode_job(connection, "parent-folder")
            self.assertIsNotNone(parent_job)
            assert parent_job is not None
            encode_runtime.prepare_terminal_encode_job_for_requeue(
                connection,
                parent_job,
                deps=web_app._encode_queue_runtime_deps(),
            )

            item_a_row = self._library_item_value(connection, item_a, library_items.c.status)
            item_b_row = self._library_item_value(connection, item_b, library_items.c.status)

        self.assertTrue(staging_a.exists())
        self.assertFalse(partial_b.exists())
        self.assertEqual(item_a_row["status"], "encoded")
        self.assertEqual(item_b_row["status"], "planned")

    def test_prepare_terminal_encode_job_for_requeue_handles_missing_child_indexes(self) -> None:
        source_a = self._create_source_file("legacy-a.mkv")
        source_b = self._create_source_file("legacy-b.mkv")
        staging_a = self._staging_path("legacy-a.mkv")
        staging_b = self._staging_path("legacy-b.mkv")
        partial_b = staging_b.with_name(f"{staging_b.stem}.partial{staging_b.suffix}")
        staging_a.parent.mkdir(parents=True, exist_ok=True)
        staging_a.write_text("encoded")
        staging_b.write_text("failed")
        partial_b.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            item_a = self._insert_library_item(connection, source_a, status="encoded")
            item_b = self._insert_library_item(connection, source_b, status="encoding")
            self._insert_staged_artifact(connection, item_a, staging_a)
            manifest = self._write_manifest(
                "manifest-legacy-retry.json",
                [
                    {"library_item_id": item_a, "staging_path": str(staging_a)},
                    {"library_item_id": item_b, "staging_path": str(staging_b)},
                ],
            )
            save_encode_job(
                connection,
                {
                    "job_id": "legacy-parent-folder",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "needs_attention",
                    "manifest_path": str(manifest),
                    "item_count": 2,
                    "saved_profile_path": None,
                    "manifest_indexes": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 0,
                    "process_pid": None,
                    "error": "legacy terminal state",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": "max_attempts_exhausted",
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": web_app._now_iso(),
                    "updated_at": web_app._now_iso(),
                },
            )
            save_encode_job(
                connection,
                {
                    "job_id": "legacy-child-malformed",
                    "prefix": "tv/show",
                    "job_kind": "shard",
                    "parent_job_id": "legacy-parent-folder",
                    "status": "needs_attention",
                    "manifest_path": str(manifest),
                    "item_count": 1,
                    "saved_profile_path": None,
                    "manifest_indexes": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 1,
                    "process_pid": None,
                    "error": "worker restart",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": "max_attempts_exhausted",
                    "last_failure_kind": "worker_restart",
                    "last_failure_at": web_app._now_iso(),
                    "host_cooldown_until": None,
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": web_app._now_iso(),
                    "updated_at": web_app._now_iso(),
                },
            )

            parent_job = load_encode_job(connection, "legacy-parent-folder")
            self.assertIsNotNone(parent_job)
            assert parent_job is not None
            encode_runtime.prepare_terminal_encode_job_for_requeue(
                connection,
                parent_job,
                deps=web_app._encode_queue_runtime_deps(),
            )

            item_a_row = self._library_item_value(connection, item_a, library_items.c.status)
            item_b_row = self._library_item_value(connection, item_b, library_items.c.status)

        self.assertFalse(staging_a.exists())
        self.assertFalse(staging_b.exists())
        self.assertFalse(partial_b.exists())
        self.assertEqual(item_a_row["status"], "planned")
        self.assertEqual(item_b_row["status"], "planned")

    def test_prepare_terminal_encode_job_for_requeue_handles_no_child_jobs(self) -> None:
        source_a = self._create_source_file("orphan-a.mkv")
        source_b = self._create_source_file("orphan-b.mkv")
        staging_a = self._staging_path("orphan-a.mkv")
        staging_b = self._staging_path("orphan-b.mkv")
        partial_a = staging_a.with_name(f"{staging_a.stem}.partial{staging_a.suffix}")
        partial_b = staging_b.with_name(f"{staging_b.stem}.partial{staging_b.suffix}")
        staging_a.parent.mkdir(parents=True, exist_ok=True)
        staging_a.write_text("encoded")
        staging_b.write_text("encoded")
        partial_a.write_text("partial")
        partial_b.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            item_a = self._insert_library_item(connection, source_a, status="encoded")
            item_b = self._insert_library_item(connection, source_b, status="encoding")
            self._insert_staged_artifact(connection, item_a, staging_a)
            manifest = self._write_manifest(
                "manifest-orphan-retry.json",
                [
                    {"library_item_id": item_a, "staging_path": str(staging_a)},
                    {"library_item_id": item_b, "staging_path": str(staging_b)},
                ],
            )
            save_encode_job(
                connection,
                {
                    "job_id": "orphan-parent-folder",
                    "prefix": "tv/show",
                    "job_kind": "folder",
                    "parent_job_id": None,
                    "status": "failed",
                    "manifest_path": str(manifest),
                    "item_count": 2,
                    "saved_profile_path": None,
                    "manifest_indexes": None,
                    "host": {},
                    "last_host": {},
                    "notes": "",
                    "bypass_schedule": False,
                    "attempt_count": 0,
                    "process_pid": None,
                    "error": "legacy folder terminal",
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": "max_attempts_exhausted",
                    "last_failure_kind": None,
                    "last_failure_at": None,
                    "host_cooldown_until": None,
                    "created_at": web_app._now_iso(),
                    "started_at": web_app._now_iso(),
                    "finished_at": web_app._now_iso(),
                    "updated_at": web_app._now_iso(),
                },
            )

            parent_job = load_encode_job(connection, "orphan-parent-folder")
            self.assertIsNotNone(parent_job)
            assert parent_job is not None
            encode_runtime.prepare_terminal_encode_job_for_requeue(
                connection,
                parent_job,
                deps=web_app._encode_queue_runtime_deps(),
            )

            item_a_row = self._library_item_value(connection, item_a, library_items.c.status)
            item_b_row = self._library_item_value(connection, item_b, library_items.c.status)

        self.assertFalse(staging_a.exists())
        self.assertFalse(staging_b.exists())
        self.assertFalse(partial_a.exists())
        self.assertFalse(partial_b.exists())
        self.assertEqual(item_a_row["status"], "planned")
        self.assertEqual(item_b_row["status"], "planned")

    def test_cleanup_encode_retry_artifacts_only_removes_selected_indexes(self) -> None:
        source_a = self._create_source_file("cleanup-a.mkv")
        source_b = self._create_source_file("cleanup-b.mkv")
        staging_a = self._staging_path("cleanup-a.mkv")
        staging_b = self._staging_path("cleanup-b.mkv")
        staging_a.parent.mkdir(parents=True, exist_ok=True)
        staging_b.parent.mkdir(parents=True, exist_ok=True)
        staging_a.write_text("a")
        staging_b.write_text("b")
        self._write_manifest(
            "manifest-cleanup.json",
            [
                {"library_item_id": 1, "staging_path": str(staging_a)},
                {"library_item_id": 2, "staging_path": str(staging_b)},
            ],
        )
        deps = web_app._encode_queue_runtime_deps()

        with open_db(self.config.paths.db_path) as connection:
            item_a = self._insert_library_item(connection, source_a, status="encoding")
            item_b = self._insert_library_item(connection, source_b, status="encoding")
            connection.execute(update(library_items).where(library_items.c.id == item_a).values(status="encoding"))
            connection.execute(update(library_items).where(library_items.c.id == item_b).values(status="encoding"))
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == item_a)
                .values(library_item_id=item_a, staging_path=str(staging_a), updated_at=web_app._now_iso())
            )
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == item_b)
                .values(library_item_id=item_b, staging_path=str(staging_b), updated_at=web_app._now_iso())
            )

        cleanup_manifest = self._write_manifest(
            "manifest-cleanup-indexed.json",
            [
                {"library_item_id": item_a, "staging_path": str(staging_a)},
                {"library_item_id": item_b, "staging_path": str(staging_b)},
            ],
        )

        with open_db(self.config.paths.db_path) as connection:
            encode_runtime._cleanup_encode_retry_artifacts(connection, manifest_path=cleanup_manifest, indexes=[0],
                                                           deps=deps)
            item_a_row = self._library_item_value(connection, item_a, library_items.c.status)
            item_b_row = self._library_item_value(connection, item_b, library_items.c.status)

        self.assertFalse(staging_a.exists())
        self.assertTrue(staging_b.exists())
        self.assertEqual(item_a_row["status"], "planned")
        self.assertEqual(item_b_row["status"], "encoding")

    def test_default_config_path_points_to_repo_config_defaults(self) -> None:
        self.assertEqual(
            web_app.DEFAULT_CONFIG_PATH.resolve(),
            (Path(__file__).resolve().parents[1] / "config" / "defaults.toml").resolve(),
        )

    def test_stop_calibration_queue_cancels_running_jobs_and_cleans_queued_jobs(self) -> None:
        running_prefix = "tv/show"
        queued_prefix = "tv/queued"
        now = web_app._now_iso()
        with open_db(self.config.paths.db_path) as connection:
            web_app._save_job_state(
                connection,
                self.config,
                running_prefix,
                {
                    "job_id": "cal-running",
                    "prefix": running_prefix,
                    "status": "running",
                    "lane": "sample",
                    "action": "baseline",
                    "host": {"key": "cbusillo@localhost", "label": "M4 Studio"},
                    "notes": "",
                    "policy": {},
                    "sample_item": {},
                    "owner_pid": os.getpid(),
                    "created_at": now,
                    "started_at": now,
                    "finished_at": None,
                    "error": None,
                },
            )
            web_app._save_job_state(
                connection,
                self.config,
                queued_prefix,
                {
                    "job_id": "cal-queued",
                    "prefix": queued_prefix,
                    "status": "queued",
                    "lane": "sample",
                    "action": "baseline",
                    "host": {"key": "cbusillo@localhost", "label": "M4 Studio"},
                    "notes": "",
                    "policy": {},
                    "sample_item": {},
                    "owner_pid": None,
                    "created_at": now,
                    "started_at": None,
                    "finished_at": None,
                    "error": None,
                },
            )
            connection.commit()

        controller = Mock()
        web_app.CALIBRATION_QUEUE_PROCESSES["cal-running"] = controller
        try:
            with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                    "mediaforce.web.app.purge_transient_artifacts"
            ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
                "mediaforce.web.app._start_encode_queue_worker"
            ):
                app = web_app.create_app(self.config.paths.config_path)

            stop_endpoint = next(
                route.endpoint
                for route in app.router.routes
                if getattr(route, "path", "") == "/api/calibration-queue/stop"
            )
            response = stop_endpoint()
            self.assertEqual(response.status_code, 200)
            self.assertIn("Stopped and cleaned the calibration queue", response.body.decode())

            with open_db(self.config.paths.db_path) as connection:
                running_job = web_app._load_job_state(connection, self.config, running_prefix)
                queued_job = web_app._load_job_state(connection, self.config, queued_prefix)

            self.assertIsNotNone(running_job)
            self.assertIsNotNone(queued_job)
            assert running_job is not None
            assert queued_job is not None
            self.assertEqual(running_job["status"], "stopped")
            self.assertEqual(queued_job["status"], "stopped")
            self.assertEqual(running_job["error"], "Calibration queue job was stopped and cleaned up.")
            self.assertEqual(queued_job["error"], "Calibration queue job was stopped and cleaned up.")
            controller.cancel.assert_called_once_with()
        finally:
            web_app.CALIBRATION_QUEUE_PROCESSES.clear()

    def test_calibration_queue_summary_surfaces_recent_failed_jobs(self) -> None:
        now = web_app._now_iso()
        with open_db(self.config.paths.db_path) as connection:
            for job_id, prefix, lane, status in (
                    ("sample-failed", "tv/sample-failed", "sample", "failed"),
                    ("proof-stopped", "tv/proof-stopped", "full", "stopped"),
                    ("sample-running", "tv/sample-running", "sample", "running"),
            ):
                web_app._save_job_state(
                    connection,
                    self.config,
                    prefix,
                    {
                        "job_id": job_id,
                        "prefix": prefix,
                        "status": status,
                        "lane": lane,
                        "action": "baseline" if lane == "sample" else "full",
                        "host": {"key": "cbusillo@localhost", "label": "M4 Studio"},
                        "notes": "",
                        "policy": {},
                        "sample_item": {},
                        "owner_pid": None,
                        "created_at": now,
                        "started_at": now,
                        "finished_at": now if status in {"failed", "stopped"} else None,
                        "error": f"{job_id} failed detail" if status == "failed" else "Stopped by operator.",
                    },
                )
            connection.commit()

            summary = list_queue_summary(connection)

        self.assertEqual(summary["active_count"], 1)
        self.assertEqual(summary["recent_failed_count"], 2)
        self.assertEqual(summary["sample"]["recent_failed_count"], 1)
        self.assertEqual(summary["full"]["recent_failed_count"], 1)
        self.assertEqual(summary["sample"]["recent_failed"][0]["prefix"], "tv/sample-failed")
        self.assertEqual(summary["full"]["recent_failed"][0]["prefix"], "tv/proof-stopped")

    def test_calibration_queue_summary_limits_recent_failed_jobs_per_lane(self) -> None:
        now = web_app._now_iso()
        with open_db(self.config.paths.db_path) as connection:
            for index in range(4):
                prefix = f"tv/sample-failed-{index}"
                web_app._save_job_state(
                    connection,
                    self.config,
                    prefix,
                    {
                        "job_id": f"sample-failed-{index}",
                        "prefix": prefix,
                        "status": "failed",
                        "lane": "sample",
                        "action": "baseline",
                        "host": {"key": "cbusillo@localhost", "label": "M4 Studio"},
                        "notes": "",
                        "policy": {},
                        "sample_item": {},
                        "owner_pid": None,
                        "created_at": now,
                        "started_at": now,
                        "finished_at": now,
                        "error": "sample failed detail",
                    },
                )
            web_app._save_job_state(
                connection,
                self.config,
                "tv/proof-failed",
                {
                    "job_id": "proof-failed",
                    "prefix": "tv/proof-failed",
                    "status": "failed",
                    "lane": "full",
                    "action": "full",
                    "host": {"key": "cbusillo@localhost", "label": "M4 Studio"},
                    "notes": "",
                    "policy": {},
                    "sample_item": {},
                    "owner_pid": None,
                    "created_at": now,
                    "started_at": now,
                    "finished_at": now,
                    "error": "proof failed detail",
                },
            )
            connection.commit()

            summary = list_queue_summary(connection, limit_per_lane=2)

        self.assertEqual(summary["sample"]["recent_failed_count"], 4)
        self.assertEqual(summary["full"]["recent_failed_count"], 1)
        self.assertEqual(len(summary["sample"]["recent_failed"]), 2)
        self.assertEqual(summary["full"]["recent_failed"][0]["prefix"], "tv/proof-failed")

    def test_calibration_queue_summary_deduplicates_failed_prefixes_before_limit(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            for job_id, prefix, updated_at in (
                    ("sample-duplicate-newer", "tv/duplicate", "2026-05-05T12:03:00+00:00"),
                    ("sample-duplicate-older", "tv/duplicate", "2026-05-05T12:02:00+00:00"),
                    ("sample-distinct", "tv/distinct", "2026-05-05T12:01:00+00:00"),
            ):
                web_app._save_job_state(
                    connection,
                    self.config,
                    prefix,
                    {
                        "job_id": job_id,
                        "prefix": prefix,
                        "status": "failed",
                        "lane": "sample",
                        "action": "baseline",
                        "host": {"key": "cbusillo@localhost", "label": "M4 Studio"},
                        "notes": "",
                        "policy": {},
                        "sample_item": {},
                        "owner_pid": None,
                        "created_at": updated_at,
                        "started_at": updated_at,
                        "finished_at": updated_at,
                        "error": "sample failed detail",
                    },
                )
                connection.execute(
                    calibration_jobs.update()
                    .where(calibration_jobs.c.job_id == job_id)
                    .values(updated_at=updated_at)
                )
            connection.commit()

            summary = list_queue_summary(connection, limit_per_lane=2)

        self.assertEqual(summary["sample"]["recent_failed_count"], 2)
        self.assertEqual(
            [job["prefix"] for job in summary["sample"]["recent_failed"]],
            ["tv/duplicate", "tv/distinct"],
        )

    def test_calibration_queue_summary_counts_recent_failures_beyond_display_limit(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            for index in range(13):
                updated_at = f"2026-05-05T12:{index:02d}:00+00:00"
                prefix = f"tv/count-failed-{index}"
                web_app._save_job_state(
                    connection,
                    self.config,
                    prefix,
                    {
                        "job_id": f"count-failed-{index}",
                        "prefix": prefix,
                        "status": "failed",
                        "lane": "sample",
                        "action": "baseline",
                        "host": {"key": "cbusillo@localhost", "label": "M4 Studio"},
                        "notes": "",
                        "policy": {},
                        "sample_item": {},
                        "owner_pid": None,
                        "created_at": updated_at,
                        "started_at": updated_at,
                        "finished_at": updated_at,
                        "error": "sample failed detail",
                    },
                )
                connection.execute(
                    calibration_jobs.update()
                    .where(calibration_jobs.c.job_id == f"count-failed-{index}")
                    .values(updated_at=updated_at)
                )
            connection.commit()

            summary = list_queue_summary(connection, limit_per_lane=2)

        self.assertEqual(summary["sample"]["recent_failed_count"], 13)
        self.assertEqual(summary["recent_failed_count"], 13)
        self.assertEqual(len(summary["sample"]["recent_failed"]), 2)

    def test_run_remote_status_probe_retries_timeout_once(self) -> None:
        host: dict[str, object] = {"host": "cbusillo@chris-mini.local"}
        timeout_exc = subprocess.TimeoutExpired(cmd=["ssh"], timeout=8)
        success = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="ok", stderr="")
        with patch(
                "mediaforce.remote._run_remote_ssh",
                side_effect=[timeout_exc, success],
        ) as run_remote_ssh_mock, patch("mediaforce.remote.time.sleep") as sleep_mock:
            result = remote._run_remote_status_probe(host, "echo ok", timeout=8)
        self.assertEqual(result, success)
        self.assertEqual(run_remote_ssh_mock.call_count, 2)
        sleep_mock.assert_called_once_with(remote.REMOTE_STATUS_RETRY_DELAY_SECONDS)

    def test_run_remote_status_probe_does_not_retry_permission_denied(self) -> None:
        host: dict[str, object] = {"host": "cbusillo@chris-mini.local"}
        denied = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=255,
            stdout="",
            stderr="Permission denied (publickey,password).",
        )
        with patch("mediaforce.remote._run_remote_ssh", return_value=denied) as run_remote_ssh_mock, patch(
                "mediaforce.remote.time.sleep"
        ) as sleep_mock:
            result = remote._run_remote_status_probe(host, "echo ok", timeout=8)
        self.assertEqual(result, denied)
        run_remote_ssh_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_classify_ssh_failure_for_new_alias_trust_prompt(self) -> None:
        classification = remote._classify_ssh_failure(
            "The authenticity of host 'chris-mini.local' can't be established. "
            "This host key is known by the following other names/addresses: chris-mini.shiny. "
            "Are you sure you want to continue connecting (yes/no/[fingerprint])?"
        )
        self.assertEqual(classification["message"], "SSH trust needs confirmation")
        self.assertFalse(classification["setup_supported"])
        self.assertFalse(classification["trust_reset_supported"])

    def test_select_encode_host_respects_parallel_limit(self) -> None:
        statuses = [
            {
                "key": "cbusillo@localhost",
                "label": "Chris-Studio",
                "priority": 100,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 1,
                "max_parallel_encodes": 1,
                "queue_active": False,
            }
        ]
        with open_db(self.config.paths.db_path) as connection:
            with patch("mediaforce.web.app._host_runtime_rows", return_value=statuses):
                host_payload, waiting_reason = web_app._select_encode_host(connection, self.config,
                                                                           {"bypass_schedule": False})
        self.assertIsNone(host_payload)
        self.assertEqual(waiting_reason, "waiting for host capacity to free up")

    def test_load_job_state_preserves_old_stopped_sample_for_retry(self) -> None:
        prefix = "tv/show/season-1"
        stale_finished_at = (
                    datetime.now(tz=UTC) - web_app.CALIBRATION_JOB_NOTICE_AFTER - timedelta(minutes=5)).isoformat()
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                calibration_jobs.insert().values(
                    job_id="stopped-sample-job",
                    prefix=prefix,
                    status="stopped",
                    lane="sample",
                    action="ai_tune",
                    host_json=json.dumps({"key": "cbusillo@localhost", "label": "M4 Studio"}, sort_keys=True),
                    notes="retry me later",
                    policy_json=json.dumps({"video": {"target_vmaf": 88.0}}, sort_keys=True),
                    sample_item_json=json.dumps({"rel_path": "tv/show/season-1/episode.mkv"}, sort_keys=True),
                    created_at=stale_finished_at,
                    started_at=stale_finished_at,
                    finished_at=stale_finished_at,
                    updated_at=stale_finished_at,
                    error="Calibration queue job was stopped and cleaned up.",
                )
            )

            payload = web_app._load_job_state(connection, self.config, prefix)

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["status"], "stopped")
        self.assertEqual(payload["action"], "ai_tune")

    def test_load_retryable_sample_job_state_ignores_newer_full_job(self) -> None:
        prefix = "tv/show/season-1-retry"
        stale_finished_at = (
                    datetime.now(tz=UTC) - web_app.CALIBRATION_JOB_NOTICE_AFTER - timedelta(minutes=5)).isoformat()
        newer_finished_at = (datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat()
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                calibration_jobs.insert().values(
                    job_id="stopped-sample-job",
                    prefix=prefix,
                    status="stopped",
                    lane="sample",
                    action="ai_tune",
                    host_json=json.dumps({"key": "cbusillo@localhost", "label": "M4 Studio"}, sort_keys=True),
                    notes="retry me later",
                    policy_json=json.dumps({"video": {"target_vmaf": 88.0}}, sort_keys=True),
                    sample_item_json=json.dumps({"rel_path": "tv/show/season-1/episode.mkv"}, sort_keys=True),
                    created_at=stale_finished_at,
                    started_at=stale_finished_at,
                    finished_at=stale_finished_at,
                    updated_at=stale_finished_at,
                    error="Calibration queue job was stopped and cleaned up.",
                )
            )
            connection.execute(
                calibration_jobs.insert().values(
                    job_id="stopped-full-job",
                    prefix=prefix,
                    status="stopped",
                    lane="full",
                    action="full",
                    host_json=json.dumps({"key": "cbusillo@localhost", "label": "M4 Studio"}, sort_keys=True),
                    notes="newer full proof run",
                    policy_json=json.dumps({"video": {"target_vmaf": 90.0}}, sort_keys=True),
                    sample_item_json=json.dumps({"rel_path": "tv/show/season-1/episode.mkv"}, sort_keys=True),
                    created_at=newer_finished_at,
                    started_at=newer_finished_at,
                    finished_at=newer_finished_at,
                    updated_at=newer_finished_at,
                    error="Proof run was stopped.",
                )
            )

            latest_payload = web_app._load_job_state(connection, self.config, prefix)
            retryable_payload = web_app._load_retryable_sample_job_state(connection, self.config, prefix)

        self.assertIsNotNone(latest_payload)
        assert latest_payload is not None
        self.assertEqual(latest_payload["job_id"], "stopped-full-job")
        self.assertIsNotNone(retryable_payload)
        assert retryable_payload is not None
        self.assertEqual(retryable_payload["job_id"], "stopped-sample-job")
        self.assertEqual(retryable_payload["lane"], "sample")

    def test_load_retryable_sample_job_state_hides_stopped_sample_after_newer_sample_run(self) -> None:
        prefix = "tv/show/season-1-retry-newer-sample"
        stale_finished_at = (
                    datetime.now(tz=UTC) - web_app.CALIBRATION_JOB_NOTICE_AFTER - timedelta(minutes=5)).isoformat()
        newer_finished_at = (datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat()
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                calibration_jobs.insert().values(
                    job_id="stopped-sample-job",
                    prefix=prefix,
                    status="stopped",
                    lane="sample",
                    action="ai_tune",
                    host_json=json.dumps({"key": "cbusillo@localhost", "label": "M4 Studio"}, sort_keys=True),
                    notes="retry me later",
                    policy_json=json.dumps({"video": {"target_vmaf": 88.0}}, sort_keys=True),
                    sample_item_json=json.dumps({"rel_path": "tv/show/season-1/episode.mkv"}, sort_keys=True),
                    created_at=stale_finished_at,
                    started_at=stale_finished_at,
                    finished_at=stale_finished_at,
                    updated_at=stale_finished_at,
                    error="Calibration queue job was stopped and cleaned up.",
                )
            )
            connection.execute(
                calibration_jobs.insert().values(
                    job_id="completed-sample-job",
                    prefix=prefix,
                    status="completed",
                    lane="sample",
                    action="ai_tune",
                    host_json=json.dumps({"key": "cbusillo@localhost", "label": "M4 Studio"}, sort_keys=True),
                    notes="newer completed sample",
                    policy_json=json.dumps({"video": {"target_vmaf": 90.0}}, sort_keys=True),
                    sample_item_json=json.dumps({"rel_path": "tv/show/season-1/episode.mkv"}, sort_keys=True),
                    created_at=newer_finished_at,
                    started_at=newer_finished_at,
                    finished_at=newer_finished_at,
                    updated_at=newer_finished_at,
                    error=None,
                )
            )

            latest_payload = web_app._load_job_state(connection, self.config, prefix)
            retryable_payload = web_app._load_retryable_sample_job_state(connection, self.config, prefix)

        self.assertIsNotNone(latest_payload)
        assert latest_payload is not None
        self.assertEqual(latest_payload["job_id"], "completed-sample-job")
        self.assertIsNone(retryable_payload)

    def test_load_latest_failed_sample_job_state_keeps_context_after_newer_sample_run(self) -> None:
        prefix = "tv/show/season-1-bench-failure-context"
        older_finished_at = (
                    datetime.now(tz=UTC) - web_app.CALIBRATION_JOB_NOTICE_AFTER - timedelta(minutes=5)).isoformat()
        newer_finished_at = (datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat()
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                calibration_jobs.insert().values(
                    job_id="failed-sample-job",
                    prefix=prefix,
                    status="failed",
                    lane="sample",
                    action="baseline",
                    host_json=json.dumps({"key": "cbusillo@localhost", "label": "M4 Studio"}, sort_keys=True),
                    notes="what can we do about the failure?",
                    policy_json=json.dumps({"video": {"target_vmaf": 97.0}}, sort_keys=True),
                    sample_item_json=json.dumps({"rel_path": "tv/show/season-1/episode.mkv"}, sort_keys=True),
                    created_at=older_finished_at,
                    started_at=older_finished_at,
                    finished_at=older_finished_at,
                    updated_at=older_finished_at,
                    error="Error: Failed to find a suitable crf",
                )
            )
            connection.execute(
                calibration_jobs.insert().values(
                    job_id="completed-sample-job",
                    prefix=prefix,
                    status="completed",
                    lane="sample",
                    action="ai_tune",
                    host_json=json.dumps({"key": "cbusillo@localhost", "label": "M4 Studio"}, sort_keys=True),
                    notes="newer completed sample",
                    policy_json=json.dumps({"video": {"target_vmaf": 90.0}}, sort_keys=True),
                    sample_item_json=json.dumps({"rel_path": "tv/show/season-1/episode.mkv"}, sort_keys=True),
                    created_at=newer_finished_at,
                    started_at=newer_finished_at,
                    finished_at=newer_finished_at,
                    updated_at=newer_finished_at,
                    error=None,
                )
            )

            retryable_payload = web_app._load_retryable_sample_job_state(connection, self.config, prefix)
            failed_payload = web_app._load_latest_failed_sample_job_state(connection, self.config, prefix)

        self.assertIsNone(retryable_payload)
        self.assertIsNotNone(failed_payload)
        assert failed_payload is not None
        self.assertEqual(failed_payload["job_id"], "failed-sample-job")
        self.assertEqual(failed_payload["error"], "Error: Failed to find a suitable crf")

    def test_load_job_state_still_hides_old_stopped_full_job_notice(self) -> None:
        prefix = "tv/show/season-2"
        stale_finished_at = (
                    datetime.now(tz=UTC) - web_app.CALIBRATION_JOB_NOTICE_AFTER - timedelta(minutes=5)).isoformat()
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                calibration_jobs.insert().values(
                    job_id="stopped-full-job",
                    prefix=prefix,
                    status="stopped",
                    lane="full",
                    action="full",
                    host_json=json.dumps({"key": "cbusillo@localhost", "label": "M4 Studio"}, sort_keys=True),
                    notes="old full run",
                    policy_json=json.dumps({"video": {"target_vmaf": 88.0}}, sort_keys=True),
                    sample_item_json=json.dumps({"rel_path": "tv/show/season-2/episode.mkv"}, sort_keys=True),
                    created_at=stale_finished_at,
                    started_at=stale_finished_at,
                    finished_at=stale_finished_at,
                    updated_at=stale_finished_at,
                    error="Calibration queue job was stopped and cleaned up.",
                )
            )

            payload = web_app._load_job_state(connection, self.config, prefix)

        self.assertIsNone(payload)

    def _build_config(self) -> MediaforceConfig:
        paths = ConfigPaths(
            project_root=self.root,
            config_path=self.root / "config.toml",
            db_path=self.root / "library.sqlite3",
            run_manifest_dir=self.root / "runs",
            web_state_dir=self.root / "web",
            review_dir=self.root / "review",
            runtime_settings_path=self.root / "runtime.json",
        )
        raw = {
            "state": {"cleanup": {"transient_artifact_retention_days": 14}},
            "media": {
                "source_roots": {"tv": str(self.root / "source" / "tv")},
                "staging_root": str(self.root / "staging"),
                "archive_root": str(self.root / "archive"),
            },
            "remote_hosts": [],
            "encode_queue": {
                "scheduler": {
                    "mode": "anytime",
                    "start_hour": 22,
                    "end_hour": 8,
                    "timezone": "local",
                }
            },
        }
        return MediaforceConfig(raw=raw, paths=paths)

    def _create_source_file(self, name: str) -> Path:
        path = self.root / "source" / "tv" / "show" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source")
        return path

    def _staging_path(self, name: str) -> Path:
        return self.root / "staging" / "tv" / "show" / name

    @staticmethod
    def _insert_library_item(connection: DBClient, source_path: Path, *,
                             status: str = "planned") -> int:
        now = web_app._now_iso()
        result = connection.execute(
            library_items.insert().values(
                source_path=str(source_path),
                rel_path=f"tv/show/{source_path.name}",
                media_root="tv",
                parent_dir="tv/show",
                file_name=source_path.name,
                container=".mkv",
                size_bytes=1024,
                mtime_ns=1,
                fingerprint=f"fingerprint-{source_path.name}",
                duration_seconds=60.0,
                video_codec="h264",
                audio_summary_json="[]",
                subtitle_summary_json="[]",
                last_scan_id="scan-1",
                discovered_at=now,
                last_seen_at=now,
                updated_at=now,
                status=status,
            )
        )
        return int(result.inserted_primary_key[0])

    @staticmethod
    def _insert_staged_artifact(connection: DBClient, library_item_id: int, staging_path: Path) -> None:
        connection.execute(
            staged_artifacts.insert().values(
                library_item_id=library_item_id,
                staging_path=str(staging_path),
                updated_at=web_app._now_iso(),
            )
        )

    @staticmethod
    def _insert_scan_run(connection: DBClient, **values: object) -> None:
        connection.execute(scan_runs.insert().values(**values))

    @staticmethod
    def _library_item_value(connection: DBClient, item_id: int, *columns: Any) -> Mapping[str, object] | None:
        row = connection.execute(
            select(*columns).where(library_items.c.id == item_id)
        ).mappings().fetchone()
        return row

    @staticmethod
    def _staged_artifact_value(connection: DBClient, item_id: int, *columns: Any) -> Mapping[str, object] | None:
        row = connection.execute(
            select(*columns).where(staged_artifacts.c.library_item_id == item_id)
        ).mappings().fetchone()
        return row

    @staticmethod
    def _item_event_rows(connection: DBClient, item_id: int) -> list[dict[str, object]]:
        return [
            {str(key): value for key, value in dict(row).items()}
            for row in connection.execute(
                select(item_events.c.event_type, item_events.c.details_json)
                .where(item_events.c.library_item_id == item_id)
                .order_by(item_events.c.id.asc())
            ).mappings().fetchall()
        ]

    def _write_manifest(self, name: str, items: list[dict[str, object]]) -> Path:
        path = self.root / "runs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"items": items}))
        return path

    def _save_job(
            self,
            connection: DBClient,
            *,
            job_id: str,
            manifest_name: str,
            host: dict[str, object],
            status: str,
            attempt_count: int,
            lease_expires_at: str | None = None,
            retry_not_before: str | None = None,
            waiting_reason: str | None = None,
    ) -> None:
        manifest_path = self.root / "runs" / manifest_name
        now = web_app._now_iso()
        save_encode_job(
            connection,
            {
                "job_id": job_id,
                "prefix": "tv/show",
                "status": status,
                "manifest_path": str(manifest_path),
                "item_count": 1,
                "saved_profile_path": None,
                "host": host,
                "last_host": {},
                "notes": "",
                "bypass_schedule": False,
                "attempt_count": attempt_count,
                "process_pid": 111 if status == "running" else None,
                "error": None,
                "leased_at": now if status == "running" else None,
                "lease_expires_at": lease_expires_at,
                "heartbeat_at": now if status == "running" else None,
                "worker_id": "test-worker" if status == "running" else None,
                "retry_not_before": retry_not_before,
                "waiting_reason": waiting_reason,
                "terminal_reason": None,
                "last_failure_kind": None,
                "last_failure_at": None,
                "host_cooldown_until": None,
                "created_at": now,
                "started_at": now if status == "running" else None,
                "finished_at": None,
                "updated_at": now,
            },
        )


if __name__ == "__main__":
    unittest.main()
