import os
import shlex
import subprocess
from typing import Callable
from typing import Any
from pathlib import Path
import time

from mediaforce.core.config import MediaforceConfig
from mediaforce.hosts.config import _host_supports_capability, _ssh_lookup_host, host_media_access_for_host, \
    remote_shell_path_export_line, stream_host_has_remote_source_roots
from mediaforce.hosts.types import HostSetupResult, HostStatus


def _find_remote_host(config: MediaforceConfig, host_key: str) -> dict[str, Any] | None:
    for host in config.remote_hosts:
        ssh_host = str(host.get("host") or "")
        label = str(host.get("label") or ssh_host or "remote")
        if host_key in {ssh_host, label}:
            return host
    return None


def reset_remote_host_trust(config: MediaforceConfig, host_key: str) -> HostSetupResult:
    host = _find_remote_host(config, host_key)
    if host is None:
        return HostSetupResult(ok=False, message="Remote host is no longer configured")

    ssh_host = str(host.get("host") or "").strip()
    lookup_host = _ssh_lookup_host(ssh_host)
    if not lookup_host:
        return HostSetupResult(ok=False, message="Remote host is missing an SSH target")

    result = subprocess.run(["ssh-keygen", "-R", lookup_host], capture_output=True, text=True, timeout=15)
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
        'FFMPEG_HWACCELS=""',
        'if [ -n "$FFMPEG_BIN" ]; then FFMPEG_HWACCELS="$("$FFMPEG_BIN" -hide_banner -hwaccels 2>/dev/null || true)"; fi',
        'if [ -z "$FFMPEG_BIN" ] || ! printf "%s\n" "$FFMPEG_HWACCELS" | grep -qi "videotoolbox"; then',
        '  if [ -n "$BREW_BIN" ]; then',
        '    HOMEBREW_NO_AUTO_UPDATE=1 "$BREW_BIN" install ffmpeg >/tmp/mediaforce-brew.log 2>&1 || { cat /tmp/mediaforce-brew.log; exit 21; }',
        '  else',
        '    echo "ffmpeg is missing or lacks VideoToolbox decode and Homebrew is unavailable for automatic install." >&2',
        '    exit 22',
        '  fi',
        'fi',
    ]


def _finish_remote_host_prepare(
        config: MediaforceConfig,
        host: dict[str, Any],
        prep_steps: list[str],
        *,
        run_remote_ssh: Callable[..., subprocess.CompletedProcess[str]],
        remote_host_status: Callable[[MediaforceConfig, dict[str, object]], HostStatus],
) -> HostSetupResult:
    repo_path = str(host.get("repo_path") or "").strip()
    staging_root = str(config.staging_root_for_host(host))
    archive_root = str(config.archive_root_for_host(host))
    supports_remote_stream_quality = stream_host_has_remote_source_roots(host)
    installs_ab_av1 = _host_supports_capability(host, "sample_calibration") or (
        _host_supports_capability(host, "encode_queue")
        and (host_media_access_for_host(host) != "stream" or supports_remote_stream_quality)
    )
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
        else "Installed ffmpeg with Homebrew if it was missing or lacked required VideoToolbox decode support."
    )
    if installs_ab_av1:
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
        prep_steps.append("Installed ab-av1 with Homebrew for encode and sample hosts if it was missing.")

    try:
        result = run_remote_ssh(
            host,
            "sh",
            "-lc",
            "\n".join(remote_commands),
            timeout=900,
        )
    except Exception as exc:
        return HostSetupResult(ok=False, message="Remote preparation failed", detail=str(exc),
                               performed_steps=prep_steps)

    if result.returncode != 0:
        return HostSetupResult(
            ok=False,
            message="Remote preparation failed",
            detail=result.stderr.strip() or result.stdout.strip() or None,
            performed_steps=prep_steps,
        )

    refreshed = remote_host_status(config, host)
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


