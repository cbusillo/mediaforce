from __future__ import annotations

import os
import hashlib
import plistlib
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mediaforce.ops.runtime_install import RuntimeProtectionError, require_release_directory, runtime_directory


LOGIN_ITEM_LABEL = "com.mediaforce.web"
MACOS_SIGNING_TEAM = "MM5YXC7T6E"
BUNDLED_UPDATE_RECOVERY = "Restore the saved app and configuration. Continue using the current Mediaforce installation until bundled updates are available."
BOOTOUT_ACCEPTED_EXIT_CODES = frozenset({0, 3, 36, 113})
LOGIN_ITEM_LOG_ROTATION_BYTES = 16 * 1024 * 1024
LOGIN_ITEM_PATH = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class LoginItemError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LoginItemPaths:
    project_root: Path
    home: Path
    config_path: Path | None = None
    credentials_path: Path | None = None

    @property
    def bundled(self) -> bool:
        return self.project_root.suffix == ".app"

    @property
    def program(self) -> Path:
        if self.bundled:
            return self.project_root / "Contents" / "MacOS" / "mediaforce-server"
        return self.project_root / ".venv" / "bin" / "mediaforce-web"

    @property
    def working_directory(self) -> Path:
        if self.bundled:
            from mediaforce.core.config import load_config

            if self.config_path is None or not self.config_path.is_absolute():
                raise LoginItemError("the bundled server requires an absolute saved config path")
            return load_config(self.config_path).paths.db_path.parent
        return self.project_root

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LOGIN_ITEM_LABEL}.plist"

    @property
    def log_dir(self) -> Path:
        return self.home / "Library" / "Logs" / "mediaforce"

    @property
    def stdout_log(self) -> Path:
        return self.log_dir / "web.log"

    @property
    def stderr_log(self) -> Path:
        return self.log_dir / "web.err.log"


def render_login_item_plist(paths: LoginItemPaths) -> bytes:
    arguments = [str(paths.program), "--no-reload"]
    if paths.config_path is not None:
        arguments.extend(["--config", str(paths.config_path)])
    if paths.credentials_path is not None:
        arguments.extend(["--credentials", str(paths.credentials_path)])
    payload = {
        "Label": LOGIN_ITEM_LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(paths.working_directory),
        "EnvironmentVariables": {"PATH": _login_item_path(paths.home)},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Standard",
        "StandardOutPath": str(paths.stdout_log),
        "StandardErrorPath": str(paths.stderr_log),
    }
    if paths.bundled:
        payload["MediaforceAppCodeHash"] = _bundle_code_hash(paths.project_root)
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def install_login_item(paths: LoginItemPaths) -> str:
    _require_supported_platform()
    if paths.bundled:
        _require_bundled_install(paths)
    else:
        try:
            require_release_directory(paths.project_root, paths.home)
        except RuntimeProtectionError as exc:
            raise LoginItemError(f"{exc}; use scripts/deploy-controller.sh (legacy recovery: release-flow.md)") from exc
    _require_program(paths.program)
    paths.plist.parent.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    if paths.bundled:
        paths.working_directory.mkdir(parents=True, exist_ok=True)
    else:
        _carry_forward_runtime_env(paths)
    rendered = render_login_item_plist(paths)
    existed = paths.plist.exists()
    if paths.plist.exists() and paths.plist.read_bytes() == rendered:
        return "unchanged"
    _atomic_write(paths.plist, rendered)
    return "updated" if existed else "installed"


def enable_login_item(paths: LoginItemPaths, *, runner: CommandRunner | None = None) -> str:
    command_runner = runner or _run_command
    install_state = install_login_item(paths)
    domain = _launch_domain()
    target = _launch_target()
    _bootout(command_runner, target)
    _wait_until_unloaded(command_runner, target)
    rotate_login_item_logs(paths)
    _require_success(command_runner(["/bin/launchctl", "enable", target]), "enable the login item")
    _bootstrap(command_runner, domain, paths.plist)
    return install_state


def disable_login_item(paths: LoginItemPaths, *, runner: CommandRunner | None = None) -> None:
    _ = paths
    _require_supported_platform()
    command_runner = runner or _run_command
    target = _launch_target()
    _bootout(command_runner, target)
    _wait_until_unloaded(command_runner, target)
    _require_success(command_runner(["/bin/launchctl", "disable", target]), "disable the login item")


