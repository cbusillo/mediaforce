"""End an interrupted mounted encoder before its output can be reused."""

import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.remote import run_remote_command


@dataclass(frozen=True)
class _RemoteProcess:
    pid: int
    parent_pid: int
    identity: str
    command: str


def _inventory(host: dict[str, Any], run_command: Callable[..., subprocess.CompletedProcess[str]]) -> dict[int, _RemoteProcess]:
    result = run_command(host, ["ps", "-ww", "-axo", "pid=,ppid=,stat=,lstart=,args="], timeout=10, wake_before_connect=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Remote process inventory failed.")
    if not result.stdout.strip():
        raise RuntimeError("Remote process inventory was empty.")
    processes = {}
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=8)
        if len(fields) != 9:
            raise RuntimeError("Remote process inventory was incomplete.")
        pid, parent_pid = int(fields[0]), int(fields[1])
        if "Z" in fields[2]:
            continue
        command = fields[8]
        identity = " ".join(" ".join(fields[3:]).split())
        processes[pid] = _RemoteProcess(pid, parent_pid, identity, command)
    return processes


def _owned_processes(processes: dict[int, _RemoteProcess], path: Path) -> dict[int, _RemoteProcess]:
    writers = {
        pid for pid, process in processes.items()
        if Path(process.command.split()[0]).name == "ffmpeg"
        and process.command.endswith(f" {path}")
    }
    watch_marker = f"pkill -TERM -f -- {shlex.quote(str(path))} 2>/dev/null"
    owned = writers | {
        pid for pid, process in processes.items()
        if Path(process.command.split()[0]).name in {"sh", "bash", "dash", "zsh"}
        and "mediaforce_connection_watch=" in process.command
        and watch_marker in process.command
    }
    for pid in writers:
        parent_pid = processes[pid].parent_pid
        while parent_pid in processes and parent_pid not in owned:
            parent = processes[parent_pid]
            if Path(parent.command.split()[0]).name not in {"sh", "bash", "dash", "zsh"}:
                break
            if "mediaforce_connection_watch=" not in parent.command:
                break
            owned.add(parent_pid)
            parent_pid = parent.parent_pid
    # The old connection watch can remain blocked after its encoder exits. Include the
    # wrapper's children so it cannot wake up later and kill a new attempt's output.
    while True:
        children = {pid for pid, process in processes.items() if process.parent_pid in owned}
        if children <= owned:
            break
        owned.update(children)
    return {pid: processes[pid] for pid in owned}


def _signal(host: dict[str, Any], processes: dict[int, _RemoteProcess], signal: str,
            run_command: Callable[..., subprocess.CompletedProcess[str]]) -> None:
    script = (
        'signal=$1; shift; while [ "$#" -gt 0 ]; do pid=$1; expected=$2; shift 2; '
        'observed=$(ps -ww -p "$pid" -o lstart=,args= | awk \'{$1=$1;print}\'); '
        '[ "$observed" != "$expected" ] || kill -"$signal" "$pid" 2>/dev/null || true; done'
    )
    arguments = [signal, *[value for process in processes.values() for value in (str(process.pid), process.identity)]]
    input_text = "set -- " + " ".join(shlex.quote(value) for value in arguments) + "\n" + script
    result = run_command(host, ["sh", "-s"], input_text=input_text, timeout=10, wake_before_connect=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Remote encoder could not be ended.")


def _output_writers(host: dict[str, Any], path: Path,
                    run_command: Callable[..., subprocess.CompletedProcess[str]]) -> set[int]:
    # Query by the file itself; ps can escape non-ASCII argv in an SSH locale.
    input_text = "set -- " + shlex.quote(str(path)) + '\nif [ -e "$1" ]; then lsof -Fpa -- "$1"; fi'
    result = run_command(host, ["sh", "-s"], input_text=input_text, timeout=10, wake_before_connect=False)
    if result.returncode not in {0, 1} or result.stderr.strip():
        raise RuntimeError(result.stderr.strip() or "Remote output ownership could not be checked.")
    writers: set[int] = set()
    pid = None
    for field in result.stdout.splitlines():
        if field.startswith("p"):
            pid = int(field[1:])
        elif field in {"aw", "au"} and pid is not None:
            writers.add(pid)
    return writers


def end_remote_output_writers(
        host: dict[str, Any], path: Path, *,
        run_command: Callable[..., subprocess.CompletedProcess[str]] = run_remote_command,
) -> None:
    """Signal only verified ffmpeg writers and their Mediaforce connection wrapper.

    Birth time and command are checked again on the host before each signal. A lost
    connection or an encoder still alive after escalation leaves cleanup unproven.
    """
    inventory = _inventory(host, run_command)
    owned = _owned_processes(inventory, path)
    if _output_writers(host, path, run_command) - owned.keys():
        raise RuntimeError("An unrecognised writer has this output open; wait before retrying.")
    if not owned:
        if any(not character.isascii() or ord(character) < 32 for character in str(path)) and any(
                "mediaforce_connection_watch=" in process.command or Path(process.command.split()[0]).name == "ffmpeg"
                for process in inventory.values()):
            raise RuntimeError("The process inventory obscures this output path; wait for its earlier connection watcher to end.")
        return
    for process in owned.values():
        if Path(process.command.split()[0]).name != "ffmpeg":
            continue
        opened = run_command(host, ["lsof", "-a", "-p", str(process.pid), "-Ffan"], timeout=10, wake_before_connect=False)
        writable = False
        owns_output = False
        for field in opened.stdout.splitlines():
            if field.startswith("f"):
                writable = False
            elif field.startswith("a"):
                writable = field[1:] in {"w", "u"}
            elif writable and field in {f"n{path}", f"n{path} (deleted)"}:
                owns_output = True
        if opened.returncode or not owns_output:
            raise RuntimeError("Could not verify which file the earlier remote encode is writing; wait before retrying.")
    # The watcher ignores TERM and runs broad legacy pkill commands when its cat
    # exits. End and verify every owned shell before releasing any waiting child.
    wrappers = {pid: process for pid, process in owned.items()
                if Path(process.command.split()[0]).name in {"sh", "bash", "dash", "zsh"}}
    if wrappers:
        _signal(host, wrappers, "KILL", run_command)
        current = _inventory(host, run_command)
        if any(pid in current and current[pid].identity == process.identity for pid, process in wrappers.items()):
            raise RuntimeError("The earlier connection watcher is still ending; wait before retrying.")
        owned = {pid: process for pid, process in owned.items() if pid not in wrappers}
    _signal(host, owned, "TERM", run_command)
    time.sleep(0.2)
    current = _inventory(host, run_command)
    remaining = {
        pid: process for pid, process in owned.items()
        if pid in current and current[pid].identity == process.identity
    }
    if remaining:
        _signal(host, remaining, "KILL", run_command)
        time.sleep(0.2)
        current = _inventory(host, run_command)
    if any(pid in current and current[pid].identity == process.identity for pid, process in owned.items()) or _owned_processes(current, path) or _output_writers(host, path, run_command):
        raise RuntimeError("The remote encode is still ending; wait before making this file again.")
