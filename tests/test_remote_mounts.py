import base64
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mediaforce import remote
from mediaforce.hosts.mount_runtime import ControllerSmbMount, RemoteSmbMount, \
    _remote_mount_script, controller_smb_mounts_from_output, load_controller_smb_mounts, mount_remote_smb_shares, \
    remote_smb_mounts_for_paths, save_controller_smb_mounts
from mediaforce.hosts.types import HostSetupResult
from mediaforce.remote import HostStatus


class RemoteMountRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime_dir = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_dir.cleanup)
        self.runtime_settings_path = Path(runtime_dir.name) / "runtime-settings.json"
        remote._MOUNT_RECOVERY_COOLDOWNS.clear()
        remote._MOUNT_RECOVERY_NO_GUI_SESSIONS.clear()

    def test_learning_keeps_saved_server_name_when_finder_used_an_alias(self) -> None:
        from mediaforce.hosts.mount_runtime import controller_smb_mounts_path, load_controller_smb_mounts, \
            save_controller_smb_mounts

        path = controller_smb_mounts_path(self.runtime_settings_path)
        save_controller_smb_mounts(path, [
            ControllerSmbMount("//cbusillo@nas.shiny/media", Path("/Volumes/media")),
            ControllerSmbMount("//cbusillo@old-nas.shiny/extras", Path("/Volumes/extras")),
            ControllerSmbMount("//cbusillo@nas._smb._tcp.local/staging", Path("/Volumes/staging")),
            ControllerSmbMount("//cbusillo@nas.shiny/archive", Path("/Volumes/archive")),
        ])
        addresses = {
            "nas.shiny": frozenset({"192.168.1.37"}),
            "nas._smb._tcp.local": frozenset({"192.168.1.37"}),
            "old-nas.shiny": frozenset({"192.168.1.20"}),
        }
        config = SimpleNamespace(paths=SimpleNamespace(runtime_settings_path=self.runtime_settings_path))
        mount_output = "\n".join([
            "//cbusillo@nas._smb._tcp.local/media on /Volumes/media (smbfs, nodev, nosuid)",
            "//cbusillo@nas._smb._tcp.local/extras on /Volumes/extras (smbfs, nodev, nosuid)",
            "//cbusillo@nas.shiny/staging on /Volumes/staging (smbfs, nodev, nosuid)",
            "//cbusillo@silent._smb._tcp.local/archive on /Volumes/archive (smbfs, nodev, nosuid)",
        ])

        with patch("mediaforce.remote._controller_smb_mount_output", return_value=mount_output), patch(
                "mediaforce.remote._controller_required_mount_roots",
                return_value={
                    Path("/Volumes/media"), Path("/Volumes/extras"), Path("/Volumes/staging"), Path("/Volumes/archive"),
                },
        ), patch(
            "mediaforce.hosts.controller_mount._resolve_server_addresses",
            side_effect=lambda server: addresses.get(server, frozenset()),
        ):
            remote.learn_controller_smb_mounts(config)

        saved = {mount.mount_point: mount.source for mount in load_controller_smb_mounts(path)}
        self.assertEqual(saved[Path("/Volumes/media")], "//cbusillo@nas.shiny/media")
        # Both names resolve to different servers: the share really moved, so learning takes it.
        self.assertEqual(saved[Path("/Volumes/extras")], "//cbusillo@nas._smb._tcp.local/extras")
        # A saved Bonjour name gives way to an ordinary host name for the same share.
        self.assertEqual(saved[Path("/Volumes/staging")], "//cbusillo@nas.shiny/staging")
        # A Bonjour name that does not resolve proves nothing, so the saved name stays.
        self.assertEqual(saved[Path("/Volumes/archive")], "//cbusillo@nas.shiny/archive")

    def test_controller_smb_mounts_parse_only_smb_volumes(self) -> None:
        mounts = controller_smb_mounts_from_output(
            "\n".join(
                [
                    "/dev/disk3s1 on / (apfs, local)",
                    "//local@NAS.local/media on /Volumes/media (smbfs, nodev, nosuid)",
                    "//local@NAS.local/My\\040Share on /Volumes/My\\040Share (smbfs, nodev)",
                    "//local@NAS.local/Raw Share on /Volumes/Raw Share (smbfs, nodev)",
                ]
            )
        )

        self.assertEqual(
            mounts,
            [
                ControllerSmbMount(source="//local@NAS.local/media", mount_point=Path("/Volumes/media")),
                ControllerSmbMount(source="//local@NAS.local/My Share", mount_point=Path("/Volumes/My Share")),
                ControllerSmbMount(source="//local@NAS.local/Raw Share", mount_point=Path("/Volumes/Raw Share")),
            ],
        )

    def test_remote_mount_plan_deduplicates_paths_and_strips_source_password(self) -> None:
        mounts = remote_smb_mounts_for_paths(
            ["/Volumes/media/tv", "/Volumes/media/transcode"],
            [ControllerSmbMount(source="//local:secret@NAS.local/media", mount_point=Path("/Volumes/media"))],
            remote_user="remote user",
        )

        self.assertEqual(
            mounts,
            [
                RemoteSmbMount(
                    mount_point=Path("/Volumes/media"),
                    share_name="media",
                    url="smb://local@NAS.local/media",
                )
            ],
        )

    def test_remote_mount_plan_rejects_unmapped_or_non_finder_paths(self) -> None:
        controller_mounts = [
            ControllerSmbMount(source="//local@NAS.local/media", mount_point=Path("/Volumes/media"))
        ]

        self.assertIsNone(
            remote_smb_mounts_for_paths(["/srv/media/tv"], controller_mounts, remote_user="remote")
        )
        self.assertIsNone(
            remote_smb_mounts_for_paths(
                ["/Volumes/media/../other/tv"],
                controller_mounts,
                remote_user="remote",
            )
        )

    def test_remote_mount_plan_strips_percent_encoded_password(self) -> None:
        mounts = remote_smb_mounts_for_paths(
            ["/Volumes/media/tv"],
            [ControllerSmbMount(source="//local%3Asecret@NAS.local/media", mount_point=Path("/Volumes/media"))],
            remote_user="remote",
        )

        self.assertEqual(mounts, [RemoteSmbMount(Path("/Volumes/media"), "media", "smb://local@NAS.local/media")])

    def test_controller_mount_mapping_round_trips_without_password(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "controller-smb-mounts.json"

            save_controller_smb_mounts(
                path,
                [ControllerSmbMount(source="//local:secret@NAS.local/media", mount_point=Path("/Volumes/media"))],
            )

            self.assertEqual(
                load_controller_smb_mounts(path),
                [ControllerSmbMount(source="//local@NAS.local/media", mount_point=Path("/Volumes/media"))],
            )
            self.assertNotIn("secret", path.read_text())

    def test_private_override_wins_over_learned_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runtime_settings_path = Path(raw_root) / "runtime-settings.json"
            save_controller_smb_mounts(
                runtime_settings_path.with_name("controller-smb-mounts.json"),
                [ControllerSmbMount(source="//local@old.local/media", mount_point=Path("/Volumes/media"))],
            )
            config = SimpleNamespace(
                raw={"controller_smb_mounts": [{"source": "//local@new.local/media", "mount_point": "/Volumes/media"}]},
                paths=SimpleNamespace(runtime_settings_path=runtime_settings_path),
            )

            mounts = remote._controller_smb_mounts_for_config(config)

        self.assertEqual(mounts[0].source, "//local@new.local/media")

    def test_remote_no_gui_failure_remains_suppressed(self) -> None:
        host = {"host": "remote@worker", "label": "Worker", "media_access": "mounted"}
        status = HostStatus(
            key="remote@worker",
            label="Worker",
            mode="ssh",
            priority=0,
            capabilities=["encode_queue"],
            available=False,
            message="Shared storage disconnected",
            missing_paths=["/Volumes/media/tv"],
            missing_mounts=["/Volumes/media"],
            platform="macos",
        )
        config = SimpleNamespace(
            raw={"controller_smb_mounts": [{"source": "//local@NAS.local/media", "mount_point": "/Volumes/media"}]},
            paths=SimpleNamespace(runtime_settings_path=self.runtime_settings_path),
        )
        run_ssh = Mock(
            return_value=subprocess.CompletedProcess(args=["ssh"], returncode=41, stdout="", stderr="")
        )

        with patch("mediaforce.remote._run_remote_ssh", run_ssh):
            first = remote.recover_remote_host_mounts(config, host, status, force=True)
            second = remote.recover_remote_host_mounts(config, host, status)

        self.assertFalse(first.ok)
        self.assertIn("signed-in macOS desktop session", second.message)
        run_ssh.assert_called_once()

    def test_remote_mount_script_hides_url_and_cleans_launchagent(self) -> None:
        script = _remote_mount_script(
            RemoteSmbMount(
                mount_point=Path("/Volumes/media"),
                share_name="media",
                url='smb://remote@NAS.local/share%22%20&%20do%20shell%20script%20%22unsafe',
            ),
            attempt_seconds=30,
        )

        self.assertNotIn("smb://", script)
        self.assertIn("launchctl bootout", script)
        self.assertIn("launchctl bootstrap", script)
        self.assertIn("/dev/console", script)
        self.assertIn("trap cleanup EXIT HUP INT TERM", script)
        self.assertIn('osacompile -o "$script_path" "$source_path"', script)
        payload_line = next(line for line in script.splitlines() if "base64 -D" in line and "source_path" in line)
        encoded = payload_line.split("printf '%s' ", 1)[1].split(" |", 1)[0].strip("'")
        applescript = base64.b64decode(encoded).decode()
        self.assertIn("set share_url to item 1 of argv", applescript)
        self.assertIn("mount volume share_url", applescript)
        self.assertNotIn("smb://", applescript)
        self.assertNotIn("unsafe", applescript)
        runner_line = next(line for line in script.splitlines() if "base64 -D" in line and "runner_path" in line)
        encoded_runner = runner_line.split("printf '%s' ", 1)[1].split(" |", 1)[0].strip("'")
        runner = base64.b64decode(encoded_runner).decode()
        self.assertIn('launchctl bootout "gui/$uid/$service_label"', runner)

    def test_remote_mount_script_matches_mount_output_with_escaped_spaces(self) -> None:
        script = _remote_mount_script(
            RemoteSmbMount(
                mount_point=Path("/Volumes/My Share"),
                share_name="My Share",
                url="smb://remote@NAS.local/My%20Share",
            ),
            attempt_seconds=30,
        )

        self.assertIn("mount_output_path='/Volumes/My\\040Share'", script)
        self.assertIn(' on $expected (smbfs,', script)
        self.assertIn(' on $mount_output_path (smbfs,', script)

    def test_mount_remote_smb_shares_returns_success_without_exposing_url(self) -> None:
        run_remote_ssh = Mock(
            return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="MEDIAFORCE_MOUNT=mounted\n", stderr="")
        )
        mount = RemoteSmbMount(
            mount_point=Path("/Volumes/media"),
            share_name="media",
            url="smb://remote@NAS.local/media",
        )

        result = mount_remote_smb_shares(
            {"host": "remote@worker", "label": "Worker"},
            [mount],
            run_remote_ssh=run_remote_ssh,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.message, "Connected shared storage on Worker.")
        self.assertNotIn("smb://", run_remote_ssh.call_args.kwargs["input_text"])
        self.assertEqual(result.performed_steps, ["Connected media using the remote Finder Keychain."])

    def test_mount_remote_smb_shares_reports_missing_gui_session(self) -> None:
        result = mount_remote_smb_shares(
            {"host": "remote@worker", "label": "Worker"},
            [RemoteSmbMount(Path("/Volumes/media"), "media", "smb://remote@NAS.local/media")],
            run_remote_ssh=Mock(
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=41, stdout="", stderr="")
            ),
        )

        self.assertFalse(result.ok)
        self.assertIn("signed-in macOS desktop session", result.message)
        self.assertIn("Sign in to Worker as remote", result.detail or "")

    def test_mount_remote_smb_shares_classifies_ssh_timeout_as_transport_failure(self) -> None:
        result = mount_remote_smb_shares(
            {"host": "remote@worker", "label": "Worker"},
            [RemoteSmbMount(Path("/Volumes/media"), "media", "smb://remote@NAS.local/media")],
            run_remote_ssh=Mock(side_effect=subprocess.TimeoutExpired(cmd=["ssh"], timeout=45)),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "ssh_transport")
        self.assertIn("SSH request timed out", result.detail or "")

    def test_timeout_without_a_reason_does_not_guess_one(self) -> None:
        result = _mount_result(42, "MEDIAFORCE_MOUNT=timeout\nMEDIAFORCE_MOUNT_JOB=exited:unknown\n")

        self.assertFalse(result.ok)
        self.assertEqual(result.message, "Worker did not connect the media share within 30 seconds.")
        self.assertIn("Finder gave no reason", result.detail or "")
        self.assertNotIn("Keychain", result.detail or "")
        self.assertEqual(result.failure_kind, "host_configuration")

    def test_timed_out_request_still_waiting_says_to_answer_the_dialog(self) -> None:
        result = _mount_result(42, "MEDIAFORCE_MOUNT=timeout\nMEDIAFORCE_MOUNT_JOB=running\n")

        self.assertIn("within 30 seconds, and the request is still waiting", result.message)
        self.assertIn("Answer or cancel it there", result.detail or "")
        self.assertIn("won't send another request", result.detail or "")
        self.assertNotIn("Keychain", result.detail or "")
        self.assertEqual(result.failure_kind, "host_configuration")

    def test_request_left_by_an_earlier_attempt_is_reported(self) -> None:
        result = _mount_result(45, "MEDIAFORCE_MOUNT=request-waiting\n")

        self.assertEqual(result.message, "Worker is still waiting on an earlier request to connect the media share.")
        self.assertIn("Answer or cancel it there", result.detail or "")
        self.assertEqual(result.failure_kind, "host_configuration")

    def test_share_connected_under_another_name_is_reported_as_stale(self) -> None:
        result = _mount_result(
            46,
            "MEDIAFORCE_MOUNT=mounted-elsewhere\nMEDIAFORCE_MOUNT_AT=/Volumes/My\\040Share-1\n",
            mount=RemoteSmbMount(Path("/Volumes/My Share"), "My Share", "smb://remote@NAS.local/My%20Share"),
        )

        self.assertEqual(result.message, "Worker has a share connected at /Volumes/My Share-1 instead of /Volumes/My Share.")
        self.assertIn("Eject /Volumes/My Share-1 on Worker", result.detail or "")
        self.assertEqual(result.failure_kind, "host_configuration")

    def test_finder_errors_are_grouped_only_by_what_they_say(self) -> None:
        cases = [
            ("Finder got an error: User canceled. (-128)", "was cancelled", "host_configuration"),
            ("Finder got an error: Authentication error (-5023)", "could not sign in", "host_configuration"),
            (
                "Finder got an error: The server smb://remote@NAS.local/media could not be found. (-35)",
                "could not reach the server",
                "host_unavailable",
            ),
            ("Finder got an error: Something new happened. (-1)", "could not connect the media share with Finder", "host_configuration"),
        ]
        for error, message, failure_kind in cases:
            with self.subTest(error=error):
                result = _mount_result(
                    42,
                    f"MEDIAFORCE_MOUNT=timeout\nMEDIAFORCE_MOUNT_JOB=exited:1\nMEDIAFORCE_MOUNT_ERR={error}\n",
                )

                self.assertIn(message, result.message)
                self.assertEqual(result.failure_kind, failure_kind)
                self.assertNotIn("remote@", result.detail or "")
        self.assertIn("save the password to Keychain", _mount_result(
            42, "MEDIAFORCE_MOUNT_JOB=exited:1\nMEDIAFORCE_MOUNT_ERR=Authentication error\n",
        ).detail or "")
        unknown = _mount_result(42, "MEDIAFORCE_MOUNT_JOB=exited:1\nMEDIAFORCE_MOUNT_ERR=Something new happened. (-1)\n")
        self.assertIn("Finder reported: \u201cSomething new happened. (-1)\u201d", unknown.detail or "")
        self.assertNotIn("Keychain", unknown.detail or "")

    def test_launch_helper_ssh_and_unexpected_failures_stay_apart(self) -> None:
        helper = _mount_result(44, "MEDIAFORCE_MOUNT=bootstrap-failed\n")
        ssh = _mount_result(255, "")
        unexpected = _mount_result(7, "")

        self.assertEqual(helper.message, "Worker could not start the Finder storage helper.")
        self.assertEqual(ssh.failure_kind, "ssh_transport")
        self.assertIn("SSH connection failed", ssh.detail or "")
        self.assertIn("ended unexpectedly (exit 7)", unexpected.detail or "")
        self.assertNotIn("launch service", unexpected.detail or "")

    def test_mount_remote_smb_shares_logs_finder_helper_evidence_without_the_account(self) -> None:
        stdout = (
            "MEDIAFORCE_MOUNT=timeout\n"
            "MEDIAFORCE_MOUNT_JOB=running\n"
            "MEDIAFORCE_MOUNT_ERR=Finder got an error: smb://remote@NAS.local/media could not be found. (-35)\n"
        )

        with self.assertLogs("mediaforce.hosts.mount_runtime", level="WARNING") as logs:
            result = mount_remote_smb_shares(
                {"host": "remote@worker", "label": "Worker"},
                [RemoteSmbMount(Path("/Volumes/media"), "media", "smb://remote@NAS.local/media")],
                run_remote_ssh=Mock(
                    return_value=subprocess.CompletedProcess(args=["ssh"], returncode=42, stdout=stdout, stderr="")
                ),
            )

        self.assertEqual(result.failure_kind, "host_configuration")
        self.assertIn("still waiting", result.message)
        self.assertNotIn("MEDIAFORCE_MOUNT", (result.message or "") + (result.detail or ""))
        [line] = logs.output
        self.assertIn("Worker did not connect media: exit 42", line)
        self.assertIn("MEDIAFORCE_MOUNT=timeout", line)
        self.assertIn("MEDIAFORCE_MOUNT_JOB=running", line)
        self.assertIn("smb://NAS.local/media could not be found. (-35)", line)
        self.assertNotIn("remote@", line)

    def test_remote_mount_script_reports_helper_state_after_a_timeout(self) -> None:
        script = _remote_mount_script(
            RemoteSmbMount(Path("/Volumes/media"), "media", "smb://remote@NAS.local/media"),
            attempt_seconds=30,
        )

        timeout_tail = script[script.index("printf 'MEDIAFORCE_MOUNT=timeout\\n'"):]
        self.assertTrue(timeout_tail.startswith("printf 'MEDIAFORCE_MOUNT=timeout\\n'\nreport_helper\nexit 42"))
        self.assertIn('/bin/launchctl print "gui/$uid/$label"', script)
        self.assertIn("MEDIAFORCE_MOUNT_ERR=", script)
        # The runner removes its own folder when AppleScript exits, so it leaves its outcome in a
        # separate folder the caller reads and then removes.
        self.assertIn('ProgramArguments.9 -string "$result_dir"', script)
        self.assertIn('if [ -n "$result_dir" ]; then /bin/rm -rf "$result_dir"; fi', script)
        runner_line = next(line for line in script.splitlines() if 'base64 -D >"$runner_path"' in line)
        runner = base64.b64decode(runner_line.split()[2].strip("'")).decode()
        self.assertLess(runner.index('>"$result_dir/error"'), runner.index('/bin/rm -rf "$runner_dir"'))
        for shell_text in (script, runner):
            self.assertEqual(subprocess.run(["/bin/sh", "-n"], input=shell_text, text=True).returncode, 0)

    def _generated_helper_scripts(self) -> tuple[str, str]:
        script = _remote_mount_script(
            RemoteSmbMount(Path("/Volumes/media"), "media", "smb://remote@NAS.local/media"),
            attempt_seconds=30,
        )
        runner_line = next(line for line in script.splitlines() if 'base64 -D >"$runner_path"' in line)
        return script, base64.b64decode(runner_line.split()[2].strip("'")).decode()

    def test_runner_saves_its_error_redacted_before_cutting_it(self) -> None:
        _script, runner = self._generated_helper_scripts()
        save_error = next(line for line in runner.splitlines() if '>"$result_dir/error"' in line)
        publish_status = next(line for line in runner.splitlines() if '"$result_dir/status"' in line)
        # A caller that sees the status trusts the saved error, so the error must be complete first.
        self.assertLess(runner.index(save_error), runner.index(publish_status))
        self.assertIn('/bin/mv -f "$result_dir/status.tmp" "$result_dir/status"', publish_status)
        error = "x" * 3990 + " smb://remote@NAS.local/media could not be found. (-35)"
        # Cutting first would keep "smb://rem", which no longer looks like an account to redact.
        self.assertTrue(error[:4000].endswith(" smb://rem"))

        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "mount.err").write_text(error)
            subprocess.run(
                ["/bin/sh", "-c", f'stderr_path="$1/mount.err"; result_dir="$1"; {save_error}', "sh", temp],
                check=True,
            )
            saved = Path(temp, "error").read_text()

        self.assertNotIn("remote", saved)
        self.assertTrue(saved.endswith(" smb://NAS"))

    def test_report_prefers_the_saved_result_once_the_runner_has_finished(self) -> None:
        script, _runner = self._generated_helper_scripts()
        report_helper = script[script.index("report_helper() {"):script.index("\n}\n", script.index("report_helper() {")) + 2]
        # launchd can still list the job as running for a moment after the runner saved its result
        # and removed its folder; stand in for that with a job that always reads as running.
        self.assertIn("job_running", report_helper)

        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "status").write_text("1\n")
            Path(temp, "error").write_text("Finder got an error: smb://NAS.local/media could not be found. (-35)\n")
            # The runner already removed its folder, so the live error file is gone.
            output = subprocess.run(
                [
                    "/bin/sh",
                    "-c",
                    f'set -u; job_running() {{ true; }}; stderr_path="$1/gone/mount.err"; result_dir="$1"\n'
                    f"{report_helper}\nreport_helper",
                    "sh",
                    temp,
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout

        self.assertEqual(
            output.splitlines(),
            [
                "MEDIAFORCE_MOUNT_JOB=exited:1",
                "MEDIAFORCE_MOUNT_ERR=Finder got an error: smb://NAS.local/media could not be found. (-35)",
            ],
        )

    def test_remote_mount_recovery_support_requires_clean_remote_macos_status(self) -> None:
        host = {"host": "remote@worker", "label": "Worker", "media_access": "mounted"}
        status = HostStatus(
            key="remote@worker",
            label="Worker",
            mode="ssh",
            priority=0,
            capabilities=["encode_queue"],
            available=False,
            message="Missing required paths",
            missing_paths=["/Volumes/media/tv"],
            missing_mounts=["/Volumes/media"],
            platform="macos",
        )
        config = SimpleNamespace(
            raw={"controller_smb_mounts": [{"source": "//local@NAS.local/media", "mount_point": "/Volumes/media"}]},
            paths=SimpleNamespace(runtime_settings_path=self.runtime_settings_path),
        )

        with patch("mediaforce.remote._controller_smb_mount_output") as mount_output:
            self.assertTrue(remote.remote_mount_recovery_supported(config, host, status))
            status.missing_paths.append("/srv/transcode")
            self.assertFalse(remote.remote_mount_recovery_supported(config, host, status))
            status.missing_paths.pop()
            status.issues.append("ffmpeg is missing")
            self.assertFalse(remote.remote_mount_recovery_supported(config, host, status))
            mount_output.assert_not_called()

    def test_remote_mount_recovery_reports_controller_storage_when_mapping_disappears(self) -> None:
        host = {"host": "remote@worker", "label": "Worker", "media_access": "mounted"}
        status = HostStatus(
            key="remote@worker",
            label="Worker",
            mode="ssh",
            priority=0,
            capabilities=["encode_queue"],
            available=False,
            message="Shared storage disconnected",
            missing_paths=["/Volumes/media/tv"],
            missing_mounts=["/Volumes/media"],
            platform="macos",
        )

        config = SimpleNamespace(
            raw={},
            paths=SimpleNamespace(runtime_settings_path=self.runtime_settings_path),
        )
        result = remote.recover_remote_host_mounts(config, host, status)

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "controller_storage_unavailable")

    def test_controller_recovery_uses_local_mount_transport(self) -> None:
        host = {"host": "local@localhost", "label": "Controller", "media_access": "mounted"}
        status = HostStatus(
            key="local@localhost",
            label="Controller",
            mode="ssh",
            priority=0,
            capabilities=["encode_queue"],
            available=False,
            message="Shared storage disconnected",
            missing_paths=["/Volumes/media/tv"],
            missing_mounts=["/Volumes/media"],
            platform="macos",
        )
        config = SimpleNamespace(
            raw={"controller_smb_mounts": [{"source": "//local@NAS.local/media", "mount_point": "/Volumes/media"}]},
            paths=SimpleNamespace(runtime_settings_path=self.runtime_settings_path),
        )

        with patch(
                "mediaforce.remote._run_local_mount_script",
                return_value=subprocess.CompletedProcess(args=["sh"], returncode=0, stdout="", stderr=""),
        ) as run_local, patch("mediaforce.remote._run_remote_ssh") as run_ssh:
            result = remote.recover_remote_host_mounts(config, host, status, force=True)

        self.assertTrue(result.ok)
        run_local.assert_called_once()
        run_ssh.assert_not_called()

    def test_controller_recovery_cooldown_suppresses_automatic_retry(self) -> None:
        host = {"host": "local@localhost", "label": "Controller", "media_access": "mounted"}
        status = HostStatus(
            key="local@localhost",
            label="Controller",
            mode="ssh",
            priority=0,
            capabilities=["encode_queue"],
            available=False,
            message="Shared storage disconnected",
            missing_paths=["/Volumes/media/tv"],
            missing_mounts=["/Volumes/media"],
            platform="macos",
        )
        config = SimpleNamespace(
            raw={"controller_smb_mounts": [{"source": "//local@NAS.local/media", "mount_point": "/Volumes/media"}]},
            paths=SimpleNamespace(runtime_settings_path=self.runtime_settings_path),
        )
        run_local = Mock(
            return_value=subprocess.CompletedProcess(args=["sh"], returncode=42, stdout="", stderr="")
        )

        with patch("mediaforce.remote._run_local_mount_script", run_local), patch(
                "mediaforce.remote._local_gui_session_token", return_value="501:123"
        ):
            first = remote.recover_remote_host_mounts(config, host, status)
            second = remote.recover_remote_host_mounts(config, host, status)

        self.assertFalse(first.ok)
        self.assertIn("cooling down", second.message)
        run_local.assert_called_once()

    def test_controller_no_gui_failure_is_suppressed_until_session_changes(self) -> None:
        host = {"host": "local@localhost", "label": "Controller", "media_access": "mounted"}
        status = HostStatus(
            key="local@localhost",
            label="Controller",
            mode="ssh",
            priority=0,
            capabilities=["encode_queue"],
            available=False,
            message="Shared storage disconnected",
            missing_paths=["/Volumes/media/tv"],
            missing_mounts=["/Volumes/media"],
            platform="macos",
        )
        config = SimpleNamespace(
            raw={"controller_smb_mounts": [{"source": "//local@NAS.local/media", "mount_point": "/Volumes/media"}]},
            paths=SimpleNamespace(runtime_settings_path=self.runtime_settings_path),
        )
        run_local = Mock(
            side_effect=[
                subprocess.CompletedProcess(args=["sh"], returncode=41, stdout="", stderr=""),
                subprocess.CompletedProcess(args=["sh"], returncode=0, stdout="", stderr=""),
            ]
        )

        with patch("mediaforce.remote._run_local_mount_script", run_local), patch(
                "mediaforce.remote._local_gui_session_token", side_effect=["501:123", "501:123", "501:456"]
        ):
            first = remote.recover_remote_host_mounts(config, host, status, force=True)
            same_session = remote.recover_remote_host_mounts(config, host, status, force=True)
            next_session = remote.recover_remote_host_mounts(config, host, status, force=True)

        self.assertFalse(first.ok)
        self.assertIn("signed-in macOS desktop session", same_session.message)
        self.assertTrue(next_session.ok)
        self.assertEqual(run_local.call_count, 2)


def _mount_result(
        returncode: int,
        stdout: str,
        *,
        mount: RemoteSmbMount | None = None,
) -> HostSetupResult:
    return mount_remote_smb_shares(
        {"host": "remote@worker", "label": "Worker"},
        [mount or RemoteSmbMount(Path("/Volumes/media"), "media", "smb://remote@NAS.local/media")],
        run_remote_ssh=Mock(
            return_value=subprocess.CompletedProcess(args=["ssh"], returncode=returncode, stdout=stdout, stderr="")
        ),
        attempt_seconds=30,
    )


# Stand-ins for the macOS-only commands the generated helper calls, so the real script can run
# here. launchctl keeps the job's state in files: a job "runs" while job-running exists.
_LAUNCHCTL_STUB = """#!/bin/sh
printf '%s\\n' "$*" >>"$STUB_DIR/launchctl.log"
case "$1" in
  print)
    case "$2" in
      */com.mediaforce.mount.*)
        if [ -e "$STUB_DIR/job-running" ]; then printf '\\tstate = running\\n'; exit 0; fi
        exit 113 ;;
    esac
    exit 0 ;;
  bootstrap)
    if [ -e "$STUB_DIR/bootstrap-starts-job" ]; then : >"$STUB_DIR/job-running"; fi
    exit 0 ;;
  bootout)
    rm -f "$STUB_DIR/job-running"
    exit 0 ;;
esac
exit 0
"""
_BASE64_STUB = "#!" + sys.executable + "\nimport base64, sys\nsys.stdout.buffer.write(base64.b64decode(sys.stdin.buffer.read()))\n"


class GeneratedMountScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        (self.root / "home").mkdir()
        stub_bodies = {
            "launchctl": _LAUNCHCTL_STUB,
            "mount": '#!/bin/sh\ncat "$STUB_DIR/mount-output" 2>/dev/null\nexit 0\n',
            "stat": "#!/bin/sh\nid -u\n",
            "base64": _BASE64_STUB,
            "osacompile": "#!/bin/sh\nexit 0\n",
            "plutil": "#!/bin/sh\nexit 0\n",
            "osascript": '#!/bin/sh\nprintf "Finder got an error: User canceled. (-128)\\n" >&2\nexit 1\n',
        }
        for name, body in stub_bodies.items():
            path = self.stubs / name
            path.write_text(body)
            path.chmod(0o755)

    def _stubbed(self, shell_text: str) -> str:
        for system_path in ("/bin/launchctl", "/sbin/mount", "/usr/bin/stat", "/usr/bin/base64",
                            "/usr/bin/osacompile", "/usr/bin/plutil", "/usr/bin/osascript"):
            shell_text = shell_text.replace(system_path, str(self.stubs / Path(system_path).name))
        return shell_text.replace("/tmp/mediaforce-mount", str(self.root / "mediaforce-mount"))

    def _run(self, mount_point: str = "/Volumes/media") -> subprocess.CompletedProcess[str]:
        script = _remote_mount_script(
            RemoteSmbMount(Path(mount_point), Path(mount_point).name, "smb://remote@NAS.local/media"),
            attempt_seconds=1,
        )
        return subprocess.run(
            ["/bin/sh", "-s"],
            input=self._stubbed(script),
            capture_output=True,
            text=True,
            env={"HOME": str(self.root / "home"), "STUB_DIR": str(self.root), "PATH": "/usr/bin:/bin"},
            timeout=30,
        )

    def _launchctl_calls(self, verb: str) -> list[str]:
        log = self.root / "launchctl.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines if line.startswith(verb + " ")]

    def _lock_dirs(self) -> list[Path]:
        return list((self.root / "home" / "Library" / "Caches" / "mediaforce").glob("mount-*.lock"))

    def test_a_timed_out_request_still_waiting_is_left_and_blocks_a_second_one(self) -> None:
        # MF-594-D0: two explicit attempts each left a dialog, because the first attempt's cleanup
        # ended its request (which does not close the dialog) and the second sent a new one.
        (self.root / "bootstrap-starts-job").touch()

        first = self._run()

        self.assertEqual(first.returncode, 42, first.stdout + first.stderr)
        self.assertIn("MEDIAFORCE_MOUNT_JOB=running", first.stdout)
        calls = [line.split(" ", 1)[0] for line in (self.root / "launchctl.log").read_text().splitlines()]
        self.assertNotIn("bootout", calls[calls.index("bootstrap"):], "the waiting request must not be ended")
        self.assertTrue((self.root / "job-running").exists())
        self.assertEqual(len(self._lock_dirs()), 1, "the runner removes the lock when the request ends")
        [result_dir] = list(self.root.glob("mediaforce-mount-result.*"))
        self.assertTrue((result_dir / "caller-gone").exists())

        second = self._run()

        self.assertEqual(second.returncode, 45, second.stdout + second.stderr)
        self.assertIn("MEDIAFORCE_MOUNT=request-waiting", second.stdout)
        self.assertEqual(len(self._launchctl_calls("bootstrap")), 1, "no second request while one waits")

    def test_a_new_attempt_starts_once_the_earlier_request_ends(self) -> None:
        (self.root / "bootstrap-starts-job").touch()
        self._run()
        # Someone answered or cancelled the dialog: the request's runner ends and cleans up.
        (self.root / "job-running").unlink()
        for lock in self._lock_dirs():
            lock.rmdir()

        again = self._run()

        self.assertEqual(again.returncode, 42, again.stdout + again.stderr)
        self.assertEqual(len(self._launchctl_calls("bootstrap")), 2)

    def test_a_timed_out_request_that_already_ended_is_cleaned_up(self) -> None:
        result = self._run()

        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertIn("MEDIAFORCE_MOUNT_JOB=exited:unknown", result.stdout)
        self.assertEqual(len(self._launchctl_calls("bootout")), 2, "once before the request, once after")
        self.assertEqual(self._lock_dirs(), [])
        self.assertEqual(list(self.root.glob("mediaforce-mount*")), [])

    def test_a_share_connected_under_another_name_is_reported_without_a_request(self) -> None:
        (self.root / "mount-output").write_text(
            "//remote@NAS.local/backup on /Volumes/media-backup (smbfs, nodev, nosuid, mounted by remote)\n"
            "/dev/disk3s1 on /Volumes/media-2 (apfs, local, journaled)\n"
            "//remote@NAS.local/My%20media on /Volumes/My\\040media-1 (smbfs, nodev, nosuid, mounted by remote)\n"
        )

        result = self._run("/Volumes/My media")

        self.assertEqual(result.returncode, 46, result.stdout + result.stderr)
        self.assertIn("MEDIAFORCE_MOUNT_AT=/Volumes/My\\040media-1\n", result.stdout)
        self.assertEqual(self._launchctl_calls("bootstrap"), [])

    def test_a_share_with_only_unrelated_mounts_still_tries_to_connect(self) -> None:
        (self.root / "mount-output").write_text(
            "//remote@NAS.local/backup on /Volumes/media-backup (smbfs, nodev, nosuid, mounted by remote)\n"
            "/dev/disk3s1 on /Volumes/media-2 (apfs, local, journaled)\n"
        )

        result = self._run()

        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(len(self._launchctl_calls("bootstrap")), 1)

    def _run_runner(self, *, caller_gone: bool) -> Path:
        script = _remote_mount_script(
            RemoteSmbMount(Path("/Volumes/media"), "media", "smb://remote@NAS.local/media"),
            attempt_seconds=1,
        )
        runner_line = next(line for line in script.splitlines() if 'base64 -D >"$runner_path"' in line)
        runner = base64.b64decode(runner_line.split()[2].strip("'")).decode()
        runner_dir = self.root / "runner"
        result_dir = self.root / "result"
        lock_dir = self.root / "lock"
        for folder in (runner_dir, result_dir, lock_dir):
            folder.mkdir()
        if caller_gone:
            (result_dir / "caller-gone").touch()
        runner_path = runner_dir / "run-mount.sh"
        runner_path.write_text(self._stubbed(runner))
        subprocess.run(
            ["/bin/sh", str(runner_path), "unused.scpt", "smb://NAS.local/media", str(runner_dir / "out"),
             str(runner_dir / "err"), "30", str(lock_dir), "com.mediaforce.mount.test", str(result_dir)],
            env={"STUB_DIR": str(self.root), "PATH": "/usr/bin:/bin"},
            check=False,
            timeout=30,
        )
        self.assertFalse(runner_dir.exists())
        self.assertFalse(lock_dir.exists())
        return result_dir

    def test_runner_keeps_its_result_for_a_caller_that_is_still_reading(self) -> None:
        result_dir = self._run_runner(caller_gone=False)

        self.assertEqual((result_dir / "status").read_text(), "1\n")
        self.assertIn("(-128)", (result_dir / "error").read_text())

    def test_runner_removes_its_result_once_the_caller_has_gone(self) -> None:
        self.assertFalse(self._run_runner(caller_gone=True).exists())


if __name__ == "__main__":
    unittest.main()
