import platform as platform_module
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from mediaforce.core.timezones import system_timezone_name
from mediaforce.core.type_defs import object_list
from mediaforce.encoding.ffmpeg import SVT_AV1_REQUIRED_ISSUE, VIDEOTOOLBOX_REQUIRED_ISSUE, has_videotoolbox_hwaccel, \
    normalize_execution_platform
from mediaforce.hosts.config import _host_supports_capability, host_media_access_for_host, \
    _parse_utc_offset_minutes, remote_shell_path_export_line, stream_host_has_remote_source_roots
from mediaforce.hosts.mount_runtime import mount_output_field
from mediaforce.hosts.types import AB_AV1_MISSING_ISSUE, FFMPEG_MISSING_ISSUE, HostStatus, \
    LINUX_SAMPLE_CALIBRATION_UNSUPPORTED_ISSUE, SAMPLE_AV1_ENCODER_MISSING_ISSUE, SAMPLE_METRIC_MISSING_ISSUE, \
    SOURCE_ROOT_READ_MISSING_ISSUE, STAGING_ROOT_WRITE_MISSING_ISSUE


def _local_tool_status_snapshot() -> dict[str, bool]:
    ffmpeg_bin = _find_local_tool("ffmpeg", fallback_paths=["/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"])
    ffmpeg_filters = _command_output([ffmpeg_bin, "-hide_banner", "-filters"]) if ffmpeg_bin else ""
    ffmpeg_encoders = _command_output([ffmpeg_bin, "-hide_banner", "-encoders"]) if ffmpeg_bin else ""
    ffmpeg_hwaccels = _command_output([ffmpeg_bin, "-hide_banner", "-hwaccels"]) if ffmpeg_bin else ""
    return {
        "xcode_clt": _command_succeeds(["xcode-select", "-p"]),
        "brew": bool(_find_local_tool("brew", fallback_paths=["/opt/homebrew/bin/brew", "/usr/local/bin/brew"])),
        "ffmpeg": bool(ffmpeg_bin),
        "ffmpeg_videotoolbox": has_videotoolbox_hwaccel(ffmpeg_hwaccels),
        "ffmpeg_libvmaf": "libvmaf" in ffmpeg_filters.lower(),
        "ffmpeg_xpsnr": "xpsnr" in ffmpeg_filters.lower(),
        "ffmpeg_libsvtav1": "libsvtav1" in ffmpeg_encoders.lower(),
        "ab_av1": bool(
            _find_local_tool("ab-av1", fallback_paths=["/opt/homebrew/bin/ab-av1", "/usr/local/bin/ab-av1"])),
    }


def _local_platform_name() -> str:
    return normalize_execution_platform(platform_module.system())


def _status_platform(value: object, *, tools: dict[str, bool] | None = None) -> str:
    normalized = normalize_execution_platform(str(value or ""))
    if normalized != "unknown":
        return normalized
    tool_status: dict[str, bool] = tools if tools is not None else {}
    if any(key in tool_status for key in ("xcode_clt", "brew", "ffmpeg_videotoolbox")):
        return "macos"
    return normalized


def _host_setup_supported(platform_name: str) -> bool:
    return platform_name == "macos"


