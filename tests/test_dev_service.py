import hashlib
import json
import os
from pathlib import Path
import shutil
import select
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from threading import Event, Thread

import pytest

from mediaforce.ops.login_item import BOOTOUT_ACCEPTED_EXIT_CODES, LOGIN_ITEM_LABEL


def prepare_dev_service(
    tmp_path: Path, service: str, running: str, process: str,
) -> tuple[Path, Path, Path, Path, dict[str, str]]:
    repo = tmp_path / "checkout with spaces"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    (repo / "frontend").mkdir()
    script = scripts / "mediaforce-dev.sh"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/mediaforce-dev.sh", script)
    source = Path(__file__).resolve().parents[1]
    for package in ("mediaforce", "mediaforce/core", "mediaforce/ops"):
        (repo / package).mkdir(exist_ok=True)
        (repo / package / "__init__.py").write_text("")
    for module in ("mediaforce/core/_process_deadline.py", "mediaforce/core/process_control.py", "mediaforce/core/dev_processes.py", "mediaforce/ops/dev_processes.py", "mediaforce/ops/dev_frontend.py"):
        shutil.copyfile(source / module, repo / module)
    if service == "symlink":
        alias = tmp_path / "checkout alias"
        alias.symlink_to(repo, target_is_directory=True)
        script = alias / "scripts/mediaforce-dev.sh"
    home = tmp_path / "home"
    state = home / "Library/Application Support/mediaforce"
    state.mkdir(parents=True)
    lock = state / "mediaforce-web.lock"
    lock_bytes = b'{"owner":"preserve this runtime"}\n'
    lock.write_bytes(lock_bytes)
    dev_state = state / "development" / hashlib.sha256(str(repo).encode()).hexdigest()
    dev_state.mkdir(parents=True)
    pid_file = dev_state / "mediaforce-web.pid"
    pid_file.write_text("")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "calls.jsonl"
    stub = "#!" + sys.executable + "\n" + '''
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
if name == "ps":
    sys.argv = [argument for argument in sys.argv if argument != "-ww"]
with Path(os.environ["DEV_TEST_LOG"]).open("a") as output:
    output.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
if name == "shasum":
    import hashlib
    print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest() + "  -")
elif name == "id":
    print(4242)
elif name == "launchctl" and sys.argv[1] == "print":
    if Path(os.environ["DEV_TEST_LOG"]).with_suffix(".unloaded").exists():
        sys.exit(1)
    if sys.argv[2] != "gui/4242/" + os.environ["DEV_TEST_LABEL"]:
        sys.exit(1)
    service = os.environ["DEV_TEST_SERVICE"]
    if service == "absent":
        sys.exit(1)
    repo = os.environ["DEV_TEST_REPO"] if service not in {"other_checkout", "other_directory"} else "/other/checkout"
    if service == "sibling_checkout":
        repo += "-sibling"
    elif service == "nested_checkout":
        repo += "/nested"
    program = repo + "/.venv/bin/mediaforce-web" if service != "other_program" else "/other/mediaforce-web-wrapper"
    if service == "other_directory":
        program = os.environ["DEV_TEST_REPO"] + "/.venv/bin/mediaforce-web"
    print(f"\\tworking directory = {repo}\\n\\tprogram = {program}\\narguments = {{\\nmediaforce-web\\n}}")
    if "DEV_TEST_SERVICE_PID" in os.environ:
        print("pid = " + os.environ["DEV_TEST_SERVICE_PID"])
elif name == "launchctl" and sys.argv[1] == "bootout":
    mode = os.environ.get("DEV_TEST_SHUTDOWN", "immediate")
    if mode == "failed":
        sys.exit(5)
    root = os.environ.get("DEV_TEST_SERVICE_PID", os.environ.get("DEV_TEST_BOOTOUT_ROOT"))
    if root:
        import signal
        os.kill(int(root), signal.SIGTERM)
    if mode not in {"delayed_unload", "stuck_unload"}:
        Path(os.environ["DEV_TEST_LOG"]).with_suffix(".unloaded").touch()
    sys.exit(int(os.environ.get("DEV_TEST_BOOTOUT_STATUS", "0")))
elif name == "ps" and "DEV_TEST_TREE" in os.environ:
    rows = json.loads(Path(os.environ["DEV_TEST_TREE"]).read_text())
    rows = [row for row in rows if not row.get("finished") or not Path(row["finished"]).exists()]
    started = os.environ.get("DEV_TEST_STARTED")
    if started and Path(started).exists():
        rows.append({"pid": json.loads(Path(started).read_text())["pid"], "parent": 0,
                     "command": os.environ.get("DEV_TEST_STARTED_COMMAND", os.environ["DEV_TEST_REPO"] + "/.venv/bin/mediaforce-web"),
                     "cwd": json.loads(Path(started).read_text()).get("cwd", os.environ["DEV_TEST_REPO"])})
    if sys.argv[1:] == ["-axo", "pid=,ppid="]:
        for row in rows:
            print(row["pid"], row["parent"])
    elif sys.argv[1] == "-p":
        row = next((row for row in rows if str(row["pid"]) == sys.argv[2]), None)
        if row:
            print(row["command"] if sys.argv[-1] == "command=" else row["parent"])
elif name == "ps" and sys.argv[1:3] == ["-p", os.environ["DEV_TEST_PID"]]:
    if sys.argv[-1] == "command=":
        process = os.environ["DEV_TEST_PROCESS"]
        repo = os.environ["DEV_TEST_REPO"] if process != "foreign" else "/foreign/checkout"
        if process == "sibling":
            repo += "-sibling"
        elif process == "nested":
            repo += "/nested"
        command = repo + "/.venv/bin/mediaforce-web"
        if process == "other_program":
            command += "-wrapper"
        elif process == "python_owned":
            command = sys.executable + " " + command + " --no-reload"
        print(command)
    elif sys.argv[-1] == "ppid=":
        print(0)
elif name == "lsof" and "-d" in sys.argv:
    rows = json.loads(Path(os.environ["DEV_TEST_TREE"]).read_text()) if "DEV_TEST_TREE" in os.environ else []
    rows = [row for row in rows if not row.get("finished") or not Path(row["finished"]).exists()]
    started = os.environ.get("DEV_TEST_STARTED")
    if started and Path(started).exists():
        rows.append({"pid": json.loads(Path(started).read_text())["pid"], "cwd": json.loads(Path(started).read_text()).get("cwd", os.environ["DEV_TEST_REPO"])})
    row = next((row for row in rows if str(row["pid"]) == sys.argv[sys.argv.index("-p") + 1]), None)
    if row and row.get("cwd", os.environ["DEV_TEST_REPO"]):
        print("p" + str(row["pid"]) + "\\n" + "n" + row.get("cwd", os.environ["DEV_TEST_REPO"]))
    else:
        sys.exit(1)
elif name == "lsof" and "DEV_TEST_TREE" in os.environ:
    rows = json.loads(Path(os.environ["DEV_TEST_TREE"]).read_text())
    port = sys.argv[2].removeprefix("-tiTCP:")
    for row in rows:
        if row.get("port") == port and not Path(row["finished"]).exists():
            print(row["pid"])
    started = os.environ.get("DEV_TEST_STARTED")
    if started and Path(started).exists() and Path(started).with_suffix(".listening").exists():
        backend = json.loads(Path(started).read_text())
        if backend["args"][backend["args"].index("--port") + 1] == port:
            print(backend["pid"])
elif name == "lsof" and os.environ["DEV_TEST_RUNNING"] == "listener":
    seen = Path(os.environ["DEV_TEST_LISTENER_SEEN"])
    if not seen.exists():
        seen.touch()
        print(os.environ["DEV_TEST_PID"])
elif name == "python3":
    assert sys.argv[1] == "-c"
    assert Path(sys.argv[-1]) == Path(os.environ["DEV_TEST_LOCK"])
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
elif name == "uv":
    assert sys.argv[1:4] == ["run", "--no-sync", "--project"]
    if sys.argv[6].endswith("/dev_frontend.py") and "DEV_TEST_READER_EXIT" in os.environ:
        print("fixture Python startup failed", file=sys.stderr)
        sys.exit(int(os.environ["DEV_TEST_READER_EXIT"]))
    if os.environ.get("DEV_TEST_CUSTODY_FAILURE"):
        print("injected custody unavailable; PID bookkeeping retained", file=sys.stderr)
        sys.exit(1)
    os.environ["PYTHONPATH"] = sys.argv[4]
    if sys.argv[6].endswith("/dev_frontend.py"):
        import runpy
        module = runpy.run_path(sys.argv[6])
        native_arguments = module["process_arguments"]
        if os.environ.get("DEV_TEST_ARGUMENT_FAILURE"):
            def read_arguments(pid):
                raise PermissionError("injected native argv refusal")
            module["main"].__globals__["process_arguments"] = read_arguments
        elif "DEV_TEST_TREE" in os.environ:
            rows = json.loads(Path(os.environ["DEV_TEST_TREE"]).read_text())
            def read_arguments(pid):
                row = next((row for row in rows if row["pid"] == pid), None)
                if not row or "command" not in row:
                    return native_arguments(pid)
                return row.get("argv", [])
            module["main"].__globals__["process_arguments"] = read_arguments
        sys.argv = sys.argv[6:]
        sys.exit(module["main"]())
    os.execv(sys.executable, [sys.executable, *sys.argv[6:]])
elif name == "dirname":
    print(Path(sys.argv[1]).parent)
elif name == "sed":
    if "-n" in sys.argv:
        for line in sys.stdin:
            if line.startswith("n"):
                print(line[1:].rstrip("\\n"))
    else:
        print(sys.stdin.read().strip())
elif name == "awk":
    if "-v" in sys.argv:
        parent = sys.argv[sys.argv.index("-v") + 1].split("=", 1)[1]
        for line in sys.stdin:
            fields = line.split()
            if len(fields) == 2 and fields[1] == parent:
                print(fields[0])
    else:
        for line in sys.stdin:
            if line.split():
                print(line.split()[0])
elif name == "sort":
    print("\\n".join(sorted(set(sys.stdin.read().splitlines()))))
elif name == "paste":
    print(",".join(sys.stdin.read().splitlines()))
elif name == "tr":
    print(sys.stdin.read().lower(), end="")
elif name == "sleep" and "DEV_TEST_TREE" in os.environ:
    import time
    if "DEV_TEST_SHUTDOWN" in os.environ:
        ticks = Path(os.environ["DEV_TEST_LOG"]).with_suffix(".ticks")
        count = int(ticks.read_text()) + 1 if ticks.exists() else 1
        ticks.write_text(str(count))
        mode = os.environ["DEV_TEST_SHUTDOWN"]
        if count == 2 and mode == "delayed_unload":
            Path(os.environ["DEV_TEST_LOG"]).with_suffix(".unloaded").touch()
        if count == 4 and "DEV_TEST_SERVICE_CLEANUP_FD" in os.environ and mode not in {"stuck_process", "failed"}:
            os.write(int(os.environ["DEV_TEST_SERVICE_CLEANUP_FD"]), b"done")
        if count == 6 and mode == "delayed_parent":
            os.write(int(os.environ["DEV_TEST_SERVICE_CLEANUP_FD"]), b"done")
        if count == 7 and mode != "startup_timeout":
            Path(os.environ["DEV_TEST_LOG"]).with_suffix(".startup-ready").touch()
        time.sleep(0.05)
    else:
        time.sleep(float(sys.argv[1]))
elif name == "mkdir":
    target = Path(sys.argv[-1])
    assert target.resolve().is_relative_to(Path(os.environ["HOME"]).resolve())
    target.mkdir(parents=True, exist_ok=True)
elif name == "nohup":
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
elif name == "rm":
    for argument in sys.argv[1:]:
        if argument.startswith("-"):
            continue
        target = Path(argument)
        assert target.resolve().is_relative_to(Path(os.environ["HOME"]).resolve())
        target.unlink(missing_ok=True)
elif name not in {"launchctl", "ps", "lsof", "sleep"}:
    raise AssertionError(name)
'''
    for command in ("shasum", "id", "launchctl", "ps", "lsof", "python3", "uv", "sleep", "dirname", "sed", "awk", "sort", "paste", "tr", "rm", "mkdir", "nohup"):
        binary = binaries / command
        binary.write_text(stub)
        binary.chmod(0o755)
    environment = {
        "PATH": str(binaries),
        "HOME": str(home),
        "DEV_TEST_LOG": str(log),
        "DEV_TEST_LABEL": LOGIN_ITEM_LABEL,
        "DEV_TEST_SERVICE": "matching" if service == "symlink" else service,
        "DEV_TEST_REPO": str(repo),
        "DEV_TEST_RUNNING": running,
        "DEV_TEST_PROCESS": process,
        "DEV_TEST_LOCK": str(lock),
        "DEV_TEST_LISTENER_SEEN": str(tmp_path / "listener-seen"),
        "MEDIAFORCE_WEB_HOST": "127.0.0.1",
        "MEDIAFORCE_WEB_PORT": "8777",
    }
    return script, lock, pid_file, log, environment


