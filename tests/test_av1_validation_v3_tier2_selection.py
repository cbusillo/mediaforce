from collections.abc import Callable
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db_tables import (
    library_item_evidence_state,
    library_items,
    metadata,
)
from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.encoding.fingerprint import (
    MEDIA_FINGERPRINT_EVIDENCE_KIND,
    MEDIA_FINGERPRINT_SCHEMA_VERSION,
    MEDIA_FINGERPRINT_TOOL_NAME,
    MEDIA_FINGERPRINT_TOOL_VERSION,
)
from mediaforce.tuning.av1_validation_v3 import (
    AV1ValidationProtocolV3,
    AV1ValidationV3Error,
    AV1ValidationV3QualificationSource,
    av1_validation_v3_qualification_key_id,
    load_av1_validation_protocol_v3,
)
from mediaforce.tuning.av1_validation_v3_qualification import (
    AV1ValidationV3QualificationError,
    AV1ValidationV3QualificationPlan,
    build_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_config_snapshot import (
    build_av1_validation_v3_tier1_config_snapshot,
)
from mediaforce.tuning.av1_validation_v3_tier1_preparation import (
    av1_validation_v3_tier1_config_sha256,
)
from mediaforce.tuning.av1_validation_v3_tier1_publication import (
    AV1ValidationV3Tier1PublicationError,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory import (
    AV1ValidationV3Tier2Inventory,
    load_av1_validation_v3_tier2_inventory,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_authorization import (
    AV1ValidationV3Tier2InventoryReadContext,
    build_av1_validation_v3_tier2_inventory_read_claim,
    build_av1_validation_v3_tier2_inventory_read_grant,
    build_av1_validation_v3_tier2_inventory_read_request,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_publication import (
    publish_av1_validation_v3_tier2_inventory_read_claim,
)
from mediaforce.tuning.av1_validation_v3_tier2_selection import (
    AV1ValidationV3Tier2SelectionError,
    AV1ValidationV3Tier2SelectionRecord,
    assert_av1_validation_v3_tier2_selection_record,
    av1_validation_v3_tier2_selection_record_from_payload,
    build_av1_validation_v3_tier2_selection_record,
    load_av1_validation_v3_tier2_selection_record,
    serialize_av1_validation_v3_tier2_selection_record,
    validate_av1_validation_v3_tier2_selection_record_sources,
)
from mediaforce.tuning.av1_validation_v3_tier2_selection_authorization import (
    AV1_VALIDATION_V3_TIER2_SELECTION_FALSE_AUTHORITY_FIELDS,
    AV1ValidationV3Tier2SelectionAuthorizationError,
    AV1ValidationV3Tier2SelectionContext,
    assert_av1_validation_v3_tier2_selection_context,
    build_av1_validation_v3_tier2_selection_claim,
    build_av1_validation_v3_tier2_selection_grant,
    build_av1_validation_v3_tier2_selection_request,
    av1_validation_v3_tier2_selection_claim_from_payload,
    av1_validation_v3_tier2_selection_grant_from_payload,
    av1_validation_v3_tier2_selection_request_from_payload,
    serialize_av1_validation_v3_tier2_selection_claim,
    serialize_av1_validation_v3_tier2_selection_grant,
    serialize_av1_validation_v3_tier2_selection_request,
    deserialize_av1_validation_v3_tier2_selection_claim,
    deserialize_av1_validation_v3_tier2_selection_grant,
    deserialize_av1_validation_v3_tier2_selection_request,
)
from mediaforce.tuning.av1_validation_v3_tier2_selection_operation import (
    AV1ValidationV3Tier2SelectionOperationError,
    AV1ValidationV3Tier2SelectionOperationResult,
    run_av1_validation_v3_tier2_selection,
)
from mediaforce.tuning.av1_validation_v3_tier2_selection_publication import (
    publish_av1_validation_v3_tier2_selection_claim,
    publish_av1_validation_v3_tier2_selection_record,
)


V3_PROTOCOL_PATH = Path("docs/validation/av1-cold-start-preregistration-v3.json")
SHA256 = f"sha256:{'a' * 64}"
COMMIT = "1" * 40
TREE = "2" * 40
FROZEN_AT = "2026-08-03T12:00:00Z"
VALID_UNTIL = "2026-08-04T12:00:00Z"
SELECTED_AT = "2026-08-03T13:00:00Z"

# Additional constants for the operation/authorization chain tests
_PLAN_VALID_UNTIL = "2026-08-06T12:00:00Z"
_REQUESTED_AT = "2026-08-03T13:00:00Z"
_REQUEST_VALID_UNTIL = "2026-08-06T10:00:00Z"
_AUTHORIZED_AT = "2026-08-03T14:00:00Z"
_GRANT_VALID_UNTIL = "2026-08-06T09:00:00Z"
_CLAIMED_AT = "2026-08-03T15:00:00Z"
_SELECTED_AT = "2026-08-03T15:00:01Z"
_OWNER = "owner-1234abcd"
_OTHER_OWNER = "owner-5678efgh"


class AV1ValidationV3Tier2SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = load_av1_validation_protocol_v3(V3_PROTOCOL_PATH)
        self.key = b"q" * 32
        self.key_id = av1_validation_v3_qualification_key_id(self.key)
        self.plan = build_av1_validation_v3_qualification_plan(
            protocol=self.protocol,
            qualification_key_id=self.key_id,
            eligibility_predicate_sha256=SHA256,
            repository_commit=COMMIT,
            repository_tree=TREE,
            config_sha256=SHA256,
            toolchain_sha256=SHA256,
            fixture_matrix_sha256=SHA256,
            frozen_at=FROZEN_AT,
            valid_until=VALID_UNTIL,
        )
        self.sources = _sources(
            animation=(9, 5, 1),
            typical=(8, 4, 2),
        )

    def _record(
        self,
        sources: list[AV1ValidationV3QualificationSource] | None = None,
    ) -> AV1ValidationV3Tier2SelectionRecord:
        return build_av1_validation_v3_tier2_selection_record(
            protocol=self.protocol,
            plan=self.plan,
            sources=self.sources if sources is None else sources,
            qualification_key=self.key,
            selected_at=SELECTED_AT,
        )

    def test_selection_record_is_deterministic_and_order_independent(self) -> None:
        forward = self._record()
        reversed_record = self._record(list(reversed(self.sources)))

        self.assertEqual(forward, reversed_record)
        self.assertEqual(forward.candidate_count, len(self.sources))
        self.assertEqual(len(forward.candidate_sources), len(self.sources))
        self.assertEqual(
            tuple(selection.stratum_name for selection in forward.selections),
            tuple(stratum.name for stratum in self.protocol.tier2_strata),
        )
        payload = forward.to_payload()
        self.assertFalse(payload["tier2_execution_authorized"])
        self.assertFalse(payload["qualification_execution_authorized"])
        self.assertFalse(payload["private_inventory_read_authorized"])
        self.assertFalse(payload["empirical_authority_conferred"])
        self.assertFalse(payload["derivation_authorized"])
        self.assertFalse(payload["holdout_authorized"])
        self.assertFalse(payload["publication_authorized"])
        self.assertNotIn("qualification_key", payload)

    def test_round_trip_and_noncanonical_bytes_rejection(self) -> None:
        record = self._record()
        payload = record.to_payload()
        self.assertEqual(
            av1_validation_v3_tier2_selection_record_from_payload(payload),
            record,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tier2-selection.json"
            path.write_bytes(serialize_av1_validation_v3_tier2_selection_record(record))
            self.assertEqual(
                load_av1_validation_v3_tier2_selection_record(path), record
            )

            path.write_text(
                json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                AV1ValidationV3Tier2SelectionError,
                "canonical",
            ):
                load_av1_validation_v3_tier2_selection_record(path)

    def test_builder_rejects_wrong_key(self) -> None:
        wrong_key = b"r" * 32
        with self.assertRaisesRegex(AV1ValidationV3Tier2SelectionError, "key"):
            build_av1_validation_v3_tier2_selection_record(
                protocol=self.protocol,
                plan=self.plan,
                sources=self.sources,
                qualification_key=wrong_key,
                selected_at=SELECTED_AT,
            )

    def test_duplicate_fingerprint_rejected_before_selection(self) -> None:
        duplicate = [*self.sources, self.sources[0]]
        with self.assertRaisesRegex(AV1ValidationV3Tier2SelectionError, "repeat"):
            self._record(duplicate)

    def test_unfillable_stratum_rejected(self) -> None:
        only_animation = [
            source for source in self.sources if source.exact_traits == ("animation",)
        ]
        with self.assertRaisesRegex(AV1ValidationV3Error, "cannot fill"):
            self._record(only_animation)

    def test_powered_candidate_overlap_rejected(self) -> None:
        powered = [
            *self.sources,
            _source(100, "darkness"),
        ]
        with self.assertRaisesRegex(AV1ValidationV3Error, "out-of-scope"):
            self._record(powered)

    def test_pipeline_readiness_is_required(self) -> None:
        not_ready = [
            source
            if source.exact_traits != ("typical",)
            else AV1ValidationV3QualificationSource(
                source_fingerprint=source.source_fingerprint,
                intent_level=source.intent_level,
                exact_traits=source.exact_traits,
                pipeline_ready=False,
            )
            for source in self.sources
        ]
        with self.assertRaisesRegex(AV1ValidationV3Error, "out-of-scope"):
            self._record(not_ready)

    def test_keyless_assertion_rejects_out_of_scope_nonselected_candidate(self) -> None:
        record = self._record()
        object.__setattr__(
            record,
            "candidate_sources",
            (*record.candidate_sources, _source(100, "darkness")),
        )
        with self.assertRaisesRegex(AV1ValidationV3Tier2SelectionError, "out-of-scope"):
            assert_av1_validation_v3_tier2_selection_record(
                self.protocol,
                self.plan,
                record,
            )

    def test_expired_or_unbound_plan_fails_closed(self) -> None:
        with self.assertRaisesRegex(AV1ValidationV3QualificationError, "not active"):
            build_av1_validation_v3_tier2_selection_record(
                protocol=self.protocol,
                plan=self.plan,
                sources=self.sources,
                qualification_key=self.key,
                selected_at=VALID_UNTIL,
            )
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2SelectionError,
            "canonical UTC",
        ):
            build_av1_validation_v3_tier2_selection_record(
                protocol=self.protocol,
                plan=self.plan,
                sources=self.sources,
                qualification_key=self.key,
                selected_at="2026-08-03T13:00:00+00:00",
            )
        unbound_plan = build_av1_validation_v3_qualification_plan(
            protocol=self.protocol,
            qualification_key_id=av1_validation_v3_qualification_key_id(b"s" * 32),
            eligibility_predicate_sha256=SHA256,
            repository_commit=COMMIT,
            repository_tree=TREE,
            config_sha256=SHA256,
            toolchain_sha256=SHA256,
            fixture_matrix_sha256=SHA256,
            frozen_at=FROZEN_AT,
            valid_until=VALID_UNTIL,
        )
        with self.assertRaisesRegex(AV1ValidationV3Tier2SelectionError, "bound"):
            assert_av1_validation_v3_tier2_selection_record(
                self.protocol,
                unbound_plan,
                self._record(),
            )

    def test_payload_tampering_rejected(self) -> None:
        payload = copy.deepcopy(self._record().to_payload())
        payload["selections"][0]["rank_sha256"] = f"sha256:{'b' * 64}"
        with self.assertRaisesRegex(AV1ValidationV3Tier2SelectionError, "digest"):
            av1_validation_v3_tier2_selection_record_from_payload(payload)

        payload = copy.deepcopy(self._record().to_payload())
        payload["tier2_execution_authorized"] = True
        with self.assertRaisesRegex(AV1ValidationV3Tier2SelectionError, "contract"):
            av1_validation_v3_tier2_selection_record_from_payload(payload)

        payload = copy.deepcopy(self._record().to_payload())
        payload["candidate_sources"][0]["pipeline_ready"] = False
        with self.assertRaisesRegex(AV1ValidationV3Tier2SelectionError, "digest"):
            av1_validation_v3_tier2_selection_record_from_payload(payload)

    def test_candidate_set_redraw_changes_record_identity(self) -> None:
        original = self._record()
        redraw = self._record(_sources(animation=(1, 3, 5), typical=(2, 4, 6)))

        self.assertNotEqual(original.candidate_set_sha256, redraw.candidate_set_sha256)
        self.assertNotEqual(original.selection_record_id, redraw.selection_record_id)
        with self.assertRaisesRegex(AV1ValidationV3Tier2SelectionError, "current"):
            validate_av1_validation_v3_tier2_selection_record_sources(
                protocol=self.protocol,
                plan=self.plan,
                record=original,
                sources=_sources(animation=(1, 3, 5), typical=(2, 4, 6)),
                qualification_key=self.key,
            )

    def test_public_summary_is_privacy_safe_and_authority_false(self) -> None:
        summary = self._record().to_public_summary(
            protocol=self.protocol,
            plan=self.plan,
        )
        for forbidden in (
            "candidate_count",
            "candidate_sources",
            "candidate_set_sha256",
            "inventory_count",
            "inventory_digest",
            "inventory_sha256",
            "qualification_key_id",
            "selected_at",
            "selections",
            "selection_payload_sha256",
            "selection_record_id",
            "source_fingerprint",
        ):
            self.assertNotIn(forbidden, summary)
        for field in (
            "tier2_execution_authorized",
            "qualification_execution_authorized",
            "private_inventory_read_authorized",
            "empirical_authority_conferred",
            "derivation_authorized",
            "holdout_authorized",
            "publication_authorized",
            "evidence_eligible",
        ):
            self.assertFalse(summary[field])


class AV1ValidationV3Tier2SelectionOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.key = b"q" * 32
        self.key_id = av1_validation_v3_qualification_key_id(self.key)
        self.config = _config(self.root)
        self.protocol = load_av1_validation_protocol_v3(V3_PROTOCOL_PATH)
        self.snapshot_bytes = build_av1_validation_v3_tier1_config_snapshot(self.config)
        config_sha256 = av1_validation_v3_tier1_config_sha256(self.snapshot_bytes)
        self.plan = _plan_with_key_id(
            self.protocol, key_id=self.key_id, config_sha256=config_sha256
        )
        self.inventory_context = self._make_inventory_context(self.plan)
        self.selection_context = self._make_selection_context(
            self.plan, self.inventory_context
        )
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        metadata.create_all(self.engine)
        self.connection = self.engine.connect()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.connection.close)

    def _make_inventory_context(
        self,
        plan: AV1ValidationV3QualificationPlan,
        *,
        owner: str = _OWNER,
        claimed_at: str = _CLAIMED_AT,
        authorized_at: str = _AUTHORIZED_AT,
        grant_valid_until: str = _GRANT_VALID_UNTIL,
    ) -> AV1ValidationV3Tier2InventoryReadContext:
        inventory_request = build_av1_validation_v3_tier2_inventory_read_request(
            protocol=self.protocol,
            plan=plan,
            requested_at=_REQUESTED_AT,
            valid_until=_REQUEST_VALID_UNTIL,
        )
        inventory_grant = build_av1_validation_v3_tier2_inventory_read_grant(
            protocol=self.protocol,
            plan=plan,
            request=inventory_request,
            owner_principal=owner,
            authorized_at=authorized_at,
            valid_until=grant_valid_until,
        )
        inventory_claim = build_av1_validation_v3_tier2_inventory_read_claim(
            protocol=self.protocol,
            plan=plan,
            request=inventory_request,
            grant=inventory_grant,
            claimed_at=claimed_at,
        )
        return AV1ValidationV3Tier2InventoryReadContext(
            plan=plan,
            request=inventory_request,
            grant=inventory_grant,
            claim=inventory_claim,
        )

    def _make_selection_context(
        self,
        plan: AV1ValidationV3QualificationPlan,
        inventory_context: AV1ValidationV3Tier2InventoryReadContext,
        *,
        owner: str = _OWNER,
        claimed_at: str = _CLAIMED_AT,
        authorized_at: str = _AUTHORIZED_AT,
        grant_valid_until: str = _GRANT_VALID_UNTIL,
    ) -> AV1ValidationV3Tier2SelectionContext:
        sel_request = build_av1_validation_v3_tier2_selection_request(
            protocol=self.protocol,
            plan=plan,
            inner_inventory_request=inventory_context.request,
            requested_at=_REQUESTED_AT,
            valid_until=_REQUEST_VALID_UNTIL,
        )
        sel_grant = build_av1_validation_v3_tier2_selection_grant(
            protocol=self.protocol,
            plan=plan,
            inner_inventory_request=inventory_context.request,
            request=sel_request,
            owner_principal=owner,
            authorized_at=authorized_at,
            valid_until=grant_valid_until,
        )
        sel_claim = build_av1_validation_v3_tier2_selection_claim(
            protocol=self.protocol,
            plan=plan,
            inner_inventory_request=inventory_context.request,
            request=sel_request,
            grant=sel_grant,
            inner_inventory_claim=inventory_context.claim,
            claimed_at=claimed_at,
        )
        return AV1ValidationV3Tier2SelectionContext(
            plan=plan,
            request=sel_request,
            grant=sel_grant,
            claim=sel_claim,
        )

    def _run_operation(
        self,
        *,
        selection_context: AV1ValidationV3Tier2SelectionContext | None = None,
        inventory_context: AV1ValidationV3Tier2InventoryReadContext | None = None,
        output_root: Path,
        repository_root: Path,
        key: bytes | None = None,
        clock_value: str = _SELECTED_AT,
        inventory_adapter: Callable[..., AV1ValidationV3Tier2Inventory] | None = None,
    ) -> AV1ValidationV3Tier2SelectionOperationResult:
        sc = selection_context if selection_context is not None else self.selection_context
        ic = inventory_context if inventory_context is not None else self.inventory_context
        actual_key = key if key is not None else self.key
        if inventory_adapter is None:
            inventory_adapter = load_av1_validation_v3_tier2_inventory
        with patch(
            "mediaforce.tuning.av1_validation_v3_tier2_selection_operation."
            "load_av1_validation_v3_qualification_key",
            return_value=actual_key,
        ):
            return run_av1_validation_v3_tier2_selection(
                self.connection,
                config=self.config,
                protocol=self.protocol,
                selection_context=sc,
                inventory_context=ic,
                config_snapshot_bytes=self.snapshot_bytes,
                output_root=output_root,
                repository_root=repository_root,
                key_root=output_root,
                clock=lambda: clock_value,
                inventory_adapter=inventory_adapter,
            )

    def test_canonical_contracts(self) -> None:
        request_payload = self.selection_context.request.to_payload()
        grant_payload = self.selection_context.grant.to_payload()
        claim_payload = self.selection_context.claim.to_payload()

        # Request: tier2_selection_execution_authorized must be False
        self.assertFalse(request_payload["tier2_selection_execution_authorized"])
        self.assertFalse(request_payload["qualification_key_read_authorized"])
        # Grant/Claim: tier2_selection_execution_authorized must be True
        self.assertTrue(grant_payload["tier2_selection_execution_authorized"])
        self.assertTrue(claim_payload["tier2_selection_execution_authorized"])
        self.assertTrue(grant_payload["qualification_key_read_authorized"])
        self.assertTrue(claim_payload["qualification_key_read_authorized"])

        # private_inventory_read_authorized must be False on all three (it's in false fields)
        self.assertFalse(request_payload["private_inventory_read_authorized"])
        self.assertFalse(grant_payload["private_inventory_read_authorized"])
        self.assertFalse(claim_payload["private_inventory_read_authorized"])

        # All false authority fields must be False on all three
        for field in AV1_VALIDATION_V3_TIER2_SELECTION_FALSE_AUTHORITY_FIELDS:
            self.assertFalse(request_payload[field], f"request.{field} should be False")
            self.assertFalse(grant_payload[field], f"grant.{field} should be False")
            self.assertFalse(claim_payload[field], f"claim.{field} should be False")

        # Request must bind inner inventory request
        self.assertTrue(
            request_payload["inner_inventory_request_id"].startswith(
                "av1vtier2invreadrequest3_"
            )
        )
        self.assertEqual(
            request_payload["inner_inventory_request_id"],
            self.inventory_context.request.request_id,
        )

        # Claim must bind inner inventory claim
        self.assertTrue(
            claim_payload["inner_inventory_claim_id"].startswith(
                "av1vtier2invreadclaim3_"
            )
        )
        self.assertEqual(
            claim_payload["inner_inventory_claim_id"],
            self.inventory_context.claim.claim_id,
        )

        # IDs must have correct prefixes
        self.assertTrue(
            self.selection_context.request.request_id.startswith("av1vtier2selrequest3_")
        )
        self.assertTrue(
            self.selection_context.grant.grant_id.startswith("av1vtier2selgrant3_")
        )
        self.assertTrue(
            self.selection_context.claim.claim_id.startswith("av1vtier2selclaim3_")
        )

        # Round-trip serialization
        for artifact, serialize_fn, deserialize_fn in (
            (
                self.selection_context.request,
                serialize_av1_validation_v3_tier2_selection_request,
                deserialize_av1_validation_v3_tier2_selection_request,
            ),
            (
                self.selection_context.grant,
                serialize_av1_validation_v3_tier2_selection_grant,
                deserialize_av1_validation_v3_tier2_selection_grant,
            ),
            (
                self.selection_context.claim,
                serialize_av1_validation_v3_tier2_selection_claim,
                deserialize_av1_validation_v3_tier2_selection_claim,
            ),
        ):
            raw = serialize_fn(artifact)
            self.assertEqual(deserialize_fn(raw), artifact)

    def test_binding_tamper_rejection(self) -> None:
        # Wrong inner inventory request ID in the selection request
        request_payload = copy.deepcopy(
            self.selection_context.request.to_payload()
        )
        request_payload["inner_inventory_request_id"] = (
            "av1vtier2invreadrequest3_" + "0" * 32
        )
        # payload_sha256 will not match - expect error
        with self.assertRaises(AV1ValidationV3Tier2SelectionAuthorizationError):
            av1_validation_v3_tier2_selection_request_from_payload(request_payload)

        # Wrong inner inventory claim ID in the selection claim
        claim_payload = copy.deepcopy(self.selection_context.claim.to_payload())
        claim_payload["inner_inventory_claim_id"] = (
            "av1vtier2invreadclaim3_" + "0" * 32
        )
        with self.assertRaises(AV1ValidationV3Tier2SelectionAuthorizationError):
            av1_validation_v3_tier2_selection_claim_from_payload(claim_payload)

        # Cross-chain: wrong inner_inventory_request binding causes context assertion to fail.
        # Use a different inner inventory request (later valid_until so builder constraint passes,
        # but the rebuilt request won't match self.selection_context.request).
        other_inv_request = build_av1_validation_v3_tier2_inventory_read_request(
            protocol=self.protocol,
            plan=self.plan,
            requested_at=_REQUESTED_AT,
            valid_until="2026-08-06T11:00:00Z",  # later than selection request valid_until
        )
        other_inv_grant = build_av1_validation_v3_tier2_inventory_read_grant(
            protocol=self.protocol,
            plan=self.plan,
            request=other_inv_request,
            owner_principal=_OWNER,
            authorized_at=_AUTHORIZED_AT,
            valid_until=_GRANT_VALID_UNTIL,
        )
        other_inv_claim = build_av1_validation_v3_tier2_inventory_read_claim(
            protocol=self.protocol,
            plan=self.plan,
            request=other_inv_request,
            grant=other_inv_grant,
            claimed_at=_CLAIMED_AT,
        )
        other_inventory_context = AV1ValidationV3Tier2InventoryReadContext(
            plan=self.plan,
            request=other_inv_request,
            grant=other_inv_grant,
            claim=other_inv_claim,
        )
        # The selection request was built with the original inventory request;
        # rebuilding with other_inv_request produces a different ID → binding mismatch.
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2SelectionAuthorizationError,
            "not bound to its plan",
        ):
            assert_av1_validation_v3_tier2_selection_context(
                self.protocol,
                self.selection_context,
                other_inventory_context,
                as_of=_SELECTED_AT,
            )

    def test_window_rejection(self) -> None:
        # as_of before claim timestamp
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2SelectionAuthorizationError,
            "not active",
        ):
            assert_av1_validation_v3_tier2_selection_context(
                self.protocol,
                self.selection_context,
                self.inventory_context,
                as_of="2026-08-03T14:59:59Z",  # before claimed_at
            )

        # as_of after grant expiry (grant_valid_until = 2026-08-06T09:00:00Z)
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2SelectionAuthorizationError,
            "not active",
        ):
            assert_av1_validation_v3_tier2_selection_context(
                self.protocol,
                self.selection_context,
                self.inventory_context,
                as_of="2026-08-06T09:00:01Z",  # after grant expiry
            )

    def test_authority_tamper_rejection(self) -> None:
        # Flip tier2_selection_execution_authorized on request to True - should fail
        payload = copy.deepcopy(self.selection_context.request.to_payload())
        payload["tier2_selection_execution_authorized"] = True
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2SelectionAuthorizationError,
            "constants are invalid",
        ):
            av1_validation_v3_tier2_selection_request_from_payload(payload)

        # Flip tier2_selection_execution_authorized on grant to False - should fail
        grant_payload = copy.deepcopy(self.selection_context.grant.to_payload())
        grant_payload["tier2_selection_execution_authorized"] = False
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2SelectionAuthorizationError,
            "constants are invalid",
        ):
            av1_validation_v3_tier2_selection_grant_from_payload(grant_payload)

        claim_payload = copy.deepcopy(self.selection_context.claim.to_payload())
        claim_payload["qualification_key_read_authorized"] = False
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2SelectionAuthorizationError,
            "constants are invalid",
        ):
            av1_validation_v3_tier2_selection_claim_from_payload(claim_payload)

        # Flip private_inventory_read_authorized to True on grant - should fail
        grant_payload2 = copy.deepcopy(self.selection_context.grant.to_payload())
        grant_payload2["private_inventory_read_authorized"] = True
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2SelectionAuthorizationError,
            "constants are invalid",
        ):
            av1_validation_v3_tier2_selection_grant_from_payload(grant_payload2)

    def test_publication_modes_and_conflicts(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir(mode=0o700)
            output = root / "artifacts"

            # First publication: created=True
            result1 = publish_av1_validation_v3_tier2_selection_claim(
                claim=self.selection_context.claim,
                output_root=output,
                repository_root=repository,
            )
            self.assertTrue(result1.created)
            self.assertEqual(result1.claim, self.selection_context.claim)

            # Second identical publication: created=False (idempotent)
            result2 = publish_av1_validation_v3_tier2_selection_claim(
                claim=self.selection_context.claim,
                output_root=output,
                repository_root=repository,
            )
            self.assertFalse(result2.created)
            self.assertEqual(result2.claim, self.selection_context.claim)

            # Different claim with same grant_id → conflict
            # Build a different claim under the same grant
            other_claim = build_av1_validation_v3_tier2_selection_claim(
                protocol=self.protocol,
                plan=self.plan,
                inner_inventory_request=self.inventory_context.request,
                request=self.selection_context.request,
                grant=self.selection_context.grant,
                inner_inventory_claim=build_av1_validation_v3_tier2_inventory_read_claim(
                    protocol=self.protocol,
                    plan=self.plan,
                    request=self.inventory_context.request,
                    grant=self.inventory_context.grant,
                    claimed_at="2026-08-03T15:01:00Z",  # different time
                ),
                claimed_at="2026-08-03T15:01:00Z",
            )
            with self.assertRaisesRegex(ValueError, "already consumed"):
                publish_av1_validation_v3_tier2_selection_claim(
                    claim=other_claim,
                    output_root=output,
                    repository_root=repository,
                )

            record = build_av1_validation_v3_tier2_selection_record(
                protocol=self.protocol,
                plan=self.plan,
                sources=_sources(animation=(1,), typical=(2,)),
                qualification_key=self.key,
                selected_at=_SELECTED_AT,
            )
            unbound_claim = build_av1_validation_v3_tier2_selection_claim(
                protocol=self.protocol,
                plan=self.plan,
                inner_inventory_request=self.inventory_context.request,
                request=self.selection_context.request,
                grant=self.selection_context.grant,
                inner_inventory_claim=self.inventory_context.claim,
                claimed_at="2026-08-03T15:02:00Z",
            )
            with self.assertRaisesRegex(
                AV1ValidationV3Tier1PublicationError,
                "not bound to its claim",
            ):
                publish_av1_validation_v3_tier2_selection_record(
                    record=record,
                    claim=unbound_claim,
                    output_root=output,
                    repository_root=repository,
                )

    def test_strict_ordering(self) -> None:
        events: list[str] = []

        def fake_adapter(*_args: object, **_kwargs: object) -> AV1ValidationV3Tier2Inventory:
            events.append("db_read")
            _insert_item(self.connection, 1, "animation", "0" * 39 + "1")
            _insert_item(self.connection, 2, "typical", "0" * 39 + "2")
            return load_av1_validation_v3_tier2_inventory(
                self.connection,
                config=self.config,
                protocol=self.protocol,
                read_context=self.inventory_context,
                config_snapshot_bytes=self.snapshot_bytes,
                clock=lambda: _SELECTED_AT,
            )

        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir(mode=0o700)
            output = root / "artifacts"

            with (
                patch(
                    "mediaforce.tuning.av1_validation_v3_tier2_selection_operation."
                    "publish_av1_validation_v3_tier2_selection_claim",
                    side_effect=lambda **kwargs: events.append("outer_claim")
                    or publish_av1_validation_v3_tier2_selection_claim(**kwargs),
                ),
                patch(
                    "mediaforce.tuning.av1_validation_v3_tier2_inventory_operation."
                    "publish_av1_validation_v3_tier2_inventory_read_claim",
                    side_effect=lambda **kwargs: events.append("inner_claim")
                    or publish_av1_validation_v3_tier2_inventory_read_claim(**kwargs),
                ),
            ):
                self._run_operation(
                    output_root=output,
                    repository_root=repository,
                    inventory_adapter=fake_adapter,
                )

        self.assertEqual(events, ["outer_claim", "inner_claim", "db_read"])

    def test_key_mismatch_terminal_failure(self) -> None:
        wrong_key = b"r" * 32  # Different from self.key
        _insert_item(self.connection, 1, "animation", "0" * 39 + "1")
        _insert_item(self.connection, 2, "typical", "0" * 39 + "2")

        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir(mode=0o700)
            output = root / "artifacts"

            with self.assertRaisesRegex(
                AV1ValidationV3Tier2SelectionOperationError,
                "does not match plan commitment",
            ):
                self._run_operation(
                    output_root=output,
                    repository_root=repository,
                    key=wrong_key,
                )

    def test_key_unavailable_terminal_failure(self) -> None:
        _insert_item(self.connection, 1, "animation", "0" * 39 + "1")
        _insert_item(self.connection, 2, "typical", "0" * 39 + "2")

        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir(mode=0o700)
            output = root / "artifacts"

            with (
                patch(
                    "mediaforce.tuning.av1_validation_v3_tier2_selection_operation."
                    "load_av1_validation_v3_qualification_key",
                    side_effect=AV1ValidationV3Tier1PublicationError(
                        "artifact_incomplete",
                        "AV1 v3 qualification key is unavailable",
                    ),
                ),
                self.assertRaisesRegex(
                    AV1ValidationV3Tier2SelectionOperationError,
                    "qualification key is unavailable",
                ),
            ):
                run_av1_validation_v3_tier2_selection(
                    self.connection,
                    config=self.config,
                    protocol=self.protocol,
                    selection_context=self.selection_context,
                    inventory_context=self.inventory_context,
                    config_snapshot_bytes=self.snapshot_bytes,
                    output_root=output,
                    repository_root=repository,
                    key_root=output,
                    clock=lambda: _SELECTED_AT,
                )

    def test_no_media_access(self) -> None:
        _insert_item(self.connection, 1, "animation", "0" * 39 + "1")
        _insert_item(self.connection, 2, "typical", "0" * 39 + "2")

        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir(mode=0o700)
            output = root / "artifacts"

            result = self._run_operation(
                output_root=output,
                repository_root=repository,
            )

        summary = result.to_public_summary()
        summary_text = json.dumps(summary)
        self.assertNotIn("/Users/", summary_text)
        self.assertNotIn("/tmp/", summary_text)
        self.assertNotIn("/private/", summary_text)

    def test_privacy_safe_summary(self) -> None:
        _insert_item(self.connection, 1, "animation", "0" * 39 + "1")
        _insert_item(self.connection, 2, "typical", "0" * 39 + "2")

        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir(mode=0o700)
            output = root / "artifacts"

            result = self._run_operation(
                output_root=output,
                repository_root=repository,
            )

        summary = result.to_public_summary()

        # Must not contain sensitive fields
        for forbidden in (
            "source_fingerprint",
            "candidate_count",
            "candidate_sources",
            "qualification_key_id",
        ):
            self.assertNotIn(forbidden, summary)

        # The selection record ID, digest, and path must not appear in the public summary
        self.assertNotIn("selection_record_id", summary)
        self.assertNotIn("selection_record_digest", summary)
        self.assertNotIn("selection_record_path", summary)

        # All authority fields must be False in public summary
        for field in AV1_VALIDATION_V3_TIER2_SELECTION_FALSE_AUTHORITY_FIELDS:
            self.assertFalse(summary[field], f"{field} should be False in public summary")
        self.assertFalse(
            summary["tier2_selection_execution_authorized"],
            "tier2_selection_execution_authorized must be False in public summary",
        )

        # Selection record must be marked as published
        self.assertTrue(summary["selection_record_published"])

    def test_replay_prevention(self) -> None:
        _insert_item(self.connection, 1, "animation", "0" * 39 + "1")
        _insert_item(self.connection, 2, "typical", "0" * 39 + "2")

        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir(mode=0o700)
            output = root / "artifacts"

            # First run succeeds
            result = self._run_operation(
                output_root=output,
                repository_root=repository,
            )
            self.assertTrue(result.outer_claim_publication.created)

            # Second run with same contexts fails on outer claim replay
            with self.assertRaisesRegex(
                AV1ValidationV3Tier2SelectionOperationError,
                "replay is not permitted",
            ):
                self._run_operation(
                    output_root=output,
                    repository_root=repository,
                )

    def test_cross_chain_owner_mismatch(self) -> None:
        # Inner inventory context with OWNER, outer selection with OTHER_OWNER
        other_selection_context = self._make_selection_context(
            self.plan,
            self.inventory_context,
            owner=_OTHER_OWNER,
        )
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2SelectionAuthorizationError,
            "owner does not match",
        ):
            assert_av1_validation_v3_tier2_selection_context(
                self.protocol,
                other_selection_context,
                self.inventory_context,
                as_of=_SELECTED_AT,
            )

    def test_cross_chain_plan_mismatch(self) -> None:
        # Build a different plan
        snapshot_bytes2 = build_av1_validation_v3_tier1_config_snapshot(self.config)
        config_sha256_2 = av1_validation_v3_tier1_config_sha256(snapshot_bytes2)
        other_plan = _plan_with_key_id(
            self.protocol,
            key_id=av1_validation_v3_qualification_key_id(b"r" * 32),
            config_sha256=config_sha256_2,
        )
        # Build an inventory context and selection context with the other plan
        other_inv_context = self._make_inventory_context(other_plan)
        other_sel_context = self._make_selection_context(other_plan, other_inv_context)

        # Now try cross-chain assert with selection_context on other_plan
        # but inventory_context on self.plan — plan IDs won't match.
        # The assert_selection_request_active rebuild uses self.inventory_context.request
        # (which belongs to self.plan), but other_sel_context.request was built with other_plan,
        # so the rebuilt request won't match → "not bound to its plan".
        with self.assertRaisesRegex(
            AV1ValidationV3Tier2SelectionAuthorizationError,
            "not bound to its plan",
        ):
            assert_av1_validation_v3_tier2_selection_context(
                self.protocol,
                other_sel_context,
                self.inventory_context,  # different plan
                as_of=_SELECTED_AT,
            )


