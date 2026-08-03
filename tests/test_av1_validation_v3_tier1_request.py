import copy
import json
from pathlib import Path
import tempfile
import unittest

from mediaforce.tuning.av1_validation_v3 import load_av1_validation_protocol_v3
from mediaforce.tuning.av1_validation_v3_qualification import (
    build_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_request import (
    AV1ValidationV3Tier1RequestError,
    assert_av1_validation_v3_tier1_request_active,
    av1_validation_v3_tier1_request_from_payload,
    build_av1_validation_v3_tier1_authorization_request,
    load_av1_validation_v3_tier1_authorization_request,
    serialize_av1_validation_v3_tier1_authorization_request,
)


V3_PROTOCOL_PATH = Path("docs/validation/av1-cold-start-preregistration-v3.json")
SHA256 = f"sha256:{'a' * 64}"
COMMIT = "1" * 40
TREE = "2" * 40
FROZEN_AT = "2026-08-03T12:00:00Z"
PLAN_VALID_UNTIL = "2026-08-04T12:00:00Z"
REQUESTED_AT = "2026-08-03T13:00:00Z"
REQUEST_VALID_UNTIL = "2026-08-03T18:00:00Z"


class AV1ValidationV3Tier1RequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = load_av1_validation_protocol_v3(V3_PROTOCOL_PATH)
        self.plan = build_av1_validation_v3_qualification_plan(
            protocol=self.protocol,
            qualification_key_id=f"av1vqkey3_{'b' * 32}",
            eligibility_predicate_sha256=SHA256,
            repository_commit=COMMIT,
            repository_tree=TREE,
            config_sha256=SHA256,
            toolchain_sha256=SHA256,
            fixture_matrix_sha256=SHA256,
            frozen_at=FROZEN_AT,
            valid_until=PLAN_VALID_UNTIL,
        )
        self.request = build_av1_validation_v3_tier1_authorization_request(
            protocol=self.protocol,
            plan=self.plan,
            requested_at=REQUESTED_AT,
            valid_until=REQUEST_VALID_UNTIL,
        )

    def test_request_is_canonical_deterministic_and_non_authorizing(self) -> None:
        rebuilt = build_av1_validation_v3_tier1_authorization_request(
            protocol=self.protocol,
            plan=self.plan,
            requested_at=REQUESTED_AT,
            valid_until=REQUEST_VALID_UNTIL,
        )
        self.assertEqual(self.request, rebuilt)
        payload = self.request.to_payload()
        self.assertEqual(payload["fixture_scope"], "deterministic_synthetic_public_only")
        self.assertEqual(payload["request_state"], "owner_action_required")
        for name in (
            "evidence_eligible",
            "private_inventory_read_authorized",
            "key_creation_authorized",
            "media_read_authorized",
            "qualification_execution_authorized",
            "evidence_creation_authorized",
            "empirical_authority_conferred",
            "public_bundle_activation_allowed",
        ):
            self.assertFalse(payload[name])
        self.assertTrue(payload["execution_requires_separate_owner_authorization"])

    def test_request_must_bind_active_protocol_and_qualification_plan(self) -> None:
        assert_av1_validation_v3_tier1_request_active(
            self.protocol,
            self.plan,
            self.request,
            as_of="2026-08-03T14:00:00Z",
        )
        different_plan = build_av1_validation_v3_qualification_plan(
            protocol=self.protocol,
            qualification_key_id=f"av1vqkey3_{'c' * 32}",
            eligibility_predicate_sha256=SHA256,
            repository_commit=COMMIT,
            repository_tree=TREE,
            config_sha256=SHA256,
            toolchain_sha256=SHA256,
            fixture_matrix_sha256=SHA256,
            frozen_at=FROZEN_AT,
            valid_until=PLAN_VALID_UNTIL,
        )
        with self.assertRaisesRegex(AV1ValidationV3Tier1RequestError, "not bound"):
            assert_av1_validation_v3_tier1_request_active(
                self.protocol,
                different_plan,
                self.request,
                as_of="2026-08-03T14:00:00Z",
            )

    def test_request_fails_before_issue_at_expiry_and_after_plan_expiry(self) -> None:
        for checked_at in (
            "2026-08-03T12:59:59Z",
            REQUEST_VALID_UNTIL,
            PLAN_VALID_UNTIL,
        ):
            with self.subTest(checked_at=checked_at):
                with self.assertRaises(AV1ValidationV3Tier1RequestError):
                    assert_av1_validation_v3_tier1_request_active(
                        self.protocol,
                        self.plan,
                        self.request,
                        as_of=checked_at,
                    )

    def test_request_cannot_outlive_qualification_plan(self) -> None:
        with self.assertRaisesRegex(AV1ValidationV3Tier1RequestError, "outlive"):
            build_av1_validation_v3_tier1_authorization_request(
                protocol=self.protocol,
                plan=self.plan,
                requested_at=REQUESTED_AT,
                valid_until="2026-08-04T12:00:01Z",
            )

    def test_owner_summary_requires_exact_active_bindings(self) -> None:
        self.assertEqual(
            self.request.to_owner_summary(
                protocol=self.protocol,
                plan=self.plan,
                as_of="2026-08-03T14:00:00Z",
            ),
            {
                "tier": "tier1",
                "fixture_scope": "deterministic_synthetic_public_only",
                "request_state": "owner_action_required",
                "execution_authorized": False,
                "evidence_eligible": False,
            },
        )

    def test_parser_and_loader_reject_tampering(self) -> None:
        self.assertEqual(
            av1_validation_v3_tier1_request_from_payload(self.request.to_payload()),
            self.request,
        )
        for key, value in (
            ("qualification_execution_authorized", True),
            ("fixture_scope", "private_media"),
            ("unexpected", "field"),
        ):
            with self.subTest(key=key):
                payload = copy.deepcopy(self.request.to_payload())
                payload[key] = value
                with self.assertRaises(AV1ValidationV3Tier1RequestError):
                    av1_validation_v3_tier1_request_from_payload(payload)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tier1-request.json"
            path.write_bytes(serialize_av1_validation_v3_tier1_authorization_request(self.request))
            self.assertEqual(
                load_av1_validation_v3_tier1_authorization_request(path),
                self.request,
            )
            path.write_text(json.dumps(self.request.to_payload(), indent=2), encoding="utf-8")
            with self.assertRaises(AV1ValidationV3Tier1RequestError):
                load_av1_validation_v3_tier1_authorization_request(path)
