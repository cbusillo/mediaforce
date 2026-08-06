from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3_tier1_publication import (
    AV1ValidationV3Tier1PublicationError,
    load_av1_validation_v3_owner_artifact,
    publish_av1_validation_v3_owner_artifact,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_authorization import (
    AV1_VALIDATION_V3_TIER2_INVENTORY_FALSE_AUTHORITY_FIELDS,
    AV1ValidationV3Tier2InventoryReadClaim,
    AV1ValidationV3Tier2InventoryReadGrant,
    AV1ValidationV3Tier2InventoryReadRequest,
    av1_validation_v3_tier2_inventory_read_claim_from_payload,
    av1_validation_v3_tier2_inventory_read_grant_from_payload,
    av1_validation_v3_tier2_inventory_read_request_from_payload,
    serialize_av1_validation_v3_tier2_inventory_read_claim,
    serialize_av1_validation_v3_tier2_inventory_read_grant,
    serialize_av1_validation_v3_tier2_inventory_read_request,
)


AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_FILENAME = (
    "tier2-inventory-read-request.json"
)
AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_DIRECTORY_PREFIX = (
    "av1-v3-tier2-inventory-read-request-"
)
AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_FILENAME = (
    "tier2-inventory-read-grant.json"
)
AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_DIRECTORY_PREFIX = (
    "av1-v3-tier2-inventory-read-grant-"
)
AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_FILENAME = (
    "tier2-inventory-read-claim.json"
)
AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_DIRECTORY_PREFIX = (
    "av1-v3-tier2-inventory-read-claim-"
)


def _false_authority_fields() -> dict[str, bool]:
    return dict.fromkeys(
        AV1_VALIDATION_V3_TIER2_INVENTORY_FALSE_AUTHORITY_FIELDS,
        False,
    )


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2InventoryReadRequestPublicationResult:
    directory: Path
    request_path: Path
    request: AV1ValidationV3Tier2InventoryReadRequest
    created: bool

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "published": True,
            "created": self.created,
            "artifact_kind": "tier2_inventory_read_request",
            "gate": "A0",
            "tier": "tier2",
            "request_id": self.request.request_id,
            "request_payload_sha256": self.request.payload_sha256,
            "single_read_requested": True,
            "private_inventory_read_authorized": False,
            "execution_requires_separate_owner_authorization": True,
            **_false_authority_fields(),
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2InventoryReadGrantPublicationResult:
    directory: Path
    grant_path: Path
    grant: AV1ValidationV3Tier2InventoryReadGrant
    created: bool

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "published": True,
            "created": self.created,
            "artifact_kind": "tier2_inventory_read_grant",
            "gate": "A0",
            "tier": "tier2",
            "request_id": self.grant.request_id,
            "grant_id": self.grant.grant_id,
            "grant_payload_sha256": self.grant.payload_sha256,
            "valid_until": self.grant.valid_until,
            "single_read_authorized": True,
            "private_inventory_read_authorized": True,
            "execution_requires_separate_owner_authorization": True,
            **_false_authority_fields(),
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2InventoryReadClaimPublicationResult:
    directory: Path
    claim_path: Path
    claim: AV1ValidationV3Tier2InventoryReadClaim
    created: bool

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "published": True,
            "created": self.created,
            "artifact_kind": "tier2_inventory_read_claim",
            "gate": "A0",
            "tier": "tier2",
            "request_id": self.claim.request_id,
            "grant_id": self.claim.grant_id,
            "claim_id": self.claim.claim_id,
            "claim_payload_sha256": self.claim.payload_sha256,
            "single_read_claimed": True,
            "private_inventory_read_authorized": True,
            "execution_requires_separate_owner_authorization": True,
            **_false_authority_fields(),
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


def publish_av1_validation_v3_tier2_inventory_read_request(
    *,
    request: AV1ValidationV3Tier2InventoryReadRequest,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier2InventoryReadRequestPublicationResult:
    directory, created = publish_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_DIRECTORY_PREFIX}"
            f"{request.request_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_FILENAME,
        content=serialize_av1_validation_v3_tier2_inventory_read_request(request),
        description="Tier 2 inventory read request",
    )
    return AV1ValidationV3Tier2InventoryReadRequestPublicationResult(
        directory=directory,
        request_path=(
            directory / AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_FILENAME
        ),
        request=request,
        created=created,
    )


