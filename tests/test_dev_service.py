import json
import os
from pathlib import Path
import shutil
import select
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib

import pytest

from mediaforce.ops.login_item import LOGIN_ITEM_LABEL


def prepare_dev_service(
    tmp_path: Path, service: str, running: str, process: str,
) -> tuple[Path, Path, Path, Path, dict[str, str]]:
    repo = tmp_path / "checkout with spaces"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "mediaforce-dev.sh"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/mediaforce-dev.sh", script)
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
    pid_file = state / "mediaforce-web.pid"
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
with Path(os.environ["DEV_TEST_LOG"]).open("a") as output:
    output.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
if name == "id":
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
elif name == "ps" and "DEV_TEST_TREE" in os.environ:
    rows = json.loads(Path(os.environ["DEV_TEST_TREE"]).read_text())
    rows = [row for row in rows if not row.get("finished") or not Path(row["finished"]).exists()]
    started = os.environ.get("DEV_TEST_STARTED")
    if started and Path(started).exists():
        rows.append({"pid": json.loads(Path(started).read_text())["pid"], "parent": 0,
                     "command": os.environ["DEV_TEST_REPO"] + "/.venv/bin/mediaforce-web"})
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
elif name == "dirname":
    print(Path(sys.argv[1]).parent)
elif name == "sed":
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
elif name == "shasum":
    import hashlib
    print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest(), " -")
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
    os.execv(sys.argv[1], sys.argv[1:])
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
    for command in ("id", "launchctl", "ps", "lsof", "python3", "sleep", "dirname", "sed", "awk", "sort", "shasum", "paste", "tr", "rm", "mkdir", "nohup"):
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
    if service != "other_directory" and (running == "idle" or process in {"owned", "python_owned"}):
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
    if (service, action, process) != ("matching", "start", "owned")
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


def assert_tree_stopped(tree: ProcessTree) -> None:
    returncode = tree.root.wait(timeout=5)
    assert returncode in {0, -signal.SIGKILL}
    if returncode == 0:
        completion = json.loads(tree.finished.read_text())
        assert completion["root_signaled"]
        assert completion["child_returncode"] < 0
    assert select.select([tree.lifetime_reader], [], [], 5)[0], "descendant survived the stop"
    assert os.read(tree.lifetime_reader, 1) == b""


@pytest.fixture
def process_trees() -> Iterator[list[ProcessTree]]:
    trees: list[ProcessTree] = []
    try:
        yield trees
    finally:
        for tree in trees:
            try:
                try:
                    os.write(tree.cleanup_writer, b"done" * 2)
                except BrokenPipeError:
                    pass
                tree.root.wait(timeout=5)
                assert select.select([tree.lifetime_reader], [], [], 5)[0], "fixture tree still alive"
                assert os.read(tree.lifetime_reader, 1) == b""
            finally:
                os.close(tree.cleanup_writer)
                os.close(tree.lifetime_reader)
                if tree.root.stdout is not None:
                    tree.root.stdout.close()
                if tree.root.stderr is not None:
                    tree.root.stderr.close()


