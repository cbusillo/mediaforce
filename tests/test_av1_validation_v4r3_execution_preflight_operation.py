from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextlib import redirect_stdout
from datetime import UTC, datetime
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

from mediaforce.tuning import (
    av1_validation_v4r3_execution_preflight_operation as operation_module,
)
from mediaforce.tuning.av1_validation_v4r3_execution_preflight_operation import (
    AV1V4R3ExecutionPreflightOperationError,
    AV1V4R3ExecutionPreflightOperationResult,
    materialize_av1_v4r3_execution_preflight,
)
from mediaforce.tuning.av1_validation_v4r3_freeze_operation import (
    load_av1_v4r3_owner_freeze,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1_V4R3_OW_AGGREGATE_SECONDS_MAX,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window_registry import (
    AV1V4R3OrdinalWindowRegistryBinding,
    AV1V4R3OrdinalWindowRegistryError,
    _RegistryContext,
    av1_v4r3_ordinal_window_registry_hmac_id,
    derive_av1_v4r3_ordinal_window_high_water,
    publish_av1_v4r3_ordinal_window_claim,
    publish_av1_v4r3_ordinal_window_grant,
    publish_av1_v4r3_ordinal_window_outcome,
    publish_av1_v4r3_ordinal_window_started,
    publish_av1_v4r3_ordinal_window_terminal,
)
from mediaforce.tuning.av1_validation_v4r3_qualification_request_operation import (
    materialize_av1_v4r3_qualification_request,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody_registry import (
    AV1V4R3PreparationCustodyRegistryBinding,
)
from tests.test_av1_validation_v4r3_preparation_custody import (
    _clock,
    _rights,
)
from tests.test_av1_validation_v4r3_qualification_request import (
    _registry_with_freeze,
)
from scripts import materialize_av1_v4r3_execution_preflight as script_module


class AV1V4R3ExecutionPreflightOperationTests(unittest.TestCase):
    def test_materializes_canonical_pair_idempotently(self) -> None:
        with TemporaryDirectory() as raw:
            preparation_binding, ordinal_binding, repository = _bindings(Path(raw))
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                created = _materialize(preparation_binding, ordinal_binding)
                existing = _materialize(
                    preparation_binding,
                    ordinal_binding,
                    clock=_clock(4, 1),
                )

            self.assertTrue(created.plan_created)
            self.assertTrue(created.preflight_created)
            self.assertFalse(existing.plan_created)
            self.assertFalse(existing.preflight_created)
            self.assertEqual(created.plan, existing.plan)
            self.assertEqual(created.preflight, existing.preflight)
            self.assertEqual(
                created.plan["r3_preflight_id"],
                created.preflight["preflight_id"],
            )
            opens = _parse(str(created.plan["plan_opens_at"]))
            closes = _parse(str(created.plan["plan_closes_at"]))
            self.assertEqual(
                int((closes - opens).total_seconds()),
                AV1_V4R3_OW_AGGREGATE_SECONDS_MAX,
            )
            self.assertEqual(
                created.preflight["created_at"], created.plan["plan_opens_at"]
            )
            for path in (created.plan_path, created.preflight_path):
                metadata = path.stat()
                self.assertEqual(metadata.st_mode & 0o777, 0o600)
                self.assertEqual(metadata.st_nlink, 1)
                self.assertEqual(metadata.st_uid, os.geteuid())

    def test_plan_only_crash_is_repaired_without_rewriting_plan(self) -> None:
        with TemporaryDirectory() as raw:
            preparation_binding, ordinal_binding, repository = _bindings(Path(raw))
            original_write = _RegistryContext.write

            def interrupt_preflight(
                context: _RegistryContext, filename: str, data: bytes
            ) -> None:
                if filename == "preflight.json":
                    raise OSError("simulated interruption")
                original_write(context, filename, data)

            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                patch.object(_RegistryContext, "write", interrupt_preflight),
                self.assertRaises(AV1V4R3ExecutionPreflightOperationError),
            ):
                _materialize(preparation_binding, ordinal_binding)

            plan_path = ordinal_binding.registry / "plan.json"
            original_plan = plan_path.read_bytes()
            self.assertFalse((ordinal_binding.registry / "preflight.json").exists())
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                repaired = _materialize(
                    preparation_binding,
                    ordinal_binding,
                    clock=_clock(4, 2),
                )
            self.assertFalse(repaired.plan_created)
            self.assertTrue(repaired.preflight_created)
            self.assertEqual(plan_path.read_bytes(), original_plan)

    def test_preflight_without_plan_and_terminal_registry_fail_closed(self) -> None:
        with TemporaryDirectory() as raw:
            preparation_binding, ordinal_binding, repository = _bindings(Path(raw))
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                pair = _materialize(preparation_binding, ordinal_binding)
            plan_bytes = pair.plan_path.read_bytes()
            pair.plan_path.unlink()
            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3ExecutionPreflightOperationError),
            ):
                _materialize(preparation_binding, ordinal_binding)
            self.assertFalse(pair.plan_path.exists())
            self.assertTrue(pair.preflight_path.exists())
            pair.plan_path.write_bytes(plan_bytes)
            pair.plan_path.chmod(0o600)
            (ordinal_binding.registry / "terminal.json").write_bytes(b"{}\n")
            (ordinal_binding.registry / "terminal.json").chmod(0o600)
            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3ExecutionPreflightOperationError),
            ):
                _materialize(preparation_binding, ordinal_binding)

    def test_grant_refuses_missing_preflight_then_succeeds_after_repair(self) -> None:
        with TemporaryDirectory() as raw:
            preparation_binding, ordinal_binding, repository = _bindings(Path(raw))
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                pair = _materialize(preparation_binding, ordinal_binding)
            pair.preflight_path.unlink()
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                publish_av1_v4r3_ordinal_window_grant(
                    binding=ordinal_binding,
                    plan=pair.plan,
                    ordinal=1,
                    clock=_clock(4, 5),
                    valid_until="2026-08-08T07:50:00Z",
                )
            self.assertFalse((ordinal_binding.registry / "terminal.json").exists())
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                repaired = _materialize(preparation_binding, ordinal_binding)
            grant = publish_av1_v4r3_ordinal_window_grant(
                binding=ordinal_binding,
                plan=repaired.plan,
                ordinal=1,
                clock=_clock(4, 5),
                valid_until="2026-08-08T07:50:00Z",
            )
            self.assertEqual(grant["ordinal"], 1)

    def test_pair_custody_is_required_after_grant_for_every_transition(self) -> None:
        with TemporaryDirectory() as raw:
            preparation_binding, ordinal_binding, repository = _bindings(Path(raw))
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                pair = _materialize(preparation_binding, ordinal_binding)
            grant = publish_av1_v4r3_ordinal_window_grant(
                binding=ordinal_binding,
                plan=pair.plan,
                ordinal=1,
                clock=_clock(4, 5),
                valid_until="2026-08-08T07:50:00Z",
            )

            pair.preflight_path.unlink()
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                publish_av1_v4r3_ordinal_window_claim(
                    binding=ordinal_binding,
                    plan=pair.plan,
                    grant=grant,
                    clock=_clock(4, 10),
                )
            self.assertFalse((ordinal_binding.registry / "terminal.json").exists())

            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                repaired = _materialize(preparation_binding, ordinal_binding)
            claim = publish_av1_v4r3_ordinal_window_claim(
                binding=ordinal_binding,
                plan=repaired.plan,
                grant=grant,
                clock=_clock(4, 10),
            )

            repaired.preflight_path.unlink()
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                publish_av1_v4r3_ordinal_window_started(
                    binding=ordinal_binding,
                    plan=repaired.plan,
                    grant=grant,
                    claim=claim,
                    clock=_clock(4, 11),
                )
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                repaired = _materialize(preparation_binding, ordinal_binding)
            started = publish_av1_v4r3_ordinal_window_started(
                binding=ordinal_binding,
                plan=repaired.plan,
                grant=grant,
                claim=claim,
                clock=_clock(4, 11),
            )

            repaired.preflight_path.unlink()
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                publish_av1_v4r3_ordinal_window_outcome(
                    binding=ordinal_binding,
                    plan=repaired.plan,
                    started=started,
                    clock=_clock(4, 12),
                    success=True,
                )
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                repaired = _materialize(preparation_binding, ordinal_binding)
            publish_av1_v4r3_ordinal_window_outcome(
                binding=ordinal_binding,
                plan=repaired.plan,
                started=started,
                clock=_clock(4, 12),
                success=True,
            )

            repaired.preflight_path.unlink()
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                publish_av1_v4r3_ordinal_window_terminal(
                    binding=ordinal_binding,
                    plan=repaired.plan,
                    clock=_clock(4, 13),
                )
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                derive_av1_v4r3_ordinal_window_high_water(
                    ordinal_binding.registry,
                    clock=_clock(4, 13),
                )
            self.assertFalse((ordinal_binding.registry / "terminal.json").exists())

    def test_corrupt_preflight_after_grant_refuses_claim_without_terminal(self) -> None:
        with TemporaryDirectory() as raw:
            preparation_binding, ordinal_binding, repository = _bindings(Path(raw))
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                pair = _materialize(preparation_binding, ordinal_binding)
            grant = publish_av1_v4r3_ordinal_window_grant(
                binding=ordinal_binding,
                plan=pair.plan,
                ordinal=1,
                clock=_clock(4, 5),
                valid_until="2026-08-08T07:50:00Z",
            )
            pair.preflight_path.write_bytes(b"{}\n")
            pair.preflight_path.chmod(0o600)
            with self.assertRaises(AV1V4R3OrdinalWindowRegistryError):
                publish_av1_v4r3_ordinal_window_claim(
                    binding=ordinal_binding,
                    plan=pair.plan,
                    grant=grant,
                    clock=_clock(4, 10),
                )
            self.assertFalse((ordinal_binding.registry / "terminal.json").exists())

    def test_registry_key_repository_expiry_and_errors_fail_safely(self) -> None:
        with TemporaryDirectory() as raw:
            preparation_binding, ordinal_binding, repository = _bindings(Path(raw))
            wrong_binding = AV1V4R3OrdinalWindowRegistryBinding(
                registry=ordinal_binding.registry,
                run_registry_id="av1v4r3runreg_" + "f" * 64,
            )
            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3ExecutionPreflightOperationError) as mismatch,
            ):
                _materialize(preparation_binding, wrong_binding)
            self.assertNotIn(str(ordinal_binding.registry), str(mismatch.exception))
            self.assertNotIn("path-privacy.key", str(mismatch.exception))

            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=("f" * 40, "e" * 40),
                ),
                self.assertRaises(AV1V4R3ExecutionPreflightOperationError),
            ):
                _materialize(preparation_binding, ordinal_binding)

            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3ExecutionPreflightOperationError),
            ):
                _materialize(
                    preparation_binding,
                    ordinal_binding,
                    clock=lambda: datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
                )

            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3ExecutionPreflightOperationError),
            ):
                _materialize(
                    preparation_binding,
                    ordinal_binding,
                    clock=lambda: datetime(2026, 8, 10, 3, 30, tzinfo=UTC),
                )

        with TemporaryDirectory() as raw:
            preparation_binding, ordinal_binding, repository = _bindings(Path(raw))
            replacement_key = b"x" * 32
            key_path = preparation_binding.registry / "path-privacy.key"
            key_path.write_bytes(replacement_key)
            key_path.chmod(0o600)
            rebound = AV1V4R3OrdinalWindowRegistryBinding(
                registry=ordinal_binding.registry,
                run_registry_id=av1_v4r3_ordinal_window_registry_hmac_id(
                    ordinal_binding.registry,
                    key=replacement_key,
                ),
            )
            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3ExecutionPreflightOperationError),
            ):
                _materialize(preparation_binding, rebound)
            self.assertEqual(list(ordinal_binding.registry.iterdir()), [])

    def test_stale_temp_corrupt_pair_and_mutated_chain_fail_closed(self) -> None:
        with TemporaryDirectory() as raw:
            preparation_binding, ordinal_binding, repository = _bindings(Path(raw))
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                pair = _materialize(preparation_binding, ordinal_binding)
            stale = ordinal_binding.registry / (
                f".preflight.json.{os.getpid()}.0123456789abcdef.tmp"
            )
            stale.write_bytes(b"stale")
            stale.chmod(0o600)
            with patch.object(
                operation_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                _materialize(preparation_binding, ordinal_binding)
            self.assertFalse(stale.exists())

            pair.preflight_path.write_bytes(b"{}\n")
            pair.preflight_path.chmod(0o600)
            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3ExecutionPreflightOperationError),
            ):
                _materialize(preparation_binding, ordinal_binding)
            self.assertEqual(pair.preflight_path.read_bytes(), b"{}\n")

        with TemporaryDirectory() as raw:
            preparation_binding, ordinal_binding, repository = _bindings(Path(raw))
            measurement = (
                preparation_binding.registry / "preparation-terminal-measurement.json"
            )
            measurement.write_bytes(b"{}\n")
            measurement.chmod(0o600)
            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3ExecutionPreflightOperationError),
            ):
                _materialize(preparation_binding, ordinal_binding)
            self.assertEqual(list(ordinal_binding.registry.iterdir()), [])

    def test_locks_are_sequential_and_concurrency_publishes_one_pair(self) -> None:
        with TemporaryDirectory() as raw:
            preparation_binding, ordinal_binding, repository = _bindings(Path(raw))
            state = {"preparation": False, "ordinal": False}
            original_preparation_lock = operation_module._locked_preparation_registry
            original_ordinal_lock = operation_module._locked_ordinal_registry

            @contextmanager
            def preparation_lock(binding: object) -> Iterator[Any]:
                self.assertFalse(state["ordinal"])
                state["preparation"] = True
                try:
                    with original_preparation_lock(binding) as context:
                        yield context
                finally:
                    state["preparation"] = False

            @contextmanager
            def ordinal_lock(registry: Path) -> Iterator[Any]:
                self.assertFalse(state["preparation"])
                state["ordinal"] = True
                try:
                    with original_ordinal_lock(registry) as context:
                        yield context
                finally:
                    state["ordinal"] = False

            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                patch.object(
                    operation_module,
                    "_locked_preparation_registry",
                    preparation_lock,
                ),
                patch.object(
                    operation_module,
                    "_locked_ordinal_registry",
                    ordinal_lock,
                ),
            ):
                first = _materialize(preparation_binding, ordinal_binding)
            self.assertTrue(first.plan_created)
            self.assertTrue(first.preflight_created)

        with TemporaryDirectory() as raw:
            preparation_binding, ordinal_binding, repository = _bindings(Path(raw))

            def materialize() -> tuple[bool, bool]:
                result = _materialize(preparation_binding, ordinal_binding)
                return result.plan_created, result.preflight_created

            with (
                patch.object(
                    operation_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                ThreadPoolExecutor(max_workers=4) as executor,
            ):
                results = list(executor.map(lambda _: materialize(), range(4)))
            self.assertEqual(results.count((True, True)), 1)
            self.assertEqual(results.count((False, False)), 3)

    def test_operation_imports_no_media_network_encoder_or_database_layers(
        self,
    ) -> None:
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
                "subprocess",
            }.isdisjoint(imports)
        )

    def test_owner_confirmed_script_is_json_only_and_non_authorizing(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            rights_path = root / "rights.json"
            rights_path.write_text(json.dumps(_rights()))
            result = SimpleNamespace(
                plan={
                    "plan_id": "av1vordplan4r3_" + "a" * 32,
                    "payload_sha256": "sha256:" + "b" * 64,
                    "r3_request_id": "av1v4r3req_" + "c" * 32,
                    "run_registry_id": "av1v4r3runreg_" + "d" * 64,
                },
                preflight={
                    "preflight_id": "av1v4r3preflight_" + "e" * 32,
                    "payload_sha256": "sha256:" + "f" * 64,
                    "execution_readiness_state": "ready_pending_owner_execution_grant",
                    "media_read_authorized": False,
                    "runtime_execution_authorized": False,
                    "dogfood_authorized": False,
                },
                plan_created=True,
                preflight_created=True,
            )
            argv = [
                "--repository-root",
                str(root),
                "--preparation-registry",
                str(root / "preparation"),
                "--ordinal-registry",
                str(root / "ordinal"),
                "--run-registry-id",
                "av1v4r3runreg_" + "d" * 64,
                "--rights-attestation",
                str(rights_path),
                "--owner-principal",
                "owner:test",
                "--confirm-owner-principal",
                "owner:test",
            ]
            output = io.StringIO()
            with (
                patch.object(
                    script_module,
                    "materialize_av1_v4r3_execution_preflight",
                    return_value=result,
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(script_module.main(argv), 0)
            summary = json.loads(output.getvalue())
            self.assertTrue(summary["ok"])
            self.assertFalse(summary["media_read_authorized"])
            self.assertFalse(summary["runtime_execution_authorized"])
            self.assertFalse(summary["dogfood_authorized"])
            self.assertNotIn("path", summary)

            output = io.StringIO()
            mismatched = [*argv[:-1], "owner:other"]
            with redirect_stdout(output):
                self.assertEqual(script_module.main(mismatched), 1)
            self.assertEqual(
                json.loads(output.getvalue())["error"],
                "AV1 v4 r3 execution preflight owner confirmation is invalid",
            )


def _bindings(
    root: Path,
) -> tuple[
    AV1V4R3PreparationCustodyRegistryBinding,
    AV1V4R3OrdinalWindowRegistryBinding,
    tuple[str, str],
]:
    preparation_binding = _registry_with_freeze(root)
    freeze = load_av1_v4r3_owner_freeze(preparation_binding)
    assert freeze is not None
    repository = (
        str(freeze["reviewed_repository"]["commit"]),
        str(freeze["reviewed_repository"]["tree"]),
    )
    with patch(
        "mediaforce.tuning.av1_validation_v4r3_qualification_request_operation."
        "_measure_clean_repository",
        return_value=repository,
    ):
        materialize_av1_v4r3_qualification_request(
            binding=preparation_binding,
            rights_attestation=_rights(),
            owner_principal="owner:test",
            clock=_clock(3, 30),
        )
    ordinal_registry = root / "ordinal-registry"
    ordinal_registry.mkdir(mode=0o700)
    key = (preparation_binding.registry / "path-privacy.key").read_bytes()
    ordinal_binding = AV1V4R3OrdinalWindowRegistryBinding(
        registry=ordinal_registry,
        run_registry_id=av1_v4r3_ordinal_window_registry_hmac_id(
            ordinal_registry,
            key=key,
        ),
    )
    return preparation_binding, ordinal_binding, repository


def _materialize(
    preparation_binding: AV1V4R3PreparationCustodyRegistryBinding,
    ordinal_binding: AV1V4R3OrdinalWindowRegistryBinding,
    *,
    clock: Callable[[], datetime] = _clock(4),
) -> AV1V4R3ExecutionPreflightOperationResult:
    return materialize_av1_v4r3_execution_preflight(
        preparation_binding=preparation_binding,
        ordinal_binding=ordinal_binding,
        rights_attestation=_rights(),
        owner_principal="owner:test",
        clock=clock,
    )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


if __name__ == "__main__":
    unittest.main()
