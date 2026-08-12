import base64
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mediaforce import remote
from mediaforce.hosts.mount_runtime import ControllerSmbMount, RemoteSmbMount, _remote_mount_script, \
    controller_smb_mounts_from_output, load_controller_smb_mounts, mount_remote_smb_shares, \
    remote_smb_mounts_for_paths, save_controller_smb_mounts
from mediaforce.remote import HostStatus


class RemoteMountRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        remote._MOUNT_RECOVERY_COOLDOWNS.clear()
        remote._MOUNT_RECOVERY_NO_GUI_SESSIONS.clear()

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
            paths=SimpleNamespace(runtime_settings_path=Path("/does/not/exist/runtime-settings.json")),
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
            token="a" * 32,
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
            token="e" * 32,
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

        with patch("mediaforce.hosts.mount_runtime.uuid.uuid4", return_value=Mock(hex="b" * 32)):
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

    def test_mount_remote_smb_shares_reports_finder_keychain_recovery(self) -> None:
        result = mount_remote_smb_shares(
            {"host": "remote@worker", "label": "Worker"},
            [RemoteSmbMount(Path("/Volumes/media"), "media", "smb://remote@NAS.local/media")],
            run_remote_ssh=Mock(
                return_value=subprocess.CompletedProcess(args=["ssh"], returncode=42, stdout="", stderr="")
            ),
        )

        self.assertFalse(result.ok)
        self.assertIn("could not connect the media share with Finder", result.message)
        self.assertIn("save the password to Keychain", result.detail or "")

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
            paths=SimpleNamespace(runtime_settings_path=Path("/does/not/exist/runtime-settings.json")),
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
            paths=SimpleNamespace(runtime_settings_path=Path("/does/not/exist/runtime-settings.json")),
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
            paths=SimpleNamespace(runtime_settings_path=Path("/does/not/exist/runtime-settings.json")),
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
            paths=SimpleNamespace(runtime_settings_path=Path("/does/not/exist/runtime-settings.json")),
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
            paths=SimpleNamespace(runtime_settings_path=Path("/does/not/exist/runtime-settings.json")),
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


if __name__ == "__main__":
    unittest.main()
