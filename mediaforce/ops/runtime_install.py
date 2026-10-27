"""Controller installation paths shared by deploy, launchd and retention."""
from __future__ import annotations

import fcntl
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class RuntimeProtectionError(RuntimeError):
    pass


def runtime_directory(home: Path) -> Path:
    return home.resolve() / "Library" / "Application Support" / "mediaforce" / "runtime"


def require_release_directory(project_root: Path, home: Path) -> None:
    releases = runtime_directory(home) / "releases"
    root = project_root.absolute()
    if root.parent != releases.absolute() or root.resolve() != root:
        raise RuntimeProtectionError(f"controller runtime must be a real directory directly under {releases}")
    if (
        (root / ".git").is_symlink()
        or not (root / ".git").is_dir()
        or (root / ".git/commondir").exists()
        or (root / ".git/objects/info/alternates").exists()
    ):
        raise RuntimeProtectionError("controller runtime must be an independent clone, not a development worktree")


def protected_runtime_roots(support_dir: Path) -> tuple[Path, ...]:
    runtime = support_dir / "runtime"
    registry = runtime / "protected-roots.json"
    try:
        values = json.loads(registry.read_text())
    except FileNotFoundError:
        values = []
    except (OSError, ValueError) as exc:
        raise RuntimeProtectionError("cannot read runtime protection registry; cleanup refused") from exc
    if not isinstance(values, list) or any(not isinstance(value, str) or not Path(value).is_absolute() for value in values):
        raise RuntimeProtectionError("invalid runtime protection registry; cleanup refused")
    return (runtime.resolve(), *(Path(value).resolve() for value in values))


def register_runtime_roots(home: Path, *roots: Path, support_dir: Path | None = None) -> None:
    from mediaforce.ops.login_item import _atomic_write

    runtime = (support_dir or runtime_directory(home).parent) / "runtime"
    existing = (*protected_runtime_roots(runtime.parent), runtime_directory(home))
    runtime.mkdir(parents=True, exist_ok=True)
    values = sorted({str(root.resolve()) for root in (*existing, *roots)})
    _atomic_write(runtime / "protected-roots.json", json.dumps(values, indent=2).encode())


def paths_overlap(left: Path, right: Path) -> bool:
    left, right = left.resolve(), right.resolve()
    return left.is_relative_to(right) or right.is_relative_to(left)


@contextmanager
def runtime_activity(support_dir: Path, *, cleanup: bool = False, wait_seconds: float = 0) -> Iterator[bool]:
    """Holds the deploy lock: shared for cleanup, exclusive for a deploy. Waits up to `wait_seconds` for a holder to
    finish, then yields False."""
    runtime = support_dir / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    with (runtime / "deploy.lock").open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, (fcntl.LOCK_SH if cleanup else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.5)
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def retained_deploy_stage(support_dir: Path) -> Path | None:
    try:
        value = (support_dir / "runtime/rollback-stage.txt").read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeProtectionError("cannot read rollback stage; cleanup refused") from exc
    stage = Path(value)
    if not stage.is_absolute() or stage.parent != (support_dir / "deployments").resolve():
        raise RuntimeProtectionError("invalid rollback stage; cleanup refused")
    return stage


def retain_deploy_stage(support_dir: Path, stage: Path) -> None:
    from mediaforce.ops.login_item import _atomic_write

    runtime = support_dir / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    _atomic_write(runtime / "rollback-stage.txt", str(stage).encode())