def _host_capability_issues(
        host: dict[str, object],
        *,
        tools: dict[str, bool],
        platform_name: str,
        repo_path: str | None,
        repo_path_exists: bool,
        path_access: dict[str, dict[str, bool]] | None = None,
        source_paths: list[str] | None = None,
        staging_path: str | None = None,
        supports_remote_stream_quality: bool | None = None,
) -> list[str]:
    capability_issues: list[str] = []
    supports_sample = _host_supports_capability(host, "sample_calibration")
    supports_encode = _host_supports_capability(host, "encode_queue")
    if supports_remote_stream_quality is None:
        supports_remote_stream_quality = stream_host_has_remote_source_roots(host)
    needs_remote_ab_av1 = supports_sample or (
        supports_encode and (host_media_access_for_host(host) != "stream" or supports_remote_stream_quality)
    )
    needs_remote_quality_metric = supports_sample or (
        supports_encode and (host_media_access_for_host(host) != "stream" or supports_remote_stream_quality)
    )

    if platform_name == "macos":
        if not bool(tools.get("xcode_clt")):
            capability_issues.append("Xcode Command Line Tools are not installed on the remote Mac.")
        if not bool(tools.get("brew")):
            capability_issues.append("Homebrew is not installed on the remote Mac.")
        if not bool(tools.get("ffmpeg")):
            capability_issues.append(FFMPEG_MISSING_ISSUE)
        elif not bool(tools.get("ffmpeg_videotoolbox")):
            capability_issues.append(VIDEOTOOLBOX_REQUIRED_ISSUE)
        if needs_remote_ab_av1 and not bool(tools.get("ab_av1")):
            capability_issues.append(AB_AV1_MISSING_ISSUE)
        if needs_remote_quality_metric and bool(tools.get("ffmpeg")) and not (
                bool(tools.get("ffmpeg_libvmaf")) or bool(tools.get("ffmpeg_xpsnr"))
        ):
            capability_issues.append(SAMPLE_METRIC_MISSING_ISSUE)
        if supports_sample:
            if bool(tools.get("ffmpeg")) and not bool(tools.get("ffmpeg_libsvtav1")):
                capability_issues.append(SAMPLE_AV1_ENCODER_MISSING_ISSUE)
    elif platform_name == "linux":
        if not bool(tools.get("ffmpeg")):
            capability_issues.append(FFMPEG_MISSING_ISSUE)
        if needs_remote_ab_av1 and not bool(tools.get("ab_av1")):
            capability_issues.append(AB_AV1_MISSING_ISSUE)
        if supports_encode and not bool(tools.get("ffmpeg_libsvtav1")):
            capability_issues.append(SVT_AV1_REQUIRED_ISSUE)
        if needs_remote_quality_metric and bool(tools.get("ffmpeg")) and not (
                bool(tools.get("ffmpeg_libvmaf")) or bool(tools.get("ffmpeg_xpsnr"))
        ):
            capability_issues.append(SAMPLE_METRIC_MISSING_ISSUE)
        if supports_sample:
            capability_issues.append(LINUX_SAMPLE_CALIBRATION_UNSUPPORTED_ISSUE)
    else:
        capability_issues.append("Unsupported remote platform.")

    if supports_encode or supports_sample:
        path_issues = _host_path_access_issues(
            path_access=path_access or {},
            source_paths=source_paths or [],
            staging_path=staging_path,
        )
        capability_issues.extend(path_issues)

    if repo_path and not repo_path_exists and supports_sample:
        capability_issues.append(f"Repo path is missing: {repo_path}")
    return capability_issues


def _host_path_access_issues(
        *,
        path_access: dict[str, dict[str, bool]],
        source_paths: list[str],
        staging_path: str | None,
) -> list[str]:
    issues: list[str] = []
    for source_path in source_paths:
        access = path_access.get(source_path, {})
        if access and bool(access.get("exists", True)) and not bool(access.get("read")):
            issues.append(f"{SOURCE_ROOT_READ_MISSING_ISSUE} {source_path}")
    if staging_path:
        access = path_access.get(staging_path, {})
        if access and bool(access.get("exists", True)) and not bool(access.get("write")):
            issues.append(f"{STAGING_ROOT_WRITE_MISSING_ISSUE} {staging_path}")
    return issues


def _find_local_tool(name: str, *, fallback_paths: list[str]) -> str | None:
    # noinspection PyDeprecation
    discovered = shutil.which(name)
    if discovered:
        return discovered
    for fallback in fallback_paths:
        candidate = Path(fallback)
        if candidate.exists():
            return str(candidate)
    return None


