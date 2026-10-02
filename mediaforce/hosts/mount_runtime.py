import base64
import hashlib
import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote

from mediaforce.core.config import MediaforceConfig, load_runtime_settings, save_runtime_settings
from mediaforce.hosts.types import HostSetupResult


REMOTE_MOUNT_ATTEMPT_SECONDS = 30
# How long a request Finder has not answered may keep waiting, usually on a dialog, before its helper
# gives up. Ending it does not close the dialog (#594), so it is left to the person who answers it.
REMOTE_MOUNT_REQUEST_HOLD_SECONDS = 12 * 60 * 60
CONTROLLER_SMB_MOUNTS_FILE_NAME = "controller-smb-mounts.json"

_NO_GUI_SESSION_EXIT = 41
_MOUNT_TIMEOUT_EXIT = 42
_MOUNT_BUSY_EXIT = 43
_MOUNT_HELPER_EXIT = 44
_MOUNT_REQUEST_WAITING_EXIT = 45
_MOUNT_ELSEWHERE_EXIT = 46
_SSH_CONNECTION_EXIT = 255
_MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
_SMB_SERVER_RE = re.compile(r"^[A-Za-z0-9._\-\[\]:]+$")
_HELPER_MARKER_RE = re.compile(r"^MEDIAFORCE_MOUNT(?:_JOB|_ERR|_AT)?=.*$", re.MULTILINE)
# The share URL names the account; an error echoing it must not put that in the log.
# Applied to the helper's error text before it is cut, so a cut can never split an account from its URL.
_REDACT_SMB_ACCOUNT_SED = "/usr/bin/sed -E 's#([Ss][Mm][Bb]://)[^@/[:space:]]+@#\\1#g'"
_URL_ACCOUNT_RE = re.compile(r"(smb://)[^@\s/]+@", re.IGNORECASE)
# Finder's own words for why `mount volume` failed. A failure is put in a group only when its error
# says so; anything else is reported in Finder's words without a guessed cause.
_FINDER_CANCELLED_MARKERS = ("(-128)", "user canceled", "user cancelled")
_FINDER_AUTHENTICATION_MARKERS = ("authenticat", "password", "credentials", "(-5023)")
_FINDER_UNREACHABLE_MARKERS = (
    "could not be found",
    "couldn't be found",
    "couldn\u2019t be found",
    "can't be found",
    "cannot be found",
    "no route to host",
    "host is down",
    "network is unreachable",
    "connection refused",
    "timed out",
)
_FINDER_ERROR_SHOWN_CHARS = 300
LOGGER = logging.getLogger(__name__)


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


