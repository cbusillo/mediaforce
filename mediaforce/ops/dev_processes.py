"""Retain native development process custody across failed stop commands."""

from contextlib import contextmanager
from collections.abc import Iterator
import ctypes
import importlib.util
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time
from uuid import UUID

if __package__:
    from mediaforce.core.dev_processes import DevelopmentProcessTree
else:
    spec = importlib.util.spec_from_file_location(
        "_mediaforce_dev_custody", Path(__file__).resolve().parents[1] / "core/dev_processes.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load development custody gateway")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    DevelopmentProcessTree = module.DevelopmentProcessTree


def boot_id() -> str:
    if sys.platform == "linux":
        return str(UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()))
    if sys.platform != "darwin":
        raise RuntimeError("native development recovery is unavailable")
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                 ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctlbyname.restype = ctypes.c_int
    size = ctypes.c_size_t()
    if libc.sysctlbyname(b"kern.bootsessionuuid", None, ctypes.byref(size), None, 0) or not 0 < size.value <= 64:
        raise OSError("cannot read native boot identity")
    value = ctypes.create_string_buffer(size.value)
    if libc.sysctlbyname(b"kern.bootsessionuuid", value, ctypes.byref(size), None, 0):
        raise OSError("cannot read native boot identity")
    return str(UUID(value.value.decode("ascii")))


@contextmanager
def state_directory(state: Path) -> Iterator[None]:
    info = state.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("unsafe development cleanup directory")
    # Relative AF_UNIX paths also work under long checkout/state paths on Darwin.
    previous = Path.cwd()
    os.chdir(state)
    try:
        yield
    finally:
        os.chdir(previous)


def remove_state(state: Path) -> None:
    (state / "control.sock").unlink(missing_ok=True)
    (state / "boot").unlink(missing_ok=True)
    state.rmdir()


def serve(pid: int, script: str, component: str, state: Path) -> int:
    tree = None
    completed = False
    startup_deadline = time.monotonic() + 5
    try:
        with state_directory(state), socket.socket(socket.AF_UNIX) as server:
            server.bind("control.sock")
            server.listen(4)
            server.settimeout(.25)
            while True:
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    if tree is None and time.monotonic() >= startup_deadline:
                        completed = True
                        return 1
                    if tree is not None and tree.finished():
                        completed = True
                        return 0
                    continue
                with connection:
                    connection.settimeout(10)
                    try:
                        request = connection.recv(4, socket.MSG_WAITALL)
                    except OSError:
                        continue
                    if request != b"stop":
                        continue
                    try:
                        if tree is None:
                            tree = DevelopmentProcessTree(pid, lambda: subprocess.run(
                                ["/bin/bash", script, "check-owner", component, str(pid)],
                                check=False, stdout=subprocess.DEVNULL, timeout=5,
                            ).returncode == 0)
                        tree.stop()
                        completed = True
                        response = b"ok"
                    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                        response = str(exc).encode(errors="replace")[:2048]
                    try:
                        connection.sendall(response)
                    except OSError:
                        pass  # A disconnected caller does not release native custody.
                    if completed:
                        return 0
                    if tree is None:
                        # A failed capture cannot prove what survived a lost root.
                        # Keep the marker even though no native handles were acquired.
                        return 1
    finally:
        if tree is not None:
            tree.close()
        if completed:
            remove_state(state)
    return 1


def request_stop(state: Path) -> None:
    deadline = time.monotonic() + 5
    with state_directory(state), socket.socket(socket.AF_UNIX) as client:
        while True:
            try:
                client.connect("control.sock")
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("cleanup supervisor unavailable; custody cannot be recovered from a PID; "
                                       "pending state can be cleared by Stop after the next system restart")
                time.sleep(.05)
        client.settimeout(10)
        client.sendall(b"stop")
        response = client.recv(2048)
        if response != b"ok":
            raise RuntimeError(response.decode(errors="replace") or "cleanup supervisor disconnected")
    deadline = time.monotonic() + 5
    while state.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("cleanup completed but supervisor bookkeeping remains")
        time.sleep(.01)


def main() -> int:
    try:
        action, pid_text, script, component, state_text = sys.argv[1:]
        if action not in {"stop", "retry", "serve"} or component not in {"backend", "frontend"}:
            raise ValueError("invalid development process stop arguments")
        state = Path(state_text).absolute()
        if action == "serve":
            return serve(int(pid_text), script, component, state)
        if action == "stop":
            try:
                state.mkdir(mode=0o700, parents=True)
            except FileExistsError:
                pass  # Concurrent/repeated stop uses the already pinned tree.
            else:
                try:
                    (state / "boot").write_text(boot_id())
                    subprocess.Popen(
                        [sys.executable, str(Path(__file__).resolve()), "serve", pid_text, script, component, str(state)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except (OSError, RuntimeError):
                    remove_state(state)
                    raise
        elif not state.exists():
            return 0
        with state_directory(state):
            recorded_boot = UUID((state / "boot").read_text())
            if recorded_boot != UUID(boot_id()):
                # Processes and native handles from an earlier boot cannot survive.
                remove_state(state)
                return 0
        request_stop(state)
        return 0
    except (OSError, RuntimeError, ValueError, IndexError) as exc:
        print(f"development stop: {exc}; PID bookkeeping retained; retry the same stop command", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
