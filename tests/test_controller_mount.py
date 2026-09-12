import fcntl
import json
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mediaforce.hosts.controller_mount import controller_mount_lock, mount_controller_smb_no_ui, \
    probe_controller_mount, probe_controller_volume
from mediaforce.hosts.mount_runtime import ControllerSmbMount


class ControllerMountTests(unittest.TestCase):
    mount = ControllerSmbMount("//local@NAS.local/media", Path("/Volumes/media"))
    required_paths = {Path("/Volumes/media/tv"): "read", Path("/Volumes/media/transcode"): "write"}

    def test_controller_mount_lock_is_nonblocking_and_shared_by_path(self) -> None:
        with patch("mediaforce.hosts.controller_mount.os.open", return_value=17), patch(
                "mediaforce.hosts.controller_mount.os.close",
        ) as close, patch("mediaforce.hosts.controller_mount.fcntl.flock") as flock:
            with controller_mount_lock(Path("/state/runtime-settings.json")) as acquired:
                self.assertTrue(acquired)

        self.assertEqual(flock.call_args_list[0].args[0], 17)
        self.assertEqual(flock.call_args_list[0].args[1], fcntl.LOCK_EX | fcntl.LOCK_NB)
        close.assert_called_once_with(17)

    def test_controller_mount_lock_raises_sanitized_open_failure(self) -> None:
        with patch(
                "mediaforce.hosts.controller_mount.os.open",
                side_effect=PermissionError("private runtime path"),
        ):
            with self.assertRaisesRegex(OSError, "Controller SMB mount lock could not be opened") as raised:
                with controller_mount_lock(Path("/private/runtime-settings.json")):
                    pass

        self.assertNotIn("private runtime path", str(raised.exception))

    def test_controller_mount_lock_contends_and_releases_after_owner_process_is_killed(self) -> None:
        child_script = "\n".join(
            [
                "import fcntl, os, sys, time",
                "path = sys.argv[1]",
                "descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)",
                "fcntl.flock(descriptor, fcntl.LOCK_EX)",
                "print('locked', flush=True)",
                "time.sleep(30)",
            ]
        )
        with tempfile.TemporaryDirectory() as raw_root:
            runtime_settings_path = Path(raw_root) / "runtime-settings.json"
            lock_path = runtime_settings_path.with_name("controller-smb-mount.lock")
            process = subprocess.Popen(
                [sys.executable, "-c", child_script, str(lock_path)],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertIsNotNone(process.stdout)
                assert process.stdout is not None
                readable, _, _ = select.select([process.stdout], [], [], 5)
                self.assertEqual(readable, [process.stdout])
                self.assertEqual(process.stdout.readline().strip(), "locked")
                with controller_mount_lock(runtime_settings_path) as acquired:
                    self.assertFalse(acquired)
            finally:
                process.kill()
                process.wait(timeout=5)

            with controller_mount_lock(runtime_settings_path) as acquired_after_kill:
                self.assertTrue(acquired_after_kill)
            with controller_mount_lock(runtime_settings_path) as acquired_after_release:
                self.assertTrue(acquired_after_release)

    @staticmethod
    def _probe(*, source: str = "//local@nas.LOCAL/media", filesystem: str = "smbfs",
               access: tuple[bool, bool] = (True, True)) -> subprocess.CompletedProcess[str]:
        flags = "\n".join("1" if value else "0" for value in access)
        output = (
            f"{source} on /Volumes/media ({filesystem}, nodev)\n"
            f"MEDIAFORCE_MOUNT_PATH_PRESENT=1\nMEDIAFORCE_ACCESS_BEGIN\n{flags}\n"
        )
        return subprocess.CompletedProcess(["sh"], 0, output, "")

    def test_probe_requires_exact_smb_identity_and_required_access(self) -> None:
        ready_run = Mock(return_value=self._probe())
        ready = probe_controller_mount(self.mount, self.required_paths, run_subprocess=ready_run)
        wrong_share = probe_controller_mount(
            self.mount,
            self.required_paths,
            run_subprocess=Mock(return_value=self._probe(source="//local@NAS.local/Media")),
        )
        unavailable = probe_controller_mount(
            self.mount, self.required_paths, run_subprocess=Mock(return_value=self._probe(access=(True, False))),
        )

        self.assertTrue(ready.mounted)
        self.assertTrue(ready.accessible)
        probe_script = ready_run.call_args.args[0][2]
        self.assertIn('[ -d "$path" ] && [ -x "$path" ]', probe_script)
        self.assertIn('pwd -P', probe_script)
        self.assertNotIn(self.mount.source, ready_run.call_args.args[0])
        self.assertEqual(wrong_share.failure_kind, "mount_identity_mismatch")
        self.assertTrue(wrong_share.occupied)
        self.assertEqual(unavailable.failure_kind, "path_unavailable")

    def test_probe_timeout_is_bounded(self) -> None:
        run = Mock(side_effect=subprocess.TimeoutExpired(["sh"], 4))
        result = probe_controller_mount(self.mount, self.required_paths, run_subprocess=run, timeout_seconds=4)

        self.assertEqual(result.failure_kind, "probe_timeout")
        self.assertEqual(run.call_args.kwargs["timeout"], 4)

    def test_credential_bearing_mapping_is_rejected_before_any_subprocess(self) -> None:
        run = Mock()
        credential_mount = ControllerSmbMount(
            "//local%3Asecret@[fe80::1]/media",
            Path("/Volumes/media"),
        )

        result = mount_controller_smb_no_ui(credential_mount, self.required_paths, run_subprocess=run)

        self.assertEqual(result.failure_kind, "invalid_mapping")
        self.assertTrue(result.action_required)
        run.assert_not_called()

    def test_ipv6_server_colons_are_not_mistaken_for_credentials(self) -> None:
        absent = subprocess.CompletedProcess(
            ["sh"], 0, "MEDIAFORCE_MOUNT_PATH_PRESENT=0\nMEDIAFORCE_ACCESS_BEGIN\n0\n0\n", "",
        )
        unavailable = subprocess.CompletedProcess(
            ["osascript"], 0, json.dumps({"status": -6602, "mountpoints": []}), "",
        )
        run = Mock(side_effect=[absent, unavailable])
        ipv6_mount = ControllerSmbMount("//local@[fe80::1]/media", Path("/Volumes/media"))

        result = mount_controller_smb_no_ui(ipv6_mount, self.required_paths, run_subprocess=run)

        self.assertEqual(result.failure_kind, "mount_failed")
        self.assertEqual(run.call_count, 2)

    def test_probe_rejects_ordinary_directory_or_symlink_at_mount_path(self) -> None:
        occupied = subprocess.CompletedProcess(
            ["sh"], 0, "MEDIAFORCE_MOUNT_PATH_PRESENT=1\nMEDIAFORCE_ACCESS_BEGIN\n0\n0\n", "",
        )

        result = probe_controller_mount(self.mount, self.required_paths, run_subprocess=Mock(return_value=occupied))

        self.assertTrue(result.occupied)
        self.assertEqual(result.failure_kind, "mount_path_occupied")

    def test_generic_volume_probe_accepts_non_smb_controller_storage(self) -> None:
        local_volume = self._probe(source="/dev/disk4s1", filesystem="apfs")

        result = probe_controller_volume(
            Path("/Volumes/media"), self.required_paths, run_subprocess=Mock(return_value=local_volume),
        )

        self.assertTrue(result.mounted)
        self.assertTrue(result.accessible)
        self.assertEqual(result.filesystem, "apfs")

    def test_occupied_non_smb_path_prevents_mount_attempt(self) -> None:
        run = Mock(return_value=self._probe(source="/dev/disk4s1", filesystem="apfs"))
        result = mount_controller_smb_no_ui(self.mount, self.required_paths, run_subprocess=run)

        self.assertFalse(result.ok)
        self.assertTrue(result.action_required)
        self.assertEqual(result.failure_kind, "mount_identity_mismatch")
        self.assertEqual(run.call_count, 1)

    def test_no_ui_mount_uses_fixed_jxa_then_requires_fresh_probe(self) -> None:
        absent = subprocess.CompletedProcess(
            ["sh"], 0, "MEDIAFORCE_MOUNT_PATH_PRESENT=0\nMEDIAFORCE_ACCESS_BEGIN\n0\n0\n", "",
        )
        mounted = subprocess.CompletedProcess(
            ["osascript"], 0, json.dumps({"status": 0, "mountpoints": ["/Volumes/media"]}), "",
        )
        run = Mock(side_effect=[absent, mounted, self._probe()])

        result = mount_controller_smb_no_ui(self.mount, self.required_paths, run_subprocess=run)

        self.assertTrue(result.ok)
        command = run.call_args_list[1].args[0]
        self.assertEqual(command[:3], ["/usr/bin/osascript", "-l", "JavaScript"])
        self.assertIn('$("NoUI")', command[4])
        self.assertIn('$("UIOption")', command[4])
        self.assertIn("NetFSMountURLSync", command[4])
        self.assertNotIn("password", command[4].lower())
        self.assertEqual(run.call_args_list[1].kwargs["timeout"], 30)

    def test_mount_timeout_requires_action_and_does_not_probe_again(self) -> None:
        absent = subprocess.CompletedProcess(
            ["sh"], 0, "MEDIAFORCE_MOUNT_PATH_PRESENT=0\nMEDIAFORCE_ACCESS_BEGIN\n0\n0\n", "",
        )
        run = Mock(side_effect=[absent, subprocess.TimeoutExpired(["osascript"], 7)])

        result = mount_controller_smb_no_ui(
            self.mount, self.required_paths, run_subprocess=run, mount_timeout_seconds=7,
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.action_required)
        self.assertEqual(result.failure_kind, "mount_timeout")
        self.assertEqual(run.call_count, 2)

    def test_unexpected_returned_mount_path_fails_closed_without_unmount(self) -> None:
        absent = subprocess.CompletedProcess(
            ["sh"], 0, "MEDIAFORCE_MOUNT_PATH_PRESENT=0\nMEDIAFORCE_ACCESS_BEGIN\n0\n0\n", "",
        )
        mounted_elsewhere = subprocess.CompletedProcess(
            ["osascript"], 0, json.dumps({"status": 0, "mountpoints": ["/Volumes/media-1"]}), "",
        )
        run = Mock(side_effect=[absent, mounted_elsewhere])

        result = mount_controller_smb_no_ui(self.mount, self.required_paths, run_subprocess=run)

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "unexpected_mount_path")
        self.assertIn("/Volumes/media-1", result.detail or "")
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("unmount", " ".join(run.call_args_list[1].args[0]).lower())

    def test_probe_matches_escaped_mount_path_and_percent_encoded_share(self) -> None:
        mount = ControllerSmbMount("//local@NAS.local/My%20Share", Path("/Volumes/My Share"))
        output = subprocess.CompletedProcess(
            ["sh"],
            0,
            "//local@nas.LOCAL/My\\040Share on /Volumes/My\\040Share (smbfs, nodev)\n"
            "MEDIAFORCE_MOUNT_PATH_PRESENT=1\nMEDIAFORCE_ACCESS_BEGIN\n1\n",
            "",
        )

        result = probe_controller_mount(
            mount,
            {Path("/Volumes/My Share/tv"): "read"},
            run_subprocess=Mock(return_value=output),
        )

        self.assertTrue(result.mounted)
        self.assertTrue(result.accessible)

    def test_mount_helper_output_requires_object_integer_status_and_string_paths(self) -> None:
        absent = subprocess.CompletedProcess(
            ["sh"], 0, "MEDIAFORCE_MOUNT_PATH_PRESENT=0\nMEDIAFORCE_ACCESS_BEGIN\n0\n0\n", "",
        )
        malformed_payloads = (
            [],
            {"status": True, "mountpoints": ["/Volumes/media"]},
            {"status": 0.0, "mountpoints": ["/Volumes/media"]},
            {"status": 0, "mountpoints": None},
            {"status": 0, "mountpoints": [17]},
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                helper = subprocess.CompletedProcess(["osascript"], 0, json.dumps(payload), "")
                run = Mock(side_effect=[absent, helper])

                result = mount_controller_smb_no_ui(self.mount, self.required_paths, run_subprocess=run)

                self.assertEqual(result.failure_kind, "mount_result_unknown")
                self.assertTrue(result.action_required)


if __name__ == "__main__":
    unittest.main()
