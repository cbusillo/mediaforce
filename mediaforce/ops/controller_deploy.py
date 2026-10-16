"""Staged controller deployment. Invoke only in an authorized deploy window."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from mediaforce.ops.login_item import (
    LoginItemPaths, _atomic_write, disable_login_item, enable_login_item,
)
from mediaforce.ops.runtime_install import (
    register_runtime_roots, require_release_directory, runtime_directory,
)


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
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
    _run(["git", "checkout", "--detach", sha], cwd=root)
    _run(["uv", "sync", "--locked", "--no-dev"], cwd=root)
    if not (root / ".venv/bin/mediaforce-web").is_file() or not (root / "frontend/build/index.html").is_file():
        raise RuntimeError("prepared runtime lacks its entrypoint or frontend build")
    return root


def backup_database(database: Path, destination: Path) -> None:
    if destination.exists():
        raise RuntimeError("backup already exists; use a fresh deploy stage")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(destination) as backup:
            source.backup(backup)
            if backup.execute("pragma quick_check").fetchall() != [("ok",)]:
                raise RuntimeError("database backup failed quick_check")


def deploy(source: Path, target: str, stage: Path, home: Path, base_url: str) -> None:
    runtime = runtime_directory(home)
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _deploy(source, target, stage, home, base_url)


def _deploy(source: Path, target: str, stage: Path, home: Path, base_url: str) -> None:
    import plistlib

    require_paused_idle_dashboard(_api(base_url, "/api/dashboard?preview_limit=0"))
    plist = LoginItemPaths(project_root=source, home=home).plist
    expected_stage_parent = runtime_directory(home).parent / "deployments"
    if stage.absolute() != stage.resolve() or stage.parent != expected_stage_parent:
        raise RuntimeError(f"deploy stage must be a real directory directly under {expected_stage_parent}")
    previous_payload = plist.read_bytes()
    previous_root = Path(plistlib.loads(previous_payload)["WorkingDirectory"])
    if not previous_root.is_absolute() or _run(["git", "status", "--porcelain"], cwd=previous_root):
        raise RuntimeError("existing runtime checkout is dirty or its identity is invalid")
    stage.mkdir(parents=True, exist_ok=False)
    _atomic_write(stage / "launch-item.before.plist", previous_payload)
    _atomic_write(stage / "previous-commit.txt", _run(["git", "rev-parse", "HEAD"], cwd=previous_root).encode())
    candidate = prepare_release(source, target, home)
    paths = LoginItemPaths(project_root=candidate, home=home)
    # Both sides are protected before launchd changes. Old worktrees remain locked and intact.
    register_runtime_roots(home, previous_root, candidate)
    _atomic_write(stage / "candidate.txt", str(candidate).encode())
    require_paused_idle_dashboard(_api(base_url, "/api/dashboard?preview_limit=0"))
    disable_login_item(paths)
    try:
        for _ in range(15):
            probe = subprocess.run(["/usr/sbin/lsof", "-tiTCP:8777", "-sTCP:LISTEN"], capture_output=True, check=False)
            if probe.returncode == 1 and not probe.stdout and not probe.stderr:
                break
            if probe.returncode != 0:
                raise RuntimeError("cannot verify listener state")
            time.sleep(1)
        else:
            raise RuntimeError("port 8777 still listening")
        backup_database(runtime_directory(home).parent / "library.sqlite3", stage / "before.sqlite")
        enable_login_item(paths)
        for _ in range(45):
            try:
                _api(base_url, "/api/dashboard/scan-job")
                break
            except (URLError, TimeoutError):
                time.sleep(1)
        else:
            raise RuntimeError("candidate did not become healthy")
        _api(base_url, "/api/encode-queue/resume", post=True)
    except Exception:
        # A migration may already have run. Do not start older code on a newer DB.
        disable_login_item(paths)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", required=True, help="exact reviewed main commit")
    parser.add_argument("--stage", type=Path, required=True, help="fresh deploy evidence/backup folder")
    args = parser.parse_args()
    deploy(args.source.resolve(), args.target, args.stage.expanduser(), Path.home(), "http://127.0.0.1:8777")


if __name__ == "__main__":
    main()
