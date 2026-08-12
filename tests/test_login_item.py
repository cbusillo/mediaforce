import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mediaforce.ops.login_item import LOGIN_ITEM_LABEL, LoginItemError, LoginItemPaths, disable_login_item, \
    enable_login_item, install_login_item, login_item_status, render_login_item_plist, rotate_login_item_logs, \
    run_login_item_command, uninstall_login_item


class LoginItemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.project_root = self.root / "repo"
        self.home = self.root / "home"
        self.program = self.project_root / ".venv" / "bin" / "mediaforce-web"
        self.program.parent.mkdir(parents=True)
        self.program.write_text("#!/bin/sh\n")
        self.program.chmod(0o755)
        self.paths = LoginItemPaths(project_root=self.project_root, home=self.home)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_rendered_plist_runs_direct_entrypoint_without_mount_dependency(self) -> None:
        payload = plistlib.loads(render_login_item_plist(self.paths))

        self.assertEqual(payload["Label"], LOGIN_ITEM_LABEL)
        self.assertEqual(payload["ProgramArguments"], [str(self.program), "--no-reload"])
        self.assertEqual(payload["WorkingDirectory"], str(self.project_root))
        self.assertTrue(payload["KeepAlive"])
        self.assertEqual(payload["ThrottleInterval"], 30)
        self.assertNotIn("uv", payload["ProgramArguments"])
        self.assertTrue(payload["EnvironmentVariables"]["PATH"].startswith(str(self.home / ".local/bin")))
        self.assertNotIn("MEDIAFORCE_ENV_FILE", payload["EnvironmentVariables"])
        self.assertNotIn("WatchPaths", payload)
        self.assertNotIn("QueueDirectories", payload)
        self.assertNotIn("/Volumes", str(payload))

    def test_rendered_plist_uses_durable_logs(self) -> None:
        payload = plistlib.loads(render_login_item_plist(self.paths))

        self.assertEqual(payload["StandardOutPath"], str(self.home / "Library/Logs/mediaforce/web.log"))
        self.assertEqual(payload["StandardErrorPath"], str(self.home / "Library/Logs/mediaforce/web.err.log"))
        self.assertEqual(Path(payload["StandardOutPath"]).relative_to(self.home), Path("Library/Logs/mediaforce/web.log"))

    def test_install_is_idempotent_and_creates_log_directory(self) -> None:
        with patch("mediaforce.ops.login_item._require_supported_platform"):
            first = install_login_item(self.paths)
            first_mtime = self.paths.plist.stat().st_mtime_ns
            second = install_login_item(self.paths)

        self.assertEqual(first, "installed")
        self.assertEqual(second, "unchanged")
        self.assertEqual(self.paths.plist.stat().st_mtime_ns, first_mtime)
        self.assertTrue(self.paths.log_dir.is_dir())

    def test_enable_orders_launchctl_operations(self) -> None:
        calls: list[list[str]] = []
        rotation_call_count: int | None = None
        self.paths.log_dir.mkdir(parents=True)
        self.paths.stdout_log.write_bytes(b"a" * 32)

        def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
            nonlocal rotation_call_count
            calls.append(command)
            if rotation_call_count is None and not self.paths.stdout_log.exists():
                rotation_call_count = len(calls)
            if command[1] == "print":
                return subprocess.CompletedProcess(command, 113, "", "not loaded")
            return subprocess.CompletedProcess(command, 0, "", "")

        def _rotate(paths: LoginItemPaths) -> None:
            nonlocal rotation_call_count
            rotation_call_count = len(calls)
            rotate_login_item_logs(paths, maximum_bytes=16)

        with patch("mediaforce.ops.login_item._require_supported_platform"), patch(
                "mediaforce.ops.login_item.rotate_login_item_logs", side_effect=_rotate
        ):
            enable_login_item(self.paths, runner=_run)

        self.assertEqual([call[1] for call in calls], ["bootout", "print", "enable", "bootstrap"])
        self.assertEqual(calls[3][-1], str(self.paths.plist))
        self.assertEqual(rotation_call_count, 2)
        self.assertTrue(self.paths.stdout_log.with_name("web.log.1").exists())

    def test_enable_retries_bootstrap_after_transient_launchctl_error(self) -> None:
        calls: list[list[str]] = []
        bootstrap_attempts = 0

        def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
            nonlocal bootstrap_attempts
            calls.append(command)
            if command[1] == "print":
                return subprocess.CompletedProcess(command, 113, "", "not loaded")
            if command[1] == "bootstrap":
                bootstrap_attempts += 1
                if bootstrap_attempts == 1:
                    return subprocess.CompletedProcess(command, 5, "", "Input/output error")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("mediaforce.ops.login_item._require_supported_platform"), patch(
                "mediaforce.ops.login_item.time.sleep"
        ):
            enable_login_item(self.paths, runner=_run)

        self.assertEqual(bootstrap_attempts, 2)

    def test_disable_tolerates_not_loaded_result(self) -> None:
        runner = Mock(
            side_effect=[
                subprocess.CompletedProcess(["launchctl"], 113, "", "not loaded"),
                subprocess.CompletedProcess(["launchctl"], 113, "", "not loaded"),
                subprocess.CompletedProcess(["launchctl"], 0, "", ""),
            ]
        )

        with patch("mediaforce.ops.login_item._require_supported_platform"):
            disable_login_item(self.paths, runner=runner)

        self.assertEqual(runner.call_count, 3)

    def test_disable_tolerates_bootout_in_progress(self) -> None:
        runner = Mock(
            side_effect=[
                subprocess.CompletedProcess(["launchctl"], 36, "", "Operation now in progress"),
                subprocess.CompletedProcess(["launchctl"], 113, "", "not loaded"),
                subprocess.CompletedProcess(["launchctl"], 0, "", ""),
            ]
        )

        with patch("mediaforce.ops.login_item._require_supported_platform"):
            disable_login_item(self.paths, runner=runner)

        self.assertEqual(runner.call_count, 3)

    def test_status_reports_current_running_item(self) -> None:
        with patch("mediaforce.ops.login_item._require_supported_platform"):
            install_login_item(self.paths)
        runner = Mock(
            return_value=subprocess.CompletedProcess(["launchctl"], 0, "pid = 4321\n", "")
        )

        with patch("mediaforce.ops.login_item._require_supported_platform"):
            status, lines = login_item_status(self.paths, runner=runner)

        self.assertEqual(status, 0)
        self.assertIn("pid 4321", lines[0])

    def test_status_detects_stale_plist(self) -> None:
        self.paths.plist.parent.mkdir(parents=True)
        self.paths.plist.write_text("stale")

        with patch("mediaforce.ops.login_item._require_supported_platform"):
            status, lines = login_item_status(self.paths, runner=Mock())

        self.assertEqual(status, 3)
        self.assertIn("stale", lines[0])

    def test_status_uses_non_argparse_code_when_not_installed(self) -> None:
        with patch("mediaforce.ops.login_item._require_supported_platform"):
            status, _ = login_item_status(self.paths, runner=Mock())

        self.assertEqual(status, 4)

    def test_log_rotation_bounds_each_log(self) -> None:
        self.paths.log_dir.mkdir(parents=True)
        self.paths.stdout_log.write_bytes(b"a" * 32)
        self.paths.stdout_log.with_name("web.log.1").write_text("old")

        rotate_login_item_logs(self.paths, maximum_bytes=16)

        self.assertFalse(self.paths.stdout_log.exists())
        self.assertEqual(self.paths.stdout_log.with_name("web.log.1").stat().st_size, 32)

    def test_uninstall_disables_and_removes_plist(self) -> None:
        with patch("mediaforce.ops.login_item._require_supported_platform"):
            install_login_item(self.paths)
        runner = Mock(
            side_effect=[
                subprocess.CompletedProcess(["launchctl"], 113, "", "not loaded"),
                subprocess.CompletedProcess(["launchctl"], 113, "", "not loaded"),
                subprocess.CompletedProcess(["launchctl"], 0, "", ""),
            ]
        )

        uninstall_login_item(self.paths, runner=runner)

        self.assertFalse(self.paths.plist.exists())
        self.assertEqual(runner.call_count, 3)

    def test_enable_fails_when_existing_job_never_unloads(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess(["launchctl"], 0, "", ""))

        with patch("mediaforce.ops.login_item._require_supported_platform"), patch(
                "mediaforce.ops.login_item.time.sleep"
        ), self.assertRaises(LoginItemError):
            enable_login_item(self.paths, runner=runner)

    def test_unsupported_action_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            run_login_item_command("unsupported", project_root=self.project_root, home=self.home)

    def test_dev_script_uses_the_same_launch_item_label(self) -> None:
        script = (Path(__file__).parents[1] / "scripts" / "mediaforce-dev.sh").read_text()

        self.assertIn(f'BACKEND_LAUNCH_AGENT="{LOGIN_ITEM_LABEL}"', script)

    def test_cli_service_command_does_not_load_mediaforce_config(self) -> None:
        from mediaforce import cli

        with patch("mediaforce.ops.login_item.run_login_item_command", return_value=0) as service, patch(
                "mediaforce.cli.load_config"
        ) as load_config:
            result = cli.main(["service", "status"])

        self.assertEqual(result, 0)
        service.assert_called_once_with(
            "status",
            project_root=Path(cli.__file__).resolve().parents[1],
            follow=False,
            stderr=False,
            lines=80,
        )
        load_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
