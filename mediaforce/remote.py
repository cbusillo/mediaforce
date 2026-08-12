import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.process_control import ManagedProcessController
from mediaforce.encoding.ffmpeg import SVT_AV1_REQUIRED_ISSUE, VIDEOTOOLBOX_REQUIRED_ISSUE
from mediaforce.hosts.config import execution_mode_for_host, host_media_access_for_host, \
    host_status_targets_current_machine, host_targets_current_machine, normalize_host_media_access, \
    remote_shell_path_export_line, ssh_target_for_host
from mediaforce.hosts.mount_runtime import REMOTE_MOUNT_ATTEMPT_SECONDS, ControllerSmbMount, RemoteSmbMount, \
    controller_smb_mounts_from_output, controller_smb_mounts_from_payload, controller_smb_mounts_path, \
    finder_mount_roots_for_paths, load_controller_smb_mounts, mount_smb_shares, remote_smb_mounts_for_paths, \
    save_controller_smb_mounts
from mediaforce.hosts.status_runtime import _current_machine_host_status as _current_machine_host_status_impl, \
    _remote_host_status as _remote_host_status_impl, _run_remote_status_probe as _run_remote_status_probe_impl
from mediaforce.hosts.setup_runtime import _finish_remote_host_prepare as _finish_remote_host_prepare_impl, \
    _bootstrap_remote_macos as _bootstrap_remote_macos_impl, \
    _install_local_ssh_key as _install_local_ssh_key_impl, \
    _request_remote_xcode_install as _request_remote_xcode_install_impl, \
    _wait_for_remote_xcode_install as _wait_for_remote_xcode_install_impl, \
    prepare_remote_host_with_password as prepare_remote_host_with_password_impl, \
    _remote_ffmpeg_install_commands as _remote_ffmpeg_install_commands_impl, \
    reset_remote_host_trust as reset_remote_host_trust_impl
from mediaforce.hosts.status_helpers import _classify_ssh_failure, _default_public_key_path, \
    _local_tool_status_snapshot, _needs_initial_ssh_key_install, \
    _private_key_path_for_public_key, _should_retry_remote_status_exception, \
    _should_retry_remote_status_failure, _ssh_access_must_be_fixed_first
from mediaforce.hosts.transport import _run_remote_ssh as _run_remote_ssh_impl, \
    copy_remote_file_to_local as copy_remote_file_to_local_impl, \
    run_remote_command as run_remote_command_impl, ssh_client_options as _ssh_client_options_impl
from mediaforce.hosts.wake_runtime import _ensure_remote_awake_for_ssh as _ensure_remote_awake_for_ssh_impl, \
    _learn_remote_wake_mac as _learn_remote_wake_mac_impl, \
    _wake_remote_host_if_configured as _wake_remote_host_if_configured_impl
from mediaforce.hosts.types import AB_AV1_MISSING_ISSUE, DEFAULT_HOST_CAPABILITIES, DEFAULT_HOST_MEDIA_ACCESS, \
    DEFAULT_WAKE_WAIT_SECONDS, HostReadinessError, HostSetupResult, HostStatus, \
    FFMPEG_MISSING_ISSUE, LINUX_SAMPLE_CALIBRATION_UNSUPPORTED_ISSUE, \
    REMOTE_STATUS_RETRY_DELAY_SECONDS, SAMPLE_AV1_ENCODER_MISSING_ISSUE, SAMPLE_METRIC_MISSING_ISSUE

