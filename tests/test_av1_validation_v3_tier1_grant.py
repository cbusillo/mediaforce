from pathlib import Path
import unittest

from mediaforce.tuning.av1_validation_v3 import load_av1_validation_protocol_v3
from mediaforce.tuning.av1_validation_v3_qualification import (
    build_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_grant import (
    AV1ValidationV3Tier1GrantError,
    assert_av1_validation_v3_tier1_grant_active,
    av1_validation_v3_tier1_grant_from_payload,
    build_av1_validation_v3_tier1_execution_grant,
)
from mediaforce.tuning.av1_validation_v3_tier1_request import (
    build_av1_validation_v3_tier1_authorization_request,
)


class AV1ValidationV3Tier1GrantTests(unittest.TestCase):
    def setUp(self) -> None:
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
            fixture_matrix_sha256=f"sha256:{'e' * 64}",
            frozen_at="2026-08-03T12:00:00Z",
            valid_until="2026-08-04T12:00:00Z",
        )
        request = build_av1_validation_v3_tier1_authorization_request(
            protocol=protocol,
            plan=plan,
            requested_at="2026-08-03T13:00:00Z",
            valid_until="2026-08-03T18:00:00Z",
        )
        self.protocol, self.plan, self.request = protocol, plan, request

    def test_grant_binds_active_request_and_excludes_every_other_scope(self) -> None:
        grant = build_av1_validation_v3_tier1_execution_grant(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            owner_principal="owner-1234abcd",
            authorized_at="2026-08-03T14:00:00Z",
            valid_until="2026-08-03T17:00:00Z",
        )
        self.assertTrue(grant.to_payload()["tier1_execution_authorized"])
        self.assertFalse(grant.to_payload()["private_inventory_read_authorized"])
        self.assertFalse(grant.to_payload()["tier2_execution_authorized"])
        assert_av1_validation_v3_tier1_grant_active(
            self.protocol, self.plan, self.request, grant, as_of="2026-08-03T15:00:00Z"
        )
        payload = grant.to_payload()
        payload["tier2_execution_authorized"] = True
        with self.assertRaises(AV1ValidationV3Tier1GrantError):
            av1_validation_v3_tier1_grant_from_payload(payload)
        payload = grant.to_payload()
        payload["evidence_eligible"] = 0
        with self.assertRaises(AV1ValidationV3Tier1GrantError):
            av1_validation_v3_tier1_grant_from_payload(payload)

    def test_grant_cannot_outlive_request(self) -> None:
        with self.assertRaisesRegex(AV1ValidationV3Tier1GrantError, "outlive"):
            build_av1_validation_v3_tier1_execution_grant(
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                owner_principal="owner-1234abcd",
                authorized_at="2026-08-03T14:00:00Z",
                valid_until="2026-08-03T18:00:01Z",
            )

    def test_grant_requires_canonical_utc_timestamps(self) -> None:
        with self.assertRaisesRegex(AV1ValidationV3Tier1GrantError, "canonical UTC"):
            build_av1_validation_v3_tier1_execution_grant(
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                owner_principal="owner-1234abcd",
                authorized_at="2026-08-03T14:00:00+00:00",
                valid_until="2026-08-03T17:00:00Z",
            )
