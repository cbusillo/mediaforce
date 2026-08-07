from __future__ import annotations

import copy
import json
import os
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import threading
import unittest

from mediaforce.tuning.av1_validation_v4 import AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
from mediaforce.tuning.av1_validation_v4_freeze import (
    build_av1_validation_v4_manifest_freeze,
)
from mediaforce.tuning.av1_validation_v4_preparation_grant import (
    load_av1_validation_v4_preparation_grant,
)
from mediaforce.tuning.av1_validation_v4_preparation_config import (
    load_av1_validation_v4_effective_config_snapshot,
)
from mediaforce.tuning.av1_validation_v4_preparation_operation import (
    run_av1_validation_v4_preparation_operation,
)
from mediaforce.tuning.av1_validation_v4_qualification_authority import (
    AV1_VALIDATION_V4_QUALIFICATION_GRANT_AUTHORITY,
    AV1ValidationV4QualificationAuthorityError,
    _QUALIFICATION_GRANT_AUTHORIZED_FIELDS,
    _QUALIFICATION_GRANT_FALSE_FIELDS,
    assert_av1_validation_v4_qualification_claim,
    assert_av1_validation_v4_qualification_grant,
    assert_av1_validation_v4_qualification_grant_active,
    assert_av1_validation_v4_qualification_request,
    assert_av1_validation_v4_qualification_claim_custody,
    av1_validation_v4_qualification_claim_id,
    av1_validation_v4_qualification_grant_id,
    av1_validation_v4_qualification_request_id,
    build_av1_validation_v4_qualification_claim,
    build_av1_validation_v4_qualification_grant,
    build_av1_validation_v4_qualification_request,
    consume_av1_validation_v4_qualification_grant,
    load_av1_validation_v4_qualification_claim,
    load_av1_validation_v4_qualification_grant,
    load_av1_validation_v4_qualification_request,
    serialize_av1_validation_v4_qualification_claim,
    serialize_av1_validation_v4_qualification_grant,
    serialize_av1_validation_v4_qualification_request,
)
from mediaforce.tuning.av1_validation_v4_qualification_runner import (
    AV1ValidationV4TraversalCallbackResult,
    AV1ValidationV4TraversalSpec,
    AV1ValidationV4QualificationRunnerError,
    av1_validation_v4_traversal_specs_from_request,
    run_av1_validation_v4_qualification,
)
from tests import test_av1_validation_v4_preparation as preparation_fixture


