from contextlib import contextmanager
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.process_control import ScheduleWindowClosedError
from mediaforce.hosts.work_window import controller_work_allowed, controller_work_schedule
from mediaforce.library.scanner import scan_library
from mediaforce.state_cleanup import purge_transient_artifacts
from mediaforce.web.runtime.automatic_publish import publish_checked_files_once


@contextmanager
def controller_clock(instant: datetime) -> Iterator[MagicMock]:
    clock = MagicMock(wraps=datetime)
    clock.now.return_value = instant
    with patch("mediaforce.web.runtime.encode_scheduler.datetime", clock), patch(
        "mediaforce.library.evidence_hosts.datetime", clock
    ), patch("mediaforce.web.runtime.encode_scheduler.system_timezone", return_value=ZoneInfo("America/New_York")):
        yield clock


def configure_controller_night(config: MediaforceConfig) -> None:
    config.raw["remote_hosts"] = [{"host": "localhost", "label": "Studio", "mode": "local", "schedule_profile": "night",
                                  "timezone": "America/New_York"}]
    config.raw["encode_queue"] = {"schedule_profiles": [{
        "key": "night", "mode": "night", "start_hour": 0, "end_hour": 5,
        "timezone": "host_local", "finish_current_file": True,
    }]}


def _config() -> MediaforceConfig:
    root = Path("/fixture")
    config = MediaforceConfig(raw={}, paths=ConfigPaths(
        project_root=root, config_path=root / "config.toml", db_path=root / "db.sqlite",
        run_manifest_dir=root / "runs", web_state_dir=root / "web", review_dir=root / "review",
        runtime_settings_path=root / "settings.json",
    ))
    configure_controller_night(config)
    return config


def test_controller_work_stops_at_close_without_probing_or_opening_database() -> None:
    with controller_clock(datetime(2026, 10, 29, 8, 59, 59, tzinfo=UTC)) as clock, patch(
        "mediaforce.hosts.config.socket.getfqdn", side_effect=AssertionError
    ), patch("mediaforce.core.db.open_db", side_effect=AssertionError):
        before = controller_work_schedule(_config())
        assert before.is_open
        assert before.closes_at == datetime(2026, 10, 29, 9, tzinfo=UTC)
        clock.now.return_value = datetime(2026, 10, 29, 9, tzinfo=UTC)
        after = controller_work_schedule(_config())
        assert not before.is_open
        assert not after.is_open
        assert after.next_opens_at == datetime(2026, 10, 30, 4, tzinfo=UTC)


def test_remote_computers_do_not_close_the_controller_window() -> None:
    config = _config()
    config.raw["remote_hosts"] = [{"host": "remote.invalid", "mode": "ssh", "schedule_profile": "never"}]
    with patch("mediaforce.library.evidence_hosts._controller_target_matches", return_value=False):
        assert controller_work_schedule(config).is_open


def test_never_means_no_local_heavy_work_without_changing_remote_schedules() -> None:
    config = _config()
    config.raw["remote_hosts"][0]["schedule_profile"] = "never"
    window = controller_work_schedule(config)
    assert not window.is_open
    assert window.next_opens_at is None


def test_all_work_uses_the_existing_first_controller_entry() -> None:
    from mediaforce.library.evidence_hosts import controller_digest_window

    config = _config()
    config.raw["remote_hosts"].append({"host": "localhost", "mode": "local", "schedule_profile": "always"})
    with controller_clock(datetime(2026, 10, 29, 9, tzinfo=UTC)):
        window = controller_work_schedule(config)
        assert not window.is_open
        assert window.next_opens_at is not None
        config.raw["remote_hosts"].reverse()
        window = controller_work_schedule(config)
        digest = controller_digest_window(config, lambda _config: [{"key": "localhost", "schedule_open": True}])
        assert window.is_open == digest.is_open
        assert window.is_open


@pytest.mark.parametrize("work", ["scan", "publish", "cleanup"])
def test_real_local_work_entrypoints_wait_before_touching_files_or_database(work: str) -> None:
    config = _config()
    connection = Mock()
    with controller_clock(datetime(2026, 10, 29, 9, tzinfo=UTC)) as clock, patch(
        "mediaforce.web.runtime.automatic_publish.open_db", side_effect=AssertionError
    ), patch("mediaforce.state_cleanup.runtime_activity", side_effect=AssertionError):
        clock.now.return_value = datetime(2026, 10, 29, 9, tzinfo=UTC)
        assert not controller_work_allowed(config)
        if work == "scan":
            with pytest.raises(ScheduleWindowClosedError):
                scan_library(connection, config)
            connection.execute.assert_not_called()
        elif work == "publish":
            publish_checked_files_once(config, load_calibration_state=lambda *_args: None, remake_files=lambda *_args: {})
        else:
            assert not purge_transient_artifacts(config, force=True)


@pytest.mark.parametrize("argv", [["scan"], ["campaign", "tv/Show"]])
def test_cli_reports_a_window_refusal_without_a_traceback(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    from mediaforce import cli

    with patch("mediaforce.cli.load_config", return_value=_config()), patch(
        "mediaforce.cli._main", side_effect=ScheduleWindowClosedError("Waiting for this computer's work window.")
    ):
        assert cli.main(argv) == 2
    output = capsys.readouterr()
    assert output.out.strip()
    assert not output.err
