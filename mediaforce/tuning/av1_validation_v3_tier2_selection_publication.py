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
from mediaforce.tuning.av1_validation_v3_tier2_selection_authorization import (
    AV1_VALIDATION_V3_TIER2_SELECTION_FALSE_AUTHORITY_FIELDS,
    AV1ValidationV3Tier2SelectionClaim,
    AV1ValidationV3Tier2SelectionGrant,
    AV1ValidationV3Tier2SelectionRequest,
    av1_validation_v3_tier2_selection_claim_from_payload,
    av1_validation_v3_tier2_selection_grant_from_payload,
    av1_validation_v3_tier2_selection_request_from_payload,
    serialize_av1_validation_v3_tier2_selection_claim,
    serialize_av1_validation_v3_tier2_selection_grant,
    serialize_av1_validation_v3_tier2_selection_request,
)
from mediaforce.tuning.av1_validation_v3_tier2_selection import (
    AV1ValidationV3Tier2SelectionRecord,
    av1_validation_v3_tier2_selection_record_from_payload,
    serialize_av1_validation_v3_tier2_selection_record,
)


AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_FILENAME = "tier2-selection-request.json"
AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_DIRECTORY_PREFIX = (
    "av1-v3-tier2-selection-request-"
)
AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_FILENAME = "tier2-selection-grant.json"
AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_DIRECTORY_PREFIX = (
    "av1-v3-tier2-selection-grant-"
)
AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_FILENAME = "tier2-selection-claim.json"
AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_DIRECTORY_PREFIX = (
    "av1-v3-tier2-selection-claim-"
)
AV1_VALIDATION_V3_TIER2_SELECTION_RECORD_FILENAME = "tier2-selection-record.json"
AV1_VALIDATION_V3_TIER2_SELECTION_RECORD_DIRECTORY_PREFIX = (
    "av1-v3-tier2-selection-record-"
)


def _false_authority_fields() -> dict[str, bool]:
    return dict.fromkeys(
        AV1_VALIDATION_V3_TIER2_SELECTION_FALSE_AUTHORITY_FIELDS,
        False,
    )


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2SelectionRequestPublicationResult:
    directory: Path
    request_path: Path
    request: AV1ValidationV3Tier2SelectionRequest
    created: bool

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "published": True,
            "created": self.created,
            "artifact_kind": "tier2_selection_request",
            "gate": "A0",
            "tier": "tier2",
            "request_id": self.request.request_id,
            "request_payload_sha256": self.request.payload_sha256,
            "single_selection_requested": True,
            "tier2_selection_execution_authorized": False,
            "qualification_key_read_authorized": False,
            "execution_requires_separate_owner_authorization": True,
            **_false_authority_fields(),
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2SelectionGrantPublicationResult:
    directory: Path
    grant_path: Path
    grant: AV1ValidationV3Tier2SelectionGrant
    created: bool

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "published": True,
            "created": self.created,
            "artifact_kind": "tier2_selection_grant",
            "gate": "A0",
            "tier": "tier2",
            "request_id": self.grant.request_id,
            "grant_id": self.grant.grant_id,
            "grant_payload_sha256": self.grant.payload_sha256,
            "valid_until": self.grant.valid_until,
            "single_selection_authorized": True,
            "tier2_selection_execution_authorized": True,
            "qualification_key_read_authorized": True,
            "execution_requires_separate_owner_authorization": True,
            **_false_authority_fields(),
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2SelectionClaimPublicationResult:
    directory: Path
    claim_path: Path
    claim: AV1ValidationV3Tier2SelectionClaim
    created: bool

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "published": True,
            "created": self.created,
            "artifact_kind": "tier2_selection_claim",
            "gate": "A0",
            "tier": "tier2",
            "request_id": self.claim.request_id,
            "grant_id": self.claim.grant_id,
            "claim_id": self.claim.claim_id,
            "claim_payload_sha256": self.claim.payload_sha256,
            "single_selection_claimed": True,
            "tier2_selection_execution_authorized": True,
            "qualification_key_read_authorized": True,
            "execution_requires_separate_owner_authorization": True,
            **_false_authority_fields(),
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2SelectionRecordPublicationResult:
    directory: Path
    record_path: Path
    created: bool

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "published": True,
            "created": self.created,
            "artifact_kind": "tier2_selection_record_publication",
            "gate": "A0",
            "tier": "tier2",
            "selection_record_published": True,
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


def publish_av1_validation_v3_tier2_selection_request(
    *,
    request: AV1ValidationV3Tier2SelectionRequest,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier2SelectionRequestPublicationResult:
    directory, created = publish_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_DIRECTORY_PREFIX}"
            f"{request.request_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_FILENAME,
        content=serialize_av1_validation_v3_tier2_selection_request(request),
        description="Tier 2 selection request",
    )
    return AV1ValidationV3Tier2SelectionRequestPublicationResult(
        directory=directory,
        request_path=(
            directory / AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_FILENAME
        ),
        request=request,
        created=created,
    )


