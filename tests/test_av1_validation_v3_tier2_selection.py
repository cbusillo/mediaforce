import copy
import json
from pathlib import Path
import tempfile
import unittest

from mediaforce.tuning.av1_validation_v3 import (
    AV1ValidationV3Error,
    AV1ValidationV3QualificationSource,
    av1_validation_v3_qualification_key_id,
    load_av1_validation_protocol_v3,
)
from mediaforce.tuning.av1_validation_v3_qualification import (
    AV1ValidationV3QualificationError,
    build_av1_validation_v3_qualification_plan,
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


V3_PROTOCOL_PATH = Path("docs/validation/av1-cold-start-preregistration-v3.json")
SHA256 = f"sha256:{'a' * 64}"
COMMIT = "1" * 40
TREE = "2" * 40
FROZEN_AT = "2026-08-03T12:00:00Z"
VALID_UNTIL = "2026-08-04T12:00:00Z"
SELECTED_AT = "2026-08-03T13:00:00Z"


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