def _command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _command_succeeds(command: list[str]) -> bool:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _local_utc_offset_minutes() -> int | None:
    text = time.strftime("%z")
    return _parse_utc_offset_minutes(text)


def _local_schedule_timezone() -> str | None:
    return system_timezone_name()


def _status_from_paths(
        *,
        key: str,
        label: str,
        mode: str,
        priority: int,
        capabilities: list[str],
        mounted_paths: dict[str, bool],
        repo_path: str | None,
        ffmpeg_path: str | None = None,
        platform: str = "unknown",
        videotoolbox_available: bool | None = None,
        schedule_timezone: str | None = None,
        utc_offset_minutes: int | None = None,
        issues: list[str] | None = None,
        missing_mounts: list[str] | None = None,
        setup_supported: bool = False,
        setup_requires_password: bool = False,
        require_paths: bool = True,
) -> HostStatus:
    missing_paths = [path for path, mounted in mounted_paths.items() if not mounted]
    missing_mount_list = list(missing_mounts or [])
    issue_list = [str(issue) for issue in object_list(issues)]
    available = (
        (len(mounted_paths) > 0 or not require_paths)
        and not missing_paths
        and not missing_mount_list
        and not issue_list
    )
    return HostStatus(
        key=key,
        label=label,
        mode=mode,
        priority=priority,
        capabilities=capabilities,
        available=available,
        message=_host_message(
            available=available,
            missing_paths=missing_paths,
            missing_mounts=missing_mount_list,
            issues=issue_list,
        ),
        missing_paths=missing_paths,
        probe_available=True,
        repo_path=repo_path,
        ffmpeg_path=ffmpeg_path,
        platform=platform,
        videotoolbox_available=videotoolbox_available,
        schedule_timezone=schedule_timezone,
        utc_offset_minutes=utc_offset_minutes,
        issues=issue_list,
        missing_mounts=missing_mount_list,
        setup_supported=setup_supported,
        setup_requires_password=setup_requires_password,
    )


def _host_message(
        *,
        available: bool,
        missing_paths: list[str],
        missing_mounts: list[str],
        issues: list[str],
) -> str:
    _ = missing_paths
    if available:
        return "Mounted and ready"
    if missing_mounts:
        return "Shared storage disconnected"
    if any(issue == LINUX_SAMPLE_CALIBRATION_UNSUPPORTED_ISSUE for issue in issues):
        return "Linux sample unsupported"
    if any(issue == "Xcode Command Line Tools are not installed on the remote Mac." for issue in issues):
        return "Finish Xcode tools install"
    if any(issue == "Homebrew is not installed on the remote Mac." for issue in issues):
        return "Install Homebrew first"
    if any(issue == FFMPEG_MISSING_ISSUE for issue in issues):
        return "Install ffmpeg first"
    if any(issue == AB_AV1_MISSING_ISSUE for issue in issues):
        return "Install ab-av1 first"
    if any(issue == SVT_AV1_REQUIRED_ISSUE for issue in issues):
        return "Install AV1 encoder support"
    if any(issue.startswith(SOURCE_ROOT_READ_MISSING_ISSUE) for issue in issues):
        return "Source root not readable"
    if any(issue.startswith(STAGING_ROOT_WRITE_MISSING_ISSUE) for issue in issues):
        return "Staging root not writable"
    if issues:
        return "Needs remote setup"
    return "Missing required paths"


def _should_retry_remote_status_exception(exc: Exception) -> bool:
    if isinstance(exc, subprocess.TimeoutExpired):
        return True
    return _should_retry_remote_status_failure(str(exc))


def _should_retry_remote_status_failure(detail: str) -> bool:
    lowered = detail.lower()
    transient_markers = (
        "timed out",
        "operation timed out",
        "connection reset",
        "connection reset by peer",
        "connection closed",
        "connection closed by remote host",
        "kex_exchange_identification",
        "broken pipe",
        "resource temporarily unavailable",
    )
    return any(marker in lowered for marker in transient_markers)


