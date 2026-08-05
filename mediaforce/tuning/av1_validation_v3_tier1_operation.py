from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
import re
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3 import (
    AV1ValidationProtocolV3,
    av1_validation_v3_id,
)
from mediaforce.tuning.av1_validation_v3_qualification import (
    AV1ValidationV3QualificationPlan,
)
from mediaforce.tuning.av1_validation_v3_tier1_coverage import (
    AV1_VALIDATION_V3_TIER1_FIXTURE_IDS,
    AV1ValidationV3Tier1CoverageAttestation,
    build_av1_validation_v3_tier1_coverage_attestation,
)
from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1ValidationV3Tier1ExecutionContext,
    AV1ValidationV3Tier1FixtureOutcome,
    execute_av1_validation_v3_tier1_fixture,
)
from mediaforce.tuning.av1_validation_v3_tier1_grant import (
    AV1ValidationV3Tier1ExecutionGrant,
    assert_av1_validation_v3_tier1_grant_active,
)
from mediaforce.tuning.av1_validation_v3_tier1_request import (
    AV1ValidationV3Tier1AuthorizationRequest,
)
from mediaforce.tuning.av1_validation_v3_tier1_runtime import (
    AV1ValidationV3Tier1CommandDiagnostic,
    AV1ValidationV3Tier1ToolchainBinding,
    paused_av1_validation_v3_tier1_runtime,
)


