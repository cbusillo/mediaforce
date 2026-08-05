from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3_tier1_residual_authorization import (
    AV1ValidationV3Tier1ResidualGrant,
    AV1ValidationV3Tier1ResidualRequest,
    av1_validation_v3_tier1_residual_grant_from_payload,
    serialize_av1_validation_v3_tier1_residual_grant,
    serialize_av1_validation_v3_tier1_residual_request,
)
from mediaforce.tuning.av1_validation_v3_tier1_residual_operation import (
    AV1ValidationV3Tier1ResidualClaim,
    AV1ValidationV3Tier1ResidualResult,
    av1_validation_v3_tier1_residual_result_from_payload,
    serialize_av1_validation_v3_tier1_residual_claim,
    serialize_av1_validation_v3_tier1_residual_result,
)
from mediaforce.tuning.av1_validation_v3_tier1_publication import (
    AV1ValidationV3Tier1PublicationError,
    load_av1_validation_v3_owner_artifact,
    publish_av1_validation_v3_owner_artifact,
)


AV1_VALIDATION_V3_TIER1_RESIDUAL_REQUEST_FILENAME = "tier1-residual-request.json"
AV1_VALIDATION_V3_TIER1_RESIDUAL_REQUEST_DIRECTORY_PREFIX = (
    "av1-v3-tier1-residual-request-"
)
AV1_VALIDATION_V3_TIER1_RESIDUAL_GRANT_FILENAME = "tier1-residual-grant.json"
AV1_VALIDATION_V3_TIER1_RESIDUAL_GRANT_DIRECTORY_PREFIX = (
    "av1-v3-tier1-residual-grant-"
)
AV1_VALIDATION_V3_TIER1_RESIDUAL_CLAIM_FILENAME = "tier1-residual-claim.json"
AV1_VALIDATION_V3_TIER1_RESIDUAL_CLAIM_DIRECTORY_PREFIX = (
    "av1-v3-tier1-residual-run-"
)
AV1_VALIDATION_V3_TIER1_RESIDUAL_RESULT_FILENAME = "tier1-residual-result.json"
AV1_VALIDATION_V3_TIER1_RESIDUAL_RESULT_DIRECTORY_PREFIX = (
    "av1-v3-tier1-residual-result-"
)


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ResidualPublicationResult:
    directory: Path
    artifact_path: Path
    artifact_id: str
    created: bool

    def to_summary(self, *, artifact_kind: str) -> dict[str, Any]:
        summary = {
            "published": True,
            "created": self.created,
            "artifact_kind": artifact_kind,
            "artifact_id": self.artifact_id,
            "gate": "A0",
            "tier": "none",
            "diagnostic_only": True,
            "evidence_eligible": False,
            "empirical_authority_conferred": False,
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


def publish_av1_validation_v3_tier1_residual_request(
    *,
    request: AV1ValidationV3Tier1ResidualRequest,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier1ResidualPublicationResult:
    return _publish(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER1_RESIDUAL_REQUEST_DIRECTORY_PREFIX}"
            f"{request.request_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER1_RESIDUAL_REQUEST_FILENAME,
        content=serialize_av1_validation_v3_tier1_residual_request(request),
        artifact_id=request.request_id,
        description="residual-probe request",
    )


def publish_av1_validation_v3_tier1_residual_grant(
    *,
    grant: AV1ValidationV3Tier1ResidualGrant,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier1ResidualPublicationResult:
    return _publish(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER1_RESIDUAL_GRANT_DIRECTORY_PREFIX}"
            f"{grant.request_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER1_RESIDUAL_GRANT_FILENAME,
        content=serialize_av1_validation_v3_tier1_residual_grant(grant),
        artifact_id=grant.grant_id,
        description="residual-probe grant",
    )


def load_published_av1_validation_v3_tier1_residual_grant(
    *,
    output_root: Path,
    repository_root: Path,
    request_id: str,
) -> AV1ValidationV3Tier1ResidualGrant:
    raw = load_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER1_RESIDUAL_GRANT_DIRECTORY_PREFIX}{request_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER1_RESIDUAL_GRANT_FILENAME,
        maximum_size=1 << 20,
    )
    try:
        grant = av1_validation_v3_tier1_residual_grant_from_payload(
            object_dict(json.loads(raw.decode("utf-8")))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 1 residual-probe grant is invalid",
        ) from exc
    if raw != serialize_av1_validation_v3_tier1_residual_grant(grant):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 1 residual-probe grant is not canonical",
        )
    return grant


def publish_av1_validation_v3_tier1_residual_claim(
    *,
    claim: AV1ValidationV3Tier1ResidualClaim,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier1ResidualPublicationResult:
    try:
        return _publish(
            output_root=output_root,
            repository_root=repository_root,
            final_name=(
                f"{AV1_VALIDATION_V3_TIER1_RESIDUAL_CLAIM_DIRECTORY_PREFIX}"
                f"{claim.grant_id}"
            ),
            filename=AV1_VALIDATION_V3_TIER1_RESIDUAL_CLAIM_FILENAME,
            content=serialize_av1_validation_v3_tier1_residual_claim(claim),
            artifact_id=claim.claim_id,
            description="residual-probe claim",
        )
    except AV1ValidationV3Tier1PublicationError as exc:
        if exc.reason_code == "artifact_conflict":
            raise AV1ValidationV3Tier1PublicationError(
                "execution_already_claimed",
                "AV1 v3 Tier 1 residual-probe claim is already consumed",
            ) from exc
        raise


def publish_av1_validation_v3_tier1_residual_result(
    *,
    result: AV1ValidationV3Tier1ResidualResult,
    output_root: Path,
    repository_root: Path,
) -> AV1ValidationV3Tier1ResidualPublicationResult:
    return _publish(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER1_RESIDUAL_RESULT_DIRECTORY_PREFIX}"
            f"{result.grant_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER1_RESIDUAL_RESULT_FILENAME,
        content=serialize_av1_validation_v3_tier1_residual_result(result),
        artifact_id=result.result_id,
        description="residual-probe result",
    )


def load_published_av1_validation_v3_tier1_residual_result(
    *,
    output_root: Path,
    repository_root: Path,
    grant_id: str,
) -> AV1ValidationV3Tier1ResidualResult:
    raw = load_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=(
            f"{AV1_VALIDATION_V3_TIER1_RESIDUAL_RESULT_DIRECTORY_PREFIX}{grant_id}"
        ),
        filename=AV1_VALIDATION_V3_TIER1_RESIDUAL_RESULT_FILENAME,
        maximum_size=1 << 20,
    )
    try:
        result = av1_validation_v3_tier1_residual_result_from_payload(
            object_dict(json.loads(raw.decode("utf-8")))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 1 residual-probe result is invalid",
        ) from exc
    if raw != serialize_av1_validation_v3_tier1_residual_result(result):
        raise AV1ValidationV3Tier1PublicationError(
            "artifact_malformed",
            "AV1 v3 Tier 1 residual-probe result is not canonical",
        )
    return result


def _publish(
    *,
    output_root: Path,
    repository_root: Path,
    final_name: str,
    filename: str,
    content: bytes,
    artifact_id: str,
    description: str,
) -> AV1ValidationV3Tier1ResidualPublicationResult:
    directory, created = publish_av1_validation_v3_owner_artifact(
        output_root=output_root,
        repository_root=repository_root,
        final_name=final_name,
        filename=filename,
        content=content,
        description=description,
    )
    return AV1ValidationV3Tier1ResidualPublicationResult(
        directory=directory,
        artifact_path=directory / filename,
        artifact_id=artifact_id,
        created=created,
    )

