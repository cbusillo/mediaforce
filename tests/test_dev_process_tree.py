import fcntl
import json
import os
import select
import socket
import signal
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tests.test_dev_service import fixture_backend, prepare_dev_service
from mediaforce.core import _process_deadline as custody, process_control
from mediaforce.ops import dev_processes


@dataclass
class NativeDevTree:
    script: Path
    pid_file: Path
    env: dict[str, str]
    wrapper: subprocess.Popen[str]
    pids: dict[str, int]
    worker_lifetime: int
    sibling_lifetime: int
    reparented: Path
    retain_cleanup_marker: bool = False


@pytest.fixture
def native_dev_tree(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[NativeDevTree]:
    script, _, pid_file, _, env = prepare_dev_service(tmp_path, "absent", "pid_file", "owned", native_custody=True)
    repo = Path(env["DEV_TEST_REPO"])
    worker = tmp_path / "worker.py"
    worker.write_text('''
import json, os, select, signal, sys
from pathlib import Path
sibling = len(sys.argv) > 4 and sys.argv[4] == "sibling"
signal.signal(signal.SIGTERM, signal.SIG_DFL if sibling else signal.SIG_IGN)
parent = os.getppid()
recorded = False
os.write(int(sys.argv[3]), b"R")
os.close(int(sys.argv[3]))
while not select.select([int(sys.argv[1])], [], [], .01)[0]:
    if not sibling and len(sys.argv) > 4 and Path(sys.argv[4]).with_suffix(".fork").exists():
        child = os.fork()
        if child:
            Path(sys.argv[4]).with_suffix(".late-fork.json").write_text(json.dumps({"worker": os.getpid(), "child": child}))
            os._exit(0)
        sibling = True
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if not sibling and not recorded and len(sys.argv) > 4 and os.getppid() != parent:
        Path(sys.argv[4]).write_text(json.dumps({"before": parent, "after": os.getppid()}))
        recorded = True
os.read(int(sys.argv[1]), 1)
''')
    backend = repo / ".venv/bin/mediaforce-web"
    backend.parent.mkdir(parents=True)
    backend.write_text('''
import json, os, select, signal, subprocess, sys
from pathlib import Path
ready_r, ready_w = os.pipe()
child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[4], str(ready_w), sys.argv[5]],
                        pass_fds=(int(sys.argv[2]), int(sys.argv[4]), ready_w))
os.close(ready_w)
assert os.read(ready_r, 1) == b"R"
os.close(ready_r)
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
print(json.dumps({"backend": os.getpid(), "worker": child.pid}), flush=True)
while not select.select([int(sys.argv[2])], [], [], .01)[0]:
    if Path(sys.argv[5]).with_suffix(".exit").exists():
        os._exit(0)
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
sibling = subprocess.Popen([sys.executable, sys.argv[2], sys.argv[3], sys.argv[7], str(ready_w), "sibling"],
                          pass_fds=(int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[7]), ready_w))
os.close(int(sys.argv[7]))
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
    sibling_r, sibling_w = os.pipe()
    reparented = tmp_path / "worker-reparented.json"
    pass_interpreter, interpreter_name = getattr(request, "param", (False, "python"))
    interpreter = backend.parent / interpreter_name
    interpreter.symlink_to(sys.executable)
    wrapper_args = [str(interpreter)] if pass_interpreter else []
    root = subprocess.Popen(
        [str(interpreter), str(wrapper), *wrapper_args, str(backend), str(worker), str(cleanup_r), str(lifetime_w),
         str(worker_w), str(reparented), str(sibling_w)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        pass_fds=(cleanup_r, lifetime_w, worker_w, sibling_w),
    )
    os.close(cleanup_r)
    os.close(lifetime_w)
    os.close(worker_w)
    os.close(sibling_w)
    owned_tree = None
    try:
        assert root.stdout is not None
        assert select.select([root.stdout], [], [], 5)[0], "fixture wrapper failed to become ready"
        rows = json.loads(root.stdout.readline())
        env["DEV_TEST_PID"] = str(rows["backend"])
        env["COLUMNS"] = "80"
        ps = Path(env["PATH"]) / "ps"
        ps.write_text("#!" + sys.executable + "\nimport os, sys\nos.execv('/bin/ps', ['ps', *sys.argv[1:]])\n")
        ps.chmod(0o755)
        owned_tree = NativeDevTree(script, pid_file, env, root, rows, worker_r, sibling_r, reparented)
        yield owned_tree
    finally:
        # Pipe EOF releases every fixture process, including workers reparented by a failed stop.
        os.close(cleanup_w)
        root.wait(timeout=5)
        assert select.select([lifetime_r], [], [], 5)[0], "fixture descendant still alive"
        assert os.read(lifetime_r, 1) == b""
        assert select.select([worker_r], [], [], 5)[0], "fixture worker still alive"
        assert os.read(worker_r, 1) == b""
        assert select.select([sibling_r], [], [], 5)[0], "fixture sibling still alive"
        assert os.read(sibling_r, 1) == b""
        cleanup_state = pid_file.parent / "backend.cleanup"
        if cleanup_state.exists() and (
                sys.platform == "linux" or owned_tree is not None and owned_tree.retain_cleanup_marker
        ):
            clear_test_owned_lost_cleanup(cleanup_state)
        deadline = time.monotonic() + 5
        while cleanup_state.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert not cleanup_state.exists(), "fixture cleanup supervisor survived teardown"
        os.close(sibling_r)
        os.close(lifetime_r)
        os.close(worker_r)
        if root.stdout is not None:
            root.stdout.close()
        assert root.stderr is not None
        root.stderr.close()


def clear_test_owned_lost_cleanup(state: Path) -> None:
    # Only fixture state is removed, after all descendant lifetime pipes proved
    # EOF and the custody supervisor's own socket proves it no longer listens.
    deadline = time.monotonic() + 5
    while True:
        try:
            with dev_processes.state_directory(state), socket.socket(socket.AF_UNIX) as client:
                client.connect("control.sock")
        except (ConnectionRefusedError, FileNotFoundError):
            if state.exists():
                dev_processes.remove_state(state)
            return
        assert time.monotonic() < deadline, "fixture cleanup supervisor survived teardown"
        time.sleep(.3)


def native_command(pid: int) -> str:
    return subprocess.check_output(
        ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
        env={**os.environ, "COLUMNS": "80"}, text=True,
    ).strip()


def stop_native_backend(tree: NativeDevTree, *, cwd: Path | None = None) -> None:
    tree.pid_file.write_text(str(tree.pids["backend"]))
    result = subprocess.run(["/bin/bash", str(tree.script), "stop", "backend"],
                            cwd=cwd, env=tree.env, capture_output=True, text=True, timeout=15)
    if sys.platform == "linux":
        assert result.returncode != 0
        assert "Linux existing-tree descendant custody is unproven" in result.stderr
        assert tree.pid_file.read_text() == str(tree.pids["backend"])
    else:
        assert result.returncode == 0, result.stderr
    assert select.select([tree.worker_lifetime], [], [], 5)[0], "owned tree survived"
    assert os.read(tree.worker_lifetime, 1) == b""


def assert_shared_workloads_survive(tree: NativeDevTree) -> None:
    # The foreign sibling handles TERM normally. Allow an accidental signal to
    # finish delivery before checking its unique lifetime pipe and the wrapper.
    assert not select.select([tree.sibling_lifetime], [], [], .1)[0], "foreign sibling exited"
    assert tree.wrapper.poll() is None, "shared wrapper became the kill root"


def test_stop_survives_unrelated_real_package_import_failure(native_dev_tree: NativeDevTree) -> None:
    tree = native_dev_tree
    package = Path(tree.env["DEV_TEST_REPO"]) / "mediaforce"
    shutil.copytree(Path(__file__).resolve().parents[1] / "mediaforce", package,
                    dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    (package / "encoding/encode_queue.py").write_text("unfinished edit !!!\n")
    stop_native_backend(tree)
    assert_shared_workloads_survive(tree)


def test_internal_stop_cleans_its_current_root_after_clearing_prior_boot_state(native_dev_tree: NativeDevTree) -> None:
    tree = native_dev_tree
    helper = Path(tree.env["DEV_TEST_REPO"]) / "mediaforce/ops/dev_processes.py"
    current_boot = "00000000-0000-0000-0000-000000000002"
    helper.write_text(helper.read_text().replace(
        "\ndef main()", f"\ndef boot_id():\n    return {current_boot!r}\n\ndef main()",
    ))
    state = tree.pid_file.parent / "backend.cleanup"
    state.mkdir(mode=0o700)
    (state / "boot").write_text("00000000-0000-0000-0000-000000000001")
    result = subprocess.run([
        sys.executable, str(helper), "stop", str(tree.pids["backend"]), str(tree.script), "backend", str(state),
    ], env=tree.env, capture_output=True, text=True, timeout=15)
    if sys.platform == "linux":
        assert result.returncode != 0
        assert "Linux existing-tree descendant custody is unproven" in result.stderr
        assert state.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert not state.exists()
    assert select.select([tree.worker_lifetime], [], [], 5)[0], "requested tree survived old-state cleanup"
    assert os.read(tree.worker_lifetime, 1) == b""
    assert_shared_workloads_survive(tree)


def test_concurrent_cleanup_completion_reports_retry_without_reboot_advice(tmp_path: Path) -> None:
    state = tmp_path / "backend.cleanup"
    state.mkdir(mode=0o700)

    def completed_elsewhere(_address: str) -> None:
        state.rmdir()
        raise FileNotFoundError

    connection = Mock()
    connection.connect.side_effect = completed_elsewhere
    context = Mock()
    context.__enter__ = Mock(return_value=connection)
    context.__exit__ = Mock(return_value=False)
    with patch.object(dev_processes.socket, "socket", return_value=context):
        with pytest.raises(RuntimeError, match="session ended while connecting; retry Stop"):
            dev_processes.request_stop(state, 4321)


def test_failed_capture_cannot_discard_workers_after_the_root_exits(native_dev_tree: NativeDevTree) -> None:
    tree = native_dev_tree
    tree.retain_cleanup_marker = True
    native = Path(tree.env["DEV_TEST_REPO"]) / "mediaforce/core/_process_deadline.py"
    exit_trigger = tree.reparented.with_suffix(".exit")
    native.write_text(native.read_text() + f'''
from pathlib import Path
_original_process_tree = _process_tree
def _process_tree(*args, **kwargs):
    tree = _original_process_tree(*args, **kwargs)
    original_refresh = tree.refresh
    initial_capture = True
    def refresh(*args, **kwargs):
        nonlocal initial_capture
        if initial_capture:
            initial_capture = False
            original_refresh(*args, **kwargs)
            Path({str(exit_trigger)!r}).touch()
            deadline = time.monotonic() + 3
            while not Path({str(tree.reparented)!r}).exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert Path({str(tree.reparented)!r}).exists(), "backend did not exit independently"
            raise RuntimeError("injected capture failure after root exit")
        return original_refresh(*args, **kwargs)
    tree.refresh = refresh
    return tree
''')
    tree.pid_file.write_text(str(tree.pids["backend"]))
    first = subprocess.run(["/bin/bash", str(tree.script), "stop", "backend"], env=tree.env,
                           capture_output=True, text=True, timeout=15)
    assert first.returncode != 0
    assert "capture failure after root exit" in first.stderr
    assert tree.reparented.exists()
    for action in ("stop", "start"):
        retry = subprocess.run(["/bin/bash", str(tree.script), action, "backend"], env=tree.env,
                               capture_output=True, text=True, timeout=15)
        assert retry.returncode != 0, retry.stdout
        assert tree.pid_file.read_text() == str(tree.pids["backend"])
        assert not select.select([tree.worker_lifetime], [], [], .1)[0], "worker did not survive failed capture"
    assert_shared_workloads_survive(tree)


@pytest.mark.parametrize("component", ["backend", "frontend"])
def test_lost_cleanup_supervisor_never_discards_pending_custody(tmp_path: Path, component: str) -> None:
    script, lock, pid_file, log, env = prepare_dev_service(tmp_path, "absent", "idle", "owned")
    lock_bytes = lock.read_bytes()
    pid_file = pid_file if component == "backend" else pid_file.with_name("mediaforce-frontend.pid")
    pid_file.write_text("12345")
    state = pid_file.parent / f"{component}.cleanup"
    state.mkdir(mode=0o700)
    fixture_boot = "00000000-0000-0000-0000-000000000001"
    (state / "boot").write_text(fixture_boot)
    helper = Path(env["DEV_TEST_REPO"]) / "mediaforce/ops/dev_processes.py"
    helper.write_text(helper.read_text().replace(
        "\ndef main()", f"\ndef boot_id():\n    return {fixture_boot!r}\n\ndef main()",
    ))
    for action in ("start", "stop"):
        result = subprocess.run(["/bin/bash", str(script), action, component], env=env,
                                capture_output=True, text=True, timeout=15)
        assert result.returncode != 0, result.stdout
        assert "cleanup" in result.stderr
        assert pid_file.read_text() == "12345"
        assert state.exists()
    assert lock.read_bytes() == lock_bytes
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(call[0] == "nohup" for call in calls)


def test_stop_can_clear_lost_custody_only_after_a_proven_new_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "backend.cleanup"
    state.mkdir(mode=0o700)
    (state / "boot").write_text("00000000-0000-0000-0000-000000000001")
    monkeypatch.setattr(dev_processes, "boot_id", lambda: "00000000-0000-0000-0000-000000000002")
    monkeypatch.setattr(sys, "argv", ["dev_processes.py", "retry", "0", "unused", "backend", str(state)])
    with patch.object(dev_processes, "DevelopmentProcessTree") as tree:
        assert dev_processes.main() == 0
        tree.assert_not_called()
    assert not state.exists()


def test_prior_boot_removal_holds_publication_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "backend.cleanup"
    state.mkdir(mode=0o700)
    old_boot = "00000000-0000-0000-0000-000000000001"
    current_boot = "00000000-0000-0000-0000-000000000002"
    (state / "boot").write_text(old_boot)

    def assert_publication_locked() -> None:
        with open(str(state) + ".lock", "r+") as competing_creator:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competing_creator, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def locked_boot_read() -> str:
        assert_publication_locked()
        return current_boot

    original_remove = dev_processes.remove_state

    def locked_remove(marker: Path) -> None:
        assert_publication_locked()
        original_remove(marker)

    monkeypatch.setattr(dev_processes, "remove_state", locked_remove)
    monkeypatch.setattr(dev_processes, "boot_id", locked_boot_read)
    dev_processes.clear_previous_boot(state)
    assert not state.exists()
    assert dev_processes.publish_state(state)
    socket_marker = state / "control.sock"
    socket_marker.touch()
    dev_processes.clear_previous_boot(state)
    assert socket_marker.exists(), "another caller removed the new current-boot session"


def test_interrupted_disposal_does_not_leave_a_pending_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "backend.cleanup"
    assert dev_processes.publish_state(state)
    original_unlink = Path.unlink

    def interrupted_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.name == "boot":
            assert not state.exists(), "active marker still visible during disposal"
            raise KeyboardInterrupt
        original_unlink.__get__(path, Path)(missing_ok=missing_ok)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", interrupted_unlink)
        with pytest.raises(KeyboardInterrupt):
            dev_processes.remove_state(state)
    assert not state.exists()
    assert dev_processes.publish_state(state), "interrupted disposal blocked the next session"
    dev_processes.remove_state(state)
    remnants = list(tmp_path.glob(".backend.cleanup-removing-*"))
    assert len(remnants) == 1
    assert (remnants[0] / "boot").is_file()


def test_invalid_boot_receipt_preserves_pending_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "backend.cleanup"
    state.mkdir(mode=0o700)
    (state / "boot").write_text("truncated boot receipt")
    monkeypatch.setattr(dev_processes, "boot_id", lambda: "00000000-0000-0000-0000-000000000002")
    monkeypatch.setattr(sys, "argv", ["dev_processes.py", "retry", "0", "unused", "backend", str(state)])
    assert dev_processes.main() == 1
    assert state.exists()


def test_interrupted_setup_does_not_publish_an_incomplete_boot_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "backend.cleanup"

    def interrupted_boot_read() -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(dev_processes, "boot_id", interrupted_boot_read)
    with pytest.raises(KeyboardInterrupt):
        dev_processes.publish_state(state)
    assert not state.exists()
    assert not list(tmp_path.glob(".backend.cleanup-*"))


@pytest.mark.parametrize("blocked_action,component", [
    ("start", "backend"), ("restart", "backend"), ("restart", "all"), ("stop", "backend"),
])
def test_repeated_stop_retains_captured_worker_custody(
    native_dev_tree: NativeDevTree, blocked_action: str, component: str, tmp_path: Path,
) -> None:
    tree = native_dev_tree
    native = Path(tree.env["DEV_TEST_REPO"]) / "mediaforce/core/_process_deadline.py"
    failed = tree.pid_file.parent / "injected-failure"
    repaired = tree.pid_file.parent / "repair"
    completed = tree.pid_file.parent / "cleanup-proved"
    poll_failed = tree.pid_file.parent / "idle-poll-failed"
    native.write_text(native.read_text() + f'''
from pathlib import Path
_original_termination = _terminate_tree
def _terminate_tree(tree, reap):
    failed = Path({str(failed)!r})
    if not Path({str(repaired)!r}).exists():
        tree.signal_all(signal.SIGTERM)
        time.sleep(.15)
        failed.touch()
        raise RuntimeError("injected post-TERM cleanup failure")
    result = _original_termination(tree, reap)
    if result.succeeded:
        Path({str(completed)!r}).touch()
    return result

_original_process_tree = _process_tree
def _process_tree(*args, **kwargs):
    tree = _original_process_tree(*args, **kwargs)
    original_refresh = tree.refresh
    def refresh(*args, **kwargs):
        if Path({str(failed)!r}).exists() and not Path({str(poll_failed)!r}).exists():
            Path({str(poll_failed)!r}).touch()
            raise RuntimeError("injected idle custody refresh failure")
        return original_refresh(*args, **kwargs)
    tree.refresh = refresh
    return tree
''')
    tree.pid_file.write_text(str(tree.pids["backend"]))
    first = subprocess.run(["/bin/bash", str(tree.script), "stop", "backend"], env=tree.env,
                           capture_output=True, text=True, timeout=15)
    assert first.returncode != 0
    assert "injected post-TERM" in first.stderr
    assert tree.pid_file.read_text() == str(tree.pids["backend"])
    assert not select.select([tree.worker_lifetime], [], [], .1)[0], "worker did not survive injected failure"
    assert tree.reparented.exists(), "root did not exit before retry"
    deadline = time.monotonic() + 3
    while not poll_failed.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert poll_failed.exists(), "idle custody fault did not run"
    assert "idle custody refresh" in (tree.pid_file.parent / "backend.cleanup/error").read_text()
    # A concurrent Stop for a different root cannot consume this tree's success.
    different = subprocess.run([
        sys.executable, str(Path(tree.env["DEV_TEST_REPO"]) / "mediaforce/ops/dev_processes.py"),
        "stop", str(tree.pids["sibling"]), str(tree.script), "backend", str(tree.pid_file.parent / "backend.cleanup"),
    ], env=tree.env, capture_output=True, text=True, timeout=10)
    assert different.returncode != 0
    assert "another development root" in different.stderr
    assert_shared_workloads_survive(tree)
    blocked = subprocess.run(["/bin/bash", str(tree.script), blocked_action, component], env=tree.env,
                             capture_output=True, text=True, timeout=15)
    assert blocked.returncode != 0, blocked.stdout
    assert tree.pid_file.read_text() == str(tree.pids["backend"])
    assert not select.select([tree.worker_lifetime], [], [], .1)[0]
    calls = [json.loads(line) for line in Path(tree.env["DEV_TEST_LOG"]).read_text().splitlines()]
    assert not any(call[0] == "nohup" for call in calls), "new backend launched before cleanup"
    repaired.touch()
    if sys.platform == "linux":
        stop_native_backend(tree)
        assert tree.pid_file.read_text() == str(tree.pids["backend"])
        assert (tree.pid_file.parent / "backend.cleanup").exists()
        assert not completed.exists(), "external Linux custody was falsely certified"
        calls = [json.loads(line) for line in Path(tree.env["DEV_TEST_LOG"]).read_text().splitlines()]
        assert not any(call[0] == "nohup" for call in calls)
        assert_shared_workloads_survive(tree)
        return
    if blocked_action == "restart" and component == "backend":
        table = tmp_path / "listeners.json"
        table.write_text("[]")
        tree.env["DEV_TEST_TREE"] = str(table)
        with fixture_backend(tmp_path, tree.env, shutdown_finished=completed) as (started, descriptors):
            restarted = subprocess.run(["/bin/bash", str(tree.script), "restart", "backend"], env=tree.env,
                                       capture_output=True, text=True, timeout=20, pass_fds=descriptors)
            assert restarted.returncode == 0, restarted.stderr
            assert select.select([tree.worker_lifetime], [], [], 5)[0], "old worker survived restart"
            assert os.read(tree.worker_lifetime, 1) == b""
            assert int(tree.pid_file.read_text()) == json.loads(started.read_text())["pid"]
        stopped = subprocess.run(["/bin/bash", str(tree.script), "stop", "backend"], env=tree.env,
                                 capture_output=True, text=True, timeout=15)
        assert stopped.returncode == 0, stopped.stderr
    else:
        stop_native_backend(tree)
    assert not tree.pid_file.exists()
    assert not (tree.pid_file.parent / "backend.cleanup").exists()
    assert_shared_workloads_survive(tree)


@pytest.mark.parametrize("native_dev_tree", [
    (False, "python"), (True, "python"), (False, "Python"), (False, "python3.13t"),
], indirect=True)
def test_stop_preserves_real_shared_wrapper_and_sibling(native_dev_tree: NativeDevTree) -> None:
    env, rows = native_dev_tree.env, native_dev_tree.pids
    binary = str(Path(env["DEV_TEST_REPO"]) / ".venv/bin/mediaforce-web")
    assert binary in native_command(rows["wrapper"])
    assert "wrapper.py" in native_command(rows["wrapper"])
    stop_native_backend(native_dev_tree)
    assert_shared_workloads_survive(native_dev_tree)


def test_stop_uses_its_checkout_when_invoked_from_another_checkout(native_dev_tree: NativeDevTree, tmp_path: Path) -> None:
    foreign = tmp_path / "foreign checkout"
    package = foreign / "mediaforce"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('raise RuntimeError("foreign checkout imported")\n')
    stop_native_backend(native_dev_tree, cwd=foreign)
    assert_shared_workloads_survive(native_dev_tree)


def test_stop_resolves_shared_listener_tree_before_signalling(native_dev_tree: NativeDevTree) -> None:
    tree = native_dev_tree
    listeners = Path(tree.env["PATH"]) / "lsof"
    listeners.write_text("#!" + sys.executable + "\n" + f'''
from pathlib import Path
seen = Path({str(tree.pid_file.parent / 'listeners-seen')!r})
if not seen.exists():
    seen.touch()
    print({tree.pids['backend']})
    print({tree.pids['worker']})
''')
    result = subprocess.run(
        ["/bin/bash", str(tree.script), "stop", "backend"], env=tree.env,
        capture_output=True, text=True, timeout=15,
    )
    if sys.platform == "linux":
        assert result.returncode != 0
        assert "Linux existing-tree descendant custody is unproven" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
    assert select.select([tree.worker_lifetime], [], [], 5)[0], "owned tree survived"
    assert os.read(tree.worker_lifetime, 1) == b""
    assert_shared_workloads_survive(tree)


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


@pytest.mark.skipif(sys.platform != "linux", reason="native Linux external-tree qualification")
@pytest.mark.parametrize("action", ["stop", "restart"])
def test_linux_late_fork_reports_unproven_custody_through_launcher(
        native_dev_tree: NativeDevTree, action: str,
) -> None:
    tree = native_dev_tree
    repo = Path(tree.env["DEV_TEST_REPO"])
    source = Path(__file__).resolve().parents[1] / "mediaforce"
    # Use the complete real package, rather than the fixture's empty initializers.
    shutil.copytree(source, repo / "mediaforce", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    custody_module = repo / "mediaforce/core/_process_deadline.py"
    trigger = tree.reparented.with_suffix(".fork")
    record = tree.reparented.with_suffix(".late-fork.json")
    with custody_module.open("a") as output:
        output.write(f'''
# Fixture-only scheduling hook: actual discovery/pinning precedes the fork.
_native_refresh = _LinuxProcessTree.refresh
_fork_triggered = False
def _late_fork_refresh(self, timeout=0.0):
    global _fork_triggered
    _native_refresh(self, timeout)
    if _fork_triggered or {tree.pids["worker"]} not in self._processes:
        return
    _fork_triggered = True
    descriptor = self._processes[{tree.pids["worker"]}].process_descriptor
    from pathlib import Path
    Path({str(trigger)!r}).touch()
    deadline = time.monotonic() + 5
    while not _pidfd_exited(descriptor) and time.monotonic() < deadline:
        time.sleep(.001)
    assert _pidfd_exited(descriptor), "tracked worker did not exit"
_LinuxProcessTree.refresh = _late_fork_refresh
''')
    tree.pid_file.write_text(str(tree.pids["backend"]))
    lock = Path(tree.env["DEV_TEST_LOCK"])
    lock_bytes = lock.read_bytes()
    result = subprocess.run(["/bin/bash", str(tree.script), action, "backend"],
                            env=tree.env, capture_output=True, text=True, timeout=15)
    assert record.exists(), result.stderr
    rows = json.loads(record.read_text())
    assert rows["worker"] == tree.pids["worker"]
    assert rows["child"] > 1
    assert result.returncode != 0, result.stdout
    assert "Linux existing-tree descendant custody is unproven" in result.stderr
    assert "backend: stopped" not in result.stdout
    assert not select.select([tree.worker_lifetime], [], [], .1)[0], "late grandchild unexpectedly exited"
    assert tree.pid_file.read_text() == str(tree.pids["backend"])
    assert lock.read_bytes() == lock_bytes
    calls = [json.loads(line) for line in Path(tree.env["DEV_TEST_LOG"]).read_text().splitlines()]
    assert not any(call[0] == "nohup" for call in calls), "restart launched a replacement"
    assert_shared_workloads_survive(tree)
    # native_dev_tree's finally releases the grandchild via its inherited pipe
    # and proves lifetime EOF, independently of PID discovery or reuse.


@pytest.mark.skipif(sys.platform != "linux", reason="native Linux external-tree qualification")
def test_linux_retry_retains_specific_custody_reason(native_dev_tree: NativeDevTree) -> None:
    tree = native_dev_tree
    stop_native_backend(tree)
    deadline = time.monotonic() + 5
    error = tree.pid_file.parent / "backend.cleanup/error"
    while (not error.exists() or "Linux existing-tree" not in error.read_text()) and time.monotonic() < deadline:
        time.sleep(0.01)
    state = error.parent
    while time.monotonic() < deadline:
        with dev_processes.state_directory(state), socket.socket(socket.AF_UNIX) as client:
            try:
                client.connect("control.sock")
            except (ConnectionRefusedError, FileNotFoundError):
                break
        # Let the supervisor's idle check run between connection probes.
        time.sleep(0.5)
    else:
        pytest.fail("cleanup supervisor still accepts connections")
    result = subprocess.run(
        ["/bin/bash", str(tree.script), "stop", "backend"], env=tree.env,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert "Linux existing-tree descendant custody is unproven" in result.stderr
    assert tree.pid_file.read_text() == str(tree.pids["backend"])
    assert_shared_workloads_survive(tree)


def test_fixture_teardown_handles_missing_socket_and_removed_state(tmp_path: Path) -> None:
    state = tmp_path / "backend.cleanup"
    state.mkdir(mode=0o700)
    (state / "boot").write_text("test-owned")
    clear_test_owned_lost_cleanup(state)
    assert not state.exists()
    clear_test_owned_lost_cleanup(state)
