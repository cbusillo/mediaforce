from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mediaforce.config import HarnessConfig, load_runtime_settings, save_runtime_settings

DEFAULT_HOST_CAPABILITIES = ("encode_queue",)
DEFAULT_WAKE_WAIT_SECONDS = 60
REMOTE_SHELL_PATH = "/opt/homebrew/opt/ffmpeg-full/bin:/usr/local/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
FFMPEG_MISSING_ISSUE = "ffmpeg is not installed on the remote PATH."
AB_AV1_MISSING_ISSUE = "ab-av1 is not installed on the remote PATH."
SAMPLE_METRIC_MISSING_ISSUE = "ffmpeg is missing both libvmaf and xpsnr support required for sampled calibration."
SAMPLE_AV1_ENCODER_MISSING_ISSUE = "ffmpeg is missing libsvtav1 support required for sampled calibration."


@dataclass(slots=True)
class HostStatus:
    key: str
    label: str
    mode: str
    priority: int
    capabilities: list[str]
    available: bool
    message: str
    missing_paths: list[str]
    repo_path: str | None = None
    utc_offset_minutes: int | None = None
    issues: list[str] = field(default_factory=list)
    detail: str | None = None
    setup_supported: bool = False
    setup_requires_password: bool = False
    trust_reset_supported: bool = False


@dataclass(slots=True)
class HostSetupResult:
    ok: bool
    message: str
    detail: str | None = None
    performed_steps: list[str] = field(default_factory=list)
    requires_password: bool = False


def collect_host_statuses(config: HarnessConfig) -> list[HostStatus]:
    return [_remote_host_status(config, host) for host in config.remote_hosts]


def ssh_target_for_host(host: dict[str, object]) -> str:
    return str(host.get("host") or host.get("key") or "").strip()