def configured_controller_smb_mounts(config: MediaforceConfig) -> list[ControllerSmbMount]:
    learned = load_controller_smb_mounts(
        controller_smb_mounts_path(config.paths.runtime_settings_path)
    )
    overrides = controller_smb_mounts_from_payload(config.raw.get("controller_smb_mounts"))
    resolved = {mount.mount_point: mount for mount in [*learned, *overrides]}
    return sorted(
        resolved.values(),
        key=lambda mount: len(mount.mount_point.parts),
        reverse=True,
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
        script = _remote_mount_script(mount, attempt_seconds=attempt_seconds)
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
        _log_helper_failure(label, mount, result)
        if is_ssh and result.returncode == _SSH_CONNECTION_EXIT:
            return HostSetupResult(
                ok=False,
                message=f"{label} did not finish the shared-storage connection request.",
                detail="The SSH connection failed. Retry after the remote host connection is stable.",
                failure_kind=transport_failure_kind,
            )
        return _failed_mount_result(
            label,
            mount,
            result,
            login_account=login_account,
            attempt_seconds=attempt_seconds,
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


def _remote_mount_script(
        mount: RemoteSmbMount,
        *,
        attempt_seconds: int,
        request_hold_seconds: int = REMOTE_MOUNT_REQUEST_HOLD_SECONDS,
) -> str:
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
            'result_dir="$8"',
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
            # Kept outside the runner's own folder so the caller can still report it after cleanup. The
            # status appears only once the error is saved, so a caller that sees it can rely on both.
            f'{_REDACT_SMB_ACCOUNT_SED} "$stderr_path" 2>/dev/null | /usr/bin/head -c 4000 >"$result_dir/error" || true',
            'printf \'%s\\n\' "$child_status" >"$result_dir/status.tmp" 2>/dev/null'
            ' && /bin/mv -f "$result_dir/status.tmp" "$result_dir/status" 2>/dev/null || true',
            # A caller that left while this request waited can no longer remove the result folder.
            'if [ -e "$result_dir/caller-gone" ]; then /bin/rm -rf "$result_dir"; fi',
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
    # One launchd job name per share, so an attempt can see a request an earlier one left waiting.
    label = f"com.mediaforce.mount.{lock_hash}"
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
request_hold_seconds={int(request_hold_seconds)}
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
# Finder connects a share at "<name>-1" when its usual folder is still taken, often by a stale
# earlier connection. Prints the first such SMB mount point, as `mount` writes it.
mounted_elsewhere() {{
  /sbin/mount 2>/dev/null | while IFS= read -r line; do
    case "$line" in *" (smbfs,"*) ;; *) continue ;; esac
    point="${{line#* on }}"
    point="${{point%% (smbfs,*}}"
    suffix="${{point#"$mount_output_path"-}}"
    if [ "$suffix" = "$point" ]; then continue; fi
    case "$suffix" in ""|*[!0-9]*) continue ;; esac
    printf '%s\\n' "$point"
    break
  done
}}
report_mounted_elsewhere() {{
  elsewhere="$(mounted_elsewhere)"
  if [ -z "$elsewhere" ]; then return 1; fi
  printf 'MEDIAFORCE_MOUNT=mounted-elsewhere\\n'
  printf 'MEDIAFORCE_MOUNT_AT=%s\\n' "$elsewhere"
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
if report_mounted_elsewhere; then
  exit {_MOUNT_ELSEWHERE_EXIT}
fi
# A request still running from an earlier attempt may have a dialog open. Ending it would not close
# that dialog, and a new request would open a second one (#594), so wait for that one instead.
job_running() {{
  /bin/launchctl print "gui/$uid/$label" 2>/dev/null | /usr/bin/grep -E -q '^[[:space:]]*state = running$'
}}
if job_running; then
  printf 'MEDIAFORCE_MOUNT=request-waiting\\n'
  exit {_MOUNT_REQUEST_WAITING_EXIT}
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
if job_running; then
  /bin/rmdir "$lock_path" >/dev/null 2>&1 || true
  printf 'MEDIAFORCE_MOUNT=request-waiting\\n'
  exit {_MOUNT_REQUEST_WAITING_EXIT}
fi
# A job left loaded after its runner ended would refuse the new one.
/bin/launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
tmp_dir=""
result_dir=""
cleanup() {{
  if [ -n "$result_dir" ] && job_running; then
    # Leave a request Finder has not answered: ending it would not close its dialog, and the next
    # attempt finds it instead of opening another. Its runner removes its own folder and the lock
    # when it ends, and the result folder too once this side has gone.
    : >"$result_dir/caller-gone" 2>/dev/null || true
    if [ -s "$result_dir/status" ]; then /bin/rm -rf "$result_dir"; fi
    return 0
  fi
  /bin/launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
  if [ -n "$tmp_dir" ]; then /bin/rm -rf "$tmp_dir"; fi
  if [ -n "$result_dir" ]; then /bin/rm -rf "$result_dir"; fi
  /bin/rmdir "$lock_path" >/dev/null 2>&1 || true
}}
trap cleanup EXIT HUP INT TERM
tmp_dir="$(/usr/bin/mktemp -d /tmp/mediaforce-mount.XXXXXX)" || exit {_MOUNT_HELPER_EXIT}
/bin/chmod 700 "$tmp_dir" >/dev/null 2>&1 || true
result_dir="$(/usr/bin/mktemp -d /tmp/mediaforce-mount-result.XXXXXX)" || exit {_MOUNT_HELPER_EXIT}
/bin/chmod 700 "$result_dir" >/dev/null 2>&1 || true
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
/usr/bin/plutil -insert ProgramArguments.6 -string "$request_hold_seconds" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert ProgramArguments.7 -string "$lock_path" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert ProgramArguments.8 -string "$label" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert ProgramArguments.9 -string "$result_dir" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert RunAtLoad -bool YES "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert StandardOutPath -string "$stdout_path" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
/usr/bin/plutil -insert StandardErrorPath -string "$stderr_path" "$plist_path" || exit {_MOUNT_HELPER_EXIT}
# Evidence for an attempt that did not mount: whether the Finder helper is still waiting (a dialog
# can be open) and the end of its error output. The caller logs it; it never changes the outcome.
report_helper() {{
  # The runner saves its status before it removes its folder, so a saved status wins over launchd,
  # and a live read that finds the folder already gone falls back to the saved error.
  if [ -s "$result_dir/status" ]; then
    printf 'MEDIAFORCE_MOUNT_JOB=exited:%s\\n' "$(/bin/cat "$result_dir/status" 2>/dev/null)"
    error_text=""
  elif job_running; then
    printf 'MEDIAFORCE_MOUNT_JOB=running\\n'
    error_text="$({_REDACT_SMB_ACCOUNT_SED} "$stderr_path" 2>/dev/null || true)"
  else
    printf 'MEDIAFORCE_MOUNT_JOB=exited:unknown\\n'
    error_text=""
  fi
  if [ -z "$error_text" ]; then
    error_text="$(/bin/cat "$result_dir/error" 2>/dev/null || true)"
  fi
  if [ -n "$error_text" ]; then
    printf 'MEDIAFORCE_MOUNT_ERR=%s\\n' "$(printf '%s' "$error_text" | /usr/bin/tr '\\r\\n' '  ' | /usr/bin/tail -c 400)"
  fi
}}
if ! /bin/launchctl bootstrap "gui/$uid" "$plist_path" >/dev/null 2>&1; then
  printf 'MEDIAFORCE_MOUNT=bootstrap-failed\\n'
  report_helper
  exit {_MOUNT_HELPER_EXIT}
fi
waited=0
while [ "$waited" -lt "$attempt_seconds" ]; do
  if mount_present; then
    printf 'MEDIAFORCE_MOUNT=mounted\\n'
    exit 0
  fi
  if report_mounted_elsewhere; then
    report_helper
    exit {_MOUNT_ELSEWHERE_EXIT}
  fi
  waited=$((waited + 1))
  /bin/sleep 1
done
printf 'MEDIAFORCE_MOUNT=timeout\\n'
report_helper
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


def _log_helper_failure(label: str, mount: RemoteSmbMount, result: subprocess.CompletedProcess[str]) -> None:
    """Record what the Finder helper reported, so a failed connection can be told apart afterwards."""
    markers = _HELPER_MARKER_RE.findall(str(result.stdout or ""))
    evidence = _URL_ACCOUNT_RE.sub(r"\1", " ".join(marker.strip() for marker in markers)) or "no helper markers"
    LOGGER.warning(
        "Finder storage helper on %s did not connect %s: exit %s; %s",
        label, mount.share_name, result.returncode, evidence,
    )


def _helper_markers(result: subprocess.CompletedProcess[str]) -> dict[str, str]:
    markers: dict[str, str] = {}
    for line in _HELPER_MARKER_RE.findall(str(result.stdout or "")):
        key, _separator, value = line.strip().partition("=")
        markers.setdefault(key, value)
    return markers


def _failed_mount_result(
        label: str,
        mount: RemoteSmbMount,
        result: subprocess.CompletedProcess[str],
        *,
        login_account: str,
        attempt_seconds: int,
) -> HostSetupResult:
    """Say why a share did not connect using only what the helper and Finder reported."""
    share = mount.share_name
    markers = _helper_markers(result)
    still_waiting_detail = (
        f"It may be showing a dialog on {label}'s screen. Answer or cancel it there, then use Prepare "
        "to retry. Mediaforce won't send another request for this share while that one waits."
    )
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
            message=f"{label} is already connecting the {share} share.",
            detail="Retry after the existing shared-storage connection attempt finishes.",
            failure_kind="host_unavailable",
        )
    if result.returncode == _MOUNT_REQUEST_WAITING_EXIT:
        return HostSetupResult(
            ok=False,
            message=f"{label} is still waiting on an earlier request to connect the {share} share.",
            detail=still_waiting_detail,
            failure_kind="host_configuration",
        )
    if result.returncode == _MOUNT_ELSEWHERE_EXIT:
        elsewhere = _decode_mount_field(markers.get("MEDIAFORCE_MOUNT_AT", "")) or "another folder"
        return HostSetupResult(
            ok=False,
            message=f"{label} has a share connected at {elsewhere} instead of {mount.mount_point}.",
            detail=(
                f"Finder uses a name like that when {mount.mount_point} is still taken, often by a stale "
                f"earlier connection. Eject {elsewhere} on {label}, then use Prepare to retry."
            ),
            failure_kind="host_configuration",
        )
    if result.returncode == _MOUNT_HELPER_EXIT:
        return HostSetupResult(
            ok=False,
            message=f"{label} could not start the Finder storage helper.",
            detail=(
                "The temporary macOS launch service failed before Finder could connect shared storage. "
                "Retry the request; if it keeps failing, inspect the remote macOS launch service."
            ),
            failure_kind="host_configuration",
        )
    if result.returncode != _MOUNT_TIMEOUT_EXIT:
        return HostSetupResult(
            ok=False,
            message=f"{label} could not connect the {share} share.",
            detail=(
                f"The storage connection request ended unexpectedly (exit {result.returncode}). "
                "Use Prepare to retry."
            ),
            failure_kind="host_configuration",
        )
    if markers.get("MEDIAFORCE_MOUNT_JOB") == "running":
        return HostSetupResult(
            ok=False,
            message=(
                f"{label} did not connect the {share} share within {attempt_seconds} seconds, "
                "and the request is still waiting."
            ),
            detail=still_waiting_detail,
            failure_kind="host_configuration",
        )
    return _finder_error_result(label, share, markers.get("MEDIAFORCE_MOUNT_ERR", ""), attempt_seconds=attempt_seconds)


def _finder_error_result(label: str, share: str, error: str, *, attempt_seconds: int) -> HostSetupResult:
    error = _URL_ACCOUNT_RE.sub(r"\1", error).strip()
    if not error:
        return HostSetupResult(
            ok=False,
            message=f"{label} did not connect the {share} share within {attempt_seconds} seconds.",
            detail=(
                f"Finder gave no reason. Use Prepare to retry; if it keeps failing, connect {share} "
                f"in Finder on {label} to see what happens."
            ),
            failure_kind="host_configuration",
        )
    lowered = error.lower()
    reported = f"Finder reported: \u201c{error[-_FINDER_ERROR_SHOWN_CHARS:]}\u201d"
    if any(marker in lowered for marker in _FINDER_CANCELLED_MARKERS):
        return HostSetupResult(
            ok=False,
            message=f"The request to connect the {share} share on {label} was cancelled.",
            detail=f"Someone cancelled the connection dialog on {label}. Use Prepare to try again.",
            failure_kind="host_configuration",
        )
    if any(marker in lowered for marker in _FINDER_AUTHENTICATION_MARKERS):
        return HostSetupResult(
            ok=False,
            message=f"{label} could not sign in to the {share} share.",
            detail=(
                f"{reported}. On {label}, connect to {share} once in Finder and save the password to "
                "Keychain, then use Prepare to retry."
            ),
            failure_kind="host_configuration",
        )
    if any(marker in lowered for marker in _FINDER_UNREACHABLE_MARKERS):
        return HostSetupResult(
            ok=False,
            message=f"{label} could not reach the server for the {share} share.",
            detail=f"{reported}. Check that the server is on and reachable from {label}, then use Prepare to retry.",
            failure_kind="host_unavailable",
        )
    return HostSetupResult(
        ok=False,
        message=f"{label} could not connect the {share} share with Finder.",
        detail=f"{reported}. Use Prepare to retry.",
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
    "REMOTE_MOUNT_REQUEST_HOLD_SECONDS",
    "RemoteSmbMount",
    "controller_smb_mounts_from_output",
    "controller_smb_mounts_from_payload",
    "controller_smb_mounts_path",
    "configured_controller_smb_mounts",
    "finder_mount_roots_for_paths",
    "load_controller_smb_mounts",
    "mount_output_field",
    "mount_remote_smb_shares",
    "mount_smb_shares",
    "remote_smb_mounts_for_paths",
    "save_controller_smb_mounts",
]
