from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any, Literal, Mapping, Sequence

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3 import (
    AV1_VALIDATION_V3_EXPERIMENT_ID,
    AV1_VALIDATION_V3_GATE_SEQUENCE,
    AV1_VALIDATION_V3_QUALIFICATION_AUTHORITY,
    AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
    AV1_VALIDATION_V3_RECOVERY_PATHS,
    AV1_VALIDATION_V3_PROTOCOL_VERSION,
    AV1ValidationProtocolV3,
    AV1ValidationV3Error,
    assert_av1_validation_v3_protocol_active,
    av1_validation_v3_id,
)
from mediaforce.tuning.av1_validation_derivation import (
    AV1_VALIDATION_DERIVATION_REASON_CODES,
    AV1_VALIDATION_DERIVATION_RUNTIME_FAILURE_REASON_CODES,
)


AV1_VALIDATION_V3_QUALIFICATION_PLAN_SCHEMA = (
    "mediaforce.av1_cold_start_v3_qualification_plan"
)
AV1_VALIDATION_V3_QUALIFICATION_ATTESTATION_SCHEMA = (
    "mediaforce.av1_cold_start_v3_qualification_attestation"
)
AV1_VALIDATION_V3_QUALIFICATION_SCHEMA_VERSION = 1
AV1_VALIDATION_V3_QUALIFICATION_CONTRACT_VERSION = "av1vq1"

AV1ValidationV3QualificationTier = Literal["tier1", "tier2"]
AV1ValidationV3QualificationPathKind = Literal[
    "recovery",
    "runtime_failure",
    "success",
    "terminal",
]