def uninstall_login_item(paths: LoginItemPaths, *, runner: CommandRunner | None = None) -> None:
    disable_login_item(paths, runner=runner)
    paths.plist.unlink(missing_ok=True)


def login_item_status(paths: LoginItemPaths, *, runner: CommandRunner | None = None) -> tuple[int, list[str]]:
    _require_supported_platform()
    if not paths.plist.exists():
        return 4, [f"login item: not installed ({paths.plist})"]
    if paths.bundled and not paths.program.is_file():
        return 3, ["login item: the saved app was moved or removed", f"  app      {paths.project_root}",
                   "  next     run service enable from the moved copy of the same app, or restore it to the saved location"]
    if paths.bundled:
        try:
            _require_same_bundle_install(paths, plistlib.loads(paths.plist.read_bytes()))
        except (LoginItemError, OSError, ValueError) as exc:
            return 3, [f"login item: {exc}"]
    try:
        rendered = render_login_item_plist(paths)
    except (OSError, ValueError, KeyError, LoginItemError) as exc:
        return 3, [f"login item: saved configuration is unavailable: {exc}"]
    if paths.plist.read_bytes() != rendered:
        return 3, [
            "login item: installed plist is stale",
            f"  plist    {paths.plist}",
            "  next     run service enable from this app" if paths.bundled else "  next     run `uv run mediaforce service enable`",
        ]
    if not paths.program.is_file() or not os.access(paths.program, os.X_OK):
        return 3, [
            "login item: program is unavailable",
            f"  program  {paths.program}",
            "  next     run `uv sync`, then enable the login item again",
        ]
    command_runner = runner or _run_command
    result = command_runner(["/bin/launchctl", "print", _launch_target()])
    lines = [
        f"login item: {'enabled' if result.returncode == 0 else 'installed but not running'} {LOGIN_ITEM_LABEL}",
        f"  plist    {paths.plist} (current)",
        f"  program  {paths.program} --no-reload",
        f"  logs     {paths.stdout_log}",
    ]
    if result.returncode != 0:
        detail = _command_detail(result)
        if detail:
            lines.append(f"  detail   {detail}")
        return 1, lines
    pid = _launchctl_value(result.stdout, "pid")
    if pid:
        lines[0] += f" pid {pid}"
    return 0, lines


def rotate_login_item_logs(paths: LoginItemPaths, *, maximum_bytes: int = LOGIN_ITEM_LOG_ROTATION_BYTES) -> None:
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    for path in (paths.stdout_log, paths.stderr_log):
        if not path.exists() or path.stat().st_size <= maximum_bytes:
            continue
        rotated = path.with_name(f"{path.name}.1")
        rotated.unlink(missing_ok=True)
        path.replace(rotated)


