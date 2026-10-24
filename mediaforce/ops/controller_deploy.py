"""Staged controller deployment. Invoke only in an authorized deploy window."""
from __future__ import annotations

import argparse
import json
from http.client import HTTPException
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

from mediaforce.ops.login_item import (
    LoginItemPaths, _atomic_write, _bootstrap, _launch_domain, _launch_target,
    _require_success, _run_command, disable_login_item, enable_login_item,
)
from mediaforce.ops.runtime_install import (
    register_runtime_roots, require_release_directory, retain_deploy_stage, runtime_activity, runtime_directory,
)


DEFAULT_STARTUP_TIMEOUT = 180
MAC_APP_NAME = "Mediaforce.app"
APPLICATIONS = Path("/Applications")


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    if command[0] == "uv":
        if cwd is None:
            raise ValueError("dependency setup requires an explicit release directory")
        environment["UV_PROJECT_ENVIRONMENT"] = str(cwd / ".venv")
        environment.pop("UV_PROJECT", None)
        environment.pop("UV_WORKING_DIR", None)
    if command[0] == "git":
        environment = {key: value for key, value in environment.items() if not key.startswith("GIT_")}
    return subprocess.run(command, cwd=cwd, env=environment, check=True, text=True, capture_output=True).stdout.strip()


def _api(base_url: str, route: str, *, post: bool = False) -> dict:
    with urlopen(Request(base_url + route, method="POST" if post else "GET"), timeout=10) as response:
        return json.load(response)


def require_paused_idle_dashboard(dashboard: dict) -> None:
    queue, calibration = dashboard["encode_queue"], dashboard["calibration_queue"]
    if queue["state"]["is_paused"] is not True or any(
        value != 0 for value in (queue["running_count"], calibration["sample"]["running_count"], calibration["full"]["running_count"])
    ):
        raise RuntimeError("work is running or the encode queue is not paused; not deploying")


def prepare_release(source: Path, target: str, home: Path) -> Path:
    sha = _run(["git", "rev-parse", "--verify", f"{target}^{{commit}}"], cwd=source)
    if len(sha) != 40 or any(character not in "0123456789abcdef" for character in sha):
        raise RuntimeError("target did not resolve to a full commit SHA")
    root = runtime_directory(home) / "releases" / f"{sha}-{uuid4().hex[:8]}"
    if root.exists() or root.is_symlink():
        raise RuntimeError(f"release already exists; preserve it and reconcile before retrying: {root}")
    if root.absolute() != root.resolve():
        raise RuntimeError("runtime install path contains a symlink; refusing deployment")
    root.parent.mkdir(parents=True, exist_ok=True)
    # --no-local prevents object hardlinks/alternates back into disposable source trees.
    _run(["git", "clone", "--no-local", "--no-checkout", str(source), str(root)])
    require_release_directory(root, home)
    _run(["git", "fetch", "--no-tags", str(source), sha], cwd=root)
    _run(["git", "checkout", "--detach", sha], cwd=root)
    _run(["uv", "sync", "--project", str(root), "--locked", "--no-dev"], cwd=root)
    _run(["bash", str(root / "scripts/build-mac-app.sh"), "build", str(built_mac_app(root).parent)], cwd=root)
    if not (root / ".venv/bin/mediaforce-web").is_file() or not (root / "frontend/build/index.html").is_file():
        raise RuntimeError("prepared runtime lacks its entrypoint or frontend build")
    if not built_mac_app(root).is_dir():
        raise RuntimeError("prepared runtime lacks its Mac app")
    return root


def built_mac_app(release: Path) -> Path:
    return release / ".build/release-app" / MAC_APP_NAME


def install_mac_app(release: Path, applications: Path = APPLICATIONS) -> None:
    installed = applications / MAC_APP_NAME
    incoming = applications / f".{MAC_APP_NAME}.incoming"
    replaced = applications / f".{MAC_APP_NAME}.replaced"
    _restore_interrupted_replacement(installed, replaced)
    shutil.rmtree(incoming, ignore_errors=True)
    shutil.copytree(built_mac_app(release), incoming, symlinks=True)
    if _is_present(installed):
        shutil.rmtree(replaced, ignore_errors=True)
        installed.rename(replaced)
    try:
        incoming.rename(installed)
    except OSError:
        _restore_interrupted_replacement(installed, replaced)
        raise
    shutil.rmtree(replaced, ignore_errors=True)


def _restore_interrupted_replacement(installed: Path, replaced: Path) -> None:
    if not _is_present(installed) and _is_present(replaced):
        replaced.rename(installed)


def _is_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def backup_database(database: Path, destination: Path) -> None:
    if destination.exists():
        raise RuntimeError("backup already exists; use a fresh deploy stage")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True) as source:
        with sqlite3.connect(destination) as backup:
            source.backup(backup)
            if backup.execute("pragma quick_check").fetchall() != [("ok",)]:
                raise RuntimeError("database backup failed quick_check")


