import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy import select

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items, scan_runs
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.library.background_work import set_background_work_paused
from mediaforce.library.evidence_queue import EvidenceQueueConflict, claim_next_evidence_work, resume_evidence_queue, \
    start_evidence_work
from mediaforce.library.evidence_state import rebuild_library_item_evidence_states
from mediaforce.library.scanner import ScanStats
from mediaforce.web import app as web_app
from mediaforce.web.runtime import job_runtime as job_runtime_module
from mediaforce.web.runtime.job_runtime import JobRuntimeDeps, active_scan_from_db, latest_scan_completed_at, \
    load_scan_status, maybe_schedule_scan, repair_stale_scan_runs, run_scan_job, save_scan_job_state, scan_is_stale
from mediaforce.web.runtime.operator_work import BoundedEvidenceRunner, build_operator_work_payload


class OperatorWorkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        project_root = Path(self.temp_dir.name)
        media_root = project_root / "tv"
        media_root.mkdir()
        self.config = MediaforceConfig(
            raw={
                "media": {
                    "libraries": [
                        {
                            "key": "tv",
                            "label": "Television",
                            "path": str(media_root),
                            "type": "tv",
                            "availability": "production",
                        }
                    ]
                }
            },
            paths=ConfigPaths(
                project_root=project_root,
                config_path=project_root / "config.toml",
                db_path=project_root / "state" / "library.sqlite3",
                run_manifest_dir=project_root / "state" / "runs",
                web_state_dir=project_root / "state" / "web",
                review_dir=project_root / "state" / "review",
                runtime_settings_path=project_root / "state" / "settings.json",
            ),
        )

    def tearDown(self) -> None:
        reset_engine_cache()
        self.temp_dir.cleanup()

    def test_payload_is_idle_without_runner_and_paginates_reachable_backlog(self) -> None:
        self._insert_item()

        with open_db(self.config.paths.db_path) as connection:
            payload = build_operator_work_payload(
                connection,
                self.config,
                scan_job=None,
                catalog_is_stale=False,
                latest_scan_completed_at=None,
                evidence_runner_active=False,
                backlog_limit=1,
            )

        self.assertEqual(payload["background"]["status"], "active")
        self.assertEqual(payload["catalog"]["freshness"], "current")
        self.assertEqual(payload["evidence"]["live_status"], "idle")
        self.assertEqual(payload["refresh"], {"mode": "manual", "interval_ms": None})
        self.assertEqual(payload["evidence"]["backlog"]["total"], 2)
        self.assertEqual(len(payload["evidence"]["backlog"]["rows"]), 1)
        self.assertTrue(payload["evidence"]["backlog"]["has_next"])
        self.assertNotIn("source_path", payload["evidence"]["backlog"]["rows"][0])

    def test_prepared_batch_stays_quiet_until_explicit_runner_start(self) -> None:
        self._insert_item()
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(connection, self.config, "tv", limit=1)
            payload = build_operator_work_payload(
                connection,
                self.config,
                scan_job=None,
                catalog_is_stale=False,
                latest_scan_completed_at=None,
                evidence_runner_active=False,
            )

        self.assertEqual(payload["evidence"]["live_status"], "paused")
        self.assertTrue(payload["evidence"]["can_resume"])
        self.assertEqual(payload["refresh"]["mode"], "manual")

    def test_resumed_queue_without_runner_offers_continue_instead_of_pause(self) -> None:
        self._insert_item()
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(connection, self.config, "tv", limit=1)
            resume_evidence_queue(connection)
            payload = build_operator_work_payload(
                connection,
                self.config,
                scan_job=None,
                catalog_is_stale=False,
                latest_scan_completed_at=None,
                evidence_runner_active=False,
            )

        self.assertTrue(payload["evidence"]["can_resume"])
        self.assertFalse(payload["evidence"]["can_pause"])

    def test_backlog_offset_clamps_to_last_reachable_page(self) -> None:
        self._insert_item()
        with open_db(self.config.paths.db_path) as connection:
            payload = build_operator_work_payload(
                connection,
                self.config,
                scan_job=None,
                catalog_is_stale=False,
                latest_scan_completed_at=None,
                evidence_runner_active=False,
                backlog_offset=100,
                backlog_limit=1,
            )

        backlog = payload["evidence"]["backlog"]
        self.assertEqual(backlog["offset"], 1)
        self.assertEqual(backlog["range_start"], 2)
        self.assertEqual(backlog["range_end"], 2)
        self.assertFalse(backlog["has_next"])

    def test_global_pause_blocks_new_batches_and_claims(self) -> None:
        self._insert_item()
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(connection, self.config, "tv", limit=1)
            set_background_work_paused(connection, is_paused=True)
            claim = claim_next_evidence_work(connection, worker_id="worker", lease_seconds=30)
            with self.assertRaisesRegex(EvidenceQueueConflict, "Background catalog"):
                start_evidence_work(connection, self.config, "tv", limit=1)

        self.assertIsNone(claim)

    def test_scan_scheduler_does_not_launch_while_background_work_is_paused(self) -> None:
        run_scan_job = Mock()
        save_scan_job_state = Mock()
        deps = JobRuntimeDeps(
            parse_iso=lambda _value: None,
            now_iso=lambda: "2026-07-19T12:00:00+00:00",
            run_scan_job=run_scan_job,
            scan_process_is_alive=lambda _pid: False,
            current_catalog_signature=lambda _config: "current",
            load_catalog_signature=lambda _config: "saved",
            load_scan_job_state=lambda _config, _prefix: None,
            save_scan_job_state=save_scan_job_state,
            calibration_job_notice_after=timedelta(hours=1),
            full_scan_stale_after=timedelta(hours=1),
            prefix_scan_stale_after=timedelta(hours=1),
            scan_retry_cooldown=timedelta(minutes=1),
            scan_interrupted_error="interrupted",
            save_catalog_signature=Mock(),
            reset_folder_card_cache=Mock(),
        )
        with open_db(self.config.paths.db_path) as connection:
            set_background_work_paused(connection, is_paused=True)
            result = maybe_schedule_scan(
                connection,
                self.config,
                None,
                deps,
                force=True,
            )

        self.assertIsNone(result)
        save_scan_job_state.assert_not_called()
        run_scan_job.assert_not_called()

    def test_scan_status_ignores_terminal_job_older_than_latest_catalog_snapshot(self) -> None:
        old_job = {
            "job_id": "old-job",
            "status": "completed",
            "created_at": "2026-07-19T10:00:00+00:00",
            "started_at": "2026-07-19T10:00:00+00:00",
            "finished_at": "2026-07-19T10:05:00+00:00",
            "stats": {"warnings": [{"code": "source_unavailable"}]},
        }
        deps = self._job_runtime_deps(load_scan_job_state=lambda _config, _prefix: old_job)
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="newer-scan",
                    started_at="2026-07-19T12:00:00+00:00",
                    completed_at="2026-07-19T12:05:00+00:00",
                    status="completed",
                    error=None,
                    owner_pid=None,
                    last_progress_at="2026-07-19T12:05:00+00:00",
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=1,
                    reprobed_count=0,
                    unchanged_count=1,
                )
            )
            status = load_scan_status(connection, self.config, None, deps)

        self.assertIsNone(status)

    def test_scan_status_prefers_database_failure_over_completed_sidecar(self) -> None:
        completed_job = {
            "job_id": "legacy-sidecar",
            "status": "completed",
            "scope": "full",
            "prefix": None,
            "created_at": "2026-07-19T11:59:00+00:00",
            "started_at": "2026-07-19T12:00:00+00:00",
            "finished_at": "2026-07-19T12:05:00+00:00",
            "error": None,
            "stats": None,
        }
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: completed_job,
        )
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="authoritative-failure",
                    started_at="2026-07-19T12:00:01+00:00",
                    completed_at="2026-07-19T12:01:00+00:00",
                    status="failed",
                    error="interrupted",
                    owner_pid=None,
                    last_progress_at="2026-07-19T12:01:00+00:00",
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=1,
                    reprobed_count=0,
                    unchanged_count=1,
                )
            )
            status = load_scan_status(connection, self.config, None, deps)

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["job_id"], "authoritative-failure")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "interrupted")

    def test_scan_status_prefers_completed_database_run_over_live_pid_sidecar(
            self,
    ) -> None:
        active_job = {
            "job_id": "linked-scan",
            "scan_id": "linked-scan",
            "status": "running",
            "scope": "full",
            "prefix": None,
            "owner_pid": 1,
            "created_at": "2026-07-19T11:59:00+00:00",
            "started_at": "2026-07-19T12:00:00+00:00",
            "finished_at": None,
            "error": None,
            "stats": None,
        }
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: active_job,
        )
        deps.scan_process_is_alive = lambda _pid: True
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="linked-scan",
                    started_at="2026-07-19T12:00:01+00:00",
                    completed_at="2026-07-19T12:05:00+00:00",
                    status="completed",
                    error=None,
                    owner_pid=None,
                    last_progress_at="2026-07-19T12:05:00+00:00",
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=1,
                    reprobed_count=0,
                    unchanged_count=1,
                )
            )
            status = load_scan_status(connection, self.config, None, deps)

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["job_id"], "linked-scan")
        self.assertEqual(status["status"], "completed")

    def test_scan_status_keeps_current_process_job_active_after_scan_commit(
            self,
    ) -> None:
        active_job = {
            "job_id": "linked-scan",
            "scan_id": "linked-scan",
            "status": "running",
            "scope": "full",
            "prefix": None,
            "owner_pid": os.getpid(),
            "created_at": "2026-07-19T11:59:00+00:00",
            "started_at": "2026-07-19T12:00:00+00:00",
            "finished_at": None,
            "error": None,
            "stats": None,
        }
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: active_job,
        )
        deps.scan_process_is_alive = lambda _pid: True
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="linked-scan",
                    started_at="2026-07-19T12:00:01+00:00",
                    completed_at="2026-07-19T12:05:00+00:00",
                    status="completed",
                    error=None,
                    owner_pid=os.getpid(),
                    last_progress_at="2026-07-19T12:05:00+00:00",
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=1,
                    reprobed_count=0,
                    unchanged_count=1,
                )
            )
            status = load_scan_status(connection, self.config, None, deps)

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["job_id"], "linked-scan")
        self.assertEqual(status["status"], "running")

    def test_active_scan_ignores_sidecar_for_another_database_scan(self) -> None:
        unrelated_job = {
            "job_id": "unrelated-scan",
            "scan_id": "unrelated-scan",
            "status": "failed",
            "scope": "full",
            "prefix": None,
            "owner_pid": 1,
            "created_at": "2026-07-19T11:59:00+00:00",
            "started_at": "2026-07-19T12:00:00+00:00",
            "finished_at": "2026-07-19T12:01:00+00:00",
            "error": "unrelated failure",
            "stats": None,
        }
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: unrelated_job,
        )
        deps.scan_process_is_alive = lambda _pid: True
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="database-scan",
                    started_at="2026-07-19T12:00:01+00:00",
                    completed_at=None,
                    status="running",
                    error=None,
                    owner_pid=1,
                    last_progress_at="2026-07-19T12:00:01+00:00",
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=1,
                    reprobed_count=0,
                    unchanged_count=0,
                )
            )
            status = active_scan_from_db(connection, self.config, None, deps)

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["job_id"], "database-scan")
        self.assertEqual(status["scan_id"], "database-scan")
        self.assertEqual(status["status"], "running")
        self.assertIsNone(status["error"])

    def test_active_scan_expires_dead_row_despite_mismatched_sidecar(self) -> None:
        unrelated_job = {
            "job_id": "unrelated-scan",
            "scan_id": "unrelated-scan",
            "status": "running",
            "scope": "full",
            "prefix": None,
            "owner_pid": 999999,
            "created_at": "2026-07-19T11:59:00+00:00",
            "started_at": "2026-07-19T12:00:00+00:00",
            "finished_at": None,
            "error": None,
            "stats": None,
        }
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: unrelated_job,
        )
        deps.scan_process_is_alive = lambda _pid: False
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="dead-database-scan",
                    started_at="2026-07-19T12:00:01+00:00",
                    completed_at=None,
                    status="running",
                    error=None,
                    owner_pid=999998,
                    last_progress_at="2026-07-19T12:00:01+00:00",
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=1,
                    reprobed_count=0,
                    unchanged_count=0,
                )
            )
            status = active_scan_from_db(connection, self.config, None, deps)
            row = connection.execute(
                select(scan_runs).where(
                    scan_runs.c.scan_id == "dead-database-scan"
                )
            ).mappings().one()

        self.assertIsNone(status)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "interrupted")
        self.assertIsNotNone(row["completed_at"])

    def test_active_scan_terminalizes_dead_row_when_sidecar_write_fails(self) -> None:
        now = "2026-07-19T12:00:00+00:00"
        running_job = {
            "job_id": "dead-sidecar-scan",
            "scan_id": "dead-sidecar-scan",
            "status": "running",
            "scope": "full",
            "prefix": None,
            "owner_pid": 999999,
            "created_at": now,
            "started_at": now,
            "finished_at": None,
            "error": None,
            "stats": None,
        }
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: running_job,
            save_scan_job_state=Mock(side_effect=OSError("sidecar write failed")),
        )
        deps.scan_process_is_alive = lambda _pid: False
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="dead-sidecar-scan",
                    started_at=now,
                    completed_at=None,
                    status="running",
                    error=None,
                    owner_pid=999999,
                    last_progress_at=now,
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=0,
                    reprobed_count=0,
                    unchanged_count=0,
                )
            )
            with self.assertRaisesRegex(OSError, "sidecar write failed"):
                active_scan_from_db(connection, self.config, None, deps)
            row = connection.execute(
                select(scan_runs).where(
                    scan_runs.c.scan_id == "dead-sidecar-scan"
                )
            ).mappings().one()

        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "interrupted")
        self.assertIsNotNone(row["completed_at"])

    def test_failed_scan_run_does_not_refresh_catalog(self) -> None:
        deps = self._job_runtime_deps(load_scan_job_state=lambda _config, _prefix: None)
        now = "2026-07-19T12:00:00+00:00"
        self._insert_item()
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="failed-scan",
                    started_at=now,
                    completed_at=now,
                    status="failed",
                    error="interrupted",
                    owner_pid=os.getpid(),
                    last_progress_at=now,
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=1,
                    reprobed_count=0,
                    unchanged_count=1,
                )
            )
            latest = latest_scan_completed_at(connection, prefix=None)
            stale = scan_is_stale(connection, self.config, None, deps)

        self.assertIsNone(latest)
        self.assertTrue(stale)

    def test_active_scan_from_db_honors_same_pid_terminal_failed_job_state(self) -> None:
        now = "2026-07-19T12:00:00+00:00"
        terminal_job = {
            "job_id": "scan-terminal-job",
            "scan_id": "same-pid-row",
            "status": "failed",
            "scope": "full",
            "prefix": None,
            "owner_pid": os.getpid(),
            "created_at": now,
            "started_at": now,
            "finished_at": now,
            "error": "fixture failure",
            "stats": None,
        }
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: terminal_job,
            save_scan_job_state=Mock(),
        )
        deps.scan_process_is_alive = lambda _pid: True
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="same-pid-row",
                    started_at=now,
                    completed_at=None,
                    status="running",
                    error=None,
                    owner_pid=os.getpid(),
                    last_progress_at=now,
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=0,
                    reprobed_count=0,
                    unchanged_count=0,
                )
            )
            status = active_scan_from_db(connection, self.config, None, deps)
            row = connection.execute(select(scan_runs).where(scan_runs.c.scan_id == "same-pid-row")).mappings().one()

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "fixture failure")
        self.assertEqual(row["status"], "failed")
        self.assertIsNotNone(row["completed_at"])

    def test_startup_repair_marks_dead_scan_run_failed_and_saves_job_failure(self) -> None:
        now = "2026-07-19T12:00:00+00:00"
        running_job = {
            "job_id": "scan-job",
            "scan_id": "stale-running-row",
            "status": "running",
            "scope": "full",
            "prefix": None,
            "owner_pid": 999999,
            "created_at": now,
            "started_at": now,
            "finished_at": None,
            "error": None,
            "stats": None,
        }
        save_scan_job_state = Mock()
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: running_job,
            save_scan_job_state=save_scan_job_state,
        )
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="stale-running-row",
                    started_at=now,
                    completed_at=None,
                    status="running",
                    error=None,
                    owner_pid=999999,
                    last_progress_at=now,
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=0,
                    reprobed_count=0,
                    unchanged_count=0,
                )
            )
            repaired = repair_stale_scan_runs(connection, self.config, deps)
            row = connection.execute(select(scan_runs).where(scan_runs.c.scan_id == "stale-running-row")).mappings().one()

        self.assertEqual(repaired, 1)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "interrupted")
        self.assertIsNotNone(row["completed_at"])
        saved_job = save_scan_job_state.call_args.args[2]
        self.assertEqual(saved_job["status"], "failed")
        self.assertEqual(saved_job["error"], "interrupted")

    def test_startup_repair_terminalizes_database_when_sidecar_write_fails(self) -> None:
        now = "2026-07-19T12:00:00+00:00"
        running_job = {
            "job_id": "stale-sidecar-row",
            "scan_id": "stale-sidecar-row",
            "status": "running",
            "scope": "full",
            "prefix": None,
            "owner_pid": 999999,
            "created_at": now,
            "started_at": now,
            "finished_at": None,
            "error": None,
            "stats": None,
        }
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: running_job,
            save_scan_job_state=Mock(side_effect=OSError("sidecar write failed")),
        )
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="stale-sidecar-row",
                    started_at=now,
                    completed_at=None,
                    status="running",
                    error=None,
                    owner_pid=999999,
                    last_progress_at=now,
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=0,
                    reprobed_count=0,
                    unchanged_count=0,
                )
            )
            with self.assertRaisesRegex(OSError, "sidecar write failed"):
                repair_stale_scan_runs(connection, self.config, deps)
            row = connection.execute(
                select(scan_runs).where(
                    scan_runs.c.scan_id == "stale-sidecar-row"
                )
            ).mappings().one()

        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "interrupted")
        self.assertIsNotNone(row["completed_at"])

    def test_startup_repair_does_not_trust_live_pid_for_preexisting_scan_run(self) -> None:
        now = "2026-07-19T12:00:00+00:00"
        running_job = {
            "job_id": "scan-job",
            "scan_id": "pid-reuse-row",
            "status": "running",
            "scope": "full",
            "prefix": None,
            "owner_pid": 1,
            "created_at": now,
            "started_at": now,
            "finished_at": None,
            "error": None,
            "stats": None,
        }
        save_scan_job_state = Mock()
        scan_process_is_alive = Mock(return_value=True)
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: running_job,
            save_scan_job_state=save_scan_job_state,
        )
        deps.scan_process_is_alive = scan_process_is_alive
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="pid-reuse-row",
                    started_at=now,
                    completed_at=None,
                    status="running",
                    error=None,
                    owner_pid=1,
                    last_progress_at=now,
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=0,
                    reprobed_count=0,
                    unchanged_count=0,
                )
            )
            repaired = repair_stale_scan_runs(connection, self.config, deps)
            row = connection.execute(select(scan_runs).where(scan_runs.c.scan_id == "pid-reuse-row")).mappings().one()

        self.assertEqual(repaired, 1)
        scan_process_is_alive.assert_not_called()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "interrupted")
        saved_job = save_scan_job_state.call_args.args[2]
        self.assertEqual(saved_job["status"], "failed")
        self.assertEqual(saved_job["error"], "interrupted")

    def test_startup_repair_fails_preexisting_sidecar_without_scan_row(self) -> None:
        path = self.config.paths.web_state_dir / "scan-full-catalog.job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "job_id": "sidecar-only",
                    "status": "queued",
                    "scope": "full",
                    "prefix": None,
                    "owner_pid": 1,
                    "created_at": "2026-07-19T12:00:00+00:00",
                    "started_at": None,
                    "finished_at": None,
                    "error": None,
                    "stats": None,
                }
            ),
            encoding="utf-8",
        )
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: None,
        )
        deps.list_scan_job_files = lambda _config: (path,)

        with open_db(self.config.paths.db_path) as connection:
            repaired = repair_stale_scan_runs(connection, self.config, deps)

        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(repaired, 1)
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error"], "interrupted")
        self.assertEqual(stored["finished_at"], "2026-07-19T12:00:00+00:00")

    def test_startup_repair_restores_completed_database_state_to_active_sidecar(
            self,
    ) -> None:
        path = self.config.paths.web_state_dir / "scan-full-catalog.job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "job_id": "linked-scan",
                    "scan_id": "linked-scan",
                    "status": "running",
                    "scope": "full",
                    "prefix": None,
                    "owner_pid": 999999,
                    "created_at": "2026-07-19T11:59:00+00:00",
                    "started_at": "2026-07-19T12:00:00+00:00",
                    "finished_at": None,
                    "error": None,
                    "stats": None,
                }
            ),
            encoding="utf-8",
        )
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: None,
        )
        deps.list_scan_job_files = lambda _config: (path,)
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="linked-scan",
                    started_at="2026-07-19T12:00:01+00:00",
                    completed_at="2026-07-19T12:05:00+00:00",
                    status="completed",
                    error=None,
                    owner_pid=None,
                    last_progress_at="2026-07-19T12:05:00+00:00",
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=1,
                    reprobed_count=0,
                    unchanged_count=1,
                )
            )
            connection.execute(
                scan_runs.insert().values(
                    scan_id="unrelated-newer-scan",
                    started_at="2026-07-19T13:00:00+00:00",
                    completed_at="2026-07-19T13:05:00+00:00",
                    status="failed",
                    error="unrelated failure",
                    owner_pid=None,
                    last_progress_at="2026-07-19T13:05:00+00:00",
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=0,
                    reprobed_count=0,
                    unchanged_count=0,
                )
            )
            repaired = repair_stale_scan_runs(connection, self.config, deps)

        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(repaired, 1)
        self.assertEqual(stored["job_id"], "linked-scan")
        self.assertEqual(stored["status"], "completed")
        self.assertIsNone(stored["error"])

    def test_startup_repair_does_not_inherit_success_for_legacy_sidecar(self) -> None:
        path = self.config.paths.web_state_dir / "scan-full-catalog.job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "job_id": "legacy-sidecar",
                    "status": "running",
                    "scope": "full",
                    "prefix": None,
                    "owner_pid": 999999,
                    "created_at": "2026-07-19T11:59:00+00:00",
                    "started_at": "2026-07-19T12:00:00+00:00",
                    "finished_at": None,
                    "error": None,
                    "stats": None,
                }
            ),
            encoding="utf-8",
        )
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: None,
        )
        deps.list_scan_job_files = lambda _config: (path,)
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="completed-database-scan",
                    started_at="2026-07-19T12:00:01+00:00",
                    completed_at="2026-07-19T12:05:00+00:00",
                    status="completed",
                    error=None,
                    owner_pid=None,
                    last_progress_at="2026-07-19T12:05:00+00:00",
                    roots_json='["tv"]',
                    scope="full",
                    prefixes_json=None,
                    file_count=1,
                    reprobed_count=0,
                    unchanged_count=1,
                )
            )
            repaired = repair_stale_scan_runs(connection, self.config, deps)

        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(repaired, 1)
        self.assertEqual(stored["job_id"], "legacy-sidecar")
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error"], "interrupted")

    def test_malformed_scan_sidecar_fails_closed_for_status_reads(self) -> None:
        path = self.config.paths.web_state_dir / "scan-full-catalog.job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda config, prefix: job_runtime_module.load_scan_job_state(
                config,
                prefix,
                lambda _config, _prefix: path,
            ),
        )

        with open_db(self.config.paths.db_path) as connection:
            status = load_scan_status(connection, self.config, None, deps)

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], job_runtime_module.DEFAULT_SCAN_INTERRUPTED_ERROR)

    def test_scan_sidecar_write_uses_same_directory_atomic_replace(self) -> None:
        path = self.config.paths.web_state_dir / "scan-full-catalog.job.json"
        replacements: list[tuple[Path, Path]] = []
        real_replace = os.replace

        def replace_and_record(source: object, destination: object) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            replacements.append((source_path, destination_path))
            real_replace(source_path, destination_path)

        with patch.object(job_runtime_module.os, "replace", side_effect=replace_and_record):
            save_scan_job_state(
                self.config,
                None,
                {"job_id": "atomic", "status": "queued"},
                lambda _config, _prefix: path,
            )

        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["job_id"], "atomic")
        self.assertEqual(len(replacements), 1)
        source_path, destination_path = replacements[0]
        self.assertEqual(source_path.parent, path.parent)
        self.assertEqual(destination_path, path)
        self.assertFalse(source_path.exists())

    def test_concurrent_scan_refreshes_publish_and_start_only_one_job(self) -> None:
        jobs: dict[str | None, dict[str, object]] = {}
        jobs_lock = threading.Lock()
        first_save_entered = threading.Event()
        release_first_save = threading.Event()
        second_started = threading.Event()
        started_job_ids: list[str] = []

        def load_job(_config: object, job_prefix: str | None) -> dict[str, object] | None:
            with jobs_lock:
                job = jobs.get(job_prefix)
                return dict(job) if job is not None else None

        def save_job(_config: object, job_prefix: str | None, payload: dict[str, object]) -> None:
            if not first_save_entered.is_set():
                first_save_entered.set()
                self.assertTrue(release_first_save.wait(timeout=1))
            with jobs_lock:
                jobs[job_prefix] = dict(payload)

        def start_thread(**kwargs: object) -> None:
            started_job_ids.append(str(kwargs["job_id"]))

        deps = self._job_runtime_deps(
            load_scan_job_state=load_job,
            save_scan_job_state=save_job,
        )
        deps.scan_process_is_alive = lambda pid: int(pid) == os.getpid() if pid is not None else False
        deps.start_scan_job_thread = start_thread
        results: list[dict[str, object] | None] = []

        def schedule_scan() -> None:
            with open_db(self.config.paths.db_path) as thread_connection:
                results.append(maybe_schedule_scan(thread_connection, self.config, None, deps, force=True))

        first = threading.Thread(target=schedule_scan)
        second = threading.Thread(target=lambda: (second_started.set(), schedule_scan()))
        first.start()
        self.assertTrue(first_save_entered.wait(timeout=1))
        second.start()
        self.assertTrue(second_started.wait(timeout=1))
        threading.Event().wait(0.05)
        self.assertEqual(results, [])

        release_first_save.set()
        first.join(timeout=1)
        second.join(timeout=1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(results), 2)
        self.assertIsNotNone(results[0])
        self.assertIsNotNone(results[1])
        assert results[0] is not None and results[1] is not None
        self.assertEqual(results[0]["job_id"], results[1]["job_id"])
        self.assertEqual(results[0]["scan_id"], results[0]["job_id"])
        self.assertEqual(results[1]["scan_id"], results[1]["job_id"])
        self.assertEqual(started_job_ids, [str(results[0]["job_id"])])

    def test_queued_scan_rechecks_global_pause_before_media_work_starts(self) -> None:
        queued_job = {
            "job_id": "queued-job",
            "status": "queued",
            "created_at": "2026-07-19T12:00:00+00:00",
        }
        save_scan_job_state = Mock()
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: queued_job,
            save_scan_job_state=save_scan_job_state,
        )
        with open_db(self.config.paths.db_path) as connection:
            set_background_work_paused(connection, is_paused=True)

        with patch("mediaforce.web.runtime.job_runtime.load_config", return_value=self.config), patch(
            "mediaforce.web.runtime.job_runtime.purge_transient_artifacts"
        ) as purge, patch("mediaforce.web.runtime.job_runtime.scan_library") as scan:
            run_scan_job(
                config_path=self.config.paths.config_path,
                prefix=None,
                job_id="queued-job",
                process_controller=ManagedProcessController(),
                deps=deps,
            )

        saved = save_scan_job_state.call_args.args[2]
        self.assertEqual(saved["status"], "paused")
        self.assertIsNone(saved["started_at"])
        purge.assert_not_called()
        scan.assert_not_called()

    def test_cancelled_scan_persists_interrupted_state(self) -> None:
        queued_job = {
            "job_id": "queued-job",
            "status": "queued",
            "created_at": "2026-07-19T12:00:00+00:00",
        }
        save_scan_job_state = Mock()
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: queued_job,
            save_scan_job_state=save_scan_job_state,
        )
        process_controller = ManagedProcessController()

        def cancel_scan(
                _connection: object,
                _config: object,
                **kwargs: object,
        ) -> ScanStats:
            self.assertIs(kwargs["process_controller"], process_controller)
            self.assertEqual(kwargs["scan_id"], "queued-job")
            raise ProcessCancelledError("shutdown")

        with patch("mediaforce.web.runtime.job_runtime.load_config", return_value=self.config), patch(
            "mediaforce.web.runtime.job_runtime.purge_transient_artifacts"
        ) as purge, patch(
            "mediaforce.web.runtime.job_runtime.scan_library",
            side_effect=cancel_scan,
        ):
            run_scan_job(
                config_path=self.config.paths.config_path,
                prefix=None,
                job_id="queued-job",
                process_controller=process_controller,
                deps=deps,
            )

        saved = save_scan_job_state.call_args_list[-1].args[2]
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error"], "interrupted")
        self.assertIsNone(saved["stats"])
        self.assertEqual(purge.call_count, 2)

    def test_stale_scan_worker_does_not_overwrite_replacement_sidecar(self) -> None:
        first_job = {
            "job_id": "scan-a",
            "scan_id": "scan-a",
            "status": "queued",
            "created_at": "2026-07-19T12:00:00+00:00",
        }
        replacement_job = {
            "job_id": "scan-b",
            "scan_id": "scan-b",
            "status": "queued",
            "created_at": "2026-07-19T12:01:00+00:00",
        }
        load_count = 0

        def load_scan_job_state(_config: object, _prefix: str | None) -> dict[str, object]:
            nonlocal load_count
            load_count += 1
            return first_job if load_count == 1 else replacement_job

        save_scan_job_state = Mock()
        deps = self._job_runtime_deps(
            load_scan_job_state=load_scan_job_state,
            save_scan_job_state=save_scan_job_state,
        )
        with (
            patch(
                "mediaforce.web.runtime.job_runtime.load_config",
                return_value=self.config,
            ),
            patch("mediaforce.web.runtime.job_runtime.purge_transient_artifacts"),
            patch(
                "mediaforce.web.runtime.job_runtime.scan_library",
                return_value=ScanStats(scan_id="scan-a"),
            ) as scan,
        ):
            run_scan_job(
                config_path=self.config.paths.config_path,
                prefix="tv",
                job_id="scan-a",
                process_controller=ManagedProcessController(),
                deps=deps,
            )

        scan.assert_called_once()
        self.assertEqual(save_scan_job_state.call_count, 1)
        saved = save_scan_job_state.call_args.args[2]
        self.assertEqual(saved["scan_id"], "scan-a")
        self.assertEqual(saved["status"], "running")
        self.assertEqual(replacement_job["scan_id"], "scan-b")
        self.assertEqual(replacement_job["status"], "queued")

    def test_fallback_scan_thread_receives_process_controller(self) -> None:
        deps = self._job_runtime_deps(
            load_scan_job_state=lambda _config, _prefix: None,
        )
        fallback_thread = Mock()
        with open_db(self.config.paths.db_path) as connection, patch(
            "mediaforce.web.runtime.job_runtime.threading.Thread",
            return_value=fallback_thread,
        ) as thread_factory:
            job = maybe_schedule_scan(
                connection,
                self.config,
                None,
                deps,
                force=True,
            )

        self.assertIsNotNone(job)
        thread_kwargs = thread_factory.call_args.kwargs["kwargs"]
        self.assertIs(thread_factory.call_args.kwargs["target"], deps.run_scan_job)
        self.assertIsInstance(
            thread_kwargs["process_controller"],
            ManagedProcessController,
        )
        fallback_thread.start.assert_called_once_with()

    def test_stuck_scan_thread_shutdown_uses_injected_process_terminator(self) -> None:
        stuck_thread = Mock()
        stuck_thread.is_alive.return_value = True
        process_controller = ManagedProcessController()
        exit_codes: list[int] = []

        def record_exit(code: int) -> None:
            exit_codes.append(code)

        try:
            with web_app.SCAN_JOB_THREADS_CONDITION:
                web_app.SCAN_JOB_THREADS["stuck-scan"] = stuck_thread
                web_app.SCAN_JOB_PROCESS_CONTROLLERS["stuck-scan"] = process_controller
            with self.assertLogs(web_app.LOGGER.name, level="CRITICAL") as captured:
                with self.assertRaisesRegex(RuntimeError, "terminator returned"):
                    web_app._wait_for_scan_job_threads(
                        grace_seconds=0,
                        terminate_process=record_exit,
                    )
        finally:
            with web_app.SCAN_JOB_THREADS_CONDITION:
                web_app.SCAN_JOB_THREADS.pop("stuck-scan", None)
                web_app.SCAN_JOB_PROCESS_CONTROLLERS.pop("stuck-scan", None)
                web_app.SCAN_JOB_THREADS_CONDITION.notify_all()

        self.assertEqual(exit_codes, [web_app.SCAN_FORCED_SHUTDOWN_EXIT_CODE])
        self.assertIn("stuck-scan", captured.output[0])

    def test_bounded_runner_rejects_duplicate_process_local_start(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def run_until_blocked(**_kwargs: object) -> None:
            started.set()
            release.wait(timeout=1)

        runner = BoundedEvidenceRunner(self.config.paths.config_path)
        with patch(
            "mediaforce.web.runtime.operator_work.run_evidence_queue_until_blocked",
            side_effect=run_until_blocked,
        ):
            self.assertTrue(runner.start(max_work_items=5))
            self.assertTrue(started.wait(timeout=1))
            self.assertTrue(runner.active)
            self.assertFalse(runner.start(max_work_items=5))
            release.set()
            for _ in range(100):
                if not runner.active:
                    break
                threading.Event().wait(0.01)

        self.assertFalse(runner.active)

    def _insert_item(self) -> None:
        now = "2026-07-19T12:00:00+00:00"
        with open_db(self.config.paths.db_path) as connection:
            result = connection.execute(
                library_items.insert().values(
                    source_path=str(self.config.paths.project_root / "tv" / "show" / "episode.mkv"),
                    rel_path="tv/show/episode.mkv",
                    media_root="tv",
                    parent_dir="tv/show",
                    file_name="episode.mkv",
                    container="mkv",
                    size_bytes=1_024,
                    mtime_ns=1,
                    fingerprint="fixture:fingerprint",
                    duration_seconds=60.0,
                    video_codec="h264",
                    audio_track_count=1,
                    subtitle_track_count=0,
                    english_audio_count=1,
                    english_subtitle_count=0,
                    audio_summary_json="[]",
                    subtitle_summary_json="[]",
                    cadence_summary_json=None,
                    media_fingerprint_json=None,
                    content_version_changed_at=now,
                    content_version_fingerprint="fixture:content",
                    status="discovered",
                    priority_score=0,
                    last_scan_id="fixture",
                    discovered_at=now,
                    last_seen_at=now,
                    updated_at=now,
                )
            )
            rebuild_library_item_evidence_states(
                connection,
                library_item_ids=[int(result.inserted_primary_key[0])],
            )

    @staticmethod
    def _job_runtime_deps(
            *,
            load_scan_job_state: object,
            save_scan_job_state: object | None = None,
    ) -> JobRuntimeDeps:
        return JobRuntimeDeps(
            parse_iso=lambda value: datetime.fromisoformat(str(value)) if value else None,
            now_iso=lambda: "2026-07-19T12:00:00+00:00",
            run_scan_job=Mock(),
            scan_process_is_alive=lambda _pid: False,
            current_catalog_signature=lambda _config: "current",
            load_catalog_signature=lambda _config: "current",
            load_scan_job_state=load_scan_job_state,
            save_scan_job_state=save_scan_job_state or Mock(),
            calibration_job_notice_after=timedelta(hours=1),
            full_scan_stale_after=timedelta(hours=1),
            prefix_scan_stale_after=timedelta(hours=1),
            scan_retry_cooldown=timedelta(minutes=1),
            scan_interrupted_error="interrupted",
            save_catalog_signature=Mock(),
            reset_folder_card_cache=Mock(),
        )


if __name__ == "__main__":
    unittest.main()
