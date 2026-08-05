from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3 import (
    AV1_VALIDATION_V3_EXPERIMENT_ID,
    AV1_VALIDATION_V3_PROTOCOL_VERSION,
    AV1ValidationProtocolV3,
    AV1ValidationV3QualificationSelection,
    AV1ValidationV3QualificationSource,
    assert_preregistered_av1_validation_protocol_v3,
    av1_validation_v3_id,
    av1_validation_v3_qualification_key_id,
    select_av1_validation_v3_tier2_sources,
)
from mediaforce.tuning.av1_validation_v3_qualification import (
    AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
    AV1ValidationV3QualificationPlan,
    assert_av1_validation_v3_qualification_plan_active,
)


AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier2_selection_record"
)
AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA_VERSION = 1
AV1_VALIDATION_V3_TIER2_SELECTION_CONTRACT_VERSION = "av1vt2sel1"
AV1_VALIDATION_V3_TIER2_SELECTION_AUTHORITY = "av1_v3_tier2_qualification_selection"
AV1_VALIDATION_V3_TIER2_SELECTION_PAYLOAD_DOMAIN = (
    "mediaforce:av1:v3:tier2-selection-record-payload:v1"
)
AV1_VALIDATION_V3_TIER2_SELECTION_CANDIDATE_DOMAIN = (
    "mediaforce:av1:v3:tier2-selection-record-candidates:v1"
)
AV1_VALIDATION_V3_TIER2_SELECTION_SELECTION_DOMAIN = (
    "mediaforce:av1:v3:tier2-selection-record-selections:v1"
)

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FALSE_AUTHORITY_FIELDS = (
    "tier1_execution_authorized",
    "tier1_coverage_eligible",
    "tier2_execution_authorized",
    "qualification_execution_authorized",
    "qualification_complete",
    "path_matrix_coverage_claimed",
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
)


