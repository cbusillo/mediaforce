from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mediaforce.tuning.av1_validation_v3 import load_av1_validation_protocol_v3
from mediaforce.tuning.av1_validation_v3_qualification import (
    build_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_compat_authorization import (
    build_av1_validation_v3_tier1_compat_grant,
    build_av1_validation_v3_tier1_compat_request,
    load_av1_validation_v3_tier1_compat_grant,
    load_av1_validation_v3_tier1_compat_request,
    serialize_av1_validation_v3_tier1_compat_grant,
    serialize_av1_validation_v3_tier1_compat_request,
)
from mediaforce.tuning.av1_validation_v3_tier1_compat_operation import (
    AV1ValidationV3Tier1CompatOperationError,
    AV1ValidationV3Tier1CompatResult,
    av1_validation_v3_tier1_compat_result_from_payload,
    build_av1_validation_v3_tier1_compat_claim,
    load_av1_validation_v3_tier1_compat_result,
    run_av1_validation_v3_tier1_compat_probe,
    serialize_av1_validation_v3_tier1_compat_result,
)
from mediaforce.tuning.av1_validation_v3_tier1_compat_probe import (
    AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_SHA256,
    AV1_VALIDATION_V3_TIER1_COMPAT_REPRESENTATION_IDS,
    AV1_VALIDATION_V3_TIER1_COMPAT_VARIANT_IDS,
    AV1ValidationV3Tier1CompatExecutionContext,
    AV1ValidationV3Tier1CompatProbeError,
    AV1ValidationV3Tier1CompatVariantOutcome,
    build_av1_validation_v3_tier1_compat_probe_plans,
    execute_av1_validation_v3_tier1_compat_representation,
    load_av1_validation_v3_tier1_compat_probe_matrix,
)
from mediaforce.tuning.av1_validation_v3_tier1_compat_publication import (
    load_published_av1_validation_v3_tier1_compat_grant,
    load_published_av1_validation_v3_tier1_compat_result,
    publish_av1_validation_v3_tier1_compat_claim,
    publish_av1_validation_v3_tier1_compat_grant,
    publish_av1_validation_v3_tier1_compat_result,
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
)
from mediaforce.tuning.av1_validation_v3_tier1_runtime import (
    AV1ValidationV3Tier1CommandDiagnostic,
)
from scripts import verify_av1_cold_start_preregistration


MATRIX_PATH = Path("docs/validation/av1-tier1-compat-probe-matrix-v1.json")


class AV1ValidationV3Tier1CompatTests(unittest.TestCase):
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
        self.request = build_av1_validation_v3_tier1_compat_request(
            protocol=self.protocol,
            plan=self.plan,
            probe_matrix_sha256=AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_SHA256,
            requested_at="2026-08-05T13:00:00Z",
            valid_until="2026-08-06T10:00:00Z",
        )
        self.grant = build_av1_validation_v3_tier1_compat_grant(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            owner_principal="owner-1234abcd",
            authorized_at="2026-08-05T14:00:00Z",
            valid_until="2026-08-06T09:00:00Z",
        )
        self.claim = build_av1_validation_v3_tier1_compat_claim(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            grant=self.grant,
            claimed_at="2026-08-05T15:00:00Z",
        )
        self.matrix = load_av1_validation_v3_tier1_compat_probe_matrix(MATRIX_PATH)

    def test_authorization_round_trips_and_remains_non_evidentiary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request_path = Path(directory) / "request.json"
            grant_path = Path(directory) / "grant.json"
            request_path.write_bytes(serialize_av1_validation_v3_tier1_compat_request(self.request))
            grant_path.write_bytes(serialize_av1_validation_v3_tier1_compat_grant(self.grant))
            self.assertEqual(load_av1_validation_v3_tier1_compat_request(request_path), self.request)
            self.assertEqual(load_av1_validation_v3_tier1_compat_grant(grant_path), self.grant)
        self.assertFalse(self.request.to_payload()["compat_probe_execution_authorized"])
        self.assertTrue(self.grant.to_payload()["compat_probe_execution_authorized"])
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

    def test_matrix_builds_three_representations_and_six_disjoint_variants(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            repository = Path(directory) / "repo"
            output = Path(directory) / "output"
            repository.mkdir()
            output.mkdir()
            plans = build_av1_validation_v3_tier1_compat_probe_plans(
                self.matrix,
                output_directory=output,
                repository_root=repository,
            )
        self.assertEqual(tuple(item.representation_id for item in plans), AV1_VALIDATION_V3_TIER1_COMPAT_REPRESENTATION_IDS)
        self.assertTrue(all(plan.generate_args[-1] == str(plan.output_path) for plan in plans))
        self.assertEqual(
            tuple(
                f"{plan.representation_id}:{surface.surface_id}"
                for plan in plans
                for surface in plan.surfaces
            ),
            AV1_VALIDATION_V3_TIER1_COMPAT_VARIANT_IDS,
        )

    def test_executor_records_stream_and_frame_observations(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            repository = Path(directory) / "repo"
            output = Path(directory) / "output"
            repository.mkdir()
            output.mkdir()
            plan = build_av1_validation_v3_tier1_compat_probe_plans(
                self.matrix,
                output_directory=output,
                repository_root=repository,
            )[0]
            outcomes = execute_av1_validation_v3_tier1_compat_representation(
                plan,
                matrix=self.matrix,
                context=self._context(),
                executor=_CompatExecutor(plan.output_path),
            )
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(item.matches_expected_color_description for item in outcomes))
        self.assertEqual(outcomes[0].observation["color_range"], "limited")

    def test_operation_requires_exact_claim_and_is_not_tier1_coverage(self) -> None:
        session = _CompatSession()

        @contextmanager
        def runtime(*args: object, **kwargs: object) -> Iterator[_CompatSession]:
            yield session

        outcomes = self._outcomes()
        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_compat_operation."
                "paused_av1_validation_v3_tier1_compat_runtime",
                runtime,
            ),
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_compat_operation."
                "build_av1_validation_v3_tier1_compat_probe_plans",
                return_value=(object(), object(), object()),
            ),
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_compat_operation."
                "execute_av1_validation_v3_tier1_compat_representation",
                side_effect=(outcomes[:2], outcomes[2:4], outcomes[4:]),
            ),
        ):
            result = run_av1_validation_v3_tier1_compat_probe(
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
        self.assertEqual(result.to_public_summary()["variants_total"], 6)
        with self.assertRaises(AV1ValidationV3Tier1CoverageError):
            av1_validation_v3_tier1_coverage_from_payload(result.to_payload())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_bytes(serialize_av1_validation_v3_tier1_compat_result(result))
            self.assertEqual(load_av1_validation_v3_tier1_compat_result(path), result)

    def test_owner_only_publication_is_idempotent_and_claim_is_single_use(self) -> None:
        result = self._result()
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            root = Path(directory)
            repository = root / "repo"
            output = root / "artifacts"
            repository.mkdir(mode=0o700)
            grant_publication = publish_av1_validation_v3_tier1_compat_grant(
                grant=self.grant,
                output_root=output,
                repository_root=repository,
            )
            first_claim = publish_av1_validation_v3_tier1_compat_claim(
                claim=self.claim,
                output_root=output,
                repository_root=repository,
            )
            second_claim = publish_av1_validation_v3_tier1_compat_claim(
                claim=self.claim,
                output_root=output,
                repository_root=repository,
            )
            result_publication = publish_av1_validation_v3_tier1_compat_result(
                result=result,
                output_root=output,
                repository_root=repository,
            )
            self.assertEqual(
                load_published_av1_validation_v3_tier1_compat_grant(
                    output_root=output,
                    repository_root=repository,
                    request_id=self.request.request_id,
                ),
                self.grant,
            )
            self.assertEqual(
                load_published_av1_validation_v3_tier1_compat_result(
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

        def invoke_claim(*args: object, **kwargs: object) -> object:
            kwargs["claim_execution"]()
            raise AssertionError("operation must stop at the existing claim")

        stdout = io.StringIO()
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_assert_preregistration_bootstrap_authority",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_assert_canonical_preregistration_runner",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_now_iso",
                return_value=self.claim.claimed_at,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_tier1_compat_execution_inputs",
                return_value=(
                    self.protocol,
                    self.plan,
                    self.request,
                    object(),
                    b"{}\n",
                    object(),
                ),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_av1_validation_v3_tier1_compat_probe_matrix",
                return_value=self.matrix,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_published_av1_validation_v3_tier1_compat_grant",
                return_value=self.grant,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "build_av1_validation_v3_tier1_compat_claim",
                return_value=self.claim,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "publish_av1_validation_v3_tier1_compat_claim",
                side_effect=verify_av1_cold_start_preregistration.AV1ValidationV3Tier1PublicationError(
                    "execution_already_claimed",
                    "claim consumed",
                ),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_av1_validation_v3_tier1_compat_probe",
                side_effect=invoke_claim,
            ),
            redirect_stdout(stdout),
        ):
            exit_code = verify_av1_cold_start_preregistration._run_tier1_compat_probe(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["failure_reason"], "execution_already_claimed")
        self.assertTrue(payload["single_execution_claimed"])
        self.assertFalse(payload["retry_authorized"])
        self.assertFalse(payload["evidence_eligible"])

    def test_runner_does_not_publish_result_after_claimed_lease_loss(self) -> None:
        args = SimpleNamespace(
            probe_matrix=MATRIX_PATH,
            grant_root=Path("/grant"),
            execution_output_root=Path("/execution"),
            variant_output_root=Path("/variants"),
            json_output=True,
        )

        def lose_lease(*args: object, **kwargs: object) -> object:
            kwargs["claim_execution"]()
            raise verify_av1_cold_start_preregistration.MediaforceRuntimeLockOwnershipError(
                "lease lost"
            )

        stdout = io.StringIO()
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_assert_preregistration_bootstrap_authority",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_assert_canonical_preregistration_runner",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_now_iso",
                return_value=self.claim.claimed_at,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_tier1_compat_execution_inputs",
                return_value=(
                    self.protocol,
                    self.plan,
                    self.request,
                    object(),
                    b"{}\n",
                    object(),
                ),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_av1_validation_v3_tier1_compat_probe_matrix",
                return_value=self.matrix,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_published_av1_validation_v3_tier1_compat_grant",
                return_value=self.grant,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "build_av1_validation_v3_tier1_compat_claim",
                return_value=self.claim,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "publish_av1_validation_v3_tier1_compat_claim",
                return_value=SimpleNamespace(created=True),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_av1_validation_v3_tier1_compat_probe",
                side_effect=lose_lease,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "publish_av1_validation_v3_tier1_compat_result",
            ) as result_publisher,
            redirect_stdout(stdout),
        ):
            exit_code = verify_av1_cold_start_preregistration._run_tier1_compat_probe(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["failure_reason"], "execution_failed")
        self.assertTrue(payload["single_execution_claimed"])
        self.assertFalse(payload["retry_authorized"])
        result_publisher.assert_not_called()

    def test_execution_input_loader_rejects_probe_matrix_drift(self) -> None:
        args = SimpleNamespace(request_artifact=Path("request.json"))
        drifted_request = SimpleNamespace(
            repository_commit=self.plan.repository_commit,
            repository_tree=self.plan.repository_tree,
            config_sha256=self.plan.config_sha256,
            toolchain_sha256=self.plan.toolchain_sha256,
            probe_matrix_sha256=f"sha256:{'e' * 64}",
        )
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_tier1_compat_base_inputs",
                return_value=(
                    self.protocol,
                    self.plan,
                    object(),
                    b"{}\n",
                    object(),
                ),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_av1_validation_v3_tier1_compat_request",
                return_value=drifted_request,
            ),
            self.assertRaisesRegex(
                AV1ValidationV3Tier1CompatOperationError,
                "drifted",
            ),
        ):
            verify_av1_cold_start_preregistration._load_tier1_compat_execution_inputs(
                args,
                as_of="2026-08-05T15:00:00Z",
            )

    def test_operation_rejects_matrix_not_bound_to_request(self) -> None:
        request = build_av1_validation_v3_tier1_compat_request(
            protocol=self.protocol,
            plan=self.plan,
            probe_matrix_sha256=f"sha256:{'e' * 64}",
            requested_at="2026-08-05T13:00:00Z",
            valid_until="2026-08-06T10:00:00Z",
        )
        grant = build_av1_validation_v3_tier1_compat_grant(
            protocol=self.protocol,
            plan=self.plan,
            request=request,
            owner_principal="owner-1234abcd",
            authorized_at="2026-08-05T14:00:00Z",
            valid_until="2026-08-06T09:00:00Z",
        )
        claim = build_av1_validation_v3_tier1_compat_claim(
            protocol=self.protocol,
            plan=self.plan,
            request=request,
            grant=grant,
            claimed_at="2026-08-05T15:00:00Z",
        )
        with self.assertRaisesRegex(
            AV1ValidationV3Tier1CompatOperationError,
            "not bound",
        ):
            run_av1_validation_v3_tier1_compat_probe(
                object(),
                protocol=self.protocol,
                plan=self.plan,
                request=request,
                grant=grant,
                claim=claim,
                config_snapshot_bytes=b"{}\n",
                matrix=self.matrix,
                output_directory=Path("/tmp"),
                repository_root=Path("/repo"),
                toolchain=object(),
                claim_execution=lambda: claim.claim_id,
            )

    def test_each_probe_surface_rechecks_live_grant_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            plan = build_av1_validation_v3_tier1_compat_probe_plans(
                self.matrix,
                output_directory=output,
                repository_root=Path.cwd(),
            )[0]
            ticks = iter(
                (
                    "2026-08-06T08:59:00Z",
                    "2026-08-06T08:59:30Z",
                    "2026-08-06T09:00:00Z",
                )
            )
            with self.assertRaisesRegex(
                AV1ValidationV3Tier1CompatProbeError,
                "not authorized",
            ):
                execute_av1_validation_v3_tier1_compat_representation(
                    plan,
                    matrix=self.matrix,
                    context=self._context(),
                    executor=_CompatExecutor(plan.output_path),
                    clock=lambda: next(ticks),
                )

    def test_generation_failure_marks_both_surfaces_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_av1_validation_v3_tier1_compat_probe_plans(
                self.matrix,
                output_directory=Path(directory),
                repository_root=Path.cwd(),
            )[0]
            outcomes = execute_av1_validation_v3_tier1_compat_representation(
                plan,
                matrix=self.matrix,
                context=self._context(),
                executor=_FailingCompatExecutor(plan.output_path, invalid_probe=False),
            )
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(item.failures == ("generation_failed",) for item in outcomes))
        self.assertTrue(all(not item.matches_expected_color_description for item in outcomes))

    def test_invalid_probe_json_is_bounded_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_av1_validation_v3_tier1_compat_probe_plans(
                self.matrix,
                output_directory=Path(directory),
                repository_root=Path.cwd(),
            )[0]
            outcomes = execute_av1_validation_v3_tier1_compat_representation(
                plan,
                matrix=self.matrix,
                context=self._context(),
                executor=_FailingCompatExecutor(plan.output_path, invalid_probe=True),
            )
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(item.failures == ("probe_output_invalid",) for item in outcomes))

    def test_mismatched_claim_callback_prevents_probe_commands(self) -> None:
        session = _CompatSession()

        @contextmanager
        def runtime(*args: object, **kwargs: object) -> Iterator[_CompatSession]:
            yield session

        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_compat_operation."
                "paused_av1_validation_v3_tier1_compat_runtime",
                runtime,
            ),
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_compat_operation."
                "build_av1_validation_v3_tier1_compat_probe_plans",
            ) as plan_builder,
            self.assertRaisesRegex(
                AV1ValidationV3Tier1CompatOperationError,
                "not published",
            ),
        ):
            run_av1_validation_v3_tier1_compat_probe(
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
                claim_execution=lambda: "av1vtier1compatclaim3_wrong",
                clock=lambda: "2026-08-05T16:00:00Z",
            )
        plan_builder.assert_not_called()

    def test_expired_grant_is_not_claimed_after_pause_acquisition(self) -> None:
        session = _CompatSession()

        @contextmanager
        def runtime(*args: object, **kwargs: object) -> Iterator[_CompatSession]:
            yield session

        claim_execution = Mock(return_value=self.claim.claim_id)
        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_compat_operation."
                "paused_av1_validation_v3_tier1_compat_runtime",
                runtime,
            ),
            self.assertRaisesRegex(ValueError, "not active"),
        ):
            run_av1_validation_v3_tier1_compat_probe(
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
                claim_execution=claim_execution,
                clock=lambda: "2026-08-06T09:00:00Z",
            )
        claim_execution.assert_not_called()

    def test_claim_and_result_keep_all_broader_authority_false(self) -> None:
        authority_fields = {
            "tier1_execution_authorized",
            "tier1_coverage_eligible",
            "tier2_execution_authorized",
            "qualification_execution_authorized",
            "evidence_creation_authorized",
            "evidence_eligible",
            "empirical_authority_conferred",
            "derivation_authorized",
            "holdout_authorized",
            "publication_authorized",
            "activation_authorized",
            "public_bundle_activation_allowed",
            "private_inventory_read_authorized",
            "media_library_read_authorized",
            "key_creation_authorized",
            "retry_authorized",
        }
        for payload in (self.claim.to_payload(), self._result().to_payload()):
            for field in authority_fields:
                self.assertIs(payload[field], False, field)
        result_payload = self._result().to_payload()
        self.assertIs(result_payload["single_execution_claimed"], True)

    def test_result_rejects_mismatched_composite_variant_identity(self) -> None:
        payload = self._result().to_payload()
        payload["variants"][0]["representation_id"] = (
            AV1_VALIDATION_V3_TIER1_COMPAT_REPRESENTATION_IDS[1]
        )
        with self.assertRaisesRegex(
            AV1ValidationV3Tier1CompatOperationError,
            "variant binding",
        ):
            av1_validation_v3_tier1_compat_result_from_payload(payload)

    def test_probe_output_root_cannot_contain_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            repository_root = output_root / "repository"
            repository_root.mkdir()
            with self.assertRaisesRegex(
                AV1ValidationV3Tier1CompatProbeError,
                "outside the repository",
            ):
                build_av1_validation_v3_tier1_compat_probe_plans(
                    self.matrix,
                    output_directory=output_root,
                    repository_root=repository_root,
                )

    def _context(self) -> AV1ValidationV3Tier1CompatExecutionContext:
        return AV1ValidationV3Tier1CompatExecutionContext(
            protocol=self.protocol,
            qualification_plan=self.plan,
            request=self.request,
            grant=self.grant,
            as_of="2026-08-05T15:00:00Z",
        )

    def _outcomes(self) -> tuple[AV1ValidationV3Tier1CompatVariantOutcome, ...]:
        return tuple(
            AV1ValidationV3Tier1CompatVariantOutcome(
                variant_id=variant_id,
                representation_id=variant_id.split(":", 1)[0],
                surface_id=variant_id.split(":", 1)[1],
                command_plan_sha256=f"sha256:{index:064x}",
                observation={
                    "width": 1280,
                    "height": 720,
                    "pix_fmt": "yuv420p10le",
                    "color_primaries": "bt709",
                    "color_transfer": "bt709",
                    "color_space": "bt709",
                    "color_range": "limited",
                },
                matches_expected_color_description=True,
                failures=(),
            )
            for index, variant_id in enumerate(AV1_VALIDATION_V3_TIER1_COMPAT_VARIANT_IDS)
        )

    def _result(self) -> AV1ValidationV3Tier1CompatResult:
        session = _CompatSession()

        @contextmanager
        def runtime(*args: object, **kwargs: object) -> Iterator[_CompatSession]:
            yield session

        outcomes = self._outcomes()
        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_compat_operation."
                "paused_av1_validation_v3_tier1_compat_runtime",
                runtime,
            ),
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_compat_operation."
                "build_av1_validation_v3_tier1_compat_probe_plans",
                return_value=(object(), object(), object()),
            ),
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_compat_operation."
                "execute_av1_validation_v3_tier1_compat_representation",
                side_effect=(outcomes[:2], outcomes[2:4], outcomes[4:]),
            ),
        ):
            return run_av1_validation_v3_tier1_compat_probe(
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


class _CompatExecutor:
    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path

    def run(self, args: tuple[str, ...]) -> AV1ValidationV3Tier1CommandResult:
        if args[0] == "ffmpeg":
            self.output_path.write_bytes(b"probe")
            return AV1ValidationV3Tier1CommandResult(0, b"", "")
        collection = "frames" if "frame=width,height" in args[args.index("-show_entries") + 1] else "streams"
        return AV1ValidationV3Tier1CommandResult(
            0,
            (
                '{"%s":[{"width":1280,"height":720,"pix_fmt":"yuv420p10le",'
                '"color_primaries":"bt709","color_transfer":"bt709",'
                '"color_space":"bt709","color_range":"tv"}]}' % collection
            ).encode(),
            "",
        )


class _FailingCompatExecutor:
    def __init__(self, output_path: Path, *, invalid_probe: bool) -> None:
        self.output_path = output_path
        self.invalid_probe = invalid_probe

    def run(self, args: tuple[str, ...]) -> AV1ValidationV3Tier1CommandResult:
        if args[0] == "ffmpeg":
            if self.invalid_probe:
                self.output_path.write_bytes(b"probe")
                return AV1ValidationV3Tier1CommandResult(0, b"", "")
            return AV1ValidationV3Tier1CommandResult(1, b"", "generation failed")
        return AV1ValidationV3Tier1CommandResult(0, b"not-json", "")


class _CompatSession:
    def __init__(self) -> None:
        self.executor = object()
        self.cleanup_passed = True
        self.diagnostics = (
            AV1ValidationV3Tier1CommandDiagnostic(
                program="ffprobe",
                argv_sha256=f"sha256:{'f' * 64}",
                outcome="completed",
                returncode=0,
                stdout_bytes=256,
                stderr_bytes=0,
                stderr_truncated=False,
            ),
        )
