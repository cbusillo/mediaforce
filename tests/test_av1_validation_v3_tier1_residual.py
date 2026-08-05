from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from typing import Any, cast
from unittest.mock import patch

from mediaforce.core.evidence import stable_json_hash
from mediaforce.tuning.av1_validation_v3 import load_av1_validation_protocol_v3
from mediaforce.tuning.av1_validation_v3_qualification import (
    build_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_coverage import (
    AV1ValidationV3Tier1CoverageError,
    av1_validation_v3_tier1_coverage_from_payload,
)
from mediaforce.tuning.av1_validation_v3_tier1_diagnostics import (
    AV1ValidationV3Tier1CommandRecord,
)
from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
    AV1ValidationV3Tier1CommandResult,
    AV1ValidationV3Tier1HashResult,
)
from mediaforce.tuning.av1_validation_v3_tier1_residual_authorization import (
    build_av1_validation_v3_tier1_residual_grant,
    build_av1_validation_v3_tier1_residual_request,
    load_av1_validation_v3_tier1_residual_grant,
    load_av1_validation_v3_tier1_residual_request,
    serialize_av1_validation_v3_tier1_residual_grant,
    serialize_av1_validation_v3_tier1_residual_request,
)
from mediaforce.tuning.av1_validation_v3_tier1_residual_operation import (
    AV1ValidationV3Tier1ResidualOperationError,
    build_av1_validation_v3_tier1_residual_claim,
    load_av1_validation_v3_tier1_residual_result,
    run_av1_validation_v3_tier1_residual_probe,
    serialize_av1_validation_v3_tier1_residual_result,
)
from mediaforce.tuning.av1_validation_v3_tier1_residual_probe import (
    AV1_VALIDATION_V3_TIER1_RESIDUAL_FIXTURE_IDS,
    AV1_VALIDATION_V3_TIER1_RESIDUAL_MATRIX_SHA256,
    AV1_VALIDATION_V3_TIER1_RESIDUAL_SURFACE_IDS,
    AV1_VALIDATION_V3_TIER1_RESIDUAL_VARIANT_IDS,
    AV1ValidationV3Tier1ResidualExecutionContext,
    build_av1_validation_v3_tier1_residual_probe_plans,
    execute_av1_validation_v3_tier1_residual_fixture,
    load_av1_validation_v3_tier1_residual_probe_matrix,
)
from mediaforce.tuning.av1_validation_v3_tier1_residual_publication import (
    load_published_av1_validation_v3_tier1_residual_grant,
    load_published_av1_validation_v3_tier1_residual_result,
    publish_av1_validation_v3_tier1_residual_claim,
    publish_av1_validation_v3_tier1_residual_grant,
    publish_av1_validation_v3_tier1_residual_result,
)
from mediaforce.tuning.av1_validation_v3_tier1_runtime import (
    AV1ValidationV3Tier1CommandDiagnostic,
)
from scripts import verify_av1_cold_start_preregistration


MATRIX_PATH = Path("docs/validation/av1-tier1-residual-probe-matrix-v1.json")
MATRIX_V3_PATH = Path("docs/validation/av1-tier1-synthetic-fixture-matrix-v3.json")