__all__ = [
    "AB_AV1_MISSING_ISSUE",
    "DEFAULT_HOST_CAPABILITIES",
    "DEFAULT_HOST_MEDIA_ACCESS",
    "DEFAULT_WAKE_WAIT_SECONDS",
    "FFMPEG_MISSING_ISSUE",
    "HostSetupResult",
    "HostStatus",
    "HostReadinessError",
    "LINUX_SAMPLE_CALIBRATION_UNSUPPORTED_ISSUE",
    "REMOTE_STATUS_RETRY_DELAY_SECONDS",
    "SAMPLE_AV1_ENCODER_MISSING_ISSUE",
    "SAMPLE_METRIC_MISSING_ISSUE",
    "SVT_AV1_REQUIRED_ISSUE",
    "VIDEOTOOLBOX_REQUIRED_ISSUE",
    "_classify_ssh_failure",
    "collect_host_statuses",
    "copy_remote_file_to_local",
    "execution_mode_for_host",
    "host_media_access_for_host",
    "host_status_targets_current_machine",
    "learn_controller_smb_mounts",
    "normalize_host_media_access",
    "prepare_remote_host_with_password",
    "recover_remote_host_mounts",
    "remote_mount_recovery_supported",
    "remote_shell_path_export_line",
    "reset_remote_host_trust",
    "run_host_lifecycle_command",
    "run_remote_command",
    "ssh_client_options",
]


_MOUNT_RECOVERY_COOLDOWN_SECONDS = 120.0
_MOUNT_RECOVERY_STATE_LOCK = threading.Lock()
_MOUNT_RECOVERY_COOLDOWNS: dict[tuple[str, str], float] = {}
_MOUNT_RECOVERY_NO_GUI_SESSIONS: dict[tuple[str, str], str] = {}


def run_host_lifecycle_command(host: dict[str, object], command: str, *, timeout: int) -> subprocess.CompletedProcess[
    str]:
    _ = host
    command_text = str(command or "").strip()
    if not command_text:
        raise RuntimeError("Lifecycle command cannot be empty.")
    return _run_subprocess_text(["sh", "-lc", command_text], timeout=timeout)


def remote_mount_recovery_supported(
        config: MediaforceConfig,
        host: dict[str, Any],
        status: HostStatus,
) -> bool:
    if status.issues or not _missing_paths_covered_by_mounts(status.missing_paths, status.missing_mounts):
        return False
    return bool(_remote_smb_mounts_for_status(config, host, status))


def recover_remote_host_mounts(
        config: MediaforceConfig,
        host: dict[str, Any],
        status: HostStatus,
        *,
        force: bool = False,
) -> HostSetupResult:
    mounts = _remote_smb_mounts_for_status(config, host, status)
    label = str(host.get("label") or status.label or host.get("host") or "Remote host").strip() or "Remote host"
    if not mounts:
        return HostSetupResult(
            ok=False,
            message=f"{label} needs shared storage connected before it can run Mediaforce work.",
            detail=(
                "Mediaforce could not match the disconnected storage to an SMB volume mounted on this Mac."
            ),
            failure_kind="controller_storage_unavailable",
        )
    current_machine = host_targets_current_machine(host)
    session = _local_gui_session_token() if current_machine else ""
    recovery_block = _mount_recovery_block(host, mounts, session=session, force=force)
    if recovery_block is not None:
        return recovery_block
    result = mount_smb_shares(
        host,
        mounts,
        run_mount_script=(
            _run_local_mount_script
            if current_machine
            else lambda script, timeout: _run_remote_ssh(
                host,
                "sh",
                "-s",
                input_text=script,
                timeout=timeout,
            )
        ),
        transport="local" if current_machine else "ssh",
    )
    _record_mount_recovery_result(host, mounts, result, session=session)
    return result


def _remote_smb_mounts_for_status(
        config: MediaforceConfig,
        host: dict[str, Any],
        status: HostStatus,
) -> list[RemoteSmbMount] | None:
    if (
            status.available
            or status.mode != "ssh"
            or status.platform != "macos"
            or not status.missing_mounts
            or host_media_access_for_host(host) != "mounted"
    ):
        return None
    controller_mounts = _controller_smb_mounts_for_config(config)
    return remote_smb_mounts_for_paths(
        status.missing_mounts,
        controller_mounts,
        remote_user=_ssh_user_for_host(host),
    )