_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_KEY_ID_RE = re.compile(r"av1vqkey3_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class AV1ValidationV3QualificationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3QualificationPath:
    tier: AV1ValidationV3QualificationTier
    path_kind: AV1ValidationV3QualificationPathKind
    path_name: str
    candidate_configuration: str | None
    stratum_id: str | None
    passed: bool

    def __post_init__(self) -> None:
        if self.tier not in {"tier1", "tier2"}:
            raise AV1ValidationV3QualificationError("AV1 v3 qualification tier is unsupported")
        if self.path_kind not in {
            "recovery",
            "runtime_failure",
            "success",
            "terminal",
        }:
            raise AV1ValidationV3QualificationError("AV1 v3 qualification path kind is unsupported")
        if not self.path_name:
            raise AV1ValidationV3QualificationError("AV1 v3 qualification path name is required")
        if self.path_kind == "success":
            if self.candidate_configuration is None:
                raise AV1ValidationV3QualificationError(
                    "AV1 v3 qualification success path requires a candidate configuration"
                )
            if self.tier == "tier1" and self.stratum_id is not None:
                raise AV1ValidationV3QualificationError(
                    "AV1 v3 Tier 1 qualification cannot bind a private stratum"
                )
            if self.tier == "tier2" and self.stratum_id is None:
                raise AV1ValidationV3QualificationError(
                    "AV1 v3 Tier 2 qualification requires a private stratum"
                )
        elif self.candidate_configuration is not None or self.stratum_id is not None:
            raise AV1ValidationV3QualificationError(
                "AV1 v3 non-success qualification paths cannot bind a candidate or stratum"
            )
        if self.path_kind != "success" and self.tier != "tier1":
            raise AV1ValidationV3QualificationError(
                "AV1 v3 fault-path coverage must use non-private Tier 1 fixtures"
            )
        if not isinstance(self.passed, bool):
            raise AV1ValidationV3QualificationError("AV1 v3 qualification result must be boolean")

    @property
    def matrix_key(self) -> tuple[str, str, str, str | None, str | None]:
        return (
            self.tier,
            self.path_kind,
            self.path_name,
            self.candidate_configuration,
            self.stratum_id,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "path_kind": self.path_kind,
            "path_name": self.path_name,
            "candidate_configuration": self.candidate_configuration,
            "stratum_id": self.stratum_id,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationV3QualificationPlan:
    plan_id: str
    protocol_id: str
    protocol_payload_sha256: str
    qualification_key_id: str
    eligibility_predicate_sha256: str
    repository_commit: str
    repository_tree: str
    config_sha256: str
    toolchain_sha256: str
    fixture_matrix_sha256: str
    expected_path_matrix_sha256: str
    frozen_at: str
    valid_until: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.protocol_id.startswith("av1vprotocol3_"):
            raise AV1ValidationV3QualificationError("AV1 v3 qualification protocol ID is invalid")
        if not _KEY_ID_RE.fullmatch(self.qualification_key_id):
            raise AV1ValidationV3QualificationError("AV1 v3 qualification key ID is invalid")
        for value, label in (
            (self.protocol_payload_sha256, "protocol digest"),
            (self.eligibility_predicate_sha256, "eligibility predicate digest"),
            (self.config_sha256, "config digest"),
            (self.toolchain_sha256, "toolchain digest"),
            (self.fixture_matrix_sha256, "fixture-matrix digest"),
            (self.expected_path_matrix_sha256, "expected path-matrix digest"),
        ):
            _require_sha256(value, label)
        for value, label in (
            (self.repository_commit, "repository commit"),
            (self.repository_tree, "repository tree"),
        ):
            if not _GIT_OBJECT_ID_RE.fullmatch(value):
                raise AV1ValidationV3QualificationError(f"AV1 v3 {label} is invalid")
        frozen_at = _parse_timestamp(self.frozen_at, "qualification plan freeze")
        valid_until = _parse_timestamp(self.valid_until, "qualification plan expiration")
        if valid_until <= frozen_at:
            raise AV1ValidationV3QualificationError(
                "AV1 v3 qualification plan expiration is invalid"
            )
        semantic_payload = self.semantic_payload()
        if self.plan_id != av1_validation_v3_id("qplan", semantic_payload):
            raise AV1ValidationV3QualificationError(
                "AV1 v3 qualification plan ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256({"plan_id": self.plan_id, **semantic_payload}):
            raise AV1ValidationV3QualificationError(
                "AV1 v3 qualification plan digest does not match its payload"
            )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_QUALIFICATION_PLAN_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_QUALIFICATION_SCHEMA_VERSION,
            "protocol_version": AV1_VALIDATION_V3_PROTOCOL_VERSION,
            "experiment_id": AV1_VALIDATION_V3_EXPERIMENT_ID,
            "contract_version": AV1_VALIDATION_V3_QUALIFICATION_CONTRACT_VERSION,
            "authority": AV1_VALIDATION_V3_QUALIFICATION_AUTHORITY,
            "artifact_namespace": AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
            "evidence_eligible": False,
            "runtime_execution_authorized": False,
            "private_inventory_read_authorized": False,
            "qualification_execution_authorized": False,
            "protocol_id": self.protocol_id,
            "protocol_payload_sha256": self.protocol_payload_sha256,
            "qualification_key_id": self.qualification_key_id,
            "eligibility_predicate_sha256": self.eligibility_predicate_sha256,
            "repository_commit": self.repository_commit,
            "repository_tree": self.repository_tree,
            "config_sha256": self.config_sha256,
            "toolchain_sha256": self.toolchain_sha256,
            "fixture_matrix_sha256": self.fixture_matrix_sha256,
            "expected_path_matrix_sha256": self.expected_path_matrix_sha256,
            "frozen_at": self.frozen_at,
            "valid_until": self.valid_until,
            "gate": "A0",
        }

    def to_payload(self) -> dict[str, Any]:
        return {"plan_id": self.plan_id, **self.semantic_payload(), "payload_sha256": self.payload_sha256}


@dataclass(frozen=True, slots=True)
class AV1ValidationV3QualificationAttestation:
    attestation_id: str
    plan_id: str
    plan_payload_sha256: str
    repository_commit: str
    repository_tree: str
    config_sha256: str
    toolchain_sha256: str
    fixture_matrix_sha256: str
    expected_path_matrix_sha256: str
    paths: tuple[AV1ValidationV3QualificationPath, ...]
    cleanup_passed: bool
    runtime_paused: bool
    accepted_at: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.plan_id.startswith("av1vqplan3_"):
            raise AV1ValidationV3QualificationError("AV1 v3 qualification attestation plan ID is invalid")
        for value, label in (
            (self.plan_payload_sha256, "qualification plan digest"),
            (self.config_sha256, "config digest"),
            (self.toolchain_sha256, "toolchain digest"),
            (self.fixture_matrix_sha256, "fixture-matrix digest"),
            (self.expected_path_matrix_sha256, "expected path-matrix digest"),
        ):
            _require_sha256(value, label)
        for value, label in (
            (self.repository_commit, "repository commit"),
            (self.repository_tree, "repository tree"),
        ):
            if not _GIT_OBJECT_ID_RE.fullmatch(value):
                raise AV1ValidationV3QualificationError(f"AV1 v3 {label} is invalid")
        if self.paths != tuple(sorted(self.paths, key=lambda path: path.matrix_key)):
            raise AV1ValidationV3QualificationError("AV1 v3 qualification paths are not canonical")
        if len({path.matrix_key for path in self.paths}) != len(self.paths):
            raise AV1ValidationV3QualificationError("AV1 v3 qualification paths are duplicated")
        if not self.cleanup_passed or not self.runtime_paused:
            raise AV1ValidationV3QualificationError(
                "AV1 v3 qualification must prove cleanup and paused runtime"
            )
        _parse_timestamp(self.accepted_at, "qualification acceptance")
        semantic_payload = self.semantic_payload()
        if self.attestation_id != av1_validation_v3_id("qattestation", semantic_payload):
            raise AV1ValidationV3QualificationError(
                "AV1 v3 qualification attestation ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256({
            "attestation_id": self.attestation_id,
            **semantic_payload,
        }):
            raise AV1ValidationV3QualificationError(
                "AV1 v3 qualification attestation digest does not match its payload"
            )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_QUALIFICATION_ATTESTATION_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_QUALIFICATION_SCHEMA_VERSION,
            "protocol_version": AV1_VALIDATION_V3_PROTOCOL_VERSION,
            "experiment_id": AV1_VALIDATION_V3_EXPERIMENT_ID,
            "contract_version": AV1_VALIDATION_V3_QUALIFICATION_CONTRACT_VERSION,
            "authority": AV1_VALIDATION_V3_QUALIFICATION_AUTHORITY,
            "artifact_namespace": AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
            "evidence_eligible": False,
            "runtime_execution_authorized": False,
            "plan_id": self.plan_id,
            "plan_payload_sha256": self.plan_payload_sha256,
            "repository_commit": self.repository_commit,
            "repository_tree": self.repository_tree,
            "config_sha256": self.config_sha256,
            "toolchain_sha256": self.toolchain_sha256,
            "fixture_matrix_sha256": self.fixture_matrix_sha256,
            "expected_path_matrix_sha256": self.expected_path_matrix_sha256,
            "paths": [path.to_payload() for path in self.paths],
            "cleanup_passed": self.cleanup_passed,
            "runtime_paused": self.runtime_paused,
            "accepted_at": self.accepted_at,
            "gate": "A",
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "attestation_id": self.attestation_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }

    def to_public_summary(
        self,
        *,
        protocol: AV1ValidationProtocolV3,
        plan: AV1ValidationV3QualificationPlan,
    ) -> dict[str, Any]:
        assert_av1_validation_v3_qualification_attestation(protocol, plan, self)
        payload = {
            "qualification_valid": True,
            "tier1_complete": True,
            "tier2_complete": True,
            "total_path_count": len(self.paths),
            "runtime_execution_authorized": False,
            "evidence_eligible": False,
        }
        assert_av1_cold_start_public_payload_safe(payload)
        return payload


