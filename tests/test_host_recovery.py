from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from typing import Any
from unittest.mock import patch

from fastapi.routing import APIRoute

import pytest

from mediaforce.core.config import ConfigPaths, MediaforceConfig, load_runtime_settings, save_runtime_settings
from mediaforce.hosts.controller_mount import ControllerMountProbe
from mediaforce.hosts.mount_runtime import ControllerSmbMount, NoUiMountResult, save_controller_smb_mounts, \
    controller_smb_mounts_path
from mediaforce.remote import HostStatus
from mediaforce.web import app as web_app
from mediaforce.web.runtime import host_recovery as recovery, host_status, recovery_alerts as alerts

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


@pytest.fixture
def config(tmp_path: Path) -> MediaforceConfig:
    config = MediaforceConfig(
        raw={"media": {"source_roots": {"tv": "/Volumes/media/tv"}, "staging_root": "/Volumes/media/transcode"},
             "remote_hosts": [{"host": "user@worker", "label": "Worker", "media_access": "mounted"}]},
        paths=ConfigPaths(project_root=tmp_path, config_path=tmp_path / "config.toml",
                          db_path=tmp_path / "db.sqlite", run_manifest_dir=tmp_path / "manifests",
                          web_state_dir=tmp_path / "web", review_dir=tmp_path / "review",
                          runtime_settings_path=tmp_path / "runtime-settings.json"),
    )
    save_controller_smb_mounts(controller_smb_mounts_path(config.paths.runtime_settings_path), [
        ControllerSmbMount("//user@nas/media", Path("/Volumes/media")),
    ])
    return config


def status(*, available: bool = False) -> HostStatus:
    return HostStatus(key="user@worker", label="Worker", mode="ssh", priority=1,
                      capabilities=["encode_queue"], available=available, message="Storage missing",
                      missing_paths=[] if available else ["/Volumes/media/tv"], platform="macos",
                      missing_mounts=[] if available else ["/Volumes/media"])


def test_background_recovery_runs_without_work_and_preserves_backoff_on_restart(config: MediaforceConfig) -> None:
    with patch.object(recovery, "datetime", wraps=datetime) as clock, patch.object(
        recovery, "probe_host_status", return_value=status(),
    ) as probe, patch.object(recovery, "recover_remote_host_mounts_no_ui", return_value=NoUiMountResult(ok=False)) as mount:
        clock.now.return_value = NOW
        recovery.process_host_recovery_once(config)
        saved = recovery.host_recovery_snapshot(config)
        retry_at = datetime.fromisoformat(saved["hosts"]["user@worker"]["next_retry_at"])
        assert retry_at > NOW
        # The function re-reads only durable state; no in-memory timer authorizes a retry.
        recovery.process_host_recovery_once(config)
        assert mount.call_count == 1
        assert probe.call_count == 2
        clock.now.return_value = retry_at
        recovery.process_host_recovery_once(config)
        assert mount.call_count == 2
        next_retry = datetime.fromisoformat(recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]["next_retry_at"])
        assert next_retry - retry_at > retry_at - NOW
        probe.return_value = status(available=True)
        clock.now.return_value = next_retry
        recovery.process_host_recovery_once(config)
        assert recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]["status"] == "ready"


def test_uncertain_remote_attempt_survives_restart_until_fresh_readiness(config: MediaforceConfig) -> None:
    with patch.object(recovery, "datetime", wraps=datetime) as clock, patch.object(
        recovery, "probe_host_status", return_value=status(),
    ) as probe, patch.object(recovery, "recover_remote_host_mounts_no_ui", side_effect=RuntimeError("lost result")) as mount:
        clock.now.return_value = NOW
        with pytest.raises(RuntimeError):
            recovery.process_host_recovery_once(config)
        clock.now.return_value = NOW + timedelta(hours=1)
        recovery.process_host_recovery_once(config)
        assert mount.call_count == 1
        assert recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]["status"] == "action_required"
        probe.return_value = status(available=True)
        clock.now.return_value += timedelta(hours=1)
        recovery.process_host_recovery_once(config)
        assert not recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]["mount_uncertain"]


