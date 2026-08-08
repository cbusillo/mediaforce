from __future__ import annotations

import ast
import copy
import io
from contextlib import redirect_stdout
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
)
from mediaforce.tuning.av1_validation_v4_execution_preflight_operation import (
    AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_BLOCKERS,
    AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_FILENAME,
    AV1ValidationV4ExecutionPreflightOperationError,
    AV1ValidationV4ExecutionPreflightOperationInputs,
    AV1ValidationV4ExecutionPreflightOperationResult,
    assert_av1_validation_v4_execution_preflight,
    materialize_av1_validation_v4_execution_preflight,
)
from mediaforce.tuning.av1_validation_v4_execution_plan import (
    derive_av1_validation_v4_production_execution_plans,
)
from mediaforce.tuning.av1_validation_v4_freeze import (
    build_av1_validation_v4_manifest_freeze,
    serialize_av1_validation_v4_manifest_freeze,
)
from mediaforce.tuning.av1_validation_v4_preparation_config import (
    load_av1_validation_v4_effective_config_snapshot,
)
from mediaforce.tuning.av1_validation_v4_preparation_grant import (
    load_av1_validation_v4_preparation_grant,
)
from mediaforce.tuning.av1_validation_v4_preparation_operation import (
    run_av1_validation_v4_preparation_operation,
)
from mediaforce.tuning import av1_validation_v4_execution_preflight_operation
from mediaforce.tuning.av1_validation_v4_qualification_authority import (
    build_av1_validation_v4_qualification_request,
    serialize_av1_validation_v4_qualification_request,
)
from scripts import materialize_av1_v4_execution_preflight as cli
from tests import test_av1_validation_v4_preparation as preparation_test_module


PLAN_MODULE_PATH = Path("mediaforce/tuning/av1_validation_v4_execution_plan.py")
OPERATION_MODULE_PATH = Path(
    "mediaforce/tuning/av1_validation_v4_execution_preflight_operation.py"
)
SCRIPT_PATH = Path("scripts/materialize_av1_v4_execution_preflight.py")