def learn_controller_smb_mounts(config: MediaforceConfig) -> int:
    observed = controller_smb_mounts_from_output(_controller_smb_mount_output())
    required_roots = _controller_required_mount_roots(config)
    learned = [mount for mount in observed if mount.mount_point in required_roots]
    if not learned:
        return 0
    path = controller_smb_mounts_path(config.paths.runtime_settings_path)
    merged = {m.mount_point: m for m in [*load_controller_smb_mounts(path), *learned]}
    save_controller_smb_mounts(path, list(merged.values()))
    return len(learned)


def _controller_smb_mounts_for_config(config: MediaforceConfig) -> list[ControllerSmbMount]:
    override_mounts = controller_smb_mounts_from_payload(config.raw.get("controller_smb_mounts"))
    learned_mounts = load_controller_smb_mounts(controller_smb_mounts_path(config.paths.runtime_settings_path))
    resolved = {m.mount_point: m for m in [*learned_mounts, *override_mounts]}
    return sorted(resolved.values(), key=lambda mount: len(mount.mount_point.parts), reverse=True)


def _controller_required_mount_roots(config: MediaforceConfig) -> set[Path]:
    roots: set[Path] = set()
    for host in config.remote_hosts:
        if host_media_access_for_host(host) != "mounted":
            continue
        paths = [
            *config.source_root_map_for_host(host).values(),
            config.staging_root_for_host(host),
            config.archive_root_for_host(host),
        ]
        roots.update(_finder_mount_roots(paths))
    return roots


def _finder_mount_roots(paths: list[Path]) -> set[Path]:
    return set(finder_mount_roots_for_paths(paths))


