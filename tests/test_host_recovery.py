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


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(target, name, *args, **kwargs)


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
    with patch_attr(recovery, "datetime", wraps=datetime) as clock, patch_attr(
        recovery, "probe_host_status", return_value=status(),
    ) as probe, patch_attr(recovery, "recover_remote_host_mounts_no_ui", return_value=NoUiMountResult(ok=False)) as mount:
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



def test_sign_in_failure_asks_once_and_stops_reconnecting_until_storage_returns(config: MediaforceConfig) -> None:
    needs_password = NoUiMountResult(ok=False, action="Worker needs its share password saved.", retry=False)
    with patch_attr(recovery, "datetime", wraps=datetime) as clock, patch_attr(
        recovery, "probe_host_status", return_value=status(),
    ) as probe, patch_attr(
        recovery, "recover_remote_host_mounts_no_ui", return_value=needs_password,
    ) as mount, patch_attr(alerts, "datetime", wraps=datetime) as alert_clock:
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
        # Someone connected the share in Finder or used Set up computer: the next check clears the request.
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
    with patch_attr(recovery, "datetime", wraps=datetime) as clock, patch_attr(
        recovery, "probe_host_status", return_value=status(),
    ) as probe, patch_attr(
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
    with patch_attr(web_app, "load_config", return_value=config):
        app = web_app.create_app(config.paths.config_path)
    endpoint = next(
        route.endpoint for route in app.router.routes
        if isinstance(route, APIRoute) and route.path == "/api/hosts"
    )
    # Reading the cards must not check anything itself.
    with patch("subprocess.run", side_effect=AssertionError("read launched a check")), patch_attr(
        host_status, "_schedule_host_status_refresh",
    ) as schedule:
        response = endpoint(0)
    schedule.assert_not_called()
    return {host["host"]: host for host in json.loads(response.body)["hosts"]}


def test_idle_background_checks_keep_remote_cards_current(
        config: MediaforceConfig, empty_host_status_cache: None,
) -> None:
    with patch_attr(host_status, "collect_host_statuses", return_value=[status(available=True)]):
        host_status.refresh_host_status_cache(config)
    assert _host_cards(config)["user@worker"]["available"]
    with patch_attr(recovery, "datetime", wraps=datetime) as clock, patch_attr(
        recovery, "probe_host_status", return_value=status(),
    ) as probe, patch_attr(recovery, "recover_remote_host_mounts_no_ui", return_value=NoUiMountResult(ok=False)):
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
    with patch_attr(recovery, "probe_host_status", return_value=status()), patch_attr(
        recovery, "recover_remote_host_mounts_no_ui", return_value=needs_password,
    ):
        recovery.process_host_recovery_once(config)
    card = _host_cards(config)["user@worker"]
    assert card["message"] == needs_password.action


def test_retrying_host_card_carries_background_recovery_state(
        config: MediaforceConfig, empty_host_status_cache: None,
) -> None:
    with patch_attr(recovery, "datetime", wraps=datetime) as clock, patch_attr(
        recovery, "probe_host_status", return_value=status(),
    ), patch_attr(recovery, "recover_remote_host_mounts_no_ui", return_value=NoUiMountResult(ok=False)):
        clock.now.return_value = NOW
        recovery.process_host_recovery_once(config)
    record = recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]
    card = _host_cards(config)["user@worker"]
    expected = {
        "status": "retrying",
        "reason": status().message,
        "first_failure_at": NOW.isoformat(),
        "last_check_at": NOW.isoformat(),
        "next_retry_at": record["next_retry_at"],
        "failure_count": 1,
    }
    assert {key: card["recovery"][key] for key in expected} == expected
    assert datetime.fromisoformat(card["recovery"]["next_retry_at"]) > NOW
    assert "when work starts" not in card["message"]
    assert "when work starts" not in card["active_reason"]


