import builtins
import fcntl
import errno
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
from unittest.mock import MagicMock, Mock
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

import pytest

from mediaforce.ops import dev_processes

SERVER = '''
import os, pathlib, select, signal, socket, subprocess, sys, time
if '--worker' in sys.argv and os.environ.get('TEST_STUBBORN') == '1':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
registry = pathlib.Path(os.environ['TEST_REGISTRY'])
fd = int(os.environ['TEST_LIFETIME_FD'])
if '--worker' in sys.argv:
    if os.environ.get('TEST_LATE_FORK') == '1':
        def late_fork(_signum, _frame):
            env = dict(os.environ, TEST_LATE_FORK='0', TEST_STUBBORN='1')
            orphan = subprocess.Popen([sys.executable, __file__, '--worker'], env=env, pass_fds=(fd,))
            while str(orphan.pid) not in registry.read_text().splitlines():
                time.sleep(.01)
            os._exit(0)
        signal.signal(signal.SIGTERM, late_fork)
    with registry.open('a') as output:
        output.write(str(os.getpid()) + '\\n')
    while not select.select([fd], [], [], .02)[0]:
        pass
    sys.exit(0)
with registry.open('a') as output:
    output.write(str(os.getpid()) + '\\n')
if '--config' in sys.argv and not pathlib.Path(sys.argv[sys.argv.index('--config')+1]).is_file():
    sys.exit(7)
worker = subprocess.Popen([sys.executable, __file__, '--worker'], pass_fds=(fd,))
while str(worker.pid) not in registry.read_text().splitlines():
    time.sleep(.01)
port = int(sys.argv[sys.argv.index('--port')+1])
if gate := os.environ.get('TEST_START_GATE'):
    while not pathlib.Path(gate).exists():
        if select.select([fd], [], [], .02)[0]:
            worker.wait(timeout=5)
            sys.exit(0)
with socket.socket() as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((os.environ.get('TEST_BIND_HOST', '127.0.0.1'), port)); listener.listen()
    while not select.select([fd], [], [], .02)[0]:
        if pathlib.Path(os.environ['TEST_CRASH']).exists():
            os._exit(3)
        if select.select([listener], [], [], 0)[0]:
            client, _ = listener.accept()
            if os.environ.get('TEST_ACTIVE_CLOSE') == '1':
                try:
                    client.sendall(b'ready')
                    client.shutdown(socket.SHUT_WR)
                except OSError:
                    pass  # A readiness probe can close before reading the reply.
            client.close()
worker.wait(timeout=5)
'''

# The fixture supplies the process inventory from native PGIDs of only the
# processes it launched. No installed ps/npm/launchctl or host process state.
PS = '''
import os, pathlib, sys
failure = pathlib.Path(os.environ['TEST_INVENTORY_FAILURE'])
if failure.exists():
    failure.with_suffix('.observed').touch()
    if failure.read_text() == 'malformed':
        print('unexpected inventory row')
        sys.exit(0)
    sys.exit(1)
for line in pathlib.Path(os.environ['TEST_REGISTRY']).read_text().splitlines():
    try:
        pid = int(line)
        print(pid, os.getpgid(pid))
    except ProcessLookupError:
        pass
'''


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def wait_until(predicate: Callable[[], bool], seconds: float = 8) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('behavior did not complete')
        time.sleep(.02)


