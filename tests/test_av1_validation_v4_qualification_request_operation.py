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
from mediaforce.tuning import av1_validation_v4_qualification_request_operation
from mediaforce.tuning.av1_validation_v4_qualification_authority import (
    load_av1_validation_v4_qualification_request,
)
from mediaforce.tuning.av1_validation_v4_qualification_request_operation import (
    AV1_VALIDATION_V4_QUALIFICATION_REQUEST_FILENAME,
    AV1ValidationV4QualificationRequestOperationError,
    AV1ValidationV4QualificationRequestOperationInputs,
    AV1ValidationV4QualificationRequestOperationResult,
    materialize_av1_validation_v4_qualification_request,
)
from scripts import materialize_av1_v4_qualification_request as cli
from tests import test_av1_validation_v4_preparation as preparation_test_module


MODULE_PATH = Path(
    "mediaforce/tuning/av1_validation_v4_qualification_request_operation.py"
)
SCRIPT_PATH = Path("scripts/materialize_av1_v4_qualification_request.py")


class AV1ValidationV4QualificationRequestOperationTests(unittest.TestCase):
    def test_success_is_canonical_non_authorizing_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            registry = self._registry(root)
            result = self._materialize(self._operation_inputs(bundle, registry))
            self.assertTrue(result.created)
            self.assertEqual(result.path.name, AV1_VALIDATION_V4_QUALIFICATION_REQUEST_FILENAME)
            self.assertEqual(result.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(result.path.stat().st_nlink, 1)
            loaded = load_av1_validation_v4_qualification_request(result.path)
            self.assertEqual(loaded, result.request)
            self.assertEqual(result.request["requested_at"], "2026-08-07T07:00:00Z")
            self.assertEqual(result.request["valid_until"], "2026-08-08T07:00:00Z")
            self.assertEqual(result.request["freeze_id"], bundle["freeze"]["freeze_id"])
            self.assertEqual(
                result.request["execution_repository"],
                {"commit": "b" * 40, "tree": "c" * 40},
            )
            for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                with self.subTest(field=field):
                    self.assertIs(result.request[field], False)

            second = self._materialize(
                self._operation_inputs(bundle, registry),
                requested_at=datetime(2026, 8, 7, 7, 30, tzinfo=UTC),
            )
            self.assertFalse(second.created)
            self.assertEqual(second.request, result.request)
            self.assertEqual(second.path.read_bytes(), result.path.read_bytes())

    def test_existing_conflict_or_expiry_preserves_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            registry = self._registry(root)
            inputs = self._operation_inputs(bundle, registry)
            result = self._materialize(inputs)
            original_bytes = result.path.read_bytes()

            with self.assertRaisesRegex(
                AV1ValidationV4QualificationRequestOperationError,
                "different",
            ):
                self._materialize(inputs, repository_identity=("d" * 40, "e" * 40))
            self.assertEqual(result.path.read_bytes(), original_bytes)

            with self.assertRaisesRegex(
                AV1ValidationV4QualificationRequestOperationError,
                "expired",
            ):
                self._materialize(
                    inputs,
                    requested_at=datetime(2026, 8, 8, 7, 0, tzinfo=UTC),
                )
            self.assertEqual(result.path.read_bytes(), original_bytes)

            foreign = b"foreign"
            result.path.unlink()
            result.path.write_bytes(foreign)
            os.chmod(result.path, 0o600)
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationRequestOperationError,
                "different or invalid",
            ):
                self._materialize(inputs)
            self.assertEqual(result.path.read_bytes(), foreign)

    def test_atomic_race_reconciles_matching_existing_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            registry = self._registry(root)
            inputs = self._operation_inputs(bundle, registry)
            first = self._materialize(inputs)
            path = first.path
            path.unlink()
            real_publish = av1_validation_v4_qualification_request_operation._atomic_publish

            def racing_publish(target: Path, data: bytes) -> bool:
                written = real_publish(target, data)
                self.assertTrue(written)
                return False

            with mock.patch.object(
                av1_validation_v4_qualification_request_operation,
                "_atomic_publish",
                side_effect=racing_publish,
            ):
                raced = self._materialize(inputs)
            self.assertFalse(raced.created)
            self.assertEqual(raced.request, first.request)
            self.assertEqual(path.stat().st_nlink, 1)

    def test_concurrent_callers_create_one_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            registry = self._registry(root)
            inputs = self._operation_inputs(bundle, registry)
            results: list[AV1ValidationV4QualificationRequestOperationResult] = []
            errors: list[BaseException] = []

            def call() -> None:
                try:
                    results.append(
                        materialize_av1_validation_v4_qualification_request(
                            inputs,
                            now=lambda: datetime(2026, 8, 7, 7, 0, tzinfo=UTC),
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=call) for _ in range(4)]
            with mock.patch.object(
                av1_validation_v4_qualification_request_operation,
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
                {
                    json.dumps(result.request, sort_keys=True)
                    for result in results
                },
                {json.dumps(results[0].request, sort_keys=True)},
            )

    def test_registry_custody_symlink_canonical_and_in_repo_rejections(self) -> None:
        repository_root = preparation_test_module.MANIFEST_PATH.resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=repository_root) as directory:
            registry = Path(directory).resolve()
            os.chmod(registry, 0o700)
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationRequestOperationError,
                "outside the repository",
            ):
                av1_validation_v4_qualification_request_operation._assert_registry(
                    registry,
                    repository_root=repository_root,
                )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            target = self._registry(root, name="target")
            symlink = root / "request-link"
            symlink.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationRequestOperationError,
                "owner-only directory",
            ):
                self._materialize(self._operation_inputs(bundle, symlink))
            loose = self._registry(root, name="loose")
            os.chmod(loose, 0o755)
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationRequestOperationError,
                "owner-only directory",
            ):
                self._materialize(self._operation_inputs(bundle, loose))
            os.chmod(loose, 0o700)
            (root / "noncanonical").mkdir(mode=0o700)
            noncanonical = root / "noncanonical" / ".." / "loose"
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationRequestOperationError,
                "owner-only directory",
            ):
                self._materialize(self._operation_inputs(bundle, noncanonical))

    def test_repository_identity_requires_clean_checkout(self) -> None:
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
            commit, tree = (
                av1_validation_v4_qualification_request_operation._measure_repository_identity(
                    repository
                )
            )
            self.assertEqual(len(commit), 40)
            self.assertEqual(len(tree), 40)
            tracked.write_text("dirty\n")
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationRequestOperationError,
                "must be clean",
            ):
                av1_validation_v4_qualification_request_operation._measure_repository_identity(
                    repository
                )

    def test_window_clamps_to_manifest_expiry_and_rejects_expired_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            registry = self._registry(root)
            near_expiry = datetime(2027, 2, 2, 0, 0, tzinfo=UTC)
            result = self._materialize(
                self._operation_inputs(bundle, registry),
                requested_at=near_expiry,
            )
            self.assertEqual(result.request["requested_at"], "2027-02-02T00:00:00Z")
            self.assertEqual(result.request["valid_until"], "2027-02-02T18:53:35Z")

            later_registry = self._registry(root, name="later")
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationRequestOperationError,
                "expired",
            ):
                self._materialize(
                    self._operation_inputs(bundle, later_registry),
                    requested_at=datetime(2027, 2, 2, 18, 53, 35, tzinfo=UTC),
                )

    def test_bundle_substitution_and_noncanonical_artifacts_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            registry = self._registry(root)
            bad_freeze = copy.deepcopy(bundle["freeze"])
            bad_freeze["measurement_id"] = "av1vprepmeas4_" + "f" * 32
            bundle["freeze_path"].write_bytes(
                json.dumps(bad_freeze, sort_keys=True, separators=(",", ":")).encode()
                + b"\n"
            )
            os.chmod(bundle["freeze_path"], 0o600)
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationRequestOperationError,
                "full bundle",
            ):
                self._materialize(self._operation_inputs(bundle, registry))

            bundle = self._bundle(root / "second")
            registry = self._registry(root, name="second-request")
            bundle["preparation_path"].write_bytes(
                bundle["preparation_path"].read_bytes() + b" "
            )
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationRequestOperationError,
                "not canonical",
            ):
                self._materialize(self._operation_inputs(bundle, registry))

    def test_no_extra_registry_artifacts_beyond_singleton_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            registry = self._registry(root)
            result = self._materialize(self._operation_inputs(bundle, registry))
            names = sorted(path.name for path in registry.iterdir())
            self.assertEqual(
                names,
                [
                    ".av1-v4-qualification-request.lock",
                    AV1_VALIDATION_V4_QUALIFICATION_REQUEST_FILENAME,
                ],
            )
            self.assertTrue(all(not name.endswith(".tmp") for name in names))
            self.assertEqual(result.request["consumption_registry"], str(registry))

    def test_operation_and_cli_forbidden_import_boundaries(self) -> None:
        operation_allowed = {
            "__future__",
            "collections.abc",
            "contextlib",
            "dataclasses",
            "datetime",
            "fcntl",
            "json",
            "mediaforce.core.evidence",
            "mediaforce.tuning.av1_validation_v4",
            "mediaforce.tuning.av1_validation_v4_freeze",
            "mediaforce.tuning.av1_validation_v4_preparation",
            "mediaforce.tuning.av1_validation_v4_preparation_claim",
            "mediaforce.tuning.av1_validation_v4_preparation_config",
            "mediaforce.tuning.av1_validation_v4_preparation_grant",
            "mediaforce.tuning.av1_validation_v4_preparation_measurement",
            "mediaforce.tuning.av1_validation_v4_qualification_authority",
            "mediaforce.tuning.av1_validation_v4_rights",
            "os",
            "pathlib",
            "secrets",
            "stat",
            "subprocess",
            "typing",
        }
        script_allowed = {
            "__future__",
            "argparse",
            "collections.abc",
            "json",
            "mediaforce.tuning.av1_validation_v4",
            "mediaforce.tuning.av1_validation_v4_qualification_request_operation",
            "pathlib",
            "sys",
            "typing",
        }
        self._assert_imports_allowed(MODULE_PATH, operation_allowed)
        self._assert_imports_allowed(SCRIPT_PATH, script_allowed)
        forbidden_import_text = (
            "qualification_grant",
            "qualification_claim",
            "run_start",
            "qualification_runner",
            "mediaforce.encoding",
            "source_path",
            "path_privacy",
            "network",
        )
        for path in (MODULE_PATH, SCRIPT_PATH):
            text = path.read_text()
            for fragment in forbidden_import_text:
                self.assertNotIn(fragment, text)

    def test_cli_outputs_path_safe_json_and_sanitized_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = self._bundle(root)
            registry = self._registry(root)
            inputs = self._operation_inputs(bundle, registry)
            output = io.StringIO()
            with mock.patch.object(
                av1_validation_v4_qualification_request_operation,
                "_measure_repository_identity",
                return_value=("b" * 40, "c" * 40),
            ), redirect_stdout(output):
                exit_code = cli.main([
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
                    "--json",
                ])
            self.assertEqual(exit_code, 0)
            path_text = json.dumps(json.loads(output.getvalue()), sort_keys=True)
            self.assertNotIn(str(root), path_text)
            self.assertNotIn(str(registry), path_text)
            self.assertIn('"qualification_execution_authorized": false', path_text)

            error_output = io.StringIO()
            with mock.patch.object(
                cli,
                "materialize_av1_validation_v4_qualification_request",
                side_effect=RuntimeError(f"bad path {root}"),
            ), redirect_stdout(error_output):
                exit_code = cli.main([
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
                    "--json",
                ])
            self.assertEqual(exit_code, 1)
            self.assertNotIn(str(root), error_output.getvalue())
            self.assertIn(
                "qualification request materialization failed",
                error_output.getvalue(),
            )

    def _bundle(self, root: Path) -> dict[str, object]:
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
        grant = load_av1_validation_v4_preparation_grant(
            inputs.preparation_grant_path
        )
        config_path = inputs.workspace / "configuration/effective-config-snapshot.json"
        preparation_path = inputs.workspace / "preparation/preparation-record.json"
        measurement_path = inputs.workspace / "measurements/preparation-measurement.json"
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
        return {
            "root": root,
            "inputs": inputs,
            "rights": rights,
            "grant": grant,
            "claim": result.claim,
            "config": config,
            "preparation": result.preparation,
            "measurement": result.measurement,
            "freeze": freeze,
            "freeze_path": freeze_path,
            "config_path": config_path,
            "preparation_path": preparation_path,
            "measurement_path": measurement_path,
        }

    def _registry(self, root: Path, *, name: str = "request-registry") -> Path:
        registry = (root / name).resolve()
        registry.mkdir(mode=0o700)
        return registry

    def _operation_inputs(
        self,
        bundle: dict[str, object],
        registry: Path,
    ) -> AV1ValidationV4QualificationRequestOperationInputs:
        inputs = bundle["inputs"]
        return AV1ValidationV4QualificationRequestOperationInputs(
            repository_root=preparation_test_module.MANIFEST_PATH.resolve().parents[2],
            registry=registry,
            manifest_path=preparation_test_module.MANIFEST_PATH.resolve(),
            freeze_path=bundle["freeze_path"],
            rights_attestation_path=inputs.rights_attestation_path,
            preparation_grant_path=inputs.preparation_grant_path,
            effective_config_path=bundle["config_path"],
            preparation_path=bundle["preparation_path"],
            preparation_measurement_path=bundle["measurement_path"],
        )

    def _materialize(
        self,
        inputs: AV1ValidationV4QualificationRequestOperationInputs,
        *,
        requested_at: datetime = datetime(2026, 8, 7, 7, 0, tzinfo=UTC),
        repository_identity: tuple[str, str] = ("b" * 40, "c" * 40),
    ) -> AV1ValidationV4QualificationRequestOperationResult:
        with mock.patch.object(
            av1_validation_v4_qualification_request_operation,
            "_measure_repository_identity",
            return_value=repository_identity,
        ):
            return materialize_av1_validation_v4_qualification_request(
                inputs,
                now=lambda: requested_at,
            )

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
