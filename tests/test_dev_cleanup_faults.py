import errno
import os
from collections.abc import Iterator
from pathlib import Path
import select
import stat
import subprocess
from unittest.mock import Mock, patch

import pytest

from mediaforce.ops import dev_processes
from tests.test_dev_process_tree import NativeDevTree, assert_shared_workloads_survive, development_tree


@pytest.fixture
def finite_custody_dev_tree(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[NativeDevTree]:
    # Retirement faults require modeled complete custody on Linux to reach disposal.
    # Native identity pins and lifetime-pipe teardown still own the finite processes.
    yield from development_tree(tmp_path, request, native_custody=False)


@pytest.mark.parametrize("resource_error", [errno.ENOSPC, errno.EDQUOT])
def test_retirement_needs_no_directory_allocation(
    finite_custody_dev_tree: NativeDevTree, resource_error: int,
) -> None:
    tree = finite_custody_dev_tree
    helper = Path(tree.env["DEV_TEST_REPO"]) / "mediaforce/ops/dev_processes.py"
    helper.write_text(helper.read_text().replace("\ndef main()", f'''
_original_mkdtemp = tempfile.mkdtemp
def _fault_mkdtemp(*args, **kwargs):
    if "-removing-" in kwargs.get("prefix", ""):
        raise OSError({resource_error}, "injected retirement allocation failure")
    return _original_mkdtemp(*args, **kwargs)
tempfile.mkdtemp = _fault_mkdtemp

def main()'''))
    tree.pid_file.write_text(str(tree.pids["backend"]))
    result = subprocess.run(["/bin/bash", str(tree.script), "stop", "backend"], env=tree.env,
                            capture_output=True, text=True, timeout=15)
    state = tree.pid_file.parent / "backend.cleanup"
    tree.retain_cleanup_marker = state.exists()
    assert result.returncode == 0, result.stderr
    assert select.select([tree.worker_lifetime], [], [], 5)[0], "owned worker survived"
    assert os.read(tree.worker_lifetime, 1) == b""
    assert_shared_workloads_survive(tree)
    assert not state.exists()
    assert not tree.pid_file.exists()


def test_partial_receipt_failure_does_not_need_another_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "backend.cleanup"
    original_write = Path.write_text
    original_mkdtemp = dev_processes.tempfile.mkdtemp

    def partial_write(path: Path, text: str) -> int:
        original_write.__get__(path, Path)(text[:8])
        raise OSError(errno.ENOSPC, "injected partial receipt")

    def allocate_once(*, prefix: str, **kwargs: Path) -> str:
        if "-removing-" in prefix:
            raise OSError(errno.ENOSPC, "injected second allocation")
        return original_mkdtemp(prefix=prefix, dir=kwargs["dir"])

    monkeypatch.setattr(dev_processes, "boot_id", lambda: "00000000-0000-0000-0000-000000000001")
    monkeypatch.setattr(Path, "write_text", partial_write)
    monkeypatch.setattr(dev_processes.tempfile, "mkdtemp", allocate_once)
    with pytest.raises(OSError, match="partial receipt"):
        dev_processes.publish_state(state)
    assert not state.exists()
    assert not list(tmp_path.glob(".backend.cleanup-*")), "unpublished partial receipt leaked"


def test_failed_retirement_keeps_completed_native_custody_retryable(finite_custody_dev_tree: NativeDevTree) -> None:
    tree = finite_custody_dev_tree
    helper = Path(tree.env["DEV_TEST_REPO"]) / "mediaforce/ops/dev_processes.py"
    repaired = tree.pid_file.parent / "repair-retirement"
    retry_connected = tree.pid_file.parent / "retirement-retry-connected"
    retirement_failure = "injected retirement rename failure"
    helper.write_text(helper.read_text().replace("\ndef main()", f'''
_original_rename = Path.rename
class _RepairGateSocket(socket.socket):
    def accept(self):
        connection, address = super().accept()
        if Path({str(repaired)!r}).exists():
            Path({str(retry_connected)!r}).touch()
        return connection, address
socket.socket = _RepairGateSocket
def _fault_rename(path, target):
    if path.name == "backend.cleanup" and not Path({str(retry_connected)!r}).exists():
        raise OSError({errno.ENOSPC}, {retirement_failure!r})
    return _original_rename(path, target)
Path.rename = _fault_rename

def main()'''))
    tree.pid_file.write_text(str(tree.pids["backend"]))
    first = subprocess.run(["/bin/bash", str(tree.script), "stop", "backend"], env=tree.env,
                           capture_output=True, text=True, timeout=15)
    state = tree.pid_file.parent / "backend.cleanup"
    tree.retain_cleanup_marker = state.exists()
    assert first.returncode != 0
    assert retirement_failure in first.stderr
    assert select.select([tree.worker_lifetime], [], [], 5)[0], "owned worker survived"
    assert os.read(tree.worker_lifetime, 1) == b""
    assert state.exists()
    assert tree.pid_file.read_text() == str(tree.pids["backend"])
    assert_shared_workloads_survive(tree)
    repaired.touch()
    retry = subprocess.run(["/bin/bash", str(tree.script), "stop", "backend"], env=tree.env,
                           capture_output=True, text=True, timeout=15)
    tree.retain_cleanup_marker = state.exists()
    assert retry.returncode == 0, retry.stderr
    assert retry_connected.exists(), "retry never contacted the retained supervisor"
    assert not state.exists()
    assert not tree.pid_file.exists()
    assert_shared_workloads_survive(tree)


def test_receipt_and_directory_sync_precede_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "backend.cleanup"
    fixture_boot = "00000000-0000-0000-0000-000000000001"
    events: list[str] = []
    original_rename = Path.rename

    def sync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            assert os.pread(descriptor, 64, 0).decode() == fixture_boot
            events.append("receipt")
        else:
            assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
            observed = os.fstat(descriptor)
            expected = (state.parent if state.exists() else next(tmp_path.glob(".backend.cleanup-candidate-*"))).stat()
            assert (observed.st_dev, observed.st_ino) == (expected.st_dev, expected.st_ino)
            events.append("published directory" if state.exists() else "candidate directory")

    def publish(path: Path, target: Path) -> Path:
        if target == state:
            events.append("publish")
        return original_rename.__get__(path, Path)(target)

    monkeypatch.setattr(dev_processes, "boot_id", lambda: fixture_boot)
    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(Path, "rename", publish)
    assert dev_processes.publish_state(state)
    assert events == ["receipt", "candidate directory", "publish", "published directory"]


@pytest.mark.parametrize("receipt", [None, "", "truncated receipt"])
def test_missing_or_invalid_receipt_never_proves_previous_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipt: str | None,
) -> None:
    state = tmp_path / "backend.cleanup"
    state.mkdir(mode=0o700)
    if receipt is not None:
        (state / "boot").write_text(receipt)
    monkeypatch.setattr(dev_processes, "boot_id", lambda: "00000000-0000-0000-0000-000000000002")
    with pytest.raises((OSError, ValueError)):
        dev_processes.clear_previous_boot(state)
    assert state.is_dir()


