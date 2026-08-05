from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.tuning.av1_validation_v3 import load_av1_validation_protocol_v3
from mediaforce.tuning.av1_validation_v3_qualification import (
    load_av1_validation_v3_qualification_plan,
    serialize_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_publication import (
    AV1_VALIDATION_V3_TIER1_PLAN_FILENAME,
    AV1_VALIDATION_V3_TIER1_PUBLICATION_DIRECTORY_PREFIX,
    AV1_VALIDATION_V3_TIER1_REQUEST_FILENAME,
    AV1ValidationV3Tier1PublicationError,
    AV1ValidationV3Tier1PublicationResult,
    publish_av1_validation_v3_tier1_preparation,
)
from mediaforce.tuning.av1_validation_v3_tier1_config_snapshot import (
    build_av1_validation_v3_tier1_config_snapshot,
)
from mediaforce.tuning.av1_validation_v3_tier1_request import (
    load_av1_validation_v3_tier1_authorization_request,
    serialize_av1_validation_v3_tier1_authorization_request,
)
from mediaforce.tuning.av1_validation_v3_tier1_runtime import (
    build_av1_validation_v3_tier1_toolchain_binding,
)
from scripts import verify_av1_cold_start_preregistration


_PROTOCOL_PATH = Path("docs/validation/av1-cold-start-preregistration-v3.json")
_KEY_ID = f"av1vqkey3_{'a' * 32}"
_REPOSITORY_COMMIT = "1" * 40
_REPOSITORY_TREE = "2" * 40
_FROZEN_AT = "2026-08-04T12:00:00Z"
_PLAN_VALID_UNTIL = "2026-08-05T12:00:00Z"
_REQUESTED_AT = "2026-08-04T13:00:00Z"
_REQUEST_VALID_UNTIL = "2026-08-04T20:00:00Z"
_TEMPORARY_PARENT = Path(tempfile.gettempdir()).resolve()


class AV1ValidationV3Tier1PublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            dir=_TEMPORARY_PARENT
        )
        self.temporary_root = Path(self.temporary_directory.name)
        self.repository_root = self.temporary_root / "repository"
        self.repository_root.mkdir(mode=0o700)
        self.output_root = self.temporary_root / "published"
        self.protocol = load_av1_validation_protocol_v3(_PROTOCOL_PATH)
        self.config_snapshot_bytes = build_av1_validation_v3_tier1_config_snapshot(
            _config(self.temporary_root)
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _publish(
        self,
        **overrides: object,
    ) -> AV1ValidationV3Tier1PublicationResult:
        arguments: dict[str, object] = {
            "protocol": self.protocol,
            "qualification_key_id": _KEY_ID,
            "repository_commit": _REPOSITORY_COMMIT,
            "repository_tree": _REPOSITORY_TREE,
            "config_bytes": self.config_snapshot_bytes,
            "ffmpeg_path": Path(sys.executable),
            "ffprobe_path": Path(sys.executable),
            "output_root": self.output_root,
            "repository_root": self.repository_root,
            "frozen_at": _FROZEN_AT,
            "plan_valid_until": _PLAN_VALID_UNTIL,
            "requested_at": _REQUESTED_AT,
            "request_valid_until": _REQUEST_VALID_UNTIL,
        }
        arguments.update(overrides)
        return publish_av1_validation_v3_tier1_preparation(**arguments)

    def test_publishes_canonical_owner_only_pair_without_authority(self) -> None:
        result = self._publish()

        self.assertTrue(result.created)
        self.assertEqual(
            result.directory.name,
            f"{AV1_VALIDATION_V3_TIER1_PUBLICATION_DIRECTORY_PREFIX}"
            f"{result.request.request_id}",
        )
        self.assertEqual(
            result.plan_path.read_bytes(),
            serialize_av1_validation_v3_qualification_plan(result.plan),
        )
        self.assertEqual(
            result.request_path.read_bytes(),
            serialize_av1_validation_v3_tier1_authorization_request(
                result.request
            ),
        )
        self.assertEqual(
            load_av1_validation_v3_qualification_plan(result.plan_path),
            result.plan,
        )
        self.assertEqual(
            load_av1_validation_v3_tier1_authorization_request(
                result.request_path
            ),
            result.request,
        )
        directory_info = result.directory.stat()
        self.assertEqual(directory_info.st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(directory_info.st_mode), 0o700)
        for path in (result.plan_path, result.request_path):
            file_info = path.stat()
            self.assertEqual(file_info.st_uid, os.getuid())
            self.assertEqual(stat.S_IMODE(file_info.st_mode), 0o600)
            self.assertEqual(file_info.st_nlink, 1)
        summary = result.to_summary()
        self.assertNotIn(str(self.output_root), json.dumps(summary))
        for field in (
            "private_inventory_read_authorized",
            "media_read_authorized",
            "tier2_execution_authorized",
            "runtime_execution_authorized",
            "qualification_execution_authorized",
            "key_creation_authorized",
            "evidence_creation_authorized",
            "evidence_eligible",
            "empirical_authority_conferred",
            "derivation_authorized",
            "holdout_authorized",
            "publication_authorized",
            "public_bundle_activation_allowed",
            "activation_authorized",
        ):
            self.assertFalse(summary[field])
        self.assertTrue(summary["execution_requires_separate_owner_authorization"])

    def test_identical_pair_is_idempotent_without_rewrite(self) -> None:
        first = self._publish()
        original_mtimes = (
            first.plan_path.stat().st_mtime_ns,
            first.request_path.stat().st_mtime_ns,
        )

        second = self._publish()

        self.assertFalse(second.created)
        self.assertEqual(second.plan, first.plan)
        self.assertEqual(second.request, first.request)
        self.assertEqual(
            original_mtimes,
            (
                second.plan_path.stat().st_mtime_ns,
                second.request_path.stat().st_mtime_ns,
            ),
        )

    def test_existing_conflict_fails_closed(self) -> None:
        result = self._publish()
        tampered = bytearray(result.plan_path.read_bytes())
        tampered[1] = ord("X") if tampered[1] != ord("X") else ord("Y")
        result.plan_path.write_bytes(bytes(tampered))
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            self._publish()
        self.assertEqual(raised.exception.reason_code, "artifact_conflict")

    def test_existing_incomplete_set_fails_closed(self) -> None:
        result = self._publish()
        result.request_path.unlink()
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            self._publish()
        self.assertEqual(raised.exception.reason_code, "artifact_incomplete")

    def test_existing_extra_member_fails_closed(self) -> None:
        result = self._publish()
        (result.directory / "unexpected").write_text("unexpected")
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            self._publish()
        self.assertEqual(raised.exception.reason_code, "artifact_malformed")

    def test_existing_fifo_member_fails_without_blocking(self) -> None:
        result = self._publish()
        result.plan_path.unlink()
        os.mkfifo(result.plan_path, 0o600)
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            self._publish()
        self.assertEqual(raised.exception.reason_code, "artifact_unsafe")

    def test_publication_root_rejects_unsafe_relationships_and_symlinks(self) -> None:
        cases = {
            "relative": Path("relative"),
            "repository": self.repository_root,
            "below_repository": self.repository_root / "artifacts",
            "above_repository": self.temporary_root,
        }
        for name, output_root in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
                    self._publish(output_root=output_root)
                self.assertEqual(
                    raised.exception.reason_code,
                    "publication_root_unsafe",
                )

        real_root = self.temporary_root / "real-output"
        real_root.mkdir(mode=0o700)
        symlink_root = self.temporary_root / "symlink-output"
        symlink_root.symlink_to(real_root, target_is_directory=True)
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            self._publish(output_root=symlink_root)
        self.assertEqual(raised.exception.reason_code, "publication_root_unsafe")

        intermediate = self.temporary_root / "symlink-parent"
        intermediate.symlink_to(real_root, target_is_directory=True)
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            self._publish(output_root=intermediate / "child")
        self.assertEqual(raised.exception.reason_code, "publication_root_unsafe")

        repository_link = self.temporary_root / "repository-link"
        repository_link.symlink_to(self.repository_root, target_is_directory=True)
        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_publication."
                "_open_or_create_owner_only_directory"
            ) as opener,
            self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised,
        ):
            self._publish(output_root=repository_link / "artifacts")
        self.assertEqual(raised.exception.reason_code, "publication_root_unsafe")
        opener.assert_not_called()

    def test_non_owner_only_existing_root_is_rejected(self) -> None:
        self.output_root.mkdir(mode=0o755)
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            self._publish()
        self.assertEqual(raised.exception.reason_code, "publication_root_unsafe")

    def test_invalid_contract_does_not_create_publication_root(self) -> None:
        with self.assertRaises(ValueError):
            self._publish(config_bytes=b"")
        self.assertFalse(self.output_root.exists())

    def test_staging_and_rename_failures_leave_no_partial_artifact(self) -> None:
        real_open = os.open

        def fail_staging_directory_open(
            path: str | bytes | int,
            flags: int,
            *args: object,
            **kwargs: object,
        ) -> int:
            if (
                isinstance(path, str)
                and path.startswith(
                    f".{AV1_VALIDATION_V3_TIER1_PUBLICATION_DIRECTORY_PREFIX}"
                )
                and flags & getattr(os, "O_DIRECTORY", 0)
            ):
                raise OSError("staging directory open failed")
            return real_open(path, flags, *args, **kwargs)

        with patch(
            "mediaforce.tuning.av1_validation_v3_tier1_publication.os.open",
            side_effect=fail_staging_directory_open,
        ):
            with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
                self._publish()
        self.assertEqual(raised.exception.reason_code, "publication_failed")
        self.assertEqual(list(self.output_root.iterdir()), [])

        with patch(
            "mediaforce.tuning.av1_validation_v3_tier1_publication._write_member",
            side_effect=OSError("write failed"),
        ):
            with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
                self._publish()
        self.assertEqual(raised.exception.reason_code, "publication_failed")
        self.assertEqual(list(self.output_root.iterdir()), [])

        with patch(
            "mediaforce.tuning.av1_validation_v3_tier1_publication.rename_exclusive",
            side_effect=OSError("rename failed"),
        ):
            with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
                self._publish()
        self.assertEqual(raised.exception.reason_code, "publication_failed")
        self.assertEqual(list(self.output_root.iterdir()), [])

    def test_toolchain_drift_fails_before_publication(self) -> None:
        first = build_av1_validation_v3_tier1_toolchain_binding(
            ffmpeg_path=Path(sys.executable),
            ffprobe_path=Path(sys.executable),
        )
        second = build_av1_validation_v3_tier1_toolchain_binding(
            ffmpeg_path=Path(sys.executable),
            ffprobe_path=Path("/bin/echo"),
        )
        with patch(
            "mediaforce.tuning.av1_validation_v3_tier1_publication."
            "build_av1_validation_v3_tier1_toolchain_binding",
            side_effect=(first, second),
        ):
            with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
                self._publish()
        self.assertEqual(raised.exception.reason_code, "toolchain_drifted")
        self.assertEqual(list(self.output_root.iterdir()), [])

    def test_publication_module_has_no_execution_authority_imports(self) -> None:
        source = Path(
            "mediaforce/tuning/av1_validation_v3_tier1_publication.py"
        ).read_text()
        for forbidden in (
            "av1_validation_v3_tier1_grant",
            "av1_validation_v3_tier1_executor",
            "av1_validation_v3_tier1_coverage",
            "mediaforce.core.db",
            "mediaforce.web.runtime_lock",
            "subprocess",
        ):
            self.assertNotIn(forbidden, source)


