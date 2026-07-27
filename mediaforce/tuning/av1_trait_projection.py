from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.encoding.fingerprint import (
    MEDIA_FINGERPRINT_SCHEMA_VERSION,
    MEDIA_FINGERPRINT_TOOL_NAME,
    MEDIA_FINGERPRINT_TOOL_VERSION,
    classify_media_fingerprint,
    media_fingerprint_policy_snapshot,
)
from mediaforce.tuning.av1_cold_start import (
    AV1_COLD_START_PROBE_SUPPORTED_INTENTS,
    AV1_COLD_START_TRAITS,
    AV1ColdStartTrait,
)


AV1_COLD_START_TRAIT_PROJECTION_SCHEMA = "mediaforce.av1_cold_start_trait_projection"
AV1_COLD_START_TRAIT_PROJECTION_SCHEMA_VERSION = 1
AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION = "acstp1"
AV1_COLD_START_TRAIT_IDENTITY_CONFIDENCE_FLOOR = 0.65
AV1_COLD_START_TRAIT_PROMOTION_CONFIDENCE_FLOOR = 0.70

AV1ColdStartTraitSelectorMatch = Literal["exact", "contains_all"]

_DIRECT_IDENTITY_FINDINGS: dict[str, AV1ColdStartTrait] = {
    "dark_luma": "darkness",
    "high_motion": "motion",
    "high_texture": "texture_detail",
}
_PROMOTED_ADVISORY_FINDINGS: dict[str, AV1ColdStartTrait] = {
    "animation_cues": "animation",
    "likely_film_grain": "grain_noise",
}
_REVIEW_ONLY_FINDINGS = frozenset({
    "audio_complexity",
    "compression_noise_advisory",
    "dark_gradient_banding_risk",
    "duplicate_cadence",
    "likely_analog_noise",
    "uncertain_noise_mix",
})
class AV1ColdStartTraitProjectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ColdStartTraitProjectionV1:
    projection_id: str
    evidence_status: str
    cell_traits: tuple[AV1ColdStartTrait, ...]
    review_advisories: tuple[str, ...]
    promoted_findings: tuple[str, ...]
    unrecognized_finding_count: int

    def __post_init__(self) -> None:
        if self.evidence_status not in {"measured", "unknown"}:
            raise AV1ColdStartTraitProjectionError("AV1 trait projection evidence status is unsupported")
        if not self.cell_traits or self.cell_traits != tuple(sorted(set(self.cell_traits))):
            raise AV1ColdStartTraitProjectionError("AV1 trait projection cell traits must be unique and sorted")
        if any(trait not in AV1_COLD_START_TRAITS for trait in self.cell_traits):
            raise AV1ColdStartTraitProjectionError("AV1 trait projection contains an unsupported cell trait")
        if self.review_advisories != tuple(sorted(set(self.review_advisories))):
            raise AV1ColdStartTraitProjectionError("AV1 review advisories must be unique and sorted")
        if self.promoted_findings != tuple(sorted(set(self.promoted_findings))):
            raise AV1ColdStartTraitProjectionError("AV1 promoted findings must be unique and sorted")
        if self.unrecognized_finding_count < 0:
            raise AV1ColdStartTraitProjectionError("AV1 unrecognized-finding count cannot be negative")
        expected_id = _projection_id(self.semantic_payload())
        if self.projection_id != expected_id:
            raise AV1ColdStartTraitProjectionError("AV1 trait projection ID does not match its payload")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_COLD_START_TRAIT_PROJECTION_SCHEMA,
            "schema_version": AV1_COLD_START_TRAIT_PROJECTION_SCHEMA_VERSION,
            "contract_version": AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION,
            "identity_confidence_floor": AV1_COLD_START_TRAIT_IDENTITY_CONFIDENCE_FLOOR,
            "promotion_confidence_floor": AV1_COLD_START_TRAIT_PROMOTION_CONFIDENCE_FLOOR,
            "evidence_status": self.evidence_status,
            "cell_traits": list(self.cell_traits),
            "review_advisories": list(self.review_advisories),
            "promoted_findings": list(self.promoted_findings),
            "unrecognized_finding_count": self.unrecognized_finding_count,
        }

    def to_payload(self) -> dict[str, Any]:
        return {"projection_id": self.projection_id, **self.semantic_payload()}


