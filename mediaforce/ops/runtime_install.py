"""Controller installation paths shared by deploy, launchd and retention."""
from __future__ import annotations

import fcntl
import json
import plistlib
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


UPDATE_INTENT_NAME = "update-intent.json"
# The live service. A proof build names its own label and port in its Info.plist so it can never address this one.
SERVICE_LABEL = "com.mediaforce.web"
SERVER_PORT = 8777


class RuntimeProtectionError(RuntimeError):
    pass


def read_update_intent(support_dir: Path) -> dict | None:
    """The unfinished app update beside this database, or None. An unreadable record still counts as unfinished."""
    try:
        value = json.loads((support_dir / "runtime" / UPDATE_INTENT_NAME).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"phase": "unreadable"}
    return value if isinstance(value, dict) else {"phase": "unreadable"}


def update_hold_reason(support_dir: Path) -> str | None:
    """Why new work and cleanup wait: an update holds them until it finishes, across quit, replacement and reboot."""
    intent = read_update_intent(support_dir)
    if intent is None:
        return None
    return f"Mediaforce is updating to build {intent.get('to', 'unknown')}. New work waits until it finishes."


def bundled_app_path() -> Path | None:
    executable = Path(sys.executable).resolve()
    if executable.name == "mediaforce-server" and executable.parent.name == "MacOS" and executable.parents[1].name == "Contents":
        return executable.parents[2]
    return None


def bundle_service(app: Path | None) -> tuple[str, int]:
    """The launchd label and local port this app's server uses: the live service's unless the build names others."""
    try:
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes()) if app else {}
    except FileNotFoundError:
        info = {}
    return str(info.get("MediaforceServiceLabel") or SERVICE_LABEL), int(info.get("MediaforceServerPort") or SERVER_PORT)


def bundle_build(app: Path | None) -> str | None:
    return str(plistlib.loads((app / "Contents/Info.plist").read_bytes())["CFBundleVersion"]) if app else None


def support_root(home: Path) -> Path:
    """Application support for this server: the live folder, or a proof build's own folder named by its label."""
    label = bundle_service(bundled_app_path())[0]
    return home.resolve() / "Library" / "Application Support" / ("mediaforce" if label == SERVICE_LABEL else label)


def require_own_support(app: Path, database: Path, home: Path) -> None:
    """A proof build never opens or records anything beside the live service's database."""
    live = home.resolve() / "Library" / "Application Support" / "mediaforce"
    if bundle_service(app)[0] != SERVICE_LABEL and database.resolve().is_relative_to(live):
        raise RuntimeProtectionError(f"a proof build must use its own config and database, not {database}")


def runtime_directory(home: Path) -> Path:
    return support_root(home) / "runtime"


def require_release_directory(project_root: Path, home: Path) -> None:
    releases = runtime_directory(home) / "releases"
    root = project_root.absolute()
    if root.parent != releases.absolute() or root.resolve() != root:
        raise RuntimeProtectionError(f"controller runtime {root} must be a real directory directly under {releases}")
    if (
        (root / ".git").is_symlink()
        or not (root / ".git").is_dir()
        or (root / ".git/commondir").exists()
        or (root / ".git/objects/info/alternates").exists()
    ):
        raise RuntimeProtectionError(f"controller runtime {root} must be an independent clone, not a development worktree")


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
