import plistlib
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mediaforce.ops import controller_deploy, login_item, runtime_install


def _independent_clone(root: Path) -> None:
    (root / ".git").mkdir(parents=True)
    program = root / ".venv/bin/mediaforce-web"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    frontend = root / "frontend/build/index.html"
    frontend.parent.mkdir(parents=True)
    frontend.write_text("frontend")


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
        return ""

    with patch.object(controller_deploy, "_run", side_effect=run):
        candidate = controller_deploy.prepare_release(source, "main", home)
    assert not candidate.is_relative_to(source.parent)
    assert installed and all(path == candidate for path in installed)
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
    paths = login_item.LoginItemPaths(candidate, home)
    paths.plist.parent.mkdir(parents=True)
    paths.plist.write_bytes(login_item.render_login_item_plist(paths))
    with patch.object(login_item, "enable_login_item", return_value="unchanged") as enable:
        assert login_item.run_login_item_command("restart", project_root=tmp_path / "worktrees/task", home=home) == 0
    assert enable.call_args.args[0].project_root == candidate


def test_database_backup_preserves_rows_and_refuses_overwrite(tmp_path: Path) -> None:
    source, backup = tmp_path / "state.sqlite", tmp_path / "before.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("create table jobs (id integer, state text)")
        connection.execute("insert into jobs values (1, 'queued')")
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


@pytest.mark.parametrize("healthy", [True, False])
def test_deploy_switch_preserves_previous_runtime_and_backup(tmp_path: Path, healthy: bool) -> None:
    from urllib.error import URLError

    home, old = tmp_path / "home", tmp_path / "worktrees/legacy"
    candidate = runtime_install.runtime_directory(home) / "releases/candidate"
    stage = runtime_install.runtime_directory(home).parent / "deployments/fixture-stage"
    old.mkdir(parents=True)
    (old / "retained-output").write_text("keep")
    _independent_clone(candidate)
    paths = login_item.LoginItemPaths(old, home)
    paths.plist.parent.mkdir(parents=True)
    original = login_item.render_login_item_plist(paths)
    paths.plist.write_bytes(original)
    database = runtime_install.runtime_directory(home).parent / "library.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("create table jobs (id integer)")
        connection.execute("insert into jobs values (42)")
    dashboard = {
        "encode_queue": {"state": {"is_paused": True}, "running_count": 0},
        "calibration_queue": {"sample": {"running_count": 0}, "full": {"running_count": 0}},
    }
    resumed: list[bool] = []
    service_running = True

    def api(_base_url: str, route: str, *, post: bool = False) -> dict:
        if route == "/api/dashboard/scan-job" and not healthy:
            raise URLError("candidate unavailable")
        if post:
            resumed.append(True)
        return dashboard

    def switch(target_paths: login_item.LoginItemPaths) -> str:
        nonlocal service_running
        service_running = True
        target_paths.plist.write_bytes(login_item.render_login_item_plist(target_paths))
        return "updated"

    def stop_service(_paths: login_item.LoginItemPaths) -> None:
        nonlocal service_running
        service_running = False

    with patch.object(controller_deploy, "_api", side_effect=api), patch.object(
        controller_deploy, "_run", side_effect=["", "previous-sha"]
    ), patch.object(controller_deploy, "prepare_release", return_value=candidate), patch.object(
        controller_deploy, "disable_login_item", side_effect=stop_service
    ), patch.object(controller_deploy, "enable_login_item", side_effect=switch), patch.object(
        controller_deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"")
    ), patch.object(controller_deploy.time, "sleep"):
        if healthy:
            controller_deploy._deploy(tmp_path / "source", "main", stage, home, "http://fixture")
        else:
            with pytest.raises(RuntimeError):
                controller_deploy._deploy(tmp_path / "source", "main", stage, home, "http://fixture")
    assert service_running is healthy
    assert resumed == ([True] if healthy else [])
    assert (old / "retained-output").read_text() == "keep"
    assert (stage / "launch-item.before.plist").read_bytes() == original
    with sqlite3.connect(stage / "before.sqlite") as connection:
        assert connection.execute("select * from jobs").fetchall() == [(42,)]
    roots = runtime_install.protected_runtime_roots(database.parent)
    assert old in roots and candidate in roots


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
