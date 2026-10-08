"""Own development servers from launch until their private process group is empty."""

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time
from typing import NoReturn, TypedDict

POLL_SECONDS = .05
START_SECONDS = 15.0
TERM_SECONDS = 3.0
STOP_SECONDS = 12.0
LOCK_SECONDS = 1.0
CLIENT_SECONDS = 2.0


class Status(TypedDict, total=False):
    state: str
    pid: int
    launcher: int
    guardian: int
    error: str


class GroupCustodyError(RuntimeError):
    """A watchdog failed after creating a server; custody may still be live."""


def group_members(pgid: int) -> set[int]:
    output = subprocess.check_output(
        ["ps", "-A", "-o", "pid=,pgid="], text=True, timeout=2,
    )
    return {int(pid) for pid, group in (line.split() for line in output.splitlines())
            if int(group) == pgid}


def leader_exited(pid: int) -> bool:
    # WNOWAIT keeps the leader's PID reserved even after a crash. Never poll()
    # or reap it while descendants can still receive a group signal.
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def enable_subreaper() -> None:
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        libc.prctl.restype = ctypes.c_int
        if libc.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "cannot adopt orphaned development children")


def reap_descendants(pgid: int) -> set[int]:
    members = group_members(pgid) - {pgid}
    for pid in members:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass  # On macOS launchd reaps descendants orphaned by the leader.
    return group_members(pgid) - {pgid}


def stop_group(child: subprocess.Popen[bytes]) -> int:
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        # Darwin can reject a signal when only the reserved zombie leader remains.
        if not leader_exited(child.pid) or reap_descendants(child.pid):
            raise
    deadline = time.monotonic() + TERM_SECONDS
    empty = False
    while True:
        if leader_exited(child.pid) and not reap_descendants(child.pid):
            if empty:
                # No group query or signal is permitted after this reap.
                return child.wait()
            empty = True
        else:
            empty = False
        if time.monotonic() >= deadline:
            os.killpg(child.pid, signal.SIGKILL)
            deadline = time.monotonic() + TERM_SECONDS
        time.sleep(POLL_SECONDS)


def log(message: str) -> None:
    try:
        print(message, flush=True)
    except (OSError, ValueError):
        pass  # Log I/O cannot interrupt custody or repeat completed cleanup.


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=.2):
            return True
    except OSError:
        return False


def send_status(connection: socket.socket, status: Status) -> None:
    connection.sendall(json.dumps(status).encode() + b"\n")


