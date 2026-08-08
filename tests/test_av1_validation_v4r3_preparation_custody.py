from __future__ import annotations

import ast
import os
import threading
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from multiprocessing import get_all_start_methods, get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from mediaforce.tuning import av1_validation_v4r3_preparation_custody as pure_module
from mediaforce.tuning import (
    av1_validation_v4r3_preparation_custody_registry as registry_module,
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
from mediaforce.tuning.av1_validation_v4r3_preparation_grant import (
    build_av1_v4r3_preparation_grant,
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


if __name__ == "__main__":
    unittest.main()