def _plan_with_key_id(
    protocol: AV1ValidationProtocolV3,
    *,
    key_id: str,
    config_sha256: str,
    frozen_at: str = FROZEN_AT,
    valid_until: str = _PLAN_VALID_UNTIL,
) -> AV1ValidationV3QualificationPlan:
    return build_av1_validation_v3_qualification_plan(
        protocol=protocol,
        qualification_key_id=key_id,
        eligibility_predicate_sha256=SHA256,
        repository_commit=COMMIT,
        repository_tree=TREE,
        config_sha256=config_sha256,
        toolchain_sha256=SHA256,
        fixture_matrix_sha256=SHA256,
        frozen_at=frozen_at,
        valid_until=valid_until,
    )


def _config(root: Path) -> MediaforceConfig:
    return MediaforceConfig(
        raw={
            "media": {
                "libraries": [
                    {
                        "key": "tv",
                        "label": "TV",
                        "path": str(root / "tv"),
                        "type": "tv",
                        "availability": "production",
                    },
                    {
                        "key": "movies",
                        "label": "Movies",
                        "path": str(root / "movies"),
                        "type": "movie",
                        "availability": "production",
                    },
                ],
                "output_container": "mkv",
                "staging_root": str(root / "staging"),
                "archive_root": str(root / "archive"),
            },
            "video": _video_policy(),
            "audio": {},
            "subtitle": {},
            "planning": {},
            "validation": {},
            "overrides": [],
            "remote_hosts": [],
        },
        paths=ConfigPaths(
            project_root=root,
            config_path=root / "config.toml",
            db_path=root / "mediaforce.sqlite3",
            run_manifest_dir=root / "runs",
            web_state_dir=root / "web",
            review_dir=root / "review",
            runtime_settings_path=root / "runtime-settings.json",
            runtime_reservation_dir=root / "runtime-reservations",
        ),
    )


