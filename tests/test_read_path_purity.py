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
from mediaforce.core.db_tables import scan_runs
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

    def test_dashboard_folder_status_and_host_reads_launch_no_work(self) -> None:
        with patch("mediaforce.web.app.load_config", return_value=self.config):
            app = web_app.create_app(self.config.paths.config_path)

        dashboard_endpoint = self._endpoint(app, "/api/dashboard", "api_dashboard")
        dashboard_scan_job_endpoint = self._endpoint(
            app,
            "/api/dashboard/scan-job",
            "api_dashboard_scan_job",
        )
        folder_endpoint = self._endpoint(app, "/api/folders/{prefix:path}", "api_folder")
        folder_status_endpoint = self._endpoint(
            app,
            "/api/folders/{prefix:path}/status",
            "api_folder_status",
        )
        hosts_endpoint = self._endpoint(app, "/api/hosts", "api_hosts")

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
            folder_response = folder_endpoint("tv/missing")
            folder_status_response = folder_status_endpoint("tv/missing")
            hosts_response = hosts_endpoint(0)

        self.assertEqual(dashboard_response.status_code, 200)
        self.assertEqual(dashboard_scan_job_response.status_code, 200)
        self.assertEqual(folder_response.status_code, 404)
        self.assertEqual(folder_status_response.status_code, 200)
        self.assertEqual(hosts_response.status_code, 200)
        run_mock.assert_not_called()
        popen_mock.assert_not_called()
        scan_thread_mock.assert_not_called()
        host_refresh_mock.assert_not_called()

    @staticmethod
    def _endpoint(app: FastAPI, path: str, name: str) -> Callable[..., Any]:
        return next(
            route.endpoint
            for route in app.router.routes
            if isinstance(route, APIRoute) and route.path == path and route.name == name
        )


if __name__ == "__main__":
    unittest.main()
