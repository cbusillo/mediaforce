from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


LOGIN_ITEM_LABEL = "com.mediaforce.web"
LOGIN_ITEM_LOG_ROTATION_BYTES = 16 * 1024 * 1024
LOGIN_ITEM_PATH = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class LoginItemError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LoginItemPaths:
    project_root: Path
    home: Path

    @property
    def program(self) -> Path:
        return self.project_root / ".venv" / "bin" / "mediaforce-web"

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
    payload = {
        "Label": LOGIN_ITEM_LABEL,
        "ProgramArguments": [str(paths.program), "--no-reload"],
        "WorkingDirectory": str(paths.project_root),
        "EnvironmentVariables": {"PATH": _login_item_path(paths.home)},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Standard",
        "StandardOutPath": str(paths.stdout_log),
        "StandardErrorPath": str(paths.stderr_log),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def install_login_item(paths: LoginItemPaths) -> str:
    _require_supported_platform()
    _require_program(paths.program)
    paths.plist.parent.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
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
    rendered = render_login_item_plist(paths)
    if paths.plist.read_bytes() != rendered:
        return 3, [
            "login item: installed plist is stale",
            f"  plist    {paths.plist}",
            "  next     run `uv run mediaforce service enable`",
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
) -> int:
    paths = LoginItemPaths(project_root=project_root.expanduser().resolve(), home=(home or Path.home()).expanduser())
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
    except LoginItemError as exc:
        print(f"login item: {exc}")
        return 1
    raise ValueError(f"Unsupported login item action: {action}")


def _atomic_write(path: Path, payload: bytes) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _bootout(runner: CommandRunner, target: str) -> None:
    result = runner(["/bin/launchctl", "bootout", target])
    if result.returncode not in {0, 3, 36, 113}:
        _require_success(result, "stop the existing login item")


def _wait_until_unloaded(runner: CommandRunner, target: str) -> None:
    for _ in range(100):
        result = runner(["/bin/launchctl", "print", target])
        if result.returncode != 0:
            return
        time.sleep(0.1)
    raise LoginItemError("the previous login item process did not unload within 10 seconds")


def _bootstrap(runner: CommandRunner, domain: str, plist_path: Path) -> None:
    command = ["/bin/launchctl", "bootstrap", domain, str(plist_path)]
    result = runner(command)
    if result.returncode == 0:
        return
    if result.returncode == 5:
        time.sleep(0.25)
        result = runner(command)
    _require_success(result, "bootstrap the login item")


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
]
