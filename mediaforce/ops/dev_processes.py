"""Retain native development process custody across failed stop commands."""

from contextlib import contextmanager
from collections.abc import Iterator
import ctypes
import fcntl
import importlib.util
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4

if __package__:
    from mediaforce.core.dev_processes import DevelopmentCustodyLostError, DevelopmentProcessTree
else:
    spec = importlib.util.spec_from_file_location(
        "_mediaforce_dev_custody", Path(__file__).resolve().parents[1] / "core/dev_processes.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load development custody gateway")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    DevelopmentProcessTree = module.DevelopmentProcessTree
    DevelopmentCustodyLostError = module.DevelopmentCustodyLostError


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


def dispose_directory(directory: Path) -> None:
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("unsafe development disposal directory")
    entries = list(directory.iterdir())
    if any(entry.name not in {"control.sock", "boot", "error"} for entry in entries):
        raise RuntimeError("unexpected development disposal contents")
    for entry in entries:
        entry.unlink(missing_ok=True)
    directory.rmdir()


class DevelopmentStateChangedError(RuntimeError):
    pass


def remove_state(state: Path, *, expected_identity: tuple[int, int] | None = None) -> None:
    # Completion or previous-boot proof belongs to the caller. Retirement needs
    # no new directory allocation and never removes the receipt while active.
    disposed = state.with_name(f".{state.name}-removing-{uuid4()}")
    if expected_identity is None:
        state.rename(disposed)
    else:
        with state_lock(state):
            try:
                current = state.lstat()
            except FileNotFoundError as exc:
                raise DevelopmentStateChangedError("cleanup marker disappeared; state retained; retry Stop") from exc
            if (current.st_dev, current.st_ino) != expected_identity:
                raise DevelopmentStateChangedError("cleanup marker changed; replacement state retained; retry Stop")
            state.rename(disposed)
    try:
        dispose_directory(disposed)
    except (OSError, RuntimeError):
        pass  # Already inert; the next locked sweep retries bounded disposal.


def sweep_disposal(state: Path) -> None:
    pattern = re.compile(rf"\.{re.escape(state.name)}-(?:removing-[0-9a-f-]{{36}}|candidate-[a-z0-9_]{{8}})")
    attempted = 0
    for artifact in state.parent.iterdir():
        if not pattern.fullmatch(artifact.name):
            continue
        attempted += 1
        try:
            dispose_directory(artifact)
        except (OSError, RuntimeError):
            pass  # Preserve unfamiliar contents and symlinked/foreign directories.
        if attempted >= 32:
            break


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def record_error(state: Path, error: Exception) -> bytes:
    response = str(error).encode(errors="replace")[:2048]
    try:
        (state / "error").write_bytes(response)
    except OSError:
        pass  # Failure to record an error must not release native handles.
    return response


@contextmanager
def state_lock(state: Path) -> Iterator[None]:
    state.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(state) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def clear_previous_boot(state: Path) -> None:
    # Check and remove under the publication lock: a competing creator must
    # not replace an old receipt between this read and removal.
    with state_lock(state):
        sweep_disposal(state)
        if state.exists():
            with state_directory(state):
                recorded_boot = UUID((state / "boot").read_text())
                if recorded_boot != UUID(boot_id()):
                    remove_state(state)


def publish_state(state: Path) -> bool:
    # Publish a complete boot record in one rename, preserving uncertain state.
    with state_lock(state):
        sweep_disposal(state)
        if state.exists() or state.is_symlink():
            return False
        candidate = Path(tempfile.mkdtemp(prefix=f".{state.name}-candidate-", dir=state.parent))
        try:
            (candidate / "boot").write_text(str(UUID(boot_id())))
            with (candidate / "boot").open("rb") as receipt:
                os.fsync(receipt.fileno())
            sync_directory(candidate)
            candidate.rename(state)
            try:
                sync_directory(state.parent.resolve(strict=True))
            except BaseException:
                try:
                    remove_state(state)  # No supervisor has been launched.
                except (OSError, RuntimeError):
                    pass  # Retain the published marker if retirement also fails.
                raise
        finally:
            if candidate.exists():
                try:
                    dispose_directory(candidate)  # Unpublished; no retirement allocation.
                except (OSError, RuntimeError):
                    pass  # Preserve the original setup error and retry via the sweep.
    return True


