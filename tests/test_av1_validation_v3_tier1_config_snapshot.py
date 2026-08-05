from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.tuning.av1_validation_v3_tier1_config_snapshot import (
    AV1ValidationV3Tier1ConfigSnapshotError,
    assert_av1_validation_v3_tier1_config_snapshot_matches,
    av1_validation_v3_tier1_config_snapshot_sha256,
    build_av1_validation_v3_tier1_config_snapshot,
    validate_av1_validation_v3_tier1_config_snapshot,
)
from mediaforce.tuning.av1_validation_v3_tier1_publication import (
    AV1_VALIDATION_V3_TIER1_CONFIG_SNAPSHOT_FILENAME,
    AV1ValidationV3Tier1PublicationError,
    publish_av1_validation_v3_tier1_config_snapshot,
)
from scripts import verify_av1_cold_start_preregistration


_TEMPORARY_PARENT = Path(tempfile.gettempdir()).resolve()


class AV1ValidationV3Tier1ConfigSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            dir=_TEMPORARY_PARENT
        )
        self.root = Path(self.temporary_directory.name)
        self.repository_root = self.root / "repository"
        self.repository_root.mkdir(mode=0o700)
        self.config = _config(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_snapshot_is_canonical_and_binds_effective_config_and_paths(self) -> None:
        first = build_av1_validation_v3_tier1_config_snapshot(self.config)
        second = build_av1_validation_v3_tier1_config_snapshot(self.config)

        self.assertEqual(first, second)
        payload = validate_av1_validation_v3_tier1_config_snapshot(first)
        self.assertEqual(payload["effective_config"], self.config.raw)
        self.assertEqual(
            payload["resolved_paths"]["runtime_settings_path"],
            str(self.config.paths.runtime_settings_path.resolve()),
        )
        self.assertTrue(
            av1_validation_v3_tier1_config_snapshot_sha256(first).startswith(
                "sha256:"
            )
        )

    def test_snapshot_detects_effective_config_and_resolved_path_drift(self) -> None:
        snapshot = build_av1_validation_v3_tier1_config_snapshot(self.config)
        changed_raw = deepcopy(self.config.raw)
        changed_raw["video"]["codec"] = "changed"
        changed_config = replace(self.config, raw=changed_raw)
        with self.assertRaises(AV1ValidationV3Tier1ConfigSnapshotError):
            assert_av1_validation_v3_tier1_config_snapshot_matches(
                changed_config,
                snapshot,
            )

        changed_paths = replace(
            self.config.paths,
            runtime_settings_path=self.root / "changed-runtime-settings.json",
        )
        with self.assertRaises(AV1ValidationV3Tier1ConfigSnapshotError):
            assert_av1_validation_v3_tier1_config_snapshot_matches(
                replace(self.config, paths=changed_paths),
                snapshot,
            )

    def test_snapshot_validation_rejects_noncanonical_or_unknown_payloads(self) -> None:
        snapshot = build_av1_validation_v3_tier1_config_snapshot(self.config)
        for invalid in (
            b"",
            snapshot + b"\n",
            b'{"schema":"wrong"}\n',
        ):
            with self.subTest(invalid=invalid[:32]):
                with self.assertRaises(AV1ValidationV3Tier1ConfigSnapshotError):
                    validate_av1_validation_v3_tier1_config_snapshot(invalid)

    def test_snapshot_publication_is_owner_only_and_idempotent(self) -> None:
        snapshot = build_av1_validation_v3_tier1_config_snapshot(self.config)
        output_root = self.root / "published"

        first = publish_av1_validation_v3_tier1_config_snapshot(
            snapshot_bytes=snapshot,
            output_root=output_root,
            repository_root=self.repository_root,
        )
        second = publish_av1_validation_v3_tier1_config_snapshot(
            snapshot_bytes=snapshot,
            output_root=output_root,
            repository_root=self.repository_root,
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.snapshot_path.read_bytes(), snapshot)
        self.assertEqual(
            first.snapshot_path.name,
            AV1_VALIDATION_V3_TIER1_CONFIG_SNAPSHOT_FILENAME,
        )
        self.assertEqual(stat.S_IMODE(first.directory.stat().st_mode), 0o700)
        snapshot_info = first.snapshot_path.stat()
        self.assertEqual(snapshot_info.st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(snapshot_info.st_mode), 0o600)
        self.assertEqual(snapshot_info.st_nlink, 1)
        self.assertNotIn(str(output_root), json.dumps(first.to_summary()))

    def test_snapshot_publication_rejects_conflict_incomplete_extra_and_fifo(self) -> None:
        snapshot = build_av1_validation_v3_tier1_config_snapshot(self.config)
        output_root = self.root / "published-conflict"
        result = publish_av1_validation_v3_tier1_config_snapshot(
            snapshot_bytes=snapshot,
            output_root=output_root,
            repository_root=self.repository_root,
        )
        tampered = bytearray(result.snapshot_path.read_bytes())
        tampered[1] = ord("X") if tampered[1] != ord("X") else ord("Y")
        result.snapshot_path.write_bytes(bytes(tampered))
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            publish_av1_validation_v3_tier1_config_snapshot(
                snapshot_bytes=snapshot,
                output_root=output_root,
                repository_root=self.repository_root,
            )
        self.assertEqual(raised.exception.reason_code, "artifact_conflict")

        output_root = self.root / "published-incomplete"
        result = publish_av1_validation_v3_tier1_config_snapshot(
            snapshot_bytes=snapshot,
            output_root=output_root,
            repository_root=self.repository_root,
        )
        result.snapshot_path.unlink()
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            publish_av1_validation_v3_tier1_config_snapshot(
                snapshot_bytes=snapshot,
                output_root=output_root,
                repository_root=self.repository_root,
            )
        self.assertEqual(raised.exception.reason_code, "artifact_incomplete")

        output_root = self.root / "published-extra"
        result = publish_av1_validation_v3_tier1_config_snapshot(
            snapshot_bytes=snapshot,
            output_root=output_root,
            repository_root=self.repository_root,
        )
        (result.directory / "unexpected").write_text("unexpected")
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            publish_av1_validation_v3_tier1_config_snapshot(
                snapshot_bytes=snapshot,
                output_root=output_root,
                repository_root=self.repository_root,
            )
        self.assertEqual(raised.exception.reason_code, "artifact_malformed")

        output_root = self.root / "published-fifo"
        result = publish_av1_validation_v3_tier1_config_snapshot(
            snapshot_bytes=snapshot,
            output_root=output_root,
            repository_root=self.repository_root,
        )
        result.snapshot_path.unlink()
        os.mkfifo(result.snapshot_path, 0o600)
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            publish_av1_validation_v3_tier1_config_snapshot(
                snapshot_bytes=snapshot,
                output_root=output_root,
                repository_root=self.repository_root,
            )
        self.assertEqual(raised.exception.reason_code, "artifact_unsafe")

    def test_snapshot_publication_cleans_staging_write_failures(self) -> None:
        snapshot = build_av1_validation_v3_tier1_config_snapshot(self.config)
        output_root = self.root / "published-write-failure"
        with patch(
            "mediaforce.tuning.av1_validation_v3_tier1_publication._write_member",
            side_effect=OSError("write failed"),
        ):
            with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
                publish_av1_validation_v3_tier1_config_snapshot(
                    snapshot_bytes=snapshot,
                    output_root=output_root,
                    repository_root=self.repository_root,
                )
        self.assertEqual(raised.exception.reason_code, "publication_failed")
        self.assertEqual(list(output_root.iterdir()), [])

    def test_invalid_snapshot_does_not_create_output_root(self) -> None:
        output_root = self.root / "invalid-output"
        with self.assertRaises(AV1ValidationV3Tier1ConfigSnapshotError):
            publish_av1_validation_v3_tier1_config_snapshot(
                snapshot_bytes=b"not-a-snapshot",
                output_root=output_root,
                repository_root=self.repository_root,
            )
        self.assertFalse(output_root.exists())

    def test_runner_writes_snapshot_without_lock_database_or_media(self) -> None:
        output_root = self.root / "runner-output"
        config_path = self.root / "custom-config.toml"
        stdout = io.StringIO()
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_assert_preregistration_bootstrap_authority",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_assert_canonical_preregistration_runner",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_config",
                return_value=self.config,
            ) as load_config_mock,
            patch.object(
                verify_av1_cold_start_preregistration,
                "exclusive_mediaforce_runtime_lock",
                side_effect=AssertionError("runtime lock must not be used"),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "open_readonly_db",
                side_effect=AssertionError("database must not be used"),
            ),
            redirect_stdout(stdout),
        ):
            exit_code = verify_av1_cold_start_preregistration.main([
                "write-tier1-config-snapshot",
                "--config",
                str(config_path),
                "--output-root",
                str(output_root),
                "--json",
            ])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload["artifact_kind"],
            "tier1_effective_config_snapshot",
        )
        self.assertFalse(payload["qualification_execution_authorized"])
        load_config_mock.assert_called_once_with(config_path)


def _config(root: Path) -> MediaforceConfig:
    paths = ConfigPaths(
        project_root=root,
        config_path=root / "config.toml",
        db_path=root / "mediaforce.sqlite3",
        run_manifest_dir=root / "manifests",
        web_state_dir=root / "web-state",
        review_dir=root / "review",
        runtime_settings_path=root / "runtime-settings.json",
        runtime_reservation_dir=root / "runtime-reservations",
    )
    return MediaforceConfig(
        raw={
            "config": {"include_files": ["folder-defaults.toml"]},
            "video": {"codec": "av1"},
            "folder_policy_overrides": {"tv/example": {"video": {"crf": 31}}},
        },
        paths=paths,
    )