def test_interrupted_disposal_is_reclaimed_without_touching_active_state(tmp_path: Path) -> None:
    state = tmp_path / "backend.cleanup"
    assert dev_processes.publish_state(state)
    original_unlink = Path.unlink

    def interrupt(path: Path, missing_ok: bool = False) -> None:
        if path.name == "boot":
            raise KeyboardInterrupt
        original_unlink.__get__(path, Path)(missing_ok=missing_ok)

    with patch.object(Path, "unlink", interrupt), pytest.raises(KeyboardInterrupt):
        dev_processes.remove_state(state)
    remnants = list(tmp_path.glob(".backend.cleanup-removing-*"))
    assert len(remnants) == 1
    assert dev_processes.publish_state(state)
    receipt = (state / "boot").read_bytes()
    assert not remnants[0].exists(), "inert disposal artifact was never reclaimed"
    assert not dev_processes.publish_state(state)
    assert (state / "boot").read_bytes() == receipt


@pytest.mark.parametrize("failed_sync", [1, 2, 3])
def test_interrupted_receipt_sync_does_not_start_a_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_sync: int,
) -> None:
    state = tmp_path / "backend.cleanup"
    calls = 0

    def sync(_descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == failed_sync:
            raise OSError(errno.ENOSPC, "injected receipt sync failure")

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(dev_processes, "boot_id", lambda: "00000000-0000-0000-0000-000000000001")
    monkeypatch.setattr(dev_processes.sys, "argv", ["dev_processes.py", "stop", "4321", "unused", "backend", str(state)])
    with patch.object(dev_processes.subprocess, "Popen") as supervisor:
        assert dev_processes.main() == 1
        supervisor.assert_not_called()
    assert not state.exists()
    assert not list(tmp_path.glob(".backend.cleanup-*"))


def test_failed_unpublished_disposal_can_be_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "backend.cleanup"

    def interrupt_boot_read() -> str:
        raise KeyboardInterrupt

    def fail_disposal(_path: Path) -> None:
        raise OSError(errno.ENOSPC, "injected disposal failure")

    with monkeypatch.context() as fault:
        fault.setattr(dev_processes, "boot_id", interrupt_boot_read)
        fault.setattr(Path, "rmdir", fail_disposal)
        with pytest.raises(KeyboardInterrupt):
            dev_processes.publish_state(state)
    candidates = list(tmp_path.glob(".backend.cleanup-candidate-*"))
    assert len(candidates) == 1
    assert not state.exists()
    assert dev_processes.publish_state(state)
    assert not candidates[0].exists()


@pytest.mark.parametrize("foreign_artifact", ["symlink", "unknown contents"])
def test_disposal_sweep_preserves_foreign_artifacts(
    tmp_path: Path, foreign_artifact: str,
) -> None:
    state = tmp_path / "backend.cleanup"
    assert dev_processes.publish_state(state)
    with patch.object(dev_processes, "dispose_directory", side_effect=OSError("injected disposal failure")):
        dev_processes.remove_state(state)
    artifact, = tmp_path.glob(".backend.cleanup-removing-*")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "boot").write_text("foreign receipt")
    if foreign_artifact == "symlink":
        dev_processes.dispose_directory(artifact)
        artifact.symlink_to(foreign, target_is_directory=True)
    else:
        (artifact / "foreign-data").write_text("preserve this")
    assert dev_processes.publish_state(state)
    assert artifact.exists()
    assert (foreign / "boot").read_text() == "foreign receipt"
    if foreign_artifact == "unknown contents":
        assert (artifact / "foreign-data").read_text() == "preserve this"