class AV1ValidationV3Tier1PublicationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            dir=_TEMPORARY_PARENT
        )
        self.temporary_root = Path(self.temporary_directory.name)
        self.output_root = self.temporary_root / "published"
        self.config_artifact = self.temporary_root / "tier1-config.toml"
        self.config_artifact.write_bytes(
            build_av1_validation_v3_tier1_config_snapshot(
                _config(self.temporary_root)
            )
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _argv(self) -> list[str]:
        return [
            "publish-tier1-preparation",
            str(_PROTOCOL_PATH),
            "--qualification-key-id",
            _KEY_ID,
            "--repository-commit",
            _REPOSITORY_COMMIT,
            "--repository-tree",
            _REPOSITORY_TREE,
            "--config-artifact",
            str(self.config_artifact),
            "--ffmpeg",
            sys.executable,
            "--ffprobe",
            sys.executable,
            "--output-root",
            str(self.output_root),
            "--frozen-at",
            _FROZEN_AT,
            "--plan-valid-until",
            _PLAN_VALID_UNTIL,
            "--requested-at",
            _REQUESTED_AT,
            "--request-valid-until",
            _REQUEST_VALID_UNTIL,
            "--json",
        ]

    def test_runner_cross_checks_identity_and_publishes_json_summary(self) -> None:
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
                "_live_repository_identity",
                return_value=(_REPOSITORY_COMMIT, _REPOSITORY_TREE),
            ),
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
            exit_code = verify_av1_cold_start_preregistration.main(self._argv())

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["published"])
        self.assertFalse(payload["qualification_execution_authorized"])
        self.assertEqual(len(list(self.output_root.iterdir())), 1)

    def test_runner_rejects_identity_mismatch_without_calling_publisher(self) -> None:
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
                "_live_repository_identity",
                return_value=("3" * 40, _REPOSITORY_TREE),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "publish_av1_validation_v3_tier1_preparation",
            ) as publisher,
            redirect_stdout(stdout),
        ):
            exit_code = verify_av1_cold_start_preregistration.main(self._argv())

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            json.loads(stdout.getvalue())["failure_reason"],
            "repository_identity_mismatch",
        )
        publisher.assert_not_called()

    def test_runner_uses_closed_failure_for_unreadable_config(self) -> None:
        self.config_artifact.unlink()
        stdout = io.StringIO()
        stderr = io.StringIO()
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
                "_live_repository_identity",
                return_value=(_REPOSITORY_COMMIT, _REPOSITORY_TREE),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = verify_av1_cold_start_preregistration.main(self._argv())

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            json.loads(stdout.getvalue())["failure_reason"],
            "input_unreadable",
        )
        self.assertEqual(stderr.getvalue(), "")


def _config(root: Path) -> MediaforceConfig:
    return MediaforceConfig(
        raw={
            "config": {"include_files": ["folder-defaults.toml"]},
            "video": {"codec": "av1"},
        },
        paths=ConfigPaths(
            project_root=root,
            config_path=root / "config.toml",
            db_path=root / "mediaforce.sqlite3",
            run_manifest_dir=root / "manifests",
            web_state_dir=root / "web-state",
            review_dir=root / "review",
            runtime_settings_path=root / "runtime-settings.json",
            runtime_reservation_dir=root / "runtime-reservations",
        ),
    )