@pytest.mark.parametrize("service,running,process", [
    (service, "idle", "owned") for service in (
        "matching", "symlink", "other_checkout", "other_directory", "sibling_checkout", "nested_checkout", "other_program", "absent",
    )
] + [
    ("absent", running, process) for running in ("pid_file", "listener", "lock")
    for process in ("owned", "python_owned", "foreign", "sibling", "nested", "other_program")
])
def test_stop_unloads_only_this_checkout_and_preserves_runtime_lock(
    tmp_path: Path, service: str, running: str, process: str,
) -> None:
    script, lock, pid_file, log, environment = prepare_dev_service(tmp_path, service, running, process)
    lock_bytes = lock.read_bytes()

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        environment["DEV_TEST_PID"] = str(child.pid)
        if running == "pid_file":
            pid_file.write_text(str(child.pid))
        elif running == "lock":
            lock_bytes = json.dumps({"pid": child.pid, "owner": "runtime fixture"}).encode()
            lock.write_bytes(lock_bytes)
        result = subprocess.run(
            ["/bin/bash", str(script), "stop", "backend"], cwd=tmp_path,
            env=environment, capture_output=True, text=True, timeout=10,
        )
        if running == "idle" or process not in {"owned", "python_owned"}:
            assert child.poll() is None
        else:
            assert child.wait(timeout=5) != 0
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)

    assert result.returncode == (1 if service == "other_directory" else 0), result.stderr
    assert lock.read_bytes() == lock_bytes
    if service != "other_directory":
        assert not pid_file.exists()
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    target = "gui/4242/" + LOGIN_ITEM_LABEL
    assert ["launchctl", "print", target] in calls
    bootouts = [call for call in calls if call[:2] == ["launchctl", "bootout"]]
    assert bootouts == ([["launchctl", "bootout", target]] if service in {"matching", "symlink"} else [])