def _controller_smb_mount_output() -> str:
    try:
        result = _run_subprocess_text(["/sbin/mount"], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _run_local_mount_script(script: str, timeout: int) -> subprocess.CompletedProcess[str]:
    return _run_subprocess_text(["/bin/sh", "-s"], input_text=script, timeout=timeout)


def _mount_recovery_block(
        host: dict[str, Any],
        mounts: list[RemoteSmbMount],
        *,
        session: str,
        force: bool,
) -> HostSetupResult | None:
    host_key = _mount_recovery_host_key(host)
    now = time.monotonic()
    with _MOUNT_RECOVERY_STATE_LOCK:
        for mount in mounts:
            key = (host_key, str(mount.mount_point))
            no_gui_session = _MOUNT_RECOVERY_NO_GUI_SESSIONS.get(key)
            if no_gui_session is not None and (
                    (session and no_gui_session == session)
                    or (not session and not force)
            ):
                return HostSetupResult(
                    ok=False,
                    message=f"{_mount_recovery_label(host)} needs a signed-in macOS desktop session to connect shared storage.",
                    detail="Sign in to the desktop, then use Prepare to retry storage recovery.",
                    failure_kind="host_unavailable",
                )
            cooldown_until = _MOUNT_RECOVERY_COOLDOWNS.get(key, 0.0)
            if not force and cooldown_until > now:
                return HostSetupResult(
                    ok=False,
                    message=f"{_mount_recovery_label(host)} is cooling down after a shared-storage recovery attempt.",
                    detail="Mediaforce will retry automatically after the bounded cooldown, or use Prepare to retry now.",
                    failure_kind="host_unavailable",
                )
    return None


def _record_mount_recovery_result(
        host: dict[str, Any],
        mounts: list[RemoteSmbMount],
        result: HostSetupResult,
        *,
        session: str,
) -> None:
    host_key = _mount_recovery_host_key(host)
    no_gui_session = "signed-in macOS desktop session" in result.message
    cooldown_until = time.monotonic() + max(_MOUNT_RECOVERY_COOLDOWN_SECONDS, REMOTE_MOUNT_ATTEMPT_SECONDS + 90)
    with _MOUNT_RECOVERY_STATE_LOCK:
        for mount in mounts:
            key = (host_key, str(mount.mount_point))
            if result.ok:
                _MOUNT_RECOVERY_COOLDOWNS.pop(key, None)
                _MOUNT_RECOVERY_NO_GUI_SESSIONS.pop(key, None)
                continue
            _MOUNT_RECOVERY_COOLDOWNS[key] = cooldown_until
            if no_gui_session:
                _MOUNT_RECOVERY_NO_GUI_SESSIONS[key] = session


def _local_gui_session_token() -> str:
    try:
        result = _run_subprocess_text(["/usr/bin/stat", "-f", "%u:%c", "/dev/console"], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _mount_recovery_host_key(host: dict[str, Any]) -> str:
    return str(host.get("host") or host.get("key") or host.get("label") or "controller").strip()


def _mount_recovery_label(host: dict[str, Any]) -> str:
    return str(host.get("label") or host.get("host") or "Controller").strip() or "Controller"


def _ssh_user_for_host(host: dict[str, Any]) -> str | None:
    target = ssh_target_for_host(host)
    if "@" not in target:
        return None
    user = target.rsplit("@", 1)[0].strip()
    return user or None


def _missing_paths_covered_by_mounts(missing_paths: list[str], missing_mounts: list[str]) -> bool:
    mount_points = [Path(os.path.normpath(path)) for path in missing_mounts]
    for raw_path in missing_paths:
        path = Path(os.path.normpath(raw_path))
        if not any(_path_is_within(path, mount_point) for mount_point in mount_points):
            return False
    return True


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _run_subprocess_text(
        command: list[str],
        *,
        timeout: int,
        capture_output: bool = True,
        text: bool = True,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        input=input_text,
        env=env,
    )


def collect_host_statuses(config: MediaforceConfig) -> list[HostStatus]:
    hosts = list(config.remote_hosts)
    if len(hosts) <= 1:
        return [_remote_host_status(config, host) for host in hosts]

    max_workers = min(len(hosts), 6)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="remote-host-status") as executor:
        future_to_index = {
            executor.submit(_remote_host_status, config, host): index
            for index, host in enumerate(hosts)
        }
        statuses: list[HostStatus | None] = [None] * len(hosts)
        for future in as_completed(future_to_index):
            statuses[future_to_index[future]] = future.result()
        return [status for status in statuses if status is not None]


def run_remote_command(
        host: dict[str, object],
        command: list[str],
        timeout: int,
        input_text: str | None = None,
        process_controller: ManagedProcessController | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_remote_command_impl(
        host,
        command,
        timeout,
        input_text=input_text,
        process_controller=process_controller,
        ssh_target_for_host=ssh_target_for_host,
        remote_shell_path_export_line=remote_shell_path_export_line,
        run_remote_ssh=_run_remote_ssh,
    )


def copy_remote_file_to_local(
        host: dict[str, object],
        remote_path: Path,
        local_path: Path,
        timeout: int,
) -> None:
    return copy_remote_file_to_local_impl(
        host,
        remote_path,
        local_path,
        timeout,
        ssh_target_for_host=ssh_target_for_host,
        ensure_remote_awake_for_ssh=_ensure_remote_awake_for_ssh,
        ssh_client_options_func=ssh_client_options,
        subprocess_run=_run_subprocess_text,
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
        recover_remote_host_mounts=recover_remote_host_mounts,
        learn_controller_smb_mounts=learn_controller_smb_mounts,
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
        process_controller: ManagedProcessController | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run_remote_ssh_impl(
        host,
        *remote_args,
        input_text=input_text,
        timeout=timeout,
        identity_file=identity_file,
        batch_mode=batch_mode,
        wake_before_connect=wake_before_connect,
        process_controller=process_controller,
        ensure_remote_awake_for_ssh=_ensure_remote_awake_for_ssh,
        ssh_client_options_func=ssh_client_options,
        subprocess_run=_run_subprocess_text,
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
        subprocess_run=_run_subprocess_text,
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
