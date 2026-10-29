import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import select

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.core.db_tables import evidence_queue_state, scan_runs
from mediaforce.encoding.encode_queue import load_queue_state, save_queue_state
from mediaforce.library.background_work import background_work_is_paused, set_background_work_paused
from mediaforce.library.evidence_queue import DEFAULT_EVIDENCE_QUEUE_NAME, EvidenceQueueConflict, load_evidence_queue_state
from mediaforce.web import app as web_app
from mediaforce.web.runtime import tool_capabilities


class ReadPathPurityTests(unittest.TestCase):
    def setUp(self) -> None:
        tool_capabilities.reset_metric_support_cache()
        self.tempdir = tempfile.TemporaryDirectory()
        project_root = Path(self.tempdir.name)
        media_root = project_root / "tv"
        media_root.mkdir()
        paths = ConfigPaths(
            project_root=project_root,
            config_path=project_root / "config.toml",
            db_path=project_root / "library.sqlite3",
            run_manifest_dir=project_root / "runs",
            web_state_dir=project_root / "web",
            review_dir=project_root / "review",
            runtime_settings_path=project_root / "runtime-settings.json",
        )
        self.config = MediaforceConfig(
            raw={
                "media": {
                    "staging_root": str(project_root / "staging"),
                    "libraries": [
                        {
                            "key": "tv",
                            "label": "TV Shows",
                            "path": str(media_root),
                            "type": "tv",
                            "availability": "browse_only",
                        }
                    ]
                },
                "video": {},
                "audio": {},
                "subtitle": {},
                "planning": {},
                "validation": {},
                "overrides": [],
                "remote_hosts": [],
            },
            paths=paths,
        )

    def tearDown(self) -> None:
        tool_capabilities.reset_metric_support_cache()
        reset_engine_cache()
        self.tempdir.cleanup()

    def test_metric_support_reads_never_probe_an_uninitialized_cache(self) -> None:
        with patch("mediaforce.web.runtime.tool_capabilities.ffmpeg_binary") as binary_mock, patch(
            "mediaforce.web.runtime.tool_capabilities.subprocess.run",
        ) as run_mock:
            support = tool_capabilities.metric_support()

        self.assertEqual(
            support,
            {"vmaf": False, "xpsnr": False, "ssim": False, "psnr": False},
        )
        binary_mock.assert_not_called()
        run_mock.assert_not_called()

    def test_metric_support_refreshes_explicitly_and_reads_cached_snapshot(self) -> None:
        binary = Path(self.tempdir.name) / "ffmpeg"
        binary.write_text("fixture", encoding="utf-8")
        completed = subprocess.CompletedProcess(
            [str(binary), "-hide_banner", "-filters"],
            0,
            stdout=" T.. libvmaf\n T.. xpsnr\n TS. ssim\n TS. psnr ",
            stderr="",
        )
        with patch(
            "mediaforce.web.runtime.tool_capabilities.ffmpeg_binary",
            return_value=str(binary),
        ), patch(
            "mediaforce.web.runtime.tool_capabilities.subprocess.run",
            return_value=completed,
        ) as run_mock:
            refreshed = tool_capabilities.refresh_metric_support()
            first_read = tool_capabilities.metric_support()
            second_refresh = tool_capabilities.refresh_metric_support()

        self.assertEqual(refreshed, {"vmaf": True, "xpsnr": True, "ssim": True, "psnr": True})
        self.assertEqual(first_read, refreshed)
        self.assertEqual(second_refresh, refreshed)
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 10)

    def test_reading_interrupted_scan_status_does_not_repair_persisted_state(self) -> None:
        started_at = "2026-07-19T08:00:00+00:00"
        web_app._save_scan_job_state(
            self.config,
            None,
            {
                "job_id": "scan-job",
                "status": "running",
                "scope": "full",
                "prefix": None,
                "owner_pid": 999_999,
                "created_at": started_at,
                "started_at": started_at,
                "finished_at": None,
                "error": None,
                "stats": None,
            },
        )
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                scan_runs.insert().values(
                    scan_id="scan-run",
                    started_at=started_at,
                    owner_pid=999_999,
                    last_progress_at=started_at,
                    roots_json='["tv"]',
                    scope="full",
                )
            )
            connection.commit()

            with patch.object(web_app, "_scan_process_is_alive", return_value=False):
                status = web_app._load_scan_status(connection, self.config, None)
            scan_row = connection.execute(select(scan_runs)).mappings().one()

        stored_job = web_app._load_scan_job_state(self.config, None)
        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertIsNone(scan_row["completed_at"])
        self.assertIsNotNone(stored_job)
        assert stored_job is not None
        self.assertEqual(stored_job["status"], "running")

    def test_dashboard_and_native_reads_launch_no_work(self) -> None:
        with patch("mediaforce.web.app.load_config", return_value=self.config):
            app = web_app.create_app(self.config.paths.config_path)

        dashboard_endpoint = self._endpoint(app, "/api/dashboard", "api_dashboard")
        dashboard_scan_job_endpoint = self._endpoint(
            app,
            "/api/dashboard/scan-job",
            "api_dashboard_scan_job",
        )
        hosts_endpoint = self._endpoint(app, "/api/hosts", "api_hosts")
        main_window_endpoint = self._endpoint(app, "/api/app/main-window", "api_app_main_window")
        poster_endpoint = self._endpoint(app, "/api/app/posters/{title_prefix:path}", "api_app_title_poster")

        unexpected_process = AssertionError("GET request attempted to launch a child process")
        with patch("subprocess.run", side_effect=unexpected_process) as run_mock, patch(
            "subprocess.Popen",
            side_effect=unexpected_process,
        ) as popen_mock, patch(
            "mediaforce.web.runtime.job_runtime.threading.Thread",
        ) as scan_thread_mock, patch(
            "mediaforce.web.runtime.host_status._schedule_host_status_refresh",
        ) as host_refresh_mock:
            dashboard_response = dashboard_endpoint(None)
            dashboard_scan_job_response = dashboard_scan_job_endpoint()
            hosts_response = hosts_endpoint(0)
            main_window_response = main_window_endpoint()
            poster_response = poster_endpoint("tv/missing")

        self.assertEqual(dashboard_response.status_code, 200)
        self.assertEqual(dashboard_scan_job_response.status_code, 200)
        self.assertEqual(hosts_response.status_code, 200)
        self.assertEqual(main_window_response.status_code, 200)
        self.assertEqual(poster_response.status_code, 404)
        run_mock.assert_not_called()
        popen_mock.assert_not_called()
        scan_thread_mock.assert_not_called()
        host_refresh_mock.assert_not_called()

    def test_native_resume_clears_saved_background_and_encode_pauses(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(evidence_queue_state.insert().values(
                queue_name=DEFAULT_EVIDENCE_QUEUE_NAME, batch_id="owner-paused-batch", status="queued",
                is_paused=1, updated_at="2026-10-28T12:00:00Z",
            ))
        for encode_paused in (False, True):
            with self.subTest(encode_paused=encode_paused):
                with open_db(self.config.paths.db_path) as connection:
                    set_background_work_paused(connection, is_paused=True)
                    state = load_queue_state(connection)
                    state.update({"is_paused": encode_paused, "stop_requested": True,
                                  "updated_at": "2026-10-28T12:00:00Z"})
                    save_queue_state(connection, state)
                with patch("mediaforce.web.app.load_config", return_value=self.config):
                    app = web_app.create_app(self.config.paths.config_path)
                with open_db(self.config.paths.db_path) as connection:
                    self.assertTrue(background_work_is_paused(connection))
                    self.assertEqual(load_queue_state(connection)["is_paused"], encode_paused)
                endpoint = self._endpoint(app, "/api/encode-queue/resume", "api_resume_encode_queue")
                response = endpoint()
                self.assertEqual(response.status_code, 200)
                with open_db(self.config.paths.db_path) as connection:
                    self.assertFalse(background_work_is_paused(connection))
                    state = load_queue_state(connection)
                    self.assertFalse(state["is_paused"])
                    self.assertFalse(state["stop_requested"])
                    self.assertFalse(load_evidence_queue_state(connection)["is_paused"])

    def test_native_resume_rolls_back_every_queue_when_evidence_resume_conflicts(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            set_background_work_paused(connection, is_paused=True)
            state = load_queue_state(connection)
            state.update({"is_paused": True, "stop_requested": True, "updated_at": "2026-10-29T12:00:00Z"})
            save_queue_state(connection, state)
            connection.execute(evidence_queue_state.insert().values(
                queue_name=DEFAULT_EVIDENCE_QUEUE_NAME,
                batch_id="paused-batch",
                status="paused",
                is_paused=1,
                cancel_requested=0,
                updated_at="2026-10-29T12:00:00Z",
            ))

        with patch("mediaforce.web.app.load_config", return_value=self.config):
            app = web_app.create_app(self.config.paths.config_path)
        endpoint = self._endpoint(app, "/api/encode-queue/resume", "api_resume_encode_queue")

        with patch(
            "mediaforce.web.runtime.queue_actions.resume_evidence_queue",
            create=True,
            side_effect=EvidenceQueueConflict,
        ), self.assertRaises(EvidenceQueueConflict):
            endpoint()

        with open_db(self.config.paths.db_path) as connection:
            self.assertTrue(load_queue_state(connection)["is_paused"])
            self.assertTrue(load_queue_state(connection)["stop_requested"])
            self.assertTrue(background_work_is_paused(connection))
            evidence_state = load_evidence_queue_state(connection)
            self.assertTrue(evidence_state["is_paused"])
            self.assertFalse(evidence_state["cancel_requested"])

    @staticmethod
    def _endpoint(app: FastAPI, path: str, name: str) -> Callable[..., Any]:
        return next(
            route.endpoint
            for route in app.router.routes
            if isinstance(route, APIRoute) and route.path == path and route.name == name
        )


if __name__ == "__main__":
    unittest.main()
