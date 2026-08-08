from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mediaforce.core.evidence import stable_json_hash
from mediaforce.tuning import (
    av1_validation_v4r3_qualification_request as request_module,
)
from mediaforce.tuning import (
    av1_validation_v4r3_qualification_request_operation as operation_module,
)
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
)
from mediaforce.tuning.av1_validation_v4r3_freeze_operation import (
    load_av1_v4r3_owner_freeze,
    materialize_av1_v4r3_owner_freeze,
)
from mediaforce.tuning.av1_validation_v4r3_freeze import (
    serialize_av1_v4r3_owner_freeze,
)
from mediaforce.tuning.av1_validation_v4r3_manifest import (
    AV1_V4R3_MANIFEST_ID,
    AV1_V4R3_MANIFEST_VALID_UNTIL,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX,
    build_av1_v4r3_ordinal_window_plan,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody_registry import (
    AV1V4R3PreparationCustodyRegistryBinding,
)
from mediaforce.tuning.av1_validation_v4r3_qualification_request import (
    AV1V4R3QualificationRequestError,
    AV1_V4R3_QUALIFICATION_REQUEST_APPROVAL_FIELD,
    AV1_V4R3_QUALIFICATION_REQUEST_SCOPE,
    AV1_V4R3_QUALIFICATION_REQUEST_TTL_SECONDS,
    assert_av1_v4r3_qualification_request,
    av1_v4r3_qualification_request_valid_until,
    build_av1_v4r3_qualification_request,
    deserialize_av1_v4r3_qualification_request,
    serialize_av1_v4r3_qualification_request,
)
from mediaforce.tuning.av1_validation_v4r3_qualification_request_operation import (
    AV1V4R3QualificationRequestOperationError,
    load_av1_v4r3_qualification_request,
    materialize_av1_v4r3_qualification_request,
)
from tests.test_av1_validation_v4r3_preparation_custody import (
    _clock,
    _prepared_registry,
    _r3_freeze,
    _rebind_r3_freeze,
    _rights,
)


class AV1V4R3QualificationRequestContractTests(unittest.TestCase):
    def test_round_trip_is_path_free_and_non_authorizing(self) -> None:
        request = _request()
        data = serialize_av1_v4r3_qualification_request(request)

        self.assertEqual(deserialize_av1_v4r3_qualification_request(data), request)
        self.assertRegex(request["request_id"], r"av1v4r3req_[0-9a-f]{32}")
        self.assertTrue(request[AV1_V4R3_QUALIFICATION_REQUEST_APPROVAL_FIELD])
        self.assertFalse(request["selection_or_partition_use_allowed"])
        self.assertNotIn(b"/private/", data)
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            self.assertIs(request[field], False)

    def test_request_is_accepted_by_ordinal_window_plan(self) -> None:
        request = _request()
        plan = build_av1_v4r3_ordinal_window_plan(
            r3_manifest_id=AV1_V4R3_MANIFEST_ID,
            r3_manifest_valid_until=AV1_V4R3_MANIFEST_VALID_UNTIL,
            r3_request_id=str(request["request_id"]),
            r3_request_valid_until=str(request["valid_until"]),
            r3_preflight_id="av1v4r3preflight_" + "a" * 32,
            r3_repository=request["reviewed_repository"],
            r3_toolchain_id=str(request["toolchain_id"]),
            r3_closure_ids=list(request["closure_ids"]),
            r3_invocation_digests=list(request["invocation_digests"]),
            plan_opens_at="2026-08-08T04:00:00Z",
            plan_closes_at="2026-08-09T16:00:00Z",
            run_registry_id="av1v4r3runreg_" + "b" * 64,
        )

        self.assertEqual(plan["r3_request_id"], request["request_id"])

    def test_rejects_rebound_state_scope_and_scalar_tampering(self) -> None:
        request = _request()
        mutations = (
            ("state", "authorized"),
            ("requested_ordinal_count", 8.0),
            ("request_ttl_seconds", float(request["request_ttl_seconds"])),
            (AV1_V4R3_QUALIFICATION_REQUEST_APPROVAL_FIELD, 1),
            ("selection_or_partition_use_allowed", 0),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                mutated = dict(request)
                mutated[field] = value
                with self.assertRaises(AV1V4R3QualificationRequestError):
                    assert_av1_v4r3_qualification_request(_rebind_request(mutated))

        mutated = dict(request)
        mutated["request_scope"] = dict(request["request_scope"])
        mutated["request_scope"]["retry_authorized"] = 0
        with self.assertRaises(AV1V4R3QualificationRequestError):
            assert_av1_v4r3_qualification_request(_rebind_request(mutated))

        mutated = dict(request)
        mutated["request_scope"] = dict(request["request_scope"])
        mutated["request_scope"]["smuggled"] = False
        with self.assertRaises(AV1V4R3QualificationRequestError):
            assert_av1_v4r3_qualification_request(_rebind_request(mutated))

        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            with self.subTest(authority=field):
                mutated = dict(request)
                mutated[field] = True
                with self.assertRaises(AV1V4R3QualificationRequestError):
                    assert_av1_v4r3_qualification_request(_rebind_request(mutated))

    def test_rejects_rebound_invocation_smuggling(self) -> None:
        request = _request()

        reordered = dict(request)
        reordered["closure_ids"] = list(reversed(request["closure_ids"]))
        reordered["invocation_digests"] = [
            dict(item) for item in reversed(request["invocation_digests"])
        ]
        with self.assertRaises(AV1V4R3QualificationRequestError):
            assert_av1_v4r3_qualification_request(_rebind_request(reordered))

        smuggled = dict(request)
        smuggled["invocation_digests"] = [
            dict(item) for item in request["invocation_digests"]
        ]
        smuggled["invocation_digests"][0]["smuggled"] = True
        with self.assertRaises(AV1V4R3QualificationRequestError):
            assert_av1_v4r3_qualification_request(_rebind_request(smuggled))

        duplicate = dict(request)
        duplicate["invocation_digests"] = [
            dict(item) for item in request["invocation_digests"]
        ]
        duplicate["invocation_digests"][1]["invocation_sha256"] = duplicate[
            "invocation_digests"
        ][0]["invocation_sha256"]
        with self.assertRaises(AV1V4R3QualificationRequestError):
            assert_av1_v4r3_qualification_request(_rebind_request(duplicate))

    def test_enforces_submission_and_request_window_boundaries(self) -> None:
        request = _request()
        freeze_decided = datetime.fromisoformat(
            str(request["freeze_decided_at"]).replace("Z", "+00:00")
        )

        at_submission_limit = dict(request)
        at_submission_limit["requested_at"] = (
            freeze_decided + timedelta(seconds=86_400)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        at_submission_limit["valid_until"] = av1_v4r3_qualification_request_valid_until(
            str(at_submission_limit["requested_at"])
        )
        at_submission_limit["request_ttl_seconds"] = (
            AV1_V4R3_QUALIFICATION_REQUEST_TTL_SECONDS
        )
        assert_av1_v4r3_qualification_request(_rebind_request(at_submission_limit))

        past_submission_limit = dict(at_submission_limit)
        past_submission_limit["requested_at"] = (
            freeze_decided + timedelta(seconds=86_401)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        past_submission_limit["valid_until"] = (
            av1_v4r3_qualification_request_valid_until(
                str(past_submission_limit["requested_at"])
            )
        )
        with self.assertRaises(AV1V4R3QualificationRequestError):
            assert_av1_v4r3_qualification_request(
                _rebind_request(past_submission_limit)
            )

        manifest_until = datetime.fromisoformat(
            AV1_V4R3_MANIFEST_VALID_UNTIL.replace("Z", "+00:00")
        )
        shortest_valid_request = dict(request)
        shortest_valid_request["freeze_decided_at"] = (
            manifest_until - timedelta(seconds=AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX + 61)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        shortest_valid_request["requested_at"] = (
            manifest_until - timedelta(seconds=AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX + 1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        shortest_valid_request["valid_until"] = AV1_V4R3_MANIFEST_VALID_UNTIL
        shortest_valid_request["request_ttl_seconds"] = (
            AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX + 1
        )
        assert_av1_v4r3_qualification_request(_rebind_request(shortest_valid_request))

        too_short = dict(shortest_valid_request)
        too_short["requested_at"] = (
            manifest_until - timedelta(seconds=AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        too_short["request_ttl_seconds"] = AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX
        with self.assertRaises(AV1V4R3QualificationRequestError):
            assert_av1_v4r3_qualification_request(_rebind_request(too_short))

    def test_rejects_invalid_freeze_owner_and_private_text(self) -> None:
        with self.assertRaises(AV1V4R3QualificationRequestError):
            _build_request(owner_principal="owner:other")

        freeze = _r3_freeze()
        freeze["state"] = "terminal_failure"
        with self.assertRaises(AV1V4R3QualificationRequestError):
            _build_request(owner_freeze=_rebind_r3_freeze(freeze))

        request = _request()
        request["owner_principal"] = "/private/owner"
        with self.assertRaises(AV1V4R3QualificationRequestError):
            assert_av1_v4r3_qualification_request(_rebind_request(request))

    def test_rejects_noncanonical_and_nonfinite_bytes(self) -> None:
        data = serialize_av1_v4r3_qualification_request(_request())
        with self.assertRaises(AV1V4R3QualificationRequestError):
            deserialize_av1_v4r3_qualification_request(data.rstrip(b"\n"))
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                with self.assertRaises(AV1V4R3QualificationRequestError):
                    deserialize_av1_v4r3_qualification_request(
                        b'{"payload_sha256":' + constant + b"}\n"
                    )

    def test_scope_constant_is_immutable_and_contract_is_pure(self) -> None:
        with self.assertRaises(TypeError):
            AV1_V4R3_QUALIFICATION_REQUEST_SCOPE["retry_authorized"] = True

        tree = ast.parse(Path(request_module.__file__).read_text())
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


class AV1V4R3QualificationRequestOperationTests(unittest.TestCase):
    def test_materialization_is_atomic_idempotent_and_custodied(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _registry_with_freeze(Path(raw))
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=("5" * 40, "6" * 40),
            ):
                created = materialize_av1_v4r3_qualification_request(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 30),
                )
                existing = materialize_av1_v4r3_qualification_request(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 31),
                )

            self.assertTrue(created.created)
            self.assertFalse(existing.created)
            self.assertEqual(created.request, existing.request)
            self.assertEqual(
                load_av1_v4r3_qualification_request(binding), created.request
            )
            stat = created.path.stat()
            self.assertEqual(stat.st_mode & 0o777, 0o600)
            self.assertEqual(stat.st_nlink, 1)
            self.assertEqual(stat.st_uid, os.geteuid())

    def test_missing_or_mutated_freeze_chain_fails_closed(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _prepared_registry(Path(raw))
            with self.assertRaises(AV1V4R3QualificationRequestOperationError):
                materialize_av1_v4r3_qualification_request(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 30),
                )

        with TemporaryDirectory() as raw:
            binding = _registry_with_freeze(Path(raw))
            measurement = binding.registry / "preparation-terminal-measurement.json"
            measurement.write_bytes(b"{}\n")
            measurement.chmod(0o600)
            with self.assertRaises(AV1V4R3QualificationRequestOperationError):
                materialize_av1_v4r3_qualification_request(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 30),
                )
            self.assertFalse((binding.registry / "qualification-request.json").exists())

        with TemporaryDirectory() as raw:
            binding = _registry_with_freeze(Path(raw))
            freeze = load_av1_v4r3_owner_freeze(binding)
            self.assertIsNotNone(freeze)
            assert freeze is not None
            freeze["effective_config_hmac_id"] = "av1v4r3confighmac_" + "f" * 32
            freeze_path = binding.registry / "owner-freeze.json"
            freeze_path.write_bytes(
                serialize_av1_v4r3_owner_freeze(_rebind_r3_freeze(freeze))
            )
            freeze_path.chmod(0o600)
            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=("5" * 40, "6" * 40),
                ),
                self.assertRaises(AV1V4R3QualificationRequestOperationError),
            ):
                materialize_av1_v4r3_qualification_request(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 30),
                )
            self.assertFalse((binding.registry / "qualification-request.json").exists())

    def test_conflicts_and_expiry_preserve_singleton(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _registry_with_freeze(Path(raw))
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=("5" * 40, "6" * 40),
            ):
                created = materialize_av1_v4r3_qualification_request(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 30),
                )
            original = created.path.read_bytes()

            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=("7" * 40, "8" * 40),
                ),
                self.assertRaises(AV1V4R3QualificationRequestOperationError),
            ):
                materialize_av1_v4r3_qualification_request(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 31),
                )
            self.assertEqual(created.path.read_bytes(), original)

            expires = datetime.fromisoformat(
                str(created.request["valid_until"]).replace("Z", "+00:00")
            )
            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=("5" * 40, "6" * 40),
                ),
                self.assertRaises(AV1V4R3QualificationRequestOperationError),
            ):
                materialize_av1_v4r3_qualification_request(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=lambda: expires,
                )
            self.assertEqual(created.path.read_bytes(), original)

    def test_stale_temporary_is_reaped_and_errors_use_operation_boundary(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _registry_with_freeze(Path(raw))
            stale = binding.registry / (
                f".qualification-request.json.{os.getpid()}.0123456789abcdef.tmp"
            )
            stale.write_bytes(b"stale")
            stale.chmod(0o600)
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=("5" * 40, "6" * 40),
            ):
                result = materialize_av1_v4r3_qualification_request(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 30),
                )
            self.assertFalse(stale.exists())

            result.path.chmod(0o644)
            try:
                with self.assertRaises(AV1V4R3QualificationRequestOperationError):
                    load_av1_v4r3_qualification_request(binding)
            finally:
                result.path.chmod(0o600)

    def test_filesystem_errors_use_operation_boundary(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _registry_with_freeze(Path(raw))
            with (
                patch.object(
                    operation_module,
                    "_locked_registry",
                    side_effect=OSError("/private/registry"),
                ),
                self.assertRaises(AV1V4R3QualificationRequestOperationError),
            ):
                materialize_av1_v4r3_qualification_request(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 30),
                )
            with (
                patch.object(
                    operation_module,
                    "_locked_registry",
                    side_effect=OSError("/private/registry"),
                ),
                self.assertRaises(AV1V4R3QualificationRequestOperationError),
            ):
                load_av1_v4r3_qualification_request(binding)

    def test_concurrent_materializers_create_one_request(self) -> None:
        with TemporaryDirectory() as raw:
            binding = _registry_with_freeze(Path(raw))

            def materialize() -> bool:
                return materialize_av1_v4r3_qualification_request(
                    binding=binding,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    clock=_clock(3, 30),
                ).created

            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=("5" * 40, "6" * 40),
                ),
                ThreadPoolExecutor(max_workers=4) as executor,
            ):
                created = list(executor.map(lambda _: materialize(), range(4)))

            self.assertEqual(created.count(True), 1)
            self.assertEqual(created.count(False), 3)

    def test_operation_does_not_import_media_or_database_layers(self) -> None:
        tree = ast.parse(Path(operation_module.__file__).read_text())
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
                "mediaforce.execution",
                "mediaforce.core.db",
                "mediaforce.web",
                "requests",
            }.isdisjoint(imports)
        )


