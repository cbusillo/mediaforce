from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import UTC, datetime
from dataclasses import replace
import io
import json
from multiprocessing import get_all_start_methods, get_context
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mediaforce.tuning import av1_validation_v4r3_execution_custody as module
from mediaforce.tuning import (
    av1_validation_v4r3_execution_preflight_operation as preflight_operation_module,
)
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
)
from mediaforce.tuning.av1_validation_v4r3_execution_custody import (
    AV1V4R3ExecutionCustodyError,
    claim_av1_v4r3_execution_grant,
)
from mediaforce.tuning.av1_validation_v4r3_execution_grant import (
    deserialize_av1_v4r3_execution_grant,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window_registry import (
    AV1V4R3OrdinalWindowRegistryBinding,
    _RegistryContext,
    publish_av1_v4r3_ordinal_window_grant,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody_registry import (
    AV1V4R3PreparationCustodyRegistryBinding,
)
from scripts import claim_av1_v4r3_execution_grant as script_module
from tests.test_av1_validation_v4r3_execution_preflight_operation import (
    _bindings,
    _materialize,
)
from tests.test_av1_validation_v4r3_preparation_custody import (
    _clock,
    _rights,
)


def _process_claim(
    preparation_registry: str,
    repository_root: str,
    ordinal_registry: str,
    run_registry_id: str,
    results: Any,
) -> None:
    try:
        claim_av1_v4r3_execution_grant(
            preparation_binding=AV1V4R3PreparationCustodyRegistryBinding(
                registry=Path(preparation_registry),
                repository_root=Path(repository_root),
            ),
            ordinal_binding=AV1V4R3OrdinalWindowRegistryBinding(
                registry=Path(ordinal_registry),
                run_registry_id=run_registry_id,
            ),
            rights_attestation=_rights(),
            owner_principal="owner:test",
            confirmed_owner_principal="owner:test",
            ordinal=1,
            valid_until="2026-08-08T07:40:00Z",
            clock=_clock(4, 10),
        )
    except AV1V4R3ExecutionCustodyError:
        results.put("rejected")
    else:
        results.put("created")


class AV1V4R3ExecutionCustodyTests(unittest.TestCase):
    def test_publishes_canonical_grant_and_irreversible_claim(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))
            result = _claim(preparation, ordinal, repository)

            self.assertTrue(result.grant_created)
            self.assertEqual(result.grant["ordinal"], 1)
            self.assertEqual(
                result.claim["execution_grant_id"], result.grant["grant_id"]
            )
            grant_path = ordinal.registry / "ordinal_01.execution-grant.json"
            claim_path = ordinal.registry / f"{result.grant['grant_id']}.claim.json"
            for path in (grant_path, claim_path):
                metadata = path.stat()
                self.assertEqual(metadata.st_mode & 0o777, 0o600)
                self.assertEqual(metadata.st_uid, os.geteuid())
                self.assertEqual(metadata.st_nlink, 1)
            for artifact in (result.grant, result.claim):
                self.assertNotIn(str(ordinal.registry), repr(artifact))
            for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                self.assertIs(result.claim[field], False)
            with self.assertRaisesRegex(
                AV1V4R3ExecutionCustodyError, "already been consumed"
            ):
                _claim(preparation, ordinal, repository, minute=11)

    def test_grant_only_failure_is_retryable_and_conflicts_fail_closed(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))
            with (
                patch.object(
                    _RegistryContext,
                    "write_burn",
                    side_effect=OSError("injected pre-claim failure"),
                ),
                self.assertRaises(AV1V4R3ExecutionCustodyError),
            ):
                _claim(preparation, ordinal, repository)
            self.assertTrue(
                (ordinal.registry / "ordinal_01.execution-grant.json").exists()
            )
            self.assertFalse(any(ordinal.registry.glob("*.claim.json")))
            with self.assertRaisesRegex(AV1V4R3ExecutionCustodyError, "conflicts"):
                _claim(
                    preparation,
                    ordinal,
                    repository,
                    minute=11,
                    valid_until="2026-08-08T07:39:00Z",
                )
            recovered = _claim(preparation, ordinal, repository, minute=11)
            self.assertFalse(recovered.grant_created)

    def test_post_create_failure_keeps_partial_claim_and_burns_grant(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))

            def create_then_fail(
                context: _RegistryContext, filename: str, data: bytes
            ) -> None:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(filename, flags, 0o600, dir_fd=context.dir_fd)
                os.close(descriptor)
                os.fsync(context.dir_fd)
                raise OSError("injected post-create failure")

            with (
                patch.object(_RegistryContext, "write_burn", create_then_fail),
                self.assertRaisesRegex(
                    AV1V4R3ExecutionCustodyError, "after consuming the grant"
                ),
            ):
                _claim(preparation, ordinal, repository)
            claims = list(ordinal.registry.glob("*.claim.json"))
            self.assertEqual(len(claims), 1)
            self.assertEqual(claims[0].stat().st_size, 0)
            with self.assertRaisesRegex(
                AV1V4R3ExecutionCustodyError, "already been consumed"
            ):
                _claim(preparation, ordinal, repository, minute=11)

    def test_thread_race_allows_exactly_one_claimant(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))

            def attempt() -> str:
                try:
                    claim_av1_v4r3_execution_grant(
                        preparation_binding=preparation,
                        ordinal_binding=ordinal,
                        rights_attestation=_rights(),
                        owner_principal="owner:test",
                        confirmed_owner_principal="owner:test",
                        ordinal=1,
                        valid_until="2026-08-08T07:40:00Z",
                        clock=_clock(4, 10),
                    )
                except AV1V4R3ExecutionCustodyError:
                    return "rejected"
                return "created"

            with (
                patch.object(
                    preflight_operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                patch.object(
                    module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                outcomes = sorted(executor.map(lambda _: attempt(), range(2)))
            self.assertEqual(outcomes, ["created", "rejected"])

    @unittest.skipUnless("fork" in get_all_start_methods(), "fork is required")
    def test_process_race_allows_exactly_one_claimant(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))
            context = get_context("fork")
            results = context.Queue()
            processes = [
                context.Process(
                    target=_process_claim,
                    args=(
                        str(preparation.registry),
                        str(preparation.repository_root),
                        str(ordinal.registry),
                        ordinal.run_registry_id,
                        results,
                    ),
                )
                for _ in range(2)
            ]
            with (
                patch.object(
                    preflight_operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                patch.object(
                    module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
            ):
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(10)
                    self.assertEqual(process.exitcode, 0)
            outcomes = sorted(results.get(timeout=2) for _ in processes)
            self.assertEqual(outcomes, ["created", "rejected"])

    def test_rechecks_half_open_window_at_burn_time(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))
            clock = _sequence_clock(
                datetime(2026, 8, 8, 4, 10, tzinfo=UTC),
                datetime(2026, 8, 8, 7, 45, tzinfo=UTC),
            )
            with (
                patch.object(
                    preflight_operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                patch.object(
                    module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3ExecutionCustodyError) as captured,
            ):
                claim_av1_v4r3_execution_grant(
                    preparation_binding=preparation,
                    ordinal_binding=ordinal,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    confirmed_owner_principal="owner:test",
                    ordinal=1,
                    valid_until="2026-08-08T07:49:00Z",
                    clock=clock,
                )
            self.assertIn(
                "outside the grant interval", str(captured.exception.__cause__)
            )
            self.assertTrue(
                (ordinal.registry / "ordinal_01.execution-grant.json").exists()
            )
            self.assertFalse(any(ordinal.registry.glob("*.claim.json")))

        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))
            with (
                patch.object(
                    preflight_operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                patch.object(
                    module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3ExecutionCustodyError) as captured,
            ):
                claim_av1_v4r3_execution_grant(
                    preparation_binding=preparation,
                    ordinal_binding=ordinal,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    confirmed_owner_principal="owner:test",
                    ordinal=1,
                    valid_until="2026-08-08T07:49:00Z",
                    clock=_clock(7, 45),
                )
            self.assertIn(
                "outside the grant interval", str(captured.exception.__cause__)
            )
            self.assertFalse(
                (ordinal.registry / "ordinal_01.execution-grant.json").exists()
            )

    def test_rejects_collapsed_or_aliased_registries(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))
            collapsed = replace(ordinal, registry=preparation.registry)
            with self.assertRaisesRegex(
                AV1V4R3ExecutionCustodyError, "registries must be distinct"
            ):
                _claim(preparation, collapsed, repository)

            alias = Path(raw) / "ordinal-alias"
            alias.symlink_to(ordinal.registry, target_is_directory=True)
            aliased = replace(ordinal, registry=alias)
            with self.assertRaisesRegex(
                AV1V4R3ExecutionCustodyError, "registries must be distinct"
            ):
                claim_av1_v4r3_execution_grant(
                    preparation_binding=replace(preparation, registry=ordinal.registry),
                    ordinal_binding=aliased,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    confirmed_owner_principal="owner:test",
                    ordinal=1,
                    valid_until="2026-08-08T07:40:00Z",
                    clock=_clock(4, 10),
                )

    def test_preexisting_symlink_claim_target_burns_grant(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))
            with (
                patch.object(
                    _RegistryContext,
                    "write_burn",
                    side_effect=OSError("injected pre-claim failure"),
                ),
                self.assertRaises(AV1V4R3ExecutionCustodyError),
            ):
                _claim(preparation, ordinal, repository)
            grant = deserialize_av1_v4r3_execution_grant(
                (ordinal.registry / "ordinal_01.execution-grant.json").read_bytes()
            )
            target = ordinal.registry / f"{grant['grant_id']}.claim.json"
            target.symlink_to(ordinal.registry / "missing")
            with self.assertRaisesRegex(
                AV1V4R3ExecutionCustodyError, "already been consumed"
            ):
                _claim(preparation, ordinal, repository, minute=11)

    def test_rejects_owner_repository_and_private_output_substitution(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))
            with self.assertRaisesRegex(
                AV1V4R3ExecutionCustodyError, "owner confirmation"
            ):
                _claim(
                    preparation,
                    ordinal,
                    repository,
                    confirmed_owner="owner:other",
                )
            with (
                patch.object(
                    module,
                    "_measure_clean_repository",
                    return_value=("f" * 40, "e" * 40),
                ),
                patch.object(
                    preflight_operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaisesRegex(
                    AV1V4R3ExecutionCustodyError, "repository binding"
                ),
            ):
                claim_av1_v4r3_execution_grant(
                    preparation_binding=preparation,
                    ordinal_binding=ordinal,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    confirmed_owner_principal="owner:test",
                    ordinal=1,
                    valid_until="2026-08-08T07:40:00Z",
                    clock=_clock(4, 10),
                )

    def test_cli_emits_public_json_only(self) -> None:
        grant = {
            "grant_id": "av1v4r3execgrant_" + "a" * 32,
            "payload_sha256": "sha256:" + "b" * 64,
            "ordinal": 1,
            "asset_id": "av1v4_animation_primary_sintel",
            "media_read_authorized": True,
            "qualification_execution_authorized": True,
            "runtime_execution_authorized": True,
            "dogfood_authorized": False,
        }
        claim = {
            "claim_id": "av1v4r3execclaim_" + "c" * 32,
            "payload_sha256": "sha256:" + "d" * 64,
        }
        with TemporaryDirectory() as raw:
            root = Path(raw)
            rights = root / "rights.json"
            rights.write_text("{}")
            argv = [
                "--repository-root",
                str(root / "repository"),
                "--preparation-registry",
                str(root / "preparation"),
                "--ordinal-registry",
                str(root / "ordinal"),
                "--run-registry-id",
                "av1v4r3runreg_" + "e" * 64,
                "--rights-attestation",
                str(rights),
                "--ordinal",
                "1",
                "--valid-until",
                "2026-08-08T07:40:00Z",
                "--owner-principal",
                "owner:test",
                "--confirm-owner-principal",
                "owner:test",
            ]
            output = io.StringIO()
            with (
                patch.object(
                    script_module,
                    "claim_av1_v4r3_execution_grant",
                    return_value=SimpleNamespace(
                        grant=grant,
                        claim=claim,
                        grant_created=True,
                    ),
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(script_module.main(argv), 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["ok"])
            self.assertNotIn(str(root), output.getvalue())

            output = io.StringIO()
            with (
                patch.object(
                    script_module,
                    "claim_av1_v4r3_execution_grant",
                    side_effect=AV1V4R3ExecutionCustodyError(
                        f"private failure at {root}"
                    ),
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(script_module.main(argv), 1)
            self.assertEqual(
                json.loads(output.getvalue())["error"],
                "AV1 v4 r3 execution custody failed",
            )
            self.assertNotIn(str(root), output.getvalue())

    def test_module_has_no_media_execution_imports(self) -> None:
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
        self.assertTrue(
            {
                "mediaforce.execution",
                "mediaforce.core.db",
                "mediaforce.tuning.av1_validation_v4_qualification_runner",
                "subprocess",
                "requests",
            }.isdisjoint(imports)
        )


def _prepared(root: Path) -> tuple[Any, Any, tuple[str, str]]:
    preparation, ordinal, repository = _bindings(root)
    with patch.object(
        preflight_operation_module,
        "_measure_clean_repository",
        return_value=repository,
    ):
        pair = _materialize(preparation, ordinal)
    publish_av1_v4r3_ordinal_window_grant(
        binding=ordinal,
        plan=pair.plan,
        ordinal=1,
        clock=_clock(4, 5),
        valid_until="2026-08-08T07:50:00Z",
    )
    return preparation, ordinal, repository


def _claim(
    preparation: Any,
    ordinal: Any,
    repository: tuple[str, str],
    *,
    minute: int = 10,
    valid_until: str = "2026-08-08T07:40:00Z",
    confirmed_owner: str = "owner:test",
) -> Any:
    with (
        patch.object(
            preflight_operation_module,
            "_measure_clean_repository",
            return_value=repository,
        ),
        patch.object(
            module,
            "_measure_clean_repository",
            return_value=repository,
        ),
    ):
        return claim_av1_v4r3_execution_grant(
            preparation_binding=preparation,
            ordinal_binding=ordinal,
            rights_attestation=_rights(),
            owner_principal="owner:test",
            confirmed_owner_principal=confirmed_owner,
            ordinal=1,
            valid_until=valid_until,
            clock=_clock(4, minute),
        )


def _sequence_clock(*values: datetime) -> Any:
    iterator = iter(values)
    return lambda: next(iterator)


if __name__ == "__main__":
    unittest.main()