def test_action_required_host_card_carries_recovery_state(
        config: MediaforceConfig, empty_host_status_cache: None,
) -> None:
    needs_password = NoUiMountResult(ok=False, action="Worker needs its share password saved.", retry=False)
    with patch_attr(recovery, "datetime", wraps=datetime) as clock, patch_attr(
        recovery, "probe_host_status", return_value=status(),
    ), patch_attr(recovery, "recover_remote_host_mounts_no_ui", return_value=needs_password):
        clock.now.return_value = NOW
        recovery.process_host_recovery_once(config)
    card = _host_cards(config)["user@worker"]
    assert card["recovery"]["status"] == "action_required"
    assert card["recovery"]["reason"] == needs_password.action
    assert card["recovery"]["first_failure_at"] == NOW.isoformat()
    assert card["recovery"]["last_check_at"] == NOW.isoformat()
    assert card["recovery"]["failure_count"] == 1


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
    with patch_attr(host_status, "collect_host_statuses", return_value=[
        controller(available=False), status(available=True),
    ]):
        host_status.refresh_host_status_cache(config)
    assert not _host_cards(config)["user@localhost"]["available"]
    with patch_attr(web_app, "load_config", return_value=config), patch_attr(
        web_app, "process_controller_storage_recovery_once",
    ), patch_attr(
        host_status, "controller_storage_recovery_snapshot", return_value=storage("ready"),
    ) as snapshot, patch_attr(
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
    with patch_attr(recovery, "datetime", wraps=datetime) as clock, patch_attr(
        recovery, "probe_host_status", return_value=unreachable,
    ), patch_attr(recovery, "recover_remote_host_mounts_no_ui") as mount, patch_attr(alerts, "datetime", wraps=datetime) as alert_clock:
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
    with patch_attr(alerts, "controller_storage_recovery_snapshot", return_value=storage), patch_attr(alerts.LOGGER, "warning") as warning:
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
    with patch_attr(recovery, "probe_host_status", return_value=status()), patch_attr(recovery, "recover_remote_host_mounts_no_ui") as mount:
        recovery.process_host_recovery_once(config)
        mount.assert_not_called()
    stop = threading.Event()
    stop.set()
    with patch_attr(recovery, "probe_host_status") as probe:
        recovery.process_host_recovery_once(config, stop)
        probe.assert_not_called()


def test_corrupt_state_warning_clears_after_manual_recovery_is_verified(config: MediaforceConfig) -> None:
    save_runtime_settings(recovery.host_recovery_path(config), {"schema_version": 999})
    with patch_attr(recovery, "probe_host_status", return_value=status(available=True)), patch_attr(
        recovery, "recover_remote_host_mounts_no_ui",
    ) as mount:
        recovery.process_host_recovery_once(config)
        assert not recovery.host_recovery_snapshot(config).get("state_error")
        mount.assert_not_called()


def test_prolonged_outage_backoff_is_capped(config: MediaforceConfig) -> None:
    unreachable = status()
    unreachable.platform = "unknown"
    unreachable.missing_mounts = []
    with patch_attr(recovery, "datetime", wraps=datetime) as clock, patch_attr(
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
    with patch_attr(recovery, "probe_host_status", side_effect=slow_probe), patch_attr(
        controller.sys, "platform", "darwin",
    ), patch_attr(controller, "probe_controller_mount", return_value=ready):
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


def _other(key: str, *, available: bool) -> HostStatus:
    row = status(available=available)
    row.key, row.label = key, key.split("@")[-1].title()
    return row


def _host_alert(config: MediaforceConfig) -> dict[str, str]:
    [alert] = [item for item in alerts.recovery_alerts(config) if item["id"].startswith("host:")]
    return alert


def test_host_alert_says_how_many_other_computers_are_still_working(
        config: MediaforceConfig, empty_host_status_cache: None,
) -> None:
    config.raw["remote_hosts"] = [
        *config.raw["remote_hosts"],
        {"host": "user@den", "label": "Den", "media_access": "mounted"},
        {"host": "user@attic", "label": "Attic", "media_access": "mounted"},
    ]
    needs_password = NoUiMountResult(ok=False, action="Worker needs its share password saved.", retry=False)

    def probe(_config: MediaforceConfig, host: dict[str, Any]) -> HostStatus:
        return status() if host["host"] == "user@worker" else _other(host["host"], available=True)

    with patch_attr(recovery, "probe_host_status", side_effect=probe), patch_attr(
        recovery, "recover_remote_host_mounts_no_ui", return_value=needs_password,
    ):
        recovery.process_host_recovery_once(config)
    # The same pass checked the other computers.
    assert _host_alert(config)["detail"].endswith("2 other computers are still working.")

    def cards(*, den: bool, attic: bool) -> None:
        with patch_attr(host_status, "collect_host_statuses", return_value=[
            status(), _other("user@den", available=den), _other("user@attic", available=attic),
        ]):
            host_status.refresh_host_status_cache(config)

    cards(den=True, attic=True)
    assert _host_alert(config)["detail"].endswith("2 other computers are still working.")
    cards(den=True, attic=False)
    assert _host_alert(config)["detail"].endswith("1 other computer is still working.")
    cards(den=False, attic=False)
    assert _host_alert(config)["detail"].endswith("No other computer is working, so work waits for this one.")
    assert needs_password.action in _host_alert(config)["detail"]


def test_host_alert_does_not_say_none_are_working_before_they_are_checked(
        config: MediaforceConfig, empty_host_status_cache: None,
) -> None:
    config.raw["remote_hosts"] = [
        *config.raw["remote_hosts"], {"host": "user@den", "label": "Den", "media_access": "mounted"},
    ]
    save_runtime_settings(recovery.host_recovery_path(config), {"schema_version": 1, "hosts": {"user@worker": {
        "label": "Worker", "status": "action_required", "reason": "Log in on Worker.",
        "first_failure_at": NOW.isoformat(), "failure_count": 1, "mount_uncertain": False,
    }}})
    assert _host_alert(config)["detail"] == "Log in on Worker. Mediaforce will keep checking."


def test_only_computer_alert_says_work_waits_for_it(config: MediaforceConfig, empty_host_status_cache: None) -> None:
    with patch_attr(recovery, "probe_host_status", return_value=status()), patch_attr(
        recovery, "recover_remote_host_mounts_no_ui", return_value=NoUiMountResult(ok=False, action="Storage needs checking.", retry=False),
    ):
        recovery.process_host_recovery_once(config)
    assert _host_alert(config)["detail"].endswith("No other computer is set up, so work waits for this one.")


def test_retry_connection_runs_one_no_dialog_pass_now(config: MediaforceConfig, empty_host_status_cache: None) -> None:
    with patch_attr(recovery, "datetime", wraps=datetime) as clock, patch_attr(
        recovery, "probe_host_status", return_value=status(),
    ) as probe, patch_attr(
        recovery, "recover_remote_host_mounts_no_ui", return_value=NoUiMountResult(ok=False),
    ) as mount, patch("mediaforce.remote.recover_remote_host_mounts", side_effect=AssertionError("Finder dialog")):
        clock.now.return_value = NOW
        recovery.process_host_recovery_once(config)
        assert _host_alert(config)["host_key"] == "user@worker"
        # The background pass is waiting out its retry delay; Retry connection does not wait.
        recovery.process_host_recovery_once(config)
        assert mount.call_count == 1
        mount.return_value = NoUiMountResult(ok=True)
        probe.side_effect = [status(), status(available=True)]
        ok, message = recovery.retry_host_connection(config, "user@worker")
    assert (ok, message) == (True, "Worker is connected.")
    assert mount.call_count == 2
    assert recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]["status"] == "ready"
    assert _host_cards(config)["user@worker"]["available"]


def test_retry_connection_tries_again_after_waiting_for_someone_at_the_computer(config: MediaforceConfig) -> None:
    needs_password = NoUiMountResult(ok=False, action="Worker needs its share password saved", retry=False)
    with patch_attr(recovery, "probe_host_status", return_value=status()), patch_attr(
        recovery, "recover_remote_host_mounts_no_ui", return_value=needs_password,
    ) as mount:
        recovery.process_host_recovery_once(config)
        ok, message = recovery.retry_host_connection(config, "user@worker")
    assert mount.call_count == 2, "the person says it is fixed, so connecting is tried again"
    assert not ok
    assert message == f"Worker is still unavailable. {needs_password.action}."
    assert recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]["retry_paused"]


