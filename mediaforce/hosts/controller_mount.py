import fcntl
import json
import os
import re
import socket
import subprocess
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal
from urllib.parse import unquote

from mediaforce.hosts.mount_runtime import ControllerSmbMount, remote_smb_mounts_for_paths


AccessMode = Literal["read", "write"]
SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]
ServerResolver = Callable[[str], frozenset[str]]

_PROBE_TIMEOUT_SECONDS = 10
_MOUNT_TIMEOUT_SECONDS = 30
_SMB_PORT = 445
_BONJOUR_SMB_SERVICE = "._smb._tcp."
_PROBE_SCRIPT = r'''set -u
mount_output="$(/sbin/mount 2>/dev/null)" || exit 20
/usr/bin/printf '%s\n' "$mount_output"
/usr/bin/printf 'MEDIAFORCE_MOUNT_PATH_PRESENT='
mount_point=$1
if [ -e "$mount_point" ] || [ -L "$mount_point" ]; then
  /usr/bin/printf '1\n'
else
  /usr/bin/printf '0\n'
fi
/usr/bin/printf '%s\n' 'MEDIAFORCE_ACCESS_BEGIN'
shift 1
for access_spec in "$@"; do
  mode=${access_spec%%:*}
  path=${access_spec#*:}
  physical_path=""
  if [ -d "$path" ] && [ -x "$path" ]; then
    physical_path="$(CDPATH= cd -- "$path" 2>/dev/null && pwd -P)"
  fi
  within_mount=0
  case "$physical_path" in
    "$mount_point"|"$mount_point"/*) within_mount=1 ;;
  esac
  if [ "$within_mount" = 1 ] && \
      { { [ "$mode" = read ] && [ -r "$path" ]; } || \
        { [ "$mode" = write ] && [ -w "$path" ]; }; }; then
    /usr/bin/printf '1\n'
  else
    /usr/bin/printf '0\n'
  fi
done
'''
_JXA_MOUNT_SCRIPT = r'''ObjC.import("Foundation");
ObjC.import("NetFS");

function run(argv) {
    const url = $.NSURL.URLWithString($(argv[0]));
    const openOptions = $.NSMutableDictionary.alloc.init;
    openOptions.setObjectForKey($("NoUI"), $("UIOption"));
    const mountpoints = Ref();
    const status = $.NetFSMountURLSync(url, null, null, null, openOptions, null, mountpoints);
    return JSON.stringify({status: Number(status)});
}
'''


@dataclass(frozen=True, slots=True)
class ControllerMountProbe:
    mounted: bool
    accessible: bool
    mount_point: Path
    observed_source: str | None = None
    filesystem: str | None = None
    occupied: bool = False
    failure_kind: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ControllerMountResult:
    ok: bool
    action_required: bool
    failure_kind: str | None
    detail: str | None
    probe: ControllerMountProbe


