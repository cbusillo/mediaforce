from __future__ import annotations

import copy
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
)
from mediaforce.tuning.av1_validation_v4_freeze import (
    AV1ValidationV4FreezeError,
    assert_av1_validation_v4_manifest_freeze,
    assert_av1_validation_v4_manifest_freeze_binds_bundle,
    av1_validation_v4_manifest_revision_2_is_frozen,
    build_av1_validation_v4_manifest_freeze,
    load_av1_validation_v4_manifest_freeze,
    serialize_av1_validation_v4_manifest_freeze,
)
from mediaforce.tuning.av1_validation_v4_freeze_operation import (
    AV1_VALIDATION_V4_FREEZE_FILENAME,
    AV1ValidationV4FreezeOperationError,
    AV1ValidationV4FreezeOperationInputs,
    AV1ValidationV4FreezeOperationResult,
    materialize_av1_validation_v4_manifest_freeze,
)
from mediaforce.tuning import av1_validation_v4_freeze_operation
from mediaforce.tuning.av1_validation_v4_preparation_config import (
    load_av1_validation_v4_effective_config_snapshot,
    serialize_av1_validation_v4_effective_config_snapshot,
)
from mediaforce.tuning.av1_validation_v4_preparation_grant import (
    load_av1_validation_v4_preparation_grant,
)
from mediaforce.tuning.av1_validation_v4_preparation_operation import (
    run_av1_validation_v4_preparation_operation,
)
from tests import test_av1_validation_v4_preparation as preparation_test_module