def test_retry_connection_rechecks_an_unconfirmed_request_with_the_safe_helper(config: MediaforceConfig) -> None:
    with patch_attr(recovery, "probe_host_status", return_value=status()), patch_attr(
        recovery, "recover_remote_host_mounts_no_ui", return_value=NoUiMountResult(ok=False, uncertain=True),
    ) as mount:
        recovery.process_host_recovery_once(config)
        ok, _message = recovery.retry_host_connection(config, "user@worker")
    assert not ok
    assert mount.call_count == 2


def test_retry_connection_refuses_while_an_attempt_is_running(config: MediaforceConfig) -> None:
    from mediaforce.hosts.controller_mount import controller_mount_lock

    with patch_attr(recovery, "probe_host_status") as probe, controller_mount_lock(
        config.paths.runtime_settings_path, host_key="user@worker",
    ) as held:
        assert held
        ok, message = recovery.retry_host_connection(config, "user@worker")
    assert not ok
    assert message == "Worker is already being checked or connected. Try again when that finishes."
    probe.assert_not_called()
    assert recovery.retry_host_connection(config, "user@gone") == (
        False, "That computer is no longer set up for Mediaforce.",
    )


def test_background_pass_keeps_a_retry_that_finished_while_it_ran(config: MediaforceConfig) -> None:
    config.raw["remote_hosts"] = [
        {"host": "user@den", "label": "Den", "media_access": "mounted"},
        *config.raw["remote_hosts"],
    ]
    retried: list[tuple[bool, str]] = []

    def probe(_config: MediaforceConfig, host: dict[str, Any]) -> HostStatus:
        if host["host"] == "user@den" and not retried:
            # Someone presses Retry connection on Worker while the background pass checks Den.
            retried.append(recovery.retry_host_connection(config, "user@worker"))
        return status() if host["host"] == "user@worker" else _other(host["host"], available=True)

    with patch_attr(recovery, "probe_host_status", side_effect=probe), patch_attr(
        recovery, "recover_remote_host_mounts_no_ui", return_value=NoUiMountResult(ok=False),
    ) as mount:
        recovery.process_host_recovery_once(config)
    assert retried and not retried[0][0]
    hosts = recovery.host_recovery_snapshot(config)["hosts"]
    assert hosts["user@den"]["status"] == "ready"
    assert hosts["user@worker"]["failure_count"] == 1, "the retry's record survives and its delay is honored"
    assert mount.call_count == 1