class AV1ValidationV3Tier2SelectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2SelectionRecord:
    selection_record_id: str
    protocol_id: str
    protocol_payload_sha256: str
    plan_id: str
    plan_payload_sha256: str
    qualification_key_id: str
    eligibility_predicate_sha256: str
    selected_at: str
    candidate_sources: tuple[AV1ValidationV3QualificationSource, ...]
    candidate_count: int
    candidate_set_sha256: str
    selection_set_sha256: str
    selections: tuple[AV1ValidationV3QualificationSelection, ...]
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.selection_record_id.startswith("av1vtier2selection3_"):
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 selection record ID is invalid"
            )
        if not self.protocol_id.startswith("av1vprotocol3_"):
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 selection protocol ID is invalid"
            )
        if not self.plan_id.startswith("av1vqplan3_"):
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 selection plan ID is invalid"
            )
        if not re.fullmatch(r"av1vqkey3_[0-9a-f]{32}", self.qualification_key_id):
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 selection key ID is invalid"
            )
        for value, label in (
            (self.protocol_payload_sha256, "protocol digest"),
            (self.plan_payload_sha256, "plan digest"),
            (self.eligibility_predicate_sha256, "eligibility predicate digest"),
            (self.candidate_set_sha256, "candidate-set digest"),
            (self.selection_set_sha256, "selection-set digest"),
            (self.payload_sha256, "payload digest"),
        ):
            _require_sha256(value, label)
        _parse_timestamp(self.selected_at, "selection timestamp")
        candidate_payloads = _canonical_source_payloads(self.candidate_sources)
        canonical_candidate_sources = tuple(
            _source_from_payload(payload) for payload in candidate_payloads
        )
        if self.candidate_sources != canonical_candidate_sources:
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 candidate sources are not canonical"
            )
        if self.candidate_count != len(candidate_payloads):
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 candidate count is invalid"
            )
        if self.candidate_set_sha256 != _domain_payload_sha256(
            AV1_VALIDATION_V3_TIER2_SELECTION_CANDIDATE_DOMAIN,
            candidate_payloads,
        ):
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 candidate-set digest does not match"
            )
        if self.selections != tuple(
            sorted(self.selections, key=lambda selection: selection.stratum_name)
        ):
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 selections are not canonical"
            )
        if len({selection.stratum_id for selection in self.selections}) != len(
            self.selections
        ):
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 selections contain duplicate strata"
            )
        if len({selection.source_fingerprint for selection in self.selections}) != len(
            self.selections
        ):
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 selections reuse a source"
            )
        candidate_fingerprints = {
            source.source_fingerprint for source in self.candidate_sources
        }
        if any(
            selection.source_fingerprint not in candidate_fingerprints
            for selection in self.selections
        ):
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 selection is not present in the candidate set"
            )
        if self.selection_set_sha256 != _domain_payload_sha256(
            AV1_VALIDATION_V3_TIER2_SELECTION_SELECTION_DOMAIN,
            [selection.to_owner_payload() for selection in self.selections],
        ):
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 selection-set digest does not match"
            )
        semantic_payload = self.semantic_payload()
        if self.selection_record_id != av1_validation_v3_id(
            "tier2selection",
            semantic_payload,
        ):
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 selection record ID does not match its payload"
            )
        if self.payload_sha256 != _domain_payload_sha256(
            AV1_VALIDATION_V3_TIER2_SELECTION_PAYLOAD_DOMAIN,
            {"selection_record_id": self.selection_record_id, **semantic_payload},
        ):
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 selection payload digest does not match"
            )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA_VERSION,
            "protocol_version": AV1_VALIDATION_V3_PROTOCOL_VERSION,
            "experiment_id": AV1_VALIDATION_V3_EXPERIMENT_ID,
            "contract_version": AV1_VALIDATION_V3_TIER2_SELECTION_CONTRACT_VERSION,
            "authority": AV1_VALIDATION_V3_TIER2_SELECTION_AUTHORITY,
            "artifact_namespace": AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
            **_false_authority_fields(),
            "protocol_id": self.protocol_id,
            "protocol_payload_sha256": self.protocol_payload_sha256,
            "plan_id": self.plan_id,
            "plan_payload_sha256": self.plan_payload_sha256,
            "qualification_key_id": self.qualification_key_id,
            "eligibility_predicate_sha256": self.eligibility_predicate_sha256,
            "selected_at": self.selected_at,
            "candidate_sources": [
                _source_payload(source) for source in self.candidate_sources
            ],
            "candidate_count": self.candidate_count,
            "candidate_set_sha256": self.candidate_set_sha256,
            "selection_set_sha256": self.selection_set_sha256,
            "selections": [
                selection.to_owner_payload() for selection in self.selections
            ],
            "gate": "A0",
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "selection_record_id": self.selection_record_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }

    def to_public_summary(
        self,
        *,
        protocol: AV1ValidationProtocolV3,
        plan: AV1ValidationV3QualificationPlan,
    ) -> dict[str, Any]:
        assert_av1_validation_v3_tier2_selection_record(protocol, plan, self)
        payload = {
            "tier2_selection_record_valid": True,
            "artifact_kind": "tier2_selection_record",
            "gate": "A0",
            "tier": "tier2",
            "protocol_id": self.protocol_id,
            "protocol_payload_sha256": self.protocol_payload_sha256,
            "plan_id": self.plan_id,
            "plan_payload_sha256": self.plan_payload_sha256,
            "selected_strata": [
                selection.stratum_name for selection in self.selections
            ],
            **_false_authority_fields(),
        }
        _assert_public_summary_safe(payload)
        return payload