def test_receipt_sync_supports_a_symlinked_state_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    actual = tmp_path / "actual-state"
    actual.mkdir(mode=0o700)
    alias = tmp_path / "state-alias"
    alias.symlink_to(actual, target_is_directory=True)
    state = alias / "backend.cleanup"
    synced: list[tuple[int, int]] = []
    original_sync = os.fsync

    def sync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode):
            synced.append((info.st_dev, info.st_ino))
        original_sync(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(dev_processes, "boot_id", lambda: "00000000-0000-0000-0000-000000000001")
    assert dev_processes.publish_state(state)
    assert (actual / "backend.cleanup/boot").read_bytes() == (state / "boot").read_bytes()
    expected = actual.stat()
    assert synced[-1] == (expected.st_dev, expected.st_ino)


@pytest.mark.parametrize("startup_failure", ["parent sync", "spawn"])
def test_consecutive_startup_and_retirement_failures_preserve_unproven_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, startup_failure: str, capsys: pytest.CaptureFixture[str],
) -> None:
    state = tmp_path / "backend.cleanup"
    current_boot = "00000000-0000-0000-0000-000000000001"
    monkeypatch.setattr(dev_processes, "boot_id", lambda: current_boot)
    monkeypatch.setattr(dev_processes.sys, "argv", ["dev_processes.py", "stop", "4321", "unused", "backend", str(state)])
    original_sync = dev_processes.sync_directory
    original_rename = Path.rename

    def fail_parent_sync(directory: Path) -> None:
        if directory == state.parent:
            raise OSError(errno.EIO, "injected parent sync failure")
        original_sync(directory)

    def fail_retirement(path: Path, target: Path) -> Path:
        if path == state:
            raise OSError(errno.ENOSPC, "injected retirement failure")
        return original_rename.__get__(path, Path)(target)

    with monkeypatch.context() as fault, patch.object(dev_processes.subprocess, "Popen") as spawn:
        fault.setattr(Path, "rename", fail_retirement)
        if startup_failure == "parent sync":
            fault.setattr(dev_processes, "sync_directory", fail_parent_sync)
        else:
            spawn.side_effect = OSError(errno.EIO, "injected spawn failure")
        assert dev_processes.main() == 1
        error = capsys.readouterr().err
        assert f"injected {startup_failure} failure" in error
        if startup_failure == "spawn":
            assert "injected retirement failure" in error
        if startup_failure == "parent sync":
            spawn.assert_not_called()
        else:
            spawn.assert_called_once()
    assert state.is_dir()
    assert (state / "boot").read_text() == current_boot
    monkeypatch.setattr(dev_processes.sys, "argv", ["dev_processes.py", "retry", "0", "unused", "backend", str(state)])
    with patch.object(dev_processes, "DevelopmentProcessTree") as tree:
        assert dev_processes.main() == 1
        tree.assert_not_called()
        assert state.exists()
        monkeypatch.setattr(dev_processes, "boot_id", lambda: "00000000-0000-0000-0000-000000000002")
        assert dev_processes.main() == 0
        tree.assert_not_called()
    assert not state.exists()