def build_av1_validation_v3_qualification_plan(
    *,
    protocol: AV1ValidationProtocolV3,
    qualification_key_id: str,
    eligibility_predicate_sha256: str,
    repository_commit: str,
    repository_tree: str,
    config_sha256: str,
    toolchain_sha256: str,
    fixture_matrix_sha256: str,
    frozen_at: str,
    valid_until: str,
) -> AV1ValidationV3QualificationPlan:
    _assert_protocol_active(protocol, as_of=frozen_at)
    if _parse_timestamp(valid_until, "qualification plan expiration") > _parse_timestamp(
        protocol.valid_until,
        "protocol expiration",
    ):
        raise AV1ValidationV3QualificationError(
            "AV1 v3 qualification plan cannot outlive its protocol"
        )
    expected_path_matrix_sha256 = av1_validation_v3_expected_path_matrix_sha256(protocol)
    semantic_payload = _qualification_plan_semantic_payload(
        protocol_id=protocol.protocol_id,
        protocol_payload_sha256=protocol.payload_sha256,
        qualification_key_id=qualification_key_id,
        eligibility_predicate_sha256=eligibility_predicate_sha256,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
        config_sha256=config_sha256,
        toolchain_sha256=toolchain_sha256,
        fixture_matrix_sha256=fixture_matrix_sha256,
        expected_path_matrix_sha256=expected_path_matrix_sha256,
        frozen_at=frozen_at,
        valid_until=valid_until,
    )
    plan_id = av1_validation_v3_id("qplan", semantic_payload)
    return AV1ValidationV3QualificationPlan(
        plan_id=plan_id,
        protocol_id=protocol.protocol_id,
        protocol_payload_sha256=protocol.payload_sha256,
        qualification_key_id=qualification_key_id,
        eligibility_predicate_sha256=eligibility_predicate_sha256,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
        config_sha256=config_sha256,
        toolchain_sha256=toolchain_sha256,
        fixture_matrix_sha256=fixture_matrix_sha256,
        expected_path_matrix_sha256=expected_path_matrix_sha256,
        frozen_at=frozen_at,
        valid_until=valid_until,
        payload_sha256=_payload_sha256({"plan_id": plan_id, **semantic_payload}),
    )