def run_login_item_command(
        action: str,
        *,
        project_root: Path,
        home: Path | None = None,
        follow: bool = False,
        stderr: bool = False,
        lines: int = 80,
        config_path: Path | None = None,
        credentials_path: Path | None = None,
) -> int:
    home = (home or Path.home()).expanduser()
    # Service commands address the installed controller, regardless of the caller's checkout.
    root = project_root.expanduser().resolve()
    requested_bundle = root.suffix == ".app"
    if not requested_bundle:
        try:
            require_release_directory(root, home)
        except RuntimeProtectionError:
            root = runtime_directory(home) / "current"
    plist = LoginItemPaths(project_root=root, home=home).plist
    if plist.exists():
        try:
            installed = plistlib.loads(plist.read_bytes())
            arguments = installed.get("ProgramArguments", [])
            installed_program = Path(arguments[0])
            installed_root = (
                installed_program.parents[2]
                if installed_program.name == "mediaforce-server"
                else Path(installed["WorkingDirectory"])
            )
            if requested_bundle and action in {"install", "enable", "restart"}:
                _require_same_bundle_install(LoginItemPaths(root, home), installed)
            else:
                root = installed_root
            if not root.is_absolute():
                raise ValueError("working directory is not absolute")
            if "--config" in arguments:
                saved_config = Path(arguments[arguments.index("--config") + 1])
                if action in {"install", "enable", "restart"} and config_path is not None and config_path != saved_config:
                    raise ValueError(f"the requested config differs from the saved service config. {BUNDLED_UPDATE_RECOVERY}")
                config_path = saved_config
                if not config_path.is_absolute():
                    raise ValueError("configuration path is not absolute")
            if "--credentials" in arguments:
                saved_credentials = Path(arguments[arguments.index("--credentials") + 1])
            else:
                saved_credentials = None
            if action in {"install", "enable", "restart"} and credentials_path is not None and credentials_path != saved_credentials:
                raise ValueError(f"the requested credential source differs from the saved service. {BUNDLED_UPDATE_RECOVERY}")
            credentials_path = saved_credentials
        except (OSError, ValueError, KeyError, IndexError, TypeError, plistlib.InvalidFileException, LoginItemError) as exc:
            print(f"login item: cannot identify installed runtime: {exc}")
            return 1
    paths = LoginItemPaths(project_root=root.resolve(), home=home, config_path=config_path, credentials_path=credentials_path)
    try:
        if action == "install":
            state = install_login_item(paths)
            print(f"login item: {state} {paths.plist}")
            return 0
        if action == "enable":
            state = enable_login_item(paths)
            print(f"login item: enabled {LOGIN_ITEM_LABEL} ({state})")
            return 0
        if action == "restart":
            state = enable_login_item(paths)
            print(f"login item: restarted {LOGIN_ITEM_LABEL} ({state})")
            return 0
        if action == "disable":
            disable_login_item(paths)
            print(f"login item: disabled {LOGIN_ITEM_LABEL}")
            return 0
        if action == "uninstall":
            uninstall_login_item(paths)
            print(f"login item: uninstalled {LOGIN_ITEM_LABEL}")
            return 0
        if action == "status":
            status, output = login_item_status(paths)
            print("\n".join(output))
            return status
        if action == "logs":
            return _show_logs(paths.stderr_log if stderr else paths.stdout_log, follow=follow, lines=lines)
    except (LoginItemError, OSError, ValueError, KeyError) as exc:
        print(f"login item: {exc}")
        return 1
    raise ValueError(f"Unsupported login item action: {action}")


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _require_bundled_install(paths: LoginItemPaths) -> None:
    if "AppTranslocation" in paths.project_root.parts:
        raise LoginItemError("move Mediaforce to its permanent location before setting up the background service")
    _verify_signed_bundle(paths.project_root)
    working = paths.working_directory.resolve()
    if working.is_relative_to(paths.project_root.resolve()):
        raise LoginItemError("the configured database must be outside the app bundle")
    if paths.credentials_path is not None:
        if not paths.credentials_path.is_absolute() or paths.credentials_path.resolve().is_relative_to(paths.project_root.resolve()):
            raise LoginItemError("the saved credential source must be an absolute path outside the app bundle")
        try:
            with paths.credentials_path.open("rb"):
                pass
        except OSError as exc:
            raise LoginItemError("the saved credential source is unreadable") from exc
    if paths.plist.exists():
        installed = plistlib.loads(paths.plist.read_bytes())
        _require_same_bundle_install(paths, installed)
        if installed.get("ProgramArguments", [])[1:] != plistlib.loads(render_login_item_plist(paths))["ProgramArguments"][1:]:
            raise LoginItemError(f"another configuration owns the service. {BUNDLED_UPDATE_RECOVERY}")


def _verify_signed_bundle(bundle: Path) -> None:
    _require_success(
        _run_command(["/usr/bin/codesign", "--verify", "--deep", "--strict",
                      f'-R=anchor apple generic and certificate leaf[subject.OU] = "{MACOS_SIGNING_TEAM}"', str(bundle)]),
        "verify the signed app bundle",
    )


def verify_bundled_startup(bundle: Path, *, home: Path | None = None) -> None:
    _verify_signed_bundle(bundle)
    paths = LoginItemPaths(bundle, home or Path.home())
    if paths.plist.exists():
        installed = plistlib.loads(paths.plist.read_bytes())
        _require_same_bundle_install(paths, installed)
        if installed["ProgramArguments"][0] != str(paths.program):
            raise LoginItemError("this app moved; run service enable from the moved copy to repair its saved path")


def _bundle_code_hash(bundle: Path) -> str:
    with (bundle / "Contents/MacOS/Mediaforce").open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _require_same_bundle_install(paths: LoginItemPaths, installed: dict[str, object]) -> None:
    arguments = installed.get("ProgramArguments")
    if not isinstance(arguments, list) or not arguments or not isinstance(arguments[0], str):
        raise LoginItemError("the saved service has no executable")
    saved_program = Path(arguments[0])
    if installed.get("MediaforceAppCodeHash") != _bundle_code_hash(paths.project_root):
        raise LoginItemError(f"a different Mediaforce build owns the service. {BUNDLED_UPDATE_RECOVERY}")
    if saved_program != paths.program and saved_program.exists():
        raise LoginItemError("another copy of this app still owns the service; use that copy")