def project_av1_cold_start_traits(
        decision: Mapping[str, Any] | None,
) -> AV1ColdStartTraitProjectionV1:
    payload = object_dict(decision)
    if str(payload.get("status") or "") != "measured":
        return _build_projection(
            evidence_status="unknown",
            cell_traits=("unknown",),
            review_advisories=(),
            promoted_findings=(),
            unrecognized_finding_count=0,
        )

    identity_traits: set[AV1ColdStartTrait] = set()
    review_advisories: set[str] = set()
    promoted_findings: set[str] = set()
    unrecognized_finding_count = 0
    identity_ambiguous = False

    for finding in (object_dict(value) for value in object_list(payload.get("findings"))):
        finding_id = str(finding.get("id") or "").strip().casefold()
        confidence = float_value(finding.get("confidence"))
        advisory = bool(finding.get("advisory"))
        direct_trait = _DIRECT_IDENTITY_FINDINGS.get(finding_id)
        promoted_trait = _PROMOTED_ADVISORY_FINDINGS.get(finding_id)

        if direct_trait is not None and not advisory:
            if confidence >= AV1_COLD_START_TRAIT_IDENTITY_CONFIDENCE_FLOOR:
                identity_traits.add(direct_trait)
            else:
                review_advisories.add(finding_id)
                identity_ambiguous = True
            continue
        if promoted_trait is not None and advisory:
            if confidence >= AV1_COLD_START_TRAIT_PROMOTION_CONFIDENCE_FLOOR:
                identity_traits.add(promoted_trait)
                promoted_findings.add(finding_id)
            else:
                review_advisories.add(finding_id)
            continue
        if finding_id in _REVIEW_ONLY_FINDINGS:
            review_advisories.add(finding_id)
            continue
        unrecognized_finding_count += 1
        identity_ambiguous = True

    if identity_ambiguous:
        cell_traits: set[AV1ColdStartTrait] = {"unknown"}
    elif identity_traits:
        cell_traits = set(identity_traits)
        if len(identity_traits) > 1:
            cell_traits.add("mixed")
    else:
        cell_traits = {"typical"}

    return _build_projection(
        evidence_status="measured",
        cell_traits=tuple(sorted(cell_traits)),
        review_advisories=tuple(sorted(review_advisories)),
        promoted_findings=tuple(sorted(promoted_findings)),
        unrecognized_finding_count=unrecognized_finding_count,
    )


def project_av1_cold_start_fingerprint_summary(
        summary: Mapping[str, Any] | None,
) -> AV1ColdStartTraitProjectionV1:
    payload = object_dict(summary)
    analysis = object_dict(payload.get("analysis"))
    tool = object_dict(analysis.get("tool"))
    if int_value(payload.get("schema_version")) != MEDIA_FINGERPRINT_SCHEMA_VERSION:
        raise AV1ColdStartTraitProjectionError("Media fingerprint schema is incompatible with AV1 trait projection")
    if str(tool.get("name") or "") != MEDIA_FINGERPRINT_TOOL_NAME:
        raise AV1ColdStartTraitProjectionError("Media fingerprint analyzer is incompatible with AV1 trait projection")
    if str(tool.get("version") or "") != MEDIA_FINGERPRINT_TOOL_VERSION:
        raise AV1ColdStartTraitProjectionError("Media fingerprint analyzer version is incompatible with AV1 trait projection")
    return project_av1_cold_start_traits(classify_media_fingerprint(analysis))


def serialize_av1_cold_start_trait_projection(
        projection: AV1ColdStartTraitProjectionV1,
) -> bytes:
    return canonical_json_bytes(projection.to_payload())


