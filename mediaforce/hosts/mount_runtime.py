import base64
import hashlib
import os
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote

from mediaforce.core.config import load_runtime_settings, save_runtime_settings
from mediaforce.hosts.types import HostSetupResult


REMOTE_MOUNT_ATTEMPT_SECONDS = 30
CONTROLLER_SMB_MOUNTS_FILE_NAME = "controller-smb-mounts.json"

_NO_GUI_SESSION_EXIT = 41
_MOUNT_TIMEOUT_EXIT = 42
_MOUNT_BUSY_EXIT = 43
_MOUNT_HELPER_EXIT = 44
_MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
_SMB_SERVER_RE = re.compile(r"^[A-Za-z0-9._\-\[\]:]+$")


@dataclass(frozen=True, slots=True)
class ControllerSmbMount:
    source: str
    mount_point: Path


@dataclass(frozen=True, slots=True)
class RemoteSmbMount:
    mount_point: Path
    share_name: str
    url: str


def controller_smb_mounts_path(runtime_settings_path: Path) -> Path:
    return runtime_settings_path.with_name(CONTROLLER_SMB_MOUNTS_FILE_NAME)


def load_controller_smb_mounts(path: Path) -> list[ControllerSmbMount]:
    try:
        payload = load_runtime_settings(path)
    except (OSError, ValueError):
        return []
    return controller_smb_mounts_from_payload(payload.get("mounts"))


def save_controller_smb_mounts(path: Path, mounts: list[ControllerSmbMount]) -> None:
    normalized = _sanitized_controller_smb_mounts(mounts)
    save_runtime_settings(
        path,
        {
            "schema_version": 1,
            "mounts": [
                {
                    "mount_point": str(mount.mount_point),
                    "source": mount.source,
                }
                for mount in normalized
            ],
        },
    )


def controller_smb_mounts_from_payload(payload: object) -> list[ControllerSmbMount]:
    if not isinstance(payload, list):
        return []
    mounts: list[ControllerSmbMount] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        mount_point = Path(os.path.normpath(str(item.get("mount_point") or "")))
        source = str(item.get("source") or "").strip()
        normalized = _sanitized_controller_smb_mount(source, mount_point)
        if normalized is not None:
            mounts.append(normalized)
    return _deduplicate_controller_smb_mounts(mounts)


def controller_smb_mounts_from_output(raw_output: str) -> list[ControllerSmbMount]:
    mounts: list[ControllerSmbMount] = []
    for raw_line in raw_output.splitlines():
        line = raw_line.strip()
        prefix, marker, raw_options = line.rpartition(" (")
        if not marker or not raw_options.endswith(")"):
            continue
        options = raw_options[:-1].split(",", 1)[0].strip().lower()
        if options != "smbfs":
            continue
        source, separator, mount_point = prefix.partition(" on ")
        if not separator or not source.startswith("//"):
            continue
        decoded_source = _decode_mount_field(source)
        decoded_mount_point = _decode_mount_field(mount_point)
        path = Path(decoded_mount_point)
        if not path.is_absolute():
            continue
        mounts.append(ControllerSmbMount(source=decoded_source, mount_point=path))
    return _deduplicate_controller_smb_mounts(mounts)


def remote_smb_mounts_for_paths(
        paths: list[str],
        controller_mounts: list[ControllerSmbMount],
        *,
        remote_user: str | None,
) -> list[RemoteSmbMount] | None:
    resolved: dict[Path, RemoteSmbMount] = {}
    for raw_path in paths:
        path = Path(os.path.normpath(str(Path(raw_path).expanduser())))
        if not path.is_absolute():
            return None
        controller_mount = next(
            (mount for mount in controller_mounts if _path_is_within(path, mount.mount_point)),
            None,
        )
        if controller_mount is None or not _finder_mount_point_supported(controller_mount.mount_point):
            return None
        remote_mount = _remote_smb_mount(controller_mount, remote_user=remote_user)
        if remote_mount is None:
            return None
        resolved[remote_mount.mount_point] = remote_mount
    return list(resolved.values())