def prepare_remote_host_with_password(
        config: MediaforceConfig,
        host_key: str,
        *,
        password: str | None,
        remote_host_status: Callable[[MediaforceConfig, dict[str, object]], HostStatus],
        needs_initial_ssh_key_install: Callable[[HostStatus], bool],
        install_local_ssh_key: Callable[[dict[str, Any], str], HostSetupResult],
        ssh_access_must_be_fixed_first: Callable[[HostStatus], bool],
        request_remote_xcode_install: Callable[[dict[str, Any]], HostSetupResult],
        bootstrap_remote_macos: Callable[[dict[str, Any], str, list[str]], HostSetupResult],
        recover_remote_host_mounts: Callable[..., HostSetupResult],
        learn_controller_smb_mounts: Callable[[MediaforceConfig], int],
        finish_remote_host_prepare: Callable[[MediaforceConfig, dict[str, Any], list[str]], HostSetupResult],
) -> HostSetupResult:
    host = _find_remote_host(config, host_key)
    if host is None:
        return HostSetupResult(ok=False, message="Remote host is no longer configured")

    ssh_host = str(host.get("host") or "").strip()
    if not ssh_host:
        return HostSetupResult(ok=False, message="Remote host is missing an SSH target")

    try:
        learn_controller_smb_mounts(config)
    except (OSError, ValueError):
        pass
    prep_steps: list[str] = []
    for _ in range(6):
        status = remote_host_status(config, host)
        if status.available:
            return HostSetupResult(
                ok=True,
                message=f"{status.label} is mounted and ready.",
                performed_steps=prep_steps,
            )
        if not status.setup_supported:
            return HostSetupResult(
                ok=False,
                message=status.message,
                detail=status.detail or "\n".join(status.issues) or None,
                performed_steps=prep_steps,
            )
        if needs_initial_ssh_key_install(status):
            if not password:
                return HostSetupResult(
                    ok=False,
                    message="The remote account password is required the first time so Mediaforce can install this Mac's SSH key.",
                    requires_password=True,
                    performed_steps=prep_steps,
                )
            key_install = install_local_ssh_key(host, password)
            prep_steps.extend(key_install.performed_steps)
            key_install.performed_steps = prep_steps
            if not key_install.ok:
                return key_install
            continue
        if status.missing_mounts and status.platform == "macos":
            mount_result = recover_remote_host_mounts(config, host, status, force=True)
            prep_steps.extend(mount_result.performed_steps)
            mount_result.performed_steps = prep_steps
            if not mount_result.ok:
                return mount_result
            continue
        if ssh_access_must_be_fixed_first(status):
            return HostSetupResult(
                ok=False,
                message=status.message,
                detail=status.detail or "\n".join(status.issues) or None,
                requires_password=status.setup_requires_password,
                performed_steps=prep_steps,
            )
        if "Xcode Command Line Tools are not installed on the remote Mac." in status.issues:
            xcode_request = request_remote_xcode_install(host)
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
            bootstrap = bootstrap_remote_macos(host, password, status.issues)
            prep_steps.extend(bootstrap.performed_steps)
            bootstrap.performed_steps = prep_steps
            if not bootstrap.ok:
                return bootstrap
            continue
        return finish_remote_host_prepare(config, host, prep_steps)

    return HostSetupResult(
        ok=False,
        message="Remote preparation stalled before the host became ready.",
        detail="Retry host preparation. If this keeps happening, inspect the remote host status output.",
        performed_steps=prep_steps,
    )