def _remote_setup_needs_password(issues: list[str]) -> bool:
    password_required_issues = {
        "Homebrew is not installed on the remote Mac.",
    }
    return any(issue in password_required_issues for issue in issues)


def _needs_initial_ssh_key_install(status: HostStatus) -> bool:
    return status.message == "SSH access setup required"


def _default_public_key_path() -> Path | None:
    preferred = [
        Path.home() / ".ssh" / "id_ed25519.pub",
        Path.home() / ".ssh" / "id_rsa.pub",
    ]
    for candidate in preferred:
        if candidate.exists():
            return candidate
    matches = sorted((Path.home() / ".ssh").glob("*.pub"))
    return matches[0] if matches else None


def _private_key_path_for_public_key(public_key: Path) -> Path | None:
    private_key = public_key.with_suffix("")
    return private_key if private_key.exists() else None


def _classify_ssh_failure(detail: str) -> dict[str, Any]:
    lowered = detail.lower()
    if "remote host identification has changed" in lowered or "host key verification failed" in lowered:
        return {
            "message": "SSH host key changed",
            "issues": [
                "The saved SSH fingerprint no longer matches this host. Verify the machine, then reset the stale known_hosts entry and retry."
            ],
            "setup_supported": False,
            "setup_requires_password": False,
            "trust_reset_supported": True,
            "show_detail": False,
        }
    if "permission denied" in lowered:
        return {
            "message": "SSH access setup required",
            "issues": [
                "Turn on Remote Login on the target Mac, then enter that account password once so Mediaforce can install this Mac's SSH key."
            ],
            "setup_supported": True,
            "setup_requires_password": True,
            "trust_reset_supported": False,
            "show_detail": True,
        }
    if (
            "the authenticity of host" in lowered
            or "are you sure you want to continue connecting" in lowered
            or "host key is known by the following other names" in lowered
    ):
        return {
            "message": "SSH trust needs confirmation",
            "issues": [
                "SSH reached the machine, but this hostname or alias still needs to be trusted. Verify the host, then retry or connect once manually to record the new alias."
            ],
            "setup_supported": False,
            "setup_requires_password": False,
            "trust_reset_supported": False,
            "show_detail": True,
        }
    if "connection refused" in lowered or "operation timed out" in lowered or "no route to host" in lowered:
        return {
            "message": "Turn on SSH first",
            "issues": [
                "Enable Remote Login on the target machine first. Once SSH answers, Mediaforce can finish setup automatically."
            ],
            "setup_supported": False,
            "setup_requires_password": False,
            "trust_reset_supported": False,
            "show_detail": True,
        }
    if (
            "could not resolve hostname" in lowered
            or "name or service not known" in lowered
            or "temporary failure in name resolution" in lowered
            or "nodename nor servname provided" in lowered
    ):
        return {
            "message": "Fix hostname or DNS first",
            "issues": [
                "Update the SSH host to a resolvable hostname or IP address before trying remote setup."
            ],
            "setup_supported": False,
            "setup_requires_password": False,
            "trust_reset_supported": False,
            "show_detail": True,
        }
    if "python3" in lowered and ("not found" in lowered or "command not found" in lowered):
        return {
            "message": "python3 is required",
            "issues": ["Install python3 on the remote host before using it for Mediaforce checks."],
            "setup_supported": True,
            "setup_requires_password": False,
            "trust_reset_supported": False,
            "show_detail": True,
        }
    return {
        "message": "SSH unavailable",
        "issues": ["Mediaforce could not complete the remote capability check."],
        "setup_supported": True,
        "setup_requires_password": False,
        "trust_reset_supported": False,
        "show_detail": True,
    }