def test_sign_in_failure_asks_once_and_stops_reconnecting_until_storage_returns(config: MediaforceConfig) -> None:
    needs_password = NoUiMountResult(ok=False, action="Worker needs its share password saved.", retry=False)
    with patch.object(recovery, "datetime", wraps=datetime) as clock, patch.object(
        recovery, "probe_host_status", return_value=status(),
    ) as probe, patch.object(
        recovery, "recover_remote_host_mounts_no_ui", return_value=needs_password,
    ) as mount, patch.object(alerts, "datetime", wraps=datetime) as alert_clock:
        clock.now.return_value = alert_clock.now.return_value = NOW
        recovery.process_host_recovery_once(config)
        record = recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]
        assert (record["status"], record["reason"]) == ("action_required", needs_password.action)
        [alert] = [item for item in alerts.recovery_alerts(config) if item["id"].startswith("host:")]
        assert needs_password.action in alert["detail"]
        for hours in range(1, 4):
            clock.now.return_value = NOW + timedelta(hours=hours)
            recovery.process_host_recovery_once(config)
        assert mount.call_count == 1
        assert probe.call_count == 4, "the computer is still checked, only reconnecting waits"
        assert recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]["reason"] == needs_password.action
        # Someone connected the share in Finder or used Prepare: the next check clears the request.
        probe.return_value = status(available=True)
        clock.now.return_value += timedelta(hours=1)
        recovery.process_host_recovery_once(config)
        assert recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]["status"] == "ready"
        assert not [item for item in alerts.recovery_alerts(config) if item["id"].startswith("host:")]
        # A later outage reconnects automatically again.
        probe.return_value = status()
        clock.now.return_value += timedelta(hours=1)
        recovery.process_host_recovery_once(config)
        assert mount.call_count == 2


def test_missing_login_asks_but_keeps_reconnecting_on_its_own(config: MediaforceConfig) -> None:
    not_logged_in = NoUiMountResult(ok=False, action="Log in on Worker.")
    with patch.object(recovery, "datetime", wraps=datetime) as clock, patch.object(
        recovery, "probe_host_status", return_value=status(),
    ) as probe, patch.object(
        recovery, "recover_remote_host_mounts_no_ui", return_value=not_logged_in,
    ) as mount:
        clock.now.return_value = NOW
        recovery.process_host_recovery_once(config)
        record = recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]
        assert (record["status"], record["reason"]) == ("action_required", not_logged_in.action)
        clock.now.return_value = NOW + timedelta(hours=1)
        mount.return_value = NoUiMountResult(ok=True)
        probe.side_effect = [status(), status(available=True)]
        recovery.process_host_recovery_once(config)
        assert mount.call_count == 2
        assert recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]["status"] == "ready"


@pytest.fixture
def empty_host_status_cache() -> Iterator[None]:
    def reset() -> None:
        host_status._HOST_STATUS_CACHE_KEY = None
        host_status._HOST_STATUS_CACHE_VALUE = []
        host_status._HOST_STATUS_CACHE_REFRESH_FUTURE = None
        host_status._LOCAL_HOST_STORAGE_SIGNATURE = None

    reset()
    yield
    reset()


def _host_cards(config: MediaforceConfig) -> dict[str, dict[str, Any]]:
    with patch.object(web_app, "load_config", return_value=config):
        app = web_app.create_app(config.paths.config_path)
    endpoint = next(
        route.endpoint for route in app.router.routes
        if isinstance(route, APIRoute) and route.path == "/api/hosts"
    )
    # Reading the cards must not check anything itself.
    with patch("subprocess.run", side_effect=AssertionError("read launched a check")), patch.object(
        host_status, "_schedule_host_status_refresh",
    ) as schedule:
        response = endpoint(0)
    schedule.assert_not_called()
    return {host["host"]: host for host in json.loads(response.body)["hosts"]}


def test_idle_background_checks_keep_remote_cards_current(
        config: MediaforceConfig, empty_host_status_cache: None,
) -> None:
    with patch.object(host_status, "collect_host_statuses", return_value=[status(available=True)]):
        host_status.refresh_host_status_cache(config)
    assert _host_cards(config)["user@worker"]["available"]
    with patch.object(recovery, "datetime", wraps=datetime) as clock, patch.object(
        recovery, "probe_host_status", return_value=status(),
    ) as probe, patch.object(recovery, "recover_remote_host_mounts_no_ui", return_value=NoUiMountResult(ok=False)):
        clock.now.return_value = NOW
        recovery.process_host_recovery_once(config)
        card = _host_cards(config)["user@worker"]
        assert not card["available"]
        assert card["probe_message"] == status().message
        probe.return_value = status(available=True)
        clock.now.return_value = NOW + timedelta(hours=1)
        recovery.process_host_recovery_once(config)
        assert _host_cards(config)["user@worker"]["available"]


