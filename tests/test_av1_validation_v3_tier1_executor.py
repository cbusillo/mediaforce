import ast
import json
from pathlib import Path
import tempfile
import unittest

from mediaforce.tuning.av1_validation_v3 import load_av1_validation_protocol_v3
from mediaforce.tuning.av1_validation_v3_qualification import (
    build_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
    AV1ValidationV3Tier1CommandResult,
    AV1ValidationV3Tier1ExecutionContext,
    AV1ValidationV3Tier1ExecutorError,
    AV1ValidationV3Tier1HashResult,
    build_av1_validation_v3_tier1_fixture_plans,
    execute_av1_validation_v3_tier1_fixture,
    load_av1_validation_v3_tier1_fixture_matrix,
)
from mediaforce.tuning.av1_validation_v3_tier1_grant import (
    build_av1_validation_v3_tier1_execution_grant,
)
from mediaforce.tuning.av1_validation_v3_tier1_request import (
    build_av1_validation_v3_tier1_authorization_request,
)


MATRIX_PATH = Path("docs/validation/av1-tier1-synthetic-fixture-matrix-v2.json")
EXPECTED_DECODED_BYTE_COUNT = 1280 * 720 * 3 * 288


class _Executor:
    def __init__(
        self,
        results: list[AV1ValidationV3Tier1CommandResult],
        hash_result: AV1ValidationV3Tier1HashResult,
        *,
        create_output: bool = True,
    ) -> None:
        self.results = results
        self.hash_result = hash_result
        self.create_output = create_output
        self.calls: list[tuple[str, ...]] = []
        self.hash_calls: list[tuple[str, ...]] = []

    def run(self, args: tuple[str, ...]) -> AV1ValidationV3Tier1CommandResult:
        self.calls.append(args)
        result = self.results.pop(0)
        if self.create_output and result.returncode == 0 and args[0] == "ffmpeg" and "lavfi" in args:
            Path(args[-1]).touch()
        return result

    def run_sha256(self, args: tuple[str, ...]) -> AV1ValidationV3Tier1HashResult:
        self.hash_calls.append(args)
        return self.hash_result


