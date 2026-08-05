from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from mediaforce.core.evidence import stable_json_hash
from mediaforce.tuning.av1_validation_v3 import AV1ValidationProtocolV3
from mediaforce.tuning.av1_validation_v3_qualification import (
    AV1ValidationV3QualificationPlan,
    build_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
)
from mediaforce.tuning.av1_validation_v3_tier1_config_snapshot import (
    AV1ValidationV3Tier1ConfigSnapshotError,
    av1_validation_v3_tier1_config_snapshot_sha256,
)
from mediaforce.tuning.av1_validation_v3_tier1_request import (
    AV1ValidationV3Tier1AuthorizationRequest,
    build_av1_validation_v3_tier1_authorization_request,
)
from mediaforce.tuning.av1_validation_v3_tier1_runtime import (
    AV1ValidationV3Tier1ToolchainBinding,
)


AV1_VALIDATION_V3_TIER1_ELIGIBILITY_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier1_eligibility_predicate"
)
AV1_VALIDATION_V3_TIER1_ELIGIBILITY_SCHEMA_VERSION = 1

_ELIGIBILITY_PREDICATE: Mapping[str, object] = MappingProxyType({
    "schema": AV1_VALIDATION_V3_TIER1_ELIGIBILITY_SCHEMA,
    "schema_version": AV1_VALIDATION_V3_TIER1_ELIGIBILITY_SCHEMA_VERSION,
    "tier": "tier1",
    "fixture_scope": "deterministic_synthetic_public_only",
    "fixture_matrix_sha256": AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
    "requires_exact_repository_identity": True,
    "requires_exact_config_bytes": True,
    "requires_exact_toolchain_binding": True,
    "requires_paused_runtime": True,
    "requires_active_tier1_grant": True,
    "requires_complete_cleanup": True,
    "private_inventory_read_authorized": False,
    "media_library_read_authorized": False,
    "tier2_execution_authorized": False,
    "path_matrix_coverage_claimed": False,
    "evidence_eligible": False,
    "qualification_complete": False,
    "empirical_authority_conferred": False,
    "derivation_authorized": False,
    "holdout_authorized": False,
    "publication_authorized": False,
    "activation_authorized": False,
})


class AV1ValidationV3Tier1PreparationError(ValueError):
    pass


def av1_validation_v3_tier1_eligibility_predicate() -> dict[str, Any]:
    return dict(_ELIGIBILITY_PREDICATE)


def av1_validation_v3_tier1_eligibility_predicate_sha256() -> str:
    return f"sha256:{stable_json_hash(dict(_ELIGIBILITY_PREDICATE))}"


def av1_validation_v3_tier1_config_sha256(config_bytes: bytes) -> str:
    try:
        return av1_validation_v3_tier1_config_snapshot_sha256(config_bytes)
    except AV1ValidationV3Tier1ConfigSnapshotError as exc:
        raise AV1ValidationV3Tier1PreparationError(
            "AV1 v3 Tier 1 config bytes are invalid"
        ) from exc


def build_av1_validation_v3_tier1_prepared_plan(
    *,
    protocol: AV1ValidationProtocolV3,
    qualification_key_id: str,
    repository_commit: str,
    repository_tree: str,
    config_bytes: bytes,
    toolchain: AV1ValidationV3Tier1ToolchainBinding,
    frozen_at: str,
    valid_until: str,
) -> AV1ValidationV3QualificationPlan:
    return build_av1_validation_v3_qualification_plan(
        protocol=protocol,
        qualification_key_id=qualification_key_id,
        eligibility_predicate_sha256=(
            av1_validation_v3_tier1_eligibility_predicate_sha256()
        ),
        repository_commit=repository_commit,
        repository_tree=repository_tree,
        config_sha256=av1_validation_v3_tier1_config_sha256(config_bytes),
        toolchain_sha256=toolchain.payload_sha256,
        fixture_matrix_sha256=AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
        frozen_at=frozen_at,
        valid_until=valid_until,
    )


def build_av1_validation_v3_tier1_prepared_request(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    repository_commit: str,
    repository_tree: str,
    config_bytes: bytes,
    toolchain: AV1ValidationV3Tier1ToolchainBinding,
    requested_at: str,
    valid_until: str,
) -> AV1ValidationV3Tier1AuthorizationRequest:
    if (
        plan.eligibility_predicate_sha256
        != av1_validation_v3_tier1_eligibility_predicate_sha256()
        or plan.fixture_matrix_sha256 != AV1_VALIDATION_V3_TIER1_MATRIX_SHA256
        or plan.repository_commit != repository_commit
        or plan.repository_tree != repository_tree
        or plan.config_sha256 != av1_validation_v3_tier1_config_sha256(config_bytes)
        or plan.toolchain_sha256 != toolchain.payload_sha256
    ):
        raise AV1ValidationV3Tier1PreparationError(
            "AV1 v3 Tier 1 plan is not bound to the frozen preparation contract"
        )
    return build_av1_validation_v3_tier1_authorization_request(
        protocol=protocol,
        plan=plan,
        requested_at=requested_at,
        valid_until=valid_until,
    )