def load_published_av1_validation_v3_tier2_selection_request(
    *,
    output_root: Path,
    repository_root: Path,
    request_id: str,
) -> AV1ValidationV3Tier2SelectionRequest:
    raw = load_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_DIRECTORY_PREFIX}"
            f"{request_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER2_SELECTION_REQUEST_FILENAME,
        maximum_size=1 << 20,
    )
    try:
        request = av1_validation_v3_tier2_selection_request_from_payload(
            object_dict(json.loads(raw.decode()))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 selection request is invalid",
        ) from exc
    if raw != serialize_av1_validation_v3_tier2_selection_request(request):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 selection request bytes are not canonical",
        )
    if request.request_id != request_id:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 selection request identity is invalid",
        )
    return request


def publish_av1_validation_v3_tier2_selection_grant(
    *,
    grant: AV1ValidationV3Tier2SelectionGrant,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier2SelectionGrantPublicationResult:
    directory, created = publish_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_DIRECTORY_PREFIX}"
            f"{grant.request_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_FILENAME,
        content=serialize_av1_validation_v3_tier2_selection_grant(grant),
        description="Tier 2 selection grant",
    )
    return AV1ValidationV3Tier2SelectionGrantPublicationResult(
        directory=directory,
        grant_path=(
            directory / AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_FILENAME
        ),
        grant=grant,
        created=created,
    )


def load_published_av1_validation_v3_tier2_selection_grant(
    *,
    output_root: Path,
    repository_root: Path,
    request_id: str,
) -> AV1ValidationV3Tier2SelectionGrant:
    raw = load_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_DIRECTORY_PREFIX}"
            f"{request_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER2_SELECTION_GRANT_FILENAME,
        maximum_size=1 << 20,
    )
    try:
        grant = av1_validation_v3_tier2_selection_grant_from_payload(
            object_dict(json.loads(raw.decode()))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 selection grant is invalid",
        ) from exc
    if raw != serialize_av1_validation_v3_tier2_selection_grant(grant):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 selection grant bytes are not canonical",
        )
    if grant.request_id != request_id:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 selection grant identity is invalid",
        )
    return grant


def publish_av1_validation_v3_tier2_selection_claim(
    *,
    claim: AV1ValidationV3Tier2SelectionClaim,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier2SelectionClaimPublicationResult:
    try:
        directory, created = publish_av1_validation_v3_owner_artifact(
            output_root=output_root,
            repository_root=repository_root,
            final_name=(
                f"{AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_DIRECTORY_PREFIX}"
                f"{claim.grant_id}"
            ),
            filename=AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_FILENAME,
            content=serialize_av1_validation_v3_tier2_selection_claim(claim),
            description="Tier 2 selection claim",
        )
    except AV1ValidationV3Tier1PublicationError as exc:
        if exc.reason_code == "artifact_conflict":
            raise AV1ValidationV3Tier1PublicationError(
                "tier2_selection_already_claimed",
                "AV1 v3 Tier 2 selection claim is already consumed",
            ) from exc
        raise
    return AV1ValidationV3Tier2SelectionClaimPublicationResult(
        directory=directory,
        claim_path=(
            directory / AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_FILENAME
        ),
        claim=claim,
        created=created,
    )


def load_published_av1_validation_v3_tier2_selection_claim(
    *,
    output_root: Path,
    repository_root: Path,
    grant_id: str,
) -> AV1ValidationV3Tier2SelectionClaim:
    raw = load_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_DIRECTORY_PREFIX}"
            f"{grant_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER2_SELECTION_CLAIM_FILENAME,
        maximum_size=1 << 20,
    )
    try:
        claim = av1_validation_v3_tier2_selection_claim_from_payload(
            object_dict(json.loads(raw.decode()))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 selection claim is invalid",
        ) from exc
    if raw != serialize_av1_validation_v3_tier2_selection_claim(claim):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 selection claim bytes are not canonical",
        )
    if claim.grant_id != grant_id:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 selection claim identity is invalid",
        )
    return claim


def publish_av1_validation_v3_tier2_selection_record(
    *,
    record: AV1ValidationV3Tier2SelectionRecord,
    claim: AV1ValidationV3Tier2SelectionClaim,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier2SelectionRecordPublicationResult:
    if (
        record.plan_id != claim.plan_id
        or record.plan_payload_sha256 != claim.plan_payload_sha256
        or record.selected_at < claim.claimed_at
    ):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 selection record is not bound to its claim",
        )
    directory, created = publish_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER2_SELECTION_RECORD_DIRECTORY_PREFIX}"
            f"{claim.claim_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER2_SELECTION_RECORD_FILENAME,
        content=serialize_av1_validation_v3_tier2_selection_record(record),
        description="Tier 2 selection record",
    )
    return AV1ValidationV3Tier2SelectionRecordPublicationResult(
        directory=directory,
        record_path=(
            directory / AV1_VALIDATION_V3_TIER2_SELECTION_RECORD_FILENAME
        ),
        created=created,
    )


def load_published_av1_validation_v3_tier2_selection_record(
    *,
    output_root: Path,
    repository_root: Path,
    claim_id: str,
) -> AV1ValidationV3Tier2SelectionRecord:
    raw = load_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER2_SELECTION_RECORD_DIRECTORY_PREFIX}"
            f"{claim_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER2_SELECTION_RECORD_FILENAME,
        maximum_size=1 << 20,
    )
    try:
        record = av1_validation_v3_tier2_selection_record_from_payload(
            object_dict(json.loads(raw.decode()))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 selection record is invalid",
        ) from exc
    if raw != serialize_av1_validation_v3_tier2_selection_record(record):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 2 selection record bytes are not canonical",
        )
    return record