def _video_policy(**overrides: object) -> dict[str, object]:
    policy: dict[str, object] = {
        "target_size_mb": 1_000,
        "target_size_bytes": 1_000_000_000,
        "target_runtime_minutes": 60,
        "size_goal_schema_version": 1,
        "size_goal_mode": "normalized",
        "size_goal_source": "test",
        "compression_intent_schema_version": 1,
        "compression_intent": "balanced",
        "compression_intent_source": "test",
        "compression_intent_confirmed": True,
        "quality_metric": "vmaf",
        "target_vmaf": 85.0,
        "min_target_vmaf": 80.0,
        "sample_projection_tolerance_percent": 10,
        "final_output_tolerance_percent": 5,
        "max_encoded_percent": 95,
    }
    policy.update(overrides)
    return policy


def _insert_item(
    connection: Connection,
    item_id: int,
    trait: str,
    source_identity: str,
    *,
    rel_path: str | None = None,
    analyzer_policy_digest: str = "sha256:majority",
) -> None:
    rel_path = rel_path or f"tv/Series {item_id}/S01E01.mkv"
    result = connection.execute(
        library_items.insert().values(
            id=item_id,
            source_path=f"/private/{item_id}.mkv",
            rel_path=rel_path,
            media_root=rel_path.split("/", 1)[0],
            parent_dir=str(Path(rel_path).parent),
            file_name=Path(rel_path).name,
            container="mkv",
            size_bytes=2_000_000_000,
            mtime_ns=1,
            fingerprint=f"source-{item_id}",
            duration_seconds=3600.0,
            video_codec="h264",
            video_bitrate=4_000_000,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
            attachment_summary_json="[]",
            media_fingerprint_json=json.dumps(_fingerprint_summary(trait)),
            content_version_fingerprint=source_identity,
            status="discovered",
            priority_score=0,
            last_scan_id="synthetic",
            discovered_at="2026-08-03T00:00:00Z",
            last_seen_at="2026-08-03T00:00:00Z",
            updated_at="2026-08-03T00:00:00Z",
        )
    )
    local_id = int(result.inserted_primary_key[0])
    connection.execute(
        library_item_evidence_state.insert().values(
            library_item_id=local_id,
            evidence_kind=MEDIA_FINGERPRINT_EVIDENCE_KIND,
            state="current",
            summary_sha256=_summary_sha256(_fingerprint_summary(trait)),
            summary_schema_version=MEDIA_FINGERPRINT_SCHEMA_VERSION,
            analyzer_name=MEDIA_FINGERPRINT_TOOL_NAME,
            analyzer_version=MEDIA_FINGERPRINT_TOOL_VERSION,
            analyzer_runtime_version="ffmpeg-test",
            policy_hash=analyzer_policy_digest,
            decision_status="measured",
            attempt_count=1,
            updated_at="2026-08-03T00:00:00Z",
        )
    )


