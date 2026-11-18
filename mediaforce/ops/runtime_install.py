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
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes()) if app else {}
    return str(info.get("MediaforceServiceLabel") or SERVICE_LABEL), int(info.get("MediaforceServerPort") or SERVER_PORT)


def bundle_build(app: Path | None) -> str | None:
    return str(plistlib.loads((app / "Contents/Info.plist").read_bytes())["CFBundleVersion"]) if app else None


def support_root(home: Path) -> Path:
    """Application support for this server: the live folder, or a proof build's own folder named by its label."""
    label = bundle_service(bundled_app_path())[0]
    return home.resolve() / "Library" / "Application Support" / ("mediaforce" if label == SERVICE_LABEL else label)


def require_own_support(app: Path, database: Path, home: Path) -> None:
    """A proof build never opens or records anything beside the live service's database, wherever its config puts it."""
    if bundle_service(app)[0] == SERVICE_LABEL:
        return
    live = (home / "Library" / "Application Support" / "mediaforce").resolve()
    plist = home / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    try:
        live_database = saved_service_database(plist)
    except (OSError, ValueError, KeyError, IndexError, RuntimeProtectionError) as exc:
        raise RuntimeProtectionError(
            f"a proof build starts only once the live service's database is known, and {plist} or the config it names "
            f"cannot be read: {exc}") from exc
    database = database.resolve()
    if database.is_relative_to(live) or (live_database is not None and database.parent == live_database.resolve().parent):
        raise RuntimeProtectionError(f"a proof build must use its own config and database, not {database}")


def require_private_credentials(path: Path) -> None:
    if path.stat().st_mode & 0o077:
        raise RuntimeProtectionError(f"{path} can be read by other users; run chmod 600 '{path}' first")


def saved_service_config(item: dict) -> Path | None:
    arguments = item["ProgramArguments"]
    if "--config" in arguments:
        return Path(arguments[arguments.index("--config") + 1])
    if Path(arguments[0]).name == "mediaforce-server":
        return None
    return Path(item["WorkingDirectory"]) / "config/defaults.toml"


def saved_service_database(plist: Path) -> Path | None:
    from mediaforce.core.config import load_config

    if not plist.exists():
        return None
    config = saved_service_config(plistlib.loads(plist.read_bytes()))
    if config is None:
        raise RuntimeProtectionError(f"the saved service at {plist} names no config")
    return load_config(config).paths.db_path


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


def register_runtime_roots(support_dir: Path, *roots: Path) -> None:
    from mediaforce.ops.login_item import _atomic_write

    runtime = support_dir / "runtime"
    existing = protected_runtime_roots(support_dir)
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
