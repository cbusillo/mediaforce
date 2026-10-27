import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from mediaforce.core.process_control import ManagedProcessController, ProcessDeadlineEnforcementError
from mediaforce.encoding.remote_media import RemoteMediaCommands, RemoteMediaConnections, RemoteMediaHostUnavailableError
from mediaforce.hosts.transport import _run_remote_ssh, run_remote_command


class EvidenceConnectionTests(unittest.TestCase):
    def test_silent_multiplexed_disconnect_is_not_bad_media(self) -> None:
        runner = RemoteMediaCommands({'host': 'fixture.invalid'}, connection=Mock(control_path=Path('/fixture/private/control'), alive=Mock(return_value=False)))
        with patch('mediaforce.encoding.remote_media.run_remote_command', return_value=subprocess.CompletedProcess([], 255, '', '')):
            with self.assertRaises(RemoteMediaHostUnavailableError):
                runner.run(['ffmpeg', '-i', 'fixture.mkv'], timeout_seconds=1, process_controller=ManagedProcessController())

    def test_remote_exit_255_with_a_live_master_keeps_file_failure_semantics(self) -> None:
        connection = Mock(control_path=Path('/fixture/private/control'), alive=Mock(return_value=True))
        runner = RemoteMediaCommands({'host': 'fixture.invalid'}, connection=connection)
        failed = subprocess.CompletedProcess([], 255, '', 'fixture file error')
        with patch('mediaforce.encoding.remote_media.run_remote_command', return_value=failed):
            result = runner.run(['ffmpeg', '-i', 'fixture.mkv'], timeout_seconds=1, process_controller=ManagedProcessController())
        self.assertIs(result, failed)
        connection.alive.assert_called_once()

    def test_timeout_preserves_media_timeout_only_with_a_live_master(self) -> None:
        for alive in (True, False):
            with self.subTest(master_alive=alive):
                connection = Mock(control_path=Path('/fixture/private/control'), alive=Mock(return_value=alive))
                runner = RemoteMediaCommands({'host': 'fixture.invalid'}, connection=connection)
                error = subprocess.TimeoutExpired(['fixture'], 1)
                expected = subprocess.TimeoutExpired if alive else RemoteMediaHostUnavailableError
                with patch('mediaforce.encoding.remote_media.run_remote_command', side_effect=error):
                    with self.assertRaises(expected):
                        runner.run(['ffmpeg'], timeout_seconds=1, process_controller=ManagedProcessController())

    def test_unproven_master_cleanup_blocks_reuse_and_retains_its_socket_directory(self) -> None:
        pool = RemoteMediaConnections()
        runner = pool.runner({'host': 'fixture.invalid'})
        connection = runner.connection
        assert connection is not None
        original = connection.controller
        connection.controller = Mock(cleanup_unproven=True)
        try:
            with patch('mediaforce.encoding.remote_media._ensure_remote_awake_for_ssh') as wake:
                with self.assertRaises(ProcessDeadlineEnforcementError):
                    connection.ensure_started(None)
                wake.assert_not_called()
            with self.assertRaises(ProcessDeadlineEnforcementError):
                pool.__exit__(None, None, None)
            self.assertTrue(connection.control_path.parent.exists())
        finally:
            # The unproven controller is a fixture; no command was started.
            connection.controller = original
            connection.close()

    def test_version_check_does_not_turn_containment_failure_into_host_wait(self) -> None:
        error = ProcessDeadlineEnforcementError('fixture unknown cleanup')
        runner = RemoteMediaCommands({'host': 'fixture.invalid'}, connection=Mock(ensure_started=Mock(side_effect=error)))
        with patch('mediaforce.encoding.remote_media.run_remote_command') as remote:
            with self.assertRaises(ProcessDeadlineEnforcementError) as caught:
                runner.ffmpeg_version(ManagedProcessController())
        self.assertIs(caught.exception, error)
        remote.assert_not_called()

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

    def test_socket_does_not_replace_the_wake_probe(self) -> None:
        with TemporaryDirectory() as directory:
            control = Path(directory)/'control'
            wake, execute = Mock(), Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
            kwargs = dict(timeout=3, control_path=control, ensure_remote_awake_for_ssh=wake,
                          ssh_client_options_func=lambda **_: [], subprocess_run=execute)
            _run_remote_ssh({'host':'fixture.invalid'}, 'true', **kwargs)
            self.assertEqual(wake.call_count, 1)
            control.touch()
            _run_remote_ssh({'host':'fixture.invalid'}, 'true', **kwargs)
            self.assertEqual(wake.call_count, 2)
            command = execute.call_args.args[0]
            self.assertIn(f'ControlPath={control}', command)

    def test_pool_reuses_only_unchanged_host_config_and_cleans_owned_directories(self) -> None:
        roots = {'tv': '/old'}
        host: dict[str, object] = {'host': 'fixture.invalid', 'source_roots': roots}
        with RemoteMediaConnections() as pool:
            first = pool.runner(host)
            self.assertIs(first, pool.runner(dict(host)))
            assert first.connection is not None
            socket = first.connection.control_path
            self.assertEqual(socket.parent.stat().st_mode & 0o777, 0o700)
            roots['tv'] = '/new'
            changed = pool.runner(host)
            self.assertIsNot(first, changed)
            self.assertFalse(socket.parent.exists())
            roots["tv"] = "/old"
            self.assertIsNot(pool.runner(host), first)
            assert changed.connection is not None
            sockets = [socket, changed.connection.control_path]
        self.assertTrue(all(not path.parent.exists() for path in sockets))

    def test_pool_cleans_owned_sockets_when_work_raises(self) -> None:
        with self.assertRaisesRegex(RuntimeError, 'fixture work'):
            with RemoteMediaConnections() as pool:
                runner = pool.runner({'host': 'fixture.invalid'})
                assert runner.connection is not None
                socket = runner.connection.control_path
                Mock(side_effect=RuntimeError('fixture work'))()
        self.assertFalse(socket.parent.exists())

    def test_master_uses_its_own_managed_controller_and_stops_on_exit(self) -> None:
        observed = []

        def master_command(command: list[str], *, process_controller: ManagedProcessController) -> None:
            observed.append(process_controller)
            path = Path(next(arg.split('=',1)[1] for arg in command if arg.startswith('ControlPath=')))
            path.touch()
            while not process_controller.cancelled:
                time.sleep(.01)
            process_controller.throw_if_cancelled()

        with patch('mediaforce.encoding.remote_media.run_command', side_effect=master_command), \
             patch('mediaforce.encoding.remote_media._ensure_remote_awake_for_ssh'):
            with RemoteMediaConnections() as pool:
                runner = pool.runner({'host': 'fixture.invalid'})
                assert runner.connection is not None
                command_controller = ManagedProcessController()
                runner.connection.ensure_started(command_controller)
                master_controller = runner.connection.controller
                thread = runner.connection.thread
                self.assertIsNot(master_controller, command_controller)
                self.assertEqual(observed, [master_controller])
            self.assertTrue(master_controller.cancelled)
            assert thread is not None
            self.assertFalse(thread.is_alive())