def run_remote_command(
    host: dict[str, object],
    command: list[str],
    *,
    timeout: int,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    ssh_host = ssh_target_for_host(host)
    if not ssh_host:
        raise RuntimeError("Remote host is missing an SSH target.")
    if not command:
        raise RuntimeError("Remote command cannot be empty.")

    normalized_host = dict(host)
    normalized_host["host"] = ssh_host
    remote_command = [Path(str(command[0])).name, *[str(arg) for arg in command[1:]]]
    script = "\n".join(
        [
            remote_shell_path_export_line(),
            shlex.join(remote_command),
        ]
    )
    return _run_remote_ssh(
        normalized_host,
        "sh",
        "-lc",
        script,
        input_text=input_text,
        timeout=timeout,
    )


def copy_remote_file_to_local(
    host: dict[str, object],
    remote_path: Path,
    local_path: Path,
    *,
    timeout: int,
) -> None:
    ssh_host = ssh_target_for_host(host)
    if not ssh_host:
        raise RuntimeError("Remote host is missing an SSH target.")

    normalized_host = dict(host)
    normalized_host["host"] = ssh_host
    _ensure_remote_awake_for_ssh(normalized_host)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "scp",
            "-q",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            f"{ssh_host}:{remote_path}",
            str(local_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "scp download failed"
        raise RuntimeError(detail)


def _remote_host_status(config: HarnessConfig, host: dict[str, object]) -> HostStatus:
    label = str(host.get("label") or host.get("host") or "remote")
    ssh_host = str(host.get("host") or "")
    repo_path = str(host.get("repo_path") or "") or None
    if not ssh_host:
        return HostStatus(
            key=label,
            label=label,
            mode="ssh",
            priority=_host_priority(host),
            capabilities=_host_capabilities(host),
            available=False,
            message="Missing SSH host configuration",
            missing_paths=[],
            repo_path=repo_path,
            issues=["Add an SSH host to enable remote checks."],
            setup_supported=False,
        )

    paths = [str(path) for path in config.source_root_map.values()] + [str(config.staging_root)]
    repo_check_path = repo_path or ""
    script = _remote_status_script(paths=paths, repo_path=repo_check_path)
    try:
        result = _run_remote_ssh(
            host,
            "sh",
            "-s",
            input_text=script,
            timeout=8,
            wake_before_connect=False,
        )
    except Exception as exc:
        return HostStatus(
            key=ssh_host,
            label=label,
            mode="ssh",
            priority=_host_priority(host),
            capabilities=_host_capabilities(host),
            available=False,
            message="SSH unavailable",
            missing_paths=[],
            repo_path=repo_path,
            issues=["The harness could not reach this host over SSH."],
            detail=str(exc),
            setup_supported=False,
        )

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "SSH host unavailable"
        classification = _classify_ssh_failure(detail)
        return HostStatus(
            key=ssh_host,
            label=label,
            mode="ssh",
            priority=_host_priority(host),
            capabilities=_host_capabilities(host),
            available=False,
            message=classification["message"],
            missing_paths=[],
            repo_path=repo_path,
            issues=classification["issues"],
            detail=detail if classification.get("show_detail", True) else None,
            setup_supported=classification["setup_supported"],
            setup_requires_password=classification["setup_requires_password"],
            trust_reset_supported=classification.get("trust_reset_supported", False),
        )

    try:
        payload = _parse_remote_status_output(result.stdout)
    except ValueError:
        return HostStatus(
            key=ssh_host,
            label=label,
            mode="ssh",
            priority=_host_priority(host),
            capabilities=_host_capabilities(host),
            available=False,
            message="Remote check returned unreadable output",
            missing_paths=[],
            repo_path=repo_path,
            issues=["The remote host responded, but the capability payload was invalid."],
            detail=result.stdout.strip() or None,
            setup_supported=True,
        )

    mounted_paths = {
        str(path_text): bool(exists)
        for path_text, exists in dict(payload.get("paths") or {}).items()
    }
    _learn_remote_wake_mac(config, host, ssh_host)
    capability_issues: list[str] = []
    tools = dict(payload.get("tools") or {})
    if not bool(tools.get("xcode_clt")):
        capability_issues.append("Xcode Command Line Tools are not installed on the remote Mac.")
    if not bool(tools.get("brew")):
        capability_issues.append("Homebrew is not installed on the remote Mac.")
    if not bool(tools.get("ffmpeg")):
        capability_issues.append(FFMPEG_MISSING_ISSUE)
    if _host_supports_capability(host, "sample_calibration"):
        if not bool(tools.get("ab_av1")):
            capability_issues.append(AB_AV1_MISSING_ISSUE)
        if bool(tools.get("ffmpeg")) and not bool(tools.get("ffmpeg_libsvtav1")):
            capability_issues.append(SAMPLE_AV1_ENCODER_MISSING_ISSUE)
        if bool(tools.get("ffmpeg")) and not (
            bool(tools.get("ffmpeg_libvmaf")) or bool(tools.get("ffmpeg_xpsnr"))
        ):
            capability_issues.append(SAMPLE_METRIC_MISSING_ISSUE)
    if repo_path and not bool(payload.get("repo_path_exists", False)):
        capability_issues.append(f"Repo path is missing: {repo_path}")
    return _status_from_paths(
        key=ssh_host,
        label=label,
        mode="ssh",
        priority=_host_priority(host),
        capabilities=_host_capabilities(host),
        mounted_paths=mounted_paths,
        repo_path=repo_path,
        utc_offset_minutes=_parse_utc_offset_minutes(payload.get("utc_offset")),
        issues=capability_issues,
        setup_supported=True,
        setup_requires_password=_remote_setup_needs_password(capability_issues),
    )


def prepare_remote_host(config: HarnessConfig, host_key: str) -> HostSetupResult:
    return prepare_remote_host_with_password(config, host_key, password=None)


def reset_remote_host_trust(config: HarnessConfig, host_key: str) -> HostSetupResult:
    host = _find_remote_host(config, host_key)
    if host is None:
        return HostSetupResult(ok=False, message="Remote host is no longer configured")

    ssh_host = str(host.get("host") or "").strip()
    lookup_host = _ssh_lookup_host(ssh_host)
    if not lookup_host:
        return HostSetupResult(ok=False, message="Remote host is missing an SSH target")

    result = subprocess.run(
        ["ssh-keygen", "-R", lookup_host],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return HostSetupResult(
            ok=False,
            message="Could not clear the saved SSH host key.",
            detail=result.stderr.strip() or result.stdout.strip() or None,
        )

    return HostSetupResult(
        ok=True,
        message=f"Cleared the saved SSH trust entry for {lookup_host}. Retry the host check now.",
    )


def prepare_remote_host_with_password(
    config: HarnessConfig,
    host_key: str,
    *,
    password: str | None,
) -> HostSetupResult:
    host = _find_remote_host(config, host_key)
    if host is None:
        return HostSetupResult(ok=False, message="Remote host is no longer configured")

    ssh_host = str(host.get("host") or "").strip()
    if not ssh_host:
        return HostSetupResult(ok=False, message="Remote host is missing an SSH target")

    prep_steps: list[str] = []
    for _ in range(6):
        status = _remote_host_status(config, host)
        if status.available:
            return HostSetupResult(
                ok=True,
                message=f"{status.label} is mounted and ready.",
                performed_steps=prep_steps,
            )
        if _needs_initial_ssh_key_install(status):
            if not password:
                return HostSetupResult(
                    ok=False,
                    message="The remote account password is required the first time so the harness can install this Mac's SSH key.",
                    requires_password=True,
                    performed_steps=prep_steps,
                )
            key_install = _install_local_ssh_key(host, password)
            prep_steps.extend(key_install.performed_steps)
            key_install.performed_steps = prep_steps
            if not key_install.ok:
                return key_install
            continue
        if _ssh_access_must_be_fixed_first(status):
            return HostSetupResult(
                ok=False,
                message=status.message,
                detail=status.detail or "\n".join(status.issues) or None,
                requires_password=status.setup_requires_password,
                performed_steps=prep_steps,
            )
        if "Xcode Command Line Tools are not installed on the remote Mac." in status.issues:
            xcode_request = _request_remote_xcode_install(host)
            prep_steps.extend(xcode_request.performed_steps)
            xcode_request.performed_steps = prep_steps
            if not xcode_request.ok:
                return xcode_request
            continue
        if status.setup_requires_password:
            if not password:
                return HostSetupResult(
                    ok=False,
                    message="The remote account password is required to finish Mac bootstrap steps like Homebrew installation.",
                    detail="Enter the remote account password and retry host preparation.",
                    requires_password=True,
                    performed_steps=prep_steps,
                )
            bootstrap = _bootstrap_remote_macos(host, password, issues=status.issues)
            prep_steps.extend(bootstrap.performed_steps)
            bootstrap.performed_steps = prep_steps
            if not bootstrap.ok:
                return bootstrap
            continue
        if status.missing_paths:
            return _finish_remote_host_prepare(config, host, prep_steps)
        return _finish_remote_host_prepare(config, host, prep_steps)

    return HostSetupResult(
        ok=False,
        message="Remote preparation stalled before the host became ready.",
        detail="Retry host preparation. If this keeps happening, inspect the remote host status output.",
        performed_steps=prep_steps,
    )


def _finish_remote_host_prepare(
    config: HarnessConfig,
    host: dict[str, Any],
    prep_steps: list[str],
) -> HostSetupResult:
    repo_path = str(host.get("repo_path") or "").strip()
    staging_root = str(config.staging_root)
    archive_root = str(config.archive_root)
    prep_steps.extend(
        [
            "Ensured the transcode and archive directories exist.",
        ]
    )
    remote_commands = [
        remote_shell_path_export_line(),
        'BREW_BIN="$(command -v brew || true)"',
        'if [ -z "$BREW_BIN" ] && [ -x /opt/homebrew/bin/brew ]; then BREW_BIN=/opt/homebrew/bin/brew; fi',
        'if [ -z "$BREW_BIN" ] && [ -x /usr/local/bin/brew ]; then BREW_BIN=/usr/local/bin/brew; fi',
        'FFMPEG_BIN="$(command -v ffmpeg || true)"',
        'AB_AV1_BIN="$(command -v ab-av1 || true)"',
        'if [ -z "$AB_AV1_BIN" ] && [ -x /opt/homebrew/bin/ab-av1 ]; then AB_AV1_BIN=/opt/homebrew/bin/ab-av1; fi',
        'if [ -z "$AB_AV1_BIN" ] && [ -x /usr/local/bin/ab-av1 ]; then AB_AV1_BIN=/usr/local/bin/ab-av1; fi',
        f"mkdir -p {shlex.quote(staging_root)} >/dev/null 2>&1",
        f"mkdir -p {shlex.quote(archive_root)} >/dev/null 2>&1",
    ]
    if repo_path:
        remote_commands.append(f"mkdir -p {shlex.quote(repo_path)} >/dev/null 2>&1")
        prep_steps.append(f"Ensured the repo path exists at {repo_path}.")
    remote_commands.extend(
        _remote_ffmpeg_install_commands(sample_calibration=_host_supports_capability(host, "sample_calibration"))
    )
    prep_steps.append(
        "Installed ffmpeg-full with Homebrew for sampled calibration hosts when required."
        if _host_supports_capability(host, "sample_calibration")
        else "Installed ffmpeg with Homebrew if it was missing."
    )
    if _host_supports_capability(host, "sample_calibration"):
        remote_commands.extend(
            [
                'if [ -z "$AB_AV1_BIN" ]; then',
                '  if [ -n "$BREW_BIN" ]; then',
                '    HOMEBREW_NO_AUTO_UPDATE=1 "$BREW_BIN" install ab-av1 >/tmp/mediaforce-brew.log 2>&1 || { cat /tmp/mediaforce-brew.log; exit 23; }',
                '  else',
                '    echo "ab-av1 is missing and Homebrew is unavailable for automatic install." >&2',
                '    exit 24',
                '  fi',
                'fi',
            ]
        )
        prep_steps.append("Installed ab-av1 with Homebrew for sampled calibration if it was missing.")

    try:
        result = _run_remote_ssh(
            host,
            "sh",
            "-lc",
            "\n".join(remote_commands),
            timeout=900,
        )
    except Exception as exc:
        return HostSetupResult(ok=False, message="Remote preparation failed", detail=str(exc), performed_steps=prep_steps)

    if result.returncode != 0:
        return HostSetupResult(
            ok=False,
            message="Remote preparation failed",
            detail=result.stderr.strip() or result.stdout.strip() or None,
            performed_steps=prep_steps,
        )

    refreshed = _remote_host_status(config, host)
    if refreshed.available:
        return HostSetupResult(
            ok=True,
            message=f"{refreshed.label} is mounted and ready.",
            performed_steps=prep_steps,
        )
    if refreshed.missing_paths:
        return HostSetupResult(
            ok=False,
            message="Remote preparation finished, but shared media paths are still missing.",
            detail="\n".join(refreshed.missing_paths),
            performed_steps=prep_steps,
        )
    return HostSetupResult(
        ok=False,
        message="Remote preparation finished, but the host still is not ready.",
        detail=refreshed.detail or "\n".join(refreshed.issues) or None,
        performed_steps=prep_steps,
    )


def _status_from_paths(
    *,
    key: str,
    label: str,
    mode: str,
    priority: int,
    capabilities: list[str],
    mounted_paths: dict[str, bool],
    repo_path: str | None,
    utc_offset_minutes: int | None = None,
    issues: list[str] | None = None,
    setup_supported: bool = False,
    setup_requires_password: bool = False,
) -> HostStatus:
    missing_paths = [path for path, mounted in mounted_paths.items() if not mounted]
    issue_list = list(issues or [])
    available = len(mounted_paths) > 0 and not missing_paths and not issue_list
    return HostStatus(
        key=key,
        label=label,
        mode=mode,
        priority=priority,
        capabilities=capabilities,
        available=available,
        message=_host_message(available=available, missing_paths=missing_paths, issues=issue_list),
        missing_paths=missing_paths,
        repo_path=repo_path,
        utc_offset_minutes=utc_offset_minutes,
        issues=issue_list,
        setup_supported=setup_supported,
        setup_requires_password=setup_requires_password,
    )


def _host_priority(host: dict[str, object]) -> int:
    try:
        return int(str(host.get("priority") or "0"))
    except (TypeError, ValueError):
        return 0


def _host_capabilities(host: dict[str, object]) -> list[str]:
    raw = host.get("capabilities")
    if isinstance(raw, str):
        values = [value.strip() for value in raw.split(",") if value.strip()]
    elif isinstance(raw, list):
        values = [str(value).strip() for value in raw if str(value).strip()]
    else:
        values = []
    normalized = sorted({value.lower().replace(" ", "_") for value in values})
    return normalized or list(DEFAULT_HOST_CAPABILITIES)


def _host_supports_capability(host: dict[str, object], capability: str) -> bool:
    return capability.lower() in _host_capabilities(host)


def remote_shell_path_export_line() -> str:
    return f'export PATH="{REMOTE_SHELL_PATH}:$PATH"'


def _remote_ffmpeg_install_commands(*, sample_calibration: bool) -> list[str]:
    if sample_calibration:
        return [
            'if [ -n "$BREW_BIN" ]; then',
            '  HOMEBREW_NO_AUTO_UPDATE=1 "$BREW_BIN" install ffmpeg-full >/tmp/mediaforce-brew.log 2>&1 || { cat /tmp/mediaforce-brew.log; exit 21; }',
            'else',
            '  echo "ffmpeg-full is required for sampled calibration and Homebrew is unavailable for automatic install." >&2',
            '  exit 22',
            'fi',
        ]
    return [
        'if [ -z "$FFMPEG_BIN" ]; then',
        '  if [ -n "$BREW_BIN" ]; then',
        '    HOMEBREW_NO_AUTO_UPDATE=1 "$BREW_BIN" install ffmpeg >/tmp/mediaforce-brew.log 2>&1 || { cat /tmp/mediaforce-brew.log; exit 21; }',
        '  else',
        '    echo "ffmpeg is missing and Homebrew is unavailable for automatic install." >&2',
        '    exit 22',
        '  fi',
        'fi',
    ]


def _parse_utc_offset_minutes(value: object) -> int | None:
    text = str(value or "").strip()
    if len(text) != 5 or text[0] not in {"+", "-"} or not text[1:].isdigit():
        return None
    sign = 1 if text[0] == "+" else -1
    hours = int(text[1:3])
    minutes = int(text[3:5])
    return sign * ((hours * 60) + minutes)


def _host_message(*, available: bool, missing_paths: list[str], issues: list[str]) -> str:
    if available:
        return "Mounted and ready"
    if any(issue == "Xcode Command Line Tools are not installed on the remote Mac." for issue in issues):
        return "Finish Xcode tools install"
    if any(issue == "Homebrew is not installed on the remote Mac." for issue in issues):
        return "Install Homebrew first"
    if issues:
        return "Needs remote setup"
    return "Missing required paths"


def _remote_setup_needs_password(issues: list[str]) -> bool:
    password_required_issues = {
        "Homebrew is not installed on the remote Mac.",
    }
    return any(issue in password_required_issues for issue in issues)


def _needs_initial_ssh_key_install(status: HostStatus) -> bool:
    return status.message == "SSH access setup required"


def _run_remote_ssh(
    host: dict[str, object],
    *remote_args: str,
    input_text: str | None = None,
    timeout: int,
    identity_file: Path | None = None,
    batch_mode: bool = True,
    wake_before_connect: bool = True,
) -> subprocess.CompletedProcess[str]:
    if wake_before_connect:
        _ensure_remote_awake_for_ssh(host)
    ssh_host = str(host.get("host") or "").strip()
    cmd = ["ssh"]
    if identity_file is not None:
        cmd.extend(["-i", str(identity_file), "-o", "IdentitiesOnly=yes"])
    cmd.extend(["-o", "StrictHostKeyChecking=accept-new"])
    if batch_mode:
        cmd.extend(["-o", "BatchMode=yes"])
    cmd.extend(["-o", "ConnectTimeout=5", ssh_host, *remote_args])
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
    )


def _ensure_remote_awake_for_ssh(
    host: dict[str, object], *, wake_wait_seconds: int = DEFAULT_WAKE_WAIT_SECONDS
) -> None:
    mac_address = str(host.get("wake_mac") or host.get("wol_mac") or "").strip()
    if not mac_address:
        return
    ssh_host = str(host.get("host") or "").strip()
    network_host = _resolved_ssh_network_host(ssh_host)
    if not network_host:
        return
    ip_address = _resolve_host_to_ip(network_host)
    if not ip_address:
        return
    if _tcp_port_is_open(ip_address, 22):
        return
    _wake_remote_host_if_configured(host)
    deadline = time.monotonic() + wake_wait_seconds
    while time.monotonic() < deadline:
        if _tcp_port_is_open(ip_address, 22):
            return
        time.sleep(2)


def _wake_remote_host_if_configured(host: dict[str, Any]) -> HostSetupResult:
    mac_address = str(host.get("wake_mac") or host.get("wol_mac") or "").strip()
    if not mac_address:
        return HostSetupResult(ok=True, message="No Wake-on-LAN step was needed.")

    normalized = _normalize_mac_address(mac_address)
    if normalized is None:
        return HostSetupResult(
            ok=False,
            message="Wake-on-LAN is configured with an invalid MAC address.",
            detail="Use a 6-byte MAC such as aa:bb:cc:dd:ee:ff.",
        )

    packet = bytes.fromhex(normalized) * 16
    packet = b"\xff" * 6 + packet
    destinations = _wake_broadcast_destinations(host)
    sent_to: list[tuple[str, int]] = []
    last_error: OSError | None = None
    for address, port in destinations:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(packet, (address, port))
            sent_to.append((address, port))
        except OSError as exc:
            last_error = exc
            continue
    if not sent_to:
        return HostSetupResult(
            ok=False,
            message="Sending the Wake-on-LAN packet failed.",
            detail=str(last_error) if last_error is not None else "No valid broadcast destination was available.",
        )

    time.sleep(8)
    return HostSetupResult(
        ok=True,
        message="Wake-on-LAN packet sent.",
        performed_steps=[f"Sent a Wake-on-LAN packet to {mac_address} via {', '.join(f'{addr}:{port}' for addr, port in sent_to)}."],
    )


def _wake_broadcast_destinations(host: dict[str, Any]) -> list[tuple[str, int]]:
    destinations: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for address in _local_broadcast_addresses(host):
        for port in (9, 7):
            destination = (address, port)
            if destination in seen:
                continue
            destinations.append(destination)
            seen.add(destination)
    for port in (9, 7):
        destination = ("255.255.255.255", port)
        if destination in seen:
            continue
        destinations.append(destination)
        seen.add(destination)
    return destinations


def _local_broadcast_addresses(host: dict[str, Any]) -> list[str]:
    addresses: list[str] = []
    resolved_host = _resolved_ssh_network_host(str(host.get("host") or "").strip())
    if resolved_host:
        ip_address = _resolve_host_to_ip(resolved_host)
        if ip_address:
            routed_interface = _interface_for_ip(ip_address)
            if routed_interface:
                addresses.extend(_broadcast_addresses_for_interface(routed_interface))
    try:
        result = subprocess.run(["ifconfig"], check=False, capture_output=True, text=True, timeout=5)
    except Exception:
        return list(dict.fromkeys(addresses))
    current_interface = ""
    for raw_line in result.stdout.splitlines():
        line = raw_line.rstrip()
        if line and not line.startswith("\t") and ":" in line:
            current_interface = line.split(":", 1)[0]
            continue
        if not current_interface:
            continue
        if "broadcast" not in line or "inet " not in line:
            continue
        parts = line.split()
        for index, part in enumerate(parts[:-1]):
            if part == "broadcast":
                addresses.append(parts[index + 1])
                break
    return list(dict.fromkeys(addresses))


def _interface_for_ip(ip_address: str) -> str | None:
    try:
        result = subprocess.run(["route", "-n", "get", ip_address], check=False, capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("interface:"):
            continue
        return stripped.split(":", 1)[1].strip() or None
    return None


def _broadcast_addresses_for_interface(interface: str) -> list[str]:
    try:
        result = subprocess.run(["ifconfig", interface], check=False, capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    if result.returncode != 0:
        return []
    addresses: list[str] = []
    for line in result.stdout.splitlines():
        if "broadcast" not in line or "inet " not in line:
            continue
        parts = line.split()
        for index, part in enumerate(parts[:-1]):
            if part == "broadcast":
                addresses.append(parts[index + 1])
                break
    return addresses


def _tcp_port_is_open(ip_address: str, port: int) -> bool:
    try:
        with socket.create_connection((ip_address, port), timeout=1.5):
            return True
    except OSError:
        return False


def _normalize_mac_address(value: str) -> str | None:
    cleaned = "".join(ch for ch in value if ch.isalnum())
    if len(cleaned) != 12 or any(ch not in "0123456789abcdefABCDEF" for ch in cleaned):
        return None
    return cleaned.lower()


def _learn_remote_wake_mac(config: HarnessConfig, host: dict[str, object], ssh_host: str) -> None:
    if str(host.get("wake_mac") or host.get("wol_mac") or "").strip():
        return
    network_host = _resolved_ssh_network_host(ssh_host)
    if not network_host:
        return
    ip_address = _resolve_host_to_ip(network_host)
    if not ip_address:
        return
    mac_address = _mac_from_arp(ip_address)
    if not mac_address:
        return
    _persist_remote_wake_mac(config, host, mac_address)


def _resolved_ssh_network_host(ssh_host: str) -> str | None:
    fallback = _ssh_lookup_host(ssh_host)
    try:
        result = subprocess.run(
            ["ssh", "-G", ssh_host],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return fallback or None
    if result.returncode != 0:
        return fallback or None
    for line in result.stdout.splitlines():
        if not line.startswith("hostname "):
            continue
        hostname = line.split(None, 1)[1].strip()
        return hostname or fallback or None
    return fallback or None


def _resolve_host_to_ip(hostname: str) -> str | None:
    try:
        return socket.gethostbyname(hostname)
    except OSError:
        return hostname if _looks_like_ipv4_address(hostname) else None


def _looks_like_ipv4_address(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    return all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


def _mac_from_arp(ip_address: str) -> str | None:
    try:
        result = subprocess.run(
            ["arp", "-n", ip_address],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    output = f"{result.stdout}\n{result.stderr}"
    for token in output.replace("(", " ").replace(")", " ").split():
        normalized = _normalize_mac_address(token)
        if normalized is not None:
            return ":".join(normalized[index : index + 2] for index in range(0, 12, 2))
    try:
        subprocess.run(
            ["ping", "-c", "1", ip_address],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        result = subprocess.run(
            ["arp", "-n", ip_address],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    output = f"{result.stdout}\n{result.stderr}"
    for token in output.replace("(", " ").replace(")", " ").split():
        normalized = _normalize_mac_address(token)
        if normalized is not None:
            return ":".join(normalized[index : index + 2] for index in range(0, 12, 2))
    return None


def _persist_remote_wake_mac(config: HarnessConfig, host: dict[str, object], mac_address: str) -> None:
    host["wake_mac"] = mac_address
    try:
        runtime_settings = load_runtime_settings(config.paths.runtime_settings_path)
        existing_runtime_hosts = runtime_settings.get("remote_hosts")
        source_hosts = existing_runtime_hosts if isinstance(existing_runtime_hosts, list) else config.remote_hosts
        updated_hosts: list[dict[str, Any]] = []
        target_host = str(host.get("host") or "")
        target_label = str(host.get("label") or "")
        for entry in source_hosts:
            if not isinstance(entry, dict):
                continue
            updated_entry = dict(entry)
            if str(updated_entry.get("host") or "") == target_host and str(updated_entry.get("label") or "") == target_label:
                updated_entry["wake_mac"] = mac_address
            updated_hosts.append(updated_entry)
        runtime_settings["remote_hosts"] = updated_hosts
        save_runtime_settings(config.paths.runtime_settings_path, runtime_settings)
    except Exception:
        return


def _find_remote_host(config: HarnessConfig, host_key: str) -> dict[str, Any] | None:
    for host in config.remote_hosts:
        ssh_host = str(host.get("host") or "")
        label = str(host.get("label") or ssh_host or "remote")
        if host_key in {ssh_host, label}:
            return host
    return None


def host_status_targets_current_machine(status: HostStatus) -> bool:
    return _host_lookup_targets_current_machine(_ssh_lookup_host(status.key))


def host_targets_current_machine(host: dict[str, object]) -> bool:
    return _host_lookup_targets_current_machine(_ssh_lookup_host(str(host.get("host") or "")))


def _host_lookup_targets_current_machine(lookup_host: str) -> bool:
    normalized = lookup_host.strip().lower()
    if not normalized:
        return False
    aliases = {
        "localhost",
        "127.0.0.1",
        "::1",
        socket.gethostname().strip().lower(),
        socket.getfqdn().strip().lower(),
    }
    return normalized in aliases


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
                "Turn on Remote Login on the target Mac, then enter that account password once so the harness can install this Mac's SSH key."
            ],
            "setup_supported": True,
            "setup_requires_password": True,
            "trust_reset_supported": False,
            "show_detail": True,
        }
    if "connection refused" in lowered or "operation timed out" in lowered or "no route to host" in lowered:
        return {
            "message": "Turn on SSH first",
            "issues": [
                "Enable Remote Login on the target machine first. Once SSH answers, the harness can finish setup automatically."
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
            "issues": ["Install python3 on the remote host before using it for harness checks."],
            "setup_supported": True,
            "setup_requires_password": False,
            "trust_reset_supported": False,
            "show_detail": True,
        }
    return {
        "message": "SSH unavailable",
        "issues": ["The harness could not complete the remote capability check."],
        "setup_supported": True,
        "setup_requires_password": False,
        "trust_reset_supported": False,
        "show_detail": True,
    }


def _install_local_ssh_key(host: dict[str, Any], password: str) -> HostSetupResult:
    ssh_host = str(host.get("host") or "").strip()
    _ensure_remote_awake_for_ssh(host)
    public_key = _default_public_key_path()
    if public_key is None:
        return HostSetupResult(
            ok=False,
            message="No local SSH public key was found for remote setup.",
            detail="Expected a public key such as ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub.",
        )
    private_key = _private_key_path_for_public_key(public_key)
    if private_key is None:
        return HostSetupResult(
            ok=False,
            message="The matching private key for remote setup was not found.",
            detail=f"Expected {public_key.with_suffix('')} next to {public_key.name}.",
        )

    public_key_text = public_key.read_text().strip()
    install_cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "PreferredAuthentications=password,keyboard-interactive",
        "-o",
        "PubkeyAuthentication=no",
        ssh_host,
        "sh",
        "-lc",
        " && ".join(
            [
                "mkdir -p ~/.ssh",
                "chmod 700 ~/.ssh",
                "touch ~/.ssh/authorized_keys",
                "chmod 600 ~/.ssh/authorized_keys",
                (
                    f"grep -qxF {shlex.quote(public_key_text)} ~/.ssh/authorized_keys "
                    f"|| printf '%s\\n' {shlex.quote(public_key_text)} >> ~/.ssh/authorized_keys"
                ),
            ]
        ),
    ]
    expect_script = "\n".join(
        [
            "set timeout 60",
            f"spawn {' '.join(shlex.quote(part) for part in install_cmd)}",
            "expect {",
            '  -re "(?i)password:" { send -- "$env(HARNESS_SSH_PASSWORD)\\r"; exp_continue }',
            '  eof { catch wait result; exit [lindex $result 3] }',
            "}",
        ]
    )
    env = {**os.environ, "HARNESS_SSH_PASSWORD": password}
    result = subprocess.run(
        ["expect", "-c", expect_script],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    if result.returncode != 0:
        return HostSetupResult(
            ok=False,
            message="Installing this Mac's SSH key failed.",
            detail=result.stderr.strip() or result.stdout.strip() or None,
            requires_password=True,
        )

    verify_result = _run_remote_ssh(host, "true", identity_file=private_key, timeout=15)
    if verify_result.returncode != 0:
        return HostSetupResult(
            ok=False,
            message="The SSH key install did not produce working passwordless access.",
            detail=(
                verify_result.stderr.strip()
                or verify_result.stdout.strip()
                or "Check the password you entered and confirm password authentication is allowed for first-time setup."
            ),
            requires_password=True,
        )

    return HostSetupResult(
        ok=True,
        message="Installed this Mac's SSH key on the remote host.",
        performed_steps=[f"Installed {public_key.name} for passwordless SSH access."],
    )


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
    candidate = public_key.with_suffix("")
    return candidate if candidate.exists() else None


def _bootstrap_remote_macos(host: dict[str, Any], password: str, *, issues: list[str]) -> HostSetupResult:
    ssh_host = str(host.get("host") or "").strip()
    public_key = _default_public_key_path()
    if public_key is None:
        return HostSetupResult(
            ok=False,
            message="No local SSH public key was found for remote bootstrap.",
            detail="Expected a public key such as ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub.",
        )
    private_key = _private_key_path_for_public_key(public_key)
    if private_key is None:
        return HostSetupResult(
            ok=False,
            message="The matching private key for remote bootstrap was not found.",
            detail=f"Expected {public_key.with_suffix('')} next to {public_key.name}.",
        )

    wants_xcode = "Xcode Command Line Tools are not installed on the remote Mac." in issues
    wants_brew = "Homebrew is not installed on the remote Mac." in issues
    if not wants_xcode and not wants_brew:
        return HostSetupResult(ok=True, message="No elevated bootstrap steps were needed.")

    if wants_xcode:
        return _request_remote_xcode_install(host)

    remote_lines = [
        f"export HARNESS_SUDO_PASSWORD={shlex.quote(password)}",
        'sudo_run() { printf "%s\\n" "$HARNESS_SUDO_PASSWORD" | sudo -S -p "" "$@"; }',
        'if ! sudo_run -v >/dev/null 2>&1; then',
        '  echo "HARNESS_BOOTSTRAP_ERROR|sudo_auth" >&2',
        '  exit 41',
        'fi',
        'ASKPASS_SCRIPT="$(mktemp /tmp/mediaforce-askpass.XXXXXX)"',
        'trap "rm -f \"$ASKPASS_SCRIPT\"" EXIT',
        "cat > \"$ASKPASS_SCRIPT\" <<'EOF'",
        '#!/bin/sh',
        'printf "%s\\n" "$HARNESS_SUDO_PASSWORD"',
        'EOF',
        'chmod 700 "$ASKPASS_SCRIPT"',
        'export SUDO_ASKPASS="$ASKPASS_SCRIPT"',
        remote_shell_path_export_line(),
        'BREW_BIN="$(command -v brew || true)"',
    ]
    performed_steps: list[str] = []

    if wants_xcode:
        remote_lines.extend(
            [
                'if ! xcode-select -p >/dev/null 2>&1; then',
                '  touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress',
                '  PRODUCT="$(softwareupdate --list 2>/dev/null | sed -n "s/^[[:space:]]*\\* Label: //p" | grep "Command Line Tools" | tail -n 1)"',
                '  rm -f /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress',
                '  if [ -z "$PRODUCT" ]; then',
                '    xcode-select --install >/dev/null 2>&1 || true',
                '    echo "HARNESS_BOOTSTRAP_ERROR|xcode_local_install" >&2',
                '    exit 31',
                '  fi',
                '  sudo_run softwareupdate --install "$PRODUCT" --verbose || exit 32',
                '  if ! xcode-select -p >/dev/null 2>&1; then',
                '    echo "HARNESS_BOOTSTRAP_ERROR|xcode_local_install" >&2',
                '    exit 31',
                '  fi',
                'fi',
            ]
        )
        performed_steps.append("Installed Xcode Command Line Tools when they were missing.")

    if wants_brew:
        remote_lines.extend(
            [
                'if [ -z "$BREW_BIN" ]; then',
                '  sudo_run mkdir -p /opt/homebrew /opt/homebrew/Cellar /opt/homebrew/Caskroom /opt/homebrew/Frameworks',
                '  sudo_run chown -R "$USER":admin /opt/homebrew',
                '  sudo_run -v >/dev/null 2>&1 || exit 41',
                '  NONINTERACTIVE=1 CI=1 SUDO_ASKPASS="$ASKPASS_SCRIPT" /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || exit 33',
                '  BREW_BIN="$(command -v brew || true)"',
                '  if [ -z "$BREW_BIN" ] && [ -x /opt/homebrew/bin/brew ]; then BREW_BIN=/opt/homebrew/bin/brew; fi',
                '  if [ -z "$BREW_BIN" ]; then',
                '    echo "HARNESS_BOOTSTRAP_ERROR|brew_install_failed" >&2',
                '    exit 33',
                '  fi',
                'fi',
            ]
        )
        performed_steps.append("Installed Homebrew when it was missing.")

    result = _run_remote_ssh(
        host,
        "sh",
        "-s",
        identity_file=private_key,
        timeout=1800,
        input_text="\n".join(remote_lines),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or None
        if detail:
            if "HARNESS_BOOTSTRAP_ERROR|xcode_local_install" in detail:
                return HostSetupResult(
                    ok=False,
                    message="Finish Xcode Command Line Tools on the remote Mac first.",
                    detail=(
                        "macOS did not expose a usable noninteractive Command Line Tools update over SSH. "
                        "Open the remote Mac locally, complete the Command Line Tools install, then retry host preparation."
                    ),
                    performed_steps=performed_steps,
                    requires_password=False,
                )
            if wants_xcode and (
                "No such update" in detail
                or "No updates are available" in detail
                or "Command Line Tools for Xcode" in detail
            ):
                return HostSetupResult(
                    ok=False,
                    message="Finish Xcode Command Line Tools on the remote Mac first.",
                    detail=(
                        "macOS did not provide a usable Command Line Tools update over SSH. "
                        "Open the remote Mac locally, complete the Command Line Tools install, then retry host preparation."
                    ),
                    performed_steps=performed_steps,
                    requires_password=False,
                )
            if "HARNESS_BOOTSTRAP_ERROR|sudo_auth" in detail:
                return HostSetupResult(
                    ok=False,
                    message="Remote sudo authentication failed.",
                    detail=(
                        "The entered password did not unlock sudo for the remote account. "
                        "Confirm the password and that the account can run sudo commands on that Mac."
                    ),
                    performed_steps=performed_steps,
                    requires_password=True,
                )
            if "HARNESS_BOOTSTRAP_ERROR|brew_install_failed" in detail or "Need sudo access on macOS" in detail:
                return HostSetupResult(
                    ok=False,
                    message="Homebrew installation did not complete on the remote Mac.",
                    detail=(
                        "The noninteractive Homebrew bootstrap did not finish cleanly over SSH. "
                        "Complete Homebrew locally on the Mac, then retry host preparation."
                    ),
                    performed_steps=performed_steps,
                    requires_password=False,
                )
        return HostSetupResult(
            ok=False,
            message="Remote bootstrap failed.",
            detail=detail,
            performed_steps=performed_steps,
            requires_password=True,
        )
    return HostSetupResult(
        ok=True,
        message="Remote Mac bootstrap completed.",
        performed_steps=performed_steps,
    )


def _request_remote_xcode_install(host: dict[str, Any]) -> HostSetupResult:
    ssh_host = str(host.get("host") or "").strip()
    public_key = _default_public_key_path()
    if public_key is None:
        return HostSetupResult(
            ok=False,
            message="No local SSH public key was found for the Xcode tools request.",
            detail="Expected a public key such as ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub.",
        )
    private_key = _private_key_path_for_public_key(public_key)
    if private_key is None:
        return HostSetupResult(
            ok=False,
            message="The matching private key for the Xcode tools request was not found.",
            detail=f"Expected {public_key.with_suffix('')} next to {public_key.name}.",
        )

    result = _run_remote_ssh(
        host,
        "xcode-select",
        "--install",
        identity_file=private_key,
        timeout=30,
    )
    detail = result.stderr.strip() or result.stdout.strip() or None
    if result.returncode == 0:
        return _wait_for_remote_xcode_install(
            host,
            private_key,
            requested_step="Requested the macOS Command Line Tools installer via xcode-select --install.",
        )
    if detail and "already installed" in detail.lower():
        return HostSetupResult(
            ok=True,
            message="Xcode Command Line Tools are already installed on the remote Mac.",
        )
    if detail and "install requested" in detail.lower():
        return _wait_for_remote_xcode_install(
            host,
            private_key,
            requested_step="The macOS Command Line Tools installer was already pending.",
        )
    return HostSetupResult(
        ok=False,
        message="Could not request the Xcode Command Line Tools installer on the remote Mac.",
        detail=detail,
        requires_password=False,
    )


def _wait_for_remote_xcode_install(
    host: dict[str, Any],
    private_key: Path,
    *,
    requested_step: str,
    wait_seconds: int = 1200,
    poll_interval_seconds: int = 10,
) -> HostSetupResult:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        poll = _run_remote_ssh(
            host,
            "xcode-select",
            "-p",
            identity_file=private_key,
            timeout=20,
        )
        if poll.returncode == 0:
            return HostSetupResult(
                ok=True,
                message="Xcode Command Line Tools finished installing on the remote Mac.",
                performed_steps=[requested_step, "Waited for the Command Line Tools install to complete."],
                requires_password=False,
            )
        time.sleep(poll_interval_seconds)

    return HostSetupResult(
        ok=False,
        message="Waiting for Xcode Command Line Tools to finish on the remote Mac.",
        detail="The installer was requested. Finish it in the remote Mac's GUI; host preparation will continue automatically if it completes before this wait window ends, otherwise click Prepare Host again.",
        performed_steps=[requested_step],
        requires_password=False,
    )


def _ssh_lookup_host(ssh_host: str) -> str:
    host_part = ssh_host.rsplit("@", 1)[-1].strip()
    if host_part.startswith("[") and "]" in host_part:
        return host_part[1 : host_part.index("]")]
    if host_part.count(":") == 1:
        name, port = host_part.rsplit(":", 1)
        if port.isdigit():
            return name
    return host_part


def _remote_status_script(*, paths: list[str], repo_path: str) -> str:
    quoted_paths = " ".join(shlex.quote(path) for path in paths)
    lines = [
        remote_shell_path_export_line(),
        'BREW_BIN="$(command -v brew || true)"',
        'if [ -z "$BREW_BIN" ] && [ -x /opt/homebrew/bin/brew ]; then BREW_BIN=/opt/homebrew/bin/brew; fi',
        'if [ -z "$BREW_BIN" ] && [ -x /usr/local/bin/brew ]; then BREW_BIN=/usr/local/bin/brew; fi',
        'FFMPEG_BIN="$(command -v ffmpeg || true)"',
        'AB_AV1_BIN="$(command -v ab-av1 || true)"',
        'if [ -z "$AB_AV1_BIN" ] && [ -x /opt/homebrew/bin/ab-av1 ]; then AB_AV1_BIN=/opt/homebrew/bin/ab-av1; fi',
        'if [ -z "$AB_AV1_BIN" ] && [ -x /usr/local/bin/ab-av1 ]; then AB_AV1_BIN=/usr/local/bin/ab-av1; fi',
        'FFMPEG_FILTERS=""',
        'FFMPEG_ENCODERS=""',
        'if [ -n "$FFMPEG_BIN" ]; then FFMPEG_FILTERS="$("$FFMPEG_BIN" -hide_banner -filters 2>/dev/null || true)"; fi',
        'if [ -n "$FFMPEG_BIN" ]; then FFMPEG_ENCODERS="$("$FFMPEG_BIN" -hide_banner -encoders 2>/dev/null || true)"; fi',
        f"for path in {quoted_paths}; do",
        '  if [ -e "$path" ]; then',
        '    printf "path|%s|1\\n" "$path"',
        "  else",
        '    printf "path|%s|0\\n" "$path"',
        "  fi",
        "done",
        'if xcode-select -p >/dev/null 2>&1; then printf "tool|xcode_clt|1\\n"; else printf "tool|xcode_clt|0\\n"; fi',
        'if [ -n "$BREW_BIN" ]; then printf "tool|brew|1\\n"; else printf "tool|brew|0\\n"; fi',
        'if [ -n "$FFMPEG_BIN" ]; then printf "tool|ffmpeg|1\\n"; else printf "tool|ffmpeg|0\\n"; fi',
        'if printf "%s\\n" "$FFMPEG_FILTERS" | grep -qi "libvmaf"; then printf "tool|ffmpeg_libvmaf|1\\n"; else printf "tool|ffmpeg_libvmaf|0\\n"; fi',
        'if printf "%s\\n" "$FFMPEG_FILTERS" | grep -qi "xpsnr"; then printf "tool|ffmpeg_xpsnr|1\\n"; else printf "tool|ffmpeg_xpsnr|0\\n"; fi',
        'if printf "%s\\n" "$FFMPEG_ENCODERS" | grep -qi "libsvtav1"; then printf "tool|ffmpeg_libsvtav1|1\\n"; else printf "tool|ffmpeg_libsvtav1|0\\n"; fi',
        'if [ -n "$AB_AV1_BIN" ]; then printf "tool|ab_av1|1\\n"; else printf "tool|ab_av1|0\\n"; fi',
        'printf "time|utc_offset|%s\\n" "$(date +%z)"',
    ]
    if repo_path:
        lines.append(
            f'if [ -e {shlex.quote(repo_path)} ]; then printf "repo|exists|1\\n"; else printf "repo|exists|0\\n"; fi'
        )
    else:
        lines.append('printf "repo|exists|1\\n"')
    return "\n".join(lines)


def _parse_remote_status_output(stdout: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"paths": {}, "tools": {}, "repo_path_exists": True, "utc_offset": None}
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
        elif kind == "tool":
            payload["tools"][key] = value == "1"
        elif kind == "repo":
            payload["repo_path_exists"] = value == "1"
        elif kind == "time":
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