def test_completed_supervisor_preserves_a_replacement_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "backend.cleanup"
    assert dev_processes.publish_state(state)
    old = tmp_path / "old-completed-session"
    tree = Mock()
    connection = Mock()
    connection.makefile.return_value.__enter__ = Mock(return_value=Mock(readline=Mock(return_value=b"stop 4321\n")))
    connection.makefile.return_value.__exit__ = Mock(return_value=False)
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    server = Mock()
    server.__enter__ = Mock(return_value=server)
    server.__exit__ = Mock(return_value=False)
    accepts = 0

    def accept() -> tuple[Mock, str]:
        nonlocal accepts
        accepts += 1
        if accepts == 1:
            return connection, "unused"
        assert accepts == 2, "old supervisor kept retrying a replacement marker"
        state.rename(old)
        assert dev_processes.publish_state(state)
        (state / "new-session").write_text("preserve this current session")
        raise TimeoutError

    original_rename = Path.rename
    failed = False

    def refuse_once(path: Path, target: Path) -> Path:
        nonlocal failed
        if path == state and not failed:
            failed = True
            raise OSError(errno.ENOSPC, "injected retirement failure")
        return original_rename.__get__(path, Path)(target)

    server.accept.side_effect = accept
    monkeypatch.setattr(Path, "rename", refuse_once)
    with (patch.object(dev_processes.socket, "socket", return_value=server),
          patch.object(dev_processes, "DevelopmentProcessTree", return_value=tree)):
        result = dev_processes.serve(4321, "unused", "backend", state)
    assert result == 1
    assert (state / "new-session").read_text() == "preserve this current session"
    tree.stop.assert_called_once()
    tree.close.assert_called_once()