class AV1ValidationV3Tier1ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = load_av1_validation_v3_tier1_fixture_matrix(MATRIX_PATH)
        protocol = load_av1_validation_protocol_v3(
            Path("docs/validation/av1-cold-start-preregistration-v3.json")
        )
        plan = build_av1_validation_v3_qualification_plan(
            protocol=protocol,
            qualification_key_id=f"av1vqkey3_{'a' * 32}",
            eligibility_predicate_sha256=f"sha256:{'b' * 64}",
            repository_commit="1" * 40,
            repository_tree="2" * 40,
            config_sha256=f"sha256:{'c' * 64}",
            toolchain_sha256=f"sha256:{'d' * 64}",
            fixture_matrix_sha256=AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
            frozen_at="2026-08-03T12:00:00Z",
            valid_until="2026-08-05T12:00:00Z",
        )
        request = build_av1_validation_v3_tier1_authorization_request(
            protocol=protocol,
            plan=plan,
            requested_at="2026-08-03T13:00:00Z",
            valid_until="2026-08-04T18:00:00Z",
        )
        grant = build_av1_validation_v3_tier1_execution_grant(
            protocol=protocol,
            plan=plan,
            request=request,
            owner_principal="owner-1234abcd",
            authorized_at="2026-08-03T14:00:00Z",
            valid_until="2026-08-04T17:00:00Z",
        )
        self.context = AV1ValidationV3Tier1ExecutionContext(
            protocol=protocol,
            qualification_plan=plan,
            request=request,
            grant=grant,
            as_of="2026-08-04T16:00:00Z",
        )

    def test_matrix_loader_and_plan_builder_preserve_frozen_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plans = build_av1_validation_v3_tier1_fixture_plans(
                self.matrix,
                output_directory=Path(directory),
                repository_root=Path.cwd(),
            )
            self.assertEqual(len(plans), 4)
            self.assertTrue(
                all(plan.matrix_sha256 == AV1_VALIDATION_V3_TIER1_MATRIX_SHA256 for plan in plans)
            )
            self.assertTrue(all("-n" in plan.generate_args for plan in plans))
            self.assertTrue(all("-frames:v" in plan.generate_args for plan in plans))
            self.assertTrue(all("288" in plan.generate_args for plan in plans))
            self.assertTrue(all("-count_frames" in plan.probe_args for plan in plans))

    def test_plan_builder_rejects_repository_and_existing_outputs(self) -> None:
        with self.assertRaises(AV1ValidationV3Tier1ExecutorError):
            build_av1_validation_v3_tier1_fixture_plans(
                self.matrix,
                output_directory=Path.cwd(),
                repository_root=Path.cwd(),
            )
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "tier1_flat_field.nut").touch()
            with self.assertRaisesRegex(AV1ValidationV3Tier1ExecutorError, "must not already exist"):
                build_av1_validation_v3_tier1_fixture_plans(
                    self.matrix,
                    output_directory=Path(directory),
                    repository_root=Path.cwd(),
                )

    def test_plan_builder_rejects_symlink_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            symlink = root / "link"
            symlink.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(AV1ValidationV3Tier1ExecutorError, "symlink"):
                build_av1_validation_v3_tier1_fixture_plans(
                    self.matrix,
                    output_directory=symlink,
                    repository_root=Path.cwd(),
                )

    def test_execute_fixture_binds_grant_probe_and_streamed_content_hash(self) -> None:
        probe = _valid_probe()
        executor = _Executor(
            [
                AV1ValidationV3Tier1CommandResult(0, b"", ""),
                AV1ValidationV3Tier1CommandResult(0, probe, ""),
            ],
            AV1ValidationV3Tier1HashResult(
                0,
                f"sha256:{'e' * 64}",
                EXPECTED_DECODED_BYTE_COUNT,
                "",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            outcome = execute_av1_validation_v3_tier1_fixture(
                "tier1_flat_field",
                matrix=self.matrix,
                output_directory=Path(directory),
                repository_root=Path.cwd(),
                context=self.context,
                executor=executor,
            )
        self.assertTrue(outcome.passed)
        self.assertEqual(outcome.content_byte_count, EXPECTED_DECODED_BYTE_COUNT)
        self.assertEqual(outcome.request_id, self.context.request.request_id)
        self.assertEqual(outcome.grant_id, self.context.grant.grant_id)
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(len(executor.hash_calls), 1)

    def test_execute_fixture_rejects_expired_grant_before_commands(self) -> None:
        expired_context = AV1ValidationV3Tier1ExecutionContext(
            protocol=self.context.protocol,
            qualification_plan=self.context.qualification_plan,
            request=self.context.request,
            grant=self.context.grant,
            as_of="2026-08-04T17:00:00Z",
        )
        executor = _Executor([], AV1ValidationV3Tier1HashResult(0, f"sha256:{'e' * 64}", 0, ""))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(AV1ValidationV3Tier1ExecutorError, "not authorized"):
                execute_av1_validation_v3_tier1_fixture(
                    "tier1_flat_field",
                    matrix=self.matrix,
                    output_directory=Path(directory),
                    repository_root=Path.cwd(),
                    context=expired_context,
                    executor=executor,
                )
        self.assertEqual(executor.calls, [])
        self.assertEqual(executor.hash_calls, [])

    def test_execute_fixture_rejects_empty_or_truncated_decoded_stream(self) -> None:
        for byte_count in (0, EXPECTED_DECODED_BYTE_COUNT - 1):
            with self.subTest(byte_count=byte_count):
                executor = _Executor(
                    [
                        AV1ValidationV3Tier1CommandResult(0, b"", ""),
                        AV1ValidationV3Tier1CommandResult(0, _valid_probe(), ""),
                    ],
                    AV1ValidationV3Tier1HashResult(
                        0,
                        f"sha256:{'e' * 64}",
                        byte_count,
                        "",
                    ),
                )
                with tempfile.TemporaryDirectory() as directory:
                    outcome = execute_av1_validation_v3_tier1_fixture(
                        "tier1_flat_field",
                        matrix=self.matrix,
                        output_directory=Path(directory),
                        repository_root=Path.cwd(),
                        context=self.context,
                        executor=executor,
                    )
                self.assertFalse(outcome.passed)
                self.assertIn("content_byte_count_mismatch", outcome.failures)

    def test_execute_fixture_rejects_missing_generated_output(self) -> None:
        executor = _Executor(
            [AV1ValidationV3Tier1CommandResult(0, b"", "")],
            AV1ValidationV3Tier1HashResult(0, f"sha256:{'e' * 64}", 0, ""),
            create_output=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            outcome = execute_av1_validation_v3_tier1_fixture(
                "tier1_flat_field",
                matrix=self.matrix,
                output_directory=Path(directory),
                repository_root=Path.cwd(),
                context=self.context,
                executor=executor,
            )
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.failures, ("generated_output_invalid",))
        self.assertEqual(executor.hash_calls, [])

    def test_execute_fixture_rederives_plan_and_rejects_mismatched_matrix(self) -> None:
        changed = dict(self.matrix)
        changed["fixture_scope"] = "forged"
        executor = _Executor([], AV1ValidationV3Tier1HashResult(0, f"sha256:{'e' * 64}", 0, ""))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AV1ValidationV3Tier1ExecutorError):
                execute_av1_validation_v3_tier1_fixture(
                    "tier1_flat_field",
                    matrix=changed,
                    output_directory=Path(directory),
                    repository_root=Path.cwd(),
                    context=self.context,
                    executor=executor,
                )
        self.assertEqual(executor.calls, [])

    def test_executor_module_has_no_runtime_or_private_imports(self) -> None:
        source = Path("mediaforce/tuning/av1_validation_v3_tier1_executor.py").read_text()
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        for forbidden in (
            "subprocess", "sqlite3", "mediaforce.web", "av1_validation_partition",
            "av1_validation_derivation", "av1_validation_harness", "av1_validation_v2",
        ):
            self.assertFalse(any(forbidden in module for module in imported))


def _valid_probe() -> bytes:
    return json.dumps({"streams": [{
        "width": 1280,
        "height": 720,
        "r_frame_rate": "24/1",
        "pix_fmt": "yuv420p10le",
        "color_primaries": "bt709",
        "color_transfer": "bt709",
        "color_space": "bt709",
        "color_range": "tv",
        "nb_read_frames": "288",
    }]}).encode()