def test_retry_connection_route_posts_and_reads_stay_check_free(
        config: MediaforceConfig, empty_host_status_cache: None,
) -> None:
    import asyncio

    with patch_attr(web_app, "load_config", return_value=config):
        app = web_app.create_app(config.paths.config_path)
    routes = {
        (route.path, method): route.endpoint for route in app.router.routes
        if isinstance(route, APIRoute) for method in route.methods
    }
    assert ("/api/hosts/retry-connection", "GET") not in routes

    class Request:
        @staticmethod
        async def json() -> dict[str, str]:
            return {"host_key": "user@worker"}

    with patch_attr(recovery, "probe_host_status", side_effect=[status(), status(available=True)]), patch_attr(
        recovery, "recover_remote_host_mounts_no_ui", return_value=NoUiMountResult(ok=True),
    ), patch_attr(web_app, "release_host_cooldowns", return_value=0) as release:
        response = asyncio.run(routes[("/api/hosts/retry-connection", "POST")](Request()))
    assert json.loads(response.body) == {"ok": True, "message": "Worker is connected.", "kind": "success"}
    release.assert_called_once()
    with patch("subprocess.run", side_effect=AssertionError("read launched a check")), patch_attr(
        recovery, "probe_host_status", side_effect=AssertionError("read launched a check"),
    ):
        routes[("/api/recovery-alerts", "GET")]()


def test_host_alert_reads_as_sentences(config: MediaforceConfig, empty_host_status_cache: None) -> None:
    unreachable = status()
    unreachable.platform = "unknown"
    unreachable.missing_mounts = []
    unreachable.message = "Fix hostname or DNS first"
    with patch_attr(recovery, "probe_host_status", return_value=unreachable), patch_attr(
        alerts, "datetime", wraps=datetime,
    ) as alert_clock:
        recovery.process_host_recovery_once(config)
        alert_clock.now.return_value = datetime.now(timezone.utc) + timedelta(hours=1)
        assert _host_alert(config)["detail"].startswith("Fix hostname or DNS first. Mediaforce is retrying")


