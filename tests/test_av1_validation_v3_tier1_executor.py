import json
from pathlib import Path
import tempfile
import unittest

from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
    AV1ValidationV3Tier1CommandResult,
    AV1ValidationV3Tier1ExecutorError,
    build_av1_validation_v3_tier1_fixture_plans,
    load_av1_validation_v3_tier1_fixture_matrix,
    verify_av1_validation_v3_tier1_fixture,
)


MATRIX_PATH = Path("docs/validation/av1-tier1-synthetic-fixture-matrix-v2.json")


class _Executor:
    def __init__(self, results: list[AV1ValidationV3Tier1CommandResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def run(self, args: tuple[str, ...]) -> AV1ValidationV3Tier1CommandResult:
        self.calls.append(args)
        return self.results.pop(0)


class AV1ValidationV3Tier1ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = load_av1_validation_v3_tier1_fixture_matrix(MATRIX_PATH)

    def test_matrix_loader_and_plan_builder_preserve_frozen_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plans = build_av1_validation_v3_tier1_fixture_plans(
                self.matrix,
                output_directory=Path(directory),
                repository_root=Path.cwd(),
            )
        self.assertEqual(len(plans), 4)
        self.assertTrue(all(plan.matrix_sha256 == AV1_VALIDATION_V3_TIER1_MATRIX_SHA256 for plan in plans))
        self.assertTrue(all("-frames:v" in plan.generate_args for plan in plans))
        self.assertTrue(all("288" in plan.generate_args for plan in plans))
        self.assertTrue(all("-count_frames" in plan.probe_args for plan in plans))

    def test_plan_builder_rejects_repository_output(self) -> None:
        with self.assertRaises(AV1ValidationV3Tier1ExecutorError):
            build_av1_validation_v3_tier1_fixture_plans(
                self.matrix,
                output_directory=Path.cwd() / "fixtures",
                repository_root=Path.cwd(),
            )

    def test_verify_fixture_binds_probe_and_decoded_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_av1_validation_v3_tier1_fixture_plans(
                self.matrix,
                output_directory=Path(directory),
                repository_root=Path.cwd(),
            )[0]
        probe = json.dumps({"streams": [{
            "width": 1280, "height": 720, "r_frame_rate": "24/1",
            "pix_fmt": "yuv420p10le", "color_primaries": "bt709",
            "color_transfer": "bt709", "color_space": "bt709",
            "color_range": "tv", "nb_read_frames": "288",
        }]}).encode()
        executor = _Executor([
            AV1ValidationV3Tier1CommandResult(0, probe, ""),
            AV1ValidationV3Tier1CommandResult(0, b"decoded-frames", ""),
        ])
        outcome = verify_av1_validation_v3_tier1_fixture(
            plan,
            matrix=self.matrix,
            executor=executor,
        )
        self.assertTrue(outcome.passed)
        self.assertTrue(outcome.content_sha256.startswith("sha256:"))
        self.assertEqual(outcome.fixture_id, "tier1_flat_field")

    def test_verify_fixture_returns_failures_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_av1_validation_v3_tier1_fixture_plans(
                self.matrix,
                output_directory=Path(directory),
                repository_root=Path.cwd(),
            )[0]
        executor = _Executor([
            AV1ValidationV3Tier1CommandResult(0, b'{"streams":[{"nb_read_frames":"N/A"}]}', ""),
            AV1ValidationV3Tier1CommandResult(1, b"", "failure"),
        ])
        outcome = verify_av1_validation_v3_tier1_fixture(
            plan,
            matrix=self.matrix,
            executor=executor,
        )
        self.assertFalse(outcome.passed)
        self.assertIn("nb_read_frames_mismatch", outcome.failures)
        self.assertIn("content_hash_failed", outcome.failures)

    def test_executor_module_has_no_runtime_or_private_imports(self) -> None:
        source = Path("mediaforce/tuning/av1_validation_v3_tier1_executor.py").read_text()
        for forbidden in (
            "subprocess", "sqlite3", "mediaforce.web", "av1_validation_partition",
            "av1_validation_derivation", "av1_validation_harness", "av1_validation_v2",
        ):
            self.assertNotIn(forbidden, source)