@dataclass
class DevFixture:
    root: Path
    state: Path
    env: dict[str, str]
    writer: int
    foreign: subprocess.Popen[bytes] | None = None

    def run(self, action: str, component: str = 'backend') -> subprocess.CompletedProcess[str]:
        return subprocess.run(['bash', str(self.root / 'scripts/mediaforce-dev.sh'), action, component],
                              env=self.env, text=True, capture_output=True, timeout=25)

    def status(self, component: str = 'backend') -> dict[str, str | int]:
        result = self.run('status', component)
        return json.loads(result.stdout.split(': ', 1)[1])

    def launch_broker(self) -> subprocess.Popen[bytes]:
        port = self.env['MEDIAFORCE_WEB_PORT']
        return subprocess.Popen(
            [sys.executable, str(self.root / 'mediaforce/ops/dev_processes.py'),
             'serve', 'backend', '--state-dir', str(self.state), '--cwd', str(self.root),
             '--host', '127.0.0.1', '--port', port, '--',
             str(self.root / '.venv/bin/mediaforce-web'), '--port', port],
            env=self.env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def assert_empty(self) -> None:
        wait_until(lambda: self.status()['state'] == 'stopped')
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', int(self.env['MEDIAFORCE_WEB_PORT'])))


@pytest.fixture
def dev(tmp_path: Path) -> Iterator[DevFixture]:
    source = Path(__file__).resolve().parents[1]
    root = tmp_path / 'checkout with spaces'
    for name in ('scripts', 'mediaforce/ops', '.venv/bin', 'frontend', 'bin'):
        (root / name).mkdir(parents=True)
    shutil.copyfile(source / 'scripts/mediaforce-dev.sh', root / 'scripts/mediaforce-dev.sh')
    shutil.copyfile(source / 'mediaforce/ops/dev_processes.py', root / 'mediaforce/ops/dev_processes.py')
    (root / '.venv/bin/python').symlink_to(sys.executable)
    for name, code in (('.venv/bin/mediaforce-web', SERVER), ('bin/npm', SERVER), ('bin/ps', PS)):
        executable = root / name
        executable.write_text('#!' + sys.executable + '\n' + code)
        executable.chmod(0o755)
    state = tmp_path / 'state'
    state.mkdir()
    registry = tmp_path / 'processes'
    registry.touch()
    env = dict(os.environ, MEDIAFORCE_DEV_STATE_DIR=str(state),
               MEDIAFORCE_WEB_PORT=str(free_port()), MEDIAFORCE_FRONTEND_DEV_PORT=str(free_port()),
               PATH=str(root / 'bin') + os.pathsep + os.environ['PATH'],
               TEST_REGISTRY=str(registry), TEST_CRASH=str(tmp_path / 'crash'),
               TEST_INVENTORY_FAILURE=str(tmp_path / 'inventory-failure'))
    # Popen closes arbitrary inherited descriptors. The fixture server receives
    # its teardown endpoint through a named FIFO opened by its own wrapper.
    fifo = tmp_path / 'lifetime'
    os.mkfifo(fifo)
    writer = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    # Replace the wrapper with one that acquires the FIFO, then execs the server.
    for executable in (root / '.venv/bin/mediaforce-web', root / 'bin/npm'):
        code = executable.read_text().replace("fd = int(os.environ['TEST_LIFETIME_FD'])", "fd = os.open(os.environ['TEST_LIFETIME_FIFO'], os.O_RDONLY | os.O_NONBLOCK)\nos.set_inheritable(fd, True)")
        executable.write_text(code)
    env['TEST_LIFETIME_FIFO'] = str(fifo)
    fixture = DevFixture(root, state, env, writer)
    try:
        yield fixture
    finally:
        os.close(writer)
        # FIFO EOF gives every test-owned worker independent teardown even when
        # a deliberately broken launcher fails its group cleanup.
        for component in ('frontend', 'backend'):
            fixture.run('stop', component)
        if fixture.foreign:
            fixture.foreign.wait(timeout=8)


@pytest.mark.parametrize('component', ['backend', 'frontend'])
def test_start_reuse_stop_and_restart(dev: DevFixture, component: str) -> None:
    assert dev.run('start', component).returncode == 0
    first = dev.status(component)
    assert first['state'] == 'running'
    assert dev.run('start', component).returncode == 0
    assert dev.status(component)['pid'] == first['pid']
    assert dev.run('restart', component).returncode == 0
    assert dev.status(component)['pid'] != first['pid']
    assert dev.run('stop', component).returncode == 0
    assert dev.status(component)['state'] == 'stopped'


@pytest.mark.parametrize('stubborn', ['0', '1'])
def test_server_crash_cleans_orphan_before_next_start(dev: DevFixture, stubborn: str) -> None:
    dev.env['TEST_STUBBORN'] = stubborn
    assert dev.run('start').returncode == 0
    pid = dev.status()['pid']
    Path(dev.env['TEST_CRASH']).touch()
    dev.assert_empty()
    assert 'empty; leader reaped' in (dev.state / 'backend.log').read_text()
    # The port being free alone is not enough: the orphan must have exited too.
    for candidate in Path(dev.env['TEST_REGISTRY']).read_text().splitlines():
        try:
            assert os.getpgid(int(candidate)) != pid
        except ProcessLookupError:
            pass


def test_launcher_sigkill_triggers_watchdog_cleanup(dev: DevFixture) -> None:
    dev.env['TEST_STUBBORN'] = '1'
    assert dev.run('start').returncode == 0
    status = dev.status()
    os.kill(int(status['launcher']), signal.SIGKILL)
    dev.assert_empty()
    assert 'empty; leader reaped' in (dev.state / 'backend.log').read_text()


def test_stop_kills_stubborn_descendants_and_preserves_foreign_server(dev: DevFixture) -> None:
    dev.env['TEST_STUBBORN'] = '1'
    foreign_env = dict(dev.env, TEST_CRASH=str(dev.root / 'no-crash'))
    foreign_port = free_port()
    foreign = subprocess.Popen([str(dev.root / '.venv/bin/mediaforce-web'), '--port', str(foreign_port)],
                               env=foreign_env, start_new_session=True)
    dev.foreign = foreign
    wait_until(lambda: dev_processes.port_open('127.0.0.1', foreign_port))
    assert dev.run('start').returncode == 0
    assert dev.run('stop').returncode == 0
    assert foreign.poll() is None
    assert dev_processes.port_open('127.0.0.1', foreign_port)


@pytest.mark.parametrize('foreign_host', ['127.0.0.1', '0.0.0.0'])
def test_occupied_port_preserves_foreign_listener(dev: DevFixture, foreign_host: str) -> None:
    port = int(dev.env['MEDIAFORCE_WEB_PORT'])
    foreign = subprocess.Popen([str(dev.root / '.venv/bin/mediaforce-web'), '--port', str(port)],
                               env=dict(dev.env, TEST_BIND_HOST=foreign_host), start_new_session=True)
    dev.foreign = foreign
    wait_until(lambda: dev_processes.port_open('127.0.0.1', port))
    assert dev.run('start').returncode != 0
    assert foreign.poll() is None
    assert dev.run('stop').returncode == 0
    assert dev_processes.port_open('127.0.0.1', port)


def test_stale_pid_records_and_runtime_lock_are_not_process_authority(dev: DevFixture) -> None:
    stale = dev.state / 'mediaforce-web.pid'
    lock = dev.state / 'mediaforce-web.lock'
    stale.write_text('1\n')
    lock.write_text('runtime owner\n')
    assert dev.run('stop').returncode == 0
    assert dev.run('start').returncode == 0
    assert dev.run('stop').returncode == 0
    assert stale.read_text() == '1\n'
    assert lock.read_text() == 'runtime owner\n'


def test_leader_is_reaped_only_after_descendants_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    remaining = iter([{42}, set(), set()])
    def observe_descendants(_pgid: int) -> set[int]:
        result = next(remaining)
        events.append('descendants remain' if result else 'descendants empty')
        return result
    child = Mock(spec=subprocess.Popen, pid=123)
    def wait() -> int:
        events.append('reap leader')
        return 0
    child.wait.side_effect = wait
    monkeypatch.setattr(dev_processes.os, 'killpg', lambda *_args: events.append('signal group'))
    monkeypatch.setattr(dev_processes, 'reap_descendants', observe_descendants)
    monkeypatch.setattr(dev_processes, 'leader_exited', lambda _pid: True)
    def released_group_snapshot(_pgid: int) -> set[int]:
        if 'reap leader' in events:
            raise OSError('snapshot failed after the leader was reaped')
        return set()
    monkeypatch.setattr(dev_processes, 'group_members', released_group_snapshot)
    assert dev_processes.stop_group(child) == 0
    assert events[0] == 'signal group'
    assert 'descendants remain' in events
    assert events[-2:] == ['descendants empty', 'reap leader']


def test_concurrent_starts_share_one_owner(dev: DevFixture) -> None:
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _index: dev.run('start'), range(3)))
    assert all(result.returncode == 0 for result in results)
    owners = {json.loads(result.stdout.split(': ', 1)[1])['pid'] for result in results}
    assert owners == {dev.status()['pid']}
    assert dev.run('stop').returncode == 0