class AV1ValidationV4ExecutionPreflightTests(unittest.TestCase):
    def test_success_derives_exact_blocked_readiness_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            registry = self._registry(root)
            result = self._materialize(self._operation_inputs(bundle, registry))
            self.assertTrue(result.created)
            self.assertEqual(
                result.path.name,
                AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_FILENAME,
            )
            self.assertEqual(result.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(result.path.stat().st_nlink, 1)
            assert_av1_validation_v4_execution_preflight(result.preflight)
            self.assertEqual(
                result.preflight["blockers"],
                list(AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_BLOCKERS),
            )
            self.assertFalse(result.preflight["execution_ready"])
            self.assertTrue(result.preflight["all_invocation_digests_match"])
            self.assertTrue(result.preflight["all_authority_fields_false"])
            for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                with self.subTest(field=field):
                    self.assertIs(result.preflight[field], False)
            self.assertEqual(len(result.preflight["plans"]), 8)
            for plan in result.preflight["plans"]:
                self.assertTrue(plan["digest_matches_expected"])
                self.assertTrue(plan["digest_matches_preparation"])
                self.assertTrue(plan["digest_matches_request"])
                self.assertEqual(
                    plan["derived_invocation_sha256"],
                    plan["expected_invocation_sha256"],
                )
                self.assertEqual(
                    plan["derived_invocation_sha256"],
                    plan["preparation_invocation_sha256"],
                )
            self.assertNotIn(str(root), json.dumps(result.preflight, sort_keys=True))

            second = self._materialize(
                self._operation_inputs(bundle, registry),
                checked_at=datetime(2026, 8, 7, 7, 30, tzinfo=UTC),
            )
            self.assertFalse(second.created)
            self.assertEqual(second.preflight, result.preflight)
            self.assertEqual(second.path.read_bytes(), result.path.read_bytes())

    def test_pure_plan_derivation_does_not_touch_source_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            source_values = set(bundle["config"]["source_paths"].values())
            real_stat = Path.stat
            real_open = os.open
            touched: list[str] = []

            def stat_spy(path: Path, *args: object, **kwargs: object) -> os.stat_result:
                if str(path) in source_values:
                    touched.append(str(path))
                    raise AssertionError("source stat attempted")
                return real_stat(path, *args, **kwargs)

            def open_spy(
                path: object, flags: int, *args: object, **kwargs: object
            ) -> int:
                if str(path) in source_values:
                    touched.append(str(path))
                    raise AssertionError("source open attempted")
                return real_open(path, flags, *args, **kwargs)

            with (
                mock.patch.object(Path, "stat", stat_spy),
                mock.patch("os.open", open_spy),
            ):
                plans = derive_av1_validation_v4_production_execution_plans(
                    manifest=bundle["manifest"],
                    effective_config=bundle["config"],
                    preparation=bundle["preparation"],
                    qualification_request=bundle["request"],
                )
            self.assertFalse(touched)
            self.assertEqual(len(plans), 8)
            self.assertTrue(all(plan.digest_matches_expected for plan in plans))

    def test_existing_conflict_preserves_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            registry = self._registry(root)
            inputs = self._operation_inputs(bundle, registry)
            result = self._materialize(inputs)
            original_bytes = result.path.read_bytes()
            with self.assertRaisesRegex(
                AV1ValidationV4ExecutionPreflightOperationError,
                "request identity",
            ):
                self._materialize(inputs, repository_identity=("d" * 40, "e" * 40))
            self.assertEqual(result.path.read_bytes(), original_bytes)

            result.path.unlink()
            result.path.write_bytes(b"foreign")
            os.chmod(result.path, 0o600)
            with self.assertRaisesRegex(
                AV1ValidationV4ExecutionPreflightOperationError,
                "not canonical",
            ):
                self._materialize(inputs)
            self.assertEqual(result.path.read_bytes(), b"foreign")

    def test_atomic_race_reconciles_matching_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            registry = self._registry(root)
            inputs = self._operation_inputs(bundle, registry)
            first = self._materialize(inputs)
            path = first.path
            path.unlink()
            real_publish = (
                av1_validation_v4_execution_preflight_operation._atomic_publish
            )

            def racing_publish(target: Path, data: bytes) -> bool:
                self.assertTrue(real_publish(target, data))
                return False

            with mock.patch.object(
                av1_validation_v4_execution_preflight_operation,
                "_atomic_publish",
                side_effect=racing_publish,
            ):
                raced = self._materialize(inputs)
            self.assertFalse(raced.created)
            self.assertEqual(raced.preflight, first.preflight)
            self.assertEqual(path.stat().st_nlink, 1)

    def test_concurrent_callers_create_one_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            registry = self._registry(root)
            inputs = self._operation_inputs(bundle, registry)
            results: list[AV1ValidationV4ExecutionPreflightOperationResult] = []
            errors: list[BaseException] = []

            def call() -> None:
                try:
                    results.append(
                        materialize_av1_validation_v4_execution_preflight(
                            inputs,
                            now=lambda: datetime(2026, 8, 7, 7, 0, tzinfo=UTC),
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=call) for _ in range(4)]
            with mock.patch.object(
                av1_validation_v4_execution_preflight_operation,
                "_measure_repository_identity",
                return_value=("b" * 40, "c" * 40),
            ):
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertFalse(errors)
            self.assertEqual(len(results), 4)
            self.assertEqual(sum(result.created for result in results), 1)
            self.assertEqual(
                len({result.preflight["payload_sha256"] for result in results}),
                1,
            )

    def test_dirty_repository_request_expiry_and_bundle_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory).resolve() / "repo"
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
            av1_validation_v4_execution_preflight_operation._measure_repository_identity(
                repository
            )
            tracked.write_text("dirty\n")
            with self.assertRaisesRegex(
                AV1ValidationV4ExecutionPreflightOperationError,
                "must be clean",
            ):
                av1_validation_v4_execution_preflight_operation._measure_repository_identity(
                    repository
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            registry = self._registry(root)
            with self.assertRaisesRegex(
                AV1ValidationV4ExecutionPreflightOperationError,
                "not active",
            ):
                self._materialize(
                    self._operation_inputs(bundle, registry),
                    checked_at=datetime(2026, 8, 8, 7, 0, tzinfo=UTC),
                )

            bad_freeze = copy.deepcopy(bundle["freeze"])
            bad_freeze["preparation_id"] = "av1vprep4_" + "f" * 32
            bundle["freeze_path"].write_bytes(
                json.dumps(bad_freeze, sort_keys=True, separators=(",", ":")).encode()
                + b"\n"
            )
            with self.assertRaisesRegex(
                AV1ValidationV4ExecutionPreflightOperationError,
                "full bundle",
            ):
                self._materialize(
                    self._operation_inputs(bundle, self._registry(root, name="bad"))
                )

    def test_fresh_request_runs_from_its_exact_clean_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = root / "repository"
            manifest_path = (
                repository / "docs/validation/av1-cold-start-preregistration-v4.json"
            )
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_bytes(
                preparation_test_module.MANIFEST_PATH.read_bytes()
            )
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
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
            repository_identity = (
                subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repository,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                subprocess.run(
                    ["git", "rev-parse", "HEAD^{tree}"],
                    cwd=repository,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
            )
            bundle = self._bundle(
                root / "artifacts",
                execution_repository=repository_identity,
            )
            inputs = self._operation_inputs(
                bundle,
                self._registry(root, name="preflight-registry"),
                repository_root=repository,
                manifest_path=manifest_path,
            )

            result = materialize_av1_validation_v4_execution_preflight(
                inputs,
                now=lambda: datetime(2026, 8, 7, 7, 0, tzinfo=UTC),
            )

            self.assertTrue(result.created)
            self.assertEqual(
                result.preflight["execution_repository"],
                {"commit": repository_identity[0], "tree": repository_identity[1]},
            )

    def test_cli_outputs_path_safe_json_and_sanitized_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            registry = self._registry(root)
            inputs = self._operation_inputs(bundle, registry)
            output = io.StringIO()
            fixed_now = lambda: datetime(2026, 8, 7, 7, 0, tzinfo=UTC)
            with (
                mock.patch.object(
                    av1_validation_v4_execution_preflight_operation,
                    "_measure_repository_identity",
                    return_value=("b" * 40, "c" * 40),
                ),
                mock.patch.object(
                    cli,
                    "materialize_av1_validation_v4_execution_preflight",
                    side_effect=lambda operation_inputs: (
                        materialize_av1_validation_v4_execution_preflight(
                            operation_inputs,
                            now=fixed_now,
                        )
                    ),
                ),
                redirect_stdout(output),
            ):
                exit_code = cli.main(self._cli_args(inputs))
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            text = json.dumps(payload, sort_keys=True)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["execution_ready"])
            self.assertEqual(payload["plan_count"], 8)
            self.assertNotIn(str(root), text)
            self.assertNotIn(str(registry), text)

            error_output = io.StringIO()
            with (
                mock.patch.object(
                    cli,
                    "materialize_av1_validation_v4_execution_preflight",
                    side_effect=RuntimeError(f"bad path {root}"),
                ),
                redirect_stdout(error_output),
            ):
                exit_code = cli.main(self._cli_args(inputs))
            self.assertEqual(exit_code, 1)
            self.assertNotIn(str(root), error_output.getvalue())
            self.assertIn("execution preflight failed", error_output.getvalue())

            usage_output = io.StringIO()
            with redirect_stdout(usage_output):
                exit_code = cli.main([])
            self.assertEqual(exit_code, 1)
            usage_payload = json.loads(usage_output.getvalue())
            self.assertFalse(usage_payload["ok"])
            self.assertNotIn("usage:", usage_output.getvalue())

            help_output = io.StringIO()
            with redirect_stdout(help_output):
                exit_code = cli.main(["--help"])
            self.assertEqual(exit_code, 1)
            help_payload = json.loads(help_output.getvalue())
            self.assertFalse(help_payload["ok"])
            self.assertNotIn("usage:", help_output.getvalue())

    def test_import_boundaries_exclude_execution_authority_and_media_seams(
        self,
    ) -> None:
        plan_allowed = {
            "__future__",
            "collections.abc",
            "copy",
            "dataclasses",
            "json",
            "mediaforce.core.evidence",
            "mediaforce.core.type_defs",
            "mediaforce.tuning.av1_validation_v4",
            "mediaforce.tuning.av1_validation_v4_preparation_config",
            "typing",
        }
        operation_allowed = {
            "__future__",
            "collections.abc",
            "contextlib",
            "dataclasses",
            "datetime",
            "fcntl",
            "hashlib",
            "json",
            "mediaforce.core.evidence",
            "mediaforce.core.type_defs",
            "mediaforce.tuning.av1_validation_v4",
            "mediaforce.tuning.av1_validation_v4_execution_plan",
            "mediaforce.tuning.av1_validation_v4_freeze",
            "mediaforce.tuning.av1_validation_v4_preparation",
            "mediaforce.tuning.av1_validation_v4_preparation_claim",
            "mediaforce.tuning.av1_validation_v4_preparation_config",
            "mediaforce.tuning.av1_validation_v4_preparation_grant",
            "mediaforce.tuning.av1_validation_v4_preparation_measurement",
            "mediaforce.tuning.av1_validation_v4_rights",
            "os",
            "pathlib",
            "secrets",
            "stat",
            "subprocess",
            "threading",
            "typing",
        }
        script_allowed = {
            "__future__",
            "argparse",
            "collections.abc",
            "json",
            "mediaforce.tuning.av1_validation_v4",
            "mediaforce.tuning.av1_validation_v4_execution_preflight_operation",
            "pathlib",
            "typing",
        }
        self._assert_imports_allowed(PLAN_MODULE_PATH, plan_allowed)
        self._assert_imports_allowed(OPERATION_MODULE_PATH, operation_allowed)
        self._assert_imports_allowed(SCRIPT_PATH, script_allowed)
        forbidden_fragments = (
            "qualification_grant",
            "qualification_claim",
            "run_start",
            "qualification_runner",
            "mediaforce.encoding",
            "mediaforce.execution",
            "runner",
            "callback",
        )
        for path in (PLAN_MODULE_PATH, OPERATION_MODULE_PATH, SCRIPT_PATH):
            text = path.read_text()
            for fragment in forbidden_fragments:
                self.assertNotIn(fragment, text)

    def _bundle(
        self,
        root: Path,
        *,
        execution_repository: tuple[str, str] = ("b" * 40, "c" * 40),
    ) -> dict[str, object]:
        root.mkdir(parents=True, exist_ok=True)
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
        rights = fixture._rights_attestation()
        grant = load_av1_validation_v4_preparation_grant(inputs.preparation_grant_path)
        config_path = inputs.workspace / "configuration/effective-config-snapshot.json"
        preparation_path = inputs.workspace / "preparation/preparation-record.json"
        measurement_path = (
            inputs.workspace / "measurements/preparation-measurement.json"
        )
        config = load_av1_validation_v4_effective_config_snapshot(config_path)
        freeze = build_av1_validation_v4_manifest_freeze(
            rights_attestation=rights,
            preparation_grant=grant,
            preparation_claim=result.claim,
            effective_config=config,
            preparation=result.preparation,
            preparation_measurement=result.measurement,
            owner_principal="owner:test",
            decided_at="2026-08-07T06:30:00Z",
            materializer_repository_commit="9" * 40,
            materializer_repository_tree="a" * 40,
        )
        freeze_path = root / "freeze.json"
        freeze_path.write_bytes(
            serialize_av1_validation_v4_manifest_freeze(
                freeze,
                rights_attestation=rights,
                preparation_grant=grant,
                preparation_claim=result.claim,
                effective_config=config,
                preparation=result.preparation,
                preparation_measurement=result.measurement,
            )
        )
        os.chmod(freeze_path, 0o600)
        request = build_av1_validation_v4_qualification_request(
            freeze=freeze,
            preparation=result.preparation,
            owner_principal="owner:test",
            execution_repository_commit=execution_repository[0],
            execution_repository_tree=execution_repository[1],
            consumption_registry=str(self._registry(root, name="claim-registry")),
            requested_at="2026-08-07T06:45:00Z",
            valid_until="2026-08-08T06:45:00Z",
        )
        request_path = root / "qualification-request.json"
        request_path.write_bytes(
            serialize_av1_validation_v4_qualification_request(request)
        )
        os.chmod(request_path, 0o600)
        manifest = json.loads(preparation_test_module.MANIFEST_PATH.read_bytes())
        return {
            "root": root,
            "inputs": inputs,
            "manifest": manifest,
            "rights": rights,
            "grant": grant,
            "claim": result.claim,
            "config": config,
            "preparation": result.preparation,
            "measurement": result.measurement,
            "freeze": freeze,
            "request": request,
            "freeze_path": freeze_path,
            "request_path": request_path,
            "config_path": config_path,
            "preparation_path": preparation_path,
            "measurement_path": measurement_path,
        }

    def _registry(self, root: Path, *, name: str = "preflight-registry") -> Path:
        registry = (root / name).resolve()
        registry.mkdir(mode=0o700, exist_ok=True)
        return registry

    def _operation_inputs(
        self,
        bundle: dict[str, object],
        registry: Path,
        *,
        repository_root: Path | None = None,
        manifest_path: Path | None = None,
    ) -> AV1ValidationV4ExecutionPreflightOperationInputs:
        inputs = bundle["inputs"]
        default_manifest = preparation_test_module.MANIFEST_PATH.resolve()
        return AV1ValidationV4ExecutionPreflightOperationInputs(
            repository_root=repository_root or default_manifest.parents[2],
            registry=registry,
            manifest_path=manifest_path or default_manifest,
            freeze_path=bundle["freeze_path"],
            rights_attestation_path=inputs.rights_attestation_path,
            preparation_grant_path=inputs.preparation_grant_path,
            effective_config_path=bundle["config_path"],
            preparation_path=bundle["preparation_path"],
            preparation_measurement_path=bundle["measurement_path"],
            qualification_request_path=bundle["request_path"],
        )

    def _materialize(
        self,
        inputs: AV1ValidationV4ExecutionPreflightOperationInputs,
        *,
        checked_at: datetime = datetime(2026, 8, 7, 7, 0, tzinfo=UTC),
        repository_identity: tuple[str, str] = ("b" * 40, "c" * 40),
    ) -> AV1ValidationV4ExecutionPreflightOperationResult:
        with mock.patch.object(
            av1_validation_v4_execution_preflight_operation,
            "_measure_repository_identity",
            return_value=repository_identity,
        ):
            return materialize_av1_validation_v4_execution_preflight(
                inputs,
                now=lambda: checked_at,
            )

    def _cli_args(
        self,
        inputs: AV1ValidationV4ExecutionPreflightOperationInputs,
    ) -> list[str]:
        return [
            "--repository-root",
            str(inputs.repository_root),
            "--registry",
            str(inputs.registry),
            "--manifest",
            str(inputs.manifest_path),
            "--freeze",
            str(inputs.freeze_path),
            "--rights-attestation",
            str(inputs.rights_attestation_path),
            "--preparation-grant",
            str(inputs.preparation_grant_path),
            "--effective-config",
            str(inputs.effective_config_path),
            "--preparation",
            str(inputs.preparation_path),
            "--preparation-measurement",
            str(inputs.preparation_measurement_path),
            "--qualification-request",
            str(inputs.qualification_request_path),
        ]

    def _assert_imports_allowed(self, path: Path, allowed: set[str]) -> None:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, allowed)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertIn(node.module, allowed)


if __name__ == "__main__":
    unittest.main()
