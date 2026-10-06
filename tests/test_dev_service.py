import hashlib
import json
import os
from pathlib import Path
import shutil
import select
import signal
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from mediaforce.ops.login_item import LOGIN_ITEM_LABEL


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
    for module in ("mediaforce/core/_process_deadline.py", "mediaforce/core/process_control.py", "mediaforce/ops/dev_processes.py"):
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
elif name == "launchctl" and sys.argv[1] == "bootout":
    Path(os.environ["DEV_TEST_LOG"]).with_suffix(".unloaded").touch()
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
    if os.environ.get("DEV_TEST_CUSTODY_FAILURE"):
        print("injected custody unavailable; PID bookkeeping retained", file=sys.stderr)
        sys.exit(1)
    os.environ["PYTHONPATH"] = sys.argv[4]
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

    assert result.returncode == 0, result.stderr
    assert lock.read_bytes() == lock_bytes
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


@pytest.fixture
def process_trees() -> Iterator[list[ProcessTree]]:
    trees: list[ProcessTree] = []
    try:
        yield trees
    finally:
        for tree in trees:
            try:
                try:
                    os.write(tree.cleanup_writer, b"done")
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
    tmp_path: Path, trees: list[ProcessTree], name: str, *, ignore_sigterm: bool = False, wrapper: bool = False,
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
Path(sys.argv[1]).write_text(json.dumps({"child_returncode": result, "root_signaled": root_signaled}))
'''
    root = subprocess.Popen(
        [sys.executable, "-c", code, str(finished), str(cleanup_reader), str(lifetime_writer), child_code,
         str(child_finished), "wrapper" if wrapper else "stubborn" if ignore_sigterm else "normal"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(cleanup_reader, lifetime_writer),
    )
    os.close(cleanup_reader)
    os.close(lifetime_writer)
    assert root.stdout is not None
    tree = ProcessTree(root, 0, finished, child_finished, cleanup_writer, lifetime_reader)
    trees.append(tree)
    assert select.select([root.stdout], [], [], 5)[0], "fixture root not ready"
    tree.child_pid = int(root.stdout.readline())
    if wrapper:
        tree.worker_pid = int(Path(str(child_finished) + "-worker-pid").read_text())
        tree.worker_finished = Path(str(child_finished) + "-worker-finished")
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
            {"pid": frontend.root.pid, "parent": 0, "command": "vite " + environment["DEV_TEST_REPO"] + "/frontend", "cwd": environment["DEV_TEST_REPO"] + "/frontend", "finished": str(frontend.finished)},
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
    assert_tree_stopped(owned)
    assert not owned.child_finished.exists()
    assert foreign.root.poll() is None
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
os.read(int(os.environ["DEV_TEST_START_CLEANUP_FD"]), 4)
''')
    binary.chmod(0o755)
    try:
        result = subprocess.run(
            ["/bin/bash", str(script), action, "backend"], cwd=tmp_path, env=environment,
            capture_output=True, text=True, timeout=20, pass_fds=(cleanup_reader, lifetime_writer),
        )
        assert result.returncode == 0, result.stderr
        if action == "restart":
            assert_tree_stopped(owned)
        else:
            assert owned.root.poll() is None
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


def frontend_tree_rows(tree: ProcessTree, checkout: str, command: str) -> list[dict[str, object]]:
    return [
        {"pid": tree.root.pid, "parent": 0, "command": command, "cwd": checkout if command.startswith("npm --prefix ") else checkout + "/frontend",
         "finished": str(tree.finished)},
        {"pid": tree.child_pid, "parent": tree.root.pid, "command": "vite worker",
         "cwd": checkout + "/frontend", "port": "4173", "finished": str(tree.child_finished)},
    ]


@pytest.mark.parametrize("checkout_kind,command_kind", [
    (checkout, command) for checkout in ("owned", "sibling", "nested", "foreign", "unavailable")
    for command in ("relative", "rewritten", "absolute", "interpreter", "vite")
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
    command = {"relative": "npm --prefix frontend run dev -- --strictPort",
               "rewritten": "npm run dev --host 127.0.0.1",
               "absolute": "npm --prefix " + checkout + "/frontend run dev -- --strictPort",
               "interpreter": sys.executable + " /fixture/bin/npm --prefix " + checkout + "/frontend run dev -- --strictPort",
               "vite": sys.executable + " " + checkout + "/frontend/node_modules/.bin/vite --strictPort"}[command_kind]
    rows = frontend_tree_rows(tree, checkout, command)
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
        assert tree.root.poll() is None
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
    rows = frontend_tree_rows(frontend, environment["DEV_TEST_REPO"] + "-sibling", "npm --prefix frontend run dev")
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
        assert tree.root.poll() is None
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
    rows = frontend_tree_rows(owned, environment["DEV_TEST_REPO"], "npm run dev") if owned is not None else []
    legacy_pid_file = None
    if running == "legacy":
        assert owned is not None
        rows[0]["cwd"] = environment["DEV_TEST_REPO"]
        legacy_pid_file = lock.parent / "mediaforce-frontend.pid"
        legacy_pid_file.write_text(str(owned.root.pid))
    foreign_checkout = environment["DEV_TEST_REPO"] + "-sibling"
    rows += frontend_tree_rows(foreign, foreign_checkout, "npm run dev")
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
            assert owned.root.poll() is None
            assert not started.exists()
            assert "frontend: running" in result.stdout
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            assert not any(call[0] == "nohup" for call in calls)
        assert foreign.root.poll() is None
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
        rows = frontend_tree_rows(owned, environment["DEV_TEST_REPO"], "npm run dev")
        rows += frontend_tree_rows(foreign, "/foreign/checkout", "npm --prefix frontend run dev")
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
    assert foreign.root.poll() is None
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
    rows = frontend_tree_rows(tree, repo + "-sibling", "npm run dev")
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
    assert tree.root.poll() is None
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
        {"pid": tree.root.pid, "parent": 0, "command": "npm run dev", "cwd": repo + "/frontend",
         "finished": str(tree.finished)},
        {"pid": tree.child_pid, "parent": tree.root.pid, "command": "sh -c vite dev", "cwd": repo + "/frontend",
         "finished": str(tree.child_finished)},
        {"pid": tree.worker_pid, "parent": tree.child_pid,
         "command": "node " + repo + "/frontend/node_modules/.bin/vite dev", "cwd": repo + "/frontend",
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
def test_frontend_stop_preserves_the_unrelated_launching_parent(
    tmp_path: Path, process_trees: list[ProcessTree], running: str,
) -> None:
    script, lock, backend_pid_file, _, environment = prepare_dev_service(tmp_path, "absent", "idle", "owned")
    launcher = start_process_tree(tmp_path, process_trees, "terminal")
    owned = start_process_tree(tmp_path, process_trees, "owned-frontend")
    repo = environment["DEV_TEST_REPO"]
    # Fake ps supplies this parent relationship; every PID is fixture-owned.
    rows = frontend_tree_rows(owned, repo, "npm run dev")
    rows[0]["parent"] = launcher.root.pid
    rows.extend([
        {"pid": launcher.root.pid, "parent": 0, "command": "-zsh", "cwd": repo + "/frontend",
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
    assert launcher.root.poll() is None
    assert not launcher.child_finished.exists()
    assert not select.select([launcher.lifetime_reader], [], [], 0)[0]
    assert not pid_file.exists()
    assert lock.read_bytes() == lock_bytes
