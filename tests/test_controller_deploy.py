import os
import plistlib
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import Column, MetaData, Table, Text

from mediaforce.ops import controller_deploy, login_item, runtime_install
from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db


def _independent_clone(root: Path) -> None:
    (root / ".git").mkdir(parents=True)
    program = root / ".venv/bin/mediaforce-web"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)


def test_prepare_release_installs_outside_source_worktree(tmp_path: Path) -> None:
    source = tmp_path / "development/worktrees/task"
    home = tmp_path / "home"
    sha = "a" * 40
    installed: list[Path] = []

    def run(command: list[str], *, cwd: Path | None = None) -> str:
        if command[1] == "rev-parse":
            return sha
        if command[1] == "clone":
            _independent_clone(Path(command[-1]))
        if command[0] == "uv":
            assert cwd is not None
            installed.append(cwd)
        if command[0] == "bash":
            assert cwd is not None
            controller_deploy.built_mac_app(cwd).mkdir(parents=True)
            installed.append(cwd)
        return ""

    with patch.object(controller_deploy, "_run", side_effect=run):
        candidate = controller_deploy.prepare_release(source, "main", home)
    assert not candidate.is_relative_to(source.parent)
    assert installed and all(path == candidate for path in installed)
    assert controller_deploy.built_mac_app(candidate).is_dir()
    runtime_install.require_release_directory(candidate, home)
    payload = plistlib.loads(login_item.render_login_item_plist(login_item.LoginItemPaths(candidate, home)))
    assert Path(payload["WorkingDirectory"]) == candidate
    assert Path(payload["ProgramArguments"][0]).is_relative_to(candidate)


def test_prepare_refuses_install_root_redirected_into_worktrees(tmp_path: Path) -> None:
    home = tmp_path / "home"
    runtime = runtime_install.runtime_directory(home)
    runtime.parent.mkdir(parents=True)
    disposable = tmp_path / "worktrees"
    disposable.mkdir()
    runtime.symlink_to(disposable, target_is_directory=True)
    with patch.object(controller_deploy, "_run", return_value="b" * 40):
        with pytest.raises(RuntimeError):
            controller_deploy.prepare_release(tmp_path / "source", "main", home)
    assert list(disposable.iterdir()) == []


def test_launch_item_refuses_development_worktree_before_writing(tmp_path: Path) -> None:
    root, home = tmp_path / "worktrees/task", tmp_path / "home"
    _independent_clone(root)
    (root / ".git").rmdir()
    (root / ".git").write_text("gitdir: elsewhere")
    paths = login_item.LoginItemPaths(root, home)
    with patch.object(login_item, "_require_supported_platform"):
        with pytest.raises(login_item.LoginItemError):
            login_item.install_login_item(paths)
    assert not paths.plist.exists()


def test_runtime_registration_retains_both_sides_of_switch(tmp_path: Path) -> None:
    old, new, later = (tmp_path / name for name in ("old-worktree", "new-release", "later-release"))
    runtime_install.register_runtime_roots(tmp_path, old, new)
    runtime_install.register_runtime_roots(tmp_path, new, later)
    roots = runtime_install.protected_runtime_roots(runtime_install.runtime_directory(tmp_path).parent)
    assert all(root in roots for root in (old, new, later))


def test_service_commands_from_worktree_address_installed_release(tmp_path: Path) -> None:
    home = tmp_path / "home"
    candidate = runtime_install.runtime_directory(home) / "releases/candidate"
    _independent_clone(candidate)
    config_path = tmp_path / "persistent/config.toml"
    paths = login_item.LoginItemPaths(candidate, home, config_path=config_path)
    paths.plist.parent.mkdir(parents=True)
    paths.plist.write_bytes(login_item.render_login_item_plist(paths))
    with patch.object(login_item, "enable_login_item", return_value="unchanged") as enable:
        assert login_item.run_login_item_command("restart", project_root=tmp_path / "worktrees/task", home=home) == 0
    assert enable.call_args.args[0].project_root == candidate
    assert enable.call_args.args[0].config_path == config_path


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_database_backup_preserves_rows_and_refuses_overwrite(tmp_path: Path, journal_mode: str) -> None:
    source, backup = tmp_path / "state.sqlite", tmp_path / "before.sqlite"
    connection = sqlite3.connect(source)
    connection.execute(f"pragma journal_mode={journal_mode}")
    connection.execute("create table jobs (id integer, state text)")
    connection.execute("insert into jobs values (1, 'queued')")
    connection.commit()
    connection.close()
    assert not Path(f"{source}-wal").exists() and not Path(f"{source}-shm").exists()
    controller_deploy.backup_database(source, backup)
    with sqlite3.connect(backup) as connection:
        assert connection.execute("select * from jobs").fetchall() == [(1, "queued")]
    with pytest.raises(RuntimeError):
        controller_deploy.backup_database(source, backup)