class AV1ValidationV3Tier1ResidualTests(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = load_av1_validation_protocol_v3(
            Path("docs/validation/av1-cold-start-preregistration-v3.json")
        )
        self.plan = build_av1_validation_v3_qualification_plan(
            protocol=self.protocol,
            qualification_key_id=f"av1vqkey3_{'a' * 32}",
            eligibility_predicate_sha256=f"sha256:{'b' * 64}",
            repository_commit="1" * 40,
            repository_tree="2" * 40,
            config_sha256=f"sha256:{'c' * 64}",
            toolchain_sha256=f"sha256:{'d' * 64}",
            fixture_matrix_sha256=AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
            frozen_at="2026-08-05T12:00:00Z",
            valid_until="2026-08-06T12:00:00Z",
        )
        self.request = build_av1_validation_v3_tier1_residual_request(
            protocol=self.protocol,
            plan=self.plan,
            probe_matrix_sha256=AV1_VALIDATION_V3_TIER1_RESIDUAL_MATRIX_SHA256,
            requested_at="2026-08-05T13:00:00Z",
            valid_until="2026-08-06T10:00:00Z",
        )
        self.grant = build_av1_validation_v3_tier1_residual_grant(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            owner_principal="owner-1234abcd",
            authorized_at="2026-08-05T14:00:00Z",
            valid_until="2026-08-06T09:00:00Z",
        )
        self.claim = build_av1_validation_v3_tier1_residual_claim(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            grant=self.grant,
            claimed_at="2026-08-05T15:00:00Z",
        )
        self.matrix = load_av1_validation_v3_tier1_residual_probe_matrix(MATRIX_PATH)

    def test_matrix_is_canonical_exact_v3_residual_contract(self) -> None:
        matrix_v3 = json.loads(MATRIX_V3_PATH.read_text())
        self.assertEqual(
            f"sha256:{stable_json_hash(self.matrix)}",
            AV1_VALIDATION_V3_TIER1_RESIDUAL_MATRIX_SHA256,
        )
        self.assertEqual(self.matrix["fixtures"], matrix_v3["fixtures"])
        self.assertEqual(self.matrix["representation"], matrix_v3["intermediate"])
        self.assertEqual(self.matrix["frame_limit"], {"method": "frames_v", "value": 288})
        self.assertEqual(
            self.matrix["informed_by"],
            {"matrix_v3_sha256": AV1_VALIDATION_V3_TIER1_MATRIX_SHA256},
        )
        self.assertTrue(self.matrix["diagnostic_only"])
        self.assertFalse(self.matrix["evidence_eligible"])

    def test_plans_have_exact_command_and_variant_counts(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            repository = Path(directory) / "repo"
            output = Path(directory) / "output"
            repository.mkdir()
            output.mkdir()
            plans = build_av1_validation_v3_tier1_residual_probe_plans(
                self.matrix,
                output_directory=output,
                repository_root=repository,
            )
        self.assertEqual(tuple(plan.fixture_id for plan in plans), AV1_VALIDATION_V3_TIER1_RESIDUAL_FIXTURE_IDS)
        self.assertEqual(len(plans), 4)
        self.assertEqual(sum(1 + len(plan.surfaces) for plan in plans), 16)
        self.assertEqual(
            tuple(surface.surface_id for plan in plans for surface in plan.surfaces),
            AV1_VALIDATION_V3_TIER1_RESIDUAL_SURFACE_IDS * 4,
        )
        self.assertEqual(
            tuple(f"{plan.fixture_id}:{surface.surface_id}" for plan in plans for surface in plan.surfaces),
            AV1_VALIDATION_V3_TIER1_RESIDUAL_VARIANT_IDS,
        )
        self.assertTrue(all(plan.generate_args[-1] == str(plan.output_path) for plan in plans))
        self.assertTrue(all(plan.surfaces[-1].streaming for plan in plans))

    def test_authorization_round_trips_and_remains_non_evidentiary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request_path = Path(directory) / "request.json"
            grant_path = Path(directory) / "grant.json"
            request_path.write_bytes(serialize_av1_validation_v3_tier1_residual_request(self.request))
            grant_path.write_bytes(serialize_av1_validation_v3_tier1_residual_grant(self.grant))
            self.assertEqual(load_av1_validation_v3_tier1_residual_request(request_path), self.request)
            self.assertEqual(load_av1_validation_v3_tier1_residual_grant(grant_path), self.grant)
        self.assertFalse(self.request.to_payload()["residual_probe_execution_authorized"])
        self.assertTrue(self.grant.to_payload()["residual_probe_execution_authorized"])
        for field in (
            "tier1_execution_authorized",
            "tier2_execution_authorized",
            "evidence_eligible",
            "empirical_authority_conferred",
            "derivation_authorized",
            "holdout_authorized",
            "publication_authorized",
            "activation_authorized",
        ):
            self.assertFalse(self.request.to_payload()[field])
            self.assertFalse(self.grant.to_payload()[field])

    def test_fixture_executor_records_stream_frame_and_hash_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_av1_validation_v3_tier1_residual_probe_plans(
                self.matrix,
                output_directory=Path(directory),
                repository_root=Path.cwd(),
            )[0]
            outcomes = execute_av1_validation_v3_tier1_residual_fixture(
                plan,
                matrix=self.matrix,
                context=self._context(),
                executor=_ResidualExecutor(plan.output_path),
            )
        self.assertEqual(tuple(item.surface_id for item in outcomes), AV1_VALIDATION_V3_TIER1_RESIDUAL_SURFACE_IDS)
        self.assertTrue(all(item.passed for item in outcomes))
        self.assertEqual(outcomes[0].observation["nb_read_frames"], 288)
        self.assertEqual(outcomes[2].content_byte_count, 796_262_400)
        self.assertTrue(outcomes[2].content_sha256.startswith("sha256:"))

    def test_hash_mismatch_is_failure_without_invented_expected_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_av1_validation_v3_tier1_residual_probe_plans(
                self.matrix,
                output_directory=Path(directory),
                repository_root=Path.cwd(),
            )[0]
            outcomes = execute_av1_validation_v3_tier1_residual_fixture(
                plan,
                matrix=self.matrix,
                context=self._context(),
                executor=_ResidualExecutor(plan.output_path, byte_count=796_262_399),
            )
        self.assertEqual(outcomes[-1].failures, ("content_byte_count_mismatch",))
        self.assertFalse(outcomes[-1].passed)

    def test_hash_command_failure_records_empty_safe_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_av1_validation_v3_tier1_residual_probe_plans(
                self.matrix,
                output_directory=Path(directory),
                repository_root=Path.cwd(),
            )[0]
            outcomes = execute_av1_validation_v3_tier1_residual_fixture(
                plan,
                matrix=self.matrix,
                context=self._context(),
                executor=_ResidualExecutor(plan.output_path, hash_returncode=1),
            )
        self.assertEqual(outcomes[-1].failures, ("content_hash_failed",))
        self.assertEqual(outcomes[-1].content_sha256, "")
        self.assertFalse(outcomes[-1].passed)

    def test_operation_is_not_tier1_coverage_and_round_trips_result(self) -> None:
        session = _ResidualSession()

        @contextmanager
        def runtime(*args: object, **kwargs: object) -> Iterator[_ResidualSession]:
            yield session

        outcomes = self._outcomes()
        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_residual_operation."
                "paused_av1_validation_v3_tier1_residual_runtime",
                runtime,
            ),
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_residual_operation."
                "build_av1_validation_v3_tier1_residual_probe_plans",
                return_value=(object(), object(), object(), object()),
            ),
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_residual_operation."
                "execute_av1_validation_v3_tier1_residual_fixture",
                side_effect=(outcomes[:3], outcomes[3:6], outcomes[6:9], outcomes[9:]),
            ),
        ):
            result = run_av1_validation_v3_tier1_residual_probe(
                object(),
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                grant=self.grant,
                claim=self.claim,
                config_snapshot_bytes=b"{}\n",
                matrix=self.matrix,
                output_directory=Path("/tmp"),
                repository_root=Path("/repo"),
                toolchain=object(),
                claim_execution=lambda: self.claim.claim_id,
                clock=lambda: "2026-08-05T16:00:00Z",
            )
        self.assertEqual(result.to_public_summary()["variants_total"], 12)
        with self.assertRaises(AV1ValidationV3Tier1CoverageError):
            av1_validation_v3_tier1_coverage_from_payload(result.to_payload())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_bytes(serialize_av1_validation_v3_tier1_residual_result(result))
            self.assertEqual(load_av1_validation_v3_tier1_residual_result(path), result)

    def test_owner_publication_replays_and_claim_is_single_use(self) -> None:
        result = self._result()
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            root = Path(directory)
            repository = root / "repo"
            output = root / "artifacts"
            repository.mkdir(mode=0o700)
            grant_publication = publish_av1_validation_v3_tier1_residual_grant(
                grant=self.grant,
                output_root=output,
                repository_root=repository,
            )
            first_claim = publish_av1_validation_v3_tier1_residual_claim(
                claim=self.claim,
                output_root=output,
                repository_root=repository,
            )
            second_claim = publish_av1_validation_v3_tier1_residual_claim(
                claim=self.claim,
                output_root=output,
                repository_root=repository,
            )
            result_publication = publish_av1_validation_v3_tier1_residual_result(
                result=result,
                output_root=output,
                repository_root=repository,
            )
            self.assertEqual(
                load_published_av1_validation_v3_tier1_residual_grant(
                    output_root=output,
                    repository_root=repository,
                    request_id=self.request.request_id,
                ),
                self.grant,
            )
            self.assertEqual(
                load_published_av1_validation_v3_tier1_residual_result(
                    output_root=output,
                    repository_root=repository,
                    grant_id=self.grant.grant_id,
                ),
                result,
            )
        self.assertTrue(grant_publication.created)
        self.assertTrue(first_claim.created)
        self.assertFalse(second_claim.created)
        self.assertTrue(result_publication.created)

    def test_runner_reports_existing_claim_as_consumed_without_retry(self) -> None:
        args = SimpleNamespace(
            probe_matrix=MATRIX_PATH,
            grant_root=Path("/grant"),
            execution_output_root=Path("/execution"),
            variant_output_root=Path("/variants"),
            json_output=True,
        )

        def invoke_claim(*args: object, **kwargs: Any) -> object:
            cast(Callable[[], str], kwargs["claim_execution"])()
            raise AssertionError("operation must stop at the existing claim")

        stdout = io.StringIO()
        with (
            patch.object(verify_av1_cold_start_preregistration, "_assert_preregistration_bootstrap_authority"),
            patch.object(verify_av1_cold_start_preregistration, "_assert_canonical_preregistration_runner"),
            patch.object(verify_av1_cold_start_preregistration, "_now_iso", return_value=self.claim.claimed_at),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_tier1_residual_execution_inputs",
                return_value=(self.protocol, self.plan, self.request, object(), b"{}\n", object()),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_av1_validation_v3_tier1_residual_probe_matrix",
                return_value=self.matrix,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_published_av1_validation_v3_tier1_residual_grant",
                return_value=self.grant,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "build_av1_validation_v3_tier1_residual_claim",
                return_value=self.claim,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "publish_av1_validation_v3_tier1_residual_claim",
                side_effect=verify_av1_cold_start_preregistration.AV1ValidationV3Tier1PublicationError(
                    "execution_already_claimed",
                    "claim consumed",
                ),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_av1_validation_v3_tier1_residual_probe",
                side_effect=invoke_claim,
            ),
            redirect_stdout(stdout),
        ):
            exit_code = verify_av1_cold_start_preregistration._run_tier1_residual_probe(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["failure_reason"], "execution_already_claimed")
        self.assertTrue(payload["single_execution_claimed"])
        self.assertFalse(payload["retry_authorized"])
        self.assertFalse(payload["evidence_eligible"])
        self.assertFalse(payload["residual_probe_execution_authorized"])

    def test_runner_does_not_publish_result_after_claimed_lease_loss(self) -> None:
        args = SimpleNamespace(
            probe_matrix=MATRIX_PATH,
            grant_root=Path("/grant"),
            execution_output_root=Path("/execution"),
            variant_output_root=Path("/variants"),
            json_output=True,
        )

        def lose_lease(*args: object, **kwargs: Any) -> object:
            cast(Callable[[], str], kwargs["claim_execution"])()
            raise verify_av1_cold_start_preregistration.MediaforceRuntimeLockOwnershipError(
                "lease lost"
            )

        stdout = io.StringIO()
        with (
            patch.object(verify_av1_cold_start_preregistration, "_assert_preregistration_bootstrap_authority"),
            patch.object(verify_av1_cold_start_preregistration, "_assert_canonical_preregistration_runner"),
            patch.object(verify_av1_cold_start_preregistration, "_now_iso", return_value=self.claim.claimed_at),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_tier1_residual_execution_inputs",
                return_value=(self.protocol, self.plan, self.request, object(), b"{}\n", object()),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_av1_validation_v3_tier1_residual_probe_matrix",
                return_value=self.matrix,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_published_av1_validation_v3_tier1_residual_grant",
                return_value=self.grant,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "build_av1_validation_v3_tier1_residual_claim",
                return_value=self.claim,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "publish_av1_validation_v3_tier1_residual_claim",
                return_value=SimpleNamespace(created=True),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_av1_validation_v3_tier1_residual_probe",
                side_effect=lose_lease,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "publish_av1_validation_v3_tier1_residual_result",
            ) as result_publisher,
            redirect_stdout(stdout),
        ):
            exit_code = verify_av1_cold_start_preregistration._run_tier1_residual_probe(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["failure_reason"], "execution_failed")
        self.assertTrue(payload["single_execution_claimed"])
        self.assertFalse(payload["retry_authorized"])
        result_publisher.assert_not_called()

    def test_docs_name_residual_contract_and_non_evidence_boundary(self) -> None:
        docs = Path("docs/development/av1-cold-start-validation.md").read_text()
        for text in (
            "av1-tier1-residual-probe-matrix-v1.json",
            "publish-tier1-residual-request",
            "authorize-tier1-residual-probe",
            "run-tier1-residual-probe",
            "twelve buffered commands and four streaming hash commands",
            "activation authority false",
        ):
            self.assertIn(text, docs)

    def _context(self) -> AV1ValidationV3Tier1ResidualExecutionContext:
        return AV1ValidationV3Tier1ResidualExecutionContext(
            protocol=self.protocol,
            qualification_plan=self.plan,
            request=self.request,
            grant=self.grant,
            as_of="2026-08-05T16:00:00Z",
        )

    def _outcomes(self) -> tuple[object, ...]:
        with tempfile.TemporaryDirectory() as directory:
            plans = build_av1_validation_v3_tier1_residual_probe_plans(
                self.matrix,
                output_directory=Path(directory),
                repository_root=Path.cwd(),
            )
            outcomes = []
            for plan in plans:
                outcomes.extend(
                    execute_av1_validation_v3_tier1_residual_fixture(
                        plan,
                        matrix=self.matrix,
                        context=self._context(),
                        executor=_ResidualExecutor(plan.output_path),
                    )
                )
            return tuple(outcomes)

    def _result(self) -> object:
        session = _ResidualSession()

        @contextmanager
        def runtime(*args: object, **kwargs: object) -> Iterator[_ResidualSession]:
            yield session

        outcomes = self._outcomes()
        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_residual_operation."
                "paused_av1_validation_v3_tier1_residual_runtime",
                runtime,
            ),
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_residual_operation."
                "build_av1_validation_v3_tier1_residual_probe_plans",
                return_value=(object(), object(), object(), object()),
            ),
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_residual_operation."
                "execute_av1_validation_v3_tier1_residual_fixture",
                side_effect=(outcomes[:3], outcomes[3:6], outcomes[6:9], outcomes[9:]),
            ),
        ):
            return run_av1_validation_v3_tier1_residual_probe(
                object(),
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                grant=self.grant,
                claim=self.claim,
                config_snapshot_bytes=b"{}\n",
                matrix=self.matrix,
                output_directory=Path("/tmp"),
                repository_root=Path("/repo"),
                toolchain=object(),
                claim_execution=lambda: self.claim.claim_id,
                clock=lambda: "2026-08-05T16:00:00Z",
            )