def deploy(
    source: Path, target: str, stage: Path, home: Path, base_url: str,
    *, database: Path, config_path: Path | None = None, startup_timeout: int = DEFAULT_STARTUP_TIMEOUT,
) -> None:
    with runtime_activity(database.parent) as acquired:
        if not acquired:
            raise RuntimeError("cleanup or another deploy is active; retry after it finishes")
        _deploy(source, target, stage, home, base_url, database=database, config_path=config_path, startup_timeout=startup_timeout)


def _deploy(
    source: Path, target: str, stage: Path, home: Path, base_url: str,
    *, database: Path, config_path: Path | None = None, startup_timeout: int = DEFAULT_STARTUP_TIMEOUT,
) -> None:
    import plistlib

    require_paused_idle_dashboard(_api(base_url, "/api/dashboard?preview_limit=0"))
    plist = LoginItemPaths(project_root=source, home=home).plist
    expected_stage_parent = database.parent / "deployments"
    if stage.absolute() != stage.resolve() or stage.parent != expected_stage_parent:
        raise RuntimeError(f"deploy stage must be a real directory directly under {expected_stage_parent}")
    previous_payload = plist.read_bytes()
    previous_root = Path(plistlib.loads(previous_payload)["WorkingDirectory"])
    if not previous_root.is_absolute() or _run(["git", "status", "--porcelain"], cwd=previous_root):
        raise RuntimeError("existing runtime checkout is dirty or its identity is invalid")
    if stage.exists():
        raise RuntimeError("deploy stage already exists; use a fresh stage")
    candidate = prepare_release(source, target, home)
    stage.mkdir(parents=True, exist_ok=False)
    _atomic_write(stage / "launch-item.before.plist", previous_payload)
    _atomic_write(stage / "previous-commit.txt", _run(["git", "rev-parse", "HEAD"], cwd=previous_root).encode())
    paths = LoginItemPaths(project_root=candidate, home=home, config_path=config_path)
    # Both sides are protected before launchd changes. Old worktrees remain locked and intact.
    register_runtime_roots(home, previous_root, candidate, support_dir=database.parent)
    _atomic_write(stage / "candidate.txt", str(candidate).encode())
    require_paused_idle_dashboard(_api(base_url, "/api/dashboard?preview_limit=0"))
    disable_login_item(paths)
    candidate_started = False
    port_closed = False
    try:
        for _ in range(15):
            probe = subprocess.run(["/usr/sbin/lsof", "-tiTCP:8777", "-sTCP:LISTEN"], capture_output=True, check=False)
            if probe.returncode == 1 and not probe.stdout and not probe.stderr:
                port_closed = True
                break
            if probe.returncode != 0:
                raise RuntimeError("cannot verify listener state")
            time.sleep(1)
        else:
            raise RuntimeError("port 8777 still listening")
        backup_database(database, stage / "before.sqlite")
        candidate_started = True
        enable_login_item(paths)
        deadline = time.monotonic() + startup_timeout
        while True:
            try:
                _api(base_url, "/api/dashboard/scan-job")
                break
            except (OSError, HTTPException, ValueError) as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError("candidate did not become healthy") from exc
                time.sleep(1)
    except Exception:
        if not candidate_started and port_closed:
            if plist.read_bytes() != previous_payload:
                raise RuntimeError("previous plist changed; reconcile before restarting")
            _require_success(_run_command(["/bin/launchctl", "enable", _launch_target()]), "restore the previous login item")
            _bootstrap(_run_command, _launch_domain(), plist)
        else:
            # Migrations or uncertain listener custody require reconciliation.
            disable_login_item(paths)
        raise
    retain_deploy_stage(database.parent, stage)
    try:
        install_mac_app(candidate)
    finally:
        # A lost resume response is uncertain; leave the healthy candidate running.
        _api(base_url, "/api/encode-queue/resume", post=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", required=True, help="exact reviewed main commit")
    parser.add_argument("--stage", type=Path, required=True, help="fresh deploy evidence/backup folder")
    parser.add_argument("--config", type=Path, help="same configuration used by the installed controller")
    parser.add_argument("--startup-timeout", type=int, default=DEFAULT_STARTUP_TIMEOUT, help="seconds allowed for startup and migrations")
    args = parser.parse_args()
    if args.startup_timeout <= 0:
        parser.error("--startup-timeout must be positive")
    from mediaforce.core.config import load_config
    config = load_config(args.config)
    deploy(
        args.source.resolve(), args.target, args.stage.expanduser(), Path.home(), "http://127.0.0.1:8777",
        database=config.paths.db_path, config_path=args.config.expanduser().resolve() if args.config else None,
        startup_timeout=args.startup_timeout,
    )


if __name__ == "__main__":
    main()