def _carry_forward_runtime_env(paths: LoginItemPaths) -> None:
    target = paths.project_root / ".env"
    if target.is_file() or not paths.plist.exists():
        return
    if target.exists():
        raise LoginItemError(f"the runtime credential path is not a file: {target}")
    try:
        installed = plistlib.loads(paths.plist.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise LoginItemError(
            f"could not identify the existing runtime credential file from {paths.plist}; create {target} first"
        ) from exc
    previous_root_value = installed.get("WorkingDirectory")
    if not isinstance(previous_root_value, str) or not previous_root_value.strip():
        raise LoginItemError(f"the existing login item has no working directory; create {target} first")
    previous_root = Path(previous_root_value).expanduser().resolve()
    if previous_root == paths.project_root.resolve():
        return
    source = previous_root / ".env"
    if not source.is_file():
        raise LoginItemError(
            f"the existing runtime has no credential file at {source}; create {target} before switching runtimes"
        )
    _atomic_write(target, source.read_bytes(), mode=0o600)


def _bootout(runner: CommandRunner, target: str) -> None:
    result = runner(["/bin/launchctl", "bootout", target])
    if result.returncode not in BOOTOUT_ACCEPTED_EXIT_CODES:
        _require_success(result, "stop the existing login item")


def _wait_until_unloaded(runner: CommandRunner, target: str) -> None:
    for _ in range(100):
        result = runner(["/bin/launchctl", "print", target])
        if result.returncode != 0:
            return
        time.sleep(0.1)
    raise LoginItemError("the previous login item process did not unload within 10 seconds")


def _bootstrap(runner: CommandRunner, domain: str, plist_path: Path) -> None:
    """Register the login item, then start it: launchd may defer a registered job's first spawn indefinitely."""
    command = ["/bin/launchctl", "bootstrap", domain, str(plist_path)]
    result = runner(command)
    if result.returncode == 5:
        time.sleep(0.25)
        result = runner(command)
    _require_success(result, "bootstrap the login item")
    # Without -k, a job launchd already started keeps running.
    _require_success(runner(["/bin/launchctl", "kickstart", f"{domain}/{LOGIN_ITEM_LABEL}"]), "start the login item")


def _require_success(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode == 0:
        return
    detail = _command_detail(result) or f"exit status {result.returncode}"
    raise LoginItemError(f"could not {action}: {detail}")


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _command_detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "").strip()


def _launch_domain() -> str:
    return f"gui/{os.getuid()}"


def _launch_target() -> str:
    return f"{_launch_domain()}/{LOGIN_ITEM_LABEL}"


def _launchctl_value(output: str, key: str) -> str | None:
    prefix = f"{key} = "
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    return None


def _login_item_path(home: Path) -> str:
    return f"{home / '.local' / 'bin'}:{LOGIN_ITEM_PATH}"


def _require_supported_platform() -> None:
    if os.uname().sysname != "Darwin":
        raise LoginItemError("macOS is required")


def _require_program(path: Path) -> None:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise LoginItemError(f"program is unavailable at {path}; run `uv sync` first")


def _show_logs(path: Path, *, follow: bool, lines: int) -> int:
    if not path.exists():
        print(f"login item: log does not exist yet ({path})")
        return 1
    command = ["/usr/bin/tail", "-n", str(max(1, lines))]
    if follow:
        command.append("-f")
    command.append(str(path))
    return subprocess.run(command, check=False).returncode


__all__ = [
    # Deploy and update transactions share these launchd and file steps.
    "_atomic_write",
    "_bootstrap",
    "_bundle_code_hash",
    "_launch_domain",
    "_launch_target",
    "_require_success",
    "_run_command",
    "_verify_signed_bundle",
    "MACOS_SIGNING_TEAM",
    "BOOTOUT_ACCEPTED_EXIT_CODES",
    "LOGIN_ITEM_LABEL",
    "LoginItemError",
    "LoginItemPaths",
    "disable_login_item",
    "enable_login_item",
    "install_login_item",
    "login_item_status",
    "render_login_item_plist",
    "rotate_login_item_logs",
    "run_login_item_command",
    "uninstall_login_item",
    "verify_bundled_startup",
]