@pytest.mark.parametrize("paused,encodes,samples", [(False, 0, 0), (True, 1, 0), (True, 0, 1)])
def test_deploy_refuses_unpaused_or_running_work(paused: bool, encodes: int, samples: int) -> None:
    dashboard = {
        "encode_queue": {"state": {"is_paused": paused}, "running_count": encodes},
        "calibration_queue": {"sample": {"running_count": samples}, "full": {"running_count": 0}},
    }
    with pytest.raises(RuntimeError):
        controller_deploy.require_paused_idle_dashboard(dashboard)


@pytest.mark.parametrize(
    "outcome", ["healthy", "unhealthy", "slow", "resume-lost", "backup-failed", "prepare-failed", "app-install-failed"],
)
@pytest.mark.parametrize("stopped", [False, True])
def test_deploy_switch_preserves_previous_runtime_and_backup(tmp_path: Path, outcome: str, stopped: bool) -> None:
    from http.client import RemoteDisconnected
    from urllib.error import URLError

    home, old = tmp_path / "home", tmp_path / "worktrees/legacy"
    candidate = runtime_install.runtime_directory(home) / "releases/candidate"
    database = tmp_path / "configured-state/library.sqlite3"
    stage = database.parent / "deployments/fixture-stage"
    old.mkdir(parents=True)
    (old / "retained-output").write_text("keep")
    _independent_clone(candidate)
    paths = login_item.LoginItemPaths(old, home)
    paths.plist.parent.mkdir(parents=True)
    original = login_item.render_login_item_plist(paths)
    paths.plist.write_bytes(original)
    database.parent.mkdir(parents=True, exist_ok=True)
    with open_db(database) as connection:
        connection.exec_driver_sql("create table jobs (id integer)")
        connection.exec_driver_sql("insert into jobs values (42)")
    config = _stopped_config(tmp_path, database)
    dashboard = {
        "encode_queue": {"state": {"is_paused": True}, "running_count": 0},
        "calibration_queue": {"sample": {"running_count": 0}, "full": {"running_count": 0}},
    }
    resume_requests: list[bool] = []
    service_running = not stopped
    clock = [0.0]

    def api(_base_url: str, route: str, *, post: bool = False) -> dict:
        if stopped and route.startswith("/api/dashboard?"):
            raise AssertionError("stopped deployment must not query the old server")
        if route == "/api/dashboard/scan-job":
            if outcome == "unhealthy":
                raise URLError("candidate unavailable")
            if outcome == "slow" and clock[0] < 60:
                raise RemoteDisconnected("migration still running")
        if post:
            resume_requests.append(True)
            if outcome == "resume-lost":
                raise RemoteDisconnected("response lost")
        return dashboard

    def switch(target_paths: login_item.LoginItemPaths) -> str:
        nonlocal service_running
        service_running = True
        if stopped:
            with sqlite3.connect(database) as database_read:
                assert database_read.execute("select is_paused from encode_queue_state").fetchone() == (1,)
        target_paths.plist.write_bytes(login_item.render_login_item_plist(target_paths))
        return "updated"

    def stop_service(_paths: login_item.LoginItemPaths) -> None:
        nonlocal service_running
        service_running = False

    def restore_service(*_args: object) -> None:
        nonlocal service_running
        service_running = True

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def git(command: list[str], *, cwd: Path | None = None) -> str:
        assert cwd == old
        return {"status": "", "rev-parse": "previous-sha"}[command[1]]

    installed_apps: list[Path] = []
    def install_app(release: Path) -> None:
        installed_apps.append(release)
        if outcome == "app-install-failed":
            raise OSError("Applications folder not writable")

    actual_backup = controller_deploy.backup_database
    def backup(source: Path, destination: Path) -> None:
        if outcome == "backup-failed":
            raise OSError("backup volume unavailable")
        actual_backup(source, destination)

    with patch.object(controller_deploy, "_api", side_effect=api), patch.object(
        controller_deploy, "_run", side_effect=git
    ), patch.object(controller_deploy, "prepare_release", return_value=candidate,
                    side_effect=RuntimeError("preparation failed") if outcome == "prepare-failed" else None), patch.object(
        controller_deploy, "disable_login_item", side_effect=stop_service
    ), patch.object(controller_deploy, "enable_login_item", side_effect=switch), patch.object(
        controller_deploy, "backup_database", side_effect=backup
    ), patch.object(controller_deploy, "install_mac_app", side_effect=install_app), patch.object(
        controller_deploy, "_bootstrap", side_effect=restore_service
    ), patch.object(
        controller_deploy, "_run_command", return_value=subprocess.CompletedProcess([], 0, "", "")
    ), patch.object(controller_deploy, "_launch_domain", return_value="gui/fixture"), patch.object(
        controller_deploy, "_launch_target", return_value="gui/fixture/service"
    ), patch.object(controller_deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"")), patch.object(
        controller_deploy.time, "sleep", side_effect=sleep
    ), patch.object(controller_deploy.time, "monotonic", side_effect=lambda: clock[0]):
        if outcome in {"healthy", "slow"}:
            controller_deploy._deploy(tmp_path / "source", "main", stage, home, "http://fixture", database=database, startup_timeout=90, stopped_config=config if stopped else None)
        else:
            with pytest.raises((RuntimeError, OSError)):
                controller_deploy._deploy(tmp_path / "source", "main", stage, home, "http://fixture", database=database, startup_timeout=90, stopped_config=config if stopped else None)
    assert service_running is (outcome != "unhealthy" and (not stopped or outcome not in {"backup-failed", "prepare-failed"}))
    healthy_candidate = outcome in {"healthy", "slow", "resume-lost", "app-install-failed"}
    assert resume_requests == ([True] if healthy_candidate else [])
    assert installed_apps == ([candidate] if healthy_candidate else [])
    assert (old / "retained-output").read_text() == "keep"
    if outcome == "prepare-failed":
        assert not stage.exists()
        return
    assert (stage / "launch-item.before.plist").read_bytes() == original
    if outcome == "backup-failed":
        assert paths.plist.read_bytes() == original
        return
    with sqlite3.connect(stage / "before.sqlite") as connection:
        assert connection.execute("select * from jobs").fetchall() == [(42,)]
    roots = runtime_install.protected_runtime_roots(database.parent)
    assert old in roots and candidate in roots
    assert runtime_install.retained_deploy_stage(database.parent) == (stage if healthy_candidate else None)