@contextmanager
def controller_mount_lock(runtime_settings_path: Path) -> Iterator[bool]:
    lock_path = runtime_settings_path.with_name("controller-smb-mount.lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        raise OSError("Controller SMB mount lock could not be opened.") from None
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        except OSError:
            raise OSError("Controller SMB mount lock could not be acquired.") from None
        yield acquired
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def probe_controller_mount(
        mount: ControllerSmbMount,
        required_paths: Mapping[str | Path, AccessMode],
        *,
        run_subprocess: SubprocessRunner = subprocess.run,
        timeout_seconds: int = _PROBE_TIMEOUT_SECONDS,
        resolve_server: ServerResolver | None = None,
) -> ControllerMountProbe:
    if _mapping_has_credentials(mount):
        return _failed_probe(
            mount.mount_point,
            "invalid_mapping",
            "The saved controller SMB mapping contains credentials and cannot be used.",
        )
    return _probe_controller_volume(
        mount.mount_point,
        required_paths,
        expected_mount=mount,
        run_subprocess=run_subprocess,
        timeout_seconds=timeout_seconds,
        resolve_server=resolve_server,
    )


def probe_controller_volume(
        mount_point: Path,
        required_paths: Mapping[str | Path, AccessMode],
        *,
        run_subprocess: SubprocessRunner = subprocess.run,
        timeout_seconds: int = _PROBE_TIMEOUT_SECONDS,
) -> ControllerMountProbe:
    return _probe_controller_volume(
        mount_point,
        required_paths,
        expected_mount=None,
        run_subprocess=run_subprocess,
        timeout_seconds=timeout_seconds,
        resolve_server=None,
    )


def _probe_controller_volume(
        mount_point: Path,
        required_paths: Mapping[str | Path, AccessMode],
        *,
        expected_mount: ControllerSmbMount | None,
        run_subprocess: SubprocessRunner,
        timeout_seconds: int,
        resolve_server: ServerResolver | None,
) -> ControllerMountProbe:
    access_specs = [f"{mode}:{Path(path)}" for path, mode in required_paths.items()]
    try:
        result = run_subprocess(
            [
                "/bin/sh", "-c", _PROBE_SCRIPT, "mediaforce-controller-probe",
                str(mount_point), *access_specs,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return _failed_probe(mount_point, "probe_timeout", "Controller storage readiness check timed out.")
    except (OSError, subprocess.SubprocessError) as exc:
        return _failed_probe(
            mount_point,
            "probe_failed",
            f"Controller storage readiness check failed with {exc.__class__.__name__}.",
        )
    if result.returncode != 0:
        return _failed_probe(
            mount_point,
            "probe_failed",
            "Controller storage readiness check could not inspect mounted volumes.",
        )
    mount_output, path_marker, remainder = result.stdout.partition("MEDIAFORCE_MOUNT_PATH_PRESENT=")
    path_present, access_marker, access_output = remainder.partition("\nMEDIAFORCE_ACCESS_BEGIN\n")
    if not path_marker or not access_marker or path_present not in {"0", "1"}:
        return _failed_probe(
            mount_point, "probe_failed", "Controller storage readiness check returned malformed output.",
        )
    observed_source, filesystem = _mounted_identity_at(mount_output, mount_point)
    occupied = path_present == "1"
    if observed_source is None:
        return ControllerMountProbe(
            False, False, mount_point, occupied=occupied,
            failure_kind="mount_path_occupied" if occupied else "mount_absent",
            detail=(
                "The expected controller mount path exists without a mounted volume."
                if occupied else "The expected controller volume is not mounted."
            ),
        )
    if expected_mount is not None and (
            filesystem != "smbfs"
            or not same_smb_share(observed_source, expected_mount.source, resolve_server=resolve_server)
    ):
        return ControllerMountProbe(
            False, False, mount_point, observed_source, filesystem, True,
            "mount_identity_mismatch", "The expected mount path is occupied by a different volume.",
        )
    access_results = access_output.splitlines()
    accessible = len(access_results) == len(access_specs) and all(value == "1" for value in access_results)
    return ControllerMountProbe(
        True, accessible, mount_point, observed_source, filesystem, True,
        None if accessible else "path_unavailable",
        None if accessible else "The controller SMB volume is mounted, but a required path is unavailable.",
    )


def mount_controller_smb_no_ui(
        mount: ControllerSmbMount,
        required_paths: Mapping[str | Path, AccessMode],
        *,
        run_subprocess: SubprocessRunner = subprocess.run,
        probe_timeout_seconds: int = _PROBE_TIMEOUT_SECONDS,
        mount_timeout_seconds: int = _MOUNT_TIMEOUT_SECONDS,
        resolve_server: ServerResolver | None = None,
) -> ControllerMountResult:
    before = probe_controller_mount(
        mount, required_paths, run_subprocess=run_subprocess, timeout_seconds=probe_timeout_seconds,
        resolve_server=resolve_server,
    )
    if before.mounted and before.accessible:
        return ControllerMountResult(True, False, None, None, before)
    if before.failure_kind == "invalid_mapping":
        return ControllerMountResult(False, True, before.failure_kind, before.detail, before)
    if before.occupied or before.failure_kind in {"probe_timeout", "probe_failed"}:
        return ControllerMountResult(False, before.occupied, before.failure_kind, before.detail, before)
    planned = remote_smb_mounts_for_paths([str(mount.mount_point)], [mount], remote_user=None)
    if not planned or len(planned) != 1:
        return ControllerMountResult(
            False, True, "invalid_mapping", "The saved controller SMB mapping is invalid.", before,
        )
    try:
        result = run_subprocess(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", _JXA_MOUNT_SCRIPT, planned[0].url],
            capture_output=True,
            text=True,
            timeout=mount_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return ControllerMountResult(
            False, True, "mount_timeout",
            "The no-UI controller SMB mount attempt timed out and must not be retried automatically.", before,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ControllerMountResult(
            False, False, "mount_helper_failed", f"The no-UI SMB helper failed with {exc.__class__.__name__}.", before,
        )
    status = _mount_helper_output(result)
    if result.returncode != 0 or status is None:
        diagnostic_class = _helper_error_class(result.stderr)
        diagnostic_detail = f" ({diagnostic_class})" if diagnostic_class else ""
        return ControllerMountResult(
            False, True, "mount_result_unknown",
            f"The no-UI SMB helper returned an unreadable result: osascript exit {result.returncode}"
            f"{diagnostic_detail}.",
            before,
        )
    if status != 0:
        return ControllerMountResult(
            False, False, "mount_failed", f"The no-UI SMB helper returned status {status}.", before,
        )
    after = probe_controller_mount(
        mount, required_paths, run_subprocess=run_subprocess, timeout_seconds=probe_timeout_seconds,
        resolve_server=resolve_server,
    )
    return ControllerMountResult(
        after.mounted and after.accessible,
        not (after.mounted and after.accessible),
        after.failure_kind,
        after.detail,
        after,
    )


def _failed_probe(mount_point: Path, failure_kind: str, detail: str) -> ControllerMountProbe:
    return ControllerMountProbe(False, False, mount_point, failure_kind=failure_kind, detail=detail)


def _mount_helper_output(result: subprocess.CompletedProcess[str]) -> int | None:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return status


def _mounted_identity_at(raw_output: str, mount_point: Path) -> tuple[str | None, str | None]:
    expected = os.fspath(mount_point)
    for raw_line in raw_output.splitlines():
        prefix, marker, raw_options = raw_line.strip().rpartition(" (")
        if not marker or not raw_options.endswith(")"):
            continue
        source, separator, raw_path = prefix.partition(" on ")
        if separator and _decode_mount_field(raw_path) == expected:
            return _decode_mount_field(source), raw_options[:-1].split(",", 1)[0].strip().lower()
    return None, None


def _smb_identity(source: str) -> tuple[str, str, str] | None:
    if not source.startswith("//"):
        return None
    authority, separator, raw_share = source[2:].partition("/")
    if not separator or not raw_share:
        return None
    raw_user, user_separator, server = authority.rpartition("@")
    user = unquote(raw_user).split(":", 1)[0] if user_separator else ""
    if not user_separator:
        server = authority
    return user, unquote(server).lower(), unquote(raw_share)


def same_smb_share(
        observed_source: str,
        expected_source: str,
        *,
        resolve_server: ServerResolver | None = None,
) -> bool:
    """Whether two SMB sources name the same share on the same server.

    The same server can be mounted under different names, for example
    `nas.shiny` and the Bonjour service name `nas._smb._tcp.local` that Finder
    uses after a reconnect (#612). Names that differ count as one server only
    when both resolve to a common address; a name that does not resolve fails
    closed.
    """
    observed = _smb_identity(observed_source)
    expected = _smb_identity(expected_source)
    if observed == expected:
        return True
    if observed is None or expected is None:
        return False
    observed_user, observed_server, observed_share = observed
    expected_user, expected_server, expected_share = expected
    if observed_user != expected_user or observed_share != expected_share:
        return False
    resolve = resolve_server or _resolve_server_addresses
    return bool(resolve(observed_server) & resolve(expected_server))


def _resolve_server_addresses(server: str) -> frozenset[str]:
    host = _bonjour_service_host(server) or server.removeprefix("[").removesuffix("]")
    try:
        addresses = socket.getaddrinfo(host, _SMB_PORT, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return frozenset()
    return frozenset(str(address[4][0]) for address in addresses)


def _bonjour_service_host(server: str) -> str | None:
    instance, marker, domain = server.partition(_BONJOUR_SMB_SERVICE)
    if not marker or not instance or domain.rstrip(".") != "local":
        return None
    return f"{instance}.local"


def _mapping_has_credentials(mount: ControllerSmbMount) -> bool:
    source = mount.source
    if not source.startswith("//"):
        return False
    authority = source[2:].partition("/")[0]
    raw_user = authority.rpartition("@")[0] if "@" in authority else ""
    return ":" in unquote(raw_user)


def _helper_error_class(stderr: str) -> str | None:
    match = re.search(r"\b(TypeError|ReferenceError|SyntaxError)\b", stderr)
    return match.group(1) if match else None


def _decode_mount_field(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


__all__ = [
    "AccessMode",
    "ControllerMountProbe",
    "ControllerMountResult",
    "controller_mount_lock",
    "mount_controller_smb_no_ui",
    "probe_controller_mount",
    "probe_controller_volume",
    "same_smb_share",
]