def build_av1_validation_v3_qualification_attestation(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    paths: Sequence[AV1ValidationV3QualificationPath],
    cleanup_passed: bool,
    runtime_paused: bool,
    accepted_at: str,
) -> AV1ValidationV3QualificationAttestation:
    assert_av1_validation_v3_qualification_plan_active(
        protocol,
        plan,
        as_of=accepted_at,
    )
    _validate_complete_path_matrix(protocol, paths)
    canonical_paths = tuple(sorted(paths, key=lambda path: path.matrix_key))
    semantic_payload = _qualification_attestation_semantic_payload(
        plan=plan,
        paths=canonical_paths,
        cleanup_passed=cleanup_passed,
        runtime_paused=runtime_paused,
        accepted_at=accepted_at,
    )
    attestation_id = av1_validation_v3_id("qattestation", semantic_payload)
    return AV1ValidationV3QualificationAttestation(
        attestation_id=attestation_id,
        plan_id=plan.plan_id,
        plan_payload_sha256=plan.payload_sha256,
        repository_commit=plan.repository_commit,
        repository_tree=plan.repository_tree,
        config_sha256=plan.config_sha256,
        toolchain_sha256=plan.toolchain_sha256,
        fixture_matrix_sha256=plan.fixture_matrix_sha256,
        expected_path_matrix_sha256=plan.expected_path_matrix_sha256,
        paths=canonical_paths,
        cleanup_passed=cleanup_passed,
        runtime_paused=runtime_paused,
        accepted_at=accepted_at,
        payload_sha256=_payload_sha256({
            "attestation_id": attestation_id,
            **semantic_payload,
        }),
    )


def assert_av1_validation_v3_qualification_plan_active(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    *,
    as_of: str,
) -> None:
    _validate_plan_binding(protocol, plan)
    _assert_protocol_active(protocol, as_of=as_of)
    checked_at = _parse_timestamp(as_of, "qualification active-check timestamp")
    frozen_at = _parse_timestamp(plan.frozen_at, "qualification plan freeze")
    valid_until = _parse_timestamp(plan.valid_until, "qualification plan expiration")
    if checked_at < frozen_at or checked_at >= valid_until:
        raise AV1ValidationV3QualificationError(
            "AV1 v3 qualification plan is not active at the requested time"
        )


