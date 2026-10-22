"""A sample that loses its computer tries again by itself, and asks the owner only when it must."""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sqlalchemy import update

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import calibration_jobs
from mediaforce.core.process_control import ManagedProcessController
from mediaforce.encoding.quality import QualitySearchError
from mediaforce.execution import resolve_stream_budget_ledger
from mediaforce.hosts.types import HostReadinessError, HostStatus
from mediaforce.tuning.calibration_jobs import load_job, save_job, update_job_telemetry
from mediaforce.web import app as web_app
from mediaforce.web.runtime import job_runtime
from mediaforce.web.runtime.calibration_runtime import SAMPLE_COMPUTER_RETRY_LIMIT, CalibrationRunDeps, \
    run_calibration_job
from mediaforce.web.runtime.queue_actions import stop_calibration_queue_action

NOW = "2026-10-21T23:38:00+00:00"
PREFIX = "tv/Futurama/Season 8"
JOB_ID = "sample-s08e02"
COMPUTER = "M2 MBP"
HOST = {"key": "cbusillo@m2-mbp.invalid", "host": "cbusillo@m2-mbp.invalid", "label": COMPUTER, "mode": "ssh"}
POLICY = {
    "video": {
        "pixel_format": "yuv420p10le",
        "sample_every": "8m",
        "sample_duration": "20s",
        "encoder": "libsvtav1",
    }
}
# What the OpenSSH client printed on 2026-10-21 when the MacBook slept.
HOST_DOWN_ON_CONNECT = "ssh: connect to host m2-mbp.invalid port 22: Host is down"
HOST_DOWN_MID_ENCODE = "client_loop: ssh_packet_write_poll: Connection to 10.0.0.12 port 22: Host is down"
REFUSED_LOGIN = "cbusillo@m2-mbp.invalid: Permission denied (publickey,password)."


def _ready_host(host_data: dict[str, Any]) -> HostStatus:
    return HostStatus(
        key=str(host_data["key"]),
        label=str(host_data["label"]),
        mode="ssh",
        priority=0,
        capabilities=["sample_calibration"],
        available=True,
        message="Mounted and ready",
        missing_paths=[],
    )


def _search_fails(error: Exception) -> Any:
    def _search(*_args: object, **_kwargs: object) -> None:
        raise error

    return _search


class SampleComputerRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        (self.root / "episode.mkv").write_text("episode")
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
                db_path=self.root / "database" / "library.sqlite3",
                run_manifest_dir=self.root / "runs",
                web_state_dir=self.root / "web",
                review_dir=self.root / "review",
                runtime_settings_path=self.root / "runtime.json",
                runtime_reservation_dir=self.root / "runtime-reservations",
            ),
        )

    def _save_job(self, *, status: str, result: dict[str, Any] | None = None) -> None:
        with open_db(self.config.paths.db_path) as connection:
            save_job(
                connection,
                {
                    "job_id": JOB_ID,
                    "prefix": PREFIX,
                    "status": status,
                    "lane": "sample",
                    "action": "baseline",
                    "host": {**HOST, "staging_root": str(self.root / "staging")},
                    "policy": POLICY,
                    "sample_item": {},
                    "result": result,
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            )
            connection.commit()

    def _job(self) -> dict[str, Any]:
        with open_db(self.config.paths.db_path) as connection:
            job = load_job(connection, JOB_ID)
        assert job is not None
        return job

    def _deps(self, *, now: str = NOW, host_ready: Any = _ready_host, search: Any = None) -> CalibrationRunDeps:
        def _save_job_state(connection: DBClient, _config: object, prefix: str, payload: dict[str, Any]) -> None:
            save_job(connection, {**payload, "prefix": prefix, "updated_at": now})

        def _load_job_state(connection: DBClient, _config: object, _prefix: str) -> dict[str, Any] | None:
            return load_job(connection, JOB_ID)

        def _unused(*_args: object, **_kwargs: object) -> Any:
            raise AssertionError("A sample whose computer is gone gets no further.")

        return CalibrationRunDeps(
            now_iso=lambda: now,
            ensure_sample_host_ready=lambda _config, host_data: host_ready(host_data),
            load_job_state=_load_job_state,
            sample_item=lambda *_args, **_kwargs: {
                "rel_path": f"{PREFIX}/episode.mkv",
                "source_path": str(self.root / "episode.mkv"),
                "source_size_bytes": 8_000_000_000,
                "video_codec": "h264",
                "video_bitrate": 8_000_000,
                "duration_seconds": 1320.0,
                "audio_summary": [],
                "subtitle_summary": [],
            },
            save_job_state=_save_job_state,
            update_job_telemetry=update_job_telemetry,
            calibration_job_compatibility_key=lambda _payload: "test-profile",
            calibration_heartbeat_seconds=60.0,
            save_calibration_state=_unused,
            record_run_verdict=_unused,
            summarize_calibration_result=_unused,
            calibration_mode_for_action=lambda _action: "sample",
            effective_video_preset=lambda *_args, **_kwargs: 4,
            search_quality_for_source=search or _unused,
            run_sample_encode=_unused,
            detect_video_crop=lambda *_args, **_kwargs: None,
            recommend_review_timestamps=_unused,
            encode_preview_clips=_unused,
            render_source_review_clips=_unused,
            generate_compare_clips_from_review_pairs=_unused,
            resolve_stream_budget_ledger=resolve_stream_budget_ledger,
            build_svt_params=lambda *_args, **_kwargs: [],
            review_url=_unused,
            encode_manifest_items=_unused,
            validate_manifest_items=_unused,
            generate_compare_clips=_unused,
            staged_artifact_columns=("library_item_id",),
        )

    def _run(self, deps: CalibrationRunDeps, controller: ManagedProcessController | None = None) -> None:
        job = self._job()
        with patch("mediaforce.web.runtime.calibration_runtime.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.calibration_runtime.purge_transient_artifacts"
        ):
            run_calibration_job(
                config_path=self.config.paths.config_path,
                prefix=PREFIX,
                action="baseline",
                host_data=dict(job["host"]),
                notes="",
                policy=POLICY,
                job_id=JOB_ID,
                seed_metadata=None,
                process_controller=controller or ManagedProcessController(),
                deps=deps,
            )

    def _queue_pass(self, *, now: str, window_open: bool = True) -> list[str]:
        deps = web_app._calibration_queue_runtime_deps()
        deps.now_iso = lambda: now
        deps.host_window_open = lambda _config, _host: window_open
        with patch("mediaforce.web.runtime.job_runtime.load_config", return_value=self.config), patch(
                "mediaforce.web.runtime.job_runtime.dispatch_calibration_job"
        ) as dispatch:
            job_runtime.process_calibration_queue_once(config_path=self.config.paths.config_path, deps=deps)
        return [str(call.args[1]["job_id"]) for call in dispatch.call_args_list]

    def _assert_waiting_for_computer(self, job: dict[str, Any], *, retry_not_before: str) -> None:
        self.assertEqual(job["status"], "queued")
        self.assertIsNone(job["error"])
        self.assertIsNone(job["finished_at"])
        progress = job["progress"]
        self.assertEqual(progress["stage"], "waiting_for_computer")
        self.assertEqual(progress["waiting_for_computer"], COMPUTER)
        self.assertTrue(progress["waiting_reason"].startswith(f"Waiting for {COMPUTER} to be available"))
        self.assertEqual(progress["retry_not_before"], retry_not_before)

    def test_a_link_dropped_mid_sample_waits_for_its_computer_and_tries_again(self) -> None:
        self._save_job(status="running")

        self._run(self._deps(search=_search_fails(QualitySearchError(f"Encoding sample 2/4\n{HOST_DOWN_MID_ENCODE}"))))

        self._assert_waiting_for_computer(self._job(), retry_not_before="2026-10-21T23:39:00+00:00")
        self.assertEqual(self._queue_pass(now="2026-10-21T23:38:30+00:00"), [])
        self.assertEqual(self._queue_pass(now="2026-10-21T23:39:00+00:00"), [JOB_ID])

    def test_an_unreachable_computer_at_start_waits_then_runs_inside_its_work_window(self) -> None:
        self._save_job(status="running")

        def _host_is_down(_host_data: dict[str, Any]) -> HostStatus:
            raise HostReadinessError(HOST_DOWN_ON_CONNECT, failure_kind="host_unavailable")

        self._run(self._deps(host_ready=_host_is_down))

        self._assert_waiting_for_computer(self._job(), retry_not_before="2026-10-21T23:39:00+00:00")
        after_wait = "2026-10-21T23:45:00+00:00"
        self.assertEqual(self._queue_pass(now=after_wait, window_open=False), [])
        self.assertEqual(self._job()["status"], "queued")
        self.assertEqual(self._queue_pass(now=after_wait, window_open=True), [JOB_ID])

        # The computer is back: the sample runs past the computer check into its own work.
        searched: list[bool] = []

        def _search(*_args: object, **_kwargs: object) -> None:
            searched.append(True)
            raise QualitySearchError("ab-av1: no crf found")

        self._run(self._deps(now=after_wait, search=_search))
        self.assertEqual(searched, [True])
        self.assertEqual(self._job()["status"], "failed")

    def test_a_refused_login_asks_the_owner_straight_away(self) -> None:
        for where in ("connect", "mid_sample"):
            with self.subTest(where=where):
                self._save_job(status="running")

                def _refused(_host_data: dict[str, Any]) -> HostStatus:
                    raise HostReadinessError(REFUSED_LOGIN, failure_kind="host_unavailable")

                deps = (
                    self._deps(host_ready=_refused)
                    if where == "connect"
                    else self._deps(search=_search_fails(QualitySearchError(f"{REFUSED_LOGIN}\n{HOST_DOWN_ON_CONNECT}")))
                )
                self._run(deps)

                job = self._job()
                self.assertEqual(job["status"], "failed")
                self.assertIn("Permission denied", job["error"])

    def test_a_sample_asks_the_owner_once_its_retries_are_used_up(self) -> None:
        self._save_job(status="running", result={"computer_retry": {"attempts": SAMPLE_COMPUTER_RETRY_LIMIT}})

        self._run(self._deps(search=_search_fails(QualitySearchError(HOST_DOWN_MID_ENCODE))))

        job = self._job()
        self.assertEqual(job["status"], "failed")
        self.assertIn(COMPUTER, job["error"])
        self.assertIn("retry the sample", job["error"])
        self.assertEqual(job["result"]["failure_kind"], "host_unreachable")
        self.assertEqual(job["result"]["tries"], SAMPLE_COMPUTER_RETRY_LIMIT + 1)
        self.assertEqual(self._queue_pass(now="2026-10-22T09:00:00+00:00"), [])

    def test_waits_between_tries_grow(self) -> None:
        waits: list[timedelta] = []
        for retries_used in range(SAMPLE_COMPUTER_RETRY_LIMIT):
            self._save_job(status="running", result={"computer_retry": {"attempts": retries_used}})
            self._run(self._deps(search=_search_fails(QualitySearchError(HOST_DOWN_MID_ENCODE))))
            job = self._job()
            self.assertEqual(job["result"]["computer_retry"]["attempts"], retries_used + 1)
            waits.append(datetime.fromisoformat(job["progress"]["retry_not_before"]) - datetime.fromisoformat(NOW))
        self.assertEqual(waits, sorted(waits))
        self.assertLess(waits[0], waits[-1])

    def test_the_owners_stop_is_not_undone_by_a_retry(self) -> None:
        with self.subTest("stop ends the link"):
            self._save_job(status="running")
            controller = ManagedProcessController()

            def _stopped_mid_search(*_args: object, **_kwargs: object) -> None:
                controller.cancel()
                raise QualitySearchError(HOST_DOWN_MID_ENCODE)

            self._run(self._deps(search=_stopped_mid_search), controller)

            self.assertEqual(self._job()["status"], "stopped")
            self.assertEqual(self._queue_pass(now="2026-10-22T09:00:00+00:00"), [])

        with self.subTest("stop lands while the try is ending"):
            self._save_job(status="running")

            def _owner_stops_then_link_drops(*_args: object, **_kwargs: object) -> None:
                with open_db(self.config.paths.db_path) as connection:
                    connection.execute(
                        update(calibration_jobs).where(calibration_jobs.c.job_id == JOB_ID).values(status="stopped")
                    )
                    connection.commit()
                raise QualitySearchError(HOST_DOWN_MID_ENCODE)

            self._run(self._deps(search=_owner_stops_then_link_drops))

            self.assertEqual(self._job()["status"], "stopped")
            self.assertEqual(self._queue_pass(now="2026-10-22T09:00:00+00:00"), [])

        with self.subTest("stop while waiting"):
            self._save_job(status="running")
            self._run(self._deps(search=_search_fails(QualitySearchError(HOST_DOWN_MID_ENCODE))))
            self.assertEqual(self._job()["status"], "queued")

            stop_calibration_queue_action(
                connection_factory=lambda: open_db(self.config.paths.db_path),
                config=self.config,
                now_iso=lambda: NOW,
                active_calibration_process_controllers=lambda: [],
                load_job_state=lambda connection, _config, _prefix: load_job(connection, JOB_ID),
                save_job_state=lambda connection, _config, prefix, payload: save_job(
                    connection, {**payload, "prefix": prefix, "updated_at": NOW}
                ),
            )

            self.assertEqual(self._job()["status"], "stopped")
            self.assertEqual(self._queue_pass(now="2026-10-22T09:00:00+00:00"), [])


if __name__ == "__main__":
    unittest.main()