@pytest.mark.parametrize('malformed', [False, True])
def test_observation_failure_retains_owner_until_cleanup_can_finish(dev: DevFixture, malformed: bool) -> None:
    dev.env['TEST_STUBBORN'] = '1'
    broker = dev.launch_broker()
    failure = Path(dev.env['TEST_INVENTORY_FAILURE'])
    stopping = None
    try:
        wait_until(lambda: dev.status()['state'] == 'running')
        owner = dev.status()
        failure.write_text('malformed' if malformed else '')
        stopping = subprocess.Popen(['bash', str(dev.root / 'scripts/mediaforce-dev.sh'), 'stop', 'backend'],
                                    env=dev.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        wait_until(lambda: failure.with_suffix('.observed').exists())
        status = dev.status()
        assert status['state'] == 'stopping'
        assert status['guardian'] == owner['guardian']
        assert stopping.poll() is None
        failure.unlink()
        output, errors = stopping.communicate(timeout=15)
        assert stopping.returncode == 0, (output, errors)
        dev.assert_empty()
    finally:
        failure.unlink(missing_ok=True)
        if stopping is not None:
            stopping.kill(); stopping.wait(timeout=5)
        broker.kill(); broker.wait(timeout=5)


@pytest.fixture
def worker_receipt(dev: DevFixture, tmp_path: Path) -> Iterator[int]:
    receipt = tmp_path / 'worker-lifetime'
    os.mkfifo(receipt)
    reader = os.open(receipt, os.O_RDONLY | os.O_NONBLOCK)
    dev.env['TEST_WORKER_RECEIPT'] = str(receipt)
    dev.env['TEST_STUBBORN'] = '1'
    executable = dev.root / '.venv/bin/mediaforce-web'
    executable.write_text(executable.read_text().replace(
        "if '--worker' in sys.argv:\n",
        "if '--worker' in sys.argv:\n    receipt = os.open(os.environ['TEST_WORKER_RECEIPT'], os.O_WRONLY)\n",
    ))
    # An absolute pinned reader allows genuine ENOENT without falling through to
    # an installed host ps. Other launcher tests keep their existing fixture.
    helper = dev.root / 'mediaforce/ops/dev_processes.py'
    helper.write_text(helper.read_text().replace('["ps",', f'[{str(dev.root / "bin/ps")!r},'))
    try:
        yield reader
    finally:
        os.close(reader)


def receipt_closed(reader: int) -> bool:
    try:
        return os.read(reader, 1) == b''
    except BlockingIOError:
        return False


@pytest.mark.parametrize('trigger', ['stop', 'crash'])
@pytest.mark.parametrize('inventory', ['missing', 'failed', 'malformed'])
def test_persistent_inventory_failure_escalates_without_releasing_custody(
    dev: DevFixture, worker_receipt: int, trigger: str, inventory: str,
) -> None:
    failure = Path(dev.env['TEST_INVENTORY_FAILURE'])
    reader = dev.root / 'bin/ps'
    reader_bytes = reader.read_bytes()
    # Qualify missing inventory before startup as well as failures after Start.
    if inventory == 'missing':
        reader.unlink()
    stopping = None
    try:
        assert dev.run('start').returncode == 0
        owner = dev.status()
        assert not receipt_closed(worker_receipt)
        if inventory != 'missing':
            failure.write_text('malformed' if inventory == 'malformed' else '')
        if trigger == 'stop':
            stopping = subprocess.Popen(
                ['bash', str(dev.root / 'scripts/mediaforce-dev.sh'), 'stop', 'backend'],
                env=dev.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        else:
            Path(dev.env['TEST_CRASH']).touch()
        logfile = dev.state / 'backend.log'
        wait_until(lambda: 'cleanup pending' in logfile.read_text())
        # FIFO EOF independently proves the TERM-ignoring worker died, even
        # though unavailable inventory must keep the leader and lock reserved.
        wait_until(lambda: receipt_closed(worker_receipt), dev_processes.TERM_SECONDS + 5)
        status = dev.status()
        assert status['state'] == 'stopping'
        assert status['guardian'] == owner['guardian']
        if stopping is not None:
            assert stopping.poll() is None
        assert 'leader reaped' not in logfile.read_text()
        with (dev.state / 'backend.lock').open('a') as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        failure.unlink(missing_ok=True)
        if inventory == 'missing':
            reader.write_bytes(reader_bytes)
            reader.chmod(0o755)
        if stopping is not None:
            output, errors = stopping.communicate(timeout=15)
            assert stopping.returncode == 0, (output, errors)
        dev.assert_empty()
        assert 'empty; leader reaped' in logfile.read_text()
    finally:
        failure.unlink(missing_ok=True)
        if not reader.exists():
            reader.write_bytes(reader_bytes)
            reader.chmod(0o755)
        if stopping is not None and stopping.poll() is None:
            stopping.kill(); stopping.wait(timeout=5)


def test_persistent_inventory_failure_logs_once_until_recovery(
    dev: DevFixture, worker_receipt: int,
) -> None:
    failure = Path(dev.env['TEST_INVENTORY_FAILURE'])
    try:
        assert dev.run('start').returncode == 0
        failure.touch()
        Path(dev.env['TEST_CRASH']).touch()
        logfile = dev.state / 'backend.log'
        wait_until(lambda: 'cleanup pending' in logfile.read_text())
        initial = logfile.read_bytes()
        time.sleep(dev_processes.POLL_SECONDS * 12)
        assert logfile.read_bytes() == initial, 'an unchanged observation failure floods the component log'
        failure.write_text('malformed')
        wait_until(lambda: logfile.read_bytes() != initial)
        changed = logfile.read_bytes()
        assert changed.count(b'cleanup pending') == 2
        time.sleep(dev_processes.POLL_SECONDS * 12)
        assert logfile.read_bytes() == changed, 'the changed error should be reported once too'
        assert dev.status()['state'] == 'stopping'
    finally:
        failure.unlink(missing_ok=True)
    dev.assert_empty()



def test_symlink_command_from_another_directory_reaches_same_owner(dev: DevFixture, tmp_path: Path) -> None:
    alias = tmp_path / 'alias'
    alias.symlink_to(dev.root, target_is_directory=True)
    assert dev.run('start').returncode == 0
    before = dev.status()
    result = subprocess.run(['bash', str(alias / 'scripts/mediaforce-dev.sh'), 'status', 'backend'],
                            cwd=tmp_path, env=dev.env, text=True, capture_output=True, timeout=5)
    assert json.loads(result.stdout.split(': ', 1)[1])['pid'] == before['pid']
    assert dev.run('stop').returncode == 0


@pytest.mark.parametrize('relative', [True, False])
def test_config_path_resolves_from_checkout(dev: DevFixture, relative: bool) -> None:
    config = dev.root / 'settings.toml'
    config.write_text('[state]\n')
    dev.env['MEDIAFORCE_CONFIG_PATH'] = config.name if relative else str(config)
    assert dev.run('start').returncode == 0
    assert dev.run('stop').returncode == 0


def test_concurrent_stops_wait_for_same_group_completion(dev: DevFixture) -> None:
    dev.env['TEST_STUBBORN'] = '1'
    assert dev.run('start').returncode == 0
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _index: dev.run('stop'), range(3)))
    assert all(result.returncode == 0 for result in results)
    dev.assert_empty()


