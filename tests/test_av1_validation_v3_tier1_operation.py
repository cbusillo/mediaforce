from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mediaforce.tuning.av1_validation_v3 import load_av1_validation_protocol_v3
from mediaforce.tuning.av1_validation_v3_qualification import (
    build_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_coverage import (
    build_av1_validation_v3_tier1_coverage_attestation,
)
from mediaforce.tuning.av1_validation_v3_tier1_execution_publication import (
    load_published_av1_validation_v3_tier1_execution_grant,
    publish_av1_validation_v3_tier1_coverage_receipt,
    publish_av1_validation_v3_tier1_execution_claim,
    publish_av1_validation_v3_tier1_execution_grant,
)
from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
    AV1ValidationV3Tier1FixtureOutcome,
)
from mediaforce.tuning.av1_validation_v3_tier1_grant import (
    build_av1_validation_v3_tier1_execution_grant,
)
from mediaforce.tuning.av1_validation_v3_tier1_operation import (
    AV1ValidationV3Tier1ExecutionClaim,
    AV1ValidationV3Tier1OperationError,
    build_av1_validation_v3_tier1_execution_claim,
    run_av1_validation_v3_tier1_synthetic_qualification,
)
from mediaforce.tuning.av1_validation_v3_tier1_publication import (
    AV1ValidationV3Tier1PublicationError,
)
from mediaforce.tuning.av1_validation_v3_tier1_request import (
    build_av1_validation_v3_tier1_authorization_request,
)
from scripts import verify_av1_cold_start_preregistration


