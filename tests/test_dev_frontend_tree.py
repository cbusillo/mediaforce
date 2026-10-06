import json
import os
import select
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

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
        # Command/parent readers and custody use the OS. Cwd is supplied from
        # the fixture's actual startup report, not an assumed launcher location.
        ps = Path(env["PATH"]) / "ps"
        ps.write_text("#!" + sys.executable + "\nimport os, sys\nos.execv('/bin/ps', ['ps', *sys.argv[1:]])\n")
        table = tmp_path / "cwd-table.json"
        table.write_text(json.dumps([{"pid": pid, "cwd": str(frontend)} for pid in rows.values()]))
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
    assert select.select([tree.owned_lifetime], [], [], 5)[0], "owned worker survived stop"
    assert os.read(tree.owned_lifetime, 1) == b""
    assert not select.select([tree.sibling_lifetime], [], [], .1)[0], "unrelated sibling exited"
    assert tree.wrapper.poll() is None, "shared wrapper became the stop root"
    assert not tree.pid_file.exists()