def _build_request(
    *,
    owner_freeze: dict[str, object] | None = None,
    owner_principal: str = "owner:test",
) -> dict[str, object]:
    requested_at = "2026-08-08T03:30:00Z"
    return build_av1_v4r3_qualification_request(
        owner_freeze=owner_freeze or _r3_freeze(),
        owner_principal=owner_principal,
        requested_at=requested_at,
        valid_until=av1_v4r3_qualification_request_valid_until(requested_at),
        requesting_repository_commit="5" * 40,
        requesting_repository_tree="6" * 40,
    )


def _request() -> dict[str, object]:
    return _build_request()


def _rebind_request(payload: dict[str, object]) -> dict[str, object]:
    rebound = dict(payload)
    rebound["request_id"] = request_module.av1_v4r3_qualification_request_id(rebound)
    without_sha = {
        key: value for key, value in rebound.items() if key != "payload_sha256"
    }
    rebound["payload_sha256"] = "sha256:" + stable_json_hash(without_sha)
    return rebound


def _registry_with_freeze(
    root: Path,
) -> AV1V4R3PreparationCustodyRegistryBinding:
    binding = _prepared_registry(root)
    with patch(
        "mediaforce.tuning.av1_validation_v4r3_freeze_operation."
        "_measure_clean_repository",
        return_value=("3" * 40, "4" * 40),
    ):
        materialize_av1_v4r3_owner_freeze(
            binding=binding,
            rights_attestation=_rights(),
            owner_principal="owner:test",
            clock=_clock(3, 20),
        )
    return binding


if __name__ == "__main__":
    unittest.main()
