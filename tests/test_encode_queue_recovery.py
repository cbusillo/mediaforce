import json
import io
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

from mediaforce import execution, quality, remote, review
from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.core.models import ProbeSummary
from mediaforce.encoding.encode_queue import load_encode_job, load_queue_state, save_encode_job, save_queue_state
from mediaforce.encoding.quality import QualitySearchResult, SampleEncodeResult
from mediaforce.remote import HostStatus
from mediaforce.review import BrowserReviewClip, CompareClip, EncodedPreviewClip
from mediaforce.web import app as web_app


class EncodeQueueRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self._build_config()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

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
            stage_row = connection.execute(
                "SELECT promoted_at FROM staged_artifacts WHERE library_item_id = ?",
                (item_id,),
            ).fetchone()
            self.assertIsNone(stage_row)
            item_status = connection.execute(
                "SELECT status FROM library_items WHERE id = ?",
                (item_id,),
            ).fetchone()[0]
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
            selected_host, waiting_reason = web_app._select_encode_host(None, self.config, job)
        self.assertIsNotNone(selected_host)
        assert selected_host is not None
        self.assertEqual(selected_host["key"], "ct103")
        self.assertIsNone(waiting_reason)

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
            host_payload, waiting_reason = web_app._select_encode_host(None, self.config, job)
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
            host_payload, waiting_reason = web_app._select_encode_host(None, self.config, {"bypass_schedule": False})
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

    def test_full_scan_becomes_stale_when_library_roots_change(self) -> None:
        source_path = self._create_source_file("episode-a.mkv")

        with open_db(self.config.paths.db_path) as connection:
            self._insert_library_item(connection, source_path)
            now = web_app._now_iso()
            connection.execute(
                """
                INSERT INTO scan_runs(scan_id, started_at, completed_at, roots_json, scope, prefixes_json,
                                      file_count, reprobed_count, unchanged_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("scan-1", now, now, json.dumps(self.config.raw["media"]["source_roots"]), "full", None, 1, 0, 0),
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
            connection.execute(
                """
                INSERT INTO scan_runs(scan_id, started_at, completed_at, roots_json, scope, prefixes_json,
                                      file_count, reprobed_count, unchanged_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "scan-older-than-threshold",
                    completed_at,
                    completed_at,
                    json.dumps(self.config.raw["media"]["source_roots"]),
                    "full",
                    None,
                    1,
                    0,
                    0,
                ),
            )

            web_app._save_catalog_signature(self.config)

            self.assertTrue(web_app._scan_is_stale(connection, self.config, prefix=None))

    def test_orphaned_scan_run_is_expired_before_rescheduling(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            now = web_app._now_iso()
            connection.execute(
                """
                INSERT INTO scan_runs(scan_id, started_at, completed_at, owner_pid, roots_json, scope, prefixes_json,
                                      file_count, reprobed_count, unchanged_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "scan-stale",
                    now,
                    None,
                    None,
                    json.dumps(self.config.raw["media"]["source_roots"]),
                    "full",
                    None,
                    0,
                    0,
                    0,
                ),
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
                "SELECT completed_at FROM scan_runs WHERE scan_id = ?",
                ("scan-stale",),
            ).fetchone()
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
                    "UPDATE library_items SET size_bytes = ?, rel_path = ?, parent_dir = ? WHERE id = ?",
                    (2 * 1024 * 1024 * 1024, f"tv/show/Season 1/item-{item_id}.mkv", "tv/show/Season 1", item_id),
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

        payload = {"mode": "sample", "job_id": "sample-2",
                   "preview_clips": [{"path": "/review-media/run/item-00/encoded-01.mp4"}], "compare_clips": [],
                   "review_media_ready": True, "accepted_at": "2026-03-28T19:10:00+00:00"}
        payload["accepted_draft_hash"] = web_app._calibration_draft_hash(payload)
        payload["accepted_sample_job_id"] = "sample-2"

        accepted_gate = web_app._review_gate(payload)

        self.assertEqual(accepted_gate["status"], "accepted")
        self.assertTrue(accepted_gate["can_confirm_full"])

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
            process_controller=unittest.mock.ANY,
            host=host,
        )
        sample_encode_mock.assert_called_once()
        self.assertEqual(sample_encode_mock.call_args.kwargs["host"], host)
        self.assertEqual(sample_encode_mock.call_args.kwargs["source_codec"], "h264")
        encode_preview_mock.assert_called_once()
        self.assertEqual(encode_preview_mock.call_args.kwargs["host"], host)
        self.assertEqual(encode_preview_mock.call_args.kwargs["source_codec"], "h264")
        self.assertEqual(payload["host"], host)
        self.assertEqual(payload["compare_clips"][0]["path"], "/review-media/remote-run/item-00/compare-01-12m-00s.mkv")

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
        self.assertEqual(status.message, "Needs remote setup")

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
        ) as run_remote_ssh_mock, patch("mediaforce.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertTrue(status.available)
        self.assertIn("/srv/media/tv", run_remote_ssh_mock.call_args.kwargs["input_text"])
        self.assertIn("/srv/media/transcode", run_remote_ssh_mock.call_args.kwargs["input_text"])

    def test_remote_host_status_stream_mode_ignores_missing_library_paths(self) -> None:
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

    def test_remote_host_status_allows_encode_only_hosts_without_ab_av1(self) -> None:
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
        self.assertTrue(status.available)
        self.assertNotIn(remote.AB_AV1_MISSING_ISSUE, status.issues)

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
                "tool|ffmpeg_xpsnr|0",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|0",
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
        self.assertIn("Installed ab-av1 with Homebrew for sampled calibration if it was missing.",
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
        waited = remote.HostSetupResult(ok=True, message="Xcode Command Line Tools finished installing on the remote Mac.")
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
        ready = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="/Library/Developer/CommandLineTools", stderr="")
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
        expected = remote.HostSetupResult(ok=True, message="Xcode Command Line Tools are already installed on the remote Mac.")
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
            connection.execute(
                "INSERT INTO staged_artifacts(library_item_id, staging_path, updated_at) VALUES (?, ?, ?)",
                (item_id, str(staging_path), web_app._now_iso()),
            )
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
            stored_status = connection.execute(
                "SELECT status FROM library_items WHERE id = ?",
                (item_id,),
            ).fetchone()[0]
            stored_validation = connection.execute(
                "SELECT validation_json FROM staged_artifacts WHERE library_item_id = ?",
                (item_id,),
            ).fetchone()[0]
            self.assertEqual(stored_status, "validated")
            self.assertTrue(json.loads(stored_validation)["passed"])

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
                "INSERT INTO staged_artifacts(library_item_id, staging_path, validation_json, updated_at) VALUES (?, ?, ?, ?)",
                (item_id, str(staging_path), json.dumps({"passed": True}), web_app._now_iso()),
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

            library_row = connection.execute(
                "SELECT source_path, rel_path, container, status, fingerprint FROM library_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            staged_row = connection.execute(
                "SELECT promoted_path, archived_source_path FROM staged_artifacts WHERE library_item_id = ?",
                (item_id,),
            ).fetchone()
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

            def wait(self) -> int:
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

            def wait(self) -> int:
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
            item_id = self._insert_library_item(connection, source_path, status="planned")
            item = {
                "library_item_id": item_id,
                "resolved_policy": {
                    "video": {"preset": 4, "encoder": "libsvtav1"},
                    "audio": {},
                    "subtitle": {},
                },
                "video_codec": "h264",
                "width": 1920,
                "height": 1080,
                "source_fingerprint": "source-fingerprint",
                "source_size_bytes": 1024,
            }
            with patch("mediaforce.execution.resolve_item_source_path", return_value=source_path), patch(
                    "mediaforce.execution.resolve_item_staging_path", return_value=staging_path
            ), patch("mediaforce.execution._search_quality", return_value=quality_result), patch(
                    "mediaforce.execution._select_streams", return_value={"audio_tracks": [], "subtitle_tracks": []}
            ), patch("mediaforce.execution._build_ffmpeg_command", return_value=["ffmpeg", "-i", str(source_path), str(staging_path)]), patch(
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
                )

            self.assertEqual(result.staging_path, staging_path)
            self.assertTrue(staging_path.exists())
            artifact_row = connection.execute(
                "SELECT staging_path, quality_metric, quality_score, staging_fingerprint FROM staged_artifacts WHERE library_item_id = ?",
                (item_id,),
            ).fetchone()
            item_row = connection.execute(
                "SELECT status FROM library_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            self.assertEqual(artifact_row["staging_path"], str(staging_path))
            self.assertEqual(artifact_row["quality_metric"], "XPSNR")
            self.assertEqual(artifact_row["quality_score"], 41.5)
            self.assertEqual(artifact_row["staging_fingerprint"], "staged-fingerprint")
            self.assertEqual(item_row["status"], "encoded")

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
            item_id = self._insert_library_item(connection, source_path, status="planned")
            item = {
                "library_item_id": item_id,
                "resolved_policy": {
                    "video": {"preset": 4, "encoder": "libsvtav1"},
                    "audio": {},
                    "subtitle": {},
                },
                "video_codec": "h264",
                "width": 1920,
                "height": 1080,
                "source_fingerprint": "source-fingerprint",
                "source_size_bytes": 1024,
            }
            with patch("mediaforce.execution.resolve_item_source_path", return_value=source_path), patch(
                    "mediaforce.execution.resolve_item_staging_path", return_value=staging_path
            ), patch("mediaforce.execution._search_quality", return_value=quality_result), patch(
                    "mediaforce.execution._select_streams", return_value={"audio_tracks": [], "subtitle_tracks": []}
            ), patch("mediaforce.execution._build_ffmpeg_command", return_value=["ffmpeg", "-i", str(source_path), str(staging_path)]), patch(
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
                    )

            self.assertFalse(staging_path.exists())
            self.assertFalse(staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}").exists())

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
            index = int(args[4])
            progress_callback = kwargs["progress_callback"]
            if callable(progress_callback):
                snapshot = {0: {"out_time_seconds": 50.0, "speed": 2.0}, 1: {"out_time_seconds": 25.0, "speed": 2.5}}[index]
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
        self.assertAlmostEqual(float(progress_snapshots[0]["percent_complete"]), 33.3333, places=3)
        self.assertEqual(progress_snapshots[1]["current_item_number"], 2)
        self.assertEqual(progress_snapshots[1]["completed_item_count"], 1)
        self.assertEqual(progress_snapshots[1]["overall_completed_duration_seconds"], 125.0)
        self.assertAlmostEqual(float(progress_snapshots[1]["percent_complete"]), 83.3333, places=3)
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
        self.assertEqual(review._planned_audio_action({"codec_name": "aac"}, {"convert_to_opus_codecs": ["aac"]}), "libopus")
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
        run_remote_command_mock.assert_not_called()

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
        telemetry = decorated.get("telemetry") or {}
        self.assertAlmostEqual(float(telemetry.get("eta_seconds") or 0.0), 90.0)
        self.assertEqual(telemetry.get("eta_copy"), "1m 30s")
        self.assertEqual(decorated["running"][0]["telemetry_summary"], "40% · 2.00x · Est. ETA 1m 0s")

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

        def remote_command_side_effect(_host: dict[str, object], cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
            remote_calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        def copy_side_effect(_host: dict[str, object], _remote_path: Path, local_path: Path, timeout: int) -> None:
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
                audio_track={"codec_name": "aac", "channels": 6},
                audio_policy={"convert_to_opus_codecs": ["aac"], "surround_5_1_opus_bitrate": "256k"},
            )

        self.assertEqual(result["action"], "libopus")
        self.assertEqual(result["bitrate"], "256k")
        self.assertEqual(result["channels"], 6)
        self.assertEqual(result["codec_name"], "aac")
        self.assertTrue(output_path.exists())
        self.assertEqual(spectrogram_mock.call_count, 2)
        encoded_audio_mock.assert_called_once()
        stack_mock.assert_called_once()
        self.assertFalse((output_path.parent / ".spectrogram-artifacts").exists())

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
        compare_clip = review.CompareClip(output_path=self.root / "compare.mkv", timestamp_seconds=30.0, duration_seconds=8.0)
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="validated")
            connection.execute(
                "INSERT INTO staged_artifacts(library_item_id, staging_path, updated_at) VALUES (?, ?, ?)",
                (item_id, str(staged_path), web_app._now_iso()),
            )
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

    def test_create_app_registers_folder_status_route_before_catch_all_folder_route(self) -> None:
        with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ):
            app = web_app.create_app(self.config.paths.config_path)

        folder_route_paths = [
            route.path
            for route in app.router.routes
            if getattr(route, "path", "").startswith("/api/folders/")
        ]
        self.assertLess(
            folder_route_paths.index("/api/folders/{prefix:path}/status"),
            folder_route_paths.index("/api/folders/{prefix:path}"),
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
            self.assertEqual(running_job["status"], "failed")
            self.assertEqual(queued_job["status"], "failed")
            self.assertEqual(running_job["error"], "Calibration queue job was stopped and cleaned up.")
            self.assertEqual(queued_job["error"], "Calibration queue job was stopped and cleaned up.")
            controller.cancel.assert_called_once_with()
        finally:
            web_app.CALIBRATION_QUEUE_PROCESSES.clear()

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
    def _insert_library_item(connection: sqlite3.Connection, source_path: Path, *,
                             status: str = "planned") -> int:
        now = web_app._now_iso()
        connection.execute(
            """
            INSERT INTO library_items(source_path, rel_path, media_root, parent_dir, file_name, container,
                                      size_bytes, mtime_ns, fingerprint, duration_seconds, video_codec,
                                      audio_summary_json, subtitle_summary_json, last_scan_id, discovered_at,
                                      last_seen_at, updated_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(source_path),
                f"tv/show/{source_path.name}",
                "tv",
                "tv/show",
                source_path.name,
                ".mkv",
                1024,
                1,
                f"fingerprint-{source_path.name}",
                60.0,
                "h264",
                "[]",
                "[]",
                "scan-1",
                now,
                now,
                now,
                status,
            ),
        )
        return int(connection.execute("SELECT id FROM library_items ORDER BY id DESC LIMIT 1").fetchone()[0])

    @staticmethod
    def _insert_staged_artifact(connection: sqlite3.Connection, library_item_id: int, staging_path: Path) -> None:
        connection.execute(
            "INSERT INTO staged_artifacts(library_item_id, staging_path, updated_at) VALUES (?, ?, ?)",
            (library_item_id, str(staging_path), web_app._now_iso()),
        )

    def _write_manifest(self, name: str, items: list[dict[str, object]]) -> Path:
        path = self.root / "runs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"items": items}))
        return path

    def _save_job(
            self,
            connection: sqlite3.Connection,
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