def test_native_completed_supervisor_cannot_retire_another_session(finite_custody_dev_tree: NativeDevTree) -> None:
    tree = finite_custody_dev_tree
    helper = Path(tree.env["DEV_TEST_REPO"]) / "mediaforce/ops/dev_processes.py"
    repaired = tree.pid_file.parent / "repair-retirement"
    returned = tree.pid_file.parent / "supervisor-returned"
    retirement_failure = "injected retirement failure"
    helper.write_text(helper.read_text().replace("\ndef main()", f'''
_original_rename = Path.rename
_original_serve = serve
def _fault_rename(path, target):
    if path.name == 'backend.cleanup' and not Path({str(repaired)!r}).exists():
        raise OSError({errno.ENOSPC}, {retirement_failure!r})
    return _original_rename(path, target)
Path.rename = _fault_rename
def serve(*args, **kwargs):
    try:
        return _original_serve(*args, **kwargs)
    finally:
        Path({str(returned)!r}).touch()

def main()'''))
    tree.pid_file.write_text(str(tree.pids["backend"]))
    first = subprocess.run(["/bin/bash", str(tree.script), "stop", "backend"], env=tree.env,
                           capture_output=True, text=True, timeout=15)
    state = tree.pid_file.parent / "backend.cleanup"
    tree.retain_cleanup_marker = state.exists()
    assert first.returncode != 0
    assert retirement_failure in first.stderr
    assert select.select([tree.worker_lifetime], [], [], 5)[0]
    assert os.read(tree.worker_lifetime, 1) == b""
    assert_shared_workloads_survive(tree)
    assert not returned.exists(), "retirement failure released the retained supervisor"
    old = state.with_name("old-completed-session")
    state.rename(old)
    state.mkdir(mode=0o700)
    (state / "boot").write_bytes((old / "boot").read_bytes())
    sentinel = state / "unpublished-session"
    sentinel.write_text("this belongs to the replacement")
    repaired.touch()
    deadline = dev_processes.time.monotonic() + 3
    while not returned.exists() and dev_processes.time.monotonic() < deadline:
        dev_processes.time.sleep(.02)
    assert returned.exists(), "old supervisor did not stop after losing its marker identity"
    assert sentinel.read_text() == "this belongs to the replacement"
    assert_shared_workloads_survive(tree)
    # This replacement was test-owned unpublished setup; no native capture ever
    # used it. The old session already proved worker EOF and returned.
    sentinel.unlink()
    dev_processes.remove_state(state)
    tree.retain_cleanup_marker = False
    dev_processes.dispose_directory(old)


def test_interruption_after_publication_retains_unproven_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "backend.cleanup"
    monkeypatch.setattr(dev_processes, "boot_id", lambda: "00000000-0000-0000-0000-000000000001")
    original_rename = Path.rename

    def interrupted_publication(path: Path, target: Path) -> Path:
        result = original_rename.__get__(path, Path)(target)
        if target == state:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(dev_processes.sys, "argv", ["dev_processes.py", "stop", "4321", "unused", "backend", str(state)])
    with monkeypatch.context() as fault, patch.object(dev_processes.subprocess, "Popen") as spawn:
        fault.setattr(Path, "rename", interrupted_publication)
        with pytest.raises(KeyboardInterrupt):
            dev_processes.main()
        spawn.assert_not_called()
    assert state.exists()
    monkeypatch.setattr(dev_processes.sys, "argv", ["dev_processes.py", "retry", "0", "unused", "backend", str(state)])
    with patch.object(dev_processes, "DevelopmentProcessTree") as tree:
        assert dev_processes.main() == 1
        tree.assert_not_called()
        assert state.exists()
        monkeypatch.setattr(dev_processes, "boot_id", lambda: "00000000-0000-0000-0000-000000000002")
        assert dev_processes.main() == 0
        tree.assert_not_called()
    assert not state.exists()