def assert_av1_validation_v3_qualification_attestation(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    attestation: AV1ValidationV3QualificationAttestation,
) -> None:
    assert_av1_validation_v3_qualification_plan_active(
        protocol,
        plan,
        as_of=attestation.accepted_at,
    )
    if (
        attestation.plan_id != plan.plan_id
        or attestation.plan_payload_sha256 != plan.payload_sha256
        or attestation.repository_commit != plan.repository_commit
        or attestation.repository_tree != plan.repository_tree
        or attestation.config_sha256 != plan.config_sha256
        or attestation.toolchain_sha256 != plan.toolchain_sha256
        or attestation.fixture_matrix_sha256 != plan.fixture_matrix_sha256
        or attestation.expected_path_matrix_sha256 != plan.expected_path_matrix_sha256
    ):
        raise AV1ValidationV3QualificationError(
            "AV1 v3 qualification attestation is not bound to its frozen plan"
        )
    _validate_complete_path_matrix(protocol, attestation.paths)


def build_av1_validation_v3_expected_qualification_paths(
    protocol: AV1ValidationProtocolV3,
) -> tuple[AV1ValidationV3QualificationPath, ...]:
    paths: list[AV1ValidationV3QualificationPath] = []
    for cell in protocol.candidate_cells:
        paths.append(AV1ValidationV3QualificationPath(
            tier="tier1",
            path_kind="success",
            path_name="complete_success",
            candidate_configuration=cell.name,
            stratum_id=None,
            passed=True,
        ))
    for reason_code in sorted(AV1_VALIDATION_DERIVATION_RUNTIME_FAILURE_REASON_CODES):
        paths.append(AV1ValidationV3QualificationPath(
            tier="tier1",
            path_kind="runtime_failure",
            path_name=reason_code,
            candidate_configuration=None,
            stratum_id=None,
            passed=True,
        ))
    for reason_code in sorted(
        AV1_VALIDATION_DERIVATION_REASON_CODES
        - AV1_VALIDATION_DERIVATION_RUNTIME_FAILURE_REASON_CODES
    ):
        paths.append(AV1ValidationV3QualificationPath(
            tier="tier1",
            path_kind="terminal",
            path_name=reason_code,
            candidate_configuration=None,
            stratum_id=None,
            passed=True,
        ))
    for recovery_path in AV1_VALIDATION_V3_RECOVERY_PATHS:
        paths.append(AV1ValidationV3QualificationPath(
            tier="tier1",
            path_kind="recovery",
            path_name=recovery_path,
            candidate_configuration=None,
            stratum_id=None,
            passed=True,
        ))
    for stratum in protocol.tier2_strata:
        for candidate_configuration in stratum.candidate_configurations:
            paths.append(AV1ValidationV3QualificationPath(
                tier="tier2",
                path_kind="success",
                path_name="complete_success",
                candidate_configuration=candidate_configuration,
                stratum_id=stratum.stratum_id,
                passed=True,
            ))
    return tuple(sorted(paths, key=lambda path: path.matrix_key))


def av1_validation_v3_expected_path_matrix_sha256(
    protocol: AV1ValidationProtocolV3,
) -> str:
    return _payload_sha256({
        "protocol_id": protocol.protocol_id,
        "paths": [
            path.to_payload()
            for path in build_av1_validation_v3_expected_qualification_paths(protocol)
        ],
    })


def serialize_av1_validation_v3_qualification_plan(
    plan: AV1ValidationV3QualificationPlan,
) -> bytes:
    return canonical_json_bytes(plan.to_payload()) + b"\n"


def serialize_av1_validation_v3_qualification_attestation(
    attestation: AV1ValidationV3QualificationAttestation,
) -> bytes:
    return canonical_json_bytes(attestation.to_payload()) + b"\n"