def serve(pid: int, script: str, component: str, state: Path) -> int:
    tree = None
    completed = False
    startup_deadline = time.monotonic() + 5
    try:
        with state_directory(state), socket.socket(socket.AF_UNIX) as server:
            marker = state.lstat()
            marker_identity = marker.st_dev, marker.st_ino
            server.bind("control.sock")
            server.listen(4)
            server.settimeout(.25)
            while True:
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    if tree is None and time.monotonic() >= startup_deadline:
                        completed = True
                    if tree is not None or completed:
                        try:
                            if completed or tree.finished():
                                completed = True
                                remove_state(state, expected_identity=marker_identity)
                                return 0 if tree is not None else 1
                        except DevelopmentStateChangedError:
                            return 1  # Never record an old session's error in a replacement.
                        except DevelopmentCustodyLostError as exc:
                            record_error(state, exc)
                            return 1
                        except (OSError, RuntimeError, ValueError) as exc:
                            record_error(state, exc)
                    continue
                with connection:
                    connection.settimeout(10)
                    try:
                        with connection.makefile("rb") as stream:
                            request = stream.readline(64)
                        command, expected = request.decode("ascii").strip().split()
                        expected_pid = int(expected)
                    except (OSError, ValueError):
                        continue
                    if command != "stop":
                        continue
                    if expected_pid not in {0, pid}:
                        try:
                            connection.sendall(b"another development root has pending cleanup; retry Stop")
                        except OSError:
                            pass
                        continue
                    try:
                        if tree is None:
                            def owns_root() -> bool:
                                result = subprocess.run(
                                    ["/bin/bash", script, "check-owner", component, str(pid)],
                                    check=False, stdout=subprocess.DEVNULL, timeout=5,
                                )
                                if result.returncode not in {0, 1}:
                                    raise RuntimeError("development process ownership unknown; preserving it")
                                return result.returncode == 0

                            tree = DevelopmentProcessTree(pid, owns_root)
                            completed = False
                        if not completed:
                            tree.stop()
                        completed = True
                        remove_state(state, expected_identity=marker_identity)
                        response = b"ok"
                    except DevelopmentStateChangedError as exc:
                        try:
                            connection.sendall(str(exc).encode(errors="replace")[:2048])
                        except OSError:
                            pass
                        return 1
                    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                        response = record_error(state, exc)
                    try:
                        connection.sendall(response)
                    except OSError:
                        pass  # A disconnected caller does not release native custody.
                    if response == b"ok":
                        return 0
                    if tree is None:
                        # A failed capture cannot prove what survived a lost root.
                        # Keep the marker even though no native handles were acquired.
                        return 1
    finally:
        if tree is not None:
            tree.close()
    return 1


def request_stop(state: Path, expected_pid: int) -> None:
    deadline = time.monotonic() + 5
    with state_directory(state), socket.socket(socket.AF_UNIX) as client:
        while True:
            try:
                client.connect("control.sock")
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if not state.exists():
                    raise RuntimeError("cleanup session ended while connecting; retry Stop")
                if time.monotonic() >= deadline:
                    previous_error = ""
                    if (state / "error").is_file():
                        with (state / "error").open("rb") as error_file:
                            previous_error = error_file.read(2048).decode(errors="replace")
                    raise RuntimeError(f"{previous_error}; cleanup supervisor unavailable; "
                                       "custody cannot be recovered from a PID; "
                                       "pending state can be cleared by Stop after the next system restart")
                time.sleep(.05)
        client.settimeout(10)
        client.sendall(f"stop {expected_pid}\n".encode("ascii"))
        response = client.recv(2048)
        if response != b"ok":
            if not state.exists():
                raise RuntimeError("cleanup session ended before replying; retry Stop")
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
        pid = int(pid_text)
        if action != "retry" and (pid <= 1 or pid == os.getpid()):
            raise ValueError("invalid development process root")
        state = Path(state_text).absolute()
        if action == "serve":
            return serve(pid, script, component, state)
        clear_previous_boot(state)
        if action == "stop":
            if publish_state(state):
                try:
                    subprocess.Popen(
                        [sys.executable, str(Path(__file__).resolve()), "serve", pid_text, script, component, str(state)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except (OSError, RuntimeError) as startup_error:
                    try:
                        remove_state(state)
                    except (OSError, RuntimeError) as retirement_error:
                        raise RuntimeError(f"{str(startup_error)[:1024]}; cleanup state retirement also failed: "
                                           f"{str(retirement_error)[:1024]}") from startup_error
                    raise
        elif not state.exists():
            return 0
        request_stop(state, pid if action == "stop" else 0)
        return 0
    except (OSError, RuntimeError, ValueError, IndexError) as exc:
        print(f"development stop: {exc}; PID bookkeeping retained; retry Stop if custody is retained; retry cannot restore lost custody", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