class AV1ValidationV4QualificationAuthorityTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # Request
    # ------------------------------------------------------------------

    def test_request_is_canonical_and_non_authorizing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            request = self._request(bundle)
            self.assertEqual(request, self._request(bundle))
            self.assertEqual(request["state"], "owner_submitted")
            self.assertEqual(request["contract_version"], "av1v4qreq1")
            self.assertTrue(request["request_id"].startswith("av1vqualreq4_"))
            self.assertEqual(len(request["invocations"]), 8)
            for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                with self.subTest(field=field):
                    self.assertIs(request[field], False)

    def test_request_binds_freeze_and_preparation_identities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            freeze = bundle["freeze"]
            request = self._request(bundle)
            self.assertEqual(request["freeze_id"], freeze["freeze_id"])
            self.assertEqual(request["freeze_payload_sha256"], freeze["payload_sha256"])
            self.assertEqual(request["preparation_id"], freeze["preparation_id"])
            self.assertEqual(
                request["preparation_payload_sha256"],
                freeze["preparation_payload_sha256"],
            )
            self.assertEqual(request["runtime_compatibility_id"], freeze["runtime_compatibility_id"])
            self.assertEqual(request["path_privacy_key_id"], freeze["path_privacy_key_id"])
            self.assertEqual(request["config_id"], freeze["config_id"])
            self.assertEqual(
                request["config_canonical_file_sha256"],
                freeze["config_canonical_file_sha256"],
            )
            # preparation_repository = freeze.reviewed_repository
            self.assertEqual(
                request["preparation_repository"],
                freeze["reviewed_repository"],
            )

    def test_request_execution_repository_is_a_separate_bound_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            freeze = bundle["freeze"]
            materializer = freeze["materializer_repository"]
            # Confirm execution_repository (from _request default) differs
            request = self._request(bundle)
            exec_repo = request["execution_repository"]
            self.assertNotEqual(exec_repo["commit"], materializer["commit"])
            self.assertNotEqual(exec_repo["tree"], materializer["tree"])
            same_identity = build_av1_validation_v4_qualification_request(
                freeze=freeze,
                preparation=bundle["preparation"],
                owner_principal="owner:test",
                execution_repository_commit=materializer["commit"],
                execution_repository_tree=materializer["tree"],
                consumption_registry="/registry/av1-v4-qualification",
                requested_at="2026-08-07T07:00:00Z",
                valid_until="2026-08-07T08:00:00Z",
            )
            self.assertEqual(same_identity["execution_repository"], materializer)

    def test_request_rejects_substituted_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            altered = copy.deepcopy(bundle["preparation"])
            # Tamper with preparation_id to break digest consistency
            altered["preparation_id"] = "av1vprep4_" + "f" * 32
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "not self-consistent",
            ):
                build_av1_validation_v4_qualification_request(
                    freeze=bundle["freeze"],
                    preparation=altered,
                    owner_principal="owner:test",
                    execution_repository_commit="b" * 40,
                    execution_repository_tree="c" * 40,
                    consumption_registry="/registry/av1-v4-qualification",
                    requested_at="2026-08-07T07:00:00Z",
                    valid_until="2026-08-07T08:00:00Z",
                )

    def test_request_rejects_private_path_in_owner_principal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "owner principal is invalid",
            ):
                build_av1_validation_v4_qualification_request(
                    freeze=bundle["freeze"],
                    preparation=bundle["preparation"],
                    owner_principal="/Users/private/owner",
                    execution_repository_commit="b" * 40,
                    execution_repository_tree="c" * 40,
                    consumption_registry="/registry/av1-v4-qualification",
                    requested_at="2026-08-07T07:00:00Z",
                    valid_until="2026-08-07T08:00:00Z",
                )

    def test_request_round_trip_requires_canonical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            request = self._request(bundle)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "request.json"
                path.write_bytes(serialize_av1_validation_v4_qualification_request(request))
                loaded = load_av1_validation_v4_qualification_request(path)
                self.assertEqual(loaded, request)
                path.write_text("{}\n")
                with self.assertRaises(AV1ValidationV4QualificationAuthorityError):
                    load_av1_validation_v4_qualification_request(path)

    # ------------------------------------------------------------------
    # Grant
    # ------------------------------------------------------------------

    def test_grant_authorizes_exactly_the_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            grant = self._grant(bundle)
            self.assertEqual(grant["state"], "owner_authorized")
            self.assertEqual(grant["contract_version"], "av1v4qgrant1")
            self.assertEqual(grant["authority"], AV1_VALIDATION_V4_QUALIFICATION_GRANT_AUTHORITY)
            self.assertTrue(grant["grant_id"].startswith("av1vqualgrant4_"))
            # Exactly three fields are True
            for field in _QUALIFICATION_GRANT_AUTHORIZED_FIELDS:
                with self.subTest(field=field):
                    self.assertIs(grant[field], True)
            # All remaining false-authority fields stay False
            for field in _QUALIFICATION_GRANT_FALSE_FIELDS:
                with self.subTest(field=field):
                    self.assertIs(grant[field], False)

    def test_grant_scope_is_one_run_wide_and_non_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            grant = self._grant(bundle)
            scope = grant["grant_scope"]
            self.assertEqual(scope["run_scope"], "one_run_wide")
            self.assertTrue(scope["single_use_authorized"])
            self.assertTrue(scope["owner_authorization_confirmed"])
            self.assertFalse(scope["resume_run_authorized"])
            self.assertFalse(scope["substitute_source_authorized"])
            self.assertFalse(scope["backfill_authorized_scope"])
            self.assertFalse(scope["adaptive_continuation_authorized"])
            self.assertEqual(scope["traversal_count"], 8)

    def test_grant_window_active_only_inside_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            grant = self._grant(bundle)
            assert_av1_validation_v4_qualification_grant_active(
                grant,
                as_of="2026-08-07T07:30:00Z",
            )
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "not active",
            ):
                assert_av1_validation_v4_qualification_grant_active(
                    grant,
                    as_of="2026-08-07T09:00:00Z",
                )
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "not active",
            ):
                assert_av1_validation_v4_qualification_grant_active(
                    grant,
                    as_of="2026-08-07T06:59:59Z",
                )

    def test_grant_exceeds_maximum_duration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            request = self._request(bundle, valid_until="2026-08-07T13:00:00Z")
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "maximum duration",
            ):
                build_av1_validation_v4_qualification_grant(
                    request=request,
                    owner_principal="owner:test",
                    authorized_at="2026-08-07T07:00:00Z",
                    valid_until="2026-08-07T11:00:01Z",
                )

    def test_grant_owner_must_match_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            request = self._request(bundle)
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "owner must match",
            ):
                build_av1_validation_v4_qualification_grant(
                    request=request,
                    owner_principal="owner:other",
                    authorized_at="2026-08-07T07:00:00Z",
                    valid_until="2026-08-07T08:00:00Z",
                )

    def test_grant_mutations_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            grant = self._grant(bundle)
            # Cannot set authorized field to False
            bad_false = copy.deepcopy(grant)
            bad_false["qualification_execution_authorized"] = False
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "must authorize qualification_execution_authorized",
            ):
                assert_av1_validation_v4_qualification_grant(bad_false)
            # Cannot set false field to True
            bad_true = copy.deepcopy(grant)
            bad_true["evidence_creation_authorized"] = True
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "cannot authorize evidence_creation_authorized",
            ):
                assert_av1_validation_v4_qualification_grant(bad_true)
            # Cannot add unknown fields
            unknown = copy.deepcopy(grant)
            unknown["custom_authority"] = True
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "unknown fields",
            ):
                assert_av1_validation_v4_qualification_grant(unknown)
            # Scope mutation rejected
            bad_scope = copy.deepcopy(grant)
            bad_scope["grant_scope"]["run_scope"] = "multi_run"
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "binding is invalid",
            ):
                assert_av1_validation_v4_qualification_grant(bad_scope)

    def test_grant_round_trip_requires_canonical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            grant = self._grant(bundle)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "grant.json"
                path.write_bytes(serialize_av1_validation_v4_qualification_grant(grant))
                loaded = load_av1_validation_v4_qualification_grant(path)
                self.assertEqual(loaded, grant)
                path.write_text("{}\n")
                with self.assertRaises(AV1ValidationV4QualificationAuthorityError):
                    load_av1_validation_v4_qualification_grant(path)

    # ------------------------------------------------------------------
    # Claim
    # ------------------------------------------------------------------

    def test_claim_is_non_authorizing_and_binds_request_and_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            request = self._request(bundle)
            grant = build_av1_validation_v4_qualification_grant(
                request=request,
                owner_principal="owner:test",
                authorized_at="2026-08-07T07:00:00Z",
                valid_until="2026-08-07T08:00:00Z",
            )
            claim = build_av1_validation_v4_qualification_claim(
                request=request,
                grant=grant,
                claimed_at="2026-08-07T07:01:00Z",
            )
            self.assertEqual(claim["state"], "grant_consumed")
            self.assertEqual(claim["contract_version"], "av1v4qclaim1")
            self.assertTrue(claim["claim_id"].startswith("av1vqualclaim4_"))
            self.assertEqual(claim["request_id"], request["request_id"])
            self.assertEqual(claim["request_payload_sha256"], request["payload_sha256"])
            self.assertEqual(claim["grant_id"], grant["grant_id"])
            self.assertEqual(claim["grant_payload_sha256"], grant["payload_sha256"])
            for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                with self.subTest(field=field):
                    self.assertIs(claim[field], False)

    def test_claim_requires_active_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            grant = self._grant(bundle)
            request = self._request(bundle)
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "not active",
            ):
                build_av1_validation_v4_qualification_claim(
                    request=request,
                    grant=grant,
                    claimed_at="2026-08-07T09:00:00Z",  # after grant expiry
                )

    def test_claim_rejects_mismatched_grant_request_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            grant = self._grant(bundle)
            # Build a second request with different execution_repository
            request2 = build_av1_validation_v4_qualification_request(
                freeze=bundle["freeze"],
                preparation=bundle["preparation"],
                owner_principal="owner:test",
                execution_repository_commit="d" * 40,
                execution_repository_tree="e" * 40,
                consumption_registry="/registry/av1-v4-qualification",
                requested_at="2026-08-07T07:00:00Z",
                valid_until="2026-08-07T08:00:00Z",
            )
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "binding does not match",
            ):
                build_av1_validation_v4_qualification_claim(
                    request=request2,
                    grant=grant,
                    claimed_at="2026-08-07T07:01:00Z",
                )

    def test_claim_round_trip_requires_canonical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            grant = self._grant(bundle)
            request = self._request(bundle)
            claim = build_av1_validation_v4_qualification_claim(
                request=request,
                grant=grant,
                claimed_at="2026-08-07T07:01:00Z",
            )
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "claim.json"
                path.write_bytes(serialize_av1_validation_v4_qualification_claim(claim))
                loaded = load_av1_validation_v4_qualification_claim(path)
                self.assertEqual(loaded, claim)
                path.write_text("{}\n")
                with self.assertRaises(AV1ValidationV4QualificationAuthorityError):
                    load_av1_validation_v4_qualification_claim(path)

    # ------------------------------------------------------------------
    # Claim registry (consume_av1_validation_v4_qualification_grant)
    # ------------------------------------------------------------------

    def test_claim_is_written_exclusive_with_custody(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            registry = Path(directory) / "qualification-registry"
            registry.mkdir(mode=0o700)
            grant = self._grant(bundle)
            request = self._request(bundle)
            claim = consume_av1_validation_v4_qualification_grant(
                registry=registry,
                request=request,
                grant=grant,
                claimed_at="2026-08-07T07:01:00Z",
            )
            grant_id = grant["grant_id"]
            claim_path = registry / f"{grant_id}.claim.json"
            self.assertTrue(claim_path.exists())
            mode = claim_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)
            self.assertEqual(claim_path.stat().st_nlink, 1)
            assert_av1_validation_v4_qualification_claim_custody(claim_path)
            loaded = load_av1_validation_v4_qualification_claim(claim_path)
            self.assertEqual(loaded, claim)

    def test_double_spend_claim_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            registry = Path(directory) / "qualification-registry"
            registry.mkdir(mode=0o700)
            grant = self._grant(bundle)
            request = self._request(bundle)
            consume_av1_validation_v4_qualification_grant(
                registry=registry,
                request=request,
                grant=grant,
                claimed_at="2026-08-07T07:01:00Z",
            )
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "already exists",
            ):
                consume_av1_validation_v4_qualification_grant(
                    registry=registry,
                    request=request,
                    grant=grant,
                    claimed_at="2026-08-07T07:02:00Z",
                )

    def test_concurrent_claim_race_only_one_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            registry = Path(directory) / "qualification-registry"
            registry.mkdir(mode=0o700)
            grant = self._grant(bundle)
            request = self._request(bundle)
            results: list[object] = []
            errors: list[object] = []

            def attempt() -> None:
                try:
                    claim = consume_av1_validation_v4_qualification_grant(
                        registry=registry,
                        request=request,
                        grant=grant,
                        claimed_at="2026-08-07T07:01:00Z",
                    )
                    results.append(claim)
                except AV1ValidationV4QualificationAuthorityError as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=attempt) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # Exactly one thread must have succeeded
            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 3)

    def test_symlinked_registry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "registry-target"
            target.mkdir(mode=0o700)
            link = Path(directory) / "registry-link"
            link.symlink_to(target, target_is_directory=True)
            bundle = self._bundle(Path(directory))
            request = self._request(bundle, consumption_registry=str(link))
            grant = build_av1_validation_v4_qualification_grant(
                request=request,
                owner_principal="owner:test",
                authorized_at="2026-08-07T07:00:00Z",
                valid_until="2026-08-07T08:00:00Z",
            )
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "owner-only real directory",
            ):
                consume_av1_validation_v4_qualification_grant(
                    registry=link,
                    request=request,
                    grant=grant,
                    claimed_at="2026-08-07T07:01:00Z",
                )

    # ------------------------------------------------------------------
    # Runner
    # ------------------------------------------------------------------

    def test_runner_sequential_eight_success_all_traversals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            grant = self._grant(bundle)
            request = self._request(bundle)
            registry = self._consume_claim(bundle, request, grant)
            specs = av1_validation_v4_traversal_specs_from_request(request)
            self.assertEqual(len(specs), 8)
            invoked_ordinals: list[int] = []

            def cb(spec: AV1ValidationV4TraversalSpec) -> AV1ValidationV4TraversalCallbackResult:
                invoked_ordinals.append(spec.ordinal)
                return AV1ValidationV4TraversalCallbackResult(
                    success=True,
                    public_summary={"traversal": spec.ordinal},
                )

            outcome = run_av1_validation_v4_qualification(
                grant=grant,
                request=request,
                registry=registry,
                callback=cb,
            )
            self.assertEqual(invoked_ordinals, list(range(1, 9)))
            self.assertEqual(outcome["outcome"], "success_all_traversals")
            self.assertIsNone(outcome["failed_at_ordinal"])
            self.assertEqual(outcome["traversals_attempted"], 8)
            self.assertEqual(len(outcome["traversal_outcomes"]), 8)
            for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                with self.subTest(field=field):
                    self.assertIs(outcome[field], False)

    def test_runner_first_failure_stops_later_ordinals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            grant = self._grant(bundle)
            request = self._request(bundle)
            registry = self._consume_claim(bundle, request, grant)
            invoked_ordinals: list[int] = []

            def cb(spec: AV1ValidationV4TraversalSpec) -> AV1ValidationV4TraversalCallbackResult:
                invoked_ordinals.append(spec.ordinal)
                success = spec.ordinal < 3  # fail at ordinal 3
                return AV1ValidationV4TraversalCallbackResult(
                    success=success,
                    public_summary={"traversal": spec.ordinal, "ok": success},
                )

            outcome = run_av1_validation_v4_qualification(
                grant=grant,
                request=request,
                registry=registry,
                callback=cb,
            )
            # Only ordinals 1, 2, 3 were invoked (3 failed)
            self.assertEqual(invoked_ordinals, [1, 2, 3])
            self.assertEqual(outcome["outcome"], "failed_partial_non_comparable")
            self.assertEqual(outcome["failed_at_ordinal"], 3)
            self.assertEqual(outcome["traversals_attempted"], 3)
            self.assertEqual(len(outcome["traversal_outcomes"]), 3)
            # All false-authority fields must be False even in failure outcome
            for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                with self.subTest(field=field):
                    self.assertIs(outcome[field], False)
            # Each traversal outcome also has all false-authority fields False
            for traversal_outcome in outcome["traversal_outcomes"]:
                for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                    with self.subTest(field=field, ordinal=traversal_outcome["ordinal"]):
                        self.assertIs(traversal_outcome[field], False)

    def test_runner_replay_is_rejected_before_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            request = self._request(bundle)
            grant = self._grant(bundle)
            registry = self._consume_claim(bundle, request, grant)
            callback_count = 0

            def cb(_spec: AV1ValidationV4TraversalSpec) -> AV1ValidationV4TraversalCallbackResult:
                nonlocal callback_count
                callback_count += 1
                return AV1ValidationV4TraversalCallbackResult(success=True, public_summary={})

            run_av1_validation_v4_qualification(
                request=request,
                grant=grant,
                registry=registry,
                callback=cb,
            )
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationRunnerError,
                "already started",
            ):
                run_av1_validation_v4_qualification(
                    request=request,
                    grant=grant,
                    registry=registry,
                    callback=cb,
                )
            self.assertEqual(callback_count, 8)

    def test_runner_rejects_private_or_authority_callback_summaries(self) -> None:
        summaries = (
            {"artifacts": ("/Users/private/source.mkv",)},
            {"/Volumes/private/source.mkv": 3},
            {"nested": {"media_read_authorized": True}},
        )
        for summary in summaries:
            with self.subTest(summary=summary), tempfile.TemporaryDirectory() as directory:
                bundle = self._bundle(Path(directory))
                request = self._request(bundle)
                grant = self._grant(bundle)
                registry = self._consume_claim(bundle, request, grant)
                with self.assertRaises(AV1ValidationV4QualificationRunnerError):
                    run_av1_validation_v4_qualification(
                        request=request,
                        grant=grant,
                        registry=registry,
                        callback=lambda _spec, value=summary: AV1ValidationV4TraversalCallbackResult(
                            success=True,
                            public_summary=value,
                        ),
                    )

    def test_loaded_request_rejects_wrong_mode_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = self._request(self._bundle(Path(directory)))
            mutated = copy.deepcopy(request)
            mutated["invocations"][0]["mode"] = "guided"
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "coverage",
            ):
                assert_av1_validation_v4_qualification_request(mutated)

    def test_runner_immediate_first_failure_is_failed_partial_non_comparable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            grant = self._grant(bundle)
            request = self._request(bundle)
            registry = self._consume_claim(bundle, request, grant)
            invoked_ordinals: list[int] = []

            def cb(spec: AV1ValidationV4TraversalSpec) -> AV1ValidationV4TraversalCallbackResult:
                invoked_ordinals.append(spec.ordinal)
                return AV1ValidationV4TraversalCallbackResult(
                    success=False,
                    public_summary={"error": "simulated failure"},
                )

            outcome = run_av1_validation_v4_qualification(
                grant=grant,
                request=request,
                registry=registry,
                callback=cb,
            )
            self.assertEqual(invoked_ordinals, [1])
            self.assertEqual(outcome["outcome"], "failed_partial_non_comparable")
            self.assertEqual(outcome["failed_at_ordinal"], 1)
            self.assertEqual(outcome["traversals_attempted"], 1)

    def test_runner_rejects_missing_consumed_claim_before_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            grant = self._grant(bundle)
            request = self._request(bundle)
            registry = Path(request["consumption_registry"])
            registry.mkdir(mode=0o700)
            invoked = False

            def cb(_spec: AV1ValidationV4TraversalSpec) -> AV1ValidationV4TraversalCallbackResult:
                nonlocal invoked
                invoked = True
                return AV1ValidationV4TraversalCallbackResult(success=True, public_summary={})

            with self.assertRaisesRegex(
                AV1ValidationV4QualificationRunnerError,
                "consumed registry claim",
            ):
                run_av1_validation_v4_qualification(
                    request=request,
                    grant=grant,
                    registry=registry,
                    callback=cb,
                )
            self.assertFalse(invoked)

    def test_runner_rejects_claim_that_does_not_bind_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            grant = self._grant(bundle)
            request = self._request(bundle)
            registry = self._consume_claim(bundle, request, grant)
            # Build a second grant with different window (different grant_id)
            grant2 = build_av1_validation_v4_qualification_grant(
                request=request,
                owner_principal="owner:test",
                authorized_at="2026-08-07T07:00:00Z",
                valid_until="2026-08-07T07:30:00Z",
            )
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationRunnerError,
                "consumed registry claim",
            ):
                run_av1_validation_v4_qualification(
                    request=request,
                    grant=grant2,
                    registry=registry,
                    callback=lambda s: AV1ValidationV4TraversalCallbackResult(
                        success=True, public_summary={}
                    ),
                )

    def test_runner_canonical_bytes_in_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            grant = self._grant(bundle)
            request = self._request(bundle)
            registry = self._consume_claim(bundle, request, grant)
            outcome = run_av1_validation_v4_qualification(
                request=request,
                grant=grant,
                registry=registry,
                callback=lambda s: AV1ValidationV4TraversalCallbackResult(
                    success=True, public_summary={"ordinal": s.ordinal}
                ),
            )
            # The run outcome must be round-trippable through canonical JSON
            raw = json.dumps(outcome, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            reloaded = json.loads(raw)
            self.assertEqual(reloaded, outcome)

    def test_no_media_interactions_in_any_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            request = self._request(bundle)
            grant = self._grant(bundle)
            claim = build_av1_validation_v4_qualification_claim(
                request=request,
                grant=grant,
                claimed_at="2026-08-07T07:01:00Z",
            )
            for name, artifact in (("request", request), ("grant", grant), ("claim", claim)):
                dumped = json.dumps(artifact)
                with self.subTest(artifact=name):
                    self.assertNotIn("/Users/", dumped)
                    self.assertNotIn("/Volumes/", dumped)
                    self.assertNotIn("/home/", dumped)
                    self.assertNotIn("missing-media", dumped)

    def test_path_disclosure_in_artifacts_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(Path(directory))
            request = self._request(bundle)
            bad_request = copy.deepcopy(request)
            bad_request["owner_principal"] = "/Volumes/private"
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "machine-local",
            ):
                assert_av1_validation_v4_qualification_request(bad_request)
            bad_grant = self._grant(bundle)
            bad_grant_copy = copy.deepcopy(bad_grant)
            bad_grant_copy["owner_principal"] = "C:\\Users\\private"
            with self.assertRaisesRegex(
                AV1ValidationV4QualificationAuthorityError,
                "machine-local",
            ):
                assert_av1_validation_v4_qualification_grant(bad_grant_copy)

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------

    def _consume_claim(
        self,
        bundle: dict[str, object],
        request: dict[str, object],
        grant: dict[str, object],
    ) -> Path:
        registry = Path(request["consumption_registry"])
        registry.mkdir(mode=0o700)
        consume_av1_validation_v4_qualification_grant(
            registry=registry,
            request=request,
            grant=grant,
            claimed_at="2026-08-07T07:01:00Z",
        )
        return registry

    def _bundle(self, root: Path) -> dict[str, object]:
        """Build a full preparation bundle and a manifest freeze for testing."""
        fixture = preparation_fixture.AV1ValidationV4PreparationTests()
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
        config = load_av1_validation_v4_effective_config_snapshot(
            result.artifact_paths["config"]
        )
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
        return {
            "root": root,
            "rights": rights,
            "grant": grant,
            "claim": result.claim,
            "config": config,
            "preparation": result.preparation,
            "measurement": result.measurement,
            "freeze": freeze,
        }

    def _request(
        self,
        bundle: dict[str, object],
        *,
        consumption_registry: str | None = None,
        valid_until: str = "2026-08-07T08:00:00Z",
    ) -> dict[str, object]:
        registry = consumption_registry or str(
            Path(bundle["root"]) / "qualification-registry"
        )
        return build_av1_validation_v4_qualification_request(
            freeze=bundle["freeze"],
            preparation=bundle["preparation"],
            owner_principal="owner:test",
            execution_repository_commit="b" * 40,
            execution_repository_tree="c" * 40,
            consumption_registry=registry,
            requested_at="2026-08-07T07:00:00Z",
            valid_until=valid_until,
        )

    def _grant(self, bundle: dict[str, object]) -> dict[str, object]:
        return build_av1_validation_v4_qualification_grant(
            request=self._request(bundle),
            owner_principal="owner:test",
            authorized_at="2026-08-07T07:00:00Z",
            valid_until="2026-08-07T08:00:00Z",
        )


if __name__ == "__main__":
    unittest.main()