def test_host_card_says_what_to_do_when_reconnecting_waits_for_someone(
        config: MediaforceConfig, empty_host_status_cache: None,
) -> None:
    needs_password = NoUiMountResult(ok=False, action="Worker needs its share password saved.", retry=False)
    with patch.object(recovery, "probe_host_status", return_value=status()), patch.object(
        recovery, "recover_remote_host_mounts_no_ui", return_value=needs_password,
    ):
        recovery.process_host_recovery_once(config)
    card = _host_cards(config)["user@worker"]
    assert card["message"] == needs_password.action


def test_controller_card_follows_its_storage_after_a_restart(
        config: MediaforceConfig, empty_host_status_cache: None,
) -> None:
    config.raw["remote_hosts"] = [
        {"host": "user@localhost", "label": "Controller", "media_access": "mounted"},
        *config.raw["remote_hosts"],
    ]

    def controller(*, available: bool) -> HostStatus:
        row = status(available=available)
        row.key, row.label = "user@localhost", "Controller"
        return row

    def storage(state: str) -> dict[str, Any]:
        return {"mounts": [{"mount_point": "/Volumes/media", "status": state}], "state_error": None}

    # The first check after a reboot ran before Finder reconnected the share.
    with patch.object(host_status, "collect_host_statuses", return_value=[
        controller(available=False), status(available=True),
    ]):
        host_status.refresh_host_status_cache(config)
    assert not _host_cards(config)["user@localhost"]["available"]
    with patch.object(web_app, "load_config", return_value=config), patch.object(
        web_app, "process_controller_storage_recovery_once",
    ), patch.object(
        host_status, "controller_storage_recovery_snapshot", return_value=storage("ready"),
    ) as snapshot, patch.object(
        host_status, "probe_host_status", return_value=controller(available=True),
    ) as probe:
        web_app._controller_storage_pass(config.paths.config_path, threading.Event())
        cards = _host_cards(config)
        assert cards["user@localhost"]["available"]
        assert cards["user@worker"]["available"]
        web_app._controller_storage_pass(config.paths.config_path, threading.Event())
        assert probe.call_count == 1, "unchanged storage needs no second check"
        snapshot.return_value = storage("retrying")
        probe.return_value = controller(available=False)
        web_app._controller_storage_pass(config.paths.config_path, threading.Event())
        assert not _host_cards(config)["user@localhost"]["available"]
        assert {call.args[1]["host"] for call in probe.call_args_list} == {"user@localhost"}


def test_unreachable_host_retries_and_eventually_notifies_without_mounting(config: MediaforceConfig) -> None:
    unreachable = status()
    unreachable.platform = "unknown"
    unreachable.missing_mounts = []
    with patch.object(recovery, "datetime", wraps=datetime) as clock, patch.object(
        recovery, "probe_host_status", return_value=unreachable,
    ), patch.object(recovery, "recover_remote_host_mounts_no_ui") as mount, patch.object(alerts, "datetime", wraps=datetime) as alert_clock:
        clock.now.return_value = alert_clock.now.return_value = NOW
        recovery.process_host_recovery_once(config)
        assert not [item for item in alerts.recovery_alerts(config) if item["id"].startswith("host:")]
        clock.now.return_value = alert_clock.now.return_value = NOW + timedelta(hours=1)
        recovery.process_host_recovery_once(config)
        assert [item for item in alerts.recovery_alerts(config) if item["id"].startswith("host:")]
        mount.assert_not_called()


def test_storage_alert_is_immediate_deduplicated_persistent_and_resolves(config: MediaforceConfig) -> None:
    storage = {"mounts": [{"mount_point": "/Volumes/media", "first_failure_at": NOW.isoformat(),
                           "status": "retrying", "reason": "Share missing"}]}
    with patch.object(alerts, "controller_storage_recovery_snapshot", return_value=storage), patch.object(alerts.LOGGER, "warning") as warning:
        alerts.record_recovery_alerts(config)
        alerts.record_recovery_alerts(config)
        assert warning.call_count == 1
        assert len(alerts.recovery_alerts(config)) == 1
        saved = load_runtime_settings(config.paths.runtime_settings_path.with_name("recovery-alerts.json"))
        assert saved["alerts"] == alerts.recovery_alerts(config)
        storage["mounts"][0]["status"] = "ready"
        alerts.record_recovery_alerts(config)
        assert not alerts.recovery_alerts(config)
        storage["mounts"][0].update(status="retrying", first_failure_at=(NOW + timedelta(hours=1)).isoformat())
        alerts.record_recovery_alerts(config)
        assert warning.call_count == 2