def _built_app(release: Path, marker: str) -> None:
    contents = controller_deploy.built_mac_app(release) / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_text(marker)


def _app_at(path: Path, marker: str) -> None:
    (path / "Contents").mkdir(parents=True)
    (path / "Contents/Info.plist").write_text(marker)
    (path / "Contents/stale-resource").write_text(marker)


@pytest.mark.parametrize("previous", ["none", "installed", "left-aside-by-interrupted-run"])
def test_mac_app_install_replaces_previous_copy(tmp_path: Path, previous: str) -> None:
    release, applications = tmp_path / "release", tmp_path / "Applications"
    _built_app(release, "new")
    installed = applications / controller_deploy.MAC_APP_NAME
    applications.mkdir()
    if previous == "installed":
        _app_at(installed, "old")
    if previous == "left-aside-by-interrupted-run":
        _app_at(applications / f".{controller_deploy.MAC_APP_NAME}.replaced", "old")
    controller_deploy.install_mac_app(release, applications)
    assert (installed / "Contents/Info.plist").read_text() == "new"
    assert not (installed / "Contents/stale-resource").exists()
    assert sorted(path.name for path in applications.iterdir()) == [controller_deploy.MAC_APP_NAME]
    assert (controller_deploy.built_mac_app(release) / "Contents/Info.plist").read_text() == "new"