class _ResidualExecutor:
    def __init__(
        self,
        output_path: Path,
        *,
        byte_count: int = 796_262_400,
        hash_returncode: int = 0,
    ) -> None:
        self.output_path = output_path
        self.byte_count = byte_count
        self.hash_returncode = hash_returncode

    def run(self, args: Sequence[str]) -> AV1ValidationV3Tier1CommandResult:
        if args[0] == "ffmpeg":
            self.output_path.write_bytes(b"fixture")
            return AV1ValidationV3Tier1CommandResult(returncode=0, stdout=b"", stderr="")
        collection = "streams" if "stream=" in " ".join(args) else "frames"
        observation = {
            "width": 1280,
            "height": 720,
            "pix_fmt": "yuv420p10le",
            "color_primaries": "bt709",
            "color_transfer": "bt709",
            "color_space": "bt709",
            "color_range": "tv",
        }
        if collection == "streams":
            observation.update({"r_frame_rate": "24/1", "nb_read_frames": "288"})
        return AV1ValidationV3Tier1CommandResult(
            returncode=0,
            stdout=json.dumps({collection: [observation]}).encode(),
            stderr="",
        )

    def run_streaming_sha256(self, args: Sequence[str]) -> AV1ValidationV3Tier1HashResult:
        return AV1ValidationV3Tier1HashResult(
            returncode=self.hash_returncode,
            content_sha256=f"sha256:{'e' * 64}",
            byte_count=self.byte_count,
            stderr="",
        )


class _ResidualSession:
    cleanup_passed = True
    diagnostics = (
        AV1ValidationV3Tier1CommandDiagnostic("ffmpeg", f"sha256:{'1' * 64}", "completed", 0, 0, 0, False),
        AV1ValidationV3Tier1CommandDiagnostic("ffprobe", f"sha256:{'2' * 64}", "completed", 0, 64, 0, False),
        AV1ValidationV3Tier1CommandDiagnostic("ffprobe", f"sha256:{'3' * 64}", "completed", 0, 64, 0, False),
        AV1ValidationV3Tier1CommandDiagnostic("ffmpeg", f"sha256:{'4' * 64}", "completed", 0, 796_262_400, 0, False),
    )
    executor = _ResidualExecutor(Path("/tmp/residual-placeholder.mkv"))


if __name__ == "__main__":
    unittest.main()
