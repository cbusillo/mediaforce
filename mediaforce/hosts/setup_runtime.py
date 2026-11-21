import os
import shlex
import subprocess
from typing import Callable
from typing import Any
from pathlib import Path
import time

from mediaforce.core.config import MediaforceConfig
from mediaforce.hosts.config import _ssh_lookup_host, remote_shell_path_export_line
from mediaforce.hosts.managed_tools import request_update
from mediaforce.hosts.transport import keep_awake_while_running
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

    result = subprocess.run(["ssh-keygen", "-R", lookup_host], capture_output=True, encoding="utf-8", timeout=15)
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
    paths = [staging_root, archive_root]
    if repo_path:
        paths.append(repo_path)
    command = shlex.join(["mkdir", "-p", *paths])
    try:
        result = run_remote_ssh(
            host,
            "sh",
            "-lc",
            command,
            timeout=30,
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

    prep_steps.append("Prepared this computer’s work folders.")
    refreshed = remote_host_status(config, host)
    if refreshed.available:
        return HostSetupResult(
            ok=True,
            message=f"{refreshed.label} is mounted and ready.",
            performed_steps=prep_steps,
        )
    if refreshed.can_update_tools and not refreshed.missing_paths:
        return request_update(config, host, refreshed, run_remote_ssh, requested=True)
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
        prepare_tools: Callable[[MediaforceConfig, dict[str, Any], HostStatus], HostSetupResult],
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
        if status.can_update_tools:
            if (str(config.staging_root_for_host(host)) in status.missing_paths or (
                    status.repo_path and f"Repo path is missing: {status.repo_path}" in status.issues
            )):
                return finish_remote_host_prepare(config, host, prep_steps)
            return prepare_tools(config, host, status)
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
                    message="Enter this computer's account password once so Mediaforce can set up signing in on its own.",
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
                           detail="The installer was requested. Finish it in the remote Mac's GUI; host preparation will continue automatically if it completes before this wait window ends, otherwise use Set up computer again.",
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
            message="Mediaforce couldn't set up signing in to this computer.",
            detail=result.stderr.strip() or result.stdout.strip() or None,
            requires_password=True,
        )

    verify_result = run_remote_ssh(host, "true", identity_file=private_key, timeout=15)
    if verify_result.returncode != 0:
        return HostSetupResult(
            ok=False,
            message="Mediaforce set up signing in, but this computer still asks for a password.",
            detail=(
                    verify_result.stderr.strip()
                    or verify_result.stdout.strip()
                    or "Check the password you entered and confirm password authentication is allowed for first-time setup."
            ),
            requires_password=True,
        )

    return HostSetupResult(
        ok=True,
        message="Mediaforce can now sign in to this computer on its own.",
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
        # Installing Homebrew can run for many minutes.
        input_text=keep_awake_while_running("\n".join(remote_lines)),
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
