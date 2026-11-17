import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from mediaforce.core.process_control import ManagedProcessController, ScheduleWindowClosedError, run_command
from mediaforce.core.schedule_deadline import guard_shell_script_for_schedule_deadline, \
    managed_schedule_close_deadline, process_result_reached_schedule_deadline

# Lines the OpenSSH client prints itself when the connection fails or is ended, as opposed to the
# remote command's own output.
_SSH_CLIENT_FAILURE_PREFIXES = (
    "ssh:",
    "client_loop:",
    "kex_exchange_identification:",
    "connection closed by",
    "connection to ",
    "timeout, server",
    "killed by signal",
    "packet_write_wait:",
)


# A Mac in dark wake answers over the network with its display and most of its cooling off, and
# sleeps again under load ("Dark Wake Thermal Emergency"); its capabilities then lack Graphics.
MACOS_DARK_WAKE_SHELL_TEST = (
    'case "$(pmset -g systemstate 2>/dev/null)" in '
    '*"Capabilities are:"*Graphics*) false ;; *"Capabilities are:"*) true ;; *) false ;; esac'
)

# Nanoseconds since the last keyboard, mouse or trackpad input; empty when the Mac cannot say.
_INPUT_IDLE_NANOSECONDS_SHELL = "ioreg -c IOHIDSystem -d 4 2>/dev/null | awk '/\"HIDIdleTime\" =/ { print $NF; exit }'"

# Declaring brief user activity brings a dark-woken Mac to full wake, which lights its display;
# putting the display straight back to sleep keeps full wake. The display is put to sleep only on
# proof that nobody touched the Mac during the request: input idle time read before and after it,
# where the first reading already outlasts the whole request (whole seconds, rounded up) and the
# second is longer still. Input during the request would make the second reading shorter than the
# request itself. Without that proof the display is left alone. No power setting changes.
REQUEST_FULL_WAKE_SHELL_LINE = (
    f'if {MACOS_DARK_WAKE_SHELL_TEST}; then '
    f'mediaforce_from=$(date +%s); mediaforce_idle_before=$({_INPUT_IDLE_NANOSECONDS_SHELL}); '
    'caffeinate -u -t 2 </dev/null >/dev/null 2>&1; '
    f'mediaforce_idle_after=$({_INPUT_IDLE_NANOSECONDS_SHELL}); mediaforce_to=$(date +%s); '
    'if [ -n "$mediaforce_idle_before" ] && [ -n "$mediaforce_idle_after" ] '
    '&& [ "$mediaforce_idle_after" -gt "$mediaforce_idle_before" ] '
    '&& [ "$mediaforce_idle_before" -ge $(((mediaforce_to - mediaforce_from + 1) * 1000000000)) ]; then '
    'pmset displaysleepnow >/dev/null 2>&1; fi; fi'
)

# On a Mac, caffeinate holds a "prevent idle sleep" assertion that ends when the shell running the
# work exits, however it ends: success, failure, Stop, a schedule deadline or a dropped link. It
# changes no power setting. Run from a subshell, it is not a job of the work's shell, so a bare
# `wait` there cannot wait for it. Where caffeinate is missing, the line does nothing.
_KEEP_AWAKE_SHELL_LINE = (
    'if command -v caffeinate >/dev/null 2>&1; then '
    f'(caffeinate -i -w "$$" </dev/null >/dev/null 2>&1 &); {REQUEST_FULL_WAKE_SHELL_LINE}; fi'
)

# The Mac's power log line for its most recent sleep: when it slept and why, in its own words.
LAST_SLEEP_ENTRY_SHELL = "pmset -g log 2>/dev/null | grep 'Entering Sleep state due to' | tail -n 1"
_SLEEP_ENTRY = re.compile(
    r"^(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}) .*Entering Sleep state due to '(?P<reason>[^']*)'"
)


def slept_from_dark_wake_after(sleep_entry: str, since: datetime) -> bool:
    match = _SLEEP_ENTRY.match(sleep_entry.strip())
    if match is None:
        return False
    slept_at = datetime.strptime(match["at"], "%Y-%m-%d %H:%M:%S %z")
    return slept_at >= since and "dark wake" in match["reason"].lower()


class EncodeConnectionLostError(RuntimeError):
    """The SSH connection ended before the work on that computer finished."""

    def __init__(self, message: str, *, dark_wake_sleep: bool = False) -> None:
        super().__init__(message)
        self.dark_wake_sleep = dark_wake_sleep


SSH_ALIVE_CHECK_SECONDS = 15
SSH_ALIVE_CHECKS_BEFORE_DROP = 4


def keep_awake_while_running(script: str) -> str:
    """Keep the computer fully awake for as long as the shell running ``script`` lives."""
    return f"{_KEEP_AWAKE_SHELL_LINE}\n{script}"


# Refusals that a later try cannot fix. They win over any drop line in the same output, because a
# failed jump-host login is often followed by the connection closing.
SSH_ACCESS_REFUSED_MARKERS = ("permission denied", "host key verification failed")


