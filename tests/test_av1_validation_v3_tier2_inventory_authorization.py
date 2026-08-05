import copy
from dataclasses import fields
import json
from pathlib import Path
import unittest

from mediaforce.tuning.av1_validation_v3 import (
    load_av1_validation_protocol_v3,
)
from mediaforce.tuning.av1_validation_v3_qualification import (
    build_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_authorization import (
    AV1ValidationV3Tier2InventoryAuthorizationError,
    AV1ValidationV3Tier2InventoryReadClaim,
    AV1ValidationV3Tier2InventoryReadGrant,
    AV1ValidationV3Tier2InventoryReadRequest,
    AV1_VALIDATION_V3_TIER2_INVENTORY_EXCLUSION_COUNTER_FIELDS,
    AV1_VALIDATION_V3_TIER2_INVENTORY_SOURCE_FINGERPRINT_DOMAIN,
    AV1_VALIDATION_V3_TIER2_INVENTORY_FALSE_AUTHORITY_FIELDS,
    assert_av1_validation_v3_tier2_inventory_read_claim_active,
    assert_av1_validation_v3_tier2_inventory_read_grant_active,
    assert_av1_validation_v3_tier2_inventory_read_request_active,
    av1_validation_v3_tier2_inventory_read_claim_from_payload,
    av1_validation_v3_tier2_inventory_read_grant_from_payload,
    av1_validation_v3_tier2_inventory_read_request_from_payload,
    build_av1_validation_v3_tier2_inventory_read_claim,
    build_av1_validation_v3_tier2_inventory_read_grant,
    build_av1_validation_v3_tier2_inventory_read_request,
    deserialize_av1_validation_v3_tier2_inventory_read_claim,
    deserialize_av1_validation_v3_tier2_inventory_read_grant,
    deserialize_av1_validation_v3_tier2_inventory_read_request,
    serialize_av1_validation_v3_tier2_inventory_read_claim,
    serialize_av1_validation_v3_tier2_inventory_read_grant,
    serialize_av1_validation_v3_tier2_inventory_read_request,
)


V3_PROTOCOL_PATH = Path("docs/validation/av1-cold-start-preregistration-v3.json")
SHA256 = f"sha256:{'a' * 64}"
COMMIT = "1" * 40
TREE = "2" * 40
KEY_ID = f"av1vqkey3_{'b' * 32}"
FROZEN_AT = "2026-08-03T12:00:00Z"
PLAN_VALID_UNTIL = "2026-08-04T12:00:00Z"
REQUESTED_AT = "2026-08-03T13:00:00Z"
REQUEST_VALID_UNTIL = "2026-08-03T18:00:00Z"
AUTHORIZED_AT = "2026-08-03T14:00:00Z"
GRANT_VALID_UNTIL = "2026-08-03T17:00:00Z"
CLAIMED_AT = "2026-08-03T15:00:00Z"
OWNER = "owner-1234abcd"


class AV1ValidationV3Tier2InventoryAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = load_av1_validation_protocol_v3(V3_PROTOCOL_PATH)
        self.plan = build_av1_validation_v3_qualification_plan(
            protocol=self.protocol,
            qualification_key_id=KEY_ID,
            eligibility_predicate_sha256=SHA256,
            repository_commit=COMMIT,
            repository_tree=TREE,
            config_sha256=SHA256,
            toolchain_sha256=SHA256,
            fixture_matrix_sha256=SHA256,
            frozen_at=FROZEN_AT,
            valid_until=PLAN_VALID_UNTIL,
        )
        self.request = build_av1_validation_v3_tier2_inventory_read_request(
            protocol=self.protocol,
            plan=self.plan,
            requested_at=REQUESTED_AT,
            valid_until=REQUEST_VALID_UNTIL,
        )
        self.grant = build_av1_validation_v3_tier2_inventory_read_grant(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            owner_principal=OWNER,
            authorized_at=AUTHORIZED_AT,
            valid_until=GRANT_VALID_UNTIL,
        )
        self.claim = build_av1_validation_v3_tier2_inventory_read_claim(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            grant=self.grant,
            claimed_at=CLAIMED_AT,
        )

    def test_happy_request_grant_claim_chain(self) -> None:
        assert_av1_validation_v3_tier2_inventory_read_request_active(
            self.protocol, self.plan, self.request, as_of=CLAIMED_AT
        )
        assert_av1_validation_v3_tier2_inventory_read_grant_active(
            self.protocol, self.plan, self.request, self.grant, as_of=CLAIMED_AT
        )
        assert_av1_validation_v3_tier2_inventory_read_claim_active(
            self.protocol, self.plan, self.request, self.grant, self.claim, as_of=CLAIMED_AT
        )

    def test_request_is_deterministic(self) -> None:
        rebuilt = build_av1_validation_v3_tier2_inventory_read_request(
            protocol=self.protocol,
            plan=self.plan,
            requested_at=REQUESTED_AT,
            valid_until=REQUEST_VALID_UNTIL,
        )
        self.assertEqual(self.request, rebuilt)

    def test_grant_is_deterministic(self) -> None:
        rebuilt = build_av1_validation_v3_tier2_inventory_read_grant(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            owner_principal=OWNER,
            authorized_at=AUTHORIZED_AT,
            valid_until=GRANT_VALID_UNTIL,
        )
        self.assertEqual(self.grant, rebuilt)

    def test_claim_is_deterministic(self) -> None:
        rebuilt = build_av1_validation_v3_tier2_inventory_read_claim(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            grant=self.grant,
            claimed_at=CLAIMED_AT,
        )
        self.assertEqual(self.claim, rebuilt)

    def test_request_carries_no_authority(self) -> None:
        payload = self.request.to_payload()
        self.assertFalse(payload["private_inventory_read_authorized"])
        self.assertIs(payload["single_read_requested"], True)
        self.assertTrue(payload["execution_requires_separate_owner_authorization"])
        for field in AV1_VALIDATION_V3_TIER2_INVENTORY_FALSE_AUTHORITY_FIELDS:
            self.assertIs(payload[field], False, f"{field} must be False in request")

    def test_grant_carries_only_private_inventory_read_authority(self) -> None:
        payload = self.grant.to_payload()
        self.assertIs(payload["private_inventory_read_authorized"], True)
        self.assertIs(payload["single_read_authorized"], True)
        for field in AV1_VALIDATION_V3_TIER2_INVENTORY_FALSE_AUTHORITY_FIELDS:
            self.assertIs(payload[field], False, f"{field} must be False in grant")

    def test_claim_carries_only_private_inventory_read_authority(self) -> None:
        payload = self.claim.to_payload()
        self.assertIs(payload["private_inventory_read_authorized"], True)
        self.assertIs(payload["single_read_claimed"], True)
        for field in AV1_VALIDATION_V3_TIER2_INVENTORY_FALSE_AUTHORITY_FIELDS:
            self.assertIs(payload[field], False, f"{field} must be False in claim")

    def test_request_round_trip(self) -> None:
        self.assertEqual(
            av1_validation_v3_tier2_inventory_read_request_from_payload(
                self.request.to_payload()
            ),
            self.request,
        )

    def test_grant_round_trip(self) -> None:
        self.assertEqual(
            av1_validation_v3_tier2_inventory_read_grant_from_payload(
                self.grant.to_payload()
            ),
            self.grant,
        )

    def test_claim_round_trip(self) -> None:
        self.assertEqual(
            av1_validation_v3_tier2_inventory_read_claim_from_payload(
                self.claim.to_payload()
            ),
            self.claim,
        )

    def test_noncanonical_bytes_rejected(self) -> None:
        canonical = serialize_av1_validation_v3_tier2_inventory_read_request(
            self.request
        )
        self.assertEqual(
            deserialize_av1_validation_v3_tier2_inventory_read_request(canonical),
            self.request,
        )
        noncanonical = json.dumps(self.request.to_payload(), indent=2).encode()
        with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
            deserialize_av1_validation_v3_tier2_inventory_read_request(noncanonical)

    def test_grant_noncanonical_bytes_rejected(self) -> None:
        canonical = serialize_av1_validation_v3_tier2_inventory_read_grant(self.grant)
        self.assertEqual(
            deserialize_av1_validation_v3_tier2_inventory_read_grant(canonical),
            self.grant,
        )
        noncanonical = json.dumps(self.grant.to_payload(), indent=2).encode()
        with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
            deserialize_av1_validation_v3_tier2_inventory_read_grant(noncanonical)

    def test_claim_noncanonical_bytes_rejected(self) -> None:
        canonical = serialize_av1_validation_v3_tier2_inventory_read_claim(self.claim)
        self.assertEqual(
            deserialize_av1_validation_v3_tier2_inventory_read_claim(canonical),
            self.claim,
        )
        noncanonical = json.dumps(self.claim.to_payload(), indent=2).encode()
        with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
            deserialize_av1_validation_v3_tier2_inventory_read_claim(noncanonical)

    def test_parser_rejects_authority_flip_in_request(self) -> None:
        for field, value in (
            ("private_inventory_read_authorized", True),
            ("single_read_requested", False),
            ("tier2_execution_authorized", True),
            ("tier2_selection_execution_authorized", True),
            ("qualification_key_read_authorized", True),
            ("private_inventory_serialization_authorized", True),
            ("key_creation_authorized", True),
            ("media_library_read_authorized", True),
            ("qualification_execution_authorized", True),
            ("execution_requires_separate_owner_authorization", False),
            ("unexpected_field", True),
        ):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.request.to_payload())
                payload[field] = value
                with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
                    av1_validation_v3_tier2_inventory_read_request_from_payload(payload)

    def test_parser_rejects_authority_flip_in_grant(self) -> None:
        for field, value in (
            ("private_inventory_read_authorized", False),
            ("single_read_authorized", False),
            ("tier1_execution_authorized", True),
            ("tier2_execution_authorized", True),
            ("derivation_authorized", True),
            ("unexpected_field", True),
        ):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.grant.to_payload())
                payload[field] = value
                with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
                    av1_validation_v3_tier2_inventory_read_grant_from_payload(payload)

    def test_parser_rejects_authority_flip_in_claim(self) -> None:
        for field, value in (
            ("private_inventory_read_authorized", False),
            ("single_read_claimed", False),
            ("activation_authorized", True),
            ("publication_authorized", True),
        ):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.claim.to_payload())
                payload[field] = value
                with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
                    av1_validation_v3_tier2_inventory_read_claim_from_payload(payload)

    def test_foreign_plan_rejected_by_request_assertion(self) -> None:
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
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2InventoryAuthorizationError, "not bound"
        ):
            assert_av1_validation_v3_tier2_inventory_read_request_active(
                self.protocol,
                different_plan,
                self.request,
                as_of=CLAIMED_AT,
            )

    def test_foreign_grant_request_binding_rejected(self) -> None:
        other_request = build_av1_validation_v3_tier2_inventory_read_request(
            protocol=self.protocol,
            plan=build_av1_validation_v3_qualification_plan(
                protocol=self.protocol,
                qualification_key_id=f"av1vqkey3_{'d' * 32}",
                eligibility_predicate_sha256=SHA256,
                repository_commit=COMMIT,
                repository_tree=TREE,
                config_sha256=SHA256,
                toolchain_sha256=SHA256,
                fixture_matrix_sha256=SHA256,
                frozen_at=FROZEN_AT,
                valid_until=PLAN_VALID_UNTIL,
            ),
            requested_at=REQUESTED_AT,
            valid_until=REQUEST_VALID_UNTIL,
        )
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2InventoryAuthorizationError, "not bound"
        ):
            assert_av1_validation_v3_tier2_inventory_read_grant_active(
                self.protocol,
                self.plan,
                other_request,
                self.grant,
                as_of=CLAIMED_AT,
            )

    def test_foreign_grant_chain_rejected_by_claim_assertion(self) -> None:
        other_grant = build_av1_validation_v3_tier2_inventory_read_grant(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            owner_principal="owner-differentabc",
            authorized_at=AUTHORIZED_AT,
            valid_until=GRANT_VALID_UNTIL,
        )
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2InventoryAuthorizationError, "not bound"
        ):
            assert_av1_validation_v3_tier2_inventory_read_claim_active(
                self.protocol,
                self.plan,
                self.request,
                other_grant,
                self.claim,
                as_of=CLAIMED_AT,
            )

    def test_request_fails_before_issue_and_at_expiry(self) -> None:
        for checked_at in ("2026-08-03T12:59:59Z", REQUEST_VALID_UNTIL):
            with self.subTest(checked_at=checked_at):
                with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
                    assert_av1_validation_v3_tier2_inventory_read_request_active(
                        self.protocol, self.plan, self.request, as_of=checked_at
                    )

    def test_request_cannot_outlive_plan(self) -> None:
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2InventoryAuthorizationError, "outlive"
        ):
            build_av1_validation_v3_tier2_inventory_read_request(
                protocol=self.protocol,
                plan=self.plan,
                requested_at=REQUESTED_AT,
                valid_until="2026-08-04T12:00:01Z",
            )

    def test_grant_cannot_outlive_request(self) -> None:
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2InventoryAuthorizationError, "outlive"
        ):
            build_av1_validation_v3_tier2_inventory_read_grant(
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                owner_principal=OWNER,
                authorized_at=AUTHORIZED_AT,
                valid_until="2026-08-03T18:00:01Z",
            )

    def test_grant_fails_before_issue_and_at_expiry(self) -> None:
        for checked_at in ("2026-08-03T13:59:59Z", GRANT_VALID_UNTIL):
            with self.subTest(checked_at=checked_at):
                with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
                    assert_av1_validation_v3_tier2_inventory_read_grant_active(
                        self.protocol,
                        self.plan,
                        self.request,
                        self.grant,
                        as_of=checked_at,
                    )

    def test_claim_outside_grant_window_rejected(self) -> None:
        with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
            build_av1_validation_v3_tier2_inventory_read_claim(
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                grant=self.grant,
                claimed_at=GRANT_VALID_UNTIL,
            )

    def test_claim_is_not_active_before_claimed_at(self) -> None:
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2InventoryAuthorizationError, "not active"
        ):
            assert_av1_validation_v3_tier2_inventory_read_claim_active(
                self.protocol,
                self.plan,
                self.request,
                self.grant,
                self.claim,
                as_of="2026-08-03T14:59:59Z",
            )

    def test_request_requires_canonical_utc_timestamps(self) -> None:
        for requested_at in (
            "2026-08-03T13:00:00+00:00",
            "2026-08-03T13:00:00.000000Z",
            "20260803T130000Z",
        ):
            with self.subTest(requested_at=requested_at):
                with self.assertRaisesRegex(
                    AV1ValidationV3Tier2InventoryAuthorizationError, "canonical UTC"
                ):
                    build_av1_validation_v3_tier2_inventory_read_request(
                        protocol=self.protocol,
                        plan=self.plan,
                        requested_at=requested_at,
                        valid_until=REQUEST_VALID_UNTIL,
                    )

    def test_grant_requires_canonical_utc_timestamps(self) -> None:
        for authorized_at in (
            "2026-08-03T14:00:00+00:00",
            "2026-08-03T14:00:00.000000Z",
            "20260803T140000Z",
        ):
            with self.subTest(authorized_at=authorized_at):
                with self.assertRaisesRegex(
                    AV1ValidationV3Tier2InventoryAuthorizationError, "canonical UTC"
                ):
                    build_av1_validation_v3_tier2_inventory_read_grant(
                        protocol=self.protocol,
                        plan=self.plan,
                        request=self.request,
                        owner_principal=OWNER,
                        authorized_at=authorized_at,
                        valid_until=GRANT_VALID_UNTIL,
                    )

    def test_claim_requires_canonical_utc_timestamp(self) -> None:
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2InventoryAuthorizationError, "canonical UTC"
        ):
            build_av1_validation_v3_tier2_inventory_read_claim(
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                grant=self.grant,
                claimed_at="2026-08-03T15:00:00+00:00",
            )

    def test_active_checks_require_canonical_utc_timestamp(self) -> None:
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2InventoryAuthorizationError, "canonical UTC"
        ):
            assert_av1_validation_v3_tier2_inventory_read_request_active(
                self.protocol,
                self.plan,
                self.request,
                as_of="2026-08-03T15:00:00+00:00",
            )

    def test_owner_principal_format_rejected(self) -> None:
        for bad_principal in ("owner-short", "user-1234abcd", "owner-UPPERCASE"):
            with self.subTest(principal=bad_principal):
                with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
                    build_av1_validation_v3_tier2_inventory_read_grant(
                        protocol=self.protocol,
                        plan=self.plan,
                        request=self.request,
                        owner_principal=bad_principal,
                        authorized_at=AUTHORIZED_AT,
                        valid_until=GRANT_VALID_UNTIL,
                    )

    def test_extra_key_in_request_payload_rejected(self) -> None:
        payload = copy.deepcopy(self.request.to_payload())
        payload["extra"] = "field"
        with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
            av1_validation_v3_tier2_inventory_read_request_from_payload(payload)

    def test_missing_key_in_grant_payload_rejected(self) -> None:
        payload = copy.deepcopy(self.grant.to_payload())
        del payload["owner_principal"]
        with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
            av1_validation_v3_tier2_inventory_read_grant_from_payload(payload)

    def test_missing_key_in_claim_payload_rejected(self) -> None:
        payload = copy.deepcopy(self.claim.to_payload())
        del payload["grant_id"]
        with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
            av1_validation_v3_tier2_inventory_read_claim_from_payload(payload)

    def test_scope_digest_drift_detected(self) -> None:
        with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
            AV1ValidationV3Tier2InventoryReadRequest(
                request_id=self.request.request_id,
                protocol_id=self.request.protocol_id,
                protocol_payload_sha256=self.request.protocol_payload_sha256,
                qualification_plan_id=self.request.qualification_plan_id,
                qualification_plan_payload_sha256=self.request.qualification_plan_payload_sha256,
                qualification_key_id=self.request.qualification_key_id,
                eligibility_predicate_sha256=self.request.eligibility_predicate_sha256,
                repository_commit=self.request.repository_commit,
                repository_tree=self.request.repository_tree,
                config_sha256=self.request.config_sha256,
                tier2_scope_digest=f"sha256:{'0' * 64}",
                inventory_projection_contract_digest=self.request.inventory_projection_contract_digest,
                requested_at=self.request.requested_at,
                valid_until=self.request.valid_until,
                payload_sha256=self.request.payload_sha256,
            )

    def test_projection_contract_digest_drift_detected(self) -> None:
        with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
            AV1ValidationV3Tier2InventoryReadRequest(
                request_id=self.request.request_id,
                protocol_id=self.request.protocol_id,
                protocol_payload_sha256=self.request.protocol_payload_sha256,
                qualification_plan_id=self.request.qualification_plan_id,
                qualification_plan_payload_sha256=self.request.qualification_plan_payload_sha256,
                qualification_key_id=self.request.qualification_key_id,
                eligibility_predicate_sha256=self.request.eligibility_predicate_sha256,
                repository_commit=self.request.repository_commit,
                repository_tree=self.request.repository_tree,
                config_sha256=self.request.config_sha256,
                tier2_scope_digest=self.request.tier2_scope_digest,
                inventory_projection_contract_digest=f"sha256:{'1' * 64}",
                requested_at=self.request.requested_at,
                valid_until=self.request.valid_until,
                payload_sha256=self.request.payload_sha256,
            )

    def test_request_owner_summary_is_privacy_safe(self) -> None:
        summary = self.request.to_owner_summary(
            protocol=self.protocol,
            plan=self.plan,
            as_of=CLAIMED_AT,
        )
        self.assertFalse(summary["private_inventory_read_authorized"])
        self.assertNotIn("tier2_scope_digest", summary)
        self.assertNotIn("inventory_projection_contract_digest", summary)
        self.assertNotIn("candidate_count", summary)
        for field in AV1_VALIDATION_V3_TIER2_INVENTORY_FALSE_AUTHORITY_FIELDS:
            self.assertIs(summary[field], False)

    def test_grant_owner_summary_is_privacy_safe(self) -> None:
        summary = self.grant.to_owner_summary(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            as_of=CLAIMED_AT,
        )
        self.assertIs(summary["private_inventory_read_authorized"], True)
        self.assertEqual(summary["owner_principal"], OWNER)
        self.assertNotIn("candidate_count", summary)
        self.assertNotIn("tier2_scope_digest", summary)

    def test_claim_owner_summary_is_privacy_safe(self) -> None:
        summary = self.claim.to_owner_summary(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            grant=self.grant,
            as_of=CLAIMED_AT,
        )
        self.assertIs(summary["private_inventory_read_authorized"], True)
        self.assertNotIn("plan_payload_sha256", summary)

    def test_summaries_require_active_chain(self) -> None:
        expired_at = "2026-08-03T18:00:01Z"
        with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
            self.request.to_owner_summary(
                protocol=self.protocol, plan=self.plan, as_of=expired_at
            )
        with self.assertRaises(AV1ValidationV3Tier2InventoryAuthorizationError):
            self.grant.to_owner_summary(
                protocol=self.protocol,
                plan=self.plan,
                request=self.request,
                as_of=expired_at,
            )

    def test_no_key_bytes_in_public_api(self) -> None:
        import inspect
        import mediaforce.tuning.av1_validation_v3_tier2_inventory_authorization as mod

        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            sig = inspect.signature(fn)
            for param_name in sig.parameters:
                self.assertNotIn(
                    "key",
                    param_name.lower().replace("_key_id", ""),
                    f"{name}() exposes a key-bytes parameter: {param_name}",
                )

    def test_module_source_has_no_db_or_runtime_dependencies(self) -> None:
        import importlib
        import inspect
        import sys

        mod_name = (
            "mediaforce.tuning.av1_validation_v3_tier2_inventory_authorization"
        )
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        module = importlib.import_module(mod_name)
        source = inspect.getsource(module)
        for forbidden in (
            "mediaforce.core.db",
            "subprocess",
            "secrets",
            "pathlib",
            "av1_validation_v3_tier2_inventory import",
            ".read_bytes(",
            "open(",
        ):
            self.assertNotIn(forbidden, source)

    def test_projection_counter_vocabulary_matches_inventory(self) -> None:
        from mediaforce.tuning.av1_validation_v3_tier2_inventory import (
            AV1_VALIDATION_V3_TIER2_INVENTORY_FINGERPRINT_DOMAIN,
            AV1ValidationV3Tier2Inventory,
        )

        inventory_counter_fields = {
            field.name
            for field in fields(AV1ValidationV3Tier2Inventory)
            if field.name.endswith("_count") and field.name != "measured_row_count"
        }
        self.assertEqual(
            set(AV1_VALIDATION_V3_TIER2_INVENTORY_EXCLUSION_COUNTER_FIELDS),
            inventory_counter_fields,
        )
        self.assertEqual(
            AV1_VALIDATION_V3_TIER2_INVENTORY_SOURCE_FINGERPRINT_DOMAIN,
            AV1_VALIDATION_V3_TIER2_INVENTORY_FINGERPRINT_DOMAIN,
        )

    def test_dataclass_types(self) -> None:
        self.assertIsInstance(self.request, AV1ValidationV3Tier2InventoryReadRequest)
        self.assertIsInstance(self.grant, AV1ValidationV3Tier2InventoryReadGrant)
        self.assertIsInstance(self.claim, AV1ValidationV3Tier2InventoryReadClaim)

    def test_id_prefixes_are_correct(self) -> None:
        self.assertTrue(self.request.request_id.startswith("av1vtier2invreadrequest3_"))
        self.assertTrue(self.grant.grant_id.startswith("av1vtier2invreadgrant3_"))
        self.assertTrue(self.claim.claim_id.startswith("av1vtier2invreadclaim3_"))