def start_process_tree(
    tmp_path: Path, trees: list[ProcessTree], name: str, *, ignore_sigterm: bool = False,
    hold_root_after_child: bool = False,
) -> ProcessTree:
    finished = tmp_path / f"{name}-finished.json"
    child_finished = tmp_path / f"{name}-child-finished"
    cleanup_reader, cleanup_writer = os.pipe()
    lifetime_reader, lifetime_writer = os.pipe()
    child_code = '''
import os
from pathlib import Path
import signal
import sys

def terminate(signum, frame):
    Path(sys.argv[2]).touch()
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)
signal.signal(signal.SIGTERM, signal.SIG_IGN if sys.argv[4] == "stubborn" else terminate)
os.write(int(sys.argv[3]), b"ready")
os.close(int(sys.argv[3]))
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
    [sys.executable, "-c", sys.argv[4], str(cleanup_reader), sys.argv[5], str(ready_writer), sys.argv[6]],
    pass_fds=(cleanup_reader, lifetime_writer, ready_writer),
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
         str(child_finished), "stubborn" if ignore_sigterm else "normal", "hold" if hold_root_after_child else "normal"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(cleanup_reader, lifetime_writer),
    )
    os.close(cleanup_reader)
    os.close(lifetime_writer)
    assert root.stdout is not None
    child_pid = int(root.stdout.readline())
    tree = ProcessTree(root, child_pid, finished, child_finished, cleanup_writer, lifetime_reader)
    trees.append(tree)
    return tree


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
            {"pid": frontend.root.pid, "parent": 0, "command": "vite " + environment["DEV_TEST_REPO"] + "/frontend", "finished": str(frontend.finished)},
            {"pid": frontend.child_pid, "parent": frontend.root.pid, "command": "vite worker", "finished": str(frontend.child_finished)},
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
    assert foreign.root.poll() is None
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
    assert owned.root.wait(timeout=5) == -signal.SIGKILL
    assert_tree_stopped(owned)
    assert not owned.child_finished.exists()
    assert foreign.root.poll() is None
    assert not foreign.finished.exists()
    assert lock.read_bytes() == lock_bytes
    assert not pid_file.exists()


def test_restart_stops_owned_tree_unloads_once_and_starts_backend_with_retained_lock(
    tmp_path: Path, process_trees: list[ProcessTree],
) -> None:
    script, lock, pid_file, log, environment = prepare_dev_service(tmp_path, "matching", "pid_file", "owned")
    owned = start_process_tree(tmp_path, process_trees, "restart")
    table = tmp_path / "process-table.json"
    table.write_text(json.dumps([
        {"pid": owned.root.pid, "parent": 0, "command": environment["DEV_TEST_REPO"] + "/.venv/bin/mediaforce-web",
         "finished": str(owned.finished)},
        {"pid": owned.child_pid, "parent": owned.root.pid, "command": "uvicorn worker",
         "finished": str(owned.child_finished)},
    ]))
    environment["DEV_TEST_TREE"] = str(table)
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

Path(os.environ["DEV_TEST_STARTED"]).write_text(json.dumps({"pid": os.getpid(), "args": sys.argv}))
Path(os.environ["DEV_TEST_STARTED"]).with_suffix(".listening").touch()
os.read(int(os.environ["DEV_TEST_START_CLEANUP_FD"]), 4)
''')
    binary.chmod(0o755)
    try:
        result = subprocess.run(
            ["/bin/bash", str(script), "restart", "backend"], cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=30,
            pass_fds=(cleanup_reader, lifetime_writer, owned.cleanup_writer),
        )
        assert result.returncode == 0, result.stderr
        assert owned.root.wait(timeout=5) == 0
        assert json.loads(owned.finished.read_text())["root_signaled"]
        assert select.select([owned.lifetime_reader], [], [], 5)[0]
        assert os.read(owned.lifetime_reader, 1) == b""
        new_backend = json.loads(started.read_text())
        assert int(pid_file.read_text()) == new_backend["pid"]
        assert new_backend["pid"] != owned.root.pid
        assert new_backend["args"][0] == str(binary)
        assert "backend: started" in result.stdout
        assert lock.read_bytes() == lock_bytes
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert [call for call in calls if call[:2] == ["launchctl", "bootout"]] == [
            ["launchctl", "bootout", "gui/4242/" + LOGIN_ITEM_LABEL],
        ]
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
    binary.parent.mkdir(parents=True)
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
    if discovery != "service_pid":
        # Prove lock/listener discovery also works when launchctl omits its PID.
        environment.pop("DEV_TEST_SERVICE_PID")
        environment["DEV_TEST_BOOTOUT_ROOT"] = str(service.root.pid)
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
        assert foreign.root.poll() is None
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
                assert service.root.poll() is None


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
    records = pid_file.parent / "backend-shutdown"
    records.mkdir()
    foreign_key = hashlib.sha256(b"/foreign/checkout").hexdigest()
    foreign_record = records / f"{foreign_key}.pids"
    foreign_bytes = f"{foreign.root.pid}\n".encode()
    foreign_record.write_bytes(foreign_bytes)
    own_key = hashlib.sha256(environment["DEV_TEST_REPO"].encode()).hexdigest()
    own_record = records / f"{own_key}.pids"
    with fixture_backend(tmp_path, environment, shutdown_finished=service.finished) as (started, descriptors):
        for _ in range(2):
            result = subprocess.run(
                ["/bin/bash", str(script), "start", "backend"], cwd=tmp_path, env=environment,
                capture_output=True, text=True, timeout=40, pass_fds=(*descriptors, service.cleanup_writer),
            )
            assert result.returncode == 1
            assert "shutdown did not finish" in result.stderr
            assert "backend: running" not in result.stdout
            assert service.root.poll() is None
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
            assert ready_backend.root.poll() is None
            assert not started.exists()
        assert not own_record.exists()
        assert foreign_record.read_bytes() == foreign_bytes
        assert foreign.root.poll() is None
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