def test_worker_fork_during_stop_is_cleaned_with_its_group(dev: DevFixture) -> None:
    dev.env['TEST_LATE_FORK'] = '1'
    assert dev.run('start').returncode == 0
    group = dev.status()['pid']
    assert dev.run('stop').returncode == 0
    pids = Path(dev.env['TEST_REGISTRY']).read_text().splitlines()
    assert len(pids) == 3, 'the original worker did not fork at the stop boundary'
    for pid in pids:
        try:
            assert os.getpgid(int(pid)) != group
        except ProcessLookupError:
            pass


@pytest.mark.parametrize('report_error', [OSError('disk full'), ValueError('closed output stream')])
def test_completion_log_failure_never_retries_a_reaped_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report_error: Exception,
) -> None:
    child = Mock(spec=subprocess.Popen, pid=123)
    cleanup = Mock(return_value=0)
    connection = Mock(spec=socket.socket)
    monkeypatch.setattr(dev_processes, 'enable_subreaper', lambda: None)
    monkeypatch.setattr(dev_processes, 'port_open', lambda _host, _port: False)
    monkeypatch.setattr(dev_processes.socket, 'socket', MagicMock())
    monkeypatch.setattr(dev_processes.signal, 'signal', Mock())
    monkeypatch.setattr(dev_processes.subprocess, 'Popen', Mock(return_value=child))
    monkeypatch.setattr(dev_processes, 'leader_exited', lambda _pid: True)
    monkeypatch.setattr(dev_processes, 'stop_group', cleanup)
    printed = False
    def report(message: str, **_kwargs: object) -> None:
        nonlocal printed
        if not printed and message.startswith('group '):
            printed = True
            raise report_error
    monkeypatch.setattr(builtins, 'print', report)
    dev_processes.guard_group(connection, ['fixture'], tmp_path, '127.0.0.1', 1234,
                              str(tmp_path / 'control.sock'))
    cleanup.assert_called_once_with(child)


def test_failed_spawn_releases_owner_and_start_can_be_retried(dev: DevFixture) -> None:
    executable = dev.root / '.venv/bin/mediaforce-web'
    original = executable.read_bytes()
    executable.unlink()
    began = time.monotonic()
    result = dev.run('start')
    assert result.returncode != 0
    assert time.monotonic() - began < 5, 'known failed launch waited for the startup deadline'
    assert json.loads(result.stdout.split(': ', 1)[1]).get('error')
    assert dev.status()['state'] == 'stopped'
    assert dev.run('stop').returncode == 0
    executable.write_bytes(original)
    executable.chmod(0o755)
    assert dev.run('start').returncode == 0
    assert dev.run('stop').returncode == 0