class AV1ValidationV4FreezeTests(unittest.TestCase):
    def test_freeze_is_deterministic_public_safe_and_non_executing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            first = self._freeze(bundle)
            second = self._freeze(bundle)
            self.assertEqual(first, second)
            self.assertTrue(
                av1_validation_v4_manifest_revision_2_is_frozen(
                    first,
                    rights_attestation=bundle["rights"],
                    preparation_grant=bundle["grant"],
                    preparation_claim=bundle["claim"],
                    effective_config=bundle["config"],
                    preparation=bundle["preparation"],
                    preparation_measurement=bundle["measurement"],
                )
            )
            self.assertTrue(first["manifest_revision_2_owner_freeze_approved"])
            self.assertFalse(first["manifest_freeze_authorized"])
            self.assertFalse(first["qualification_execution_authorized"])
            self.assertNotIn("/Volumes/", json.dumps(first, sort_keys=True))
            for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                with self.subTest(field=field):
                    self.assertIs(first[field], False)

    def test_freeze_binds_all_config_identity_domains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            freeze = self._freeze(bundle)
            expected_file_sha = "sha256:" + hashlib.sha256(
                serialize_av1_validation_v4_effective_config_snapshot(
                    bundle["config"]
                )
            ).hexdigest()
            self.assertEqual(
                freeze["config_id"],
                bundle["config"]["config_id"],
            )
            self.assertEqual(
                freeze["config_payload_sha256"],
                bundle["config"]["payload_sha256"],
            )
            self.assertEqual(
                freeze["config_canonical_file_sha256"],
                expected_file_sha,
            )
            self.assertEqual(
                freeze["preparation_effective_config_sha256"],
                expected_file_sha,
            )

    def test_freeze_allows_decision_after_preparation_grant_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            freeze = self._freeze(bundle, decided_at="2026-08-07T08:00:00Z")
            self.assertEqual(freeze["decided_at"], "2026-08-07T08:00:00Z")

    def test_freeze_rejects_invalid_timeline_and_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            with self.assertRaisesRegex(AV1ValidationV4FreezeError, "timeline"):
                self._freeze(bundle, decided_at="2026-08-07T05:59:59Z")
            with self.assertRaisesRegex(AV1ValidationV4FreezeError, "timeline"):
                self._freeze(bundle, decided_at="2026-08-08T06:00:01Z")
            with self.assertRaisesRegex(AV1ValidationV4FreezeError, "canonical"):
                self._freeze(bundle, decided_at="2026-08-07T06:30:00.100000Z")
            authority = self._freeze(bundle)
            authority["qualification_execution_authorized"] = True
            with self.assertRaisesRegex(
                AV1ValidationV4FreezeError,
                "cannot authorize qualification_execution_authorized",
            ):
                assert_av1_validation_v4_manifest_freeze(authority)
            swapped = self._freeze(bundle)
            swapped["config_canonical_file_sha256"] = swapped[
                "config_payload_sha256"
            ]
            with self.assertRaisesRegex(
                AV1ValidationV4FreezeError,
                "reviewed state is invalid",
            ):
                assert_av1_validation_v4_manifest_freeze(swapped)
            private = self._freeze(bundle)
            private["owner_principal"] = "/Volumes/private"
            with self.assertRaisesRegex(
                AV1ValidationV4FreezeError,
                "machine-local text",
            ):
                assert_av1_validation_v4_manifest_freeze(private)

    def test_freeze_bundle_rejects_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            freeze = self._freeze(bundle)
            other = copy.deepcopy(bundle["measurement"])
            other["measurement_id"] = "av1vprepmeas4_" + "f" * 32
            with self.assertRaises(AV1ValidationV4FreezeError):
                assert_av1_validation_v4_manifest_freeze_binds_bundle(
                    freeze,
                    rights_attestation=bundle["rights"],
                    preparation_grant=bundle["grant"],
                    preparation_claim=bundle["claim"],
                    effective_config=bundle["config"],
                    preparation=bundle["preparation"],
                    preparation_measurement=other,
                )

    def test_materialization_is_canonical_owner_only_and_singleton(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root)
            registry = root / "freeze-registry"
            registry.mkdir(mode=0o700)
            inputs = self._operation_inputs(bundle, registry=registry)
            result = self._materialize(inputs)
            self.assertEqual(result.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(result.path.stat().st_nlink, 1)
            loaded = load_av1_validation_v4_manifest_freeze(
                result.path,
                rights_attestation=bundle["rights"],
                preparation_grant=bundle["grant"],
                preparation_claim=bundle["claim"],
                effective_config=bundle["config"],
                preparation=bundle["preparation"],
                preparation_measurement=bundle["measurement"],
            )
            self.assertEqual(loaded, result.freeze)
            linked_temp = registry / f".{result.path.name}.crash.tmp"
            os.link(result.path, linked_temp)
            stale_temp = registry / f".{result.path.name}.stale.tmp"
            stale_temp.write_bytes(b"stale complete temp")
            os.chmod(stale_temp, 0o600)
            self.assertEqual(result.path.stat().st_nlink, 2)
            reconciled = self._materialize(
                inputs,
                decided_at=datetime(2026, 8, 7, 6, 31, tzinfo=UTC),
            )
            self.assertEqual(reconciled.freeze, result.freeze)
            self.assertFalse(linked_temp.exists())
            self.assertFalse(stale_temp.exists())
            self.assertEqual(result.path.stat().st_nlink, 1)
            with self.assertRaisesRegex(
                AV1ValidationV4FreezeOperationError,
                "different freeze",
            ):
                self._materialize(
                    inputs,
                    decided_at=datetime(2026, 8, 7, 6, 31, tzinfo=UTC),
                    repository_identity=("b" * 40, "c" * 40),
                )

    def test_foreign_singleton_and_atomic_race_preserve_existing_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root)
            registry = root / "freeze-registry"
            registry.mkdir(mode=0o700)
            path = registry / AV1_VALIDATION_V4_FREEZE_FILENAME
            path.write_bytes(b"foreign")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(
                AV1ValidationV4FreezeOperationError,
                "different or invalid freeze",
            ):
                self._materialize(
                    self._operation_inputs(bundle, registry=registry)
                )
            self.assertEqual(path.read_bytes(), b"foreign")
            self.assertFalse(
                av1_validation_v4_freeze_operation._atomic_publish(
                    path,
                    b"replacement",
                )
            )
            self.assertEqual(path.read_bytes(), b"foreign")

    def test_symlinked_registry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root)
            target = root / "freeze-target"
            target.mkdir(mode=0o700)
            registry = root / "freeze-link"
            registry.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(
                AV1ValidationV4FreezeOperationError,
                "owner-only directory",
            ):
                self._materialize(
                    self._operation_inputs(bundle, registry=registry),
                )

    def test_load_rejects_noncanonical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self._bundle(root)
            freeze = self._freeze(bundle)
            path = root / "freeze.json"
            path.write_bytes(
                serialize_av1_validation_v4_manifest_freeze(
                    freeze,
                    rights_attestation=bundle["rights"],
                    preparation_grant=bundle["grant"],
                    preparation_claim=bundle["claim"],
                    effective_config=bundle["config"],
                    preparation=bundle["preparation"],
                    preparation_measurement=bundle["measurement"],
                )
                + b" "
            )
            with self.assertRaisesRegex(
                AV1ValidationV4FreezeError,
                "not canonical",
            ):
                load_av1_validation_v4_manifest_freeze(
                    path,
                    rights_attestation=bundle["rights"],
                    preparation_grant=bundle["grant"],
                    preparation_claim=bundle["claim"],
                    effective_config=bundle["config"],
                    preparation=bundle["preparation"],
                    preparation_measurement=bundle["measurement"],
                )

    def test_registry_inside_repository_and_bad_custody_are_rejected(self) -> None:
        repository_root = preparation_test_module.MANIFEST_PATH.resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=repository_root) as directory:
            registry = Path(directory)
            os.chmod(registry, 0o700)
            with self.assertRaisesRegex(
                AV1ValidationV4FreezeOperationError,
                "outside the repository",
            ):
                av1_validation_v4_freeze_operation._assert_registry(
                    registry,
                    repository_root=repository_root,
                )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "freeze.json"
            path.write_bytes(b"{}\n")
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(
                AV1ValidationV4FreezeOperationError,
                "custody verification failed",
            ):
                av1_validation_v4_freeze_operation._assert_file_custody(
                    path,
                    expected_size=3,
                )

    def test_repository_identity_requires_clean_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            tracked = repository / "tracked.txt"
            tracked.write_text("clean\n")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "fixture",
                ],
                cwd=repository,
                check=True,
            )
            commit, tree = (
                av1_validation_v4_freeze_operation._measure_repository_identity(
                    repository
                )
            )
            self.assertEqual(len(commit), 40)
            self.assertEqual(len(tree), 40)
            untracked = repository / "untracked.txt"
            untracked.write_text("untracked\n")
            with self.assertRaisesRegex(
                AV1ValidationV4FreezeOperationError,
                "must be clean",
            ):
                av1_validation_v4_freeze_operation._measure_repository_identity(
                    repository
                )
            untracked.unlink()
            tracked.write_text("dirty\n")
            with self.assertRaisesRegex(
                AV1ValidationV4FreezeOperationError,
                "must be clean",
            ):
                av1_validation_v4_freeze_operation._measure_repository_identity(
                    repository
                )

    def _bundle(self, root: Path) -> dict[str, object]:
        fixture = preparation_test_module.AV1ValidationV4PreparationTests()
        inputs, _digest_calls = fixture._operation_inputs(root)
        digests = iter(("1", "2", "3"))
        result = run_av1_validation_v4_preparation_operation(
            inputs,
            now=lambda: datetime(2026, 8, 7, 6, 0, tzinfo=UTC),
            random_bytes=lambda count: b"k" * count,
            probe_tool=fixture._probe_tool,
            binary_digest=lambda _path: "sha256:" + next(digests) * 64,
        )
        return {
            "root": root,
            "inputs": inputs,
            "rights": fixture._rights_attestation(),
            "grant": load_av1_validation_v4_preparation_grant(
                inputs.preparation_grant_path
            ),
            "claim": result.claim,
            "config": load_av1_validation_v4_effective_config_snapshot(
                result.artifact_paths["config"]
            ),
            "preparation": result.preparation,
            "measurement": result.measurement,
        }

    def _freeze(
        self,
        bundle: dict[str, object],
        *,
        decided_at: str = "2026-08-07T06:30:00Z",
    ) -> dict[str, object]:
        return build_av1_validation_v4_manifest_freeze(
            rights_attestation=bundle["rights"],
            preparation_grant=bundle["grant"],
            preparation_claim=bundle["claim"],
            effective_config=bundle["config"],
            preparation=bundle["preparation"],
            preparation_measurement=bundle["measurement"],
            owner_principal="owner:test",
            decided_at=decided_at,
            materializer_repository_commit="9" * 40,
            materializer_repository_tree="a" * 40,
        )

    def _operation_inputs(
        self,
        bundle: dict[str, object],
        *,
        registry: Path,
    ) -> AV1ValidationV4FreezeOperationInputs:
        inputs = bundle["inputs"]
        workspace = inputs.workspace
        return AV1ValidationV4FreezeOperationInputs(
            repository_root=preparation_test_module.MANIFEST_PATH.resolve().parents[2],
            registry=registry,
            manifest_path=preparation_test_module.MANIFEST_PATH.resolve(),
            rights_attestation_path=inputs.rights_attestation_path,
            preparation_grant_path=inputs.preparation_grant_path,
            effective_config_path=workspace
            / "configuration/effective-config-snapshot.json",
            preparation_path=workspace / "preparation/preparation-record.json",
            preparation_measurement_path=workspace
            / "measurements/preparation-measurement.json",
        )

    def _materialize(
        self,
        inputs: AV1ValidationV4FreezeOperationInputs,
        *,
        decided_at: datetime = datetime(2026, 8, 7, 6, 30, tzinfo=UTC),
        repository_identity: tuple[str, str] = ("9" * 40, "a" * 40),
    ) -> AV1ValidationV4FreezeOperationResult:
        with mock.patch.object(
            av1_validation_v4_freeze_operation,
            "_measure_repository_identity",
            return_value=repository_identity,
        ):
            return materialize_av1_validation_v4_manifest_freeze(
                inputs,
                now=lambda: decided_at,
            )


if __name__ == "__main__":
    unittest.main()