def load_av1_validation_v3_qualification_plan(
    path: Path,
) -> AV1ValidationV3QualificationPlan:
    raw = path.read_bytes()
    try:
        plan = av1_validation_v3_qualification_plan_from_payload(
            object_dict(json.loads(raw.decode("utf-8")))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3QualificationError("AV1 v3 qualification plan is invalid") from exc
    if raw != serialize_av1_validation_v3_qualification_plan(plan):
        raise AV1ValidationV3QualificationError("AV1 v3 qualification plan bytes are not canonical")
    return plan


def load_av1_validation_v3_qualification_attestation(
    path: Path,
) -> AV1ValidationV3QualificationAttestation:
    raw = path.read_bytes()
    try:
        attestation = av1_validation_v3_qualification_attestation_from_payload(
            object_dict(json.loads(raw.decode("utf-8")))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3QualificationError(
            "AV1 v3 qualification attestation is invalid"
        ) from exc
    if raw != serialize_av1_validation_v3_qualification_attestation(attestation):
        raise AV1ValidationV3QualificationError(
            "AV1 v3 qualification attestation bytes are not canonical"
        )
    return attestation


def av1_validation_v3_qualification_plan_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3QualificationPlan:
    value = object_dict(payload)
    expected = {
        "plan_id",
        "schema",
        "schema_version",
        "protocol_version",
        "experiment_id",
        "contract_version",
        "authority",
        "artifact_namespace",
        "evidence_eligible",
        "runtime_execution_authorized",
        "private_inventory_read_authorized",
        "qualification_execution_authorized",
        "protocol_id",
        "protocol_payload_sha256",
        "qualification_key_id",
        "eligibility_predicate_sha256",
        "repository_commit",
        "repository_tree",
        "config_sha256",
        "toolchain_sha256",
        "fixture_matrix_sha256",
        "expected_path_matrix_sha256",
        "frozen_at",
        "valid_until",
        "gate",
        "payload_sha256",
    }
    _require_exact_keys(value, expected, "qualification plan")
    _require_qualification_contract(
        value,
        AV1_VALIDATION_V3_QUALIFICATION_PLAN_SCHEMA,
        expected_gate="A0",
    )
    plan = AV1ValidationV3QualificationPlan(
        plan_id=str(value.get("plan_id") or ""),
        protocol_id=str(value.get("protocol_id") or ""),
        protocol_payload_sha256=str(value.get("protocol_payload_sha256") or ""),
        qualification_key_id=str(value.get("qualification_key_id") or ""),
        eligibility_predicate_sha256=str(value.get("eligibility_predicate_sha256") or ""),
        repository_commit=str(value.get("repository_commit") or ""),
        repository_tree=str(value.get("repository_tree") or ""),
        config_sha256=str(value.get("config_sha256") or ""),
        toolchain_sha256=str(value.get("toolchain_sha256") or ""),
        fixture_matrix_sha256=str(value.get("fixture_matrix_sha256") or ""),
        expected_path_matrix_sha256=str(value.get("expected_path_matrix_sha256") or ""),
        frozen_at=str(value.get("frozen_at") or ""),
        valid_until=str(value.get("valid_until") or ""),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    if value != plan.to_payload():
        raise AV1ValidationV3QualificationError("AV1 v3 qualification plan payload is not exact")
    return plan


def av1_validation_v3_qualification_attestation_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3QualificationAttestation:
    value = object_dict(payload)
    expected = {
        "attestation_id",
        "schema",
        "schema_version",
        "protocol_version",
        "experiment_id",
        "contract_version",
        "authority",
        "artifact_namespace",
        "evidence_eligible",
        "runtime_execution_authorized",
        "plan_id",
        "plan_payload_sha256",
        "repository_commit",
        "repository_tree",
        "config_sha256",
        "toolchain_sha256",
        "fixture_matrix_sha256",
        "expected_path_matrix_sha256",
        "paths",
        "cleanup_passed",
        "runtime_paused",
        "accepted_at",
        "gate",
        "payload_sha256",
    }
    _require_exact_keys(value, expected, "qualification attestation")
    _require_qualification_contract(
        value,
        AV1_VALIDATION_V3_QUALIFICATION_ATTESTATION_SCHEMA,
        expected_gate="A",
    )
    paths = tuple(
        _qualification_path_from_payload(object_dict(item))
        for item in object_list(value.get("paths"))
    )
    attestation = AV1ValidationV3QualificationAttestation(
        attestation_id=str(value.get("attestation_id") or ""),
        plan_id=str(value.get("plan_id") or ""),
        plan_payload_sha256=str(value.get("plan_payload_sha256") or ""),
        repository_commit=str(value.get("repository_commit") or ""),
        repository_tree=str(value.get("repository_tree") or ""),
        config_sha256=str(value.get("config_sha256") or ""),
        toolchain_sha256=str(value.get("toolchain_sha256") or ""),
        fixture_matrix_sha256=str(value.get("fixture_matrix_sha256") or ""),
        expected_path_matrix_sha256=str(value.get("expected_path_matrix_sha256") or ""),
        paths=paths,
        cleanup_passed=_required_bool(value.get("cleanup_passed")),
        runtime_paused=_required_bool(value.get("runtime_paused")),
        accepted_at=str(value.get("accepted_at") or ""),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    if value != attestation.to_payload():
        raise AV1ValidationV3QualificationError(
            "AV1 v3 qualification attestation payload is not exact"
        )
    return attestation


def _qualification_plan_semantic_payload(
    *,
    protocol_id: str,
    protocol_payload_sha256: str,
    qualification_key_id: str,
    eligibility_predicate_sha256: str,
    repository_commit: str,
    repository_tree: str,
    config_sha256: str,
    toolchain_sha256: str,
    fixture_matrix_sha256: str,
    expected_path_matrix_sha256: str,
    frozen_at: str,
    valid_until: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_QUALIFICATION_PLAN_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_QUALIFICATION_SCHEMA_VERSION,
        "protocol_version": AV1_VALIDATION_V3_PROTOCOL_VERSION,
        "experiment_id": AV1_VALIDATION_V3_EXPERIMENT_ID,
        "contract_version": AV1_VALIDATION_V3_QUALIFICATION_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_QUALIFICATION_AUTHORITY,
        "artifact_namespace": AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
        "evidence_eligible": False,
        "runtime_execution_authorized": False,
        "private_inventory_read_authorized": False,
        "qualification_execution_authorized": False,
        "protocol_id": protocol_id,
        "protocol_payload_sha256": protocol_payload_sha256,
        "qualification_key_id": qualification_key_id,
        "eligibility_predicate_sha256": eligibility_predicate_sha256,
        "repository_commit": repository_commit,
        "repository_tree": repository_tree,
        "config_sha256": config_sha256,
        "toolchain_sha256": toolchain_sha256,
        "fixture_matrix_sha256": fixture_matrix_sha256,
        "expected_path_matrix_sha256": expected_path_matrix_sha256,
        "frozen_at": frozen_at,
        "valid_until": valid_until,
        "gate": "A0",
    }


def _qualification_attestation_semantic_payload(
    *,
    plan: AV1ValidationV3QualificationPlan,
    paths: Sequence[AV1ValidationV3QualificationPath],
    cleanup_passed: bool,
    runtime_paused: bool,
    accepted_at: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_QUALIFICATION_ATTESTATION_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_QUALIFICATION_SCHEMA_VERSION,
        "protocol_version": AV1_VALIDATION_V3_PROTOCOL_VERSION,
        "experiment_id": AV1_VALIDATION_V3_EXPERIMENT_ID,
        "contract_version": AV1_VALIDATION_V3_QUALIFICATION_CONTRACT_VERSION,
        "authority": AV1_VALIDATION_V3_QUALIFICATION_AUTHORITY,
        "artifact_namespace": AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE,
        "evidence_eligible": False,
        "runtime_execution_authorized": False,
        "plan_id": plan.plan_id,
        "plan_payload_sha256": plan.payload_sha256,
        "repository_commit": plan.repository_commit,
        "repository_tree": plan.repository_tree,
        "config_sha256": plan.config_sha256,
        "toolchain_sha256": plan.toolchain_sha256,
        "fixture_matrix_sha256": plan.fixture_matrix_sha256,
        "expected_path_matrix_sha256": plan.expected_path_matrix_sha256,
        "paths": [path.to_payload() for path in paths],
        "cleanup_passed": cleanup_passed,
        "runtime_paused": runtime_paused,
        "accepted_at": accepted_at,
        "gate": "A",
    }


def _validate_plan_binding(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
) -> None:
    if (
        plan.protocol_id != protocol.protocol_id
        or plan.protocol_payload_sha256 != protocol.payload_sha256
        or plan.expected_path_matrix_sha256
        != av1_validation_v3_expected_path_matrix_sha256(protocol)
    ):
        raise AV1ValidationV3QualificationError(
            "AV1 v3 qualification plan is not bound to the frozen protocol"
        )
    if _parse_timestamp(plan.valid_until, "qualification plan expiration") > _parse_timestamp(
        protocol.valid_until,
        "protocol expiration",
    ):
        raise AV1ValidationV3QualificationError(
            "AV1 v3 qualification plan cannot outlive its protocol"
        )


def _validate_complete_path_matrix(
    protocol: AV1ValidationProtocolV3,
    paths: Sequence[AV1ValidationV3QualificationPath],
) -> None:
    expected = build_av1_validation_v3_expected_qualification_paths(protocol)
    if tuple(sorted(paths, key=lambda path: path.matrix_key)) != expected:
        raise AV1ValidationV3QualificationError(
            "AV1 v3 qualification path matrix is incomplete or invalid"
        )


def _qualification_path_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3QualificationPath:
    value = object_dict(payload)
    _require_exact_keys(value, {
        "tier",
        "path_kind",
        "path_name",
        "candidate_configuration",
        "stratum_id",
        "passed",
    }, "qualification path")
    return AV1ValidationV3QualificationPath(
        tier=str(value.get("tier") or ""),
        path_kind=str(value.get("path_kind") or ""),
        path_name=str(value.get("path_name") or ""),
        candidate_configuration=_optional_text(value.get("candidate_configuration")),
        stratum_id=_optional_text(value.get("stratum_id")),
        passed=_required_bool(value.get("passed")),
    )


def _require_qualification_contract(
    value: Mapping[str, Any],
    schema: str,
    *,
    expected_gate: str,
) -> None:
    if (
        value.get("schema") != schema
        or value.get("schema_version") != AV1_VALIDATION_V3_QUALIFICATION_SCHEMA_VERSION
        or value.get("protocol_version") != AV1_VALIDATION_V3_PROTOCOL_VERSION
        or value.get("experiment_id") != AV1_VALIDATION_V3_EXPERIMENT_ID
        or value.get("contract_version") != AV1_VALIDATION_V3_QUALIFICATION_CONTRACT_VERSION
        or value.get("authority") != AV1_VALIDATION_V3_QUALIFICATION_AUTHORITY
        or value.get("artifact_namespace") != AV1_VALIDATION_V3_QUALIFICATION_NAMESPACE
        or value.get("evidence_eligible") is not False
        or value.get("runtime_execution_authorized") is not False
        or value.get("gate") != expected_gate
        or expected_gate not in AV1_VALIDATION_V3_GATE_SEQUENCE
    ):
        raise AV1ValidationV3QualificationError("AV1 v3 qualification contract is invalid")
    if (
        schema == AV1_VALIDATION_V3_QUALIFICATION_PLAN_SCHEMA
        and (
            value.get("private_inventory_read_authorized") is not False
            or value.get("qualification_execution_authorized") is not False
        )
    ):
        raise AV1ValidationV3QualificationError("AV1 v3 qualification plan authority is invalid")


def _payload_sha256(payload: object) -> str:
    return f"sha256:{stable_json_hash(payload)}"


def _assert_protocol_active(protocol: AV1ValidationProtocolV3, *, as_of: str) -> None:
    try:
        assert_av1_validation_v3_protocol_active(protocol, as_of=as_of)
    except AV1ValidationV3Error as exc:
        raise AV1ValidationV3QualificationError(
            "AV1 v3 qualification protocol is not active at the requested time"
        ) from exc


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV3QualificationError(f"AV1 v3 {label} is invalid") from exc
    if parsed.tzinfo is None:
        raise AV1ValidationV3QualificationError(
            f"AV1 v3 {label} must include a timezone"
        )
    return parsed.astimezone(UTC)


def _require_sha256(value: str, label: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise AV1ValidationV3QualificationError(f"AV1 v3 {label} is invalid")


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise AV1ValidationV3QualificationError(f"AV1 v3 {label} keys are invalid")


def _required_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise AV1ValidationV3QualificationError("AV1 v3 boolean value is invalid")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise AV1ValidationV3QualificationError("AV1 v3 optional text value is invalid")
    return value