def guard_group(connection: socket.socket, command: list[str], cwd: Path,
                host: str, port: int, socket_name: str) -> None:
    enable_subreaper()
    stopping = False

    def request_shutdown(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    if port_open(host, port):
        raise OSError("development port already has a listener")
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
    status: Status = {"state": "starting", "pid": 0,
                      "launcher": os.getppid(), "guardian": os.getpid()}
    child = subprocess.Popen(command, cwd=cwd, start_new_session=True,
                             stdin=subprocess.DEVNULL)
    try:
        try:
            status["pid"] = child.pid
            send_status(connection, status)
            deadline = time.monotonic() + START_SECONDS
            while not stopping:
                if leader_exited(child.pid):
                    status.update(state="stopping", error="development server exited")
                    break
                if status["state"] == "starting":
                    if port_open(host, port):
                        status["state"] = "running"
                        send_status(connection, status)
                    elif time.monotonic() >= deadline:
                        status.update(state="stopping", error="development server did not start listening")
                        break
                if select.select([connection], [], [], POLL_SECONDS)[0]:
                    # Only the launcher owns the other endpoint. EOF includes SIGKILL
                    # of that launcher; server descendants never inherit this fd.
                    connection.recv(64)
                    stopping = True
        except OSError:
            pass
        finally:
            status["state"] = "stopping"
            try:
                send_status(connection, status)
            except OSError:
                pass
            # Keep custody and the component lock while a transient observation or
            # signal error prevents completion. Clients can retry Stop.
            while True:
                try:
                    result = stop_group(child)
                    break
                except Exception as exc:
                    log(f"development cleanup pending: {exc}")
                    time.sleep(POLL_SECONDS)
            log(f"group {child.pid} empty; leader reaped with exit {result}")
            Path(socket_name).unlink(missing_ok=True)
    except BaseException as exc:
        # Unknown post-spawn failure must not masquerade as a clean startup exit.
        raise GroupCustodyError(str(exc)) from exc


def serve(component: str, command: list[str], cwd: Path, host: str, port: int) -> int:
    # An ignored inherited SIGCHLD would auto-reap the server and release its
    # PID before group completion. Both launcher and watchdog retain exit status.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    socket_name = component + ".sock"
    with open(component + ".lock", "a") as lock, socket.socket(socket.AF_UNIX) as server:
        lock_deadline = time.monotonic() + LOCK_SECONDS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= lock_deadline:
                    return 0
                time.sleep(POLL_SECONDS)
        Path(socket_name).unlink(missing_ok=True)
        server.bind(socket_name)
        server.listen()
        parent, child = socket.socketpair()
        guardian = os.fork()
        if guardian == 0:
            parent.close()
            server.close()
            try:
                guard_group(child, command, cwd, host, port, socket_name)
                os._exit(0)
            except Exception as exc:
                log(f"development launcher failed: {exc}")
                try:
                    send_status(child, {"state": "failed", "error": str(exc)})
                except OSError:
                    pass
                os._exit(1 if isinstance(exc, GroupCustodyError) else 0)
        child.close()
        status: Status = {"state": "starting", "launcher": os.getpid(), "guardian": guardian}
        shutting_down = False
        stop_requested = False

        def request_shutdown(_signum: int, _frame: object) -> None:
            nonlocal shutting_down
            shutting_down = True

        def notify_stop() -> None:
            nonlocal stop_requested
            if not stop_requested:
                try:
                    parent.sendall(b"stop")
                except OSError as request_error:
                    log(f"development stop request pending: {request_error}")
                else:
                    stop_requested = True

        signal.signal(signal.SIGTERM, request_shutdown)
        signal.signal(signal.SIGINT, request_shutdown)
        pending = b""
        try:
            while True:
                if shutting_down:
                    notify_stop()
                    shutting_down = False
                for ready in select.select([server, parent], [], [], POLL_SECONDS)[0]:
                    if ready is parent:
                        data = parent.recv(4096)
                        if not data:
                            _, exit_status = os.waitpid(guardian, 0)
                            guardian = 0
                            if exit_status == 0:
                                return 0
                            status.update(state="failed", error="group guardian failed; new starts blocked")
                            # Retain the lock and report failure rather than invent
                            # ownership from a saved PID after losing the guardian.
                            parent.close()
                            return serve_failure(server, status)
                        pending += data
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            status = json.loads(line)
                    else:
                        try:
                            with server.accept()[0] as client:
                                client.settimeout(CLIENT_SECONDS)
                                action = client.recv(64).decode()
                                if action == "stop":
                                    notify_stop()
                                    status["state"] = "stopping"
                                send_status(client, status)
                        except (OSError, UnicodeError):
                            continue  # A failed client cannot release component custody.
        finally:
            parent.close()
            if guardian:
                _, exit_status = os.waitpid(guardian, 0)
                if exit_status != 0:
                    status.update(state="failed", error="group guardian failed; new starts blocked")
                    serve_failure(server, status)
            Path(socket_name).unlink(missing_ok=True)


def serve_failure(server: socket.socket, status: Status) -> NoReturn:
    while True:
        try:
            with server.accept()[0] as client:
                client.settimeout(CLIENT_SECONDS)
                client.recv(64)
                send_status(client, status)
        except OSError:
            continue


def request(component: str, action: str) -> Status:
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(CLIENT_SECONDS + 1)
            try:
                client.connect(component + ".sock")
            except (FileNotFoundError, ConnectionRefusedError):
                with open(component + ".lock", "a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return {"state": "stopping"}
                return {"state": "stopped"}
            client.sendall(action.encode())
            with client.makefile("r") as response:
                return json.loads(response.readline())
    except (OSError, ValueError) as exc:
        return {"state": "unknown", "error": f"control unavailable: {exc}"}


def start(component: str, command: list[str], cwd: Path, host: str, port: int) -> Status:
    launcher: subprocess.Popen[bytes] | None = None
    deadline = time.monotonic() + START_SECONDS + 2
    while time.monotonic() < deadline:
        status = request(component, "status")
        if status["state"] in {"running", "failed"}:
            return status
        if status["state"] == "stopped":
            if launcher is not None and launcher.poll() is not None:
                return {"state": "failed", "error": "launcher exited before startup; see component log"}
            if launcher is None:
                with open(component + ".log", "ab", buffering=0) as output:
                    launcher = subprocess.Popen(
                        [sys.executable, str(Path(__file__).resolve()), "serve", component,
                         "--state-dir", str(Path.cwd()), "--cwd", str(cwd),
                         "--host", host, "--port", str(port), "--", *command],
                        start_new_session=True, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                    )
        time.sleep(POLL_SECONDS)
    return {"state": "failed", "error": "server did not start; see component log"}


def stop(component: str) -> Status:
    deadline = time.monotonic() + STOP_SECONDS
    status: Status = {"state": "unknown", "error": "no control response"}
    while time.monotonic() < deadline:
        status = request(component, "stop")
        if status["state"] in {"stopped", "failed"}:
            return status
        time.sleep(POLL_SECONDS)
    if status["state"] == "unknown":
        return status
    return {"state": "stopping", "error": "cleanup is still running; retry Stop"}


def tcp_port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("development port must be between 1 and 65535")
    return port


def component_command(component: str, root: Path) -> tuple[list[str], Path, str, int]:
    if component == "backend":
        host = os.environ.get("MEDIAFORCE_WEB_HOST", "127.0.0.1")
        port = tcp_port(os.environ.get("MEDIAFORCE_WEB_PORT", "8777"))
        reload_enabled = os.environ.get("MEDIAFORCE_WEB_RELOAD", "false").lower() in {"1", "true", "yes", "on"}
        command = [str(root / ".venv/bin/mediaforce-web"), "--host", host, "--port", str(port),
                   "--reload" if reload_enabled else "--no-reload"]
        if config := os.environ.get("MEDIAFORCE_CONFIG_PATH"):
            config_path = Path(config).expanduser()
            if not config_path.is_absolute():
                config_path = root / config_path
            command.extend(["--config", str(config_path.resolve())])
        return command, root, host, port
    host = os.environ.get("MEDIAFORCE_FRONTEND_DEV_HOST", "127.0.0.1")
    port = tcp_port(os.environ.get("MEDIAFORCE_FRONTEND_DEV_PORT", "4173"))
    return ["npm", "--prefix", str(root / "frontend"), "run", "dev", "--",
            "--host", host, "--port", str(port), "--strictPort"], root / "frontend", host, port


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "restart", "status", "smoke", "serve"])
    parser.add_argument("component", choices=["all", "backend", "frontend"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    argv = sys.argv[1:]
    separator = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:separator])
    command = argv[separator + 1:]
    root = args.root.resolve()
    state = args.state_dir or Path(os.environ.get("MEDIAFORCE_DEV_STATE_DIR", str(
        Path.home() / "Library/Application Support/mediaforce/development" /
        hashlib.sha256(str(root).encode()).hexdigest())))
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chdir(state)
    if args.action == "serve":
        return serve(args.component, command, args.cwd, args.host, args.port)
    failed = False
    status: Status = {"state": "stopped"}
    components = ["frontend", "backend"] if args.component == "all" else [args.component]
    commands = {component: component_command(component, root) for component in components} \
        if args.action in {"start", "restart"} else {}
    for component in components:
        if args.action in {"stop", "restart"}:
            status = stop(component)
            if status["state"] != "stopped":
                print(f"{component}: {json.dumps(status)}")
                failed = True
                continue
        if args.action in {"start", "restart"}:
            status = start(component, *commands[component])
            failed |= status["state"] != "running"
        elif args.action == "status":
            status = request(component, "status")
            failed |= status["state"] != "running"
        elif args.action == "smoke":
            from urllib.request import urlopen
            _, _, host, port = component_command(component, root)
            routes = ["/", "/api/dashboard", "/api/settings", "/api/hosts"] if component == "backend" else ["/"]
            for route in routes:
                with urlopen(f"http://{host}:{port}{route}", timeout=2) as response:
                    response.read()
            status = {"state": "smoke passed"}
        print(f"{component}: {json.dumps(status)}")
    return int(failed)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as error:
        print(f"development command failed: {error}", file=sys.stderr)
        raise SystemExit(1)
