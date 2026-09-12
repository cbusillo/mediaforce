import fcntl
import json
import os
import re
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

_PROBE_TIMEOUT_SECONDS = 10
_MOUNT_TIMEOUT_SECONDS = 30
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
    let paths = [];
    if (mountpoints[0]) {
        paths = ObjC.deepUnwrap(mountpoints[0]);
    }
    return JSON.stringify({status: Number(status), mountpoints: paths});
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
    )


def _probe_controller_volume(
        mount_point: Path,
        required_paths: Mapping[str | Path, AccessMode],
        *,
        expected_mount: ControllerSmbMount | None,
        run_subprocess: SubprocessRunner,
        timeout_seconds: int,
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
            filesystem != "smbfs" or _smb_identity(observed_source) != _smb_identity(expected_mount.source)
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
) -> ControllerMountResult:
    before = probe_controller_mount(
        mount, required_paths, run_subprocess=run_subprocess, timeout_seconds=probe_timeout_seconds,
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
    status, returned_paths = _mount_helper_output(result)
    if result.returncode != 0 or status is None:
        return ControllerMountResult(
            False, True, "mount_result_unknown", "The no-UI SMB helper returned an unreadable result.", before,
        )
    if status != 0:
        return ControllerMountResult(
            False, False, "mount_failed", f"The no-UI SMB helper returned status {status}.", before,
        )
    if str(mount.mount_point) not in returned_paths:
        safe_paths = _safe_returned_mount_paths(returned_paths)
        location_detail = f" Returned path(s): {', '.join(safe_paths)}." if safe_paths else ""
        return ControllerMountResult(
            False, True, "unexpected_mount_path",
            f"The SMB helper did not mount the share at the saved controller path.{location_detail}", before,
        )
    after = probe_controller_mount(
        mount, required_paths, run_subprocess=run_subprocess, timeout_seconds=probe_timeout_seconds,
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


def _mount_helper_output(result: subprocess.CompletedProcess[str]) -> tuple[int | None, list[str]]:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, []
    if not isinstance(payload, dict):
        return None, []
    status = payload.get("status")
    paths = payload.get("mountpoints")
    if isinstance(status, bool) or not isinstance(status, int):
        return None, []
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        return None, []
    return status, paths


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


def _mapping_has_credentials(mount: ControllerSmbMount) -> bool:
    source = mount.source
    if not source.startswith("//"):
        return False
    authority = source[2:].partition("/")[0]
    raw_user = authority.rpartition("@")[0] if "@" in authority else ""
    return ":" in unquote(raw_user)


def _safe_returned_mount_paths(paths: list[str]) -> list[str]:
    safe: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        if (
                path.parent == Path("/Volumes")
                and path.name
                and not any(ord(character) < 32 for character in raw_path)
        ):
            safe.append(str(path))
    return safe


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
]