def _fingerprint_summary(trait: str) -> dict:
    return {
        "schema_version": MEDIA_FINGERPRINT_SCHEMA_VERSION,
        "analysis": {
            "sampled_frames": 120,
            "coverage": 1.0,
            "aggregate": _aggregate(trait),
            "audio_probe": {},
            "tool": {
                "name": MEDIA_FINGERPRINT_TOOL_NAME,
                "version": MEDIA_FINGERPRINT_TOOL_VERSION,
            },
        },
    }


def _aggregate(trait: str) -> dict:
    if trait == "animation":
        return {
            "duplicate_like_frame_fraction": 0.8,
            "edge_density_p90": 0.04,
        }
    return {
        "dark_frame_fraction": 0.0,
        "gradient_frame_fraction": 0.0,
        "banding_risk_score": 0.0,
        "high_motion_frame_fraction": 0.0,
        "ydif_p90": 0.0,
        "high_texture_frame_fraction": 0.0,
        "edge_density_p90": 0.0,
        "duplicate_like_frame_fraction": 0.0,
        "temporal_noise_proxy": 0.0,
        "chroma_instability": 0.0,
        "smooth_temporal_noise_fraction": 0.0,
    }


def _summary_sha256(summary: object) -> str:
    payload = json.dumps(summary, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


def _sources(
    *,
    animation: tuple[int, ...],
    typical: tuple[int, ...],
) -> list[AV1ValidationV3QualificationSource]:
    return [
        *(_source(index, "animation") for index in animation),
        *(_source(index, "typical") for index in typical),
    ]


def _source(index: int, trait: str) -> AV1ValidationV3QualificationSource:
    return AV1ValidationV3QualificationSource(
        source_fingerprint=f"sha256:{index:064x}",
        intent_level="balanced",
        exact_traits=(trait,),
        pipeline_ready=True,
    )


if __name__ == "__main__":
    unittest.main()
