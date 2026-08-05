from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3_tier1_coverage import (
    AV1ValidationV3Tier1CoverageAttestation,
    serialize_av1_validation_v3_tier1_coverage_attestation,
)
from mediaforce.tuning.av1_validation_v3_tier1_grant import (
    AV1ValidationV3Tier1ExecutionGrant,
    av1_validation_v3_tier1_grant_from_payload,
    serialize_av1_validation_v3_tier1_execution_grant,
)
from mediaforce.tuning.av1_validation_v3_tier1_operation import (
    AV1ValidationV3Tier1ExecutionClaim,
    serialize_av1_validation_v3_tier1_execution_claim,
)
from mediaforce.tuning.av1_validation_v3_tier1_publication import (
    AV1ValidationV3Tier1PublicationError,
    load_av1_validation_v3_owner_artifact,
    publish_av1_validation_v3_owner_artifact,
)


AV1_VALIDATION_V3_TIER1_GRANT_FILENAME = "tier1-execution-grant.json"
AV1_VALIDATION_V3_TIER1_GRANT_DIRECTORY_PREFIX = "av1-v3-tier1-grant-"
AV1_VALIDATION_V3_TIER1_CLAIM_FILENAME = "tier1-execution-claim.json"
AV1_VALIDATION_V3_TIER1_CLAIM_DIRECTORY_PREFIX = "av1-v3-tier1-run-"
AV1_VALIDATION_V3_TIER1_RECEIPT_FILENAME = "tier1-coverage-receipt.json"
AV1_VALIDATION_V3_TIER1_RECEIPT_DIRECTORY_PREFIX = "av1-v3-tier1-coverage-"


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1GrantPublicationResult:
    directory: Path
    grant_path: Path
    grant: AV1ValidationV3Tier1ExecutionGrant
    created: bool

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "published": True,
            "created": self.created,
            "artifact_kind": "tier1_execution_grant",
            "gate": "A0",
            "tier": "tier1",
            "request_id": self.grant.request_id,
            "grant_id": self.grant.grant_id,
            "grant_payload_sha256": self.grant.payload_sha256,
            "valid_until": self.grant.valid_until,
            "tier1_execution_authorized": True,
            "private_inventory_read_authorized": False,
            "media_read_authorized": False,
            "tier2_execution_authorized": False,
            "evidence_creation_authorized": False,
            "evidence_eligible": False,
            "empirical_authority_conferred": False,
            "derivation_authorized": False,
            "holdout_authorized": False,
            "publication_authorized": False,
            "activation_authorized": False,
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ClaimPublicationResult:
    directory: Path
    claim_path: Path
    claim: AV1ValidationV3Tier1ExecutionClaim
    created: bool


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ReceiptPublicationResult:
    directory: Path
    receipt_path: Path
    attestation: AV1ValidationV3Tier1CoverageAttestation
    created: bool

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "published": True,
            "created": self.created,
            "artifact_kind": "tier1_coverage_receipt",
            "gate": "A0",
            **self.attestation.to_public_summary(),
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


def publish_av1_validation_v3_tier1_execution_grant(
    *,
    grant: AV1ValidationV3Tier1ExecutionGrant,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier1GrantPublicationResult:
    directory, created = publish_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=f"{AV1_VALIDATION_V3_TIER1_GRANT_DIRECTORY_PREFIX}{grant.request_id}",
        filename=AV1_VALIDATION_V3_TIER1_GRANT_FILENAME,
        content=serialize_av1_validation_v3_tier1_execution_grant(grant),
        description="execution grant",
    )
    return AV1ValidationV3Tier1GrantPublicationResult(
        directory=directory,
        grant_path=directory / AV1_VALIDATION_V3_TIER1_GRANT_FILENAME,
        grant=grant,
        created=created,
    )


def load_published_av1_validation_v3_tier1_execution_grant(
    *,
    output_root: Path,
    repository_root: Path,
    request_id: str,
) -> AV1ValidationV3Tier1ExecutionGrant:
    raw = load_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=f"{AV1_VALIDATION_V3_TIER1_GRANT_DIRECTORY_PREFIX}{request_id}",
        filename=AV1_VALIDATION_V3_TIER1_GRANT_FILENAME,
        maximum_size=1 << 20,
    )
    try:
        grant = av1_validation_v3_tier1_grant_from_payload(
            object_dict(json.loads(raw.decode("utf-8")))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 1 execution grant is invalid",
        ) from exc
    if raw != serialize_av1_validation_v3_tier1_execution_grant(grant):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 1 execution grant bytes are not canonical",
        )
    return grant


def publish_av1_validation_v3_tier1_execution_claim(
    *,
    claim: AV1ValidationV3Tier1ExecutionClaim,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier1ClaimPublicationResult:
    try:
        directory, created = publish_av1_validation_v3_owner_artifact(
            output_root=output_root,
            repository_root=repository_root,
            final_name=f"{AV1_VALIDATION_V3_TIER1_CLAIM_DIRECTORY_PREFIX}{claim.grant_id}",
            filename=AV1_VALIDATION_V3_TIER1_CLAIM_FILENAME,
            content=serialize_av1_validation_v3_tier1_execution_claim(claim),
            description="execution claim",
        )
    except AV1ValidationV3Tier1PublicationError as exc:
        if exc.reason_code == "artifact_conflict":
            raise AV1ValidationV3Tier1PublicationError(
                "execution_already_claimed",
                "AV1 v3 Tier 1 execution claim slot is already consumed",
            ) from exc
        raise
    return AV1ValidationV3Tier1ClaimPublicationResult(
        directory=directory,
        claim_path=directory / AV1_VALIDATION_V3_TIER1_CLAIM_FILENAME,
        claim=claim,
        created=created,
    )


def publish_av1_validation_v3_tier1_coverage_receipt(
    *,
    attestation: AV1ValidationV3Tier1CoverageAttestation,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier1ReceiptPublicationResult:
    directory, created = publish_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=f"{AV1_VALIDATION_V3_TIER1_RECEIPT_DIRECTORY_PREFIX}{attestation.grant_id}",
        filename=AV1_VALIDATION_V3_TIER1_RECEIPT_FILENAME,
        content=serialize_av1_validation_v3_tier1_coverage_attestation(attestation),
        description="coverage receipt",
    )
    return AV1ValidationV3Tier1ReceiptPublicationResult(
        directory=directory,
        receipt_path=directory / AV1_VALIDATION_V3_TIER1_RECEIPT_FILENAME,
        attestation=attestation,
        created=created,
    )
