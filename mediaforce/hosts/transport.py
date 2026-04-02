import subprocess
from pathlib import Path
from typing import Callable


def ssh_client_options(*, batch_mode: bool = True, connect_timeout_seconds: int = 5) -> list[str]:
    options = [
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UpdateHostKeys=yes",
        "-o",
        "CheckHostIP=no",
    ]
    if batch_mode:
        options.extend(["-o", "BatchMode=yes"])
    options.extend(["-o", f"ConnectTimeout={connect_timeout_seconds}"])
    return options


def _run_remote_ssh(
        host: dict[str, object],
        *remote_args: str,
        input_text: str | None = None,
        timeout: int,
        identity_file: Path | None = None,
        batch_mode: bool = True,
        wake_before_connect: bool = True,
        ensure_remote_awake_for_ssh: Callable[[dict[str, object]], None],
        ssh_client_options_func: Callable[..., list[str]],
        subprocess_run: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    if wake_before_connect:
        ensure_remote_awake_for_ssh(host)
    ssh_host = str(host.get("host") or "").strip()
    cmd = ["ssh"]
    if identity_file is not None:
        cmd.extend(["-i", str(identity_file), "-o", "IdentitiesOnly=yes"])
    cmd.extend(ssh_client_options_func(batch_mode=batch_mode))
    cmd.extend([ssh_host, *remote_args])
    return subprocess_run(cmd, capture_output=True, text=True, timeout=timeout, input_text=input_text)


def run_remote_command(
        host: dict[str, object],
        command: list[str],
        timeout: int,
        input_text: str | None = None,
        *,
        ssh_target_for_host: Callable[[dict[str, object]], str],
        remote_shell_path_export_line: Callable[[], str],
        run_remote_ssh: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    ssh_host = ssh_target_for_host(host)
    if not ssh_host:
        raise RuntimeError("Remote host is missing an SSH target.")
    if not command:
        raise RuntimeError("Remote command cannot be empty.")

    normalized_host = dict(host)
    normalized_host["host"] = ssh_host
    remote_command = [Path(str(command[0])).name, *[str(arg) for arg in command[1:]]]
    import shlex

    script = "\n".join(
        [
            remote_shell_path_export_line(),
            shlex.join(remote_command),
        ]
    )
    return run_remote_ssh(
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
        timeout: int,
        *,
        ssh_target_for_host: Callable[[dict[str, object]], str],
        ensure_remote_awake_for_ssh: Callable[[dict[str, object]], None],
        ssh_client_options_func: Callable[..., list[str]],
        subprocess_run: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    ssh_host = ssh_target_for_host(host)
    if not ssh_host:
        raise RuntimeError("Remote host is missing an SSH target.")

    normalized_host = dict(host)
    normalized_host["host"] = ssh_host
    ensure_remote_awake_for_ssh(normalized_host)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess_run([
        "scp",
        "-q",
        *ssh_client_options_func(batch_mode=True),
        f"{ssh_host}:{remote_path}",
        str(local_path),
    ], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "scp download failed"
        raise RuntimeError(detail)
