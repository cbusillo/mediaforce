import json
import os
import select
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from mediaforce.ops import dev_frontend
from tests.test_dev_process_tree import native_command
from tests.test_dev_service import prepare_dev_service


@dataclass
class FrontendTree:
    script: Path
    pid_file: Path
    env: dict[str, str]
    interpreter: Path
    wrapper: subprocess.Popen[str]
    rows: dict[str, int]
    owned_lifetime: int
    sibling_lifetime: int


@pytest.fixture(params=[
    ("workspace", "node", False), ("Python Projects", "node", False), ("node work", "node", False),
    ("Python Projects", "node runtime/node", False), ("workspace", "node", True),
    ("workspace", "node runtime/node", True),
])
def frontend_tree(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[FrontendTree]:
    checkout_name, interpreter_path, remove_interpreter = request.param
    checkout_parent = tmp_path / checkout_name
    checkout_parent.mkdir()
    script, _, backend_pid_file, _, env = prepare_dev_service(checkout_parent, "absent", "idle", "owned")
    repo = Path(env["DEV_TEST_REPO"])
    frontend = repo / "frontend"
    vite = frontend / "node_modules/.bin/vite"
    vite.parent.mkdir(parents=True)
    worker = tmp_path / "worker.py"
    worker.write_text('''
import os, signal, sys
signal.signal(signal.SIGTERM, signal.SIG_IGN if sys.argv[3] == "owned" else signal.SIG_DFL)
os.write(int(sys.argv[4]), b"R")
os.close(int(sys.argv[4]))
os.read(int(sys.argv[1]), 1)
''')
    vite.write_text('''
import json, os, signal, subprocess, sys
ready_r, ready_w = os.pipe()
child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[3], "owned", str(ready_w)],
                         pass_fds=(int(sys.argv[2]), int(sys.argv[3]), ready_w))
os.close(ready_w)
assert os.read(ready_r, 1) == b"R"
os.close(ready_r)
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
print(json.dumps({"vite": os.getpid(), "worker": child.pid, "cwd": os.getcwd()}), flush=True)
os.read(int(sys.argv[2]), 1)
''')
    wrapper_script = tmp_path / "shared-wrapper.py"
    wrapper_script.write_text('''
import json, os, subprocess, sys
vite = subprocess.Popen([sys.argv[1], *sys.argv[2:6]], stdout=subprocess.PIPE,
                        pass_fds=(int(sys.argv[4]), int(sys.argv[5])))
os.close(int(sys.argv[5]))
rows = json.loads(vite.stdout.readline())
ready_r, ready_w = os.pipe()
sibling = subprocess.Popen([sys.executable, sys.argv[3], sys.argv[4], sys.argv[6], "sibling", str(ready_w)],
                          pass_fds=(int(sys.argv[4]), int(sys.argv[6]), ready_w))
os.close(int(sys.argv[6]))
os.close(ready_w)
assert os.read(ready_r, 1) == b"R"
os.close(ready_r)
rows.update(wrapper=os.getpid(), sibling=sibling.pid)
print(json.dumps(rows), flush=True)
os.read(int(sys.argv[4]), 1)
vite.wait()
sibling.wait()
''')
    # Pin the node-shaped argv without requiring a host Node installation.
    node = tmp_path / interpreter_path
    node.parent.mkdir(parents=True, exist_ok=True)
    node.symlink_to(sys.executable)
    cleanup_r, cleanup_w = os.pipe()
    owned_r, owned_w = os.pipe()
    sibling_r, sibling_w = os.pipe()
    root = subprocess.Popen(
        [sys.executable, str(wrapper_script), str(node), str(vite), str(worker),
         str(cleanup_r), str(owned_w), str(sibling_w)],
        cwd=frontend, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        pass_fds=(cleanup_r, owned_w, sibling_w),
    )
    os.close(cleanup_r)
    os.close(owned_w)
    os.close(sibling_w)
    try:
        assert root.stdout is not None
        assert select.select([root.stdout], [], [], 5)[0], "frontend fixture failed to start"
        rows = json.loads(root.stdout.readline())
        assert rows.pop("cwd") == str(frontend)
        # Args, parentage inside the fixture and custody are native. Supply
        # startup cwd and a controlled foreign controller boundary so unrelated
        # harness ancestors cannot turn this into an unknown-ownership test.
        ps = Path(env["PATH"]) / "ps"
        ps.write_text("#!" + sys.executable + f'''\nimport os, sys
if sys.argv[1:] == ['-p', '{os.getpid()}', '-o', 'ppid=']:
    print(0)
else:
    os.execv('/bin/ps', ['ps', *sys.argv[1:]])
''')
        table = tmp_path / "cwd-table.json"
        table.write_text(json.dumps([
            *({"pid": pid, "cwd": str(frontend)} for pid in rows.values()),
            {"pid": os.getpid(), "cwd": str(tmp_path)},
        ]))
        env.update(DEV_TEST_TREE=str(table), COLUMNS="80")
        if remove_interpreter:
            node.unlink()
        yield FrontendTree(script, backend_pid_file.with_name("mediaforce-frontend.pid"), env, node,
                           root, rows, owned_r, sibling_r)
    finally:
        os.close(cleanup_w)
        root.wait(timeout=5)
        for reader in (owned_r, sibling_r):
            assert select.select([reader], [], [], 5)[0], "fixture process survived teardown"
            assert os.read(reader, 1) == b""
            os.close(reader)
        if root.stdout is not None:
            root.stdout.close()
        assert root.stderr is not None
        root.stderr.close()


@pytest.mark.parametrize("discovery", ["pid_file", "listener"])
def test_frontend_stop_preserves_native_shared_wrapper_and_sibling(frontend_tree: FrontendTree, discovery: str) -> None:
    tree = frontend_tree
    repo = Path(tree.env["DEV_TEST_REPO"])
    command = native_command(tree.rows["wrapper"])
    assert "shared-wrapper.py" in command
    assert str(repo / "frontend/node_modules/.bin/vite") in command
    vite_command = native_command(tree.rows["vite"])
    assert vite_command.startswith(str(tree.interpreter) + " ")
    assert str(repo / "frontend/node_modules/.bin/vite") in vite_command
    if discovery == "pid_file":
        tree.pid_file.write_text(str(tree.rows["worker"]))
    else:
        table = Path(tree.env["DEV_TEST_TREE"])
        rows = json.loads(table.read_text())
        for row in rows:
            if row["pid"] == tree.rows["worker"]:
                row.update(port="4173", finished=str(table.with_suffix(".finished")))
        table.write_text(json.dumps(rows))
    result = subprocess.run(["/bin/bash", str(tree.script), "stop", "frontend"],
                            env=tree.env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "parent ownership unknown" not in result.stderr
    assert select.select([tree.owned_lifetime], [], [], 5)[0], "owned worker survived stop"
    assert os.read(tree.owned_lifetime, 1) == b""
    assert not select.select([tree.sibling_lifetime], [], [], .1)[0], "unrelated sibling exited"
    assert tree.wrapper.poll() is None, "shared wrapper became the stop root"
    assert not tree.pid_file.exists()


@pytest.mark.parametrize("action", ["stop", "restart"])
@pytest.mark.parametrize("failure", ["parent", "recheck"])
def test_native_unknown_parent_and_custody_recheck(frontend_tree: FrontendTree, action: str, failure: str) -> None:
    tree = frontend_tree
    tree.pid_file.write_text(str(tree.rows["vite"]))
    reader = Path(tree.env["DEV_TEST_REPO"]) / "mediaforce/ops/dev_frontend.py"
    counter = reader.with_suffix(".reads")
    target = tree.rows["wrapper"] if failure == "parent" else tree.rows["vite"]
    # Qualify controlled argument unavailability with real native identities;
    # the second Vite read occurs inside the custody recheck after discovery.
    reader.write_text(reader.read_text().replace(
        "def process_arguments(pid: int) -> list[str]:",
        f'''def process_arguments(pid: int) -> list[str]:
    if pid == {target}:
        counter = Path({str(counter)!r})
        count = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(count))
        if {failure == "parent"!r} or count >= 2:
            raise PermissionError("controlled native argument unavailability")''',
    ))
    result = subprocess.run(["/bin/bash", str(tree.script), action, "frontend"],
                            env=tree.env, capture_output=True, text=True, timeout=20)
    assert "ownership unknown" in result.stderr
    assert "ownership changed" not in result.stderr
    assert tree.wrapper.poll() is None
    assert not select.select([tree.sibling_lifetime], [], [], .1)[0]
    if failure == "recheck":
        assert result.returncode != 0
        assert "PID bookkeeping retained" in result.stderr
        assert tree.pid_file.read_text() == str(tree.rows["vite"])
        assert not select.select([tree.owned_lifetime], [], [], .1)[0]
    else:
        assert f"native argument ownership unknown for pid {target}: controlled native argument unavailability" in result.stderr
        assert "stopping proven subtree" in result.stderr
        assert "frontend: stopped" in result.stdout
        assert select.select([tree.owned_lifetime], [], [], 5)[0]
        assert os.read(tree.owned_lifetime, 1) == b""
        if action == "stop":
            assert not tree.pid_file.exists()
        else:
            assert tree.pid_file.read_text().strip() != str(tree.rows["vite"])
            assert "frontend: failed to start" in result.stderr
        # The fixture has no npm launcher; restart gets as far as requesting
        # that supported replacement, then its intentionally absent stub fails.
        assert result.returncode == (0 if action == "stop" else 1)


def test_native_reader_exit_between_cwd_and_argument_read(tmp_path: Path) -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"], cwd=tmp_path,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    process.wait(timeout=5)
    assert process.stdout.read() == b""
    result = subprocess.run([sys.executable, dev_frontend.__file__, str(process.pid),
                             str(tmp_path), str(tmp_path / "frontend")],
                            capture_output=True, text=True, timeout=5)
    process.stdout.close()
    assert process.stderr is not None
    process.stderr.close()
    assert result.returncode == 1, result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize("failure", ["cwd", "environment"])
def test_native_discovery_prerequisite_failure_retains_tree(frontend_tree: FrontendTree, failure: str) -> None:
    tree = frontend_tree
    tree.pid_file.write_text(str(tree.rows["vite"]))
    if failure == "cwd":
        table = Path(tree.env["DEV_TEST_TREE"])
        rows = json.loads(table.read_text())
        for row in rows:
            row["cwd"] = ""
        table.write_text(json.dumps(rows))
    else:
        (Path(tree.env["PATH"]) / "uv").unlink()
    result = subprocess.run(["/bin/bash", str(tree.script), "stop", "frontend"],
                            env=tree.env, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "ownership unknown" in result.stderr
    assert "PID bookkeeping retained" in result.stderr
    assert tree.pid_file.read_text() == str(tree.rows["vite"])
    assert not select.select([tree.owned_lifetime], [], [], .1)[0]
    assert not select.select([tree.sibling_lifetime], [], [], .1)[0]
    assert tree.wrapper.poll() is None
