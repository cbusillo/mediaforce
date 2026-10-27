import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from mediaforce.core.process_control import ManagedProcessController
from mediaforce.encoding.remote_media import RemoteMediaCommands, RemoteMediaConnections, RemoteMediaHostUnavailableError
from mediaforce.hosts.transport import _run_remote_ssh, run_remote_command


class EvidenceConnectionTests(unittest.TestCase):
    def test_silent_multiplexed_disconnect_is_not_bad_media(self) -> None:
        runner = RemoteMediaCommands({'host': 'fixture.invalid'}, control_path=Path('/fixture/private/control'))
        with patch('mediaforce.encoding.remote_media.run_remote_command', return_value=subprocess.CompletedProcess([], 255, '', '')):
            with self.assertRaises(RemoteMediaHostUnavailableError):
                runner.run(['ffmpeg', '-i', 'fixture.mkv'], timeout_seconds=1, process_controller=ManagedProcessController())

    def test_tool_version_is_refreshed_between_claims(self) -> None:
        runner = RemoteMediaCommands({'host': 'fixture.invalid'})
        replies = [subprocess.CompletedProcess([], 0, 'ffmpeg fixture one\n', ''),
                   subprocess.CompletedProcess([], 0, 'ffmpeg fixture two\n', '')]
        with patch('mediaforce.encoding.remote_media.run_remote_command', side_effect=replies) as remote:
            first = runner.ffmpeg_version()
            self.assertEqual(runner.ffmpeg_version(), first)
            runner.begin_claim()
            second = runner.ffmpeg_version()
        self.assertNotEqual(first, second)
        self.assertEqual(remote.call_count, 2)

    def test_reuse_keeps_awake_assertion_and_schedule_guard(self) -> None:
        remote = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        with patch('mediaforce.hosts.transport.managed_schedule_close_deadline', return_value=123), \
             patch('mediaforce.hosts.transport.guard_shell_script_for_schedule_deadline', side_effect=lambda body, _: 'guard\n'+body), \
             patch('mediaforce.hosts.transport.process_result_reached_schedule_deadline', return_value=False):
            run_remote_command({'host':'fixture.invalid'}, ['ffmpeg','-version'], 3,
                               control_path=Path('/fixture/private/control'), ssh_target_for_host=lambda _: 'fixture.invalid',
                               remote_shell_path_export_line=lambda: 'export PATH=/fixture', run_remote_ssh=remote)
        script = remote.call_args.args[-1]
        self.assertIn('caffeinate', script)
        self.assertIn('guard', script)
        self.assertEqual(remote.call_args.kwargs['control_path'], Path('/fixture/private/control'))

    def test_only_existing_owned_socket_skips_wake_probe(self) -> None:
        with TemporaryDirectory() as directory:
            control = Path(directory)/'control'
            wake, execute = Mock(), Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
            kwargs = dict(timeout=3, control_path=control, ensure_remote_awake_for_ssh=wake,
                          ssh_client_options_func=lambda **_: [], subprocess_run=execute)
            _run_remote_ssh({'host':'fixture.invalid'}, 'true', **kwargs)
            self.assertEqual(wake.call_count, 1)
            control.touch()
            _run_remote_ssh({'host':'fixture.invalid'}, 'true', **kwargs)
            self.assertEqual(wake.call_count, 1)
            command = execute.call_args.args[0]
            self.assertIn('ControlMaster=auto', command)
            self.assertIn(f'ControlPath={control}', command)
            self.assertIn('ControlPersist=30', command)

    def test_pool_reuses_only_unchanged_host_config_and_cleans_only_owned_sockets(self) -> None:
        roots = {'tv': '/old'}
        host: dict[str, object] = {'host': 'fixture.invalid', 'source_roots': roots}
        with patch('mediaforce.encoding.remote_media.subprocess.run') as stop:
            with RemoteMediaConnections() as pool:
                first = pool.runner(host)
                same = pool.runner(dict(host))
                self.assertIs(first, same)
                assert first.control_path is not None
                self.assertEqual(first.control_path.parent.stat().st_mode & 0o777, 0o700)
                first.control_path.touch()
                roots['tv'] = '/new'
                changed = pool.runner(host)
                self.assertIsNot(first, changed)
                assert changed.control_path is not None
                changed.control_path.touch()
                sockets = [first.control_path, changed.control_path]
            self.assertEqual({Path(call.args[0][call.args[0].index('-S')+1]) for call in stop.call_args_list}, set(sockets))
            self.assertTrue(all(not socket.parent.exists() for socket in sockets))

    def test_pool_cleans_owned_sockets_when_work_raises(self) -> None:
        with patch('mediaforce.encoding.remote_media.subprocess.run', side_effect=OSError('fixture disconnect')):
            with self.assertRaisesRegex(RuntimeError, 'fixture work'):
                with RemoteMediaConnections() as pool:
                    runner = pool.runner({'host': 'fixture.invalid'})
                    assert runner.control_path is not None
                    socket = runner.control_path
                    socket.touch()
                    Mock(side_effect=RuntimeError('fixture work'))()
        self.assertFalse(socket.parent.exists())
