from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3 import AV1ValidationProtocolV3
from mediaforce.tuning.av1_validation_v3_tier2_inventory import (
    AV1ValidationV3Tier2Inventory,
    load_av1_validation_v3_tier2_inventory,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_authorization import (
    AV1ValidationV3Tier2InventoryReadContext,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_publication import (
    AV1ValidationV3Tier2InventoryReadClaimPublicationResult,
    publish_av1_validation_v3_tier2_inventory_read_claim,
)


class AV1ValidationV3Tier2InventoryOperationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier2InventoryReadOperationResult:
    summary: Mapping[str, Any]

    def __post_init__(self) -> None:
        assert_av1_cold_start_public_payload_safe(self.summary)

    def to_public_summary(self) -> dict[str, Any]:
        return dict(self.summary)


def run_av1_validation_v3_tier2_inventory_read(
    connection: DBClient,
    *,
    config: MediaforceConfig,
    protocol: AV1ValidationProtocolV3,
    read_context: AV1ValidationV3Tier2InventoryReadContext,
    config_snapshot_bytes: bytes,
    output_root: Path,
    repository_root: Path,
    clock: Callable[[], str],
    adapter: Callable[
        ..., AV1ValidationV3Tier2Inventory
    ] = load_av1_validation_v3_tier2_inventory,
) -> AV1ValidationV3Tier2InventoryReadOperationResult:
    claim_publication = publish_av1_validation_v3_tier2_inventory_read_claim(
        claim=read_context.claim,
        output_root=output_root,
        repository_root=repository_root,
    )
    inventory = adapter(
        connection,
        config=config,
        protocol=protocol,
        read_context=read_context,
        config_snapshot_bytes=config_snapshot_bytes,
        clock=clock,
    )
    if not isinstance(inventory, AV1ValidationV3Tier2Inventory):
        raise AV1ValidationV3Tier2InventoryOperationError(
            "AV1 v3 Tier 2 inventory adapter returned an invalid result"
        )
    return AV1ValidationV3Tier2InventoryReadOperationResult(
        summary=_public_summary(
            claim_publication=claim_publication,
            inventory=inventory,
        ),
    )


def _public_summary(
    *,
    claim_publication: AV1ValidationV3Tier2InventoryReadClaimPublicationResult,
    inventory: AV1ValidationV3Tier2Inventory,
) -> dict[str, Any]:
    summary = {
        "artifact_kind": "tier2_inventory_read_summary",
        "gate": "A0",
        "tier": "tier2",
        "request_id": claim_publication.claim.request_id,
        "grant_id": claim_publication.claim.grant_id,
        "claim_id": claim_publication.claim.claim_id,
        "read_claim_published": True,
        "read_claim_created": claim_publication.created,
        "private_inventory_read_authorized": True,
        "single_read_claimed": True,
        "measured_row_count": inventory.measured_row_count,
        "frozen_stratum_count": len(inventory.frozen_stratum_private_counts),
        "incompatible_evidence_count": inventory.incompatible_evidence_count,
        "malformed_identity_count": inventory.malformed_identity_count,
        "duplicate_source_identity_row_count": (
            inventory.duplicate_source_identity_row_count
        ),
        "missing_evidence_summary_count": inventory.missing_evidence_summary_count,
        "projection_error_count": inventory.projection_error_count,
        "ambiguous_trait_count": inventory.ambiguous_trait_count,
        "non_tier2_trait_count": inventory.non_tier2_trait_count,
        "powered_candidate_cell_overlap_count": (
            inventory.powered_candidate_cell_overlap_count
        ),
        "non_balanced_intent_count": inventory.non_balanced_intent_count,
        "unconfirmed_intent_count": inventory.unconfirmed_intent_count,
        "incomplete_quality_contract_count": (
            inventory.incomplete_quality_contract_count
        ),
        "infeasible_stream_budget_count": inventory.infeasible_stream_budget_count,
        "tier2_execution_authorized": False,
        "media_read_authorized": False,
        "private_inventory_serialization_authorized": False,
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