def finder_mount_roots_for_paths(paths: list[str | Path]) -> list[Path]:
    roots: dict[Path, None] = {}
    for raw_path in paths:
        path = Path(os.path.normpath(str(Path(raw_path).expanduser())))
        parts = path.parts
        if len(parts) < 3 or parts[0] != "/" or parts[1] != "Volumes":
            continue
        roots[Path("/Volumes") / parts[2]] = None
    return list(roots)


def mount_smb_shares(
        host: dict[str, Any],
        mounts: list[RemoteSmbMount],
        *,
        run_mount_script: Callable[[str, int], subprocess.CompletedProcess[str]],
        transport: str,
        attempt_seconds: int = REMOTE_MOUNT_ATTEMPT_SECONDS,
) -> HostSetupResult:
    label = str(host.get("label") or host.get("host") or "Remote host").strip() or "Remote host"
    login_account = _login_account_for_host(host)
    is_ssh = transport == "ssh"
    transport_failure_kind = "ssh_transport" if is_ssh else "host_unavailable"
    request_name = "remote request" if is_ssh else "local Finder helper"
    timeout_detail = (
        "The SSH request timed out. Retry after the remote host connection is stable."
        if is_ssh
        else "The local Finder helper timed out. Retry after the desktop session is responsive."
    )
    mounted_names: list[str] = []
    for mount in mounts:
        token = uuid.uuid4().hex
        script = _remote_mount_script(mount, token=token, attempt_seconds=attempt_seconds)
        try:
            result = run_mount_script(script, attempt_seconds + 15)
        except subprocess.TimeoutExpired:
            return HostSetupResult(
                ok=False,
                message=f"{label} did not finish the shared-storage connection request.",
                detail=timeout_detail,
                failure_kind=transport_failure_kind,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return HostSetupResult(
                ok=False,
                message=f"{label} could not run the shared-storage connection request.",
                detail=f"The {request_name} failed with {exc.__class__.__name__}.",
                failure_kind=transport_failure_kind,
            )
        if result.returncode == 0:
            mounted_names.append(mount.share_name)
            continue
        if result.returncode == _NO_GUI_SESSION_EXIT:
            return HostSetupResult(
                ok=False,
                message=f"{label} needs a signed-in macOS desktop session to connect shared storage.",
                detail=f"Sign in to {label} as {login_account}, then use Prepare to retry storage recovery.",
                failure_kind="host_unavailable",
            )
        if result.returncode == _MOUNT_BUSY_EXIT:
            return HostSetupResult(
                ok=False,
                message=f"{label} is already connecting the {mount.share_name} share.",
                detail="Retry after the existing shared-storage connection attempt finishes.",
                failure_kind="host_unavailable",
            )
        if result.returncode == _MOUNT_TIMEOUT_EXIT:
            return _finder_recovery_result(label, mount)
        return HostSetupResult(
            ok=False,
            message=f"{label} could not start the Finder storage helper.",
            detail=(
                "The temporary macOS launch service failed before Finder could connect shared storage. "
                "Retry the request; if it keeps failing, inspect the remote macOS launch service."
            ),
            failure_kind="host_configuration",
        )
    names = ", ".join(mounted_names)
    location = "remote " if is_ssh else ""
    return HostSetupResult(
        ok=True,
        message=f"Connected shared storage on {label}.",
        performed_steps=[f"Connected {names} using the {location}Finder Keychain."],
    )


def mount_remote_smb_shares(
        host: dict[str, Any],
        mounts: list[RemoteSmbMount],
        *,
        run_remote_ssh: Callable[..., subprocess.CompletedProcess[str]],
        attempt_seconds: int = REMOTE_MOUNT_ATTEMPT_SECONDS,
) -> HostSetupResult:
    def _run_script(script: str, timeout: int) -> subprocess.CompletedProcess[str]:
        return run_remote_ssh(host, "sh", "-s", input_text=script, timeout=timeout)

    return mount_smb_shares(
        host,
        mounts,
        run_mount_script=_run_script,
        transport="ssh",
        attempt_seconds=attempt_seconds,
    )


def _remote_mount_script(mount: RemoteSmbMount, *, token: str, attempt_seconds: int) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise ValueError("Remote mount token must be a lowercase UUID hex value.")
    expected = str(mount.mount_point)
    if not _finder_mount_point_supported(mount.mount_point):
        raise ValueError("Finder SMB mounts must target one direct child of /Volumes.")
    applescript = "\n".join([
        "use scripting additions",
        "on run argv",
        "    set share_url to item 1 of argv",
        "    mount volume share_url",
        "end run",
        "",
    ])
    runner = "\n".join(
        [
            "#!/bin/sh",
            "set -u",
            'script="$1"',
            'mount_url="$2"',
            'stdout_path="$3"',
            'stderr_path="$4"',
            'timeout_seconds="$5"',
            'lock_path="$6"',
            'service_label="$7"',
            'uid="$(/usr/bin/id -u)"',
            'runner_dir="${0%/*}"',
            '/usr/bin/osascript "$script" "$mount_url" >"$stdout_path" 2>"$stderr_path" &',
            "child_pid=$!",
            "(",
            '  /bin/sleep "$timeout_seconds"',
            '  /bin/kill "$child_pid" >/dev/null 2>&1 || true',
            ") &",
            "watchdog_pid=$!",
            "child_status=0",
            'wait "$child_pid" || child_status=$?',
            '/bin/kill "$watchdog_pid" >/dev/null 2>&1 || true',
            'wait "$watchdog_pid" >/dev/null 2>&1 || true',
            '/bin/rm -rf "$runner_dir"',
            '/bin/rmdir "$lock_path" >/dev/null 2>&1 || true',
            '/bin/launchctl bootout "gui/$uid/$service_label" >/dev/null 2>&1 || true',
            'exit "$child_status"',
            "",
        ]
    )
    applescript_payload = base64.b64encode(applescript.encode()).decode()
    runner_payload = base64.b64encode(runner.encode()).decode()
    mount_url_payload = base64.b64encode(mount.url.encode()).decode()
    lock_hash = hashlib.sha256(expected.encode()).hexdigest()[:24]
    label = f"com.mediaforce.mount.{token}"
    q_expected = shlex.quote(expected)
    q_mount_output_path = shlex.quote(mount_output_field(expected))
    q_label = shlex.quote(label)
    q_applescript_payload = shlex.quote(applescript_payload)
    q_runner_payload = shlex.quote(runner_payload)
    q_mount_url_payload = shlex.quote(mount_url_payload)
    return f"""set -u
expected={q_expected}
mount_output_path={q_mount_output_path}
label={q_label}
attempt_seconds={int(attempt_seconds)}
mount_present() {{
  mount_output="$(/sbin/mount 2>/dev/null || true)"
  if printf '%s\\n' "$mount_output" | /usr/bin/grep -F -- " on $expected (smbfs," >/dev/null 2>&1; then
    return 0
  fi
  if [ "$mount_output_path" != "$expected" ]; then
    printf '%s\\n' "$mount_output" | /usr/bin/grep -F -- " on $mount_output_path (smbfs," >/dev/null 2>&1
    return $?
  fi
  return 1
}}
if mount_present; then
  printf 'MEDIAFORCE_MOUNT=already-mounted\\n'
  exit 0
fi
uid="$(/usr/bin/id -u)"
console_uid="$(/usr/bin/stat -f '%u' /dev/console 2>/dev/null || true)"
if [ "$console_uid" != "$uid" ] || ! /bin/launchctl print "gui/$uid" >/dev/null 2>&1; then
  printf 'MEDIAFORCE_MOUNT=no-gui-session\\n'
  exit {_NO_GUI_SESSION_EXIT}
fi
lock_root="$HOME/Library/Caches/mediaforce"
/bin/mkdir -p "$lock_root" || exit {_MOUNT_HELPER_EXIT}
/bin/chmod 700 "$lock_root" >/dev/null 2>&1 || true
lock_path="$lock_root/mount-{lock_hash}.lock"
if [ -d "$lock_path" ] && [ -n "$(/usr/bin/find "$lock_path" -prune -mmin +2 -print 2>/dev/null)" ]; then
  /bin/rm -rf "$lock_path"
fi
if ! /bin/mkdir "$lock_path" 2>/dev/null; then
  waited=0
  while [ "$waited" -lt "$attempt_seconds" ]; do
    if mount_present; then
      printf 'MEDIAFORCE_MOUNT=joined-existing\\n'
      exit 0
    fi
    waited=$((waited + 1))
    /bin/sleep 1
  done
  printf 'MEDIAFORCE_MOUNT=busy-timeout\\n'
  exit {_MOUNT_BUSY_EXIT}
fi
tmp_dir=""
cleanup() {{
  /bin/launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
  if [ -n "$tmp_dir" ]; then /bin/rm -rf "$tmp_dir"; fi
  /bin/rmdir "$lock_path" >/dev/null 2>&1 || true
}}
trap cleanup EXIT HUP INT TERM
tmp_dir="$(/usr/bin/mktemp -d /tmp/mediaforce-mount.XXXXXX)" || exit {_MOUNT_HELPER_EXIT}
/bin/chmod 700 "$tmp_dir" >/dev/null 2>&1 || true
source_path="$tmp_dir/mount.applescript"
script_path="$tmp_dir/mount.scpt"
runner_path="$tmp_dir/run-mount.sh"
plist_path="$tmp_dir/$label.plist"
stdout_path="$tmp_dir/mount.out"
stderr_path="$tmp_dir/mount.err"
mount_url="$(/usr/bin/printf '%s' {q_mount_url_payload} | /usr/bin/base64 -D)" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/printf '%s' {q_applescript_payload} | /usr/bin/base64 -D >"$source_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/printf '%s' {q_runner_payload} | /usr/bin/base64 -D >"$runner_path" || exit {_MOUNT_HELPER_EXIT}
/bin/chmod 700 "$runner_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/osacompile -o "$script_path" "$source_path" >/dev/null 2>&1 || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -create xml1 "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert Label -string "$label" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert ProgramArguments -json '[]' "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert ProgramArguments.0 -string /bin/sh "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert ProgramArguments.1 -string "$runner_path" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert ProgramArguments.2 -string "$script_path" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert ProgramArguments.3 -string "$mount_url" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert ProgramArguments.4 -string "$stdout_path" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert ProgramArguments.5 -string "$stderr_path" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert ProgramArguments.6 -string "$((attempt_seconds + 5))" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert ProgramArguments.7 -string "$lock_path" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert ProgramArguments.8 -string "$label" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert RunAtLoad -bool YES "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert StandardOutPath -string "$stdout_path" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert StandardErrorPath -string "$stderr_path" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
if ! /bin/launchctl bootstrap "gui/$uid" "$plist_path" >/dev/null 2>&1; then
  printf 'MEDIAFORCE_MOUNT=bootstrap-failed\\n'
  exit {_MOUNT_HELPER_EXIT}
fi
waited=0
while [ "$waited" -lt "$attempt_seconds" ]; do
  if mount_present; then
    printf 'MEDIAFORCE_MOUNT=mounted\\n'
    exit 0
  fi
  waited=$((waited + 1))
  /bin/sleep 1
done
printf 'MEDIAFORCE_MOUNT=timeout\\n'
exit {_MOUNT_TIMEOUT_EXIT}
"""


def _remote_smb_mount(
        controller_mount: ControllerSmbMount,
        *,
        remote_user: str | None,
) -> RemoteSmbMount | None:
    source = controller_mount.source
    if not source.startswith("//"):
        return None
    authority, separator, raw_share_path = source[2:].partition("/")
    if not separator or not raw_share_path:
        return None
    raw_source_user = authority.rsplit("@", 1)[0] if "@" in authority else ""
    server = authority.rsplit("@", 1)[-1]
    if not server or not _SMB_SERVER_RE.fullmatch(server):
        return None
    source_user = unquote(raw_source_user).split(":", 1)[0]
    selected_user = source_user or unquote(remote_user or "")
    if any(ord(char) < 32 for char in selected_user):
        return None
    user_prefix = f"{quote(selected_user, safe='-._~')}@" if selected_user else ""
    decoded_share_path = unquote(raw_share_path)
    if not decoded_share_path or any(ord(char) < 32 for char in decoded_share_path):
        return None
    share_path = quote(decoded_share_path, safe="/:@-._~")
    share_name = decoded_share_path.rstrip("/").rsplit("/", 1)[-1] or controller_mount.mount_point.name
    return RemoteSmbMount(
        mount_point=controller_mount.mount_point,
        share_name=share_name,
        url=f"smb://{user_prefix}{server}/{share_path}",
    )


def _finder_recovery_result(label: str, mount: RemoteSmbMount) -> HostSetupResult:
    return HostSetupResult(
        ok=False,
        message=f"{label} could not connect the {mount.share_name} share with Finder.",
        detail=(
            f"On {label}, connect to {mount.share_name} once in Finder, "
            "save the password to Keychain, then retry."
        ),
        failure_kind="host_configuration",
    )


def _login_account_for_host(host: dict[str, Any]) -> str:
    target = str(host.get("host") or host.get("key") or "").strip()
    if "@" in target:
        account = target.rsplit("@", 1)[0].strip()
        if account:
            return account
    return "the configured SSH account"


def _sanitized_controller_smb_mount(source: str, mount_point: Path) -> ControllerSmbMount | None:
    if not _finder_mount_point_supported(mount_point) or not source.startswith("//"):
        return None
    authority, separator, raw_share_path = source[2:].partition("/")
    if not separator or not raw_share_path:
        return None
    raw_user = authority.rsplit("@", 1)[0] if "@" in authority else ""
    server = authority.rsplit("@", 1)[-1]
    if not server or not _SMB_SERVER_RE.fullmatch(server):
        return None
    user = unquote(raw_user).split(":", 1)[0]
    share_path = unquote(raw_share_path)
    if any(ord(char) < 32 for char in user + share_path):
        return None
    user_prefix = f"{quote(user, safe='-._~')}@" if user else ""
    return ControllerSmbMount(
        source=f"//{user_prefix}{server}/{share_path}",
        mount_point=mount_point,
    )


def _deduplicate_controller_smb_mounts(mounts: list[ControllerSmbMount]) -> list[ControllerSmbMount]:
    resolved: dict[Path, ControllerSmbMount] = {}
    for mount in mounts:
        resolved[mount.mount_point] = mount
    return sorted(resolved.values(), key=lambda mount: len(mount.mount_point.parts), reverse=True)


def _sanitized_controller_smb_mounts(mounts: list[ControllerSmbMount]) -> list[ControllerSmbMount]:
    sanitized = [
        normalized
        for mount in mounts
        if (normalized := _sanitized_controller_smb_mount(mount.source, mount.mount_point)) is not None
    ]
    return _deduplicate_controller_smb_mounts(sanitized)


def _finder_mount_point_supported(path: Path) -> bool:
    return (
        path.is_absolute()
        and path.parent == Path("/Volumes")
        and bool(path.name)
        and not any(ord(char) < 32 for char in str(path))
    )


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _decode_mount_field(value: str) -> str:
    return _MOUNT_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), value)


def mount_output_field(value: str) -> str:
    return value.replace("\\", "\\134").replace("\t", "\\011").replace("\n", "\\012").replace(" ", "\\040")


__all__ = [
    "CONTROLLER_SMB_MOUNTS_FILE_NAME",
    "ControllerSmbMount",
    "REMOTE_MOUNT_ATTEMPT_SECONDS",
    "RemoteSmbMount",
    "controller_smb_mounts_from_output",
    "controller_smb_mounts_from_payload",
    "controller_smb_mounts_path",
    "finder_mount_roots_for_paths",
    "load_controller_smb_mounts",
    "mount_output_field",
    "mount_remote_smb_shares",
    "mount_smb_shares",
    "remote_smb_mounts_for_paths",
    "save_controller_smb_mounts",
]