def build_av1_validation_v3_tier2_selection_record(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    sources: Sequence[AV1ValidationV3QualificationSource],
    qualification_key: bytes,
    selected_at: str,
) -> AV1ValidationV3Tier2SelectionRecord:
    assert_av1_validation_v3_qualification_plan_active(
        protocol,
        plan,
        as_of=selected_at,
    )
    qualification_key_id = av1_validation_v3_qualification_key_id(qualification_key)
    if qualification_key_id != plan.qualification_key_id:
        raise AV1ValidationV3Tier2SelectionError(
            "AV1 v3 Tier 2 selection key does not match its commitment"
        )
    candidate_payloads = _canonical_source_payloads(sources)
    candidate_sources = tuple(
        _source_from_payload(payload) for payload in candidate_payloads
    )
    selections = select_av1_validation_v3_tier2_sources(
        protocol=protocol,
        sources=candidate_sources,
        qualification_key=qualification_key,
        expected_key_id=plan.qualification_key_id,
    )
    selection_payloads = [selection.to_owner_payload() for selection in selections]
    candidate_set_sha256 = _domain_payload_sha256(
        AV1_VALIDATION_V3_TIER2_SELECTION_CANDIDATE_DOMAIN,
        candidate_payloads,
    )
    selection_set_sha256 = _domain_payload_sha256(
        AV1_VALIDATION_V3_TIER2_SELECTION_SELECTION_DOMAIN,
        selection_payloads,
    )
    semantic_payload = _selection_record_semantic_payload(
        protocol=protocol,
        plan=plan,
        selected_at=selected_at,
        candidate_sources=candidate_sources,
        candidate_count=len(candidate_payloads),
        candidate_set_sha256=candidate_set_sha256,
        selection_set_sha256=selection_set_sha256,
        selections=selections,
    )
    selection_record_id = av1_validation_v3_id("tier2selection", semantic_payload)
    return AV1ValidationV3Tier2SelectionRecord(
        selection_record_id=selection_record_id,
        protocol_id=protocol.protocol_id,
        protocol_payload_sha256=protocol.payload_sha256,
        plan_id=plan.plan_id,
        plan_payload_sha256=plan.payload_sha256,
        qualification_key_id=qualification_key_id,
        eligibility_predicate_sha256=plan.eligibility_predicate_sha256,
        selected_at=selected_at,
        candidate_sources=candidate_sources,
        candidate_count=len(candidate_payloads),
        candidate_set_sha256=candidate_set_sha256,
        selection_set_sha256=selection_set_sha256,
        selections=selections,
        payload_sha256=_domain_payload_sha256(
            AV1_VALIDATION_V3_TIER2_SELECTION_PAYLOAD_DOMAIN,
            {"selection_record_id": selection_record_id, **semantic_payload},
        ),
    )


def assert_av1_validation_v3_tier2_selection_record(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    record: AV1ValidationV3Tier2SelectionRecord,
) -> None:
    assert_preregistered_av1_validation_protocol_v3(protocol)
    if (
        record.protocol_id != protocol.protocol_id
        or record.protocol_payload_sha256 != protocol.payload_sha256
        or record.plan_id != plan.plan_id
        or record.plan_payload_sha256 != plan.payload_sha256
        or record.qualification_key_id != plan.qualification_key_id
        or record.eligibility_predicate_sha256 != plan.eligibility_predicate_sha256
    ):
        raise AV1ValidationV3Tier2SelectionError(
            "AV1 v3 Tier 2 selection record is not bound to its plan"
        )
    assert_av1_validation_v3_qualification_plan_active(
        protocol,
        plan,
        as_of=record.selected_at,
    )
    expected_strata = tuple(stratum.stratum_id for stratum in protocol.tier2_strata)
    actual_strata = tuple(selection.stratum_id for selection in record.selections)
    if actual_strata != expected_strata:
        raise AV1ValidationV3Tier2SelectionError(
            "AV1 v3 Tier 2 selection record does not cover the frozen strata"
        )
    candidates_by_fingerprint = {
        source.source_fingerprint: source for source in record.candidate_sources
    }
    for stratum, selection in zip(
        protocol.tier2_strata, record.selections, strict=True
    ):
        source = candidates_by_fingerprint[selection.source_fingerprint]
        if (
            selection.stratum_name != stratum.name
            or source.intent_level != stratum.intent_level
            or source.exact_traits != stratum.exact_traits
            or not source.pipeline_ready
        ):
            raise AV1ValidationV3Tier2SelectionError(
                "AV1 v3 Tier 2 selection does not match its frozen stratum"
            )


def validate_av1_validation_v3_tier2_selection_record_sources(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    record: AV1ValidationV3Tier2SelectionRecord,
    sources: Sequence[AV1ValidationV3QualificationSource],
    qualification_key: bytes,
) -> None:
    rebuilt = build_av1_validation_v3_tier2_selection_record(
        protocol=protocol,
        plan=plan,
        sources=sources,
        qualification_key=qualification_key,
        selected_at=record.selected_at,
    )
    if rebuilt != record:
        raise AV1ValidationV3Tier2SelectionError(
            "AV1 v3 Tier 2 selection record does not match current candidates"
        )