@pytest.mark.parametrize("payload", [
    {"schema_version": 999},
    {"schema_version": 1, "hosts": {"user@worker": {"status": "retrying"}}},
])
def test_stopped_worker_and_corrupt_state_do_not_mount(config: MediaforceConfig, payload: dict[str, object]) -> None:
    save_runtime_settings(recovery.host_recovery_path(config), payload)
    with patch.object(recovery, "probe_host_status", return_value=status()), patch.object(recovery, "recover_remote_host_mounts_no_ui") as mount:
        recovery.process_host_recovery_once(config)
        mount.assert_not_called()
    stop = threading.Event()
    stop.set()
    with patch.object(recovery, "probe_host_status") as probe:
        recovery.process_host_recovery_once(config, stop)
        probe.assert_not_called()


def test_corrupt_state_warning_clears_after_manual_recovery_is_verified(config: MediaforceConfig) -> None:
    save_runtime_settings(recovery.host_recovery_path(config), {"schema_version": 999})
    with patch.object(recovery, "probe_host_status", return_value=status(available=True)), patch.object(
        recovery, "recover_remote_host_mounts_no_ui",
    ) as mount:
        recovery.process_host_recovery_once(config)
        assert not recovery.host_recovery_snapshot(config).get("state_error")
        mount.assert_not_called()


def test_prolonged_outage_backoff_is_capped(config: MediaforceConfig) -> None:
    unreachable = status()
    unreachable.platform = "unknown"
    unreachable.missing_mounts = []
    with patch.object(recovery, "datetime", wraps=datetime) as clock, patch.object(
        recovery, "probe_host_status", return_value=unreachable,
    ):
        clock.now.return_value = NOW
        recovery.process_host_recovery_once(config)
        snapshot = recovery.host_recovery_snapshot(config)
        snapshot["hosts"][unreachable.key].update(failure_count=1000, next_retry_at=None)
        save_runtime_settings(recovery.host_recovery_path(config), snapshot)
        recovery.process_host_recovery_once(config)
        retry_at = datetime.fromisoformat(recovery.host_recovery_snapshot(config)["hosts"][unreachable.key]["next_retry_at"])
        assert timedelta(0) < retry_at - NOW <= timedelta(seconds=recovery._MAX_RETRY_SECONDS)


def test_slow_remote_probe_does_not_hold_controller_readiness(config: MediaforceConfig) -> None:
    from mediaforce.web.runtime import controller_storage_recovery as controller

    started = threading.Event()
    release = threading.Event()

    def slow_probe(_config: MediaforceConfig, _host: dict[str, object]) -> HostStatus:
        started.set()
        assert release.wait(5)
        return status(available=True)

    ready = ControllerMountProbe(True, True, Path("/Volumes/media"), "//user@nas/media", "smbfs", True)
    worker = threading.Thread(target=recovery.process_host_recovery_once, args=(config,))
    with patch.object(recovery, "probe_host_status", side_effect=slow_probe), patch.object(
        controller.sys, "platform", "darwin",
    ), patch.object(controller, "probe_controller_mount", return_value=ready):
        worker.start()
        try:
            assert started.wait(5)
            controller.process_controller_storage_recovery_once(config)
            snapshot = controller.controller_storage_recovery_snapshot(config)
            assert snapshot["mounts"] and all(mount["status"] == "ready" for mount in snapshot["mounts"])
            assert worker.is_alive()
        finally:
            release.set()
            worker.join(5)
        assert not worker.is_alive()


def test_connection_locks_contend_only_for_the_same_computer(config: MediaforceConfig) -> None:
    from mediaforce.hosts.controller_mount import controller_mount_lock

    with controller_mount_lock(config.paths.runtime_settings_path, host_key="user@worker") as remote_acquired:
        assert remote_acquired
        with controller_mount_lock(config.paths.runtime_settings_path) as controller_acquired:
            assert controller_acquired
        with controller_mount_lock(config.paths.runtime_settings_path, host_key="user@other") as other_acquired:
            assert other_acquired
        with controller_mount_lock(config.paths.runtime_settings_path, host_key="user@worker") as duplicate_acquired:
            assert not duplicate_acquired
