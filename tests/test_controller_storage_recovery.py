from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.hosts.controller_mount import ControllerMountProbe, ControllerMountResult
from mediaforce.hosts.mount_runtime import (
    ControllerSmbMount,
    controller_smb_mounts_path,
    save_controller_smb_mounts,
)
from mediaforce.web.runtime import controller_storage_recovery as recovery


NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


class ControllerStorageRecoveryTest(TestCase):
    def setUp(self) -> None:
        self.platform_patch = patch.object(recovery.sys, "platform", "darwin")
        self.platform_patch.start()
        self.addCleanup(self.platform_patch.stop)
        self.tempdir = TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.config = MediaforceConfig(
            raw={
                "media": {
                    "source_roots": {"tv": "/Volumes/media/tv"},
                    "staging_root": "/Volumes/staging/encodes",
                    "archive_root": "/Volumes/archive/library",
                },
                "video": {},
                "audio": {},
                "subtitle": {},
                "planning": {},
                "validation": {},
                "overrides": [],
                "remote_hosts": [
                    {
                        "key": "mounted",
                        "media_access": "mounted",
                        "source_roots": {"tv": "/Volumes/remote-media/tv"},
                        "staging_root": "/Volumes/remote-stage/encodes",
                    },
                    {
                        "key": "stream",
                        "media_access": "stream",
                        "source_roots": {"tv": "/remote/not-controller-visible"},
                        "staging_root": "/remote/staging",
                    },
                ],
            },
            paths=ConfigPaths(
                project_root=root,
                config_path=root / "config.toml",
                db_path=root / "mediaforce.db",
                run_manifest_dir=root / "manifests",
                web_state_dir=root / "web",
                review_dir=root / "review",
                runtime_settings_path=root / "runtime-settings.json",
            ),
        )
        self.mounts = [
            ControllerSmbMount("//user@nas/media", Path("/Volumes/media")),
            ControllerSmbMount("//user@nas/staging", Path("/Volumes/staging")),
            ControllerSmbMount("//user@worker/media", Path("/Volumes/remote-media")),
            ControllerSmbMount("//user@worker/stage", Path("/Volumes/remote-stage")),
        ]
        save_controller_smb_mounts(
            controller_smb_mounts_path(self.config.paths.runtime_settings_path),
            self.mounts,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_process_probes_controller_and_mounted_remote_paths_only(self) -> None:
        def ready_probe(mount: ControllerSmbMount, required_paths: object) -> ControllerMountProbe:
            return ControllerMountProbe(True, True, mount.mount_point, mount.source, "smbfs", True)

        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", side_effect=ready_probe,
        ) as probe, patch.object(recovery, "mount_controller_smb_no_ui") as mount:
            recovery.process_controller_storage_recovery_once(self.config)

        observed_paths = {
            path
            for call in probe.call_args_list
            for path in call.args[1]
        }
        self.assertEqual(observed_paths, {
            Path("/Volumes/media/tv"),
            Path("/Volumes/staging/encodes"),
            Path("/Volumes/remote-media/tv"),
            Path("/Volumes/remote-stage/encodes"),
        })
        mount.assert_not_called()
        snapshot = recovery.controller_storage_recovery_snapshot(self.config)
        self.assertEqual({item["status"] for item in snapshot["mounts"]}, {"ready"})

    def test_retry_backoff_does_not_repeat_or_slide_before_due(self) -> None:
        absent = ControllerMountProbe(
            False, False, self.mounts[0].mount_point, failure_kind="mount_absent"
        )
        failed = ControllerMountResult(False, False, "mount_failed", "secret URL", absent)
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=absent,
        ), patch.object(
            recovery, "mount_controller_smb_no_ui", return_value=failed,
        ) as mount:
            recovery.process_controller_storage_recovery_once(self.config)
            first = recovery.controller_storage_recovery_snapshot(self.config)
            recovery.process_controller_storage_recovery_once(self.config)
            second = recovery.controller_storage_recovery_snapshot(self.config)

        media_calls = [call for call in mount.call_args_list if call.args[0] == self.mounts[0]]
        self.assertEqual(len(media_calls), 1)
        first_media = next(item for item in first["mounts"] if item["mount_point"] == "/Volumes/media")
        second_media = next(item for item in second["mounts"] if item["mount_point"] == "/Volumes/media")
        self.assertEqual(first_media["next_retry_at"], "2026-09-12T12:00:30Z")
        self.assertEqual(second_media["next_retry_at"], first_media["next_retry_at"])
        self.assertNotIn("secret", str(second))

    def test_attempt_budget_is_one_share_per_pass(self) -> None:
        def absent(mount: ControllerSmbMount, _paths: object) -> ControllerMountProbe:
            return ControllerMountProbe(
                False, False, mount.mount_point, failure_kind="mount_absent"
            )

        def failed(mount: ControllerSmbMount, paths: object) -> ControllerMountResult:
            return ControllerMountResult(
                False, False, "mount_failed", None, absent(mount, paths)
            )
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", side_effect=absent,
        ), patch.object(recovery, "mount_controller_smb_no_ui", side_effect=failed) as mount:
            recovery.process_controller_storage_recovery_once(self.config)

        first_mount_calls = [call for call in mount.call_args_list if call.args[0] == self.mounts[0]]
        self.assertEqual(len(first_mount_calls), 1)

    def test_action_required_latch_only_fresh_probes_until_manual_recovery(self) -> None:
        absent = ControllerMountProbe(
            False, False, self.mounts[0].mount_point, failure_kind="mount_absent"
        )
        timeout = ControllerMountResult(False, True, "mount_timeout", "private", absent)
        ready = ControllerMountProbe(
            True, True, self.mounts[0].mount_point, self.mounts[0].source, "smbfs", True
        )
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=absent,
        ), patch.object(recovery, "mount_controller_smb_no_ui", return_value=timeout) as mount:
            recovery.process_controller_storage_recovery_once(self.config)
            recovery.process_controller_storage_recovery_once(self.config)
        first_mount_calls = [call for call in mount.call_args_list if call.args[0] == self.mounts[0]]
        self.assertEqual(len(first_mount_calls), 1)
        latched = recovery.controller_storage_recovery_snapshot(self.config)
        self.assertEqual(latched["mounts"][0]["status"], "action_required")

        changed_mounts = [
            ControllerSmbMount("//user@replacement/media", Path("/Volumes/media")),
            *self.mounts[1:],
        ]
        save_controller_smb_mounts(
            controller_smb_mounts_path(self.config.paths.runtime_settings_path),
            changed_mounts,
        )
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=absent,
        ), patch.object(recovery, "mount_controller_smb_no_ui") as changed_identity_mount:
            recovery.process_controller_storage_recovery_once(self.config)
        changed_identity_calls = [
            call
            for call in changed_identity_mount.call_args_list
            if call.args[0].mount_point == Path("/Volumes/media")
        ]
        self.assertEqual(changed_identity_calls, [])

        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=ready,
        ), patch.object(recovery, "mount_controller_smb_no_ui") as mount:
            recovery.process_controller_storage_recovery_once(self.config)
        mount.assert_not_called()
        recovered = recovery.controller_storage_recovery_snapshot(self.config)
        self.assertEqual({item["status"] for item in recovered["mounts"]}, {"ready"})

    def test_missing_mapping_requires_action_without_mount_attempt(self) -> None:
        save_controller_smb_mounts(
            controller_smb_mounts_path(self.config.paths.runtime_settings_path),
            [],
        )
        absent = ControllerMountProbe(
            False, False, Path("/Volumes/media"), failure_kind="mount_absent"
        )
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount",
        ) as smb_probe, patch.object(
            recovery, "probe_controller_volume", return_value=absent,
        ) as volume_probe, patch.object(recovery, "mount_controller_smb_no_ui") as mount:
            recovery.process_controller_storage_recovery_once(self.config)

        smb_probe.assert_not_called()
        self.assertEqual(volume_probe.call_count, 4)
        mount.assert_not_called()
        snapshot = recovery.controller_storage_recovery_snapshot(self.config)
        self.assertEqual({item["status"] for item in snapshot["mounts"]}, {"action_required"})

    def test_admission_fails_closed_for_stale_ready_and_ignores_stream_host(self) -> None:
        ready = ControllerMountProbe(
            True, True, self.mounts[0].mount_point, self.mounts[0].source, "smbfs", True
        )
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=ready,
        ):
            recovery.process_controller_storage_recovery_once(self.config)

        mounted_host = self.config.remote_hosts[0]
        stream_host = self.config.remote_hosts[1]
        with patch.object(recovery, "_utc_now", return_value=NOW):
            self.assertIsNone(recovery.controller_storage_admission_issue(self.config, mounted_host))
            self.assertIsNone(recovery.controller_storage_admission_issue(self.config, stream_host))
        with patch.object(
            recovery, "_utc_now", return_value=datetime(2026, 9, 12, 12, 2, tzinfo=timezone.utc),
        ):
            self.assertIn(
                "fresh",
                recovery.controller_storage_admission_issue(self.config, mounted_host) or "",
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