def test_two_computer_outage_keeps_work_on_the_other_and_readmits_after_a_fresh_check(
        config: MediaforceConfig, empty_host_status_cache: None,
) -> None:
    """Worker loses its storage while Den keeps encoding; nothing waits on the Director (#943)."""
    from mediaforce.core.db import open_db
    from mediaforce.core.db_tables import encode_jobs
    from mediaforce.web.runtime import controller_storage_recovery as controller

    config.raw["remote_hosts"] = [
        {**config.raw["remote_hosts"][0], "capabilities": ["encode_queue"]},
        {"host": "user@den", "label": "Den", "media_access": "mounted", "capabilities": ["encode_queue"],
         "max_parallel_encodes": 2},
    ]
    worker_storage = {"connected": True}
    checked: list[str] = []

    def worker(*, available: bool) -> HostStatus:
        row = status(available=available)
        row.priority = 2  # Worker is preferred whenever the queue may use it.
        return row

    def probe(_config: MediaforceConfig, host: dict[str, Any]) -> HostStatus:
        checked.append(host["host"])
        if host["host"] == "user@worker":
            return worker(available=worker_storage["connected"])
        return _other("user@den", available=True)

    # Den is already encoding one file and has room for another.
    with open_db(config.paths.db_path) as db:
        db.execute(encode_jobs.insert().values(
            job_id="den-running", prefix="tv/Show", status="running", manifest_path="/runs/den.json",
            host_json=json.dumps({"key": "user@den", "host": "user@den", "label": "Den"}),
            created_at=NOW.isoformat(), updated_at=NOW.isoformat(),
        ))
    ready = ControllerMountProbe(True, True, Path("/Volumes/media"), "//user@nas/media", "smbfs", True)
    with patch_attr(controller.sys, "platform", "darwin"), patch_attr(
        controller, "probe_controller_mount", return_value=ready,
    ):
        controller.process_controller_storage_recovery_once(config)
    with patch_attr(host_status, "collect_host_statuses", side_effect=lambda cfg: [
        probe(cfg, host) for host in cfg.remote_hosts
    ]):
        host_status.refresh_host_status_cache(config)

    def chosen() -> str:
        with open_db(config.paths.db_path) as connection:
            host, reason = web_app._select_encode_host(connection, config, {})
        assert host is not None, reason
        return str(host["key"])

    attempts: list[tuple[datetime, datetime]] = []

    def no_ui_mount(_config: MediaforceConfig, host: dict[str, Any], _status: HostStatus) -> NoUiMountResult:
        assert host["host"] == "user@worker", "only the computer that lost storage reconnects"
        record = recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]
        assert record["mount_uncertain"], "the attempt is saved before the connection is tried"
        attempts.append((clock.now.return_value, datetime.fromisoformat(record["next_retry_at"])))
        return NoUiMountResult(ok=False)

    def run_pass(at: datetime) -> None:
        clock.now.return_value = alert_clock.now.return_value = at
        recovery.process_host_recovery_once(config)
        alerts.record_recovery_alerts(config)

    with patch_attr(recovery, "datetime", wraps=datetime) as clock, patch_attr(
        alerts, "datetime", wraps=datetime,
    ) as alert_clock, patch_attr(recovery, "probe_host_status", side_effect=probe), patch_attr(
        recovery, "recover_remote_host_mounts_no_ui", side_effect=no_ui_mount,
    ), patch("mediaforce.remote.recover_remote_host_mounts", side_effect=AssertionError("Finder dialog")), patch(
        "mediaforce.hosts.mount_runtime.mount_smb_shares", side_effect=AssertionError("Finder dialog"),
    ), patch("subprocess.run", side_effect=AssertionError("real command")), patch_attr(
        host_status, "_schedule_host_status_refresh", side_effect=AssertionError("unstubbed full check"),
    ), patch_attr(alerts.LOGGER, "warning") as warning, patch_attr(alerts.LOGGER, "info") as recovered:
        run_pass(NOW)
        assert chosen() == "user@worker"
        assert not alerts.recovery_alerts(config)

        # Worker's storage drops just before its next routine check, 30 seconds after the last one.
        # The worker loop polls every 30 seconds; poll more often to prove that the saved backoff,
        # not the poll rate, paces reconnects.
        worker_storage["connected"] = False
        outage = NOW + timedelta(seconds=30)
        seen_alerts: list[list[dict[str, str]]] = []
        for step in range(0, 12 * 60, 10):
            run_pass(outage + timedelta(seconds=step))
            seen_alerts.append(alerts.recovery_alerts(config))
            assert chosen() == "user@den", "Den keeps taking work while Worker is out"
            cards = _host_cards(config)
            assert cards["user@den"]["available"] and cards["user@den"]["active_encode_count"] == 1
            assert not cards["user@worker"]["available"]
            assert cards["user@worker"]["recovery"]["status"] == "retrying"

        assert len(attempts) >= 4, "background reconnects keep trying on their own"
        for (_previous, due), (attempted, _next_due) in zip(attempts, attempts[1:]):
            assert attempted >= due, "a reconnect never runs before its backoff ends"
        gaps = [later[0] - earlier[0] for earlier, later in zip(attempts, attempts[1:])]
        assert gaps == sorted(gaps) and gaps[-1] <= timedelta(seconds=recovery._MAX_RETRY_SECONDS + 10)

        # Exactly one warning: for Worker, not before the outage is prolonged, the same one every pass.
        first_shown = next(index for index, current in enumerate(seen_alerts) if current)
        assert timedelta(minutes=5) <= timedelta(seconds=10 * first_shown) < timedelta(minutes=5, seconds=10)
        assert all(current == seen_alerts[first_shown] for current in seen_alerts[first_shown:])
        [alert] = seen_alerts[-1]
        assert alert["host_key"] == "user@worker" and alert["title"] == "Worker is unavailable"
        assert alert["detail"].endswith("1 other computer is still working.")
        assert warning.call_count == 1
        recovered.assert_not_called()

        # Someone reconnects Worker's storage. Until a check confirms it, nothing changes.
        worker_storage["connected"] = True
        last_attempt, due = attempts[-1]
        checked.clear()
        run_pass(due - timedelta(seconds=1))
        assert "user@worker" not in checked
        assert chosen() == "user@den", "Worker is not taken back on saved state alone"
        assert len(alerts.recovery_alerts(config)) == 1

        attempt_count = len(attempts)
        run_pass(due)
        assert "user@worker" in checked
        assert len(attempts) == attempt_count, "the check found storage connected; nothing to reconnect"
        assert recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]["status"] == "ready"
        assert not alerts.recovery_alerts(config)
        assert chosen() == "user@worker", "the fresh check admits Worker again"
        assert _host_cards(config)["user@worker"]["available"]
        assert warning.call_count == 1
        recovered.assert_called_once()