def _remote_status_script(
        *,
        paths: list[str],
        repo_path: str,
        include_expensive_tools: bool = True,
        mount_paths: list[str] | None = None,
        writable_paths: list[str] | None = None,
) -> str:
    quoted_paths = " ".join(shlex.quote(path) for path in paths)
    quoted_writable_paths = " ".join(shlex.quote(path) for path in (writable_paths or []))
    lines = [
        remote_shell_path_export_line(),
        'BREW_BIN="$(command -v brew || true)"',
        'if [ -z "$BREW_BIN" ] && [ -x /opt/homebrew/bin/brew ]; then BREW_BIN=/opt/homebrew/bin/brew; fi',
        'if [ -z "$BREW_BIN" ] && [ -x /usr/local/bin/brew ]; then BREW_BIN=/usr/local/bin/brew; fi',
        'FFMPEG_BIN="$(command -v ffmpeg || true)"',
        'PLATFORM_NAME="$(uname -s 2>/dev/null | tr "[:upper:]" "[:lower:]")"',
        'if [ "$PLATFORM_NAME" = "darwin" ]; then PLATFORM_NAME="macos"; fi',
        'AB_AV1_BIN="$(command -v ab-av1 || true)"',
        'if [ -z "$AB_AV1_BIN" ] && [ -x /opt/homebrew/bin/ab-av1 ]; then AB_AV1_BIN=/opt/homebrew/bin/ab-av1; fi',
        'if [ -z "$AB_AV1_BIN" ] && [ -x /usr/local/bin/ab-av1 ]; then AB_AV1_BIN=/usr/local/bin/ab-av1; fi',
    ]
    if include_expensive_tools:
        lines.extend([
            'FFMPEG_FILTERS=""',
            'FFMPEG_ENCODERS=""',
            'FFMPEG_HWACCELS=""',
            'if [ -n "$FFMPEG_BIN" ]; then FFMPEG_FILTERS="$("$FFMPEG_BIN" -hide_banner -filters 2>/dev/null || true)"; fi',
            'if [ -n "$FFMPEG_BIN" ]; then FFMPEG_ENCODERS="$("$FFMPEG_BIN" -hide_banner -encoders 2>/dev/null || true)"; fi',
            'if [ -n "$FFMPEG_BIN" ]; then FFMPEG_HWACCELS="$("$FFMPEG_BIN" -hide_banner -hwaccels 2>/dev/null || true)"; fi',
        ])
    lines.extend([
        'if xcode-select -p >/dev/null 2>&1; then printf "tool|xcode_clt|1\\n"; else printf "tool|xcode_clt|0\\n"; fi',
        'if [ -n "$BREW_BIN" ]; then printf "tool|brew|1\\n"; else printf "tool|brew|0\\n"; fi',
        'if [ -n "$FFMPEG_BIN" ]; then printf "tool|ffmpeg|1\\n"; else printf "tool|ffmpeg|0\\n"; fi',
        'if [ -n "$FFMPEG_BIN" ]; then printf "toolpath|ffmpeg|%s\\n" "$FFMPEG_BIN"; fi',
        'if [ -n "$FFMPEG_BIN" ]; then FFMPEG_REAL="$FFMPEG_BIN"; if command -v realpath >/dev/null 2>&1; then FFMPEG_REAL="$(realpath "$FFMPEG_BIN" 2>/dev/null || printf "%s" "$FFMPEG_BIN")"; fi; if FFMPEG_SIGNATURE="$(stat -f "%N:%z:%m" "$FFMPEG_REAL" 2>/dev/null)"; then :; else FFMPEG_SIGNATURE="$(stat -c "%N:%s:%Y" "$FFMPEG_REAL" 2>/dev/null || true)"; fi; printf "toolmeta|ffmpeg_signature|%s\\n" "$FFMPEG_SIGNATURE"; fi',
    ])
    if include_expensive_tools:
        lines.extend([
            'if printf "%s\\n" "$FFMPEG_HWACCELS" | grep -qi "videotoolbox"; then printf "tool|ffmpeg_videotoolbox|1\\n"; else printf "tool|ffmpeg_videotoolbox|0\\n"; fi',
            'if printf "%s\\n" "$FFMPEG_FILTERS" | grep -qi "libvmaf"; then printf "tool|ffmpeg_libvmaf|1\\n"; else printf "tool|ffmpeg_libvmaf|0\\n"; fi',
            'if printf "%s\\n" "$FFMPEG_FILTERS" | grep -qi "xpsnr"; then printf "tool|ffmpeg_xpsnr|1\\n"; else printf "tool|ffmpeg_xpsnr|0\\n"; fi',
            'if printf "%s\\n" "$FFMPEG_ENCODERS" | grep -qi "libsvtav1"; then printf "tool|ffmpeg_libsvtav1|1\\n"; else printf "tool|ffmpeg_libsvtav1|0\\n"; fi',
        ])
    lines.extend([
        'if [ -n "$AB_AV1_BIN" ]; then printf "tool|ab_av1|1\\n"; else printf "tool|ab_av1|0\\n"; fi',
        'printf "meta|platform|%s\\n" "$PLATFORM_NAME"',
        'TIMEZONE_NAME=""',
        'if [ -L /etc/localtime ]; then TIMEZONE_PATH="$(readlink /etc/localtime 2>/dev/null || true)"; case "$TIMEZONE_PATH" in */zoneinfo.default/*) TIMEZONE_NAME="${TIMEZONE_PATH#*/zoneinfo.default/}" ;; */zoneinfo/*) TIMEZONE_NAME="${TIMEZONE_PATH#*/zoneinfo/}" ;; esac; fi',
        'if [ -z "$TIMEZONE_NAME" ] && [ -r /etc/timezone ]; then TIMEZONE_NAME="$(head -n 1 /etc/timezone 2>/dev/null | tr -d "[:space:]" || true)"; fi',
        'if [ -z "$TIMEZONE_NAME" ] && command -v timedatectl >/dev/null 2>&1; then TIMEZONE_NAME="$(timedatectl show --property=Timezone --value 2>/dev/null || true)"; fi',
        'printf "time|schedule_timezone|%s\\n" "$TIMEZONE_NAME"',
        'printf "time|utc_offset|%s\\n" "$(date +%z)"',
    ])
    if mount_paths:
        lines.append('MOUNT_OUTPUT="$(/sbin/mount 2>/dev/null || true)"')
        for mount_path in mount_paths:
            lines.extend([
                f"mount_path={shlex.quote(mount_path)}",
                f"mount_output_path={shlex.quote(mount_output_field(mount_path))}",
                'if printf "%s\\n" "$MOUNT_OUTPUT" | /usr/bin/grep -F -- " on $mount_path (" >/dev/null 2>&1 || printf "%s\\n" "$MOUNT_OUTPUT" | /usr/bin/grep -F -- " on $mount_output_path (" >/dev/null 2>&1; then',
                '  printf "mount|%s|1\\n" "$mount_path"',
                'else',
                '  printf "mount|%s|0\\n" "$mount_path"',
                'fi',
            ])
    if paths:
        lines.extend([
            f"for path in {quoted_paths}; do",
            '  exists=0',
            '  readable=0',
            '  writable=0',
            '  should_probe_write=0',
            f"  for write_path in {quoted_writable_paths}; do",
            '    if [ "$path" = "$write_path" ]; then should_probe_write=1; fi',
            '  done',
            '  if [ -d "$path" ]; then',
            '    exists=1',
            '    if ls "$path" >/dev/null 2>&1; then readable=1; fi',
            '    if [ "$should_probe_write" = "1" ]; then',
            '      probe="$(mktemp "$path/.mediaforce-write-test.XXXXXX" 2>/dev/null || true)"',
            '      if [ -n "$probe" ]; then writable=1; rm -f "$probe" >/dev/null 2>&1 || true; fi',
            '    fi',
            '  elif [ -e "$path" ] && [ -r "$path" ]; then',
            '    exists=1',
            '    readable=1',
            '  elif [ -e "$path" ]; then',
            '    exists=1',
            '  fi',
            '  if [ "$exists" = "1" ]; then printf "path|%s|1\\n" "$path"; else printf "path|%s|0\\n" "$path"; fi',
            '  printf "pathexists|%s|%s\\n" "$path" "$exists"',
            '  printf "pathread|%s|%s\\n" "$path" "$readable"',
            '  printf "pathwrite|%s|%s\\n" "$path" "$writable"',
            "done",
        ])
    if repo_path:
        lines.append(
            f'if [ -e {shlex.quote(repo_path)} ]; then printf "repo|exists|1\\n"; else printf "repo|exists|0\\n"; fi'
        )
    else:
        lines.append('printf "repo|exists|1\\n"')
    return "\n".join(lines)