def load_published_av1_validation_v3_tier2_inventory_read_request(
    *,
    output_root: Path,
    repository_root: Path,
    request_id: str,
) -> AV1ValidationV3Tier2InventoryReadRequest:
    raw = load_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_DIRECTORY_PREFIX}"
            f"{request_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER2_INVENTORY_READ_REQUEST_FILENAME,
        maximum_size=1 << 20,
    )
    try:
        request = av1_validation_v3_tier2_inventory_read_request_from_payload(
            object_dict(json.loads(raw.decode()))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 inventory read request is invalid",
        ) from exc
    if raw != serialize_av1_validation_v3_tier2_inventory_read_request(request):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 inventory read request bytes are not canonical",
        )
    if request.request_id != request_id:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 inventory read request identity is invalid",
        )
    return request


def publish_av1_validation_v3_tier2_inventory_read_grant(
    *,
    grant: AV1ValidationV3Tier2InventoryReadGrant,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier2InventoryReadGrantPublicationResult:
    directory, created = publish_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_DIRECTORY_PREFIX}"
            f"{grant.request_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_FILENAME,
        content=serialize_av1_validation_v3_tier2_inventory_read_grant(grant),
        description="Tier 2 inventory read grant",
    )
    return AV1ValidationV3Tier2InventoryReadGrantPublicationResult(
        directory=directory,
        grant_path=(
            directory / AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_FILENAME
        ),
        grant=grant,
        created=created,
    )


def load_published_av1_validation_v3_tier2_inventory_read_grant(
    *,
    output_root: Path,
    repository_root: Path,
    request_id: str,
) -> AV1ValidationV3Tier2InventoryReadGrant:
    raw = load_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_DIRECTORY_PREFIX}"
            f"{request_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER2_INVENTORY_READ_GRANT_FILENAME,
        maximum_size=1 << 20,
    )
    try:
        grant = av1_validation_v3_tier2_inventory_read_grant_from_payload(
            object_dict(json.loads(raw.decode()))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 inventory read grant is invalid",
        ) from exc
    if raw != serialize_av1_validation_v3_tier2_inventory_read_grant(grant):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 inventory read grant bytes are not canonical",
        )
    if grant.request_id != request_id:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 inventory read grant identity is invalid",
        )
    return grant


def publish_av1_validation_v3_tier2_inventory_read_claim(
    *,
    claim: AV1ValidationV3Tier2InventoryReadClaim,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier2InventoryReadClaimPublicationResult:
    try:
        directory, created = publish_av1_validation_v3_owner_artifact(
            output_root=output_root,
            repository_root=repository_root,
            final_name=(
                f"{AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_DIRECTORY_PREFIX}"
                f"{claim.grant_id}"
            ),
            filename=AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_FILENAME,
            content=serialize_av1_validation_v3_tier2_inventory_read_claim(claim),
            description="Tier 2 inventory read claim",
        )
    except AV1ValidationV3Tier1PublicationError as exc:
        if exc.reason_code == "artifact_conflict":
            raise AV1ValidationV3Tier1PublicationError(
                "inventory_read_already_claimed",
                "AV1 v3 Tier 2 inventory read claim is already consumed",
            ) from exc
        raise
    return AV1ValidationV3Tier2InventoryReadClaimPublicationResult(
        directory=directory,
        claim_path=(
            directory / AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_FILENAME
        ),
        claim=claim,
        created=created,
    )


def load_published_av1_validation_v3_tier2_inventory_read_claim(
    *,
    output_root: Path,
    repository_root: Path,
    grant_id: str,
) -> AV1ValidationV3Tier2InventoryReadClaim:
    raw = load_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_DIRECTORY_PREFIX}"
            f"{grant_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER2_INVENTORY_READ_CLAIM_FILENAME,
        maximum_size=1 << 20,
    )
    try:
        claim = av1_validation_v3_tier2_inventory_read_claim_from_payload(
            object_dict(json.loads(raw.decode()))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 inventory read claim is invalid",
        ) from exc
    if raw != serialize_av1_validation_v3_tier2_inventory_read_claim(claim):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 inventory read claim bytes are not canonical",
        )
    if claim.grant_id != grant_id:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 inventory read claim identity is invalid",
        )
    return claim