# Of those, the ones that say an established or attempted link went away, which a later try can fix.
_SSH_CONNECTION_DROP_MARKERS = (
    "killed by signal",
    "closed by remote host",
    "connection closed by",
    "connection reset",
    "broken pipe",
    "timed out",
    "timeout, server",
    "packet_write_wait:",
)


def ssh_connection_lost(returncode: int, stderr: str | None) -> bool:
    """The SSH client, not the command it ran, ended the run, for any reason including refused access.

    The client exits 255 and says why when its connection fails or it is told to stop. A negative
    status means the client itself was killed by a signal before it could say anything.
    """
    if returncode < 0:
        return True
    if returncode != 255:
        return False
    return bool(_ssh_client_lines(stderr))


def ssh_connection_dropped(returncode: int, stderr: str | None) -> bool:
    """The SSH link itself went away, as opposed to the computer refusing access.

    Refused logins and changed host keys also end the run as an SSH failure, but trying again
    cannot fix them, so they are not a dropped link.
    """
    if returncode < 0:
        return True
    if returncode != 255:
        return False
    if any(marker in (stderr or "").lower() for marker in SSH_ACCESS_REFUSED_MARKERS):
        return False
    return any(marker in line for line in _ssh_client_lines(stderr) for marker in _SSH_CONNECTION_DROP_MARKERS)


def ssh_host_unreachable(returncode: int, stderr: str | None) -> bool:
    """The SSH client could not reach the computer or lost it, for example because it is asleep.

    A refused login also fails in the client, but the computer answered, so it is not this.
    """
    if not ssh_connection_lost(returncode, stderr):
        return False
    return not any(marker in (stderr or "").lower() for marker in SSH_ACCESS_REFUSED_MARKERS)


def _ssh_client_lines(stderr: str | None) -> list[str]:
    lines = [line.strip().lower() for line in (stderr or "").splitlines()]
    return [line for line in lines if line.startswith(_SSH_CLIENT_FAILURE_PREFIXES)]


def ssh_client_options(
        *,
        batch_mode: bool = True,
        connect_timeout_seconds: int = 5,
        alive_check_seconds: int = SSH_ALIVE_CHECK_SECONDS,
        alive_checks_before_drop: int = SSH_ALIVE_CHECKS_BEFORE_DROP,
) -> list[str]:
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
    options.extend(["-o", f"ServerAliveInterval={alive_check_seconds}", "-o", f"ServerAliveCountMax={alive_checks_before_drop}"])
    return options


def _run_remote_ssh(
        host: dict[str, object],
        *remote_args: str,
        input_text: str | None = None,
        timeout: int,
        identity_file: Path | None = None,
        batch_mode: bool = True,
        wake_before_connect: bool = True,
        process_controller: ManagedProcessController | None = None,
        idle_timeout: float | None = None,
        control_path: Path | None = None,
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
    if control_path is not None:
        cmd.extend(["-o", "ControlMaster=no", "-o", "ControlPersist=no", "-o", "ForkAfterAuthentication=no", "-o", f"ControlPath={control_path}"])
    cmd.append(ssh_host)
    if remote_args:
        cmd.append(shlex.join([str(arg) for arg in remote_args]))
    if process_controller is not None or idle_timeout is not None:
        return run_command(
            cmd,
            process_controller=process_controller,
            timeout=timeout,
            input_text=input_text,
            idle_timeout=idle_timeout,
        )
    return subprocess_run(cmd, capture_output=True, text=True, timeout=timeout, input_text=input_text)


def run_remote_command(
        host: dict[str, object],
        command: list[str],
        timeout: int,
        input_text: str | None = None,
        process_controller: ManagedProcessController | None = None,
        *,
        idle_timeout: float | None = None,
        control_path: Path | None = None,
        wake_before_connect: bool = True,
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
    deadline = managed_schedule_close_deadline(host, process_controller)
    if deadline is not None:
        script = guard_shell_script_for_schedule_deadline(script, deadline)
    if wake_before_connect:
        # Work worth waking a computer for is worth keeping it awake for; probes that leave a
        # sleeping computer asleep also let it sleep.
        script = keep_awake_while_running(script)
    remote_kwargs: dict[str, Any] = {
        "input_text": input_text,
        "timeout": timeout,
    }
    if process_controller is not None:
        remote_kwargs["process_controller"] = process_controller
    if idle_timeout is not None:
        remote_kwargs["idle_timeout"] = idle_timeout
    if not wake_before_connect:
        remote_kwargs["wake_before_connect"] = False
    if control_path is not None:
        remote_kwargs["control_path"] = control_path
    result = run_remote_ssh(
        normalized_host,
        "sh",
        "-lc",
        script,
        **remote_kwargs,
    )
    if deadline is not None and process_result_reached_schedule_deadline(result, host):
        raise ScheduleWindowClosedError("Encode host schedule window closed.")
    return result


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
