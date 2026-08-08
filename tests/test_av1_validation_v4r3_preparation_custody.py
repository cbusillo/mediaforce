from __future__ import annotations

import ast
import os
import subprocess
import threading
import unittest
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from multiprocessing import get_all_start_methods, get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.tuning import av1_validation_v4r3_freeze as freeze_module
from mediaforce.tuning import (
    av1_validation_v4r3_freeze_operation as freeze_operation_module,
)
from mediaforce.tuning.av1_validation_v4r3_freeze import (
    AV1V4R3FreezeError,
    AV1_V4R3_FREEZE_APPROVAL_FIELD,
    build_av1_v4r3_owner_freeze,
    deserialize_av1_v4r3_owner_freeze,
    serialize_av1_v4r3_owner_freeze,
)
from mediaforce.tuning.av1_validation_v4r3_freeze_operation import (
    AV1V4R3FreezeOperationError,
    load_av1_v4r3_owner_freeze,
    materialize_av1_v4r3_owner_freeze,
)
from mediaforce.tuning import av1_validation_v4r3_preparation_custody as pure_module
from mediaforce.tuning import (
    av1_validation_v4r3_preparation_custody_registry as registry_module,
)
from mediaforce.tuning import av1_validation_v4r3_preparation_bundle as bundle_module
from mediaforce.tuning import av1_validation_v4r3_preparation_config as config_module
from mediaforce.tuning import (
    av1_validation_v4r3_preparation_measurement as measurement_module,
)
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_SOURCE_IDS,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    build_av1_validation_v4_rights_attestation,
)
from mediaforce.tuning.av1_validation_v4r3_path_privacy import (
    av1_v4r3_path_privacy_key_id,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody import (
    AV1V4R3PreparationCustodyError,
    assert_av1_v4r3_path_privacy_key_custody,
    assert_av1_v4r3_preparation_claim,
    assert_av1_v4r3_preparation_registry_binding,
    build_av1_v4r3_path_privacy_key_custody,
    build_av1_v4r3_preparation_claim,
    build_av1_v4r3_preparation_registry_binding,
    deserialize_av1_v4r3_path_privacy_key_custody,
    deserialize_av1_v4r3_preparation_claim,
    deserialize_av1_v4r3_preparation_registry_binding,
    serialize_av1_v4r3_path_privacy_key_custody,
    serialize_av1_v4r3_preparation_claim,
    serialize_av1_v4r3_preparation_registry_binding,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody_registry import (
    AV1V4R3PreparationCustodyRegistryBinding,
    AV1V4R3PreparationCustodyRegistryError,
    AV1V4R3PreparationGrantPublication,
    assert_av1_v4r3_preparation_custody_file,
    assert_av1_v4r3_preparation_custody_registry,
    consume_av1_v4r3_preparation_grant,
    load_av1_v4r3_path_privacy_key_custody,
    load_av1_v4r3_preparation_claim,
    publish_av1_v4r3_preparation_grant,
)
from mediaforce.tuning import (
    av1_validation_v4r3_preparation_operation as operation_module,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_operation import (
    AV1V4R3PreparationBundleResult,
    run_av1_v4r3_preparation_bundle,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_bundle import (
    AV1V4R3PreparationBundleError,
    build_av1_v4r3_preparation_bundle,
    deserialize_av1_v4r3_preparation_bundle,
    serialize_av1_v4r3_preparation_bundle,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_config import (
    AV1V4R3PreparationConfigError,
    build_av1_v4r3_effective_config_snapshot,
    deserialize_av1_v4r3_effective_config_snapshot,
    serialize_av1_v4r3_effective_config_snapshot,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_grant import (
    build_av1_v4r3_preparation_grant,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_measurement import (
    AV1V4R3PreparationMeasurementError,
    build_av1_v4r3_preparation_failure_measurement,
    build_av1_v4r3_preparation_success_measurement,
    deserialize_av1_v4r3_preparation_measurement,
    serialize_av1_v4r3_preparation_measurement,
)
from mediaforce.tuning.av1_validation_v4r3_rights import (
    build_av1_v4r3_rights_attestation,
)


def _clock(hour: int, minute: int = 0) -> Callable[[], datetime]:
    return lambda: datetime(2026, 8, 8, hour, minute, tzinfo=UTC)


def _process_consume(registry: str, repository: str, results: Any) -> None:
    binding = AV1V4R3PreparationCustodyRegistryBinding(
        registry=Path(registry),
        repository_root=Path(repository),
    )
    try:
        consume_av1_v4r3_preparation_grant(
            binding=binding,
            rights_attestation=_rights(),
            clock=_clock(3, 10),
        )
    except AV1V4R3PreparationCustodyRegistryError:
        results.put("rejected")
    else:
        results.put("created")


def _grant() -> dict[str, object]:
    return build_av1_v4r3_preparation_grant(
        rights_attestation=_rights(),
        owner_principal="owner:test",
        repository_commit="1" * 40,
        repository_tree="2" * 40,
        authorized_at="2026-08-08T03:00:00Z",
        valid_until="2026-08-08T04:00:00Z",
    )


def _claim() -> dict[str, object]:
    return build_av1_v4r3_preparation_claim(
        preparation_grant=_grant(),
        rights_attestation=_rights(),
        claimed_at="2026-08-08T03:10:00Z",
    )


def _custody() -> dict[str, object]:
    return build_av1_v4r3_path_privacy_key_custody(
        preparation_claim=_claim(),
        key_id=av1_v4r3_path_privacy_key_id(b"k" * 32),
        created_at="2026-08-08T03:10:01Z",
    )


class AV1V4R3PreparationCustodyContractTests(unittest.TestCase):
    def test_artifacts_round_trip_without_paths_or_authority(self) -> None:
        artifacts = (
            (
                build_av1_v4r3_preparation_registry_binding(),
                serialize_av1_v4r3_preparation_registry_binding,
                deserialize_av1_v4r3_preparation_registry_binding,
            ),
            (
                _claim(),
                serialize_av1_v4r3_preparation_claim,
                deserialize_av1_v4r3_preparation_claim,
            ),
            (
                _custody(),
                serialize_av1_v4r3_path_privacy_key_custody,
                deserialize_av1_v4r3_path_privacy_key_custody,
            ),
        )
        for payload, serializer, deserializer in artifacts:
            with self.subTest(schema=payload["schema"]):
                data = serializer(payload)
                self.assertEqual(deserializer(data), payload)
                self.assertNotIn(b"/Volumes/", data)
                self.assertNotIn(b"path-privacy.key", data)
                for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                    self.assertIs(payload[field], False)
                with self.assertRaises(AV1V4R3PreparationCustodyError):
                    deserializer(data.rstrip(b"\n"))

    def test_claim_binds_active_grant_rights_owner_and_repository(self) -> None:
        claim = _claim()
        self.assertTrue(claim["claim_id"].startswith("av1v4r3prepclaim_"))
        self.assertEqual(claim["preparation_grant_id"], _grant()["grant_id"])
        self.assertEqual(claim["repository"], _grant()["repository"])
        with self.assertRaises(AV1V4R3PreparationCustodyError):
            build_av1_v4r3_preparation_claim(
                preparation_grant=_grant(),
                rights_attestation=_rights(),
                claimed_at="2026-08-08T04:00:00Z",
            )

    def test_custody_binds_claim_and_contains_no_key_material(self) -> None:
        custody = _custody()
        self.assertTrue(custody["custody_id"].startswith("av1v4r3keycustody_"))
        self.assertEqual(custody["claim_id"], _claim()["claim_id"])
        self.assertEqual(custody["key_bytes_length"], 32)
        self.assertEqual(custody["key_file_mode"], "0600")
        self.assertTrue(custody["key_file_create_exclusive"])
        self.assertFalse(custody["key_material_serialized"])
        self.assertNotIn("key_material", custody)
        with self.assertRaisesRegex(AV1V4R3PreparationCustodyError, "cannot precede"):
            build_av1_v4r3_path_privacy_key_custody(
                preparation_claim=_claim(),
                key_id=av1_v4r3_path_privacy_key_id(b"k" * 32),
                created_at="2026-08-08T03:09:59Z",
            )

    def test_mutations_and_hostile_shapes_fail_closed(self) -> None:
        cases: list[tuple[dict[str, object], object]] = []
        marker = build_av1_v4r3_preparation_registry_binding()
        marker["consumption_registry_token"] = "av1v4r3prepregistry_" + "0" * 32
        cases.append((marker, assert_av1_v4r3_preparation_registry_binding))
        claim = _claim()
        claim["owner_principal"] = "/Volumes/private"
        cases.append((claim, assert_av1_v4r3_preparation_claim))
        custody = _custody()
        custody["runtime_execution_authorized"] = True
        cases.append((custody, assert_av1_v4r3_path_privacy_key_custody))
        numeric_boolean = _custody()
        numeric_boolean["key_material_serialized"] = 0
        cases.append((numeric_boolean, assert_av1_v4r3_path_privacy_key_custody))
        boolean_version = _claim()
        boolean_version["schema_version"] = True
        cases.append((boolean_version, assert_av1_v4r3_preparation_claim))
        unknown = _custody()
        unknown["key_material"] = "secret"
        cases.append((unknown, assert_av1_v4r3_path_privacy_key_custody))
        for payload, assertion in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(AV1V4R3PreparationCustodyError):
                    assertion(payload)

        deep = _claim()
        nested: list[object] = []
        cursor = nested
        for _ in range(40):
            child: list[object] = []
            cursor.append(child)
            cursor = child
        deep["repository"] = nested
        with self.assertRaisesRegex(AV1V4R3PreparationCustodyError, "too deep"):
            assert_av1_v4r3_preparation_claim(deep)
        with self.assertRaisesRegex(AV1V4R3PreparationCustodyError, "non-finite"):
            deserialize_av1_v4r3_preparation_claim(b'{"schema":NaN}\n')

    def test_pure_module_has_no_io_or_execution_imports(self) -> None:
        tree = ast.parse(Path(pure_module.__file__).read_text())
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        for forbidden in {
            "fcntl",
            "os",
            "pathlib",
            "secrets",
            "subprocess",
            "mediaforce.execution",
            "mediaforce.core.db",
        }:
            self.assertNotIn(forbidden, imported_modules)
            self.assertNotIn(forbidden, imported_names)


class AV1V4R3PreparationCustodyRegistryTests(unittest.TestCase):
    def test_grant_publication_is_idempotent_and_conflicts_fail(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(Path(raw))
            first = _publish(binding)
            second = _publish(binding)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(first.grant, second.grant)
            with self.assertRaisesRegex(
                AV1V4R3PreparationCustodyRegistryError, "conflicts"
            ):
                publish_av1_v4r3_preparation_grant(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    repository_commit="1" * 40,
                    repository_tree="3" * 40,
                    authorized_at="2026-08-08T03:00:00Z",
                    valid_until="2026-08-08T04:00:00Z",
                    clock=_clock(3, 5),
                )

    def test_consumption_creates_custody_without_exposing_key_bytes(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(Path(raw))
            _publish(binding)
            result = consume_av1_v4r3_preparation_grant(
                binding=binding,
                rights_attestation=_rights(),
                clock=_clock(3, 10),
            )
            key_path = result.artifact_paths["path_privacy_key"]
            metadata = key_path.stat()
            self.assertEqual(metadata.st_size, 32)
            self.assertEqual(metadata.st_mode & 0o777, 0o600)
            self.assertEqual(metadata.st_nlink, 1)
            self.assertEqual(load_av1_v4r3_preparation_claim(binding), result.claim)
            self.assertEqual(
                load_av1_v4r3_path_privacy_key_custody(binding),
                result.key_custody,
            )
            assert_av1_v4r3_preparation_custody_file(
                binding, "path-privacy-key-custody.json"
            )
            for payload in (result.claim, result.key_custody):
                self.assertNotIn(str(binding.registry), repr(payload))
                self.assertFalse(_contains_bytes(payload))
                for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                    self.assertIs(payload[field], False)
            with self.assertRaisesRegex(
                AV1V4R3PreparationCustodyRegistryError, "already been consumed"
            ):
                consume_av1_v4r3_preparation_grant(
                    binding=binding,
                    rights_attestation=_rights(),
                    clock=_clock(3, 11),
                )
            with self.assertRaisesRegex(
                AV1V4R3PreparationCustodyRegistryError, "already consumed its grant"
            ):
                _publish(binding)

    def test_post_claim_failure_removes_key_and_permanently_burns_grant(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(Path(raw))
            _publish(binding)
            with patch.object(
                registry_module,
                "build_av1_v4r3_path_privacy_key_custody",
                side_effect=RuntimeError("injected custody failure"),
            ):
                with self.assertRaisesRegex(
                    AV1V4R3PreparationCustodyRegistryError,
                    "after consuming the grant",
                ):
                    consume_av1_v4r3_preparation_grant(
                        binding=binding,
                        rights_attestation=_rights(),
                        clock=_clock(3, 10),
                    )
            self.assertIsNotNone(load_av1_v4r3_preparation_claim(binding))
            self.assertFalse((binding.registry / "path-privacy.key").exists())
            self.assertFalse(
                (binding.registry / "path-privacy-key-custody.json").exists()
            )
            with self.assertRaisesRegex(
                AV1V4R3PreparationCustodyRegistryError, "already been consumed"
            ):
                consume_av1_v4r3_preparation_grant(
                    binding=binding,
                    rights_attestation=_rights(),
                    clock=_clock(3, 11),
                )

    def test_retained_claim_reconciles_orphan_key_after_crash(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(Path(raw))
            _publish(binding)
            result = consume_av1_v4r3_preparation_grant(
                binding=binding,
                rights_attestation=_rights(),
                clock=_clock(3, 10),
            )
            result.artifact_paths["path_privacy_key_custody"].unlink()
            self.assertTrue(result.artifact_paths["path_privacy_key"].exists())
            with self.assertRaisesRegex(
                AV1V4R3PreparationCustodyRegistryError, "already been consumed"
            ):
                consume_av1_v4r3_preparation_grant(
                    binding=binding,
                    rights_attestation=_rights(),
                    clock=_clock(3, 11),
                )
            self.assertFalse(result.artifact_paths["path_privacy_key"].exists())
            self.assertIsNotNone(load_av1_v4r3_preparation_claim(binding))

    def test_failed_claim_publication_does_not_consume_grant(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(Path(raw))
            _publish(binding)
            real_write = registry_module._RegistryContext.write_exclusive

            def fail_claim(
                context: object,
                filename: str,
                data: bytes,
            ) -> None:
                if filename == "preparation-claim.json":
                    raise AV1V4R3PreparationCustodyRegistryError(
                        "injected claim failure"
                    )
                real_write(context, filename, data)

            with patch.object(
                registry_module._RegistryContext,
                "write_exclusive",
                new=fail_claim,
            ):
                with self.assertRaisesRegex(
                    AV1V4R3PreparationCustodyRegistryError,
                    "injected claim failure",
                ):
                    consume_av1_v4r3_preparation_grant(
                        binding=binding,
                        rights_attestation=_rights(),
                        clock=_clock(3, 10),
                    )
            self.assertIsNone(load_av1_v4r3_preparation_claim(binding))
            self.assertFalse((binding.registry / "path-privacy.key").exists())
            consume_av1_v4r3_preparation_grant(
                binding=binding,
                rights_attestation=_rights(),
                clock=_clock(3, 11),
            )

    def test_failed_claim_link_is_failure_atomic(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(Path(raw))
            _publish(binding)
            real_link = registry_module.os.link

            def fail_claim_link(
                source: str,
                destination: str,
                **kwargs: object,
            ) -> None:
                if destination == "preparation-claim.json":
                    raise OSError("injected claim link failure")
                real_link(source, destination, **kwargs)

            with patch.object(registry_module.os, "link", new=fail_claim_link):
                with self.assertRaisesRegex(
                    AV1V4R3PreparationCustodyRegistryError,
                    "publication failed",
                ):
                    consume_av1_v4r3_preparation_grant(
                        binding=binding,
                        rights_attestation=_rights(),
                        clock=_clock(3, 10),
                    )
            self.assertIsNone(load_av1_v4r3_preparation_claim(binding))
            self.assertFalse(
                any(name.endswith(".tmp") for name in os.listdir(binding.registry))
            )
            consume_av1_v4r3_preparation_grant(
                binding=binding,
                rights_attestation=_rights(),
                clock=_clock(3, 11),
            )

    def test_thread_race_has_exactly_one_successful_consumer(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(Path(raw))
            _publish(binding)
            outcomes: list[str] = []

            def consume() -> None:
                try:
                    consume_av1_v4r3_preparation_grant(
                        binding=binding,
                        rights_attestation=_rights(),
                        clock=_clock(3, 10),
                    )
                except AV1V4R3PreparationCustodyRegistryError:
                    outcomes.append("rejected")
                else:
                    outcomes.append("created")

            threads = [threading.Thread(target=consume) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(outcomes.count("created"), 1)
            self.assertEqual(outcomes.count("rejected"), 3)

    @unittest.skipUnless("fork" in get_all_start_methods(), "requires fork")
    def test_process_race_has_exactly_one_successful_consumer(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(Path(raw))
            _publish(binding)
            context = get_context("fork")
            results = context.Queue()
            processes = [
                context.Process(
                    target=_process_consume,
                    args=(
                        str(binding.registry),
                        str(binding.repository_root),
                        results,
                    ),
                )
                for _ in range(3)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            outcomes = [results.get(timeout=2) for _ in processes]
            self.assertEqual(outcomes.count("created"), 1)
            self.assertEqual(outcomes.count("rejected"), 2)

    def test_registry_and_files_fail_closed_on_path_mode_and_links(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            binding = _binding(root)
            assert_av1_v4r3_preparation_custody_registry(binding)
            binding.registry.chmod(0o755)
            with self.assertRaises(AV1V4R3PreparationCustodyRegistryError):
                assert_av1_v4r3_preparation_custody_registry(binding)
            binding.registry.chmod(0o700)
            _publish(binding)
            grant_path = binding.registry / "preparation-grant.json"
            outside = root / "outside.json"
            outside.write_text("{}\n")
            outside.chmod(0o600)
            grant_path.unlink()
            os.symlink(outside, grant_path)
            with self.assertRaises(AV1V4R3PreparationCustodyRegistryError):
                assert_av1_v4r3_preparation_custody_file(
                    binding, "preparation-grant.json"
                )
            grant_path.unlink()
            os.link(outside, grant_path)
            with self.assertRaises(AV1V4R3PreparationCustodyRegistryError):
                assert_av1_v4r3_preparation_custody_file(
                    binding, "preparation-grant.json"
                )

    def test_stale_atomic_write_links_are_reconciled_under_lock(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(Path(raw))
            _publish(binding)
            grant_path = binding.registry / "preparation-grant.json"
            linked_temp = binding.registry / (
                ".preparation-grant.json.999.0123456789abcdef.tmp"
            )
            os.link(grant_path, linked_temp)
            orphan_temp = binding.registry / (
                ".path-privacy.key.999.fedcba9876543210.tmp"
            )
            orphan_temp.write_bytes(b"k" * 32)
            orphan_temp.chmod(0o600)
            self.assertEqual(grant_path.stat().st_nlink, 2)
            _publish(binding)
            self.assertFalse(linked_temp.exists())
            self.assertFalse(orphan_temp.exists())
            self.assertEqual(grant_path.stat().st_nlink, 1)

    def test_malformed_stale_temp_locks_registry_fail_closed(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(Path(raw))
            malformed = binding.registry / (
                ".preparation-grant.json.999.0123456789abcdef.tmp"
            )
            malformed.write_bytes(b"{}\n")
            malformed.chmod(0o644)
            with self.assertRaisesRegex(
                AV1V4R3PreparationCustodyRegistryError, "custody is invalid"
            ):
                _publish(binding)
            self.assertTrue(malformed.exists())

    def test_corrupt_registry_loaders_normalize_domain_errors(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(Path(raw))
            _publish(binding)
            result = consume_av1_v4r3_preparation_grant(
                binding=binding,
                rights_attestation=_rights(),
                clock=_clock(3, 10),
            )
            result.artifact_paths["preparation_claim"].write_bytes(b"\xff\n")
            with self.assertRaisesRegex(
                AV1V4R3PreparationCustodyRegistryError,
                "claim registry artifact is invalid",
            ):
                load_av1_v4r3_preparation_claim(binding)
            result.artifact_paths["preparation_claim"].write_bytes(
                serialize_av1_v4r3_preparation_claim(result.claim)
            )
            result.artifact_paths["path_privacy_key_custody"].write_bytes(b"\xff\n")
            with self.assertRaisesRegex(
                AV1V4R3PreparationCustodyRegistryError,
                "key custody registry artifact is invalid",
            ):
                load_av1_v4r3_path_privacy_key_custody(binding)

    def test_registry_must_be_disjoint_from_repository(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository = root / "repository"
            repository.mkdir()
            nested = repository / "registry"
            nested.mkdir(mode=0o700)
            binding = AV1V4R3PreparationCustodyRegistryBinding(
                registry=nested,
                repository_root=repository,
            )
            with self.assertRaisesRegex(
                AV1V4R3PreparationCustodyRegistryError, "outside"
            ):
                assert_av1_v4r3_preparation_custody_registry(binding)

    def test_registry_module_has_no_media_execution_or_subprocess_surface(self) -> None:
        source = Path(registry_module.__file__).read_text()
        tree = ast.parse(source)
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        for forbidden in {
            "subprocess",
            "mediaforce.execution",
            "mediaforce.core.db",
            "mediaforce.web",
        }:
            self.assertNotIn(forbidden, imported_modules)
            self.assertNotIn(forbidden, imported_names)
        self.assertNotIn("source_path", source)
        self.assertNotIn("media_path", source)


def _binding(root: Path) -> AV1V4R3PreparationCustodyRegistryBinding:
    repository = root / "repository"
    repository.mkdir()
    registry = root / "registry"
    registry.mkdir(mode=0o700)
    return AV1V4R3PreparationCustodyRegistryBinding(
        registry=registry.resolve(),
        repository_root=repository.resolve(),
    )


def _contains_bytes(value: object) -> bool:
    if isinstance(value, (bytes, bytearray)):
        return True
    if isinstance(value, dict):
        return any(
            _contains_bytes(key) or _contains_bytes(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_bytes(child) for child in value)
    return False


def _publish(
    binding: AV1V4R3PreparationCustodyRegistryBinding,
) -> AV1V4R3PreparationGrantPublication:
    return publish_av1_v4r3_preparation_grant(
        binding=binding,
        rights_attestation=_rights(),
        owner_principal="owner:test",
        repository_commit="1" * 40,
        repository_tree="2" * 40,
        authorized_at="2026-08-08T03:00:00Z",
        valid_until="2026-08-08T04:00:00Z",
        clock=_clock(3, 5),
    )


def _rights() -> dict[str, object]:
    return build_av1_v4r3_rights_attestation(prior_revision_attestation=_prior_rights())


def _prior_rights() -> dict[str, object]:
    source_claims = {
        AV1_VALIDATION_V4_SOURCE_IDS[0]: {
            "license_basis": "CC-BY-3.0",
            "attribution_required": True,
            "redistribution_disposition": "none",
            "terms_reviewed": ["sintel-sharing.html", "cc-by-3.0-legalcode.html"],
            "terms_summary": "Official Sintel sharing terms reviewed.",
        },
        AV1_VALIDATION_V4_SOURCE_IDS[1]: {
            "license_basis": "CC-BY-3.0",
            "license_precedence": "official_blender_gooseberry_title_grant",
            "netflix_mirror_is_technical_provenance_only": True,
            "attribution_required": True,
            "redistribution_disposition": "none",
            "terms_reviewed": [
                "cosmos-license.html",
                "cosmos-object-license-response.xml",
                "netflix-techblog-readme.txt",
                "cc-by-3.0-legalcode.html",
            ],
            "terms_summary": "Gooseberry title grant controls.",
        },
        AV1_VALIDATION_V4_SOURCE_IDS[2]: {
            "license_basis": "CC-BY-3.0",
            "attribution_required": True,
            "redistribution_disposition": "none",
            "terms_reviewed": ["tears-sharing.html", "cc-by-3.0-legalcode.html"],
            "terms_summary": "Official Tears of Steel sharing terms reviewed.",
        },
        AV1_VALIDATION_V4_SOURCE_IDS[3]: {
            "license_basis": "NASA-media-guidelines",
            "video_stream_only": True,
            "allowed_stream_indexes": [0],
            "excluded_stream_indexes": [1],
            "third_party_audio_ingredients_present": True,
            "nasa_acknowledgement_required": True,
            "endorsement_prohibited": True,
            "attribution_required": True,
            "redistribution_disposition": "none",
            "terms_reviewed": [
                "nasa-media-guidelines.html",
                "nasa-earth-views-metadata.json",
                "nasa-earth-views-asset.json",
            ],
            "terms_summary": "NASA guidance reviewed; only video stream 0 is eligible.",
        },
    }
    return build_av1_validation_v4_rights_attestation(
        owner_principal="owner:test",
        attested_at="2026-08-07T05:30:00Z",
        source_claims=source_claims,
    )


class AV1V4R3PreparationBundleContractTests(unittest.TestCase):
    def test_private_config_bundle_and_success_measurement_round_trip(self) -> None:
        config = _r3_config()
        bundle = _r3_bundle(config)
        measurement = build_av1_v4r3_preparation_success_measurement(
            preparation_grant=_grant(),
            preparation_claim=_claim(),
            key_custody=_custody(),
            effective_config=config,
            preparation_bundle=bundle,
            path_privacy_key=b"k" * 32,
            started_at="2026-08-08T03:10:02Z",
            completed_at="2026-08-08T03:10:03Z",
            probes=_r3_probes(),
        )
        self.assertEqual(
            deserialize_av1_v4r3_effective_config_snapshot(
                serialize_av1_v4r3_effective_config_snapshot(config)
            ),
            config,
        )
        bundle_bytes = serialize_av1_v4r3_preparation_bundle(bundle)
        self.assertEqual(deserialize_av1_v4r3_preparation_bundle(bundle_bytes), bundle)
        self.assertEqual(
            deserialize_av1_v4r3_preparation_measurement(
                serialize_av1_v4r3_preparation_measurement(measurement)
            ),
            measurement,
        )
        self.assertEqual(len(bundle["invocation_digests"]), 8)
        self.assertEqual(
            bundle["unresolved_execution_bindings"],
            {
                "production_stream_plan_id": None,
                "stream_budget_ledger_id": None,
                "assigned_stage": "execution_preflight",
            },
        )
        self.assertNotIn(b"/private/media", bundle_bytes)
        self.assertNotIn("effective_config_id", bundle)
        self.assertNotIn("effective_config_payload_sha256", bundle)
        self.assertRegex(
            str(bundle["effective_config_hmac_id"]),
            r"av1v4r3confighmac_[0-9a-f]{32}",
        )
        measurement_bytes = serialize_av1_v4r3_preparation_measurement(measurement)
        for private_root in (
            b"/private/media",
            b"/private/runtime",
            b"/private/state",
            b"/private/temp",
        ):
            self.assertNotIn(private_root, bundle_bytes)
            self.assertNotIn(private_root, measurement_bytes)
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            self.assertIs(bundle[field], False)
            self.assertIs(measurement[field], False)

    def test_effective_config_public_binding_is_keyed(self) -> None:
        first_config = _r3_config()
        changed_paths = _r3_source_paths()
        changed_paths[AV1_VALIDATION_V4_SOURCE_IDS[0]] = "/private/media/changed.mkv"
        second_config = build_av1_v4r3_effective_config_snapshot(
            repository_commit="1" * 40,
            repository_tree="2" * 40,
            source_paths=changed_paths,
            dedicated_instance_paths=_r3_instance_paths(),
            quality_temp_paths=_r3_quality_temp_paths(),
        )
        first_bundle = _r3_bundle(first_config)
        second_bundle = _r3_bundle(second_config)
        self.assertNotEqual(
            first_bundle["effective_config_hmac_id"],
            second_bundle["effective_config_hmac_id"],
        )
        self.assertNotIn(first_config["config_id"], repr(first_bundle))
        self.assertNotIn(first_config["payload_sha256"], repr(first_bundle))

    def test_pure_contract_modules_have_no_io_or_execution_imports(self) -> None:
        forbidden = {
            "fcntl",
            "os",
            "pathlib",
            "secrets",
            "subprocess",
            "mediaforce.execution",
            "mediaforce.core.db",
            "mediaforce.web",
        }
        for module in (config_module, bundle_module, measurement_module):
            tree = ast.parse(Path(module.__file__).read_text())
            imports = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module is not None
            } | {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            }
            with self.subTest(module=module.__name__):
                self.assertTrue(forbidden.isdisjoint(imports))

    def test_config_rejects_hostile_paths_without_filesystem_access(self) -> None:
        paths = _r3_source_paths()
        paths[AV1_VALIDATION_V4_SOURCE_IDS[0]] = "/private/media/../escape.mkv"
        with self.assertRaises(AV1V4R3PreparationConfigError):
            build_av1_v4r3_effective_config_snapshot(
                repository_commit="1" * 40,
                repository_tree="2" * 40,
                source_paths=paths,
                dedicated_instance_paths=_r3_instance_paths(),
                quality_temp_paths=_r3_quality_temp_paths(),
            )

    def test_failure_measurement_is_absorbing_and_stage_attributed(self) -> None:
        failure = build_av1_v4r3_preparation_failure_measurement(
            preparation_grant=_grant(),
            preparation_claim=_claim(),
            key_custody=_custody(),
            started_at="2026-08-08T03:10:02Z",
            completed_at="2026-08-08T03:10:03Z",
            stages_completed=("validate_custody_chain", "start_terminal_attempt"),
            probes=(),
            failure_stage="read_path_privacy_key",
            error_class="RuntimeError",
            error_code="stage_failed",
            message_sha256="sha256:" + "a" * 64,
        )
        self.assertEqual(failure["state"], "terminal_failure")
        self.assertIs(failure["retry_authorized"], False)
        self.assertIs(failure["resume_allowed"], False)
        tampered = dict(failure)
        tampered["resume_allowed"] = True
        with self.assertRaises(AV1V4R3PreparationMeasurementError):
            serialize_av1_v4r3_preparation_measurement(tampered)

    def test_bundle_rejects_replaced_invocation_digest(self) -> None:
        bundle = _r3_bundle(_r3_config())
        tampered = dict(bundle)
        invocations = [dict(item) for item in bundle["invocation_digests"]]
        invocations[0]["invocation_sha256"] = "sha256:" + "f" * 64
        tampered["invocation_digests"] = invocations
        tampered["bundle_id"] = bundle_module._bundle_id(tampered)
        without_sha = {
            key: value for key, value in tampered.items() if key != "payload_sha256"
        }
        tampered["payload_sha256"] = "sha256:" + stable_json_hash(without_sha)
        with self.assertRaises(AV1V4R3PreparationBundleError):
            serialize_av1_v4r3_preparation_bundle(tampered)

    def test_bundle_rejects_key_that_does_not_match_custody(self) -> None:
        with self.assertRaises(AV1V4R3PreparationBundleError):
            build_av1_v4r3_preparation_bundle(
                preparation_grant=_grant(),
                preparation_claim=_claim(),
                key_custody=_custody(),
                effective_config=_r3_config(),
                path_privacy_key=b"x" * 32,
                toolchain={
                    str(probe["tool"]): {
                        "binary_sha256": str(probe["binary_sha256"]),
                        "version": str(probe["version"]),
                    }
                    for probe in _r3_probes()
                },
                runtime={
                    "python_implementation": "CPython",
                    "python_version": "3.13.7",
                    "platform_system": "Darwin",
                    "platform_machine": "arm64",
                },
            )

    def test_new_artifacts_reject_noncanonical_bytes(self) -> None:
        config = _r3_config()
        bundle = _r3_bundle(config)
        measurement = build_av1_v4r3_preparation_success_measurement(
            preparation_grant=_grant(),
            preparation_claim=_claim(),
            key_custody=_custody(),
            effective_config=config,
            preparation_bundle=bundle,
            path_privacy_key=b"k" * 32,
            started_at="2026-08-08T03:10:02Z",
            completed_at="2026-08-08T03:10:03Z",
            probes=_r3_probes(),
        )
        cases = (
            (
                serialize_av1_v4r3_effective_config_snapshot(config),
                deserialize_av1_v4r3_effective_config_snapshot,
            ),
            (
                serialize_av1_v4r3_preparation_bundle(bundle),
                deserialize_av1_v4r3_preparation_bundle,
            ),
            (
                serialize_av1_v4r3_preparation_measurement(measurement),
                deserialize_av1_v4r3_preparation_measurement,
            ),
        )
        for data, loader in cases:
            with self.subTest(loader=loader.__name__), self.assertRaises(ValueError):
                loader(data.rstrip(b"\n"))


class AV1V4R3PreparationBundleOperationTests(unittest.TestCase):
    def test_probe_scope_must_match_the_canonical_version_argv(self) -> None:
        grant = dict(_grant())
        scope = dict(grant["operation_scope"])
        scope["ffmpeg_version_probe_argv"] = ["-i", "/private/media/1.mkv"]
        grant["operation_scope"] = scope
        with (
            patch.object(operation_module.subprocess, "run") as run,
            self.assertRaisesRegex(
                AV1V4R3PreparationCustodyRegistryError, "probe scope is invalid"
            ),
        ):
            operation_module._probe_tool_versions(grant, {}, None, [])
        run.assert_not_called()

    def test_operation_uses_nonexistent_media_paths_and_becomes_terminal(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            binding = _binding(root)
            _publish(binding)
            consume_av1_v4r3_preparation_grant(
                binding=binding,
                rights_attestation=_rights(),
                clock=_clock(3, 10),
            )
            tools = _stub_tools(root)
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=("1" * 40, "2" * 40),
            ):
                result = run_av1_v4r3_preparation_bundle(
                    binding=binding,
                    repository_commit="1" * 40,
                    repository_tree="2" * 40,
                    source_paths=_r3_source_paths(),
                    dedicated_instance_paths=_r3_instance_paths(),
                    quality_temp_paths=_r3_quality_temp_paths(),
                    tool_paths=tools,
                    clock=_clock(3, 11),
                )
            self.assertIsInstance(result, AV1V4R3PreparationBundleResult)
            self.assertEqual(result.measurement["state"], "terminal_success")
            self.assertTrue(result.artifact_paths["effective_config"].exists())
            self.assertTrue(result.artifact_paths["preparation_bundle"].exists())
            for path in _r3_source_paths().values():
                self.assertFalse(Path(path).exists())
            with self.assertRaisesRegex(
                AV1V4R3PreparationCustodyRegistryError, "already terminal"
            ):
                run_av1_v4r3_preparation_bundle(
                    binding=binding,
                    repository_commit="1" * 40,
                    repository_tree="2" * 40,
                    source_paths=_r3_source_paths(),
                    dedicated_instance_paths=_r3_instance_paths(),
                    quality_temp_paths=_r3_quality_temp_paths(),
                    tool_paths=tools,
                    clock=_clock(3, 12),
                )

    def test_post_start_failure_retains_custody_and_blocks_retry(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            binding = _binding(root)
            _publish(binding)
            custody_result = consume_av1_v4r3_preparation_grant(
                binding=binding,
                rights_attestation=_rights(),
                clock=_clock(3, 10),
            )
            tools = _stub_tools(root)
            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=("1" * 40, "2" * 40),
                ),
                patch.object(
                    operation_module,
                    "_probe_tool_versions",
                    side_effect=TimeoutError("private path /Volumes/secret"),
                ),
            ):
                with self.assertRaisesRegex(
                    AV1V4R3PreparationCustodyRegistryError, "attempt terminated"
                ):
                    run_av1_v4r3_preparation_bundle(
                        binding=binding,
                        repository_commit="1" * 40,
                        repository_tree="2" * 40,
                        source_paths=_r3_source_paths(),
                        dedicated_instance_paths=_r3_instance_paths(),
                        quality_temp_paths=_r3_quality_temp_paths(),
                        tool_paths=tools,
                        clock=_clock(3, 11),
                    )
            measurement = deserialize_av1_v4r3_preparation_measurement(
                (
                    binding.registry / "preparation-terminal-measurement.json"
                ).read_bytes()
            )
            self.assertEqual(measurement["state"], "terminal_failure")
            self.assertEqual(measurement["failure"]["stage"], "probe_toolchain")
            self.assertNotIn("Volumes", repr(measurement))
            self.assertTrue(custody_result.artifact_paths["path_privacy_key"].exists())
            self.assertTrue(
                custody_result.artifact_paths["path_privacy_key_custody"].exists()
            )
            self.assertFalse((binding.registry / "effective-config.json").exists())
            self.assertFalse((binding.registry / "preparation-bundle.json").exists())

    def test_racing_attempts_publish_one_terminal(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            binding = _binding(root)
            _publish(binding)
            consume_av1_v4r3_preparation_grant(
                binding=binding,
                rights_attestation=_rights(),
                clock=_clock(3, 10),
            )
            tools = _stub_tools(root)
            outcomes: list[str] = []

            def invoke() -> None:
                try:
                    run_av1_v4r3_preparation_bundle(
                        binding=binding,
                        repository_commit="1" * 40,
                        repository_tree="2" * 40,
                        source_paths=_r3_source_paths(),
                        dedicated_instance_paths=_r3_instance_paths(),
                        quality_temp_paths=_r3_quality_temp_paths(),
                        tool_paths=tools,
                        clock=_clock(3, 11),
                    )
                except AV1V4R3PreparationCustodyRegistryError:
                    outcomes.append("rejected")
                else:
                    outcomes.append("created")

            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=("1" * 40, "2" * 40),
            ):
                threads = [threading.Thread(target=invoke) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
            self.assertCountEqual(outcomes, ["created", "rejected"])
            self.assertTrue(
                (binding.registry / "preparation-terminal-measurement.json").exists()
            )

    def test_binary_substitution_publishes_terminal_failure(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            binding = _binding(root)
            _publish(binding)
            consume_av1_v4r3_preparation_grant(
                binding=binding,
                rights_attestation=_rights(),
                clock=_clock(3, 10),
            )
            tools = _stub_tools(root)
            tools["ab_av1"].write_text(
                "#!/bin/sh\nprintf '%s\\n' 'ab_av1 test 1.0'\nprintf '\\n' >> \"$0\"\n"
            )
            tools["ab_av1"].chmod(0o700)
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=("1" * 40, "2" * 40),
            ):
                with self.assertRaisesRegex(
                    AV1V4R3PreparationCustodyRegistryError, "attempt terminated"
                ):
                    run_av1_v4r3_preparation_bundle(
                        binding=binding,
                        repository_commit="1" * 40,
                        repository_tree="2" * 40,
                        source_paths=_r3_source_paths(),
                        dedicated_instance_paths=_r3_instance_paths(),
                        quality_temp_paths=_r3_quality_temp_paths(),
                        tool_paths=tools,
                        clock=_clock(3, 11),
                    )
            measurement = deserialize_av1_v4r3_preparation_measurement(
                (
                    binding.registry / "preparation-terminal-measurement.json"
                ).read_bytes()
            )
            self.assertEqual(measurement["state"], "terminal_failure")
            self.assertEqual(measurement["failure"]["stage"], "probe_toolchain")

    def test_interrupted_attempt_recovers_to_terminal_failure(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            binding = _binding(root)
            _publish(binding)
            custody_result = consume_av1_v4r3_preparation_grant(
                binding=binding,
                rights_attestation=_rights(),
                clock=_clock(3, 10),
            )
            with registry_module._locked_registry(binding) as context:
                context.write_exclusive(
                    registry_module._ATTEMPT_NAME,
                    operation_module._attempt_bytes(
                        grant=custody_result.grant,
                        claim=custody_result.claim,
                        custody=custody_result.key_custody,
                        started_at="2026-08-08T03:10:02Z",
                    ),
                )
            with self.assertRaisesRegex(
                AV1V4R3PreparationCustodyRegistryError, "already started"
            ):
                run_av1_v4r3_preparation_bundle(
                    binding=binding,
                    repository_commit="1" * 40,
                    repository_tree="2" * 40,
                    source_paths=_r3_source_paths(),
                    dedicated_instance_paths=_r3_instance_paths(),
                    quality_temp_paths=_r3_quality_temp_paths(),
                    tool_paths=_stub_tools(root),
                    clock=_clock(3, 11),
                )
            measurement = deserialize_av1_v4r3_preparation_measurement(
                (
                    binding.registry / "preparation-terminal-measurement.json"
                ).read_bytes()
            )
            self.assertEqual(measurement["state"], "terminal_failure")
            self.assertEqual(
                measurement["failure"]["error_code"], "interrupted_prior_attempt"
            )
            self.assertTrue(custody_result.artifact_paths["path_privacy_key"].exists())

    def test_repository_measurement_requires_exact_clean_toplevel(self) -> None:
        with TemporaryDirectory() as raw:
            repository = Path(raw) / "repository"
            repository.mkdir()
            _git(repository, "init")
            _git(repository, "config", "user.email", "test@example.com")
            _git(repository, "config", "user.name", "Mediaforce Test")
            (repository / "tracked.txt").write_text("tracked\n")
            _git(repository, "add", "tracked.txt")
            _git(repository, "commit", "-m", "fixture")
            commit, tree = operation_module._measure_clean_repository(repository)
            self.assertEqual(commit, _git(repository, "rev-parse", "HEAD"))
            self.assertEqual(tree, _git(repository, "rev-parse", "HEAD^{tree}"))
            nested = repository / "nested"
            nested.mkdir()
            with self.assertRaisesRegex(
                AV1V4R3PreparationCustodyRegistryError, "clean and canonical"
            ):
                operation_module._measure_clean_repository(nested)
            (repository / "untracked.txt").write_text("dirty\n")
            with self.assertRaisesRegex(
                AV1V4R3PreparationCustodyRegistryError, "clean and canonical"
            ):
                operation_module._measure_clean_repository(repository)


class AV1V4R3FreezeContractTests(unittest.TestCase):
    def test_freeze_round_trips_without_paths_or_downstream_authority(self) -> None:
        freeze = _r3_freeze()
        data = serialize_av1_v4r3_owner_freeze(freeze)
        self.assertEqual(deserialize_av1_v4r3_owner_freeze(data), freeze)
        self.assertIs(freeze[AV1_V4R3_FREEZE_APPROVAL_FIELD], True)
        self.assertEqual(len(freeze["invocation_digests"]), 8)
        self.assertEqual(
            freeze["effective_config_hmac_id"],
            _r3_bundle(_r3_config())["effective_config_hmac_id"],
        )
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            self.assertIs(freeze[field], False)
        for private_root in (
            b"/private/media",
            b"/private/runtime",
            b"/private/state",
            b"/private/temp",
        ):
            self.assertNotIn(private_root, data)
        with self.assertRaises(AV1V4R3FreezeError):
            deserialize_av1_v4r3_owner_freeze(data.rstrip(b"\n"))

    def test_freeze_rejects_terminal_failure_and_owner_mismatch(self) -> None:
        failure = build_av1_v4r3_preparation_failure_measurement(
            preparation_grant=_grant(),
            preparation_claim=_claim(),
            key_custody=_custody(),
            started_at="2026-08-08T03:10:02Z",
            completed_at="2026-08-08T03:10:03Z",
            stages_completed=("validate_custody_chain", "start_terminal_attempt"),
            probes=(),
            failure_stage="read_path_privacy_key",
            error_class="RuntimeError",
            error_code="stage_failed",
            message_sha256="sha256:" + "a" * 64,
        )
        with self.assertRaises(AV1V4R3FreezeError):
            _build_r3_freeze(measurement=failure)
        with self.assertRaises(AV1V4R3FreezeError):
            _build_r3_freeze(owner_principal="owner:other")

    def test_freeze_rejects_decision_outside_grant_window(self) -> None:
        with self.assertRaises(AV1V4R3FreezeError):
            _build_r3_freeze(decided_at="2026-08-08T04:00:00Z")

    def test_freeze_enforces_lag_manifest_and_chronology_boundaries(self) -> None:
        freeze = _r3_freeze()
        completed_at = datetime.fromisoformat(
            str(freeze["measurement_completed_at"]).replace("Z", "+00:00")
        )
        extended_grant = (completed_at + timedelta(days=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        at_lag_limit = dict(freeze)
        at_lag_limit["preparation_grant_valid_until"] = extended_grant
        at_lag_limit["decided_at"] = (
            completed_at + timedelta(seconds=86_400)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        serialize_av1_v4r3_owner_freeze(_rebind_r3_freeze(at_lag_limit))

        past_lag_limit = dict(at_lag_limit)
        past_lag_limit["decided_at"] = (
            completed_at + timedelta(seconds=86_401)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.assertRaises(AV1V4R3FreezeError):
            serialize_av1_v4r3_owner_freeze(_rebind_r3_freeze(past_lag_limit))

        manifest_deadline = datetime.fromisoformat(
            freeze_module.AV1_V4R3_MANIFEST_VALID_UNTIL.replace("Z", "+00:00")
        )
        at_manifest_deadline = dict(freeze)
        at_manifest_deadline["preparation_grant_valid_until"] = (
            manifest_deadline + timedelta(days=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        at_manifest_deadline["measurement_started_at"] = (
            manifest_deadline - timedelta(minutes=2)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        at_manifest_deadline["measurement_completed_at"] = (
            manifest_deadline - timedelta(minutes=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        at_manifest_deadline["decided_at"] = freeze_module.AV1_V4R3_MANIFEST_VALID_UNTIL
        with self.assertRaises(AV1V4R3FreezeError):
            serialize_av1_v4r3_owner_freeze(_rebind_r3_freeze(at_manifest_deadline))

        chronology_inversion = dict(freeze)
        chronology_inversion["decided_at"] = "2026-08-08T03:10:02Z"
        with self.assertRaises(AV1V4R3FreezeError):
            serialize_av1_v4r3_owner_freeze(_rebind_r3_freeze(chronology_inversion))

    def test_freeze_rejects_mutated_authority_and_invocation(self) -> None:
        freeze = _r3_freeze()
        mutated = dict(freeze)
        mutated["runtime_execution_authorized"] = True
        with self.assertRaises(AV1V4R3FreezeError):
            serialize_av1_v4r3_owner_freeze(mutated)
        mutated = dict(freeze)
        invocations = [dict(item) for item in freeze["invocation_digests"]]
        invocations[0]["ordinal"] = 2
        mutated["invocation_digests"] = invocations
        with self.assertRaises(AV1V4R3FreezeError):
            serialize_av1_v4r3_owner_freeze(mutated)

    def test_freeze_rejects_rebound_states_and_scalar_types(self) -> None:
        freeze = _r3_freeze()
        mutations = (
            ("preparation_bundle_state", "terminal_failure"),
            ("measurement_state", "terminal_failure"),
            ("schema_version", 1.0),
            (freeze_module.AV1_V4R3_FREEZE_APPROVAL_FIELD, 1),
            ("selection_or_partition_use_allowed", 0),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                mutated = dict(freeze)
                mutated[field] = value
                with self.assertRaises(AV1V4R3FreezeError):
                    serialize_av1_v4r3_owner_freeze(_rebind_r3_freeze(mutated))

        mutated = dict(freeze)
        mutated["freeze_scope"] = dict(freeze["freeze_scope"])
        mutated["freeze_scope"]["retry_authorized"] = 0
        with self.assertRaises(AV1V4R3FreezeError):
            serialize_av1_v4r3_owner_freeze(_rebind_r3_freeze(mutated))

        mutated = dict(freeze)
        mutated["freeze_scope"] = dict(freeze["freeze_scope"])
        mutated["freeze_scope"]["smuggled"] = False
        with self.assertRaises(AV1V4R3FreezeError):
            serialize_av1_v4r3_owner_freeze(_rebind_r3_freeze(mutated))

        mutated = dict(freeze)
        mutated["invocation_digests"] = [
            dict(item) for item in freeze["invocation_digests"]
        ]
        mutated["invocation_digests"][0]["ordinal"] = 1.0
        with self.assertRaises(AV1V4R3FreezeError):
            serialize_av1_v4r3_owner_freeze(_rebind_r3_freeze(mutated))

    def test_freeze_rejects_nonfinite_json_constants(self) -> None:
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                with self.assertRaises(AV1V4R3FreezeError):
                    deserialize_av1_v4r3_owner_freeze(
                        b'{"payload_sha256":' + constant + b"}\n"
                    )

    def test_freeze_serialization_normalizes_validation_errors(self) -> None:
        with self.assertRaises(AV1V4R3FreezeError):
            serialize_av1_v4r3_owner_freeze({"invalid": object()})
        with self.assertRaises(AV1V4R3FreezeError):
            serialize_av1_v4r3_owner_freeze({"invalid": float("nan")})

        data = canonical_json_bytes(_r3_freeze()) + b"\n"
        with (
            patch.object(
                freeze_module,
                "assert_av1_v4r3_owner_freeze",
                side_effect=TypeError("unexpected"),
            ),
            self.assertRaises(AV1V4R3FreezeError),
        ):
            deserialize_av1_v4r3_owner_freeze(data)

    def test_freeze_rejects_rebound_invocation_smuggling(self) -> None:
        freeze = _r3_freeze()
        bogus_closures = dict(freeze)
        bogus_closures["closure_ids"] = [f"bogus-{index}" for index in range(8)]
        bogus_closures["invocation_digests"] = [
            {
                **invocation,
                "closure_id": bogus_closures["closure_ids"][index],
            }
            for index, invocation in enumerate(freeze["invocation_digests"])
        ]
        with self.assertRaises(AV1V4R3FreezeError):
            serialize_av1_v4r3_owner_freeze(_rebind_r3_freeze(bogus_closures))

        smuggled = dict(freeze)
        smuggled["invocation_digests"] = [
            dict(item) for item in freeze["invocation_digests"]
        ]
        smuggled["invocation_digests"][0]["smuggled"] = True
        with self.assertRaises(AV1V4R3FreezeError):
            serialize_av1_v4r3_owner_freeze(_rebind_r3_freeze(smuggled))

        duplicate = dict(freeze)
        duplicate["invocation_digests"] = [
            dict(item) for item in freeze["invocation_digests"]
        ]
        duplicate["invocation_digests"][1]["invocation_sha256"] = duplicate[
            "invocation_digests"
        ][0]["invocation_sha256"]
        with self.assertRaises(AV1V4R3FreezeError):
            serialize_av1_v4r3_owner_freeze(_rebind_r3_freeze(duplicate))

    def test_freeze_contract_has_no_io_or_execution_imports(self) -> None:
        tree = ast.parse(Path(freeze_module.__file__).read_text())
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertTrue(
            {
                "fcntl",
                "os",
                "pathlib",
                "secrets",
                "subprocess",
                "mediaforce.execution",
                "mediaforce.core.db",
                "mediaforce.web",
            }.isdisjoint(imports)
        )

    def test_freeze_scope_constant_is_immutable(self) -> None:
        with self.assertRaises(TypeError):
            freeze_module.AV1_V4R3_FREEZE_SCOPE["retry_authorized"] = True


class AV1V4R3FreezeOperationTests(unittest.TestCase):
    def test_clock_and_cohort_value_errors_use_operation_boundary(self) -> None:
        def failing_clock() -> datetime:
            raise RuntimeError("clock")

        with TemporaryDirectory() as raw:
            binding = _prepared_registry(Path(raw))
            with (
                patch.object(
                    freeze_operation_module,
                    "deserialize_av1_v4r3_preparation_measurement",
                    side_effect=ValueError("invalid"),
                ),
                self.assertRaises(AV1V4R3FreezeOperationError),
            ):
                materialize_av1_v4r3_owner_freeze(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 20),
                )

            with (
                patch.object(
                    freeze_operation_module,
                    "_measure_clean_repository",
                    return_value=("3" * 40, "4" * 40),
                ),
                self.assertRaises(AV1V4R3FreezeOperationError),
            ):
                materialize_av1_v4r3_owner_freeze(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=failing_clock,
                )

    def test_serialization_failure_uses_operation_boundary(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _prepared_registry(Path(raw))
            with (
                patch.object(
                    freeze_operation_module,
                    "_measure_clean_repository",
                    return_value=("3" * 40, "4" * 40),
                ),
                patch.object(
                    freeze_operation_module,
                    "serialize_av1_v4r3_owner_freeze",
                    side_effect=AV1V4R3FreezeError("invalid"),
                ),
                self.assertRaises(AV1V4R3FreezeOperationError),
            ):
                materialize_av1_v4r3_owner_freeze(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 20),
                )

    def test_registry_and_freeze_custody_errors_use_operation_boundary(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            binding = _prepared_registry(root)
            with patch.object(
                freeze_operation_module,
                "_measure_clean_repository",
                return_value=("3" * 40, "4" * 40),
            ):
                result = materialize_av1_v4r3_owner_freeze(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 20),
                )
            result.path.chmod(0o644)
            with self.assertRaises(AV1V4R3FreezeOperationError):
                load_av1_v4r3_owner_freeze(binding)
            with (
                patch.object(
                    freeze_operation_module,
                    "_measure_clean_repository",
                    return_value=("3" * 40, "4" * 40),
                ),
                self.assertRaises(AV1V4R3FreezeOperationError),
            ):
                materialize_av1_v4r3_owner_freeze(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 21),
                )
            result.path.chmod(0o600)
            binding.registry.chmod(0o755)
            with self.assertRaises(AV1V4R3FreezeOperationError):
                load_av1_v4r3_owner_freeze(binding)
            binding.registry.chmod(0o700)

    def test_unprepared_registry_uses_freeze_operation_error_boundary(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _binding(Path(raw))
            with self.assertRaisesRegex(
                AV1V4R3FreezeOperationError, "preparation cohort is unavailable"
            ):
                materialize_av1_v4r3_owner_freeze(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 20),
                )

    def test_repository_measurement_uses_freeze_operation_error_boundary(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _prepared_registry(Path(raw))
            with (
                patch.object(
                    freeze_operation_module,
                    "_measure_clean_repository",
                    side_effect=AV1V4R3PreparationCustodyRegistryError("dirty"),
                ),
                self.assertRaisesRegex(
                    AV1V4R3FreezeOperationError,
                    "materializer repository is invalid",
                ),
            ):
                materialize_av1_v4r3_owner_freeze(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 20),
                )

    def test_materialization_is_atomic_idempotent_and_owner_only(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            binding = _prepared_registry(root)
            with patch.object(
                freeze_operation_module,
                "_measure_clean_repository",
                return_value=("3" * 40, "4" * 40),
            ):
                first = materialize_av1_v4r3_owner_freeze(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 20),
                )
                second = materialize_av1_v4r3_owner_freeze(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 30),
                )
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(first.freeze, second.freeze)
            metadata = first.path.stat()
            self.assertEqual(metadata.st_mode & 0o777, 0o600)
            self.assertEqual(metadata.st_nlink, 1)
            self.assertEqual(load_av1_v4r3_owner_freeze(binding), first.freeze)
            self.assertTrue((binding.registry / "preparation-claim.json").exists())
            self.assertTrue((binding.registry / "path-privacy.key").exists())

    def test_conflicting_owner_or_repository_fails_closed(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            binding = _prepared_registry(root)
            with patch.object(
                freeze_operation_module,
                "_measure_clean_repository",
                return_value=("3" * 40, "4" * 40),
            ):
                materialize_av1_v4r3_owner_freeze(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 20),
                )
                with self.assertRaises(AV1V4R3FreezeOperationError):
                    materialize_av1_v4r3_owner_freeze(
                        binding=binding,
                        rights_attestation=_rights(),
                        owner_principal="owner:other",
                        clock=_clock(3, 21),
                    )
            with (
                patch.object(
                    freeze_operation_module,
                    "_measure_clean_repository",
                    return_value=("5" * 40, "6" * 40),
                ),
                self.assertRaisesRegex(AV1V4R3FreezeOperationError, "conflicts"),
            ):
                materialize_av1_v4r3_owner_freeze(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 22),
                )

    def test_terminal_failure_preparation_cannot_be_frozen(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            binding = _binding(root)
            _publish(binding)
            consume_av1_v4r3_preparation_grant(
                binding=binding,
                rights_attestation=_rights(),
                clock=_clock(3, 10),
            )
            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=("1" * 40, "2" * 40),
                ),
                patch.object(
                    operation_module,
                    "_probe_tool_versions",
                    side_effect=TimeoutError("probe failed"),
                ),
            ):
                with self.assertRaises(AV1V4R3PreparationCustodyRegistryError):
                    run_av1_v4r3_preparation_bundle(
                        binding=binding,
                        repository_commit="1" * 40,
                        repository_tree="2" * 40,
                        source_paths=_r3_source_paths(),
                        dedicated_instance_paths=_r3_instance_paths(),
                        quality_temp_paths=_r3_quality_temp_paths(),
                        tool_paths=_stub_tools(root),
                        clock=_clock(3, 11),
                    )
            with (
                patch.object(
                    freeze_operation_module,
                    "_measure_clean_repository",
                    return_value=("3" * 40, "4" * 40),
                ),
                self.assertRaises(AV1V4R3FreezeOperationError),
            ):
                materialize_av1_v4r3_owner_freeze(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 20),
                )
            self.assertFalse((binding.registry / "owner-freeze.json").exists())

    def test_racing_materializers_create_one_freeze(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            binding = _prepared_registry(root)
            outcomes: list[str] = []

            def invoke() -> None:
                result = materialize_av1_v4r3_owner_freeze(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 20),
                )
                outcomes.append("created" if result.created else "existing")

            with patch.object(
                freeze_operation_module,
                "_measure_clean_repository",
                return_value=("3" * 40, "4" * 40),
            ):
                threads = [threading.Thread(target=invoke) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertCountEqual(outcomes, ["created", "existing"])


def _r3_source_paths() -> dict[str, str]:
    return {
        asset_id: f"/private/media/{index}.mkv"
        for index, asset_id in enumerate(AV1_VALIDATION_V4_SOURCE_IDS, start=1)
    }


def _r3_instance_paths() -> dict[str, str]:
    return {
        "runtime_lock": "/private/runtime/lock",
        "source_root": "/private/media",
        "state_root": "/private/state",
        "temp_root": "/private/temp",
    }


def _r3_quality_temp_paths() -> dict[str, str]:
    return {
        asset_id: f"/private/temp/quality/{index}"
        for index, asset_id in enumerate(AV1_VALIDATION_V4_SOURCE_IDS, start=1)
    }


def _r3_config() -> dict[str, object]:
    return build_av1_v4r3_effective_config_snapshot(
        repository_commit="1" * 40,
        repository_tree="2" * 40,
        source_paths=_r3_source_paths(),
        dedicated_instance_paths=_r3_instance_paths(),
        quality_temp_paths=_r3_quality_temp_paths(),
    )


def _r3_probes() -> list[dict[str, object]]:
    return [
        {
            "tool": name,
            "argv": ["--version"] if name == "ab_av1" else ["-version"],
            "version": f"{name} test 1.0",
            "binary_sha256": f"sha256:{index:064x}",
        }
        for index, name in enumerate(("ab_av1", "ffmpeg", "ffprobe"), start=1)
    ]


def _r3_bundle(config: Mapping[str, Any]) -> dict[str, object]:
    probes = _r3_probes()
    return build_av1_v4r3_preparation_bundle(
        preparation_grant=_grant(),
        preparation_claim=_claim(),
        key_custody=_custody(),
        effective_config=config,
        path_privacy_key=b"k" * 32,
        toolchain={
            str(probe["tool"]): {
                "binary_sha256": str(probe["binary_sha256"]),
                "version": str(probe["version"]),
            }
            for probe in probes
        },
        runtime={
            "python_implementation": "CPython",
            "python_version": "3.13.7",
            "platform_system": "Darwin",
            "platform_machine": "arm64",
        },
    )


def _r3_success_measurement() -> dict[str, object]:
    config = _r3_config()
    bundle = _r3_bundle(config)
    return build_av1_v4r3_preparation_success_measurement(
        preparation_grant=_grant(),
        preparation_claim=_claim(),
        key_custody=_custody(),
        effective_config=config,
        preparation_bundle=bundle,
        path_privacy_key=b"k" * 32,
        started_at="2026-08-08T03:10:02Z",
        completed_at="2026-08-08T03:10:03Z",
        probes=_r3_probes(),
    )


def _build_r3_freeze(
    *,
    measurement: Mapping[str, Any] | None = None,
    owner_principal: str = "owner:test",
    decided_at: str = "2026-08-08T03:20:00Z",
) -> dict[str, Any]:
    return build_av1_v4r3_owner_freeze(
        rights_attestation=_rights(),
        preparation_grant=_grant(),
        preparation_claim=_claim(),
        key_custody=_custody(),
        preparation_bundle=_r3_bundle(_r3_config()),
        preparation_measurement=measurement or _r3_success_measurement(),
        owner_principal=owner_principal,
        decided_at=decided_at,
        materializer_repository_commit="3" * 40,
        materializer_repository_tree="4" * 40,
    )


def _r3_freeze() -> dict[str, Any]:
    return _build_r3_freeze()


def _rebind_r3_freeze(payload: Mapping[str, Any]) -> dict[str, Any]:
    rebound = dict(payload)
    rebound["freeze_id"] = freeze_module._freeze_id(rebound)
    without_sha = {
        key: value for key, value in rebound.items() if key != "payload_sha256"
    }
    rebound["payload_sha256"] = "sha256:" + stable_json_hash(without_sha)
    return rebound


def _prepared_registry(root: Path) -> AV1V4R3PreparationCustodyRegistryBinding:
    binding = _binding(root)
    _publish(binding)
    consume_av1_v4r3_preparation_grant(
        binding=binding,
        rights_attestation=_rights(),
        clock=_clock(3, 10),
    )
    with patch.object(
        operation_module,
        "_measure_clean_repository",
        return_value=("1" * 40, "2" * 40),
    ):
        run_av1_v4r3_preparation_bundle(
            binding=binding,
            repository_commit="1" * 40,
            repository_tree="2" * 40,
            source_paths=_r3_source_paths(),
            dedicated_instance_paths=_r3_instance_paths(),
            quality_temp_paths=_r3_quality_temp_paths(),
            tool_paths=_stub_tools(root),
            clock=_clock(3, 11),
        )
    return binding


def _stub_tools(root: Path) -> dict[str, Path]:
    tools: dict[str, Path] = {}
    for name in ("ab_av1", "ffmpeg", "ffprobe"):
        path = root / name
        path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{name} test 1.0'\n")
        path.chmod(0o700)
        tools[name] = path
    return tools


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "LANG": "C",
            "LC_ALL": "C",
        },
    )
    return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