def test_launcher_restores_child_exit_custody_before_fork(tmp_path: Path) -> None:
    module_path = dev_processes.__file__
    assert module_path is not None
    helper = Path(module_path).resolve()
    probe = '''
import importlib.util, json, os, signal, sys
from pathlib import Path
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
spec = importlib.util.spec_from_file_location('dev_custody_probe', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
os.chdir(sys.argv[2])
def before_fork():
    print(json.dumps({'retains_child_exit': signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL}))
    raise OSError('fixture stops before process creation')
module.os.fork = before_fork
try:
    module.serve('backend', ['fixture'], Path.cwd(), '127.0.0.1', 1234)
except OSError:
    pass
'''
    result = subprocess.run([sys.executable, '-c', probe, str(helper), str(tmp_path)],
                            text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['retains_child_exit']


def test_start_with_inherited_ignored_child_signal_still_stops(dev: DevFixture) -> None:
    entry = '''
import os, signal, sys
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
os.execv(sys.executable, [sys.executable, sys.argv[1], 'start', 'backend'])
'''
    result = subprocess.run([sys.executable, '-c', entry, str(dev.root / 'mediaforce/ops/dev_processes.py')],
                            env=dev.env, text=True, capture_output=True, timeout=25)
    assert result.returncode == 0, result.stderr
    assert dev.run('stop').returncode == 0
    dev.assert_empty()


def test_restart_after_server_active_close_reuses_available_port(dev: DevFixture) -> None:
    dev.env['TEST_ACTIVE_CLOSE'] = '1'
    assert dev.run('start').returncode == 0
    first = dev.status()['pid']
    with socket.create_connection(('127.0.0.1', int(dev.env['MEDIAFORCE_WEB_PORT']))) as client:
        assert client.recv(5) == b'ready'
        assert client.recv(1) == b''  # Server sends FIN before the client closes.
    assert dev.run('restart').returncode == 0
    assert dev.status()['pid'] != first
    assert dev.run('stop').returncode == 0
    dev.assert_empty()


@pytest.mark.parametrize('guardian_failed', [False, True])
def test_silent_control_client_preserves_running_or_failed_owner(
    dev: DevFixture, guardian_failed: bool,
) -> None:
    broker = dev.launch_broker()
    try:
        wait_until(lambda: dev.status()['state'] == 'running')
        owner = dev.status()
        if guardian_failed:
            os.kill(int(owner['guardian']), signal.SIGKILL)
            wait_until(lambda: dev.status()['state'] == 'failed')
        stalled = "import socket,time; s=socket.socket(socket.AF_UNIX); s.connect('backend.sock'); time.sleep(2.5)"
        subprocess.run([sys.executable, '-c', stalled], cwd=dev.state, check=True, timeout=5)
        status = dev.status()
        assert status['state'] == ('failed' if guardian_failed else 'running')
        assert status['pid'] == owner['pid']
        assert os.getpgid(int(owner['pid'])) == owner['pid']
        if guardian_failed:
            assert dev.run('start').returncode != 0
            assert dev.status()['state'] == 'failed'
        else:
            assert dev.run('stop').returncode == 0
    finally:
        # This fixture is the broker's parent and has not reaped it; its PID
        # remains reserved even when the planted client fault made it exit.
        try:
            os.kill(broker.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        broker.wait(timeout=5)


@pytest.mark.parametrize('component,variable', [
    ('backend', 'MEDIAFORCE_WEB_PORT'), ('frontend', 'MEDIAFORCE_FRONTEND_DEV_PORT'),
])
@pytest.mark.parametrize('port', ['-1', '0', '87777'])
def test_invalid_development_port_is_rejected_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str, variable: str, port: str,
) -> None:
    monkeypatch.setenv(variable, port)
    with pytest.raises(ValueError):
        dev_processes.component_command(component, tmp_path)


def test_broker_waits_for_a_temporary_lock_probe(dev: DevFixture, tmp_path: Path) -> None:
    observed = tmp_path / 'lock-probe-observed'
    script = """
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('fixture_launcher', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
original = module.fcntl.flock
def flock(fd, operation):
    try:
        return original(fd, operation)
    except BlockingIOError:
        Path(sys.argv[3]).touch()
        raise
module.fcntl.flock = flock
os.chdir(sys.argv[2])
raise SystemExit(module.serve('backend', [sys.argv[4], '--port', sys.argv[5]],
                            Path(sys.argv[6]), '127.0.0.1', int(sys.argv[5])))
"""
    broker = None
    try:
        with (dev.state / 'backend.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            broker = subprocess.Popen(
                [sys.executable, '-c', script, str(dev.root / 'mediaforce/ops/dev_processes.py'),
                 str(dev.state), str(observed), str(dev.root / '.venv/bin/mediaforce-web'),
                 dev.env['MEDIAFORCE_WEB_PORT'], str(dev.root)], env=dev.env,
                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            wait_until(observed.exists)
        wait_until(lambda: dev.status()['state'] == 'running')
        assert dev.run('stop').returncode == 0
    finally:
        if broker is not None:
            broker.kill()
            broker.wait(timeout=5)


def test_start_retries_after_observing_a_temporary_lock_probe(dev: DevFixture, tmp_path: Path) -> None:
    observed = tmp_path / 'start-probe-observed'
    script = """
import importlib.util, json, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('fixture_launcher', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
original = module.request
def request(component, action):
    status = original(component, action)
    Path(sys.argv[3]).write_text(status['state'])
    return status
module.request = request
os.chdir(sys.argv[2])
result = module.start('backend', [sys.argv[4], '--port', sys.argv[5]],
                      Path(sys.argv[6]), '127.0.0.1', int(sys.argv[5]))
print(json.dumps(result))
raise SystemExit(result['state'] != 'running')
"""
    with (dev.state / 'backend.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        starting = subprocess.Popen(
            [sys.executable, '-c', script, str(dev.root / 'mediaforce/ops/dev_processes.py'),
             str(dev.state), str(observed), str(dev.root / '.venv/bin/mediaforce-web'),
             dev.env['MEDIAFORCE_WEB_PORT'], str(dev.root)], env=dev.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        wait_until(observed.exists)
        assert observed.read_text() == 'stopping'
    output, errors = starting.communicate(timeout=25)
    assert starting.returncode == 0, (output, errors)
    assert dev.run('stop').returncode == 0


def test_status_queued_behind_a_silent_client_completes(dev: DevFixture, tmp_path: Path) -> None:
    broker = dev.launch_broker()
    first = second = None
    ready = tmp_path / 'first-client'; queued = tmp_path / 'second-client'
    try:
        wait_until(lambda: dev.status()['state'] == 'running')
        os.kill(broker.pid, signal.SIGSTOP)
        silent = "import socket,time,sys; from pathlib import Path; s=socket.socket(socket.AF_UNIX); s.connect('backend.sock'); Path(sys.argv[1]).touch(); time.sleep(5)"
        first = subprocess.Popen([sys.executable, '-c', silent, str(ready)], cwd=dev.state)
        wait_until(ready.exists)
        script = """
import importlib.util, json, os, socket, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('fixture_launcher', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
original = socket.socket
class ObservedSocket(original):
    def sendall(self, data, *args):
        result = super().sendall(data, *args)
        Path(sys.argv[3]).touch()
        return result
module.socket.socket = ObservedSocket
os.chdir(sys.argv[2])
print(json.dumps(module.request('backend', 'status')))
"""
        second = subprocess.Popen(
            [sys.executable, '-c', script, str(dev.root / 'mediaforce/ops/dev_processes.py'),
             str(dev.state), str(queued)], env=dev.env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        wait_until(queued.exists)
        time.sleep(.25)  # Both requests are queued before the server's client timer starts.
        os.kill(broker.pid, signal.SIGCONT)
        output, errors = second.communicate(timeout=5)
        assert second.returncode == 0, errors
        assert json.loads(output)['state'] == 'running'
        assert dev.run('stop').returncode == 0
    finally:
        os.kill(broker.pid, signal.SIGCONT)
        broker.kill(); broker.wait(timeout=5)
        for client in (first, second):
            if client is not None:
                client.kill(); client.wait(timeout=5)


@pytest.mark.parametrize('error', [OSError(errno.ENOBUFS, 'status buffer unavailable'), OSError(errno.EPROTOTYPE, 'closing socket')])
def test_status_send_failure_cannot_skip_group_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    child = Mock(spec=subprocess.Popen, pid=123)
    cleanup = Mock(return_value=0)
    connection = Mock(spec=socket.socket)
    report = Mock(side_effect=[None, error])
    monkeypatch.setattr(dev_processes, 'enable_subreaper', lambda: None)
    monkeypatch.setattr(dev_processes, 'port_open', lambda _host, _port: False)
    monkeypatch.setattr(dev_processes.socket, 'socket', MagicMock())
    monkeypatch.setattr(dev_processes.signal, 'signal', Mock())
    monkeypatch.setattr(dev_processes.subprocess, 'Popen', Mock(return_value=child))
    monkeypatch.setattr(dev_processes, 'leader_exited', lambda _pid: True)
    monkeypatch.setattr(dev_processes, 'stop_group', cleanup)
    monkeypatch.setattr(dev_processes, 'send_status', report)
    try:
        dev_processes.guard_group(connection, ['fixture'], tmp_path, '127.0.0.1', 1234,
                                  str(tmp_path / 'control.sock'))
    except RuntimeError:
        pass
    cleanup.assert_called_once_with(child)


def test_unknown_post_spawn_failure_retains_failed_owner(dev: DevFixture) -> None:
    script = """
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('fixture_launcher', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
original = module.send_status
def report(connection, status):
    if status['state'] == 'stopping':
        raise RuntimeError('fixture interrupts post-spawn completion')
    original(connection, status)
module.send_status = report
os.chdir(sys.argv[2])
raise SystemExit(module.serve('backend', [sys.argv[3], '--port', sys.argv[4]],
                            Path(sys.argv[5]), '127.0.0.1', int(sys.argv[4])))
"""
    broker = subprocess.Popen(
        [sys.executable, '-c', script, str(dev.root / 'mediaforce/ops/dev_processes.py'),
         str(dev.state), str(dev.root / '.venv/bin/mediaforce-web'),
         dev.env['MEDIAFORCE_WEB_PORT'], str(dev.root)], env=dev.env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_until(lambda: dev.status()['state'] == 'running')
        dev.run('stop')
        assert dev.status()['state'] == 'failed'
        assert dev.run('start').returncode != 0
        assert dev.status()['state'] == 'failed'
    finally:
        broker.kill(); broker.wait(timeout=5)


def test_repeated_stop_keeps_control_responsive_during_pending_cleanup(dev: DevFixture, tmp_path: Path) -> None:
    script = r"""
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('fixture_launcher', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
original = module.socket.socketpair
def pair():
    parent, child = original()
    parent.setsockopt(module.socket.SOL_SOCKET, module.socket.SO_SNDBUF, 512)
    return parent, child
module.socket.socketpair = pair
os.chdir(sys.argv[2])
raise SystemExit(module.serve('backend', [sys.argv[3], '--port', sys.argv[4]],
                            Path(sys.argv[5]), '127.0.0.1', int(sys.argv[4])))
"""
    broker = subprocess.Popen(
        [sys.executable, '-c', script, str(dev.root / 'mediaforce/ops/dev_processes.py'),
         str(dev.state), str(dev.root / '.venv/bin/mediaforce-web'),
         dev.env['MEDIAFORCE_WEB_PORT'], str(dev.root)], env=dev.env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    failure = Path(dev.env['TEST_INVENTORY_FAILURE'])
    try:
        wait_until(lambda: dev.status()['state'] == 'running')
        failure.touch()
        client = """
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location('fixture_launcher', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
os.chdir(sys.argv[2]); module.STOP_SECONDS = .01
for _ in range(40):
    status = module.stop('backend')
    assert status['state'] == 'stopping', status
print(json.dumps(module.request('backend', 'status')))
"""
        result = subprocess.run(
            [sys.executable, '-c', client, str(dev.root / 'mediaforce/ops/dev_processes.py'), str(dev.state)],
            env=dev.env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['state'] == 'stopping'
        failure.unlink()
        assert dev.run('stop').returncode == 0
    finally:
        failure.unlink(missing_ok=True)
        broker.kill(); broker.wait(timeout=5)


def test_invalid_restart_settings_preserve_both_components(dev: DevFixture) -> None:
    assert dev.run('start', 'all').returncode == 0
    backend = dev.status('backend')['pid']; frontend = dev.status('frontend')['pid']
    dev.env['MEDIAFORCE_FRONTEND_DEV_PORT'] = '87777'
    assert dev.run('restart', 'all').returncode != 0
    assert dev.status('backend')['pid'] == backend
    assert dev.status('frontend')['pid'] == frontend
    assert dev.run('stop', 'all').returncode == 0


def test_one_empty_snapshot_cannot_reap_a_leader_while_a_worker_lives(monkeypatch: pytest.MonkeyPatch) -> None:
    child = Mock(spec=subprocess.Popen, pid=123)
    observations = iter([set(), {42}, set(), set()])
    alive = True
    calls = 0
    def descendants(_group: int) -> set[int]:
        nonlocal alive, calls
        calls += 1
        value = next(observations)
        if calls >= 3:
            alive = False
        return value
    def reap() -> int:
        assert not alive, 'one missed snapshot released the leader while its worker was alive'
        return 0
    child.wait.side_effect = reap
    monkeypatch.setattr(dev_processes.os, 'killpg', Mock())
    monkeypatch.setattr(dev_processes, 'leader_exited', lambda _pid: True)
    monkeypatch.setattr(dev_processes, 'reap_descendants', descendants)
    assert dev_processes.stop_group(child) == 0


def test_unavailable_frontend_control_does_not_skip_backend_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # A real unavailable socket response is checked separately; this controls the
    # command's two-component dispatch without waiting on host processes.
    monkeypatch.setattr(sys, 'argv', ['fixture', 'stop', 'all', '--state-dir', str(tmp_path)])
    stopped: list[str] = []
    def stop(component: str) -> dev_processes.Status:
        stopped.append(component)
        if component == 'frontend':
            return {'state': 'unknown', 'error': 'control unavailable'}
        return {'state': 'stopped'}
    monkeypatch.setattr(dev_processes, 'stop', stop)
    original = Path.cwd()
    try:
        assert dev_processes.main() == 1
    finally:
        os.chdir(original)
    assert stopped == ['frontend', 'backend']
    assert 'backend' in capsys.readouterr().out


def test_empty_control_reply_is_unknown_not_stopped(tmp_path: Path) -> None:
    script = "import socket,sys; from pathlib import Path; s=socket.socket(socket.AF_UNIX); s.bind('backend.sock'); s.listen(); Path(sys.argv[1]).touch(); c,_=s.accept(); c.recv(64); c.close()"
    ready = tmp_path / 'ready'
    server = subprocess.Popen([sys.executable, '-c', script, str(ready)], cwd=tmp_path)
    original = Path.cwd()
    try:
        wait_until(ready.exists)
        os.chdir(tmp_path)
        status = dev_processes.request('backend', 'status')
        assert status['state'] == 'unknown'
        assert status.get('error')
    finally:
        os.chdir(original)
        server.kill(); server.wait(timeout=5)



def test_failed_stop_notification_can_be_retried(dev: DevFixture) -> None:
    script = """
import errno, importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('fixture_launcher', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
original = module.socket.socketpair
class FlakyParent:
    def __init__(self, connection):
        self.connection = connection
        self.failed = False
    def __getattr__(self, name):
        return getattr(self.connection, name)
    def sendall(self, data):
        if not self.failed:
            self.failed = True
            raise OSError(errno.ENOBUFS, 'fixture rejects the first stop notification')
        return self.connection.sendall(data)
def pair():
    parent, child = original()
    return FlakyParent(parent), child
module.socket.socketpair = pair
os.chdir(sys.argv[2])
raise SystemExit(module.serve('backend', [sys.argv[3], '--port', sys.argv[4]],
                            Path(sys.argv[5]), '127.0.0.1', int(sys.argv[4])))
"""
    broker = subprocess.Popen(
        [sys.executable, '-c', script, str(dev.root / 'mediaforce/ops/dev_processes.py'),
         str(dev.state), str(dev.root / '.venv/bin/mediaforce-web'),
         dev.env['MEDIAFORCE_WEB_PORT'], str(dev.root)], env=dev.env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_until(lambda: dev.status()['state'] == 'running')
        assert dev.run('stop').returncode == 0
        dev.assert_empty()
    finally:
        broker.kill(); broker.wait(timeout=5)


@pytest.mark.parametrize('finish', ['listen', 'stop', 'crash'])
def test_slow_start_keeps_custody_after_client_deadline(
    dev: DevFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish: str,
) -> None:
    gate = tmp_path / 'allow-listen'
    dev.env['TEST_START_GATE'] = str(gate)
    script = """
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('fixture_launcher', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.START_SECONDS = .15
os.chdir(sys.argv[2])
raise SystemExit(module.serve('backend', [sys.argv[3], '--port', sys.argv[4]],
                            Path(sys.argv[5]), '127.0.0.1', int(sys.argv[4])))
"""
    broker = subprocess.Popen(
        [sys.executable, '-c', script, str(dev.root / 'mediaforce/ops/dev_processes.py'),
         str(dev.state), str(dev.root / '.venv/bin/mediaforce-web'),
         dev.env['MEDIAFORCE_WEB_PORT'], str(dev.root)], env=dev.env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_until(lambda: dev.status().get('pid', 0) != 0)
        owner = dev.status()
        monkeypatch.setattr(dev_processes, 'START_SECONDS', .15)
        monkeypatch.chdir(dev.state)
        result = dev_processes.start('backend', ['unused'], dev.root, '127.0.0.1',
                                    int(dev.env['MEDIAFORCE_WEB_PORT']))
        assert result['state'] == 'failed'
        assert result.get('error')
        assert dev.status()['state'] == 'starting'
        assert dev.status()['pid'] == owner['pid']
        assert os.getpgid(int(owner['pid'])) == owner['pid']
        assert not dev_processes.port_open('127.0.0.1', int(dev.env['MEDIAFORCE_WEB_PORT']))
        if finish == 'listen':
            gate.touch()
            wait_until(lambda: dev.status()['state'] == 'running')
            assert dev.status()['pid'] == owner['pid']
            assert dev.run('stop').returncode == 0
        elif finish == 'stop':
            assert dev.run('stop').returncode == 0
        else:
            os.kill(int(owner['pid']), signal.SIGKILL)
        dev.assert_empty()
        assert broker.wait(timeout=5) == 0
    finally:
        broker.kill()
        broker.wait(timeout=5)


@pytest.mark.parametrize('guardian_status', [0, 256])
def test_parent_connection_reset_observes_guardian_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guardian_status: int,
) -> None:
    parent = Mock(spec=socket.socket)
    parent.recv.side_effect = ConnectionResetError(errno.ECONNRESET, 'unread Stop')
    child = Mock(spec=socket.socket)
    server_context = MagicMock()
    server = server_context.__enter__.return_value
    waited = Mock(return_value=(123, guardian_status))
    failure = Mock(return_value=7)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dev_processes.signal, 'signal', Mock())
    monkeypatch.setattr(dev_processes.socket, 'socket', Mock(return_value=server_context))
    monkeypatch.setattr(dev_processes.socket, 'socketpair', lambda: (parent, child))
    monkeypatch.setattr(dev_processes.os, 'fork', lambda: 123)
    monkeypatch.setattr(dev_processes.os, 'waitpid', waited)
    monkeypatch.setattr(dev_processes.select, 'select', lambda *_args: ([parent], [], []))
    monkeypatch.setattr(dev_processes, 'serve_failure', failure)
    result = dev_processes.serve('backend', ['fixture'], tmp_path, '127.0.0.1', 1234)
    waited.assert_called_once_with(123, 0)
    assert result == (0 if guardian_status == 0 else 7)
    if guardian_status == 0:
        failure.assert_not_called()
    else:
        assert failure.call_args.args[0] is server
        assert failure.call_args.args[1]['state'] == 'failed'
    parent.close.assert_called()


@pytest.mark.parametrize('host', ['::1', '::'])
def test_ipv6_bind_probe_uses_resolved_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str,
) -> None:
    address = (host, 1234, 0, 0)
    resolved = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', address)
    lookup = Mock(return_value=[resolved])
    probe_context = MagicMock()
    probe = probe_context.__enter__.return_value
    created: list[tuple[int, int, int]] = []
    def make_socket(family: int = socket.AF_INET, kind: int = socket.SOCK_STREAM,
                    protocol: int = 0) -> MagicMock:
        created.append((family, kind, protocol))
        if family != resolved[0]:
            probe.bind.side_effect = OSError('address family mismatch')
        return probe_context
    child = Mock(spec=subprocess.Popen, pid=123)
    spawned = Mock(return_value=child)
    cleanup = Mock(return_value=0)
    connection = Mock(spec=socket.socket)
    monkeypatch.setattr(dev_processes, 'enable_subreaper', lambda: None)
    monkeypatch.setattr(dev_processes, 'port_open', lambda _host, _port: False)
    monkeypatch.setattr(dev_processes.socket, 'getaddrinfo', lookup)
    monkeypatch.setattr(dev_processes.socket, 'socket', make_socket)
    monkeypatch.setattr(dev_processes.signal, 'signal', Mock())
    monkeypatch.setattr(dev_processes.subprocess, 'Popen', spawned)
    monkeypatch.setattr(dev_processes, 'leader_exited', lambda _pid: True)
    monkeypatch.setattr(dev_processes, 'stop_group', cleanup)
    dev_processes.guard_group(connection, ['fixture'], tmp_path,
                              host, 1234, str(tmp_path / 'control.sock'))
    assert created == [(resolved[0], resolved[1], resolved[2])]
    lookup.assert_called_once_with(host, 1234, type=socket.SOCK_STREAM)
    probe.bind.assert_called_once_with(address)
    spawned.assert_called_once()
    cleanup.assert_called_once_with(child)


@pytest.mark.parametrize('outcome', ['ipv4_available', 'none_available', 'ipv4_busy'])
def test_bind_probe_checks_usable_addresses_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    addresses = [
        (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('::1', 1234, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 1234)),
    ]
    connection = Mock(spec=socket.socket)
    def make_socket(family: int, _kind: int, _protocol: int) -> MagicMock:
        context = MagicMock()
        def bind(_address: tuple[object, ...]) -> None:
            if outcome == 'none_available' or (family == socket.AF_INET6 and outcome == 'ipv4_available'):
                raise OSError(errno.EADDRNOTAVAIL, 'resolved address is unavailable')
            if family == socket.AF_INET and outcome == 'ipv4_busy':
                raise OSError(errno.EADDRINUSE, 'listener already bound')
        context.__enter__.return_value.bind.side_effect = bind
        return context
    child = Mock(spec=subprocess.Popen, pid=123)
    spawned = Mock(return_value=child)
    monkeypatch.setattr(dev_processes, 'enable_subreaper', lambda: None)
    monkeypatch.setattr(dev_processes, 'port_open', lambda _host, _port: False)
    monkeypatch.setattr(dev_processes.socket, 'getaddrinfo', Mock(return_value=addresses))
    monkeypatch.setattr(dev_processes.socket, 'socket', make_socket)
    monkeypatch.setattr(dev_processes.signal, 'signal', Mock())
    monkeypatch.setattr(dev_processes.subprocess, 'Popen', spawned)
    monkeypatch.setattr(dev_processes, 'leader_exited', lambda _pid: True)
    monkeypatch.setattr(dev_processes, 'stop_group', Mock(return_value=0))
    if outcome == 'ipv4_available':
        dev_processes.guard_group(connection, ['fixture'], tmp_path, 'localhost', 1234,
                                  str(tmp_path / 'control.sock'))
        spawned.assert_called_once()
    else:
        with pytest.raises(OSError) as raised:
            dev_processes.guard_group(connection, ['fixture'], tmp_path, 'localhost', 1234,
                                      str(tmp_path / 'control.sock'))
        expected = errno.EADDRNOTAVAIL if outcome == 'none_available' else errno.EADDRINUSE
        assert raised.value.errno == expected
        spawned.assert_not_called()
