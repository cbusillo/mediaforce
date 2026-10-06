import json
import os
import select
import signal
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tests.test_dev_service import prepare_dev_service
from mediaforce.core import _process_deadline as custody, process_control


@dataclass
class NativeDevTree:
    script: Path
    pid_file: Path
    env: dict[str, str]
    wrapper: subprocess.Popen[str]
    pids: dict[str, int]
    worker_lifetime: int
    reparented: Path


@pytest.fixture
def native_dev_tree(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[NativeDevTree]:
    script, _, pid_file, _, env = prepare_dev_service(tmp_path, "absent", "pid_file", "owned")
    repo = Path(env["DEV_TEST_REPO"])
    worker = tmp_path / "worker.py"
    worker.write_text('''
import json, os, select, signal, sys
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
parent = os.getppid()
recorded = False
os.write(int(sys.argv[3]), b"R")
os.close(int(sys.argv[3]))
while not select.select([int(sys.argv[1])], [], [], .01)[0]:
    if not recorded and len(sys.argv) > 4 and os.getppid() != parent:
        Path(sys.argv[4]).write_text(json.dumps({"before": parent, "after": os.getppid()}))
        recorded = True
os.read(int(sys.argv[1]), 1)
''')
    backend = repo / ".venv/bin/mediaforce-web"
    backend.parent.mkdir(parents=True)
    backend.write_text('''
import json, os, signal, subprocess, sys
ready_r, ready_w = os.pipe()
child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[4], str(ready_w), sys.argv[5]],
                        pass_fds=(int(sys.argv[2]), int(sys.argv[4]), ready_w))
os.close(ready_w)
assert os.read(ready_r, 1) == b"R"
os.close(ready_r)
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
print(json.dumps({"backend": os.getpid(), "worker": child.pid}), flush=True)
os.read(int(sys.argv[2]), 1)
''')
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text('''
import json, os, subprocess, sys
if sys.argv[1] == sys.executable:
    sys.argv.pop(1)
backend = subprocess.Popen([sys.executable, *sys.argv[1:]], stdout=subprocess.PIPE,
                           pass_fds=(int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])))
os.close(int(sys.argv[5]))
assert backend.stdout is not None
rows = json.loads(backend.stdout.readline())
ready_r, ready_w = os.pipe()
sibling = subprocess.Popen([sys.executable, sys.argv[2], sys.argv[3], sys.argv[4], str(ready_w)],
                          pass_fds=(int(sys.argv[3]), int(sys.argv[4]), ready_w))
os.close(ready_w)
assert os.read(ready_r, 1) == b"R"
os.close(ready_r)
rows.update(wrapper=os.getpid(), sibling=sibling.pid)
print(json.dumps(rows), flush=True)
os.read(int(sys.argv[3]), 1)
backend.wait()
sibling.wait()
''')
    cleanup_r, cleanup_w = os.pipe()
    lifetime_r, lifetime_w = os.pipe()
    worker_r, worker_w = os.pipe()
    reparented = tmp_path / "worker-reparented.json"
    pass_interpreter, interpreter_name = getattr(request, "param", (False, "python"))
    interpreter = backend.parent / interpreter_name
    interpreter.symlink_to(sys.executable)
    wrapper_args = [str(interpreter)] if pass_interpreter else []
    root = subprocess.Popen(
        [str(interpreter), str(wrapper), *wrapper_args, str(backend), str(worker), str(cleanup_r), str(lifetime_w),
         str(worker_w), str(reparented)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        pass_fds=(cleanup_r, lifetime_w, worker_w),
    )
    os.close(cleanup_r)
    os.close(lifetime_w)
    os.close(worker_w)
    try:
        assert root.stdout is not None
        assert select.select([root.stdout], [], [], 5)[0], "fixture wrapper failed to become ready"
        rows = json.loads(root.stdout.readline())
        env["DEV_TEST_PID"] = str(rows["backend"])
        ps = Path(env["PATH"]) / "ps"
        ps.write_text("#!" + sys.executable + "\nimport os, sys\nos.execv('/bin/ps', ['ps', *sys.argv[1:]])\n")
        ps.chmod(0o755)
        yield NativeDevTree(script, pid_file, env, root, rows, worker_r, reparented)
    finally:
        # Pipe EOF releases every fixture process, including workers reparented by a failed stop.
        os.close(cleanup_w)
        root.wait(timeout=5)
        assert select.select([lifetime_r], [], [], 5)[0], "fixture descendant still alive"
        assert os.read(lifetime_r, 1) == b""
        assert select.select([worker_r], [], [], 5)[0], "fixture worker still alive"
        assert os.read(worker_r, 1) == b""
        os.close(lifetime_r)
        os.close(worker_r)
        if root.stdout is not None:
            root.stdout.close()
        assert root.stderr is not None
        root.stderr.close()


def native_command(pid: int) -> str:
    return subprocess.check_output(["/bin/ps", "-p", str(pid), "-o", "command="], text=True).strip()


def stop_native_backend(tree: NativeDevTree, *, cwd: Path | None = None) -> None:
    tree.pid_file.write_text(str(tree.pids["backend"]))
    result = subprocess.run(["/bin/bash", str(tree.script), "stop", "backend"],
                            cwd=cwd, env=tree.env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert select.select([tree.worker_lifetime], [], [], 5)[0], "owned tree survived"
    assert os.read(tree.worker_lifetime, 1) == b""


@pytest.mark.parametrize("native_dev_tree", [
    (False, "python"), (True, "python"), (False, "Python"), (False, "python3.13t"),
], indirect=True)
def test_stop_preserves_real_shared_wrapper_and_sibling(native_dev_tree: NativeDevTree) -> None:
    env, wrapper, rows = native_dev_tree.env, native_dev_tree.wrapper, native_dev_tree.pids
    binary = str(Path(env["DEV_TEST_REPO"]) / ".venv/bin/mediaforce-web")
    assert binary in native_command(rows["wrapper"])
    assert "wrapper.py" in native_command(rows["wrapper"])
    stop_native_backend(native_dev_tree)
    assert wrapper.poll() is None, "shared wrapper became the kill root"
    assert native_command(rows["sibling"]), "foreign sibling was stopped"


def test_stop_uses_its_checkout_when_invoked_from_another_checkout(native_dev_tree: NativeDevTree, tmp_path: Path) -> None:
    foreign = tmp_path / "foreign checkout"
    package = foreign / "mediaforce"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('raise RuntimeError("foreign checkout imported")\n')
    stop_native_backend(native_dev_tree, cwd=foreign)
    assert native_dev_tree.wrapper.poll() is None


def test_stop_cleans_stubborn_worker_after_native_reparenting(native_dev_tree: NativeDevTree) -> None:
    stop_native_backend(native_dev_tree)
    parentage = json.loads(native_dev_tree.reparented.read_text())
    assert parentage["before"] == native_dev_tree.pids["backend"]
    assert parentage["after"] != parentage["before"]


def test_stop_checks_ownership_after_pinning_and_never_signals_a_changed_owner() -> None:
    tree = Mock()
    owns = Mock(return_value=False)
    order = Mock()
    order.attach_mock(tree.add_root, "pin")
    order.attach_mock(owns, "owns")
    with (patch.object(custody, "_process_tree", return_value=tree),
          patch.object(process_control.os, "getpid", return_value=10)):
        with pytest.raises(RuntimeError, match="ownership changed"):
            process_control.stop_existing_process_tree(4321, owns)
    assert [call[0] for call in order.mock_calls] == ["pin", "owns"]
    tree.signal_all.assert_not_called()
    tree.close.assert_called_once()


def test_stop_surfaces_native_custody_failure_and_closes_tree() -> None:
    tree = Mock()
    tree.add_root.side_effect = RuntimeError("cannot pin live process")
    owns = Mock()
    with (patch.object(custody, "_process_tree", return_value=tree),
          patch.object(process_control.os, "getpid", return_value=10)):
        with pytest.raises(RuntimeError, match="cannot pin"):
            process_control.stop_existing_process_tree(4321, owns)
    owns.assert_not_called()
    tree.signal_all.assert_not_called()
    tree.close.assert_called_once()


def test_stop_reports_unproven_cleanup() -> None:
    tree = Mock()
    result = Mock(succeeded=False, reason="worker remained live")
    with (patch.object(custody, "_process_tree", return_value=tree),
          patch.object(process_control.os, "getpid", return_value=10),
          patch.object(custody, "_terminate_tree", return_value=result)):
        with pytest.raises(RuntimeError, match="worker remained live"):
            process_control.stop_existing_process_tree(4321, lambda: True)
    tree.close.assert_called_once()


def test_external_linux_tree_excludes_helpers_children_and_retains_reparented_identity() -> None:
    tree = object.__new__(custody._LinuxProcessTree)
    tree._external_root = True
    tree._signal_failure_reason = None
    tree._processes = {100: custody._LinuxProcessIdentity(100, 50, 1000)}
    tree._pidfd_send_signal = Mock()

    def register(pid: int, parent: int) -> bool:
        tree._processes[pid] = custody._LinuxProcessIdentity(pid, parent, 2000)
        return True

    with (patch.object(custody.os, "getpid", return_value=10),
          patch.object(custody, "_pidfd_exited", return_value=False),
          patch.object(custody, "_linux_child_pids") as children,
          patch.object(tree, "_register", side_effect=register)):
        # Only the root has a child; the helper's unrelated child is never enumerated.
        children.side_effect = lambda pid, *_: {200} if pid == 100 else ({999} if pid == 10 else set())
        tree.refresh()
        assert set(tree._processes) == {100, 200}
        assert all(call.args[0] != 10 for call in children.call_args_list)
        children.return_value = set()
        children.side_effect = None
        tree.signal_all(signal.SIGKILL)
    assert [call.args[0] for call in tree._pidfd_send_signal.call_args_list] == [2000, 1000]


@pytest.mark.parametrize("action,component", [("stop", "backend"), ("restart", "backend"), ("restart", "all")])
def test_failed_custody_preserves_pid_file_and_prevents_restart(tmp_path: Path, action: str, component: str) -> None:
    script, lock, pid_file, log, env = prepare_dev_service(tmp_path, "absent", "pid_file", "owned")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        env["DEV_TEST_PID"] = str(child.pid)
        env["DEV_TEST_CUSTODY_FAILURE"] = "1"
        pid_file.write_text(str(child.pid))
        lock_bytes = lock.read_bytes()
        result = subprocess.run(["/bin/bash", str(script), action, component], env=env,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert "custody unavailable" in result.stderr
        assert child.poll() is None
        assert pid_file.read_text() == str(child.pid)
        assert lock.read_bytes() == lock_bytes
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert not any(call[0] == "nohup" for call in calls)
    finally:
        child.kill()
        child.wait(timeout=5)