@pytest.mark.parametrize("service,action,process", [
    (service, action, process)
    for service in ("matching", "sibling_checkout", "other_program", "absent")
    for action, process in (("start", "owned"), ("start", "foreign"), ("restart", "foreign"))
])
def test_start_and_restart_keep_retained_runtime_lock(
    tmp_path: Path, action: str, process: str, service: str,
) -> None:
    script, lock, _, log, environment = prepare_dev_service(tmp_path, service, "listener", process)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        environment["DEV_TEST_PID"] = str(child.pid)
        lock_bytes = json.dumps({"pid": child.pid, "owner": "retained runtime"}).encode()
        lock.write_bytes(lock_bytes)
        # Keep the foreign listener visible across both ownership queries in start.
        if process == "foreign":
            table = tmp_path / "process-table.json"
            table.write_text(json.dumps([{
                "pid": child.pid, "parent": 0, "command": "/foreign/checkout/.venv/bin/mediaforce-web",
                "port": "8777", "finished": str(tmp_path / "never-finished"),
            }]))
            environment["DEV_TEST_TREE"] = str(table)
        result = subprocess.run(
            ["/bin/bash", str(script), action, "backend"], cwd=tmp_path,
            env=environment, capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == (0 if process == "owned" else 1), result.stderr
        assert ("backend: running" in result.stdout) if process == "owned" else ("refusing to start" in result.stderr)
        assert child.poll() is None
        assert lock.read_bytes() == lock_bytes
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        bootouts = [call for call in calls if call[:2] == ["launchctl", "bootout"]]
        target = "gui/4242/" + LOGIN_ITEM_LABEL
        assert bootouts == ([["launchctl", "bootout", target]] if service == "matching" else [])
    finally:
        child.kill()
        child.wait(timeout=5)


def test_start_reuses_owned_listener_without_a_pid_file_or_lock_pid(tmp_path: Path) -> None:
    script, lock, _, log, environment = prepare_dev_service(tmp_path, "absent", "listener", "owned")
    lock_bytes = lock.read_bytes()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        environment["DEV_TEST_PID"] = str(child.pid)
        result = subprocess.run(
            ["/bin/bash", str(script), "start", "backend"], cwd=tmp_path,
            env=environment, capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert f"listener {child.pid}" in result.stdout
        assert child.poll() is None
        assert lock.read_bytes() == lock_bytes
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert any(call[0] == "lsof" for call in calls)
        assert not any(call[0] == "nohup" for call in calls)
    finally:
        child.kill()
        child.wait(timeout=5)


@dataclass
class ProcessTree:
    root: subprocess.Popen[bytes]
    child_pid: int
    finished: Path
    child_finished: Path
    cleanup_writer: int
    lifetime_reader: int
    reaper: Thread
    worker_pid: int | None = None
    worker_finished: Path | None = None


def assert_tree_stopped(tree: ProcessTree) -> None:
    returncode = tree.root.wait(timeout=5)
    # Native cleanup signals deepest children first; the parent can finish or
    # receive SIGTERM during interpreter teardown. EOF still proves every child exited.
    assert returncode in {0, -signal.SIGTERM, -signal.SIGKILL}
    if returncode == 0:
        completion = json.loads(tree.finished.read_text())
        assert completion["child_returncode"] < 0
    assert select.select([tree.lifetime_reader], [], [], 5)[0], "descendant survived the stop"
    assert os.read(tree.lifetime_reader, 1) == b""


def assert_root_running(tree: ProcessTree) -> None:
    assert tree.root.stdout is not None
    assert not select.select([tree.root.stdout], [], [], 0.1)[0], "fixture root exited"


def finish_process_tree(tree: ProcessTree) -> None:
    try:
        try:
            os.write(tree.cleanup_writer, b"done" * 2)
        except BrokenPipeError:
            pass
        tree.root.wait(timeout=5)
        tree.reaper.join(timeout=5)
        assert not tree.reaper.is_alive(), "fixture reaper still running"
        assert select.select([tree.lifetime_reader], [], [], 5)[0], "fixture tree still alive"
        assert os.read(tree.lifetime_reader, 1) == b""
    finally:
        os.close(tree.cleanup_writer)
        os.close(tree.lifetime_reader)
        if tree.root.stdout is not None:
            tree.root.stdout.close()
        if tree.root.stderr is not None:
            tree.root.stderr.close()


@contextmanager
def owned_process_trees() -> Iterator[list[ProcessTree]]:
    trees: list[ProcessTree] = []
    try:
        yield trees
    finally:
        with ExitStack() as cleanup:
            for tree in reversed(trees):
                cleanup.callback(finish_process_tree, tree)


@pytest.fixture
def process_trees() -> Iterator[list[ProcessTree]]:
    with owned_process_trees() as trees:
        yield trees


def start_process_tree(
    tmp_path: Path, trees: list[ProcessTree], name: str, *, ignore_sigterm: bool = False, wrapper: bool = False, hold_root_after_child: bool = False,
) -> ProcessTree:
    finished = tmp_path / f"{name}-finished.json"
    child_finished = tmp_path / f"{name}-child-finished"
    cleanup_reader, cleanup_writer = os.pipe()
    lifetime_reader, lifetime_writer = os.pipe()
    child_code = '''
import os
from pathlib import Path
import signal
import subprocess
import sys

worker = None
if sys.argv[4] == "wrapper":
    ready_reader, ready_writer = os.pipe()
    worker = subprocess.Popen(
        [sys.executable, "-c", sys.argv[6], sys.argv[1], sys.argv[2] + "-worker-finished", str(ready_writer), "normal"],
        pass_fds=(int(sys.argv[1]), int(sys.argv[5]), ready_writer),
    )
    os.close(ready_writer)
    assert os.read(ready_reader, 1) == b"r"
    os.close(ready_reader)
    Path(sys.argv[2] + "-worker-pid").write_text(str(worker.pid))

def terminate(signum, frame):
    Path(sys.argv[2]).touch()
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)
signal.signal(signal.SIGTERM, signal.SIG_IGN if sys.argv[4] == "stubborn" else terminate)
os.write(int(sys.argv[3]), b"ready")
os.close(int(sys.argv[3]))
if worker is not None:
    worker.wait()
else:
    os.read(int(sys.argv[1]), 4)
Path(sys.argv[2]).touch()
'''
    code = '''
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

cleanup_reader, lifetime_writer = int(sys.argv[2]), int(sys.argv[3])
ready_reader, ready_writer = os.pipe()
child = subprocess.Popen(
    [sys.executable, "-c", sys.argv[4], str(cleanup_reader), sys.argv[5], str(ready_writer), sys.argv[6],
     str(lifetime_writer), sys.argv[4]],
    pass_fds=(cleanup_reader, lifetime_writer, ready_writer), stdout=subprocess.DEVNULL,
)
os.close(ready_writer)
assert os.read(ready_reader, 1) == b"r"
os.close(ready_reader)
root_signaled = False
def finish(signum, frame):
    global root_signaled
    root_signaled = True
signal.signal(signal.SIGTERM, finish)
print(child.pid, flush=True)
result = child.wait()
if sys.argv[7] == "hold":
    os.read(cleanup_reader, 4)
Path(sys.argv[1]).write_text(json.dumps({"child_returncode": result, "root_signaled": root_signaled}))
'''
    root = subprocess.Popen(
        [sys.executable, "-c", code, str(finished), str(cleanup_reader), str(lifetime_writer), child_code,
         str(child_finished), "wrapper" if wrapper else "stubborn" if ignore_sigterm else "normal",
         "hold" if hold_root_after_child else "normal"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(cleanup_reader, lifetime_writer),
    )
    os.close(cleanup_reader)
    os.close(lifetime_writer)
    assert root.stdout is not None
    reaper = Thread(target=root.wait, name=f"{name}-fixture-reaper", daemon=True)
    tree = ProcessTree(root, 0, finished, child_finished, cleanup_writer, lifetime_reader, reaper)
    trees.append(tree)
    reaper.start()
    assert select.select([root.stdout], [], [], 5)[0], "fixture root not ready"
    tree.child_pid = int(root.stdout.readline())
    if wrapper:
        tree.worker_pid = int(Path(str(child_finished) + "-worker-pid").read_text())
        tree.worker_finished = Path(str(child_finished) + "-worker-finished")
    return tree


def test_fixture_reaps_a_completed_tree_while_the_command_is_still_running(
    tmp_path: Path, process_trees: list[ProcessTree],
) -> None:
    tree = start_process_tree(tmp_path, process_trees, "completed-service")
    os.write(tree.cleanup_writer, b"done")
    assert select.select([tree.lifetime_reader], [], [], 5)[0], "fixture descendants did not exit"
    assert os.read(tree.lifetime_reader, 1) == b""
    deadline = time.monotonic() + 5
    while tree.root.returncode is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert tree.root.returncode == 0, "completed fixture root was not reaped during the command"


def test_fixture_releases_later_trees_after_a_teardown_observation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    trees: list[ProcessTree] = []
    try:
        with pytest.raises(RuntimeError, match="injected wait observer failure"):
            with owned_process_trees() as trees:
                first = start_process_tree(tmp_path, trees, "first-tree")
                later = start_process_tree(tmp_path, trees, "later-tree")

                def unavailable_wait(timeout: float | None = None) -> int:
                    raise RuntimeError(f"injected wait observer failure with timeout {timeout}")

                monkeypatch.setattr(first.root, "wait", unavailable_wait)
        assert later.root.returncode == 0, "later fixture was not released after the failure"
        assert not later.reaper.is_alive()
    finally:
        for tree in trees:
            if tree.root.returncode is None:
                try:
                    os.write(tree.cleanup_writer, b"done" * 2)
                except OSError:
                    pass
            tree.reaper.join(timeout=5)


def test_foreign_root_exit_is_observed_before_reaper_status_publication(
    tmp_path: Path, process_trees: list[ProcessTree], monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = start_process_tree(tmp_path, process_trees, "foreign-root")
    status_received, publish_status = Event(), Event()
    handle_status = tree.root._handle_exitstatus

    def hold_status(status: int) -> None:
        status_received.set()
        assert publish_status.wait(5), "exit status publication was not released"
        handle_status(status)

    monkeypatch.setattr(tree.root, "_handle_exitstatus", hold_status)
    try:
        assert_root_running(tree)
        os.write(tree.cleanup_writer, b"done" * 2)
        assert status_received.wait(5), "fixture root did not exit"
        with pytest.raises(AssertionError, match="fixture root exited"):
            assert_root_running(tree)
    finally:
        publish_status.set()


@pytest.mark.parametrize("running", ["pid_file", "lock", "listener"])
@pytest.mark.parametrize("component", ["backend", "all"])
def test_stop_discovers_owned_parent_and_descendants_and_preserves_foreign_tree(
    tmp_path: Path, process_trees: list[ProcessTree], running: str, component: str,
) -> None:
    script, lock, pid_file, _, environment = prepare_dev_service(tmp_path, "sibling_checkout", running, "owned")
    owned = start_process_tree(tmp_path, process_trees, "owned")
    foreign = start_process_tree(tmp_path, process_trees, "foreign")
    rows = []
    for tree, repo in ((owned, environment["DEV_TEST_REPO"]), (foreign, "/foreign/checkout")):
        rows.extend([
            {"pid": tree.root.pid, "parent": 0, "command": repo + "/.venv/bin/mediaforce-web", "finished": str(tree.finished)},
            {"pid": tree.child_pid, "parent": tree.root.pid, "command": "uvicorn worker",
             "port": "8777", "finished": str(tree.child_finished)},
        ])
    frontend = None
    if component == "all":
        frontend = start_process_tree(tmp_path, process_trees, "frontend")
        rows.extend([
            {"pid": frontend.root.pid, "parent": 0, "command": "vite " + environment["DEV_TEST_REPO"] + "/frontend",
             "argv": ["vite", environment["DEV_TEST_REPO"] + "/frontend"],
             "cwd": environment["DEV_TEST_REPO"] + "/frontend", "finished": str(frontend.finished)},
            {"pid": frontend.child_pid, "parent": frontend.root.pid, "command": "vite worker",
             "argv": ["vite", "worker"], "finished": str(frontend.child_finished)},
        ])
        (pid_file.parent / "mediaforce-frontend.pid").write_text(str(frontend.root.pid))
    table = tmp_path / "process-table.json"
    table.write_text(json.dumps(rows))
    environment["DEV_TEST_TREE"] = str(table)
    if running == "pid_file":
        pid_file.write_text(str(owned.child_pid))
    lock_bytes = json.dumps({"pid": owned.child_pid if running == "lock" else foreign.child_pid}).encode()
    lock.write_bytes(lock_bytes)
    result = subprocess.run(
        ["/bin/bash", str(script), "stop", component], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert_tree_stopped(owned)
    assert_root_running(foreign)
    assert not foreign.finished.exists()
    if frontend is not None:
        assert_tree_stopped(frontend)
    assert lock.read_bytes() == lock_bytes
    assert not pid_file.exists()
    assert "unloaded launch agent" not in result.stdout


def test_stop_forces_stubborn_owned_tree_to_exit(
    tmp_path: Path, process_trees: list[ProcessTree],
) -> None:
    script, lock, pid_file, _, environment = prepare_dev_service(tmp_path, "absent", "pid_file", "owned")
    owned = start_process_tree(tmp_path, process_trees, "stubborn", ignore_sigterm=True)
    foreign = start_process_tree(tmp_path, process_trees, "foreign")
    table = tmp_path / "process-table.json"
    rows = []
    for tree, repo in ((owned, environment["DEV_TEST_REPO"]), (foreign, "/foreign/checkout")):
        rows.extend([
            {"pid": tree.root.pid, "parent": 0, "command": repo + "/.venv/bin/mediaforce-web",
             "finished": str(tree.finished)},
            {"pid": tree.child_pid, "parent": tree.root.pid, "command": "uvicorn worker",
             "finished": str(tree.child_finished)},
        ])
    table.write_text(json.dumps(rows))
    environment["DEV_TEST_TREE"] = str(table)
    pid_file.write_text(str(owned.child_pid))
    lock_bytes = lock.read_bytes()
    result = subprocess.run(
        ["/bin/bash", str(script), "stop", "backend"], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert_tree_stopped(owned)
    assert not owned.child_finished.exists()
    assert_root_running(foreign)
    assert not foreign.finished.exists()
    assert lock.read_bytes() == lock_bytes
    assert not pid_file.exists()


@pytest.mark.parametrize("action", ["restart", "start"])
def test_backend_start_replaces_only_its_checkout_pid_record_with_retained_lock(
    tmp_path: Path, process_trees: list[ProcessTree], action: str,
) -> None:
    script, lock, pid_file, log, environment = prepare_dev_service(tmp_path, "matching" if action == "restart" else "absent", "pid_file", "owned")
    owned = start_process_tree(tmp_path, process_trees, "restart")
    table = tmp_path / "process-table.json"
    table.write_text(json.dumps([
        {"pid": owned.root.pid, "parent": 0, "command": environment["DEV_TEST_REPO"] + "/.venv/bin/mediaforce-web" if action == "restart" else "browser helper",
         "finished": str(owned.finished)},
        {"pid": owned.child_pid, "parent": owned.root.pid, "command": "uvicorn worker",
         "finished": str(owned.child_finished)},
    ]))
    environment["DEV_TEST_TREE"] = str(table)
    if action == "restart":
        environment["DEV_TEST_SERVICE_PID"] = str(owned.root.pid)
        environment["DEV_TEST_SHUTDOWN"] = "immediate"
        environment["DEV_TEST_SERVICE_CLEANUP_FD"] = str(owned.cleanup_writer)
    pid_file.write_text(str(owned.child_pid))
    lock_bytes = json.dumps({"pid": owned.child_pid, "owner": "retained runtime"}).encode()
    lock.write_bytes(lock_bytes)
    started = tmp_path / "started.json"
    environment["DEV_TEST_STARTED"] = str(started)
    cleanup_reader, cleanup_writer = os.pipe()
    lifetime_reader, lifetime_writer = os.pipe()
    environment["DEV_TEST_START_CLEANUP_FD"] = str(cleanup_reader)
    binary = Path(environment["DEV_TEST_REPO"]) / ".venv/bin/mediaforce-web"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!" + sys.executable + "\n" + '''
import json
import os
from pathlib import Path
import sys

Path(os.environ["DEV_TEST_STARTED"]).write_text(json.dumps({"pid": os.getpid(), "args": sys.argv, "cwd": os.getcwd()}))
Path(os.environ["DEV_TEST_STARTED"]).with_suffix(".listening").touch()
os.read(int(os.environ["DEV_TEST_START_CLEANUP_FD"]), 4)
''')
    binary.chmod(0o755)
    try:
        result = subprocess.run(
            ["/bin/bash", str(script), action, "backend"], cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=30, pass_fds=(cleanup_reader, lifetime_writer, owned.cleanup_writer),
        )
        assert result.returncode == 0, result.stderr
        if action == "restart":
            assert owned.root.wait(timeout=5) == 0
            assert json.loads(owned.finished.read_text())["root_signaled"]
            assert select.select([owned.lifetime_reader], [], [], 5)[0]
            assert os.read(owned.lifetime_reader, 1) == b""
        else:
            assert_root_running(owned)
        new_backend = json.loads(started.read_text())
        assert int(pid_file.read_text()) == new_backend["pid"]
        assert new_backend["pid"] != owned.root.pid
        assert new_backend["args"][0] == str(binary)
        assert "backend: started" in result.stdout
        assert lock.read_bytes() == lock_bytes
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert [call for call in calls if call[:2] == ["launchctl", "bootout"]] == ([
            ["launchctl", "bootout", "gui/4242/" + LOGIN_ITEM_LABEL],
        ] if action == "restart" else [])
    finally:
        os.close(cleanup_reader)
        os.close(lifetime_writer)
        try:
            os.write(cleanup_writer, b"done")
        except BrokenPipeError:
            pass
        os.close(cleanup_writer)
        assert select.select([lifetime_reader], [], [], 5)[0], "started fixture backend still alive"
        assert os.read(lifetime_reader, 1) == b""
        os.close(lifetime_reader)


def frontend_tree_rows(tree: ProcessTree, checkout: str, arguments: list[str]) -> list[dict[str, object]]:
    return [
        {"pid": tree.root.pid, "parent": 0, "command": " ".join(arguments), "argv": arguments,
         "cwd": checkout if arguments[:2] == ["npm", "--prefix"] else checkout + "/frontend",
         "finished": str(tree.finished)},
        {"pid": tree.child_pid, "parent": tree.root.pid, "command": "vite worker", "argv": ["vite", "worker"],
         "cwd": checkout + "/frontend", "port": "4173", "finished": str(tree.child_finished)},
    ]


@pytest.mark.parametrize("checkout_kind,command_kind", [
    (checkout, command) for checkout in ("owned", "sibling", "nested", "foreign", "unavailable")
    for command in ("relative", "rewritten", "absolute", "interpreter", "vite", "debug_vite", "paused_vite")
])
@pytest.mark.parametrize("running", ["pid_file", "listener"])
def test_frontend_stop_requires_exact_checkout_and_stops_the_whole_tree(
    tmp_path: Path, process_trees: list[ProcessTree], checkout_kind: str, command_kind: str, running: str,
) -> None:
    script, lock, backend_pid_file, _, environment = prepare_dev_service(tmp_path, "absent", "idle", "owned")
    tree = start_process_tree(tmp_path, process_trees, "frontend")
    repo = environment["DEV_TEST_REPO"]
    checkout = {"owned": repo, "sibling": repo + "-sibling", "nested": repo + "/nested",
                "foreign": "/foreign/checkout", "unavailable": ""}[checkout_kind]
    arguments = {"relative": ["npm", "--prefix", "frontend", "run", "dev", "--", "--strictPort"],
                 "rewritten": ["npm run dev --host 127.0.0.1"],
                 "absolute": ["npm", "--prefix", checkout + "/frontend", "run", "dev", "--", "--strictPort"],
                 "interpreter": [sys.executable, "/fixture/bin/npm", "--prefix", checkout + "/frontend", "run", "dev"],
                 "vite": [sys.executable, checkout + "/frontend/node_modules/.bin/vite", "--strictPort"],
                 "debug_vite": ["node", "--inspect=0", checkout + "/frontend/node_modules/.bin/vite", "--strictPort"],
                 "paused_vite": ["node", "--inspect-brk=0", checkout + "/frontend/node_modules/.bin/vite", "--strictPort"]}[command_kind]
    rows = frontend_tree_rows(tree, checkout, arguments)
    if checkout_kind == "unavailable":
        for row in rows:
            row["cwd"] = ""
    table = tmp_path / "process-table.json"
    table.write_text(json.dumps(rows))
    environment["DEV_TEST_TREE"] = str(table)
    pid_file = backend_pid_file.parent / "mediaforce-frontend.pid"
    if running == "pid_file":
        pid_file.write_text(str(tree.child_pid))
    lock_bytes = lock.read_bytes()
    result = subprocess.run(
        ["/bin/bash", str(script), "stop", "frontend"], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    if checkout_kind == "owned":
        assert_tree_stopped(tree)
        assert not pid_file.exists()
    else:
        assert_root_running(tree)
        assert not tree.finished.exists()
        assert not tree.child_finished.exists()
        assert not select.select([tree.lifetime_reader], [], [], 0)[0]
        assert not pid_file.exists()
    assert lock.read_bytes() == lock_bytes


@pytest.mark.parametrize("action,component", [
    (action, component) for action in ("stop", "start", "restart") for component in ("frontend", "all")
])
def test_frontend_actions_preserve_foreign_trees_and_both_shared_pid_files(
    tmp_path: Path, process_trees: list[ProcessTree], action: str, component: str,
) -> None:
    script, lock, backend_pid_file, _, environment = prepare_dev_service(tmp_path, "absent", "idle", "owned")
    frontend = start_process_tree(tmp_path, process_trees, "foreign-frontend")
    backend = start_process_tree(tmp_path, process_trees, "foreign-backend")
    rows = frontend_tree_rows(frontend, environment["DEV_TEST_REPO"] + "-sibling", ["npm", "--prefix", "frontend", "run", "dev"])
    rows.append({"pid": backend.root.pid, "parent": 0, "command": "/foreign/.venv/bin/mediaforce-web",
                 "port": "8777", "finished": str(backend.finished)})
    table = tmp_path / "process-table.json"
    table.write_text(json.dumps(rows))
    environment["DEV_TEST_TREE"] = str(table)
    frontend_pid_file = lock.parent / "mediaforce-frontend.pid"
    backend_pid_file = lock.parent / "mediaforce-web.pid"
    frontend_pid_file.write_text(str(frontend.root.pid))
    backend_pid_file.write_text(str(backend.root.pid))
    lock_bytes = lock.read_bytes()
    result = subprocess.run(
        ["/bin/bash", str(script), action, component], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == (0 if action == "stop" else 1), result.stderr
    for tree in (frontend, backend):
        assert_root_running(tree)
        assert not tree.child_finished.exists()
        assert not select.select([tree.lifetime_reader], [], [], 0)[0]
    assert frontend_pid_file.read_text() == str(frontend.root.pid)
    assert backend_pid_file.read_text() == str(backend.root.pid)
    assert lock.read_bytes() == lock_bytes


@pytest.mark.parametrize("action,running", [("start", "pid_file"), ("start", "listener"), ("start", "stale"), ("start", "reused_pid"), ("start", "different_port"), ("restart", "pid_file"), ("restart", "legacy")])
def test_owned_frontend_reuse_and_restart(
    tmp_path: Path, process_trees: list[ProcessTree], action: str, running: str,
) -> None:
    script, lock, backend_pid_file, log, environment = prepare_dev_service(tmp_path, "absent", "idle", "owned")
    owned = start_process_tree(tmp_path, process_trees, "owned-frontend") if action == "restart" or running in {"pid_file", "listener"} else None
    foreign = start_process_tree(tmp_path, process_trees, "foreign-frontend")
    rows = frontend_tree_rows(owned, environment["DEV_TEST_REPO"], ["npm run dev"]) if owned is not None else []
    legacy_pid_file = None
    if running == "legacy":
        assert owned is not None
        rows[0]["cwd"] = environment["DEV_TEST_REPO"]
        legacy_pid_file = lock.parent / "mediaforce-frontend.pid"
        legacy_pid_file.write_text(str(owned.root.pid))
    foreign_checkout = environment["DEV_TEST_REPO"] + "-sibling"
    rows += frontend_tree_rows(foreign, foreign_checkout, ["npm run dev"])
    # The foreign checkout runs on another port.
    rows[-1].pop("port")
    foreign_pid_file = None
    shared_pid_file = None
    if running == "different_port":
        environment["MEDIAFORCE_FRONTEND_DEV_PORT"] = "4175"
        rows[-1]["port"] = "4174"
        foreign_pid_file = lock.parent / "development" / hashlib.sha256(foreign_checkout.encode()).hexdigest() / "mediaforce-frontend.pid"
        foreign_pid_file.parent.mkdir(parents=True)
        foreign_pid_file.write_text(str(foreign.root.pid))
        shared_pid_file = lock.parent / "mediaforce-frontend.pid"
        shared_pid_file.write_text(str(foreign.root.pid))
    table = tmp_path / "process-table.json"
    table.write_text(json.dumps(rows))
    environment["DEV_TEST_TREE"] = str(table)
    pid_file = backend_pid_file.parent / "mediaforce-frontend.pid"
    if running == "pid_file":
        assert owned is not None
        pid_file.write_text(str(owned.child_pid))
    elif running == "stale":
        pid_file.write_text("not-a-pid")
    elif running == "reused_pid":
        pid_file.write_text(str(foreign.root.pid))
    started = tmp_path / "started.json"
    environment["DEV_TEST_STARTED"] = str(started)
    environment["DEV_TEST_STARTED_COMMAND"] = "npm run dev"
    cleanup_reader, cleanup_writer = os.pipe()
    lifetime_reader, lifetime_writer = os.pipe()
    environment["DEV_TEST_START_CLEANUP_FD"] = str(cleanup_reader)
    npm = Path(environment["PATH"]) / "npm"
    npm.write_text("#!" + sys.executable + "\n" + '''
import json
import os
from pathlib import Path
import sys
Path(os.environ["DEV_TEST_STARTED"]).write_text(json.dumps({"pid": os.getpid(), "args": sys.argv, "cwd": os.getcwd()}))
os.read(int(os.environ["DEV_TEST_START_CLEANUP_FD"]), 4)
''')
    npm.chmod(0o755)
    lock_bytes = lock.read_bytes()
    try:
        result = subprocess.run(
            ["/bin/bash", str(script), action, "frontend"], cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=20, pass_fds=(cleanup_reader, lifetime_writer),
        )
        assert result.returncode == 0, result.stderr
        if action == "restart" or running in {"stale", "reused_pid", "different_port"}:
            if action == "restart":
                assert owned is not None
                if running == "legacy":
                    assert owned.root.wait(timeout=5) == 0
                    completion = json.loads(owned.finished.read_text())
                    assert not completion["root_signaled"]
                    assert completion["child_returncode"] < 0
                    assert select.select([owned.lifetime_reader], [], [], 5)[0]
                    assert os.read(owned.lifetime_reader, 1) == b""
                    assert legacy_pid_file is not None
                    assert legacy_pid_file.read_text() == str(owned.root.pid)
                else:
                    assert_tree_stopped(owned)
            new_frontend = json.loads(started.read_text())
            assert int(pid_file.read_text()) == new_frontend["pid"]
            assert new_frontend["cwd"] == environment["DEV_TEST_REPO"] + "/frontend"
            assert new_frontend["args"][1:5] == ["--prefix", environment["DEV_TEST_REPO"] + "/frontend", "run", "dev"]
            assert "frontend: started" in result.stdout
            assert new_frontend["pid"] != foreign.root.pid
            if foreign_pid_file is not None and shared_pid_file is not None:
                assert foreign_pid_file.read_text() == str(foreign.root.pid)
                assert shared_pid_file.read_text() == str(foreign.root.pid)
                assert new_frontend["args"][new_frontend["args"].index("--port") + 1] == environment["MEDIAFORCE_FRONTEND_DEV_PORT"]
        else:
            assert owned is not None
            assert_root_running(owned)
            assert not started.exists()
            assert "frontend: running" in result.stdout
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            assert not any(call[0] == "nohup" for call in calls)
        assert_root_running(foreign)
        assert not foreign.child_finished.exists()
        assert lock.read_bytes() == lock_bytes
    finally:
        os.close(cleanup_reader)
        os.close(lifetime_writer)
        try:
            os.write(cleanup_writer, b"done")
        except BrokenPipeError:
            pass
        os.close(cleanup_writer)
        assert select.select([lifetime_reader], [], [], 5)[0], "started fixture frontend still alive"
        assert os.read(lifetime_reader, 1) == b""
        os.close(lifetime_reader)


@pytest.mark.parametrize("component", ["frontend", "backend"])
def test_stop_owned_listener_preserves_foreign_pid_record(
    tmp_path: Path, process_trees: list[ProcessTree], component: str,
) -> None:
    script, lock, backend_pid_file, _, environment = prepare_dev_service(tmp_path, "absent", "idle", "owned")
    owned = start_process_tree(tmp_path, process_trees, "owned")
    foreign = start_process_tree(tmp_path, process_trees, "foreign")
    if component == "frontend":
        rows = frontend_tree_rows(owned, environment["DEV_TEST_REPO"], ["npm run dev"])
        rows += frontend_tree_rows(foreign, "/foreign/checkout", ["npm", "--prefix", "frontend", "run", "dev"])
        pid_file = lock.parent / "mediaforce-frontend.pid"
    else:
        rows = [
            {"pid": tree.root.pid, "parent": 0, "command": repo + "/.venv/bin/mediaforce-web",
             "port": "8777", "finished": str(tree.finished)}
            for tree, repo in ((owned, environment["DEV_TEST_REPO"]), (foreign, "/foreign/checkout"))
        ]
        rows.append({"pid": owned.child_pid, "parent": owned.root.pid, "command": "uvicorn worker",
                     "finished": str(owned.child_finished)})
        pid_file = lock.parent / "mediaforce-web.pid"
    table = tmp_path / "process-table.json"
    table.write_text(json.dumps(rows))
    environment["DEV_TEST_TREE"] = str(table)
    pid_file.write_text(str(foreign.root.pid))
    lock_bytes = lock.read_bytes()
    result = subprocess.run(
        ["/bin/bash", str(script), "stop", component], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert_tree_stopped(owned)
    assert_root_running(foreign)
    assert not foreign.child_finished.exists()
    assert pid_file.read_text() == str(foreign.root.pid)
    assert lock.read_bytes() == lock_bytes


@pytest.mark.parametrize("running", ["pid_file", "listener"])
def test_rewritten_npm_in_repo_root_cannot_claim_a_foreign_prefix(
    tmp_path: Path, process_trees: list[ProcessTree], running: str,
) -> None:
    script, lock, backend_pid_file, _, environment = prepare_dev_service(tmp_path, "absent", "idle", "owned")
    tree = start_process_tree(tmp_path, process_trees, "foreign-prefix")
    repo = environment["DEV_TEST_REPO"]
    rows = frontend_tree_rows(tree, repo + "-sibling", ["npm run dev"])
    rows[0]["cwd"] = repo
    table = tmp_path / "process-table.json"
    table.write_text(json.dumps(rows))
    environment["DEV_TEST_TREE"] = str(table)
    pid_file = backend_pid_file.parent / "mediaforce-frontend.pid"
    if running == "pid_file":
        pid_file.write_text(str(tree.root.pid))
    lock_bytes = lock.read_bytes()
    result = subprocess.run(
        ["/bin/bash", str(script), "stop", "frontend"], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert_root_running(tree)
    assert not tree.child_finished.exists()
    assert not select.select([tree.lifetime_reader], [], [], 0)[0]
    if running == "pid_file":
        assert not pid_file.exists()
    assert lock.read_bytes() == lock_bytes


@pytest.mark.parametrize("running", ["pid_file", "listener"])
def test_frontend_stop_climbs_through_an_intermediate_shell(
    tmp_path: Path, process_trees: list[ProcessTree], running: str,
) -> None:
    script, lock, backend_pid_file, _, environment = prepare_dev_service(tmp_path, "absent", "idle", "owned")
    tree = start_process_tree(tmp_path, process_trees, "frontend-wrapper", wrapper=True)
    assert tree.worker_pid is not None
    assert tree.worker_finished is not None
    repo = environment["DEV_TEST_REPO"]
    rows = [
        {"pid": tree.root.pid, "parent": 0, "command": "npm run dev", "argv": ["npm run dev"], "cwd": repo + "/frontend",
         "finished": str(tree.finished)},
        {"pid": tree.child_pid, "parent": tree.root.pid, "command": "sh -c vite dev", "argv": ["sh", "-c", "vite dev"], "cwd": repo + "/frontend",
         "finished": str(tree.child_finished)},
        {"pid": tree.worker_pid, "parent": tree.child_pid,
         "command": "node " + repo + "/frontend/node_modules/.bin/vite dev",
         "argv": ["node", repo + "/frontend/node_modules/.bin/vite", "dev"], "cwd": repo + "/frontend",
         "port": "4173", "finished": str(tree.worker_finished)},
    ]
    table = tmp_path / "process-table.json"
    table.write_text(json.dumps(rows))
    environment["DEV_TEST_TREE"] = str(table)
    pid_file = backend_pid_file.parent / "mediaforce-frontend.pid"
    if running == "pid_file":
        pid_file.write_text(str(tree.worker_pid))
    lock_bytes = lock.read_bytes()
    result = subprocess.run(
        ["/bin/bash", str(script), "stop", "frontend"], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert_tree_stopped(tree)
    assert tree.worker_finished.exists()
    assert not pid_file.exists()
    assert lock.read_bytes() == lock_bytes


@pytest.mark.parametrize("running", ["pid_file", "listener"])
@pytest.mark.parametrize("launcher_kind", ["terminal", "npm_argument", "node_npm_argument", "node_vite_argument", "debug_node_vite_argument"])
def test_frontend_stop_preserves_the_unrelated_launching_parent(
    tmp_path: Path, process_trees: list[ProcessTree], running: str, launcher_kind: str,
) -> None:
    script, lock, backend_pid_file, _, environment = prepare_dev_service(tmp_path, "absent", "idle", "owned")
    launcher = start_process_tree(tmp_path, process_trees, "terminal")
    owned = start_process_tree(tmp_path, process_trees, "owned-frontend")
    repo = environment["DEV_TEST_REPO"]
    arguments = {
        "terminal": ["-zsh"],
        "npm_argument": [sys.executable, "/fixture/shared-wrapper.py", "/fixture/bin/npm", "--prefix", repo + "/frontend", "run", "dev"],
        "node_npm_argument": ["node", "/fixture/shared-wrapper.js", "/fixture/bin/npm", "--prefix", repo + "/frontend", "run", "dev"],
        "node_vite_argument": ["node", "/fixture/shared-wrapper.js", repo + "/frontend/node_modules/.bin/vite", "dev"],
        "debug_node_vite_argument": ["node", "--inspect=0", "/fixture/shared-wrapper.js", repo + "/frontend/node_modules/.bin/vite", "dev"],
    }[launcher_kind]
    # Fake ps supplies this parent relationship; every PID is fixture-owned.
    rows = frontend_tree_rows(owned, repo, ["npm run dev"])
    rows[0]["parent"] = launcher.root.pid
    rows.extend([
        {"pid": launcher.root.pid, "parent": 0, "command": " ".join(arguments), "argv": arguments, "cwd": repo + "/frontend",
         "finished": str(launcher.finished)},
        {"pid": launcher.child_pid, "parent": launcher.root.pid, "command": "unrelated task", "cwd": repo + "/frontend",
         "finished": str(launcher.child_finished)},
    ])
    table = tmp_path / "process-table.json"
    table.write_text(json.dumps(rows))
    environment["DEV_TEST_TREE"] = str(table)
    pid_file = backend_pid_file.parent / "mediaforce-frontend.pid"
    if running == "pid_file":
        pid_file.write_text(str(owned.child_pid))
    lock_bytes = lock.read_bytes()
    result = subprocess.run(
        ["/bin/bash", str(script), "stop", "frontend"], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert_tree_stopped(owned)
    assert_root_running(launcher)
    assert not launcher.child_finished.exists()
    assert not select.select([launcher.lifetime_reader], [], [], 0)[0]
    assert not pid_file.exists()
    assert lock.read_bytes() == lock_bytes


@contextmanager
def fixture_backend(
    tmp_path: Path, environment: dict[str, str], *, shutdown_finished: Path | None = None,
    slow_start: bool = False,
) -> Iterator[tuple[Path, tuple[int, ...]]]:
    started = tmp_path / "new-backend.json"
    cleanup_reader, cleanup_writer = os.pipe()
    lifetime_reader, lifetime_writer = os.pipe()
    environment.update({
        "DEV_TEST_STARTED": str(started),
        "DEV_TEST_START_CLEANUP_FD": str(cleanup_reader),
        "DEV_TEST_REQUIRED_SHUTDOWN": str(shutdown_finished) if shutdown_finished else "",
        "DEV_TEST_SLOW_START": str(slow_start),
    })
    binary = Path(environment["DEV_TEST_REPO"]) / ".venv/bin/mediaforce-web"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!" + sys.executable + "\n" + '''
import json
import os
from pathlib import Path
import select
import sys

cleanup = int(os.environ["DEV_TEST_START_CLEANUP_FD"])
required = os.environ["DEV_TEST_REQUIRED_SHUTDOWN"]
if required:
    assert Path(required).exists(), "new backend launched before service shutdown completed"
Path(os.environ["DEV_TEST_STARTED"]).write_text(json.dumps({"pid": os.getpid(), "args": sys.argv}))
if os.environ["DEV_TEST_SLOW_START"] == "True":
    ready = Path(os.environ["DEV_TEST_LOG"]).with_suffix(".startup-ready")
    while not ready.exists():
        if select.select([cleanup], [], [], 0.01)[0]:
            sys.exit(0)
Path(os.environ["DEV_TEST_STARTED"]).with_suffix(".listening").touch()
os.read(cleanup, 4)
''')
    binary.chmod(0o755)
    try:
        yield started, (cleanup_reader, lifetime_writer)
    finally:
        os.close(cleanup_reader)
        os.close(lifetime_writer)
        try:
            os.write(cleanup_writer, b"done")
        except BrokenPipeError:
            pass
        os.close(cleanup_writer)
        assert select.select([lifetime_reader], [], [], 5)[0], "started backend survived fixture cleanup"
        assert os.read(lifetime_reader, 1) == b""
        os.close(lifetime_reader)


@pytest.mark.parametrize("startup", ["startup", "startup_timeout"])
def test_fresh_backend_polls_until_discovery_or_reports_bounded_failure(tmp_path: Path, startup: str) -> None:
    script, lock, pid_file, log, environment = prepare_dev_service(tmp_path, "absent", "idle", "owned")
    table = tmp_path / "empty-processes.json"
    table.write_text("[]")
    environment.update({"DEV_TEST_TREE": str(table), "DEV_TEST_SHUTDOWN": startup})
    lock_bytes = lock.read_bytes()
    with fixture_backend(tmp_path, environment, slow_start=True) as (started, descriptors):
        result = subprocess.run(
            ["/bin/bash", str(script), "start", "backend"], cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=30, pass_fds=descriptors,
        )
        assert result.returncode == (0 if startup == "startup" else 1), result.stderr
        assert lock.read_bytes() == lock_bytes
        if startup == "startup":
            assert int(pid_file.read_text()) == json.loads(started.read_text())["pid"]
            assert "backend: started" in result.stdout
        else:
            assert started.exists()
            assert not started.with_suffix(".listening").exists()
            assert "backend: started" not in result.stdout
            assert "backend: failed to start; see " in result.stderr
            result = subprocess.run(
                ["/bin/bash", str(script), "start", "backend"], cwd=tmp_path, env=environment,
                capture_output=True, text=True, timeout=30, pass_fds=descriptors,
            )
            assert result.returncode == 1
            assert "has not started listening" in result.stderr
            assert "backend: running" not in result.stdout
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert sum(call[0] == "sleep" for call in calls) > 1
        assert sum(call[0] == "nohup" for call in calls) == 1
        assert not any(call[:2] == ["launchctl", "bootout"] for call in calls)


@pytest.mark.parametrize("action", ["start", "restart"])
@pytest.mark.parametrize("shutdown", ["delayed_unload", "delayed_process", "delayed_parent", "stuck_unload", "stuck_process", "failed"])
@pytest.mark.parametrize("discovery", ["lock", "service_pid", "listener"])
def test_backend_waits_for_service_unload_and_process_completion(
    tmp_path: Path, process_trees: list[ProcessTree], action: str, shutdown: str, discovery: str,
) -> None:
    script, lock, pid_file, log, environment = prepare_dev_service(tmp_path, "matching", "idle", "owned")
    service = start_process_tree(tmp_path, process_trees, "service", hold_root_after_child=shutdown == "delayed_parent")
    foreign = start_process_tree(tmp_path, process_trees, "foreign-service")
    rows = [
        {"pid": service.root.pid, "parent": 0, "command": environment["DEV_TEST_REPO"] + "/.venv/bin/mediaforce-web",
         "finished": str(service.finished)},
        {"pid": service.child_pid, "parent": service.root.pid, "command": "uvicorn worker",
         "finished": str(service.child_finished), "port": "8777" if discovery == "listener" else ""},
    ]
    table = tmp_path / "service-processes.json"
    table.write_text(json.dumps(rows))
    environment.update({
        "DEV_TEST_TREE": str(table),
        "DEV_TEST_SERVICE_PID": str(service.root.pid),
        "DEV_TEST_SHUTDOWN": shutdown,
        "DEV_TEST_SERVICE_CLEANUP_FD": str(service.cleanup_writer),
    })
    lock_bytes = json.dumps({"pid": service.root.pid, "owner": "retained"}).encode() if discovery == "lock" else lock.read_bytes()
    lock.write_bytes(lock_bytes)
    succeeds = shutdown in {"delayed_unload", "delayed_process", "delayed_parent"}
    with fixture_backend(tmp_path, environment, shutdown_finished=service.finished, slow_start=True) as (started, descriptors):
        result = subprocess.run(
            ["/bin/bash", str(script), action, "backend"], cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=40,
            pass_fds=(*descriptors, service.cleanup_writer),
        )
        assert result.returncode == (0 if succeeds else 1), result.stderr
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert sum(call[:2] == ["launchctl", "bootout"] for call in calls) == 1
        assert_root_running(foreign)
        assert lock.read_bytes() == lock_bytes
        if succeeds:
            assert service.root.wait(timeout=5) == 0
            assert json.loads(service.finished.read_text())["root_signaled"]
            assert select.select([service.lifetime_reader], [], [], 5)[0]
            assert os.read(service.lifetime_reader, 1) == b""
            backend = json.loads(started.read_text())
            assert int(pid_file.read_text()) == backend["pid"]
            assert backend["pid"] != service.root.pid
            assert "backend: started" in result.stdout
            assert "backend: running" not in result.stdout
            assert "completed shutdown" in result.stdout
        else:
            assert not started.exists()
            assert not any(call[0] == "nohup" for call in calls)
            assert "refusing to continue" in result.stderr
            assert "backend: running" not in result.stdout
            assert "unloaded launch agent" not in result.stdout
            if shutdown in {"stuck_process", "failed"}:
                assert_root_running(service)


@pytest.mark.parametrize("replacement", ["fresh", "already_listening"])
def test_shutdown_timeout_retry_waits_for_old_process_then_clears_only_own_record(
    tmp_path: Path, process_trees: list[ProcessTree], replacement: str,
) -> None:
    script, lock, pid_file, log, environment = prepare_dev_service(tmp_path, "matching", "lock", "owned")
    service = start_process_tree(tmp_path, process_trees, "retry-service")
    foreign = start_process_tree(tmp_path, process_trees, "foreign-record")
    table = tmp_path / "processes.json"
    table.write_text(json.dumps([{
        "pid": service.root.pid, "parent": 0,
        "command": environment["DEV_TEST_REPO"] + "/.venv/bin/mediaforce-web", "finished": str(service.finished),
    }]))
    environment.update({
        "DEV_TEST_TREE": str(table), "DEV_TEST_SERVICE_PID": str(service.root.pid),
        "DEV_TEST_SHUTDOWN": "stuck_process", "DEV_TEST_SERVICE_CLEANUP_FD": str(service.cleanup_writer),
    })
    lock_bytes = json.dumps({"pid": service.root.pid, "owner": "retained"}).encode()
    lock.write_bytes(lock_bytes)
    foreign_key = hashlib.sha256(b"/foreign/checkout").hexdigest()
    foreign_record = pid_file.parent.parent / foreign_key / "backend-shutdown.pids"
    foreign_record.parent.mkdir()
    foreign_bytes = f"{foreign.root.pid}\n".encode()
    foreign_record.write_bytes(foreign_bytes)
    own_record = pid_file.parent / "backend-shutdown.pids"
    with fixture_backend(tmp_path, environment, shutdown_finished=service.finished) as (started, descriptors):
        for _ in range(2):
            result = subprocess.run(
                ["/bin/bash", str(script), "start", "backend"], cwd=tmp_path, env=environment,
                capture_output=True, text=True, timeout=40, pass_fds=(*descriptors, service.cleanup_writer),
            )
            assert result.returncode == 1
            assert "shutdown did not finish" in result.stderr
            assert "backend: running" not in result.stdout
            assert_root_running(service)
            assert str(service.root.pid) in own_record.read_text().splitlines()
            assert not started.exists()
        os.write(service.cleanup_writer, b"done" * 2)
        assert service.root.wait(timeout=5) == 0
        assert select.select([service.lifetime_reader], [], [], 5)[0]
        assert os.read(service.lifetime_reader, 1) == b""
        ready_backend = None
        if replacement == "already_listening":
            ready_backend = start_process_tree(tmp_path, process_trees, "ready-backend")
            rows = json.loads(table.read_text())
            rows.extend([
                {"pid": ready_backend.root.pid, "parent": 0,
                 "command": environment["DEV_TEST_REPO"] + "/.venv/bin/mediaforce-web", "finished": str(ready_backend.finished)},
                {"pid": ready_backend.child_pid, "parent": ready_backend.root.pid,
                 "command": "uvicorn worker", "port": "8777", "finished": str(ready_backend.child_finished)},
            ])
            table.write_text(json.dumps(rows))
            pid_file.write_text(str(ready_backend.root.pid))
        result = subprocess.run(
            ["/bin/bash", str(script), "start", "backend"], cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=30, pass_fds=(*descriptors, service.cleanup_writer),
        )
        assert result.returncode == 0, result.stderr
        if ready_backend is None:
            assert "backend: started" in result.stdout
        else:
            assert "backend: running" in result.stdout
            assert f"pid {ready_backend.root.pid}" in result.stdout
            assert_root_running(ready_backend)
            assert not started.exists()
        assert not own_record.exists()
        assert foreign_record.read_bytes() == foreign_bytes
        assert_root_running(foreign)
        assert lock.read_bytes() == lock_bytes
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert sum(call[:2] == ["launchctl", "bootout"] for call in calls) == 1
        assert sum(call[0] == "nohup" for call in calls) == (1 if ready_backend is None else 0)


@pytest.mark.parametrize("action,component", [("start", "all"), ("restart", "all"), ("stop", "backend")])
def test_failed_bootout_stops_combined_actions_without_launching_or_force_stopping(
    tmp_path: Path, action: str, component: str,
) -> None:
    script, lock, _, log, environment = prepare_dev_service(tmp_path, "matching", "idle", "owned")
    environment["DEV_TEST_SHUTDOWN"] = "failed"
    lock_bytes = lock.read_bytes()
    result = subprocess.run(
        ["/bin/bash", str(script), action, component], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1
    assert "could not unload login item" in result.stderr
    assert "unloaded launch agent" not in result.stdout
    assert lock.read_bytes() == lock_bytes
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert sum(call[:2] == ["launchctl", "bootout"] for call in calls) == 1
    assert not any(call[0] == "nohup" or call[:2] == ["ps", "-axo"] for call in calls)


@pytest.mark.parametrize("action,component", [("start", "all"), ("restart", "all"), ("stop", "backend")])
def test_mismatched_login_item_preserves_its_process_and_refuses_management(
    tmp_path: Path, action: str, component: str,
) -> None:
    script, lock, _, log, environment = prepare_dev_service(tmp_path, "other_directory", "lock", "owned")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        environment["DEV_TEST_PID"] = str(child.pid)
        lock_bytes = json.dumps({"pid": child.pid}).encode()
        lock.write_bytes(lock_bytes)
        result = subprocess.run(
            ["/bin/bash", str(script), action, component], cwd=tmp_path,
            env=environment, capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 1
        assert "different working directory; refusing to manage it" in result.stderr
        assert child.poll() is None
        assert lock.read_bytes() == lock_bytes
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert not any(call[:2] == ["launchctl", "bootout"] or call[0] == "nohup" for call in calls)
    finally:
        child.kill()
        child.wait(timeout=5)


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_idle_loaded_login_item_does_not_wait_on_independent_development_backend(
    tmp_path: Path, process_trees: list[ProcessTree], action: str,
) -> None:
    script, lock, pid_file, log, environment = prepare_dev_service(tmp_path, "matching", "lock", "owned")
    dev = start_process_tree(tmp_path, process_trees, "independent-dev")
    table = tmp_path / "independent-dev.json"
    table.write_text(json.dumps([
        {"pid": dev.root.pid, "parent": 0, "command": environment["DEV_TEST_REPO"] + "/.venv/bin/mediaforce-web",
         "finished": str(dev.finished)},
        {"pid": dev.child_pid, "parent": dev.root.pid, "command": "uvicorn worker", "port": "8777",
         "finished": str(dev.child_finished)},
    ]))
    environment["DEV_TEST_TREE"] = str(table)
    pid_file.write_text(str(dev.root.pid))
    lock_bytes = json.dumps({"pid": dev.child_pid, "owner": "independent development"}).encode()
    lock.write_bytes(lock_bytes)
    with fixture_backend(tmp_path, environment) as (started, descriptors):
        result = subprocess.run(
            ["/bin/bash", str(script), action, "backend"], cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=40, pass_fds=descriptors,
        )
        assert result.returncode == 0, result.stderr
        if action == "start":
            assert_root_running(dev)
            assert "backend: running" in result.stdout
            assert not started.exists()
        else:
            assert_tree_stopped(dev)
            if action == "restart":
                assert "backend: started" in result.stdout
                assert int(pid_file.read_text()) == json.loads(started.read_text())["pid"]
        assert lock.read_bytes() == lock_bytes
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert sum(call[:2] == ["launchctl", "bootout"] for call in calls) == 1


@pytest.mark.parametrize("root_command", ["reload", "uv"])
def test_lock_worker_reuse_compares_listener_and_lock_process_roots(
    tmp_path: Path, process_trees: list[ProcessTree], root_command: str,
) -> None:
    script, lock, _, log, environment = prepare_dev_service(tmp_path, "absent", "lock", "owned")
    dev = start_process_tree(tmp_path, process_trees, "lock-worker")
    backend = environment["DEV_TEST_REPO"] + "/.venv/bin/mediaforce-web"
    table = tmp_path / "lock-worker.json"
    table.write_text(json.dumps([
        {"pid": dev.root.pid, "parent": 0, "command": backend if root_command == "reload" else "uv run mediaforce-web",
         "finished": str(dev.finished)},
        {"pid": dev.child_pid, "parent": dev.root.pid, "command": "uvicorn worker" if root_command == "reload" else backend,
         "port": "8777", "finished": str(dev.child_finished)},
    ]))
    environment["DEV_TEST_TREE"] = str(table)
    lock_bytes = json.dumps({"pid": dev.child_pid, "owner": "lock worker"}).encode()
    lock.write_bytes(lock_bytes)
    result = subprocess.run(
        ["/bin/bash", str(script), "start", "backend"], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert f"pid {dev.child_pid}" in result.stdout
    assert_root_running(dev)
    assert lock.read_bytes() == lock_bytes
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(call[0] == "nohup" for call in calls)


@pytest.mark.parametrize("bootout_status", sorted(BOOTOUT_ACCEPTED_EXIT_CODES))
def test_dev_handoff_waits_after_every_exit_status_the_service_manager_accepts(
    tmp_path: Path, process_trees: list[ProcessTree], bootout_status: int,
) -> None:
    script, lock, _, log, environment = prepare_dev_service(tmp_path, "matching", "idle", "owned")
    service = start_process_tree(tmp_path, process_trees, "accepted-unload")
    table = tmp_path / "accepted-service.json"
    table.write_text(json.dumps([{
        "pid": service.root.pid, "parent": 0, "command": environment["DEV_TEST_REPO"] + "/.venv/bin/mediaforce-web",
        "finished": str(service.finished),
    }]))
    environment.update({
        "DEV_TEST_TREE": str(table), "DEV_TEST_SERVICE_PID": str(service.root.pid),
        "DEV_TEST_SHUTDOWN": "delayed_unload", "DEV_TEST_SERVICE_CLEANUP_FD": str(service.cleanup_writer),
        "DEV_TEST_BOOTOUT_STATUS": str(bootout_status),
    })
    lock_bytes = lock.read_bytes()
    with fixture_backend(tmp_path, environment, shutdown_finished=service.finished) as (_, descriptors):
        result = subprocess.run(
            ["/bin/bash", str(script), "start", "backend"], cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=30, pass_fds=(*descriptors, service.cleanup_writer),
        )
        assert result.returncode == 0, result.stderr
        assert service.root.wait(timeout=5) == 0
        assert "completed shutdown" in result.stdout
        assert "backend: started" in result.stdout
        assert lock.read_bytes() == lock_bytes
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert sum(call[:2] == ["launchctl", "bootout"] for call in calls) == 1


@pytest.mark.parametrize("discovery", ["pid_file", "listener"])
@pytest.mark.parametrize("action,component,reader_exit", [
    (action, component, "1") for action in ("start", "status", "stop", "restart")
    for component in ("frontend", "all")
] + [("stop", "frontend", status) for status in ("2", "127", "137")])
def test_frontend_reader_failure_preserves_live_tree_and_record(
    tmp_path: Path, process_trees: list[ProcessTree], discovery: str,
    action: str, component: str, reader_exit: str,
) -> None:
    script, _, backend_pid_file, _, env = prepare_dev_service(tmp_path, "absent", "idle", "owned")
    tree = start_process_tree(tmp_path, process_trees, "frontend")
    table = tmp_path / "process-table.json"
    rows = frontend_tree_rows(tree, env["DEV_TEST_REPO"], ["npm run dev"])
    backend = start_process_tree(tmp_path, process_trees, "backend") if component == "all" else None
    if backend is not None:
        rows.append({"pid": backend.root.pid, "parent": 0,
                     "command": env["DEV_TEST_REPO"] + "/.venv/bin/mediaforce-web",
                     "port": "8777", "finished": str(backend.finished)})
        backend_pid_file.write_text(str(backend.root.pid))
    table.write_text(json.dumps(rows))
    env.update(DEV_TEST_TREE=str(table), DEV_TEST_READER_EXIT=reader_exit)
    pid_file = backend_pid_file.with_name("mediaforce-frontend.pid")
    pid_file.write_text(str(tree.child_pid if discovery == "pid_file" else 999999))
    original = pid_file.read_bytes()
    result = subprocess.run(["/bin/bash", str(script), action, component], env=env,
                            capture_output=True, text=True, timeout=20)
    # status:all has its existing aggregate success convention on main.
    assert result.returncode != 0 or (action, component) == ("status", "all")
    assert "ownership unknown" in result.stderr
    assert not any(f"frontend: {outcome}" in result.stdout for outcome in ("running", "started", "stopped"))
    assert tree.root.poll() is None
    assert not tree.child_finished.exists()
    assert pid_file.read_bytes() == original
    if backend is not None:
        assert backend.root.poll() is None
        assert not backend.child_finished.exists()
    if (action, component, reader_exit) == ("stop", "frontend", "1"):
        del env["DEV_TEST_READER_EXIT"]
        recovered = subprocess.run(["/bin/bash", str(script), "stop", "frontend"], env=env,
                                   capture_output=True, text=True, timeout=20)
        assert recovered.returncode == 0, recovered.stderr
        assert_tree_stopped(tree)
        assert not pid_file.exists()


@pytest.mark.parametrize("discovery", ["pid_file", "listener"])
def test_unknown_arguments_preserve_frontend_bookkeeping(
    tmp_path: Path, process_trees: list[ProcessTree], discovery: str,
) -> None:
    script, _, backend_pid_file, _, env = prepare_dev_service(tmp_path, "absent", "idle", "owned")
    tree = start_process_tree(tmp_path, process_trees, "frontend")
    table = tmp_path / "process-table.json"
    table.write_text(json.dumps(frontend_tree_rows(tree, env["DEV_TEST_REPO"], ["npm run dev"])))
    env.update(DEV_TEST_TREE=str(table), DEV_TEST_ARGUMENT_FAILURE="1")
    pid_file = backend_pid_file.with_name("mediaforce-frontend.pid")
    pid_file.write_text(str(tree.child_pid if discovery == "pid_file" else 999999))
    original = pid_file.read_bytes()
    result = subprocess.run(["/bin/bash", str(script), "stop", "frontend"], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "ownership unknown" in result.stderr
    assert_root_running(tree)
    assert not tree.child_finished.exists()
    assert pid_file.read_bytes() == original
