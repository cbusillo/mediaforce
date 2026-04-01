import os
import shlex
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig, load_runtime_settings, save_runtime_settings
from mediaforce.encoding.ffmpeg import SVT_AV1_REQUIRED_ISSUE, VIDEOTOOLBOX_REQUIRED_ISSUE
from mediaforce.hosts.config import _host_capabilities, _host_lookup_targets_current_machine, _host_priority, \
    _host_supports_capability, _parse_utc_offset_minutes, _ssh_lookup_host, execution_mode_for_host, \
    host_media_access_for_host, host_status_targets_current_machine, host_targets_current_machine, \
    normalize_host_media_access, remote_shell_path_export_line, ssh_target_for_host
from mediaforce.hosts.status_runtime import _current_machine_host_status as _current_machine_host_status_impl, \
    _remote_host_status as _remote_host_status_impl, _run_remote_status_probe as _run_remote_status_probe_impl
from mediaforce.hosts.setup_runtime import _find_remote_host, \
    _finish_remote_host_prepare as _finish_remote_host_prepare_impl, \
    _bootstrap_remote_macos as _bootstrap_remote_macos_impl, \
    _install_local_ssh_key as _install_local_ssh_key_impl, \
    _request_remote_xcode_install as _request_remote_xcode_install_impl, \
    _wait_for_remote_xcode_install as _wait_for_remote_xcode_install_impl, \
    prepare_remote_host_with_password as prepare_remote_host_with_password_impl, \
    _remote_ffmpeg_install_commands as _remote_ffmpeg_install_commands_impl, \
    reset_remote_host_trust as reset_remote_host_trust_impl
from mediaforce.hosts.status_helpers import _classify_ssh_failure, _command_output, _command_succeeds, \
    _default_public_key_path, _local_tool_status_snapshot, _needs_initial_ssh_key_install, \
    _private_key_path_for_public_key, _should_retry_remote_status_exception, \
    _should_retry_remote_status_failure, _ssh_access_must_be_fixed_first
from mediaforce.hosts.transport import _run_remote_ssh as _run_remote_ssh_impl, \
    copy_remote_file_to_local as copy_remote_file_to_local_impl, \
    run_remote_command as run_remote_command_impl, ssh_client_options as _ssh_client_options_impl
from mediaforce.hosts.wake_runtime import _ensure_remote_awake_for_ssh as _ensure_remote_awake_for_ssh_impl, \
    _learn_remote_wake_mac as _learn_remote_wake_mac_impl, \
    _wake_remote_host_if_configured as _wake_remote_host_if_configured_impl
from mediaforce.hosts.wake_helpers import _broadcast_addresses_for_interface, _interface_for_ip, \
    _local_broadcast_addresses, _looks_like_ipv4_address, _mac_from_arp, _normalize_mac_address, \
    _persist_remote_wake_mac, _resolve_host_to_ip, _resolved_ssh_network_host, _tcp_port_is_open, \
    _wake_broadcast_destinations
from mediaforce.hosts.types import AB_AV1_MISSING_ISSUE, DEFAULT_HOST_CAPABILITIES, DEFAULT_HOST_MEDIA_ACCESS, \
    DEFAULT_WAKE_WAIT_SECONDS, FFMPEG_MISSING_ISSUE, HostSetupResult, HostStatus, \
    LINUX_SAMPLE_CALIBRATION_UNSUPPORTED_ISSUE, REMOTE_SHELL_PATH, REMOTE_STATUS_RETRY_DELAY_SECONDS, \
    SAMPLE_AV1_ENCODER_MISSING_ISSUE, SAMPLE_METRIC_MISSING_ISSUE


def run_host_lifecycle_command(host: dict[str, object], command: str, *, timeout: int) -> subprocess.CompletedProcess[
    str]:
    _ = host
    command_text = str(command or "").strip()
    if not command_text:
        raise RuntimeError("Lifecycle command cannot be empty.")
    return subprocess.run(["sh", "-lc", command_text], capture_output=True, text=True, timeout=timeout)


def collect_host_statuses(config: MediaforceConfig) -> list[HostStatus]:
    return [_remote_host_status(config, host) for host in config.remote_hosts]


