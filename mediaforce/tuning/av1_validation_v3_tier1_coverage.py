from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3 import AV1ValidationProtocolV3, av1_validation_v3_id
from mediaforce.tuning.av1_validation_v3_qualification import AV1ValidationV3QualificationPlan
from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
    AV1ValidationV3Tier1FixtureOutcome,
)
from mediaforce.tuning.av1_validation_v3_tier1_grant import (
    AV1ValidationV3Tier1ExecutionGrant,
    assert_av1_validation_v3_tier1_grant_active,
)
from mediaforce.tuning.av1_validation_v3_tier1_request import (
    AV1ValidationV3Tier1AuthorizationRequest,
)


AV1_VALIDATION_V3_TIER1_COVERAGE_SCHEMA = "mediaforce.av1_cold_start_v3_tier1_coverage"
AV1_VALIDATION_V3_TIER1_COVERAGE_SCHEMA_VERSION = 1
AV1_VALIDATION_V3_TIER1_COVERAGE_CONTRACT_VERSION = "av1vt1c1"
AV1_VALIDATION_V3_TIER1_FIXTURE_IDS = (
    "tier1_flat_field",
    "tier1_high_detail_noise",
    "tier1_high_motion",
    "tier1_scene_change",
)
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class AV1ValidationV3Tier1CoverageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1FixtureCoverage:
    fixture_id: str
    command_plan_sha256: str
    content_sha256: str
    content_byte_count: int
    passed: bool
    failures: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.fixture_id not in AV1_VALIDATION_V3_TIER1_FIXTURE_IDS:
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 fixture coverage ID is invalid"
            )
        if not _SHA256_RE.fullmatch(self.command_plan_sha256):
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 command-plan digest is invalid"
            )
        if self.content_sha256 and not _SHA256_RE.fullmatch(self.content_sha256):
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 content digest is invalid"
            )
        if isinstance(self.content_byte_count, bool) or self.content_byte_count < 0:
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 content byte count is invalid"
            )
        if not isinstance(self.passed, bool):
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 fixture result is invalid"
            )
        if self.failures != tuple(sorted(set(self.failures))) or any(
            not reason for reason in self.failures
        ):
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 fixture failures are invalid"
            )
        if self.passed == bool(self.failures):
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 fixture result contradicts its failures"
            )
        if self.passed and (not self.content_sha256 or self.content_byte_count <= 0):
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 passed fixture content is invalid"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "fixture_id": self.fixture_id,
            "command_plan_sha256": self.command_plan_sha256,
            "content_sha256": self.content_sha256,
            "content_byte_count": self.content_byte_count,
            "passed": self.passed,
            "failures": list(self.failures),
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1CoverageAttestation:
    attestation_id: str
    plan_id: str
    plan_payload_sha256: str
    request_id: str
    request_payload_sha256: str
    grant_id: str
    grant_payload_sha256: str
    repository_commit: str
    repository_tree: str
    config_sha256: str
    toolchain_sha256: str
    fixture_matrix_sha256: str
    fixtures: tuple[AV1ValidationV3Tier1FixtureCoverage, ...]
    cleanup_passed: bool
    runtime_paused: bool
    completed_at: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.attestation_id.startswith("av1vtier1coverage3_"):
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 coverage attestation ID is invalid"
            )
        if not self.plan_id.startswith("av1vqplan3_"):
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 coverage plan ID is invalid"
            )
        if not self.request_id.startswith("av1vtier1request3_"):
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 coverage request ID is invalid"
            )
        if not self.grant_id.startswith("av1vtier1grant3_"):
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 coverage grant ID is invalid"
            )
        for value in (
            self.plan_payload_sha256,
            self.request_payload_sha256,
            self.grant_payload_sha256,
            self.config_sha256,
            self.toolchain_sha256,
            self.fixture_matrix_sha256,
            self.payload_sha256,
        ):
            if not _SHA256_RE.fullmatch(value):
                raise AV1ValidationV3Tier1CoverageError(
                    "AV1 v3 Tier 1 coverage digest is invalid"
                )
        if not _GIT_OBJECT_ID_RE.fullmatch(self.repository_commit) or not _GIT_OBJECT_ID_RE.fullmatch(
            self.repository_tree
        ):
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 coverage repository identity is invalid"
            )
        if tuple(fixture.fixture_id for fixture in self.fixtures) != AV1_VALIDATION_V3_TIER1_FIXTURE_IDS:
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 coverage fixture set is incomplete"
            )
        if not self.cleanup_passed or not self.runtime_paused:
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 coverage completion fields are invalid"
            )
        _parse_timestamp(self.completed_at)
        semantic = self.semantic_payload()
        if self.attestation_id != av1_validation_v3_id("tier1coverage", semantic):
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 coverage attestation ID is invalid"
            )
        expected_payload_sha256 = f"sha256:{stable_json_hash({'attestation_id': self.attestation_id, **semantic})}"
        if self.payload_sha256 != expected_payload_sha256:
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 coverage payload digest is invalid"
            )

    def semantic_payload(self) -> dict[str, Any]:
        return _semantic_payload(
            plan_id=self.plan_id,
            plan_payload_sha256=self.plan_payload_sha256,
            request_id=self.request_id,
            request_payload_sha256=self.request_payload_sha256,
            grant_id=self.grant_id,
            grant_payload_sha256=self.grant_payload_sha256,
            repository_commit=self.repository_commit,
            repository_tree=self.repository_tree,
            config_sha256=self.config_sha256,
            toolchain_sha256=self.toolchain_sha256,
            fixture_matrix_sha256=self.fixture_matrix_sha256,
            fixtures=self.fixtures,
            completed_at=self.completed_at,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "attestation_id": self.attestation_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }

    def to_public_summary(self) -> dict[str, object]:
        summary = {
            "attestation_id": self.attestation_id,
            "payload_sha256": self.payload_sha256,
            "tier": "tier1",
            "fixtures_passed": sum(fixture.passed for fixture in self.fixtures),
            "fixtures_total": len(self.fixtures),
            "qualification_complete": False,
            "tier2_complete": False,
            "evidence_eligible": False,
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


def build_av1_validation_v3_tier1_coverage_attestation(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1AuthorizationRequest,
    grant: AV1ValidationV3Tier1ExecutionGrant,
    outcomes: Sequence[AV1ValidationV3Tier1FixtureOutcome],
    cleanup_passed: bool,
    runtime_paused: bool,
    completed_at: str,
) -> AV1ValidationV3Tier1CoverageAttestation:
    assert_av1_validation_v3_tier1_grant_active(
        protocol, plan, request, grant, as_of=completed_at
    )
    if not cleanup_passed or not runtime_paused:
        raise AV1ValidationV3Tier1CoverageError(
            "AV1 v3 Tier 1 coverage requires paused runtime and complete cleanup"
        )
    if plan.fixture_matrix_sha256 != AV1_VALIDATION_V3_TIER1_MATRIX_SHA256:
        raise AV1ValidationV3Tier1CoverageError(
            "AV1 v3 Tier 1 coverage is not bound to the frozen fixture matrix"
        )
    for outcome in outcomes:
        if (
            outcome.matrix_sha256 != plan.fixture_matrix_sha256
            or outcome.request_id != request.request_id
            or outcome.grant_id != grant.grant_id
            or outcome.repository_commit != plan.repository_commit
            or outcome.toolchain_sha256 != plan.toolchain_sha256
        ):
            raise AV1ValidationV3Tier1CoverageError(
                "AV1 v3 Tier 1 fixture outcome is not bound to the active run"
            )
    fixtures = tuple(sorted((_coverage_from_outcome(item) for item in outcomes), key=lambda item: item.fixture_id))
    if tuple(fixture.fixture_id for fixture in fixtures) != AV1_VALIDATION_V3_TIER1_FIXTURE_IDS:
        raise AV1ValidationV3Tier1CoverageError(
            "AV1 v3 Tier 1 coverage fixture set is incomplete"
        )
    semantic = _semantic_payload(
        plan_id=plan.plan_id,
        plan_payload_sha256=plan.payload_sha256,
        request_id=request.request_id,
        request_payload_sha256=request.payload_sha256,
        grant_id=grant.grant_id,
        grant_payload_sha256=grant.payload_sha256,
        repository_commit=plan.repository_commit,
        repository_tree=plan.repository_tree,
        config_sha256=plan.config_sha256,
        toolchain_sha256=plan.toolchain_sha256,
        fixture_matrix_sha256=plan.fixture_matrix_sha256,
        fixtures=fixtures,
        completed_at=completed_at,
    )
    attestation_id = av1_validation_v3_id("tier1coverage", semantic)
    return AV1ValidationV3Tier1CoverageAttestation(
        attestation_id=attestation_id,
        plan_id=plan.plan_id,
        plan_payload_sha256=plan.payload_sha256,
        request_id=request.request_id,
        request_payload_sha256=request.payload_sha256,
        grant_id=grant.grant_id,
        grant_payload_sha256=grant.payload_sha256,
        repository_commit=plan.repository_commit,
        repository_tree=plan.repository_tree,
        config_sha256=plan.config_sha256,
        toolchain_sha256=plan.toolchain_sha256,
        fixture_matrix_sha256=plan.fixture_matrix_sha256,
        fixtures=fixtures,
        cleanup_passed=True,
        runtime_paused=True,
        completed_at=completed_at,
        payload_sha256=f"sha256:{stable_json_hash({'attestation_id': attestation_id, **semantic})}",
    )


def serialize_av1_validation_v3_tier1_coverage_attestation(
    attestation: AV1ValidationV3Tier1CoverageAttestation,
) -> bytes:
    return canonical_json_bytes(attestation.to_payload()) + b"\n"


def assert_av1_validation_v3_tier1_coverage_attestation(
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1AuthorizationRequest,
    grant: AV1ValidationV3Tier1ExecutionGrant,
    attestation: AV1ValidationV3Tier1CoverageAttestation,
) -> None:
    validated = av1_validation_v3_tier1_coverage_from_payload(attestation.to_payload())
    if validated != attestation:
        raise AV1ValidationV3Tier1CoverageError(
            "AV1 v3 Tier 1 coverage attestation is not canonical"
        )
    assert_av1_validation_v3_tier1_grant_active(
        protocol, plan, request, grant, as_of=attestation.completed_at
    )
    if (
        attestation.plan_id != plan.plan_id
        or attestation.plan_payload_sha256 != plan.payload_sha256
        or attestation.request_id != request.request_id
        or attestation.request_payload_sha256 != request.payload_sha256
        or attestation.grant_id != grant.grant_id
        or attestation.grant_payload_sha256 != grant.payload_sha256
        or attestation.repository_commit != plan.repository_commit
        or attestation.repository_tree != plan.repository_tree
        or attestation.config_sha256 != plan.config_sha256
        or attestation.toolchain_sha256 != plan.toolchain_sha256
        or attestation.fixture_matrix_sha256 != plan.fixture_matrix_sha256
        or attestation.fixture_matrix_sha256 != AV1_VALIDATION_V3_TIER1_MATRIX_SHA256
    ):
        raise AV1ValidationV3Tier1CoverageError(
            "AV1 v3 Tier 1 coverage is not bound to its active run"
        )


def load_av1_validation_v3_tier1_coverage_attestation(
    path: Path,
) -> AV1ValidationV3Tier1CoverageAttestation:
    raw = path.read_bytes()
    try:
        attestation = av1_validation_v3_tier1_coverage_from_payload(
            object_dict(json.loads(raw.decode("utf-8")))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1CoverageError(
            "AV1 v3 Tier 1 coverage attestation is invalid"
        ) from exc
    if raw != serialize_av1_validation_v3_tier1_coverage_attestation(attestation):
        raise AV1ValidationV3Tier1CoverageError(
            "AV1 v3 Tier 1 coverage attestation bytes are not canonical"
        )
    return attestation


def av1_validation_v3_tier1_coverage_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3Tier1CoverageAttestation:
    value = object_dict(payload)
    fixtures = tuple(_fixture_from_payload(item) for item in object_list(value.get("fixtures")))
    attestation = AV1ValidationV3Tier1CoverageAttestation(
        attestation_id=str(value.get("attestation_id") or ""),
        plan_id=str(value.get("plan_id") or ""),
        plan_payload_sha256=str(value.get("plan_payload_sha256") or ""),
        request_id=str(value.get("request_id") or ""),
        request_payload_sha256=str(value.get("request_payload_sha256") or ""),
        grant_id=str(value.get("grant_id") or ""),
        grant_payload_sha256=str(value.get("grant_payload_sha256") or ""),
        repository_commit=str(value.get("repository_commit") or ""),
        repository_tree=str(value.get("repository_tree") or ""),
        config_sha256=str(value.get("config_sha256") or ""),
        toolchain_sha256=str(value.get("toolchain_sha256") or ""),
        fixture_matrix_sha256=str(value.get("fixture_matrix_sha256") or ""),
        fixtures=fixtures,
        cleanup_passed=value.get("cleanup_passed") is True,
        runtime_paused=value.get("runtime_paused") is True,
        completed_at=str(value.get("completed_at") or ""),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    semantic = attestation.semantic_payload()
    expected = {"attestation_id": attestation.attestation_id, **semantic, "payload_sha256": attestation.payload_sha256}
    if value != expected:
        raise AV1ValidationV3Tier1CoverageError(
            "AV1 v3 Tier 1 coverage payload fields are invalid"
        )
    if attestation.attestation_id != av1_validation_v3_id("tier1coverage", semantic):
        raise AV1ValidationV3Tier1CoverageError("AV1 v3 Tier 1 coverage ID is invalid")
    if attestation.payload_sha256 != f"sha256:{stable_json_hash({'attestation_id': attestation.attestation_id, **semantic})}":
        raise AV1ValidationV3Tier1CoverageError("AV1 v3 Tier 1 coverage digest is invalid")
    if (
        not attestation.cleanup_passed
        or not attestation.runtime_paused
        or tuple(fixture.fixture_id for fixture in fixtures)
        != AV1_VALIDATION_V3_TIER1_FIXTURE_IDS
    ):
        raise AV1ValidationV3Tier1CoverageError(
            "AV1 v3 Tier 1 coverage completion fields are invalid"
        )
    return attestation


def _coverage_from_outcome(
    outcome: AV1ValidationV3Tier1FixtureOutcome,
) -> AV1ValidationV3Tier1FixtureCoverage:
    return AV1ValidationV3Tier1FixtureCoverage(
        fixture_id=outcome.fixture_id,
        command_plan_sha256=outcome.command_plan_sha256,
        content_sha256=outcome.content_sha256,
        content_byte_count=outcome.content_byte_count,
        passed=outcome.passed,
        failures=tuple(sorted(set(outcome.failures))),
    )


def _fixture_from_payload(value: object) -> AV1ValidationV3Tier1FixtureCoverage:
    item = object_dict(value)
    byte_count = item.get("content_byte_count")
    if type(byte_count) is not int:
        raise AV1ValidationV3Tier1CoverageError(
            "AV1 v3 Tier 1 fixture byte count is invalid"
        )
    fixture = AV1ValidationV3Tier1FixtureCoverage(
        fixture_id=str(item.get("fixture_id") or ""),
        command_plan_sha256=str(item.get("command_plan_sha256") or ""),
        content_sha256=str(item.get("content_sha256") or ""),
        content_byte_count=byte_count,
        passed=item.get("passed") is True,
        failures=tuple(str(reason) for reason in object_list(item.get("failures"))),
    )
    if item != fixture.to_payload():
        raise AV1ValidationV3Tier1CoverageError("AV1 v3 Tier 1 fixture coverage is invalid")
    return fixture


def _semantic_payload(
    *,
    plan_id: str,
    plan_payload_sha256: str,
    request_id: str,
    request_payload_sha256: str,
    grant_id: str,
    grant_payload_sha256: str,
    repository_commit: str,
    repository_tree: str,
    config_sha256: str,
    toolchain_sha256: str,
    fixture_matrix_sha256: str,
    fixtures: tuple[AV1ValidationV3Tier1FixtureCoverage, ...],
    completed_at: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_TIER1_COVERAGE_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER1_COVERAGE_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER1_COVERAGE_CONTRACT_VERSION,
        "authority": "av1_v3_tier1_execution",
        "gate": "A0",
        "tier": "tier1",
        "evidence_eligible": False,
        "qualification_attestation_eligible": False,
        "qualification_complete": False,
        "tier1_complete": True,
        "tier2_complete": False,
        "tier2_execution_authorized": False,
        "private_inventory_read_authorized": False,
        "key_creation_authorized": False,
        "media_library_read_authorized": False,
        "empirical_authority_conferred": False,
        "derivation_authorized": False,
        "holdout_authorized": False,
        "publication_authorized": False,
        "activation_authorized": False,
        "path_matrix_coverage_claimed": False,
        "plan_id": plan_id,
        "plan_payload_sha256": plan_payload_sha256,
        "request_id": request_id,
        "request_payload_sha256": request_payload_sha256,
        "grant_id": grant_id,
        "grant_payload_sha256": grant_payload_sha256,
        "repository_commit": repository_commit,
        "repository_tree": repository_tree,
        "config_sha256": config_sha256,
        "toolchain_sha256": toolchain_sha256,
        "fixture_matrix_sha256": fixture_matrix_sha256,
        "fixtures": [fixture.to_payload() for fixture in fixtures],
        "cleanup_passed": True,
        "runtime_paused": True,
        "completed_at": completed_at,
    }


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV3Tier1CoverageError(
            "AV1 v3 Tier 1 coverage timestamp is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise AV1ValidationV3Tier1CoverageError(
            "AV1 v3 Tier 1 coverage timestamp must include a timezone"
        )
    normalized = parsed.astimezone(UTC)
    if normalized.isoformat(timespec="seconds").replace("+00:00", "Z") != value:
        raise AV1ValidationV3Tier1CoverageError(
            "AV1 v3 Tier 1 coverage timestamp must use canonical UTC"
        )
    return normalized