@pytest.mark.parametrize("previous", ["installed", "left-aside-by-interrupted-run"])
@pytest.mark.parametrize("failing_step", ["copy", "move-in"])
def test_failed_mac_app_install_keeps_previous_app(tmp_path: Path, previous: str, failing_step: str) -> None:
    release, applications = tmp_path / "release", tmp_path / "Applications"
    _built_app(release, "new")
    installed = applications / controller_deploy.MAC_APP_NAME
    applications.mkdir()
    _app_at(installed if previous == "installed" else applications / f".{controller_deploy.MAC_APP_NAME}.replaced", "old")
    incoming = applications / f".{controller_deploy.MAC_APP_NAME}.incoming"

    def rename(path: Path, target: Path) -> Path:
        if path == incoming:
            raise OSError("Applications folder refused the move")
        os.rename(path, target)
        return target

    failure = (
        patch.object(controller_deploy.shutil, "copytree", side_effect=OSError("disk full"))
        if failing_step == "copy" else patch.object(Path, "rename", rename)
    )
    with failure, pytest.raises(OSError):
        controller_deploy.install_mac_app(release, applications)
    assert (installed / "Contents/Info.plist").read_text() == "old"


@pytest.mark.parametrize("shared_metadata", ["commondir", "objects/info/alternates"])
def test_launch_item_refuses_clone_with_shared_git_storage(tmp_path: Path, shared_metadata: str) -> None:
    home = tmp_path / "home"
    candidate = runtime_install.runtime_directory(home) / "releases/candidate"
    _independent_clone(candidate)
    shared = candidate / ".git" / shared_metadata
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text("/disposable/git/storage")
    with pytest.raises(runtime_install.RuntimeProtectionError):
        runtime_install.require_release_directory(candidate, home)


def test_first_service_install_can_use_prepared_independent_release(tmp_path: Path) -> None:
    home = tmp_path / "home"
    candidate = runtime_install.runtime_directory(home) / "releases/candidate"
    _independent_clone(candidate)
    with patch.object(login_item, "_require_supported_platform"):
        assert login_item.run_login_item_command("install", project_root=candidate, home=home) == 0
    plist = login_item.LoginItemPaths(candidate, home).plist
    assert Path(plistlib.loads(plist.read_bytes())["WorkingDirectory"]) == candidate


@pytest.mark.parametrize("tool", ["git", "uv"])
def test_release_commands_cannot_inherit_disposable_project_targets(tmp_path: Path, tool: str) -> None:
    candidate, disposable = tmp_path / "candidate", tmp_path / "worktree"
    environment = {
        "PATH": "/fixture/bin", "VIRTUAL_ENV": str(disposable / ".venv"),
        "UV_PROJECT_ENVIRONMENT": str(disposable / ".venv"), "UV_PROJECT": str(disposable),
        "UV_WORKING_DIR": str(disposable), "GIT_DIR": str(disposable / ".git"),
        "GIT_WORK_TREE": str(disposable),
    }
    with patch.dict(controller_deploy.os.environ, environment, clear=True), patch.object(
        controller_deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")
    ) as run:
        controller_deploy._run([tool, "fixture-command"], cwd=candidate)
    invoked_environment = run.call_args.kwargs["env"]
    assert run.call_args.kwargs["cwd"] == candidate
    assert "VIRTUAL_ENV" not in invoked_environment
    if tool == "uv":
        assert Path(invoked_environment["UV_PROJECT_ENVIRONMENT"]).parent == candidate
        assert "UV_PROJECT" not in invoked_environment and "UV_WORKING_DIR" not in invoked_environment
    else:
        assert not any(key.startswith("GIT_") for key in invoked_environment)


