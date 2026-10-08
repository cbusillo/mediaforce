import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from unittest.mock import Mock
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
with socket.socket() as listener:
    listener.bind(('127.0.0.1', port)); listener.listen()
    while not select.select([fd], [], [], .02)[0]:
        if pathlib.Path(os.environ['TEST_CRASH']).exists():
            os._exit(3)
        if select.select([listener], [], [], 0)[0]:
            client, _ = listener.accept()
            client.close()
worker.wait(timeout=5)
'''

# The fixture supplies the process inventory from native PGIDs of only the
# processes it launched. No installed ps/npm/launchctl or host process state.
PS = '''
import os, pathlib, sys
if pathlib.Path(os.environ['TEST_INVENTORY_FAILURE']).exists():
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

    def assert_empty(self) -> None:
        wait_until(lambda: self.status()['state'] == 'stopped')
        with socket.socket() as listener:
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
    dev.foreign = subprocess.Popen([str(dev.root / '.venv/bin/mediaforce-web'), '--port', str(foreign_port)],
                                   env=foreign_env, start_new_session=True)
    wait_until(lambda: dev_processes.port_open('127.0.0.1', foreign_port))
    assert dev.run('start').returncode == 0
    assert dev.run('stop').returncode == 0
    assert dev.foreign.poll() is None
    assert dev_processes.port_open('127.0.0.1', foreign_port)


def test_occupied_port_preserves_foreign_listener(dev: DevFixture) -> None:
    port = int(dev.env['MEDIAFORCE_WEB_PORT'])
    dev.foreign = subprocess.Popen([str(dev.root / '.venv/bin/mediaforce-web'), '--port', str(port)],
                                   env=dev.env, start_new_session=True)
    wait_until(lambda: dev_processes.port_open('127.0.0.1', port))
    assert dev.run('start').returncode != 0
    assert dev.foreign.poll() is None
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
    remaining = iter([{42}, set()])
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
    monkeypatch.setattr(dev_processes, 'group_members', lambda _pgid: set())
    assert dev_processes.stop_group(child) == 0
    assert events == ['signal group', 'descendants remain', 'descendants empty', 'reap leader']


def test_concurrent_starts_share_one_owner(dev: DevFixture) -> None:
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _index: dev.run('start'), range(3)))
    assert all(result.returncode == 0 for result in results)
    owners = {json.loads(result.stdout.split(': ', 1)[1])['pid'] for result in results}
    assert owners == {dev.status()['pid']}
    assert dev.run('stop').returncode == 0


def test_observation_failure_retains_owner_until_cleanup_can_finish(dev: DevFixture) -> None:
    dev.env['TEST_STUBBORN'] = '1'
    assert dev.run('start').returncode == 0
    owner = dev.status()
    failure = Path(dev.env['TEST_INVENTORY_FAILURE'])
    failure.touch()
    stopping = subprocess.Popen(['bash', str(dev.root / 'scripts/mediaforce-dev.sh'), 'stop', 'backend'],
                                env=dev.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_until(lambda: 'cleanup pending' in (dev.state / 'backend.log').read_text())
        status = dev.status()
        assert status['state'] == 'stopping'
        assert status['guardian'] == owner['guardian']
        assert stopping.poll() is None
    finally:
        failure.unlink(missing_ok=True)
    output, errors = stopping.communicate(timeout=15)
    assert stopping.returncode == 0, (output, errors)
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