AV1_VALIDATION_V3_TIER1_CLAIM_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier1_execution_claim"
)
AV1_VALIDATION_V3_TIER1_CLAIM_SCHEMA_VERSION = 1
AV1_VALIDATION_V3_TIER1_CLAIM_CONTRACT_VERSION = "av1vt1x1"
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class AV1ValidationV3Tier1OperationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ExecutionClaim:
    claim_id: str
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
    claimed_at: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.claim_id.startswith("av1vtier1claim3_"):
            raise AV1ValidationV3Tier1OperationError(
                "AV1 v3 Tier 1 execution claim ID is invalid"
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
                raise AV1ValidationV3Tier1OperationError(
                    "AV1 v3 Tier 1 execution claim digest is invalid"
                )
        _parse_timestamp(self.claimed_at)
        semantic = self.semantic_payload()
        if self.claim_id != av1_validation_v3_id("tier1claim", semantic):
            raise AV1ValidationV3Tier1OperationError(
                "AV1 v3 Tier 1 execution claim ID does not match its payload"
            )
        expected = f"sha256:{stable_json_hash({'claim_id': self.claim_id, **semantic})}"
        if self.payload_sha256 != expected:
            raise AV1ValidationV3Tier1OperationError(
                "AV1 v3 Tier 1 execution claim digest does not match its payload"
            )
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER1_CLAIM_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER1_CLAIM_SCHEMA_VERSION,
            "contract_version": AV1_VALIDATION_V3_TIER1_CLAIM_CONTRACT_VERSION,
            "tier": "tier1",
            "fixture_scope": "deterministic_synthetic_public_only",
            "fixture_ids": list(AV1_VALIDATION_V3_TIER1_FIXTURE_IDS),
            "single_execution_claimed": True,
            "retry_authorized": False,
            "private_inventory_read_authorized": False,
            "media_library_read_authorized": False,
            "tier2_execution_authorized": False,
            "evidence_eligible": False,
            "empirical_authority_conferred": False,
            "derivation_authorized": False,
            "holdout_authorized": False,
            "publication_authorized": False,
            "activation_authorized": False,
            "plan_id": self.plan_id,
            "plan_payload_sha256": self.plan_payload_sha256,
            "request_id": self.request_id,
            "request_payload_sha256": self.request_payload_sha256,
            "grant_id": self.grant_id,
            "grant_payload_sha256": self.grant_payload_sha256,
            "repository_commit": self.repository_commit,
            "repository_tree": self.repository_tree,
            "config_sha256": self.config_sha256,
            "toolchain_sha256": self.toolchain_sha256,
            "fixture_matrix_sha256": self.fixture_matrix_sha256,
            "claimed_at": self.claimed_at,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1OperationResult:
    attestation: AV1ValidationV3Tier1CoverageAttestation
    outcomes: tuple[AV1ValidationV3Tier1FixtureOutcome, ...]
    diagnostics: tuple[AV1ValidationV3Tier1CommandDiagnostic, ...]

    @property
    def passed(self) -> bool:
        return all(outcome.passed for outcome in self.outcomes)

    def to_public_summary(self) -> dict[str, object]:
        summary = {
            **self.attestation.to_public_summary(),
            "gate": "A0",
            "coverage_passed": self.passed,
            "runtime_cleanup_passed": True,
            "private_inventory_read_authorized": False,
            "media_read_authorized": False,
            "tier2_execution_authorized": False,
            "evidence_creation_authorized": False,
            "empirical_authority_conferred": False,
            "derivation_authorized": False,
            "holdout_authorized": False,
            "publication_authorized": False,
            "activation_authorized": False,
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


def build_av1_validation_v3_tier1_execution_claim(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1AuthorizationRequest,
    grant: AV1ValidationV3Tier1ExecutionGrant,
    claimed_at: str,
) -> AV1ValidationV3Tier1ExecutionClaim:
    assert_av1_validation_v3_tier1_grant_active(
        protocol,
        plan,
        request,
        grant,
        as_of=claimed_at,
    )
    semantic = {
        "schema": AV1_VALIDATION_V3_TIER1_CLAIM_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER1_CLAIM_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V3_TIER1_CLAIM_CONTRACT_VERSION,
        "tier": "tier1",
        "fixture_scope": "deterministic_synthetic_public_only",
        "fixture_ids": list(AV1_VALIDATION_V3_TIER1_FIXTURE_IDS),
        "single_execution_claimed": True,
        "retry_authorized": False,
        "private_inventory_read_authorized": False,
        "media_library_read_authorized": False,
        "tier2_execution_authorized": False,
        "evidence_eligible": False,
        "empirical_authority_conferred": False,
        "derivation_authorized": False,
        "holdout_authorized": False,
        "publication_authorized": False,
        "activation_authorized": False,
        "plan_id": plan.plan_id,
        "plan_payload_sha256": plan.payload_sha256,
        "request_id": request.request_id,
        "request_payload_sha256": request.payload_sha256,
        "grant_id": grant.grant_id,
        "grant_payload_sha256": grant.payload_sha256,
        "repository_commit": plan.repository_commit,
        "repository_tree": plan.repository_tree,
        "config_sha256": plan.config_sha256,
        "toolchain_sha256": plan.toolchain_sha256,
        "fixture_matrix_sha256": plan.fixture_matrix_sha256,
        "claimed_at": claimed_at,
    }
    claim_id = av1_validation_v3_id("tier1claim", semantic)
    return AV1ValidationV3Tier1ExecutionClaim(
        claim_id=claim_id,
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
        claimed_at=claimed_at,
        payload_sha256=f"sha256:{stable_json_hash({'claim_id': claim_id, **semantic})}",
    )


def assert_av1_validation_v3_tier1_execution_claim(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1AuthorizationRequest,
    grant: AV1ValidationV3Tier1ExecutionGrant,
    claim: AV1ValidationV3Tier1ExecutionClaim,
) -> None:
    expected = build_av1_validation_v3_tier1_execution_claim(
        protocol=protocol,
        plan=plan,
        request=request,
        grant=grant,
        claimed_at=claim.claimed_at,
    )
    if claim != expected:
        raise AV1ValidationV3Tier1OperationError(
            "AV1 v3 Tier 1 execution claim is not bound to the active run"
        )


def serialize_av1_validation_v3_tier1_execution_claim(
    claim: AV1ValidationV3Tier1ExecutionClaim,
) -> bytes:
    return canonical_json_bytes(claim.to_payload()) + b"\n"


def run_av1_validation_v3_tier1_synthetic_qualification(
    config: MediaforceConfig,
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1AuthorizationRequest,
    grant: AV1ValidationV3Tier1ExecutionGrant,
    config_snapshot_bytes: bytes,
    matrix: Mapping[str, object],
    output_directory: Path,
    repository_root: Path,
    toolchain: AV1ValidationV3Tier1ToolchainBinding,
    claim: AV1ValidationV3Tier1ExecutionClaim,
    claim_execution: Callable[[], str],
    clock: Callable[[], str] | None = None,
) -> AV1ValidationV3Tier1OperationResult:
    now = clock or _canonical_utc_now
    assert_av1_validation_v3_tier1_execution_claim(
        protocol=protocol,
        plan=plan,
        request=request,
        grant=grant,
        claim=claim,
    )
    initial_context = AV1ValidationV3Tier1ExecutionContext(
        protocol=protocol,
        qualification_plan=plan,
        request=request,
        grant=grant,
        as_of=claim.claimed_at,
    )
    outcomes: list[AV1ValidationV3Tier1FixtureOutcome] = []
    with paused_av1_validation_v3_tier1_runtime(
        config,
        config_snapshot_bytes=config_snapshot_bytes,
        context=initial_context,
        matrix=matrix,
        output_directory=output_directory,
        repository_root=repository_root,
        toolchain=toolchain,
    ) as session:
        if claim_execution() != claim.claim_id:
            raise AV1ValidationV3Tier1OperationError(
                "AV1 v3 Tier 1 execution claim was not published"
            )
        for fixture_id in AV1_VALIDATION_V3_TIER1_FIXTURE_IDS:
            context = replace(initial_context, as_of=now())
            outcomes.append(
                execute_av1_validation_v3_tier1_fixture(
                    fixture_id,
                    matrix=matrix,
                    output_directory=output_directory,
                    repository_root=repository_root,
                    context=context,
                    executor=session.executor,
                )
            )
    if not session.cleanup_passed:
        raise AV1ValidationV3Tier1OperationError(
            "AV1 v3 Tier 1 fixture cleanup did not complete"
        )
    completed_at = now()
    attestation = build_av1_validation_v3_tier1_coverage_attestation(
        protocol=protocol,
        plan=plan,
        request=request,
        grant=grant,
        outcomes=outcomes,
        cleanup_passed=session.cleanup_passed,
        runtime_paused=True,
        completed_at=completed_at,
    )
    return AV1ValidationV3Tier1OperationResult(
        attestation=attestation,
        outcomes=tuple(outcomes),
        diagnostics=session.diagnostics,
    )


def _canonical_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV3Tier1OperationError(
            "AV1 v3 Tier 1 execution claim timestamp is invalid"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != UTC.utcoffset(parsed)
        or parsed.microsecond
        or not value.endswith("Z")
        or parsed.isoformat().replace("+00:00", "Z") != value
    ):
        raise AV1ValidationV3Tier1OperationError(
            "AV1 v3 Tier 1 execution claim timestamp is invalid"
        )
    return parsed