def test_a_deploy_waits_for_the_cleanup_its_own_pause_started(tmp_path: Path) -> None:
    import threading

    database = tmp_path / "support" / "library.sqlite3"
    started, finish = threading.Event(), threading.Event()

    def cleanup() -> None:
        with runtime_install.runtime_activity(database.parent, cleanup=True) as acquired:
            assert acquired
            started.set()
            finish.wait(5)

    def deploy() -> None:
        controller_deploy.deploy(tmp_path, "main", tmp_path / "stage", tmp_path, "http://test", database=database)

    holder = threading.Thread(target=cleanup)
    holder.start()
    started.wait(5)
    with patch.object(controller_deploy, "_deploy") as deployed:
        with patch.object(controller_deploy, "DEPLOY_LOCK_WAIT_SECONDS", 0.2), pytest.raises(RuntimeError, match="stayed active"):
            deploy()
        deployed.assert_not_called()
        threading.Timer(0.3, finish.set).start()
        deploy()
    holder.join()
    deployed.assert_called_once()


def _stopped_config(root: Path, database: Path) -> MediaforceConfig:
    return MediaforceConfig(
        raw={"media": {"source_roots": {}, "staging_root": str(root / "staging"), "archive_root": str(root / "archive")}},
        paths=ConfigPaths(project_root=root, config_path=root / "config.toml", db_path=database,
                          run_manifest_dir=root / "runs", web_state_dir=root / "web", review_dir=root / "review",
                          runtime_settings_path=root / "settings.json", runtime_reservation_dir=root / "reservations"),
    )


@pytest.mark.parametrize("table,column,status", [
    ("encode_jobs", "status", "running"), ("calibration_jobs", "status", "starting"),
    ("scan_runs", "status", "running"), ("library_item_evidence_state", "work_status", "running"),
])
def test_stopped_deploy_refuses_recorded_running_work(tmp_path: Path, table: str, column: str, status: str) -> None:
    database = tmp_path / "library.sqlite3"
    with open_db(database) as connection:
        running = Table(table, MetaData(), Column(column, Text))
        running.drop(connection)
        running.create(connection)
        connection.execute(running.insert().values({column: status}))
    with patch.object(controller_deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"")):
        with pytest.raises(RuntimeError):
            controller_deploy.require_stopped_idle_controller(_stopped_config(tmp_path, database))


@pytest.mark.parametrize("result", [
    subprocess.CompletedProcess([], 0, b"123", b""), subprocess.CompletedProcess([], 1, b"", b"permission denied"),
    subprocess.CompletedProcess([], 2, b"", b""),
])
def test_stopped_deploy_refuses_unknown_or_open_listener(result: subprocess.CompletedProcess[bytes]) -> None:
    with patch.object(controller_deploy.subprocess, "run", return_value=result):
        with pytest.raises(RuntimeError):
            controller_deploy.require_closed_listener()


def test_stopped_deploy_owns_database_until_verified_backup_and_pause(tmp_path: Path) -> None:
    from collections.abc import Callable
    from typing import Any

    from mediaforce.web.runtime_lock import assert_mediaforce_runtime_lock_held, MediaforceRuntimeLockOwnershipError

    database = tmp_path / "library.sqlite3"
    with open_db(database):
        pass
    config = _stopped_config(tmp_path, database)
    snapshot = tmp_path / "before.sqlite"

    def switch(*_args: object, stopped_config: MediaforceConfig,
               release_stopped_runtime: Callable[[], None], **_kwargs: Any) -> None:
        lease = assert_mediaforce_runtime_lock_held()
        controller_deploy.require_stopped_idle_controller(stopped_config)
        controller_deploy.backup_database(database, snapshot)
        controller_deploy.pause_stopped_queue(stopped_config)
        release_stopped_runtime()
        with pytest.raises(MediaforceRuntimeLockOwnershipError):
            lease.assert_active()

    with patch("mediaforce.ops.controller_deploy.load_config", return_value=config), patch.object(
        controller_deploy, "_deploy", side_effect=switch
    ), patch.object(controller_deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"")):
        controller_deploy.deploy(tmp_path / "source", "main", tmp_path / "stage", tmp_path / "home", "http://fixture",
                                 database=database, stopped=True)
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute("select count(*) from encode_queue_state").fetchone() == (0,)
    with sqlite3.connect(database) as connection:
        assert connection.execute("select is_paused from encode_queue_state").fetchone() == (1,)