def av1_trait_selector_producer_blockers(
        *,
        intent_level: str,
        required_traits: Sequence[str],
        match: AV1ColdStartTraitSelectorMatch,
        expected_prediction: str,
) -> tuple[str, ...]:
    normalized_traits = tuple(sorted({str(value).strip().casefold() for value in required_traits if str(value).strip()}))
    blockers: set[str] = set()
    if not normalized_traits:
        blockers.add("trait_selector_empty")
    if match not in {"exact", "contains_all"}:
        blockers.add("trait_selector_match_unsupported")
    if any(trait not in AV1_COLD_START_TRAITS for trait in normalized_traits):
        blockers.add("trait_unsupported")
    if expected_prediction not in {"recommended", "no_recommendation"}:
        blockers.add("expected_prediction_unsupported")
    if expected_prediction == "recommended" and intent_level not in AV1_COLD_START_PROBE_SUPPORTED_INTENTS:
        blockers.add("intent_cannot_produce_recommendation")
    if "low_motion_dialogue" in normalized_traits:
        blockers.add("low_motion_dialogue_analyzer_unavailable")
    if "unknown" in normalized_traits:
        blockers.add("unknown_evidence_rejected_by_request_contract")
    if "typical" in normalized_traits and len(normalized_traits) > 1:
        blockers.add("typical_trait_is_exclusive")
    if "unknown" in normalized_traits and len(normalized_traits) > 1:
        blockers.add("unknown_trait_is_exclusive")
    current_producer_traits = _current_producer_traits()
    unsupported_producer_traits = set(normalized_traits).difference(
        current_producer_traits,
        {"unknown", "low_motion_dialogue"},
    )
    if unsupported_producer_traits:
        blockers.add("trait_producer_unavailable")

    base_traits = set(normalized_traits).difference({"mixed", "typical", "unknown"})
    if match == "exact":
        if "mixed" in normalized_traits and len(base_traits) < 2:
            blockers.add("mixed_exact_selector_requires_two_base_traits")
        if len(base_traits) > 1 and "mixed" not in normalized_traits:
            blockers.add("multi_trait_exact_selector_requires_mixed")

    return tuple(sorted(blockers))


def av1_trait_selector_matches(
        projected_traits: Sequence[str],
        required_traits: Sequence[str],
        match: AV1ColdStartTraitSelectorMatch,
) -> bool:
    projected = {str(value).strip().casefold() for value in projected_traits if str(value).strip()}
    required = {str(value).strip().casefold() for value in required_traits if str(value).strip()}
    if match == "exact":
        return projected == required
    if match == "contains_all":
        return required.issubset(projected)
    raise AV1ColdStartTraitProjectionError("AV1 trait selector match is unsupported")


def _build_projection(
        *,
        evidence_status: str,
        cell_traits: tuple[AV1ColdStartTrait, ...],
        review_advisories: tuple[str, ...],
        promoted_findings: tuple[str, ...],
        unrecognized_finding_count: int,
) -> AV1ColdStartTraitProjectionV1:
    semantic_payload = {
        "schema": AV1_COLD_START_TRAIT_PROJECTION_SCHEMA,
        "schema_version": AV1_COLD_START_TRAIT_PROJECTION_SCHEMA_VERSION,
        "contract_version": AV1_COLD_START_TRAIT_PROJECTION_CONTRACT_VERSION,
        "identity_confidence_floor": AV1_COLD_START_TRAIT_IDENTITY_CONFIDENCE_FLOOR,
        "promotion_confidence_floor": AV1_COLD_START_TRAIT_PROMOTION_CONFIDENCE_FLOOR,
        "evidence_status": evidence_status,
        "cell_traits": list(cell_traits),
        "review_advisories": list(review_advisories),
        "promoted_findings": list(promoted_findings),
        "unrecognized_finding_count": unrecognized_finding_count,
    }
    return AV1ColdStartTraitProjectionV1(
        projection_id=_projection_id(semantic_payload),
        evidence_status=evidence_status,
        cell_traits=cell_traits,
        review_advisories=review_advisories,
        promoted_findings=promoted_findings,
        unrecognized_finding_count=unrecognized_finding_count,
    )


def _projection_id(payload: Mapping[str, Any]) -> str:
    return f"av1tp1_{stable_json_hash(payload)[:32]}"


def _current_producer_traits() -> frozenset[str]:
    findings = {
        str(value).strip().casefold()
        for value in media_fingerprint_policy_snapshot().get("findings", [])
        if str(value).strip()
    }
    producer_traits = {
        trait
        for finding_id, trait in {**_DIRECT_IDENTITY_FINDINGS, **_PROMOTED_ADVISORY_FINDINGS}.items()
        if finding_id in findings
    }
    producer_traits.add("typical")
    if len(producer_traits.difference({"typical"})) >= 2:
        producer_traits.add("mixed")
    return frozenset(producer_traits)