def _parse_remote_status_output(stdout: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "paths": {},
        "tools": {},
        "tool_paths": {},
        "tool_meta": {},
        "path_access": {},
        "mounts": {},
        "repo_path_exists": True,
        "utc_offset": None,
        "platform": "unknown",
    }
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            raise ValueError(f"Unexpected remote status line: {line}")
        kind, key, value = parts
        if kind == "path":
            payload["paths"][key] = value == "1"
        elif kind == "pathexists":
            access = payload["path_access"].setdefault(key, {})
            access["exists"] = value == "1"
        elif kind == "pathread":
            access = payload["path_access"].setdefault(key, {})
            access["read"] = value == "1"
        elif kind == "pathwrite":
            access = payload["path_access"].setdefault(key, {})
            access["write"] = value == "1"
        elif kind == "mount":
            payload["mounts"][key] = value == "1"
        elif kind == "tool":
            payload["tools"][key] = value == "1"
        elif kind == "toolpath":
            payload["tool_paths"][key] = value
        elif kind == "toolmeta":
            payload["tool_meta"][key] = value
        elif kind == "repo":
            payload["repo_path_exists"] = value == "1"
        elif kind == "time":
            payload[key] = value
        elif kind == "meta":
            payload[key] = value
        else:
            raise ValueError(f"Unknown remote status kind: {kind}")
    return payload


