import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items, scan_runs
from mediaforce.library.background_work import set_background_work_paused
from mediaforce.library.evidence_queue import EvidenceQueueConflict, claim_next_evidence_work, resume_evidence_queue, \
    start_evidence_work
from mediaforce.library.evidence_state import rebuild_library_item_evidence_states
from mediaforce.web.runtime.job_runtime import JobRuntimeDeps, load_scan_status, maybe_schedule_scan, run_scan_job
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
                deps=deps,
            )

        saved = save_scan_job_state.call_args.args[2]
        self.assertEqual(saved["status"], "paused")
        self.assertIsNone(saved["started_at"])
        purge.assert_not_called()
        scan.assert_not_called()

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