def _request_remote_xcode_install(
        host: dict[str, Any],
        *,
        default_public_key_path: Callable[[], Path | None],
        private_key_path_for_public_key: Callable[[Path], Path | None],
        run_remote_ssh: Callable[..., subprocess.CompletedProcess[str]],
        wait_for_remote_xcode_install: Callable[..., HostSetupResult],
) -> HostSetupResult:
    public_key = default_public_key_path()
    if public_key is None:
        return HostSetupResult(
            ok=False,
            message="No local SSH public key was found for the Xcode tools request.",
            detail="Expected a public key such as ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub.",
        )
    private_key = private_key_path_for_public_key(public_key)
    if private_key is None:
        return HostSetupResult(
            ok=False,
            message="The matching private key for the Xcode tools request was not found.",
            detail=f"Expected {public_key.with_suffix('')} next to {public_key.name}.",
        )

    result = run_remote_ssh(
        host,
        "xcode-select",
        "--install",
        identity_file=private_key,
        timeout=30,
    )
    detail = result.stderr.strip() or result.stdout.strip() or None
    if result.returncode == 0:
        return wait_for_remote_xcode_install(
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
        return wait_for_remote_xcode_install(
            host,
            private_key,
            requested_step="The macOS Command Line Tools installer was already pending.",
        )
    return HostSetupResult(ok=False,
                           message="Could not request the Xcode Command Line Tools installer on the remote Mac.",
                           detail=detail)


def _wait_for_remote_xcode_install(
        host: dict[str, Any],
        private_key: Path,
        *,
        requested_step: str,
        run_remote_ssh: Callable[..., subprocess.CompletedProcess[str]],
        wait_seconds: int = 1200,
        poll_interval_seconds: int = 10,
) -> HostSetupResult:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        poll = run_remote_ssh(
            host,
            "xcode-select",
            "-p",
            identity_file=private_key,
            timeout=20,
        )
        if poll.returncode == 0:
            return HostSetupResult(ok=True, message="Xcode Command Line Tools finished installing on the remote Mac.",
                                   performed_steps=[requested_step,
                                                    "Waited for the Command Line Tools install to complete."])
        time.sleep(poll_interval_seconds)

    return HostSetupResult(ok=False, message="Waiting for Xcode Command Line Tools to finish on the remote Mac.",
                           detail="The installer was requested. Finish it in the remote Mac's GUI; host preparation will continue automatically if it completes before this wait window ends, otherwise click Prepare Host again.",
                           performed_steps=[requested_step])


def _install_local_ssh_key(
        host: dict[str, Any],
        password: str,
        *,
        ensure_remote_awake_for_ssh: Callable[[dict[str, object]], None],
        default_public_key_path: Callable[[], Path | None],
        private_key_path_for_public_key: Callable[[Path], Path | None],
        ssh_client_options: Callable[..., list[str]],
        subprocess_run: Callable[..., subprocess.CompletedProcess[str]],
        run_remote_ssh: Callable[..., subprocess.CompletedProcess[str]],
) -> HostSetupResult:
    ssh_host = str(host.get("host") or "").strip()
    ensure_remote_awake_for_ssh(host)
    public_key = default_public_key_path()
    if public_key is None:
        return HostSetupResult(
            ok=False,
            message="No local SSH public key was found for remote setup.",
            detail="Expected a public key such as ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub.",
        )
    private_key = private_key_path_for_public_key(public_key)
    if private_key is None:
        return HostSetupResult(
            ok=False,
            message="The matching private key for remote setup was not found.",
            detail=f"Expected {public_key.with_suffix('')} next to {public_key.name}.",
        )

    public_key_text = public_key.read_text().strip()
    install_cmd = [
        "ssh",
        *ssh_client_options(batch_mode=False),
        "-o",
        "PreferredAuthentications=password,keyboard-interactive",
        "-o",
        "PubkeyAuthentication=no",
        ssh_host,
        (
            "sh -lc "
            + shlex.quote(
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
                )
            )
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
    result = subprocess_run(["expect", "-c", expect_script], capture_output=True, text=True, timeout=90, env=env)
    if result.returncode != 0:
        return HostSetupResult(
            ok=False,
            message="Installing this Mac's SSH key failed.",
            detail=result.stderr.strip() or result.stdout.strip() or None,
            requires_password=True,
        )

    verify_result = run_remote_ssh(host, "true", identity_file=private_key, timeout=15)
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


def _bootstrap_remote_macos(
        host: dict[str, Any],
        password: str,
        *,
        issues: list[str],
        default_public_key_path: Callable[[], Path | None],
        private_key_path_for_public_key: Callable[[Path], Path | None],
        request_remote_xcode_install: Callable[[dict[str, Any]], HostSetupResult],
        run_remote_ssh: Callable[..., subprocess.CompletedProcess[str]],
) -> HostSetupResult:
    public_key = default_public_key_path()
    if public_key is None:
        return HostSetupResult(
            ok=False,
            message="No local SSH public key was found for remote bootstrap.",
            detail="Expected a public key such as ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub.",
        )
    private_key = private_key_path_for_public_key(public_key)
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
        return request_remote_xcode_install(host)

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

    result = run_remote_ssh(
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
                return HostSetupResult(ok=False, message="Finish Xcode Command Line Tools on the remote Mac first.",
                                       detail=(
                                           "macOS did not expose a usable noninteractive Command Line Tools update over SSH. "
                                           "Open the remote Mac locally, complete the Command Line Tools install, then retry host preparation."
                                       ), performed_steps=performed_steps)
            if wants_xcode and (
                    "No such update" in detail
                    or "No updates are available" in detail
                    or "Command Line Tools for Xcode" in detail
            ):
                return HostSetupResult(ok=False, message="Finish Xcode Command Line Tools on the remote Mac first.",
                                       detail=(
                                           "macOS did not provide a usable Command Line Tools update over SSH. "
                                           "Open the remote Mac locally, complete the Command Line Tools install, then retry host preparation."
                                       ), performed_steps=performed_steps)
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
                return HostSetupResult(ok=False, message="Homebrew installation did not complete on the remote Mac.",
                                       detail=(
                                           "The noninteractive Homebrew bootstrap did not finish cleanly over SSH. "
                                           "Complete Homebrew locally on the Mac, then retry host preparation."
                                       ), performed_steps=performed_steps)
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
