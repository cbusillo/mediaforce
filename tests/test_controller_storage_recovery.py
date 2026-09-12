from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
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
        self.assertEqual(volume_probe.call_count, 3)
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

    def test_explicit_mapping_overrides_learned_identity_and_invalidates_ready_signature(self) -> None:
        ready = ControllerMountProbe(
            True, True, self.mounts[0].mount_point, self.mounts[0].source, "smbfs", True
        )
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=ready,
        ):
            recovery.process_controller_storage_recovery_once(self.config)

        self.config.raw["controller_smb_mounts"] = [
            {"source": "//user@replacement/media", "mount_point": "/Volumes/media"}
        ]
        with patch.object(recovery, "_utc_now", return_value=NOW):
            self.assertIn(
                "changed",
                recovery.controller_storage_admission_issue(self.config, None) or "",
            )

        observed_mounts: list[ControllerSmbMount] = []

        def capture_probe(
                mount: ControllerSmbMount,
                _required_paths: object,
        ) -> ControllerMountProbe:
            observed_mounts.append(mount)
            return ControllerMountProbe(
                True, True, mount.mount_point, mount.source, "smbfs", True
            )

        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", side_effect=capture_probe,
        ):
            recovery.process_controller_storage_recovery_once(self.config)

        media_mount = next(
            mount for mount in observed_mounts if mount.mount_point == Path("/Volumes/media")
        )
        self.assertEqual(media_mount.source, "//user@replacement/media")

    def test_ready_from_previous_process_is_rejected_until_fresh_probe(self) -> None:
        ready = ControllerMountProbe(
            True, True, self.mounts[0].mount_point, self.mounts[0].source, "smbfs", True
        )
        with patch.object(recovery, "_PROCESS_GENERATION", "generation-a"), patch.object(
            recovery, "_utc_now", return_value=NOW,
        ), patch.object(recovery, "probe_controller_mount", return_value=ready):
            recovery.process_controller_storage_recovery_once(self.config)
            self.assertIsNone(recovery.controller_storage_admission_issue(self.config, None))

        with patch.object(recovery, "_PROCESS_GENERATION", "generation-b"), patch.object(
            recovery, "_utc_now", return_value=NOW,
        ), patch.object(recovery, "probe_controller_mount", return_value=ready):
            self.assertIn(
                "current service process",
                recovery.controller_storage_admission_issue(self.config, None) or "",
            )
            recovery.process_controller_storage_recovery_once(self.config)
            self.assertIsNone(recovery.controller_storage_admission_issue(self.config, None))

    def test_future_ready_timestamp_is_rejected(self) -> None:
        ready = ControllerMountProbe(
            True, True, self.mounts[0].mount_point, self.mounts[0].source, "smbfs", True
        )
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=ready,
        ):
            recovery.process_controller_storage_recovery_once(self.config)
        earlier = datetime(2026, 9, 12, 11, 59, tzinfo=timezone.utc)
        with patch.object(recovery, "_utc_now", return_value=earlier):
            self.assertIn(
                "fresh",
                recovery.controller_storage_admission_issue(self.config, None) or "",
            )

    def test_corrupt_existing_state_blocks_mount_until_fresh_readiness(self) -> None:
        recovery.controller_storage_recovery_path(self.config).write_text("{broken", encoding="utf-8")
        absent = ControllerMountProbe(
            False, False, self.mounts[0].mount_point, failure_kind="mount_absent"
        )
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=absent,
        ), patch.object(recovery, "mount_controller_smb_no_ui") as mount:
            recovery.process_controller_storage_recovery_once(self.config)
        mount.assert_not_called()
        blocked = recovery.controller_storage_recovery_snapshot(self.config)
        self.assertEqual({item["status"] for item in blocked["mounts"]}, {"action_required"})
        self.assertIn("unreadable", blocked["mounts"][0]["reason"])

        ready = ControllerMountProbe(
            True, True, self.mounts[0].mount_point, self.mounts[0].source, "smbfs", True
        )
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=ready,
        ), patch.object(recovery, "mount_controller_smb_no_ui") as mount:
            recovery.process_controller_storage_recovery_once(self.config)
        mount.assert_not_called()
        restored = recovery.controller_storage_recovery_snapshot(self.config)
        self.assertIsNone(restored["state_error"])
        self.assertEqual({item["status"] for item in restored["mounts"]}, {"ready"})

    def test_transient_probe_timeout_can_later_reach_automatic_mount(self) -> None:
        timeout = ControllerMountProbe(
            False, False, Path("/Volumes/media"), failure_kind="probe_timeout"
        )
        absent = ControllerMountProbe(
            False, False, Path("/Volumes/media"), failure_kind="mount_absent"
        )
        failed = ControllerMountResult(False, False, "mount_failed", None, absent)
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=timeout,
        ), patch.object(recovery, "mount_controller_smb_no_ui") as mount:
            recovery.process_controller_storage_recovery_once(self.config)
        mount.assert_not_called()

        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=absent,
        ), patch.object(
            recovery, "mount_controller_smb_no_ui", return_value=failed,
        ) as mount:
            recovery.process_controller_storage_recovery_once(self.config)
        self.assertEqual(mount.call_args_list[0].args[0].mount_point, Path("/Volumes/media"))

    def test_probe_timeout_does_not_clear_ambiguous_mount_latch(self) -> None:
        absent = ControllerMountProbe(
            False, False, Path("/Volumes/media"), failure_kind="mount_absent"
        )
        timeout_result = ControllerMountResult(
            False, True, "mount_timeout", None, absent
        )
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=absent,
        ), patch.object(
            recovery, "mount_controller_smb_no_ui", return_value=timeout_result,
        ):
            recovery.process_controller_storage_recovery_once(self.config)

        probe_timeout = ControllerMountProbe(
            False, False, Path("/Volumes/media"), failure_kind="probe_timeout"
        )
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=probe_timeout,
        ), patch.object(recovery, "mount_controller_smb_no_ui") as mount:
            recovery.process_controller_storage_recovery_once(self.config)
        mount.assert_not_called()

        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=absent,
        ), patch.object(recovery, "mount_controller_smb_no_ui") as mount:
            recovery.process_controller_storage_recovery_once(self.config)
        media_calls = [call for call in mount.call_args_list if call.args[0].mount_point == Path("/Volumes/media")]
        self.assertEqual(media_calls, [])

    def test_inaccessible_mounted_share_does_not_consume_mount_budget(self) -> None:
        def probe(mount: ControllerSmbMount, _paths: object) -> ControllerMountProbe:
            if mount.mount_point == Path("/Volumes/media"):
                return ControllerMountProbe(
                    True, False, mount.mount_point, mount.source, "smbfs", True,
                    "path_unavailable",
                )
            return ControllerMountProbe(
                False, False, mount.mount_point, failure_kind="mount_absent"
            )

        def fail_mount(mount: ControllerSmbMount, _paths: object) -> ControllerMountResult:
            absent = ControllerMountProbe(
                False, False, mount.mount_point, failure_kind="mount_absent"
            )
            return ControllerMountResult(False, False, "mount_failed", None, absent)

        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", side_effect=probe,
        ), patch.object(
            recovery, "mount_controller_smb_no_ui", side_effect=fail_mount,
        ) as mount:
            recovery.process_controller_storage_recovery_once(self.config)

        self.assertEqual(len(mount.call_args_list), 1)
        self.assertEqual(mount.call_args_list[0].args[0].mount_point, Path("/Volumes/remote-stage"))

    def test_ready_timestamp_is_captured_after_each_probe(self) -> None:
        times = [
            NOW,
            datetime(2026, 9, 12, 12, 0, 10, tzinfo=timezone.utc),
            datetime(2026, 9, 12, 12, 0, 20, tzinfo=timezone.utc),
            datetime(2026, 9, 12, 12, 0, 30, tzinfo=timezone.utc),
        ]

        def ready(mount: ControllerSmbMount, _paths: object) -> ControllerMountProbe:
            return ControllerMountProbe(
                True, True, mount.mount_point, mount.source, "smbfs", True
            )

        with patch.object(recovery, "_utc_now", side_effect=times), patch.object(
            recovery, "probe_controller_mount", side_effect=ready,
        ):
            recovery.process_controller_storage_recovery_once(self.config)

        snapshot = recovery.controller_storage_recovery_snapshot(self.config)
        self.assertEqual(
            {item["last_check_at"] for item in snapshot["mounts"]},
            {
                "2026-09-12T12:00:10Z",
                "2026-09-12T12:00:20Z",
                "2026-09-12T12:00:30Z",
            },
        )

    def test_stop_event_prevents_later_root_checks(self) -> None:
        stop_event = threading.Event()

        def ready_then_stop(mount: ControllerSmbMount, _paths: object) -> ControllerMountProbe:
            stop_event.set()
            return ControllerMountProbe(
                True, True, mount.mount_point, mount.source, "smbfs", True
            )

        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", side_effect=ready_then_stop,
        ) as probe:
            recovery.process_controller_storage_recovery_once(self.config, stop_event)

        self.assertEqual(probe.call_count, 1)
        snapshot = recovery.controller_storage_recovery_snapshot(self.config)
        self.assertEqual(len(snapshot["mounts"]), 1)

    def test_native_exception_leaves_durable_attempt_latch(self) -> None:
        absent = ControllerMountProbe(
            False, False, Path("/Volumes/media"), failure_kind="mount_absent"
        )
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=absent,
        ), patch.object(
            recovery, "mount_controller_smb_no_ui", side_effect=RuntimeError("crash"),
        ):
            with self.assertRaises(RuntimeError):
                recovery.process_controller_storage_recovery_once(self.config)

        persisted = recovery.controller_storage_recovery_snapshot(self.config)
        media = next(item for item in persisted["mounts"] if item["mount_point"] == "/Volumes/media")
        self.assertEqual(media["status"], "action_required")
        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", return_value=absent,
        ), patch.object(recovery, "mount_controller_smb_no_ui") as mount:
            recovery.process_controller_storage_recovery_once(self.config)
        media_calls = [call for call in mount.call_args_list if call.args[0].mount_point == Path("/Volumes/media")]
        self.assertEqual(media_calls, [])

    def test_unsupported_platform_launches_no_native_operations(self) -> None:
        with patch.object(recovery.sys, "platform", "linux"), patch.object(
            recovery, "probe_controller_mount",
        ) as smb_probe, patch.object(
            recovery, "probe_controller_volume",
        ) as volume_probe, patch.object(recovery, "mount_controller_smb_no_ui") as mount:
            recovery.process_controller_storage_recovery_once(self.config)

        smb_probe.assert_not_called()
        volume_probe.assert_not_called()
        mount.assert_not_called()
        snapshot = recovery.controller_storage_recovery_snapshot(self.config)
        self.assertEqual({item["status"] for item in snapshot["mounts"]}, {"action_required"})

    def test_ready_admission_stays_available_during_healthy_probe(self) -> None:
        def ready(mount: ControllerSmbMount, _paths: object) -> ControllerMountProbe:
            return ControllerMountProbe(
                True, True, mount.mount_point, mount.source, "smbfs", True
            )

        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", side_effect=ready,
        ):
            recovery.process_controller_storage_recovery_once(self.config)

        admissions_during_probe: list[str | None] = []

        def ready_with_admission(
                mount: ControllerSmbMount,
                _paths: object,
        ) -> ControllerMountProbe:
            admissions_during_probe.append(
                recovery.controller_storage_admission_issue(self.config, None)
            )
            return ControllerMountProbe(
                True, True, mount.mount_point, mount.source, "smbfs", True
            )

        with patch.object(recovery, "_utc_now", return_value=NOW), patch.object(
            recovery, "probe_controller_mount", side_effect=ready_with_admission,
        ):
            recovery.process_controller_storage_recovery_once(self.config)

        self.assertEqual(admissions_during_probe, [None, None, None])


if __name__ == "__main__":
    import unittest

    unittest.main()