class AV1ValidationV3Tier1OperationTests(unittest.TestCase):
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
        self.request = build_av1_validation_v3_tier1_authorization_request(
            protocol=self.protocol,
            plan=self.plan,
            requested_at="2026-08-05T13:00:00Z",
            valid_until="2026-08-06T10:00:00Z",
        )
        self.grant = build_av1_validation_v3_tier1_execution_grant(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            owner_principal="owner-1234abcd",
            authorized_at="2026-08-05T14:00:00Z",
            valid_until="2026-08-06T09:00:00Z",
        )

    def test_claim_is_canonical_and_bound_to_one_grant(self) -> None:
        claim = self._claim()

        self.assertTrue(claim.claim_id.startswith("av1vtier1claim3_"))
        self.assertEqual(claim.grant_id, self.grant.grant_id)
        self.assertEqual(
            claim.fixture_matrix_sha256, AV1_VALIDATION_V3_TIER1_MATRIX_SHA256
        )
        self.assertFalse(claim.to_payload()["retry_authorized"])

        with self.assertRaises(AV1ValidationV3Tier1OperationError):
            replace(claim, claimed_at="2026-08-05T15:00:00+00:00")

    def test_owner_artifacts_are_idempotent_and_enforce_one_grant_per_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir(mode=0o700)
            output = root / "artifacts"

            first = publish_av1_validation_v3_tier1_execution_grant(
                grant=self.grant,
                output_root=output,
                repository_root=repository,
            )
            second = publish_av1_validation_v3_tier1_execution_grant(
                grant=self.grant,
                output_root=output,
                repository_root=repository,
            )

            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(
                load_published_av1_validation_v3_tier1_execution_grant(
                    output_root=output,
                    repository_root=repository,
                    request_id=self.request.request_id,
                ),
                self.grant,
            )

            replacement = build_av1_validation_v3_tier1_execution_grant(
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                owner_principal="owner-1234abcd",
                authorized_at="2026-08-05T14:01:00Z",
                valid_until="2026-08-06T09:00:00Z",
            )
            with self.assertRaises(AV1ValidationV3Tier1PublicationError):
                publish_av1_validation_v3_tier1_execution_grant(
                    grant=replacement,
                    output_root=output,
                    repository_root=repository,
                )

    def test_claim_and_receipt_are_published_once_per_grant(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir(mode=0o700)
            output = root / "artifacts"
            claim = self._claim()
            attestation = build_av1_validation_v3_tier1_coverage_attestation(
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                grant=self.grant,
                outcomes=self._outcomes(),
                cleanup_passed=True,
                runtime_paused=True,
                completed_at="2026-08-05T16:00:00Z",
            )

            first_claim = publish_av1_validation_v3_tier1_execution_claim(
                claim=claim,
                output_root=output,
                repository_root=repository,
            )
            second_claim = publish_av1_validation_v3_tier1_execution_claim(
                claim=claim,
                output_root=output,
                repository_root=repository,
            )
            first_receipt = publish_av1_validation_v3_tier1_coverage_receipt(
                attestation=attestation,
                output_root=output,
                repository_root=repository,
            )
            second_receipt = publish_av1_validation_v3_tier1_coverage_receipt(
                attestation=attestation,
                output_root=output,
                repository_root=repository,
            )

            self.assertTrue(first_claim.created)
            self.assertFalse(second_claim.created)
            self.assertTrue(first_receipt.created)
            self.assertFalse(second_receipt.created)

            later_claim = build_av1_validation_v3_tier1_execution_claim(
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                grant=self.grant,
                claimed_at="2026-08-05T15:01:00Z",
            )
            with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
                publish_av1_validation_v3_tier1_execution_claim(
                    claim=later_claim,
                    output_root=output,
                    repository_root=repository,
                )
            self.assertEqual(
                raised.exception.reason_code,
                "execution_already_claimed",
            )

    def test_claim_publication_preserves_non_conflict_safety_failures(self) -> None:
        with patch(
            "mediaforce.tuning.av1_validation_v3_tier1_execution_publication."
            "publish_av1_validation_v3_owner_artifact",
            side_effect=AV1ValidationV3Tier1PublicationError(
                "artifact_unsafe",
                "unsafe claim target",
            ),
        ):
            with self.assertRaises(AV1ValidationV3Tier1PublicationError) as raised:
                publish_av1_validation_v3_tier1_execution_claim(
                    claim=self._claim(),
                    output_root=Path("/execution"),
                    repository_root=Path("/repository"),
                )
        self.assertEqual(raised.exception.reason_code, "artifact_unsafe")

    def test_operation_runs_each_fixture_once_and_builds_receipt_after_cleanup(
        self,
    ) -> None:
        session = _FakeSession(cleanup_passed=True)
        fixture_ids: list[str] = []
        events: list[str] = []
        checked_at: list[str] = []
        claim = self._claim()

        @contextmanager
        def fake_runtime(*args: object, **kwargs: object) -> Iterator[None]:
            events.append("lock")
            yield session

        def fake_execute(
            fixture_id: str, **kwargs: object
        ) -> AV1ValidationV3Tier1FixtureOutcome:
            events.append(fixture_id)
            fixture_ids.append(fixture_id)
            checked_at.append(kwargs["context"].as_of)
            return self._outcomes()[len(fixture_ids) - 1]

        times = iter(
            (
                "2026-08-05T15:01:00Z",
                "2026-08-05T15:02:00Z",
                "2026-08-05T15:03:00Z",
                "2026-08-05T15:04:00Z",
                "2026-08-05T16:00:00Z",
            )
        )
        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_operation."
                "paused_av1_validation_v3_tier1_runtime",
                fake_runtime,
            ),
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_operation."
                "execute_av1_validation_v3_tier1_fixture",
                fake_execute,
            ),
        ):
            result = run_av1_validation_v3_tier1_synthetic_qualification(
                object(),
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                grant=self.grant,
                config_snapshot_bytes=b"{}\n",
                matrix={},
                output_directory=Path("/tmp"),
                repository_root=Path("/repository"),
                toolchain=object(),
                claim=claim,
                claim_execution=lambda: (
                    events.append("claim") or claim.claim_id
                ),
                clock=lambda: next(times),
            )

        self.assertEqual(events[:2], ["lock", "claim"])
        self.assertEqual(
            fixture_ids, [outcome.fixture_id for outcome in self._outcomes()]
        )
        self.assertEqual(
            checked_at,
            [
                "2026-08-05T15:01:00Z",
                "2026-08-05T15:02:00Z",
                "2026-08-05T15:03:00Z",
                "2026-08-05T15:04:00Z",
            ],
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.attestation.completed_at, "2026-08-05T16:00:00Z")

    def test_operation_refuses_receipt_when_cleanup_fails(self) -> None:
        session = _FakeSession(cleanup_passed=False)
        claim = self._claim()

        @contextmanager
        def fake_runtime(*args: object, **kwargs: object) -> Iterator[None]:
            yield session

        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_operation."
                "paused_av1_validation_v3_tier1_runtime",
                fake_runtime,
            ),
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_operation."
                "execute_av1_validation_v3_tier1_fixture",
                side_effect=self._outcomes(),
            ),
            self.assertRaisesRegex(AV1ValidationV3Tier1OperationError, "cleanup"),
        ):
            run_av1_validation_v3_tier1_synthetic_qualification(
                object(),
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                grant=self.grant,
                config_snapshot_bytes=b"{}\n",
                matrix={},
                output_directory=Path("/tmp"),
                repository_root=Path("/repository"),
                toolchain=object(),
                claim=claim,
                claim_execution=lambda: claim.claim_id,
                clock=lambda: "2026-08-05T15:01:00Z",
            )

    def test_operation_retains_claim_and_stops_after_fixture_exception(self) -> None:
        session = _FakeSession(cleanup_passed=True)
        events: list[str] = []
        claim = self._claim()

        @contextmanager
        def fake_runtime(*args: object, **kwargs: object) -> Iterator[None]:
            events.append("lock")
            yield session

        def fail_fixture(
            fixture_id: str,
            **kwargs: object,
        ) -> AV1ValidationV3Tier1FixtureOutcome:
            events.append(fixture_id)
            raise RuntimeError("fixture interrupted")

        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_operation."
                "paused_av1_validation_v3_tier1_runtime",
                fake_runtime,
            ),
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_operation."
                "execute_av1_validation_v3_tier1_fixture",
                fail_fixture,
            ),
            self.assertRaisesRegex(RuntimeError, "interrupted"),
        ):
            run_av1_validation_v3_tier1_synthetic_qualification(
                object(),
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                grant=self.grant,
                config_snapshot_bytes=b"{}\n",
                matrix={},
                output_directory=Path("/tmp"),
                repository_root=Path("/repository"),
                toolchain=object(),
                claim=claim,
                claim_execution=lambda: (
                    events.append("claim") or claim.claim_id
                ),
                clock=lambda: "2026-08-05T15:01:00Z",
            )

        self.assertEqual(events, ["lock", "claim", "tier1_flat_field"])

    def test_operation_refuses_unverified_claim_publication(self) -> None:
        session = _FakeSession(cleanup_passed=True)
        claim = self._claim()

        @contextmanager
        def fake_runtime(*args: object, **kwargs: object) -> Iterator[None]:
            yield session

        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier1_operation."
                "paused_av1_validation_v3_tier1_runtime",
                fake_runtime,
            ),
            self.assertRaisesRegex(
                AV1ValidationV3Tier1OperationError,
                "was not published",
            ),
        ):
            run_av1_validation_v3_tier1_synthetic_qualification(
                object(),
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                grant=self.grant,
                config_snapshot_bytes=b"{}\n",
                matrix={},
                output_directory=Path("/tmp"),
                repository_root=Path("/repository"),
                toolchain=object(),
                claim=claim,
                claim_execution=lambda: "wrong-claim",
                clock=lambda: "2026-08-05T15:01:00Z",
            )

    def test_operation_rejects_claim_from_a_different_grant(self) -> None:
        other_grant = build_av1_validation_v3_tier1_execution_grant(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            owner_principal="owner-1234abcd",
            authorized_at="2026-08-05T14:01:00Z",
            valid_until="2026-08-06T09:00:00Z",
        )
        other_claim = build_av1_validation_v3_tier1_execution_claim(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            grant=other_grant,
            claimed_at="2026-08-05T15:00:00Z",
        )
        with self.assertRaisesRegex(
            AV1ValidationV3Tier1OperationError,
            "not bound",
        ):
            run_av1_validation_v3_tier1_synthetic_qualification(
                object(),
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                grant=self.grant,
                config_snapshot_bytes=b"{}\n",
                matrix={},
                output_directory=Path("/tmp"),
                repository_root=Path("/repository"),
                toolchain=object(),
                claim=other_claim,
                claim_execution=lambda: other_claim.claim_id,
                clock=lambda: "2026-08-05T15:01:00Z",
            )

    def test_runner_reports_existing_claim_as_consumed_without_retry(self) -> None:
        args = SimpleNamespace(
            claimed_at="2026-08-05T15:00:00Z",
            matrix=Path("matrix.json"),
            grant_root=Path("/grant"),
            execution_output_root=Path("/execution"),
            fixture_output_root=Path("/fixtures"),
            json_output=True,
        )
        claim = SimpleNamespace(claim_id="av1vtier1claim3_test")
        request = SimpleNamespace(request_id="av1vtier1request3_test")
        grant = SimpleNamespace(grant_id="av1vtier1grant3_test")

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
                return_value=args.claimed_at,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_tier1_execution_inputs",
                return_value=(object(), object(), request, object(), b"{}\n", object()),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_av1_validation_v3_tier1_fixture_matrix",
                return_value={},
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_published_av1_validation_v3_tier1_execution_grant",
                return_value=grant,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "build_av1_validation_v3_tier1_execution_claim",
                return_value=claim,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "publish_av1_validation_v3_tier1_execution_claim",
                side_effect=AV1ValidationV3Tier1PublicationError(
                    "execution_already_claimed",
                    "claim consumed",
                ),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_av1_validation_v3_tier1_synthetic_qualification",
                side_effect=invoke_claim,
            ),
            redirect_stdout(stdout),
        ):
            exit_code = verify_av1_cold_start_preregistration._run_tier1_synthetic_qualification(
                args
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["failure_reason"], "execution_already_claimed")
        self.assertTrue(payload["single_execution_claimed"])
        self.assertFalse(payload["retry_authorized"])

    def test_runner_reports_lease_loss_after_claim_without_receipt(self) -> None:
        args = SimpleNamespace(
            matrix=Path("matrix.json"),
            grant_root=Path("/grant"),
            execution_output_root=Path("/execution"),
            fixture_output_root=Path("/fixtures"),
            json_output=True,
        )
        claim = SimpleNamespace(claim_id="av1vtier1claim3_test")
        request = SimpleNamespace(request_id="av1vtier1request3_test")
        grant = SimpleNamespace(grant_id="av1vtier1grant3_test")

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
                return_value="2026-08-05T15:00:00Z",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_load_tier1_execution_inputs",
                return_value=(object(), object(), request, object(), b"{}\n", object()),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_av1_validation_v3_tier1_fixture_matrix",
                return_value={},
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "load_published_av1_validation_v3_tier1_execution_grant",
                return_value=grant,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "build_av1_validation_v3_tier1_execution_claim",
                return_value=claim,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "publish_av1_validation_v3_tier1_execution_claim",
                return_value=SimpleNamespace(created=True),
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "run_av1_validation_v3_tier1_synthetic_qualification",
                side_effect=lose_lease,
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "publish_av1_validation_v3_tier1_coverage_receipt",
            ) as receipt_publisher,
            redirect_stdout(stdout),
        ):
            exit_code = verify_av1_cold_start_preregistration._run_tier1_synthetic_qualification(
                args
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["failure_reason"], "execution_failed")
        self.assertTrue(payload["single_execution_claimed"])
        self.assertFalse(payload["retry_authorized"])
        receipt_publisher.assert_not_called()

    def _claim(self) -> AV1ValidationV3Tier1ExecutionClaim:
        return build_av1_validation_v3_tier1_execution_claim(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            grant=self.grant,
            claimed_at="2026-08-05T15:00:00Z",
        )

    def _outcomes(self) -> tuple[AV1ValidationV3Tier1FixtureOutcome, ...]:
        fixture_ids = (
            "tier1_flat_field",
            "tier1_high_detail_noise",
            "tier1_high_motion",
            "tier1_scene_change",
        )
        return tuple(
            AV1ValidationV3Tier1FixtureOutcome(
                fixture_id=fixture_id,
                matrix_sha256=AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
                request_id=self.request.request_id,
                grant_id=self.grant.grant_id,
                repository_commit=self.plan.repository_commit,
                toolchain_sha256=self.plan.toolchain_sha256,
                command_plan_sha256=f"sha256:{index:064x}",
                content_sha256=f"sha256:{index + 10:064x}",
                content_byte_count=796_262_400,
                passed=True,
                failures=(),
                observation={},
            )
            for index, fixture_id in enumerate(fixture_ids)
        )


class _FakeSession:
    def __init__(self, *, cleanup_passed: bool) -> None:
        self.executor = object()
        self.cleanup_passed = cleanup_passed
        self.diagnostics = ()