# How the client reports each way a check can fail. The address is in the output, as it is in real
# output, so the tests can show it stays out of the words people read.
_ADDRESS = "chris@m2-mbp.local"
_CHECK_FAILURES = {
    "asleep_timeout": f"ssh: connect to host {_ADDRESS} port 22: Operation timed out",
    "asleep_no_route": f"ssh: connect to host {_ADDRESS} port 22: No route to host",
    "asleep_down": f"ssh: connect to host {_ADDRESS} port 22: Host is down",
    "asleep_network": f"ssh: connect to host {_ADDRESS} port 22: Network is unreachable",
    "asleep_dropped": f"ssh: connect to host {_ADDRESS} port 22: Connection timed out",
    "identity_changed": "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed.",
    "remote_login_off": f"ssh: connect to host {_ADDRESS} port 22: Connection refused",
    "not_found": f"ssh: Could not resolve hostname {_ADDRESS}: nodename nor servname provided, or not known",
    "password_needed": f"{_ADDRESS}: Permission denied (publickey,password).",
}


def _assert_plain_words(text: str, label: str) -> None:
    assert label in text
    lowered = text.lower()
    for jargon in ("ssh", "host", "dns", "known_hosts", "fingerprint", _ADDRESS, "/"):
        assert jargon not in lowered, (jargon, text)


def _checked(config: MediaforceConfig, stderr: str) -> HostStatus:
    import subprocess

    from mediaforce import remote

    host = {"host": _ADDRESS, "label": "M2 MBP", "capabilities": ["encode_queue"]}
    failed = subprocess.CompletedProcess(args=["ssh"], returncode=255, stdout="", stderr=stderr)
    with patch("mediaforce.remote._run_remote_ssh", return_value=failed), patch("mediaforce.remote.time.sleep"):
        return remote._remote_host_status(
config, host,
        )


@pytest.mark.parametrize("case", sorted(_CHECK_FAILURES))
def test_failed_computer_check_names_the_computer_in_plain_words(config: MediaforceConfig, case: str) -> None:
    checked = _checked(config, _CHECK_FAILURES[case])
    assert not checked.available
    for text in (checked.message, *checked.issues):
        _assert_plain_words(text, "M2 MBP")


def test_a_computer_that_does_not_answer_may_be_off_or_asleep(config: MediaforceConfig) -> None:
    asleep = [_checked(config, _CHECK_FAILURES[case]) for case in ("asleep_timeout", "asleep_no_route", "asleep_dropped", "asleep_down", "asleep_network")]
    remote_login_off = _checked(config, _CHECK_FAILURES["remote_login_off"])
    for checked in asleep:
        assert "asleep" in " ".join(checked.issues)
        assert checked.message == asleep[0].message
    # A computer that turns Mediaforce away is awake, so it is not told to wake up.
    assert remote_login_off.message != asleep[0].message
    assert "asleep" not in " ".join(remote_login_off.issues)
    # The raw output stays available behind Details.
    assert _ADDRESS in (asleep[0].detail or "")