def av1_validation_v3_tier2_selection_record_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3Tier2SelectionRecord:
    value = object_dict(payload)
    expected = {
        "selection_record_id",
        "schema",
        "schema_version",
        "protocol_version",
        "experiment_id",
        "contract_version",
        "authority",
        "artifact_namespace",
        *_FALSE_AUTHORITY_FIELDS,
        "protocol_id",
        "protocol_payload_sha256",
        "plan_id",
        "plan_payload_sha256",
        "qualification_key_id",
        "eligibility_predicate_sha256",
        "selected_at",
        "candidate_sources",
        "candidate_count",
        "candidate_set_sha256",
        "selection_set_sha256",
        "selections",
        "gate",
        "payload_sha256",
    }
    _require_exact_keys(value, expected, "selection record")
    _require_selection_contract(value)
    selections = tuple(
        _selection_from_payload(object_dict(item))
        for item in object_list(value.get("selections"))
    )
    record = AV1ValidationV3Tier2SelectionRecord(
        selection_record_id=str(value.get("selection_record_id") or ""),
        protocol_id=str(value.get("protocol_id") or ""),
        protocol_payload_sha256=str(value.get("protocol_payload_sha256") or ""),
        plan_id=str(value.get("plan_id") or ""),
        plan_payload_sha256=str(value.get("plan_payload_sha256") or ""),
        qualification_key_id=str(value.get("qualification_key_id") or ""),
        eligibility_predicate_sha256=str(
            value.get("eligibility_predicate_sha256") or ""
        ),
        selected_at=str(value.get("selected_at") or ""),
        candidate_sources=tuple(
            _source_from_payload(object_dict(item))
            for item in object_list(value.get("candidate_sources"))
        ),
        candidate_count=_strict_int(value.get("candidate_count"), "candidate count"),
        candidate_set_sha256=str(value.get("candidate_set_sha256") or ""),
        selection_set_sha256=str(value.get("selection_set_sha256") or ""),
        selections=selections,
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    if value != record.to_payload():
        raise AV1ValidationV3Tier2SelectionError(
            "AV1 v3 Tier 2 selection record payload is not exact"
        )
    return record


def serialize_av1_validation_v3_tier2_selection_record(
    record: AV1ValidationV3Tier2SelectionRecord,
) -> bytes:
    return canonical_json_bytes(record.to_payload()) + b"\n"


def load_av1_validation_v3_tier2_selection_record(
    path: Path,
) -> AV1ValidationV3Tier2SelectionRecord:
    raw = path.read_bytes()
    try:
        record = av1_validation_v3_tier2_selection_record_from_payload(
            object_dict(json.loads(raw.decode("utf-8")))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier2SelectionError(
            "AV1 v3 Tier 2 selection record is invalid"
        ) from exc
    if raw != serialize_av1_validation_v3_tier2_selection_record(record):
        raise AV1ValidationV3Tier2SelectionError(
            "AV1 v3 Tier 2 selection record bytes are not canonical"
        )
    return record


def _selection_record_semantic_payload(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    selected_at: str,
    candidate_sources: Sequence[AV1ValidationV3QualificationSource],
    candidate_count: int,
    candidate_set_sha256: str,
    selection_set_sha256: str,
    selections: Sequence[AV1ValidationV3QualificationSelection],
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA_VERSION,
        "protocol_version": AV1_VALIDATION_V3_PROTOCOL_VERSION,
        "experiment_id": AV1_VALIDATION_V3_EXPERIMENT_ID,
        "contract_version": AV1_VALIDATION_V3_TIER2_SELECTION_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_TIER2_SELECTION_AUTHORITY,
        "artifact_namespace": AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
        **_false_authority_fields(),
        "protocol_id": protocol.protocol_id,
        "protocol_payload_sha256": protocol.payload_sha256,
        "plan_id": plan.plan_id,
        "plan_payload_sha256": plan.payload_sha256,
        "qualification_key_id": plan.qualification_key_id,
        "eligibility_predicate_sha256": plan.eligibility_predicate_sha256,
        "selected_at": selected_at,
        "candidate_sources": [_source_payload(source) for source in candidate_sources],
        "candidate_count": candidate_count,
        "candidate_set_sha256": candidate_set_sha256,
        "selection_set_sha256": selection_set_sha256,
        "selections": [selection.to_owner_payload() for selection in selections],
        "gate": "A0",
    }


def _canonical_source_payloads(
    sources: Sequence[AV1ValidationV3QualificationSource],
) -> list[dict[str, Any]]:
    payloads = sorted(
        (_source_payload(source) for source in sources),
        key=lambda payload: payload["source_fingerprint"],
    )
    fingerprints = [payload["source_fingerprint"] for payload in payloads]
    if len(fingerprints) != len(set(fingerprints)):
        raise AV1ValidationV3Tier2SelectionError(
            "AV1 v3 Tier 2 candidates repeat a source fingerprint"
        )
    return payloads


def _source_payload(source: AV1ValidationV3QualificationSource) -> dict[str, Any]:
    return {
        "source_fingerprint": source.source_fingerprint,
        "intent_level": source.intent_level,
        "exact_traits": list(source.exact_traits),
        "pipeline_ready": source.pipeline_ready,
    }


def _source_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3QualificationSource:
    value = object_dict(payload)
    _require_exact_keys(
        value,
        {
            "source_fingerprint",
            "intent_level",
            "exact_traits",
            "pipeline_ready",
        },
        "candidate source",
    )
    return AV1ValidationV3QualificationSource(
        source_fingerprint=str(value.get("source_fingerprint") or ""),
        intent_level=str(value.get("intent_level") or ""),
        exact_traits=tuple(
            str(item) for item in object_list(value.get("exact_traits"))
        ),
        pipeline_ready=_required_bool(value.get("pipeline_ready")),
    )


def _selection_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3QualificationSelection:
    value = object_dict(payload)
    _require_exact_keys(
        value,
        {
            "stratum_id",
            "stratum_name",
            "source_fingerprint",
            "rank_sha256",
        },
        "selection",
    )
    return AV1ValidationV3QualificationSelection(
        stratum_id=str(value.get("stratum_id") or ""),
        stratum_name=str(value.get("stratum_name") or ""),
        source_fingerprint=str(value.get("source_fingerprint") or ""),
        rank_sha256=str(value.get("rank_sha256") or ""),
    )


def _require_selection_contract(value: Mapping[str, Any]) -> None:
    if (
        value.get("schema") != AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA
        or value.get("schema_version")
        != AV1_VALIDATION_V3_TIER2_SELECTION_SCHEMA_VERSION
        or value.get("protocol_version") != AV1_VALIDATION_V3_PROTOCOL_VERSION
        or value.get("experiment_id") != AV1_VALIDATION_V3_EXPERIMENT_ID
        or value.get("contract_version")
        != AV1_VALIDATION_V3_TIER2_SELECTION_CONTRACT_VERSION
        or value.get("authority") != AV1_VALIDATION_V3_TIER2_SELECTION_AUTHORITY
        or value.get("artifact_namespace") != AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE
        or any(value.get(field) is not False for field in _FALSE_AUTHORITY_FIELDS)
        or value.get("gate") != "A0"
    ):
        raise AV1ValidationV3Tier2SelectionError(
            "AV1 v3 Tier 2 selection contract is invalid"
        )


def _domain_payload_sha256(domain: str, payload: object) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "domain": domain,
                "payload": payload,
            }
        )
    ).hexdigest()
    return f"sha256:{digest}"