def _ssh_access_must_be_fixed_first(status: HostStatus) -> bool:
    if status.available or status.missing_paths:
        return False
    prep_fixable_issues = {
        FFMPEG_MISSING_ISSUE,
        "Xcode Command Line Tools are not installed on the remote Mac.",
        "Homebrew is not installed on the remote Mac.",
        AB_AV1_MISSING_ISSUE,
        SAMPLE_METRIC_MISSING_ISSUE,
        SAMPLE_AV1_ENCODER_MISSING_ISSUE,
    }
    if status.repo_path and f"Repo path is missing: {status.repo_path}" in status.issues:
        remaining_issues = [
            issue for issue in status.issues if issue != f"Repo path is missing: {status.repo_path}"
        ]
    else:
        remaining_issues = list(status.issues)
    return any(issue not in prep_fixable_issues for issue in remaining_issues)


__all__ = [
    "_classify_ssh_failure",
    "_command_output",
    "_command_succeeds",
    "_default_public_key_path",
    "_find_local_tool",
    "_host_capability_issues",
    "_host_message",
    "_host_setup_supported",
    "_local_platform_name",
    "_local_schedule_timezone",
    "_local_tool_status_snapshot",
    "_local_utc_offset_minutes",
    "_needs_initial_ssh_key_install",
    "_parse_remote_status_output",
    "_remote_setup_needs_password",
    "_remote_status_script",
    "_private_key_path_for_public_key",
    "_should_retry_remote_status_exception",
    "_should_retry_remote_status_failure",
    "_ssh_access_must_be_fixed_first",
    "_status_from_paths",
    "_status_platform",
]
