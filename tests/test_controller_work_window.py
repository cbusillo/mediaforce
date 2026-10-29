import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.web import app as web_app
from mediaforce.web.runtime.encode_scheduler import controller_work_schedule
from mediaforce.web.runtime.worker_supervision import run_supervised_worker_loop


def _config() -> MediaforceConfig:
    root = Path("/fixture")
    config = MediaforceConfig(raw={}, paths=ConfigPaths(
        project_root=root, config_path=root / "config.toml", db_path=root / "db.sqlite",
        run_manifest_dir=root / "runs", web_state_dir=root / "web", review_dir=root / "review",
        runtime_settings_path=root / "settings.json",
    ))
    config.raw["encode_queue"] = {"schedule_profiles": [{
        "key": "night", "mode": "night", "start_hour": 0, "end_hour": 5,
        "timezone": "host_local", "finish_current_file": True,
    }]}
    return config


def test_controller_work_stops_at_close_without_probing_or_opening_database() -> None:
    config = _config()
    config.raw["remote_hosts"] = [{
        "host": "localhost", "label": "Studio", "mode": "local", "schedule_profile": "night",
        "timezone": "America/New_York",
    }]
    deps = web_app._encode_scheduler_deps()
    with patch("mediaforce.web.runtime.host_status.collect_host_statuses", side_effect=AssertionError), patch(
        "mediaforce.web.app.open_db", side_effect=AssertionError
    ):
        before = controller_work_schedule(config, deps, now=datetime(2026, 10, 29, 8, 59, 59, tzinfo=UTC))
        after = controller_work_schedule(config, deps, now=datetime(2026, 10, 29, 9, tzinfo=UTC))
    assert before.is_open
    assert not after.is_open
    assert before.current_closes_at == datetime(2026, 10, 29, 9, tzinfo=UTC)
    assert after.next_opens_at == datetime(2026, 10, 30, 4, tzinfo=UTC)


def test_remote_computers_do_not_close_the_controller_window() -> None:
    config = _config()
    config.raw["remote_hosts"] = [{"host": "remote.invalid", "mode": "ssh", "schedule_profile": "never"}]
    with patch("mediaforce.web.runtime.encode_scheduler.host_targets_current_machine", return_value=False):
        assert controller_work_schedule(config, web_app._encode_scheduler_deps()).is_open


def test_supervised_work_rechecks_the_window_before_every_pass() -> None:
    stop = threading.Event()
    allowed = iter([False, True, False])
    calls: list[str] = []

    def wait(_seconds: float) -> None:
        calls.append("wait")
        if calls.count("wait") == 3:
            stop.set()

    run_supervised_worker_loop(
        process_once_fn=lambda: calls.append("work"), poll_seconds=1, stop_event=stop,
        logger=logging.getLogger(__name__), failure_message="failed", wait_for_next_poll_fn=wait,
        work_allowed_fn=lambda: next(allowed),
    )
    assert calls == ["wait", "work", "wait", "wait"]


def test_failed_window_check_does_not_start_work_or_kill_the_worker() -> None:
    stop = threading.Event()
    work = Mock()
    logger = Mock()
    run_supervised_worker_loop(
        process_once_fn=work, poll_seconds=1, stop_event=stop, logger=logger, failure_message="failed",
        work_allowed_fn=Mock(side_effect=OSError("settings unavailable")),
        wait_for_next_poll_fn=lambda _seconds: stop.set(),
    )
    work.assert_not_called()
    logger.exception.assert_called_once()


def test_kinds_report_waits_without_starting_a_read_and_recovers_when_window_opens() -> None:
    from fastapi import FastAPI
    from starlette.routing import Route

    from mediaforce.web.routes.app_window import register_app_window_routes

    app = FastAPI()
    opened = False
    report = Mock(return_value={"kinds": []})
    register_app_window_routes(
        app, main_window_payload=dict, tv_shows_payload=dict, movies_payload=dict, other_media_payload=dict,
        sample_payload=lambda _prefix: None, approve_sample_action=lambda *_args: {}, make_sample_action=lambda *_args: {},
        compress_season_action=lambda _prefix: {}, kinds_payload=report, approve_kind_action=lambda *_args: {},
        stop_kind_action=lambda _kind: {}, controller_work_allowed=lambda: opened,
    )
    route = next(route for route in app.routes if isinstance(route, Route) and route.path == "/api/app/kinds")
    assert isinstance(route, Route)
    endpoint = route.endpoint
    assert endpoint().status_code == 503
    report.assert_not_called()
    opened = True
    assert endpoint().status_code == 200
    report.assert_called_once()


def test_evidence_waits_before_host_probes_or_claims() -> None:
    from dataclasses import replace

    from mediaforce.library.evidence_worker import default_evidence_worker_deps, process_evidence_queue_once

    config = _config()
    deps = replace(default_evidence_worker_deps(), controller_work_allowed=lambda _config: False)
    with patch("mediaforce.library.evidence_worker.open_db", side_effect=AssertionError), patch(
        "mediaforce.library.evidence_worker._unmeasurable_roots", side_effect=AssertionError
    ):
        assert process_evidence_queue_once(config_path=config.paths.config_path, config=config, deps=deps) == 0


def test_publication_waits_before_reading_files() -> None:
    from mediaforce.web.runtime.automatic_publish import publish_checked_files_once

    with patch("mediaforce.web.runtime.automatic_publish.open_db", side_effect=AssertionError):
        publish_checked_files_once(
            _config(), load_calibration_state=lambda *_args: None, remake_files=lambda *_args: {},
            controller_work_allowed=lambda _config: False,
        )


def test_sample_dispatch_waits_before_opening_database() -> None:
    from dataclasses import replace

    from mediaforce.web.runtime.job_runtime import process_calibration_queue_once

    config = _config()
    deps = replace(web_app._calibration_queue_runtime_deps(), controller_work_allowed=lambda _config: False)
    with patch("mediaforce.web.runtime.job_runtime.load_config", return_value=config), patch(
        "mediaforce.web.runtime.job_runtime.open_db", side_effect=AssertionError
    ):
        process_calibration_queue_once(config_path=config.paths.config_path, deps=deps)