def run_remote_command(
        host: dict[str, object],
        command: list[str],
        *,
        timeout: int,
        input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_remote_command_impl(
        host,
        command,
        timeout=timeout,
        input_text=input_text,
        ssh_target_for_host=ssh_target_for_host,
        remote_shell_path_export_line=remote_shell_path_export_line,
        run_remote_ssh=_run_remote_ssh,
    )


def copy_remote_file_to_local(
        host: dict[str, object],
        remote_path: Path,
        local_path: Path,
        *,
        timeout: int,
) -> None:
    return copy_remote_file_to_local_impl(
        host,
        remote_path,
        local_path,
        timeout=timeout,
        ssh_target_for_host=ssh_target_for_host,
        ensure_remote_awake_for_ssh=_ensure_remote_awake_for_ssh,
        ssh_client_options_func=ssh_client_options,
        subprocess_run=subprocess.run,
    )


def _remote_host_status(config: MediaforceConfig, host: dict[str, object]) -> HostStatus:
    return _remote_host_status_impl(
        config,
        host,
        current_machine_host_status=_current_machine_host_status,
        run_remote_status_probe=_run_remote_status_probe,
        learn_remote_wake_mac=_learn_remote_wake_mac,
    )


def _current_machine_host_status(
        config: MediaforceConfig,
        host: dict[str, object],
        *,
        ssh_host: str,
        label: str,
        repo_path: str | None,
) -> HostStatus:
    return _current_machine_host_status_impl(
        config,
        host,
        ssh_host=ssh_host,
        label=label,
        repo_path=repo_path,
        local_tool_status_snapshot=_local_tool_status_snapshot,
    )


def prepare_remote_host(config: MediaforceConfig, host_key: str) -> HostSetupResult:
    return prepare_remote_host_with_password(config, host_key, password=None)


def reset_remote_host_trust(config: MediaforceConfig, host_key: str) -> HostSetupResult:
    return reset_remote_host_trust_impl(config, host_key)


def prepare_remote_host_with_password(
        config: MediaforceConfig,
        host_key: str,
        *,
        password: str | None,
) -> HostSetupResult:
    return prepare_remote_host_with_password_impl(
        config,
        host_key,
        password=password,
        remote_host_status=_remote_host_status,
        needs_initial_ssh_key_install=_needs_initial_ssh_key_install,
        install_local_ssh_key=_install_local_ssh_key,
        ssh_access_must_be_fixed_first=_ssh_access_must_be_fixed_first,
        request_remote_xcode_install=_request_remote_xcode_install,
        bootstrap_remote_macos=lambda host, pwd, issues: _bootstrap_remote_macos(host, pwd, issues=issues),
        finish_remote_host_prepare=_finish_remote_host_prepare,
    )


def _finish_remote_host_prepare(
        config: MediaforceConfig,
        host: dict[str, Any],
        prep_steps: list[str],
) -> HostSetupResult:
    return _finish_remote_host_prepare_impl(
        config,
        host,
        prep_steps,
        run_remote_ssh=_run_remote_ssh,
        remote_host_status=_remote_host_status,
    )


def _remote_ffmpeg_install_commands(*, sample_calibration: bool) -> list[str]:
    return _remote_ffmpeg_install_commands_impl(sample_calibration=sample_calibration)


def _run_remote_status_probe(
        host: dict[str, object],
        script: str,
        *,
        timeout: int,
) -> subprocess.CompletedProcess[str]:
    return _run_remote_status_probe_impl(
        host,
        script,
        timeout=timeout,
        run_remote_ssh=_run_remote_ssh,
        should_retry_remote_status_exception=_should_retry_remote_status_exception,
        should_retry_remote_status_failure=_should_retry_remote_status_failure,
        sleep=time.sleep,
        retry_delay_seconds=REMOTE_STATUS_RETRY_DELAY_SECONDS,
    )


def ssh_client_options(*, batch_mode: bool = True, connect_timeout_seconds: int = 5) -> list[str]:
    return _ssh_client_options_impl(batch_mode=batch_mode, connect_timeout_seconds=connect_timeout_seconds)


def _run_remote_ssh(
        host: dict[str, object],
        *remote_args: str,
        input_text: str | None = None,
        timeout: int,
        identity_file: Path | None = None,
        batch_mode: bool = True,
        wake_before_connect: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run_remote_ssh_impl(
        host,
        *remote_args,
        input_text=input_text,
        timeout=timeout,
        identity_file=identity_file,
        batch_mode=batch_mode,
        wake_before_connect=wake_before_connect,
        ensure_remote_awake_for_ssh=_ensure_remote_awake_for_ssh,
        ssh_client_options_func=ssh_client_options,
        subprocess_run=subprocess.run,
    )


def _ensure_remote_awake_for_ssh(
        host: dict[str, object], *, wake_wait_seconds: int = DEFAULT_WAKE_WAIT_SECONDS
) -> None:
    return _ensure_remote_awake_for_ssh_impl(
        host,
        wake_wait_seconds=wake_wait_seconds,
        wake_remote_host_if_configured=_wake_remote_host_if_configured,
    )


def _wake_remote_host_if_configured(host: dict[str, Any]) -> HostSetupResult:
    return _wake_remote_host_if_configured_impl(host)


def _learn_remote_wake_mac(config: MediaforceConfig, host: dict[str, object], ssh_host: str) -> None:
    return _learn_remote_wake_mac_impl(config, host, ssh_host)


def _install_local_ssh_key(host: dict[str, Any], password: str) -> HostSetupResult:
    return _install_local_ssh_key_impl(
        host,
        password,
        ensure_remote_awake_for_ssh=_ensure_remote_awake_for_ssh,
        default_public_key_path=_default_public_key_path,
        private_key_path_for_public_key=_private_key_path_for_public_key,
        ssh_client_options=ssh_client_options,
        subprocess_run=subprocess.run,
        run_remote_ssh=_run_remote_ssh,
    )


def _bootstrap_remote_macos(host: dict[str, Any], password: str, *, issues: list[str]) -> HostSetupResult:
    return _bootstrap_remote_macos_impl(
        host,
        password,
        issues=issues,
        default_public_key_path=_default_public_key_path,
        private_key_path_for_public_key=_private_key_path_for_public_key,
        request_remote_xcode_install=_request_remote_xcode_install,
        run_remote_ssh=_run_remote_ssh,
    )


def _request_remote_xcode_install(host: dict[str, Any]) -> HostSetupResult:
    return _request_remote_xcode_install_impl(
        host,
        default_public_key_path=_default_public_key_path,
        private_key_path_for_public_key=_private_key_path_for_public_key,
        run_remote_ssh=_run_remote_ssh,
        wait_for_remote_xcode_install=_wait_for_remote_xcode_install,
    )


def _wait_for_remote_xcode_install(
        host: dict[str, Any],
        private_key: Path,
        *,
        requested_step: str,
        wait_seconds: int = 1200,
        poll_interval_seconds: int = 10,
) -> HostSetupResult:
    return _wait_for_remote_xcode_install_impl(
        host,
        private_key,
        requested_step=requested_step,
        run_remote_ssh=_run_remote_ssh,
        wait_seconds=wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