def _require_sha256(value: str, label: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise AV1ValidationV3Tier2SelectionError(
            f"AV1 v3 Tier 2 selection {label} is invalid"
        )


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV3Tier2SelectionError(
            f"AV1 v3 Tier 2 selection {label} is invalid"
        ) from exc
    canonical = parsed.astimezone(UTC)
    if (
        parsed.utcoffset() != UTC.utcoffset(None)
        or canonical.microsecond != 0
        or value != canonical.isoformat().replace("+00:00", "Z")
    ):
        raise AV1ValidationV3Tier2SelectionError(
            f"AV1 v3 Tier 2 selection {label} must use canonical UTC"
        )
    return canonical


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise AV1ValidationV3Tier2SelectionError(
            f"AV1 v3 Tier 2 {label} keys are invalid"
        )


def _strict_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AV1ValidationV3Tier2SelectionError(
            f"AV1 v3 Tier 2 selection {label} is invalid"
        )
    return value


def _required_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise AV1ValidationV3Tier2SelectionError(
            "AV1 v3 Tier 2 selection boolean is invalid"
        )
    return value


def _false_authority_fields() -> dict[str, bool]:
    return dict.fromkeys(_FALSE_AUTHORITY_FIELDS, False)


def _assert_public_summary_safe(payload: Mapping[str, Any]) -> None:
    try:
        assert_av1_cold_start_public_payload_safe(payload)
    except ValueError as exc:
        raise AV1ValidationV3Tier2SelectionError(
            "AV1 v3 Tier 2 public selection summary contains private data"
        ) from exc