def test_a_check_that_never_finishes_does_not_prove_a_computer_is_away(config: MediaforceConfig) -> None:
    import subprocess

    from mediaforce import remote

    host = {"host": _ADDRESS, "label": "M2 MBP", "capabilities": ["encode_queue"]}
    with patch("mediaforce.remote._run_remote_ssh", side_effect=subprocess.TimeoutExpired(["ssh"], 8)), patch(
            "mediaforce.remote.time.sleep",
    ):
        checked = remote._remote_host_status(
config, host,
        )
    assert checked.check_failure == "check_failed"
    with patch_attr(recovery, "probe_host_status", return_value=checked):
        recovery.process_host_recovery_once(config)
    assert not recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]["network_absent"]


def test_failed_check_keeps_the_same_buttons(config: MediaforceConfig) -> None:
    identity = _checked(config, _CHECK_FAILURES["identity_changed"])
    password = _checked(config, _CHECK_FAILURES["password_needed"])
    assert identity.trust_reset_supported and not identity.setup_supported and identity.detail is None
    assert password.setup_supported and password.setup_requires_password


def test_storage_warning_keeps_the_mount_point_behind_details(config: MediaforceConfig) -> None:
    storage = {"mounts": [{
        "mount_point": "/Volumes/media", "status": "retrying", "reason": "The required SMB share is not mounted.",
        "first_failure_at": NOW.isoformat(),
    }], "state_error": "Controller storage recovery state at /tmp/state.json is unreadable."}
    with patch_attr(alerts, "controller_storage_recovery_snapshot", return_value=storage), patch_attr(
            alerts, "host_recovery_snapshot", return_value={"hosts": {}},
    ):
        warnings = alerts.recovery_alerts(config)
    assert len(warnings) == 2
    for warning in warnings:
        for text in (warning["title"], warning["detail"]):
            assert "/" not in text and "smb" not in text.lower() and "controller" not in text.lower()
        assert "/" in warning["technical"]


def test_uncertain_mount_disappears_and_reachable_computer_recovers_automatically(config: MediaforceConfig) -> None:
    with patch_attr(recovery, "datetime", wraps=datetime) as clock, patch_attr(
        recovery, "probe_host_status", return_value=status(),
    ) as probe, patch_attr(
        recovery, "recover_remote_host_mounts_no_ui", return_value=NoUiMountResult(ok=False, uncertain=True),
    ) as mount:
        clock.now.return_value = NOW
        recovery.process_host_recovery_once(config)
        first = recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]
        assert first["mount_uncertain"]
        assert first["status"] == "retrying"
        mount.return_value = NoUiMountResult(ok=True)
        probe.side_effect = [status(), status(available=True)]
        clock.now.return_value += timedelta(minutes=5)
        recovery.process_host_recovery_once(config)
        record = recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]
        assert mount.call_count == 2
        assert record["status"] == "ready"
        assert not record["mount_uncertain"]
        assert not [alert for alert in alerts.recovery_alerts(config) if alert["id"].startswith("host:")]


@pytest.mark.parametrize("model,notifies", [("MacBook Pro", False), ("Mac mini", True), (None, True)])
def test_absent_computer_uses_last_seen_model_and_desktop_grace(
        config: MediaforceConfig, model: str | None, notifies: bool,
) -> None:
    ready = status(available=True)
    ready.computer_model = model
    absent = status()
    absent.check_failure = "unreachable"
    absent.probe_available = False
    absent.missing_mounts = []
    with patch_attr(recovery, "datetime", wraps=datetime) as clock, patch_attr(
        alerts, "datetime", wraps=datetime,
    ) as alert_clock, patch_attr(recovery, "probe_host_status", return_value=ready) as probe, patch_attr(
        recovery, "recover_remote_host_mounts_no_ui",
    ) as mount, patch_attr(alerts.LOGGER, "warning") as warning:
        clock.now.return_value = alert_clock.now.return_value = NOW
        recovery.process_host_recovery_once(config)
        probe.return_value = absent
        grace = recovery.DESKTOP_ABSENCE_GRACE
        signatures = ("not_found", "asleep_timeout", "asleep_down", "asleep_dropped", "asleep_network", "asleep_no_route")
        for elapsed, signature in zip((timedelta(), grace / 4, grace / 2, grace, grace + timedelta(minutes=5), grace * 2), signatures):
            absent.check_failure = _checked(config, _CHECK_FAILURES[signature]).check_failure
            clock.now.return_value = alert_clock.now.return_value = NOW + timedelta(minutes=5) + elapsed
            recovery.process_host_recovery_once(config)
            record = recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]
            assert record["status"] == "retrying"
            assert record["computer_model"] == model
            assert record["network_absent"]
            alerts.record_recovery_alerts(config)
            host_alerts = [alert for alert in alerts.recovery_alerts(config) if alert["id"].startswith("host:")]
            assert bool(host_alerts) == (notifies and elapsed >= grace)
        assert warning.call_count == int(notifies)
        mount.assert_not_called()
        probe.return_value = ready
        clock.now.return_value += timedelta(minutes=5)
        recovery.process_host_recovery_once(config)
        assert recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]["status"] == "ready"
        assert not [alert for alert in alerts.recovery_alerts(config) if alert["id"].startswith("host:")]


