from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from mediaforce.tuning.av1_validation_v3 import (
    av1_validation_v3_qualification_key_id,
)
from mediaforce.tuning.av1_validation_v3_tier1_publication import (
    AV1_VALIDATION_V3_QUALIFICATION_KEY_DIRECTORY,
    AV1_VALIDATION_V3_QUALIFICATION_KEY_FILENAME,
    AV1ValidationV3Tier1PublicationError,
    ensure_av1_validation_v3_qualification_key,
    load_av1_validation_v3_qualification_key,
)
from scripts import verify_av1_cold_start_preregistration


_TEMPORARY_PARENT = Path(tempfile.gettempdir()).resolve()


class AV1ValidationV3QualificationKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            dir=_TEMPORARY_PARENT
        )
        self.root = Path(self.temporary_directory.name)
        self.repository_root = self.root / "repository"
        self.repository_root.mkdir(mode=0o700)
        self.output_root = self.root / "qualification-key-root"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_key_creation_is_owner_only_durable_and_idempotent(self) -> None:
        first = ensure_av1_validation_v3_qualification_key(
            output_root=self.output_root,
            repository_root=self.repository_root,
        )
        first_key = load_av1_validation_v3_qualification_key(
            output_root=self.output_root,
            repository_root=self.repository_root,
        )
        second = ensure_av1_validation_v3_qualification_key(
            output_root=self.output_root,
            repository_root=self.repository_root,
        )
        second_key = load_av1_validation_v3_qualification_key(
            output_root=self.output_root,
            repository_root=self.repository_root,
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first_key, second_key)
        self.assertEqual(len(first_key), 32)
        self.assertEqual(
            first.qualification_key_id,
            av1_validation_v3_qualification_key_id(first_key),
        )
        self.assertEqual(first.qualification_key_id, second.qualification_key_id)
        self.assertTrue(first.qualification_key_id.startswith("av1vqkey3_"))
        self.assertEqual(
            first.directory.name,
            AV1_VALIDATION_V3_QUALIFICATION_KEY_DIRECTORY,
        )
        self.assertEqual(
            first.key_path.name,
            AV1_VALIDATION_V3_QUALIFICATION_KEY_FILENAME,
        )
        root_info = self.output_root.stat()
        self.assertEqual(root_info.st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(root_info.st_mode), 0o700)
        directory_info = first.directory.stat()
        self.assertEqual(directory_info.st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(directory_info.st_mode), 0o700)
        key_info = first.key_path.stat()
        self.assertEqual(key_info.st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(key_info.st_mode), 0o600)
        self.assertEqual(key_info.st_nlink, 1)
        summary = first.to_summary()
        self.assertNotIn(str(self.output_root), json.dumps(summary))
        self.assertFalse(summary["key_creation_authorized"])
        self.assertFalse(summary["qualification_execution_authorized"])

    def test_key_loader_rejects_incomplete_extra_fifo_and_wrong_size(self) -> None:
        result = ensure_av1_validation_v3_qualification_key(
            output_root=self.output_root,
            repository_root=self.repository_root,
        )
        result.key_path.unlink()
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            load_av1_validation_v3_qualification_key(
                output_root=self.output_root,
                repository_root=self.repository_root,
            )
        self.assertEqual(raised.exception.reason_code, "artifact_incomplete")

        result.key_path.write_bytes(b"x" * 32)
        (result.directory / "unexpected").write_text("unexpected")
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            load_av1_validation_v3_qualification_key(
                output_root=self.output_root,
                repository_root=self.repository_root,
            )
        self.assertEqual(raised.exception.reason_code, "artifact_malformed")

        (result.directory / "unexpected").unlink()
        result.key_path.unlink()
        os.mkfifo(result.key_path, 0o600)
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            load_av1_validation_v3_qualification_key(
                output_root=self.output_root,
                repository_root=self.repository_root,
            )
        self.assertEqual(raised.exception.reason_code, "artifact_unsafe")

        result.key_path.unlink()
        descriptor = os.open(
            result.key_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(descriptor, b"too-short")
        finally:
            os.close(descriptor)
        with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
            load_av1_validation_v3_qualification_key(
                output_root=self.output_root,
                repository_root=self.repository_root,
            )
        self.assertEqual(raised.exception.reason_code, "artifact_unsafe")

    def test_missing_key_load_does_not_create_output_root(self) -> None:
        with self.assertRaises(AV1ValidationV3Tier1PublicationError):
            load_av1_validation_v3_qualification_key(
                output_root=self.output_root,
                repository_root=self.repository_root,
            )
        self.assertFalse(self.output_root.exists())

    def test_runner_creates_key_without_runtime_or_database_authority(self) -> None:
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
                "exclusive_mediaforce_runtime_lock",
                side_effect=AssertionError("runtime lock must not be used"),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "open_readonly_db",
                side_effect=AssertionError("database must not be used"),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_config",
                side_effect=AssertionError("config must not be loaded"),
            ),
            redirect_stdout(stdout),
        ):
            exit_code = verify_av1_cold_start_preregistration.main([
                "create-qualification-key",
                "--output-root",
                str(self.output_root),
                "--json",
            ])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["artifact_kind"], "v3_qualification_key")
        self.assertTrue(payload["qualification_key_id"].startswith("av1vqkey3_"))
        self.assertFalse(payload["qualification_execution_authorized"])