def test_unknown_result_retries_before_asking_and_keeps_the_warning_during_attempts(config: MediaforceConfig) -> None:
    with patch_attr(recovery, "datetime", wraps=datetime) as clock, patch_attr(
        alerts, "datetime", wraps=datetime,
    ) as alert_clock, patch_attr(recovery, "probe_host_status", return_value=status()), patch_attr(
        recovery, "recover_remote_host_mounts_no_ui", return_value=NoUiMountResult(ok=False, uncertain=True),
    ) as mount, patch_attr(alerts.LOGGER, "warning") as warning:
        clock.now.return_value = alert_clock.now.return_value = NOW
        recovery.process_host_recovery_once(config)
        assert not alerts.recovery_alerts(config)
        clock.now.return_value = alert_clock.now.return_value = NOW + timedelta(minutes=5)
        recovery.process_host_recovery_once(config)
        alerts.record_recovery_alerts(config)
        [first] = alerts.recovery_alerts(config)

        def reconnect(_config: MediaforceConfig, _host: dict[str, Any], _status: HostStatus) -> NoUiMountResult:
            assert recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]["status"] == "action_required"
            assert alerts.recovery_alerts(config) == [first]
            return NoUiMountResult(ok=False, uncertain=True)

        mount.side_effect = reconnect
        clock.now.return_value = alert_clock.now.return_value = NOW + timedelta(minutes=10)
        recovery.process_host_recovery_once(config)
        alerts.record_recovery_alerts(config)
        assert warning.call_count == 1


@pytest.mark.parametrize("signature", [
    "kex_exchange_identification: Connection reset by peer",
    "Connection timed out during banner exchange",
    "Connection closed by remote host",
])
def test_post_connection_failures_do_not_prove_network_absence(config: MediaforceConfig, signature: str) -> None:
    assert _checked(config, signature).check_failure == "check_failed"


def test_a_standing_storage_question_survives_a_failed_check(config: MediaforceConfig) -> None:
    with patch_attr(recovery, "datetime", wraps=datetime) as clock, patch_attr(
        recovery, "probe_host_status", return_value=status(),
    ) as probe, patch_attr(recovery, "recover_remote_host_mounts_no_ui", return_value=NoUiMountResult(
        ok=False, uncertain=True, action="The computer could not read its mounted-storage list.",
    )):
        clock.now.return_value = NOW
        recovery.process_host_recovery_once(config)
        clock.now.return_value += timedelta(minutes=5)
        recovery.process_host_recovery_once(config)
        first = recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]
        failed = status()
        failed.check_failure = "check_failed"
        failed.message = "The computer could not finish its check."
        probe.return_value = failed
        clock.now.return_value += timedelta(minutes=5)
        recovery.process_host_recovery_once(config)
        current = recovery.host_recovery_snapshot(config)["hosts"]["user@worker"]
        assert current["status"] == "action_required"
        assert current["first_failure_at"] == first["first_failure_at"]
        assert current["reason"] == failed.message
        assert not current["network_absent"]


def test_synchronous_raw_status_cache_starts_with_real_results_and_refreshes(config: MediaforceConfig) -> None:
    with patch_attr(host_status, "collect_host_statuses", return_value=[status()]) as probe, patch_attr(
        host_status.time, "monotonic", return_value=100.0,
    ) as clock:
        assert not host_status.current_host_statuses(config)[0].available
        assert not host_status.current_host_statuses(config)[0].available
        probe.assert_called_once()
        clock.return_value += host_status._HOST_STATUS_CACHE_TTL_SECONDS + 1
        probe.return_value = [status(available=True)]
        assert host_status.current_host_statuses(config)[0].available
        assert probe.call_count == 2
